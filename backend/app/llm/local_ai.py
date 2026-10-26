"""IA local en la propia PC (spec ia-local): Ollama y LM Studio como proveedores de ULTRON.

Por que existe: con un modelo descargado no hay costo por uso, no hay cupos y lo que se le manda no sale de la maquina. Los dos
motores hablan el formato de OpenAI (`/v1`), asi que el chat, las herramientas y el streaming de siempre funcionan igual; lo
nuevo es encontrarlos solos (REQ-001), sin frenar a ULTRON cuando estan apagados (REQ-002), y saber su ventana de contexto
(REQ-011): Ollama arranca con 4.096 tokens y recorta el principio del pedido SIN avisar.

Reglas:
- Sin configurar nada: se busca cada motor en su direccion de fabrica. `OLLAMA_BASE_URL` / `LMSTUDIO_BASE_URL` la cambian (otra
  PC de la red, otro puerto). `LOCAL_AI=off` apaga la busqueda.
- Cada consulta tiene un tope de `PROBE_TIMEOUT` s y su resultado se recuerda (`ALIVE_TTL` si responde, `DEAD_TTL` si no): un
  motor apagado no suma 1,5 s a cada apertura del selector.
- Las direcciones salen SOLO del entorno o del `.env` (nunca de la pantalla) y se validan: `http(s)://host[:puerto]`, sin
  usuario ni clave embebidos.
- Las consultas van SIEMPRE directo (sin proxy): un motor local nunca esta detras del proxy de la empresa.
- Solo libreria estandar (urllib): no suma dependencias.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

PROBE_TIMEOUT = 1.5  # REQ-002: segundos como mucho por motor
ALIVE_TTL = 300.0  # un motor que respondio se vuelve a consultar a los 5 min (igual que la lista de modelos)
DEAD_TTL = 30.0  # uno apagado, a los 30 s (asi aparece solo poco despues de abrirlo)
CONTEXT_TTL = 60.0  # la ventana de un modelo cargado puede cambiar al recargarlo
OFF_VALUES = {"off", "0", "false", "no", "apagado"}


@dataclass(frozen=True)
class LocalEngine:
    id: str
    label: str
    default_url: str
    url_env: str


ENGINES: tuple[LocalEngine, ...] = (
    LocalEngine("ollama", "Ollama (local)", "http://127.0.0.1:11434/v1", "OLLAMA_BASE_URL"),
    LocalEngine("lmstudio", "LM Studio (local)", "http://127.0.0.1:1234/v1", "LMSTUDIO_BASE_URL"),
)
BY_ID = {e.id: e for e in ENGINES}

_LOCK = threading.Lock()
_ALIVE: dict[str, tuple[float, bool]] = {}
_CONTEXT: dict[tuple[str, str], tuple[float, int | None]] = {}


def enabled(env_value: Callable[[str], str]) -> bool:
    """REQ-001: la busqueda esta prendida salvo `LOCAL_AI=off`."""
    return env_value("LOCAL_AI").strip().lower() not in OFF_VALUES


def normalize_url(raw: str) -> str | None:
    """`http(s)://host[:puerto][/ruta]` sin barra final, o None si no es valida o trae usuario/clave (nunca se usa ni se muestra)."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        parts = urlsplit(raw)
        port = parts.port  # valida el puerto (lanza ValueError si no es un numero valido)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
        return None
    del port
    return raw.rstrip("/")


def base_url(engine: LocalEngine, env_value: Callable[[str], str]) -> str | None:
    """La direccion a usar: la del entorno/.env si hay una valida; si no, la de fabrica. Una invalida se ignora con un aviso en el log."""
    configured = env_value(engine.url_env)
    if configured:
        url = normalize_url(configured)
        if url is None:
            logger.warning("%s no es una direccion valida (se usa la de fabrica de %s)", engine.url_env, engine.label)
        else:
            return url
    return engine.default_url


def is_on_this_pc(url: str) -> bool:
    """REQ-005: True si el motor corre en esta misma PC (lo que se le manda no sale de la maquina)."""
    host = (urlsplit(url).hostname or "").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _direct_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))  # sin proxy: el motor es local


