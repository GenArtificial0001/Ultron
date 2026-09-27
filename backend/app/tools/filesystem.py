"""
Tools de filesystem del agente Vision: read, write, edit, glob, grep.

Replican el comportamiento esencial de las tools homonimas de OpenCode
(packages/core/src/tool/read.ts, etc.) pero simplificadas: sin el sistema
de permisos "ask" (el usuario eligio "acceso total sin friccion" para esta
v1 -- ver AGENTS.md), y sin distincion de "Location" externa/interna (todas
las rutas se resuelven tal cual, absolutas o relativas al directorio base).

Directorio base por defecto (arquitectura carpeta-por-sesion, definitiva):
si no se especifica una ruta absoluta, se resuelve relativa a la carpeta
de PROYECTO de la sesion actual (`session.session_id` ->
persistence_service.project_root_for_session) -- NUNCA a
`backend/workspace/`, que era el default viejo y generaba justamente el
problema que se queria evitar: archivos de trabajo real mezclados con el
codigo fuente de Vision, sin relacion con el "tema" de la sesion (ej.
Quimica vs Matematica). Si la sesion todavia no tiene carpeta asignada
(no deberia pasar en uso normal, ya que el modal de nueva sesion es
bloqueante, pero se cubre el caso igual para no romper turnos viejos/en
memoria), cae a `settings.workspace_path` como ultimo recurso."""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path

import pandas as pd

from app.config import get_settings
from app.lsp import diagnose_python_file
from app.tools.base import Tool

# Extensiones de datos tabulares que `read` parsea con pandas en vez de
# decodificar como texto plano -- REQ real (usuario 2026-09-16): antes,
# un .xlsx (binario, formato ZIP) pasaba por `p.read_text(errors="replace")`
# sin lanzar excepcion pero devolviendo puro ruido/mojibake, y el modelo
# interpretaba eso como "no puedo leer este formato" y le pedia al usuario
# que lo exporte a mano como CSV -- pese a que pandas/openpyxl (los engines
# reales) ya estan instalados. Vision necesita poder leer/analizar Excel de
# punta a punta sin esa friccion.
_EXCEL_EXTS = {".xlsx", ".xlsm", ".xls"}
_TABULAR_EXTS = _EXCEL_EXTS | {".csv", ".tsv", ".parquet"}


def _read_tabular(p: Path, offset: int, limit: int, sheet: str | int | None) -> str:
    """Devuelve un resumen legible por el modelo (hojas/columnas/tipos +
    preview de filas) de un archivo tabular, en vez de su contenido binario
    crudo. `offset`/`limit` paginan FILAS de datos (1-indexed, mismo
    contrato que el resto de `read` para archivos de texto)."""
    suffix = p.suffix.lower()
    sheet_names: list[str] | None = None
    active_sheet: str | int = sheet if sheet is not None else 0

    try:
        if suffix in _EXCEL_EXTS:
            xl = pd.ExcelFile(p)
            sheet_names = xl.sheet_names
            if sheet is None:
                active_sheet = sheet_names[0]
            df = xl.parse(active_sheet)
        elif suffix == ".parquet":
            df = pd.read_parquet(p)
        else:
            sep = "\t" if suffix == ".tsv" else ","
            df = pd.read_csv(p, sep=sep)
    except Exception as exc:
        return f"Error parseando '{p.name}' como archivo de datos: {exc}"

    n_rows, n_cols = df.shape
    start = max(offset - 1, 0)
    preview = df.iloc[start : start + limit]

    lines = [f"Archivo: {p.name} ({n_rows} filas x {n_cols} columnas)"]
    if sheet_names is not None:
        lines.append(
            f"Hojas: {', '.join(sheet_names)} (mostrando: '{active_sheet}' -- "
            "pasar `sheet` para elegir otra)"
        )
    lines.append("Columnas: " + ", ".join(f"{c} ({df[c].dtype})" for c in df.columns))
    lines.append(
        f"Filas {start + 1}-{min(start + limit, n_rows)} de {n_rows}:"
        if n_rows
        else "(sin filas)"
    )
    if not preview.empty:
        lines.append(preview.to_markdown(index=False))
    return "\n".join(lines)


