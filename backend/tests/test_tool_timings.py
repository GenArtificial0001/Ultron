"""Unit de `app/tool_timings.py` (barra de progreso adaptativa): ventana
movil, presupuesto por promedio y degradacion silenciosa ante un JSON roto
(regla del AGENTS.md: los paths no criticos nunca rompen el turno)."""

from __future__ import annotations

import json

import pytest

from app import tool_timings as tt


@pytest.fixture
def timings_file(tmp_path, monkeypatch):
    path = tmp_path / "data" / "tool_timings.json"
    monkeypatch.setattr(tt, "TIMINGS_FILE", path)
    return path


def test_unknown_operation_returns_default_budget_without_history(timings_file):
    assert tt.get_budget_ms("bash") == (tt.DEFAULT_BUDGET_MS, False)


def test_budget_is_average_of_recorded_durations(timings_file):
    for ms in (100, 200, 300):
        tt.record_duration("bash", ms)
    assert tt.get_budget_ms("bash") == (200.0, True)


def test_window_keeps_only_most_recent_measurements(timings_file):
    for i in range(tt.WINDOW_SIZE + 5):
        tt.record_duration("read", i)
    history = json.loads(timings_file.read_text(encoding="utf-8"))["read"]
    assert len(history) == tt.WINDOW_SIZE
    assert history[0] == 5 and history[-1] == tt.WINDOW_SIZE + 4


def test_operations_are_tracked_independently(timings_file):
    tt.record_duration("a", 10)
    tt.record_duration("b", 30)
    assert tt.get_budget_ms("a")[0] == 10 and tt.get_budget_ms("b")[0] == 30


def test_corrupt_file_degrades_to_empty_and_recovers(timings_file):
    timings_file.parent.mkdir(parents=True)
    timings_file.write_text("{no es json", encoding="utf-8")
    assert tt.get_budget_ms("bash") == (tt.DEFAULT_BUDGET_MS, False)
    tt.record_duration("bash", 50)  # reescribe el archivo sano
    assert tt.get_budget_ms("bash") == (50.0, True)


def test_unwritable_target_never_raises(tmp_path, monkeypatch):
    # El "archivo" es un directorio: write_text falla -> se loguea y sigue.
    bad = tmp_path / "es-un-dir"
    bad.mkdir()
    monkeypatch.setattr(tt, "TIMINGS_FILE", bad)
    tt.record_duration("bash", 1)


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("oracle_query", "Consultando Oracle…"),
        ("skill:x", "Cargando skill x…"),
        ("herramienta_nueva", "Ejecutando herramienta_nueva…"),
    ],
)
def test_describe_operation(key, expected):
    assert tt.describe_operation(key) == expected
