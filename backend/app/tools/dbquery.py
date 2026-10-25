"""
Tools de datos: exponen los conectores propios de Vision (Oracle,
BigQuery, SharePoint, SAP) al agente como tool-calls. Cada una devuelve
los datos en un formato tabular compacto (markdown-like, primeras N
filas) para que el modelo pueda razonar sobre ellos sin recibir un JSON
gigante.

spec controles-seguridad-optimizacion-lightspeed-bigquery: ademas del
comportamiento base de arriba, este modulo implementa:
  - REQ-002: dry run + pausa dura por costo (>1 GiB configurable) antes de
    ejecutar una consulta de negocio en BigQuery, con cache global (todo
    el proceso, no solo la sesion) por hash EXACTO del SQL y TTL corto.
    FIX 2026-09-21: ahora tambien respeta la aprobacion humana en el
    reintento (`SessionState.bq_cost_approved`), igual que REQ-003 -- antes
    bloqueaba SIEMPRE, sin memoria de "ya lo aprobaste".
  - REQ-003: guardrail estructural de riesgo en BigQuery (DROP/DELETE/
    TRUNCATE/UPDATE/MERGE/CREATE TABLE/CREATE OR REPLACE TABLE) -- se
    intercepta ANTES de llamar al conector, sin depender del criterio del
    LLM. Bloquea la primera vez que ve una consulta riesgosa (obliga a
    usar `question` con sufijo "(Riesgo)"); si el agente reintenta la
    MISMA consulta despues de la aprobacion humana, esa vez si se ejecuta.
  - REQ-004: export automatico a CSV (+ link de descarga) cuando el
    resultado real de CUALQUIER conector (Oracle/BigQuery/SharePoint/SAP)
    supera un umbral configurable de filas -- no es exclusivo de BigQuery.
    FIX 2026-09-21: si el propio SQL del modelo trae un LIMIT/FETCH FIRST/
    ROWNUM explicito que coincide con el total devuelto, se agrega una nota
    pidiendole al agente que reintente sin ese tope -- antes, un LIMIT
    puesto por el LLM (ej. "LIMIT 50") desactivaba el export por completo
    sin que nadie se enterara de que habia mas datos.

Orden de evaluacion para una consulta de BigQuery (ver spec, "Orden de
ejecucion de los guardrails"): REQ-003 (riesgo, sin llamar a BigQuery) ->
REQ-002 (dry run + costo) -> ejecucion real -> REQ-004 (export si aplica).
Nunca se paralelizan entre si para una misma consulta (evita gastar un dry
run sobre algo que de todas formas se iba a bloquear por riesgo).
"""

from __future__ import annotations

import hashlib
import re
import time
from pathlib import Path
from typing import Any

import pandas as pd

from app.config import get_settings
from app.connectors import (
    BigQueryConnector,
    GraphConnector,
    OracleConnector,
    SapConnector,
    SharePointConnector,
)
from app.connectors.graph_connector import GraphConnectionError, GraphForbiddenError
from app.tools.base import Tool
from app import connections, switches

MAX_ROWS_TO_MODEL = 50


# ---------------------------------------------------------------------------
# Pedido del usuario (2026-09-21): poder cambiar desde la pestaña
# "Conexiones" de Configuracion el tope que determina que es una "consulta
# pesada" (umbral de costo en bytes de REQ-002, umbral de filas y tamaño de
# preview de REQ-004) SIN editar `.env` a mano ni reiniciar el backend.
# `Settings` es `@lru_cache` (fijo desde el arranque, ver app/config.py),
# asi que el valor EDITABLE en runtime vive en `ProjectConfig`
# (app/project_context.py, mismo JSON en disco que ya usan los parametros
# de generacion) -- estas 3 funciones resuelven "ProjectConfig si esta
# seteado, sino el default de Settings", en un solo lugar para no repetir
# la logica en cada guardrail.
# ---------------------------------------------------------------------------
# spec conexiones-con-nombre REQ-003: las tools de Oracle/SharePoint/SAP/Graph aceptan `connection` (nombre de la conexion). Sin `connection`, o sin
# archivo de conexiones, se usa el `get_settings()` de siempre (el `.env` de hoy): la compatibilidad (REQ-001) es el caso por defecto.
_CONNECTION_PARAM = {
    "type": "string",
    "description": (
        "Nombre de la conexion a usar (ver 'Conexiones disponibles' del sistema). "
        "Si se omite, se usa la conexion predeterminada de este tipo."
    ),
}


def _connection_settings(type_id: str, args: dict) -> tuple[Any, str | None]:
    """Devuelve (settings, error). `error` es un texto para el modelo cuando el nombre pedido no existe (lista los validos)."""
    settings, _conn, error = _connection_context(type_id, args)
    return settings, error


def _connection_context(type_id: str, args: dict) -> tuple[Any, Any, str | None]:
    """(settings, conexion, error). La conexion sirve para enmascarar sus valores en un mensaje de error (`_safe_error`); es None si viene del `.env` de hoy."""
    name = str(args.get("connection") or "").strip() or None
    base = get_settings()
    try:
        conn = switches.resolve_enabled(type_id, name)
    except switches.DisabledConnectionError as exc:
        return None, None, f"Error: {exc}"
    except connections.ConnectionLookupError as exc:
        return (base, None, None) if name is None else (None, None, f"Error: {exc}")
    if not conn.from_file:
        return base, None, None  # la conexion viene del .env de hoy: exactamente el comportamiento de antes
    return connections.settings_for(conn), conn, None


