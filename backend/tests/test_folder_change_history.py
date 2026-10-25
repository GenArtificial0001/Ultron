"""Spec cambio-carpeta-historial (0.35.9): al cambiar la carpeta de una sesion se PREGUNTA si se copia su historial.

Todo en `tmp_path` con bases SQLite REALES (baratas, y asi se prueba el comportamiento y no un doble). El indice maestro va a un temporal por
la fixture autouse `tmp_master_db`. Sin red ni modelo."""

from __future__ import annotations

import json
import logging
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import main
from app.persistence import db, master_index
from app.persistence import service as svc

USER = "jorge"
NOW = 1_800_000_000.0
SID = "sesion-cambio"


@pytest.fixture(autouse=True)
def fixed_user(monkeypatch):
    monkeypatch.setenv("USERNAME", USER)
    monkeypatch.setattr(svc, "current_windows_user", lambda: USER)


@pytest.fixture(autouse=True)
def sync_writer(monkeypatch):
    queued: list[str] = []

    def submit(job):
        queued.append(getattr(job, "__name__", "job"))
        job()

    monkeypatch.setattr(svc, "submit", submit)
    return queued


@pytest.fixture
def folders(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    return a, b


def seed(folder, sid=SID, messages=3, images=0, files=0, title="Mi sesion", user=USER):
    """Sesion REAL en la base de `folder`: mensajes con texto, herramienta, y opcionalmente imagenes, adjuntos y tareas."""
    path = db.db_path_for(str(folder))
    db.ensure_session(path, sid, NOW)
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "UPDATE session SET title = ?, title_manual = 1, windows_user_created = ?, model = 'gpt-x' WHERE id = ?",
            (title, f"{user}_20260101000000", sid),
        )
        conn.commit()
    finally:
        conn.close()
    for n in range(messages):
        db.insert_message(path, f"m{n}", sid, "user" if n % 2 == 0 else "assistant", NOW + n)
        db.insert_part(path, f"m{n}-t", f"m{n}", sid, "text", {"text": f"texto secreto {n}"}, NOW + n)
    if messages:
        db.insert_part(path, "m0-tool", "m0", sid, "tool", {"name": "grep", "result": "salida"}, NOW)
    for i in range(images):
        db.insert_part(path, f"img{i}", "m0", sid, "image", {"media_type": "image/png", "data": "QUJD" * 50}, NOW)
    for i in range(files):
        db.insert_part(
            path, f"file{i}", "m0", sid, "file", {"name": f"x{i}.pdf", "path": f"attachment/x{i}.pdf", "size": 10}, NOW
        )
    return path


def rows(path, sql, *params):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def dump(path, tables=("session", "message", "part", "todo")):
    """Todo el contenido de una base, para comparar 'antes' y 'despues'."""
    out = {}
    for table in tables:
        out[table] = rows(path, f"SELECT * FROM {table} ORDER BY 1, 2")
    return out


def register(folder, sid=SID):
    """Apunta el indice a `folder` SIN titulo: asi no renombra la sesion de la base (seed ya le puso su titulo)."""
    svc.assign_project_path(sid, str(folder), "")


# ---- REQ-001/002: la consulta previa --------------------------------------------------------------------------------------
def test_preview_is_applicable_when_the_old_folder_has_history_and_the_new_one_has_none(folders):
    a, b = folders
    seed(a, messages=5, files=2)
    register(a)
    info = svc.folder_change_preview(SID, str(b))
    assert info["applicable"] is True
    assert info["message_count"] == 5 and info["target_message_count"] == 0 and info["attachments"] == 2
    assert info["old_path"] == str(a) and info["new_path"] == str(b)


def test_preview_does_not_write_anything(folders):
    a, b = folders
    path = seed(a, messages=2)
    register(a)
    before = dump(path)
    svc.folder_change_preview(SID, str(b))
    assert dump(path) == before
    assert not db.db_path_for(str(b)).exists()  # ni siquiera crea la base de la carpeta nueva


