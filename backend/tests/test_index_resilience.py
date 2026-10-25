"""Spec indice-resiliente-y-borrado-seguro (0.35.7): el indice maestro se puede reconstruir, se respalda cada dia y borrar una
sesion deja rastro y de verdad borra.

Todo en `tmp_path` con bases SQLite REALES (son baratas y asi se prueba el comportamiento, no un doble): el indice maestro va a
un temporal por la fixture autouse `tmp_master_db`. Nada de red. El tiempo se pasa por parametro (`now=`) para no depender del
reloj."""

from __future__ import annotations

import logging
import sqlite3
import time

import pytest

from app.persistence import db, master_index, purge
from app.persistence import index_recovery as rec
from app.persistence import service as svc

USER = "jorge"
OTHER = "marta"
DAY = 86_400.0
NOW = 1_800_000_000.0


@pytest.fixture(autouse=True)
def fixed_user(monkeypatch):
    monkeypatch.setenv("USERNAME", USER)
    monkeypatch.setattr(svc, "current_windows_user", lambda: USER)
    monkeypatch.setattr(purge, "current_windows_user", lambda: USER)
    monkeypatch.setattr(rec, "current_windows_user", lambda: USER)


@pytest.fixture(autouse=True)
def sync_writer(monkeypatch):
    monkeypatch.setattr(svc, "submit", lambda job: job())


def make_folder(tmp_path, name):
    folder = tmp_path / name
    folder.mkdir()
    return folder


def add_session(folder, sid, *, user=USER, at=NOW, messages=2, title=None):
    """Crea una sesion REAL en la base de `folder`, creada por `user`, con `messages` mensajes."""
    path = db.db_path_for(str(folder))
    db.ensure_session(path, sid, at)
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "UPDATE session SET windows_user_created = ?, updated_at = ?, title = ? WHERE id = ?",
            (f"{user}_20260101000000", at, title or f"titulo {sid}", sid),
        )
        conn.commit()
    finally:
        conn.close()
    for n in range(messages):
        db.insert_message(path, f"{sid}-m{n}", sid, "user", at + n)
        db.insert_part(path, f"{sid}-p{n}", f"{sid}-m{n}", sid, "text", {"text": f"mensaje secreto {sid} {n}"}, at + n)
    return path


def index_ids():
    return {e["session_id"] for e in master_index.list_all_for_user(USER)}


def raw_master(sql, *params):
    conn = sqlite3.connect(master_index.master_db_path())
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


# ---- REQ-002: lapidas -------------------------------------------------------------------------------------------------
def test_deleting_from_the_index_leaves_a_tombstone():
    master_index.set_project_path("s1", "/p", title="t", windows_user_created=USER)
    master_index.delete_session("s1")
    assert master_index.is_tombstoned("s1") is True
    assert master_index.is_tombstoned("otra") is False


def test_registering_a_session_removes_its_tombstone():
    master_index.set_project_path("s1", "/p", windows_user_created=USER)
    master_index.delete_session("s1")
    master_index.set_project_path("s1", "/p2", windows_user_created=USER)
    assert master_index.is_tombstoned("s1") is False


def test_a_second_delete_does_not_duplicate_the_tombstone():
    master_index.set_project_path("s1", "/p", windows_user_created=USER)
    master_index.delete_session("s1")
    master_index.delete_session("s1")
    assert raw_master("SELECT COUNT(*) FROM deleted_sessions WHERE session_id = 's1'")[0][0] == 1


# ---- REQ-003: carpetas conocidas --------------------------------------------------------------------------------------
def test_a_folder_is_remembered_after_its_sessions_are_deleted():
    master_index.set_project_path("s1", "/carpeta-a", windows_user_created=USER)
    master_index.delete_session("s1")
    assert "/carpeta-a" in master_index.known_folders()


def test_known_folders_joins_the_index_and_the_remembered_ones_without_repeats():
    master_index.set_project_path("s1", "/a", windows_user_created=USER)
    master_index.set_project_path("s2", "/a", windows_user_created=USER)
    master_index.set_project_path("s3", "/b", windows_user_created=USER)
    master_index.delete_session("s3")
    assert sorted(master_index.known_folders()) == ["/a", "/b"]