def _safe_error(conn: Any, exc: BaseException) -> str:
    """Texto de una excepcion para devolverselo al MODELO. Las tools devuelven el error crudo al LLM (y de ahi al historial y al proveedor de IA): con conexiones que
    llevan la contrasena dentro del bloque, un driver que falla con el DSN en el mensaje la filtraria. Se enmascaran los valores de la conexion (REQ-004)."""
    text = str(exc)
    return connections.redact(conn, text) if conn is not None else text


def _effective_bq_cost_threshold(settings: Any) -> int:
    from app.project_context import get_project_config  # import diferido: evita ciclo

    override = get_project_config().bq_cost_guardrail_bytes
    return override if override is not None else settings.bq_cost_guardrail_bytes


def _effective_export_row_threshold(settings: Any) -> int:
    from app.project_context import get_project_config  # import diferido: evita ciclo

    override = get_project_config().export_row_threshold
    return override if override is not None else settings.export_row_threshold


def _effective_export_preview_rows(settings: Any) -> int:
    from app.project_context import get_project_config  # import diferido: evita ciclo

    override = get_project_config().export_preview_rows
    return override if override is not None else settings.export_preview_rows


# Investigacion de rendimiento 2026-09 ("Vision tarda mucho"): `_df_to_text`
# ya trunca lo que se le MUESTRA al modelo a MAX_ROWS_TO_MODEL, pero antes
# igual se traia TODO el resultado de la base primero -- un SQL del modelo
# sin LIMIT/ROWNUM/FETCH FIRST podia arrastrar una tabla entera antes de
# descartar el 99% en Python. Si el SQL no trae ya alguna clausula de tope
# (deteccion simple por palabra clave, no un parser SQL completo -- a
# proposito conservador: mejor un falso negativo que romper una query
# valida), se envuelve en una subquery con el tope aplicado en el motor.
MAX_ROWS_FROM_DB = 500
_HAS_ROW_LIMIT_RE = re.compile(r"\b(rownum|fetch\s+first|limit)\b", re.IGNORECASE)


def _cap_oracle_query(query: str, cap: int = MAX_ROWS_FROM_DB) -> str:
    if _HAS_ROW_LIMIT_RE.search(query):
        return query
    return f"SELECT * FROM ({query.rstrip(';')}) WHERE ROWNUM <= {cap}"


def _cap_bigquery_query(query: str, cap: int = MAX_ROWS_FROM_DB) -> str:
    if _HAS_ROW_LIMIT_RE.search(query):
        return query
    return f"SELECT * FROM ({query.rstrip(';')}) LIMIT {cap}"


# ---------------------------------------------------------------------------
# Bug real reportado por el usuario (2026-09-21): pidio "el detalle" de 181
# contenedores, el LLM escribio su PROPIO `... LIMIT 50` en el SQL (por
# prudencia de tokens, sin saber que eso desactivaba el export automatico
# de REQ-004) -- Vision respeta un LIMIT/FETCH FIRST/ROWNUM explicito del
# modelo (ver `_HAS_ROW_LIMIT_RE` arriba, "el motor no le agrega otro tope
# encima"), asi que el DataFrame que llego a `_df_to_text` YA tenia
# exactamente 50 filas -- nunca vio las 181 reales, y como 50 <=
# `export_row_threshold` (50 por defecto), no exporto nada. El bug no
# estaba en el export en si: es que ningun aviso le decia al LLM "tu propio
# LIMIT puede estar ocultando mas datos". Se detecta ese tope explicito en
# el TEXTO del SQL (no en el resultado) y, si el conteo de filas devueltas
# coincide EXACTO con ese tope, se agrega una nota clara pidiendole al
# agente que reintente sin el limite si quiere el dataset completo/export.
_EXPLICIT_LIMIT_RE = re.compile(r"\blimit\s+(\d+)\b", re.IGNORECASE)
_EXPLICIT_FETCH_FIRST_RE = re.compile(
    r"\bfetch\s+first\s+(\d+)\s+rows?\b", re.IGNORECASE
)
_EXPLICIT_ROWNUM_RE = re.compile(r"\browNum\s*<=\s*(\d+)\b", re.IGNORECASE)


def _detect_explicit_row_cap(query: str) -> int | None:
    """Busca un tope de filas EXPLICITO escrito por el propio LLM en el SQL
    (`LIMIT N` de BigQuery, `FETCH FIRST N ROWS`/`ROWNUM <= N` de Oracle).
    Devuelve el numero N encontrado (el ULTIMO de la consulta, por si hay
    subqueries con limit propio y un limit externo que es el que realmente
    aplica al resultado final) o None si no hay ninguno."""
    last_match: int | None = None
    for rx in (_EXPLICIT_LIMIT_RE, _EXPLICIT_FETCH_FIRST_RE, _EXPLICIT_ROWNUM_RE):
        for m in rx.finditer(query):
            last_match = int(m.group(1))
    return last_match


# ---------------------------------------------------------------------------
# REQ-003: guardrail estructural de riesgo en BigQuery (DROP/DELETE/TRUNCATE/
# UPDATE/MERGE/CREATE TABLE/CREATE OR REPLACE TABLE)
# ---------------------------------------------------------------------------
# Matching EXACTO de estos verbos/patrones como PRIMERA palabra de cada
# statement (decision explicita del usuario: no se amplia a INSERT/CREATE
# VIEW/ALTER/CALL/etc. en esta entrega). CREATE TABLE se agrego aparte
# porque "no todos pueden crear tablas" -- pero CREATE VIEW/FUNCTION/
# PROCEDURE/SCHEMA NO disparan este guardrail.
_RISKY_SIMPLE_VERBS = ("DROP", "DELETE", "TRUNCATE", "UPDATE", "MERGE")
# CREATE (OR REPLACE)? TABLE -- "OR REPLACE" es opcional en el medio.
_RISKY_CREATE_TABLE_RE = re.compile(
    r"^CREATE\s+(?:OR\s+REPLACE\s+)?TABLE\b", re.IGNORECASE
)
_RISKY_SIMPLE_VERBS_RE = re.compile(
    r"^(" + "|".join(_RISKY_SIMPLE_VERBS) + r")\b", re.IGNORECASE
)

