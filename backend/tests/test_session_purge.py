"""Unit de la auto-purga de sesiones inactivas (`persistence/purge.py`,
REQ-007): nunca se borra sin respaldo exportado y un fallo no frena al resto.
Las capas `db`/`master_index`/`transcript` se stubean: se prueba la POLITICA."""

from __future__ import annotations

import asyncio
import time

import pytest

from app.persistence import purge


class FakeDb:
    """Registra borrados y sirve candidatas por carpeta de proyecto."""

    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.candidates: list[dict] = []
        self.deleted: list[str] = []
        self.cutoffs: list[float] = []

    def db_path_for(self, project_path):
        p = self.tmp / (str(project_path).replace("/", "_").replace("\\", "_").replace(":", "") + ".db")
        return p

    def sessions_older_than(self, db_path, cutoff):
        self.cutoffs.append(cutoff)
        return list(self.candidates)

    def delete_session(self, db_path, sid):
        self.deleted.append(sid)


@pytest.fixture
def fake(tmp_path, monkeypatch):
    f = FakeDb(tmp_path)
    monkeypatch.setattr(purge.db, "db_path_for", f.db_path_for)
    monkeypatch.setattr(purge.db, "sessions_older_than", f.sessions_older_than)
    monkeypatch.setattr(purge.db, "delete_session", f.delete_session)
    monkeypatch.setattr(purge.master_index, "delete_session", lambda sid: f.deleted.append(f"master:{sid}"))
    monkeypatch.setattr(purge, "export_session_to_file", lambda sid: f"/respaldo/{sid}.md")
    return f


def _create_db(fake, project="proj"):
    fake.db_path_for(project).write_text("")  # existe el archivo


def test_missing_db_returns_zero(fake):
    assert purge._purge_one_project_folder("sin-db") == 0
    assert fake.cutoffs == []


def test_cutoff_is_retention_days_ago(fake):
    _create_db(fake)
    purge._purge_one_project_folder("proj")
    expected = time.time() - purge.RETENTION_DAYS * 86400
    assert abs(fake.cutoffs[0] - expected) < 5


def test_exports_before_deleting_both_indexes(fake):
    _create_db(fake)
    fake.candidates = [{"id": "s1"}, {"id": "s2"}]
    assert purge._purge_one_project_folder("proj") == 2
    assert fake.deleted == ["s1", "master:s1", "s2", "master:s2"]


def test_no_backup_means_no_delete(fake, monkeypatch):
    _create_db(fake)
    fake.candidates = [{"id": "s1"}]
    monkeypatch.setattr(purge, "export_session_to_file", lambda sid: None)
    assert purge._purge_one_project_folder("proj") == 0
    assert fake.deleted == []  # regla critica: sin respaldo, no se borra


def test_one_failure_does_not_stop_the_rest(fake, monkeypatch):
    _create_db(fake)
    fake.candidates = [{"id": "malo"}, {"id": "bueno"}]

    def export(sid):
        if sid == "malo":
            raise OSError("disco lleno")
        return f"/r/{sid}.md"

    monkeypatch.setattr(purge, "export_session_to_file", export)
    assert purge._purge_one_project_folder("proj") == 1
    assert fake.deleted == ["bueno", "master:bueno"]


def test_purge_once_visits_each_distinct_project_once(fake, monkeypatch):
    monkeypatch.setattr(purge, "current_windows_user", lambda: "usuario1")
    monkeypatch.setattr(
        purge.master_index,
        "list_all_for_user",
        lambda user: [
            {"project_path": "A"},
            {"project_path": "A"},
            {"project_path": "B"},
            {"project_path": None},
        ],
    )
    visited: list[str] = []
    monkeypatch.setattr(purge, "_purge_one_project_folder", lambda p: visited.append(p) or 2)
    assert purge.purge_once() == 4
    assert sorted(visited) == ["A", "B"]


def test_purge_loop_survives_errors_and_repeats(monkeypatch):
    calls = {"purge": 0, "sleep": []}

    def boom():
        calls["purge"] += 1
        raise RuntimeError("db bloqueada")

    async def fake_sleep(s):
        calls["sleep"].append(s)
        if len(calls["sleep"]) == 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(purge, "purge_once", boom)
    monkeypatch.setattr(purge.asyncio, "sleep", fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(purge.run_purge_loop())
    assert calls["purge"] == 2 and calls["sleep"] == [purge.PURGE_INTERVAL_SECONDS] * 2