def test_an_empty_path_is_never_remembered():
    master_index.set_project_path("s1", "", windows_user_created=USER)
    assert master_index.known_folders() == []


# ---- REQ-004/005/006: re-escaneo --------------------------------------------------------------------------------------
def test_rescan_registers_an_orphan_of_a_known_folder(tmp_path):
    folder = make_folder(tmp_path, "proyecto")
    add_session(folder, "huerfana", at=NOW - DAY)
    master_index.set_project_path("otra", str(folder), windows_user_created=USER)  # la carpeta ya es conocida
    assert rec.rescan_index(now=NOW) == ["huerfana"]
    assert "huerfana" in index_ids()
    entry = next(e for e in master_index.list_all_for_user(USER) if e["session_id"] == "huerfana")
    assert entry["project_path"] == str(folder) and entry["title"] == "titulo huerfana"


def test_rescan_records_the_owner_without_the_timestamp(tmp_path):
    folder = make_folder(tmp_path, "proyecto")
    add_session(folder, "s1", at=NOW - DAY)
    master_index.set_project_path("otra", str(folder), windows_user_created=USER)
    rec.rescan_index(now=NOW)
    assert master_index.get_owner("s1") == USER


def test_rescan_finds_a_folder_whose_only_session_was_deleted(tmp_path):
    folder = make_folder(tmp_path, "proyecto")
    master_index.set_project_path("vieja", str(folder), windows_user_created=USER)
    master_index.delete_session("vieja")  # el indice ya no tiene la carpeta, known_folders si
    add_session(folder, "s1", at=NOW - DAY)
    assert rec.rescan_index(now=NOW) == ["s1"]


def test_rescan_does_not_touch_a_session_already_in_the_index(tmp_path):
    folder = make_folder(tmp_path, "proyecto")
    add_session(folder, "s1", at=NOW - DAY)
    master_index.set_project_path("s1", "/ruta-que-el-usuario-eligio", windows_user_created=USER)
    master_index.set_project_path("otra", str(folder), windows_user_created=USER)
    assert rec.rescan_index(now=NOW) == []
    assert master_index.get_project_path("s1") == "/ruta-que-el-usuario-eligio"


def test_rescan_never_resurrects_a_deleted_session(tmp_path):
    folder = make_folder(tmp_path, "proyecto")
    add_session(folder, "borrada", at=NOW - DAY)  # sigue en la base (el borrado no pudo limpiarla)
    master_index.set_project_path("borrada", str(folder), windows_user_created=USER)
    master_index.delete_session("borrada")
    assert rec.rescan_index(now=NOW) == []
    assert "borrada" not in index_ids()


def test_rescan_ignores_sessions_of_another_windows_user(tmp_path):
    folder = make_folder(tmp_path, "compartida")
    add_session(folder, "ajena", user=OTHER, at=NOW - DAY)
    add_session(folder, "mia", at=NOW - DAY)
    master_index.set_project_path("otra", str(folder), windows_user_created=USER)
    assert rec.rescan_index(now=NOW) == ["mia"]
    assert "ajena" not in index_ids()


def test_rescan_ignores_a_user_whose_name_only_starts_like_mine(tmp_path):
    folder = make_folder(tmp_path, "compartida")
    add_session(folder, "de-jorge2", user="jorge2", at=NOW - DAY)
    master_index.set_project_path("otra", str(folder), windows_user_created=USER)
    assert rec.rescan_index(now=NOW) == []


def test_rescan_skips_sessions_older_than_the_retention(tmp_path):
    folder = make_folder(tmp_path, "proyecto")
    add_session(folder, "vieja", at=NOW - (purge.RETENTION_DAYS + 1) * DAY)
    add_session(folder, "justa", at=NOW - (purge.RETENTION_DAYS - 1) * DAY)
    master_index.set_project_path("otra", str(folder), windows_user_created=USER)
    assert rec.rescan_index(now=NOW) == ["justa"]