# Elimina comentarios de linea (--...) y de bloque (/*...*/) antes de partir
# en statements -- evita falsos positivos/negativos por texto comentado
# (ej. "-- esto es un delete" no debe disparar nada) y falsas fronteras de
# statement por un ';' dentro de un comentario.
_LINE_COMMENT_RE = re.compile(r"--[^\n]*")
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)


def _strip_sql_comments(sql: str) -> str:
    sql = _BLOCK_COMMENT_RE.sub(" ", sql)
    sql = _LINE_COMMENT_RE.sub(" ", sql)
    return sql


def _detect_risky_statement(query: str) -> tuple[str, str] | None:
    """Devuelve (patron_detectado, statement_completo) si ALGUN statement
    del SQL (separado por ';', sin comentarios) arranca con un verbo/patron
    riesgoso -- o None si no se detecta nada. Case-insensitive. Deteccion
    por PRIMERA PALABRA de cada statement, no por substring en cualquier
    parte -- asi una columna llamada 'updated_at' en un SELECT normal NO
    dispara el guardrail (a diferencia de una busqueda ingenua de
    substring), pero 'SELECT 1; DELETE FROM t' si lo dispara en el segundo
    statement."""
    cleaned = _strip_sql_comments(query)
    for raw_stmt in cleaned.split(";"):
        stmt = raw_stmt.strip()
        if not stmt:
            continue
        create_match = _RISKY_CREATE_TABLE_RE.match(stmt)
        if create_match:
            return create_match.group(0).upper(), stmt
        simple_match = _RISKY_SIMPLE_VERBS_RE.match(stmt)
        if simple_match:
            return simple_match.group(1).upper(), stmt
    return None


def _hash_query(query: str) -> str:
    """Hash estable de la consulta EXACTA (sin normalizar espacios/mayus) --
    usado tanto para el set de riesgos ya vistos (REQ-003) como para el
    cache de dry run (REQ-002). Si el agente cambia aunque sea un caracter
    del SQL, se trata como una consulta distinta a proposito (nunca
    relajar un guardrail por una coincidencia parcial)."""
    return hashlib.sha256(query.encode("utf-8", errors="replace")).hexdigest()


def _risk_check(query: str, session: Any, seen_attr: str) -> tuple[str, str] | None:
    """Nucleo compartido de la guarda de riesgo (BigQuery y Oracle). Devuelve (patron, statement) si hay que BLOQUEAR esta vez,
    o None si la consulta es segura o ya fue mostrada al usuario (el reintento tras su confirmacion). Registra el hash de la
    consulta en `session.<seen_attr>` al bloquear. Sin sesion no hay donde recordar la aprobacion: siempre bloquea."""
    detected = _detect_risky_statement(query)
    if detected is None:
        return None
    seen = getattr(session, seen_attr, None) if session is not None else None
    query_hash = _hash_query(query)
    if seen is not None and query_hash in seen:
        return None  # ya aprobado por el usuario en un intento anterior
    if seen is not None:
        seen.add(query_hash)
    return detected


def _bigquery_risk_check(query: str, session: Any) -> str | None:
    """REQ-003: si detecta un verbo/patron riesgoso y esta consulta EXACTA todavia no fue mostrada al usuario en esta sesion,
    devuelve el mensaje de bloqueo (sin ejecutar nada) y registra el hash como visto. Si el hash YA estaba registrado (el agente
    ya mostro la alarma antes y esta reintentando tras la respuesta del usuario), devuelve None -- se deja continuar hacia
    REQ-002/ejecucion real ("bloquea una vez, permite el reintento", decision explicita del usuario)."""
    blocked = _risk_check(query, session, "bq_risky_seen")
    if blocked is None:
        return None
    pattern, statement = blocked
    return (
        f"[GUARDRAIL DE RIESGO - BigQuery] Se detecto el patron '{pattern}' en la consulta, "
        "que puede modificar o eliminar datos/tablas de forma irreversible. La consulta NO "
        "se ejecuto.\n\n"
        f"Statement detectado:\n{statement}\n\n"
        f"Motivo: '{pattern}' esta en la lista de operaciones de riesgo que ULTRON bloquea "
        "estructuralmente antes de tocar BigQuery (DROP, DELETE, TRUNCATE, UPDATE, MERGE, "
        "CREATE TABLE, CREATE OR REPLACE TABLE).\n\n"
        "Debes usar la tool `question` para pedirle confirmacion explicita al usuario, "
        'agregando el sufijo "(Riesgo)" a la opcion que autoriza continuar (esto dispara la '
        "alarma y el banner rojo). Si el usuario confirma, volve a intentar EXACTAMENTE la "
        "misma consulta -- recien ahi se ejecutara de verdad."
    )


