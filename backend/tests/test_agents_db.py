"""spec agentes-programados: CRUD de las tablas `agents`/`agent_runs`
(master_index.py) y calculo de proximo disparo (`app/scheduler/agents.py`).
Un agente automatiza uno de 3 tipos (`automation_type`): 'command'
(secuencia ORDENADA de comandos propios), 'skill', o 'file'. Patron
DBManager: DB maestro redirigido a temp dir (fixture `tmp_master_db`, ver
test_schedules_db.py / conftest.py), nunca el vision.db real del usuario."""

from __future__ import annotations

import sqlite3
import time

from app.persistence import master_index as mi
from app.scheduler.agents import compute_agent_broken_status, next_due


def test_migrates_old_single_command_schema(tmp_master_db):
    """Simula un vision.db creado con la version VIEJA del schema
    (command_name/turn_id singulares, sin automation_type) -- confirma que
    `_migrate()` los pasa a listas sin perder datos y sin romper con
    columnas NOT NULL sin default."""
    conn = sqlite3.connect(str(tmp_master_db))
    conn.executescript(
        """
        CREATE TABLE agents (
            id TEXT PRIMARY KEY, label TEXT NOT NULL, session_id TEXT NOT NULL,
            command_name TEXT NOT NULL, kind TEXT NOT NULL,
            trigger_json TEXT NOT NULL DEFAULT '{}', enabled INTEGER NOT NULL DEFAULT 1,
            last_fired_at REAL, created_at REAL NOT NULL, updated_at REAL NOT NULL
        );
        CREATE TABLE agent_runs (
            id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, session_id TEXT NOT NULL,
            turn_id TEXT NOT NULL, started_at REAL NOT NULL, finished_at REAL,
            status TEXT NOT NULL DEFAULT 'running', error TEXT NOT NULL DEFAULT ''
        );
        """
    )
    conn.execute(
        "INSERT INTO agents VALUES ('a1','Viejo','sess-1','reporte','once','{}',1,NULL,1.0,1.0)"
    )
    conn.execute(
        "INSERT INTO agent_runs VALUES ('r1','a1','sess-1','turn-viejo',1.0,NULL,'running','')"
    )
    conn.commit()
    conn.close()

    agents = mi.list_agents()
    assert len(agents) == 1
    assert agents[0]["automation_type"] == "command"
    assert agents[0]["commands"] == ["reporte"]

    running = mi.list_running_agent_runs()
    assert len(running) == 1
    assert running[0]["turn_ids"] == ["turn-viejo"]

    # confirma que un INSERT nuevo (sin tocar las columnas viejas) sigue andando
    mi.create_agent(
        "a2", "Nuevo", "sess-1", "skill", [], "mi-skill", "", "once", {}, True
    )
    assert len(mi.list_agents()) == 2


def test_create_and_list_agent_command_sequence(tmp_master_db):
    a = mi.create_agent(
        "a1",
        "Reporte diario",
        "sess-1",
        "command",
        ["reporte", "cierre"],
        "",
        "",
        "recurring",
        {"rule": "daily", "time": "08:30"},
        True,
    )
    assert a["id"] == "a1"
    assert a["session_id"] == "sess-1"
    assert a["automation_type"] == "command"
    assert a["commands"] == ["reporte", "cierre"]
    lst = mi.list_agents()
    assert len(lst) == 1
    assert lst[0]["enabled"] is True


def test_create_skill_and_file_agents(tmp_master_db):
    skill_agent = mi.create_agent(
        "a-skill",
        "Auditoria",
        "sess-1",
        "skill",
        [],
        "warehouse-task-analysis",
        "",
        "once",
        {"target_iso": "2026-09-13T08:30"},
        True,
    )
    assert skill_agent["automation_type"] == "skill"
    assert skill_agent["skill_name"] == "warehouse-task-analysis"

    file_agent = mi.create_agent(
        "a-file",
        "Cierre",
        "sess-1",
        "file",
        [],
        "",
        "C:/scripts/cierre.bat",
        "once",
        {"target_iso": "2026-09-13T08:30"},
        True,
    )
    assert file_agent["automation_type"] == "file"
    assert file_agent["file_path"] == "C:/scripts/cierre.bat"


def test_update_agent(tmp_master_db):
    mi.create_agent(
        "a1",
        "A",
        "sess-1",
        "command",
        ["cmd"],
        "",
        "",
        "once",
        {"target_iso": "2026-09-13T08:30"},
        True,
    )
    up = mi.update_agent(
        "a1", {"label": "Renombrado", "enabled": False, "commands": ["otro"]}
    )
    assert up is not None
    assert up["label"] == "Renombrado"
    assert up["enabled"] is False
    assert up["commands"] == ["otro"]


def test_update_missing_agent_returns_none(tmp_master_db):
    assert mi.update_agent("no-existe", {"label": "x"}) is None
    assert mi.get_agent("no-existe") is None


