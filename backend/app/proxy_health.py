"""Salud del proxy de salida (spec proxy-autoheal): al arrancar, Vision verifica que su salida a internet responda ANTES de usarla y, si la guardada murio, elige otra viva.

Por que existe (incidente real, 2026-10-18): la base cifrada guardaba `http://19.12.64.237:83`, un egress de la empresa que quedo caido (el valor venia del `.env` que se importo UNA vez en la
0.27.0), y el Vision instalado no cargaba los modelos: `msal`/`requests` conectaban a la IP muerta y la persona veia un error ilegible. Ademas el DNS de la empresa alterna
`proxy.empresa.com` entre una IP viva y esa misma muerta, o sea que hasta el default del SDK del proveedor de IA falla por turnos.

El listado de candidatos se arma EN VIVO, nunca desde una lista fija en el codigo (decision del usuario, D1): 1) el proxy guardado, 2) todas las IPs que hoy resuelve el nombre del
proxy de la empresa, 3) lo que dice la PAC que Windows ya tiene configurada (la mantiene la empresa, no Vision) y 4) salir directo. La prueba es TCP crudo (`socket.create_connection`), sin proxy y sin
credenciales: solo responde «¿este egress esta vivo?». Un nombre que rota entre IPs solo cuenta como sano si TODAS sus IPs responden.

Garantias: nada de aca lanza ni bloquea el arranque (todo vive en un hilo con plazo); una variable de entorno del SISTEMA manda sobre todo (no se prueba ni se cambia); si no habia proxy
guardado la base no se escribe nunca; la base solo se toca si ya esta activa (activarla por esto ocultaria un `.env` sin importar); una contrasena dentro de la direccion nunca sale en un
aviso, un log ni la pantalla; los avisos son frases para una persona (guardian de jerga de `tests/test_friendly_errors.py`)."""

from __future__ import annotations

import ctypes
import ipaddress
import logging
import os
import re
import socket
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urlsplit

from app import config
from app import config_store as cs
from app import general_settings as gs

logger = logging.getLogger(__name__)

# Sin proxy predeterminado de fabrica (la version de origen traia el de su empresa): solo cuenta el que la persona guardo, la PAC y salir directo.
DEFAULT_PROXY = ""
# Excepciones del proxy que se agregan solas al curar: ninguna de fabrica (cada empresa sabe las suyas; localhost ya va siempre directo).
_FALLBACK_NO_PROXY = ""
# Destino de internet que sirve para saber si «directo» funciona (un servicio publico grande, siempre arriba).
DIRECT_PROBE = ("login.microsoftonline.com", 443)
# Host sobre el que se le pide a la PAC de Windows el proxy de salida.
_PAC_PROBE_URL = "https://login.microsoftonline.com/"

PROBE_TIMEOUT = 2.5  # segundos por conexion de prueba
DEADLINE = 15.0  # plazo total de un chequeo (los candidatos se prueban en paralelo)
WATCH_INTERVAL = 20.0
WATCH_MAX_SECONDS = 300.0

# Nombre de host valido (etiquetas de letras, numeros y guiones separadas por puntos): una frase con espacios no es una direccion.
_HOSTNAME_RE = re.compile(
    r"(?=.{1,253}$)[a-z0-9_]([a-z0-9_-]{0,61}[a-z0-9_])?(\.[a-z0-9_]([a-z0-9_-]{0,61}[a-z0-9_])?)*", re.IGNORECASE
)

_LOCK = threading.RLock()
_STATE: dict = {"result": None, "candidates": [], "checked_at": None}
_WATCH_THREAD: threading.Thread | None = None


