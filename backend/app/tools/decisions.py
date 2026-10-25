"""
Tool `record_decision` (spec decisions-md-automatico): el LLM la llama
cuando reconoce que se tomo una decision real durante la conversacion
(ej. "vendorizamos Chart.js en vez de CDN, por AGENTS.md") -- mismo
patron que `todowrite` (tool explicita, no deteccion automatica de texto
libre, que seria fragil y con falsos positivos).

REQ-002/003: cada decision se AGREGA (append, nunca reescribe lo
existente) a `<carpeta-de-proyecto-de-la-sesion>/decisions/decisions.md`
(subcarpeta fija de la arquitectura carpeta-por-sesion, ver
ensure_project_structure en app/persistence/service.py -- antes era
`DECISIONS.md` suelto en la raiz), con decision/motivo/alternativa
descartada (opcional)/timestamp/session_id -- suficiente para que alguien
lea el archivo meses despues sin releer la conversacion completa.

REQ-004: la carpeta se resuelve con el MISMO mecanismo de
carpeta-por-sesion (persistence_service) -- si la sesion no tiene ninguna
carpeta asignada, la tool devuelve un error explicito al LLM en vez de
escribir en cualquier lado (nunca en la raiz del propio repo de Vision
por accidente).

REQ-005: lock de archivo simple (msvcrt, nativo de Windows, sin
dependencia nueva) para que dos sesiones escribiendo casi al mismo tiempo
sobre el mismo decisions.md compartido no se corrompan entre si -- el
riesgo ya es bajo por ser append puro, el lock es proteccion adicional.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from app.tools.base import Tool

logger = logging.getLogger(__name__)

DECISIONS_FILENAME = "decisions.md"
LOCK_TIMEOUT_S = 5.0
LOCK_POLL_INTERVAL_S = 0.05


def _append_with_lock(path: Path, entry: str) -> None:
    """REQ-005: lock de archivo REAL via `msvcrt.locking` (nativo de
    Windows, stdlib -- sin dependencia nueva, coherente con AGENTS.md).
    Sin esto, dos procesos escribiendo casi simultaneamente en modo
    append podrian intercalar bytes de escrituras distintas (el modo "a"
    de Python NO es atomico ni exclusivo por si solo en Windows).

    Se bloquea SIEMPRE el mismo byte fijo (offset 0) -- no la posicion de
    escritura real -- para que el lock sea trivial de tomar/soltar sin
    depender de calcular offsets moviles. Windows permite lockear un rango
    mas alla del EOF, asi que un archivo recien creado de 0 bytes se puede
    lockear igual: NO hace falta escribirle un byte dummy antes.

    BUG real corregido (QA-002, deteccion QA 2026-09-21): la version
    anterior escribia el encabezado con `path.write_bytes(header)` ANTES
    de tomar el lock. `write_bytes` abre en modo "wb", que TRUNCA -- y
    truncar un archivo cuyo byte 0 esta lockeado por otro proceso falla
    con `PermissionError`. Resultado: justo en el caso que REQ-005 dice
    proteger (dos sesiones escribiendo el mismo decisions.md compartido),
    la decision se PERDIA con un "Error: no se pudo escribir decisions.md"
    en vez de esperar el lock. Ahora el archivo se crea en modo "ab" (no
    trunca, no falla con un lock ajeno) y el encabezado se escribe DENTRO
    de la seccion lockeada, recien despues de confirmar -- ya con el lock
    tomado -- que el archivo sigue vacio (si otro proceso lo escribio
    mientras esperabamos, no se duplica)."""
    import msvcrt

    path.parent.mkdir(parents=True, exist_ok=True)
    # Crear si no existe, SIN truncar y sin fallar aunque otro proceso lo
    # tenga lockeado -- solo garantiza que el open("r+b") de abajo funcione.
    with open(path, "ab"):
        pass

    header = "# decisions.md\n\nBitácora automática de decisiones registradas por ULTRON.\n\n".encode()
    data = entry.encode("utf-8")
    deadline = time.monotonic() + LOCK_TIMEOUT_S

    with open(path, "r+b") as f:
        while True:
            try:
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"No se pudo obtener el lock de '{path}' tras {LOCK_TIMEOUT_S}s."
                    ) from None
                time.sleep(LOCK_POLL_INTERVAL_S)
        try:
            f.seek(
                0, 2
            )  # al final real -- otro proceso pudo haber agregado algo mientras se esperaba el lock
            if f.tell() == 0:
                # REQ-002: encabezado legible, una sola vez. Se decide aca
                # (con el lock ya tomado) y no antes, para que dos procesos
                # concurrentes no lo escriban los dos.
                f.write(header)
            f.write(data)
            f.flush()
        finally:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)


def _format_entry(
    decision: str, motivo: str, alternativa_descartada: str | None, session_id: str
) -> str:
    """REQ-002/003: formato Markdown cronologico -- cada entrada se
    agrega DESPUES de la anterior (append), nunca se reordena ni
    reescribe lo que ya habia en el archivo."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")  # noqa: DTZ005 -- hora local de la maquina, no UTC
    lines = [f"## {timestamp} — {decision}", "", f"**Motivo:** {motivo}"]
    if alternativa_descartada:
        lines.append(f"**Alternativa descartada:** {alternativa_descartada}")
    lines.append(f"**Sesión:** {session_id}")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(
        ""
    )  # linea en blanco extra: separa visualmente del "##" de la proxima entrada
    return "\n".join(lines)


