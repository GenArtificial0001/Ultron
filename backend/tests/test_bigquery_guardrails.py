"""spec controles-seguridad-optimizacion-lightspeed-bigquery: guardrails
REQ-002 (dry run + costo) y REQ-003 (riesgo DML/DDL/CREATE TABLE) de
`app/tools/dbquery.py`. Patron DBManager (AGENTS.md): todo mockeado, nunca
pega a BigQuery real."""

from __future__ import annotations

from unittest.mock import MagicMock

import pandas as pd
import pytest

from app.config import Settings
from app.session.events import SessionState
from app.tools import dbquery


def _session() -> SessionState:
    return SessionState(session_id="test-session")


# ---------------------------------------------------------------------------
# REQ-003: deteccion de statements riesgosos
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "query,expected_pattern",
    [
        ("DROP TABLE proyecto.dataset.tabla", "DROP"),
        ("DELETE FROM proyecto.dataset.tabla WHERE id = 1", "DELETE"),
        ("TRUNCATE TABLE proyecto.dataset.tabla", "TRUNCATE"),
        ("UPDATE proyecto.dataset.tabla SET x = 1", "UPDATE"),
        ("MERGE INTO proyecto.dataset.tabla USING src ON true", "MERGE"),
        ("CREATE TABLE proyecto.dataset.nueva (id INT64)", "CREATE TABLE"),
        (
            "CREATE OR REPLACE TABLE proyecto.dataset.nueva (id INT64)",
            "CREATE OR REPLACE TABLE",
        ),
        ("  drop   table   x", "DROP"),  # case-insensitive + espacios
    ],
)
def test_detect_risky_statement_matches_each_pattern(query, expected_pattern):
    detected = dbquery._detect_risky_statement(query)
    assert detected is not None
    pattern, statement = detected
    assert pattern == expected_pattern
    assert statement.strip().lower().startswith(query.strip().lower()[:5])


@pytest.mark.parametrize(
    "query",
    [
        "SELECT * FROM proyecto.dataset.tabla",
        "SELECT updated_at FROM proyecto.dataset.tabla",  # columna, no UPDATE
        "CREATE VIEW proyecto.dataset.v AS SELECT 1",  # CREATE VIEW no bloquea
        "CREATE FUNCTION proyecto.dataset.f() RETURNS INT64 AS (1)",
        "-- esto es un delete\nSELECT 1",  # comentario, no statement real
        "/* DROP TABLE x */ SELECT 1",
    ],
)
def test_detect_risky_statement_ignores_safe_queries(query):
    assert dbquery._detect_risky_statement(query) is None


def test_detect_risky_statement_finds_it_in_second_statement_of_multi_sql():
    query = "SELECT 1; DELETE FROM proyecto.dataset.tabla"
    detected = dbquery._detect_risky_statement(query)
    assert detected is not None
    assert detected[0] == "DELETE"


# ---------------------------------------------------------------------------
# REQ-003: bloquea 1ra vez, permite reintento tras aprobacion
# ---------------------------------------------------------------------------
def test_bigquery_risk_check_blocks_first_time_then_allows_retry():
    session = _session()
    query = "DELETE FROM proyecto.dataset.tabla WHERE id = 1"

    first = dbquery._bigquery_risk_check(query, session)
    assert first is not None
    assert "DELETE" in first
    assert "question" in first

    second = dbquery._bigquery_risk_check(query, session)
    assert second is None  # ya aprobado, no vuelve a bloquear la MISMA query


def test_bigquery_risk_check_treats_different_query_as_new():
    session = _session()
    dbquery._bigquery_risk_check("DELETE FROM t WHERE id = 1", session)
    # una consulta DISTINTA (aunque mismo verbo) vuelve a bloquear
    result = dbquery._bigquery_risk_check("DELETE FROM t WHERE id = 2", session)
    assert result is not None


def test_bigquery_risk_check_returns_none_for_safe_query():
    session = _session()
    assert dbquery._bigquery_risk_check("SELECT * FROM t", session) is None


