"""Spec aviso-compactacion-automatica (0.36.2): Vision AVISA cuando compacta o poda la conversacion, al empezar y al terminar.

Caso real (2026-10-24): una sesion al 100 % quedo en 43 % sin ningun aviso. `maybe_compact` primero PODA los resultados de herramientas viejos y solo
si sigue arriba del 85 % RESUME con el modelo; en una sesion con muchos resultados la poda sola alcanza, devolvia `False` y el aviso nunca salia.
Se prueba `prepare_compaction` (poda y dice que paso, sin llamar al modelo), que `maybe_compact` conserva su contrato, y lo que la persona ve por
`POST /api/chat`. El modelo es un FAKE: nunca hay red ni login del proveedor de IA."""

from __future__ import annotations

import asyncio
import re

import pytest
from fastapi.testclient import TestClient

from app import main
from app.session import compaction as cp

import test_chat_end_to_end as chat  # noqa: E402  (el entorno falso del chat de punta a punta: modelo, sesion y compactacion apagada)

WINDOW = 1_000_000

# Fixtures del chat de punta a punta, reutilizadas por alias (importarlas con su nombre las pisaria en cada parametro: F811).
_offline = chat._offline  # autouse: apaga red, catalogo y compactacion para cada test de este archivo
model = chat.model
sid = chat.sid
ask = chat.ask
says = chat.says
shown_text = chat.shown_text
types = chat.types

# La fixture autouse `_offline` (importada de test_chat_end_to_end) reemplaza `maybe_compact` por una que devuelve False durante cada test. Para probar
# el contrato REAL se captura aca, al importar el modulo, antes de que actue ninguna fixture.
REAL_MAYBE_COMPACT = cp.maybe_compact


def big_history(calls: int, size: int = 30_000) -> list[dict]:
    """Conversacion con muchos resultados de herramientas grandes: la poda (ventana protegida de ~20.000 tokens) los vacia casi todos."""
    msgs: list[dict] = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hola"}]
    for i in range(calls):
        msgs.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": f"c{i}", "type": "function", "function": {"name": "x", "arguments": "{}"}}],
            }
        )
        msgs.append({"role": "tool", "tool_call_id": f"c{i}", "content": "x" * size})
    msgs.append({"role": "user", "content": "otra pregunta"})
    return msgs


