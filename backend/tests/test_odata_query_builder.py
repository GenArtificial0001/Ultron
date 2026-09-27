"""Unit de `ODataQueryBuilder` (Builder OData v2 de `SapConnector`): puro, sin
HTTP ni CSRF -- exactamente la ventaja que motivo extraerlo del conector."""

from __future__ import annotations

from app.connectors.odata_query_builder import ODataQueryBuilder

BASE = "https://sap.example/sap/opu/odata/sap/SVC/"


def test_url_without_entity_params():
    url = ODataQueryBuilder("ZENT").build_url(BASE)
    assert url == BASE + "ZENT/Results"


def test_url_with_function_import_params_inside_parentheses():
    url = (
        ODataQueryBuilder("ZENT")
        .with_params({"P_a": "1", "P_b": "datetime'2026-07-24T00:00:00'"})
        .build_url(BASE)
    )
    assert url == BASE + "ZENT(P_a=1,P_b=datetime'2026-07-24T00:00:00')/Results"


def test_custom_results_suffix():
    assert ODataQueryBuilder("ZENT", results_suffix="").build_url("/") == "/ZENT"


def test_query_params_defaults():
    params = ODataQueryBuilder("ZENT").build_query_params(skip=0, sap_client="100")
    assert params == {
        "sap-client": "100",
        "$format": "json",
        "$skip": 0,
        "$top": 200,
        "$inlinecount": "allpages",
    }


def test_query_params_with_select_filter_and_page_size():
    builder = (
        ODataQueryBuilder("ZENT")
        .with_select(["a", "b"])
        .with_filter("W eq 'FI07'")
        .with_page_size(50)
    )
    params = builder.build_query_params(skip=150, sap_client="200")
    assert params["$select"] == "a,b"
    assert params["$filter"] == "W eq 'FI07'"
    assert params["$top"] == 50 and params["$skip"] == 150
    assert builder.page_size == 50


def test_none_select_and_filter_are_omitted():
    params = (
        ODataQueryBuilder("ZENT")
        .with_select(None)
        .with_filter(None)
        .build_query_params(0, "100")
    )
    assert "$select" not in params and "$filter" not in params


def test_skip_varies_per_page_but_builder_is_reusable():
    builder = ODataQueryBuilder("ZENT").with_page_size(10)
    skips = [builder.build_query_params(s, "100")["$skip"] for s in (0, 10, 20)]
    assert skips == [0, 10, 20]


def test_with_params_copies_input_dict():
    original = {"P_a": "1"}
    builder = ODataQueryBuilder("ZENT").with_params(original)
    original["P_a"] = "mutado"
    assert "P_a=1" in builder.build_url("/")
