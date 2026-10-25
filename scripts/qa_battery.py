"""Bateria de control de codigo de Vision (QA).

Ejecucion ad-hoc contra el repo, sin dependencias de terceros salvo ruff (opcional).
Uso:
  python scripts/qa_battery.py            # estatico (sin backend)
  python scripts/qa_battery.py --runtime  # ademas pega a http://127.0.0.1:8030

Cubre (metodologia del AGENTS.md de vision-web: no hay test suite formal):
  1. Cross-check estatico: todo `getElementById("x")` de frontend/js/*.js
     debe existir como id en frontend/index.html (y en otros .html si se agregan).
  2. Balance de sintaxis JS (braces/parillas/parentesis) ignorando strings,
     template literals y comentarios -- heuristico, valida que no quede una
     edicion cortada a la mitad (bug real de buffer ya visto en este repo).
  3. Byte-compile de todo backend/app (python -m compileall).
  4. Integridad del registry de tools: app.tools.build_registry() carga Y reporta
     al menos N tools (<= count real). Aqui 21.
  5. (--runtime) Smoke de endpoints clave de la API.
  6. (--runtime) Carga real en un navegador Chromium (Playwright) de "/" --
     cero errores de consola/pageerror. Agregado tras un bug real
     (2026-09-19): una variable JS duplicada (`let` repetido, error
     SEMANTICO invisible para js_balance()) rompio el modulo app.js
     ENTERO y con el la inicializacion del splash-screen -- solo se
     detecta cargando la pagina en un navegador de verdad.

Las ultimas modificaciones protegidas por esta bateria:
  - Bug click skill desde acceso rapido (app.js insertSlashSkill): el caso
    compositor vacio debe insertar "/skill <nombre> " (no vaciar el input).
  - Boton "Probar alarma" (cfg-loop-alarm-test) presente en HTML y cableado.
  - Control de alarma del loop (tipo/frecuencia/volumen) referenciados y presentes.
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
API = "http://127.0.0.1:8030"

PASS = "PASS"
FAIL = "FAIL"


def check(name: str, ok: bool, detail: str = "") -> bool:
    tag = PASS if ok else FAIL
    print(f"[{tag}] {name}" + (f" -- {detail}" if detail and not ok else ""))
    return ok


def js_getelement_ids(text: str) -> set[str]:
    return set(re.findall(r'getElementById\("([^"]+)"\)', text))


def html_ids(text: str) -> set[str]:
    return set(re.findall(r'id="([^"]+)"', text))


def js_dynamic_ids(text: str) -> set[str]:
    """Ids creados en runtime vía `algo.id = "x"` (no estan en el HTML estático)."""
    return set(re.findall(r'\.\w*id\s*=\s*"([^"]+)"', text))


def cross_reference_js_html() -> bool:
    js_files = sorted((ROOT / "frontend/js").glob("*.js"))
    html_text = (ROOT / "frontend/index.html").read_text(encoding="utf-8")
    html_ids_set = html_ids(html_text)
    dynamic_ids: set[str] = set()
    for jf in js_files:
        dynamic_ids |= js_dynamic_ids(jf.read_text(encoding="utf-8"))
    ok = True
    for jf in js_files:
        text = jf.read_text(encoding="utf-8")
        refs = js_getelement_ids(text)
        missing = (refs - html_ids_set) - dynamic_ids
        if missing:
            ok = False
            check(
                f"getElementById en {jf.name}",
                False,
                "faltan en index.html: " + ", ".join(sorted(missing)),
            )
        else:
            check(f"getElementById en {jf.name}", True)
    return ok


def _prev_is_value(text: str, i: int) -> bool:
    """True si el token anterior sugiere DIVISION (no regex) en `/`."""
    j = i - 1
    while j >= 0 and text[j] in " \t\n":
        j -= 1
    if j < 0:
        return False
    return (
        text[j]
        in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_)$]}'\">"
    )


def _skip_regex(text: str, i: int, n: int) -> int:
    """Avanza el indice sobre un regex literal hasta despues del '/cierre' + flags."""
    i += 1
    in_class = False
    while i < n:
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if c == "\n":
            return i  # regex raro; no cronpiar mas alla de la linea
        if c == "[":
            in_class = True
        elif c == "]":
            in_class = False
        elif c == "/" and not in_class:
            i += 1
            while i < n and text[i] in "dgimsuvy":
                i += 1
            return i
        i += 1
    return i


def js_balance(path: Path) -> tuple[bool, str]:
    """Heuristico de balance de { } ( ) [ ] fuera de strings/comentarios."""
    text = path.read_text(encoding="utf-8")
    i, n = 0, len(text)
    stack: list[str] = []
    pairs = {")": "(", "]": "[", "}": "{"}
    opens = set("([{")
    line = 1
    while i < n:
        c = text[i]
        if c == "\n":
            line += 1
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
                if text[i] == "\n":
                    line += 1
            i += 2
            continue
        if c in "\"'`":
            quote = c
            i += 1
            while i < n:
                e = text[i]
                if e == "\\":
                    i += 2
                    continue
                if quote == "`" and e == "${":
                    # saltar template literal: empujar brace
                    j = text.find("}", i + 2)
                    if j != -1:
                        i = j + 1
                        continue
                    i += 1
                    continue
                if e == quote:
                    break
                i += 1
            i += 1
            continue
        if c == "/" and not _prev_is_value(text, i):
            # regex literal: saltar hasta el "/" de cierre (respetando \escapes
            # y clases [...]) e ignorar flags. Evita falsos positivos tipo
            # /[&<>"']/g con comillas dentro de la regex.
            i = _skip_regex(text, i, n)
            continue
        if c in opens:
            stack.append(c)
        elif c in pairs:
            if not stack or stack[-1] != pairs[c]:
                return (
                    False,
                    f"{path.name}:{line} desbalance (esperaba {pairs[c]}, hay {c})",
                )
            stack.pop()
        i += 1
    if stack:
        return False, f"{path.name} cierra con sin cerrar: {stack}"
    return True, ""


def js_checks() -> bool:
    # --all-js balancea TODOS los .js; por defecto solo los editados con mas
    # riesgo de corte (app.js). El lexer heuristico en stdlib es fragile ante
    # regex/division y template literals anidados (falsos positivos en
    # markdown.js sin node/bun disponibles para un parseo real).
    if "--all-js" in sys.argv:
        targets = sorted((ROOT / "frontend/js").glob("*.js"))
    else:
        targets = [ROOT / "frontend/js/app.js"]
    ok = True
    for jf in targets:
        bal, msg = js_balance(jf)
        ok = check(f"balance sintaxis JS {jf.name}", bal, msg) and ok
    return ok


def py_compile() -> bool:
    from py_compile import compile as pyc

    ok = True
    count = 0
    for p in sorted((ROOT / "backend/app").rglob("*.py")):
        try:
            pyc(str(p), doraise=True)
            count += 1
        except Exception as exc:  # noqa: BLE001
            ok = check(f"py_compile {p.relative_to(ROOT)}", False, str(exc)) and ok
    check(f"py_compile backend/app ({count} archivos)", ok)
    return ok


def tool_registry(expect: int = 21) -> bool:
    code = (
        "import sys; sys.path.insert(0, 'backend'); "
        "from app.tools import build_registry; "
        "print(len(build_registry().all()))"
    )
    try:
        out = subprocess.run(
            [str(ROOT / "backend/.venv/Scripts/python.exe"), "-c", code],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
        ).stdout.strip()
        return check(
            f"registry tools == {expect}", out == str(expect), f"obtuvo: {out}"
        )
    except FileNotFoundError:
        return check("venv python para registry", False, "backend/.venv no encontrado")


def recent_mods() -> bool:
    """Aserciones puntuales de las ultimas modificaciones (2026-09):
    - Bug click skill desde acceso rapido (compositor vacio) -> inserta
      '/skill <nombre> ' en vez de vaciar el input.
    - Boton 'Probar alarma' (cfg-loop-alarm-test) presente y cableado.
    - Controles de alarma del loop (tipo/frecuencia/volumen) presentes.
    - Programador de alarmas (Tiempo/Agenda) + cobertura de otras features.
    """
    js = (ROOT / "frontend/js/app.js").read_text(encoding="utf-8")
    html = (ROOT / "frontend/index.html").read_text(encoding="utf-8")
    main_py = (ROOT / "backend/app/main.py").read_text(encoding="utf-8")
    ok = True

    ok = (
        check(
            "skill click: rama compositor vacio inserta /skill",
            "hasSlashToken" in js and "/skill ${skillName} " in js,
        )
        and ok
    )
    ok = (
        check("skill click: usa requestSubmit para enviar", "requestSubmit()" in js)
        and ok
    )
    ok = (
        check(
            "boton Probar alarma presente en HTML", 'id="cfg-loop-alarm-test"' in html
        )
        and ok
    )
    ok = (
        check(
            "boton Probar alarma cableado (playLoopAlarm)",
            "cfgLoopAlarmTestEl" in js
            and 'addEventListener("click", playLoopAlarm)' in js,
        )
        and ok
    )
    for elid in (
        "cfg-loop-alarm-type",
        "cfg-loop-alarm-interval",
        "cfg-loop-alarm-volume",
    ):
        ok = (
            check(f"control loop alarm '{elid}' en HTML", f'id="{elid}"' in html) and ok
        )
        ok = (
            check(
                f"control loop alarm '{elid}' referenciado en app.js", f'"{elid}"' in js
            )
            and ok
        )

    # REQ actualizado tras el rename "Tiempo/Agenda" -> pestaña unica
    # "Agentes" (ids alarm-* -> agent-*, ver app.js:5432-5446) y la
    # migracion del motor de disparo de alarmas de frontend (setInterval
    # scheduleTick) a backend (app/scheduler/alarms.py, run_alarms_loop) --
    # estos checks estaban comprobando ids/engine que ya no existen.
    for elid in (
        "agents-list",
        "agent-save",
        "agent-cancel-edit",
        "alarm-toast",
        "agent-kind",
        "agent-file",
    ):
        ok = check(f"programador: '{elid}' en HTML", f'id="{elid}"' in html) and ok
    ok = (
        check("programador: pestaña data-tab=agentes", 'data-tab="agentes"' in html)
        and ok
    )
    ok = (
        check(
            "programador: engine backend agentes (run_agents_loop)",
            "async def run_agents_loop"
            in (ROOT / "backend/app/scheduler/agents.py").read_text(encoding="utf-8"),
        )
        and ok
    )
    ok = (
        check(
            "programador: engine backend alarmas (run_alarms_loop, dispara con backend caido/navegador cerrado)",
            "async def run_alarms_loop"
            in (ROOT / "backend/app/scheduler/alarms.py").read_text(encoding="utf-8"),
        )
        and ok
    )
    ok = (
        check("programador: persistencia GLOBAL (API /api/alarms)", "/api/alarms" in js)
        and ok
    )
    ok = (
        check(
            "programador: endpoints backend globales",
            '@app.get("/api/alarms")' in main_py,
        )
        and ok
    )
    ok = (
        check(
            "programador: campo archivo a ejecutar",
            'id="agent-file"' in html and "agentFileEl" in js,
        )
        and ok
    )
    ok = (
        check(
            "programador: is_due_now (no next_due) decide el disparo -- next_due() siempre da futuro",
            "def is_due_now"
            in (ROOT / "backend/app/scheduler/agents.py").read_text(encoding="utf-8"),
        )
        and ok
    )

    ok = (
        check(
            "modo cerebro: controles en HTML",
            'id="cfg-brain-autosend-enabled"' in html
            and 'id="cfg-brain-autosend-pause"' in html,
        )
        and ok
    )
    ok = check("modo cerebro: cableado en app.js", "cfgBrainAutosend" in js) and ok

    ok = (
        check(
            "voz/tts: toggle en HTML + prueba en app.js",
            'id="cfg-tts-enabled"' in html and "speakTestPhrase" in js,
        )
        and ok
    )
    ok = (
        check(
            "explorador-archivos: picker nativo + openExplorer en app.js",
            "native-picker" in js and "openExplorer" in js,
        )
        and ok
    )
    ok = (
        check(
            "cola-mensajes: user-turn controls en app.js",
            "buildUserTurnControls" in js and "user-turn-controls" in js,
        )
        and ok
    )
    ok = (
        check(
            "reproducir-rangos: boton play en app.js",
            "msg-play-btn" in js or "handlePlayMessageClick" in js,
        )
        and ok
    )
    ok = (
        check(
            "limpiar-conversacion: /clear cableado",
            "/clear" in js or "clearConversation" in js,
        )
        and ok
    )
    ok = (
        check(
            "comandos-export-compact: endpoints en app.js",
            "commands/export" in js and "commands/compact" in js,
        )
        and ok
    )
    ok = (
        check(
            "timestamps-mensajes: buildMessageTimeEl en app.js",
            "buildMessageTimeEl" in js,
        )
        and ok
    )
    ok = (
        check(
            "conectores: 5 archivos en connectors/",
            all(
                (ROOT / "backend/app/connectors" / f).exists()
                for f in (
                    "oracle_connector.py",
                    "bigquery_connector.py",
                    "sharepoint_connector.py",
                    "sap_connector.py",
                    "graph_connector.py",
                )
            ),
        )
        and ok
    )

    # Fix 2026-09-19 #1: dev/Iniciar-Vision.bat no abria el navegador solo
    # (a diferencia de prod/Iniciar-Vision.bat y Vision.exe, que corren
    # launch_vision.py). Verifica que exista el script standalone de
    # polling+open y que el .bat de dev realmente lo invoque ANTES de la
    # linea de uvicorn.
    dev_bat = (ROOT / "Iniciar-Vision.bat").read_text(encoding="utf-8")
    wait_open_script = ROOT / "backend/scripts/wait_and_open_browser.py"
    ok = (
        check(
            "auto-abrir navegador: script wait_and_open_browser.py existe",
            wait_open_script.exists(),
        )
        and ok
    )
    ok = (
        check(
            "auto-abrir navegador: dev/Iniciar-Vision.bat lo invoca",
            "wait_and_open_browser.py" in dev_bat,
        )
        and ok
    )
    launch_vision_src = (ROOT / "launch_vision.py").read_text(encoding="utf-8")
    ok = (
        check(
            "auto-abrir navegador: launch_vision.py abre webbrowser al responder",
            "_open_browser_when_ready" in launch_vision_src
            and "webbrowser.open" in launch_vision_src,
        )
        and ok
    )

    # Fix 2026-09-19 #2: el picker nativo de carpeta (tkinter) quedaba
    # detras del navegador -- topmost solo no alcanza en Windows
    # (LockSetForegroundWindow). Verifica que el fix de foreground real
    # (truco de ALT + SetForegroundWindow) siga presente en main.py.
    main_py_full = (ROOT / "backend/app/main.py").read_text(encoding="utf-8")
    ok = (
        check(
            "picker nativo: _force_foreground_window definida",
            "_force_foreground_window" in main_py_full,
        )
        and ok
    )
    ok = (
        check(
            "picker nativo: SetForegroundWindow + truco ALT (keybd_event)",
            "SetForegroundWindow" in main_py_full and "keybd_event" in main_py_full,
        )
        and ok
    )
    ok = (
        check(
            "picker nativo: _pick() llama lift/focus_force/_force_foreground_window",
            "root.lift()" in main_py_full
            and "root.focus_force()" in main_py_full
            and "_force_foreground_window(root)" in main_py_full,
        )
        and ok
    )

    # spec agentes-huerfanos-y-gestion-skills (2026-09-19): sesion de
    # reserva + tag rojo por config invalida + reemplazo del sync de
    # Skills (Git -> carpeta local ya existente) + panel CRUD de skills
    # locales. REQ-001 a 009.
    agents_py_full = (ROOT / "backend/app/scheduler/agents.py").read_text(
        encoding="utf-8"
    )
    ok = (
        check(
            "REQ-001: sesion de reserva -- get_fallback_session_id + clave app_settings",
            "def get_fallback_session_id" in agents_py_full
            and "AGENT_FALLBACK_SESSION_KEY" in agents_py_full,
        )
        and ok
    )
    ok = (
        check(
            "REQ-001: endpoints GET/POST /api/settings/agent-fallback-session",
            '@app.get("/api/settings/agent-fallback-session")' in main_py_full
            and '@app.post("/api/settings/agent-fallback-session")' in main_py_full,
        )
        and ok
    )
    master_index_full = (ROOT / "backend/app/persistence/master_index.py").read_text(
        encoding="utf-8"
    )
    ok = (
        check(
            "REQ-002: reassign_agents_session (reasignacion en bloque, nunca parcial)",
            "def reassign_agents_session" in master_index_full,
        )
        and ok
    )
    ok = (
        check(
            "REQ-002/004: GET /api/session/{id}/agent-impact + DELETE bloquea reserva con dependientes",
            "/agent-impact" in main_py_full and "is_fallback_blocked" in main_py_full,
        )
        and ok
    )
    ok = (
        check(
            "REQ-005: compute_agent_broken_status detecta sesion/comando/skill roto",
            "def compute_agent_broken_status" in agents_py_full,
        )
        and ok
    )
    ok = (
        check(
            "REQ-005: GET /api/agents adjunta 'broken' por agente",
            'agent["broken"] = compute_agent_broken_status(agent)' in main_py_full,
        )
        and ok
    )
    ok = (
        check(
            "REQ-005 (frontend): tag rojo data-broken + resaltado de campo en formulario",
            "data-broken" in (ROOT / "frontend/js/app.js").read_text(encoding="utf-8")
            and "#agents-list .schedule-item[data-broken]"
            in (ROOT / "frontend/css/styles.css").read_text(encoding="utf-8"),
        )
        and ok
    )
    ok = (
        check(
            "REQ-006: orden de desempate documentado + test de regresion",
            "REQ-006" in agents_py_full
            and "test_fire_order_prioritizes_oldest_created_on_tie"
            in (ROOT / "backend/tests/test_agents_db.py").read_text(encoding="utf-8"),
        )
        and ok
    )
    ok = (
        check(
            "REQ-007: sync Git de skills ELIMINADO (skills_repo.py + boton GitHub)",
            not (ROOT / "backend/app/skills_repo.py").exists()
            and "cfg-skills-sync"
            not in (ROOT / "frontend/index.html").read_text(encoding="utf-8"),
        )
        and ok
    )
    skill_py_full = (ROOT / "backend/app/tools/skill.py").read_text(encoding="utf-8")
    ok = (
        check(
            "REQ-008: CRUD de skills locales (save/delete_local_skill)",
            "def save_local_skill" in skill_py_full
            and "def delete_local_skill" in skill_py_full,
        )
        and ok
    )
    ok = (
        check(
            "REQ-008: panel Skills con listado global read-only + local CRUD en HTML",
            "cfg-skills-global-list" in html
            and "cfg-skills-local-list" in html
            and "local-skill-editor-overlay" in html,
        )
        and ok
    )
    ok = (
        check(
            "REQ-009: aviso de agentes en uso al borrar skill local (frontend)",
            "_countAgentsUsingSkill"
            in (ROOT / "frontend/js/app.js").read_text(encoding="utf-8"),
        )
        and ok
    )

    # spec configuracion-limpieza-credenciales-servidor (2026-09-19):
    # REQ-101/102 -- eliminacion INTENCIONAL de las pestañas Pomodoro y
    # Sitio SharePoint. Estos checks confirman que la eliminacion sigue
    # firme (no que fueron reintroducidas sin querer, ej. por un merge o
    # una copia vieja de index.html) -- lo opuesto de la mayoria de los
    # checks de esta bateria, que verifican PRESENCIA de algo.
    app_js_full = (ROOT / "frontend/js/app.js").read_text(encoding="utf-8")
    ok = (
        check(
            "REQ-101: pestaña Pomodoro eliminada (sin data-tab=pomodoro ni motor JS)",
            'data-tab="pomodoro"' not in html
            and "function pomodoroStart" not in app_js_full,
        )
        and ok
    )
    ok = (
        check(
            "REQ-102: pestaña Sitio SharePoint eliminada de la UI (backend intacto)",
            'data-tab="sharepoint"' not in html
            and "loadSharepointSettingsPane" not in app_js_full
            # el backend NO debe tocarse -- si desaparece, es una regresion de alcance
            and "sharepoint_drive_id"
            in (ROOT / "backend/app/project_context.py").read_text(encoding="utf-8"),
        )
        and ok
    )

    # REQ-103/104/105/106: rediseño data-driven de Credenciales.
    credentials_py_full = (ROOT / "backend/app/credentials.py").read_text(
        encoding="utf-8"
    )
    ok = (
        check(
            "REQ-103: CONNECTOR_SCHEMA cubre los 4 conectores (22 campos reales)",
            "CONNECTOR_SCHEMA" in credentials_py_full
            and "def get_connector_schema" in credentials_py_full,
        )
        and ok
    )
    ok = (
        check(
            "REQ-103: endpoint GET /api/config/credentials/schema",
            '@app.get("/api/config/credentials/schema")' in main_py_full,
        )
        and ok
    )
    ok = (
        check(
            "REQ-104: acordeon de Credenciales generado 100% dinamico (sin inputs hardcodeados)",
            "cred-accordion" in html
            and 'id="cred-oracle-user"'
            not in html  # el viejo input hardcodeado ya NO debe existir
            and "_buildCredAccordionSection" in app_js_full,
        )
        and ok
    )
    ok = (
        check(
            "REQ-105: guardar POR SECCION (payload {fields: ...}, no un solo POST fijo)",
            "_saveConnectorCredentials" in app_js_full and "req.fields" in main_py_full,
        )
        and ok
    )
    ok = (
        check(
            "REQ-106: endpoint de probar conexion por conector",
            '@app.post("/api/config/credentials/test/{connector}")' in main_py_full
            and "_testConnectorConnection" in app_js_full,
        )
        and ok
    )

    # REQ-107/108 REVERTIDOS (2026-09-20): la deteccion de IP local para
    # conectarse desde el celular se elimino -- exponer el servidor a la
    # LAN (0.0.0.0) exigia ademas una regla de firewall de Windows con
    # privilegios de administrador, y el usuario decidio explicitamente NO
    # automatizar la apertura de un puerto de entrada en una notebook
    # corporativa de la empresa. Mismo patron ya usado para Pomodoro/SharePoint:
    # estos checks confirman que la reversion sigue firme (no que fue
    # reintroducida sin querer, ej. por un merge o una copia vieja).
    ok = (
        check(
            "REQ-107/108: deteccion de IP local ELIMINADA (network_info.py no existe)",
            not (ROOT / "backend/app/network_info.py").exists()
            and '@app.get("/api/network/local-ips")' not in main_py_full
            and "cfg-server-ips-list" not in html
            and "loadServerIpsList" not in app_js_full,
        )
        and ok
    )
    ok = (
        check(
            "REQ-109: servidor de dev sigue bindeado a 127.0.0.1 (loopback, sin exponer a la LAN)",
            "--host 127.0.0.1"
            in (ROOT / "Iniciar-Vision.bat").read_text(encoding="utf-8"),
        )
        and ok
    )

    # Bug real encontrado en auditoria de codigo (2026-09-20, prueba de
    # agentes del usuario: "cuando cambio de sesion se ven command-progress/
    # mensajes de la sesion original mezclados"): handleEvent() (el switch
    # central de eventos SSE) filtra la mayoria de los casos por
    # `ctx.turnSessionId === state.sessionId`, pero 4 casos NO lo hacian
    # ("progress-estimate", "error", "stopped", "done") -- un turno
    # corriendo en background (ej. un agente) contaminaba la UI de la
    # sesion que el usuario esta mirando. Verificado en vivo con Playwright
    # (turno real de 10s en una sesion, sesion distinta visible durante
    # ese lapso: 0 barras de progreso/mensajes filtrados). El guard esta
    # DENTRO de cada `case`, no antes -- se verifica que el bloque de texto
    # entre cada cabecera de case y la siguiente contenga el guard.
    handle_event_start = app_js_full.index("function handleEvent(event, ctx)")
    # Bug real en este MISMO check (encontrado al correrlo por primera vez):
    # un rango fijo de 8000 caracteres no alcanzaba a cubrir toda la
    # funcion (~14280 caracteres reales, con ~15 casos y comentarios
    # extensos) -- "stopped"/"done" quedaban directamente fuera del texto
    # analizado. Se usa el inicio de la PROXIMA funcion de nivel de modulo
    # como limite real, en vez de un numero inventado.
    next_fn_start = app_js_full.find("\nfunction ", handle_event_start + 100)
    handle_event_body = app_js_full[handle_event_start:next_fn_start]
    for case_name in ("progress-estimate", "error", "stopped", "done"):
        case_marker = f'case "{case_name}":'
        case_start = handle_event_body.find(case_marker)
        next_case_start = (
            handle_event_body.find("case ", case_start + len(case_marker))
            if case_start >= 0
            else -1
        )
        if case_start >= 0 and next_case_start == -1:
            next_case_start = len(handle_event_body)  # ultimo case de la funcion
        case_block = (
            handle_event_body[case_start:next_case_start] if case_start >= 0 else ""
        )
        ok = (
            check(
                f'handleEvent case "{case_name}": tiene el guard de sesion activa',
                case_start >= 0
                and "if (ctx.turnSessionId !== state.sessionId) break;" in case_block,
            )
            and ok
        )

    # Bug real reportado por el usuario (2026-09-20, mismo dia): "la
    # respuesta de las 16:15 se coloco encima de la respuesta de las
    # 16:14" -- turnos consecutivos del modo Loop autonomo dentro de UNA
    # SOLA conexion SSE (`_drain_session_queue` los encadena sin cortar)
    # se mezclaban/pisaban en el DOM porque `renderIncomingStream`
    # resolvia `assistantDiv` una sola vez al principio. Fix:
    # `closeCurrentAssistantTurn()` (cierra+persiste+RESETEA el estado)
    # se llama en "done" Y "error" -- verificado en vivo con Playwright
    # mockeando 2 turnos consecutivos en el mismo stream SSE: sin el fix,
    # ambas respuestas se mezclaban en una sola burbuja; con el fix,
    # quedan en 2 burbujas separadas y en orden correcto.
    ok = (
        check(
            'handleEvent: closeCurrentAssistantTurn() definida y usada en "done"/"error" (fix desorden turnos Loop)',
            "const closeCurrentAssistantTurn = ()" in app_js_full
            and app_js_full.count("ctx.closeCurrentAssistantTurn();") == 2,
        )
        and ok
    )

    # Bug real reportado por el usuario (2026-09-20, MISMO DIA, distinto del
    # bug de arriba): "Error de conexion: closeCurrentAssistantTurn is not
    # defined". Causa raiz: la funcion estaba declarada con `const` DENTRO
    # del `for (const part of parts)` (scope de BLOQUE), pero se llamaba
    # tambien COMO RED DE SEGURIDAD despues del `while` que envuelve a ese
    # `for` (ver el ultimo llamado de renderIncomingStream). Si el stream se
    # cortaba antes de que el `for` interno corriera una sola vez (0 bytes
    # utiles, o ningun "data:" valido), esa declaracion nunca llegaba a
    # existir en un scope alcanzable desde la red de seguridad ->
    # ReferenceError. Fix: se declara UNA SOLA VEZ, en el scope de TODA
    # `renderIncomingStream` (antes del `while`), nunca dentro del `for`.
    # Esto es DISTINTO del check de arriba (que solo confirma que la
    # funcion EXISTE y se llama 2 veces) -- ese check ya pasaba ANTES del
    # fix, porque "const closeCurrentAssistantTurn = ()" seguia apareciendo
    # en el archivo (solo que en el scope equivocado). Se verifica
    # especificamente que la declaracion este en el bloque de
    # renderIncomingStream ANTERIOR al `while (true) {`, y que NO quede
    # ninguna segunda declaracion (duplicada) dentro del `for`.
    render_stream_start = app_js_full.find("async function renderIncomingStream(")
    while_start = app_js_full.find("while (true) {", render_stream_start)
    render_stream_preamble = app_js_full[render_stream_start:while_start]
    declarations_in_file = app_js_full.count("const closeCurrentAssistantTurn = ()")
    ok = (
        check(
            "renderIncomingStream: closeCurrentAssistantTurn declarada en scope de "
            "funcion, ANTES del while (fix ReferenceError conexion cortada temprano)",
            render_stream_start != -1
            and while_start != -1
            and "const closeCurrentAssistantTurn = ()" in render_stream_preamble
            and declarations_in_file == 1,
        )
        and ok
    )

    # Bug real reportado por el usuario (2026-09-20): "el boton send-btn-
    # label se queda en detener y luego en deteniendo... queda colgado
    # ahi... tengo que refrescar". Causa: POST /api/chat/{id}/stop
    # devuelve `{"stopped": false, "reason": "no_active_turn"}` SIN emitir
    # ningun evento SSE cuando ya no hay turno activo -- el frontend
    # dependia EXCLUSIVAMENTE del evento 'stopped' para volver a "idle".
    # Verificado en vivo con Playwright: sin el fix el boton queda pegado
    # en data-state="stopping"; con el fix vuelve a "idle" apenas el
    # backend contesta stopped:false.
    ok = (
        check(
            "boton Detener: resetea a idle si el backend contesta stopped:false (fix boton colgado)",
            "if (data.stopped === false)" in app_js_full
            and 'setSendButtonState("idle");' in app_js_full,
        )
        and ok
    )

    # spec controles-seguridad-optimizacion-lightspeed-bigquery -- 3 bugs
    # reales reportados por el usuario el 2026-09-21, todos en la misma
    # sesion de trabajo, sobre la feature de guardrails/export de BigQuery
    # implementada previamente:
    dbquery_py = (ROOT / "backend" / "app" / "tools" / "dbquery.py").read_text(
        encoding="utf-8"
    )
    main_py_full = (ROOT / "backend" / "app" / "main.py").read_text(encoding="utf-8")

    # Bug 1: "el enlace no deberia descargarmelo de nuevo, deberia abrirme
    # el archivo sin descargar". FileResponse de Starlette usa
    # Content-Disposition: attachment por defecto (fuerza descarga) --
    # el endpoint de exports debe pedir explicitamente "inline".
    ok = (
        check(
            'endpoint de exports usa content_disposition_type="inline" (fix descarga forzada)',
            'content_disposition_type="inline"' in main_py_full,
        )
        and ok
    )

    # Bug 2: "el sistema de seguridad de BigQuery mantiene el bloqueo...
    # incluso tras tu confirmacion". El guardrail de COSTO (REQ-002) no
    # tenia mecanismo de aprobacion -- a diferencia del de RIESGO (REQ-003,
    # que ya usaba bq_risky_seen). Se agrego bq_cost_approved en
    # SessionState + el chequeo simetrico en _bigquery_dry_run_check.
    events_py = (ROOT / "backend" / "app" / "session" / "events.py").read_text(
        encoding="utf-8"
    )
    ok = (
        check(
            "SessionState.bq_cost_approved existe (fix guardrail de costo sin memoria de aprobacion)",
            "bq_cost_approved: set[str]" in events_py,
        )
        and ok
    )
    ok = (
        check(
            "_bigquery_dry_run_check consulta bq_cost_approved antes de bloquear de nuevo",
            "cost_approved = (" in dbquery_py and "bq_cost_approved" in dbquery_py,
        )
        and ok
    )

    # Bug 3: pidio "el detalle" de 181 contenedores, el LLM puso su propio
    # `LIMIT 50` en el SQL y el export automatico nunca se disparo porque
    # el DataFrame ya llegaba recortado a 50 filas -- se agrego deteccion
    # de un tope explicito del propio SQL + aviso al agente.
    ok = (
        check(
            "_detect_explicit_row_cap existe y _df_to_text la usa (fix LIMIT del LLM oculta filas)",
            "def _detect_explicit_row_cap(" in dbquery_py
            and "explicit_cap = _detect_explicit_row_cap(query)" in dbquery_py,
        )
        and ok
    )

    # Pedido explicito del usuario: poder configurar desde la pestaña
    # "Conexiones" el umbral que determina "consulta pesada", con status
    # por color de cada conector y precarga de datasets/tablas/listas.
    index_html_full = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    ok = (
        check(
            'pestaña "Conexiones" existe en index.html (data-tab="connections")',
            'data-tab="connections"' in index_html_full
            and 'data-pane="connections"' in index_html_full,
        )
        and ok
    )
    ok = (
        check(
            "endpoints GET/POST /api/config/connections + precarga bigquery/sharepoint",
            '@app.get("/api/config/connections")' in main_py_full
            and '@app.post("/api/config/connections")' in main_py_full
            and '@app.get("/api/config/connections/bigquery/datasets")' in main_py_full
            and '@app.get("/api/config/connections/bigquery/tables")' in main_py_full
            and '@app.get("/api/config/connections/sharepoint/lists")' in main_py_full,
        )
        and ok
    )
    ok = (
        check(
            "_effective_bq_cost_threshold/_effective_export_* resuelven override de ProjectConfig",
            "def _effective_bq_cost_threshold(" in dbquery_py
            and "def _effective_export_row_threshold(" in dbquery_py
            and "def _effective_export_preview_rows(" in dbquery_py,
        )
        and ok
    )
    ok = (
        check(
            "loadConnectionsSettingsPane cableada en switchSettingsTab (carga bajo demanda)",
            "loadConnectionsSettingsPane()" in app_js_full
            and 'tabName === "connections"' in app_js_full,
        )
        and ok
    )
    return ok


def runtime_smoke() -> bool:
    import json
    import urllib.error
    import urllib.request

    ok = True
    endpoints = [
        "/api/status",
        "/api/models",
        "/api/connectors/status",
        "/api/skills",
        "/api/config/project",
        "/api/config/generation",
        "/api/fs/roots",
    ]
    for ep in endpoints:
        try:
            with urllib.request.urlopen(f"{API}{ep}", timeout=10) as r:
                code = r.status
                detail = ""
                if ep == "/api/status":
                    try:
                        data = json.loads(r.read())
                        ok = (
                            check("/api/status trae version", bool(data.get("version")))
                            and ok
                        )
                    except Exception:  # noqa: BLE001
                        pass
            ok = check(f"GET {ep} HTTP {code}", code < 500, f"code {code}")
            if code >= 500:
                ok = False
        except urllib.error.HTTPError as e:
            # 4xx esperados (ej. falta estado de sesion) NO son bug; 5xx si.
            check(f"GET {ep} HTTP {e.code}", e.code < 500, f"HTTPError {e.code}")
            ok = e.code < 500 and ok
        except Exception as exc:  # noqa: BLE001
            check(f"GET {ep}", False, str(exc))
            ok = False
    return ok


def browser_console_check() -> bool:
    """Gap real encontrado en QA manual (2026-09-19): el usuario reporto
    "se rompio el lanzador del ojo" (splash-screen con Three.js) tras un
    cambio que introdujo `let _localSkillsCache` DOS VECES en app.js --
    un SyntaxError de "Identifier already declared" que corta el modulo
    ENTERO desde el parseo (initSplashScreen(), al final del archivo,
    nunca llega a ejecutarse). Ninguno de los checks estaticos existentes
    (js_balance(), cross_reference_js_html()) puede detectar esto: el
    archivo esta perfectamente balanceado en llaves/parentesis, el error
    es SEMANTICO (variable duplicada en el mismo scope), no sintactico en
    el sentido de simbolos.

    Unica forma real de atraparlo: cargar la pagina en un navegador de
    VERDAD (Playwright, ya instalado en .venv) y mirar la consola. Solo
    corre en --runtime (necesita backend levantado Y un navegador
    Chromium disponible) -- si Playwright no esta instalado, se salta
    con un aviso en vez de fallar la bateria entera (mismo criterio
    'mejor esfuerzo' que el resto de runtime_smoke)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return check("navegador real: playwright no instalado, chequeo salteado", True)

    errors: list[str] = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.on(
                "console",
                lambda msg: (
                    errors.append(f"[{msg.type}] {msg.text}")
                    if msg.type == "error"
                    else None
                ),
            )
            page.on("pageerror", lambda exc: errors.append(f"[pageerror] {exc}"))
            page.goto(f"{API}/", wait_until="networkidle", timeout=15000)
            page.wait_for_timeout(2000)
            splash_ok = page.is_visible("#splash-screen")
            eye_has_content = (
                page.eval_on_selector(
                    "#splash-eye-container", "el => el.innerHTML.length"
                )
                > 0
            )
            browser.close()
    except Exception as exc:  # noqa: BLE001 -- best-effort, nunca debe romper la bateria
        return check("navegador real: no se pudo lanzar Chromium", True, str(exc))

    ok = check(
        "navegador real: cero errores de consola/pageerror al cargar /",
        not errors,
        "; ".join(errors)[:300],
    )
    ok = (
        check("navegador real: splash-screen (lanzador del ojo) visible", splash_ok)
        and ok
    )
    ok = (
        check(
            "navegador real: canvas Three.js del ojo tiene contenido",
            eye_has_content,
        )
        and ok
    )
    return ok


