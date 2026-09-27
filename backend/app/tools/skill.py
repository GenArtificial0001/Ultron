"""
Tool `skill`: carga bajo demanda el contenido completo de un archivo
SKILL.md (o <nombre>.md) desde `settings.skills_path`, replicando el rol
de packages/core/src/tool/skill.ts de OpenCode.

Cada subcarpeta directa de `skills_dir` que contenga un SKILL.md se
considera una skill disponible. `list_available_skills()` arma el bloque
`<available_skills>` que se inyecta en el system prompt (ver
app/llm/loop.py), y esta tool devuelve el contenido completo cuando el
modelo decide invocarla por nombre exacto.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.persistence import master_index
from app.project_context import get_project_config
from app.tool_timings import record_duration
from app.tools.base import Tool

logger = logging.getLogger(__name__)

# Config de aplicacion (nivel VISION, persistida en la DB maestra .vision/vision.db,
# NO por sesion) -- pedido del usuario (2026-09): las skills GLOBALES no se
# precargan / quedan "vacias" hasta que se vinculen o se habiliten; se pueden
# activar/desactivar cuando se quiera, y el repo de skills se vincula por ruta.
SKILLS_GLOBAL_ENABLED_KEY = "skills_global_enabled"
SKILLS_REPO_PATH_KEY = "skills_repo_path"

# Cache del escaneo de skills de las 3 fuentes globales (`discover_skills`).
# Se vuelve a escanear el filesystem en CADA turno (via list_available_skills_xml,
# que arma el bloque <available_skills> del system prompt) y en cada ejecucion
# de la tool `skill`. Con 18+ skills son ~30ms de I/O por llamada, la mayoria
# repitiendo lo mismo. Este cache TTL corto amortigua eso SIN cambiar la
# semantica: la clave incluye las 3 rutas que determinan el resultado, asi que
# si el usuario cambia a mitad de sesion la carpeta de skills (Configuracion) o
# la del repo, el cache se invalida solo en la proxima lectura.
_SKILL_CACHE_TTL_S = 30.0
_skill_cache: tuple[float, tuple[str, str, str], list[dict[str, str]]] | None = None
_skill_cache_lock = threading.Lock()


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Extrae un frontmatter YAML simple (name/description) sin depender
    de una libreria YAML completa -- alcanza con 'clave: valor' por linea."""
    meta: dict[str, str] = {}
    body = text
    match = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.DOTALL)
    if match:
        raw_meta, body = match.group(1), match.group(2)
        for line in raw_meta.splitlines():
            if ":" in line:
                key, _, value = line.partition(":")
                meta[key.strip()] = value.strip().strip('"')
    return meta, body


def _scan_skills_dir(base: Path) -> list[dict[str, str]]:
    """Escanea un directorio en busca de subcarpetas con un SKILL.md."""
    if not base.exists():
        return []

    found = []
    for sub in sorted(base.iterdir()):
        if not sub.is_dir():
            continue
        skill_file = sub / "SKILL.md"
        if not skill_file.exists():
            continue
        text = skill_file.read_text(encoding="utf-8", errors="replace")
        meta, _ = _parse_frontmatter(text)
        found.append(
            {
                "name": meta.get("name", sub.name),
                "description": meta.get("description", ""),
                "path": str(skill_file),
            }
        )
    return found


