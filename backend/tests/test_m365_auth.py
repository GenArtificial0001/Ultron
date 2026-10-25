"""Login con la cuenta de la empresa (spec office-outlook-teams-mcp REQ-M02, REQ-M14).

Lo que se vigila (el flujo completo contra el servidor real lo probo el usuario a mano el 2026-10-17; aca van las piezas que deciden si funciona en CADA PC):
1. La direccion de retorno sale del puerto REAL de Vision (se puede cambiar en Configuracion), no de un 8020 fijo.
2. El registro del cliente guardado solo se reutiliza si su direccion de retorno sigue siendo la de ahora; si no, se descarta (el servidor la rechazaria).
3. El cliente HTTP valida con el almacen de certificados de Windows: el proxy de la empresa inspecciona el trafico y con `certifi` solo da CERTIFICATE_VERIFY_FAILED.
4. El resultado del login (codigo y estado) llega por la ruta local y se entrega UNA sola vez; un estado ajeno se rechaza (defensa contra otra pagina)."""

from __future__ import annotations

import asyncio
import ssl

import httpx
import pytest

from app import secret_store
from app.m365 import auth, token_store

pytestmark = pytest.mark.skipif(not secret_store.is_available(), reason="DPAPI solo existe en Windows")


def test_the_redirect_address_uses_the_real_port():
    assert auth.redirect_uri(8020) == "http://127.0.0.1:8020/api/m365/callback"
    assert auth.redirect_uri(9100) == "http://127.0.0.1:9100/api/m365/callback"


def test_the_connector_address_comes_from_the_configuration(monkeypatch):
    """ULTRON (spec portar-desde-origen): la version de origen traia fija la direccion de su empresa; aca la carga la persona."""
    from app.config import get_settings

    monkeypatch.setattr(auth, "CONNECTOR_URL", "")
    monkeypatch.setattr(get_settings(), "m365_connector_url", " https://conector.mi-empresa.test ")
    assert auth.connector_url() == "https://conector.mi-empresa.test"


def test_without_an_address_it_says_what_to_configure_instead_of_calling_anyone(monkeypatch):
    from app.config import get_settings
    from app.m365.errors import NotConfigured

    monkeypatch.setattr(auth, "CONNECTOR_URL", "")
    monkeypatch.setattr(get_settings(), "m365_connector_url", "")
    with pytest.raises(NotConfigured) as info:
        auth.connector_url()
    assert info.value.friendly.code == "M365-SIN-CONECTOR" and "dirección del conector" in info.value.friendly.message


def _saved_client(redirect: str):
    from mcp.shared.auth import OAuthClientInformationFull

    info = OAuthClientInformationFull(
        client_id="cliente-vision-123",
        redirect_uris=[redirect],
        token_endpoint_auth_method="none",
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
    )
    asyncio.run(token_store.M365TokenStorage().set_client_info(info))


def test_a_saved_registration_with_the_same_address_is_kept():
    _saved_client(auth.redirect_uri(8020))

    auth.discard_registration_if_address_changed(8020)

    assert token_store.load_client_info() is not None


def test_a_saved_registration_with_another_port_is_discarded():
    """El usuario cambio el puerto de Vision: el servidor rechazaria la direccion vieja; hay que registrar de nuevo."""
    _saved_client(auth.redirect_uri(8020))

    auth.discard_registration_if_address_changed(9100)

    assert token_store.load_client_info() is None


def test_without_a_registration_there_is_nothing_to_discard_and_nothing_breaks():
    auth.discard_registration_if_address_changed(8020)

    assert token_store.load_client_info() is None


def test_the_http_client_validates_with_the_windows_certificate_store():
    client = auth.http_client_factory()

    try:
        # httpx guarda el contexto SSL en el transporte; lo que importa es que NO sea el de `certifi` por defecto: hay que poder verificar con el almacen del sistema.
        context = client._transport._pool._ssl_context  # noqa: SLF001 -- no hay API publica para leerlo
        assert isinstance(context, ssl.SSLContext) and context.verify_mode == ssl.CERT_REQUIRED
    finally:
        asyncio.run(client.aclose())


def test_the_http_client_keeps_headers_and_auth_the_sdk_passes():
    auth_obj = httpx.BasicAuth("a", "b")

    client = auth.http_client_factory(headers={"X-Prueba": "1"}, auth=auth_obj)

    try:
        assert client.headers["X-Prueba"] == "1" and client.auth is auth_obj
    finally:
        asyncio.run(client.aclose())


def test_the_login_result_is_delivered_once_and_a_foreign_state_is_rejected():
    flow = auth.LoginFlow()
    flow.begin(state="estado-correcto")

    assert flow.deliver(code="codigo-x", state="estado-ajeno") is False, "otra pagina no puede completar mi login"
    assert flow.deliver(code="codigo-x", state="estado-correcto") is True
    assert flow.deliver(code="otro", state="estado-correcto") is False, "el codigo se entrega una sola vez"


def test_a_login_flow_that_was_never_started_accepts_nothing():
    assert auth.LoginFlow().deliver(code="x", state="y") is False


