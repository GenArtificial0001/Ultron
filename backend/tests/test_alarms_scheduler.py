"""Unit del reloj de alarmas en backend (`scheduler/alarms.py`). Se prueba la
LOGICA de disparo (quien vence, snooze, pomodoro, aislamiento de fallos);
el sonido/toast nativos de Windows (`win11toast`, `notifier`, `os.startfile`)
son fakes: nunca suena nada ni se abre ningun archivo."""

from __future__ import annotations

import asyncio
import sys
import types

import pytest

from app import notifier
from app.scheduler import alarms
from app.tools import base as tool_base


def _sched(sid="a1", **over):
    base = {"id": sid, "label": "ZCC", "kind": "alarm", "enabled": True, "sound": "beep", "file_path": ""}
    base.update(over)
    return base


@pytest.fixture
def env(monkeypatch):
    """Instala fakes de toast/alerta/auditoria y devuelve el registro de llamadas."""
    calls = {"toast": [], "alert": [], "audit": [], "last_fired": [], "toast_exc": None}

    def notify(**kw):
        if calls["toast_exc"]:
            raise calls["toast_exc"]
        calls["toast"].append(kw)

    monkeypatch.setitem(sys.modules, "win11toast", types.SimpleNamespace(notify=notify))
    monkeypatch.setattr(notifier, "show_alert", lambda *a: calls["alert"].append(a))
    monkeypatch.setattr(tool_base, "_audit", lambda *a: calls["audit"].append(a))
    monkeypatch.setattr(alarms, "get_snooze_minutes", lambda: 5)
    monkeypatch.setattr(alarms, "wav_path", lambda s: f"/wav/{s}.wav")
    monkeypatch.setattr(alarms.mi, "update_schedule_last_fired", lambda sid, ts: calls["last_fired"].append(sid))
    monkeypatch.setattr(notifier, "pop_due_snoozes", lambda kind, now: [])
    return calls


def test_fire_alarm_shows_alert_and_toast(env):
    alarms.fire_alarm(_sched(label="Cafe", sound="pulse"))
    assert env["alert"] == [("alarm", "a1", "Cafe", "/wav/pulse.wav", 5)]
    assert env["toast"][0]["title"].endswith("Cafe")
    assert env["audit"] == []  # sin archivo asociado


def test_fire_alarm_defaults_for_missing_label_and_sound(env):
    alarms.fire_alarm({"id": "x"})
    assert env["alert"][0][2:4] == ("(sin etiqueta)", "/wav/beep.wav")


def test_toast_failure_does_not_block_alert_or_file(env, monkeypatch):
    env["toast_exc"] = RuntimeError("sin AppUserModelID")
    started: list[str] = []
    monkeypatch.setattr("os.startfile", started.append, raising=False)
    alarms.fire_alarm(_sched(file_path=" C:/reporte.xlsx "))
    assert env["alert"] and started == ["C:/reporte.xlsx"]
    assert env["audit"][0][1]["status"] == "ok"


def test_file_open_error_is_audited_not_raised(env, monkeypatch):
    def boom(path):
        raise OSError("no existe")

    monkeypatch.setattr("os.startfile", boom, raising=False)
    alarms.fire_alarm(_sched(file_path="C:/nada.txt"))
    action, payload = env["audit"][0]
    assert action == "run_file" and payload["status"] == "error" and "no existe" in payload["error"]
    assert payload["source"] == "alarm:a1"


def test_due_alarm_fires_and_records_last_fired(env, monkeypatch):
    monkeypatch.setattr(alarms.mi, "list_schedules", lambda: [_sched("a1"), _sched("a2")])
    monkeypatch.setattr(alarms, "is_due_now", lambda s, now: s["id"] == "a1")
    alarms._check_and_fire_due_alarms()
    assert [a[1] for a in env["alert"]] == ["a1"]
    assert env["last_fired"] == ["a1"]


def test_disabled_and_pomodoro_never_fire(env, monkeypatch):
    monkeypatch.setattr(
        alarms.mi,
        "list_schedules",
        lambda: [_sched("off", enabled=False), _sched("pomo", kind="pomodoro")],
    )
    monkeypatch.setattr(alarms, "is_due_now", lambda s, now: True)
    alarms._check_and_fire_due_alarms()
    assert env["alert"] == [] and env["last_fired"] == []


def test_failure_in_one_alarm_is_isolated_and_still_marked_fired(env, monkeypatch):
    monkeypatch.setattr(alarms.mi, "list_schedules", lambda: [_sched("a1"), _sched("a2")])
    monkeypatch.setattr(alarms, "is_due_now", lambda s, now: True)
    real_fire = alarms.fire_alarm

    def flaky(s):
        if s["id"] == "a1":
            raise RuntimeError("boom")
        real_fire(s)

    monkeypatch.setattr(alarms, "fire_alarm", flaky)
    alarms._check_and_fire_due_alarms()
    assert env["last_fired"] == ["a1", "a2"]  # a1 no reintenta en bucle
    assert [a[1] for a in env["alert"]] == ["a2"]


def test_snooze_refires_enabled_alarm_without_due_check(env, monkeypatch):
    monkeypatch.setattr(notifier, "pop_due_snoozes", lambda kind, now: ["a1", "borrada", "off"])
    monkeypatch.setattr(
        alarms.mi, "list_schedules", lambda: [_sched("a1"), _sched("off", enabled=False)]
    )
    monkeypatch.setattr(alarms, "is_due_now", lambda s, now: False)
    alarms._check_and_fire_due_alarms()
    assert [a[1] for a in env["alert"]] == ["a1"]
    assert env["last_fired"] == ["a1"]


def test_snooze_failure_is_isolated(env, monkeypatch):
    monkeypatch.setattr(notifier, "pop_due_snoozes", lambda kind, now: ["a1"])
    monkeypatch.setattr(alarms.mi, "list_schedules", lambda: [_sched("a1")])
    monkeypatch.setattr(alarms, "is_due_now", lambda s, now: False)
    monkeypatch.setattr(alarms, "fire_alarm", lambda s: (_ for _ in ()).throw(RuntimeError("x")))
    alarms._check_and_fire_due_alarms()
    assert env["last_fired"] == ["a1"]


def test_run_loop_survives_tick_errors_and_sleeps(monkeypatch):
    ticks = {"n": 0, "sleeps": []}

    def tick():
        ticks["n"] += 1
        raise RuntimeError("db bloqueada")

    async def fake_sleep(seconds):
        ticks["sleeps"].append(seconds)
        if len(ticks["sleeps"]) == 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(alarms, "_check_and_fire_due_alarms", tick)
    monkeypatch.setattr(alarms.asyncio, "sleep", fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(alarms.run_alarms_loop())
    assert ticks["n"] == 2 and ticks["sleeps"] == [alarms.ALARM_POLL_INTERVAL_SECONDS] * 2
