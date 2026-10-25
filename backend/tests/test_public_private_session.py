"""Spec sesion-publica-privada (0.36.2): una sesion puede ser PUBLICA (sin dueño: otra persona puede importar una COPIA) o PRIVADA (por defecto).

Pedido del usuario (2026-10-24): «por defecto siempre privada; al hacerla publica se borra el CSID; quien crea una sesion sobre una carpeta con una sesion
publica puede importarla, pero cada uno escribe como un fork distinto». Todo en `tmp_path` con bases SQLite REALES; el indice maestro va a un temporal
por la fixture autouse `tmp_master_db`. Sin red ni modelo."""

from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import main
from app.persistence import db, master_index
from app.persistence import service as svc
from app.session.events import get_session_store

USER = "jorge"
OTHER = "marta"
NOW = 1_800_000_000.0
NEW = "sesion-nueva"
PUB = "sesion-publica"
PRIV = "sesion-privada-ajena"
MINE = "sesion-mia"
MAIL = "m365_search_messages"
MARK = "contenido privado omitido"


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


@pytest.fixture
def client():
    return TestClient(main.app)


def seed(folder, sid, user=USER, public=False, messages=4, title="Charla"):
    """Sesion real en la base de `folder`. Una publica queda SIN dueño (CSID borrado), como la deja `set_public(True)`."""
    path = db.db_path_for(str(folder))
    db.ensure_session(path, sid, NOW)
    owner = "" if public else f"{user}_20260101000000"
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "UPDATE session SET title = ?, title_manual = 1, windows_user_created = ?, is_public = ?, updated_at = ? WHERE id = ?",
            (title, owner, 1 if public else 0, NOW + messages, sid),
        )
        conn.commit()
    finally:
        conn.close()
    for n in range(messages):
        db.insert_message(path, f"{sid}-m{n}", sid, "user" if n % 2 == 0 else "assistant", NOW + n)
        db.insert_part(path, f"{sid}-p{n}", f"{sid}-m{n}", sid, "text", {"text": f"texto {sid} {n}"}, NOW + n)
    if messages:  # las herramientas cuelgan del primer mensaje: sin mensajes no hay donde ponerlas
        tools = [
            ("t1", {"name": MAIL, "arguments": "{}", "result": "CORREO CONFIDENCIAL de Marta"}),
            ("t2", {"name": "grep", "arguments": "{}", "result": "salida de grep"}),
            ("t3", {"name": "m365_send_drafted_message", "arguments": "{}", "result": "otro correo"}),
        ]
        for tag, data in tools:
            db.insert_part(path, f"{sid}-{tag}", f"{sid}-m0", sid, "tool", data, NOW)
    return path


def q(path, sql, *params):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def dump(path):
    return {t: q(path, f"SELECT * FROM {t} ORDER BY 1, 2") for t in ("session", "message", "part", "todo")}


# ---------------------------------------------------------------------------------------------------------------------
# REQ-001: privada por defecto, migracion
# ---------------------------------------------------------------------------------------------------------------------
def test_a_new_session_is_private_by_default(folder):
    path = db.db_path_for(str(folder))
    db.ensure_session(path, NEW, NOW)
    assert q(path, "SELECT is_public FROM session WHERE id = ?", NEW) == [(0,)]
    assert db.get_session_meta(path, NEW)["is_public"] is False