def _oracle_risk_check(query: str, session: Any) -> str | None:
    """PQ-005 / BL-023: la misma guarda de riesgo que BigQuery, para Oracle (misma lista de verbos, mismo flujo: bloquea UNA vez,
    pide confirmacion con `question` y deja pasar el reintento de la MISMA consulta). Las aprobaciones de Oracle y de BigQuery son
    independientes (`oracle_risky_seen` vs `bq_risky_seen`). Importa mas en Oracle: un DDL (DROP/TRUNCATE) confirma solo."""
    blocked = _risk_check(query, session, "oracle_risky_seen")
    if blocked is None:
        return None
    pattern, statement = blocked
    return (
        f"[GUARDRAIL DE RIESGO - Oracle] Se detecto el patron '{pattern}' en la consulta, "
        "que puede modificar o eliminar datos/tablas de forma irreversible. La consulta NO "
        "se ejecuto.\n\n"
        f"Statement detectado:\n{statement}\n\n"
        f"Motivo: '{pattern}' esta en la lista de operaciones de riesgo que ULTRON bloquea "
        "estructuralmente antes de tocar Oracle (DROP, DELETE, TRUNCATE, UPDATE, MERGE, "
        "CREATE TABLE, CREATE OR REPLACE TABLE).\n\n"
        "Debes usar la tool `question` para pedirle confirmacion explicita al usuario, "
        'agregando el sufijo "(Riesgo)" a la opcion que autoriza continuar (esto dispara la '
        "alarma y el banner rojo). Si el usuario confirma, volve a intentar EXACTAMENTE la "
        "misma consulta -- recien ahi se ejecutara de verdad."
    )


# ---------------------------------------------------------------------------
# REQ-002: dry run + control de costos de BigQuery
# ---------------------------------------------------------------------------
# Cache GLOBAL A TODO EL PROCESO (no por sesion -- decision explicita del
# usuario: si dos sesiones/compañeros de la misma instancia local corren la
# misma consulta en la ventana de TTL, la segunda no repite el round-trip
# de red). Guarda hash_sql -> (bytes_estimados, timestamp_monotonic). Nunca
# relaja el guardrail: un hit de cache devuelve el MISMO veredicto que
# hubiera dado un dry run fresco, solo evita la llamada de red.
_BQ_DRY_RUN_CACHE: dict[str, tuple[int, float]] = {}


def _format_bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.2f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.2f} TB"


def _bigquery_dry_run_check(
    query: str, connector: BigQueryConnector, session: Any = None
) -> str | None:
    """REQ-002: corre (o reusa del cache) un dry run de la consulta; si el
    estimado de bytes procesados supera el umbral configurado, devuelve el
    mensaje de pausa (sin ejecutar la consulta real) -- None si esta por
    debajo del umbral, o si esta consulta EXACTA ya fue aprobada por el
    usuario en esta sesion (ver `SessionState.bq_cost_approved`).

    FIX 2026-09-21 (bug real reportado por el usuario: 'el sistema de
    seguridad de BigQuery mantiene el bloqueo... incluso tras tu
    confirmacion'): antes de este fix, esta funcion bloqueaba SIEMPRE que
    el costo superaba el umbral, sin ningun mecanismo de aprobacion --
    a diferencia de `_bigquery_risk_check` (REQ-003), que si lo tenia. El
    LLM entraba en un loop pidiendole al usuario "optimizar columnas" en
    vez de simplemente reintentar tras el "si" del usuario, porque
    tecnicamente NO HABIA forma de que el reintento pasara. Mismo patron
    ahora: hash de la consulta -> si ya esta en `bq_cost_approved`, se deja
    pasar (el cache de dry run NUNCA se salta, solo el guardrail de pausa)."""
    settings = get_settings()
    threshold = _effective_bq_cost_threshold(settings)
    ttl = settings.bq_dry_run_cache_ttl_seconds
    query_hash = _hash_query(query)

    now = time.monotonic()
    cached = _BQ_DRY_RUN_CACHE.get(query_hash)
    if cached is not None and (now - cached[1]) < ttl:
        estimated_bytes = cached[0]
    else:
        estimated_bytes = connector.dry_run(query)
        _BQ_DRY_RUN_CACHE[query_hash] = (estimated_bytes, now)

    if estimated_bytes <= threshold:
        return None

    cost_approved = (
        getattr(session, "bq_cost_approved", None) if session is not None else None
    )
    if cost_approved is not None and query_hash in cost_approved:
        return None  # ya aprobado por el usuario en un intento anterior

    if cost_approved is not None:
        cost_approved.add(query_hash)

    return (
        f"[GUARDRAIL DE COSTO - BigQuery] Esta consulta procesaria aproximadamente "
        f"{_format_bytes(estimated_bytes)}, que supera el limite configurado de "
        f"{_format_bytes(threshold)}. La consulta NO se ejecuto.\n\n"
        f"Consulta:\n{query}\n\n"
        "Debes usar la tool `question` para pedirle confirmacion explicita al usuario antes "
        "de ejecutarla (mostrando el costo estimado). Si esta consulta se va a repetir seguido "
        "y es pesada, sugerile tambien la opcion de armar una automatizacion en Python fuera "
        "del chat (no generes el script ahora, solo ofrecelo como idea si el usuario confirma "
        "interes). Si el usuario confirma que la ejecute igual, volve a intentar EXACTAMENTE la "
        "misma consulta (mismo texto SQL) -- recien ahi se ejecutara de verdad, sin volver a "
        "pausarse por costo."
    )