def test_preview_is_not_applicable_without_a_previous_folder(folders):
    _, b = folders
    assert svc.folder_change_preview(SID, str(b))["applicable"] is False


def test_preview_is_not_applicable_for_the_same_folder_even_if_written_differently(folders):
    a, _ = folders
    seed(a)
    register(a)
    same = str(a).replace("\\", "/").upper()  # mismas carpetas en Windows: otra barra y otras mayusculas
    assert svc.folder_change_preview(SID, same)["applicable"] is False
    assert svc.folder_change_preview(SID, str(a) + "\\")["applicable"] is False


def test_preview_is_not_applicable_when_the_old_history_is_empty(folders):
    a, b = folders
    seed(a, messages=0)
    register(a)
    assert svc.folder_change_preview(SID, str(b))["applicable"] is False


def test_preview_is_not_applicable_when_the_new_folder_already_has_that_sessions_messages(folders):
    a, b = folders
    seed(a, messages=4)
    seed(b, messages=2)
    register(a)
    info = svc.folder_change_preview(SID, str(b))
    assert info["applicable"] is False and info["target_message_count"] == 2


def test_preview_ignores_messages_of_another_session_in_the_new_folder(folders):
    a, b = folders
    seed(a, messages=4)
    seed(b, sid="otra-sesion", messages=9)
    register(a)
    assert svc.folder_change_preview(SID, str(b))["applicable"] is True


def test_preview_with_an_unreadable_old_database_is_not_applicable_and_does_not_raise(folders):
    a, b = folders
    (a / "db").mkdir()
    (a / "db" / "vision.db").write_bytes(b"basura " * 100)
    master_index.set_project_path(SID, str(a), windows_user_created=USER)
    assert svc.folder_change_preview(SID, str(b))["applicable"] is False


def test_preview_with_a_folder_that_does_not_exist_is_not_applicable(folders, tmp_path):
    a, _ = folders
    seed(a)
    register(a)
    assert svc.folder_change_preview(SID, str(tmp_path / "no-existe"))["applicable"] is False


# ---- REQ-003/004/005/006: la copia ----------------------------------------------------------------------------------------
def test_copy_brings_every_message_part_image_and_title(folders):
    a, b = folders
    path_a = seed(a, messages=6, images=2, files=1, title="Titulo original")
    register(a)
    copied = svc.change_folder_with_history(SID, str(b), "Mi sesion")
    assert copied == 6
    path_b = db.db_path_for(str(b))
    assert rows(path_b, "SELECT COUNT(*) FROM message WHERE session_id = ?", SID)[0][0] == 6
    assert (
        rows(path_b, "SELECT COUNT(*) FROM part WHERE session_id = ?", SID)[0][0]
        == rows(path_a, "SELECT COUNT(*) FROM part")[0][0]
    )
    assert rows(path_b, "SELECT COUNT(*) FROM part WHERE type = 'image'")[0][0] == 2
    assert rows(path_b, "SELECT id, role, created_at FROM message ORDER BY created_at, id") == rows(
        path_a, "SELECT id, role, created_at FROM message ORDER BY created_at, id"
    )
    assert rows(path_b, "SELECT id, seq, type, data FROM part ORDER BY id") == rows(
        path_a, "SELECT id, seq, type, data FROM part ORDER BY id"
    )


def test_copy_keeps_the_original_title_and_creator(folders):
    a, b = folders
    seed(a, title="Titulo original")
    register(a)
    svc.change_folder_with_history(SID, str(b), "")
    row = rows(db.db_path_for(str(b)), "SELECT title, windows_user_created, model FROM session WHERE id = ?", SID)[0]
    assert row[0] == "Titulo original" and row[1].startswith(f"{USER}_") and row[2] == "gpt-x"


def test_the_title_the_person_sees_wins_over_the_copied_one(folders):
    """El modal manda el nombre que se ve en pantalla: si es distinto del guardado, ese es el que queda (igual que en cualquier asignacion)."""
    a, b = folders
    seed(a, title="Titulo viejo")
    register(a)
    svc.change_folder_with_history(SID, str(b), "Titulo nuevo")
    assert rows(db.db_path_for(str(b)), "SELECT title FROM session WHERE id = ?", SID) == [("Titulo nuevo",)]