def coverage_check() -> bool:
    """REQ-007 (spec bateria-qa-integracion): cobertura viva. Lee
    `scripts/qa_coverage.json` (feature -> tokens) y cruza cada token contra
    el codigo de ESTA bateria (+ backend/tests/). Una feature sin NINGUN
    token cubierto se marca SIN COBERTURA — via /qa-audit alimenta una tarea
    /todo para el /loop, asi el "olvido" de testear una feature nueva se
    vuelve una tarea, no un silencio."""
    import json as _json

    cov_file = ROOT / "scripts" / "qa_coverage.json"
    if not cov_file.exists():
        return check(
            "cobertura: manifiesto faltante", False, "scripts/qa_coverage.json"
        )
    try:
        data = _json.loads(cov_file.read_text(encoding="utf-8"))
    except ValueError as exc:  # noqa: BLE001
        return check("cobertura: manifiesto invalido", False, str(exc))
    battery_src = (ROOT / "scripts" / "qa_battery.py").read_text(encoding="utf-8")
    tests_src = (
        "".join(
            p.read_text(encoding="utf-8")
            for p in sorted((ROOT / "backend/tests").glob("*.py"))
        )
        if (ROOT / "backend/tests").exists()
        else ""
    )
    corpus = battery_src + "\n" + tests_src
    ok = True
    for feat in data.get("features", []):
        slug = feat.get("slug", "?")
        tokens = feat.get("tokens", [])
        covered = any(t in corpus for t in tokens)
        ok = (
            check(
                f"cobertura: {slug}",
                covered,
                "SIN COBERTURA: agregar check/test + token (via /qa-audit -> /todo)",
            )
            and ok
        )
    return ok


