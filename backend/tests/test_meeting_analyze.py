"""Endpoint del Modo reunion (`POST /api/meeting/analyze`, spec narracion-agente-efimera y asistente-contextual-voz, BL-006).

Recibe fragmentos de una transcripcion en vivo y decide si Vision opina. `test_meeting_gate.py` ya cubre el gate heuristico solo; esto cubre el ENDPOINT que lo usa: que no invoque al
modelo cuando no corresponde (cada llamada al modelo cuesta), que no pise un turno de chat en curso, que arme el contexto completo cuando si responde y que un error del modelo llegue
a la persona en lugar de perderse como un "no hubo opinion".

`run_turn` se reemplaza por un generador falso que registra el mensaje que recibe: nunca hay modelo, red ni login del proveedor de IA (ADR-015).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import main
from app.meeting_gate import MeetingGateState
from app.session.events import get_session_store

SID = "sesion-reunion"


class FakeTask:
    def done(self):
        return False

    def cancel(self):
        pass


@pytest.fixture
def turns(monkeypatch):
    """Reemplaza `run_turn`. `turns.events` = lo que emite; `turns.calls` = (session_id, mensaje) de cada invocacion."""
    state = type("T", (), {})()
    state.calls = []
    state.events = [{"type": "text-delta", "data": {"text": "Mi opinion."}}, {"type": "done", "data": {}}]
    state.raises = None

    async def fake_run_turn(session, user_message, session_id):
        state.calls.append((session_id, user_message))
        if state.raises:
            raise state.raises
        for e in state.events:
            yield e

    monkeypatch.setattr(main, "run_turn", fake_run_turn)
    return state


@pytest.fixture
def client(monkeypatch):
    main._MEETING_GATE_STATES.pop(SID, None)
    yield TestClient(main.app, raise_server_exceptions=False)
    main._MEETING_GATE_STATES.pop(SID, None)
    get_session_store().delete(SID)


def analyze(client, fragment, full="", force=False):
    return client.post(
        "/api/meeting/analyze", json={"session_id": SID, "fragment": fragment, "full_transcript": full, "force": force}
    )


# ---------------------------------------------------------------------------
# El gate: sin mencion no se gasta una llamada al modelo
# ---------------------------------------------------------------------------
class TestGate:
    def test_without_a_mention_there_is_no_opinion_and_no_model_call(self, client, turns):
        r = analyze(client, "hablemos del presupuesto")

        assert r.status_code == 200 and r.json() == {"opinion": None}
        assert turns.calls == []

    def test_with_a_mention_the_model_answers(self, client, turns):
        r = analyze(client, "ULTRON, que opinas del stock")

        assert r.json() == {"opinion": "Mi opinion."}
        assert len(turns.calls) == 1

    def test_a_second_mention_right_after_is_throttled(self, client, turns):
        analyze(client, "ULTRON uno")

        r = analyze(client, "ULTRON dos")

        assert r.json() == {"opinion": None} and len(turns.calls) == 1  # una rafaga no dispara dos respuestas

    def test_the_gate_state_is_per_session(self, client, turns):
        analyze(client, "ULTRON uno")
        main._MEETING_GATE_STATES.pop(SID, None)  # otra reunion / otra sesion: arranca sin throttle

        assert analyze(client, "ULTRON dos").json()["opinion"] == "Mi opinion."


# ---------------------------------------------------------------------------
# force: el click manual en el ojo
# ---------------------------------------------------------------------------
class TestForce:
    def test_a_manual_click_answers_even_without_a_mention(self, client, turns):
        r = analyze(client, "hablemos del presupuesto", force=True)

        assert r.json() == {"opinion": "Mi opinion."}

    def test_a_manual_click_skips_the_throttle(self, client, turns):
        analyze(client, "ULTRON uno")  # arma el throttle

        assert analyze(client, "algo", force=True).json()["opinion"] == "Mi opinion."  # el click manual lo salta

    def test_a_manual_click_arms_the_throttle_for_the_next_spoken_mention(self, client, turns):
        """Es lo UNICO que arma el throttle en este escenario (no hubo ninguna mencion hablada antes): sin eso, decir 'Vision' justo despues del click
        dispararia una segunda respuesta en rafaga."""
        analyze(client, "algo sin mencion", force=True)

        assert analyze(client, "ULTRON tres").json() == {"opinion": None}
        assert len(turns.calls) == 1

    def test_a_manual_click_with_nothing_transcribed_answers_cheaply_without_the_model(self, client, turns):
        r = analyze(client, "   ", full="  ", force=True)

        assert r.json() == {"opinion": "Todavía no escuché nada para opinar."}
        assert turns.calls == []

    def test_a_manual_click_with_only_the_full_transcript_does_call_the_model(self, client, turns):
        r = analyze(client, "", full="Juan dijo que el presupuesto sube", force=True)

        assert r.json()["opinion"] == "Mi opinion." and len(turns.calls) == 1


# ---------------------------------------------------------------------------
# Un turno de chat en curso no se pisa
# ---------------------------------------------------------------------------
class TestBusySession:
    def test_it_does_not_run_over_a_text_turn_that_is_in_progress(self, client, turns):
        get_session_store().get_or_create(SID).current_task = FakeTask()

        r = analyze(client, "ULTRON, opina")

        assert r.json() == {"opinion": None} and turns.calls == []  # run_turn no es reentrante sobre el mismo historial

    def test_a_dropped_invocation_still_consumed_the_throttle_slot(self, client, turns):
        """Descartar por sesion ocupada NO deja pasar una segunda mencion en rafaga apenas se libere: el slot del throttle ya se uso."""
        session = get_session_store().get_or_create(SID)
        session.current_task = FakeTask()
        analyze(client, "ULTRON uno")
        session.current_task = None

        assert analyze(client, "ULTRON dos").json() == {"opinion": None}


# ---------------------------------------------------------------------------
# El mensaje que le llega al modelo
# ---------------------------------------------------------------------------
class TestWhatTheModelIsAsked:
    def test_with_a_transcript_it_gets_all_of_it_and_the_last_phrase_highlighted(self, client, turns):
        analyze(client, "ULTRON, que dijo Juan", full="Juan dijo que el presupuesto sube un diez por ciento")

        _, message = turns.calls[0]
        assert "Transcripción completa de audio en vivo" in message
        assert "Juan dijo que el presupuesto sube un diez por ciento" in message
        assert message.rstrip().endswith("ULTRON, que dijo Juan")  # la ultima frase va AL FINAL, resaltada
        assert message.index("presupuesto") < message.index("Última frase transcrita")

    def test_without_a_transcript_only_the_fragment_is_sent(self, client, turns):
        analyze(client, "ULTRON, hola")

        _, message = turns.calls[0]
        assert "[Transcripción de audio en vivo, Modo reunión] ULTRON, hola" in message
        assert "Transcripción completa" not in message

    @pytest.mark.parametrize(
        "needle",
        [
            "VOZ ALTA",  # brevedad: se escucha, no se lee
            "reconocimiento de voz automático",  # protocolo para transcripcion confusa
            "nunca digas que no podés acceder",  # aclaracion de acceso al chat (bug real: el modelo negaba tener el historial)
            "Modo Cerebro",  # alcance del rol: operar sobre archivos se deriva al Modo Cerebro
        ],
    )
    def test_the_four_style_and_scope_instructions_are_always_included(self, client, turns, needle):
        analyze(client, "ULTRON, opina", full="algo de contexto")

        assert needle in turns.calls[0][1]

    @pytest.mark.parametrize("needle", ["VOZ ALTA", "reconocimiento de voz automático", "nunca digas que no podés acceder", "Modo Cerebro"])
    def test_the_same_four_instructions_are_included_when_there_is_no_transcript_yet(self, client, turns, needle):
        """El primer fragmento de una reunion llega SIN transcripcion acumulada: usa otro armado del mensaje y tambien tiene que llevar las cuatro instrucciones."""
        analyze(client, "ULTRON, hola", full="")

        assert needle in turns.calls[0][1]

    def test_the_instructions_come_before_the_transcript(self, client, turns):
        analyze(client, "ULTRON, opina", full="ESTO ES LA TRANSCRIPCION")

        message = turns.calls[0][1]
        assert message.index("Instrucción de estilo") < message.index("ESTO ES LA TRANSCRIPCION")

    def test_the_session_id_reaches_the_turn(self, client, turns):
        analyze(client, "ULTRON, hola")

        assert turns.calls[0][0] == SID


# ---------------------------------------------------------------------------
# Lo que devuelve y los errores
# ---------------------------------------------------------------------------
class TestResult:
    def test_the_text_chunks_are_joined_and_trimmed(self, client, turns):
        turns.events = [
            {"type": "text-delta", "data": {"text": "  Hola, "}},
            {"type": "tool-call", "data": {}},
            {"type": "text-delta", "data": {"text": "mundo.  "}},
        ]

        assert analyze(client, "ULTRON").json() == {"opinion": "Hola, mundo."}

    def test_an_empty_answer_is_none_not_an_empty_string(self, client, turns):
        turns.events = [{"type": "text-delta", "data": {"text": "   "}}, {"type": "done", "data": {}}]

        assert analyze(client, "ULTRON").json() == {"opinion": None}

    def test_a_model_error_event_reaches_the_user_instead_of_looking_like_no_opinion(self, client, turns):
        """Bug real: un 429 por limite de gasto se tragaba en silencio y era indistinguible de 'el gate no aprobo'."""
        turns.events = [{"type": "error", "data": {"message": "El modelo alcanzó su límite de gasto."}}]

        r = analyze(client, "ULTRON, opina")

        assert r.status_code == 502
        assert "No pude responderte en la reunión" in r.json()["detail"] and "límite de gasto" in r.json()["detail"]

    def test_an_error_event_without_a_message_still_gives_a_readable_error(self, client, turns):
        turns.events = [{"type": "error", "data": {}}]

        r = analyze(client, "ULTRON")

        assert r.status_code == 502 and "Error desconocido del LLM" in r.json()["detail"]

    def test_a_crash_inside_the_turn_gives_a_plain_message_and_keeps_the_meeting_listening(self, client, turns):
        turns.raises = RuntimeError("boom interno con detalle tecnico")

        r = analyze(client, "ULTRON")

        assert r.status_code == 502
        assert "La reunión sigue escuchando" in r.json()["detail"]
        assert "boom interno" not in r.json()["detail"]  # el detalle tecnico va al log, no a la persona

    def test_an_error_after_some_text_is_still_an_error_not_a_half_opinion(self, client, turns):
        turns.events = [
            {"type": "text-delta", "data": {"text": "Empiezo a opinar"}},
            {"type": "error", "data": {"message": "se cortó"}},
        ]

        assert analyze(client, "ULTRON").status_code == 502


def test_the_gate_state_type_is_what_the_endpoint_keeps_per_session(client, turns):
    analyze(client, "ULTRON")

    assert isinstance(main._MEETING_GATE_STATES[SID], MeetingGateState)
