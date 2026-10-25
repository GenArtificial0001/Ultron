"""Controles de la conversacion en `main.py` (BL-006): limpiar, detener, reordenar la cola y eliminar una sesion.

Son las acciones que borran o frenan cosas del usuario, asi que lo que importa es que NO hagan de mas: limpiar no toca el plan ni el loop, detener apaga tambien el loop autonomo,
reordenar solo mueve lo que sigue en cola, y eliminar una sesion respeta a los agentes que dependen de ella (REQ-002/003/004, spec agentes-huerfanos-y-gestion-skills).
Sin ningun modelo: la tarea en curso se simula con un objeto que dice si termino. La base maestra es siempre la de `tmp_path` (fixture autouse de conftest).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import main
from app.persistence import master_index as mi
from app.session.events import UserTurn, get_session_store

SID = "sesion-controles"
OTHER = "sesion-reserva"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(main, "_assert_session_owner", lambda session_id: None)
    return TestClient(main.app)


@pytest.fixture(autouse=True)
def clean():
    yield
    for sid in (SID, OTHER):
        get_session_store().delete(sid)


class FakeTask:
    """Tarea en curso: `is_busy` solo mira `done()`. Registra si alguien la cancelo a la fuerza (el almacen lo hace al ELIMINAR una sesion, nunca al detener)."""

    def __init__(self):
        self.cancelled = False

    def done(self):
        return False

    def cancel(self):
        self.cancelled = True


def session(busy=False):
    s = get_session_store().get_or_create(SID)
    s.current_task = FakeTask() if busy else None  # is_busy depende de current_task
    return s


def queue_three(s):
    for n in ("a", "b", "c"):
        s.enqueue_turn(UserTurn(id=n, text=f"msg {n}"))
    return s


# ---------------------------------------------------------------------------
# POST /api/session/{id}/clear
# ---------------------------------------------------------------------------
class TestClear:
    def test_it_empties_messages_turns_and_queue_and_tells_the_ui(self, client):
        s = session()
        s.messages.extend([{"role": "user", "content": "hola"}, {"role": "assistant", "content": "chau"}])
        queue_three(s)
        inbox = s.subscribe()  # como una pestana abierta: tiene que enterarse de que se limpio

        r = client.post(f"/api/session/{SID}/clear")

        assert r.status_code == 200 and r.json() == {"cleared": True}
        assert s.messages == [] and s.turns == {} and s.queue_order == []
        assert inbox.get_nowait()["type"] == "conversation-cleared"

    def test_it_asks_sqlite_to_forget_the_history(self, client, monkeypatch):
        session()
        cleared = []
        monkeypatch.setattr(main.persistence_service, "clear_messages", lambda sid: cleared.append(sid))

        client.post(f"/api/session/{SID}/clear")

        assert cleared == [SID]

    def test_it_does_not_touch_the_plan_the_mode_or_the_loop(self, client):
        s = session()
        s.loop_active = True
        s.loop_turns_count = 4
        s.todos = [{"content": "paso 1", "status": "pending"}]

        client.post(f"/api/session/{SID}/clear")

        assert s.loop_active is True and s.loop_turns_count == 4
        assert s.todos == [{"content": "paso 1", "status": "pending"}]

    def test_a_busy_session_cannot_be_cleared_and_keeps_everything(self, client, monkeypatch):
        s = session(busy=True)
        s.messages.append({"role": "user", "content": "hola"})
        cleared = []
        monkeypatch.setattr(main.persistence_service, "clear_messages", lambda sid: cleared.append(sid))

        r = client.post(f"/api/session/{SID}/clear")

        assert r.status_code == 409 and "Detener" in r.json()["detail"]
        assert s.messages == [{"role": "user", "content": "hola"}] and cleared == []

    def test_a_session_that_is_not_open_says_to_reload(self, client):
        get_session_store().delete(SID)

        r = client.post(f"/api/session/{SID}/clear")

        assert r.status_code == 404 and "Recargá la página" in r.json()["detail"]


# ---------------------------------------------------------------------------
# POST /api/chat/{id}/stop
# ---------------------------------------------------------------------------
class TestStop:
    def test_without_an_active_turn_there_is_nothing_to_stop(self, client):
        session()

        assert client.post(f"/api/chat/{SID}/stop").json() == {"stopped": False, "reason": "no_active_turn"}

    def test_an_unknown_session_has_nothing_to_stop(self, client):
        get_session_store().delete(SID)

        assert client.post(f"/api/chat/{SID}/stop").json() == {"stopped": False, "reason": "no_active_turn"}

    def test_it_asks_the_running_turn_to_stop_instead_of_cancelling_it_by_force(self, client):
        s = session(busy=True)

        r = client.post(f"/api/chat/{SID}/stop")

        assert r.json() == {"stopped": True}
        assert s.stop_requested is True
        assert s.current_task is not None and s.current_task.cancelled is False  # NO se cancela a la fuerza: la herramienta en curso termina

    def test_a_real_stop_is_never_confused_with_an_edit(self, client):
        s = session(busy=True)
        s.edit_pending_text = "texto a medio editar"

        client.post(f"/api/chat/{SID}/stop")

        assert s.stop_requested is True and s.edit_pending_text is None

    def test_stopping_also_turns_off_the_autonomous_loop(self, client):
        """Si no, `_maybe_continue_loop` armaria el proximo turno apenas termine el actual e ignoraria que el usuario quiso frenar."""
        s = session(busy=True)
        s.loop_active = True
        s.loop_turns_count = 7
        s.loop_paused_for_question = True
        s.loop_auto = True

        client.post(f"/api/chat/{SID}/stop")

        assert (s.loop_active, s.loop_turns_count, s.loop_paused_for_question, s.loop_auto) == (False, 0, False, False)

    def test_a_stop_without_a_turn_does_not_touch_the_loop(self, client):
        s = session()
        s.loop_active = True

        client.post(f"/api/chat/{SID}/stop")

        assert s.loop_active is True and s.stop_requested is False

    # REQ-014 f (spec fix-detener-y-progreso-visible) + REQ-007b (spec modo-plan-loop), revision: Detener llega con el id del turno que el
    # usuario estaba viendo, que ya termino. Si lo que corre ahora es el siguiente mensaje que el USUARIO dejo en cola, no se lo frena; si
    # es la continuacion del loop autonomo, si: un Detener frena el turno y apaga el loop (antes de la rama ya era asi).
    def test_a_stop_aimed_at_a_finished_turn_does_not_stop_the_users_next_message(self, client):
        s = session(busy=True)
        s.active_turn_id = "mensaje-siguiente"

        r = client.post(f"/api/chat/{SID}/stop", json={"turn_id": "mensaje-anterior"})

        assert r.json() == {"stopped": False, "reason": "other_turn_active", "active_turn_id": "mensaje-siguiente"}
        assert s.stop_requested is False

    def test_a_stop_aimed_at_the_loop_turn_that_just_ended_stops_the_next_loop_turn_and_the_loop(self, client):
        s = session(busy=True)
        s.active_turn_id = "loop-siguiente"  # lo arma `_maybe_continue_loop` apenas termina el anterior
        s.loop_active = True
        s.loop_turns_count = 3
        s.loop_auto = True

        r = client.post(f"/api/chat/{SID}/stop", json={"turn_id": "loop-anterior"})

        assert r.json()["stopped"] is True, r.json()
        assert s.stop_requested is True, "el turno siguiente del loop se frena"
        assert (s.loop_active, s.loop_turns_count, s.loop_paused_for_question, s.loop_auto) == (False, 0, False, False)

    def test_a_stop_aimed_at_the_loop_turn_turns_off_the_loop_but_lets_the_users_queued_message_answer(self, client):
        """El usuario dejo un mensaje en cola mientras el loop trabajaba: al terminar el turno del loop arranca ESE mensaje (la cola humana
        va primero). Detener mirando el turno del loop no frena el mensaje del usuario (REQ-014 f), pero el loop no sigue despues."""
        s = session(busy=True)
        s.active_turn_id = "mensaje-del-usuario"
        s.loop_active = True
        s.loop_turns_count = 3
        s.loop_auto = True

        r = client.post(f"/api/chat/{SID}/stop", json={"turn_id": "loop-anterior"})

        body = r.json()
        assert body["stopped"] is False and body["reason"] == "other_turn_active", body
        assert s.stop_requested is False, "el mensaje del usuario sigue respondiendose"
        assert (s.loop_active, s.loop_turns_count, s.loop_paused_for_question, s.loop_auto) == (False, 0, False, False)


# ---------------------------------------------------------------------------
# POST /api/chat/{id}/queue/reorder
# ---------------------------------------------------------------------------
class TestReorderQueue:
    def reorder(self, client, message_id, position):
        return client.post(f"/api/chat/{SID}/queue/reorder", json={"message_id": message_id, "new_position": position})

    def test_it_moves_a_queued_message_and_returns_the_new_order(self, client):
        queue_three(session())

        r = self.reorder(client, "c", 0)

        assert r.status_code == 200 and r.json() == {"reordered": True, "queue_order": ["c", "a", "b"]}

    def test_the_new_order_is_kept_in_the_session(self, client):
        s = queue_three(session())

        self.reorder(client, "a", 2)

        assert s.queue_order == ["b", "c", "a"]

    @pytest.mark.parametrize(
        ("position", "expected"), [(-5, ["c", "a", "b"]), (99, ["a", "b", "c"]), (1, ["a", "c", "b"])]
    )
    def test_a_position_outside_the_queue_is_clamped_to_its_ends(self, client, position, expected):
        queue_three(session())

        r = self.reorder(client, "c", position)

        assert r.json()["queue_order"] == expected

    def test_a_message_not_in_the_queue_is_a_404_and_changes_nothing(self, client):
        s = queue_three(session())

        r = self.reorder(client, "ya-enviado", 0)

        assert r.status_code == 404 and "ya no está en la cola" in r.json()["detail"]
        assert s.queue_order == ["a", "b", "c"]

    def test_a_session_that_is_not_open_says_to_reload(self, client):
        get_session_store().delete(SID)

        r = self.reorder(client, "a", 0)

        assert r.status_code == 404 and "Recargá la página" in r.json()["detail"]


# ---------------------------------------------------------------------------
# DELETE /api/session/{id}
# ---------------------------------------------------------------------------
def make_agent(agent_id, session_id):
    mi.create_agent(
        agent_id,
        f"Agente {agent_id}",
        session_id,
        "skill",
        [],
        "x",
        "",
        "recurring",
        {"rule": "daily", "time": "08:30"},
        True,
    )


def set_fallback(session_id):
    from app.scheduler.agents import AGENT_FALLBACK_SESSION_KEY

    mi.set_app_setting(AGENT_FALLBACK_SESSION_KEY, session_id)


class TestDeleteSession:
    def test_a_session_without_agents_is_deleted_everywhere(self, client, monkeypatch):
        s = session()
        s.messages.append({"role": "user", "content": "hola"})
        calls = []
        monkeypatch.setattr(
            main.browser_workspace, "delete_browser_dir", lambda sid: calls.append(("browser", sid)) or True
        )
        monkeypatch.setattr(main.persistence_service, "delete_session", lambda sid: calls.append(("sqlite", sid)))

        r = client.delete(f"/api/session/{SID}")

        assert r.status_code == 200
        assert r.json() == {"deleted": True, "browser_deleted": True, "agents_reassigned": 0, "agents_orphaned": 0}
        assert get_session_store().get(SID) is None
        assert calls == [("browser", SID), ("sqlite", SID)]  # carpeta de vista previa y registro persistido

    def test_deleting_a_session_that_was_never_in_memory_still_cleans_the_rest(self, client, monkeypatch):
        get_session_store().delete(SID)
        calls = []
        monkeypatch.setattr(main.browser_workspace, "delete_browser_dir", lambda sid: False)
        monkeypatch.setattr(main.persistence_service, "delete_session", lambda sid: calls.append(sid))

        r = client.delete(f"/api/session/{SID}")

        assert r.json()["deleted"] is False and r.json()["browser_deleted"] is False and calls == [SID]

    def test_with_a_fallback_the_agents_are_reassigned_in_bulk_before_deleting(self, client, monkeypatch):
        session()
        get_session_store().get_or_create(OTHER)
        mi.upsert_session_index(OTHER, "Reserva") if hasattr(mi, "upsert_session_index") else None
        monkeypatch.setattr(mi, "session_index_exists", lambda sid: sid == OTHER)
        set_fallback(OTHER)
        make_agent("a1", SID)
        make_agent("a2", SID)
        monkeypatch.setattr(main.persistence_service, "delete_session", lambda sid: None)

        r = client.delete(f"/api/session/{SID}")

        assert r.json()["agents_reassigned"] == 2 and r.json()["agents_orphaned"] == 0
        assert mi.count_agents_by_session(SID) == 0 and mi.count_agents_by_session(OTHER) == 2

    def test_without_a_fallback_the_agents_are_left_orphaned_and_counted(self, client, monkeypatch):
        session()
        make_agent("a1", SID)
        monkeypatch.setattr(main.persistence_service, "delete_session", lambda sid: None)

        r = client.delete(f"/api/session/{SID}")

        assert r.json()["agents_reassigned"] == 0 and r.json()["agents_orphaned"] == 1
        assert mi.count_agents_by_session(SID) == 1  # siguen apuntando a la sesion vieja (se marcan 'broken' al listar)

    def test_with_no_fallback_configured_nothing_is_moved_even_if_the_index_says_an_empty_id_exists(self, client, monkeypatch):
        """Sin reserva configurada `get_fallback_session_id()` es '' (nunca None). Reasignar los agentes a '' los dejaria apuntando a una sesion que no existe,
        perdidos para siempre en lugar de 'huerfanos' visibles; el guard `and fallback_id` es lo unico que lo evita si el indice acepta ''."""
        session()
        make_agent("a1", SID)
        monkeypatch.setattr(mi, "session_index_exists", lambda sid: True)  # peor caso: el indice acepta cualquier id, incluido ''
        monkeypatch.setattr(main.persistence_service, "delete_session", lambda sid: None)

        r = client.delete(f"/api/session/{SID}")

        assert r.json()["agents_reassigned"] == 0 and r.json()["agents_orphaned"] == 1
        assert mi.count_agents_by_session("") == 0

    def test_a_fallback_that_no_longer_exists_does_not_receive_the_agents(self, client, monkeypatch):
        session()
        set_fallback("reserva-borrada")
        make_agent("a1", SID)
        monkeypatch.setattr(mi, "session_index_exists", lambda sid: False)
        monkeypatch.setattr(main.persistence_service, "delete_session", lambda sid: None)

        r = client.delete(f"/api/session/{SID}")

        assert r.json()["agents_reassigned"] == 0 and r.json()["agents_orphaned"] == 1
        assert mi.count_agents_by_session("reserva-borrada") == 0

    def test_the_fallback_session_itself_is_protected_while_agents_depend_on_it(self, client, monkeypatch):
        session()
        set_fallback(SID)
        make_agent("a1", SID)
        deleted = []
        monkeypatch.setattr(main.persistence_service, "delete_session", lambda sid: deleted.append(sid))

        r = client.delete(f"/api/session/{SID}")

        assert r.status_code == 409 and "sesión de reserva" in r.json()["detail"] and "1 agente" in r.json()["detail"]
        assert deleted == [] and get_session_store().get(SID) is not None  # y no se borro nada

    def test_the_fallback_session_with_no_agents_can_be_deleted(self, client, monkeypatch):
        """El bloqueo depende de agentes REALES hoy, no del rol de 'reserva' en abstracto."""
        session()
        set_fallback(SID)
        monkeypatch.setattr(main.persistence_service, "delete_session", lambda sid: None)

        assert client.delete(f"/api/session/{SID}").status_code == 200

    def test_agents_of_other_sessions_are_never_touched(self, client, monkeypatch):
        session()
        set_fallback(OTHER)
        monkeypatch.setattr(mi, "session_index_exists", lambda sid: sid == OTHER)
        make_agent("mio", SID)
        make_agent("ajeno", "otra-sesion-cualquiera")
        monkeypatch.setattr(main.persistence_service, "delete_session", lambda sid: None)

        client.delete(f"/api/session/{SID}")

        assert mi.count_agents_by_session("otra-sesion-cualquiera") == 1 and mi.count_agents_by_session(OTHER) == 1
