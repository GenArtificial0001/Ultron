"""Endpoints del loop autonomo y del modo de la sesion (`POST /api/session/{id}/loop` y `POST /api/session/{id}/mode`, spec modo-plan-loop, BL-006).

`test_loop_continuation.py` ya cubre la DECISION de seguir o parar. Esto cubre la puerta de entrada: que /loop no arranque sin tareas pendientes (gastaria tokens sin nada que hacer), que
reinicie el estado del loop al empezar, que NO lance un turno nuevo encima de uno en curso (lo toma `_maybe_continue_loop` cuando ese termine), y que el modo (plan/activo) se valide en el
SERVIDOR: ese valor decide si `write`/`edit`/`bash` se le ofrecen al modelo, asi que un valor raro no puede colarse.

`_launch_turn` se reemplaza: nunca hay modelo ni red (ADR-015).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import main
from app.session.events import get_session_store

SID = "sesion-loop-api"
OTHERS = "sesion-ajena"


class FakeTask:
    def done(self):
        return False

    def cancel(self):
        pass


@pytest.fixture
def launched(monkeypatch):
    turns = []
    monkeypatch.setattr(main, "_launch_turn", lambda session, session_id, turn: turns.append((session_id, turn)))
    return turns


@pytest.fixture
def owner_checks(monkeypatch):
    seen = []

    def check(session_id):
        seen.append(session_id)
        if session_id == OTHERS:
            raise main.HTTPException(status_code=403, detail="Esta sesión es de otro usuario.")

    monkeypatch.setattr(main, "_assert_session_owner", check)
    return seen


@pytest.fixture
def client(owner_checks):
    yield TestClient(main.app, raise_server_exceptions=False)
    for sid in (SID, OTHERS):
        get_session_store().delete(sid)


def session(todos=None, busy=False):
    s = get_session_store().get_or_create(SID)
    s.todos = todos if todos is not None else [{"content": "paso 1", "status": "pending"}]
    s.current_task = FakeTask() if busy else None
    return s


def start(client, body=None, session_id=SID):
    return (
        client.post(f"/api/session/{session_id}/loop", json=body)
        if body is not None
        else client.post(f"/api/session/{session_id}/loop")
    )


# ---------------------------------------------------------------------------
# POST /loop
# ---------------------------------------------------------------------------
class TestStartLoop:
    def test_it_starts_the_loop_and_launches_the_first_turn_right_away(self, client, launched):
        s = session([{"content": "a", "status": "pending"}, {"content": "b", "status": "pending"}])

        r = start(client)

        assert r.status_code == 200 and r.json() == {"loop_started": True, "pending_count": 2}
        assert s.loop_active is True and len(launched) == 1
        assert launched[0][0] == SID and launched[0][1].id.startswith("loop-")

    def test_without_pending_tasks_it_refuses_and_changes_nothing(self, client, launched):
        s = session([{"content": "listo", "status": "completed"}])

        r = start(client)

        assert r.status_code == 400 and "No hay tareas pendientes" in r.json()["detail"]
        assert s.loop_active is False and launched == []

    def test_with_no_plan_at_all_it_refuses(self, client, launched):
        session([])

        assert start(client).status_code == 400 and launched == []

    def test_a_pending_subtask_is_enough_to_start(self, client, launched):
        session(
            [{"content": "principal", "status": "in_progress", "subtasks": [{"content": "paso", "status": "pending"}]}]
        )

        r = start(client)

        assert r.status_code == 200 and r.json()["pending_count"] == 1

    def test_it_resets_the_loop_state_left_over_from_a_previous_run(self, client, launched):
        s = session()
        s.loop_turns_count = 17
        s.loop_paused_for_question = True

        start(client)

        # arranca de cero: no hereda el contador ni la pausa de la corrida anterior (si no, el tope de 30 llegaria antes de tiempo)
        assert s.loop_paused_for_question is False
        assert s.loop_turns_count == 1  # 0 al reiniciar + 1 por el primer turno sintetizado

    def test_a_turn_already_running_is_never_run_over(self, client, launched):
        """El loop solo se activa; `_maybe_continue_loop` toma la posta cuando ese turno termine."""
        s = session(busy=True)

        r = start(client)

        assert r.status_code == 200 and s.loop_active is True
        assert launched == []

    def test_auto_mode_is_off_by_default_and_on_when_asked(self, client, launched):
        s = session()
        start(client)
        assert s.loop_auto is False

        s2 = session()
        start(client, {"auto": True})
        assert s2.loop_auto is True

    def test_an_empty_body_is_accepted(self, client, launched):
        session()

        assert start(client, {}).status_code == 200

    def test_it_asks_for_authorization_on_the_session(self, client, launched, owner_checks):
        session()
        start(client)

        assert owner_checks == [SID]

    def test_a_foreign_session_cannot_start_a_loop(self, client, launched):
        get_session_store().get_or_create(OTHERS).todos = [{"content": "x", "status": "pending"}]

        r = start(client, session_id=OTHERS)

        assert r.status_code == 403 and launched == []
        assert get_session_store().get(OTHERS).loop_active is False


# ---------------------------------------------------------------------------
# POST /mode
# ---------------------------------------------------------------------------
class TestSessionMode:
    def set_mode(self, client, mode, session_id=SID):
        return client.post(f"/api/session/{session_id}/mode", json={"mode": mode})

    @pytest.mark.parametrize("mode", ["plan", "active"])
    def test_the_two_valid_modes_are_saved_and_returned(self, client, mode):
        r = self.set_mode(client, mode)

        assert r.status_code == 200 and r.json() == {"mode": mode}
        assert get_session_store().get(SID).mode == mode

    @pytest.mark.parametrize("mode", ["", "PLAN", "Active", "admin", "plan ", "write"])
    def test_any_other_value_is_refused_and_the_mode_does_not_change(self, client, mode):
        """El valor decide si write/edit/bash se filtran del payload hacia el modelo: se valida en el servidor, nunca se confia en el frontend."""
        session()
        get_session_store().get(SID).mode = "plan"

        r = self.set_mode(client, mode)

        assert r.status_code == 400 and "'plan' o 'active'" in r.json()["detail"]
        assert get_session_store().get(SID).mode == "plan"

    def test_changing_the_mode_keeps_the_conversation_and_the_plan(self, client):
        s = session([{"content": "paso", "status": "pending"}])
        s.messages.append({"role": "user", "content": "hola"})

        self.set_mode(client, "active")

        assert s.messages == [{"role": "user", "content": "hola"}] and s.todos == [
            {"content": "paso", "status": "pending"}
        ]

    def test_it_creates_the_session_if_it_was_not_in_memory(self, client):
        get_session_store().delete(SID)

        r = self.set_mode(client, "active")

        assert r.status_code == 200 and get_session_store().get(SID) is not None

    def test_it_asks_for_authorization_and_a_foreign_session_cannot_change_the_mode(self, client, owner_checks):
        ok = self.set_mode(client, "active")
        denied = self.set_mode(client, "active", session_id=OTHERS)

        assert ok.status_code == 200 and denied.status_code == 403
        assert owner_checks == [SID, OTHERS]
        assert get_session_store().get(OTHERS) is None  # y no se creo la sesion ajena
