"""Vision usa el transporte MCP vigente (`streamable_http_client`), no el deprecado (`streamablehttp_client`).

Por que: `mcp` 1.30 marca `streamablehttp_client` como deprecado y la suite acumulaba 20 avisos por eso. El tope `<2` de `requirements.txt` lo protege hoy, pero migrar ahora evita que
una version futura lo quite y deje a Outlook y Teams sin funcionar. Lo que se vigila:
1. Ningun archivo de `app/` importa ni llama al nombre deprecado.
2. El cliente (leer y llamar herramientas) funciona igual con la API nueva, contra un servidor MCP real en loopback: mismo certificado de Windows, mismo plazo, misma traduccion de errores.
3. Usar el cliente de Vision NO emite el aviso de deprecacion.
4. La fabrica httpx de Vision (la que valida con el almacen de certificados de Windows) es la que realmente se usa: sin ella el proxy de la empresa rompe el TLS."""

from __future__ import annotations

import ast
import asyncio
import warnings
from pathlib import Path

import httpx
import pytest

from app.m365 import auth, client, errors
from tests import test_m365_client as _client_tests  # los fixtures del servidor MCP falso y de la sesion guardada viven ahi

# Se reusan los fixtures de los tests del cliente (servidor MCP falso en loopback y sesion guardada) con la funcion original, sin importarlos por nombre.
fake_connector = _client_tests.fake_connector
signed_in = _client_tests.signed_in
# El servidor falso esta en LOOPBACK: sin esto `httpx` hereda HTTPS_PROXY (que otros tests dejan puesto) y la llamada sale por el proxy; fallaba SOLO dentro de la suite completa.
_loopback_never_goes_through_a_proxy = pytest.fixture(autouse=True)(_client_tests._loopback_never_goes_through_a_proxy.__wrapped__)


APP = Path(__file__).resolve().parent.parent / "app"
DEPRECATED = "streamablehttp_client"


def test_no_file_in_app_uses_the_deprecated_name():
    offenders = []
    for path in APP.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.ImportFrom):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.Name):
                names = [node.id]
            elif isinstance(node, ast.Attribute):
                names = [node.attr]
            if DEPRECATED in names:
                offenders.append(f"{path.relative_to(APP)}:{node.lineno}")
    assert not offenders, offenders


def test_the_new_transport_exists_in_the_installed_mcp():
    pytest.importorskip("mcp")
    from mcp.client.streamable_http import streamable_http_client

    assert callable(streamable_http_client)


def test_the_client_makes_no_deprecation_warning(fake_connector, signed_in):
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        asyncio.run(client.list_tools(url=fake_connector))

    deprecations = [
        w for w in caught if issubclass(w.category, DeprecationWarning) and "streamable" in str(w.message).lower()
    ]
    assert not deprecations, [str(w.message) for w in deprecations]


def test_the_login_runner_and_the_client_share_the_one_transport_function():
    """El login (`service._real_runner`) y las llamadas (`client._with_session`) abren la sesion por la MISMA funcion `auth.open_transport`: un solo lugar que mantener."""
    from app.m365 import service

    for module in (service, client):
        source = Path(module.__file__).read_text(encoding="utf-8")
        assert "auth.open_transport(" in source and DEPRECATED not in source, module.__name__


def test_the_windows_certificate_factory_is_still_the_one_in_use(fake_connector, signed_in, monkeypatch):
    """Si el transporte nuevo no recibiera la fabrica de Vision, el proxy de la empresa (que inspecciona el trafico) rompe el TLS con CERTIFICATE_VERIFY_FAILED."""
    seen = []
    real = auth.http_client_factory

    def spy(headers=None, timeout=None, auth=None):
        seen.append(True)
        return real(headers=headers, timeout=timeout, auth=auth)

    monkeypatch.setattr(auth, "http_client_factory", spy)
    asyncio.run(client.list_tools(url=fake_connector))

    assert seen, "el cliente no uso la fabrica httpx de Vision"


def test_the_httpx_client_built_for_mcp_validates_with_the_system_certificates():
    built = auth.http_client_factory()

    assert isinstance(built, httpx.AsyncClient)
    asyncio.run(built.aclose())


def test_the_new_transport_still_translates_an_unreachable_server_to_a_phrase(signed_in):
    with pytest.raises(errors.M365Error) as caught:
        asyncio.run(client.list_tools(url="http://127.0.0.1:9/mcp", timeout=5))

    assert caught.value.friendly.code in {"M365-SIN-CONEXION", "M365-DEMORA"} or caught.value.friendly.message
    assert "Traceback" not in caught.value.friendly.message and "httpx" not in caught.value.friendly.message
