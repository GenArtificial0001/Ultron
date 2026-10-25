"""Tools de consulta que ve el modelo (`app/tools/dbquery.py`, BL-006): listados de BigQuery y SharePoint, SAP, Microsoft Graph y los avisos de conector apagado.

Los conectores en si ya tienen sus tests (`test_sap_connector`, `test_graph_connector`, `test_bigquery_connector`, `test_sharepoint_connector`); aca se prueba la
CAPA DE TOOL: que valide los argumentos, respete los interruptores `ENABLE_*`, convierta cada error en un texto util para el modelo (nunca una excepcion) y dé a
los errores de permiso (403) el trato distinto que pide la skill de investigacion interna. Todo con conectores falsos: sin red ni credenciales.
"""

from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

from app.connectors.graph_connector import GraphConnectionError, GraphForbiddenError
from app.tools import dbquery as dq


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    s = SimpleNamespace(
        enable_oracle=True,
        enable_bigquery=True,
        enable_sharepoint=True,
        enable_sap=True,
        export_row_threshold=50,
        export_preview_rows=10,
        bq_cost_guardrail_bytes=10**12,
    )
    monkeypatch.setattr(dq, "get_settings", lambda: s)
    return s


def fake(monkeypatch, name, **methods):
    """Reemplaza el conector `name` de dbquery por una clase falsa cuyos metodos son los de `methods` (cada uno puede ser un valor o una excepcion)."""

    class Fake:
        calls: list = []

        def __init__(self, settings):
            pass

    for method, outcome in methods.items():

        def call(self, *a, _o=outcome, _m=method, **kw):
            Fake.calls.append((_m, a, kw))
            if isinstance(_o, Exception):
                raise _o
            return _o

        setattr(Fake, method, call)
    monkeypatch.setattr(dq, name, Fake)
    return Fake


# ---------------------------------------------------------------------------
# Conector apagado o argumentos faltantes (Oracle y BigQuery)
# ---------------------------------------------------------------------------
class TestOracleAndBigQueryGates:
    def test_oracle_off_says_which_switch(self, settings):
        settings.enable_oracle = False

        assert "ENABLE_ORACLE=false" in dq._oracle_query_execute({"query": "SELECT 1"}, None)

    def test_oracle_without_a_query_asks_for_it(self):
        assert "falta el argumento 'query'" in dq._oracle_query_execute({}, None)
        assert "falta el argumento 'query'" in dq._oracle_query_execute({"query": ""}, None)

    def test_oracle_failure_is_returned_as_text_not_raised(self, monkeypatch):
        fake(monkeypatch, "OracleConnector", fetch_query=RuntimeError("ORA-12541: sin listener"))

        out = dq._oracle_query_execute({"query": "SELECT 1 FROM DUAL"}, None)

        assert out.startswith("Error") and "ORA-12541" in out

    def test_bigquery_off_says_which_switch(self, settings):
        settings.enable_bigquery = False

        assert "ENABLE_BIGQUERY=false" in dq._bigquery_query_execute({"query": "SELECT 1"}, None)

    def test_bigquery_without_a_query_asks_for_it(self):
        assert "falta el argumento 'query'" in dq._bigquery_query_execute({}, None)


# ---------------------------------------------------------------------------
# bigquery_list_tables
# ---------------------------------------------------------------------------
class TestBigQueryListTables:
    def test_without_a_dataset_it_lists_datasets(self, monkeypatch):
        fake(monkeypatch, "BigQueryConnector", list_datasets=["ds_a", "ds_b"])

        assert dq._bigquery_list_tables_execute({}) == "Datasets disponibles: ds_a, ds_b"

    def test_with_a_dataset_it_lists_its_tables(self, monkeypatch):
        bq = fake(monkeypatch, "BigQueryConnector", list_tables=["t1", "t2"])

        assert dq._bigquery_list_tables_execute({"dataset_id": "ds_a"}) == "Tablas en dataset 'ds_a': t1, t2"
        assert bq.calls[0][1] == ("ds_a",)

    def test_an_empty_dataset_says_there_are_no_tables(self, monkeypatch):
        fake(monkeypatch, "BigQueryConnector", list_tables=[])

        assert dq._bigquery_list_tables_execute({"dataset_id": "vacio"}) == "(sin tablas)"

    def test_a_project_without_datasets_says_so(self, monkeypatch):
        fake(monkeypatch, "BigQueryConnector", list_datasets=[])

        assert dq._bigquery_list_tables_execute({}) == "(sin datasets)"

    def test_an_empty_dataset_name_lists_datasets_instead(self, monkeypatch):
        bq = fake(monkeypatch, "BigQueryConnector", list_datasets=["ds_a"])

        assert dq._bigquery_list_tables_execute({"dataset_id": ""}).startswith("Datasets disponibles")
        assert bq.calls[0][0] == "list_datasets"

    def test_a_failure_is_returned_as_text(self, monkeypatch):
        fake(monkeypatch, "BigQueryConnector", list_datasets=PermissionError("403 sin acceso"))

        out = dq._bigquery_list_tables_execute({})

        assert out.startswith("Error listando BigQuery") and "403 sin acceso" in out


