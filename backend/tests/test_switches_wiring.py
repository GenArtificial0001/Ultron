"""El enchufe de los interruptores (spec interruptores-conexiones-skills REQ-001/002/003/007): lo que el MODELO realmente recibe y lo que pasa si igual llama.

`test_switches.py` prueba el estado; aca se prueban los tres puntos donde ese estado manda: el payload de tools (`ToolRegistry.definitions`), el rechazo en `dispatch` ANTES de
abrir ninguna conexion de red, el bloque de conexiones del prompt y el estado del sidebar (que deja de probar lo apagado)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app import connections as conns
from app import connectors_status as cs
from app import switches as sw
from app.config import Settings
from app.persistence import master_index
from app.tools import build_registry
from app.tools import dbquery as dq

@pytest.fixture(autouse=True)
def _ultron_m365_on():
    """ULTRON trae Outlook y Teams APAGADO de fabrica (spec portar-desde-origen): estos tests prueban el comportamiento con el interruptor
    prendido, como lo deja la persona que lo activo. Los que prueban el apagado lo apagan ellos mismos."""
    from app import switches as _sw

    _sw.set_m365_enabled(True)
    yield


@pytest.fixture(autouse=True)
def _ultron_office_on():
    """ULTRON trae Office APAGADO de fabrica (spec portar-desde-origen): estos tests prueban el comportamiento con el interruptor
    prendido, como lo deja la persona que lo activo. Los que prueban el apagado lo apagan ellos mismos."""
    from app import switches as _sw

    _sw.set_office_enabled(True)
    yield


def _base(**kw) -> Settings:
    return Settings(_env_file=None, **kw)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    store: dict[str, str] = {}
    monkeypatch.setattr(master_index, "get_app_setting", lambda k: store.get(k))
    monkeypatch.setattr(master_index, "set_app_setting", lambda k, v: store.__setitem__(k, v))
    monkeypatch.setattr(master_index, "unset_app_setting", lambda k: store.pop(k, None))
    monkeypatch.setattr(conns, "CONNECTIONS_FILE", Path("no-existe-connections.json"))
    env = _base(
        oracle_host="db", oracle_user="u", oracle_password="p", oracle_service_name="s",
        sharepoint_site_url="https://x/sites/l", azure_client_id="c", azure_client_secret="s", azure_tenant_id="t",
        sap_base_url="https://sap", sap_user="su", sap_password="sp",
        confluence_email="a@b.com", confluence_api_token="tok-123456", bq_project_id="proyecto-bq",
    )
    monkeypatch.setattr(conns, "get_settings", lambda: env)
    monkeypatch.setattr("app.config.get_settings", lambda: env)
    monkeypatch.setattr(dq, "get_settings", lambda: env)
    monkeypatch.setattr(cs, "get_settings", lambda: env)
    conns.reset_for_tests()
    yield store
    conns.reset_for_tests()


def sent_tool_names(registry=None, **kw) -> set[str]:
    registry = registry or build_registry()
    return {d["function"]["name"] for d in registry.definitions(**kw)}


def write_connections(monkeypatch, tmp_path, blocks):
    path = tmp_path / "connections.json"
    path.write_text(json.dumps({"connections": blocks}), encoding="utf-8")
    monkeypatch.setattr(conns, "CONNECTIONS_FILE", path)
    conns.reset_for_tests()


def oracle_block(name):
    return {"type": "oracle", "name": name, "fields": {"oracle_user": "u", "oracle_password": "p", "oracle_host": "h", "oracle_service_name": "s"}}


# ---------------------------------------------------------------------------------------------------------------------
# REQ-001/002 lo que se le manda al modelo
# ---------------------------------------------------------------------------------------------------------------------
# Herramientas de Vision SIN contar Outlook y Teams (spec office-outlook-teams-mcp: 37 `m365_*` que se apagan con SU interruptor, ver test_m365_switch.py). Se escribe como suma para que
# el proximo cambio no obligue a adivinar de donde sale el total.
CORE_TOOLS = 28
M365_TOOLS = 37  # 28 de Outlook y Teams + 9 de OneDrive, canales y `trust_senders` (spec m365-archivos-canales-confianza)
OFFICE_TOOLS = 4  # spec office-com-automatizacion: office_excel_list_workbooks, _read_range, _write_range y office_convert


def test_with_nothing_off_the_model_gets_every_tool_exactly_as_before():
    assert len(sent_tool_names()) == len(build_registry().all()) == CORE_TOOLS + M365_TOOLS + OFFICE_TOOLS


def test_turning_confluence_off_takes_its_six_tools_out_of_what_the_model_receives():
    sw.set_connection_enabled("confluence:principal", False)

    sent = sent_tool_names()

    assert not [n for n in sent if n.startswith("confluence_")] and len(sent) == CORE_TOOLS + M365_TOOLS + OFFICE_TOOLS - 6


def test_turning_one_connection_off_leaves_the_tools_of_the_others_alone():
    sw.set_connection_enabled("confluence:principal", False)

    sent = sent_tool_names()

    assert {"oracle_query", "sap_query", "sharepoint_list_items", "bigquery_query", "graph_search", "read", "bash", "skill"} <= sent


def test_the_user_can_work_with_only_bigquery(monkeypatch):
    """Caso del pedido: «si quiero trabajar con solo BigQuery y no quiero que consulte en SharePoint, apago eso»."""
    for key in ("oracle:datamart", "sharepoint:sitio logistico", "sap:principal", "confluence:principal", "graph:principal"):
        if key in sw.all_connection_keys():
            sw.set_connection_enabled(key, False)

    sent = sent_tool_names()

    assert {"bigquery_query", "bigquery_list_tables"} <= sent
    assert not {n for n in sent if n.split("_")[0] in ("oracle", "sap", "sharepoint", "confluence", "graph")}


def test_two_oracle_with_one_off_keep_the_oracle_tool_and_its_connection_argument_only_accepts_the_active_one(monkeypatch, tmp_path):
    write_connections(monkeypatch, tmp_path, [oracle_block("Datamart"), oracle_block("Planta")])
    sw.set_connection_enabled("oracle:planta", False)

    assert "oracle_query" in sent_tool_names()
    with pytest.raises(sw.DisabledConnectionError):
        sw.resolve_enabled("oracle", "Planta")
    assert sw.resolve_enabled("oracle", "Datamart").name == "Datamart"
    assert sw.resolve_enabled("oracle").name == "Datamart", "sin nombre usa la activa, no la apagada"


def test_a_disabled_default_falls_back_to_the_first_active_one(monkeypatch, tmp_path):
    block = oracle_block("Datamart")
    block["default"] = True
    write_connections(monkeypatch, tmp_path, [block, oracle_block("Planta")])
    sw.set_connection_enabled("oracle:datamart", False)

    assert sw.resolve_enabled("oracle").name == "Planta"


def test_with_every_oracle_off_the_oracle_tool_disappears(monkeypatch, tmp_path):
    write_connections(monkeypatch, tmp_path, [oracle_block("Datamart"), oracle_block("Planta")])
    sw.set_connection_enabled("oracle:datamart", False)
    sw.set_connection_enabled("oracle:planta", False)

    assert "oracle_query" not in sent_tool_names()
    with pytest.raises(sw.DisabledConnectionError):
        sw.resolve_enabled("oracle")


def test_the_plan_mode_filter_and_the_switch_filter_add_up():
    sw.set_connection_enabled("confluence:principal", False)

    plan = sent_tool_names(exclude_dangerous=True)

    assert "bash" not in plan and "write" not in plan and not [n for n in plan if n.startswith("confluence_")]


def test_the_filter_is_decided_on_every_call_not_once_at_startup():
    registry = build_registry()
    before = sent_tool_names(registry)

    sw.set_connection_enabled("confluence:principal", False)
    middle = sent_tool_names(registry)
    sw.set_connection_enabled("confluence:principal", True)
    after = sent_tool_names(registry)

    assert before == after and len(middle) == len(before) - 6, "apagar y prender se refleja en el turno siguiente, sin reiniciar"


# ---------------------------------------------------------------------------------------------------------------------
# REQ-003 defensa en profundidad: no se abre nada
# ---------------------------------------------------------------------------------------------------------------------
class Counting:
    built = 0

    def __init__(self, settings):
        Counting.built += 1

    def __getattr__(self, name):
        def call(*a, **k):
            return None

        return call


def test_dispatching_a_tool_of_a_disabled_connection_is_refused_before_anything_runs(monkeypatch):
    import app.tools.confluence as cf

    Counting.built = 0
    monkeypatch.setattr(cf, "ConfluenceConnector", Counting)
    sw.set_connection_enabled("confluence:principal", False)

    out = asyncio.run(build_registry().dispatch("confluence_search", json.dumps({"query": "x"})))

    assert "desactivada" in out and "Configuracion > Conexiones" in out
    assert Counting.built == 0, "no se debe construir ningun conector de una conexion apagada"


def test_the_refusal_message_has_no_credentials(monkeypatch):
    sw.set_connection_enabled("confluence:principal", False)

    out = asyncio.run(build_registry().dispatch("confluence_get_page", json.dumps({"ref": "9"})))

    assert "tok-123456" not in out and "a@b.com" not in out


def test_an_oracle_call_naming_a_disabled_connection_never_opens_it(monkeypatch, tmp_path):
    write_connections(monkeypatch, tmp_path, [oracle_block("Datamart"), oracle_block("Planta")])
    Counting.built = 0
    monkeypatch.setattr(dq, "OracleConnector", Counting)
    sw.set_connection_enabled("oracle:planta", False)

    out = dq._oracle_query_execute({"query": "SELECT 1 FROM dual", "connection": "Planta"})

    assert out.startswith("Error:") and "desactivada" in out and Counting.built == 0


def test_an_oracle_call_without_a_name_uses_the_active_connection(monkeypatch, tmp_path):
    write_connections(monkeypatch, tmp_path, [oracle_block("Datamart"), oracle_block("Planta")])
    seen = []

    class Fake:
        def __init__(self, settings):
            seen.append(settings.oracle_host)

        def fetch_query(self, *a, **k):
            import pandas as pd

            return pd.DataFrame({"a": [1]})

    monkeypatch.setattr(dq, "OracleConnector", Fake)
    block_a = oracle_block("Datamart")
    block_a["fields"]["oracle_host"] = "h-datamart"
    block_b = oracle_block("Planta")
    block_b["fields"]["oracle_host"] = "h-planta"
    write_connections(monkeypatch, tmp_path, [block_a, block_b])
    sw.set_connection_enabled("oracle:datamart", False)

    dq._oracle_query_execute({"query": "SELECT 1 FROM dual"})

    assert seen == ["h-planta"]


@pytest.mark.parametrize("tool,args", [("sharepoint_list_lists", {}), ("sap_query", {"service_name": "S", "entity_name": "E"}), ("oracle_query", {"query": "SELECT 1 FROM dual"})])
def test_every_data_tool_refuses_when_its_only_connection_is_off(tool, args):
    key = {"sharepoint_list_lists": "sharepoint:sitio logistico", "sap_query": "sap:principal", "oracle_query": "oracle:datamart"}[tool]
    sw.set_connection_enabled(key, False)

    out = asyncio.run(build_registry().dispatch(tool, json.dumps(args)))

    assert "desactivada" in out


def test_the_confluence_endpoints_answer_409_for_a_disabled_connection(monkeypatch):
    from fastapi.testclient import TestClient

    import app.main as main

    monkeypatch.setattr(main, "get_settings", lambda: _base(confluence_email="a@b.com", confluence_api_token="tok-123456"))
    monkeypatch.setattr(main, "ConfluenceConnector", Counting)
    Counting.built = 0
    sw.set_connection_enabled("confluence:principal", False)

    client = TestClient(main.app)
    statuses = [
        client.get("/api/confluence/tree").status_code,
        client.get("/api/confluence/children", params={"id": "5"}).status_code,
        client.get("/api/confluence/page", params={"ref": "9"}).status_code,
    ]

    assert statuses == [409, 409, 409] and Counting.built == 0


# ---------------------------------------------------------------------------------------------------------------------
# prompt y estado del sidebar
# ---------------------------------------------------------------------------------------------------------------------
def test_a_disabled_connection_is_not_named_in_the_prompt(monkeypatch, tmp_path):
    write_connections(monkeypatch, tmp_path, [oracle_block("Datamart"), oracle_block("Planta")])
    assert "Planta" in conns.describe_for_prompt()

    sw.set_connection_enabled("oracle:planta", False)

    block = conns.describe_for_prompt()
    assert "Planta" not in block, "una conexion apagada no existe para el modelo"


def test_the_prompt_block_disappears_when_only_one_connection_of_a_type_is_left(monkeypatch, tmp_path):
    write_connections(monkeypatch, tmp_path, [oracle_block("Datamart"), oracle_block("Planta")])
    sw.set_connection_enabled("oracle:planta", False)

    assert conns.describe_for_prompt() == "", "con una sola activa el modelo no tiene nada que elegir"


def test_the_status_panel_does_not_probe_a_disabled_connection(monkeypatch):
    import app.connectors.confluence_connector as cc
    import app.connectors.oracle_connector as oc

    probed = []

    class Fake:
        def __init__(self, settings):
            self.kind = type(self).__name__

        def test_connection(self):
            probed.append(self.kind)
            return True

    class FakeOracle(Fake):
        pass

    class FakeConfluence(Fake):
        pass

    monkeypatch.setattr(oc, "OracleConnector", FakeOracle)
    monkeypatch.setattr(cc, "ConfluenceConnector", FakeConfluence)
    sw.set_connection_enabled("confluence:principal", False)

    result = asyncio.run(cs.check_all_connectors())

    assert "FakeConfluence" not in probed and "FakeOracle" in probed
    assert "Confluence - Principal" not in [r["display"] for r in result]


def test_the_status_panel_does_not_probe_bigquery_when_it_is_off(monkeypatch):
    probed = []

    class FakeBq:
        def __init__(self, settings):
            pass

        def test_connection(self):
            probed.append("bq")
            return True

    monkeypatch.setattr(cs, "BigQueryConnector", FakeBq)
    sw.set_connection_enabled("bigquery:bigquery", False)

    result = asyncio.run(cs.check_all_connectors())

    assert probed == [] and "bigquery" not in [r["name"] for r in result]


def test_turning_a_connection_back_on_makes_it_appear_in_the_panel_again(monkeypatch):
    import app.connectors.confluence_connector as cc

    class Fake:
        def __init__(self, settings):
            pass

        def test_connection(self):
            return True

    monkeypatch.setattr(cc, "ConfluenceConnector", Fake)
    sw.set_connection_enabled("confluence:principal", False)
    sw.set_connection_enabled("confluence:principal", True)

    assert "Confluence - Principal" in [r.get("display") for r in asyncio.run(cs.check_all_connectors())]


# ---------------------------------------------------------------------------------------------------------------------
# Dos barreras INDEPENDIENTES para una tool de una conexion apagada: cada una se prueba sola
# ---------------------------------------------------------------------------------------------------------------------
def test_barrier_one_the_registry_dispatch_refuses_even_if_the_tool_itself_would_not(monkeypatch):
    """Barrera 1 (`ToolRegistry.dispatch`): rechaza ANTES de llamar a la tool. Se prueba con una tool que NO tiene ninguna defensa propia y que registra si se la ejecuto."""
    from app.tools.base import Tool, ToolRegistry

    ran = []
    registry = ToolRegistry()
    registry.register(Tool(name="confluence_search", description="d", parameters={"type": "object", "properties": {}}, execute=lambda a, s=None: ran.append(1) or "ejecuto"))
    sw.set_connection_enabled("confluence:principal", False)

    out = asyncio.run(registry.dispatch("confluence_search", "{}"))

    assert ran == [] and "desactivada" in out


def test_barrier_two_the_tool_itself_refuses_even_if_the_registry_did_not_filter(monkeypatch):
    """Barrera 2 (la propia tool de Confluence): rechaza aunque se la llame directo, salteando el registro. Hay llamadas internas que no pasan por `dispatch`."""
    import app.tools.confluence as cf

    Counting.built = 0
    monkeypatch.setattr(cf, "ConfluenceConnector", Counting)
    sw.set_connection_enabled("confluence:principal", False)

    out = cf._get_page_execute({"ref": "9"})

    assert out.startswith("Error:") and "desactivada" in out and Counting.built == 0


def test_the_rejection_message_names_the_connection_but_carries_none_of_its_fields(monkeypatch, tmp_path):
    block = oracle_block("Planta")
    block["fields"]["oracle_password"] = "CLAVE-SECRETA-DE-PLANTA"
    block["fields"]["oracle_host"] = "host-secreto-planta"
    write_connections(monkeypatch, tmp_path, [oracle_block("Datamart"), block])
    sw.set_connection_enabled("oracle:planta", False)

    with pytest.raises(sw.DisabledConnectionError) as err:
        sw.resolve_enabled("oracle", "Planta")

    text = str(err.value)
    assert "Planta" in text and "CLAVE-SECRETA-DE-PLANTA" not in text and "host-secreto-planta" not in text
    assert "{" not in text, "ningun diccionario de campos en el mensaje"


# ---------------------------------------------------------------------------------------------------------------------
# Barrera 2 tambien en Graph y BigQuery. Hueco encontrado al retomar la spec (2026-10-08): las tools de Graph descartaban el error de `_connection_context` (con Graph
# apagado quedaba `GraphConnector(None)`, que cae a `get_settings()` y abre la conexion) y las de BigQuery no miraban los interruptores. Solo las frenaba `dispatch`.
# ---------------------------------------------------------------------------------------------------------------------
GRAPH_TOOLS = [
    ("_graph_search_execute", {"query": "x"}),
    ("_graph_directory_lookup_execute", {"identifier": "a@b.com"}),
    ("_graph_directory_manager_execute", {"identifier": "a@b.com"}),
]
BIGQUERY_TOOLS = [("_bigquery_query_execute", {"query": "SELECT 1"}), ("_bigquery_list_tables_execute", {})]


class CountingDict(Counting):
    """Como `Counting`, pero cada metodo devuelve un dict vacio: las tools de directorio de Graph leen el resultado con `.get` FUERA de su try."""

    def __getattr__(self, name):
        return lambda *a, **k: {}


def _env_without(monkeypatch, **overrides) -> None:
    """Reemplaza el `.env` del fixture por uno igual pero con `overrides` (ej. BigQuery o SharePoint sin configurar)."""
    fields = dict(
        oracle_host="db", oracle_user="u", oracle_password="p", oracle_service_name="s",
        sharepoint_site_url="https://x/sites/l", azure_client_id="c", azure_client_secret="s", azure_tenant_id="t",
        sap_base_url="https://sap", sap_user="su", sap_password="sp",
        confluence_email="a@b.com", confluence_api_token="tok-123456", bq_project_id="proyecto-bq",
    )
    fields.update(overrides)
    env = _base(**fields)
    for target in (conns, dq, cs):
        monkeypatch.setattr(target, "get_settings", lambda: env)
    monkeypatch.setattr("app.config.get_settings", lambda: env)
    conns.reset_for_tests()


@pytest.mark.parametrize("fn,args", GRAPH_TOOLS)
def test_barrier_two_a_graph_tool_called_directly_refuses_when_graph_is_off(monkeypatch, fn, args):
    Counting.built = 0
    monkeypatch.setattr(dq, "GraphConnector", CountingDict)
    sw.set_connection_enabled("graph:principal", False)

    out = getattr(dq, fn)(args)

    assert out.startswith("Error:") and "desactivada" in out and Counting.built == 0


@pytest.mark.parametrize("fn,args", BIGQUERY_TOOLS)
def test_barrier_two_a_bigquery_tool_called_directly_refuses_when_bigquery_is_off(monkeypatch, fn, args):
    Counting.built = 0
    monkeypatch.setattr(dq, "BigQueryConnector", CountingDict)
    sw.set_connection_enabled("bigquery:bigquery", False)

    out = getattr(dq, fn)(args)

    assert out.startswith("Error:") and "desactivada" in out and Counting.built == 0


@pytest.mark.parametrize("fn,args", GRAPH_TOOLS + BIGQUERY_TOOLS)
def test_barrier_two_another_connection_off_does_not_block_graph_or_bigquery(monkeypatch, fn, args):
    """Con OTRA conexion apagada (la lista guardada no esta vacia), Graph y BigQuery siguen abriendo su conector como siempre."""
    Counting.built = 0
    monkeypatch.setattr(dq, "GraphConnector", CountingDict)
    monkeypatch.setattr(dq, "BigQueryConnector", CountingDict)
    sw.set_connection_enabled("confluence:principal", False)

    getattr(dq, fn)(args)

    assert Counting.built == 1


@pytest.mark.parametrize("fn,args", BIGQUERY_TOOLS)
def test_barrier_two_a_stale_switch_of_an_unconfigured_bigquery_does_not_block_it(monkeypatch, _isolated, fn, args):
    """Una clave apagada que quedo guardada de cuando BigQuery estaba configurada no la frena si hoy no esta configurada: la pantalla ya no la muestra y el usuario no
    podria prenderla. Sigue el camino de siempre, igual que `tool_is_available` (la tool explica lo que falta)."""
    _isolated[sw.CONNECTIONS_DISABLED_KEY] = json.dumps(["bigquery:bigquery"])
    _env_without(monkeypatch, bq_project_id="")
    Counting.built = 0
    monkeypatch.setattr(dq, "BigQueryConnector", CountingDict)

    out = getattr(dq, fn)(args)

    assert "desactivada" not in out and Counting.built == 1


@pytest.mark.parametrize("fn,args", GRAPH_TOOLS)
def test_barrier_two_a_stale_switch_of_an_unconfigured_graph_does_not_block_it(monkeypatch, _isolated, fn, args):
    """Igual para Graph: sin la SharePoint de la que toma sus credenciales, Graph no esta configurada y una clave vieja no la frena."""
    _isolated[sw.CONNECTIONS_DISABLED_KEY] = json.dumps(["graph:principal"])
    _env_without(monkeypatch, sharepoint_site_url="", azure_client_id="", azure_client_secret="", azure_tenant_id="")
    Counting.built = 0
    monkeypatch.setattr(dq, "GraphConnector", CountingDict)

    out = getattr(dq, fn)(args)

    assert "desactivada" not in out and Counting.built == 1


def test_ultron_ships_office_and_outlook_teams_off(monkeypatch):
    """ULTRON (spec portar-desde-origen, decision del usuario 2026-10-25): en una instalacion limpia, ninguna herramienta de Office ni de
    Outlook y Teams sale hacia el modelo hasta que la persona las prende."""
    from app import switches as sw
    from app.persistence import master_index

    master_index.unset_app_setting(sw.OFFICE_DISABLED_KEY)
    master_index.unset_app_setting(sw.M365_DISABLED_KEY)
    names = sent_tool_names()
    assert not any(n.startswith(("office_", "m365_")) for n in names)
    assert len(names) == CORE_TOOLS


def test_building_the_tool_list_reads_the_switches_once_not_once_per_tool(monkeypatch):
    """ULTRON (2026-10-25): armar las definiciones en cada paso abria la base una vez por tool (69 por paso)."""
    from app.persistence import master_index

    reads = []
    real = master_index.get_app_setting
    monkeypatch.setattr(master_index, "get_app_setting", lambda key: reads.append(key) or real(key))
    build_registry().definitions()
    assert len(reads) <= 4, reads  # Office, Outlook/Teams y la lista de conexiones apagadas: una vez cada uno


@pytest.mark.parametrize("office,m365", [(True, True), (False, False), (True, False), (False, True)])
def test_the_one_shot_filter_decides_exactly_like_the_per_tool_check(office, m365):
    from app import switches as sw

    sw.set_office_enabled(office)
    sw.set_m365_enabled(m365)
    available = sw.availability_filter()
    assert [t.name for t in build_registry().all() if available(t.name)] == [
        t.name for t in build_registry().all() if sw.tool_is_available(t.name)
    ]
