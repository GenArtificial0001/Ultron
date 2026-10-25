"""spec persistencia-sqlite-sesiones REQ-003/REQ-004: una sesion guardada en SQLite se recupera tras reiniciar Vision.

REQ-003 dice "cualquier request para esa sesion": antes, `GET /api/todos`, `POST .../clear`, `GET /api/context` y
`POST /api/commands/compact` usaban `store.get()` y, despues de un reinicio, devolvian el panel de tareas vacio, un 404
("Recarga la pagina") o 0 tokens aunque la conversacion estuviera en disco. Sesion y SQLite reales en `tmp_path`
(fixture `tmp_master_db`); el "reinicio" se simula sacando la sesion del almacen en memoria, igual que
`test_edit_after_restart.py`. El dueño de la sesion es el usuario de Windows actual: el chequeo BOLA real corre."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import main
from app.persistence import db, master_index
from app.persistence import service as persistence
from app.session.events import SessionStore, get_session_store

SID = "sesion-recuperada"
UNKNOWN = "sesion-que-no-existe-en-ningun-lado"
TODOS = [
    {"content": "Revisar el stock", "status": "in_progress", "priority": "high"},
    {"content": "Armar el informe", "status": "pending", "priority": "medium"},
]


@pytest.fixture
def client():
    return TestClient(main.app)


@pytest.fixture
def saved(tmp_path, tmp_master_db) -> Path:
    """Conversacion de un turno + plan de 2 tareas, guardada en SQLite y FUERA de memoria (Vision recien arrancado)."""
    root = tmp_path / "proyecto"
    root.mkdir()
    master_index.set_project_path(SID, str(root), windows_user_created=f"{persistence.current_windows_user()}_20261008230000")
    path = db.db_path_for(str(root))
    db.ensure_session(path, SID, 1.0)
    db.insert_message(path, "m1", SID, "user", 1.0)
    db.insert_part(path, "m1-t", "m1", SID, "text", {"text": "cuanto stock hay de la pieza 123"}, 1.0)
    db.insert_message(path, "a1", SID, "assistant", 2.0)
    db.insert_part(path, "a1-t", "a1", SID, "text", {"text": "Hay 40 unidades en el deposito central."}, 2.0)
    db.replace_todos(path, SID, TODOS)
    store = get_session_store()
    store.delete(SID)
    yield path
    store.delete(SID)


# ---------------------------------------------------------------------------------------------------------------------
# REQ-003: el almacen reconstruye la sesion desde SQLite
# ---------------------------------------------------------------------------------------------------------------------
def test_the_store_rebuilds_messages_and_todos_from_sqlite(saved):
    session = SessionStore().get_or_create(SID)

    assert [m["role"] for m in session.messages] == ["user", "assistant"]
    assert "40 unidades" in str(session.messages[-1]["content"])
    assert [t["content"] for t in session.todos] == ["Revisar el stock", "Armar el informe"]


def test_a_session_only_on_disk_exists_so_it_is_not_reported_as_orphan(saved, client):
    assert SessionStore().exists(SID) is True
    status = client.get(f"/api/session/{SID}/status").json()
    assert status["exists"] is True and status["busy"] is False


def test_a_session_in_no_place_does_not_exist(saved, client):
    assert SessionStore().exists(UNKNOWN) is False
    assert client.get(f"/api/session/{UNKNOWN}/status").json()["exists"] is False


# ---------------------------------------------------------------------------------------------------------------------
# REQ-003: "cualquier request" -- los endpoints que antes no la recuperaban
# ---------------------------------------------------------------------------------------------------------------------
def test_the_task_panel_shows_the_saved_plan_after_a_restart(saved, client):
    resp = client.get(f"/api/todos/{SID}")

    assert resp.status_code == 200
    assert [t["content"] for t in resp.json()["todos"]] == ["Revisar el stock", "Armar el informe"]


def test_the_context_meter_counts_the_saved_conversation_after_a_restart(saved, client):
    data = client.get(f"/api/context/{SID}").json()

    assert data["tokens"] > 0 and data["pct_used"] >= 0


def test_clearing_works_after_a_restart_and_also_empties_sqlite(saved, client):
    resp = client.post(f"/api/session/{SID}/clear")

    assert resp.status_code == 200 and resp.json() == {"cleared": True}
    assert persistence.load_session_messages(SID) == [], "no reaparece al recargar"
    assert [t["content"] for t in persistence.load_session_todos(SID)] == ["Revisar el stock", "Armar el informe"], (
        "limpiar NO toca el plan"
    )


def test_compacting_works_after_a_restart(saved, client, monkeypatch):
    seen = []

    async def fake_compact(session_id, messages, context_window, model):
        seen.append((session_id, len(messages)))
        return True

    monkeypatch.setattr(main, "compact_session", fake_compact)

    resp = client.post(f"/api/commands/compact/{SID}")

    assert resp.status_code == 200 and resp.json() == {"compacted": True}
    assert seen == [(SID, 2)], "compacta la conversacion recuperada de SQLite"


def test_a_session_in_no_place_is_still_rejected_and_never_created(saved, client, monkeypatch):
    async def fake_compact(*args):  # pragma: no cover - no debe llamarse
        raise AssertionError("no hay nada que compactar")

    monkeypatch.setattr(main, "compact_session", fake_compact)

    assert client.get(f"/api/todos/{UNKNOWN}").json() == {"todos": []}
    assert client.get(f"/api/context/{UNKNOWN}").json()["tokens"] == 0
    assert client.post(f"/api/session/{UNKNOWN}/clear").status_code == 404
    assert client.post(f"/api/commands/compact/{UNKNOWN}").status_code == 404
    assert get_session_store().get(UNKNOWN) is None, "consultar una sesion inexistente no crea una sesion vacia"


def test_another_users_saved_session_is_still_forbidden(saved, client, monkeypatch):
    """El dueño se chequea ANTES de recuperar nada (BOLA): si quien pide es otro usuario de Windows, sigue dando 403."""
    monkeypatch.setattr(persistence, "current_windows_user", lambda: "otro.usuario")

    for resp in (
        client.get(f"/api/todos/{SID}"),
        client.get(f"/api/context/{SID}"),
        client.post(f"/api/session/{SID}/clear"),
        client.post(f"/api/commands/compact/{SID}"),
    ):
        assert resp.status_code == 403
    assert get_session_store().get(SID) is None
    assert len(persistence.load_session_messages(SID)) == 2, "nada se borro"


# ---------------------------------------------------------------------------------------------------------------------
# REQ-004: listado de sesiones persistidas
# ---------------------------------------------------------------------------------------------------------------------
def test_the_saved_sessions_list_has_the_expected_shape(saved, client):
    data = client.get("/api/sessions", params={"session_id": SID}).json()

    assert data["persistence_available"] is True
    row = next(s for s in data["sessions"] if s["id"] == SID)
    assert {"id", "title", "created_at", "updated_at", "message_count"} <= set(row)
    assert row["message_count"] == 2


def test_without_a_session_folder_the_list_is_empty_and_says_so(tmp_master_db, client):
    data = client.get("/api/sessions", params={"session_id": "sesion-sin-carpeta"}).json()

    assert data == {"sessions": [], "persistence_available": False}


# ---------------------------------------------------------------------------------------------------------------------
# Revision 2026-10-09: dos restauraciones a la vez no se pisan
# ---------------------------------------------------------------------------------------------------------------------
def test_a_restore_from_another_thread_never_replaces_the_session_that_is_already_running(saved, monkeypatch):
    """Los endpoints sync (tareas, contexto, limpiar) corren en el threadpool. Si uno restaura la sesion de SQLite mientras el
    event loop la crea y arranca un turno sobre ella, el que llega despues NO puede reemplazarla por otra copia: el turno quedaria
    en un objeto que ya nadie ve (Detener no lo frenaria y un mensaje nuevo arrancaria un segundo turno en paralelo)."""
    import threading

    store = SessionStore()
    in_the_middle, let_it_finish = threading.Event(), threading.Event()
    real_load = persistence.load_session_messages
    worker = threading.current_thread()

    def slow_load(session_id):
        if threading.current_thread() is not worker:  # el hilo del endpoint se queda leyendo SQLite
            in_the_middle.set()
            assert let_it_finish.wait(5)
        return real_load(session_id)

    monkeypatch.setattr(persistence, "load_session_messages", slow_load)
    restored = {}
    thread = threading.Thread(target=lambda: restored.setdefault("s", store.get_or_restore(SID)))
    thread.start()
    assert in_the_middle.wait(5)

    running = store.get_or_create(SID)  # el event loop: crea la sesion y "arranca un turno" sobre ella
    let_it_finish.set()
    thread.join(5)

    assert store.get(SID) is running, "la sesion con el turno sigue siendo la del almacen"
    assert restored["s"] is running, "el que llego despues usa la misma, no una copia"
