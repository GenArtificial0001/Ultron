"""spec editar-mensaje-tras-reinicio: editar o borrar un mensaje YA CERRADO funciona igual tras reiniciar Vision.

Sesion y SQLite reales en `tmp_path` (fixture `tmp_master_db`). El "reinicio" se simula sacando la sesion del
almacen en memoria: `get_or_create` la reconstruye desde la base, igual que despues de arrancar el servidor.
`_launch_turn` se reemplaza por una funcion que escribe en la base lo mismo que `run_turn` (mensaje + texto +
archivos), para ver el estado FINAL de SQLite sin llamar a ningun modelo."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import main
from app.persistence import db, master_index, writer
from app.persistence import service as persistence
from app.session.events import get_session_store

SID = "sesion-reinicio"
NOTICE = "[Archivos adjuntos en la carpeta de la sesión"


@pytest.fixture
def client():
    return TestClient(main.app)


@pytest.fixture
def project(tmp_path, tmp_master_db):
    root = tmp_path / "proyecto"
    (root / "attachment").mkdir(parents=True)
    master_index.set_project_path(
        SID, str(root), windows_user_created=f"{persistence.current_windows_user()}_20260929120000"
    )
    return root


@pytest.fixture
def launched(monkeypatch):
    """Reemplaza `_launch_turn`: guarda lo que escribiria `run_turn` y devuelve los turnos lanzados."""
    turns = []

    def fake_launch(session, session_id, turn):
        turns.append(turn)
        persistence.record_message_start(session_id, turn.id, "user")
        persistence.record_part_start(session_id, turn.id, f"{turn.id}-t", "text", {"text": turn.text})
        for i, att in enumerate(turn.attachments or []):
            persistence.record_part_start(session_id, turn.id, f"{turn.id}-f{i}", "file", att)
        writer.flush()

    monkeypatch.setattr(main, "_launch_turn", fake_launch)
    monkeypatch.setattr(main, "_assert_session_owner", lambda session_id: None)
    return turns


def seed(project: Path, files=("stock.csv",), images=()):
    """Conversacion de 2 turnos ya guardada en SQLite (m1 con adjuntos, m2 sin nada)."""
    path = db.db_path_for(str(project))
    db.ensure_session(path, SID, 1.0)
    db.insert_message(path, "m1", SID, "user", 1.0)
    db.insert_part(path, "m1-t", "m1", SID, "text", {"text": "analizalo"}, 1.0)
    for i, name in enumerate(files):
        (project / "attachment" / name).write_bytes(b"a,b\n1,2\n")
        meta = {"name": name, "path": f"attachment/{name}", "size": 8, "mime": "text/csv", "kind": "csv", "inplace": False}
        db.insert_part(path, f"m1-f{i}", "m1", SID, "file", meta, 1.0)
    for i, url in enumerate(images):
        db.insert_part(path, f"m1-i{i}", "m1", SID, "image", {"url": url}, 1.0)
    db.insert_message(path, "a1", SID, "assistant", 2.0)
    db.insert_part(path, "a1-t", "a1", SID, "text", {"text": "respuesta vieja"}, 2.0)
    db.insert_message(path, "m2", SID, "user", 3.0)
    db.insert_part(path, "m2-t", "m2", SID, "text", {"text": "otra cosa"}, 3.0)
    db.insert_message(path, "a2", SID, "assistant", 4.0)
    db.insert_part(path, "a2-t", "a2", SID, "text", {"text": "otra respuesta"}, 4.0)
    return path


def restart_server():
    """Como arrancar el servidor de nuevo: la sesion ya no esta en memoria."""
    get_session_store().delete(SID)
    assert get_session_store().get(SID) is None


def db_texts(path):
    return [(m["role"], m["content"] if isinstance(m["content"], str) else "<lista>") for m in db.load_messages(path, SID)]


# ---------------------------------------------------------------------------
# REQ-001 / REQ-003: tras reiniciar, editar funciona y conserva los archivos
# ---------------------------------------------------------------------------
def test_editing_a_closed_message_after_restart_works_and_keeps_files(client, project, launched):
    seed(project, images=["data:image/jpeg;base64,AAAA"])
    restart_server()

    r = client.patch(f"/api/chat/{SID}/message/m1", json={"text": "analizalo mejor"})

    assert r.status_code == 200, r.text
    assert r.json()["case"] == "history_truncated_and_relaunched"
    [turn] = launched
    assert turn.text == "analizalo mejor"
    assert [a["path"] for a in turn.attachments] == ["attachment/stock.csv"], "se perdio el archivo tras reiniciar"
    assert turn.images == ["data:image/jpeg;base64,AAAA"], "se perdio la imagen tras reiniciar"


def test_the_agent_history_after_the_resend_starts_from_the_edited_message(client, project, launched):
    seed(project)
    restart_server()

    client.patch(f"/api/chat/{SID}/message/m1", json={"text": "nuevo"})

    s = get_session_store().get(SID)
    assert s.messages == [], "lo posterior al mensaje editado debe haberse descartado del historial en memoria"


# ---------------------------------------------------------------------------
# REQ-002 / REQ-007: la base queda limpia y en orden
# ---------------------------------------------------------------------------
def test_editing_truncates_the_database_and_rewrites_the_message_clean(client, project, launched):
    path = seed(project)
    restart_server()

    client.patch(f"/api/chat/{SID}/message/m1", json={"text": "analizalo mejor"})
    writer.flush()

    msgs = db_texts(path)
    # ANTES se leia 'analizaloanalizalo mejor' (texto viejo + nuevo) y la respuesta vieja seguia ahi
    assert msgs == [("user", msgs[0][1])]
    assert msgs[0][1].startswith("analizalo mejor") and "analizaloanalizalo" not in msgs[0][1]
    assert "respuesta vieja" not in str(msgs) and "otra cosa" not in str(msgs), "quedo conversacion posterior"


def test_editing_a_message_in_the_middle_keeps_the_ones_before_it(client, project, launched):
    path = seed(project)
    restart_server()

    r = client.patch(f"/api/chat/{SID}/message/m2", json={"text": "otra cosa mejor"})
    writer.flush()

    assert r.status_code == 200
    msgs = db_texts(path)
    assert [role for role, _ in msgs] == ["user", "assistant", "user"]
    assert msgs[0][1].startswith("analizalo") and msgs[1][1] == "respuesta vieja"
    assert msgs[2][1] == "otra cosa mejor"
    [turn] = launched
    assert turn.attachments is None  # m2 no tenia archivos: no se inventan


def test_editing_without_a_restart_also_cleans_the_database(client, project, launched):
    """El mismo hueco existia SIN reiniciar: se corrompia la base igual."""
    path = seed(project)
    get_session_store().delete(SID)
    s = get_session_store().get_or_create(SID)  # sesion viva, con turno registrado en memoria
    from app.session.events import UserTurn

    s.turns["m1"] = UserTurn(id="m1", text="analizalo", messages_start_index=1)

    client.patch(f"/api/chat/{SID}/message/m1", json={"text": "otra version"})
    writer.flush()

    msgs = db_texts(path)
    assert len(msgs) == 1 and msgs[0][1].startswith("otra version") and "analizalo" not in msgs[0][1].replace("otra version", "")


# ---------------------------------------------------------------------------
# REQ-003 (seguridad): archivos eliminados o inexistentes no se reenvian
# ---------------------------------------------------------------------------
def test_deleted_or_missing_files_are_not_resent(client, project, launched):
    path = seed(project, files=("sigue.csv", "borrado.csv", "falta.csv"))
    (project / "attachment" / "falta.csv").unlink()  # el usuario lo borro a mano
    with db._connect(path) as conn:  # tombstone de /limpiar-adjuntos
        conn.execute(
            "UPDATE part SET data = json_set(data, '$.deleted', json('true')) WHERE id = 'm1-f1'"
        )
    restart_server()

    client.patch(f"/api/chat/{SID}/message/m1", json={"text": "de nuevo"})

    [turn] = launched
    assert [a["name"] for a in turn.attachments] == ["sigue.csv"]


def test_a_saved_path_that_escapes_the_project_is_dropped(client, project, launched, tmp_path):
    path = seed(project, files=())
    secreto = tmp_path / "secreto.txt"
    secreto.write_text("no deberia salir")
    db.insert_part(path, "m1-evil", "m1", SID, "file", {"name": "secreto.txt", "path": str(secreto), "inplace": False}, 1.0)
    db.insert_part(path, "m1-evil2", "m1", SID, "file", {"name": "x", "path": "attachment/../../secreto.txt"}, 1.0)
    restart_server()

    client.patch(f"/api/chat/{SID}/message/m1", json={"text": "de nuevo"})

    [turn] = launched
    assert turn.attachments is None, "una ruta guardada fuera del proyecto NO debe reenviarse"


# ---------------------------------------------------------------------------
# REQ-004: borrar un mensaje cerrado lo borra tambien de la base
# ---------------------------------------------------------------------------
def test_deleting_a_closed_message_removes_it_from_the_database(client, project, launched):
    path = seed(project)
    restart_server()

    r = client.delete(f"/api/chat/{SID}/message/m2")
    writer.flush()

    assert r.status_code == 200 and r.json()["case"] == "history_truncated"
    msgs = db_texts(path)
    assert [role for role, _ in msgs] == ["user", "assistant"], "el mensaje borrado reaparecia al reiniciar"
    assert msgs[0][1].startswith("analizalo")


# ---------------------------------------------------------------------------
# REQ-005 / REQ-006: ids desconocidos y comportamiento previo
# ---------------------------------------------------------------------------
def test_an_unknown_message_id_is_still_404(client, project, launched):
    seed(project)
    restart_server()

    assert client.patch(f"/api/chat/{SID}/message/no-existe", json={"text": "x"}).status_code == 404
    assert client.delete(f"/api/chat/{SID}/message/no-existe").status_code == 404
    assert not launched


def test_a_session_that_exists_nowhere_is_404(client, project, launched):
    r = client.patch("/api/chat/sesion-fantasma/message/m1", json={"text": "x"})
    assert r.status_code == 404


def test_the_message_is_located_by_its_id_even_if_an_earlier_response_was_deleted(client, project, launched):
    """El indice en memoria se corre al borrar una respuesta anterior: la base lo ubica por id real."""
    seed(project)
    restart_server()
    s = get_session_store().get_or_create(SID)
    from app.session.events import UserTurn

    # turno con un `messages_start_index` DESFASADO: apunta a OTRO mensaje de usuario (m1, indice 0). Un indice
    # que cayera en un mensaje del asistente ya lo rechaza el chequeo de rol; este caso solo lo ataja el texto.
    s.turns["m2"] = UserTurn(id="m2", text="otra cosa", messages_start_index=0)

    r = client.patch(f"/api/chat/{SID}/message/m2", json={"text": "otra cosa mejor"})
    writer.flush()

    assert r.status_code == 200
    msgs = db_texts(db.db_path_for(str(project)))
    assert [role for role, _ in msgs] == ["user", "assistant", "user"], "se trunco el mensaje equivocado"
    assert msgs[0][1].startswith("analizalo")
    # ...y tambien el historial EN MEMORIA que ve el agente: ahi es donde un indice desfasado hace dano
    # (la base se trunca por id, asi que sola no delata el error).
    memory = get_session_store().get(SID).messages
    assert [m["role"] for m in memory] == ["user", "assistant"], "el historial del agente quedo mal truncado"
    assert str(memory[0]["content"]).startswith("analizalo")