def _record_decision_execute(args: dict, session: Any = None) -> str:
    if session is None or not session.session_id:
        return "Error: no hay sesión activa para registrar la decisión."

    decision = args.get("decision", "").strip()
    motivo = args.get("motivo", "").strip()
    if not decision:
        return "Error: falta el argumento 'decision'."
    if not motivo:
        return "Error: falta el argumento 'motivo'."
    alternativa_descartada = args.get("alternativa_descartada") or None

    # REQ-004: se resuelve la carpeta PROPIA de esta sesion (arquitectura
    # carpeta-por-sesion, definitiva) -- import diferido para evitar ciclo
    # (persistence importa tools indirectamente via loop.py).
    from app.persistence import service as persistence  # import diferido: evita ciclo

    project_root = persistence.project_root_for_session(session.session_id)
    if project_root is None:
        # REQ-008 (spec carpeta-por-sesion): el chat funciona libre sin
        # carpeta -- recien cuando el LLM intenta usar una tool que SI la
        # necesita, se avisa al frontend (evento SSE, mismo mecanismo ya
        # usado por 'loop-paused' en question.py) para que abra
        # automaticamente el modal de asignar carpeta. El LLM tambien
        # recibe el error en texto para poder explicarselo al usuario en
        # el chat, por si el evento SSE no llega a tiempo (ej. usuario
        # mirando otra pestana).
        session.push_event("project-folder-required", {"action": "record_decision"})
        return (
            "Error: esta sesión no tiene ninguna carpeta de proyecto asignada -- "
            "no hay dónde guardar decisions.md. Se le pidió al usuario elegir una "
            "carpeta; avisale que la elija desde el modal que va a aparecer."
        )

    decisions_path = project_root / persistence.DECISIONS_SUBDIR / DECISIONS_FILENAME

    entry = _format_entry(decision, motivo, alternativa_descartada, session.session_id)

    try:
        _append_with_lock(decisions_path, entry)
    except OSError as exc:
        logger.warning(
            "No se pudo escribir decisions.md en '%s'", decisions_path, exc_info=True
        )
        return f"Error: no se pudo escribir decisions.md ({exc})."

    return f"Decisión registrada en {decisions_path}: '{decision}'."


record_decision_tool = Tool(
    name="record_decision",
    description=(
        "Registra una decision REAL tomada durante la conversacion en decisions/decisions.md, "
        "dentro de la carpeta de proyecto de esta sesion. Usar cuando el usuario "
        "confirma explicitamente un criterio tecnico, elige entre alternativas, o "
        "establece una regla que debe recordarse a futuro -- NO usar para acciones "
        "rutinarias o intermedias que no son decisiones de fondo."
    ),
    parameters={
        "type": "object",
        "properties": {
            "decision": {
                "type": "string",
                "description": "Que se decidio, en una frase corta (ej. 'Vendorizar Chart.js en vez de usar CDN').",
            },
            "motivo": {
                "type": "string",
                "description": "Por que se tomo esa decision -- el razonamiento o contexto que la justifica.",
            },
            "alternativa_descartada": {
                "type": "string",
                "description": "Que alternativa se considero y se descarto, si aplica.",
            },
        },
        "required": ["decision", "motivo"],
    },
    execute=_record_decision_execute,
    # Toca session.push_event() (asyncio.Queue) -- ver comentario de
    # Tool.thread_safe en app/tools/base.py.
    thread_safe=False,
)
