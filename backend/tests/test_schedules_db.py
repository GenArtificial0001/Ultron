"""REQ-001 (spec bateria-qa-integracion): CRUD de la tabla `schedules` GLOBAL
(`master_index.py`), la que alimenta el programador de alarmas/pomodoro.
Patron DBManager: DB maestro redirigido a temp dir (fixture `tmp_master_db`),
nunca el `<carpeta de datos>\\vision.db` real del usuario.
"""

from __future__ import annotations

from app.persistence import master_index as mi


def test_create_and_list(tmp_master_db):
    s = mi.create_schedule(
        "s1",
        "Diaria 8:30",
        "recurring",
        {"rule": "daily", "time": "08:30"},
        "beep",
        "",
        True,
    )
    assert s["id"] == "s1"
    assert s["label"] == "Diaria 8:30"
    assert s["kind"] == "recurring"
    lst = mi.list_schedules()
    assert len(lst) == 1
    assert lst[0]["sound"] == "beep"
    assert lst[0]["enabled"] is True


def test_create_with_file_path(tmp_master_db):
    s = mi.create_schedule(
        "s1",
        "Con archivo",
        "once",
        {"target_iso": "2026-09-13T08:30"},
        "pulse",
        "C:/scripts/cierre.bat",
        True,
    )
    assert s["file_path"] == "C:/scripts/cierre.bat"


def test_update_schedule(tmp_master_db):
    mi.create_schedule(
        "s1", "A", "once", {"target_iso": "2026-09-13T08:30"}, "pulse", "", True
    )
    up = mi.update_schedule(
        "s1", {"label": "Renombrada", "sound": "double", "enabled": False}
    )
    assert up is not None
    assert up["label"] == "Renombrada"
    assert up["sound"] == "double"
    assert up["enabled"] is False


def test_update_missing_returns_none(tmp_master_db):
    assert mi.update_schedule("no-existe", {"label": "x"}) is None
    assert mi.get_schedule("no-existe") is None


def test_last_fired_and_delete(tmp_master_db):
    mi.create_schedule("s1", "A", "once", {"target_iso": "x"}, "beep", "", True)
    mi.update_schedule_last_fired("s1", 123.0)
    assert mi.get_schedule("s1")["last_fired_at"] == 123.0
    assert mi.delete_schedule("s1") is True
    assert mi.delete_schedule("s1") is False
    assert mi.list_schedules() == []
