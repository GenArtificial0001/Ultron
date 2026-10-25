"""Las tools de datos eligen la CONEXION por nombre (spec conexiones-con-nombre REQ-003) y la correcta llega al conector.

Se prueba la capa de tool con conectores falsos que anotan con que `Settings` fueron construidos: asi se ve que `connection="Planta"` usa las credenciales de
Planta y nunca las de otra, que sin `connection` se usa la predeterminada, que un nombre inexistente devuelve un texto util (con los validos) y NO abre ninguna
conexion, y que una conexion que viene del `.env` de hoy sigue usando exactamente el `get_settings()` de siempre (compatibilidad, REQ-001)."""

from __future__ import annotations

import json
import pandas as pd
import pytest

from app import connections as conns
from app.config import Settings
from app.tools import dbquery as dq


def _base(**kw) -> Settings:
    return Settings(_env_file=None, **kw)


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setattr(conns, "CONNECTIONS_FILE", tmp_path / "connections.json")
    base = _base()
    monkeypatch.setattr(conns, "get_settings", lambda: base)
    monkeypatch.setattr(dq, "get_settings", lambda: base)
    conns.reset_for_tests()
    yield base
    conns.reset_for_tests()


def write(tmp_path, blocks):
    path = tmp_path / "connections.json"
    path.write_text(json.dumps({"connections": blocks}), encoding="utf-8")


def oracle(name, host):
    return {
        "type": "oracle",
        "name": name,
        "fields": {"oracle_user": "u", "oracle_password": "p", "oracle_host": host, "oracle_service_name": "svc"},
    }


def sharepoint(name, site):
    return {
        "type": "sharepoint",
        "name": name,
        "fields": {
            "sharepoint_site_url": site,
            "azure_client_id": "c",
            "azure_client_secret": "s",
            "azure_tenant_id": "t",
        },
    }


def sap(name, url):
    return {"type": "sap", "name": name, "fields": {"sap_base_url": url, "sap_user": "u", "sap_password": "p"}}


def fake(monkeypatch, name, **methods):
    """Conector falso que anota el `Settings` con el que se lo construyo."""

    class Fake:
        built_with: list = []
        calls: list = []

        def __init__(self, settings):
            Fake.built_with.append(settings)

    for method, outcome in methods.items():

        def call(self, *a, _o=outcome, _m=method, **kw):
            Fake.calls.append((_m, a, kw))
            return _o

        setattr(Fake, method, call)
    monkeypatch.setattr(dq, name, Fake)
    return Fake


FRAME = pd.DataFrame({"a": [1]})


# ---------------------------------------------------------------------------------------------------------------------
# Oracle
# ---------------------------------------------------------------------------------------------------------------------
def test_oracle_uses_the_credentials_of_the_requested_connection(monkeypatch, tmp_path):
    write(tmp_path, [oracle("Datamart", "h-datamart"), oracle("Planta", "h-planta")])
    f = fake(monkeypatch, "OracleConnector", fetch_query=FRAME)

    dq._oracle_query_execute({"query": "SELECT 1 FROM dual", "connection": "Planta"})

    assert [s.oracle_host for s in f.built_with] == ["h-planta"]


def test_oracle_without_connection_uses_the_default_one(monkeypatch, tmp_path):
    write(tmp_path, [oracle("Datamart", "h-datamart"), oracle("Planta", "h-planta")])
    f = fake(monkeypatch, "OracleConnector", fetch_query=FRAME)

    dq._oracle_query_execute({"query": "SELECT 1 FROM dual"})

    assert [s.oracle_host for s in f.built_with] == ["h-datamart"]


def test_an_unknown_connection_returns_the_valid_names_and_opens_nothing(monkeypatch, tmp_path):
    write(tmp_path, [oracle("Datamart", "h1"), oracle("Planta", "h2")])
    f = fake(monkeypatch, "OracleConnector", fetch_query=FRAME)

    out = dq._oracle_query_execute({"query": "SELECT 1 FROM dual", "connection": "Inexistente"})

    assert out.startswith("Error:") and "'Datamart'" in out and "'Planta'" in out
    assert f.built_with == [], "con un nombre inexistente no se debe abrir ninguna conexion"


def test_the_risk_check_still_runs_before_opening_a_named_connection(monkeypatch, tmp_path):
    write(tmp_path, [oracle("Planta", "h2")])
    f = fake(monkeypatch, "OracleConnector", fetch_query=FRAME)

    out = dq._oracle_query_execute({"query": "DROP TABLE x", "connection": "Planta"})

    assert "question" in out.lower() or "confirm" in out.lower()
    assert f.built_with == []


