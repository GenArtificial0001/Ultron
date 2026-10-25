"""BL-007: el reloj de agentes programados (`app/scheduler/agents.py`) -- cuando disparar, que disparar y como se cierra.

Este modulo corre comandos, skills y archivos del usuario SIN que nadie lo mire, asi que un error de borde se nota tarde
(una alarma que no suena a las 8:30). Los tests de `test_agents_db.py` cubren el CRUD y el orden de disparo; aca se cubre la
LOGICA: calculo de horarios, despacho por tipo, secuencias, stop y barrido.

Reglas del test: sin `sleep`, el tiempo es un numero que se pasa (`now`) o un reloj falso; `os.startfile`, el notificador y
`_launch_turn` se reemplazan por espias (jamas se abre un archivo ni una ventana de verdad); la base maestra va a un temp
(fixture `tmp_master_db`). Un modulo ya existente nace verde: el "rojo" lo da la prueba de mutacion (ver .agent/STATE.md)."""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.persistence import master_index as mi
from app.scheduler import agents as ag
from app.session.events import TURN_STATUS_CANCELLED, TURN_STATUS_DONE, UserTurn, get_session_store

# Un martes a las 10:00 hora local (weekday()==1). Todo se calcula desde aca: nada depende del dia en que corra la suite.
TUE_10 = datetime(2026, 9, 29, 10, 0, 0)
NOW = TUE_10.timestamp()
H = 3600
D = 86400


def agent(kind="recurring", rule="daily", **over):
    base = {
        "id": "a1",
        "label": "Reporte",
        "session_id": "s1",
        "kind": kind,
        "enabled": True,
        "automation_type": "command",
        "commands": [],
        "skill_name": "",
        "file_path": "",
        "trigger": {"rule": rule, "time": "08:30"},
        "created_at": NOW - 30 * D,
        "last_fired_at": None,
    }
    base.update(over)
    return base


class _FrozenDatetime(datetime):
    """`datetime` cuyo `now()` devuelve el instante congelado: `next_due` mezcla `time.time()` con `datetime.now()`."""

    frozen: float = NOW

    @classmethod
    def now(cls, tz=None):
        return cls.fromtimestamp(cls.frozen, tz)


@pytest.fixture
def clock(monkeypatch):
    """Congela las DOS fuentes de tiempo que usa el modulo (`time.time()` y `datetime.now()`) en `NOW` (martes 10:00).
    Congelar solo una daba resultados con el reloj real (primer borrador): `next_due` usa ambas. `clock.set(ts)` lo mueve."""
    _FrozenDatetime.frozen = NOW
    state = {"now": NOW}
    monkeypatch.setattr(ag.time, "time", lambda: state["now"])
    monkeypatch.setattr(ag, "datetime", _FrozenDatetime)

    def set_(ts):
        state["now"] = ts
        _FrozenDatetime.frozen = ts

    return SimpleNamespace(set=set_, get=lambda: state["now"])


