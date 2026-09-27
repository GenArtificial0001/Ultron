"""QA-009: activacion de WAL sin `database is locked` cuando varios hilos abren
una base NUEVA a la vez (arranque del backend: purga + agentes + alarmas sobre
el indice maestro; hilo escritor + request HTTP sobre la base de un proyecto).
Antes del fix fallaba ~1 de cada 3 arranques con 6 hilos."""

from __future__ import annotations

import sqlite3
import threading

import pytest

from app.persistence import db, master_index, sqlite_wal

THREADS = 6
ROUNDS = 15


def _hammer(target) -> list[str]:
    barrier = threading.Barrier(THREADS)
    errors: list[str] = []

    def worker(i):
        barrier.wait()
        try:
            target(i)
        except Exception as exc:  # noqa: BLE001 -- se reporta cualquier fallo
            errors.append(repr(exc))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return errors


def test_concurrent_first_open_of_master_index_never_locks(tmp_path, monkeypatch):
    for r in range(ROUNDS):
        path = tmp_path / f"r{r}" / "vision.db"
        monkeypatch.setattr(master_index, "master_db_path", lambda p=path: p)
        errors = _hammer(lambda i: master_index.list_schedules() if i % 2 else master_index.list_all_for_user("u"))
        assert errors == [], f"ronda {r}: {errors[0]}"


def test_concurrent_first_open_of_project_db_never_locks(tmp_path):
    for r in range(ROUNDS):
        path = tmp_path / f"r{r}" / "db" / "vision.db"
        errors = _hammer(lambda i, p=path: db.ensure_session(p, f"s{i}", 1.0))
        assert errors == [], f"ronda {r}: {errors[0]}"
        assert len(db.list_sessions(path)) == THREADS  # y todas las escrituras llegaron


def test_database_ends_in_wal_mode(tmp_path):
    path = tmp_path / "db" / "vision.db"
    db.ensure_session(path, "s", 1.0)
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        conn.close()


class FakeConn:
    """Conexion minima: `PRAGMA journal_mode` devuelve el modo actual; el SET
    falla con los errores guionados antes de tener exito."""

    def __init__(self, mode="delete", set_errors=()):
        self.mode, self.set_errors, self.set_calls = mode, list(set_errors), 0

    def execute(self, sql):
        if sql == "PRAGMA journal_mode":
            return type("C", (), {"fetchone": lambda _s: (self.mode,)})()
        self.set_calls += 1
        if self.set_errors:
            raise self.set_errors.pop(0)
        self.mode = "wal"
        return None


def test_already_wal_is_not_switched_again():
    conn = FakeConn(mode="WAL")
    sqlite_wal.ensure_wal(conn)
    assert conn.set_calls == 0  # ni siquiera pide el lock exclusivo


def test_lock_held_by_another_process_is_retried(monkeypatch):
    monkeypatch.setattr(sqlite_wal, "_WAL_RETRY_SLEEP", 0.001)
    locked = sqlite3.OperationalError("database is locked")
    conn = FakeConn(set_errors=[locked, locked])
    sqlite_wal.ensure_wal(conn)
    assert conn.set_calls == 3 and conn.mode == "wal"


def test_gives_up_after_retry_window(monkeypatch):
    monkeypatch.setattr(sqlite_wal, "WAL_RETRY_SECONDS", 0.05)
    monkeypatch.setattr(sqlite_wal, "_WAL_RETRY_SLEEP", 0.005)
    conn = FakeConn(set_errors=[sqlite3.OperationalError("database is locked")] * 1000)
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        sqlite_wal.ensure_wal(conn)


def test_other_errors_are_not_retried():
    conn = FakeConn(set_errors=[sqlite3.OperationalError("disk I/O error")])
    with pytest.raises(sqlite3.OperationalError, match="disk I/O"):
        sqlite_wal.ensure_wal(conn)
    assert conn.set_calls == 1
