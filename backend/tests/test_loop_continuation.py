"""Decision del loop autonomo (`_maybe_continue_loop` y `collect_pending_todos` en `main.py`, spec modo-plan-loop, BL-006).

Cuando un turno termina y no hay nada en la cola humana, esta funcion decide si Vision se arma SOLO el proximo turno. Un error aca gasta tokens sin que nadie lo haya pedido
(un loop que no se apaga) o abandona un plan a medias sin avisar. Las cinco salidas posibles: ya esta pausado esperando a una persona, no queda nada pendiente (exito, sin
alarma), llego al tope de 30 turnos (guardarail, CON alarma) y la normal (sintetiza el turno). `_launch_turn` se reemplaza: nunca hay modelo ni red.
"""

from __future__ import annotations

import pytest

from app import main
from app.session.events import SessionState

SID = "sesion-loop"


@pytest.fixture
def launched(monkeypatch):
    turns = []
    monkeypatch.setattr(main, "_launch_turn", lambda session, session_id, turn: turns.append((session_id, turn)))
    return turns


def make_session(todos=None, **state):
    s = SessionState(session_id=SID)
    s.todos = todos if todos is not None else [{"content": "paso 1", "status": "pending"}]
    s.loop_active = True
    for k, v in state.items():
        setattr(s, k, v)
    return s


def events(s):
    """Los eventos que se le mandaron a la pantalla, via una suscripcion SSE."""
    q = s.subscribe()
    return q


def drain(q):
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


# ---------------------------------------------------------------------------
# collect_pending_todos
# ---------------------------------------------------------------------------
class TestCollectPendingTodos:
    def test_only_pending_top_level_tasks_count(self):
        s = make_session(
            [
                {"content": "a", "status": "pending"},
                {"content": "b", "status": "in_progress"},
                {"content": "c", "status": "completed"},
                {"content": "d", "status": "cancelled"},
            ]
        )

        assert [t["content"] for t in main.collect_pending_todos(s)] == ["a"]

    def test_pending_subtasks_of_a_task_already_in_progress_still_count(self):
        """El modelo suele marcar la tarea principal 'in_progress' y dejar sus pasos 'pending': el loop tiene que seguir mientras haya CUALQUIER paso pendiente."""
        s = make_session(
            [
                {
                    "content": "principal",
                    "status": "in_progress",
                    "subtasks": [
                        {"content": "paso 1", "status": "completed"},
                        {"content": "paso 2", "status": "pending"},
                    ],
                }
            ]
        )

        assert [t["content"] for t in main.collect_pending_todos(s)] == ["paso 2"]

    def test_a_pending_task_and_its_pending_subtasks_are_all_listed(self):
        s = make_session(
            [
                {
                    "content": "t",
                    "status": "pending",
                    "subtasks": [{"content": "s1", "status": "pending"}, {"content": "s2", "status": "pending"}],
                }
            ]
        )

        assert [t["content"] for t in main.collect_pending_todos(s)] == ["t", "s1", "s2"]

    def test_no_todos_and_none_subtasks_are_fine(self):
        assert main.collect_pending_todos(make_session([])) == []
        assert (
            main.collect_pending_todos(make_session([{"content": "x", "status": "completed", "subtasks": None}])) == []
        )

    def test_an_item_without_status_is_not_pending(self):
        assert main.collect_pending_todos(make_session([{"content": "sin estado"}])) == []


# ---------------------------------------------------------------------------
# _maybe_continue_loop
# ---------------------------------------------------------------------------
class TestNormalContinuation:
    def test_it_synthesizes_the_next_turn_and_counts_it(self, launched):
        s = make_session(loop_turns_count=3)

        main._maybe_continue_loop(s, SID)

        assert s.loop_turns_count == 4 and s.loop_active is True
        [(session_id, turn)] = launched
        assert session_id == SID and turn.id.startswith("loop-")
        assert "todowrite" in turn.text and "pending" in turn.text and "question" in turn.text

    def test_each_synthetic_turn_has_its_own_id(self, launched):
        s = make_session()

        main._maybe_continue_loop(s, SID)
        main._maybe_continue_loop(s, SID)

        assert launched[0][1].id != launched[1][1].id

    def test_the_synthetic_turn_tells_the_model_to_recommend_instead_of_asking_when_there_is_a_clear_option(
        self, launched
    ):
        main._maybe_continue_loop(make_session(), SID)

        assert "recomendada" in launched[0][1].text

    def test_it_does_not_emit_a_stop_event_while_continuing(self, launched):
        s = make_session()
        q = events(s)

        main._maybe_continue_loop(s, SID)

        assert drain(q) == []

    def test_a_pending_subtask_alone_keeps_the_loop_going(self, launched):
        s = make_session(
            [{"content": "principal", "status": "in_progress", "subtasks": [{"content": "paso", "status": "pending"}]}]
        )

        main._maybe_continue_loop(s, SID)

        assert len(launched) == 1