def test_bigquery_query_execute_blocks_without_calling_connector(monkeypatch):
    """Integracion minima: `_bigquery_query_execute` no debe ni instanciar
    el conector cuando el guardrail de riesgo corta antes."""
    settings = Settings(enable_bigquery=True)
    monkeypatch.setattr(dbquery, "get_settings", lambda: settings)
    connector_mock = MagicMock()
    monkeypatch.setattr(dbquery, "BigQueryConnector", connector_mock)

    session = _session()
    result = dbquery._bigquery_query_execute(
        {"query": "DROP TABLE proyecto.dataset.tabla"}, session
    )

    assert "GUARDRAIL DE RIESGO" in result
    connector_mock.assert_not_called()


# ---------------------------------------------------------------------------
# REQ-002: dry run + cache + umbral de costo
# ---------------------------------------------------------------------------
def setup_function(_fn):
    dbquery._BQ_DRY_RUN_CACHE.clear()


def test_dry_run_check_blocks_when_over_threshold(monkeypatch):
    settings = Settings(bq_cost_guardrail_bytes=1_000, bq_dry_run_cache_ttl_seconds=300)
    monkeypatch.setattr(dbquery, "get_settings", lambda: settings)
    connector = MagicMock()
    connector.dry_run.return_value = 2_000

    result = dbquery._bigquery_dry_run_check("SELECT * FROM t", connector)

    assert result is not None
    assert "GUARDRAIL DE COSTO" in result
    connector.dry_run.assert_called_once()


def test_dry_run_check_allows_when_under_threshold(monkeypatch):
    settings = Settings(
        bq_cost_guardrail_bytes=1_000_000, bq_dry_run_cache_ttl_seconds=300
    )
    monkeypatch.setattr(dbquery, "get_settings", lambda: settings)
    connector = MagicMock()
    connector.dry_run.return_value = 500

    assert dbquery._bigquery_dry_run_check("SELECT * FROM t", connector) is None


def test_dry_run_check_reuses_cache_within_ttl(monkeypatch):
    settings = Settings(bq_cost_guardrail_bytes=1_000, bq_dry_run_cache_ttl_seconds=300)
    monkeypatch.setattr(dbquery, "get_settings", lambda: settings)
    connector = MagicMock()
    connector.dry_run.return_value = 2_000

    query = "SELECT * FROM t WHERE x = 1"
    first = dbquery._bigquery_dry_run_check(query, connector)
    second = dbquery._bigquery_dry_run_check(query, connector)

    assert first is not None and second is not None
    connector.dry_run.assert_called_once()  # 2do hit vino del cache, no de otra llamada


def test_dry_run_check_never_relaxes_verdict_even_from_cache(monkeypatch):
    """El cache nunca cambia el veredicto: si la 1ra vez bloqueo, la 2da
    (servida desde cache) debe seguir bloqueando -- nunca 'aprender' a
    dejar pasar una consulta pesada solo porque ya se evaluo antes."""
    settings = Settings(bq_cost_guardrail_bytes=1_000, bq_dry_run_cache_ttl_seconds=300)
    monkeypatch.setattr(dbquery, "get_settings", lambda: settings)
    connector = MagicMock()
    connector.dry_run.return_value = 5_000

    query = "SELECT * FROM tabla_pesada"
    first = dbquery._bigquery_dry_run_check(query, connector)
    second = dbquery._bigquery_dry_run_check(query, connector)

    assert first is not None
    assert second is not None
    assert "GUARDRAIL DE COSTO" in second


def test_dry_run_check_expires_cache_after_ttl(monkeypatch):
    settings = Settings(bq_cost_guardrail_bytes=1_000, bq_dry_run_cache_ttl_seconds=0)
    monkeypatch.setattr(dbquery, "get_settings", lambda: settings)
    connector = MagicMock()
    connector.dry_run.return_value = 2_000

    query = "SELECT * FROM t"
    dbquery._bigquery_dry_run_check(query, connector)
    dbquery._bigquery_dry_run_check(query, connector)

    assert connector.dry_run.call_count == 2  # TTL=0 -> nunca sirve del cache


