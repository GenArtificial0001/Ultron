"""Interruptor de Outlook y Teams (spec office-outlook-teams-mcp REQ-M09): apagarlo saca TODAS las herramientas `m365_*` del modelo y las rechaza si igual las pide.

Microsoft 365 no es una conexion del gestor (no tiene campos ni credenciales: tiene un login), asi que NO entra en `describe_switches()` -- cuyo contrato fijan `test_switches.py` y
`test_switches_wiring.py` -- sino que tiene su propia clave y sus propias funciones en `app/switches.py`. Lo que se vigila:
1. ULTRON: por defecto esta APAGADO (spec portar-desde-origen, decision del usuario 2026-10-25); la persona lo prende. El resto de los
   tests arranca con el interruptor prendido, como lo dejaria alguien que lo activo.
2. Apagado, ninguna `m365_*` sale hacia el modelo y `dispatch` la rechaza antes de ejecutar nada (defensa en profundidad, como las conexiones).
3. Apagarlo NO cierra la sesion ni borra el token: volver a prenderlo no obliga a iniciar sesion de nuevo.
4. Un valor roto guardado nunca lo prende (ante la duda, apagado).
5. Las rutas lo leen y lo cambian solo desde esta PC y la propia pagina."""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from app import main, secret_store
from app import switches as sw
from app.m365 import service, token_store
from app.persistence import master_index
from app.tools import build_registry

BASE = "http://127.0.0.1:8020"


@pytest.fixture(autouse=True)
def _fresh_service():
    service.reset_for_tests()
    yield
    service.reset_for_tests()


@pytest.fixture(autouse=True)
def _turned_on_by_the_person(request):
    """ULTRON: viene apagado de fabrica. Salvo los tests de la instalacion limpia, cada test arranca con el interruptor PRENDIDO."""
    if not request.node.name.startswith(("test_it_is_off_by_default", "test_a_broken_stored_value")):
        sw.set_m365_enabled(True)
    yield


@pytest.fixture
def api():
    return TestClient(main.app, base_url=BASE, client=("127.0.0.1", 50000))


def test_it_is_off_by_default_in_ultron():
    assert sw.is_m365_enabled() is False


def test_turning_it_off_and_on_again():
    sw.set_m365_enabled(False)
    assert sw.is_m365_enabled() is False

    sw.set_m365_enabled(True)
    assert sw.is_m365_enabled() is True


def test_turning_it_on_stores_an_explicit_on():
    """ULTRON: sin clave guardada es apagado, asi que prenderlo deja un `0` explicito."""
    sw.set_m365_enabled(False)
    sw.set_m365_enabled(True)

    assert master_index.get_app_setting(sw.M365_DISABLED_KEY) == "0"


@pytest.mark.parametrize("garbage", ["", "no-es-json", "[1,2]", "null", "{}", "2"])
def test_a_broken_stored_value_never_turns_it_on(garbage: str):
    master_index.set_app_setting(sw.M365_DISABLED_KEY, garbage)

    assert sw.is_m365_enabled() is False


def test_the_m365_tools_are_filtered_by_their_prefix_not_by_a_hand_kept_list():
    """Una tool `m365_*` nueva tiene que quedar cubierta sola: si hubiera que acordarse de anotarla en una lista, la primera que se olvide seguiria saliendo con el interruptor apagado."""
    sw.set_m365_enabled(False)

    assert sw.tool_is_available("m365_get_emails") is False
    assert sw.tool_is_available("m365_una_herramienta_que_todavia_no_existe") is False
    assert sw.tool_is_available("read") is True and sw.tool_is_available("confluence_search") is True


def test_with_it_on_the_m365_tools_are_available():
    assert sw.tool_is_available("m365_get_emails") is True


def test_the_other_switches_keep_working_exactly_as_before():
    """Agregar este interruptor no cambia el contrato de `describe_switches()`: una conexion de Oracle, BigQuery o Confluence sigue igual."""
    sw.set_m365_enabled(False)

    assert all(not entry["key"].startswith("m365") for entry in sw.describe_switches())


