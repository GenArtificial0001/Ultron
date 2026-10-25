"""Spec importar-historial-de-carpeta (0.36.0): al elegir una carpeta que YA tiene conversaciones anteriores, se avisa y se ofrece importarlas.

Caso real del usuario (2026-10-23): sesion nueva «Auto Vision» + carpeta con la sesion vieja «Presentacion» (124 mensajes). Todo en `tmp_path` con bases
SQLite REALES; el indice maestro va a un temporal por la fixture autouse `tmp_master_db`. Sin red ni modelo."""

from __future__ import annotations

import logging
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import main
from app.persistence import db, master_index, writer
from app.persistence import service as svc
from app.session.events import get_session_store

USER = "jorge"
OTHER = "marta"
NOW = 1_800_000_000.0
NEW = "sesion-nueva"
OLD = "sesion-vieja"


@pytest.fixture(autouse=True)
def fixed_user(monkeypatch):
    monkeypatch.setenv("USERNAME", USER)
    monkeypatch.setattr(svc, "current_windows_user", lambda: USER)


@pytest.fixture(autouse=True)
def sync_writer(monkeypatch):
    monkeypatch.setattr(svc, "submit", lambda job: job())


@pytest.fixture
def folder(tmp_path):
    f = tmp_path / "proyecto"
    f.mkdir()
    return f


def seed(folder, sid=OLD, messages=4, images=0, files=0, todos=0, title="Presentacion", user=USER, at=NOW):
    """Sesion REAL en la base de `folder`: mensajes con texto y herramienta, y opcionalmente imagenes, adjuntos y tareas."""
    path = db.db_path_for(str(folder))
    db.ensure_session(path, sid, at)
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "UPDATE session SET title = ?, title_manual = 1, windows_user_created = ?, model = 'gpt-x', updated_at = ? WHERE id = ?",
            (title, f"{user}_20260101000000", at + messages, sid),
        )
        for i in range(todos):
            conn.execute(
                "INSERT INTO todo (session_id, position, content, status, priority, subtasks_json) VALUES (?, ?, ?, 'pending', 'high', '[]')",
                (sid, i, f"tarea {i}"),
            )
        conn.commit()
    finally:
        conn.close()
    for n in range(messages):
        db.insert_message(path, f"{sid}-m{n}", sid, "user" if n % 2 == 0 else "assistant", at + n)
        db.insert_part(path, f"{sid}-p{n}", f"{sid}-m{n}", sid, "text", {"text": f"texto secreto {sid} {n}"}, at + n)
    if messages:
        db.insert_part(path, f"{sid}-tool", f"{sid}-m0", sid, "tool", {"name": "grep", "result": "salida"}, at)
    for i in range(images):
        db.insert_part(path, f"{sid}-img{i}", f"{sid}-m0", sid, "image", {"media_type": "image/png", "data": "QUJD" * 40}, at)
    for i in range(files):
        db.insert_part(path, f"{sid}-f{i}", f"{sid}-m0", sid, "file", {"name": f"x{i}.pdf", "path": f"attachment/x{i}.pdf"}, at)
    return path


def rows(path, sql, *params):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def dump(path, tables=("session", "message", "part", "todo")):
    return {t: rows(path, f"SELECT * FROM {t} ORDER BY 1, 2") for t in tables}


# ---- REQ-001/002/003: la consulta ------------------------------------------------------------------------------------------
def test_it_lists_the_previous_sessions_of_the_folder_with_title_count_and_date(folder):
    seed(folder, OLD, messages=6, title="Presentacion")
    seed(folder, "otra", messages=2, title="Otra charla", at=NOW - 5000)
    info = svc.folder_sessions(NEW, str(folder))
    assert info["applicable"] is True
    assert [(s["id"], s["title"], s["message_count"]) for s in info["sessions"]] == [
        (OLD, "Presentacion", 6),
        ("otra", "Otra charla", 2),
    ]
    assert all(s["updated_at"] > 0 for s in info["sessions"])


def test_it_orders_by_last_activity_most_recent_first(folder):
    seed(folder, "vieja", at=NOW - 9000, title="a")
    seed(folder, "nueva", at=NOW, title="b")
    seed(folder, "media", at=NOW - 4000, title="c")
    assert [s["id"] for s in svc.folder_sessions(NEW, str(folder))["sessions"]] == ["nueva", "media", "vieja"]