# ---------------------------------------------------------------------------------------------------------------------
# Direcciones
# ---------------------------------------------------------------------------------------------------------------------
def normalize(raw: object) -> str | None:
    """`esquema://host:puerto` en minuscula y sin usuario, ruta ni barra final; None si no es una direccion de proxy usable."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    if "://" not in text:
        text = "http://" + text
    try:
        parts = urlsplit(text)
        host = parts.hostname
        port = parts.port
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not host or not (_is_ip(host) or _HOSTNAME_RE.fullmatch(host)):
        return None
    if port is None:
        port = 443 if parts.scheme == "https" else 80
    if not 1 <= port <= 65_535:
        return None
    shown = f"[{host}]" if ":" in host else host
    return f"{parts.scheme}://{shown.lower()}:{port}"


def _userinfo(raw: str) -> str:
    """El `usuario:clave@` de una direccion (o vacio): se conserva al cambiar de proxy, pero NUNCA se muestra."""
    try:
        parts = urlsplit(raw if "://" in raw else "http://" + raw)
        if parts.username is None:
            return ""
        return parts.username + (":" + parts.password if parts.password else "") + "@"
    except ValueError:
        return ""


def _with_userinfo(url: str, userinfo: str) -> str:
    return url.replace("://", "://" + userinfo, 1) if userinfo and url else url


def mask(raw: str) -> str:
    """Para mostrar: la direccion normalizada, sin usuario ni clave."""
    return normalize(raw) or ""


def _short(url: str) -> str:
    """`host:puerto` sin esquema, sin usuario ni clave: es lo que se nombra en los avisos. Una direccion guardada a mano y rota (puerto fuera de rango, texto suelto) no
    tiene `host:puerto` que mostrar: se nombra como «una direccion no valida», y NUNCA se copia su texto (podria traer una clave)."""
    normalized = normalize(url)
    if normalized is None:
        return "una dirección no válida"
    parts = urlsplit(normalized)
    host = f"[{parts.hostname}]" if parts.hostname and ":" in parts.hostname else parts.hostname
    return f"{host}:{parts.port}"


def _split(url: str) -> tuple[str, int]:
    parts = urlsplit(url)
    return str(parts.hostname), int(parts.port or 0)


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _same(a: str, b: str) -> bool:
    na, nb = normalize(a), normalize(b)
    return na is not None and na == nb


def _default_no_proxy() -> str:
    return _FALLBACK_NO_PROXY


# ---------------------------------------------------------------------------------------------------------------------
# El mundo (se reemplaza en los tests): TCP, DNS y PAC
# ---------------------------------------------------------------------------------------------------------------------
def _tcp_alive(host: str, port: int) -> float | None:
    """Segundos que tardo en abrir la conexion TCP, o None si no respondio en `PROBE_TIMEOUT`. No manda nada por la conexion."""
    started = time.monotonic()
    try:
        sock = socket.create_connection((host, port), timeout=PROBE_TIMEOUT)
    except OSError:
        return None
    sock.close()
    return time.monotonic() - started


def _resolve(host: str) -> list[str]:
    """Todas las IPv4 que resuelve hoy `host`, sin repetir y en el orden del DNS. Lanza si no resuelve."""
    seen: list[str] = []
    for info in socket.getaddrinfo(host, None, family=socket.AF_INET, type=socket.SOCK_STREAM):
        ip = str(info[4][0])
        if ip not in seen:
            seen.append(ip)
    return seen


def _pac_proxies() -> list[str]:
    """Los proxies que la PAC de Windows (la de la VPN corporativa en la empresa) indica HOY para salir a internet: `host:puerto`. Lista vacia si no hay PAC, si dice DIRECT o fuera de Windows.

    La evalua el propio Windows (WinHTTP): no se baja ni se interpreta JavaScript aca. Los proxies de loopback (el agente local de la VPN corporativa) no cuentan: no son una salida de la empresa."""
    if sys.platform != "win32":
        return []
    import winreg  # import diferido: solo existe en Windows

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
        ) as key:
            pac_url = str(winreg.QueryValueEx(key, "AutoConfigURL")[0]).strip()
    except OSError:
        return []
    if not pac_url:
        return []
    return _ask_windows_pac(pac_url, _PAC_PROBE_URL)


def _ask_windows_pac(pac_url: str, probe_url: str) -> list[str]:
    import ctypes.wintypes as wt  # import diferido: solo Windows

    class AutoProxyOptions(ctypes.Structure):
        _fields_ = [
            ("dwFlags", wt.DWORD),
            ("dwAutoDetectFlags", wt.DWORD),
            ("lpszAutoConfigUrl", wt.LPCWSTR),
            ("lpvReserved", ctypes.c_void_p),
            ("dwReserved", wt.DWORD),
            ("fAutoLogonIfChallenged", wt.BOOL),
        ]

    class ProxyInfo(ctypes.Structure):
        # `c_void_p` y NO `LPWSTR`: con `LPWSTR` ctypes convierte el campo a `str` al leerlo y el puntero ORIGINAL se pierde; despues `GlobalFree` recibia una direccion invalida y corrompia el
        # heap (el proceso moria a los ~6 s con 0xc0000374, sin traceback). Bug de la 0.33.0/0.33.1, que solo aparece cuando la PAC entrega un proxy (con DIRECT no hay nada que liberar).
        _fields_ = [("dwAccessType", wt.DWORD), ("lpszProxy", ctypes.c_void_p), ("lpszProxyBypass", ctypes.c_void_p)]

    winhttp = ctypes.WinDLL("winhttp", use_last_error=True)  # type: ignore[attr-defined]
    winhttp.WinHttpOpen.restype = ctypes.c_void_p
    winhttp.WinHttpOpen.argtypes = [wt.LPCWSTR, wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD]
    winhttp.WinHttpGetProxyForUrl.argtypes = [
        ctypes.c_void_p,
        wt.LPCWSTR,
        ctypes.POINTER(AutoProxyOptions),
        ctypes.POINTER(ProxyInfo),
    ]
    winhttp.WinHttpCloseHandle.argtypes = [ctypes.c_void_p]
    session = winhttp.WinHttpOpen("ULTRON", 1, None, None, 0)  # 1 = sin proxy propio
    if not session:
        return []
    try:
        options = AutoProxyOptions(0x2, 0, pac_url, None, 0, False)  # 0x2 = evaluar la PAC de esa direccion
        info = ProxyInfo()
        if not winhttp.WinHttpGetProxyForUrl(session, probe_url, ctypes.byref(options), ctypes.byref(info)):
            return []
        # Se lee el texto ANTES de liberar, con el puntero original (`c_void_p` -> int), y se libera ese MISMO puntero (WinHTTP lo reserva con GlobalAlloc).
        text = ctypes.wstring_at(info.lpszProxy) if info.lpszProxy else ""
        kernel32 = ctypes.WinDLL("kernel32")  # type: ignore[attr-defined]
        kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
        kernel32.GlobalFree.restype = ctypes.c_void_p
        for pointer in (info.lpszProxy, info.lpszProxyBypass):
            if pointer:
                kernel32.GlobalFree(pointer)
    finally:
        winhttp.WinHttpCloseHandle(session)
    out: list[str] = []
    for item in re.split(r"[;\s]+", text):
        entry = item.split("=", 1)[-1].strip()
        norm = normalize(entry)
        if not norm:
            continue
        host, _ = _split(norm)
        if host in ("localhost", "::1") or host.startswith("127."):
            continue
        if entry not in out:
            out.append(entry)
    return out


# ---------------------------------------------------------------------------------------------------------------------
# Candidatos
# ---------------------------------------------------------------------------------------------------------------------
@dataclass
class Candidate:
    source: str  # guardado | predeterminado | dns | pac | directo
    url: str  # "" = directo
    alive: bool | None = None  # None = sin probar
    ms: float | None = None


def build_candidates(saved: str) -> list[Candidate]:
    """El listado EN VIVO, en orden fijo y sin repetidos: guardado (o el predeterminado, si hubiera) -> IPs actuales de su nombre en el DNS -> PAC de Windows -> directo."""
    out: list[Candidate] = []
    seen: set[str] = set()

    def add(source: str, url: str | None) -> None:
        if url and url not in seen:
            seen.add(url)
            out.append(Candidate(source, url))

    if saved.strip():
        add("guardado", normalize(saved))
    elif DEFAULT_PROXY:
        add("predeterminado", normalize(DEFAULT_PROXY))
    # Con un predeterminado se siguen las IPs de SU nombre (como la version de origen); sin el, las del proxy guardado si es un nombre.
    base = DEFAULT_PROXY or saved.strip()
    if base:
        host, port = _split(base)
        try:
            if not _is_ip(host):
                for ip in _resolve(host):
                    add("dns", normalize(f"{ip}:{port}"))
        except Exception:  # noqa: BLE001 -- sin DNS el listado sigue con lo demas
            logger.warning("No se pudo consultar el DNS de la salida a internet", exc_info=True)
    try:
        for entry in _pac_proxies():
            add("pac", normalize(entry))
    except Exception:  # noqa: BLE001 -- sin PAC (no es Windows, sin VPN) el listado sigue con lo demas
        logger.warning("No se pudo leer la PAC de Windows", exc_info=True)
    out.append(Candidate("directo", ""))
    return out


def _probe(cand: Candidate) -> None:
    """Prueba un candidato y deja `alive`/`ms`. Un nombre solo es sano si TODAS sus IPs responden (hay DNS corporativos que rotan entre una viva y una muerta)."""
    try:
        if cand.source == "directo":
            ms = _tcp_alive(*DIRECT_PROBE)
        else:
            host, port = _split(cand.url)
            if _is_ip(host):
                ms = _tcp_alive(host, port)
            else:
                times = [_tcp_alive(ip, port) for ip in _resolve(host)]
                ms = max(t for t in times if t is not None) if times and all(t is not None for t in times) else None
    except Exception:  # noqa: BLE001 -- una prueba que falla es un candidato muerto, nunca un error
        ms = None
    cand.alive = ms is not None
    cand.ms = ms


def _probe_all(cands: list[Candidate]) -> None:
    """Todos en paralelo (el peor caso cuesta una espera, no la suma); lo que no termina en el plazo queda sin vida."""
    threads = [threading.Thread(target=_probe, args=(c,), daemon=True, name="vision-proxy-probe") for c in cands]
    for t in threads:
        t.start()
    limit = time.monotonic() + max(0.05, DEADLINE - 0.1)
    for t in threads:
        t.join(max(0.0, limit - time.monotonic()))
    for c in cands:
        if c.alive is None:
            c.alive = False


# ---------------------------------------------------------------------------------------------------------------------
# Resultado y avisos
# ---------------------------------------------------------------------------------------------------------------------
@dataclass
class Result:
    action: str  # ok | system | chosen | healed | direct | none | error
    message: str = ""  # frase para la persona ("" = nada que decir)
    chosen: Candidate | None = None
    candidates: list[Candidate] = field(default_factory=list)
    persisted: bool = False


_NONE_MESSAGE = (
    "No hay conexión a internet disponible. Conectá la VPN de tu empresa, si usás una y probá de nuevo desde Configuración > Red. "
    "ULTRON sigue abierto y la busca solo durante unos minutos, pero el chat con inteligencia artificial no va a funcionar hasta que vuelva."
)


def _message(action: str, old: str, new: str, persisted: bool) -> str:
    if action == "healed":
        tail = "y lo dejó guardado en Configuración > Red." if persisted else "durante esta sesión."
        return f"La salida a internet que ULTRON tenía guardada ({_short(old)}) dejó de responder. ULTRON pasó a usar {_short(new)}, que sí responde, {tail}"
    if action == "chosen":
        return (
            f"La salida a internet de la empresa por su nombre ({_short(DEFAULT_PROXY)}) apunta a una dirección que no responde. "
            f"ULTRON usa {_short(new)}, que sí responde, durante esta sesión."
        )
    if action == "direct":
        if old:
            return (
                f"La salida a internet que ULTRON tenía guardada ({_short(old)}) no responde y ninguna otra respondió, "
                "pero hay conexión directa. ULTRON la usa por ahora y se conserva lo guardado, por si en otra red sí sirve."
            )
        return (
            "Ninguna salida a internet de la empresa responde, pero hay conexión directa. ULTRON la usa durante esta sesión."
        )
    return _NONE_MESSAGE


# ---------------------------------------------------------------------------------------------------------------------
# Lo guardado y lo aplicado
# ---------------------------------------------------------------------------------------------------------------------
def _saved_proxy() -> str:
    if cs.is_active():
        return (cs.get_general().get("https_proxy") or "").strip()
    # Base inactiva: `Settings` lee `HTTPS_PROXY` del entorno; un predeterminado puesto por una libreria no es algo que la persona haya guardado.
    value = (config.get_settings().https_proxy or "").strip()
    return "" if _is_sdk_default(value) else value


def _is_sdk_default(value: str | None) -> bool:
    return bool(DEFAULT_PROXY) and value is not None and _same(value, DEFAULT_PROXY)


def _forget_sdk_default_env() -> None:
    """Un predeterminado que una libreria puso en el entorno NO es una decision del sistema y estorba al aplicar la cura."""
    for var in ("HTTP_PROXY", "HTTPS_PROXY"):
        if _is_sdk_default(os.environ.get(var)) and not config._vision_owns(var):
            os.environ.pop(var, None)


def _system_decides() -> bool:
    """True si el SISTEMA (variable de entorno de Windows) fija el proxy: manda sobre todo y no se toca."""
    for var in ("HTTPS_PROXY", "HTTP_PROXY"):
        value = os.environ.get(var)
        if value is None or config._vision_owns(var) or _is_sdk_default(value):
            continue
        return True
    return False


def _apply(url: str, *, old_saved: str, persist: bool) -> bool:
    """Aplica la cura al proceso (y a la base si `persist` y ya esta activa). `url` vacio = salir directo. Devuelve si quedo guardada."""
    settings = config.get_settings()
    _forget_sdk_default_env()
    userinfo = _userinfo(old_saved) if old_saved else ""
    full = _with_userinfo(url, userinfo)
    http_follows = (
        (not settings.http_proxy.strip())
        or _same(settings.http_proxy, old_saved)
        or _is_sdk_default(settings.http_proxy)
    )
    settings.https_proxy = full
    if http_follows:
        settings.http_proxy = full
    # `Settings` lee `NO_PROXY` del entorno y ahi Vision mismo dejo SOLO los hosts locales (`config._apply_proxy`): con esa lista sola no hay ninguna excepcion de la persona.
    no_proxy_added = bool(full) and bool(_default_no_proxy()) and settings.no_proxy.strip() in ("", config._LOCAL_HOSTS)
    if no_proxy_added:
        settings.no_proxy = _default_no_proxy()  # sin esto lo interno de la red saldria por el proxy
    config.reapply_proxy(settings)
    saved = False
    if persist and old_saved and cs.is_active():
        updates: dict[str, str | None] = {"https_proxy": full or None}
        if http_follows and _same(cs.get_general().get("http_proxy", ""), old_saved):
            updates["http_proxy"] = full or None
        if no_proxy_added:
            updates["no_proxy"] = settings.no_proxy
        try:
            gs.save(updates)
            saved = True
        except Exception:  # noqa: BLE001 -- no poder guardar no puede tirar abajo la cura de esta sesion
            logger.warning("La salida a internet quedo curada en esta sesion pero no se pudo guardar", exc_info=True)
    if not full:
        for var in ("HTTP_PROXY", "HTTPS_PROXY"):
            if not os.environ.get(var):
                os.environ[var] = config._VISION_SET_ENV[var] = ""  # vacio explicito: ninguna libreria pone el suyo
    return saved


# ---------------------------------------------------------------------------------------------------------------------
# El chequeo
# ---------------------------------------------------------------------------------------------------------------------
def _check_inner(persist: bool, cancelled: threading.Event) -> Result:
    if _system_decides():
        return Result("system")
    saved = _saved_proxy()
    cands = build_candidates(saved)
    _probe_all(cands)
    if cancelled.is_set():
        return Result("none", _NONE_MESSAGE, None, cands)
    chosen = next((c for c in cands if c.alive and c.source != "directo"), None)
    direct = next(c for c in cands if c.source == "directo")
    if chosen is not None:
        if chosen.source in ("guardado", "predeterminado"):
            return Result("ok", "", chosen, cands)
        saved_ok = _apply(chosen.url, old_saved=saved, persist=persist)
        action = "healed" if saved else "chosen"
        return Result(
            action, _message(action, normalize(saved) or saved, chosen.url, saved_ok), chosen, cands, saved_ok
        )
    if direct.alive:
        # 0.33.1: salir directo vale SOLO para esta sesion y NO toca lo guardado. Un proxy que hoy no responde puede ser el unico valido en otra red (la oficina de la empresa sin VPN):
        # borrarlo (como hacia la 0.33.0) obligaba a cargarlo de nuevo. Lo guardado se vuelve a probar en cada arranque y se usa apenas responde. Solo la persona lo borra,
        # eligiendo «conexion directa» en Configuracion > Red > Proxy (`apply_choice("")`).
        _apply("", old_saved=saved, persist=False)
        return Result("direct", _message("direct", normalize(saved) or "", "", False), direct, cands, False)
    return Result("none", _NONE_MESSAGE, None, cands)


def check(persist: bool = True) -> Result:
    """Verifica la salida a internet y la cura si hace falta. NUNCA lanza ni pasa del plazo (`DEADLINE`)."""
    with _LOCK:
        cancelled = threading.Event()
        box: dict[str, Result] = {}

        def run() -> None:
            try:
                box["r"] = _check_inner(persist, cancelled)
            except Exception:  # noqa: BLE001 -- el chequeo nunca puede romper el arranque
                logger.warning("El chequeo de la salida a internet fallo", exc_info=True)
                box["r"] = Result("error", _NONE_MESSAGE)

        worker = threading.Thread(target=run, daemon=True, name="vision-proxy-check")
        worker.start()
        worker.join(DEADLINE)
        if worker.is_alive():
            cancelled.set()
            logger.warning("El chequeo de la salida a internet no termino en %s segundos", DEADLINE)
            result = Result("none", _NONE_MESSAGE)
        else:
            result = box.get("r") or Result("error", _NONE_MESSAGE)
        _STATE.update(
            result=result,
            candidates=list(result.candidates),
            checked_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        )
        return result


# ---------------------------------------------------------------------------------------------------------------------
# Pantalla: foto, probar uno, guardar y reconectar
# ---------------------------------------------------------------------------------------------------------------------
def _candidate_view(c: Candidate) -> dict:
    return {
        "source": c.source,
        "url": mask(c.url),
        "alive": c.alive,
        "ms": None if c.ms is None else round(c.ms * 1000),
    }


def snapshot() -> dict:
    """Lo que ve la pestana Red > Proxy. No sale a la red: muestra lo ultimo que se chequeo."""
    result: Result | None = _STATE["result"]
    try:
        saved = mask(_saved_proxy())
    except Exception:  # noqa: BLE001 -- sin base legible la foto sigue
        saved = ""
    in_use = mask(os.environ.get("HTTPS_PROXY", ""))
    return {
        "saved": saved,
        "in_use": in_use,
        # 0.33.1: salir directo conserva lo guardado, asi que «guardada» y «en uso» pueden ser distintas A PROPOSITO; la pantalla lo explica en vez de dejar creer que es un error.
        "kept_saved": bool(saved) and saved != in_use,
        "candidates": [_candidate_view(c) for c in _STATE["candidates"]],
        "action": result.action if result else None,
        "message": result.message if result else "",
        "checked_at": _STATE["checked_at"],
        "watching": watching(),
    }


def probe_one(raw: str) -> dict:
    """Prueba UNA direccion suelta y no cambia nada. Vacio = la conexion directa. Lanza ValueError si no es una direccion usable."""
    if not str(raw or "").strip():
        cand = Candidate("directo", "")
    else:
        url = normalize(raw)
        if url is None:
            raise ValueError("Esa dirección de proxy no es válida.")
        cand = Candidate("manual", url)
    _probe(cand)
    return _candidate_view(cand)


def apply_choice(raw: str, force: bool = False) -> dict:
    """«Guardar y reconectar»: guarda la direccion elegida y la aplica ya, sin reiniciar. Una direccion que no responde se rechaza salvo `force`. Vacio = salir directo."""
    text = str(raw or "").strip()
    url = "" if not text else normalize(text)
    if url is None:
        raise ValueError("Esa dirección de proxy no es válida.")
    cand = Candidate("directo" if not url else "manual", url)
    _probe(cand)
    if not cand.alive and not force:
        return {"applied": False, "saved": False, "alive": False}
    with _LOCK:
        old = _saved_proxy()
        if url and not old:
            old = url  # primera vez: no habia nada que reemplazar, pero la eleccion de la persona se guarda
        saved = _apply(url, old_saved=old, persist=True)
    # saved=False con applied=True: vale en esta sesion pero no quedo guardado (la base cifrada todavia no esta activa: primero hay que importar en Conexiones).
    return {"applied": True, "saved": saved, "alive": bool(cand.alive)}


# ---------------------------------------------------------------------------------------------------------------------
# Vigilancia del arranque (REQ-011)
# ---------------------------------------------------------------------------------------------------------------------
def _announce(result: Result) -> None:
    if result.message:
        logger.warning("%s", result.message)


def watch_loop(
    interval: float = WATCH_INTERVAL, max_seconds: float = WATCH_MAX_SECONDS, sleep=time.sleep, clock=time.monotonic
) -> Result:
    """Chequea; si no hay ninguna salida, reintenta cada `interval` segundos hasta `max_seconds` (la VPN puede conectar despues de abrir Vision)."""
    started = clock()
    result = check()
    _announce(result)
    while result.action == "none":
        if clock() - started >= max_seconds:
            break
        sleep(interval)
        result = check()
        if result.action != "none":
            _announce(result)
    return result


def watching() -> bool:
    return _WATCH_THREAD is not None and _WATCH_THREAD.is_alive()


def start_watch() -> bool:
    """Arranca el chequeo + vigilancia en segundo plano (hilo daemon). Uno solo a la vez; False si ya habia uno."""
    global _WATCH_THREAD
    with _LOCK:
        if watching():
            return False
        _WATCH_THREAD = threading.Thread(target=watch_loop, daemon=True, name="vision-proxy-watch")
        _WATCH_THREAD.start()
        return True


def reset_for_tests() -> None:
    global _WATCH_THREAD
    _WATCH_THREAD = None
    _STATE.update(result=None, candidates=[], checked_at=None)