# ---------------------------------------------------------------------------------------------------------------------
# A) HORARIOS: next_due (proximo disparo a mostrar en la UI)
# ---------------------------------------------------------------------------------------------------------------------
class TestNextDue:
    def test_daily_after_todays_time_is_tomorrow(self, clock):
        # 10:00 y la hora es 08:30 -> hoy ya paso: manana 08:30
        assert ag.next_due(agent(rule="daily")) == datetime(2026, 9, 30, 8, 30).timestamp()

    def test_daily_before_todays_time_is_today(self, clock):
        a = agent(rule="daily", trigger={"rule": "daily", "time": "18:00"})
        assert ag.next_due(a) == datetime(2026, 9, 29, 18, 0).timestamp()

    def test_weekdays_on_friday_evening_skips_the_weekend(self, clock):
        clock.set(datetime(2026, 10, 2, 18, 0).timestamp())  # viernes 18:00, ya paso 08:30
        assert ag.next_due(agent(rule="weekdays")) == datetime(2026, 10, 5, 8, 30).timestamp()  # lunes

    def test_weekdays_on_saturday_goes_to_monday(self, clock):
        clock.set(datetime(2026, 10, 3, 9, 0).timestamp())
        assert ag.next_due(agent(rule="weekdays")) == datetime(2026, 10, 5, 8, 30).timestamp()

    def test_weekly_uses_the_js_weekday_convention(self, clock):
        """0=domingo..6=sabado (igual que el <select> del frontend), no la de Python (lunes=0)."""
        friday = agent(rule="weekly", trigger={"rule": "weekly", "time": "09:00", "weekday": 5})
        assert datetime.fromtimestamp(ag.next_due(friday)).weekday() == 4
        saturday = agent(rule="weekly", trigger={"rule": "weekly", "time": "09:00", "weekday": 6})
        assert datetime.fromtimestamp(ag.next_due(saturday)).weekday() == 5

    def test_weekly_on_sunday_as_text_already_works(self, clock):
        """Con el texto "0" siempre anduvo (`"0"` es verdadero en Python). Se fija para que el arreglo del numero no lo rompa."""
        sunday = agent(rule="weekly", trigger={"rule": "weekly", "time": "09:00", "weekday": "0"})
        assert datetime.fromtimestamp(ag.next_due(sunday)).weekday() == 6

    def test_weekly_on_sunday_as_a_number_means_sunday(self, clock):
        """El <select> del frontend manda Number("0") == 0 para el domingo (app.js:8627)."""
        sunday = agent(rule="weekly", trigger={"rule": "weekly", "time": "09:00", "weekday": 0})
        assert datetime.fromtimestamp(ag.next_due(sunday)).weekday() == 6

    @pytest.mark.parametrize("weekday", [None, "", "lunes?", 7, -1, 99, "9"])
    def test_a_missing_or_invalid_weekday_falls_back_to_monday_like_before(self, clock, weekday):
        """El arreglo del domingo NO cambia lo que ya andaba: ausente, ilegible o fuera de 0..6 sigue siendo lunes."""
        a = agent(rule="weekly", trigger={"rule": "weekly", "time": "09:00", "weekday": weekday})
        assert datetime.fromtimestamp(ag.next_due(a)).weekday() == 0

    @pytest.mark.parametrize("weekday,py_weekday", [(0, 6), (1, 0), (2, 1), (3, 2), (4, 3), (5, 4), (6, 5), ("3", 2), (3.0, 2)])
    def test_every_valid_weekday_maps_to_the_right_day(self, clock, weekday, py_weekday):
        """0=domingo..6=sabado (convencion JS), como numero, como texto o como float."""
        a = agent(rule="weekly", trigger={"rule": "weekly", "time": "09:00", "weekday": weekday})
        assert datetime.fromtimestamp(ag.next_due(a)).weekday() == py_weekday

    @pytest.mark.parametrize("hm,expected", [("00:00", (0, 0)), ("23:59", (23, 59)), ("8:5", (8, 5)), ("09:30", (9, 30))])
    def test_the_boundaries_of_a_valid_time_are_kept(self, clock, hm, expected):
        """23:59 y 00:00 son validos: el rango del arreglo no debe tragarse los extremos."""
        a = agent(rule="daily", trigger={"rule": "daily", "time": hm})
        due = datetime.fromtimestamp(ag.next_due(a))
        assert (due.hour, due.minute) == expected

    def test_weekly_with_an_invalid_time_still_returns_a_date(self, clock):
        a = agent(rule="weekly", trigger={"rule": "weekly", "time": "99:99", "weekday": 5})
        assert datetime.fromtimestamp(ag.next_due(a)).hour == 8

    def test_weekly_with_garbage_weekday_falls_back_to_monday(self, clock):
        a = agent(rule="weekly", trigger={"rule": "weekly", "time": "09:00", "weekday": "lunes?"})
        assert datetime.fromtimestamp(ag.next_due(a)).weekday() == 0

    def test_weekly_same_weekday_but_time_already_passed_is_next_week(self, clock):
        a = agent(rule="weekly", trigger={"rule": "weekly", "time": "08:30", "weekday": 2})  # martes JS=2
        assert ag.next_due(a) == datetime(2026, 10, 6, 8, 30).timestamp()

    def test_ndays_counts_from_the_last_firing(self, clock):
        a = agent(rule="ndays", trigger={"rule": "ndays", "n": 3}, last_fired_at=NOW - D)
        assert ag.next_due(a) == NOW - D + 3 * D

    def test_ndays_falls_back_to_created_at_when_never_fired(self, clock):
        a = agent(rule="ndays", trigger={"rule": "ndays", "n": 2}, created_at=NOW - 3 * D)
        assert ag.next_due(a) == NOW - 3 * D + 4 * D  # 2 dias, 4 dias: el primero ya paso

    def test_nhours_steps_forward_until_strictly_in_the_future(self, clock):
        a = agent(rule="nhours", trigger={"rule": "nhours", "n": 2}, last_fired_at=NOW - 5 * H)
        assert ag.next_due(a) == NOW - 5 * H + 6 * H

    @pytest.mark.parametrize("n", [0, -3, "x", None])
    def test_an_invalid_interval_counts_as_1(self, clock, n):
        a = agent(rule="nhours", trigger={"rule": "nhours", "n": n}, last_fired_at=NOW - 30 * 60)
        assert ag.next_due(a) == NOW - 30 * 60 + H

    @pytest.mark.parametrize("hm", ["ocho y media", "", None, "8"])
    def test_a_malformed_time_falls_back_to_0830(self, clock, hm):
        a = agent(rule="daily", trigger={"rule": "daily", "time": hm})
        assert ag.next_due(a) == datetime(2026, 9, 30, 8, 30).timestamp()

    @pytest.mark.parametrize("hm", ["25:99", "24:00", "08:60", "-1:30"])
    def test_an_out_of_range_time_falls_back_to_0830(self, clock, hm):
        a = agent(rule="daily", trigger={"rule": "daily", "time": hm})
        assert ag.next_due(a) == datetime(2026, 9, 30, 8, 30).timestamp()

    def test_once_returns_its_target_even_if_past(self, clock):
        past = (TUE_10 - timedelta(days=1)).isoformat()
        assert ag.next_due(agent(kind="once", trigger={"target_iso": past})) == (TUE_10 - timedelta(days=1)).timestamp()

    @pytest.mark.parametrize("iso", ["", "ayer", None])
    def test_once_with_an_invalid_date_is_none(self, clock, iso):
        assert ag.next_due(agent(kind="once", trigger={"target_iso": iso})) is None

    def test_unknown_rule_is_none(self, clock):
        assert ag.next_due(agent(rule="cada-luna-llena")) is None


