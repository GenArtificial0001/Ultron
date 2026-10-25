"""Mensajes cuando Outlook y Teams no estan disponibles (spec office-outlook-teams-mcp REQ-M04).

Lo que se vigila:
1. Cada situacion real tiene SU frase, y es la que le dice a la persona que hacer: sin sesion, sesion vencida, bloqueo de la empresa (pais o tipo de empleado), conector que no
   responde y falta de red. Antes todo eso caia en un «devolvio un error inesperado» que no ayuda.
2. Ningun mensaje trae jerga ni texto crudo: pasan el MISMO guardian que usa `test_friendly_errors.py` (sin «token», «http», codigos de estado, «api»...).
3. Nada de lo que hay en la excepcion (un token, una URL con parametros) se cuela en lo que ve la persona; el crudo va solo a `technical`."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app.m365 import errors
from tests.test_friendly_errors import _ACTION, _JARGON  # el mismo guardian de jerga y de «que hacer»

SECRET = "eyJhbGciOiJSUzI1NiJ9.TOKEN-SECRETO-7712"


def _status_error(status: int, body: str = "") -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://graph.eakc.empresa.com/")
    response = httpx.Response(status, request=request, text=body)
    return httpx.HTTPStatusError(
        f"Client error '{status}' for url 'https://graph.eakc.empresa.com/?access_token={SECRET}'",
        request=request,
        response=response,
    )


CASES = [
    ("sin sesion", errors.NeedsLogin(), "M365-SIN-SESION"),
    ("401", _status_error(401), "M365-SESION-VENCIDA"),
    ("renovacion rechazada", RuntimeError("invalid_grant: the refresh token has expired"), "M365-SESION-VENCIDA"),
    ("403 de la empresa", _status_error(403, "Access denied: country blocked (RUS)"), "M365-BLOQUEO"),
    ("demora", asyncio.TimeoutError(), "M365-DEMORA"),
    ("sin red", httpx.ConnectError("[Errno 11001] getaddrinfo failed"), "M365-SIN-CONEXION"),
    ("otra cosa", RuntimeError("algo raro"), "M365-FALLO"),
]


@pytest.mark.parametrize(("label", "exc", "code"), CASES, ids=[c[0] for c in CASES])
def test_each_situation_gets_its_own_phrase(label: str, exc: BaseException, code: str):
    assert errors.describe(exc).code == code, label


@pytest.mark.parametrize(("label", "exc", "code"), CASES, ids=[c[0] for c in CASES])
def test_every_message_passes_the_jargon_guard_and_says_what_to_do(label: str, exc: BaseException, code: str):
    message = errors.describe(exc).message

    assert not _JARGON.search(message), f"{label}: jerga en {message!r}"
    assert _ACTION.search(message), f"{label}: no le dice a la persona que hacer: {message!r}"


@pytest.mark.parametrize(("label", "exc", "code"), CASES, ids=[c[0] for c in CASES])
def test_nothing_from_the_exception_leaks_into_the_message(label: str, exc: BaseException, code: str):
    friendly = errors.describe(exc)

    assert SECRET not in friendly.message
    assert "graph.eakc.empresa.com" not in friendly.message and "access_token" not in friendly.message


def test_the_technical_detail_keeps_the_raw_text_but_never_a_token():
    technical = errors.technical_detail(_status_error(401))

    assert "401" in technical
    assert SECRET not in technical, "el detalle tecnico no puede llevar el token que venia en la URL"


def test_a_401_and_a_403_are_not_confused():
    """Un 403 NO manda a «volver a iniciar sesion»: eso no lo arregla (es una regla de la empresa). Puede NOMBRAR la accion para decir que no sirve, pero no pedirla."""
    forbidden = errors.describe(_status_error(403))

    assert forbidden.code != errors.describe(_status_error(401)).code
    assert "tocá «iniciar sesión»" not in forbidden.message.lower() and "volvé a tocar" not in forbidden.message.lower()
    assert "no lo arregla" in forbidden.message.lower(), "tiene que aclarar que iniciar sesion de nuevo no sirve"


def test_a_failed_oauth_flow_means_sign_in_again_whatever_the_sdk_calls_it():
    """Caso medido el 2026-10-17: con el token rechazado el SDK rehace el login y falla con sus propios tipos (`OAuthRegistrationError`, `OAuthFlowError`, `OAuthTokenError`).
    Todos significan lo mismo para la persona: volver a iniciar sesion. Antes caian en «error inesperado», que no ayuda."""
    from mcp.client.auth.exceptions import OAuthFlowError, OAuthRegistrationError, OAuthTokenError

    for exc in (
        OAuthRegistrationError("Registration failed: 401 unauthorized"),
        OAuthFlowError("flow"),
        OAuthTokenError("Token exchange failed"),
    ):
        assert errors.describe(exc).code == "M365-SESION-VENCIDA", type(exc).__name__


def test_a_network_cut_that_mentions_blocked_is_not_taken_for_a_empresa_block():
    """Un corte de proxy o de red dice a veces «blocked» o «policy». Sin un 403 real eso NO es el bloqueo de la empresa (el remedio es otro: la VPN)."""
    cut = httpx.ConnectError("connection blocked by proxy policy")

    assert errors.describe(cut).code == "M365-SIN-CONEXION"


def test_a_403_is_a_empresa_block_even_when_the_body_is_empty():
    assert errors.describe(_status_error(403, "")).code == "M365-BLOQUEO"


def test_the_missing_session_error_carries_the_friendly_message():
    exc = errors.NeedsLogin()

    assert isinstance(exc, errors.M365Error)
    assert exc.friendly.code == "M365-SIN-SESION" and "Iniciar sesión" in exc.friendly.message