# ---------------------------------------------------------------------------
# REQ-004: export automatico a CSV + link de descarga (multi-conector)
# ---------------------------------------------------------------------------
def _write_export_csv(
    df: pd.DataFrame, session: Any, source: str
) -> tuple[Path, str] | None:
    """Escribe el DataFrame COMPLETO a un CSV nuevo en `exports/` de la
    carpeta de proyecto de la sesion. Devuelve (ruta_absoluta, url_descarga)
    o None si la sesion no tiene carpeta de proyecto asignada (modo 100%
    en memoria -- no hay donde persistir el archivo, ver AGENTS.md)."""
    session_id = getattr(session, "session_id", None) if session is not None else None
    if not session_id:
        return None

    from app.persistence import service as persistence  # import diferido: evita ciclo

    project_root = persistence.project_root_for_session(session_id)
    if project_root is None:
        return None

    exports_dir = project_root / persistence.EXPORTS_SUBDIR
    exports_dir.mkdir(parents=True, exist_ok=True)

    timestamp = time.strftime("%Y%m%d-%H%M%S")
    filename = f"{source}_{timestamp}.csv"
    out_path = exports_dir / filename
    # Si dos exports caen en el mismo segundo (2 tools en paralelo, ver
    # asyncio.gather en loop.py), se desambigua con un sufijo incremental
    # en vez de pisar el archivo anterior.
    counter = 1
    while out_path.exists():
        out_path = exports_dir / f"{source}_{timestamp}-{counter}.csv"
        counter += 1

    df.to_csv(out_path, index=False)
    download_url = f"/api/sessions/{session_id}/exports/{out_path.name}"
    return out_path, download_url


def _df_to_text(
    df: pd.DataFrame, session: Any = None, source: str = "export", query: str = ""
) -> str:
    if df.empty:
        return "(sin resultados)"
    settings = get_settings()
    preview_rows = _effective_export_preview_rows(settings)
    row_threshold = _effective_export_row_threshold(settings)
    total = len(df)

    # Bug real 2026-09-21 (ver comentario largo en _detect_explicit_row_cap):
    # si el propio SQL del modelo trae un tope explicito (LIMIT/FETCH FIRST/
    # ROWNUM) y el resultado devuelto coincide EXACTO con ese numero, es
    # senial de que probablemente HAY MAS FILAS en la fuente real -- se
    # avisa esto SIEMPRE (incluso si total <= preview_rows, para que el
    # aviso no dependa de si tambien se dispara el export), antes de decidir
    # si además corresponde exportar.
    explicit_cap = _detect_explicit_row_cap(query) if query else None
    cap_warning = ""
    if explicit_cap is not None and total == explicit_cap:
        cap_warning = (
            f"\n\n[NOTA: tu consulta trae un tope explicito de {explicit_cap} filas "
            f"(LIMIT/FETCH FIRST/ROWNUM) y devolvio EXACTAMENTE {explicit_cap} -- es "
            "probable que existan mas filas en la fuente real que este limite esta "
            "ocultando. Si el usuario pidio 'el detalle'/'todos los registros'/el "
            "dataset completo, reintenta la MISMA consulta SIN ese tope (o con un "
            "numero mayor) para que ULTRON pueda exportar el resultado completo a CSV "
            "si corresponde.]"
        )

    preview = df.head(preview_rows)
    text = preview.to_csv(index=False)
    if total <= preview_rows:
        return text + cap_warning

    text += (
        f"\n... ({total} filas totales, se muestran las primeras {preview_rows}) ..."
    )

    # REQ-004: el resultado real supera el umbral configurado -- ademas del
    # preview de arriba (que ya respeta el ORDER BY que haya traido la
    # consulta, sin forzar ninguno nuevo), se exporta el DataFrame COMPLETO
    # a un CSV y se informa la ruta + link de descarga clickeable.
    if total > row_threshold:
        exported = _write_export_csv(df, session, source)
        if exported is None:
            text += (
                "\n\n[Export no disponible: esta sesion no tiene una carpeta de proyecto "
                "asignada, asi que no hay donde guardar el CSV completo -- solo se puede "
                "mostrar el preview de arriba.]"
            )
        else:
            out_path, download_url = exported
            text += (
                f"\n\n**Resultado completo exportado** ({total} filas): "
                f"[{out_path.name}]({download_url})\n"
                f"Ruta completa: `{out_path}`"
            )
    return text + cap_warning


# --------------------------------------------------------------------------
# oracle_query
# --------------------------------------------------------------------------
def _oracle_query_execute(args: dict, session: Any = None) -> str:
    settings, conn, conn_error = _connection_context("oracle", args)
    if conn_error:
        return conn_error
    if not settings.enable_oracle:
        return "Error: el conector Oracle esta deshabilitado (ENABLE_ORACLE=false)."
    query = args.get("query", "")
    if not query:
        return "Error: falta el argumento 'query' (SQL SELECT)."
    risk_block = _oracle_risk_check(query, session)  # PQ-005: antes de abrir ninguna conexion
    if risk_block is not None:
        return risk_block
    try:
        df = OracleConnector(settings).fetch_query(_cap_oracle_query(query))
        return _df_to_text(df, session, source="oracle", query=query)
    except Exception as exc:
        return f"Error consultando Oracle: {_safe_error(conn, exc)}"


oracle_query_tool = Tool(
    name="oracle_query",
    description=(
        "Ejecuta una consulta SQL (SELECT) contra la base de datos Oracle (Datamart) de la empresa y devuelve los resultados en CSV. "
        "Si la consulta modifica datos o tablas (DROP, DELETE, TRUNCATE, UPDATE, MERGE, CREATE TABLE) la herramienta NO la ejecuta "
        "y te pide confirmar con el usuario antes con la tool `question`."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "SQL SELECT, ej. SELECT * FROM MMDBDP7.CPNT023_PART WHERE ROWNUM <= 10",
            },
            "connection": _CONNECTION_PARAM,
        },
        "required": ["query"],
    },
    execute=_oracle_query_execute,
)