# ---------------------------------------------------------------------------------------------------------------------
# A) HORARIOS: is_due_now (la decision de "hay que disparar YA")
# ---------------------------------------------------------------------------------------------------------------------
class TestIsDueNow:
    def test_daily_fires_after_its_time_if_not_fired_today(self):
        assert ag.is_due_now(agent(rule="daily"), NOW) is True

    def test_daily_does_not_fire_before_its_time(self):
        a = agent(rule="daily", trigger={"rule": "daily", "time": "18:00"})
        assert ag.is_due_now(a, NOW) is False

    def test_it_does_not_fire_twice_for_the_same_due(self):
        due = datetime(2026, 9, 29, 8, 30).timestamp()
        assert ag.is_due_now(agent(rule="daily", last_fired_at=due), NOW) is False

    def test_it_fires_again_if_the_last_firing_was_before_todays_due(self):
        assert ag.is_due_now(agent(rule="daily", last_fired_at=NOW - D), NOW) is True

    def test_exactly_at_the_due_time_it_fires(self):
        at = datetime(2026, 9, 29, 8, 30).timestamp()
        assert ag.is_due_now(agent(rule="daily"), at) is True

    def test_one_second_before_the_due_time_it_does_not(self):
        at = datetime(2026, 9, 29, 8, 30).timestamp() - 1
        assert ag.is_due_now(agent(rule="daily"), at) is False

    def test_it_never_recovers_yesterdays_due(self):
        """Bug real documentado (2026-09-17): una alarma 'Diario 23:59' consultada a las 8am NO debe disparar el 23:59 de AYER."""
        a = agent(rule="daily", trigger={"rule": "daily", "time": "23:59"})
        assert ag.is_due_now(a, datetime(2026, 9, 29, 8, 0).timestamp()) is False

    def test_a_just_created_agent_does_not_fire_a_due_from_before_it_existed(self):
        """Creado a las 14:00 con 'Diario 08:30': NO dispara el 08:30 de hoy (esperar al de manana)."""
        a = agent(rule="daily", created_at=datetime(2026, 9, 29, 9, 0).timestamp())
        assert ag.is_due_now(a, NOW) is False

    def test_a_due_created_in_the_same_second_is_still_valid(self):
        due = datetime(2026, 9, 29, 8, 30).timestamp()
        assert ag.is_due_now(agent(rule="daily", created_at=due), NOW) is True

    def test_weekdays_does_not_fire_on_saturday(self):
        sat = datetime(2026, 10, 3, 12, 0).timestamp()
        assert ag.is_due_now(agent(rule="weekdays", created_at=sat - 30 * D), sat) is False

    def test_weekdays_fires_on_friday(self):
        fri = datetime(2026, 10, 2, 12, 0).timestamp()
        assert ag.is_due_now(agent(rule="weekdays", created_at=fri - 30 * D), fri) is True

    def test_weekly_on_sunday_fires_on_sunday(self):
        """Lo que manda la UI es el numero 0; con el texto "0" ya andaba, con el numero no."""
        sunday_noon = datetime(2026, 10, 4, 12, 0).timestamp()
        a = agent(rule="weekly", trigger={"rule": "weekly", "time": "08:30", "weekday": 0}, created_at=sunday_noon - 30 * D)
        assert ag.is_due_now(a, sunday_noon) is True
        assert ag.is_due_now(a, sunday_noon + D) is False  # el lunes NO

    @pytest.mark.parametrize("hm", ["25:99", "24:00", "08:60"])
    def test_an_out_of_range_time_does_not_raise(self, hm):
        a = agent(rule="daily", trigger={"rule": "daily", "time": hm})
        assert ag.is_due_now(a, NOW) in (True, False)

    def test_weekly_fires_only_on_its_weekday(self):
        a = agent(rule="weekly", trigger={"rule": "weekly", "time": "08:30", "weekday": 2})  # martes
        assert ag.is_due_now(a, NOW) is True
        assert ag.is_due_now(a, NOW + D) is False  # miercoles

    def test_ndays_needs_the_full_interval_since_last_firing(self):
        a = agent(rule="ndays", trigger={"rule": "ndays", "n": 2}, last_fired_at=NOW - 2 * D + 1)
        assert ag.is_due_now(a, NOW) is False
        a["last_fired_at"] = NOW - 2 * D
        assert ag.is_due_now(a, NOW) is True

    def test_nhours_fires_once_the_hours_passed(self):
        a = agent(rule="nhours", trigger={"rule": "nhours", "n": 3}, last_fired_at=NOW - 3 * H)
        assert ag.is_due_now(a, NOW) is True

    def test_once_fires_when_its_moment_arrives_and_only_once(self):
        target = TUE_10 - timedelta(minutes=5)
        a = agent(kind="once", trigger={"target_iso": target.isoformat()}, created_at=target.timestamp() - D)
        assert ag.is_due_now(a, NOW) is True
        a["last_fired_at"] = NOW
        assert ag.is_due_now(a, NOW + D) is False

    def test_once_in_the_future_does_not_fire_early(self):
        target = TUE_10 + timedelta(hours=1)
        a = agent(kind="once", trigger={"target_iso": target.isoformat()})
        assert ag.is_due_now(a, NOW) is False

    def test_once_with_an_invalid_date_never_fires(self):
        assert ag.is_due_now(agent(kind="once", trigger={"target_iso": "xx"}), NOW) is False

    def test_unknown_rule_never_fires(self):
        assert ag.is_due_now(agent(rule="???"), NOW) is False


