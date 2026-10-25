"""Uso de las conexiones del conector de Oracle (`app/connectors/oracle_connector.py`, BL-006): `test_connection`, `fetch_query` y `fetch_table`.

`test_oracle_connector_pool.py` ya prueba el pool cacheado; esto prueba lo que se hace CON la conexion. Sin Oracle real: el pool y `pandas.read_sql` se reemplazan por falsos.

Punto importante de seguridad que queda fijado aca: `fetch_query` ejecuta el SQL tal cual (la proteccion contra DROP/DELETE vive en la tool, `oracle_query`, ver `test_oracle_guardrails.py`),
pero los PARAMETROS siempre viajan aparte (`params=`), nunca pegados al texto de la consulta. `fetch_table` si arma texto con el nombre de la tabla: no tiene llamadores hoy (PQ-007, se dejo
como esta), por eso se fija su comportamiento actual en vez de cambiarlo.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pandas as pd
import pytest

from app.config import Settings
from app.connectors import oracle_connector as oc


class FakeConnection:
    """Una conexion de pool: se usa con `with` y al salir se DEVUELVE al pool (no se cierra el socket)."""

    def __init__(self, fetch_raises: Exception | None = None):
        self.entered = 0
        self.exited = 0
        self.executed: list[str] = []
        self.fetch_raises = fetch_raises
        self.cursor_obj = MagicMock()

        def execute(sql, *a, **kw):
            self.executed.append(sql)

        def fetchone():
            if self.fetch_raises:
                raise self.fetch_raises
            return (1,)

        self.cursor_obj.execute.side_effect = execute
        self.cursor_obj.fetchone.side_effect = fetchone

    def __enter__(self):
        self.entered += 1
        return self

    def __exit__(self, *exc):
        self.exited += 1
        return False

    def cursor(self):
        return self.cursor_obj


@pytest.fixture
def conn(monkeypatch):
    c = FakeConnection()
    monkeypatch.setattr(oc, "_get_pool", lambda settings: MagicMock(acquire=lambda: c))
    return c


@pytest.fixture
def read_sql(monkeypatch):
    calls = []

    def fake(query, connection, params=None):
        calls.append((query, connection, params))
        return pd.DataFrame({"X": [1, 2]})

    monkeypatch.setattr(oc.pd, "read_sql", fake)
    return calls


def make(schema="MMDBDP7"):
    return oc.OracleConnector(
        Settings(oracle_user="u", oracle_host="h", oracle_port=1521, oracle_service_name="svc", oracle_schema=schema)
    )


# ---------------------------------------------------------------------------
# test_connection
# ---------------------------------------------------------------------------
class TestConnection:
    def test_true_when_oracle_answers_and_the_connection_goes_back_to_the_pool(self, conn):
        assert make().test_connection() is True
        assert conn.executed == ["SELECT 1 FROM dual"]
        assert conn.entered == 1 and conn.exited == 1  # salir del `with` la devuelve al pool

    def test_false_instead_of_raising_when_the_query_fails(self, conn):
        conn.fetch_raises = RuntimeError("ORA-12541: sin listener")

        assert make().test_connection() is False

    def test_the_connection_is_returned_even_when_the_query_fails(self, conn):
        conn.fetch_raises = RuntimeError("falla")
        make().test_connection()

        assert conn.exited == 1  # si no se devolviera, el pool (max 4) se agotaria tras 4 fallos

    def test_false_when_the_pool_cannot_even_be_created(self, monkeypatch):
        def boom(settings):
            raise RuntimeError("ORA-01017: credenciales invalidas")

        monkeypatch.setattr(oc, "_get_pool", boom)

        assert make().test_connection() is False


# ---------------------------------------------------------------------------
# fetch_query
# ---------------------------------------------------------------------------
class TestFetchQuery:
    def test_it_runs_the_sql_as_is_on_a_pooled_connection(self, conn, read_sql):
        df = make().fetch_query("SELECT * FROM t WHERE ROWNUM <= 5")

        assert list(df["X"]) == [1, 2]
        query, used, params = read_sql[0]
        assert query == "SELECT * FROM t WHERE ROWNUM <= 5" and used is conn and params is None

    def test_parameters_travel_separately_never_pasted_into_the_text(self, conn, read_sql):
        make().fetch_query("SELECT * FROM t WHERE id = :id", params={"id": "1; DROP TABLE t"})

        query, _, params = read_sql[0]
        assert params == {"id": "1; DROP TABLE t"}
        assert "DROP" not in query  # el valor peligroso queda como DATO, no como parte del SQL

    def test_the_connection_is_returned_to_the_pool_after_the_query(self, conn, read_sql):
        make().fetch_query("SELECT 1 FROM dual")

        assert conn.entered == 1 and conn.exited == 1

    def test_the_connection_is_returned_even_if_the_query_fails(self, conn, monkeypatch):
        def boom(query, connection, params=None):
            raise RuntimeError("ORA-00942: tabla inexistente")

        monkeypatch.setattr(oc.pd, "read_sql", boom)

        with pytest.raises(RuntimeError, match="ORA-00942"):
            make().fetch_query("SELECT * FROM no_existe")
        assert conn.exited == 1

    def test_a_query_error_reaches_the_caller_so_the_tool_can_report_it(self, conn, monkeypatch):
        monkeypatch.setattr(oc.pd, "read_sql", MagicMock(side_effect=ValueError("sql invalido")))

        with pytest.raises(ValueError, match="sql invalido"):
            make().fetch_query("SELEC")


# ---------------------------------------------------------------------------
# fetch_table (sin llamadores hoy, PQ-007: se fija el comportamiento actual)
# ---------------------------------------------------------------------------
class TestFetchTable:
    def test_default_schema_and_limit(self, conn, read_sql):
        make().fetch_table("CPNT023_PART")

        assert read_sql[0][0] == "SELECT * FROM MMDBDP7.CPNT023_PART FETCH FIRST 200 ROWS ONLY"

    def test_an_explicit_schema_wins_over_the_configured_one(self, conn, read_sql):
        make().fetch_table("T", schema="OTRO")

        assert read_sql[0][0] == "SELECT * FROM OTRO.T FETCH FIRST 200 ROWS ONLY"

    def test_without_any_schema_the_table_name_is_used_alone(self, conn, read_sql):
        make(schema="").fetch_table("T")

        assert read_sql[0][0] == "SELECT * FROM T FETCH FIRST 200 ROWS ONLY"

    def test_a_custom_limit(self, conn, read_sql):
        make().fetch_table("T", limit=5)

        assert read_sql[0][0].endswith("FETCH FIRST 5 ROWS ONLY")

    @pytest.mark.parametrize("limit", [None, 0])
    def test_no_limit_reads_the_whole_table(self, conn, read_sql, limit):
        make().fetch_table("T", limit=limit)

        assert read_sql[0][0] == "SELECT * FROM MMDBDP7.T"

    def test_the_limit_is_forced_to_an_integer(self, conn, read_sql):
        """El limite entra al texto de la consulta: `int()` evita que un valor raro (ej. '5; DROP ...') se cuele como SQL."""
        make().fetch_table("T", limit="7")

        assert read_sql[0][0].endswith("FETCH FIRST 7 ROWS ONLY")

    def test_a_non_numeric_limit_is_rejected_instead_of_pasted(self, conn, read_sql):
        with pytest.raises(ValueError):
            make().fetch_table("T", limit="5; DROP TABLE T")
        assert read_sql == []  # no llego a ejecutarse nada