def test_it_marks_which_sessions_are_registered_in_the_index(folder):
    seed(folder, OLD)
    seed(folder, "suelta")
    master_index.set_project_path(OLD, str(folder), windows_user_created=USER)
    got = {s["id"]: s["registered"] for s in svc.folder_sessions(NEW, str(folder))["sessions"]}
    assert got == {OLD: True, "suelta": False}


def test_it_caps_the_list_at_twenty(folder):
    for i in range(25):
        seed(folder, f"s{i:02d}", messages=1, at=NOW + i)
    assert len(svc.folder_sessions(NEW, str(folder))["sessions"]) == 20


def test_it_does_not_list_the_current_session_nor_empty_ones(folder):
    seed(folder, NEW, messages=0)
    seed(folder, "vacia", messages=0)
    seed(folder, OLD, messages=3)
    assert [s["id"] for s in svc.folder_sessions(NEW, str(folder))["sessions"]] == [OLD]


def test_it_never_lists_sessions_of_another_windows_user(folder):
    seed(folder, "ajena", user=OTHER)
    seed(folder, "de-jorge2", user="jorge2")  # un usuario que solo EMPIEZA igual
    assert svc.folder_sessions(NEW, str(folder))["applicable"] is False


def test_it_is_not_applicable_when_there_is_nothing_to_offer(folder):
    assert svc.folder_sessions(NEW, str(folder))["applicable"] is False  # carpeta sin base
    seed(folder, NEW, messages=0)
    assert svc.folder_sessions(NEW, str(folder))["applicable"] is False  # solo la actual, vacia


def test_it_is_not_applicable_for_a_folder_that_does_not_exist(tmp_path):
    assert svc.folder_sessions(NEW, str(tmp_path / "no-existe"))["applicable"] is False


def test_it_is_not_applicable_when_it_is_the_sessions_own_current_folder(folder):
    """La misma carpeta que ya tiene la sesion (aunque se escriba distinto) no es un cambio: no se avisa."""
    seed(folder, OLD)
    master_index.set_project_path(NEW, str(folder), windows_user_created=USER)
    assert svc.folder_sessions(NEW, str(folder))["applicable"] is False
    assert svc.folder_sessions(NEW, str(folder).replace(chr(92), "/").upper())["applicable"] is False


def test_it_is_not_applicable_when_the_current_session_already_has_messages_there(folder):
    seed(folder, NEW, messages=3)
    seed(folder, OLD, messages=5)
    info = svc.folder_sessions(NEW, str(folder))
    assert info["applicable"] is False


def test_it_survives_an_unreadable_database(folder):
    (folder / "db").mkdir()
    (folder / "db" / "vision.db").write_bytes(b"basura " * 100)
    assert svc.folder_sessions(NEW, str(folder))["applicable"] is False


def test_the_query_writes_nothing_and_creates_no_folder(folder, tmp_path):
    path = seed(folder, OLD)
    before = dump(path)
    svc.folder_sessions(NEW, str(folder))
    assert dump(path) == before
    other = tmp_path / "otra"
    other.mkdir()
    svc.folder_sessions(NEW, str(other))
    assert not (other / "db").exists()


def test_can_import_is_false_when_the_current_session_has_a_conversation_in_memory(folder):
    seed(folder, OLD)
    session = get_session_store().get_or_create(NEW)
    session.messages = [{"role": "system", "content": "p"}, {"role": "user", "content": "hola"}]
    try:
        info = svc.folder_sessions(NEW, str(folder))
        assert info["applicable"] is True and info["can_import"] is False
    finally:
        get_session_store().delete(NEW)


def test_can_import_is_true_for_an_empty_session(folder):
    seed(folder, OLD)
    assert svc.folder_sessions(NEW, str(folder))["can_import"] is True


# ---- REQ-004/005/006: importar ----------------------------------------------------------------------------------------------
def test_import_brings_messages_parts_images_todos_and_keeps_the_order(folder):
    path = seed(folder, OLD, messages=6, images=2, todos=3)
    copied = svc.import_session_history(NEW, str(folder), OLD)
    assert copied == 6
    msgs = rows(path, "SELECT id, role, created_at FROM message WHERE session_id = ? ORDER BY created_at, id", NEW)
    assert [(r[1], r[2]) for r in msgs] == [(r[1], r[2]) for r in rows(path, "SELECT id, role, created_at FROM message WHERE session_id = ? ORDER BY created_at, id", OLD)]
    assert rows(path, "SELECT COUNT(*) FROM part WHERE session_id = ?", NEW)[0][0] == rows(path, "SELECT COUNT(*) FROM part WHERE session_id = ?", OLD)[0][0]
    assert rows(path, "SELECT COUNT(*) FROM part WHERE session_id = ? AND type = 'image'", NEW)[0][0] == 2
    assert rows(path, "SELECT position, content FROM todo WHERE session_id = ? ORDER BY position", NEW) == [(0, "tarea 0"), (1, "tarea 1"), (2, "tarea 2")]