def test_rescan_picks_the_most_recent_copy_when_an_id_is_in_two_folders(tmp_path):
    vieja = make_folder(tmp_path, "vieja")
    nueva = make_folder(tmp_path, "nueva")
    add_session(vieja, "dup", at=NOW - 10 * DAY, messages=5)
    add_session(nueva, "dup", at=NOW - DAY, messages=1)
    master_index.set_project_path("a", str(vieja), windows_user_created=USER)
    master_index.set_project_path("b", str(nueva), windows_user_created=USER)
    assert rec.rescan_index(now=NOW) == ["dup"]
    assert master_index.get_project_path("dup") == str(nueva)


def test_rescan_skips_a_folder_without_database_or_with_a_broken_one(tmp_path):
    sin_base = make_folder(tmp_path, "sin-base")
    rota = make_folder(tmp_path, "rota")
    (rota / "db").mkdir()
    (rota / "db" / "vision.db").write_bytes(b"esto no es sqlite " * 50)
    buena = make_folder(tmp_path, "buena")
    add_session(buena, "ok", at=NOW - DAY)
    for n, f in enumerate((sin_base, rota, buena)):
        master_index.set_project_path(f"x{n}", str(f), windows_user_created=USER)
    assert rec.rescan_index(now=NOW) == ["ok"]


def test_rescan_skips_a_folder_that_no_longer_exists(tmp_path):
    master_index.set_project_path("x", str(tmp_path / "se-borro"), windows_user_created=USER)
    assert rec.rescan_index(now=NOW) == []


def test_rescan_is_idempotent_and_never_writes_in_a_session_database(tmp_path):
    folder = make_folder(tmp_path, "proyecto")
    path = add_session(folder, "s1", at=NOW - DAY)
    master_index.set_project_path("otra", str(folder), windows_user_created=USER)
    before = path.read_bytes()
    assert rec.rescan_index(now=NOW) == ["s1"]
    assert rec.rescan_index(now=NOW) == []
    assert path.read_bytes() == before


def test_rescan_does_not_register_a_session_without_title_as_blank(tmp_path):
    folder = make_folder(tmp_path, "proyecto")
    add_session(folder, "s1", at=NOW - DAY, title="")
    master_index.set_project_path("otra", str(folder), windows_user_created=USER)
    rec.rescan_index(now=NOW)
    entry = next(e for e in master_index.list_all_for_user(USER) if e["session_id"] == "s1")
    assert entry["title"] != ""


# ---- REQ-007: copia diaria del indice ---------------------------------------------------------------------------------
def backups(folder):
    return sorted(p.name for p in folder.glob("vision-indice-*.db"))


def test_backup_creates_a_consistent_dated_copy(tmp_path):
    master_index.set_project_path("s1", "/p", title="uno", windows_user_created=USER)
    dest = tmp_path / "backups"
    made = rec.backup_index(dest, now=NOW)
    assert made is not None and made.name == time.strftime("vision-indice-%Y%m%d.db", time.localtime(NOW))
    copy = sqlite3.connect(made)
    try:
        assert copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert copy.execute("SELECT session_id FROM session_index").fetchall() == [("s1",)]
    finally:
        copy.close()


def test_backup_is_not_repeated_the_same_day(tmp_path):
    master_index.set_project_path("s1", "/p", windows_user_created=USER)
    dest = tmp_path / "backups"
    first = rec.backup_index(dest, now=NOW)
    master_index.set_project_path("s2", "/p", windows_user_created=USER)
    assert rec.backup_index(dest, now=NOW + 3600) is None
    copy = sqlite3.connect(first)
    try:
        assert copy.execute("SELECT COUNT(*) FROM session_index").fetchone()[0] == 1  # no se pisó
    finally:
        copy.close()


def test_backup_keeps_only_the_seven_most_recent(tmp_path):
    master_index.set_project_path("s1", "/p", windows_user_created=USER)
    dest = tmp_path / "backups"
    for day in range(10):
        rec.backup_index(dest, now=NOW + day * DAY)
    kept = backups(dest)
    assert len(kept) == 7
    expected = sorted(time.strftime("vision-indice-%Y%m%d.db", time.localtime(NOW + d * DAY)) for d in range(3, 10))
    assert kept == expected