def test_a_connection_from_the_env_of_today_uses_the_global_settings_object(monkeypatch, _env):
    """Compatibilidad (REQ-001): sin archivo, la conexion «Datamart» sale del `.env` y se usa el MISMO objeto `get_settings()` de siempre."""
    base = _base(oracle_host="db-env", oracle_user="u", oracle_password="p", oracle_service_name="s")
    monkeypatch.setattr(conns, "get_settings", lambda: base)
    monkeypatch.setattr(dq, "get_settings", lambda: base)
    f = fake(monkeypatch, "OracleConnector", fetch_query=FRAME)

    dq._oracle_query_execute({"query": "SELECT 1 FROM dual"})

    assert f.built_with == [base]


def test_with_nothing_configured_the_tool_behaves_as_before(monkeypatch):
    f = fake(monkeypatch, "OracleConnector", fetch_query=FRAME)

    out = dq._oracle_query_execute({"query": "SELECT 1 FROM dual"})

    assert not out.startswith("Error: No hay ninguna conexion") and len(f.built_with) == 1


def test_the_oracle_switch_is_still_respected_for_a_named_connection(monkeypatch, tmp_path, _env):
    off = _base(enable_oracle=False)
    monkeypatch.setattr(conns, "get_settings", lambda: off)
    monkeypatch.setattr(dq, "get_settings", lambda: off)
    write(tmp_path, [oracle("Planta", "h2")])
    f = fake(monkeypatch, "OracleConnector", fetch_query=FRAME)

    out = dq._oracle_query_execute({"query": "SELECT 1 FROM dual", "connection": "Planta"})

    assert "ENABLE_ORACLE=false" in out and f.built_with == []


# ---------------------------------------------------------------------------------------------------------------------
# SharePoint
# ---------------------------------------------------------------------------------------------------------------------
def test_sharepoint_list_items_uses_the_requested_site(monkeypatch, tmp_path):
    write(
        tmp_path,
        [sharepoint("Sitio Logistico", "https://x/sites/log"), sharepoint("Sitio Calidad", "https://x/sites/cal")],
    )
    f = fake(monkeypatch, "SharePointConnector", fetch_list_items=FRAME)

    dq._sharepoint_list_items_execute({"list_name": "L", "connection": "Sitio Calidad"})

    assert [s.sharepoint_site_url for s in f.built_with] == ["https://x/sites/cal"]


def test_sharepoint_list_lists_uses_the_requested_site(monkeypatch, tmp_path):
    write(
        tmp_path,
        [sharepoint("Sitio Logistico", "https://x/sites/log"), sharepoint("Sitio Calidad", "https://x/sites/cal")],
    )
    f = fake(monkeypatch, "SharePointConnector", list_lists=["A", "B"])

    out = dq._sharepoint_list_lists_execute({"connection": "Sitio Calidad"})

    assert out == "Listas disponibles: A, B" and [s.sharepoint_site_url for s in f.built_with] == [
        "https://x/sites/cal"
    ]


def test_sharepoint_unknown_connection_lists_the_valid_names(monkeypatch, tmp_path):
    write(tmp_path, [sharepoint("Sitio Logistico", "https://x/sites/log")])
    f = fake(monkeypatch, "SharePointConnector", list_lists=["A"])

    out = dq._sharepoint_list_lists_execute({"connection": "Nope"})

    assert "'Sitio Logistico'" in out and f.built_with == []


# ---------------------------------------------------------------------------------------------------------------------
# SAP y Graph
# ---------------------------------------------------------------------------------------------------------------------
def test_sap_uses_the_requested_system(monkeypatch, tmp_path):
    write(tmp_path, [sap("Principal", "https://sap-prod"), sap("Calidad", "https://sap-qa")])
    f = fake(monkeypatch, "SapConnector", fetch_generic=FRAME)

    dq._sap_query_execute({"service_name": "S", "entity_name": "E", "connection": "Calidad"})

    assert [s.sap_base_url for s in f.built_with] == ["https://sap-qa"]


