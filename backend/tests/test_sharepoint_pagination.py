"""Paginacion del conector de SharePoint (`fetch_list_items`, `list_lists`) y su tool `sharepoint_list_items`.

Bug real reportado por el usuario (2026-10-01): el tracker ZCC mostraba items 249-354 en SharePoint pero Vision solo veia
hasta el id 213, siempre el mismo bloque viejo. Causa: `top=200` hacia de tamano de pagina Y de tope total, asi que toda
lista con mas de 200 items se cortaba en los primeros 200 aunque Graph siguiera ofreciendo `@odata.nextLink`.

Sin red: `requests.get` se reemplaza por un Graph falso que sirve paginas y registra cada URL pedida."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.connectors import sharepoint_connector as spc
from app.tools import dbquery


class FakeGraph:
    """Sirve `total` items de a `page_size` (lee `$top` de la URL) con `@odata.nextLink`, como Graph."""

    def __init__(self, total: int, status: int = 200, fail_on_page: int | None = None):
        self.total, self.status, self.fail_on_page, self.urls = total, status, fail_on_page, []

    def get(self, url, **_kw):
        self.urls.append(url)
        page = len(self.urls)
        if self.fail_on_page == page:
            return SimpleNamespace(status_code=500, text="boom", json=lambda: {})
        top = int(url.split("$top=")[1].split("&")[0]) if "$top=" in url else 200
        start = 0 if "skip=" not in url else int(url.split("skip=")[1])
        end = min(start + top, self.total)
        body = {"value": [{"fields": {"id": i + 1}} for i in range(start, end)]}
        if end < self.total:
            body["@odata.nextLink"] = f"{url.split('?')[0]}?$top={top}&skip={end}"
        return SimpleNamespace(status_code=self.status, text="", json=lambda: body)


@pytest.fixture
def connector(monkeypatch):
    monkeypatch.setattr(spc.SharePointConnector, "_acquire_token", lambda self: "tok")
    monkeypatch.setattr(spc.SharePointConnector, "_resolve_site_id", lambda self: "site")
    monkeypatch.setattr(spc.SharePointConnector, "_get_list_id", lambda self, name: "lista")
    monkeypatch.setattr(spc.SharePointConnector, "_proxies", lambda self: None)
    settings = SimpleNamespace(sharepoint_default_list_name="ZCC")
    return spc.SharePointConnector(settings)


def serve(monkeypatch, graph: FakeGraph) -> FakeGraph:
    monkeypatch.setattr(spc.requests, "get", graph.get)
    return graph


# --- el bug original ---------------------------------------------------------------------------------------------
def test_a_list_longer_than_one_page_is_read_completely(connector, monkeypatch):
    graph = serve(monkeypatch, FakeGraph(total=354))  # el caso real: 354 items, antes solo llegaba hasta ~200

    df = connector.fetch_list_items("ZCC")

    assert len(df) == 354
    assert df["id"].max() == 354, "el item mas reciente tiene que aparecer (antes se cortaba en el bloque viejo)"
    assert len(graph.urls) == 2 and "$top=200" in graph.urls[0]


def test_page_size_only_controls_the_http_page_not_the_total(connector, monkeypatch):
    graph = serve(monkeypatch, FakeGraph(total=95))

    df = connector.fetch_list_items("ZCC", page_size=20)

    assert len(df) == 95 and len(graph.urls) == 5
    assert all("$top=20" in u for u in graph.urls)


def test_a_short_list_needs_a_single_request(connector, monkeypatch):
    graph = serve(monkeypatch, FakeGraph(total=7))

    assert len(connector.fetch_list_items("ZCC")) == 7 and len(graph.urls) == 1


def test_an_empty_list_gives_an_empty_table(connector, monkeypatch):
    serve(monkeypatch, FakeGraph(total=0))

    assert connector.fetch_list_items("ZCC").empty


# --- max_items: el tope total, ahora explicito ----------------------------------------------------------------------
def test_max_items_caps_the_total_exactly(connector, monkeypatch):
    serve(monkeypatch, FakeGraph(total=354))

    df = connector.fetch_list_items("ZCC", max_items=250)

    assert len(df) == 250 and list(df["id"][:3]) == [1, 2, 3]


def test_max_items_stops_asking_for_pages_it_does_not_need(connector, monkeypatch):
    graph = serve(monkeypatch, FakeGraph(total=1000))

    connector.fetch_list_items("ZCC", page_size=100, max_items=150)

    assert len(graph.urls) == 2, "con 150 de tope y paginas de 100 alcanzan 2 pedidos, no 10"


def test_max_items_larger_than_the_list_returns_everything(connector, monkeypatch):
    serve(monkeypatch, FakeGraph(total=30))

    assert len(connector.fetch_list_items("ZCC", max_items=500)) == 30


# --- errores ----------------------------------------------------------------------------------------------------------
def test_an_error_on_a_later_page_is_reported_not_swallowed(connector, monkeypatch):
    serve(monkeypatch, FakeGraph(total=500, fail_on_page=2))

    with pytest.raises(spc.SharePointConnectionError, match="500"):
        connector.fetch_list_items("ZCC")


def test_missing_list_name_asks_for_it(monkeypatch):
    monkeypatch.setattr(spc.SharePointConnector, "_acquire_token", lambda self: "tok")
    c = spc.SharePointConnector(SimpleNamespace(sharepoint_default_list_name=""))
    with pytest.raises(ValueError, match="list_display_name"):
        c.fetch_list_items()


# --- list_lists pagina igual --------------------------------------------------------------------------------------------
def test_list_lists_follows_next_link(connector, monkeypatch):
    pages = [
        {"value": [{"displayName": "A"}, {"displayName": "B"}], "@odata.nextLink": "https://g/next"},
        {"value": [{"displayName": "C"}]},
    ]
    seen = []

    def get(url, **_kw):
        seen.append(url)
        return SimpleNamespace(status_code=200, text="", json=lambda: pages[len(seen) - 1])

    monkeypatch.setattr(spc.requests, "get", get)

    assert connector.list_lists() == ["A", "B", "C"] and seen[1] == "https://g/next"


def test_list_lists_error_is_reported(connector, monkeypatch):
    monkeypatch.setattr(spc.requests, "get", lambda *_a, **_k: SimpleNamespace(status_code=403, text="no", json=lambda: {}))

    with pytest.raises(spc.SharePointConnectionError, match="403"):
        connector.list_lists()


# --- la tool que ve el modelo ---------------------------------------------------------------------------------------------
class RecordingConnector:
    calls: list[tuple] = []

    def __init__(self, _settings):
        pass

    def fetch_list_items(self, name, **kw):
        RecordingConnector.calls.append((name, kw))
        import pandas as pd

        return pd.DataFrame({"id": [1, 2]})


@pytest.fixture
def tool(monkeypatch):
    RecordingConnector.calls = []
    monkeypatch.setattr(dbquery, "SharePointConnector", RecordingConnector)
    monkeypatch.setattr(dbquery, "get_settings", lambda: SimpleNamespace(enable_sharepoint=True))
    monkeypatch.setattr(dbquery, "_df_to_text", lambda df, *_a, **_k: f"{len(df)} filas")
    return dbquery._sharepoint_list_items_execute


def test_the_tool_exposes_max_items_and_no_longer_promises_only_200():
    props = dbquery.sharepoint_list_items_tool.parameters["properties"]
    assert "max_items" in props and props["max_items"]["type"] == "integer"
    assert "TODOS" in dbquery.sharepoint_list_items_tool.description


def test_the_tool_without_max_items_asks_for_the_whole_list(tool):
    assert tool({"list_name": "ZCC"}) == "2 filas"
    assert RecordingConnector.calls == [("ZCC", {"max_items": None})]


def test_the_tool_passes_an_explicit_cap_through(tool):
    tool({"list_name": "ZCC", "max_items": 50})
    assert RecordingConnector.calls[0][1] == {"max_items": 50}


@pytest.mark.parametrize("raw, expected", [("50", 50), (" 50 ", 50), (50.0, 50), ("50.7", 50), ("1e2", 100), (3.9, 3)])
def test_the_tool_accepts_a_cap_the_model_sent_as_text_or_float(tool, raw, expected):
    """El modelo a veces manda "50" (texto) o 50.0 en vez de 50: no debe romper la consulta."""
    out = tool({"list_name": "ZCC", "max_items": raw})
    assert out == "2 filas" and RecordingConnector.calls[0][1] == {"max_items": expected}


@pytest.mark.parametrize("raw", ["muchos", "", 0, -5, True, [1]])
def test_the_tool_ignores_a_nonsense_cap_instead_of_returning_nothing(tool, raw):
    """0, negativo o basura NO deben devolver una tabla vacia en silencio: se ignora el tope y se trae la lista entera."""
    tool({"list_name": "ZCC", "max_items": raw})
    assert RecordingConnector.calls[0][1] == {"max_items": None}


def test_parse_max_items_directly():
    """Un booleano no es un tope: `True` no debe convertirse en 1 y recortar la lista a una fila."""
    parse = dbquery._parse_max_items
    assert parse(None) is None
    assert parse(True) is None and parse(False) is None
    assert parse("1e2") == 100 and parse("50.7") == 50
    assert parse(float("inf")) is None and parse(float("nan")) is None