def test_waiting_for_the_login_gives_the_code_the_browser_returned():
    async def scenario():
        flow = auth.LoginFlow()
        flow.begin(state="s1")
        waiter = asyncio.create_task(flow.wait(timeout=5))
        await asyncio.sleep(0)
        flow.deliver(code="el-codigo", state="s1")
        return await waiter

    assert asyncio.run(scenario()) == ("el-codigo", "s1")


def test_waiting_for_the_login_times_out_with_a_friendly_error():
    async def scenario():
        flow = auth.LoginFlow()
        flow.begin(state="s1")
        return await flow.wait(timeout=0.05)

    with pytest.raises(auth.LoginTimeout) as caught:
        asyncio.run(scenario())

    assert "iniciar" in str(caught.value).lower()


def test_an_error_returned_by_the_browser_is_reported_not_swallowed():
    async def scenario():
        flow = auth.LoginFlow()
        flow.begin(state="s1")
        flow.fail(state="s1", reason="access_denied")
        return await flow.wait(timeout=1)

    with pytest.raises(auth.LoginDenied):
        asyncio.run(scenario())


# ---------------------------------------------------------------------------------------------------------------------
# Hallazgo con el uso REAL (2026-10-17): un token vencido tiene que RENOVARSE, no pedir otro login
# ---------------------------------------------------------------------------------------------------------------------
def _token_issued_at(monkeypatch, *, when: float, expires_in: int = 4471):
    """Guarda un token como si lo hubiera emitido el servidor en `when` (hora epoch)."""
    from mcp.shared.auth import OAuthToken

    # Solo se parchea el reloj DENTRO de este bloque: `monkeypatch.undo()` deshace TODOS los parches del test, incluido el que aisla la base maestra en tmp_path (y las
    # lecturas siguientes irian a la base REAL de la persona).
    with monkeypatch.context() as local:
        local.setattr(token_store.time, "time", lambda: when)
        asyncio.run(token_store.M365TokenStorage().set_tokens(OAuthToken(access_token="A" * 20, token_type="Bearer", expires_in=expires_in, refresh_token="R" * 20)))


def _initialized(provider):
    asyncio.run(provider._initialize())  # noqa: SLF001 -- es lo que el SDK corre antes de la primera peticion
    return provider.context


def test_a_token_that_expired_while_vision_was_closed_is_not_taken_for_valid(monkeypatch):
    """Antes: el SDK leia el token del disco SIN hora de vencimiento y lo daba por valido para siempre."""
    import time

    _saved_client(auth.redirect_uri(8020))
    _token_issued_at(monkeypatch, when=time.time() - 99_999)

    ctx = _initialized(auth.build_provider(8020, auth.LoginFlow()))

    assert ctx.is_token_valid() is False, "un token de hace 27 horas no puede darse por valido"
    assert ctx.can_refresh_token() is True, "pero tiene refresh_token y cliente: se puede RENOVAR sin pedir otro login"


def test_a_token_still_in_force_is_kept_as_valid(monkeypatch):
    import time

    _saved_client(auth.redirect_uri(8020))
    _token_issued_at(monkeypatch, when=time.time() - 100)  # emitido hace 100 s, dura 4471

    ctx = _initialized(auth.build_provider(8020, auth.LoginFlow()))

    assert ctx.is_token_valid() is True


def test_a_token_saved_without_an_issue_time_is_treated_as_expired_so_it_gets_renewed(monkeypatch):
    """Los que ya estan guardados en las PC (de las pruebas) no traen hora: no se sabe cuando nacieron. Lo seguro es renovarlos; si el servidor los rechaza, la persona vuelve a entrar."""
    from app import secret_store
    from app.persistence import master_index

    _saved_client(auth.redirect_uri(8020))
    legacy = {"access_token": "A" * 20, "token_type": "Bearer", "expires_in": 4471, "refresh_token": "R" * 20}
    master_index.set_secure_setting(token_store.TOKEN_KEY, secret_store.encrypt_json(legacy))

    ctx = _initialized(auth.build_provider(8020, auth.LoginFlow()))

    assert ctx.is_token_valid() is False and ctx.can_refresh_token() is True


def test_a_token_without_expiry_information_from_the_server_is_not_invalidated(monkeypatch):
    """Si el servidor nunca dijo cuanto dura (`expires_in` ausente), no hay vencimiento que calcular: se deja como esta."""
    from mcp.shared.auth import OAuthToken

    _saved_client(auth.redirect_uri(8020))
    asyncio.run(token_store.M365TokenStorage().set_tokens(OAuthToken(access_token="A" * 20, token_type="Bearer", refresh_token="R" * 20)))

    ctx = _initialized(auth.build_provider(8020, auth.LoginFlow()))

    assert ctx.is_token_valid() is True


def test_the_silent_provider_used_by_the_tools_computes_the_expiry_too(monkeypatch):
    """Las herramientas (`client.py`) usan OTRO proveedor que el login: tiene que calcular el vencimiento igual, si no el arreglo solo valdria para el boton."""
    import time

    from app.m365 import client

    _saved_client(auth.redirect_uri(8020))
    _token_issued_at(monkeypatch, when=time.time() - 99_999)

    ctx = _initialized(client._silent_provider(auth.CONNECTOR_URL))  # noqa: SLF001

    assert ctx.is_token_valid() is False and ctx.can_refresh_token() is True
