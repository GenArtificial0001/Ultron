"""
Varios proveedores de IA a la vez (spec varios-proveedores).

El usuario pone en el `.env` solo la clave de cada proveedor, con un nombre fijo
(`GEMINI_API_KEY`, `GROQ_API_KEY`...): Vision ya conoce la direccion y el formato
de cada uno (REQ-001/002, decision del usuario: "es incomodisima la carga de URL
y demas parametros"). Todos hablan el formato de chat de OpenAI; Claude, por la
capa compatible de Anthropic.

Cada modelo se identifica como `proveedor/modelo` (`gemini/gemini-2.5-flash`,
`groq/openai/gpt-oss-120b`): asi el selector los agrupa y el loop sabe a que
proveedor hablarle en cada paso, incluso cuando el salto automatico cambia de
proveedor a mitad de un turno (REQ-004/005).

Compatibilidad (REQ-007): la configuracion de siempre (`LLM_PROVIDER`,
`LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODELS`, tambien cargada desde
Configuracion > Credenciales) sigue valiendo: ese es el proveedor "principal", y
un modelo sin prefijo se entiende como de ese proveedor.

El `.env` se relee solo cuando cambia (por su fecha de modificacion): agregar
una clave hace aparecer al proveedor sin reiniciar Vision (REQ-002).
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from dotenv import dotenv_values
from openai import OpenAI

from app.config import BASE_DIR, get_settings

logger = logging.getLogger(__name__)

ANTHROPIC_VERSION = "2023-06-01"
MODELS_TTL_SECONDS = 300
# Modelos que no sirven para chatear: se esconden de la lista (REQ-003).
_NOT_CHAT = (
    "embedding", "tts", "whisper", "transcribe", "image", "imagen", "veo", "guard", "moderation",
    "audio", "aqa", "orpheus", "dall-e", "sora", "realtime", "live", "learnlm", "robotics",
    # Gemini (medido 2026-09-27): musica, imagenes, agentes de investigacion y variantes especiales.
    "lyria", "nano-banana", "deep-research", "antigravity", "computer-use", "customtools", "safeguard",
)


def _newest(ids: list[str], pattern: str) -> str | None:
    """El de version mas alta que cumple `pattern` (con un grupo para la version)."""
    best: tuple[tuple[int, ...], str] | None = None
    for model in ids:
        match = re.match(pattern, model)
        if match:
            version = tuple(int(part) for part in match.group(1).split("."))
            if best is None or version > best[0]:
                best = (version, model)
    return best[1] if best else None


def _gemini_suggest(ids: list[str]) -> list[str]:
    """Los Flash (con cupo gratuito): el alias que sigue al mas nuevo, el Flash de
    version mas alta y el liviano. Los 2.5 ya no estan para cuentas nuevas y los
    Pro no tienen cupo gratuito (medido 2026-09-27); se pueden marcar a mano."""
    wanted = ("gemini-flash-latest", _newest(ids, r"^gemini-(\d+(?:\.\d+)*)-flash$"), "gemini-flash-lite-latest")
    return [w for w in wanted if w and w in ids] or ids[:4]


def _first(ids: list[str], wanted: tuple[str, ...], limit: int = 4) -> list[str]:
    """Sugeridos: los de `wanted` que existan, en ese orden; si no hay, los primeros."""
    found = [w for w in wanted if w in ids]
    return found or ids[:limit]


@dataclass(frozen=True)
class ProviderSpec:
    id: str
    label: str
    base_url: str
    key_names: tuple[str, ...]
    # "Gratis con limites": el plan gratuito puede usar lo que se le manda
    # para entrenar -> pide confirmacion antes de usarse (REQ-006).
    free: bool = False
    anthropic: bool = False
    suggest: Callable[[list[str]], list[str]] = field(default=lambda ids: ids[:4])
    # Modelos gratuitos dentro de un proveedor pago (OpenRouter: sufijo ":free").
    free_model: Callable[[str], bool] = field(default=lambda model: False)


KNOWN: dict[str, ProviderSpec] = {
    spec.id: spec
    for spec in (
        ProviderSpec(
            "anthropic", "Claude", "https://api.anthropic.com/v1/", ("ANTHROPIC_API_KEY",), anthropic=True,
            suggest=lambda ids: [i for i in ids if "-4-5" not in i and "-4-1" not in i][:4] or ids[:4],
        ),
        ProviderSpec(
            "gemini", "Gemini", "https://generativelanguage.googleapis.com/v1beta/openai/", ("GEMINI_API_KEY",),
            free=True,
            suggest=_gemini_suggest,
        ),
        ProviderSpec("deepseek", "DeepSeek", "https://api.deepseek.com", ("DEEPSEEK_API_KEY",)),
        ProviderSpec(
            "groq", "Groq", "https://api.groq.com/openai/v1", ("GROQ_API_KEY", "META_GROQ_API_KEY"), free=True,
            suggest=lambda ids: [i for i in ids if "gpt-oss" in i or "qwen" in i or "llama" in i][:4] or ids[:4],
        ),
        ProviderSpec(
            "openrouter", "OpenRouter", "https://openrouter.ai/api/v1", ("OPENROUTER_API_KEY",),
            suggest=lambda ids: [i for i in ids if i.endswith(":free")][:6] or ids[:4],
            free_model=lambda model: model.endswith(":free"),
        ),
        ProviderSpec(
            "openai", "OpenAI", "https://api.openai.com/v1", ("OPENAI_API_KEY",),
            suggest=lambda ids: [i for i in ids if i.startswith("gpt-")][:4] or ids[:4],
        ),
    )
}
CUSTOM_ID = "custom"
PRINCIPAL_ID = "principal"
PREFIXES = (*KNOWN, CUSTOM_ID, PRINCIPAL_ID)


@dataclass(frozen=True)
class ActiveProvider:
    id: str
    label: str
    base_url: str
    api_key: str
    free: bool
    anthropic: bool
    # Solo el proveedor principal: `LLM_MODELS`, si esta completa (REQ-007).
    fixed_models: tuple[str, ...] = ()

    @property
    def headers(self) -> dict[str, str] | None:
        # La capa compatible de Anthropic acepta la clave como Bearer; su
        # GET /models la pide en x-api-key.
        if self.anthropic:
            return {"x-api-key": self.api_key, "anthropic-version": ANTHROPIC_VERSION}
        return None

    def spec(self) -> ProviderSpec | None:
        return KNOWN.get(self.id)


# ---------------------------------------------------------------------------
# REQ-002: claves del `.env`, releido cuando cambia.
# ---------------------------------------------------------------------------
_ENV_LOCK = threading.Lock()
_ENV_CACHE: dict[str, Any] = {"mtime": None, "values": {}}


def _env_file_values() -> dict[str, str]:
    path = BASE_DIR / ".env"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    with _ENV_LOCK:
        if _ENV_CACHE["mtime"] != mtime:
            _ENV_CACHE["values"] = {k: (v or "") for k, v in dotenv_values(path).items()}
            _ENV_CACHE["mtime"] = mtime
        return dict(_ENV_CACHE["values"])


def env_value(name: str) -> str:
    """Valor de una variable: el entorno del proceso manda; si no, el `.env`."""
    return (os.environ.get(name) or _env_file_values().get(name) or "").strip()


def _host_label(base_url: str) -> str:
    return urlparse(base_url).netloc or base_url


def _principal() -> ActiveProvider | None:
    """La configuracion de siempre (LLM_*), si esta completa (REQ-007)."""
    settings = get_settings()
    kind = settings.llm_provider.strip().lower()
    key = settings.llm_api_key.strip()
    base_url = settings.llm_base_url.strip()
    if not key or kind not in ("anthropic", "openai_compatible"):
        return None
    fixed = tuple(settings.llm_model_list)
    if kind == "anthropic":
        spec = KNOWN["anthropic"]
        return ActiveProvider("anthropic", spec.label, base_url or spec.base_url, key, False, True, fixed)
    if not base_url:
        return None
    host = urlparse(base_url).netloc
    for spec in KNOWN.values():
        if urlparse(spec.base_url).netloc == host:
            return ActiveProvider(spec.id, spec.label, base_url, key, spec.free, spec.anthropic, fixed)
    return ActiveProvider(PRINCIPAL_ID, _host_label(base_url), base_url, key, False, False, fixed)


def active_providers() -> dict[str, ActiveProvider]:
    """Proveedores con clave, en el orden de la tabla de la spec."""
    result: dict[str, ActiveProvider] = {}
    principal = _principal()
    for spec in KNOWN.values():
        key = next((env_value(name) for name in spec.key_names if env_value(name)), "")
        if key:
            fixed = principal.fixed_models if principal and principal.id == spec.id else ()
            result[spec.id] = ActiveProvider(spec.id, spec.label, spec.base_url, key, spec.free, spec.anthropic, fixed)
        elif principal is not None and principal.id == spec.id:
            result[spec.id] = principal
    custom_key, custom_url = env_value("CUSTOM_API_KEY"), env_value("CUSTOM_BASE_URL")
    if custom_key and custom_url:
        label = env_value("CUSTOM_LABEL") or _host_label(custom_url)
        result[CUSTOM_ID] = ActiveProvider(CUSTOM_ID, label, custom_url, custom_key, False, False)
    if principal is not None and principal.id == PRINCIPAL_ID:
        result[PRINCIPAL_ID] = principal
    return result


def signature() -> tuple:
    """Cambia si se agrega, quita o cambia una clave (para renovar la lista de modelos)."""
    return tuple(sorted((p.id, p.base_url, hash(p.api_key), p.fixed_models) for p in active_providers().values()))


def has_principal() -> bool:
    """Si esta completa la configuracion de siempre (LLM_*)."""
    return _principal() is not None


def principal_id() -> str | None:
    principal = _principal()
    if principal is not None:
        return principal.id
    return next(iter(active_providers()), None)


# ---------------------------------------------------------------------------
# Ids "proveedor/modelo" (REQ-004).
# ---------------------------------------------------------------------------
def split_model(model_id: str) -> tuple[str | None, str]:
    """("gemini", "gemini-2.5-flash") para "gemini/gemini-2.5-flash". Sin
    prefijo conocido, el modelo es del proveedor principal."""
    prefix, sep, rest = (model_id or "").partition("/")
    if sep and prefix in PREFIXES and rest:
        return prefix, rest
    return principal_id(), model_id


def canonical(model_id: str) -> str:
    provider_id, name = split_model(model_id)
    return f"{provider_id}/{name}" if provider_id else name


def api_model(model_id: str) -> str:
    """El nombre que entiende el proveedor (sin el prefijo de Vision)."""
    return split_model(model_id)[1]


def provider_of(model_id: str) -> str | None:
    return split_model(model_id)[0]


def display_name(model_id: str) -> str:
    """Como se nombra un modelo en los avisos: "gemini-2.5-flash (Gemini)"; los
    del proveedor principal u "otro compatible", solo por su nombre (como antes)."""
    prefix, sep, rest = (model_id or "").partition("/")
    if not sep or not rest:
        return model_id
    if prefix in (PRINCIPAL_ID, CUSTOM_ID) or (has_principal() and prefix == principal_id()):
        return rest
    spec = KNOWN.get(prefix)
    return f"{rest} ({spec.label})" if spec is not None else model_id


def is_free(provider_id: str | None, name: str) -> bool:
    spec = KNOWN.get(provider_id or "")
    return bool(spec and (spec.free or spec.free_model(name)))


class ProviderUnavailable(RuntimeError):
    """El modelo pedido es de un proveedor sin clave en el `.env`."""


_CLIENTS: dict[tuple, OpenAI] = {}
_CLIENTS_LOCK = threading.Lock()


def client_for(model_id: str, force_refresh: bool = False) -> OpenAI:
    """Cliente del proveedor de `model_id`, reusado mientras su clave no cambie."""
    return client_for_provider(provider_of(model_id), force_refresh=force_refresh, model_id=model_id)


def client_for_provider(provider_id: str | None, force_refresh: bool = False, model_id: str = "") -> OpenAI:
    provider = active_providers().get(provider_id or "")
    if provider is None:
        detail = f" (modelo {model_id})" if model_id else ""
        raise ProviderUnavailable(f"No hay clave para el proveedor '{provider_id}' en el .env{detail}.")
    key = (provider.base_url, provider.api_key)
    with _CLIENTS_LOCK:
        if force_refresh or key not in _CLIENTS:
            _CLIENTS[key] = OpenAI(api_key=provider.api_key, base_url=provider.base_url, default_headers=provider.headers)
        return _CLIENTS[key]


# ---------------------------------------------------------------------------
# REQ-003: modelos de cada proveedor.
# ---------------------------------------------------------------------------
_MODELS: dict[tuple, tuple[float, list[str]]] = {}


def chat_models(ids: list[str]) -> list[str]:
    return [i for i in ids if not any(word in i.lower() for word in _NOT_CHAT)]


def provider_models(provider: ActiveProvider) -> list[str]:
    """Modelos de chat del proveedor (cache de 5 minutos por clave)."""
    if provider.fixed_models:
        return list(provider.fixed_models)
    key = (provider.id, provider.base_url, hash(provider.api_key))
    cached = _MODELS.get(key)
    if cached and time.monotonic() - cached[0] < MODELS_TTL_SECONDS:
        return list(cached[1])
    client = OpenAI(api_key=provider.api_key, base_url=provider.base_url, default_headers=provider.headers)
    ids = [str(m.id).removeprefix("models/") for m in client.models.list()]
    models = chat_models(ids)
    _MODELS[key] = (time.monotonic(), models)
    return list(models)


def suggested_models(provider: ActiveProvider, ids: list[str]) -> list[str]:
    spec = provider.spec()
    return spec.suggest(ids) if spec else ids[:4]


def clear_caches() -> None:
    with _ENV_LOCK:
        _ENV_CACHE["mtime"] = None
    _MODELS.clear()
    with _CLIENTS_LOCK:
        _CLIENTS.clear()