# ---------------------------------------------------------------------------------------------------------------------
# B) DISPARO por tipo
# ---------------------------------------------------------------------------------------------------------------------
@pytest.fixture
def fire_env(monkeypatch, tmp_master_db):
    """Espias: alerta visual, auditoria, lanzamiento de turnos y apertura de archivos. Nada real."""
    spy = SimpleNamespace(alerts=[], audits=[], launched=[], started=[], startfile_error=None)

    from app import notifier
    from app.tools import base as tool_base

    monkeypatch.setattr(notifier, "show_alert", lambda *a, **k: spy.alerts.append(a))
    monkeypatch.setattr(tool_base, "_audit", lambda action, data: spy.audits.append((action, data)))

    def startfile(path):
        if spy.startfile_error:
            raise spy.startfile_error
        spy.started.append(path)

    monkeypatch.setattr(ag, "get_snooze_minutes", lambda: 10)
    import os

    monkeypatch.setattr(os, "startfile", startfile, raising=False)

    import app.main as main

    monkeypatch.setattr(main, "_launch_turn", lambda session, sid, turn: spy.launched.append((sid, turn.text)))
    return spy


def last_run():
    return mi.list_agent_runs("a1")[0]  # la mas reciente (ORDER BY started_at DESC)


def make(**over):
    a = agent(**over)
    mi.create_agent(a["id"], a["label"], a["session_id"], a["automation_type"], a["commands"], a["skill_name"], a["file_path"], "once", {}, True)
    return a


class TestFireAgent:
    def test_always_shows_the_visual_alert_with_the_beep_sound(self, fire_env):
        ag.fire_agent(make(automation_type="file", file_path="x.txt"))
        assert len(fire_env.alerts) == 1
        kind, agent_id, label, wav, snooze = fire_env.alerts[0]
        assert (kind, agent_id, snooze) == ("agent", "a1", 10) and wav.endswith("beep.wav")

    def test_an_agent_without_a_label_gets_a_placeholder(self, fire_env):
        ag.fire_agent(make(automation_type="file", file_path="x.txt", label=""))
        assert fire_env.alerts[0][2] == "(sin etiqueta)"

    def test_dispatches_by_automation_type(self, fire_env, monkeypatch):
        calls = []
        monkeypatch.setattr(ag, "_fire_file_agent", lambda a: calls.append("file"))
        monkeypatch.setattr(ag, "_fire_skill_agent", lambda a: calls.append("skill"))
        monkeypatch.setattr(ag, "_fire_command_agent", lambda a: calls.append("command"))

        for t in ("file", "skill", "command", None, "algo-raro"):
            ag.fire_agent(agent(automation_type=t))

        assert calls == ["file", "skill", "command", "command", "command"], "lo desconocido cae en 'command'"


