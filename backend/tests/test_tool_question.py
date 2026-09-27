"""Unit de la tool `question` (spec modo-plan-loop REQ-008/009/011): opciones
malformadas, opcion "(Recomendada)", pausa por "(Riesgo)" y auto-seleccion del
loop autonomo. Regresion clave: un string en `options` NO se itera por caracter
(bug real del "teclado gigante")."""

from __future__ import annotations

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