def discover_skills() -> list[dict[str, str]]:
    """Escanea las skills GLOBALES (usuario con skills_repo vinculado + Vision local).

    Pedido del usuario (2026-09): por defecto NO se precargan -- si
    `skills_global_enabled` (config de Vision en la DB maestra) no es "true",
    se devuelve [] ("vacias") hasta que se vinculen o habiliten. Las skills
    locales de la SESION se agregan aparte en list_available_skills_xml."""
    global _skill_cache
    settings = get_settings()
    cfg = get_project_config()
    global_enabled = master_index.get_app_setting(SKILLS_GLOBAL_ENABLED_KEY) == "true"
    repo_path = (
        master_index.get_app_setting(SKILLS_REPO_PATH_KEY) or cfg.skills_repo_path or ""
    )
    cache_key = (str(global_enabled), repo_path, str(settings.skills_path))

    now = time.monotonic()
    with _skill_cache_lock:
        if _skill_cache is not None:
            ts, key, cached = _skill_cache
            if now - ts < _SKILL_CACHE_TTL_S and key == cache_key:
                return [dict(s) for s in cached]

    if not global_enabled:
        with _skill_cache_lock:
            _skill_cache = (time.monotonic(), cache_key, [])
        return []

    by_name: dict[str, dict[str, str]] = {}
    for s in _scan_skills_dir(settings.global_skills_path):
        by_name[s["name"]] = s
    if repo_path:
        for s in _scan_skills_dir(Path(repo_path)):
            by_name[s["name"]] = s
    for s in _scan_skills_dir(settings.skills_path):
        by_name[s["name"]] = s

    result = sorted(by_name.values(), key=lambda s: s["name"])
    with _skill_cache_lock:
        _skill_cache = (time.monotonic(), cache_key, [dict(s) for s in result])
    return result


def list_available_skills_xml(session_id: str | None = None) -> str:
    """Arma el bloque <available_skills> para inyectar en el system prompt
    -- globales SIEMPRE + locales de `session_id` si se pasa (arquitectura
    carpeta-por-sesion: el LLM debe conocer de entrada las skills propias
    del tema/proyecto de ESTA sesion, sin que el usuario tenga que pedirle
    explicitamente que las cargue)."""
    skills = discover_skills()
    if session_id:
        by_name = {s["name"]: s for s in skills}
        for s in discover_local_skills(session_id):
            by_name[s["name"]] = s  # local pisa global en caso de colision de nombre
        skills = sorted(by_name.values(), key=lambda s: s["name"])
    if not skills:
        return ""
    items = "\n".join(
        f"  <skill>\n    <name>{s['name']}</name>\n    <description>{s['description']}</description>\n  </skill>"
        for s in skills
    )
    return f"<available_skills>\n{items}\n</available_skills>"


# ---------------------------------------------------------------------------
# Skills LOCALES de sesion (comando "/local-skills", arquitectura
# carpeta-por-sesion): a diferencia de `discover_skills()` de arriba (que
# lee SIEMPRE las mismas 3 fuentes globales del usuario/instalacion), esto
# lee la subcarpeta fija `skills/` DENTRO de la carpeta de proyecto de
# CADA sesion -- son skills propias de ese tema/proyecto puntual (ej. una
# skill de "Quimica organica" solo tiene sentido en la sesion de Quimica,
# no en la de Matematica). Pedido explicito del usuario: "/skills" sigue
# mostrando las globales de siempre, "/local-skills" es un comando NUEVO
# y separado para estas.
# ---------------------------------------------------------------------------
def discover_local_skills(session_id: str) -> list[dict[str, str]]:
    """Escanea `skills/` dentro de la carpeta de proyecto de `session_id`
    -- [] si la sesion no tiene carpeta asignada o la subcarpeta esta
    vacia (nunca lanza, mismo criterio 'mejor esfuerzo' del resto)."""
    from app.persistence import service as persistence  # import diferido: evita ciclo

    project_root = persistence.project_root_for_session(session_id)
    if project_root is None:
        return []
    return sorted(
        _scan_skills_dir(project_root / persistence.SKILLS_SUBDIR),
        key=lambda s: s["name"],
    )


# ---------------------------------------------------------------------------
# REQ-008/009 (spec agentes-huerfanos-y-gestion-skills): CRUD de skills
# LOCALES de sesion desde el panel de Configuracion > Skills -- mismo
# patron que `app/commands.py` (save_custom_command/delete_custom_command):
# un archivo por skill (SKILL.md DENTRO de su propia subcarpeta, a
# diferencia de comandos que es un .md suelto -- asi la skill puede tener
# archivos hermanos, ver `_skill_execute` mas arriba). Las skills GLOBALES
# (carpeta configurable, ver SKILLS_REPO_PATH_KEY) son de SOLO LECTURA
# desde Vision (decision registrada en spec.md, seccion Reglas de negocio:
# evitar que Vision pise una fuente externa que podria resincronizarse por
# otro medio en cualquier momento) -- por eso no hay
# save/delete_global_skill, solo local.
# ---------------------------------------------------------------------------
_INVALID_SKILL_NAME_CHARS = set('<>:"/\\|?*')