class TestFileAgent:
    def test_opens_the_file_audits_it_and_records_a_done_run(self, fire_env):
        ag._fire_file_agent(make(automation_type="file", file_path="  C:\\x\\reporte.xlsx  "))

        assert fire_env.started == ["C:\\x\\reporte.xlsx"], "se recorta el espacio de los costados"
        assert fire_env.audits == [("run_file", {"path": "C:\\x\\reporte.xlsx", "status": "ok", "source": "agent:a1"})]
        assert last_run()["status"] == "done"

    def test_without_a_path_it_records_an_error_and_opens_nothing(self, fire_env):
        ag._fire_file_agent(make(automation_type="file", file_path="   "))

        assert fire_env.started == [] and fire_env.audits == []
        r = last_run()
        assert r["status"] == "error" and "archivo configurado" in r["error"]

    def test_an_os_error_is_recorded_in_plain_language_and_audited_raw(self, fire_env):
        fire_env.startfile_error = FileNotFoundError(2, "El sistema no puede encontrar el archivo", "C:\\x\\roto.xlsx")

        ag._fire_file_agent(make(automation_type="file", file_path="C:\\x\\roto.xlsx"))

        r = last_run()
        assert r["status"] == "error"
        assert "WinError" not in r["error"] and "Errno" not in r["error"], "el historial lo lee una persona: sin jerga"
        action, data = fire_env.audits[0]
        assert action == "run_file" and data["status"] == "error" and data["source"] == "agent:a1"
        assert "El sistema no puede encontrar" in data["error"], "el texto crudo SI queda en la auditoria"

    def test_a_file_run_never_stays_running(self, fire_env):
        ag._fire_file_agent(make(automation_type="file", file_path="x.txt"))
        assert last_run()["status"] != "running"


class TestCommandAgent:
    def _commands(self, monkeypatch, available: dict[str, str]):
        monkeypatch.setattr(ag, "list_custom_commands", lambda root: [{"name": n, "prompt": p} for n, p in available.items()])
        monkeypatch.setattr(ag, "project_root_for_session", lambda sid: None)

    def test_without_commands_it_records_an_error(self, fire_env):
        ag._fire_command_agent(make(commands=[]))
        r = last_run()
        assert r["status"] == "error" and "ningún comando" in r["error"] and fire_env.launched == []

    def test_a_missing_command_launches_NOTHING_and_names_all_missing(self, fire_env, monkeypatch):
        """Regla clave: una secuencia a medias seria peor que ninguna. Se reportan TODOS los faltantes juntos."""
        self._commands(monkeypatch, {"uno": "haz uno"})

        ag._fire_command_agent(make(commands=["uno", "dos", "tres"]))

        assert fire_env.launched == [], "no se dispara NI el comando que si existe"
        r = last_run()
        assert r["status"] == "error" and "/dos" in r["error"] and "/tres" in r["error"] and "/uno" not in r["error"]

    def test_all_present_launches_the_sequence_in_the_chosen_order(self, fire_env, monkeypatch):
        self._commands(monkeypatch, {"b": "prompt B", "a": "prompt A"})
        get_session_store().get_or_create("s1")

        ag._fire_command_agent(make(commands=["b", "a"]))

        assert fire_env.launched == [("s1", "prompt B")], "el primero arranca; el 2o queda encolado atras"
        run = last_run()
        assert run["status"] == "running" and run["commands"] == ["b", "a"] and len(run["turn_ids"]) == 2
        s = get_session_store().get("s1")
        assert [s.turns[i].text for i in s.queue_order] == ["prompt A"]


class TestSkillAgent:
    def test_without_a_skill_it_records_an_error(self, fire_env):
        ag._fire_skill_agent(make(automation_type="skill", skill_name="   "))
        r = last_run()
        assert r["status"] == "error" and "ninguna skill" in r["error"] and fire_env.launched == []

    def test_launches_a_single_synthetic_skill_turn(self, fire_env):
        get_session_store().get_or_create("s1")

        ag._fire_skill_agent(make(automation_type="skill", skill_name=" skill-interna "))

        assert fire_env.launched == [("s1", "/skill skill-interna ")], "mismo texto que el panel de accesos rapidos"
        assert last_run()["commands"] == ["skill:skill-interna"]