def test_copy_leaves_the_old_database_exactly_as_it_was(folders):
    a, b = folders
    path_a = seed(a, messages=5, images=1, files=1)
    register(a)
    before = dump(path_a)
    size = path_a.stat().st_size
    svc.change_folder_with_history(SID, str(b), "Mi sesion")
    assert dump(path_a) == before
    assert path_a.stat().st_size == size


def test_copy_points_the_index_to_the_new_folder(folders):
    a, b = folders
    seed(a)
    register(a)
    svc.change_folder_with_history(SID, str(b), "Mi sesion")
    assert master_index.get_project_path(SID) == str(b)


def test_copy_never_mixes_with_a_history_already_there(folders):
    a, b = folders
    seed(a, messages=5)
    path_b = seed(b, messages=2)
    register(a)
    history = (
        "message",
        "part",
        "todo",
    )  # `assign_project_path` siempre vuelve a sembrar el titulo: la fila `session` puede cambiar
    before = dump(path_b, history)
    assert svc.change_folder_with_history(SID, str(b), "Mi sesion") == 0
    assert dump(path_b, history) == before


def test_copy_is_idempotent(folders):
    a, b = folders
    seed(a, messages=4)
    register(a)
    assert svc.change_folder_with_history(SID, str(b), "x") == 4
    master_index.set_project_path(SID, str(a), windows_user_created=USER)  # vuelve a la vieja y repite
    assert svc.change_folder_with_history(SID, str(b), "x") == 0
    assert rows(db.db_path_for(str(b)), "SELECT COUNT(*) FROM message")[0][0] == 4


def test_copy_does_not_touch_other_sessions_of_the_old_folder(folders):
    a, b = folders
    seed(a, sid=SID, messages=3)
    seed(a, sid="hermana", messages=7)
    register(a)
    svc.change_folder_with_history(SID, str(b), "x")
    assert rows(db.db_path_for(str(b)), "SELECT COUNT(*) FROM session")[0][0] == 1
    assert rows(db.db_path_for(str(b)), "SELECT COUNT(*) FROM message")[0][0] == 3


def test_copy_without_a_previous_folder_just_assigns_it(folders):
    _, b = folders
    assert svc.change_folder_with_history(SID, str(b), "x") == 0
    assert master_index.get_project_path(SID) == str(b)


