"""PQ-005 / BL-023 (opcion (a), delegada por el usuario en ADR-018): `oracle_query` tiene la misma guarda de riesgo que
BigQuery (DROP, DELETE, TRUNCATE, UPDATE, MERGE, CREATE TABLE, CREATE OR REPLACE TABLE), con el mismo flujo: bloquea
UNA vez, pide confirmacion con la tool `question` y deja pasar el reintento de la MISMA consulta.

Antes, `_oracle_query_execute` pasaba el SQL del modelo tal cual a `pd.read_sql`: nada impedia un DROP o un DELETE.
Todo mockeado: nunca hay conexion a Oracle (patron DBManager, AGENTS.md).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pandas as pd
import pytest

from app.config import Settings
from app.session.events import SessionState
from app.tools import dbquery


def _session() -> SessionState:
    return SessionState(session_id="test-session")


@pytest.fixture()
def oracle(monkeypatch):
    """Conector Oracle falso con el modulo configurado: devuelve la instancia para contar las llamadas."""
    monkeypatch.setattr(dbquery, "get_settings", lambda: Settings(enable_oracle=True))
    instance = MagicMock()
    instance.fetch_query.return_value = pd.DataFrame({"X": [1]})
    monkeypatch.setattr(dbquery, "OracleConnector", MagicMock(return_value=instance))
    return instance


RISKY = [
    "DROP TABLE MMDBDP7.CPNT023_PART",
    "DELETE FROM MMDBDP7.CPNT023_PART WHERE ROWNUM <= 1",
    "TRUNCATE TABLE MMDBDP7.CPNT023_PART",
    "UPDATE MMDBDP7.CPNT023_PART SET X = 1",
    "MERGE INTO MMDBDP7.CPNT023_PART USING src ON (1=1)",
    "CREATE TABLE MMDBDP7.NUEVA (ID NUMBER)",
    "CREATE OR REPLACE TABLE MMDBDP7.NUEVA (ID NUMBER)",
    "  delete from t",  # espacios y minusculas
    "SELECT 1 FROM DUAL; DROP TABLE t",  # el segundo statement
    "/* inocente */ DROP TABLE t",  # comentario por delante
]


@pytest.mark.parametrize("query", RISKY)
def test_a_risky_statement_is_blocked_without_touching_oracle(oracle, query):
    result = dbquery._oracle_query_execute({"query": query}, _session())

    assert "GUARDRAIL DE RIESGO - Oracle" in result
    assert "La consulta NO se ejecuto." in result
    assert "question" in result and "(Riesgo)" in result
    dbquery.OracleConnector.assert_not_called()  # ni siquiera se abre el conector


def test_the_block_names_the_detected_pattern_and_statement(oracle):
    result = dbquery._oracle_query_execute({"query": "DELETE FROM t WHERE ID = 7"}, _session())

    assert "'DELETE'" in result and "DELETE FROM t WHERE ID = 7" in result


def test_the_same_query_runs_on_retry_after_the_user_confirms(oracle):
    session = _session()
    query = "DELETE FROM t WHERE ID = 7"

    first = dbquery._oracle_query_execute({"query": query}, session)
    second = dbquery._oracle_query_execute({"query": query}, session)

    assert "GUARDRAIL DE RIESGO" in first
    assert "GUARDRAIL DE RIESGO" not in second  # bloquea una vez, permite el reintento
    oracle.fetch_query.assert_called_once()


def test_a_different_query_needs_its_own_confirmation(oracle):
    session = _session()
    dbquery._oracle_query_execute({"query": "DELETE FROM t WHERE ID = 7"}, session)

    other = dbquery._oracle_query_execute({"query": "DELETE FROM t WHERE ID = 8"}, session)

    assert "GUARDRAIL DE RIESGO" in other
    oracle.fetch_query.assert_not_called()


def test_without_a_session_a_risky_query_is_always_blocked(oracle):
    """Sin sesion no hay donde recordar la aprobacion: nunca se ejecuta (mismo criterio que el dry run de BigQuery)."""
    for _ in range(2):
        assert "GUARDRAIL DE RIESGO" in dbquery._oracle_query_execute({"query": "DROP TABLE t"}, None)
    oracle.fetch_query.assert_not_called()


@pytest.mark.parametrize(
    "query",
    [
        "SELECT * FROM MMDBDP7.CPNT023_PART WHERE ROWNUM <= 10",
        "SELECT updated_at, deleted_flag FROM t",  # el verbo dentro de un nombre de columna no cuenta
        "WITH x AS (SELECT 1 FROM DUAL) SELECT * FROM x",
        "-- delete de ejemplo" + chr(10) + "SELECT 1 FROM DUAL",  # verbo solo en un comentario
        "SELECT 'DROP TABLE t' FROM DUAL",  # verbo dentro de un texto: no es la primera palabra del statement
    ],
)
def test_safe_queries_run_without_a_prompt(oracle, query):
    result = dbquery._oracle_query_execute({"query": query}, _session())

    assert "GUARDRAIL" not in result
    oracle.fetch_query.assert_called_once()


def test_oracle_and_bigquery_approvals_are_independent(oracle):
    """Aprobar un DELETE en BigQuery no deja pasar el MISMO texto en Oracle (otra base, otro riesgo)."""
    session = _session()
    query = "DELETE FROM t WHERE ID = 7"
    dbquery._bigquery_risk_check(query, session)  # el usuario "aprobo" esa consulta para BigQuery

    result = dbquery._oracle_query_execute({"query": query}, session)

    assert "GUARDRAIL DE RIESGO - Oracle" in result


def test_the_bigquery_guard_message_is_unchanged():
    """No regresion: refactorizar la guarda para compartirla no cambia lo que ve el usuario en BigQuery."""
    result = dbquery._bigquery_risk_check("DROP TABLE p.d.t", _session())

    assert result is not None
    assert result.startswith("[GUARDRAIL DE RIESGO - BigQuery] Se detecto el patron 'DROP' en la consulta,")
    assert (
        "antes de tocar BigQuery (DROP, DELETE, TRUNCATE, UPDATE, MERGE, CREATE TABLE, CREATE OR REPLACE TABLE)"
        in result
    )


def test_the_tool_description_tells_the_model_about_the_guard():
    assert "confirm" in dbquery.oracle_query_tool.description.lower()