# ---------------------------------------------------------------------------
# FIX 2026-09-21: bug real reportado por el usuario -- "el sistema de
# seguridad de BigQuery mantiene el bloqueo... incluso tras tu confirmacion".
# El guardrail de costo debe respetar la aprobacion humana en el reintento,
# igual que el guardrail de riesgo (REQ-003) ya lo hacia.
# ---------------------------------------------------------------------------
def test_dry_run_check_blocks_first_time_then_allows_retry_with_session(monkeypatch):
    settings = Settings(bq_cost_guardrail_bytes=1_000, bq_dry_run_cache_ttl_seconds=300)
    monkeypatch.setattr(dbquery, "get_settings", lambda: settings)
    connector = MagicMock()
    connector.dry_run.return_value = 5_000
    session = _session()
    query = "SELECT * FROM tabla_pesada"

    first = dbquery._bigquery_dry_run_check(query, connector, session)
    assert first is not None
    assert "GUARDRAIL DE COSTO" in first

    second = dbquery._bigquery_dry_run_check(query, connector, session)
    assert second is None  # ya aprobado, ahora SI se deja pasar


def test_dry_run_check_treats_different_query_as_new_approval(monkeypatch):
    settings = Settings(bq_cost_guardrail_bytes=1_000, bq_dry_run_cache_ttl_seconds=300)
    monkeypatch.setattr(dbquery, "get_settings", lambda: settings)
    connector = MagicMock()
    connector.dry_run.return_value = 5_000
    session = _session()

    dbquery._bigquery_dry_run_check("SELECT * FROM t WHERE x = 1", connector, session)
    # consulta DISTINTA (aunque tambien pesada) -- no hereda la aprobacion
    result = dbquery._bigquery_dry_run_check(
        "SELECT * FROM t WHERE x = 2", connector, session
    )
    assert result is not None


def test_dry_run_check_without_session_never_approves(monkeypatch):
    """Sin session (ej. llamada directa de test/utilitario), no hay donde
    guardar la aprobacion -- debe seguir bloqueando siempre, nunca crashear."""
    settings = Settings(bq_cost_guardrail_bytes=1_000, bq_dry_run_cache_ttl_seconds=300)
    monkeypatch.setattr(dbquery, "get_settings", lambda: settings)
    connector = MagicMock()
    connector.dry_run.return_value = 5_000

    first = dbquery._bigquery_dry_run_check("SELECT * FROM t", connector, None)
    second = dbquery._bigquery_dry_run_check("SELECT * FROM t", connector, None)
    assert first is not None
    assert second is not None  # sin session, sin aprobacion posible


def test_bigquery_query_execute_full_flow_blocks_then_allows_retry(monkeypatch):
    """Integracion completa: 1er intento de una consulta pesada se
    bloquea por costo; 2do intento (misma query, misma session) se
    ejecuta de verdad -- reproduce el bug real reportado por el usuario."""
    settings = Settings(enable_bigquery=True, bq_cost_guardrail_bytes=1_000)
    monkeypatch.setattr(dbquery, "get_settings", lambda: settings)

    connector_instance = MagicMock()
    connector_instance.dry_run.return_value = 5_000
    connector_instance.fetch_query.return_value = pd.DataFrame({"a": [1, 2, 3]})
    connector_cls = MagicMock(return_value=connector_instance)
    monkeypatch.setattr(dbquery, "BigQueryConnector", connector_cls)

    session = _session()
    query = "SELECT * FROM `Warehouse.T-ASN-CNT-K-6M` WHERE DT_RECEIVED = '2026-09-18'"

    first = dbquery._bigquery_query_execute({"query": query}, session)
    assert "GUARDRAIL DE COSTO" in first
    connector_instance.fetch_query.assert_not_called()

    second = dbquery._bigquery_query_execute({"query": query}, session)
    assert "GUARDRAIL DE COSTO" not in second
    connector_instance.fetch_query.assert_called_once()


# ---------------------------------------------------------------------------
# FIX 2026-09-21: bug real reportado por el usuario -- pidio "el detalle" de
# 181 contenedores, el LLM puso su propio LIMIT 50 y el export automatico
# nunca se disparo porque el DataFrame ya llegaba recortado a 50 filas.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "query,cap",
    [
        ("SELECT * FROM t LIMIT 50", 50),
        ("select * from t limit 10", 10),
        ("SELECT * FROM t FETCH FIRST 25 ROWS ONLY", 25),
        ("SELECT * FROM t WHERE ROWNUM <= 100", 100),
    ],
)
def test_detect_explicit_row_cap_finds_each_syntax(query, cap):
    assert dbquery._detect_explicit_row_cap(query) == cap


