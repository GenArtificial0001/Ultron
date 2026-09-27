"""spec controles-seguridad-optimizacion-lightspeed-bigquery, pestaña
"Conexiones" (pedido del usuario 2026-09-21: "necesito poder desde la
configuracion cambiar el tope limite para determinar que es una consulta
pesada"). Cubre:
  - GET/POST /api/config/connections: override en runtime de los 3 umbrales
    configurables (bq_cost_guardrail_bytes/export_row_threshold/
    export_preview_rows), persistidos via ProjectConfig (mismo mecanismo
    que los parametros de generacion).
  - Resolucion efectiva en app/tools/dbquery.py (_effective_*): el override
    de ProjectConfig gana sobre el default de Settings/.env cuando esta
    seteado, y cae al default cuando es None.
  - GET /api/config/connections/bigquery/{datasets,tables} y
    /sharepoint/lists: precarga simple, mockeando los conectores reales
    (patron DBManager -- nunca pega a BigQuery/SharePoint real).

Todo mockeado -- el conftest.py del proyecto ya aisla BASE_DIR/ProjectConfig
a un temp por sesion de test, asi que estos tests nunca tocan el
project_config.json real de la maquina."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app import project_context as pc
from app.config import Settings
from app.main import app
from app.tools import dbquery


client = TestClient(app)


def setup_function(_fn):
    # Cada test arranca sin overrides guardados -- evita que un test deje
    # estado que contamine al siguiente dentro de la misma sesion de pytest.
    pc.set_connection_thresholds(
        bq_cost_guardrail_bytes=None,
        export_row_threshold=None,
        export_preview_rows=None,
    )


# ---------------------------------------------------------------------------
# GET/POST /api/config/connections
# ---------------------------------------------------------------------------
def test_get_connections_returns_defaults_when_no_override():
    resp = client.get("/api/config/connections")
    assert resp.status_code == 200
    data = resp.json()
    assert data["overrides"] == {
        "bq_cost_guardrail_bytes": None,
        "export_row_threshold": None,
        "export_preview_rows": None,
    }
    # effective == defaults reales de Settings cuando no hay override
    assert (
        data["effective"]["bq_cost_guardrail_bytes"]
        == data["defaults"]["bq_cost_guardrail_bytes"]
    )
    assert (
        data["effective"]["export_row_threshold"]
        == data["defaults"]["export_row_threshold"]
    )


def test_post_connections_saves_partial_override():
    resp = client.post(
        "/api/config/connections",
        json={"bq_cost_guardrail_bytes": 536_870_912, "export_row_threshold": 100},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["bq_cost_guardrail_bytes"] == 536_870_912
    assert body["export_row_threshold"] == 100
    assert body["export_preview_rows"] is None  # no se mando -- sigue en default

    # se refleja en un GET posterior
    resp2 = client.get("/api/config/connections")
    data = resp2.json()
    assert data["overrides"]["bq_cost_guardrail_bytes"] == 536_870_912
    assert data["overrides"]["export_row_threshold"] == 100
    assert data["effective"]["bq_cost_guardrail_bytes"] == 536_870_912


def test_post_connections_null_reverts_to_default():
    client.post("/api/config/connections", json={"export_row_threshold": 200})
    resp = client.post("/api/config/connections", json={"export_row_threshold": None})
    assert resp.status_code == 200
    assert resp.json()["export_row_threshold"] is None

    data = client.get("/api/config/connections").json()
    assert data["overrides"]["export_row_threshold"] is None
    assert (
        data["effective"]["export_row_threshold"]
        == data["defaults"]["export_row_threshold"]
    )


# ---------------------------------------------------------------------------
# Resolucion efectiva real en dbquery.py -- el override debe pisar el
# default de Settings en los guardrails/export, no solo en el endpoint.
# ---------------------------------------------------------------------------
def test_effective_bq_cost_threshold_uses_override_when_set():
    settings = Settings(bq_cost_guardrail_bytes=1_073_741_824)
    pc.set_connection_thresholds(bq_cost_guardrail_bytes=500_000_000)
    assert dbquery._effective_bq_cost_threshold(settings) == 500_000_000


def test_effective_bq_cost_threshold_falls_back_to_settings_default():
    settings = Settings(bq_cost_guardrail_bytes=1_073_741_824)
    pc.set_connection_thresholds(bq_cost_guardrail_bytes=None)
    assert dbquery._effective_bq_cost_threshold(settings) == 1_073_741_824


def test_effective_export_row_threshold_uses_override_when_set():
    settings = Settings(export_row_threshold=50)
    pc.set_connection_thresholds(export_row_threshold=10)
    assert dbquery._effective_export_row_threshold(settings) == 10


def test_effective_export_preview_rows_uses_override_when_set():
    settings = Settings(export_preview_rows=50)
    pc.set_connection_thresholds(export_preview_rows=5)
    assert dbquery._effective_export_preview_rows(settings) == 5


def test_dry_run_check_respects_ui_configured_threshold(monkeypatch):
    """Integracion: si el usuario bajo el umbral desde la pestaña
    Conexiones a 1000 bytes, una consulta de 2000 bytes debe bloquearse
    aunque el default de Settings sea mucho mas alto."""
    settings = Settings(
        bq_cost_guardrail_bytes=1_073_741_824, bq_dry_run_cache_ttl_seconds=300
    )
    monkeypatch.setattr(dbquery, "get_settings", lambda: settings)
    pc.set_connection_thresholds(bq_cost_guardrail_bytes=1_000)
    dbquery._BQ_DRY_RUN_CACHE.clear()

    connector = MagicMock()
    connector.dry_run.return_value = 2_000

    result = dbquery._bigquery_dry_run_check("SELECT * FROM t", connector)
    assert result is not None
    assert "GUARDRAIL DE COSTO" in result


# ---------------------------------------------------------------------------
# Precarga: GET /api/config/connections/bigquery/{datasets,tables},
# /sharepoint/lists -- mockeando los conectores reales.
# ---------------------------------------------------------------------------
def test_list_bigquery_datasets_returns_sorted_list():
    connector_instance = MagicMock()
    connector_instance.list_datasets.return_value = ["zzz_dataset", "aaa_dataset"]
    with patch(
        "app.connectors.bigquery_connector.BigQueryConnector",
        return_value=connector_instance,
    ):
        resp = client.get("/api/config/connections/bigquery/datasets")
    assert resp.status_code == 200
    assert resp.json() == {"datasets": ["aaa_dataset", "zzz_dataset"]}


def test_list_bigquery_datasets_disabled_returns_400(monkeypatch):
    settings = Settings(enable_bigquery=False)
    with patch("app.main.get_settings", lambda: settings):
        resp = client.get("/api/config/connections/bigquery/datasets")
    assert resp.status_code == 400


def test_list_bigquery_datasets_connector_error_returns_502():
    connector_instance = MagicMock()
    connector_instance.list_datasets.side_effect = RuntimeError("sin credenciales")
    with patch(
        "app.connectors.bigquery_connector.BigQueryConnector",
        return_value=connector_instance,
    ):
        resp = client.get("/api/config/connections/bigquery/datasets")
    assert resp.status_code == 502


def test_list_bigquery_tables_returns_sorted_list():
    connector_instance = MagicMock()
    connector_instance.list_tables.return_value = ["tabla_b", "tabla_a"]
    with patch(
        "app.connectors.bigquery_connector.BigQueryConnector",
        return_value=connector_instance,
    ):
        resp = client.get(
            "/api/config/connections/bigquery/tables?dataset_id=Warehouse"
        )
    assert resp.status_code == 200
    assert resp.json() == {"tables": ["tabla_a", "tabla_b"]}
    connector_instance.list_tables.assert_called_once_with("Warehouse")


def test_list_sharepoint_lists_returns_sorted_list():
    connector_instance = MagicMock()
    connector_instance.list_lists.return_value = ["Lista Z", "Lista A"]
    with patch("app.main.SharePointConnector", return_value=connector_instance):
        resp = client.get("/api/config/connections/sharepoint/lists")
    assert resp.status_code == 200
    assert resp.json() == {"lists": ["Lista A", "Lista Z"]}


def test_list_sharepoint_lists_disabled_returns_400():
    settings = Settings(enable_sharepoint=False)
    with patch("app.main.get_settings", lambda: settings):
        resp = client.get("/api/config/connections/sharepoint/lists")
    assert resp.status_code == 400
