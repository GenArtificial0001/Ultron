"""Investigacion de rendimiento 2026-09 ("Vision tarda mucho"): un SQL del
modelo sin LIMIT/ROWNUM/FETCH FIRST podia arrastrar una tabla entera de
Oracle/BigQuery antes de truncar lo que se le MUESTRA al modelo (50 filas,
ver MAX_ROWS_TO_MODEL). `_cap_oracle_query`/`_cap_bigquery_query` envuelven
el SQL en una subquery con tope SOLO si no hay ya alguna clausula de limite."""

from __future__ import annotations

from app.tools.dbquery import MAX_ROWS_FROM_DB, _cap_bigquery_query, _cap_oracle_query


def test_oracle_wraps_query_without_limit():
    capped = _cap_oracle_query("SELECT * FROM MMDBDP7.CPNT023_PART")
    assert capped == f"SELECT * FROM (SELECT * FROM MMDBDP7.CPNT023_PART) WHERE ROWNUM <= {MAX_ROWS_FROM_DB}"


def test_oracle_leaves_query_with_rownum_untouched():
    query = "SELECT * FROM t WHERE ROWNUM <= 10"
    assert _cap_oracle_query(query) == query


def test_oracle_leaves_query_with_fetch_first_untouched():
    query = "SELECT * FROM t FETCH FIRST 10 ROWS ONLY"
    assert _cap_oracle_query(query) == query


def test_bigquery_wraps_query_without_limit():
    capped = _cap_bigquery_query("SELECT * FROM `proj.ds.tabla`")
    assert capped == f"SELECT * FROM (SELECT * FROM `proj.ds.tabla`) LIMIT {MAX_ROWS_FROM_DB}"


def test_bigquery_leaves_query_with_limit_untouched():
    query = "SELECT * FROM `proj.ds.tabla` LIMIT 10"
    assert _cap_bigquery_query(query) == query


def test_cap_strips_trailing_semicolon_before_wrapping():
    capped = _cap_oracle_query("SELECT * FROM t;")
    assert capped == f"SELECT * FROM (SELECT * FROM t) WHERE ROWNUM <= {MAX_ROWS_FROM_DB}"
