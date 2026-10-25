"""Spec aviso-eliminar-sesion-usuarios (0.36.2): el aviso de «Eliminar sesion» dice quien mas usa esa carpeta, SIN romper el aislamiento por usuario.

Pedido del usuario (2026-10-25): «cuando voy a eliminar una sesion que me muestre que usuarios y como se llama el nombre de sesion de cada usuario». Decision de
privacidad: mis sesiones con nombre; las PUBLICAS con titulo y quien las publico; las PRIVADAS de otras personas solo el usuario y la cantidad, nunca titulos.
Bases SQLite reales en `tmp_path`; el indice maestro va a un temporal por la fixture autouse `tmp_master_db`."""

from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import main
from app.persistence import db, master_index
from app.persistence import service as svc

ME = "jorge"
NOW = 1_800_000_000.0
DEL = "sesion-a-eliminar"


@pytest.fixture(autouse=True)
def fixed_user(monkeypatch):
    monkeypatch.setenv("USERNAME", ME)
    monkeypatch.setattr(svc, "current_windows_user", lambda: ME)


@pytest.fixture
def folder(tmp_path):
    f = tmp_path / "proyecto"
    f.mkdir()
    return f


@pytest.fixture
def client():
    return TestClient(main.app)


def make(folder, sid, *, owner=ME, public=False, published_by="", title="Charla", messages=2):
    path = db.db_path_for(str(folder))
    db.ensure_session(path, sid, NOW)
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "UPDATE session SET title = ?, title_manual = 1, windows_user_created = ?, is_public = ?, published_by = ? WHERE id = ?",
            (title, "" if public else f"{owner}_20260101000000", 1 if public else 0, published_by, sid),
        )
        conn.commit()
    finally:
        conn.close()
    for n in range(messages):
        db.insert_message(path, f"{sid}-m{n}", sid, "user", NOW + n)
        db.insert_part(path, f"{sid}-p{n}", f"{sid}-m{n}", sid, "text", {"text": f"texto {n}"}, NOW + n)
    return path


@pytest.fixture
def doomed(folder):
    """La sesion que se esta por eliminar: mia, en `folder`, registrada en el indice maestro."""
    make(folder, DEL, title="La que borro")
    master_index.set_project_path(DEL, str(folder), title="La que borro", windows_user_created=ME)
    return folder


def others(session_id=DEL):
    return svc.deletion_info(session_id)["others_in_folder"]


# ---------------------------------------------------------------------------------------------------------------------
# REQ-001 / REQ-002: mis sesiones
# ---------------------------------------------------------------------------------------------------------------------
def test_the_old_fields_are_untouched_and_the_new_one_is_added(doomed):
    info = svc.deletion_info(DEL)
    assert {"message_count", "project_path", "copies_elsewhere", "others_in_folder"} <= set(info)
    assert info["message_count"] == 2 and info["project_path"] == str(doomed)
    assert set(info["others_in_folder"]) == {"mine", "public", "private_by_user"}


def test_my_other_sessions_in_the_folder_are_listed_with_name_and_count(doomed):
    make(doomed, "otra-mia", title="Plan de stock", messages=5)
    mine = others()["mine"]
    assert [(s["id"], s["title"], s["message_count"]) for s in mine] == [("otra-mia", "Plan de stock", 5)]


def test_the_session_being_deleted_is_not_repeated(doomed):
    assert all(s["id"] != DEL for s in others()["mine"])


def test_my_sessions_are_ordered_by_recent_activity_and_empty_ones_are_skipped(doomed):
    make(doomed, "vieja", title="Vieja", messages=1)
    make(doomed, "reciente", title="Reciente", messages=1)
    make(doomed, "vacia", title="Vacia", messages=0)
    path = db.db_path_for(str(doomed))
    conn = sqlite3.connect(path)
    conn.execute("UPDATE session SET updated_at = ? WHERE id = 'vieja'", (NOW + 10,))
    conn.execute("UPDATE session SET updated_at = ? WHERE id = 'reciente'", (NOW + 900,))
    conn.commit()
    conn.close()
    assert [s["id"] for s in others()["mine"]] == ["reciente", "vieja"]


def test_the_list_of_my_sessions_has_a_cap_of_twenty(doomed):
    for n in range(25):
        make(doomed, f"mia-{n:02d}", title=f"S{n}", messages=1)
    assert len(others()["mine"]) == 20


# ---------------------------------------------------------------------------------------------------------------------
# REQ-003: publicas
# ---------------------------------------------------------------------------------------------------------------------
def test_public_sessions_show_title_and_who_published_them(doomed):
    make(doomed, "pub-m", public=True, published_by="marta", title="Del equipo", messages=4)
    (pub,) = others()["public"]
    assert (pub["title"], pub["published_by"], pub["message_count"]) == ("Del equipo", "marta", 4)


def test_an_old_public_session_without_label_says_unknown(doomed):
    make(doomed, "pub-vieja", public=True, published_by="", title="Vieja publica")
    assert others()["public"][0]["published_by"] == "desconocido"


def test_my_own_public_session_goes_with_mine_not_with_the_public_ones(doomed):
    make(doomed, "mia-publica", public=True, published_by=ME, title="Mia publica")
    # En la base una publica no tiene dueño: que es MIA lo dice el indice maestro de esta PC.
    master_index.set_project_path("mia-publica", str(doomed), title="x", windows_user_created=ME)
    info = others()
    assert [s["id"] for s in info["mine"]] == ["mia-publica"] and info["public"] == []