def test_a_failing_copy_still_changes_the_folder_and_does_not_raise(folders, monkeypatch, caplog):
    a, b = folders
    seed(a)
    register(a)

    def boom(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(db, "copy_session_history", boom)
    with caplog.at_level(logging.WARNING):
        assert svc.change_folder_with_history(SID, str(b), "x") == 0
    assert master_index.get_project_path(SID) == str(b)
    assert any("copiar" in r.getMessage().lower() for r in caplog.records)


def test_the_copy_is_queued_before_the_index_points_to_the_new_folder(folders, monkeypatch):
    """Orden de la cola (decision 5): primero la copia, despues el puntero, asi nada posterior de la sesion la precede."""
    a, b = folders
    seed(a)
    register(a)
    order: list[str] = []
    real_copy = db.copy_session_history
    real_set = master_index.set_project_path
    monkeypatch.setattr(db, "copy_session_history", lambda *x, **k: order.append("copia") or real_copy(*x, **k))
    monkeypatch.setattr(master_index, "set_project_path", lambda *x, **k: order.append("puntero") or real_set(*x, **k))
    svc.change_folder_with_history(SID, str(b), "x")
    assert order.index("copia") < order.index("puntero")


def test_the_copy_logs_one_line_without_message_text(folders, caplog):
    a, b = folders
    seed(a, messages=3)
    register(a)
    with caplog.at_level(logging.DEBUG):
        svc.change_folder_with_history(SID, str(b), "x")
    lines = [
        r.getMessage()
        for r in caplog.records
        if "copi" in r.getMessage().lower() and "historial" in r.getMessage().lower()
    ]
    assert len(lines) == 1
    assert SID in lines[0] and str(a) in lines[0] and str(b) in lines[0] and "3" in lines[0]
    assert "texto secreto" not in caplog.text


# ---- REQ-004: la copia de imagenes y tareas --------------------------------------------------------------------------------
def test_copy_brings_the_todo_list(folders):
    a, b = folders
    path_a = seed(a)
    conn = sqlite3.connect(path_a)
    try:
        conn.execute(
            "INSERT INTO todo (session_id, position, content, status, priority, subtasks_json) VALUES (?, 0, 'hacer', 'pending', 'high', '[]')",
            (SID,),
        )
        conn.execute(
            "INSERT INTO todo (session_id, position, content, status, priority, subtasks_json) VALUES (?, 1, 'revisar', 'done', 'low', '[1]')",
            (SID,),
        )
        conn.commit()
    finally:
        conn.close()
    register(a)
    svc.change_folder_with_history(SID, str(b), "x")
    got = rows(
        db.db_path_for(str(b)),
        "SELECT position, content, status, priority, subtasks_json FROM todo WHERE session_id = ? ORDER BY position",
        SID,
    )
    assert got == [(0, "hacer", "pending", "high", "[]"), (1, "revisar", "done", "low", "[1]")]


# ---- REQ-001/003: los endpoints --------------------------------------------------------------------------------------------
@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(main, "_assert_session_owner", lambda session_id: None)
    return TestClient(main.app)


class TestEndpoints:
    def test_preview_endpoint(self, client, folders):
        a, b = folders
        seed(a, messages=4, files=1)
        register(a)
        body = client.get(f"/api/sessions/{SID}/folder-change-preview", params={"path": str(b)}).json()
        assert body["applicable"] is True and body["message_count"] == 4 and body["attachments"] == 1

    def test_preview_endpoint_without_path_is_a_clear_400(self, client):
        r = client.get(f"/api/sessions/{SID}/folder-change-preview", params={"path": "  "})
        assert r.status_code == 400 and "carpeta" in r.json()["detail"].lower()

    def test_preview_endpoint_checks_the_owner(self, client, monkeypatch, folders):
        from fastapi import HTTPException

        def deny(session_id):
            raise HTTPException(status_code=403, detail="No autorizado")

        monkeypatch.setattr(main, "_assert_session_owner", deny)
        r = client.get(f"/api/sessions/{SID}/folder-change-preview", params={"path": str(folders[1])})
        assert r.status_code == 403

    def test_post_with_copy_history_true_copies(self, client, folders):
        a, b = folders
        seed(a, messages=4)
        register(a)
        r = client.post(f"/api/sessions/{SID}/project-path", json={"path": str(b), "title": "", "copy_history": True})
        assert r.status_code == 200 and r.json()["assigned"] is True and r.json()["copied_messages"] == 4
        assert rows(db.db_path_for(str(b)), "SELECT COUNT(*) FROM message")[0][0] == 4

    def test_post_without_the_field_behaves_exactly_as_before(self, client, folders):
        a, b = folders
        seed(a, messages=4)
        register(a)
        r = client.post(f"/api/sessions/{SID}/project-path", json={"path": str(b), "title": ""})
        assert r.status_code == 200 and r.json() == {"assigned": True, "path": str(b)}  # EXACTAMENTE la respuesta de siempre
        assert (
            not db.db_path_for(str(b)).exists()
            or rows(db.db_path_for(str(b)), "SELECT COUNT(*) FROM message")[0][0] == 0
        )
        assert master_index.get_project_path(SID) == str(b)

    def test_copied_messages_is_reported_only_when_the_copy_was_asked(self, client, folders):
        a, b = folders
        seed(a, messages=4)
        register(a)
        asked = client.post(f"/api/sessions/{SID}/project-path", json={"path": str(b), "copy_history": True}).json()
        assert asked["copied_messages"] == 4
        c = b.parent / "c"
        c.mkdir()
        not_asked = client.post(f"/api/sessions/{SID}/project-path", json={"path": str(c), "copy_history": False}).json()
        assert "copied_messages" not in not_asked

    def test_post_with_copy_history_false_does_not_copy(self, client, folders):
        a, b = folders
        seed(a, messages=4)
        register(a)
        client.post(f"/api/sessions/{SID}/project-path", json={"path": str(b), "copy_history": False})
        assert (
            not db.db_path_for(str(b)).exists()
            or rows(db.db_path_for(str(b)), "SELECT COUNT(*) FROM message")[0][0] == 0
        )

    def test_post_to_a_folder_that_does_not_exist_is_still_a_400_and_copies_nothing(self, client, folders, tmp_path):
        a, _ = folders
        seed(a)
        register(a)
        r = client.post(
            f"/api/sessions/{SID}/project-path", json={"path": str(tmp_path / "no-existe"), "copy_history": True}
        )
        assert r.status_code == 400
        assert master_index.get_project_path(SID) == str(a)

    def test_first_assignment_keeps_saving_what_was_said_without_a_folder(self, client, folders):
        """No se rompe lo de la primera asignacion (spec fix-detener-y-progreso-visible, REQ-016)."""
        _, b = folders
        r = client.post(f"/api/sessions/{SID}/project-path", json={"path": str(b), "title": "Nueva"})
        assert r.status_code == 200 and master_index.get_project_path(SID) == str(b)


# ---- huecos cerrados tras las mutaciones ------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "spelling",
    [
        lambda p: str(p).replace("\\", "/"),  # otra barra
        lambda p: str(p).upper(),  # otras mayusculas (Windows no distingue)
        lambda p: str(p) + "/",  # barra final
        lambda p: str(p) + "\\.",  # punto final
    ],
    ids=["otra-barra", "mayusculas", "barra-final", "punto-final"],
)
def test_the_same_folder_written_in_any_way_is_never_a_change(folders, spelling):
    """El indice guarda la ruta TAL CUAL se escribio la primera vez: el modal puede devolverla escrita distinto y sigue siendo la misma."""
    a, _ = folders
    seed(a)
    master_index.set_project_path(SID, str(a), windows_user_created=USER)  # tal cual, sin normalizar
    assert svc.folder_change_preview(SID, spelling(a))["applicable"] is False


