"""
Registro de tiempos reales de ejecucion por tipo de operacion (tool o
skill), usado por la barra de progreso adaptativa del frontend (spec
barra-progreso-adaptativa) para estimar cuanto puede tardar una operacion
ANTES de que termine, en base a lo que tardo realmente las veces
anteriores -- no es un modelo de ML entrenado, es una heuristica
adaptativa tipo barra de progreso de Windows: presupuesto inicial +
recalculo en vivo, sin prometer exactitud.

Persistencia: JSON simple en backend/data/tool_timings.json, con una
ventana movil de las ultimas N mediciones por operacion (no un historial
infinito, para que la estimacion refleje el comportamiento RECIENTE).
"""

from __future__ import annotations

import json
import logging
import os
import statistics
import threading
import time

from app.config import BASE_DIR

logger = logging.getLogger(__name__)

TIMINGS_FILE = BASE_DIR / "data" / "tool_timings.json"

# Ventana movil: cuantas mediciones recientes se conservan por operacion.
# Un numero chico (en vez de un historial infinito) es lo que hace que la
# estimacion se "adapte" con el uso reciente, no quede congelada desde la
# primera vez que se uso esa tool/skill.
WINDOW_SIZE = 20

# Presupuesto generico (ms) cuando no hay NINGUN historial para una
# operacion nueva -- deliberadamente conservador (mejor sobreestimar un
# poco que mostrar una barra que llega a 100% y se queda esperando).
DEFAULT_BUDGET_MS = 4000

# REQ-006 (spec fix-detener-y-progreso-visible): operaciones cuyo presupuesto es la MEDIANA de la ventana en vez de la media. `bash`
# mezcla de todo (un `echo` de 0,2 s con un script de transcripcion de 100 s): con la media, UNA corrida larga inflaba la estimacion
# de los proximos 19 comandos. La mediana representa al comando tipico; si este tarda mas, la barra lo dice («sigue trabajando»).
# Las demas operaciones quedan con la media, como siempre.
_MEDIAN_OPERATIONS = frozenset({"bash"})


# tiempos-herramientas-seguros REQ-001/003: las tools de un mismo paso corren en paralelo (`asyncio.gather` +
# `to_thread`), asi que `record_duration` (leer -> modificar -> reescribir) se pisaba a si mismo: con 8 hilos se
# perdian la mayoria de las mediciones y el archivo podia leerse cortado (13 avisos "No se pudo leer" en app.log).
# Un candado serializa las lecturas y escrituras; es reentrante porque `record_duration` llama a `_load`/`_save`.
_lock = threading.RLock()


def _load() -> dict[str, list[float]]:
    with _lock:
        if not TIMINGS_FILE.exists():
            return {}
        try:
            return json.loads(TIMINGS_FILE.read_text(encoding="utf-8"))
        except Exception:
            # REQ-004: el archivo ya esta corrupto (de una version anterior). Se aparta para poder revisarlo en vez
            # de pisarlo, y se avisa UNA vez: al apartarlo el archivo deja de existir y no se repite el aviso.
            corrupt = TIMINGS_FILE.with_name(TIMINGS_FILE.name + ".corrupto")
            try:
                os.replace(TIMINGS_FILE, corrupt)
                logger.warning(
                    "tool_timings.json estaba corrupto: se aparto como %s y se reinicia vacio",
                    corrupt.name,
                    exc_info=True,
                )
            except OSError:
                logger.warning("No se pudo leer tool_timings.json, se reinicia vacio", exc_info=True)
            return {}


def _replace_with_retry(src, dst, attempts: int = 8, wait_s: float = 0.02) -> None:
    """`os.replace` con reintentos cortos. En Windows falla con PermissionError si OTRO proceso (antivirus, el propio
    usuario con el archivo abierto) lo esta leyendo justo ese instante; reintentar unos ms lo resuelve y evita perder
    la medicion en silencio."""
    for i in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(wait_s)


def _save(data: dict[str, list[float]]) -> None:
    """REQ-002: escritura ATOMICA (temporal + reemplazo). Un lector nunca ve el JSON a medias y una caida a mitad
    de escritura deja intacto el archivo anterior."""
    tmp = TIMINGS_FILE.with_name(TIMINGS_FILE.name + f".{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        TIMINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        _replace_with_retry(tmp, TIMINGS_FILE)
    except Exception:
        # No debe romper el turno del agente si falla la escritura del
        # historial de timings -- es una feature de UX, no algo critico.
        logger.warning("No se pudo escribir tool_timings.json", exc_info=True)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def record_duration(operation_key: str, duration_ms: float) -> None:
    """Registra cuanto tardo REALMENTE una operacion (tool_name, o
    'skill:<nombre>' para cargas de skill) -- ver REQ-001 de la spec.
    Mantiene solo las ultimas WINDOW_SIZE mediciones por operacion."""
    with _lock:  # leer-modificar-escribir como una unidad (REQ-001)
        data = _load()
        history = data.setdefault(operation_key, [])
        history.append(round(duration_ms, 1))
        if len(history) > WINDOW_SIZE:
            del history[: len(history) - WINDOW_SIZE]
        _save(data)


def get_budget_ms(operation_key: str) -> tuple[float, bool]:
    """Devuelve (presupuesto_estimado_ms, has_history). `has_history=False`
    significa que no hay ninguna medicion previa para esta operacion --
    el frontend usa esto para decidir si mostrar un % concreto o el modo
    indeterminado (REQ-004 de la spec)."""
    data = _load()
    history = data.get(operation_key, [])
    if not history:
        return DEFAULT_BUDGET_MS, False
    if operation_key in _MEDIAN_OPERATIONS:
        return float(statistics.median(history)), True  # REQ-006
    avg = sum(history) / len(history)
    return avg, True


def describe_operation(operation_key: str) -> str:
    """Texto contextual en lenguaje natural para mostrar junto a la barra
    (REQ-005) -- ej. 'oracle_query' -> 'Consultando Oracle…',
    'skill:logistica-general' -> 'Cargando skill de logística
    general…'. Fallback generico para operaciones sin nombre bonito
    mapeado."""
    if operation_key.startswith("skill:"):
        skill_name = operation_key.removeprefix("skill:")
        return f"Cargando skill {skill_name}…"

    labels = {
        "oracle_query": "Consultando Oracle…",
        "bigquery_query": "Consultando BigQuery…",
        "bigquery_list_tables": "Listando tablas de BigQuery…",
        "sharepoint_list_items": "Consultando SharePoint…",
        "sharepoint_list_lists": "Listando SharePoint…",
        "sap_query": "Consultando SAP…",
        "bash": "Ejecutando comando…",
        "read": "Leyendo archivo…",
        "write": "Escribiendo archivo…",
        "edit": "Editando archivo…",
        "glob": "Buscando archivos…",
        "grep": "Buscando en archivos…",
        "webfetch": "Obteniendo contenido web…",
        "graph_search": "Buscando en Microsoft Graph…",
        "graph_directory_lookup": "Consultando directorio corporativo…",
        "graph_directory_manager": "Consultando directorio corporativo…",
        "todowrite": "Actualizando lista de tareas…",
        "question": "Preparando pregunta…",
    }
    return labels.get(operation_key, f"Ejecutando {operation_key}…")