def test_backup_never_touches_files_it_did_not_create(tmp_path):
    master_index.set_project_path("s1", "/p", windows_user_created=USER)
    dest = tmp_path / "backups"
    dest.mkdir()
    manual = dest / "vision-indice-20261022-215013-antes-de-reregistrar.db"
    manual.write_bytes(b"copia manual")
    otra = dest / "notas.txt"
    otra.write_text("hola")
    for day in range(10):
        rec.backup_index(dest, now=NOW + day * DAY)
    assert manual.read_bytes() == b"copia manual" and otra.read_text() == "hola"


def test_backup_leaves_no_temporary_file_behind(tmp_path):
    master_index.set_project_path("s1", "/p", windows_user_created=USER)
    dest = tmp_path / "backups"
    rec.backup_index(dest, now=NOW)
    assert [p.name for p in dest.iterdir() if not p.name.startswith("vision-indice-")] == []
    assert not list(dest.glob("*.tmp"))


def test_a_failed_backup_returns_none_and_does_not_raise(tmp_path, monkeypatch, caplog):
    blocker = tmp_path / "backups"
    blocker.write_text("soy un archivo, no una carpeta")  # no se puede crear la carpeta de copias
    with caplog.at_level(logging.WARNING):
        assert rec.backup_index(blocker, now=NOW) is None
    assert any("copia" in r.message.lower() for r in caplog.records)


# ---- REQ-008: el ciclo de arranque ------------------------------------------------------------------------------------
def test_the_purge_loop_runs_backup_then_rescan_then_purge_in_that_order(monkeypatch):
    order: list[str] = []
    monkeypatch.setattr(purge.index_recovery, "backup_index", lambda *a, **k: order.append("copia"))
    monkeypatch.setattr(purge.index_recovery, "rescan_index", lambda *a, **k: order.append("rescan") or [])
    monkeypatch.setattr(purge, "purge_once", lambda: order.append("purga") or 0)

    import asyncio

    async def stop(_):
        raise asyncio.CancelledError

    monkeypatch.setattr(purge.asyncio, "sleep", stop)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(purge.run_purge_loop())
    assert order == ["copia", "rescan", "purga"]


@pytest.mark.parametrize("failing", ["backup_index", "rescan_index"])
def test_a_failure_in_one_step_does_not_stop_the_next(monkeypatch, failing):
    order: list[str] = []

    def step(name):
        def run(*a, **k):
            order.append(name)
            if name == failing:
                raise RuntimeError("fallo simulado")
            return []

        return run

    monkeypatch.setattr(purge.index_recovery, "backup_index", step("backup_index"))
    monkeypatch.setattr(purge.index_recovery, "rescan_index", step("rescan_index"))
    monkeypatch.setattr(purge, "purge_once", lambda: order.append("purge_once") or 0)

    import asyncio

    async def stop(_):
        raise asyncio.CancelledError

    monkeypatch.setattr(purge.asyncio, "sleep", stop)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(purge.run_purge_loop())
    assert order == ["backup_index", "rescan_index", "purge_once"]


def test_a_rescan_after_a_purge_does_not_bring_back_what_the_purge_deleted(tmp_path):
    folder = make_folder(tmp_path, "proyecto")
    add_session(folder, "vieja", at=NOW - 31 * DAY)
    master_index.set_project_path("vieja", str(folder), windows_user_created=USER)
    master_index.delete_session("vieja")
    assert rec.rescan_index(now=NOW) == []


# ---- REQ-001: el borrado deja rastro en el log ------------------------------------------------------------------------
def test_deleting_a_session_logs_id_title_folder_and_message_count(tmp_path, caplog):
    folder = make_folder(tmp_path, "proyecto")
    add_session(folder, "s1", messages=3, title="Mi sesion")
    svc.assign_project_path("s1", str(folder), "Mi sesion")
    with caplog.at_level(logging.INFO, logger="app.persistence.service"):
        svc.delete_session("s1")
    line = next(r.getMessage() for r in caplog.records if "borrada" in r.getMessage().lower())
    assert "s1" in line and "Mi sesion" in line and str(folder) in line and "3" in line


def test_the_delete_log_never_contains_message_text(tmp_path, caplog):
    folder = make_folder(tmp_path, "proyecto")
    add_session(folder, "s1", messages=2)
    svc.assign_project_path("s1", str(folder), "t")
    with caplog.at_level(logging.DEBUG):
        svc.delete_session("s1")
    assert "mensaje secreto" not in caplog.text


