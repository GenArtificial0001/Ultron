"""spec busqueda-mensajes REQ-007: la busqueda global recorre TODAS las carpetas de proyecto
del usuario (cada sesion vive en la suya), no solo la de la sesion abierta.

Sesiones e indice maestro reales en `tmp_path` (fixture `tmp_master_db`)."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app import main
from app.persistence import db, master_index
from app.persistence import service as persistence

WORD = "palabraclave"


@pytest.fixture
def client():
    return TestClient(main.app)


def make_session(tmp_path, sid, folder, text, *, at=None, owner=None):
    """Sesion `sid` en la carpeta `folder` con un mensaje que contiene `text`."""
    project = tmp_path / folder
    project.mkdir(exist_ok=True)
    user = owner or persistence.current_windows_user()
    master_index.set_project_path(sid, str(project), windows_user_created=f"{user}_20260930120000")
    dbp = db.db_path_for(str(project))
    now = at if at is not None else time.time()
    db.ensure_session(dbp, sid, now)
    if owner:
        # `ensure_session` siempre escribe al usuario de Windows ACTUAL: para simular una
        # sesion de otra persona se pisa la columna, igual que quedaria en una carpeta compartida.
        import sqlite3

        with sqlite3.connect(dbp) as conn:
            conn.execute("UPDATE session SET windows_user_created = ? WHERE id = ?", (f"{owner}_20260930120000", sid))
    db.insert_message(dbp, f"{sid}-m", sid, "user", now)
    db.insert_part(dbp, f"{sid}-t", f"{sid}-m", sid, "text", {"text": text}, now)
    return project


def test_finds_the_word_in_sessions_of_different_folders(tmp_path, tmp_master_db):
    make_session(tmp_path, "quimica", "Quimica", f"la {WORD} en quimica")
    make_session(tmp_path, "mate", "Matematica", f"otra {WORD} en matematica")
    make_session(tmp_path, "sin-palabra", "Otra", "nada que ver")

    found = persistence.search_all_sessions_for_current_user(WORD)

    assert {r["id"] for r in found} == {"quimica", "mate"}
    assert all(WORD in r["snippet"].lower() or "<" in r["snippet"] or r["snippet"] for r in found)


def test_a_folder_shared_by_two_sessions_is_searched_once_and_returns_both(tmp_path, tmp_master_db):
    make_session(tmp_path, "s1", "Comun", f"{WORD} uno")
    make_session(tmp_path, "s2", "Comun", f"{WORD} dos")

    found = persistence.search_all_sessions_for_current_user(WORD)

    assert sorted(r["id"] for r in found) == ["s1", "s2"]  # sin duplicados


def test_most_recent_sessions_come_first(tmp_path, tmp_master_db):
    make_session(tmp_path, "vieja", "A", f"{WORD}", at=1_000_000.0)
    make_session(tmp_path, "nueva", "B", f"{WORD}", at=2_000_000.0)
    make_session(tmp_path, "media", "C", f"{WORD}", at=1_500_000.0)

    found = persistence.search_all_sessions_for_current_user(WORD)

    assert [r["id"] for r in found] == ["nueva", "media", "vieja"]


def test_the_total_is_capped_across_folders(tmp_path, tmp_master_db):
    for i in range(6):
        make_session(tmp_path, f"s{i}", f"F{i}", WORD, at=1_000_000.0 + i)

    found = persistence.search_all_sessions_for_current_user(WORD, limit=4)

    assert len(found) == 4 and found[0]["id"] == "s5"  # las 4 mas recientes de TODAS las carpetas


def test_sessions_of_another_windows_user_are_not_returned(tmp_path, tmp_master_db):
    make_session(tmp_path, "mia", "Mia", WORD)
    make_session(tmp_path, "ajena", "Ajena", WORD, owner="otra.persona")

    found = persistence.search_all_sessions_for_current_user(WORD)

    assert [r["id"] for r in found] == ["mia"]


def test_a_folder_that_was_deleted_is_skipped_without_breaking_the_rest(tmp_path, tmp_master_db):
    make_session(tmp_path, "ok", "Buena", WORD)
    borrada = make_session(tmp_path, "perdida", "Borrada", WORD)
    import shutil

    shutil.rmtree(borrada)

    found = persistence.search_all_sessions_for_current_user(WORD)

    assert [r["id"] for r in found] == ["ok"]


def test_a_corrupt_database_is_skipped_without_breaking_the_rest(tmp_path, tmp_master_db):
    make_session(tmp_path, "ok", "Buena", WORD)
    rota = make_session(tmp_path, "rota", "Rota", WORD)
    db.db_path_for(str(rota)).write_bytes(b"esto no es una base de datos sqlite")

    found = persistence.search_all_sessions_for_current_user(WORD)

    assert [r["id"] for r in found] == ["ok"]


def test_no_results_when_nothing_matches(tmp_path, tmp_master_db):
    make_session(tmp_path, "s", "F", "hola")
    assert persistence.search_all_sessions_for_current_user(WORD) == []


def test_endpoint_is_global_when_the_session_has_a_folder(client, tmp_path, tmp_master_db):
    make_session(tmp_path, "actual", "Actual", "sin la palabra buscada")
    make_session(tmp_path, "otra", "Otra", f"aca esta la {WORD}")

    body = client.get("/api/sessions/search", params={"q": WORD, "session_id": "actual"}).json()

    assert body["persistence_available"] is True
    assert [r["id"] for r in body["results"]] == ["otra"]  # esta en OTRA carpeta, igual aparece


def test_endpoint_works_even_if_the_open_session_has_no_folder(client, tmp_path, tmp_master_db):
    # Caso real ("no me aparece la barra"): una "Nueva sesion" todavia no tiene carpeta, y aun
    # asi el usuario busca lo que dijo en sus otras sesiones. Antes respondia vacio SIN buscar.
    make_session(tmp_path, "otra", "Otra", f"aca esta la {WORD}")

    sin_id = client.get("/api/sessions/search", params={"q": WORD}).json()
    sesion_nueva = client.get("/api/sessions/search", params={"q": WORD, "session_id": "nueva-sin-carpeta"}).json()

    for body in (sin_id, sesion_nueva):
        assert body["persistence_available"] is True
        assert [r["id"] for r in body["results"]] == ["otra"]


def test_endpoint_keeps_the_degraded_contract_when_no_folder_exists_at_all(client, tmp_master_db):
    # Ninguna sesion con carpeta en todo el indice: no hay SQLite que consultar y el frontend
    # cae a localStorage (REQ-006).
    body = client.get("/api/sessions/search", params={"q": WORD}).json()
    assert body == {"results": [], "persistence_available": False}


def test_endpoint_with_an_empty_term_returns_nothing(client, tmp_path, tmp_master_db):
    make_session(tmp_path, "actual", "Actual", WORD)
    body = client.get("/api/sessions/search", params={"q": "   ", "session_id": "actual"}).json()
    assert body["results"] == [] and body["persistence_available"] is True