def form_consistency_check() -> bool:
    """REQ-005 (spec bateria-qa-integracion): consistencia de formularios.
    Regla accionable: los inputs de formularios (Settings/modales) deben estar
    etiquetados. Se cuenta como etiquetado si: (a) hay `<label for="id">`,
    (b) el input cae DENTRO de un `<label>` envolvente (asociacion valida,
    ej. los checkboxes), o (c) lleva `aria-label`. Excepciones documentadas:
    campos de "chrome" (composer/search/file) que no son formularios de
    configuracion y usan placeholder/ocultos a proposito."""
    import re as _re

    html = (ROOT / "frontend/index.html").read_text(encoding="utf-8")
    labels_for = set(_re.findall(r'<label[^>]*for="([^"]+)"', html))
    input_ids = set(_re.findall(r'<(?:input|select|textarea)[^>]*id="([^"]+)"', html))
    # (b) inputs dentro de un <label> envolvente sin for=
    wrapped: set[str] = set()
    for m in _re.finditer(r"<label\b(?![^>]*\bfor=)(.*?)</label>", html, _re.S):
        wrapped |= set(
            _re.findall(r'<(?:input|select|textarea)[^>]*id="([^"]+)"', m.group(1))
        )
    # (c) aria-label. Bug corregido (spec voz-fish-audio): antes se juntaban
    # los TEXTOS del aria-label y se comparaban contra ids, asi que esta
    # regla nunca contaba. Ahora se juntan los ids de los campos que lo tienen.
    aria: set[str] = set()
    for tag in _re.findall(r"<(?:input|select|textarea)\b[^>]*>", html):
        tag_id = _re.search(r'\bid="([^"]+)"', tag)
        if tag_id and 'aria-label="' in tag:
            aria.add(tag_id.group(1))
    chrome = {
        "chat-input",
        "image-file-input",
        "session-search-input",
    }  # campos no-form
    missing = sorted((input_ids - labels_for - wrapped - aria) - chrome)
    if missing:
        return check(
            "formularios: inputs sin label",
            False,
            f"{len(missing)}: " + ", ".join(missing[:25]),
        )
    check("formularios: todo input/select/textarea tiene label/aria", True)
    return True


def main() -> None:
    results: list[bool] = []
    results.append(cross_reference_js_html())
    results.append(js_checks())
    results.append(py_compile())
    results.append(tool_registry())
    results.append(recent_mods())
    results.append(form_consistency_check())
    results.append(coverage_check())
    if "--runtime" in sys.argv:
        results.append(runtime_smoke())
        results.append(browser_console_check())
    print("-" * 60)
    print("RESULTADO GLOBAL:", "OK" if all(results) else "HAY FALLOS")


if __name__ == "__main__":
    main()