def test_delete_agent_also_deletes_its_runs(tmp_master_db):
    mi.create_agent("a1", "A", "sess-1", "command", ["cmd"], "", "", "once", {}, True)
    mi.create_agent_run("r1", "a1", "sess-1", ["cmd"], ["turn-1"])
    assert mi.delete_agent("a1") is True
    assert mi.delete_agent("a1") is False
    assert mi.list_agents() == []
    assert mi.list_agent_runs(agent_id="a1") == []


def test_agent_runs_crud_multi_turn(tmp_master_db):
    mi.create_agent(
        "a1", "A", "sess-1", "command", ["cmd1", "cmd2"], "", "", "once", {}, True
    )
    run = mi.create_agent_run(
        "r1", "a1", "sess-1", ["cmd1", "cmd2"], ["turn-1", "turn-2"]
    )
    assert run["status"] == "running"
    assert run["turn_ids"] == ["turn-1", "turn-2"]
    assert mi.list_running_agent_runs() == [run]

    mi.finish_agent_run("r1", "done")
    assert mi.list_running_agent_runs() == []
    runs = mi.list_agent_runs(agent_id="a1")
    assert len(runs) == 1
    assert runs[0]["status"] == "done"
    assert runs[0]["finished_at"] is not None


def test_agent_run_error_keeps_message(tmp_master_db):
    mi.create_agent("a1", "A", "sess-1", "command", ["cmd"], "", "", "once", {}, True)
    mi.create_agent_run("r1", "a1", "sess-1", ["cmd"], [])
    mi.finish_agent_run("r1", "error", "el comando ya no existe")
    runs = mi.list_agent_runs()
    assert runs[0]["status"] == "error"
    assert runs[0]["error"] == "el comando ya no existe"


def test_list_agent_runs_pagination_and_count(tmp_master_db):
    mi.create_agent("a1", "A", "sess-1", "command", ["cmd"], "", "", "once", {}, True)
    for i in range(5):
        mi.create_agent_run(f"r{i}", "a1", "sess-1", ["cmd"], [f"turn-{i}"])
    assert mi.count_agent_runs(agent_id="a1") == 5
    page1 = mi.list_agent_runs(agent_id="a1", limit=2, offset=0)
    page2 = mi.list_agent_runs(agent_id="a1", limit=2, offset=2)
    assert len(page1) == 2
    assert len(page2) == 2
    assert {r["id"] for r in page1}.isdisjoint({r["id"] for r in page2})


def test_clear_finished_agent_runs_keeps_running(tmp_master_db):
    mi.create_agent("a1", "A", "sess-1", "command", ["cmd"], "", "", "once", {}, True)
    mi.create_agent_run("r-running", "a1", "sess-1", ["cmd"], ["turn-1"])
    mi.create_agent_run("r-done", "a1", "sess-1", ["cmd"], ["turn-2"])
    mi.finish_agent_run("r-done", "done")

    cleared = mi.clear_finished_agent_runs(agent_id="a1")
    assert cleared == 1
    remaining = mi.list_agent_runs(agent_id="a1")
    assert len(remaining) == 1
    assert remaining[0]["id"] == "r-running"


# -- next_due(): misma semantica que nextDueMs del frontend (app.js) --------
def test_next_due_once_future():
    agent = {"kind": "once", "trigger": {"target_iso": "2099-01-01T08:30"}}
    due = next_due(agent)
    assert due is not None and due > time.time()


def test_next_due_once_invalid_returns_none():
    agent = {"kind": "once", "trigger": {"target_iso": "no-es-una-fecha"}}
    assert next_due(agent) is None


def test_next_due_daily_is_always_in_future():
    agent = {"kind": "recurring", "trigger": {"rule": "daily", "time": "08:30"}}
    due = next_due(agent)
    assert due is not None and due > time.time()


def test_next_due_ndays_anchors_on_last_fired():
    now = time.time()
    agent = {
        "kind": "recurring",
        "trigger": {"rule": "ndays", "n": 2},
        "last_fired_at": now - 3600,  # disparo hace 1h
        "created_at": now - 100000,
    }
    due = next_due(agent)
    # proximo disparo: ~2 dias despues del ultimo fired (ya que 1h no paso el intervalo)
    assert due is not None
    assert due > now + (2 * 86400 - 3600 - 10)


def test_next_due_unknown_rule_returns_none():
    agent = {"kind": "recurring", "trigger": {"rule": "nope"}}
    assert next_due(agent) is None


# ---------------------------------------------------------------------------
# spec agentes-huerfanos-y-gestion-skills: sesion de reserva (REQ-001/002/004),
# deteccion de agente huerfano (REQ-003/005) y orden de desempate (REQ-006).
# ---------------------------------------------------------------------------
def test_count_agents_by_session(tmp_master_db):
    mi.create_agent("a1", "A", "sess-1", "command", ["cmd"], "", "", "once", {}, True)
    mi.create_agent("a2", "B", "sess-1", "command", ["cmd"], "", "", "once", {}, True)
    mi.create_agent("a3", "C", "sess-2", "command", ["cmd"], "", "", "once", {}, True)
    assert mi.count_agents_by_session("sess-1") == 2
    assert mi.count_agents_by_session("sess-2") == 1
    assert mi.count_agents_by_session("sess-inexistente") == 0


