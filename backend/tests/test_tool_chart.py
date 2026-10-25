"""Unit de `render_chart` (spec dashboard-graficos): agrupa un CSV de un
resultado previo por categoria y emite el evento SSE `chart-ready`. La lectura
del resultado persistido (`persistence.find_tool_result`) se stubea."""

from __future__ import annotations

import pytest

from app.tools import chart as c


class FakeSession:
    def __init__(self, sid="s1"):
        self.session_id = sid
        self.events: list[tuple[str, dict]] = []

    def push_event(self, kind, payload):
        self.events.append((kind, payload))


CSV = "tipo,qty\nA,1\nB,2\nA,3\nB,x\n,5\n"


@pytest.fixture
def stored(monkeypatch):
    """Simula el resultado de una tool de datos guardado en SQLite."""
    state = {"part": {"result": CSV}, "args": None}

    def find(session_id, call_id, tools):
        state["args"] = (session_id, call_id, tools)
        return state["part"]

    monkeypatch.setattr(c.persistence, "find_tool_result", find)
    return state


def run(args, session=None):
    return c._render_chart_execute(args, session)


def base_args(**over):
    args = {"chart_type": "donut", "category_column": "tipo", "value_column": "qty"}
    args.update(over)
    return args


def test_parse_csv_handles_quotes_and_blank_lines():
    headers, rows = c._parse_csv('a,b\n"x,y",2\n\n')
    assert headers == ["a", "b"] and rows == [["x,y", "2"]]
    assert c._parse_csv("") == ([], [])


@pytest.mark.parametrize(
    ("session", "args", "fragment"),
    [
        (None, base_args(), "no hay sesion activa"),
        (FakeSession(""), base_args(), "no hay sesion activa"),
        (FakeSession(), base_args(chart_type="pie"), "'donut' o 'bar'"),
        (FakeSession(), base_args(category_column=""), "faltan"),
        (FakeSession(), base_args(value_column=""), "faltan"),
    ],
)
def test_input_validation(stored, session, args, fragment):
    assert fragment in run(args, session)


def test_groups_by_category_summing_values_and_emits_event(stored):
    s = FakeSession()
    out = run(base_args(), s)
    kind, payload = s.events[0]
    assert kind == "chart-ready"
    grouped = dict(zip(payload["labels"], payload["values"], strict=True))
    # 5 filas; B: 2 + (no numerico -> cuenta 1); categoria vacia -> "(vacío)"
    assert grouped == {"A": 4.0, "B": 3.0, "(vacío)": 5.0}
    assert payload["title"] == "qty por tipo" and payload["chart_type"] == "donut"
    assert "3 categorias" in out and "5 filas" in out and "SQLite" in out


def test_custom_title_and_call_id_are_forwarded(stored):
    s = FakeSession()
    run(base_args(title="Mi grafico", tool_call_id="call_7", chart_type="bar"), s)
    assert s.events[0][1]["title"] == "Mi grafico"
    assert stored["args"] == ("s1", "call_7", c.CHARTABLE_TOOLS)


def test_falls_back_to_csv_fallback_when_nothing_persisted(stored):
    stored["part"] = None
    s = FakeSession()
    out = run(base_args(csv_fallback="tipo,qty\nZ,9\n"), s)
    assert s.events[0][1]["labels"] == ["Z"] and "truncado" in out


def test_empty_persisted_result_uses_fallback(stored):
    stored["part"] = {"result": ""}
    assert "1 categorias" in run(base_args(csv_fallback="tipo,qty\nZ,9\n"), FakeSession())


def test_no_data_anywhere(stored):
    stored["part"] = None
    assert "no se encontro ningun resultado" in run(base_args(), FakeSession())


def test_unknown_columns_list_available_ones(stored):
    out = run(base_args(category_column="nope"), FakeSession())
    assert "'nope' no existe" in out and "tipo, qty" in out
    assert "'zzz' no existe" in run(base_args(value_column="zzz"), FakeSession())


def test_short_rows_are_skipped(stored):
    stored["part"] = {"result": "tipo,qty\nA,1\nB\n"}
    s = FakeSession()
    run(base_args(), s)
    assert s.events[0][1]["labels"] == ["A"]


def test_tool_metadata():
    assert c.render_chart_tool.name == "render_chart" and not c.render_chart_tool.dangerous
