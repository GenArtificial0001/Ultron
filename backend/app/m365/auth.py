"""Login con la cuenta de la empresa para Outlook y Teams (spec office-outlook-teams-mcp REQ-M02, REQ-M14).

Por que existe: el conector de la empresa (EAKC `mcp-msgraph`) pide un login OAuth con PKCE. El cliente OAuth oficial del paquete `mcp` hace todo el flujo (descubrimiento, registro del
cliente, navegador, canje y renovacion del token); lo que Vision pone son las TRES piezas que dependen de esta PC y que el 2026-10-17 las pruebas reales mostraron necesarias:

1. La direccion de retorno sale del puerto REAL de Vision (se puede cambiar en Configuracion). Si el puerto cambio, el registro viejo del cliente apunta a una direccion que el
   servidor rechazaria: se descarta y se registra de nuevo (`discard_registration_if_address_changed`).
2. El cliente HTTP valida los certificados con el almacen de Windows (`http_client_factory`). El proxy de la empresa inspecciona el trafico y con `certifi` (lo que usa httpx por
   defecto) da `CERTIFICATE_VERIFY_FAILED: self-signed certificate in certificate chain`. Medido el 2026-10-17: con el almacen del sistema conecta.
3. El navegador vuelve a Vision por `/api/m365/callback`. `LoginFlow` recibe ese resultado, lo entrega UNA sola vez y solo si el `state` es el del login en curso: otra pagina no
   puede completar un login que la persona no inicio.

Nada de la direccion del conector ni del registro se configura a mano: la direccion viene incorporada y el registro lo hace el propio login la primera vez (REQ-M14)."""

from __future__ import annotations

import asyncio
import logging
import ssl
import time
import webbrowser
from urllib.parse import parse_qs, urlparse

import httpx
from mcp.client.auth import OAuthClientProvider
from mcp.shared.auth import OAuthClientMetadata

from app.friendly_errors import FriendlyError
from app.m365 import token_store
from app.m365.errors import M365Error

logger = logging.getLogger(__name__)

# ULTRON: la direccion del conector MCP de Microsoft 365 se configura (`m365_connector_url`); la version de origen tenia fija la de su
# empresa. `CONNECTOR_URL` queda vacio: si alguien lo fija (los tests), manda sobre la configuracion.
CONNECTOR_URL = ""


def connector_url() -> str:
    """La direccion configurada del conector. Lanza `NotConfigured` (frase para la persona) si no hay ninguna."""
    from app.config import get_settings  # import diferido: igual que el resto del paquete
    from app.m365.errors import NotConfigured

    url = (CONNECTOR_URL or getattr(get_settings(), "m365_connector_url", "") or "").strip()
    if not url:
        raise NotConfigured()
    return url
CALLBACK_PATH = "/api/m365/callback"
# Plazo para que la persona termine el login en el navegador. Pasado eso Vision deja de esperar (no queda una tarea colgada).
LOGIN_TIMEOUT_SECONDS = 240.0
_ALREADY_EXPIRED = 1.0  # una hora pasada que el SDK lee como «vencido» (0.0 lo lee como «sin vencimiento»)
_POLL_SECONDS = 0.1


class LoginTimeout(M365Error):
    """La persona no completo el inicio de sesion en el navegador a tiempo."""

    def __init__(self) -> None:
        super().__init__(
            FriendlyError(
                "M365-LOGIN-DEMORA",
                "No llegó a completarse el inicio de sesión. Volvé a tocar «Iniciar sesión» y terminá de entrar con tu cuenta de la empresa en la ventana que se abre.",
            )
        )


class LoginDenied(M365Error):
    """El navegador volvio con un rechazo (la persona cancelo o la empresa no lo permitio)."""

    def __init__(self) -> None:
        super().__init__(
            FriendlyError(
                "M365-LOGIN-RECHAZADO",
                "No se pudo iniciar sesión con tu cuenta de la empresa: se canceló o no se autorizó. Volvé a tocar «Iniciar sesión» y aceptá el acceso cuando te lo pida.",
            )
        )


def redirect_uri(port: int) -> str:
    """Direccion a la que el navegador devuelve el codigo: la propia pagina local de Vision, en SU puerto."""
    return f"http://127.0.0.1:{port}{CALLBACK_PATH}"


def discard_registration_if_address_changed(port: int) -> None:
    """Si el registro guardado del cliente apunta a OTRA direccion de retorno (el usuario cambio el puerto), se descarta: el servidor rechazaria el login con la vieja."""
    saved = token_store.load_client_info()
    if saved is None:
        return
    wanted = redirect_uri(port)
    if wanted not in [str(uri) for uri in (saved.redirect_uris or [])]:
        logger.info(
            "Microsoft 365: el puerto de ULTRON cambio; se descarta el registro de cliente anterior y se registra uno nuevo"
        )
        token_store.forget_client()


def http_client_factory(
    headers: dict[str, str] | None = None,
    timeout: httpx.Timeout | None = None,
    auth: httpx.Auth | None = None,
) -> httpx.AsyncClient:
    """Igual que el cliente por defecto de `mcp`, pero validando con el almacen de certificados de Windows (el proxy de la empresa inspecciona el trafico)."""
    kwargs: dict = {
        "timeout": timeout or httpx.Timeout(30.0, read=300.0),
        "verify": ssl.create_default_context(),
    }
    if headers is not None:
        kwargs["headers"] = headers
    if auth is not None:
        kwargs["auth"] = auth
    return httpx.AsyncClient(**kwargs)