def _base_dir_for_session(session) -> Path:
    """Resuelve el directorio base por defecto para una tool de
    filesystem: la carpeta de proyecto de `session` si tiene una
    asignada, sino el workspace generico de Vision como fallback."""
    if session is not None and getattr(session, "session_id", None):
        from app.persistence import (
            service as persistence,
        )  # import diferido: evita ciclo

        project_root = persistence.project_root_for_session(session.session_id)
        if project_root is not None:
            return project_root
    return get_settings().workspace_path


def _resolve(path: str, session=None) -> Path:
    p = Path(path)
    if not p.is_absolute():
        p = _base_dir_for_session(session) / p
    return p


# --------------------------------------------------------------------------
# read
# --------------------------------------------------------------------------
def _read_execute(args: dict, session=None) -> str:
    path = args.get("path", "")
    offset = int(args.get("offset", 1))
    limit = int(args.get("limit", 2000))
    if not path:
        return "Error: falta el argumento 'path'."

    p = _resolve(path, session)
    if not p.exists():
        return f"Error: no existe el path '{p}'."

    if p.is_dir():
        entries = sorted(p.iterdir(), key=lambda e: e.name)
        lines = [f"{e.name}/" if e.is_dir() else e.name for e in entries]
        return "\n".join(lines) if lines else "(directorio vacio)"

    # BUG real corregido (QA-001, deteccion QA 2026-09-21): `_read_tabular`
    # existia y funcionaba, pero NADIE la llamaba -- era codigo muerto. El
    # efecto era exactamente el que su propio comentario decia haber
    # resuelto: un .xlsx (binario ZIP) caia en el `read_text(errors="replace")`
    # de abajo, devolvia mojibake sin lanzar excepcion, y el modelo concluia
    # "no puedo leer este formato" y le pedia al usuario exportar a CSV a
    # mano -- pese a que pandas/openpyxl ya estan instalados.
    if p.suffix.lower() in _TABULAR_EXTS:
        return _read_tabular(p, offset, limit, args.get("sheet"))

    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return f"Error leyendo archivo: {exc}"

    all_lines = text.splitlines()
    start = max(offset - 1, 0)
    chunk = all_lines[start : start + limit]
    numbered = [f"{start + i + 1}: {line}" for i, line in enumerate(chunk)]
    return (
        "\n".join(numbered) if numbered else "(archivo vacio o offset fuera de rango)"
    )


read_tool = Tool(
    name="read",
    description=(
        "Lee un archivo de texto (con numeros de linea) o lista el contenido "
        "de un directorio. Usar 'offset'/'limit' para paginar archivos grandes. "
        "Los archivos de datos tabulares (.xlsx/.xlsm/.xls/.csv/.tsv/.parquet) se "
        "parsean con pandas y devuelven un resumen (hojas, columnas con su tipo y "
        "un preview de filas) en vez del binario crudo -- NO hace falta pedirle al "
        "usuario que los exporte a CSV. En un Excel con varias hojas, usar 'sheet' "
        "para elegir cual leer (por defecto, la primera)."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Ruta absoluta o relativa al workspace",
            },
            "offset": {
                "type": "integer",
                "description": "Linea inicial (1-indexed). En archivos tabulares, fila de datos inicial.",
                "default": 1,
            },
            "limit": {
                "type": "integer",
                "description": "Maximo de lineas (o de filas, en archivos tabulares) a devolver",
                "default": 2000,
            },
            "sheet": {
                "type": "string",
                "description": (
                    "Solo para Excel: nombre de la hoja a leer. Por defecto se lee la "
                    "primera; el resultado lista siempre todas las hojas disponibles."
                ),
            },
        },
        "required": ["path"],
    },
    execute=_read_execute,
)