def _get_json(url: str, timeout: float) -> object:
    with _direct_opener().open(urllib.request.Request(url, headers={"Accept": "application/json"}), timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def is_alive(url: str, now: Callable[[], float] | None = None) -> bool:
    """REQ-001/002: ¿responde `GET <url>/models`? Con tope de `PROBE_TIMEOUT` y recordado `ALIVE_TTL`/`DEAD_TTL`. Nunca lanza."""
    t = (now or time.monotonic)()
    with _LOCK:
        cached = _ALIVE.get(url)
        if cached is not None:
            checked, alive = cached
            if t - checked < (ALIVE_TTL if alive else DEAD_TTL):
                return alive
    try:
        _get_json(f"{url}/models", PROBE_TIMEOUT)
        alive = True
    except Exception:  # noqa: BLE001 -- apagado, puerto equivocado, respuesta rara: simplemente no aparece
        alive = False
    with _LOCK:
        _ALIVE[url] = (t, alive)
    return alive


def detected(env_value: Callable[[str], str]) -> list[tuple[LocalEngine, str]]:
    """Los motores locales que hoy responden, con su direccion. Lista vacia si `LOCAL_AI=off`."""
    if not enabled(env_value):
        return []
    found = []
    for engine in ENGINES:
        url = base_url(engine, env_value)
        if url and is_alive(url):
            found.append((engine, url))
    return found


def _root(url: str) -> str:
    """`http://host:puerto` (sin el `/v1`): las APIs propias de cada motor cuelgan de ahi."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def context_length(engine_id: str, url: str, model: str, now: Callable[[], float] | None = None) -> int | None:
    """REQ-011: la ventana de contexto con la que el motor tiene CARGADO el modelo (None si no esta cargado o no se sabe).

    - Ollama: `GET /api/ps` -> `models[].context_length` (lo que muestra `ollama ps`, columna CONTEXT).
    - LM Studio: `GET /api/v0/models` -> `data[].loaded_context_length`.
    Recordado `CONTEXT_TTL` segundos. Nunca lanza."""
    key = (url, model)
    t = (now or time.monotonic)()
    with _LOCK:
        cached = _CONTEXT.get(key)
        if cached is not None and t - cached[0] < CONTEXT_TTL:
            return cached[1]
    value: int | None = None
    try:
        if engine_id == "ollama":
            data = _get_json(f"{_root(url)}/api/ps", PROBE_TIMEOUT)
            for item in (data.get("models") or []) if isinstance(data, dict) else []:
                if model in (item.get("name"), item.get("model")):
                    value = item.get("context_length")
                    break
        elif engine_id == "lmstudio":
            data = _get_json(f"{_root(url)}/api/v0/models", PROBE_TIMEOUT)
            for item in (data.get("data") or []) if isinstance(data, dict) else []:
                if item.get("id") == model:
                    value = item.get("loaded_context_length")
                    break
    except Exception:  # noqa: BLE001 -- el dato es una ayuda: sin el, la ventana queda como desconocida
        value = None
    value = value if isinstance(value, int) and value > 0 else None
    with _LOCK:
        _CONTEXT[key] = (t, value)
    return value


LOAD_TIMEOUT = 180.0  # cargar un modelo en memoria: 5 s medidos en la PC de prueba; con disco lento o sin GPU, mucho mas


def _post_json(url: str, body: dict, timeout: float) -> object:
    data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    with _direct_opener().open(request, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace") or "{}")


def loaded_context_length(engine_id: str, url: str, model: str) -> int | None:
    """REQ-011: la ventana con la que el motor va a correr el modelo, ANTES de mandarle el pedido.

    Si Ollama todavia no lo cargo, `/api/ps` no lo lista y no hay forma de saberla: se carga con `POST /api/generate` sin
    `prompt` (la forma documentada de cargarlo sin generar nada) y se vuelve a preguntar. La espera no se pierde: el pedido
    real la pagaria igual. LM Studio carga los modelos recien al recibir el pedido; sin cargar, queda desconocida. Nunca lanza."""
    value = context_length(engine_id, url, model)
    if value is not None or engine_id != "ollama":
        return value
    try:
        _post_json(f"{_root(url)}/api/generate", {"model": model}, LOAD_TIMEOUT)
    except Exception:  # noqa: BLE001 -- si no se pudo cargar, el pedido real dara el error con su explicacion
        logger.warning("No se pudo cargar %s en Ollama para leer su ventana de contexto", model, exc_info=True)
        return None
    with _LOCK:
        _CONTEXT.pop((url, model), None)  # el «desconocida» de antes de cargarlo ya no vale
    return context_length(engine_id, url, model)


def ensure_direct(url: str) -> None:
    """Si hay proxy en el entorno, agrega el host del motor a `NO_PROXY` (httpx, el transporte del cliente de OpenAI, lo respeta).
    localhost ya va siempre directo (`config._apply_proxy`); esto cubre un motor en otra PC de la red."""
    import os

    if not (os.environ.get("HTTP_PROXY") or os.environ.get("HTTPS_PROXY")):
        return
    host = urlsplit(url).hostname or ""
    current = [h.strip() for h in os.environ.get("NO_PROXY", "").split(",") if h.strip()]
    if host and host not in current:
        os.environ["NO_PROXY"] = ",".join([*current, host])


def clear_caches() -> None:
    with _LOCK:
        _ALIVE.clear()
        _CONTEXT.clear()