def test_an_old_database_without_the_column_is_migrated_keeping_everything_private(folder):
    path = db.db_path_for(str(folder))
    path.parent.mkdir(parents=True)
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE session (id TEXT PRIMARY KEY, title TEXT NOT NULL, model TEXT, title_manual INTEGER NOT NULL DEFAULT 0,
            windows_user_created TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, updated_at REAL NOT NULL);
        INSERT INTO session (id, title, windows_user_created, created_at, updated_at) VALUES ('vieja', 'Vieja', 'jorge_20260101000000', 1, 1);
        """
    )
    conn.commit()
    conn.close()
    meta = db.get_session_meta(path, "vieja")  # abrir la base la migra
    assert meta["is_public"] is False and meta["title"] == "Vieja"
    assert q(path, "SELECT windows_user_created FROM session WHERE id = 'vieja'") == [("jorge_20260101000000",)]


# ---------------------------------------------------------------------------------------------------------------------
# REQ-002: set_public
# ---------------------------------------------------------------------------------------------------------------------
def test_making_it_public_clears_the_owner_and_flags_it(folder):
    path = seed(folder, MINE)
    assert db.set_public(path, MINE, True, USER, NOW) is True
    assert q(path, "SELECT is_public, windows_user_created FROM session WHERE id = ?", MINE) == [(1, "")]


def test_making_it_private_again_restores_the_current_user(folder):
    path = seed(folder, MINE, public=True)
    assert db.set_public(path, MINE, False, USER, NOW) is True
    ((flag, owner),) = q(path, "SELECT is_public, windows_user_created FROM session WHERE id = ?", MINE)
    assert flag == 0 and owner.startswith(f"{USER}_") and len(owner) == len(USER) + 15


@pytest.mark.parametrize("public", [True, False])
def test_it_is_idempotent_and_never_touches_messages(folder, public):
    path = seed(folder, MINE, public=not public)
    before = dump(path)["message"], dump(path)["part"]
    db.set_public(path, MINE, public, USER, NOW)
    once = dump(path)["session"]
    db.set_public(path, MINE, public, USER, NOW)
    assert (dump(path)["message"], dump(path)["part"]) == before
    assert [r[:5] for r in dump(path)["session"]] == [r[:5] for r in once]


def test_an_unknown_session_returns_false(folder):
    path = db.db_path_for(str(folder))
    db.ensure_session(path, NEW, NOW)
    assert db.set_public(path, "no-existe", True, USER, NOW) is False


# ---------------------------------------------------------------------------------------------------------------------
# REQ-003: el listado
# ---------------------------------------------------------------------------------------------------------------------
def test_it_lists_other_peoples_public_sessions_marked_as_such(folder):
    seed(folder, PUB, user=OTHER, public=True, messages=6, title="Del equipo")
    info = svc.folder_sessions(NEW, str(folder))
    assert info["applicable"] is True
    (s,) = info["sessions"]
    assert (s["id"], s["title"], s["message_count"], s["own"], s["public"]) == (PUB, "Del equipo", 6, False, True)


def test_it_never_lists_other_peoples_private_sessions(folder):
    seed(folder, PRIV, user=OTHER, public=False)
    info = svc.folder_sessions(NEW, str(folder))
    assert info == {"applicable": False, "can_import": False, "sessions": []}


def test_own_and_public_sessions_are_listed_together_and_own_ones_are_flagged(folder):
    seed(folder, MINE, user=USER, messages=3, title="Mia")
    seed(folder, PUB, user=OTHER, public=True, messages=5, title="Publica")
    sessions = {s["id"]: s for s in svc.folder_sessions(NEW, str(folder))["sessions"]}
    assert set(sessions) == {MINE, PUB}
    assert (sessions[MINE]["own"], sessions[MINE]["public"]) == (True, False)
    assert (sessions[PUB]["own"], sessions[PUB]["public"]) == (False, True)


def test_my_own_public_session_is_flagged_own_and_public(folder):
    """En la base una publica no tiene dueño; que es MIA lo dice el indice maestro de esta PC."""
    seed(folder, MINE, user=USER, public=True)
    master_index.set_project_path(MINE, str(folder), title="x", windows_user_created=USER)
    (s,) = svc.folder_sessions(NEW, str(folder))["sessions"]
    assert (s["own"], s["public"]) == (True, True)


def test_the_current_session_is_never_listed(folder):
    seed(folder, NEW, user=OTHER, public=True)
    assert svc.folder_sessions(NEW, str(folder))["sessions"] == [] or all(
        s["id"] != NEW for s in svc.folder_sessions(NEW, str(folder))["sessions"]
    )


def test_an_empty_public_session_is_not_offered(folder):
    seed(folder, PUB, user=OTHER, public=True, messages=0)
    assert svc.folder_sessions(NEW, str(folder))["sessions"] == []


# ---------------------------------------------------------------------------------------------------------------------
# REQ-004: preflight
# ---------------------------------------------------------------------------------------------------------------------
def test_preflight_accepts_a_public_session_of_someone_else(folder):
    seed(folder, PUB, user=OTHER, public=True)
    svc.preflight_import(NEW, str(folder), PUB)  # no lanza


def test_preflight_still_rejects_a_private_session_of_someone_else_with_the_same_phrase(folder):
    seed(folder, PRIV, user=OTHER, public=False)
    with pytest.raises(ValueError, match="No se encontró esa conversación anterior"):
        svc.preflight_import(NEW, str(folder), PRIV)


def test_preflight_rejects_an_unflagged_session_without_owner_that_is_not_public(folder):
    """Sin dueño pero NO marcada publica (base vieja o a mano): no se ofrece. Solo `is_public = 1` abre la puerta."""
    path = seed(folder, PRIV, user=OTHER, public=False)
    conn = sqlite3.connect(path)
    conn.execute("UPDATE session SET windows_user_created = '' WHERE id = ?", (PRIV,))
    conn.commit()
    conn.close()
    with pytest.raises(ValueError, match="No se encontró esa conversación anterior"):
        svc.preflight_import(NEW, str(folder), PRIV)
    assert svc.folder_sessions(NEW, str(folder))["sessions"] == []


# ---------------------------------------------------------------------------------------------------------------------
# REQ-005 / REQ-006: el fork y la privacidad de lo ajeno
# ---------------------------------------------------------------------------------------------------------------------
def test_importing_a_public_session_copies_the_text_and_hides_the_outlook_and_teams_results(folder):
    path = seed(folder, PUB, user=OTHER, public=True, messages=4)
    assert svc.import_session_history(NEW, str(folder), PUB) == 4
    tools = {
        r[0]: json.loads(r[1]) for r in q(path, "SELECT id, data FROM part WHERE session_id = ? AND type = 'tool'", NEW)
    }
    by_name = {t["name"]: t for t in tools.values()}
    assert by_name[MAIL]["result"] == MARK
    assert by_name["m365_send_drafted_message"]["result"] == MARK
    assert by_name["grep"]["result"] == "salida de grep"
    assert "CONFIDENCIAL" not in json.dumps(q(path, "SELECT data FROM part WHERE session_id = ?", NEW))
    assert len(q(path, "SELECT 1 FROM part WHERE session_id = ? AND type = 'text'", NEW)) == 4


def test_the_tool_call_itself_is_kept_only_the_result_is_hidden(folder):
    path = seed(folder, PUB, user=OTHER, public=True)
    svc.import_session_history(NEW, str(folder), PUB)
    (data,) = q(path, "SELECT data FROM part WHERE session_id = ? AND data LIKE ?", NEW, f'%"{MAIL}"%')
    parsed = json.loads(data[0])
    assert parsed["name"] == MAIL and parsed["arguments"] == "{}" and parsed["result"] == MARK


def test_importing_my_own_session_copies_everything_including_outlook_results(folder):
    path = seed(folder, MINE, user=USER, messages=3)
    svc.import_session_history(NEW, str(folder), MINE)
    results = [
        json.loads(r[0]).get("result")
        for r in q(path, "SELECT data FROM part WHERE session_id = ? AND type = 'tool'", NEW)
    ]
    assert "CORREO CONFIDENCIAL de Marta" in results and MARK not in results


def test_my_own_PUBLIC_session_is_still_mine_so_it_copies_everything(folder):
    """Una sesion publica que yo mismo cree (sin dueño en la base) no se filtra al importarla en mi propia carpeta si el indice maestro dice que es mia."""
    path = seed(folder, MINE, user=USER, public=True, messages=3)
    master_index.set_project_path(MINE, str(folder), title="x", windows_user_created=USER)
    svc.import_session_history(NEW, str(folder), MINE)
    results = [
        json.loads(r[0]).get("result")
        for r in q(path, "SELECT data FROM part WHERE session_id = ? AND type = 'tool'", NEW)
    ]
    assert "CORREO CONFIDENCIAL de Marta" in results


def test_the_fork_belongs_to_the_importer_and_is_private(folder):
    path = seed(folder, PUB, user=OTHER, public=True)
    svc.import_session_history(NEW, str(folder), PUB)
    ((flag, owner),) = q(path, "SELECT is_public, windows_user_created FROM session WHERE id = ?", NEW)
    assert flag == 0 and owner.startswith(f"{USER}_")


def test_the_original_public_session_is_not_modified(folder):
    path = seed(folder, PUB, user=OTHER, public=True)
    before = {
        t: [r for r in rows if str(r[0]).startswith(PUB) or (t == "session" and r[0] == PUB)]
        for t, rows in dump(path).items()
    }
    svc.import_session_history(NEW, str(folder), PUB)
    after = {
        t: [r for r in rows if str(r[0]).startswith(PUB) or (t == "session" and r[0] == PUB)]
        for t, rows in dump(path).items()
    }
    assert before == after
    assert q(path, "SELECT is_public, windows_user_created FROM session WHERE id = ?", PUB) == [(1, "")]


# ---------------------------------------------------------------------------------------------------------------------
# REQ-007: el endpoint
# ---------------------------------------------------------------------------------------------------------------------
@pytest.fixture
def registered(folder):
    """La sesion MINE ya vive en `folder` y el indice maestro la tiene a mi nombre."""
    seed(folder, MINE, user=USER, messages=2)
    master_index.set_project_path(MINE, str(folder), title="Mia", windows_user_created=USER)
    get_session_store().delete(MINE)
    yield folder
    get_session_store().delete(MINE)


def test_the_owner_can_make_it_public_and_the_answer_says_so(registered, client):
    resp = client.post(f"/api/sessions/{MINE}/visibility", json={"public": True})
    assert resp.status_code == 200 and resp.json() == {"public": True}
    path = db.db_path_for(str(registered))
    assert q(path, "SELECT is_public, windows_user_created FROM session WHERE id = ?", MINE) == [(1, "")]


def test_the_owner_can_make_it_private_again(registered, client):
    client.post(f"/api/sessions/{MINE}/visibility", json={"public": True})
    resp = client.post(f"/api/sessions/{MINE}/visibility", json={"public": False})
    assert resp.status_code == 200 and resp.json() == {"public": False}
    ((flag, owner),) = q(
        db.db_path_for(str(registered)), "SELECT is_public, windows_user_created FROM session WHERE id = ?", MINE
    )
    assert flag == 0 and owner.startswith(f"{USER}_")


def test_someone_else_cannot_change_the_visibility_403(registered, client, monkeypatch):
    monkeypatch.setattr(svc, "current_windows_user", lambda: OTHER)
    resp = client.post(f"/api/sessions/{MINE}/visibility", json={"public": True})
    assert resp.status_code == 403
    assert q(db.db_path_for(str(registered)), "SELECT is_public FROM session WHERE id = ?", MINE) == [(0,)]


def test_without_a_folder_it_answers_400_with_a_sentence(client):
    resp = client.post("/api/sessions/sin-carpeta-xyz/visibility", json={"public": True})
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert isinstance(detail, str) and "carpeta" in detail.lower()


def test_the_visibility_can_be_read_back(registered, client):
    assert client.get(f"/api/sessions/{MINE}/visibility").json() == {"public": False}
    client.post(f"/api/sessions/{MINE}/visibility", json={"public": True})
    assert client.get(f"/api/sessions/{MINE}/visibility").json() == {"public": True}


def test_the_visibility_of_a_session_without_a_folder_reads_as_private(client):
    assert client.get("/api/sessions/otra-sin-carpeta-xyz/visibility").json() == {"public": False}


def test_a_session_created_with_a_public_flag_via_project_path_starts_public(folder, client):
    resp = client.post(
        f"/api/sessions/{NEW}/project-path", json={"path": str(folder), "title": "Nueva", "public": True}
    )
    assert resp.status_code == 200
    assert q(db.db_path_for(str(folder)), "SELECT is_public, windows_user_created FROM session WHERE id = ?", NEW) == [
        (1, "")
    ]


def test_without_the_flag_project_path_keeps_the_session_private(folder, client):
    resp = client.post(f"/api/sessions/{NEW}/project-path", json={"path": str(folder), "title": "Nueva"})
    assert resp.status_code == 200
    rows = q(db.db_path_for(str(folder)), "SELECT is_public FROM session WHERE id = ?", NEW)
    assert rows in ([], [(0,)])


def test_importing_a_public_session_through_the_endpoint_works(folder, client):
    seed(folder, PUB, user=OTHER, public=True, messages=4)
    resp = client.post(
        f"/api/sessions/{NEW}/project-path", json={"path": str(folder), "title": "Nueva", "import_from_session_id": PUB}
    )
    assert resp.status_code == 200 and resp.json()["imported_messages"] == 4


def test_importing_a_private_session_of_someone_else_through_the_endpoint_is_rejected(folder, client):
    seed(folder, PRIV, user=OTHER, public=False, messages=4)
    resp = client.post(
        f"/api/sessions/{NEW}/project-path",
        json={"path": str(folder), "title": "Nueva", "import_from_session_id": PRIV},
    )
    assert resp.status_code == 400
    assert master_index.get_project_path(NEW) is None


def test_a_session_just_assigned_to_a_folder_without_a_row_can_be_made_public(folder, client):
    """Cierra el mutante «sin ensure_session»: una sesion recien asignada a una carpeta todavia no hablo, asi que su fila no existe en la base;
    marcarla publica tiene que crearla, no fallar ni quedar privada en silencio."""
    master_index.set_project_path("sin-fila", str(folder), title="x", windows_user_created=USER)
    path = db.db_path_for(str(folder))
    assert not path.exists() or q(path, "SELECT 1 FROM session WHERE id = ?", "sin-fila") == []

    resp = client.post("/api/sessions/sin-fila/visibility", json={"public": True})

    assert resp.status_code == 200 and resp.json() == {"public": True}
    assert q(path, "SELECT is_public, windows_user_created FROM session WHERE id = ?", "sin-fila") == [(1, "")]
    assert client.get("/api/sessions/sin-fila/visibility").json() == {"public": True}