def test_deleting_a_session_without_folder_still_logs_and_tombstones(caplog):
    with caplog.at_level(logging.INFO, logger="app.persistence.service"):
        svc.delete_session("sin-carpeta")
    line = next(r.getMessage() for r in caplog.records if "borrada" in r.getMessage().lower())
    assert "sin-carpeta" in line and "sin carpeta" in line.lower()
    assert master_index.is_tombstoned("sin-carpeta") is True


def test_deleting_with_an_unreadable_database_still_logs_and_does_not_raise(tmp_path, caplog, monkeypatch):
    from app.persistence import writer

    monkeypatch.setattr(svc, "submit", writer.submit)  # el hilo escritor REAL: cada trabajo falla o sigue por separado
    folder = make_folder(tmp_path, "proyecto")
    (folder / "db").mkdir()
    (folder / "db" / "vision.db").write_bytes(b"basura " * 100)
    master_index.set_project_path("s1", str(folder), title="x", windows_user_created=USER)
    with caplog.at_level(logging.INFO):
        svc.delete_session("s1")  # no debe lanzar
        writer.flush()
    assert any("borrada" in r.getMessage().lower() for r in caplog.records)
    assert master_index.get_project_path("s1") is None
    assert master_index.is_tombstoned("s1") is True


def test_the_purge_also_logs_and_tombstones(tmp_path, monkeypatch, caplog):
    folder = make_folder(tmp_path, "proyecto")
    add_session(folder, "vieja", at=time.time() - 40 * DAY, messages=4)  # hora REAL: la purga corta con time.time()
    master_index.set_project_path("vieja", str(folder), title="Vieja", windows_user_created=USER)
    monkeypatch.setattr(purge, "export_session_to_file", lambda sid: "/respaldo.md")
    with caplog.at_level(logging.INFO):
        assert purge._purge_one_project_folder(str(folder)) == 1
    assert any(
        "borrada" in r.getMessage().lower() and "vieja" in r.getMessage() and "4" in r.getMessage()
        for r in caplog.records
    )
    assert master_index.is_tombstoned("vieja") is True


# ---- REQ-009: el borrado compacta el archivo --------------------------------------------------------------------------
def big_session(folder, sid, n=200):
    path = db.db_path_for(str(folder))
    db.ensure_session(path, sid, NOW)
    for i in range(n):
        db.insert_message(path, f"{sid}-m{i}", sid, "user", NOW + i)
        db.insert_part(path, f"{sid}-p{i}", f"{sid}-m{i}", sid, "text", {"text": f"palabra-unica-{sid} " * 60}, NOW + i)
    return path


def test_deleting_a_session_shrinks_the_file_and_removes_its_text(tmp_path):
    folder = make_folder(tmp_path, "proyecto")
    path = big_session(folder, "grande")
    db.ensure_session(path, "chica", NOW)
    size_before = path.stat().st_size
    assert b"palabra-unica-grande" in path.read_bytes()
    assert db.delete_session(path, "grande") is True
    assert path.stat().st_size < size_before / 2
    assert b"palabra-unica-grande" not in path.read_bytes()
    assert db.session_exists(path, "chica") is True  # lo que no se borra, sigue


def test_deleting_leaves_no_wal_residue_with_the_text(tmp_path):
    folder = make_folder(tmp_path, "proyecto")
    path = big_session(folder, "grande")
    db.delete_session(path, "grande")
    wal = path.with_name(path.name + "-wal")
    assert not wal.exists() or b"palabra-unica-grande" not in wal.read_bytes()


def test_the_delete_stands_even_if_compacting_fails(tmp_path, monkeypatch, caplog):
    folder = make_folder(tmp_path, "proyecto")
    path = big_session(folder, "grande", n=5)

    def boom(_path):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(db, "compact_database", boom)
    with caplog.at_level(logging.WARNING):
        assert db.delete_session(path, "grande") is True  # el borrado vale
    assert db.session_exists(path, "grande") is False
    assert any("compact" in r.getMessage().lower() for r in caplog.records)