def test_import_uses_new_ids_so_nothing_collides_with_the_original(folder):
    path = seed(folder, OLD, messages=3)
    svc.import_session_history(NEW, str(folder), OLD)
    new_ids = {r[0] for r in rows(path, "SELECT id FROM message WHERE session_id = ?", NEW)}
    old_ids = {r[0] for r in rows(path, "SELECT id FROM message WHERE session_id = ?", OLD)}
    assert new_ids and not (new_ids & old_ids)
    assert all(i.startswith(f"{NEW}:") for i in new_ids)


def test_import_keeps_the_parts_attached_to_their_new_messages(folder):
    path = seed(folder, OLD, messages=3, images=1)
    svc.import_session_history(NEW, str(folder), OLD)
    orphans = rows(
        path,
        "SELECT COUNT(*) FROM part p WHERE p.session_id = ? AND NOT EXISTS (SELECT 1 FROM message m WHERE m.id = p.message_id AND m.session_id = ?)",
        NEW,
        NEW,
    )
    assert orphans[0][0] == 0


def test_import_leaves_the_original_session_exactly_as_it_was(folder):
    path = seed(folder, OLD, messages=5, images=1, files=1, todos=2)
    before = {
        "session": rows(path, "SELECT * FROM session WHERE id = ?", OLD),
        "message": rows(path, "SELECT * FROM message WHERE session_id = ? ORDER BY id", OLD),
        "part": rows(path, "SELECT * FROM part WHERE session_id = ? ORDER BY id", OLD),
        "todo": rows(path, "SELECT * FROM todo WHERE session_id = ? ORDER BY position", OLD),
    }
    svc.import_session_history(NEW, str(folder), OLD)
    after = {
        "session": rows(path, "SELECT * FROM session WHERE id = ?", OLD),
        "message": rows(path, "SELECT * FROM message WHERE session_id = ? ORDER BY id", OLD),
        "part": rows(path, "SELECT * FROM part WHERE session_id = ? ORDER BY id", OLD),
        "todo": rows(path, "SELECT * FROM todo WHERE session_id = ? ORDER BY position", OLD),
    }
    assert after == before


def test_import_keeps_the_attachments_because_the_folder_is_the_same(folder):
    path = seed(folder, OLD, messages=2, files=2)
    svc.import_session_history(NEW, str(folder), OLD)
    files = rows(path, "SELECT data FROM part WHERE session_id = ? AND type = 'file' ORDER BY id", NEW)
    assert len(files) == 2 and all("attachment/x" in f[0] for f in files)


def test_import_gives_the_new_session_the_old_title_when_it_has_none(folder):
    path = seed(folder, OLD, title="Presentacion")
    svc.import_session_history(NEW, str(folder), OLD)
    assert rows(path, "SELECT title FROM session WHERE id = ?", NEW)[0][0]  # existe y no esta vacio


def test_the_current_session_belongs_to_the_current_user(folder):
    path = seed(folder, OLD)
    svc.import_session_history(NEW, str(folder), OLD)
    assert rows(path, "SELECT windows_user_created FROM session WHERE id = ?", NEW)[0][0].startswith(f"{USER}_")


def test_import_never_mixes_with_a_conversation_already_there(folder):
    path = seed(folder, NEW, messages=2, title="Ya hablada")
    seed(folder, OLD, messages=5)
    before = dump(path, ("message", "part"))
    with pytest.raises(ValueError):
        svc.import_session_history(NEW, str(folder), OLD)
    assert dump(path, ("message", "part")) == before


def test_importing_twice_never_duplicates_anything(folder):
    """La segunda vez la sesion ya tiene mensajes: se rechaza (no se mezcla) y la base queda igual. A nivel de base tambien devuelve 0."""
    path = seed(folder, OLD, messages=4)
    assert svc.import_session_history(NEW, str(folder), OLD) == 4
    with pytest.raises(ValueError):
        svc.import_session_history(NEW, str(folder), OLD)
    assert db.import_session_history(path, OLD, NEW, "jorge_20260101000000", NOW) == 0
    assert rows(path, "SELECT COUNT(*) FROM message WHERE session_id = ?", NEW)[0][0] == 4
    assert rows(path, "SELECT COUNT(*) FROM part WHERE session_id = ?", NEW)[0][0] == rows(path, "SELECT COUNT(*) FROM part WHERE session_id = ?", OLD)[0][0]