# ---------------------------------------------------------------------------------------------------------------------
# REQ-004: privadas de otros, SIN titulos
# ---------------------------------------------------------------------------------------------------------------------
def test_private_sessions_of_others_are_only_counted_per_user(doomed):
    make(doomed, "p1", owner="marta", title="SECRETO uno")
    make(doomed, "p2", owner="marta", title="SECRETO dos")
    make(doomed, "p3", owner="luis", title="SECRETO tres")
    grouped = {g["user"]: g["count"] for g in others()["private_by_user"]}
    assert grouped == {"marta": 2, "luis": 1}


def test_private_sessions_of_others_never_leak_titles_ids_or_message_counts(doomed):
    make(doomed, "p-secreta-id", owner="marta", title="TITULO PRIVADO DE MARTA", messages=7)
    blob = json.dumps(others(), ensure_ascii=False)
    assert "TITULO PRIVADO" not in blob and "p-secreta-id" not in blob
    (group,) = others()["private_by_user"]
    assert set(group) == {"user", "count"}


def test_private_groups_are_ordered_by_most_sessions_first(doomed):
    make(doomed, "a1", owner="ana")
    for n in range(3):
        make(doomed, f"b{n}", owner="beto")
    assert [g["user"] for g in others()["private_by_user"]] == ["beto", "ana"]


def test_the_http_endpoint_exposes_the_groups_and_does_not_leak_private_titles(doomed, client):
    """El pedido real del modal: `GET /api/session/{id}/agent-impact` (el que consulta el frontend) trae `others_in_folder` sin titulos privados."""
    make(doomed, "p1", owner="marta", title="TITULO PRIVADO DE MARTA")
    make(doomed, "pub", public=True, published_by="luis", title="Titulo publico")
    resp = client.get(f"/api/session/{DEL}/agent-impact")
    assert resp.status_code == 200
    data = resp.json()
    assert data["others_in_folder"]["private_by_user"] == [{"user": "marta", "count": 1}]
    assert [s["title"] for s in data["others_in_folder"]["public"]] == ["Titulo publico"]
    assert "TITULO PRIVADO" not in resp.text


# ---------------------------------------------------------------------------------------------------------------------
# REQ-005: mejor esfuerzo
# ---------------------------------------------------------------------------------------------------------------------
def test_without_a_folder_the_field_is_empty():
    assert svc.deletion_info("sin-carpeta-xyz")["others_in_folder"] == {"mine": [], "public": [], "private_by_user": []}


def test_an_unreadable_database_gives_an_empty_field_and_never_raises(doomed, monkeypatch):
    monkeypatch.setattr(db, "read_sessions_readonly", lambda p: (_ for _ in ()).throw(sqlite3.DatabaseError("rota")))
    assert svc.deletion_info(DEL)["others_in_folder"] == {"mine": [], "public": [], "private_by_user": []}


def test_a_folder_with_only_the_session_being_deleted_has_nothing_to_show(doomed):
    assert others() == {"mine": [], "public": [], "private_by_user": []}


# ---------------------------------------------------------------------------------------------------------------------
# Cierre de mutantes (6, 8, 13 y 15 de la corrida del 2026-10-25)
# ---------------------------------------------------------------------------------------------------------------------
def test_an_empty_public_session_is_not_listed(doomed):
    """Mutante «sin message_count en publicas»: una publica sin mensajes no aporta nada al aviso."""
    make(doomed, "pub-vacia", public=True, published_by="marta", title="Vacia", messages=0)
    make(doomed, "pub-llena", public=True, published_by="marta", title="Llena", messages=2)
    assert [s["title"] for s in others()["public"]] == ["Llena"]


def test_an_empty_private_session_of_someone_else_is_not_counted(doomed):
    """Mutante «sin message_count en privadas ajenas»: una privada vacia de otra persona no cuenta, y un usuario con solo vacias no aparece."""
    make(doomed, "p-vacia", owner="marta", title="x", messages=0)
    make(doomed, "p-llena", owner="luis", title="x", messages=2)
    assert others()["private_by_user"] == [{"user": "luis", "count": 1}]


def test_several_public_sessions_are_all_listed_up_to_the_cap(doomed):
    """Mutante «public[:1]» y mutante de tope: se listan todas hasta 20, no solo la primera."""
    for n in range(3):
        make(doomed, f"pub-{n}", public=True, published_by="marta", title=f"Publica {n}", messages=1)
    assert len(others()["public"]) == 3
    for n in range(3, 25):
        make(doomed, f"pub-{n}", public=True, published_by="marta", title=f"Publica {n}", messages=1)
    assert len(others()["public"]) == 20


def test_without_a_folder_the_database_is_never_even_opened(monkeypatch):
    """Mutante «sin la guarda de carpeta»: con folder vacio no se debe leer ninguna base (ni siquiera una que lance y se trague el error)."""
    calls: list[object] = []
    monkeypatch.setattr(db, "read_sessions_readonly", lambda p: calls.append(p) or [])
    assert svc._others_in_folder("x", None) == {"mine": [], "public": [], "private_by_user": []}
    assert svc._others_in_folder("x", "") == {"mine": [], "public": [], "private_by_user": []}
    assert calls == []
