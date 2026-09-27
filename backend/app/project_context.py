"""
Gestion del "contexto de proyecto" activo de Vision.

CAMBIO DE ARQUITECTURA (carpeta-por-sesion, definitiva -- ver docstring
largo en app/persistence/service.py): la carpeta LOCAL de proyecto dejo
de ser una preferencia GLOBAL configurable aca (`cfg.local_path` fue
eliminado) -- cada sesion tiene la suya propia, obligatoria desde su
creacion (ver POST /api/sessions/{id}/project-path, indice maestro en
app/persistence/master_index.py). `build_project_context_prompt()` ahora
recibe `session_id` y arma el arbol/docs clave de la carpeta de ESA
sesion, no de un valor global.

Lo que SIGUE siendo config global aca (no es por-sesion, son integraciones
de infraestructura/cuenta, no "de que trata esta conversacion"): la
carpeta de SharePoint activa, la ruta del repo de skills compartidas, y
los parametros de generacion del modelo (temperature/top_p/etc). Todo eso
sigue en un JSON simple en disco (`backend/data/project_config.json`), NO
en el .env (eso es config de credenciales/infra, esto es preferencia de
uso que el usuario cambia seguido desde la UI).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from app.config import BASE_DIR

CONFIG_FILE = BASE_DIR / "data" / "project_config.json"

# Carpetas que nunca se muestran en el explorador ni se cuentan en el
# arbol de contexto -- ruido tecnico, no "proyecto" real.
IGNORED_DIR_NAMES = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".idea",
    ".vscode",
    "dist",
    "build",
    ".next",
    ".cache",
    ".pytest_cache",
}

# Archivos que se leen COMPLETOS como "docs clave" al armar el contexto
# inicial (ver Fase de brainstorming: "Arbol de archivos + docs clave").
KEY_DOC_NAMES = [
    "README.md",
    "readme.md",
    "AGENTS.md",
    "CLAUDE.md",
    "SKILL.md",
    "package.json",
    "requirements.txt",
    "pyproject.toml",
]

MAX_TREE_ENTRIES = 400
MAX_KEY_DOC_CHARS = 6000


@dataclass
class ProjectConfig:
    sharepoint_drive_id: str = ""
    sharepoint_drive_name: str = ""
    sharepoint_folder_id: str = ""
    sharepoint_folder_path: str = ""
    # Carpeta local donde Vision busca/guarda skills adicionales (ver
    # app/tools/skill.py). Puede ser un clon local del repo de GitHub
    # un repositorio de skills (ver app/skills_repo.py) u otra carpeta
    # cualquiera -- Vision no distingue el origen, solo lee/escribe
    # subcarpetas con SKILL.md ahi.
    skills_repo_path: str = ""
    # Parametros de generacion elegidos en Configuracion (ver §1 del
    # apartado "Modelo e inteligencia"). None = no se manda ese parametro
    # (deja que el proveedor de IA use el default del modelo). Se filtran contra
    # `valid_args` real del modelo activo antes de mandarse (ver
    # app/llm/loop.py) para nunca mandar un parametro que el modelo no
    # soporte.
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    reasoning_effort: str = ""
    verbosity: str = ""
    small_model: str = ""

    # spec controles-seguridad-optimizacion-lightspeed-bigquery, pestaña
    # "Conexiones" (pedido explicito del usuario 2026-09-21: "necesito
    # poder desde la configuracion cambiar el tope limite para determinar
    # que es una consulta pesada"). None = usar el default de
    # `Settings`/`.env` (`app/config.py`) -- estos campos SOLO existen para
    # que el usuario los pise desde la UI sin editar `.env` a mano ni
    # reiniciar el backend (a diferencia de Settings, que es @lru_cache y
    # normalmente fijo desde el arranque). Ver `app/tools/dbquery.py:
    # _effective_bq_cost_threshold()` y hermanas para la resolucion real
    # (ProjectConfig gana si esta seteado, sino cae a Settings).
    bq_cost_guardrail_bytes: int | None = None
    export_row_threshold: int | None = None
    export_preview_rows: int | None = None

    # spec salto-automatico-modelos REQ-001: lista de prioridad que arma el
    # usuario en Configuracion > Modelo e inteligencia. Apagado por defecto
    # (REQ-006: sin activarlo, el comportamiento es el de siempre). La lista
    # por defecto y los limites viven en app/llm/model_fallback.py.
    model_fallback_enabled: bool = False
    model_fallback_order: list[str] = field(
        default_factory=lambda: ["claude-opus-5-5", "claude-sonnet-5", "claude-haiku-4-5-20251001"]
    )
    model_fallback_pause_minutes: int = 10


def _load() -> ProjectConfig:
    if not CONFIG_FILE.exists():
        return ProjectConfig()
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        return ProjectConfig(
            **{k: v for k, v in data.items() if k in ProjectConfig.__annotations__}
        )
    except Exception:
        return ProjectConfig()


def _save(cfg: ProjectConfig) -> None:
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(
        json.dumps(asdict(cfg), ensure_ascii=False, indent=2), encoding="utf-8"
    )


def get_project_config() -> ProjectConfig:
    return _load()


def set_sharepoint_project(
    drive_id: str, drive_name: str, folder_id: str, folder_path: str
) -> ProjectConfig:
    cfg = _load()
    cfg.sharepoint_drive_id = drive_id
    cfg.sharepoint_drive_name = drive_name
    cfg.sharepoint_folder_id = folder_id
    cfg.sharepoint_folder_path = folder_path
    _save(cfg)
    return cfg


def clear_sharepoint_project() -> ProjectConfig:
    cfg = _load()
    cfg.sharepoint_drive_id = ""
    cfg.sharepoint_drive_name = ""
    cfg.sharepoint_folder_id = ""
    cfg.sharepoint_folder_path = ""
    _save(cfg)
    return cfg


def set_skills_repo_path(path: str) -> ProjectConfig:
    cfg = _load()
    cfg.skills_repo_path = path
    _save(cfg)
    return cfg


def set_generation_params(**kwargs: Any) -> ProjectConfig:
    """Actualiza cualquier subconjunto de los campos de generacion
    (temperature/top_p/max_tokens/reasoning_effort/verbosity/small_model).
    Pasar None explicito borra ese parametro (vuelve al default del modelo)."""
    cfg = _load()
    for key, value in kwargs.items():
        if key in ProjectConfig.__annotations__:
            setattr(cfg, key, value)
    _save(cfg)
    return cfg


def set_model_fallback(
    *, enabled: bool | None = None, order: list[str] | None = None, pause_minutes: int | None = None
) -> ProjectConfig:
    """spec salto-automatico-modelos REQ-001: guarda lo que se mande (None = no
    tocar ese campo). La validacion la hace el endpoint (`/api/config/model-fallback`)."""
    cfg = _load()
    if enabled is not None:
        cfg.model_fallback_enabled = enabled
    if order is not None:
        cfg.model_fallback_order = list(order)
    if pause_minutes is not None:
        cfg.model_fallback_pause_minutes = pause_minutes
    _save(cfg)
    return cfg


def set_connection_thresholds(**kwargs: Any) -> ProjectConfig:
    """Pestaña 'Conexiones' (pedido del usuario 2026-09-21): actualiza
    cualquier subconjunto de `bq_cost_guardrail_bytes`/`export_row_threshold`/
    `export_preview_rows`. Pasar None explicito vuelve a usar el default de
    `Settings`/`.env` para ese campo puntual."""
    cfg = _load()
    for key, value in kwargs.items():
        if key in ProjectConfig.__annotations__:
            setattr(cfg, key, value)
    _save(cfg)
    return cfg


# ---------------------------------------------------------------------------
# Filesystem local: listado de unidades/carpetas para el explorador, y
# construccion del arbol + docs clave para el contexto inicial del agente.
# ---------------------------------------------------------------------------
def list_local_roots() -> list[dict]:
    """Unidades disponibles en Windows (C:\\, D:\\, ...) como raices del
    explorador de carpetas locales."""
    import string
    from pathlib import Path as P

    roots = []
    for letter in string.ascii_uppercase:
        p = P(f"{letter}:/")
        if p.exists():
            roots.append({"path": str(p), "name": f"{letter}:\\"})
    return roots


def list_local_children(path: str) -> list[dict]:
    """Lista subcarpetas de `path` (solo directorios, para navegar) --
    igual criterio de exclusion que IGNORED_DIR_NAMES."""
    p = Path(path)
    if not p.exists() or not p.is_dir():
        raise FileNotFoundError(f"No existe o no es un directorio: {path}")

    entries = []
    try:
        for child in sorted(p.iterdir(), key=lambda c: c.name.lower()):
            if not child.is_dir():
                continue
            if child.name in IGNORED_DIR_NAMES or child.name.startswith("."):
                continue
            entries.append({"path": str(child), "name": child.name})
    except PermissionError:
        pass
    return entries


# Caracteres invalidos en nombres de archivo/carpeta de Windows -- se
# valida el nombre ANTES de construir la ruta, en vez de dejar que
# Path.mkdir() falle con un OSError crudo dificil de explicarle al usuario.
_INVALID_FOLDER_NAME_CHARS = set('<>:"/\\|?*')


def create_local_folder(parent_path: str, folder_name: str) -> dict:
    """Crea una subcarpeta nueva DENTRO de `parent_path` (nunca en otro
    lado -- ver spec: 'siempre dentro de la carpeta actual que estas
    viendo'), usada por el boton '+ Nueva carpeta' del explorador de
    Configuracion > Proyecto local / Skills."""
    folder_name = folder_name.strip()
    if not folder_name:
        raise ValueError("El nombre de la carpeta no puede estar vacio.")
    if any(ch in _INVALID_FOLDER_NAME_CHARS for ch in folder_name):
        raise ValueError(
            f"El nombre no puede contener ninguno de estos caracteres: {' '.join(_INVALID_FOLDER_NAME_CHARS)}"
        )
    if folder_name in (".", ".."):
        raise ValueError("Nombre de carpeta invalido.")

    parent = Path(parent_path)
    if not parent.exists() or not parent.is_dir():
        raise FileNotFoundError(f"La carpeta padre no existe: {parent_path}")

    new_folder = parent / folder_name
    if new_folder.exists():
        raise FileExistsError(
            f"Ya existe una carpeta o archivo llamado '{folder_name}' ahi."
        )

    new_folder.mkdir(parents=False, exist_ok=False)
    return {"path": str(new_folder), "name": new_folder.name}


def _should_ignore(name: str) -> bool:
    return name in IGNORED_DIR_NAMES or name.startswith(".")


def build_local_project_tree(path: str) -> dict:
    """Arbol de archivos (nombres/estructura, sin contenido) + contenido
    completo de los "docs clave" que existan (README/AGENTS.md/etc.) --
    ver Fase de diseno: 'Arbol de archivos + docs clave'. Limitado a
    MAX_TREE_ENTRIES para no generar un prompt gigante en proyectos enormes."""
    root = Path(path)
    if not root.exists() or not root.is_dir():
        raise FileNotFoundError(f"No existe o no es un directorio: {path}")

    lines: list[str] = []
    count = 0

    def walk(d: Path, prefix: str = "") -> None:
        nonlocal count
        if count >= MAX_TREE_ENTRIES:
            return
        try:
            children = sorted(d.iterdir(), key=lambda c: (c.is_file(), c.name.lower()))
        except PermissionError:
            return
        for child in children:
            if count >= MAX_TREE_ENTRIES:
                lines.append(
                    f"{prefix}... (arbol truncado, mas de {MAX_TREE_ENTRIES} entradas)"
                )
                return
            if _should_ignore(child.name):
                continue
            if child.is_dir():
                lines.append(f"{prefix}{child.name}/")
                count += 1
                walk(child, prefix + "  ")
            else:
                lines.append(f"{prefix}{child.name}")
                count += 1

    walk(root)

    key_docs: dict[str, str] = {}
    for doc_name in KEY_DOC_NAMES:
        doc_path = root / doc_name
        if doc_path.exists() and doc_path.is_file():
            try:
                text = doc_path.read_text(encoding="utf-8", errors="replace")
                key_docs[doc_name] = text[:MAX_KEY_DOC_CHARS]
            except Exception:  # noqa: S112 -- best-effort: un doc ilegible no debe frenar el resto
                continue

    return {"path": str(root), "tree": "\n".join(lines), "key_docs": key_docs}


def build_project_context_prompt(session_id: str | None = None) -> str:
    """Arma el bloque de texto que se inyecta en el system prompt del
    agente con el contexto del proyecto activo (carpeta de ESTA sesion +
    SharePoint global), igual espiritu que Claude Code arma contexto al
    abrir un repo. Devuelve string vacio si no hay ningun proyecto
    configurado.

    Arquitectura carpeta-por-sesion (definitiva): la carpeta local ya NO
    es un valor global (`cfg.local_path` fue eliminado) -- se resuelve la
    carpeta PROPIA de `session_id` via el indice maestro. Sin
    `session_id` (o sin carpeta asignada todavia), simplemente no hay
    bloque de "Proyecto local activo"."""
    cfg = get_project_config()
    parts: list[str] = []

    local_path = None
    if session_id:
        from app.persistence import master_index  # import diferido: evita ciclo

        local_path = master_index.get_project_path(session_id)

    if local_path:
        try:
            info = build_local_project_tree(local_path)
            parts.append(f"## Proyecto local activo: {info['path']}\n")
            if info["tree"]:
                parts.append(f"Estructura de archivos:\n```\n{info['tree']}\n```\n")
            for name, content in info["key_docs"].items():
                parts.append(f"### {name}\n```\n{content}\n```\n")
        except Exception as exc:
            parts.append(
                f"(No se pudo cargar el proyecto local '{local_path}': {exc})\n"
            )

    if cfg.sharepoint_drive_id:
        parts.append(
            f"## Carpeta de SharePoint activa: biblioteca '{cfg.sharepoint_drive_name}', "
            f"carpeta '{cfg.sharepoint_folder_path or '(raiz)'}' "
            f"(drive_id={cfg.sharepoint_drive_id}, folder_id={cfg.sharepoint_folder_id or 'root'}). "
            "Usa las tools de SharePoint para explorar su contenido cuando el usuario lo pida.\n"
        )

    if not parts:
        return ""

    return (
        "<project_context>\n"
        "El usuario configuro el siguiente contexto de proyecto desde Configuracion. "
        "Ya tenes esta informacion de entrada, no hace falta pedirsela ni volver a listarla "
        "salvo que necesites mas detalle de un archivo puntual (usa la tool `read`).\n\n"
        + "\n".join(parts)
        + "</project_context>"
    )
