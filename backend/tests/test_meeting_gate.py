"""Unit del gate heuristico del Modo reunion (spec narracion-agente-efimera
REQ-004/005): maxima sensibilidad ante la mencion + throttle anti-rafaga."""

from __future__ import annotations

import pytest

from app import meeting_gate as mg


@pytest.mark.parametrize(
    "fragment",
    ["ULTRON, que opinas", "che JARVIS", "ultrón, ¿qué decís?", "ultron", "oye Ultron?"],
)
def test_any_mention_triggers(fragment):
    assert mg.check_invocation(fragment, mg.MeetingGateState()) is True


@pytest.mark.parametrize("fragment", ["", "   ", "\n", "hablemos del presupuesto", "supervisiones", "revisiones", "la visión es clara", "vision"])
def test_no_mention_or_blank_never_triggers(fragment):
    assert mg.check_invocation(fragment, mg.MeetingGateState()) is False


def test_throttle_blocks_burst_then_allows_after_interval(monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(mg.time, "monotonic", lambda: clock["t"])
    state = mg.MeetingGateState()

    assert mg.check_invocation("ULTRON uno", state) is True
    clock["t"] += mg._MIN_SECONDS_BETWEEN_INTERVENTIONS - 0.1
    assert mg.check_invocation("ULTRON dos", state) is False  # dentro del espaciado
    clock["t"] += 0.2
    assert mg.check_invocation("ULTRON tres", state) is True


def test_rejected_fragment_does_not_reset_throttle(monkeypatch):
    clock = {"t": 500.0}
    monkeypatch.setattr(mg.time, "monotonic", lambda: clock["t"])
    state = mg.MeetingGateState()
    mg.check_invocation("ULTRON", state)
    stamp = state.last_intervention_at
    clock["t"] += 1
    mg.check_invocation("ULTRON otra vez", state)  # bloqueada por throttle
    mg.check_invocation("sin mencion", state)
    assert state.last_intervention_at == stamp


def test_states_are_independent_per_session():
    a, b = mg.MeetingGateState(), mg.MeetingGateState()
    assert mg.check_invocation("ULTRON", a) is True
    assert mg.check_invocation("ULTRON", b) is True