def open_transport(url: str, provider: httpx.Auth):
    """El transporte MCP vigente (`streamable_http_client`) para `url`. A diferencia del deprecado, NO recibe `auth` ni una fabrica: recibe el cliente httpx ya armado, asi que se lo
    construye aca con la fabrica de Vision (certificados de Windows, por el proxy de la empresa) y con el cliente OAuth del SDK. Mismos plazos que el deprecado: 30 s para conectar y 300 s de lectura.

    Uso: `async with auth.open_transport(url, provider) as (read, write, _):`. El cliente httpx se cierra solo al salir."""
    from contextlib import asynccontextmanager

    from mcp.client.streamable_http import streamable_http_client  # import diferido: el modulo se carga aunque `mcp` falte (los tests lo exigen aparte)

    @asynccontextmanager
    async def _open():
        async with http_client_factory(timeout=httpx.Timeout(30.0, read=300.0), auth=provider) as http_client:
            async with streamable_http_client(url, http_client=http_client) as streams:
                yield streams

    return _open()


class LoginFlow:
    """Un inicio de sesion en curso. El navegador entrega el resultado por `/api/m365/callback`; el cliente OAuth del SDK lo espera con `wait()`."""

    def __init__(self) -> None:
        self._state: str | None = None
        self._code: str | None = None
        self._failed = False
        self._delivered = False

    def begin(self, state: str) -> None:
        """Empieza un login nuevo con el `state` que el SDK puso en la direccion de autorizacion; descarta cualquier resultado anterior."""
        self._state = state
        self._code = None
        self._failed = False
        self._delivered = False

    def deliver(self, code: str, state: str) -> bool:
        """El navegador volvio con el codigo. True solo si es del login en curso y todavia no se entrego uno."""
        if self._state is None or self._delivered or not state or state != self._state or not code:
            return False
        self._code = code
        self._delivered = True
        return True

    def fail(self, state: str, reason: str) -> bool:
        """El navegador volvio con un rechazo. El motivo crudo solo va al log, nunca a la pantalla."""
        if self._state is None or self._delivered or state != self._state:
            return False
        logger.warning("Microsoft 365: el inicio de sesion volvio con un rechazo (%s)", reason[:60])
        self._failed = True
        self._delivered = True
        return True

    async def wait(self, timeout: float = LOGIN_TIMEOUT_SECONDS) -> tuple[str, str | None]:
        """Espera el resultado del navegador. Devuelve `(codigo, state)`; lanza `LoginDenied` o `LoginTimeout`."""
        deadline = time.monotonic() + timeout
        while True:
            if self._delivered:
                if self._failed:
                    raise LoginDenied()
                return self._code or "", self._state
            if time.monotonic() >= deadline:
                self._state = None  # un codigo que llegue tarde ya no se acepta
                raise LoginTimeout()
            await asyncio.sleep(_POLL_SECONDS)


class VisionOAuthProvider(OAuthClientProvider):
    """El cliente OAuth del SDK, con UN arreglo: al cargar el token del disco le calcula cuando VENCE.

    Hallazgo con el uso real (2026-10-17): el SDK guarda `expires_in` («dura 4471 s») pero no desde cuando, y decide si un token vale por `token_expiry_time`, que al leerlo del disco queda
    VACIO: lo daba por valido para siempre. Con el token vencido mandaba la peticion, el servidor respondia 401 y recien ahi pedia un login NUEVO (que una herramienta no puede hacer: sale
    «volve a iniciar sesion») en vez de RENOVAR en silencio con el `refresh_token`. Medido: el token de la persona tenia 256 min y duraba 68; a la mañana siguiente habia que volver a
    entrar. Ahora, con la hora de emision que guarda `token_store`, el SDK sabe que esta vencido y renueva solo; la persona solo vuelve a entrar si el `refresh_token` tambien murio."""

    async def _initialize(self) -> None:
        await super()._initialize()
        self.context.token_expiry_time = _expiry_of_saved_token(self.context.current_tokens)


def _expiry_of_saved_token(tokens) -> float | None:
    """Hora (epoch) en que vence el token cargado del disco, o None si el servidor nunca dijo cuanto dura.

    Un token guardado SIN hora de emision (los de las primeras pruebas) se trata como YA vencido: no se sabe cuando nacio y lo seguro es renovarlo; si el servidor lo rechaza, la persona vuelve a
    iniciar sesion con el boton. Ojo: se usa `1.0` (el 1 de enero de 1970) y NO `0.0`, porque el SDK pregunta `not token_expiry_time` y un `0.0` cuenta como «sin vencimiento» (token valido)."""
    if tokens is None or tokens.expires_in is None:
        return None
    issued = token_store.issued_at()
    if issued is None:
        return _ALREADY_EXPIRED
    return issued + int(tokens.expires_in)


def build_provider(port: int, flow: LoginFlow, *, open_browser=webbrowser.open) -> OAuthClientProvider:
    """El cliente OAuth del SDK, listo para usar con `open_transport(CONNECTOR_URL, provider)`.

    Cliente PUBLICO (sin clave, `token_endpoint_auth_method: none`): lo que se probo contra el servidor de la empresa el 2026-10-17. El token y el registro quedan cifrados
    en `token_store`."""
    discard_registration_if_address_changed(port)
    metadata = OAuthClientMetadata(
        client_name="ULTRON",
        redirect_uris=[redirect_uri(port)],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        token_endpoint_auth_method="none",
    )

    async def _redirect(authorization_url: str) -> None:
        state = (parse_qs(urlparse(authorization_url).query).get("state") or [""])[0]
        flow.begin(state)
        open_browser(authorization_url)

    async def _callback() -> tuple[str, str | None]:
        return await flow.wait()

    return VisionOAuthProvider(
        server_url=connector_url(),
        client_metadata=metadata,
        storage=token_store.M365TokenStorage(),
        redirect_handler=_redirect,
        callback_handler=_callback,
    )