# --------------------------------------------------------------------------
# bigquery_query
# --------------------------------------------------------------------------
def _bigquery_switch_error() -> str | None:
    """spec interruptores-conexiones-skills REQ-003 (barrera 2): aviso si BigQuery esta configurada y el usuario la APAGO; None si se puede usar.

    BigQuery no pasa por `_connection_context` (no tiene conexiones con nombre), asi que sus tools no miraban el interruptor y solo las frenaba `dispatch`. Sin configurar
    no se frena aunque haya quedado una clave apagada vieja: la pantalla ya no la muestra y el usuario no podria prenderla (mismo criterio que `tool_is_available`)."""
    if switches.BIGQUERY_KEY not in switches.all_connection_keys():
        return None
    try:
        switches.assert_connection_enabled("bigquery", "bigquery")
    except switches.DisabledConnectionError as exc:
        return f"Error: {exc}"
    return None


def _bigquery_query_execute(args: dict, session: Any = None) -> str:
    settings = get_settings()
    if not settings.enable_bigquery:
        return "Error: el conector BigQuery esta deshabilitado (ENABLE_BIGQUERY=false)."
    switch_error = _bigquery_switch_error()
    if switch_error:
        return switch_error
    query = args.get("query", "")
    if not query:
        return "Error: falta el argumento 'query' (SQL de BigQuery)."

    # Orden de guardrails (ver docstring del modulo y spec): REQ-003 primero
    # (sin llamar a BigQuery), despues REQ-002 (dry run + costo), recien
    # despues la ejecucion real. Nunca se paralelizan entre si para una
    # misma consulta -- cada uno puede cortar antes de gastar el siguiente.
    risk_block = _bigquery_risk_check(query, session)
    if risk_block is not None:
        return risk_block

    try:
        connector = BigQueryConnector(settings)
        cost_block = _bigquery_dry_run_check(query, connector, session)
        if cost_block is not None:
            return cost_block

        df = connector.fetch_query(
            _cap_bigquery_query(query),
            maximum_bytes_billed=_effective_bq_cost_threshold(settings),
        )
        return _df_to_text(df, session, source="bigquery", query=query)
    except Exception as exc:
        return f"Error consultando BigQuery: {exc}"


bigquery_query_tool = Tool(
    name="bigquery_query",
    description=(
        "Ejecuta una consulta SQL de BigQuery y devuelve los resultados en CSV. Usar sintaxis "
        "`proyecto.dataset.tabla` entre backticks.\n\n"
        "GUARDRAILS (spec controles-seguridad-optimizacion-lightspeed-bigquery): antes de "
        "consultar una tabla nueva en esta sesion, priorizá explorar su INFORMATION_SCHEMA "
        "(TABLES/TABLE_OPTIONS/COLUMNS) para conocer su esquema real -- si esa metadata viene "
        "vacia, segui igual con la consulta normal. Ademas, esta tool puede DEVOLVER UN BLOQUEO "
        "en vez de resultados si: (a) detecta DROP/DELETE/TRUNCATE/UPDATE/MERGE/CREATE TABLE en "
        "la consulta (riesgo de datos), o (b) el costo estimado supera el limite configurado. En "
        "ambos casos, el bloqueo te va a pedir explicitamente que uses la tool `question` para "
        "confirmar con el usuario -- si confirma, volve a invocar esta misma tool con la MISMA "
        "consulta exacta, recien ahi se ejecuta de verdad (sin volver a pausarse por lo mismo).\n\n"
        "IMPORTANTE: si el usuario pide 'el detalle'/'todos los registros'/el dataset completo, "
        "NO le agregues tu propio LIMIT bajo (ej. LIMIT 50) solo por prudencia de tokens -- eso le "
        "OCULTA filas reales al usuario y desactiva el export automatico a CSV. Deja que ULTRON "
        "decida el preview/export segun la configuracion real (umbral de filas), no lo decidas vos "
        "recortando la consulta de antemano."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "SQL de BigQuery, ej. SELECT * FROM `proyecto.dataset.tabla` LIMIT 10",
            }
        },
        "required": ["query"],
    },
    execute=_bigquery_query_execute,
)


def _bigquery_list_tables_execute(args: dict, session: Any = None) -> str:
    settings = get_settings()
    switch_error = _bigquery_switch_error()
    if switch_error:
        return switch_error
    dataset_id = args.get("dataset_id") or None
    try:
        bq = BigQueryConnector(settings)
        if dataset_id:
            tables = bq.list_tables(dataset_id)
            return (
                f"Tablas en dataset '{dataset_id}': " + ", ".join(tables)
                if tables
                else "(sin tablas)"
            )
        datasets = bq.list_datasets()
        return (
            "Datasets disponibles: " + ", ".join(datasets)
            if datasets
            else "(sin datasets)"
        )
    except Exception as exc:
        return f"Error listando BigQuery: {exc}"


bigquery_list_tables_tool = Tool(
    name="bigquery_list_tables",
    description="Lista los datasets del proyecto BigQuery configurado, o las tablas de un dataset especifico.",
    parameters={
        "type": "object",
        "properties": {
            "dataset_id": {
                "type": "string",
                "description": "Nombre del dataset (opcional)",
            }
        },
    },
    execute=_bigquery_list_tables_execute,
)


# --------------------------------------------------------------------------
# sharepoint_list_items
# --------------------------------------------------------------------------
def _parse_max_items(raw: Any) -> int | None:
    """Tope de filas que pidio el modelo, o None (= la lista completa).

    El modelo a veces manda "50" (texto) o 50.0 en vez de 50: se acepta. Un valor sin sentido (0, negativo, texto
    que no es numero, booleano, lista) se IGNORA y se trae la lista entera. Antes un "50" daba un error de tipos
    dentro del conector, un 0 devolvia una tabla vacia en silencio y un negativo recortaba mal las filas: en los tres
    casos el usuario se quedaba sin datos sin saber por que."""
    if raw is None:
        return None
    try:
        # `str()` primero: `True`/`False` no son numeros validos como texto ("True"), asi que caen en el except
        # y devuelven None (si se hiciera `float(True)` valdria 1.0 y un tope booleano recortaria la lista a 1 fila).
        value = int(float(str(raw).strip()))
    except (TypeError, ValueError, OverflowError):
        return None
    return value if value > 0 else None


