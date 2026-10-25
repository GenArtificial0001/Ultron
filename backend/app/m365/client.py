"""Cliente MCP de Outlook y Teams (spec office-outlook-teams-mcp REQ-M04, REQ-M05, REQ-M12).

Por que existe: es la unica puerta de Vision hacia el conector de la empresa (EAKC `mcp-msgraph`). Las tools del chat (`m365_*`) y la pantalla de Configuracion pasan por aca, de modo que
hay UN solo lugar donde se conecta, se aplica el plazo y se traducen los errores.

Reglas que cumple:
- SIN SESION no se intenta conectar: sale `NeedsLogin` sin tocar la red. Una herramienta NUNCA inicia un login por su cuenta ni abre el navegador: iniciar sesion es una accion de
  la persona desde Configuracion > Conexiones (`auth.LoginFlow`). Si el token venciera y no se pudiera renovar, el cliente OAuth del SDK intentaria abrir el navegador; aca ese
  intento se convierte en `NeedsLogin`, que es lo que la persona tiene que hacer.
- Cada llamada tiene PLAZO (15 s por defecto, spec D8): pasado ese tiempo sale un mensaje claro y la tarea se cancela; nada queda colgado.
- Los errores del SDK llegan envueltos en grupos de excepciones (anyio): se desenvuelven hasta la causa real antes de traducirlos a la frase para la persona (`errors.describe`).
- El resultado vuelve como texto plano (`ToolOutput`): jamas el token ni objetos del SDK."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from mcp import ClientSession
from mcp.client.auth import OAuthClientProvider
from mcp.shared.auth import OAuthClientMetadata

from app.m365 import auth, errors, token_store

logger = logging.getLogger(__name__)

# Plazo por llamada (spec D8: «si Outlook no responde en 15 segundos, Vision lo informa en lenguaje claro y no se cuelga»).
DEFAULT_TIMEOUT_SECONDS = 15.0


@dataclass(frozen=True)
class ToolOutput:
    """Lo que devuelve una herramienta del conector, ya como texto. `is_error` = la propia herramienta dijo que fallo (no es una falla de conexion)."""

    text: str
    is_error: bool = False


def _leaf(exc: BaseException) -> BaseException:
    """Baja por los `ExceptionGroup` hasta la primera causa real. `NeedsLogin` tiene prioridad si esta entre ellas."""
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        for inner in exc.exceptions:
            if isinstance(inner, errors.M365Error):
                return inner
        exc = exc.exceptions[0]
    return exc


def _silent_provider(url: str) -> OAuthClientProvider:
    """Cliente OAuth que RENUEVA el token solo, pero que si hiciera falta un login nuevo falla con `NeedsLogin` en vez de abrir el navegador."""
    saved = token_store.load_client_info()
    redirect = str(saved.redirect_uris[0]) if saved and saved.redirect_uris else auth.redirect_uri(8020)

    async def _no_browser(_authorization_url: str) -> None:
        raise errors.NeedsLogin()

    async def _no_callback() -> tuple[str, str | None]:
        raise errors.NeedsLogin()

    return auth.VisionOAuthProvider(
        server_url=url,
        client_metadata=OAuthClientMetadata(
            client_name="Vision",
            redirect_uris=[redirect],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            token_endpoint_auth_method="none",
        ),
        storage=token_store.M365TokenStorage(),
        redirect_handler=_no_browser,
        callback_handler=_no_callback,
    )


async def _with_session(url: str, work, timeout: float):
    """Abre una sesion MCP contra `url`, corre `work(session)` y la cierra, todo dentro del plazo. Traduce cualquier falla a `M365Error`."""
    if not token_store.has_session():
        raise errors.NeedsLogin()  # sin sesion no se toca la red

    async def _run():
        async with auth.open_transport(url, _silent_provider(url)) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await work(session)

    try:
        return await asyncio.wait_for(_run(), timeout=timeout)
    except errors.M365Error:
        raise
    except BaseException as exc:  # noqa: BLE001 -- se desenvuelve y se traduce; una cancelacion real (CancelledError) se relanza abajo
        if isinstance(exc, asyncio.CancelledError):
            raise
        cause = _leaf(exc)
        if isinstance(cause, errors.M365Error):
            raise cause from None
        # El crudo va al log SIN secretos; la persona ve la frase.
        logger.warning("Microsoft 365: fallo la llamada al conector (%s)", errors.technical_detail(cause))
        raise errors.M365Error(errors.describe(cause)) from None


async def list_tools(url: str | None = None, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> list[str]:
    """Nombres de las herramientas que el conector ofrece hoy. `url` None = la configurada (`auth.connector_url`)."""
    url = url or auth.connector_url()

    async def _work(session: ClientSession) -> list[str]:
        names: list[str] = []
        cursor: str | None = None
        while True:
            page = await session.list_tools(cursor=cursor)
            names.extend(tool.name for tool in page.tools)
            cursor = page.nextCursor
            if not cursor:
                return names

    return await _with_session(url, _work, timeout)


async def call_tool(
    name: str, arguments: dict | None = None, url: str | None = None, timeout: float = DEFAULT_TIMEOUT_SECONDS
) -> ToolOutput:
    """Llama a UNA herramienta del conector y devuelve su texto. `url` None = la configurada (`auth.connector_url`)."""
    url = url or auth.connector_url()

    async def _work(session: ClientSession) -> ToolOutput:
        result = await session.call_tool(name, arguments or {})
        text = "\n".join(part.text for part in result.content if getattr(part, "type", "") == "text")
        return ToolOutput(text=text, is_error=bool(result.isError))

    return await _with_session(url, _work, timeout)
