"""
Cliente del proveedor de IA (spec vision-generico REQ-001/003).

Vision habla el formato de chat de OpenAI (streaming y tool-calling). El
proveedor se elige en el `.env`:

- `LLM_PROVIDER=anthropic`: Claude con clave de Anthropic, via su capa
  compatible con OpenAI (`https://api.anthropic.com/v1/`).
- `LLM_PROVIDER=openai_compatible`: OpenAI, Azure OpenAI o el gateway propio de
  una empresa. `LLM_BASE_URL` es obligatorio.

Sin clave, o sin direccion en `openai_compatible`, se corta con
`ProviderNotConfigured` (mensaje claro en el chat): si no se le pasa una
direccion, la libreria de OpenAI mandaria el pedido a su servidor publico.

Varios proveedores a la vez (spec varios-proveedores): ver `app/llm/providers.py`;
`get_client(model=...)` elige el de cada modelo.

El dictado por voz usa su propio proveedor (`STT_*`, ver `get_stt_client`).
El proxy de la empresa (`HTTP_PROXY`/`HTTPS_PROXY`/`NO_PROXY` del `.env`) lo
aplica `app/config.py` al entorno del proceso; httpx, que usa la libreria, lo
toma de ahi.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TypeVar

import openai
from openai import OpenAI

from app.config import get_settings

ANTHROPIC_BASE_URL = "https://api.anthropic.com/v1/"
ANTHROPIC_VERSION = "2023-06-01"
PROVIDERS = ("anthropic", "openai_compatible")

logger = logging.getLogger(__name__)
_T = TypeVar("_T")


class ProviderNotConfigured(RuntimeError):
    """Falta configurar el proveedor de IA (o el del dictado) en el `.env`."""


_CLIENT: OpenAI | None = None
_CLIENT_CONFIG: tuple | None = None


def provider_config() -> tuple[str, str, dict[str, str] | None]:
    """(direccion, clave, headers extra) del proveedor configurado."""
    settings = get_settings()
    provider = settings.llm_provider.strip().lower()
    base_url = settings.llm_base_url.strip()
    api_key = settings.llm_api_key.strip()
    if provider not in PROVIDERS:
        raise ProviderNotConfigured(
            f"LLM_PROVIDER='{settings.llm_provider}' no es valido: usa 'anthropic' u 'openai_compatible'."
        )
    if provider == "openai_compatible" and not base_url:
        raise ProviderNotConfigured("Falta LLM_BASE_URL: la direccion del proveedor de IA.")
    if not api_key:
        raise ProviderNotConfigured("Falta LLM_API_KEY: la clave del proveedor de IA.")
    if provider == "anthropic":
        # La capa compatible acepta la clave como Bearer (lo que manda la
        # libreria de OpenAI); GET /models de Anthropic la pide en x-api-key.
        headers = {"x-api-key": api_key, "anthropic-version": ANTHROPIC_VERSION}
        return base_url or ANTHROPIC_BASE_URL, api_key, headers
    return base_url, api_key, None


def get_client(force_refresh: bool = False, model: str | None = None) -> OpenAI:
    """Cliente del proveedor de IA. Se reusa mientras la configuracion no
    cambie (por ejemplo, desde Configuracion > Credenciales); `force_refresh`
    lo recrea igual (el loop lo pide tras un 401).

    spec varios-proveedores REQ-004/005: con `model` ("gemini/gemini-2.5-flash")
    devuelve el cliente de SU proveedor, asi el salto puede cambiar de proveedor
    a mitad de un turno. Sin `model`, el proveedor principal (LLM_*), como siempre."""
    global _CLIENT, _CLIENT_CONFIG

    if model is not None or not get_settings().llm_api_key.strip():
        from app.llm import providers  # import diferido: providers usa la config de aca

        try:
            if model is not None:
                return providers.client_for(model, force_refresh=force_refresh)
            provider_id = providers.principal_id()
            if provider_id is None:
                raise ProviderNotConfigured(
                    "Falta LLM_API_KEY o la clave de algun proveedor (ej. GEMINI_API_KEY) en el .env."
                )
            return providers.client_for_provider(provider_id, force_refresh=force_refresh)
        except providers.ProviderUnavailable as exc:
            raise ProviderNotConfigured(str(exc)) from exc

    config = provider_config()
    if force_refresh or _CLIENT is None or config != _CLIENT_CONFIG:
        base_url, api_key, headers = config
        _CLIENT = OpenAI(api_key=api_key, base_url=base_url, default_headers=headers)
        _CLIENT_CONFIG = config
    return _CLIENT


def get_stt_client() -> tuple[OpenAI, str]:
    """(cliente, modelo) del dictado por voz: un endpoint de audio compatible
    con OpenAI (`/audio/transcriptions`, ej. Whisper de OpenAI)."""
    settings = get_settings()
    base_url = settings.stt_base_url.strip()
    api_key = settings.stt_api_key.strip()
    if not base_url or not api_key:
        raise ProviderNotConfigured("El dictado por voz no esta configurado (faltan STT_BASE_URL y STT_API_KEY).")
    return OpenAI(api_key=api_key, base_url=base_url), settings.stt_model.strip() or "whisper-1"


def call_with_token_retry(
    fn: Callable[[OpenAI], _T],
    client_getter: Callable[..., OpenAI] | None = None,
) -> _T:
    """Llama `fn(cliente)` y, si el proveedor responde 401, recrea el cliente (`force_refresh=True`) y repite UNA sola
    vez: nunca mas de 2 intentos, sin bucles.

    Viene de la version de origen (spec transcripcion-reintenta-token): el dictado, la lectura de imagenes de PDF y la
    compactacion la usan. Con una clave fija, un 401 casi siempre es una clave mal cargada, pero recrear el cliente toma
    la clave nueva si la persona la acaba de cambiar en el `.env` o en Configuracion.

    `fn` recibe el cliente y debe REARMAR sus argumentos en cada intento. Solo el 401 se reintenta. `client_getter`: de
    donde sale el cliente (por defecto `get_client`; para un modelo de otro proveedor, pasar uno que use `model=`)."""
    getter = client_getter or get_client
    try:
        return fn(getter())
    except openai.AuthenticationError as exc:
        logger.warning("El proveedor de IA devolvio 401, reintentando con el cliente recreado: %s", exc)
        return fn(getter(force_refresh=True))


def call_model_with_token_retry(model: str, fn: Callable[[OpenAI, str], _T]) -> _T:
    """`call_with_token_retry` para UN modelo "proveedor/modelo": `fn(cliente, nombre)` recibe el cliente del proveedor de ese
    modelo y el nombre que ese proveedor entiende (sin el prefijo de ULTRON).

    Bug real 2026-10-26: la compactacion y la lectura de PDF llamaban `call_with_token_retry` sin `client_getter`. Con un
    modelo de Ollama elegido, el pedido iba al proveedor PRINCIPAL (en la nube) con el nombre `ollama/qwen3:4b`. Sin internet
    fallaba, el salto automatico le echaba la culpa a Ollama, que andaba bien, y lo pausaba: ULTRON no respondia nada."""
    from app.llm import providers  # import diferido: providers usa la config de aca

    name = providers.api_model(model)
    return call_with_token_retry(
        lambda client: fn(client, name),
        lambda force_refresh=False: get_client(force_refresh=force_refresh, model=model),
    )