def test_graph_uses_the_azure_credentials_of_sitio_logistico_from_the_file(monkeypatch, tmp_path):
    write(tmp_path, [sharepoint("Sitio Calidad", "https://x/cal"), sharepoint("Sitio Logistico", "https://x/log")])
    # el secreto distinto permite saber de cual de los dos se tomo
    blocks = json.loads((tmp_path / "connections.json").read_text())["connections"]
    blocks[0]["fields"]["azure_client_secret"] = "secreto-calidad"
    blocks[1]["fields"]["azure_client_secret"] = "secreto-logistico"
    write(tmp_path, blocks)
    f = fake(monkeypatch, "GraphConnector", search=[])

    dq._graph_search_execute({"query": "algo"})

    assert [s.azure_client_secret for s in f.built_with] == ["secreto-logistico"]


def test_graph_lookup_and_manager_use_the_same_derived_credentials(monkeypatch, tmp_path):
    write(tmp_path, [sharepoint("Sitio Logistico", "https://x/log")])
    f = fake(
        monkeypatch, "GraphConnector", directory_lookup={"displayName": "n"}, directory_manager={"displayName": "m"}
    )

    dq._graph_directory_lookup_execute({"identifier": "a@b.c"})
    dq._graph_directory_manager_execute({"identifier": "a@b.c"})

    assert len(f.built_with) == 2 and all(s.sharepoint_site_url == "https://x/log" for s in f.built_with)


# ---------------------------------------------------------------------------------------------------------------------
# Lo que ve el modelo
# ---------------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "tool", [dq.oracle_query_tool, dq.sharepoint_list_items_tool, dq.sharepoint_list_lists_tool, dq.sap_query_tool]
)
def test_the_tools_advertise_the_connection_parameter_but_never_require_it(tool):
    props = tool.parameters["properties"]

    assert "connection" in props and props["connection"]["type"] == "string"
    assert "connection" not in tool.parameters.get("required", [])


def test_the_tools_that_have_no_selectable_connection_do_not_advertise_it():
    for tool in (
        dq.graph_search_tool,
        dq.graph_directory_lookup_tool,
        dq.graph_directory_manager_tool,
        dq.bigquery_query_tool,
    ):
        assert "connection" not in tool.parameters.get("properties", {}), tool.name


def test_a_result_does_not_leak_the_credentials_of_the_connection(monkeypatch, tmp_path):
    write(tmp_path, [oracle("Planta", "h-secreto-host")])
    f = fake(monkeypatch, "OracleConnector", fetch_query=FRAME)

    out = dq._oracle_query_execute({"query": "SELECT 1 FROM dual", "connection": "Planta"})

    assert "h-secreto-host" not in out and f.built_with


# ---------------------------------------------------------------------------------------------------------------------
# REQ-004: el error de una tool llega al MODELO (y de ahi al historial y al proveedor de IA): no puede llevar los valores de la conexion
# ---------------------------------------------------------------------------------------------------------------------
SECRET = "SECRETO-HERRAMIENTA-3e9b"


def _boom(monkeypatch, name, method, message):
    class Boom:
        def __init__(self, settings):
            self.settings = settings

        def __getattr__(self, attr):
            if attr != method:
                raise AttributeError(attr)

            def call(*a, **k):
                raise RuntimeError(message(self.settings))

            return call

    monkeypatch.setattr(dq, name, Boom)


def test_an_oracle_error_does_not_hand_the_password_to_the_model(monkeypatch, tmp_path):
    write(tmp_path, [{"type": "oracle", "name": "Planta", "fields": {"oracle_user": "usr-planta", "oracle_password": SECRET, "oracle_host": "h-planta", "oracle_service_name": "svc"}}])
    _boom(monkeypatch, "OracleConnector", "fetch_query", lambda s: f"ORA-12154 conectando usr-planta/{s.oracle_password}@h-planta/svc")

    out = dq._oracle_query_execute({"query": "SELECT 1 FROM dual", "connection": "Planta"})

    assert out.startswith("Error consultando Oracle:") and SECRET not in out and "h-planta" not in out and "usr-planta" not in out


def test_a_sharepoint_error_does_not_hand_the_client_secret_to_the_model(monkeypatch, tmp_path):
    write(tmp_path, [sharepoint("Sitio Logistico", "https://x/sites/log")])
    blocks = json.loads((tmp_path / "connections.json").read_text())["connections"]
    blocks[0]["fields"]["azure_client_secret"] = SECRET
    write(tmp_path, blocks)
    _boom(monkeypatch, "SharePointConnector", "fetch_list_items", lambda s: f"AADSTS7000215 invalid secret {s.azure_client_secret}")
    _boom_lists = dq._sharepoint_list_items_execute({"list_name": "L"})

    assert SECRET not in _boom_lists and "Error consultando SharePoint:" in _boom_lists