def test_import_rejects_a_missing_source_with_a_clear_error(folder):
    seed(folder, "otra")
    with pytest.raises(ValueError, match="(?i)no se encontr"):
        svc.import_session_history(NEW, str(folder), "no-existe")


def test_import_rejects_a_source_of_another_user(folder):
    seed(folder, "ajena", user=OTHER)
    with pytest.raises(ValueError):
        svc.import_session_history(NEW, str(folder), "ajena")


def test_import_rejects_importing_a_session_into_itself(folder):
    seed(folder, OLD)
    with pytest.raises(ValueError):
        svc.import_session_history(OLD, str(folder), OLD)


def test_import_rejects_a_folder_that_does_not_exist(tmp_path):
    with pytest.raises(FileNotFoundError):
        svc.import_session_history(NEW, str(tmp_path / "no-existe"), OLD)


def test_import_logs_one_line_without_message_text(folder, caplog):
    seed(folder, OLD, messages=3)
    with caplog.at_level(logging.DEBUG):
        svc.import_session_history(NEW, str(folder), OLD)
    lines = [r.getMessage() for r in caplog.records if "import" in r.getMessage().lower() and "historial" in r.getMessage().lower()]
    assert len(lines) == 1 and NEW in lines[0] and OLD in lines[0] and "3" in lines[0]
    assert "texto secreto" not in caplog.text


# ---- REQ-007: la memoria de la sesion ---------------------------------------------------------------------------------------
def test_after_importing_the_session_in_memory_has_the_history(folder):
    seed(folder, OLD, messages=4)
    master_index.set_project_path(NEW, str(folder), windows_user_created=USER)
    session = get_session_store().get_or_create(NEW)
    try:
        svc.import_session_history(NEW, str(folder), OLD)
        main._reload_session_memory(NEW)
        assert len([m for m in session.messages if m["role"] in ("user", "assistant")]) >= 4
    finally:
        get_session_store().delete(NEW)


def test_it_does_not_touch_the_memory_of_a_session_with_a_running_turn(folder):
    import asyncio

    seed(folder, OLD, messages=4)
    master_index.set_project_path(NEW, str(folder), windows_user_created=USER)
    session = get_session_store().get_or_create(NEW)
    session.messages = [{"role": "system", "content": "p"}]
    loop = asyncio.new_event_loop()
    try:
        session.current_task = loop.create_future()  # un turno "corriendo": no esta hecho
        svc.import_session_history(NEW, str(folder), OLD)
        main._reload_session_memory(NEW)
        assert session.messages == [{"role": "system", "content": "p"}]
    finally:
        session.current_task = None
        loop.close()
        get_session_store().delete(NEW)


# ---- REQ-013: la cantidad informada de 0.35.9 --------------------------------------------------------------------------------
def test_change_folder_reports_the_real_number_with_the_real_writer_thread(tmp_path, monkeypatch):
    """Error de 0.35.9: con el hilo escritor REAL el resultado llegaba vacio y se informaba 0 aunque la copia SI se hacia."""
    monkeypatch.setattr(svc, "submit", writer.submit)
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    seed(a, "sesion-x", messages=4)
    master_index.set_project_path("sesion-x", str(a), windows_user_created=USER)
    assert svc.change_folder_with_history("sesion-x", str(b), "") == 4
    writer.flush()
    assert rows(db.db_path_for(str(b)), "SELECT COUNT(*) FROM message")[0][0] == 4


# ---- los endpoints ---------------------------------------------------------------------------------------------------------
@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(main, "_assert_session_owner", lambda session_id: None)
    return TestClient(main.app)


