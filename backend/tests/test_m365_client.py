"""Cliente MCP de Outlook y Teams (spec office-outlook-teams-mcp REQ-M04, REQ-M05, REQ-M12).

Se prueba contra un servidor MCP FALSO en loopback (la red externa esta bloqueada en la suite; loopback si se permite). Lo que se vigila:
1. Sin sesion NO se intenta conectar: sale `NeedsLogin` con su frase, sin tocar la red y sin abrir el navegador (una herramienta nunca inicia un login por su cuenta).
2. Con sesion, `list_tools` y `call_tool` hablan MCP de verdad (initialize, tools/list, tools/call).
3. Un plazo vencido, un servidor caido y un error de la herramienta salen como mensajes claros, no como excepciones crudas ni como una espera infinita.
4. El token viaja solo hacia el conector; el texto del token nunca aparece en lo que devuelve el cliente."""

from __future__ import annotations

import asyncio
import socket
import threading
import time

import pytest

from app import secret_store
from app.m365 import client, errors, token_store

pytestmark = pytest.mark.skipif(not secret_store.is_available(), reason="DPAPI solo existe en Windows")

ACCESS = "ACCESS-TOKEN-SECRETO-5521"


@pytest.fixture(autouse=True)
def _loopback_never_goes_through_a_proxy(monkeypatch):
    """`httpx` hereda HTTPS_PROXY/HTTP_PROXY del entorno, y otros tests (`config._apply_proxy`) lo dejan puesto: sin esto, la llamada al servidor falso en LOOPBACK sale por el proxy de
    la empresa y el hermetismo de la suite la bloquea (fallaba solo dentro de la suite completa, nunca aislado). En una PC real el proxy SI hace falta para llegar al conector: esto
    solo aisla estos tests."""
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


@pytest.fixture
def fake_connector():
    """Servidor MCP falso (sin estado, JSON simple) con tres herramientas: una normal, una lenta y una que falla."""
    import uvicorn
    from mcp.server.fastmcp import FastMCP

    mcp_server = FastMCP("teams-graph", host="127.0.0.1", stateless_http=True, json_response=True)

    @mcp_server.tool()
    def get_meetings(period: str = "") -> str:
        """Eventos del calendario."""
        return f"reuniones:{period or 'today'}"

    @mcp_server.tool()
    def slow(seconds: float = 2.0) -> str:
        """Tarda en responder."""
        time.sleep(seconds)
        return "tarde"

    @mcp_server.tool()
    def boom() -> str:
        """Falla del lado del servidor."""
        raise ValueError("la herramienta fallo")

    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(mcp_server.streamable_http_app(), host="127.0.0.1", port=port, log_level="critical")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}/mcp"
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture
def signed_in():
    from mcp.shared.auth import OAuthToken

    asyncio.run(
        token_store.M365TokenStorage().set_tokens(
            OAuthToken(access_token=ACCESS, token_type="Bearer", expires_in=4471, refresh_token="R" * 20)
        )
    )


def test_without_a_session_nothing_is_attempted_and_the_person_is_told_to_sign_in():
    """La direccion es inalcanzable a proposito: si el cliente intentara conectar, el error seria de red y no `NeedsLogin`."""
    with pytest.raises(errors.NeedsLogin) as caught:
        asyncio.run(client.list_tools(url="http://127.0.0.1:9/mcp"))

    assert caught.value.friendly.code == "M365-SIN-SESION"


def test_a_tool_call_without_a_session_never_opens_the_browser(monkeypatch):
    opened = []
    monkeypatch.setattr("webbrowser.open", lambda *a, **k: opened.append(a) or True)

    with pytest.raises(errors.NeedsLogin):
        asyncio.run(client.call_tool("get_meetings", {}, url="http://127.0.0.1:9/mcp"))

    assert opened == [], "una herramienta no puede abrir el navegador por su cuenta"