# ---------------------------------------------------------------------------------------------------------------------
# C) SECUENCIA: sesion libre u ocupada
# ---------------------------------------------------------------------------------------------------------------------
class TestLaunchSequence:
    def test_free_session_launches_the_first_and_queues_the_rest(self, fire_env):
        session = get_session_store().get_or_create("seq-free")
        mi.create_agent("a1", "x", "seq-free", "command", ["c1", "c2", "c3"], "", "", "once", {}, True)

        ag._launch_sequence("a1", "seq-free", "run-1", ["c1", "c2", "c3"], ["p1", "p2", "p3"])

        assert fire_env.launched == [("seq-free", "p1")]
        assert [session.turns[i].text for i in session.queue_order] == ["p2", "p3"], "el 1o se lanza; el resto queda encolado EN ORDEN"
        run = mi.list_running_agent_runs()[0]
        assert run["id"] == "run-1" and len(run["turn_ids"]) == 3 and all(t.startswith("agent-") for t in run["turn_ids"])

    def test_busy_session_queues_everything_and_launches_nothing(self, fire_env):
        session = get_session_store().get_or_create("seq-busy")
        session.current_task = SimpleNamespace(done=lambda: False)  # is_busy es una propiedad: depende de current_task
        mi.create_agent("a1", "x", "seq-busy", "command", ["c1", "c2"], "", "", "once", {}, True)

        ag._launch_sequence("a1", "seq-busy", "run-2", ["c1", "c2"], ["p1", "p2"])

        assert fire_env.launched == [], "con la sesion ocupada NO se pisa el turno en curso"
        assert len(mi.list_running_agent_runs()[0]["turn_ids"]) == 2
        assert len(session.queue_order) == 2 and all(session.turns[i].status == "queued" for i in session.queue_order)
        session.current_task = None


# ---------------------------------------------------------------------------------------------------------------------
# D) CONTROL: stop_agent y run_agent_now
# ---------------------------------------------------------------------------------------------------------------------
class FakeSession:
    def __init__(self, active=None, queued=()):
        self.active_turn_id = active
        self.stop_requested = False
        self.edit_pending_text = "pendiente"
        self.queued = set(queued)
        self.removed = []

    def remove_queued_turn(self, tid):
        if tid in self.queued:
            self.queued.discard(tid)
            self.removed.append(tid)
            return True
        return False


@pytest.fixture
def store(monkeypatch):
    sessions: dict[str, FakeSession] = {}
    monkeypatch.setattr(ag, "get_session_store", lambda: SimpleNamespace(get=sessions.get, get_or_create=lambda s: sessions.setdefault(s, FakeSession())))
    return sessions


def running_run(session_id="s1", turn_ids=("t1", "t2"), agent_id="a1", run_id="r1"):
    mi.create_agent_run(run_id, agent_id, session_id, ["c"], list(turn_ids))


class TestStopAgent:
    def test_without_a_running_run_it_returns_false(self, tmp_master_db, store):
        assert ag.stop_agent("a1") is False

    def test_stops_the_active_turn_cooperatively_and_clears_pending_edit(self, tmp_master_db, store):
        store["s1"] = FakeSession(active="t1", queued=["t2"])
        running_run()

        assert ag.stop_agent("a1") is True

        s = store["s1"]
        assert s.stop_requested is True and s.edit_pending_text is None
        assert s.removed == ["t2"], "el paso siguiente de la MISMA secuencia sale de la cola: si no, arrancaria solo"

    def test_removes_queued_steps_even_if_none_is_active(self, tmp_master_db, store):
        store["s1"] = FakeSession(active=None, queued=["t1", "t2"])
        running_run()

        assert ag.stop_agent("a1") is True
        assert store["s1"].removed == ["t1", "t2"] and store["s1"].stop_requested is False

    def test_does_not_touch_an_active_turn_of_another_run(self, tmp_master_db, store):
        store["s1"] = FakeSession(active="de-otro-usuario", queued=[])
        running_run()

        assert ag.stop_agent("a1") is False
        assert store["s1"].stop_requested is False, "no se corta un turno que no es de esta secuencia"

    def test_a_lost_session_does_not_crash(self, tmp_master_db, store):
        running_run(session_id="s-perdida")
        assert ag.stop_agent("a1") is False

    def test_only_this_agents_runs_are_stopped(self, tmp_master_db, store):
        store["s1"] = FakeSession(active="t1", queued=[])
        running_run(agent_id="OTRO", run_id="r-otro")

        assert ag.stop_agent("a1") is False and store["s1"].stop_requested is False