def test_changing_to_the_same_folder_written_differently_copies_nothing_and_keeps_one_history(folders):
    a, _ = folders
    seed(a, messages=4)
    master_index.set_project_path(SID, str(a), windows_user_created=USER)
    assert svc.change_folder_with_history(SID, str(a).replace("\\", "/").upper(), "") == 0
    assert rows(db.db_path_for(str(a)), "SELECT COUNT(*) FROM message")[0][0] == 4


def test_two_really_different_folders_are_not_taken_for_the_same(folders):
    a, b = folders
    seed(a, messages=2)
    register(a)
    assert svc.folder_change_preview(SID, str(b))["applicable"] is True


def test_copying_creates_only_the_database_in_the_new_folder(folders):
    """`ensure_project_structure` NO crea subcarpetas a proposito (pedido del usuario, 2026-09-09): copiar el historial tampoco las crea."""
    a, b = folders
    seed(a)
    register(a)
    svc.change_folder_with_history(SID, str(b), "x")
    assert (b / "db" / "vision.db").is_file()
    assert sorted(p.name for p in b.iterdir()) == ["db"]


def test_copying_a_session_the_old_database_does_not_have_copies_nothing(folders):
    a, b = folders
    seed(a, sid="otra", messages=3)  # la base vieja existe, pero no tiene ESTA sesion
    master_index.set_project_path(SID, str(a), windows_user_created=USER)
    assert db.copy_session_history(db.db_path_for(str(a)), db.db_path_for(str(b)), SID, NOW) == 0
    assert (
        not db.db_path_for(str(b)).exists() or rows(db.db_path_for(str(b)), "SELECT COUNT(*) FROM session")[0][0] == 0
    )