# --------------------------------------------------------------------------
# write
# --------------------------------------------------------------------------
def _skill_scope_for_path(p: Path, session=None) -> str:
    """REQ-002 (spec update-skills): clasifica una ruta absoluta de un
    SKILL.md como 'global' (cae en global_skills_path/repo_path/skills_path,
    las 3 fuentes que escanea discover_skills()) o 'local' (cae en la
    subcarpeta skills/ de la carpeta de proyecto de la sesion, ver
    discover_local_skills()). 'unknown' si no cae en ninguna -- caso borde
    (ej. sesion sin carpeta asignada) donde de todos modos conviene avisar
    al frontend para que invalide sus caches, en vez de quedarse calladO."""
    from app.persistence import service as persistence  # import diferido: evita ciclo
    from app.project_context import get_project_config

    settings = get_settings()
    resolved = p.resolve()

    global_candidates = [settings.global_skills_path, settings.skills_path]
    cfg = get_project_config()
    if cfg.skills_repo_path:
        global_candidates.append(Path(cfg.skills_repo_path).resolve())
    for candidate in global_candidates:
        try:
            if resolved.is_relative_to(candidate.resolve()):
                return "global"
        except (OSError, ValueError):
            continue

    if session is not None and getattr(session, "session_id", None):
        project_root = persistence.project_root_for_session(session.session_id)
        if project_root is not None:
            local_skills_dir = (project_root / persistence.SKILLS_SUBDIR).resolve()
            try:
                if resolved.is_relative_to(local_skills_dir):
                    return "local"
            except (OSError, ValueError):
                pass

    return "unknown"


async def _write_execute(args: dict, session=None) -> str:
    path = args.get("path", "")
    content = args.get("content", "")
    if not path:
        return "Error: falta el argumento 'path'."

    p = _resolve(path, session)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    result = f"Archivo escrito: {p} ({len(content)} caracteres)."

    # REQ-002 (spec update-skills): BUG REAL reportado por el usuario --
    # cuando Vision crea una skill sola durante el chat (ej. "creame una
    # skill de X"), usa esta misma tool generica `write` (no hay una tool
    # dedicada a skills) para escribir el SKILL.md. Sin este evento, nada
    # le avisaba al frontend que el listado de skills cambio, y las 3
    # listas (panel rapido, Configuracion, menu "/") quedaban desactualizadas
    # hasta recargar la pagina. Deteccion por NOMBRE de archivo (no por
    # contenido/frontmatter) a proposito -- mas simple y sin falsos
    # negativos; sobrescribir un SKILL.md existente (edicion) tambien
    # dispara el evento, lo cual es aceptable (invalidar cache de mas no
    # rompe nada). Mismo mecanismo "evento fuera-de-banda" que ya usa
    # render_chart/chart-ready (ver app/tools/chart.py).
    if session is not None and p.name.lower() == "skill.md":
        session.push_event(
            "skill-created",
            {"path": str(p), "scope": _skill_scope_for_path(p, session)},
        )

    diagnostics = await diagnose_python_file(p)
    if diagnostics:
        result += f"\n\n{diagnostics}"
    return result


write_tool = Tool(
    name="write",
    description="Crea o sobrescribe un archivo con el contenido dado (crea carpetas intermedias si hacen falta).",
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Ruta absoluta o relativa al workspace",
            },
            "content": {
                "type": "string",
                "description": "Contenido completo a escribir",
            },
        },
        "required": ["path", "content"],
    },
    execute=_write_execute,
    dangerous=True,
)