# ---------------------------------------------------------------------------
# sharepoint_list_items / sharepoint_list_lists
# ---------------------------------------------------------------------------
class TestSharePointTools:
    def test_items_off_says_which_switch(self, settings):
        settings.enable_sharepoint = False

        assert "ENABLE_SHAREPOINT=false" in dq._sharepoint_list_items_execute({"list_name": "ZCC"})

    def test_items_are_returned_as_csv_and_the_list_name_is_passed_through(self, monkeypatch):
        sp = fake(
            monkeypatch, "SharePointConnector", fetch_list_items=pd.DataFrame({"id": [1, 2], "estado": ["A", "B"]})
        )

        out = dq._sharepoint_list_items_execute({"list_name": "ZCC"})

        assert out.splitlines()[:3] == ["id,estado", "1,A", "2,B"]
        assert sp.calls[0][1] == ("ZCC",) and sp.calls[0][2] == {"max_items": None}  # sin tope: la lista completa

    def test_without_a_list_name_the_default_one_is_used(self, monkeypatch):
        sp = fake(monkeypatch, "SharePointConnector", fetch_list_items=pd.DataFrame({"id": [1]}))

        dq._sharepoint_list_items_execute({})

        assert sp.calls[0][1] == (None,)  # None: el conector usa SHAREPOINT_DEFAULT_LIST_NAME

    def test_an_empty_list_says_there_are_no_results(self, monkeypatch):
        fake(monkeypatch, "SharePointConnector", fetch_list_items=pd.DataFrame())

        assert dq._sharepoint_list_items_execute({"list_name": "ZCC"}) == "(sin resultados)"

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (None, None),
            (50, 50),
            ("50", 50),
            (50.0, 50),
            (" 7 ", 7),
            (0, None),
            (-3, None),
            ("abc", None),
            (True, None),
            ([1], None),
            ("", None),
        ],
    )
    def test_max_items_accepts_numbers_in_any_clothes_and_ignores_nonsense(self, raw, expected):
        assert dq._parse_max_items(raw) == expected

    def test_a_cap_from_the_model_reaches_the_connector(self, monkeypatch):
        sp = fake(monkeypatch, "SharePointConnector", fetch_list_items=pd.DataFrame({"id": [1]}))

        dq._sharepoint_list_items_execute({"list_name": "ZCC", "max_items": "25"})

        assert sp.calls[0][2] == {"max_items": 25}

    def test_items_failure_is_returned_as_text(self, monkeypatch):
        fake(monkeypatch, "SharePointConnector", fetch_list_items=ValueError("Debes indicar list_display_name"))

        out = dq._sharepoint_list_items_execute({})

        assert out.startswith("Error consultando SharePoint") and "Debes indicar" in out

    def test_lists_are_joined_with_commas(self, monkeypatch):
        fake(monkeypatch, "SharePointConnector", list_lists=["ZCC", "Slotting"])

        assert dq._sharepoint_list_lists_execute({}) == "Listas disponibles: ZCC, Slotting"

    def test_no_lists_says_so(self, monkeypatch):
        fake(monkeypatch, "SharePointConnector", list_lists=[])

        assert dq._sharepoint_list_lists_execute({}) == "(sin listas)"

    def test_lists_failure_is_returned_as_text(self, monkeypatch):
        fake(monkeypatch, "SharePointConnector", list_lists=RuntimeError("sitio caido"))

        assert dq._sharepoint_list_lists_execute({}).startswith("Error listando SharePoint")