def test_the_old_database_is_opened_read_only(folders):
    """La garantia de «no se toca el origen»: abrirlo en solo lectura. Con el ARCHIVO marcado de solo lectura (atributo de Windows), si la
    copia lo abriera para escribir, SQLite fallaria. Ademas, su tamano y su fecha de modificacion quedan exactamente iguales."""
    import os
    import stat

    a, b = folders
    path = seed(a, messages=3)
    os.chmod(path, stat.S_IREAD)
    before = (path.stat().st_size, path.stat().st_mtime_ns)
    try:
        assert db.copy_session_history(path, db.db_path_for(str(b)), SID, NOW) == 3
        assert (path.stat().st_size, path.stat().st_mtime_ns) == before
    finally:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)


def test_the_previews_count_reads_read_only_and_creates_nothing(folders):
    a, b = folders
    seed(a)
    register(a)
    svc.folder_change_preview(SID, str(b))
    assert not (b / "db").exists()


def test_the_first_assignment_through_the_endpoint_saves_the_chat_held_in_memory_even_asking_for_the_copy(
    monkeypatch, folders
):
    """`copy_history: true` en una sesion SIN carpeta anterior cae en el camino de siempre (lo hablado sin carpeta se guarda): no se pierde."""
    from app.session.events import get_session_store

    monkeypatch.setattr(main, "_assert_session_owner", lambda session_id: None)
    _, b = folders
    sid = "sin-carpeta-previa"
    session = get_session_store().get_or_create(sid)
    session.messages = [
        {"role": "system", "content": "prompt"},
        {"role": "user", "content": "hola en memoria"},
        {"role": "assistant", "content": "eco"},
    ]
    try:
        r = TestClient(main.app).post(f"/api/sessions/{sid}/project-path", json={"path": str(b), "copy_history": True})
        assert r.status_code == 200 and r.json()["copied_messages"] == 0
        assert rows(db.db_path_for(str(b)), "SELECT COUNT(*) FROM message WHERE session_id = ?", sid)[0][0] == 2
    finally:
        get_session_store().delete(sid)


# ---- defensas que la regla «el destino ya tiene historial» tapa en el uso normal: se prueban directo ----------------------------
BS = chr(
    92
)  # UNA barra invertida: escribirla a mano dentro de un literal con rutas de Windows es un escape invalido de Python
BASE = BS.join(["C:", "Users", "x", "Mi Carpeta"])


@pytest.mark.parametrize(
    ("a", "b", "same"),
    [
        ("C:/Users/x/Mi Carpeta", "C:/Users/x/Mi Carpeta", True),
        (BASE, "C:/Users/x/Mi Carpeta", True),  # otra barra
        (BASE, BASE.upper(), True),  # mayusculas
        (BASE, BASE + BS, True),  # barra final
        (BASE, BASE + BS + ".", True),  # punto final
        (BASE, BS.join(["C:", "Users", "x", "Otra"]), False),
        (BASE, BS.join(["D:", "Users", "x", "Mi Carpeta"]), False),  # otra unidad
    ],
)
def test_same_folder_ignores_how_a_path_is_written_but_not_which_folder_it_is(a, b, same):
    assert svc._same_folder(a, b) is same  # noqa: SLF001


def test_the_copy_opens_the_old_database_with_the_read_only_flag(folders, monkeypatch):
    """Garantia de «no se toca el origen»: la URI con la que se abre la base vieja lleva `mode=ro` (SQLite deja pasar una escritura en un
    archivo de solo lectura sin avisar, asi que no se puede probar solo con atributos del archivo)."""
    a, b = folders
    path = seed(a, messages=2)
    opened: list[str] = []
    real = sqlite3.connect

    def spy(target, *args, **kwargs):
        opened.append(str(target))
        return real(target, *args, **kwargs)

    monkeypatch.setattr(db.sqlite3, "connect", spy)
    assert db.copy_session_history(path, db.db_path_for(str(b)), SID, NOW) == 2
    origin = [u for u in opened if path.name in u and "mode=ro" in u]
    assert origin, f"el origen no se abrio con mode=ro: {opened}"
    assert not any(path.name in u and "mode=ro" not in u and str(b) not in u for u in opened), opened