def test_reassign_agents_session_moves_all_in_bloc(tmp_master_db):
    """REQ-002: la reasignacion es SIEMPRE en bloque, nunca parcial."""
    mi.create_agent(
        "a1", "A", "sess-vieja", "command", ["cmd"], "", "", "once", {}, True
    )
    mi.create_agent(
        "a2", "B", "sess-vieja", "skill", [], "mi-skill", "", "once", {}, True
    )
    mi.create_agent(
        "a3", "C", "sess-otra", "command", ["cmd"], "", "", "once", {}, True
    )

    moved = mi.reassign_agents_session("sess-vieja", "sess-reserva")
    assert moved == 2
    assert mi.count_agents_by_session("sess-vieja") == 0
    assert mi.count_agents_by_session("sess-reserva") == 2
    # el agente de otra sesion no se toca
    assert mi.get_agent("a3")["session_id"] == "sess-otra"


def test_session_index_exists(tmp_master_db):
    assert mi.session_index_exists("no-existe") is False
    mi.set_project_path("sess-1", "C:/proyecto", "Mi sesión", "usuario1")
    assert mi.session_index_exists("sess-1") is True
    mi.delete_session("sess-1")
    assert mi.session_index_exists("sess-1") is False


def test_compute_agent_broken_status_session_missing(tmp_master_db):
    """REQ-003/005: sesion borrada (nunca existio en session_index, o se
    borro) -- se prioriza SIEMPRE por sobre comandos/skill (decision
    registrada en spec.md, seccion Reglas de negocio)."""
    agent = mi.create_agent(
        "a1", "A", "sess-borrada", "command", ["cmd"], "", "", "once", {}, True
    )
    broken = compute_agent_broken_status(agent)
    assert broken is not None
    assert broken["field"] == "session_id"


def test_compute_agent_broken_status_healthy_session_no_commands(tmp_master_db):
    """Agente sano: sesion existe, sin comandos configurados (no hay nada
    que validar en 'commands' si la lista esta vacia)."""
    mi.set_project_path("sess-1", "C:/proyecto", "Mi sesión", "usuario1")
    agent = mi.create_agent(
        "a1", "A", "sess-1", "command", [], "", "", "once", {}, True
    )
    assert compute_agent_broken_status(agent) is None


def test_compute_agent_broken_status_missing_skill(tmp_master_db, monkeypatch):
    """REQ-005: skill que ya no existe -- gap real que esta feature cierra
    (antes NUNCA se detectaba una skill faltante al listar, solo al
    disparar)."""
    from app.tools import skill as skill_module

    mi.set_project_path("sess-1", "C:/proyecto", "Mi sesión", "usuario1")
    monkeypatch.setattr(skill_module, "discover_skills", lambda: [])
    monkeypatch.setattr(skill_module, "discover_local_skills", lambda sid: [])

    agent = mi.create_agent(
        "a1", "A", "sess-1", "skill", [], "skill-fantasma", "", "once", {}, True
    )
    broken = compute_agent_broken_status(agent)
    assert broken is not None
    assert broken["field"] == "skill_name"


def test_compute_agent_broken_status_existing_skill_is_healthy(
    tmp_master_db, monkeypatch
):
    from app.tools import skill as skill_module

    mi.set_project_path("sess-1", "C:/proyecto", "Mi sesión", "usuario1")
    monkeypatch.setattr(
        skill_module,
        "discover_skills",
        lambda: [{"name": "mi-skill", "description": "", "path": ""}],
    )
    monkeypatch.setattr(skill_module, "discover_local_skills", lambda sid: [])

    agent = mi.create_agent(
        "a1", "A", "sess-1", "skill", [], "mi-skill", "", "once", {}, True
    )
    assert compute_agent_broken_status(agent) is None


def test_fire_order_prioritizes_oldest_created_on_tie(tmp_master_db):
    """REQ-006 (documentado a pedido explicito del usuario): si dos agentes
    vencen exactamente en el mismo instante, el CREADO ANTES se dispara
    primero -- efecto de `list_agents() ORDER BY created_at ASC` +
    preservacion de orden de insercion de dict (garantia de Python 3.7+)."""
    mi.create_agent(
        "a-viejo", "Viejo", "sess-1", "command", ["cmd"], "", "", "once", {}, True
    )
    mi.create_agent(
        "a-nuevo", "Nuevo", "sess-1", "command", ["cmd"], "", "", "once", {}, True
    )

    agents = mi.list_agents()
    assert [a["id"] for a in agents] == ["a-viejo", "a-nuevo"]

    # Mismo mecanismo que _check_and_fire_due_agents(): dict comprehension
    # sobre list_agents() preserva el orden -- el primero en iterar es el
    # primero en dispararse en caso de empate real de horario.
    agents_by_id = {a["id"]: a for a in agents}
    assert list(agents_by_id.keys()) == ["a-viejo", "a-nuevo"]