class TestWhenThereIsNothingLeft:
    def test_the_loop_turns_itself_off_without_an_alarm_and_says_it_completed(self, launched):
        s = make_session([{"content": "listo", "status": "completed"}], loop_turns_count=5, loop_auto=True)
        q = events(s)

        main._maybe_continue_loop(s, SID)

        assert launched == []
        assert (s.loop_active, s.loop_turns_count, s.loop_auto) == (False, 0, False)
        assert (
            s.loop_paused_for_question is False
        )  # el exito NO deja el loop 'pausado': no hay alarma ni nada que responder
        assert [(e["type"], e["data"]) for e in drain(q)] == [("loop-stopped", {"reason": "completed"})]

    def test_an_empty_plan_also_finishes(self, launched):
        s = make_session([])

        main._maybe_continue_loop(s, SID)

        assert launched == [] and s.loop_active is False


class TestSafetyCap:
    def test_the_cap_is_thirty_turns(self):
        assert main.LOOP_MAX_TURNS == 30

    def test_at_the_cap_it_stops_and_raises_the_alarm_instead_of_continuing(self, launched):
        s = make_session(loop_turns_count=main.LOOP_MAX_TURNS, loop_auto=True)
        q = events(s)

        main._maybe_continue_loop(s, SID)

        assert launched == []
        assert (s.loop_active, s.loop_turns_count, s.loop_auto) == (False, 0, False)
        assert (
            s.loop_paused_for_question is True
        )  # a diferencia del exito: el plan NO termino solo, amerita revision humana
        assert [(e["type"], e["data"]) for e in drain(q)] == [("loop-stopped", {"reason": "max_turns"})]

    def test_one_turn_below_the_cap_still_runs(self, launched):
        s = make_session(loop_turns_count=main.LOOP_MAX_TURNS - 1)

        main._maybe_continue_loop(s, SID)

        assert len(launched) == 1 and s.loop_turns_count == main.LOOP_MAX_TURNS and s.loop_active is True

    def test_above_the_cap_it_also_stops(self, launched):
        s = make_session(loop_turns_count=main.LOOP_MAX_TURNS + 5)

        main._maybe_continue_loop(s, SID)

        assert launched == [] and s.loop_active is False

    def test_a_loop_that_never_finishes_stops_after_exactly_thirty_synthetic_turns(self, launched):
        """Simula 100 llamadas seguidas con un plan que nunca avanza: el tope corta en 30, no en 31 ni en 100."""
        s = make_session()
        q = events(s)

        for _ in range(100):
            main._maybe_continue_loop(s, SID)

        assert len(launched) == main.LOOP_MAX_TURNS
        reasons = [e["data"]["reason"] for e in drain(q) if e["type"] == "loop-stopped"]
        assert reasons == ["max_turns"]  # una sola alarma, no una por llamada


class TestPausedForAQuestion:
    def test_while_waiting_for_a_human_answer_nothing_is_synthesized_or_changed(self, launched):
        s = make_session(loop_paused_for_question=True, loop_turns_count=9, loop_auto=True)
        q = events(s)

        main._maybe_continue_loop(s, SID)

        assert launched == [] and drain(q) == []
        assert (s.loop_active, s.loop_turns_count, s.loop_auto) == (True, 9, True)

    def test_the_pause_wins_even_if_the_cap_was_reached_or_nothing_is_pending(self, launched):
        s = make_session([], loop_paused_for_question=True, loop_turns_count=main.LOOP_MAX_TURNS)
        q = events(s)

        main._maybe_continue_loop(s, SID)

        assert launched == [] and drain(q) == [] and s.loop_active is True