def test_detect_explicit_row_cap_returns_none_without_limit():
    assert dbquery._detect_explicit_row_cap("SELECT * FROM t") is None


def test_detect_explicit_row_cap_uses_last_match_for_nested_limits():
    # subquery con su propio LIMIT interno + un LIMIT externo real -- el que
    # aplica al resultado final es el ULTIMO de la consulta.
    query = "SELECT * FROM (SELECT * FROM t LIMIT 500) sub LIMIT 50"
    assert dbquery._detect_explicit_row_cap(query) == 50


def test_df_to_text_warns_when_result_matches_explicit_limit(monkeypatch):
    settings = Settings(export_row_threshold=50, export_preview_rows=50)
    monkeypatch.setattr(dbquery, "get_settings", lambda: settings)
    df = pd.DataFrame({"a": range(50)})  # exactamente 50 filas, igual al LIMIT

    text = dbquery._df_to_text(
        df, session=None, source="bigquery", query="SELECT * FROM t LIMIT 50"
    )

    assert "NOTA" in text
    assert "50" in text
    assert (
        "mas filas" in text.lower()
        or "más filas" in text.lower()
        or "ocultando" in text.lower()
    )


def test_df_to_text_no_warning_when_result_is_under_the_limit(monkeypatch):
    """Si el LIMIT era 50 pero la fuente real solo tenia 30 filas, no hay
    nada oculto -- no corresponde ninguna advertencia."""
    settings = Settings(export_row_threshold=50, export_preview_rows=50)
    monkeypatch.setattr(dbquery, "get_settings", lambda: settings)
    df = pd.DataFrame({"a": range(30)})

    text = dbquery._df_to_text(
        df, session=None, source="bigquery", query="SELECT * FROM t LIMIT 50"
    )

    assert "NOTA" not in text


def test_df_to_text_no_warning_without_explicit_limit(monkeypatch):
    settings = Settings(export_row_threshold=50, export_preview_rows=50)
    monkeypatch.setattr(dbquery, "get_settings", lambda: settings)
    df = pd.DataFrame({"a": range(50)})

    text = dbquery._df_to_text(
        df, session=None, source="bigquery", query="SELECT * FROM t"
    )

    assert "NOTA" not in text


# ---------------------------------------------------------------------------
# REQ-004: export a CSV cuando supera el umbral de filas (multi-conector)
# ---------------------------------------------------------------------------
def test_df_to_text_no_export_when_under_threshold(monkeypatch, tmp_path):
    settings = Settings(export_row_threshold=50, export_preview_rows=50)
    monkeypatch.setattr(dbquery, "get_settings", lambda: settings)
    df = pd.DataFrame({"a": range(10)})

    text = dbquery._df_to_text(df, session=None, source="oracle")

    assert "exportado" not in text.lower()


def test_df_to_text_exports_csv_when_over_threshold(monkeypatch, tmp_path):
    settings = Settings(export_row_threshold=5, export_preview_rows=5)
    monkeypatch.setattr(dbquery, "get_settings", lambda: settings)

    from app.persistence import service as persistence

    monkeypatch.setattr(persistence, "project_root_for_session", lambda sid: tmp_path)

    session = SessionState(session_id="s1")
    df = pd.DataFrame({"a": range(20)})

    text = dbquery._df_to_text(df, session=session, source="bigquery")

    assert "exportado" in text.lower()
    assert "/api/sessions/s1/exports/" in text
    exported_files = list(
        (tmp_path / persistence.EXPORTS_SUBDIR).glob("bigquery_*.csv")
    )
    assert len(exported_files) == 1
    exported_df = pd.read_csv(exported_files[0])
    assert len(exported_df) == 20  # el CSV tiene TODAS las filas, no solo el preview


def test_df_to_text_reports_when_no_project_folder(monkeypatch):
    settings = Settings(export_row_threshold=5, export_preview_rows=5)
    monkeypatch.setattr(dbquery, "get_settings", lambda: settings)

    from app.persistence import service as persistence

    monkeypatch.setattr(persistence, "project_root_for_session", lambda sid: None)

    session = SessionState(session_id="s-sin-carpeta")
    df = pd.DataFrame({"a": range(20)})

    text = dbquery._df_to_text(df, session=session, source="oracle")

    assert "no disponible" in text.lower()