def _sharepoint_list_items_execute(args: dict, session: Any = None) -> str:
    settings, conn, conn_error = _connection_context("sharepoint", args)
    if conn_error:
        return conn_error
    if not settings.enable_sharepoint:
        return "Error: el conector SharePoint esta deshabilitado (ENABLE_SHAREPOINT=false)."
    list_name = args.get("list_name") or None
    # Bug real (2026-10-01): antes no existia forma de pedir MAS de 200 filas
    # -- `fetch_list_items` ahora pagina SIEMPRE la lista completa via
    # `@odata.nextLink` salvo que el modelo pida explicitamente un tope con
    # `max_items` (ej. "dame solo los primeros 50"). Sin este argumento, se
    # trae la lista entera (ver app/connectors/sharepoint_connector.py).
    max_items = _parse_max_items(args.get("max_items"))
    try:
        sp = SharePointConnector(settings)
        df = sp.fetch_list_items(list_name, max_items=max_items)
        return _df_to_text(df, session, source="sharepoint")
    except Exception as exc:
        return f"Error consultando SharePoint: {_safe_error(conn, exc)}"


sharepoint_list_items_tool = Tool(
    name="sharepoint_list_items",
    description=(
        "Lee TODOS los items de una lista de SharePoint (por nombre visible), "
        "recorriendo todas las paginas de la lista, y los devuelve en CSV "
        "(con export automatico si supera el umbral configurado). Si no se "
        "especifica, usa la lista por defecto configurada."
    ),
    parameters={
        "type": "object",
        "properties": {
            "list_name": {
                "type": "string",
                "description": "Nombre visible de la lista de SharePoint",
            },
            "connection": _CONNECTION_PARAM,
            "max_items": {
                "type": "integer",
                "description": (
                    "Tope opcional de filas totales a traer. Si se omite, se "
                    "trae la lista COMPLETA (todas las paginas via "
                    "@odata.nextLink), no solo las primeras 200. Usar solo si "
                    "el usuario pidio explicitamente un numero acotado."
                ),
            },
        },
    },
    execute=_sharepoint_list_items_execute,
)


def _sharepoint_list_lists_execute(args: dict, session: Any = None) -> str:
    settings, conn, conn_error = _connection_context("sharepoint", args)
    if conn_error:
        return conn_error
    try:
        sp = SharePointConnector(settings)
        lists = sp.list_lists()
        return "Listas disponibles: " + ", ".join(lists) if lists else "(sin listas)"
    except Exception as exc:
        return f"Error listando SharePoint: {_safe_error(conn, exc)}"


sharepoint_list_lists_tool = Tool(
    name="sharepoint_list_lists",
    description="Lista los nombres de todas las listas disponibles en el sitio de SharePoint configurado.",
    parameters={"type": "object", "properties": {"connection": _CONNECTION_PARAM}},
    execute=_sharepoint_list_lists_execute,
)


# --------------------------------------------------------------------------
# sap_query
# --------------------------------------------------------------------------
def _sap_query_execute(args: dict, session: Any = None) -> str:
    settings, conn, conn_error = _connection_context("sap", args)
    if conn_error:
        return conn_error
    if not settings.enable_sap:
        return "Error: el conector SAP esta deshabilitado (ENABLE_SAP=false)."
    service_name = args.get("service_name", "")
    entity_name = args.get("entity_name", "")
    if not service_name or not entity_name:
        return "Error: se requieren 'service_name' y 'entity_name'."
    try:
        sap = SapConnector(settings)
        df = sap.fetch_generic(
            service_name=service_name,
            entity_name=entity_name,
            entity_params=args.get("entity_params"),
            select_fields=args.get("select_fields"),
            odata_filter=args.get("odata_filter"),
            results_suffix=args.get("results_suffix", "/Results"),
        )
        return _df_to_text(df, session, source="sap")
    except Exception as exc:
        return f"Error consultando SAP: {_safe_error(conn, exc)}"


sap_query_tool = Tool(
    name="sap_query",
    description=(
        "Consulta un servicio OData v2 generico de SAP EWM/WMS. Requiere conocer "
        "service_name/entity_name de antemano (ej. de documentacion o un HAR de referencia)."
    ),
    parameters={
        "type": "object",
        "properties": {
            "connection": _CONNECTION_PARAM,
            "service_name": {"type": "string"},
            "entity_name": {"type": "string"},
            "entity_params": {
                "type": "object",
                "description": "Parametros de function import (opcional)",
            },
            "select_fields": {"type": "array", "items": {"type": "string"}},
            "odata_filter": {"type": "string"},
            "results_suffix": {"type": "string", "default": "/Results"},
        },
        "required": ["service_name", "entity_name"],
    },
    execute=_sap_query_execute,
)