# ---------------------------------------------------------------------------
# sap_query
# ---------------------------------------------------------------------------
class TestSapTool:
    def test_off_says_which_switch(self, settings):
        settings.enable_sap = False

        assert "ENABLE_SAP=false" in dq._sap_query_execute({"service_name": "S", "entity_name": "E"})

    @pytest.mark.parametrize(
        "args", [{}, {"service_name": "S"}, {"entity_name": "E"}, {"service_name": "", "entity_name": "E"}]
    )
    def test_both_service_and_entity_are_required(self, args):
        assert "se requieren 'service_name' y 'entity_name'" in dq._sap_query_execute(args)

    def test_it_passes_every_argument_to_the_connector_and_returns_csv(self, monkeypatch):
        sap = fake(monkeypatch, "SapConnector", fetch_generic=pd.DataFrame({"Id": [1]}))

        out = dq._sap_query_execute(
            {
                "service_name": "ZSERV_SRV",
                "entity_name": "Pedidos",
                "entity_params": {"Werks": "1000"},
                "select_fields": ["Id"],
                "odata_filter": "Id eq 1",
                "results_suffix": "/Items",
            }
        )

        assert out.splitlines() == ["Id", "1"]
        assert sap.calls[0][2] == {
            "service_name": "ZSERV_SRV",
            "entity_name": "Pedidos",
            "entity_params": {"Werks": "1000"},
            "select_fields": ["Id"],
            "odata_filter": "Id eq 1",
            "results_suffix": "/Items",
        }

    def test_the_results_suffix_defaults_to_results(self, monkeypatch):
        sap = fake(monkeypatch, "SapConnector", fetch_generic=pd.DataFrame({"Id": [1]}))

        dq._sap_query_execute({"service_name": "S", "entity_name": "E"})

        assert sap.calls[0][2]["results_suffix"] == "/Results" and sap.calls[0][2]["entity_params"] is None

    def test_an_empty_answer_says_there_are_no_results(self, monkeypatch):
        fake(monkeypatch, "SapConnector", fetch_generic=pd.DataFrame())

        assert dq._sap_query_execute({"service_name": "S", "entity_name": "E"}) == "(sin resultados)"

    def test_a_failure_is_returned_as_text(self, monkeypatch):
        fake(monkeypatch, "SapConnector", fetch_generic=RuntimeError("Login SAP rechazado"))

        out = dq._sap_query_execute({"service_name": "S", "entity_name": "E"})

        assert out.startswith("Error consultando SAP") and "Login SAP rechazado" in out


# ---------------------------------------------------------------------------
# graph_search / graph_directory_lookup / graph_directory_manager
# ---------------------------------------------------------------------------
HIT = {
    "name": "Plan.xlsx",
    "modified": "2026-09-01",
    "createdBy": "Ana",
    "modifiedBy": "Beto",
    "url": "https://x/plan",
    "summary": "resumen " + "x" * 300,
}


class TestGraphSearchTool:
    def test_without_a_query_it_asks_for_it(self):
        assert "falta el argumento 'query'" in dq._graph_search_execute({})

    def test_each_hit_is_one_readable_entry_with_a_trimmed_summary(self, monkeypatch):
        g = fake(monkeypatch, "GraphConnector", search=[HIT])

        out = dq._graph_search_execute({"query": "slotting", "size": 5})

        assert out.startswith(
            "- Plan.xlsx | modificado: 2026-09-01 | creadoPor: Ana | modificadoPor: Beto | url: https://x/plan"
        )
        assert (
            out.splitlines()[1].startswith("  resumen: resumen")
            and len(out.splitlines()[1]) <= len("  resumen: ") + 200
        )
        assert g.calls[0][1] == ("slotting",) and g.calls[0][2] == {"size": 5}

    def test_the_size_defaults_to_25_and_limits_what_is_shown(self, monkeypatch):
        g = fake(monkeypatch, "GraphConnector", search=[dict(HIT, name=f"f{i}") for i in range(40)])

        out = dq._graph_search_execute({"query": "q"})

        assert g.calls[0][2] == {"size": 25}
        assert out.count("\n- ") + 1 == 25  # 40 devueltos, 25 mostrados

    def test_no_hits_says_so(self, monkeypatch):
        fake(monkeypatch, "GraphConnector", search=[])

        assert dq._graph_search_execute({"query": "q"}) == "(sin resultados)"

    def test_a_403_is_flagged_as_an_authorization_gate_not_a_generic_error(self, monkeypatch):
        """Que el modelo distinga 'no tengo permiso' de 'fallo algo': la skill de investigacion interna exige documentar el limite, no reintentar."""
        fake(monkeypatch, "GraphConnector", search=GraphForbiddenError("permiso insuficiente"))

        out = dq._graph_search_execute({"query": "q"})

        assert out == "[gate 403 - permiso insuficiente] permiso insuficiente"

    def test_other_failures_are_a_plain_error(self, monkeypatch):
        fake(monkeypatch, "GraphConnector", search=RuntimeError("timeout"))

        assert dq._graph_search_execute({"query": "q"}) == "Error en Graph Search: timeout"


