"""PQ-004 / BL-019 (opcion (a), delegada por el usuario en ADR-018): la API rechaza con 422 un horario invalido al CREAR o EDITAR
un agente o una alarma, con un mensaje para personas.

Antes, `trigger` era un dict libre: un "25:99" se guardaba y el reloj lo interpretaba en silencio como 08:30 (BL-007 arreglo que
el reloj no se rompiera, pero el usuario nunca se enteraba). Todo con TestClient y la base maestra en `tmp_path`: sin red.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app

c = TestClient(app)

OK_ONCE = {"target_iso": "2030-01-01T08:30"}
OK_DAILY = {"rule": "daily", "time": "08:30"}


def alarm(kind="recurring", trigger=None, **extra):
    return {"label": "A", "kind": kind, "trigger": OK_DAILY if trigger is None else trigger, "sound": "beep", **extra}


def agent(kind="recurring", trigger=None, **extra):
    return {
        "label": "G",
        "session_id": "s1",
        "automation_type": "skill",
        "skill_name": "x",
        "kind": kind,
        "trigger": OK_DAILY if trigger is None else trigger,
        **extra,
    }


# Cada caso: (descripcion, kind, trigger). Todos deben ser rechazados.
INVALID = [
    ("hora 25:99", "recurring", {"rule": "daily", "time": "25:99"}),
    ("hora 24:00", "recurring", {"rule": "daily", "time": "24:00"}),
    ("minuto 60", "recurring", {"rule": "weekdays", "time": "08:60"}),
    ("hora con letras", "recurring", {"rule": "daily", "time": "ocho y media"}),
    ("hora sin minutos", "recurring", {"rule": "daily", "time": "8"}),
    ("hora negativa", "recurring", {"rule": "daily", "time": "-1:30"}),
    ("regla desconocida", "recurring", {"rule": "nunca", "time": "08:30"}),
    ("sin regla", "recurring", {"time": "08:30"}),
    ("trigger vacio", "recurring", {}),
    ("dia 7", "recurring", {"rule": "weekly", "time": "08:30", "weekday": 7}),
    ("dia negativo", "recurring", {"rule": "weekly", "time": "08:30", "weekday": -1}),
    ("dia con letras", "recurring", {"rule": "weekly", "time": "08:30", "weekday": "lunes"}),
    ("n cero", "recurring", {"rule": "ndays", "n": 0}),
    ("n negativo", "recurring", {"rule": "nhours", "n": -3}),
    ("n con letras", "recurring", {"rule": "ndays", "n": "dos"}),
    ("dia booleano", "recurring", {"rule": "weekly", "time": "08:30", "weekday": True}),
    ("n booleano", "recurring", {"rule": "ndays", "n": True}),
    ("once sin fecha", "once", {}),
    ("once con fecha basura", "once", {"target_iso": "no-es-una-fecha"}),
    ("once con mes 13", "once", {"target_iso": "2030-13-01T08:30"}),
    ("once con fecha vacia", "once", {"target_iso": ""}),
    ("tipo de repeticion desconocido", "cada-tanto", OK_DAILY),
]

VALID = [
    ("diario", "recurring", {"rule": "daily", "time": "08:30"}),
    ("diario sin hora (el reloj usa 08:30)", "recurring", {"rule": "daily"}),
    ("medianoche", "recurring", {"rule": "daily", "time": "00:00"}),
    ("ultimo minuto", "recurring", {"rule": "daily", "time": "23:59"}),
    ("dias habiles", "recurring", {"rule": "weekdays", "time": "07:05"}),
    ("domingo", "recurring", {"rule": "weekly", "time": "08:30", "weekday": 0}),
    ("sabado", "recurring", {"rule": "weekly", "time": "08:30", "weekday": 6}),
    ("semanal sin dia (el reloj usa lunes)", "recurring", {"rule": "weekly", "time": "08:30"}),
    ("cada 2 dias", "recurring", {"rule": "ndays", "n": 2}),
    ("cada 3 horas", "recurring", {"rule": "nhours", "n": 3, "time": "08:30"}),
    ("una vez", "once", {"target_iso": "2030-01-01T08:30"}),
    ("una vez con segundos", "once", {"target_iso": "2030-01-01T08:30:15"}),
]


@pytest.mark.parametrize(("why", "kind", "trigger"), INVALID, ids=[i[0] for i in INVALID])
def test_an_invalid_alarm_is_rejected_with_a_message(tmp_master_db, why, kind, trigger):
    r = c.post("/api/alarms", json=alarm(kind, trigger))

    assert r.status_code == 422, why
    assert isinstance(r.json()["detail"], str) and r.json()["detail"].strip()
    assert c.get("/api/alarms").json() == {"schedules": []}  # y no se guardo nada


@pytest.mark.parametrize(("why", "kind", "trigger"), INVALID, ids=[i[0] for i in INVALID])
def test_an_invalid_agent_is_rejected_with_a_message(tmp_master_db, why, kind, trigger):
    r = c.post("/api/agents", json=agent(kind, trigger))

    assert r.status_code == 422, why
    assert isinstance(r.json()["detail"], str) and r.json()["detail"].strip()
    assert c.get("/api/agents").json()["agents"] == []


@pytest.mark.parametrize(("why", "kind", "trigger"), VALID, ids=[i[0] for i in VALID])
def test_a_valid_schedule_is_still_accepted(tmp_master_db, why, kind, trigger):
    assert c.post("/api/alarms", json=alarm(kind, trigger)).status_code == 200, why
    assert c.post("/api/agents", json=agent(kind, trigger)).status_code == 200, why


def test_the_message_says_what_is_wrong_and_how_to_fix_it(tmp_master_db):
    detail = c.post("/api/alarms", json=alarm("recurring", {"rule": "daily", "time": "25:99"})).json()["detail"]

    assert "25:99" in detail and "hh:mm" in detail.lower()


def test_a_once_without_a_date_says_the_date_is_missing(tmp_master_db):
    """El mensaje es parte del contrato con la persona: «falta» (no «no es valida») cuando no escribio nada."""
    detail = c.post("/api/alarms", json=alarm("once", {})).json()["detail"]

    assert "Falta la fecha" in detail


def test_the_dow_message_lists_what_is_valid(tmp_master_db):
    detail = c.post("/api/alarms", json=alarm("recurring", {"rule": "weekly", "time": "08:30", "weekday": 9})).json()[
        "detail"
    ]

    assert "0" in detail and "6" in detail


def test_editing_into_an_invalid_time_is_rejected_and_keeps_the_old_one(tmp_master_db):
    sid = c.post("/api/alarms", json=alarm()).json()["schedule"]["id"]

    r = c.put(f"/api/alarms/{sid}", json={"trigger": {"rule": "daily", "time": "25:99"}})

    assert r.status_code == 422
    assert c.get("/api/alarms").json()["schedules"][0]["trigger"] == OK_DAILY


def test_editing_an_agent_into_an_invalid_time_is_rejected(tmp_master_db):
    aid = c.post("/api/agents", json=agent()).json()["agent"]["id"]

    r = c.put(f"/api/agents/{aid}", json={"trigger": {"rule": "weekly", "time": "08:30", "weekday": 99}})

    assert r.status_code == 422
    assert c.get("/api/agents").json()["agents"][0]["trigger"] == OK_DAILY


def test_changing_the_kind_validates_against_the_stored_trigger(tmp_master_db):
    """Pasar de recurrente a una vez SIN mandar fecha deja un 'once' sin target_iso: tambien se rechaza."""
    sid = c.post("/api/alarms", json=alarm()).json()["schedule"]["id"]

    r = c.put(f"/api/alarms/{sid}", json={"kind": "once"})

    assert r.status_code == 422


def test_a_valid_edit_still_works(tmp_master_db):
    sid = c.post("/api/alarms", json=alarm()).json()["schedule"]["id"]

    r = c.put(f"/api/alarms/{sid}", json={"trigger": {"rule": "weekly", "time": "09:15", "weekday": 0}})

    assert r.status_code == 200 and r.json()["schedule"]["trigger"]["weekday"] == 0


def test_toggling_a_legacy_schedule_with_a_bad_trigger_still_works(tmp_master_db):
    """Un agente que YA estaba guardado con un horario malo (de antes de esta validacion) se tiene que poder apagar o renombrar:
    la validacion corre solo si el pedido toca `kind` o `trigger`."""
    from app.persistence import master_index as mi

    legacy = mi.create_schedule("legacy", "Vieja", "recurring", {"rule": "daily", "time": "25:99"}, "beep", "", True)
    agent_legacy = mi.create_agent(
        "legacy-ag", "Vieja", "s1", "skill", [], "x", "", "recurring", {"rule": "daily", "time": "25:99"}, True
    )

    assert c.put(f"/api/alarms/{legacy['id']}", json={"enabled": False}).status_code == 200
    assert c.put(f"/api/alarms/{legacy['id']}", json={"label": "Renombrada"}).status_code == 200
    assert c.put(f"/api/agents/{agent_legacy['id']}", json={"enabled": False}).status_code == 200


def test_the_validator_itself_is_pure_and_returns_none_when_valid():
    from app.scheduler import triggers

    assert triggers.validate_trigger("recurring", {"rule": "daily", "time": "08:30"}) is None
    assert triggers.validate_trigger("once", {"target_iso": "2030-01-01T08:30"}) is None
    assert "25:99" in (triggers.validate_trigger("recurring", {"rule": "daily", "time": "25:99"}) or "")
