"""Errores de Outlook y Teams, dichos para una persona no tecnica (spec office-outlook-teams-mcp REQ-M04).

Por que existe: el conector de la empresa puede no estar disponible por razones muy distintas (la persona no inicio sesion, la sesion vencio, la empresa bloqueo la cuenta por pais o tipo de
empleado, el servidor no responde, no hay VPN) y cada una tiene una solucion DISTINTA. Antes de esto todo caia en un «error inesperado» que no ayuda. Aca cada una tiene su frase,
que dice que paso y que hacer, y pasa el mismo guardian de jerga que el resto de los mensajes de Vision (`tests/test_friendly_errors.py`).

Reglas:
- el texto crudo de la excepcion NUNCA va al mensaje: va solo a `technical_detail()`, que ademas le quita cualquier token o secreto que venga en una URL o en el cuerpo;
- un 401 y un 403 NO se confunden: iniciar sesion de nuevo arregla el 401 pero NO un 403 (es una regla de cumplimiento de la empresa que ninguna instalacion salva).

Reusa `FriendlyError` de `app/friendly_errors.py` para que el resto de Vision (pantalla, tools, log) lo trate igual que cualquier otro error de conector."""

from __future__ import annotations

import asyncio
import re

import httpx

from app.friendly_errors import FriendlyError

# Texto de un secreto dentro de una URL o un cuerpo: parametros tipicos de OAuth y cabeceras `Bearer`. Se tapa ANTES de dejar el crudo en el detalle tecnico.
_SECRET_IN_TEXT = re.compile(
    r"(?i)((?:access_token|refresh_token|id_token|token|code|client_secret|authorization)[=:]\s*)([^\s&'\"]+)|(Bearer\s+)([A-Za-z0-9._~+/=-]+)"
)
# Un JWT suelto (tres tramos base64 separados por puntos), por si aparece fuera de un parametro.
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{3,}\b")

_EXPIRED_HINTS = ("invalid_grant", "refresh token", "expired", "token has expired", "aadsts")
# El SDK de `mcp` falla con SUS tipos cuando rehace el login (todos heredan de OAuthFlowError). Se reconocen por el nombre de la clase, como hace `friendly_errors.py`,
# para no importar el SDK en este modulo.
_OAUTH_FLOW_TYPES = {"oauthflowerror", "oauthtokenerror", "oauthregistrationerror"}
_NETWORK_TYPES = {
    "connecterror",
    "connecttimeout",
    "proxyerror",
    "sslerror",
    "remoteprotocolerror",
    "readerror",
    "networkerror",
}


class M365Error(Exception):
    """Falla de Outlook o Teams con su frase ya lista para la persona (`friendly`)."""

    def __init__(self, friendly: FriendlyError) -> None:
        super().__init__(friendly.message)
        self.friendly = friendly


class NeedsLogin(M365Error):
    """No hay sesion iniciada: la persona tiene que iniciarla desde Configuracion > Conexiones."""

    def __init__(self) -> None:
        super().__init__(
            FriendlyError(
                "M365-SIN-SESION",
                "Todavía no iniciaste sesión en Outlook y Teams. Revisá Configuración > Conexiones, tocá «Iniciar sesión» y entrá con tu cuenta de la empresa.",
            )
        )


class NotConfigured(M365Error):
    """ULTRON: no hay direccion del conector MCP de Microsoft 365 cargada."""

    def __init__(self) -> None:
        super().__init__(
            FriendlyError(
                "M365-SIN-CONECTOR",
                "Outlook y Teams todavía no están configurados: falta la dirección del conector de Microsoft 365. Cargala en "
                "Configuración > Conexiones > General, en «Inteligencia artificial».",
            )
        )


def _status_of(exc: BaseException) -> int | None:
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if isinstance(status, int):
        return status
    status = getattr(exc, "status_code", None)
    return status if isinstance(status, int) else None


def _body_text(exc: BaseException) -> str:
    response = getattr(exc, "response", None)
    try:
        return str(response.text) if response is not None else ""
    except Exception:  # noqa: BLE001 -- un cuerpo ilegible no debe impedir armar el mensaje
        return ""


def redact(text: str) -> str:
    """Tapa tokens, codigos y secretos que vengan en una URL, una cabecera o un cuerpo."""

    def _hide(match: re.Match[str]) -> str:
        return f"{match.group(1)}[OMITIDO]" if match.group(1) else f"{match.group(3)}[OMITIDO]"

    return _JWT.sub("[OMITIDO]", _SECRET_IN_TEXT.sub(_hide, text))


def technical_detail(exc: BaseException) -> str:
    """El crudo recortado para quien soporta Vision, SIN secretos (el token puede venir en la URL del error)."""
    status = _status_of(exc)
    prefix = f"HTTP {status}: " if status else ""
    return redact(f"{prefix}{type(exc).__name__}: {exc} {_body_text(exc)}".strip())[:300]


def describe(exc: BaseException) -> FriendlyError:
    """La frase para la persona. Un `M365Error` ya trae la suya."""
    if isinstance(exc, M365Error):
        return exc.friendly
    status = _status_of(exc)
    text = f"{exc} {_body_text(exc)}".lower()
    names = {cls.__name__.lower() for cls in type(exc).__mro__}

    # Solo un 403 REAL es el bloqueo de la empresa. Antes tambien se aceptaba cualquier texto con «blocked» o «policy», y un corte de proxy o de VPN (que dice
    # justo eso) mandaba a la persona a pedir ayuda al equipo del conector cuando lo que hacia falta era revisar la VPN.
    if status == 403:
        return FriendlyError(
            "M365-BLOQUEO",
            "la empresa no deja usar Outlook y Teams desde esta cuenta (puede ser por el país desde el que te conectás o por el tipo de empleado). "
            "Iniciar sesión de nuevo no lo arregla: pedile ayuda al equipo que administra el conector de la empresa.",
            status,
        )
    # Con el token rechazado y sin forma de renovarlo, el SDK rehace el login y falla con sus tipos (medido el 2026-10-17: «Registration failed: 401»). Para la persona
    # significa lo mismo que un 401: volver a iniciar sesion. Antes caia en «error inesperado», que no ayuda.
    if status == 401 or names & _OAUTH_FLOW_TYPES or any(hint in text for hint in _EXPIRED_HINTS):
        return FriendlyError(
            "M365-SESION-VENCIDA",
            "Tu sesión de Outlook y Teams venció. Volvé a tocar «Iniciar sesión» en Configuración > Conexiones y entrá de nuevo con tu cuenta de la empresa.",
            status,
        )
    if isinstance(exc, asyncio.TimeoutError) or any("timeout" in name for name in names):
        return FriendlyError(
            "M365-DEMORA",
            "Outlook y Teams tardaron demasiado en responder. Revisá que estés conectado a la red de la empresa (VPN) y probá de nuevo en unos minutos.",
            status,
        )
    if (
        names & _NETWORK_TYPES
        or isinstance(exc, (httpx.TransportError, OSError))
        or any(hint in text for hint in ("getaddrinfo", "connection", "unreachable"))
    ):
        return FriendlyError(
            "M365-SIN-CONEXION",
            "No pude conectarme con Outlook y Teams. Revisá que estés conectado a la red de la empresa (VPN) y probá de nuevo.",
            status,
        )
    return FriendlyError(
        "M365-FALLO",
        "Outlook y Teams devolvieron un error inesperado. Probá de nuevo; si se repite, avisá a soporte.",
        status,
    )