def _local_skill_dir(project_path: str, name: str) -> Path:
    from app.persistence import service as persistence  # import diferido: evita ciclo

    return Path(project_path) / persistence.SKILLS_SUBDIR / name


def save_local_skill(
    project_path: str,
    name: str,
    description: str,
    content: str,
    *,
    original_name: str = "",
) -> list[dict[str, str]]:
    """Crea o actualiza una skill LOCAL de sesion (`skills/<name>/SKILL.md`).
    `original_name` != `name` implica un RENOMBRE (edicion cambiando el
    nombre): se crea la carpeta nueva y se borra la vieja, para no dejar
    dos copias huerfanas de la misma skill. Lanza ValueError con el mismo
    criterio de validacion que `save_custom_command` (nombre/contenido
    vacios) mas la validacion de caracteres invalidos de Windows ya usada
    en `project_context.create_local_folder`."""
    name = name.strip()
    description = description.strip()
    content = content.strip()
    if not name:
        raise ValueError("El nombre de la skill no puede estar vacío.")
    if any(ch in _INVALID_SKILL_NAME_CHARS for ch in name):
        raise ValueError(
            f"El nombre no puede contener ninguno de estos caracteres: {' '.join(_INVALID_SKILL_NAME_CHARS)}"
        )
    if not content:
        raise ValueError("El contenido de la skill no puede estar vacío.")

    skill_dir = _local_skill_dir(project_path, name)
    skill_dir.mkdir(parents=True, exist_ok=True)
    frontmatter = f'---\nname: {name}\ndescription: "{description}"\n---\n'
    (skill_dir / "SKILL.md").write_text(frontmatter + content + "\n", encoding="utf-8")

    if original_name and original_name != name:
        old_dir = _local_skill_dir(project_path, original_name)
        if old_dir.exists():
            import shutil

            # Bug REAL encontrado en QA manual (2026-09-19), NO teorico: en
            # una carpeta sincronizada por OneDrive (ver AGENTS.md workspace:
            # todas las sesiones reales del usuario viven bajo
            # "OneDrive - Empresa\..."), la carpeta vieja queda con el
            # atributo ReparsePoint (placeholder "Files On-Demand"). Con
            # `ignore_errors=True` (version anterior de este codigo),
            # `rmtree` fallaba en SILENCIO al querer borrar el directorio
            # raiz ya vacio, dejando una carpeta HUERFANA sin avisar a
            # nadie -- literalmente el mismo tipo de estado roto silencioso
            # que esta feature entera busca evitar para agentes. Fix:
            # `ignore_errors=False` + limpieza best-effort del directorio
            # residual con `os.rmdir` (que si tolera un reparse point ya
            # vacio) envuelta en su propio try/except, en vez de tragarse
            # CUALQUIER error de rmtree sin dejar rastro.
            try:
                shutil.rmtree(old_dir)
            except OSError:
                logger.warning(
                    "No se pudo borrar por completo la carpeta vieja de la skill "
                    "renombrada '%s' (%s) -- puede ser un placeholder de OneDrive "
                    "'Files On-Demand'. Reintentando solo el directorio.",
                    original_name,
                    old_dir,
                    exc_info=True,
                )
                try:
                    import os

                    os.rmdir(old_dir)
                except OSError:
                    logger.warning(
                        "Quedó una carpeta residual (probablemente vacía) en '%s' -- "
                        "no impide que la skill renombrada funcione, pero conviene "
                        "borrarla a mano si OneDrive no la limpia solo.",
                        old_dir,
                    )

    return discover_local_skills_by_path(project_path)


