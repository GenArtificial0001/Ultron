"""Unit de la tool `question` (spec modo-plan-loop REQ-008/009/011): opciones
malformadas, opcion "(Recomendada)", pausa por "(Riesgo)" y auto-seleccion del
loop autonomo. Regresion clave: un string en `options` NO se itera por caracter
(bug real del "teclado gigante"). REQ-018 (spec fix-detener-y-progreso-visible):
que pregunta queda sin contestar para que F5 la vuelva a mostrar."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.tools import question as q


class FakeSession:
    def __init__(self, loop_active=False):
        self.loop_active = loop_active
        self.loop_paused_for_question = False
        self.events: list[tuple[str, dict]] = []

    def push_event(self, kind, payload):
        self.events.append((kind, payload))


def kinds(session):
    return [k for k, _ in session.events]


# ---- helpers puros ---------------------------------------------------------
def test_normalize_option_accepts_str_dict_and_other():
    assert q._normalize_option("Si") == {"label": "Si", "description": ""}
    assert q._normalize_option({"label": "A", "description": "d"}) == {"label": "A", "description": "d"}
    assert q._normalize_option({"x": 1})["label"] == "{'x': 1}"  # dict sin label
    assert q._normalize_option(7) == {"label": "7", "description": ""}


@pytest.mark.parametrize(
    ("labels", "expected"),
    [
        (["A", "B"], None),
        (["A (Recomendada)", "B"], 0),
        (["A", "  b (RECOMENDADA)  "], 1),
        (["(Recomendada) A"], None),  # el sufijo debe ir al FINAL
        ([], None),
    ],
)
def test_find_recommended_index(labels, expected):
    assert q._find_recommended_index([{"label": x} for x in labels]) == expected


def test_malformed_string_options_are_repaired():
    raw = '<option label="Uno" description="primera"> <option label="Dos"/>'
    assert q._parse_options_from_malformed_string(raw) == [
        {"label": "Uno", "description": "primera"},
        {"label": "Dos", "description": ""},
    ]


def test_unrecognized_string_options_become_empty_not_chars():
    assert q._parse_options_from_malformed_string("una opcion cualquiera") == []


# ---- ejecucion -------------------------------------------------------------
def test_missing_question():
    assert q._question_execute({}).startswith("Error: falta")


def test_without_session_returns_text_only():
    out = q._question_execute({"question": "¿Seguimos?", "options": ["Si", "No"]})
    assert "Si, No" in out and "Esperando" in out


def test_free_answer_when_no_options():
    assert "(respuesta libre)" in q._question_execute({"question": "¿Nombre?"})
    assert "(respuesta libre)" in q._question_execute({"question": "¿Nombre?", "options": None})


def test_string_options_do_not_explode_into_characters():
    s = FakeSession()
    q._question_execute({"question": "x", "options": "texto suelto"}, s)
    assert s.events[0][1]["options"] == []  # jamas 12 botones de 1 letra


def test_normal_mode_emits_only_question_event():
    s = FakeSession()
    q._question_execute({"question": "?", "options": [{"label": "A (Recomendada)"}, "B"]}, s)
    assert kinds(s) == ["question"]
    payload = s.events[0][1]
    assert payload["recommended_index"] == 0 and payload["auto_answered"] is None


def test_loop_auto_selects_recommended_without_pausing():
    s = FakeSession(loop_active=True)
    out = q._question_execute({"question": "?", "options": ["A (Recomendada)", "B"]}, s)
    assert s.loop_paused_for_question is False and kinds(s) == ["question"]
    assert s.events[0][1]["auto_answered"] == "A (Recomendada)"
    assert "auto-selecciono" in out


def test_loop_pauses_when_no_recommended_option():
    s = FakeSession(loop_active=True)
    out = q._question_execute({"question": "?", "options": ["A", "B"]}, s)
    assert s.loop_paused_for_question is True and kinds(s) == ["loop-paused", "question"]
    assert "Esperando" in out


def test_loop_pauses_on_risk_even_if_recommended_exists():
    s = FakeSession(loop_active=True)
    q._question_execute({"question": "?", "options": ["A (Recomendada)", "Borrar todo (Riesgo)"]}, s)
    assert s.loop_paused_for_question is True and s.events[1][1]["auto_answered"] is None


def test_loop_inactive_ignores_risk_marker():
    s = FakeSession(loop_active=False)
    q._question_execute({"question": "?", "options": ["A (Riesgo)"]}, s)
    assert s.loop_paused_for_question is False and kinds(s) == ["question"]


def test_session_without_loop_attribute_is_tolerated():
    s = SimpleNamespace(push_event=lambda *a: None)
    assert "Esperando" in q._question_execute({"question": "?", "options": ["A"]}, s)


def test_tool_metadata():
    assert q.question_tool.thread_safe is False and not q.question_tool.dangerous


# ---- REQ-018 (spec fix-detener-y-progreso-visible): F5 vuelve a mostrar la pregunta sin contestar ------------------------------
def test_pending_question_view_only_for_a_question_that_waits_for_the_user():
    args = {"question": "¿Sigo?", "options": [{"label": "Sí (Recomendada)"}, "No"]}
    part = {"name": "question", "arguments": json.dumps(args)}
    waiting = q._question_execute(args, FakeSession())
    auto = q._question_execute(args, FakeSession(loop_active=True))
    assert q.pending_question_view({**part, "result": waiting}) == {
        "question": "¿Sigo?",
        "options": [{"label": "Sí (Recomendada)", "description": ""}, {"label": "No", "description": ""}],
    }
    assert q.pending_question_view({**part, "result": auto}) is None  # el loop ya eligio solo: no espera a nadie
    assert q.pending_question_view({**part, "result": q._question_execute({})}) is None  # error: no se mostro nada
    assert q.pending_question_view({**part}) is None  # sin resultado: la tool no termino


def test_pending_question_view_tolerates_broken_arguments():
    ok = q._question_execute({"question": "x"})
    assert q.pending_question_view({"arguments": "{roto", "result": ok}) is None
    assert q.pending_question_view({"arguments": json.dumps(["no", "es", "objeto"]), "result": ok}) is None
    assert q.pending_question_view({"arguments": json.dumps({"options": []}), "result": ok}) is None  # sin pregunta
    malformed = json.dumps({"question": "¿Cuál?", "options": '<option label="Uno"> <option label="Dos"/>'})
    assert [o["label"] for o in q.pending_question_view({"arguments": malformed, "result": ok})["options"]] == ["Uno", "Dos"]
    odd = json.dumps({"question": "¿Y?", "options": {"a": 1}})
    assert q.pending_question_view({"arguments": odd, "result": ok})["options"] == []  # nunca se itera otra cosa que una lista


def test_the_page_gets_the_pending_question_unless_its_answer_already_waits_in_the_queue(monkeypatch):
    from app import main
    from app.persistence import service as svc
    from app.session.events import UserTurn, get_session_store

    sid = "s-req018-pendiente"
    args = {"question": "¿Sigo?", "options": ["Sí", "No"]}
    part = {"name": "question", "call_id": "q1", "arguments": json.dumps(args), "result": q._question_execute(args)}
    monkeypatch.setattr(svc, "load_pending_question", lambda session_id: part if session_id == sid else None)
    assert main._pending_question_for_page(sid)["question"] == "¿Sigo?"
    assert main._pending_question_for_page("otra-sesion") is None

    session = get_session_store().get_or_create(sid)
    session.enqueue_turn(UserTurn(id="t-resp", text="Sí"))  # el usuario ya toco una opcion y espera en la cola
    try:
        assert main._pending_question_for_page(sid) is None, "mostrarla de nuevo invitaria a contestar dos veces"
    finally:
        session.remove_queued_turn("t-resp")