# --------------------------------------------------------------------------
# graph_search / graph_directory_lookup / graph_directory_manager
#
# Estas tools son las que usa (y requiere) la skill
# investigacion-interna-graph para buscar personas/proyectos internos
# de la empresa. Sin ellas registradas, el modelo no tiene forma real de resolver
# una consulta como "quien es Fulano de la empresa" y cae en un rechazo generico
# de privacidad -- con las tools disponibles, en cambio, puede seguir el
# router de fuentes documentado en esa skill (Directory primero, Search
# como descubrimiento, con los gates de autorizacion 403 documentados).
# --------------------------------------------------------------------------
def _graph_search_execute(args: dict, session: Any = None) -> str:
    query = args.get("query", "")
    size = int(args.get("size", 25))
    if not query:
        return "Error: falta el argumento 'query'."
    graph_settings, graph_conn, conn_error = _connection_context("graph", {})
    if conn_error:
        # spec interruptores-conexiones-skills REQ-003 (barrera 2): con Graph apagada el contexto trae el aviso y `graph_settings` es None; sin esta linea,
        # `GraphConnector(None)` caia a `get_settings()` y abria la conexion igual. Sin nombre, el unico error posible es el de conexion desactivada.
        return conn_error
    try:
        results = GraphConnector(graph_settings).search(query, size=size)
    except GraphForbiddenError as exc:
        return f"[gate 403 - permiso insuficiente] {_safe_error(graph_conn, exc)}"
    except Exception as exc:
        return f"Error en Graph Search: {_safe_error(graph_conn, exc)}"

    if not results:
        return "(sin resultados)"

    lines = []
    for r in results[:size]:
        lines.append(
            f"- {r['name']} | modificado: {r['modified']} | creadoPor: {r['createdBy']} | "
            f"modificadoPor: {r['modifiedBy']} | url: {r['url']}\n  resumen: {r['summary'][:200]}"
        )
    return "\n".join(lines)


graph_search_tool = Tool(
    name="graph_search",
    description=(
        "Busqueda de texto completo (Microsoft Search) sobre documentos y listas de M365 "
        "(SharePoint/OneDrive) indexados. Usar para descubrir documentos/proyectos que "
        "mencionan a una persona o tema, siguiendo el router de fuentes de la skill de "
        "investigacion interna. Usar comillas para nombres exactos, ej. '\"Nombre Apellido\"'."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Consulta KQL, ej. '\"Nombre Apellido\" Warehouse'",
            },
            "size": {
                "type": "integer",
                "default": 25,
                "description": "Cantidad de resultados (1-100)",
            },
        },
        "required": ["query"],
    },
    execute=_graph_search_execute,
)


def _graph_directory_lookup_execute(args: dict, session: Any = None) -> str:
    identifier = args.get("identifier", "")
    if not identifier:
        return "Error: falta el argumento 'identifier' (UPN, correo o id de usuario)."
    graph_settings, graph_conn, conn_error = _connection_context("graph", {})
    if conn_error:
        return conn_error  # REQ-003 (barrera 2), ver `_graph_search_execute`
    try:
        user = GraphConnector(graph_settings).directory_lookup(identifier)
    except GraphForbiddenError as exc:
        return f"[gate 403 - permiso insuficiente] {_safe_error(graph_conn, exc)}"
    except GraphConnectionError as exc:
        return f"Error: {_safe_error(graph_conn, exc)}"
    except Exception as exc:
        return f"Error consultando directorio: {_safe_error(graph_conn, exc)}"

    return (
        f"displayName: {user.get('displayName')}\n"
        f"mail: {user.get('mail')}\n"
        f"userPrincipalName: {user.get('userPrincipalName')}\n"
        f"jobTitle: {user.get('jobTitle')}\n"
        f"department: {user.get('department')}\n"
        f"officeLocation: {user.get('officeLocation')}\n"
        f"accountEnabled: {user.get('accountEnabled')}"
    )


graph_directory_lookup_tool = Tool(
    name="graph_directory_lookup",
    description=(
        "Busca el perfil corporativo (displayName, correo, puesto, departamento) de un usuario "
        "de la empresa por UPN/correo/id en Microsoft Graph Directory. Puede fallar con un gate 403 si "
        "la app no tiene permiso de Directory otorgado -- en ese caso, usar graph_search como alternativa."
    ),
    parameters={
        "type": "object",
        "properties": {
            "identifier": {
                "type": "string",
                "description": "UPN, correo o id de usuario",
            }
        },
        "required": ["identifier"],
    },
    execute=_graph_directory_lookup_execute,
)


def _graph_directory_manager_execute(args: dict, session: Any = None) -> str:
    identifier = args.get("identifier", "")
    if not identifier:
        return "Error: falta el argumento 'identifier' (UPN, correo o id de usuario)."
    graph_settings, graph_conn, conn_error = _connection_context("graph", {})
    if conn_error:
        return conn_error  # REQ-003 (barrera 2), ver `_graph_search_execute`
    try:
        manager = GraphConnector(graph_settings).directory_manager(identifier)
    except GraphForbiddenError as exc:
        return f"[gate 403 - permiso insuficiente] {_safe_error(graph_conn, exc)}"
    except GraphConnectionError as exc:
        return f"Error: {_safe_error(graph_conn, exc)}"
    except Exception as exc:
        return f"Error consultando manager: {_safe_error(graph_conn, exc)}"

    return (
        f"manager displayName: {manager.get('displayName')}\n"
        f"mail: {manager.get('mail')}\n"
        f"jobTitle: {manager.get('jobTitle')}\n"
        f"department: {manager.get('department')}"
    )


graph_directory_manager_tool = Tool(
    name="graph_directory_manager",
    description="Devuelve el manager directo de un usuario de la empresa (mismo gate 403 que graph_directory_lookup).",
    parameters={
        "type": "object",
        "properties": {
            "identifier": {
                "type": "string",
                "description": "UPN, correo o id de usuario",
            }
        },
        "required": ["identifier"],
    },
    execute=_graph_directory_manager_execute,
)