class TestRunAgentNow:
    def test_unknown_agent_returns_false_and_fires_nothing(self, tmp_master_db, monkeypatch):
        fired = []
        monkeypatch.setattr(ag, "fire_agent", lambda a: fired.append(a))
        assert ag.run_agent_now("no-existe") is False and fired == []

    def test_fires_and_stamps_last_fired_so_a_once_does_not_repeat(self, tmp_master_db, monkeypatch, clock):
        make(automation_type="file", file_path="x.txt")
        fired = []
        monkeypatch.setattr(ag, "fire_agent", lambda a: fired.append(a["id"]))

        assert ag.run_agent_now("a1") is True

        assert fired == ["a1"] and mi.get_agent("a1")["last_fired_at"] == NOW


# ---------------------------------------------------------------------------------------------------------------------
# E) RELOJ: _check_and_fire_due_agents y _sweep_finished_runs
# ---------------------------------------------------------------------------------------------------------------------
@pytest.fixture
def tick_env(monkeypatch, tmp_master_db, clock):
    spy = SimpleNamespace(fired=[], snoozes=[], boom_for=set())

    def fire(a):
        spy.fired.append(a["id"])
        if a["id"] in spy.boom_for:
            raise RuntimeError("fallo al disparar")

    from app import notifier

    monkeypatch.setattr(ag, "fire_agent", fire)
    monkeypatch.setattr(notifier, "pop_due_snoozes", lambda kind, now: list(spy.snoozes))
    return spy


class TestCheckAndFire:
    def test_a_disabled_agent_never_fires(self, tick_env):
        mi.create_agent("d1", "x", "s1", "command", [], "", "", "once", {"target_iso": (TUE_10 - timedelta(hours=1)).isoformat()}, False)
        ag._check_and_fire_due_agents()
        assert tick_env.fired == []

    def test_a_due_agent_fires_and_is_stamped(self, tick_env):
        mi.create_agent("d1", "x", "s1", "command", [], "", "", "once", {"target_iso": (TUE_10 - timedelta(hours=1)).isoformat()}, True)
        # el "once" debe haberse creado ANTES de su momento: se retrocede created_at a mano
        mi_set_created("d1", NOW - 2 * D)

        ag._check_and_fire_due_agents()

        assert tick_env.fired == ["d1"] and mi.get_agent("d1")["last_fired_at"] == NOW

    def test_it_does_not_fire_the_same_agent_on_the_next_tick(self, tick_env):
        mi.create_agent("d1", "x", "s1", "command", [], "", "", "once", {"target_iso": (TUE_10 - timedelta(hours=1)).isoformat()}, True)
        mi_set_created("d1", NOW - 2 * D)

        ag._check_and_fire_due_agents()
        ag._check_and_fire_due_agents()

        assert tick_env.fired == ["d1"]

    def test_a_failing_agent_does_not_stop_the_others_and_is_still_stamped(self, tick_env, caplog):
        past = (TUE_10 - timedelta(hours=1)).isoformat()
        for i in ("f1", "f2", "f3"):
            mi.create_agent(i, i, "s1", "command", [], "", "", "once", {"target_iso": past}, True)
            mi_set_created(i, NOW - 2 * D)
        tick_env.boom_for = {"f2"}

        with caplog.at_level("WARNING"):
            ag._check_and_fire_due_agents()

        assert tick_env.fired == ["f1", "f2", "f3"], "el fallo del 2o no frena al 3o"
        assert mi.get_agent("f2")["last_fired_at"] == NOW, "se marca igual: sin esto reintentaria para siempre contra un agente roto"
        assert any("f2" in r.getMessage() or "Fallo al disparar" in r.getMessage() for r in caplog.records)

    def test_an_agent_with_an_invalid_time_does_not_block_the_others(self, tick_env):
        past = (TUE_10 - timedelta(hours=1)).isoformat()
        mi.create_agent("roto", "roto", "s1", "command", [], "", "", "recurring", {"rule": "daily", "time": "25:99"}, True)
        mi.create_agent("sano", "sano", "s1", "command", [], "", "", "once", {"target_iso": past}, True)
        mi_set_created("roto", NOW - 2 * D)  # el roto va primero (created_at ASC)
        mi_set_created("sano", NOW - 1 * D)

        ag._check_and_fire_due_agents()

        assert "sano" in tick_env.fired, "un agente mal escrito frenaba el disparo de todos los demas, cada 20 s"

    def test_a_snooze_refires_without_checking_the_schedule(self, tick_env):
        mi.create_agent("z1", "x", "s1", "command", [], "", "", "once", {"target_iso": "2999-01-01T00:00:00"}, True)
        tick_env.snoozes = ["z1"]

        ag._check_and_fire_due_agents()

        assert tick_env.fired == ["z1"], "el 'Zzz' es una orden del usuario: no depende del horario"
        assert mi.get_agent("z1")["last_fired_at"] == NOW

    def test_a_snooze_of_a_disabled_or_deleted_agent_is_ignored(self, tick_env):
        mi.create_agent("z2", "x", "s1", "command", [], "", "", "once", {}, False)
        tick_env.snoozes = ["z2", "no-existe"]

        ag._check_and_fire_due_agents()

        assert tick_env.fired == []

    def test_a_failing_snooze_refire_is_still_stamped(self, tick_env):
        mi.create_agent("z3", "x", "s1", "command", [], "", "", "once", {}, True)
        tick_env.snoozes = ["z3"]
        tick_env.boom_for = {"z3"}

        ag._check_and_fire_due_agents()

        assert mi.get_agent("z3")["last_fired_at"] == NOW