class TestEndpoints:
    def test_the_query_endpoint_answers_with_the_list(self, client, folder):
        seed(folder, OLD, messages=7, title="Presentacion")
        body = client.get(f"/api/sessions/{NEW}/folder-sessions", params={"path": str(folder)}).json()
        assert body["applicable"] is True and body["can_import"] is True
        assert [(s["id"], s["message_count"]) for s in body["sessions"]] == [(OLD, 7)]

    def test_the_query_endpoint_without_path_is_a_clear_400(self, client):
        r = client.get(f"/api/sessions/{NEW}/folder-sessions", params={"path": " "})
        assert r.status_code == 400 and "carpeta" in r.json()["detail"].lower()

    def test_the_query_endpoint_checks_the_owner(self, client, monkeypatch, folder):
        from fastapi import HTTPException

        def deny(session_id):
            raise HTTPException(status_code=403, detail="No autorizado")

        monkeypatch.setattr(main, "_assert_session_owner", deny)
        assert client.get(f"/api/sessions/{NEW}/folder-sessions", params={"path": str(folder)}).status_code == 403

    def test_post_with_import_copies_and_reports_the_number(self, client, folder):
        path = seed(folder, OLD, messages=5)
        r = client.post(f"/api/sessions/{NEW}/project-path", json={"path": str(folder), "import_from_session_id": OLD})
        assert r.status_code == 200 and r.json()["imported_messages"] == 5 and r.json()["assigned"] is True
        assert rows(path, "SELECT COUNT(*) FROM message WHERE session_id = ?", NEW)[0][0] == 5
        assert master_index.get_project_path(NEW) == str(folder)

    def test_post_with_import_leaves_the_session_with_its_context_in_memory(self, client, folder):
        seed(folder, OLD, messages=4)
        get_session_store().get_or_create(NEW)  # la sesion nueva ya esta en memoria (como en el uso real, donde la pagina la abrio)
        client.post(f"/api/sessions/{NEW}/project-path", json={"path": str(folder), "import_from_session_id": OLD})
        try:
            session = get_session_store().get(NEW)
            assert session is not None and len([m for m in session.messages if m["role"] in ("user", "assistant")]) >= 4
            shown = client.get(f"/api/sessions/{NEW}/messages").json()["messages"]
            assert len(shown) >= 4
        finally:
            get_session_store().delete(NEW)

    def test_post_without_the_new_fields_is_exactly_as_before(self, client, folder):
        seed(folder, OLD, messages=3)
        r = client.post(f"/api/sessions/{NEW}/project-path", json={"path": str(folder)})
        assert r.status_code == 200 and r.json() == {"assigned": True, "path": str(folder)}
        assert rows(db.db_path_for(str(folder)), "SELECT COUNT(*) FROM message WHERE session_id = ?", NEW)[0][0] == 0

    def test_post_import_into_a_session_that_already_talked_is_a_clear_400(self, client, folder):
        seed(folder, NEW, messages=2)
        seed(folder, OLD, messages=4)
        r = client.post(f"/api/sessions/{NEW}/project-path", json={"path": str(folder), "import_from_session_id": OLD})
        assert r.status_code == 400 and r.json()["detail"]
        assert rows(db.db_path_for(str(folder)), "SELECT COUNT(*) FROM message WHERE session_id = ?", NEW)[0][0] == 2

    def test_post_import_from_a_missing_session_is_a_clear_400_and_assigns_nothing(self, client, folder):
        seed(folder, "otra")
        r = client.post(f"/api/sessions/{NEW}/project-path", json={"path": str(folder), "import_from_session_id": "no-existe"})
        assert r.status_code == 400
        assert master_index.get_project_path(NEW) is None

    def test_a_failing_copy_still_assigns_the_folder_and_says_so(self, client, folder, monkeypatch, caplog):
        seed(folder, OLD, messages=4)

        def boom(*a, **k):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(db, "import_session_history", boom)
        with caplog.at_level(logging.WARNING):
            r = client.post(f"/api/sessions/{NEW}/project-path", json={"path": str(folder), "import_from_session_id": OLD})
        assert r.status_code == 200 and r.json()["import_failed"] is True and r.json()["imported_messages"] == 0
        assert master_index.get_project_path(NEW) == str(folder)
        assert any("importar" in rec.getMessage().lower() for rec in caplog.records)

    def test_import_and_copy_history_together_do_not_both_run(self, client, tmp_path):
        """Las dos preguntas son excluyentes; si llegan las dos, manda la de importar (la sesion es nueva) y no se copia nada de otra carpeta."""
        a, b = tmp_path / "a", tmp_path / "b"
        a.mkdir()
        b.mkdir()
        seed(b, OLD, messages=3)
        r = client.post(
            f"/api/sessions/{NEW}/project-path",
            json={"path": str(b), "import_from_session_id": OLD, "copy_history": True},
        )
        assert r.status_code == 200 and r.json()["imported_messages"] == 3