def test_deleting_a_session_that_does_not_exist_does_not_compact(tmp_path, monkeypatch):
    folder = make_folder(tmp_path, "proyecto")
    path = big_session(folder, "x", n=1)
    called = []
    monkeypatch.setattr(db, "compact_database", lambda p: called.append(p))
    assert db.delete_session(path, "no-existe") is False
    assert called == []


# ---- REQ-010: agent-impact trae lo que el modal necesita --------------------------------------------------------------
def test_impact_info_counts_messages_and_names_the_folder(tmp_path):
    folder = make_folder(tmp_path, "proyecto")
    add_session(folder, "s1", messages=5)
    svc.assign_project_path("s1", str(folder), "t")
    info = svc.deletion_info("s1")
    assert info["message_count"] == 5 and info["project_path"] == str(folder) and info["copies_elsewhere"] == []


def test_impact_info_for_a_session_without_folder_is_empty():
    info = svc.deletion_info("nada")
    # spec aviso-eliminar-sesion-usuarios: `others_in_folder` es un campo ADITIVO (vacio sin carpeta); los de siempre no cambian.
    assert {k: info[k] for k in ("message_count", "project_path", "copies_elsewhere")} == {
        "message_count": 0,
        "project_path": "",
        "copies_elsewhere": [],
    }
    assert info["others_in_folder"] == {"mine": [], "public": [], "private_by_user": []}


def test_impact_info_with_an_unreadable_database_does_not_raise(tmp_path):
    folder = make_folder(tmp_path, "proyecto")
    (folder / "db").mkdir()
    (folder / "db" / "vision.db").write_bytes(b"basura " * 100)
    master_index.set_project_path("s1", str(folder), windows_user_created=USER)
    info = svc.deletion_info("s1")
    assert info["message_count"] == 0 and info["project_path"] == str(folder)


def test_impact_info_lists_the_other_folder_that_has_the_same_id(tmp_path):
    a = make_folder(tmp_path, "a")
    b = make_folder(tmp_path, "b")
    add_session(a, "dup", messages=1)
    add_session(b, "dup", messages=9)
    master_index.set_project_path("dup", str(a), windows_user_created=USER)
    master_index.set_project_path("otra", str(b), windows_user_created=USER)  # b es una carpeta conocida
    info = svc.deletion_info("dup")
    assert info["copies_elsewhere"] == [{"project_path": str(b), "message_count": 9}]


def test_impact_info_does_not_count_the_own_folder_as_a_copy(tmp_path):
    a = make_folder(tmp_path, "a")
    add_session(a, "s1", messages=2)
    master_index.set_project_path("s1", str(a), windows_user_created=USER)
    assert svc.deletion_info("s1")["copies_elsewhere"] == []


def test_impact_info_ignores_a_copy_of_another_windows_user(tmp_path):
    a = make_folder(tmp_path, "a")
    b = make_folder(tmp_path, "b")
    add_session(a, "dup", messages=1)
    add_session(b, "dup", user=OTHER, messages=3)
    master_index.set_project_path("dup", str(a), windows_user_created=USER)
    master_index.set_project_path("otra", str(b), windows_user_created=USER)
    assert svc.deletion_info("dup")["copies_elsewhere"] == []


# ---- REQ-010: el endpoint ---------------------------------------------------------------------------------------------
class TestAgentImpactEndpoint:
    """`GET /api/session/{id}/agent-impact` suma los campos del modal SIN cambiar los que ya tenia."""

    @pytest.fixture
    def client(self, monkeypatch):
        from fastapi.testclient import TestClient

        from app import main

        monkeypatch.setattr(main, "_assert_session_owner", lambda session_id: None)
        return TestClient(main.app)

    def test_it_keeps_the_old_fields_and_adds_the_new_ones(self, client, tmp_path):
        folder = make_folder(tmp_path, "proyecto")
        add_session(folder, "s1", messages=4)
        master_index.set_project_path("s1", str(folder), windows_user_created=USER)
        body = client.get("/api/session/s1/agent-impact").json()
        for old in ("agent_count", "is_fallback_session", "is_fallback_blocked", "fallback_session_id", "will_reassign_to_fallback"):
            assert old in body
        assert body["message_count"] == 4 and body["project_path"] == str(folder) and body["copies_elsewhere"] == []

    def test_a_session_without_folder_answers_with_empty_values(self, client):
        body = client.get("/api/session/no-existe/agent-impact").json()
        assert body["message_count"] == 0 and body["project_path"] == "" and body["copies_elsewhere"] == []
        assert body["agent_count"] == 0

    def test_a_failure_building_the_summary_does_not_break_the_agent_check(self, client, monkeypatch):
        def boom(_sid):
            raise RuntimeError("fallo simulado")

        monkeypatch.setattr(svc, "deletion_info", boom)
        r = client.get("/api/session/s1/agent-impact")
        assert r.status_code == 200
        body = r.json()
        assert body["agent_count"] == 0 and body["message_count"] == 0