def mi_set_created(agent_id, ts):
    """Retrocede `created_at` (la API publica no lo permite): un 'once' del pasado solo dispara si el agente ya existia."""
    with mi._connect() as conn:
        conn.execute("UPDATE agents SET created_at = ? WHERE id = ?", (ts, agent_id))


class TestSweepFinishedRuns:
    def _turn(self, tid, status):
        t = UserTurn(id=tid, text="x")
        t.status = status
        return t

    def _session(self, store, turns):
        s = FakeSweepSession({t.id: t for t in turns})
        store["s1"] = s
        return s

    def test_a_run_without_turns_is_left_alone(self, tmp_master_db, store):
        running_run(turn_ids=())
        ag._sweep_finished_runs()
        assert mi.list_running_agent_runs()

    def test_a_lost_session_closes_the_run_with_a_plain_note(self, tmp_master_db, store):
        running_run(session_id="s-perdida")
        ag._sweep_finished_runs()

        assert not mi.list_running_agent_runs()
        r = last_run()
        assert r["status"] == "done" and "Vision se reinició" in r["error"]

    def test_a_missing_turn_closes_the_run_as_unknown(self, tmp_master_db, store):
        self._session(store, [self._turn("t1", TURN_STATUS_DONE)])  # falta t2
        running_run()

        ag._sweep_finished_runs()

        assert not mi.list_running_agent_runs() and "Vision se reinició" in last_run()["error"]

    def test_all_done_closes_as_done(self, tmp_master_db, store):
        self._session(store, [self._turn("t1", TURN_STATUS_DONE), self._turn("t2", TURN_STATUS_DONE)])
        running_run()

        ag._sweep_finished_runs()

        assert last_run()["status"] == "done"

    def test_one_step_still_active_keeps_the_run_running(self, tmp_master_db, store):
        self._session(store, [self._turn("t1", TURN_STATUS_DONE), self._turn("t2", "active")])
        running_run()

        ag._sweep_finished_runs()

        assert len(mi.list_running_agent_runs()) == 1

    def test_cancelled_with_nothing_pending_closes_as_cancelled(self, tmp_master_db, store):
        self._session(store, [self._turn("t1", TURN_STATUS_DONE), self._turn("t2", TURN_STATUS_CANCELLED)])
        running_run()

        ag._sweep_finished_runs()

        assert last_run()["status"] == "cancelled"

    def test_cancelled_but_another_step_is_still_queued_keeps_running(self, tmp_master_db, store):
        self._session(store, [self._turn("t1", TURN_STATUS_CANCELLED), self._turn("t2", "queued")])
        running_run()

        ag._sweep_finished_runs()

        assert len(mi.list_running_agent_runs()) == 1


class FakeSweepSession:
    def __init__(self, turns):
        self.turns = turns


# ---------------------------------------------------------------------------------------------------------------------
# E) El bucle de fondo
# ---------------------------------------------------------------------------------------------------------------------
def test_the_loop_survives_a_failing_tick_and_keeps_going(monkeypatch):
    import asyncio

    calls = {"n": 0}

    def tick():
        calls["n"] += 1
        raise RuntimeError("se cayo el ciclo")

    async def stop_after_two(_s):
        if calls["n"] >= 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(ag, "_check_and_fire_due_agents", tick)
    monkeypatch.setattr(ag, "_sweep_finished_runs", lambda: None)
    monkeypatch.setattr(ag.asyncio, "sleep", stop_after_two)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(ag.run_agents_loop())

    assert calls["n"] == 2, "una excepcion en un ciclo no mata el reloj: sigue con el siguiente"


def test_the_poll_interval_is_20_seconds():
    assert ag.AGENT_POLL_INTERVAL_SECONDS == 20