# --------------------------------------------------------------------------
# edit
# --------------------------------------------------------------------------
async def _edit_execute(args: dict, session=None) -> str:
    path = args.get("path", "")
    old_string = args.get("old_string", "")
    new_string = args.get("new_string", "")
    replace_all = bool(args.get("replace_all", False))
    if not path or not old_string:
        return "Error: se requieren 'path' y 'old_string'."

    p = _resolve(path, session)
    if not p.exists():
        return f"Error: no existe el path '{p}'."

    text = p.read_text(encoding="utf-8", errors="replace")
    count = text.count(old_string)
    if count == 0:
        return "Error: 'old_string' no se encontro en el archivo."
    if count > 1 and not replace_all:
        return (
            f"Error: 'old_string' aparece {count} veces. Agrega mas contexto para "
            "identificar una unica coincidencia, o pasa replace_all=true."
        )

    new_text = (
        text.replace(old_string, new_string)
        if replace_all
        else text.replace(old_string, new_string, 1)
    )
    p.write_text(new_text, encoding="utf-8")
    result = f"Archivo editado: {p} ({count if replace_all else 1} reemplazo(s))."
    diagnostics = await diagnose_python_file(p)
    if diagnostics:
        result += f"\n\n{diagnostics}"
    return result


edit_tool = Tool(
    name="edit",
    description="Reemplaza una porcion exacta de texto dentro de un archivo existente.",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "old_string": {
                "type": "string",
                "description": "Texto exacto a reemplazar",
            },
            "new_string": {"type": "string", "description": "Texto de reemplazo"},
            "replace_all": {"type": "boolean", "default": False},
        },
        "required": ["path", "old_string", "new_string"],
    },
    execute=_edit_execute,
    dangerous=True,
)


# --------------------------------------------------------------------------
# glob
# --------------------------------------------------------------------------
def _glob_execute(args: dict, session=None) -> str:
    pattern = args.get("pattern", "")
    path = args.get("path", "")
    if not pattern:
        return "Error: falta el argumento 'pattern'."

    base = _resolve(path, session) if path else _base_dir_for_session(session)
    if not base.exists():
        return f"Error: no existe el directorio base '{base}'."

    matches = sorted(
        base.glob(pattern),
        key=lambda p: p.stat().st_mtime if p.exists() else 0,
        reverse=True,
    )
    if not matches:
        return "(sin resultados)"
    return "\n".join(str(m) for m in matches[:200])


glob_tool = Tool(
    name="glob",
    description="Busca archivos por patron glob (ej. '**/*.py') dentro de un directorio base.",
    parameters={
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Patron glob, ej. '**/*.py'"},
            "path": {
                "type": "string",
                "description": "Directorio base (default: workspace)",
            },
        },
        "required": ["pattern"],
    },
    execute=_glob_execute,
)


# --------------------------------------------------------------------------
# grep
# --------------------------------------------------------------------------
def _grep_execute(args: dict, session=None) -> str:
    pattern = args.get("pattern", "")
    path = args.get("path", "")
    include = args.get("include", "*")
    if not pattern:
        return "Error: falta el argumento 'pattern'."

    base = _resolve(path, session) if path else _base_dir_for_session(session)
    if not base.exists():
        return f"Error: no existe el directorio base '{base}'."

    try:
        regex = re.compile(pattern)
    except re.error as exc:
        return f"Error: patron regex invalido: {exc}"

    results: list[str] = []
    for file_path in base.rglob("*"):
        if not file_path.is_file():
            continue
        if not fnmatch.fnmatch(file_path.name, include):
            continue
        try:
            text = file_path.read_text(encoding="utf-8", errors="ignore")
        except Exception:  # noqa: S112 -- best-effort: un archivo ilegible no debe frenar el grep del resto
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if regex.search(line):
                results.append(f"{file_path}:{lineno}: {line.strip()[:300]}")
                if len(results) >= 300:
                    break
        if len(results) >= 300:
            break

    return "\n".join(results) if results else "(sin coincidencias)"


grep_tool = Tool(
    name="grep",
    description="Busca un patron regex dentro del contenido de archivos de un directorio.",
    parameters={
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Expresion regular a buscar"},
            "path": {
                "type": "string",
                "description": "Directorio base (default: workspace)",
            },
            "include": {
                "type": "string",
                "description": "Filtro de nombre de archivo, ej. '*.py'",
                "default": "*",
            },
        },
        "required": ["pattern"],
    },
    execute=_grep_execute,
)