@pytest.fixture
def rejecting_connector():
    """Servidor como el de la empresa cuando ya no acepta el token: publica su descubrimiento OAuth (sin eso el SDK ni llega a pedir el navegador) y responde 401 al MCP.
    Es lo que hace que el SDK quiera abrir el navegador para un login nuevo, que es lo que una herramienta NO puede hacer."""
    import uvicorn
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse, Response
    from starlette.routing import Route

    port = _free_port()
    base = f"http://127.0.0.1:{port}"

    async def protected_resource(_request):
        return JSONResponse({"resource": f"{base}/mcp", "authorization_servers": [base]})

    async def auth_server(_request):
        return JSONResponse(
            {
                "issuer": base,
                "authorization_endpoint": f"{base}/authorize",
                "token_endpoint": f"{base}/token",
                "registration_endpoint": f"{base}/register",
                "response_types_supported": ["code"],
                "grant_types_supported": ["authorization_code", "refresh_token"],
                "code_challenge_methods_supported": ["S256"],
                "token_endpoint_auth_methods_supported": ["none"],
            }
        )

    async def reject(_request):
        return Response("unauthorized", status_code=401, headers={"WWW-Authenticate": 'Bearer error="invalid_token"'})

    async def token_refused(_request):
        return JSONResponse({"error": "invalid_grant"}, status_code=400)

    routes = [
        Route("/.well-known/oauth-protected-resource{rest:path}", protected_resource),
        Route("/.well-known/oauth-authorization-server{rest:path}", auth_server),
        Route("/token", token_refused, methods=["POST"]),
        Route("/{path:path}", reject, methods=["GET", "POST", "DELETE"]),
    ]
    server = uvicorn.Server(uvicorn.Config(Starlette(routes=routes), host="127.0.0.1", port=port, log_level="critical"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"{base}/mcp"
    server.should_exit = True
    thread.join(timeout=5)


def _register_a_client():
    """Ya hay un cliente registrado (como despues del primer login): el SDK no vuelve a registrar y salta directo a pedir el navegador."""
    from mcp.shared.auth import OAuthClientInformationFull

    info = OAuthClientInformationFull(
        client_id="cliente-vision-123",
        redirect_uris=["http://127.0.0.1:8020/api/m365/callback"],
        token_endpoint_auth_method="none",
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
    )
    asyncio.run(token_store.M365TokenStorage().set_client_info(info))


def test_when_the_server_rejects_the_token_the_person_is_told_to_sign_in_and_the_browser_stays_closed(rejecting_connector, signed_in, monkeypatch):
    """El caso que la prueba de 'sin sesion' no cubre: HAY un token guardado y un cliente registrado, pero el servidor ya no acepta el token. El SDK intentaria abrir el
    navegador por su cuenta; una herramienta no puede hacer eso. Tiene que salir la frase de volver a iniciar sesion y no abrirse nada."""
    _register_a_client()
    opened = []
    monkeypatch.setattr("webbrowser.open", lambda *a, **k: opened.append(a) or True)

    with pytest.raises(errors.M365Error) as caught:
        asyncio.run(client.call_tool("get_meetings", {}, url=rejecting_connector, timeout=10))

    assert opened == [], "una herramienta no puede abrir el navegador por su cuenta"
    assert caught.value.friendly.code == "M365-SIN-SESION", caught.value.friendly.code  # llego a pedir el navegador y se corto ahi (NeedsLogin), no por otra falla


def test_list_tools_returns_the_names_the_server_offers(fake_connector, signed_in):
    names = asyncio.run(client.list_tools(url=fake_connector))

    assert {"get_meetings", "slow", "boom"} <= set(names)


def test_call_tool_returns_the_text_of_the_result(fake_connector, signed_in):
    out = asyncio.run(client.call_tool("get_meetings", {"period": "week"}, url=fake_connector))

    assert out.text == "reuniones:week" and out.is_error is False


def test_a_tool_error_is_reported_as_an_error_result_not_as_a_crash(fake_connector, signed_in):
    out = asyncio.run(client.call_tool("boom", {}, url=fake_connector))

    assert out.is_error is True and out.text


def test_a_call_that_takes_too_long_is_cut_with_a_friendly_message(fake_connector, signed_in):
    with pytest.raises(errors.M365Error) as caught:
        asyncio.run(client.call_tool("slow", {"seconds": 3.0}, url=fake_connector, timeout=0.4))

    assert caught.value.friendly.code == "M365-DEMORA"


def test_an_unreachable_server_gives_the_no_connection_message(signed_in):
    with pytest.raises(errors.M365Error) as caught:
        asyncio.run(client.call_tool("get_meetings", {}, url=f"http://127.0.0.1:{_free_port()}/mcp", timeout=5))

    assert caught.value.friendly.code == "M365-SIN-CONEXION"


def test_the_token_never_comes_back_in_the_result(fake_connector, signed_in):
    out = asyncio.run(client.call_tool("get_meetings", {}, url=fake_connector))

    assert ACCESS not in out.text and ACCESS not in repr(out)