def text_history(chars: int) -> list[dict]:
    """Conversacion de puro texto: la poda no libera nada, hace falta resumir."""
    return [{"role": "system", "content": "sys"}] + [
        {"role": "user" if i % 2 == 0 else "assistant", "content": "a" * (chars // 20)} for i in range(20)
    ]


@pytest.fixture
def fake_summary(monkeypatch):
    calls: list[str] = []

    async def fake(session_id, messages, window, model_id):
        calls.append(session_id)
        return True

    monkeypatch.setattr(cp, "compact_session", fake)
    return calls


# ---------------------------------------------------------------------------------------------------------------------
# REQ-001 / REQ-002: `prepare_compaction` poda y dice que paso (sin llamar al modelo)
# ---------------------------------------------------------------------------------------------------------------------
def test_below_the_threshold_nothing_is_touched():
    msgs = [{"role": "user", "content": "hola"}]
    before = [dict(m) for m in msgs]
    report = cp.prepare_compaction(msgs, WINDOW)
    assert report["action"] == "none" and msgs == before


def test_an_invalid_window_is_none():
    assert cp.prepare_compaction(big_history(5), 0)["action"] == "none"


def test_when_pruning_is_enough_it_says_pruned():
    """El caso real: arriba del 85 % y con la poda alcanza."""
    report = cp.prepare_compaction(big_history(90), WINDOW)
    assert report["action"] == "pruned"
    assert report["before_pct"] >= 85 and report["after_pct"] < 85


def test_when_pruning_is_not_enough_it_says_needs_summary():
    report = cp.prepare_compaction(text_history(WINDOW * 3), WINDOW)
    assert report["action"] == "needs_summary" and report["after_pct"] >= 85


def test_it_never_calls_the_model(fake_summary):
    cp.prepare_compaction(text_history(WINDOW * 3), WINDOW)
    cp.prepare_compaction(big_history(90), WINDOW)
    assert fake_summary == []


def test_the_percentages_are_integers_and_after_is_not_higher_than_before():
    report = cp.prepare_compaction(big_history(90), WINDOW)
    assert isinstance(report["before_pct"], int) and isinstance(report["after_pct"], int)
    assert report["after_pct"] <= report["before_pct"]


def test_the_percentages_match_the_real_estimate():
    msgs = big_history(90)
    expected_before = round(cp.estimate_tokens(msgs) / WINDOW * 100)
    report = cp.prepare_compaction(msgs, WINDOW)
    assert report["before_pct"] == expected_before
    assert report["after_pct"] == round(cp.estimate_tokens(msgs) / WINDOW * 100)


def test_it_is_idempotent_so_maybe_compact_afterwards_does_nothing_more(fake_summary):
    msgs = big_history(90)
    cp.prepare_compaction(msgs, WINDOW)
    snapshot = [dict(m) for m in msgs]
    assert asyncio.run(REAL_MAYBE_COMPACT("s", msgs, WINDOW, "m")) is False
    assert msgs == snapshot and fake_summary == []


# ---------------------------------------------------------------------------------------------------------------------
# REQ-003: maybe_compact no cambia su contrato
# ---------------------------------------------------------------------------------------------------------------------
def test_maybe_compact_returns_false_when_only_pruned(fake_summary):
    assert asyncio.run(REAL_MAYBE_COMPACT("s", big_history(90), WINDOW, "m")) is False


def test_maybe_compact_returns_true_when_summarized(fake_summary):
    assert asyncio.run(REAL_MAYBE_COMPACT("s", text_history(WINDOW * 3), WINDOW, "m")) is True


def test_maybe_compact_returns_false_below_the_threshold(fake_summary):
    assert asyncio.run(REAL_MAYBE_COMPACT("s", [{"role": "user", "content": "hola"}], WINDOW, "m")) is False


def test_maybe_compact_still_lets_a_failure_propagate(monkeypatch):
    """Contrato de siempre: el que llama (`_compact_history_best_effort`) decide que hacer con la excepcion."""

    async def boom(*a, **k):
        raise RuntimeError("429 tope")

    monkeypatch.setattr(cp, "compact_session", boom)
    with pytest.raises(RuntimeError):
        asyncio.run(REAL_MAYBE_COMPACT("s", text_history(WINDOW * 3), WINDOW, "m"))


# ---------------------------------------------------------------------------------------------------------------------
# REQ-004..008: lo que la persona ve por /api/chat
# ---------------------------------------------------------------------------------------------------------------------
@pytest.fixture
def client():
    return TestClient(main.app)


def notes(events: list[dict]) -> list[str]:
    return [e["data"]["text"] for e in events if e["type"] == "system-note"]


def stage(monkeypatch, *, prune=None, summarized=None, prune_raises=None, summary_raises=None, log=None):
    """Arma el turno: `prune` es lo que devuelve `prepare_compaction` (dict) y `summarized` lo que devuelve `maybe_compact` (bool)."""
    import app.session.compaction as compaction_module

    def fake_prepare(messages, window):
        if log is not None:
            log.append("poda")
        if prune_raises:
            raise prune_raises
        return prune

    async def fake_maybe(session_id, messages, window, model_id):
        if log is not None:
            log.append("resumen")
        if summary_raises:
            raise summary_raises
        return summarized

    monkeypatch.setattr(compaction_module, "prepare_compaction", fake_prepare)
    monkeypatch.setattr(compaction_module, "maybe_compact", fake_maybe)


NEEDS = {"action": "needs_summary", "before_pct": 100, "after_pct": 97}


def test_a_summary_announces_the_start_and_then_the_result(client, model, sid, monkeypatch):
    stage(monkeypatch, prune=NEEDS, summarized=True)
    model.use([says("[[TIPO: conversacional]]Listo.")])

    events = ask(client, sid, "hola")

    found = notes(events)
    assert len(found) == 2
    assert re.search(r"resumiendo", found[0], re.I)
    assert "100" in found[1]
    assert types(events).index("system-note") < types(events).index("text-delta")


def test_the_start_note_goes_out_before_the_summary_work_starts(client, model, sid, monkeypatch):
    """La nota de inicio tiene que salir ANTES de la espera del resumen, no despues: se mide el orden real de los dos hechos."""
    order: list[str] = []
    stage(monkeypatch, prune=NEEDS, summarized=True, log=order)
    model.use([says("[[TIPO: conversacional]]Listo.")])

    original_notice = __import__("app.llm.loop", fromlist=["_compaction_notice"])._compaction_notice

    def spy(kind, report, after_pct=None):
        order.append(f"nota:{kind}")
        return original_notice(kind, report, after_pct)

    monkeypatch.setattr("app.llm.loop._compaction_notice", spy)
    ask(client, sid, "hola")

    assert order.index("nota:start") < order.index("resumen") < order.index("nota:summarized")


def test_the_closing_note_reports_the_real_percentage_after_summarizing(client, model, sid, monkeypatch):
    import app.session.compaction as compaction_module

    def fake_prepare(messages, window):
        return {"action": "needs_summary", "before_pct": 100, "after_pct": 100}

    async def shrink(session_id, messages, window, model_id):
        del messages[1:]  # deja solo el system prompt: el uso real baja mucho
        return True

    monkeypatch.setattr(compaction_module, "prepare_compaction", fake_prepare)
    monkeypatch.setattr(compaction_module, "maybe_compact", shrink)
    model.use([says("[[TIPO: conversacional]]Listo.")])

    found = notes(ask(client, sid, "hola"))

    assert len(found) == 2
    closing = found[1]
    assert "100%" in closing
    (after,) = re.findall(r"a (\d+)%", closing)
    assert int(after) < 5


def test_pruning_alone_gives_one_note_with_the_percentages_and_no_start_note(client, model, sid, monkeypatch):
    stage(monkeypatch, prune={"action": "pruned", "before_pct": 100, "after_pct": 43}, summarized=False)
    model.use([says("[[TIPO: conversacional]]Listo.")])

    found = notes(ask(client, sid, "hola"))

    assert len(found) == 1
    assert "100%" in found[0] and "43%" in found[0]
    assert not re.search(r"resumiendo", found[0], re.I)


def test_nothing_to_do_means_no_notes(client, model, sid, monkeypatch):
    stage(monkeypatch, prune={"action": "none", "before_pct": 20, "after_pct": 20}, summarized=False)
    model.use([says("[[TIPO: conversacional]]Listo.")])
    assert notes(ask(client, sid, "hola")) == []


def test_a_summary_that_could_not_be_done_says_so_and_the_turn_goes_on(client, model, sid, monkeypatch):
    stage(monkeypatch, prune=NEEDS, summarized=False)
    model.use([says("[[TIPO: conversacional]]Sigo igual.")])

    events = ask(client, sid, "hola")

    found = notes(events)
    assert len(found) == 2
    assert re.search(r"resumiendo", found[0], re.I)
    assert re.search(r"no pude", found[1], re.I)
    assert shown_text(events) == "Sigo igual." and types(events)[-1] == "done" and "error" not in types(events)


def test_a_summary_that_raises_says_so_and_never_cuts_the_turn(client, model, sid, monkeypatch, caplog):
    stage(monkeypatch, prune=NEEDS, summary_raises=RuntimeError("429 tope de consumo"))
    model.use([says("[[TIPO: conversacional]]Sigo.")])

    with caplog.at_level("WARNING"):
        events = ask(client, sid, "hola")

    found = notes(events)
    assert len(found) == 2 and re.search(r"no pude", found[1], re.I)
    assert shown_text(events) == "Sigo." and "error" not in types(events)
    assert "se sigue sin compactar" in caplog.text


def test_a_failure_while_pruning_never_cuts_the_turn_and_adds_no_notes(client, model, sid, monkeypatch):
    stage(monkeypatch, prune_raises=RuntimeError("explotó"), summarized=False)
    model.use([says("[[TIPO: conversacional]]Sigo.")])

    events = ask(client, sid, "hola")

    assert shown_text(events) == "Sigo." and "error" not in types(events)
    assert notes(events) == []


def test_if_something_compacts_without_a_prior_report_it_still_announces_it(client, model, sid, monkeypatch):
    """Compatibilidad: un `maybe_compact` que resume aunque la poda dijo `none` sigue avisando, como hasta ahora."""
    stage(monkeypatch, prune={"action": "none", "before_pct": 10, "after_pct": 10}, summarized=True)
    model.use([says("[[TIPO: conversacional]]Listo.")])

    found = notes(ask(client, sid, "hola"))

    assert len(found) == 1 and "Resumí" in found[0]


@pytest.mark.parametrize("scenario", ["summarized", "pruned", "failed"])
def test_the_notes_have_no_jargon(client, model, sid, monkeypatch, scenario):
    if scenario == "pruned":
        stage(monkeypatch, prune={"action": "pruned", "before_pct": 100, "after_pct": 43}, summarized=False)
    else:
        stage(monkeypatch, prune=NEEDS, summarized=(scenario == "summarized"))
    model.use([says("[[TIPO: conversacional]]Listo.")])

    texts = notes(ask(client, sid, "hola"))

    assert texts
    for text in texts:
        for jargon in (
            "token",
            "poda",
            "podad",
            "contexto del modelo",
            "maybe_compact",
            "429",
            "HTTP",
            "JSON",
            "Traceback",
            "prune",
        ):
            assert jargon.lower() not in text.lower(), (jargon, text)


def test_the_notes_are_not_saved_in_the_history(client, model, sid, monkeypatch):
    stage(monkeypatch, prune=NEEDS, summarized=True)
    model.use([says("[[TIPO: conversacional]]Listo.")])

    ask(client, sid, "hola")

    session = main.get_session_store().get_or_create(sid)
    assert all("resumiendo" not in str(m.get("content", "")).lower() for m in session.messages)
    assert [m["role"] for m in session.messages] == ["system", "user", "assistant"]