class TestGraphDirectoryTools:
    USER = {
        "displayName": "Jorge",
        "mail": "j@empresa.com",
        "userPrincipalName": "j@empresa.com",
        "jobTitle": "Analista",
        "department": "Logistica",
        "officeLocation": "la planta",
        "accountEnabled": True,
    }

    def test_lookup_without_an_identifier_asks_for_it(self):
        assert "falta el argumento 'identifier'" in dq._graph_directory_lookup_execute({})

    def test_lookup_shows_the_profile_fields(self, monkeypatch):
        g = fake(monkeypatch, "GraphConnector", directory_lookup=self.USER)

        out = dq._graph_directory_lookup_execute({"identifier": "j@empresa.com"})

        assert out.splitlines() == [
            "displayName: Jorge",
            "mail: j@empresa.com",
            "userPrincipalName: j@empresa.com",
            "jobTitle: Analista",
            "department: Logistica",
            "officeLocation: la planta",
            "accountEnabled: True",
        ]
        assert g.calls[0][1] == ("j@empresa.com",)

    def test_lookup_with_missing_fields_prints_none_instead_of_failing(self, monkeypatch):
        fake(monkeypatch, "GraphConnector", directory_lookup={"displayName": "Solo nombre"})

        out = dq._graph_directory_lookup_execute({"identifier": "x"})

        assert "displayName: Solo nombre" in out and "mail: None" in out

    def test_lookup_403_is_a_gate(self, monkeypatch):
        fake(monkeypatch, "GraphConnector", directory_lookup=GraphForbiddenError("sin Directory.Read.All"))

        assert (
            dq._graph_directory_lookup_execute({"identifier": "x"})
            == "[gate 403 - permiso insuficiente] sin Directory.Read.All"
        )

    def test_lookup_connection_error_is_a_plain_error(self, monkeypatch):
        fake(monkeypatch, "GraphConnector", directory_lookup=GraphConnectionError("No se encontro un usuario"))

        assert dq._graph_directory_lookup_execute({"identifier": "x"}) == "Error: No se encontro un usuario"

    def test_lookup_unexpected_error_is_reported(self, monkeypatch):
        fake(monkeypatch, "GraphConnector", directory_lookup=RuntimeError("boom"))

        assert dq._graph_directory_lookup_execute({"identifier": "x"}) == "Error consultando directorio: boom"

    def test_manager_without_an_identifier_asks_for_it(self):
        assert "falta el argumento 'identifier'" in dq._graph_directory_manager_execute({})

    def test_manager_shows_the_manager_fields(self, monkeypatch):
        fake(
            monkeypatch,
            "GraphConnector",
            directory_manager={
                "displayName": "Jefa",
                "mail": "jefa@empresa.com",
                "jobTitle": "Gerente",
                "department": "Logistica",
            },
        )

        out = dq._graph_directory_manager_execute({"identifier": "x"})

        assert out.splitlines() == [
            "manager displayName: Jefa",
            "mail: jefa@empresa.com",
            "jobTitle: Gerente",
            "department: Logistica",
        ]

    def test_manager_403_is_a_gate(self, monkeypatch):
        fake(monkeypatch, "GraphConnector", directory_manager=GraphForbiddenError("sin permiso"))

        assert dq._graph_directory_manager_execute({"identifier": "x"}).startswith("[gate 403 - permiso insuficiente]")

    def test_manager_connection_error_is_a_plain_error(self, monkeypatch):
        fake(monkeypatch, "GraphConnector", directory_manager=GraphConnectionError("sin manager asignado"))

        assert dq._graph_directory_manager_execute({"identifier": "x"}) == "Error: sin manager asignado"

    def test_manager_unexpected_error_is_reported(self, monkeypatch):
        fake(monkeypatch, "GraphConnector", directory_manager=RuntimeError("boom"))

        assert dq._graph_directory_manager_execute({"identifier": "x"}) == "Error consultando manager: boom"


def test_a_forbidden_error_is_not_caught_by_the_generic_connection_branch():
    """GraphForbiddenError hereda de GraphConnectionError: el orden de los `except` importa, o un 403 saldria como un error corriente."""
    assert issubclass(GraphForbiddenError, GraphConnectionError)
