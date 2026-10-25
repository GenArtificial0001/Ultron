"""Unit de `app/session/compaction.py` (spec gestion-contexto-conversacion):
poda de outputs de tools, corte por turnos y disparo automatico al 85%.
El LLM que resume (`_summarize`) se reemplaza por un fake: nunca hay red."""

from __future__ import annotations

import asyncio

import pytest

from app.session import compaction as cp


def _user(text: str) -> dict:
    return {"role": "user", "content": text}


def _assistant(text: str) -> dict:
    return {"role": "assistant", "content": text}


def _tool(text: str, call_id: str = "c1") -> dict:
    return {"role": "tool", "tool_call_id": call_id, "content": text}


def test_split_into_turns_starts_a_turn_at_each_user_message():
    msgs = [_user("1"), _assistant("a"), _user("2"), _assistant("b"), _tool("t")]
    turns = cp._split_into_turns(msgs)
    assert [len(t) for t in turns] == [2, 3]
    assert cp._split_into_turns([]) == []


@pytest.mark.parametrize(
    ("window", "expected"),
    [(1_000, 2_000), (20_000, 5_000), (10_000_000, 15_000)],
)
def test_recent_budget_is_clamped(window, expected):
    assert cp._recent_budget_tokens(window) == expected


def test_prune_keeps_recent_tool_outputs_and_call_ids():
    big = "x" * (cp._PRUNE_PROTECT_TOKENS * 4)  # llena la ventana protegida por si sola
    msgs = [_tool("viejo", "old"), _tool(big, "new")]
    assert cp._prune_tool_outputs(msgs) is True
    assert msgs[0]["content"] == cp._PRUNED_MARKER
    assert msgs[0]["tool_call_id"] == "old"  # estructura intacta
    assert msgs[1]["content"] == big


def test_prune_is_idempotent_and_reports_nothing_to_do():
    msgs = [_tool("chico")]
    assert cp._prune_tool_outputs(msgs) is False
    assert cp._prune_tool_outputs(msgs) is False


def test_select_recent_turns_always_keeps_the_latest_turn():
    huge = [_user("u" * 400_000)]
    old, recent = cp._select_recent_turns([[_user("a")], huge], context_window=10_000)
    assert recent == [huge] and old == [[_user("a")]]


def test_transcript_formats_roles_and_truncates_tool_output():
    transcript = cp._turns_to_transcript(
        [[_user("hola"), _assistant("que tal"), _tool("z" * 500), {"role": "user", "content": []}]]
    )
    lines = transcript.splitlines()
    assert lines[0] == "Usuario: hola" and lines[1] == "ULTRON: que tal"
    assert lines[2].startswith("[resultado de herramienta] ") and len(lines[2]) < 260
    assert lines[3] == "Usuario: (mensaje con imagenes)"


def test_compact_session_replaces_old_turns_with_summary(monkeypatch):
    async def fake_summarize(transcript: str, model: str) -> str:
        assert "Usuario: viejo" in transcript
        return "RESUMEN"

    monkeypatch.setattr(cp, "_summarize", fake_summarize)
    system = {"role": "system", "content": "sys"}
    filler = "r" * 12_000  # ~3000 tokens: excede el presupuesto reciente (2000) de 1 turno
    messages = [system, _user("viejo"), _assistant(filler), _user("nuevo"), _assistant(filler)]

    assert asyncio.run(cp.compact_session("s", messages, 1_000, "m")) is True
    assert messages[0] == system
    assert "RESUMEN" in messages[1]["content"] and "1 turno(s)" in messages[1]["content"]
    assert messages[-2]["content"] == "nuevo"  # el turno reciente se conserva completo


def test_compact_session_without_old_turns_is_a_noop(monkeypatch):
    async def boom(*_a):
        raise AssertionError("no debe resumir si no hay turnos viejos")

    monkeypatch.setattr(cp, "_summarize", boom)
    messages = [_user("unico"), _assistant("r")]
    assert asyncio.run(cp.compact_session("s", messages, 100_000, "m")) is False
    assert len(messages) == 2


def test_maybe_compact_below_threshold_does_nothing(monkeypatch):
    async def boom(*_a, **_k):
        raise AssertionError("no debe compactar bajo el umbral")

    monkeypatch.setattr(cp, "compact_session", boom)
    assert asyncio.run(cp.maybe_compact("s", [_user("hola")], 100_000, "m")) is False
    assert asyncio.run(cp.maybe_compact("s", [_user("hola")], 0, "m")) is False  # ventana invalida


def test_maybe_compact_triggers_at_threshold(monkeypatch):
    called: list[str] = []

    async def fake_compact(session_id, messages, window, model):
        called.append(session_id)
        return True

    monkeypatch.setattr(cp, "compact_session", fake_compact)
    messages = [_user("m" * 4_000)]  # 1000 tokens sobre una ventana de 1000 -> 100%
    assert asyncio.run(cp.maybe_compact("sid", messages, 1_000, "m")) is True
    assert called == ["sid"]


# REQ-014 c (spec fix-detener-y-progreso-visible, revision): mientras se espera el resumen del modelo (18 s medidos) el usuario puede
# borrar o editar historial viejo (el turno que compacta sigue corriendo). La compactacion no puede devolverle lo borrado a la memoria.
def _history_with_old_and_recent():
    system = {"role": "system", "content": "sys"}
    filler = "r" * 12_000  # ~3000 tokens: el turno viejo no entra en el presupuesto reciente (2000)
    return [system, _user("viejo"), _assistant(filler), _user("BORRAME"), _assistant("resp borrame"), _user("nuevo")]


def test_a_recent_message_deleted_while_summarizing_does_not_come_back(monkeypatch):
    messages = _history_with_old_and_recent()

    async def fake_summarize(transcript: str, model: str) -> str:
        del messages[3:5]  # el usuario borra el turno «BORRAME» mientras se espera el resumen
        return "RESUMEN"

    monkeypatch.setattr(cp, "_summarize", fake_summarize)

    assert asyncio.run(cp.compact_session("s", messages, 1_000, "m")) is True
    contents = [m["content"] for m in messages]
    assert "BORRAME" not in contents and "resp borrame" not in contents, f"lo borrado volvio a la memoria: {contents}"
    assert contents[0] == "sys" and "RESUMEN" in contents[1] and contents[2:] == ["nuevo"]


def test_an_old_message_deleted_while_summarizing_discards_the_stale_summary(monkeypatch):
    messages = _history_with_old_and_recent()

    async def fake_summarize(transcript: str, model: str) -> str:
        assert "Usuario: viejo" in transcript
        del messages[1:5]  # el usuario borra «viejo» (y todo lo que seguia hasta el turno en curso)
        return "RESUMEN que habla de viejo"

    monkeypatch.setattr(cp, "_summarize", fake_summarize)

    # El resumen ya no sirve (habla de lo que se borro): no se aplica y la lista queda como la dejo el borrado.
    assert asyncio.run(cp.compact_session("s", messages, 1_000, "m")) is False
    assert [m["content"] for m in messages] == ["sys", "nuevo"]