def delete_local_skill(project_path: str, name: str) -> list[dict[str, str]]:
    """REQ-009: borra la carpeta completa de una skill local por nombre.
    No-op (sin error) si no existia -- mismo criterio que
    `delete_custom_command`."""
    import shutil

    skill_dir = _local_skill_dir(project_path, name.strip())
    if skill_dir.exists():
        shutil.rmtree(skill_dir, ignore_errors=True)
    return discover_local_skills_by_path(project_path)


def discover_local_skills_by_path(project_path: str) -> list[dict[str, str]]:
    """Variante de `discover_local_skills` que recibe la carpeta de
    proyecto YA resuelta (en vez de `session_id`) -- usada por
    save/delete_local_skill que ya tienen `project_path` a mano, evita un
    segundo round-trip a `project_root_for_session`."""
    from app.persistence import service as persistence  # import diferido: evita ciclo

    return sorted(
        _scan_skills_dir(Path(project_path) / persistence.SKILLS_SUBDIR),
        key=lambda s: s["name"],
    )


def discover_local_skills_with_body(session_id: str) -> list[dict[str, str]]:
    """REQ-008: variante de `discover_local_skills` que ADEMAS incluye el
    cuerpo (sin frontmatter) de cada SKILL.md -- usada exclusivamente por
    el panel de edicion (`GET /api/local-skills/{session_id}`) para poder
    precargar el editor con el contenido real sin necesitar un endpoint
    generico de "leer archivo por ruta" (evita abrir una superficie nueva
    de path traversal solo para este caso puntual, ver nota de seguridad
    en spec.md seccion Skills sugeridas)."""
    result = []
    for s in discover_local_skills(session_id):
        try:
            text = Path(s["path"]).read_text(encoding="utf-8", errors="replace")
            _, body = _parse_frontmatter(text)
        except OSError:
            body = ""
        result.append({**s, "content": body.strip()})
    return result


def _skill_execute(args: dict, session: Any = None) -> str:
    name = args.get("name", "")
    if not name:
        return "Error: falta el argumento 'name'."

    session_id = getattr(session, "session_id", None)

    # REQ-001 (spec barra-progreso-adaptativa): se mide con una clave
    # ESPECIFICA por skill ('skill:<nombre>'), no solo bajo el nombre
    # generico de la tool 'skill' -- cada skill tiene un tamano/tiempo de
    # lectura distinto (una skill de 500 lineas tarda mas que una de 50),
    # asi que conviene una estimacion propia por skill, no una promediada
    # entre todas.
    start = time.monotonic()
    try:
        # Busca primero en las globales, despues en las locales de esta
        # sesion (mismo orden que list_available_skills_xml -- local pisa
        # global en caso de colision de nombre).
        all_skills = discover_skills()
        if session_id:
            all_skills = all_skills + discover_local_skills(session_id)
        for s in reversed(all_skills):
            if s["name"] == name:
                skill_file = Path(s["path"])
                text = skill_file.read_text(encoding="utf-8", errors="replace")
                _, body = _parse_frontmatter(text)
                siblings = [
                    p.name
                    for p in skill_file.parent.iterdir()
                    if p.is_file() and p.name != "SKILL.md"
                ]
                siblings_txt = (
                    f"\nArchivos relacionados: {', '.join(siblings[:10])}"
                    if siblings
                    else ""
                )
                return f'<skill_content name="{name}">\n{body}\n</skill_content>{siblings_txt}'

        disponibles = ", ".join(s["name"] for s in all_skills) or "(ninguna)"
        return f"Error: no existe la skill '{name}'. Disponibles: {disponibles}"
    finally:
        elapsed_ms = (time.monotonic() - start) * 1000
        record_duration(f"skill:{name}", elapsed_ms)


skill_tool = Tool(
    name="skill",
    description="Carga el contenido completo de una skill (SKILL.md) por su nombre exacto.",
    parameters={
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
    },
    execute=_skill_execute,
)