def test_signing_in_state_is_untouched_by_the_switch():
    from mcp.shared.auth import OAuthToken

    if not secret_store.is_available():
        pytest.skip("DPAPI solo existe en Windows")
    asyncio.run(
        token_store.M365TokenStorage().set_tokens(
            OAuthToken(access_token="A" * 20, token_type="Bearer", expires_in=4471, refresh_token="R" * 20)
        )
    )

    sw.set_m365_enabled(False)
    sw.set_m365_enabled(True)

    assert token_store.has_session() is True, "apagar el interruptor no puede cerrar la sesion"


def test_dispatch_refuses_a_disabled_m365_tool_before_running_anything():
    from app.tools.base import Tool

    ran = []
    registry = build_registry()
    registry.register(
        Tool(
            name="m365_get_emails",
            description="x",
            parameters={"type": "object", "properties": {}},
            execute=lambda a, s=None: ran.append(1) or "ok",
        )
    )
    sw.set_m365_enabled(False)

    out = asyncio.run(registry.dispatch("m365_get_emails", json.dumps({})))

    assert "desactivada" in out and "Configuracion > Conexiones" in out
    assert ran == [], "una herramienta apagada no puede ejecutarse"


def test_definitions_leave_out_exactly_the_m365_tools_when_it_is_off():
    """Apagado, salen las 37 herramientas `m365_*` reales (28 de siempre + 9 de archivos, canales y confianza, spec m365-archivos-canales-confianza) y NINGUNA otra."""
    registry = build_registry()

    on = {d["function"]["name"] for d in registry.definitions()}
    sw.set_m365_enabled(False)
    off = {d["function"]["name"] for d in registry.definitions()}

    m365 = {name for name in on if name.startswith("m365_")}
    assert len(m365) == 37, "tienen que estar las 37 registradas con el interruptor encendido"
    assert not {name for name in off if name.startswith("m365_")}, "apagado no puede salir ninguna m365_*"
    assert on - off == m365, "apagar Outlook y Teams no puede sacar ninguna otra herramienta"
    assert off == on - m365


def test_a_new_m365_tool_added_later_is_turned_off_too_without_touching_any_list():
    """El filtro es por PREFIJO: una herramienta `m365_*` que se agregue manana queda cubierta sola."""
    from app.tools.base import Tool

    registry = build_registry()
    registry.register(Tool(name="m365_herramienta_futura", description="x", parameters={"type": "object", "properties": {}}, execute=lambda a, s=None: "ok"))
    assert "m365_herramienta_futura" in {d["function"]["name"] for d in registry.definitions()}

    sw.set_m365_enabled(False)

    assert "m365_herramienta_futura" not in {d["function"]["name"] for d in registry.definitions()}


def test_the_status_route_reports_whether_it_is_on(api):
    assert api.get("/api/m365/status").json()["enabled"] is True

    sw.set_m365_enabled(False)

    assert api.get("/api/m365/status").json()["enabled"] is False


def test_the_switch_route_turns_it_off_and_on(api):
    off = api.post("/api/m365/switch", json={"enabled": False})
    on = api.post("/api/m365/switch", json={"enabled": True})

    assert off.status_code == 200 and off.json()["enabled"] is False
    assert on.status_code == 200 and on.json()["enabled"] is True and sw.is_m365_enabled() is True


def test_the_switch_route_rejects_another_pc_and_another_website(api):
    remote = TestClient(main.app, base_url=BASE, client=("10.20.30.40", 50000))

    assert remote.post("/api/m365/switch", json={"enabled": False}).status_code == 403
    assert (
        api.post(
            "/api/m365/switch",
            json={"enabled": False},
            headers={"Origin": "https://sitio-ajeno.com", "Sec-Fetch-Site": "cross-site"},
        ).status_code
        == 403
    )
    assert sw.is_m365_enabled() is True, "un pedido rechazado no cambia nada"


def test_the_switch_route_needs_a_real_boolean(api):
    assert api.post("/api/m365/switch", json={"enabled": "no"}).status_code == 422
    assert api.post("/api/m365/switch", json={}).status_code == 422
    assert sw.is_m365_enabled() is True