# ---- huecos cerrados tras las mutaciones ------------------------------------------------------------------------------
def test_deleting_remembers_the_folder_of_a_session_registered_before_this_feature(tmp_path):
    """Una fila escrita por una version ANTERIOR (sin `known_folders`): al borrarla, su carpeta no se pierde de vista."""
    folder = make_folder(tmp_path, "vieja")
    conn = sqlite3.connect(master_index.master_db_path())
    try:
        conn.executescript(master_index._SCHEMA)  # noqa: SLF001 - crea las tablas sin pasar por set_project_path
        conn.execute(
            "INSERT INTO session_index (session_id, project_path, title, windows_user_created, created_at, updated_at) "
            "VALUES ('antigua', ?, 't', ?, 1, 1)",
            (str(folder), USER),
        )
        conn.commit()
    finally:
        conn.close()
    assert str(folder) not in [r[0] for r in raw_master("SELECT project_path FROM known_folders")]
    master_index.delete_session("antigua")
    assert str(folder) in master_index.known_folders()


def test_a_copy_that_fails_the_integrity_check_is_discarded(tmp_path, monkeypatch, caplog):
    master_index.set_project_path("s1", "/p", windows_user_created=USER)
    monkeypatch.setattr(rec, "_integrity_ok", lambda conn: False)
    dest = tmp_path / "backups"
    with caplog.at_level(logging.WARNING):
        assert rec.backup_index(dest, now=NOW) is None
    assert backups(dest) == [] and not list(dest.glob("*.tmp"))
    assert any("integridad" in (r.getMessage() + str(r.exc_info)).lower() for r in caplog.records)


def test_the_integrity_check_accepts_a_good_database_and_rejects_a_damaged_one(tmp_path):
    good = sqlite3.connect(":memory:")
    good.execute("CREATE TABLE t (a)")
    assert rec._integrity_ok(good) is True  # noqa: SLF001
    bad = tmp_path / "danada.db"
    seed = sqlite3.connect(bad)
    seed.execute("CREATE TABLE t (a TEXT)")
    seed.executemany("INSERT INTO t VALUES (?)", [("x" * 500,)] * 200)
    seed.execute("CREATE INDEX i ON t(a)")
    seed.commit()
    seed.close()
    data = bytearray(bad.read_bytes())
    for k in range(4096 * 2, len(data) - 1, 97):  # ensucia el contenido de las paginas de datos, no la cabecera
        data[k] ^= 0xFF
    bad.write_bytes(bytes(data))
    conn = sqlite3.connect(bad)
    try:
        try:
            assert rec._integrity_ok(conn) is False  # noqa: SLF001
        except sqlite3.DatabaseError:
            pass  # una base muy danada tambien puede fallar al verificarla: backup_index lo trata como copia mala
    finally:
        conn.close()


def test_the_delete_log_says_unknown_when_the_messages_could_not_be_counted(tmp_path, caplog, monkeypatch):
    from app.persistence import writer

    monkeypatch.setattr(svc, "submit", writer.submit)
    folder = make_folder(tmp_path, "proyecto")
    (folder / "db").mkdir()
    (folder / "db" / "vision.db").write_bytes(b"basura " * 100)
    master_index.set_project_path("s1", str(folder), title="x", windows_user_created=USER)
    with caplog.at_level(logging.INFO):
        svc.delete_session("s1")
        writer.flush()
    line = next(r.getMessage() for r in caplog.records if "borrada" in r.getMessage().lower())
    assert "mensajes=?" in line