def test_a_sharepoint_lists_error_does_not_leak_either(monkeypatch, tmp_path):
    blocks = [sharepoint("Sitio Logistico", "https://x/sites/log")]
    blocks[0]["fields"]["azure_client_secret"] = SECRET
    write(tmp_path, blocks)
    _boom(monkeypatch, "SharePointConnector", "list_lists", lambda s: f"fallo con {s.azure_client_secret}")

    assert SECRET not in dq._sharepoint_list_lists_execute({})


def test_a_sap_error_does_not_hand_the_password_to_the_model(monkeypatch, tmp_path):
    write(tmp_path, [{"type": "sap", "name": "Principal", "fields": {"sap_base_url": "https://sap-interno", "sap_user": "usr-sap", "sap_password": SECRET}}])
    _boom(monkeypatch, "SapConnector", "fetch_generic", lambda s: f"401 en {s.sap_base_url} para usr-sap con {s.sap_password}")

    out = dq._sap_query_execute({"service_name": "S", "entity_name": "E"})

    assert SECRET not in out and "https://sap-interno" not in out


@pytest.mark.parametrize("tool", ["search", "lookup", "manager"])
def test_a_graph_error_does_not_hand_the_azure_secret_to_the_model(monkeypatch, tmp_path, tool):
    blocks = [sharepoint("Sitio Logistico", "https://x/sites/log")]
    blocks[0]["fields"]["azure_client_secret"] = SECRET
    write(tmp_path, blocks)
    method = {"search": "search", "lookup": "directory_lookup", "manager": "directory_manager"}[tool]
    _boom(monkeypatch, "GraphConnector", method, lambda s: f"invalid_client {s.azure_client_secret}")
    run = {
        "search": lambda: dq._graph_search_execute({"query": "x"}),
        "lookup": lambda: dq._graph_directory_lookup_execute({"identifier": "a@b.c"}),
        "manager": lambda: dq._graph_directory_manager_execute({"identifier": "a@b.c"}),
    }[tool]

    assert SECRET not in run()


def test_a_forbidden_graph_error_is_masked_too(monkeypatch, tmp_path):
    from app.connectors.graph_connector import GraphForbiddenError

    blocks = [sharepoint("Sitio Logistico", "https://x/sites/log")]
    blocks[0]["fields"]["azure_client_secret"] = SECRET
    write(tmp_path, blocks)

    class Forbidden:
        def __init__(self, settings):
            self.secret = settings.azure_client_secret

        def search(self, *a, **k):
            raise GraphForbiddenError(f"403 para el secret {self.secret}")

    monkeypatch.setattr(dq, "GraphConnector", Forbidden)

    out = dq._graph_search_execute({"query": "x"})

    assert out.startswith("[gate 403") and SECRET not in out


def test_an_error_from_the_env_connection_keeps_the_original_text(monkeypatch):
    """Sin archivo la conexion viene del `.env` de hoy: no hay nada que enmascarar y el mensaje del conector llega intacto, como antes."""
    _boom(monkeypatch, "OracleConnector", "fetch_query", lambda s: "ORA-00942: la tabla no existe")

    assert dq._oracle_query_execute({"query": "SELECT 1 FROM x"}) == "Error consultando Oracle: ORA-00942: la tabla no existe"


@pytest.mark.parametrize("tool", ["lookup", "manager"])
def test_a_graph_connection_error_is_masked_too(monkeypatch, tmp_path, tool):
    """`GraphConnectionError` tiene su propia rama en lookup y manager (devuelve `Error: ...` al modelo): tambien tiene que enmascararse."""
    from app.connectors.graph_connector import GraphConnectionError

    blocks = [sharepoint("Sitio Logistico", "https://x/sites/log")]
    blocks[0]["fields"]["azure_client_secret"] = SECRET
    write(tmp_path, blocks)

    class Down:
        def __init__(self, settings):
            self.secret = settings.azure_client_secret

        def directory_lookup(self, *a, **k):
            raise GraphConnectionError(f"no hay token con {self.secret}")

        directory_manager = directory_lookup

    monkeypatch.setattr(dq, "GraphConnector", Down)
    run = dq._graph_directory_lookup_execute if tool == "lookup" else dq._graph_directory_manager_execute

    out = run({"identifier": "a@b.c"})

    assert out.startswith("Error:") and SECRET not in out
