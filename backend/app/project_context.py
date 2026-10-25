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
import os
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

    # spec adjuntar-archivos-chat REQ-007 a: umbrales de tamano de un adjunto,
    # editables en Configuracion (mismo patron que `bq_cost_guardrail_bytes`).
    # Sobre `attachment_warn_mb` se pide confirmar ("copiarlo ocupa ese espacio");
    # sobre `attachment_inplace_mb` ya no se sube: se lee en su lugar por ruta.
    attachment_warn_mb: int = 100
    attachment_inplace_mb: int = 500

    # spec leer-imagenes-pdf-word REQ-002/004/007: filtro de imagenes utiles y umbral de costo para preguntar antes de leerlas.
    # Se pregunta si hay MAS de `image_ask_count` imagenes utiles O si cuestan MAS de `image_ask_pct` % de la ventana del modelo.
    image_min_px: int = 64
    image_ask_count: int = 5
    image_ask_pct: int = 10

    # spec salto-automatico-modelos REQ-001: lista de prioridad que arma el
    # usuario en Configuracion > Modelo e inteligencia. Apagado por defecto
    # (REQ-006: sin activarlo, el comportamiento es el de siempre). La lista
    # por defecto y los limites viven en app/llm/model_fallback.py.
    model_fallback_enabled: bool = False
    # spec varios-proveedores REQ-005: la lista arranca VACIA (decision del
    # usuario: "vacia, la armo yo"); admite modelos de cualquier proveedor.
    model_fallback_order: list[str] = field(default_factory=list)
    model_fallback_pause_minutes: int = 10

    # spec varios-proveedores REQ-003/006: modelos que se muestran de cada
    # proveedor ({"gemini": ["gemini-2.5-flash", ...]}; sin entrada = los
    # sugeridos) y proveedores gratuitos que el usuario confirmo.
    provider_models: dict[str, list[str]] = field(default_factory=dict)
    provider_consent: dict[str, bool] = field(default_factory=dict)


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


def set_attachment_thresholds(warn_mb: int | None = None, inplace_mb: int | None = None) -> ProjectConfig:
    """REQ-007 a: guarda los umbrales de adjuntos. Se valida que sean positivos y que
    el de "leer en su lugar" no sea menor que el de aviso (si no, el aviso nunca
    se veria). ValueError con mensaje para personas si no cumplen."""
    cfg = _load()
    warn = cfg.attachment_warn_mb if warn_mb is None else warn_mb
    inplace = cfg.attachment_inplace_mb if inplace_mb is None else inplace_mb
    if warn < 1 or inplace < 1:
        raise ValueError("Los umbrales tienen que ser de al menos 1 MB.")
    if inplace < warn:
        raise ValueError("El límite para leer en su lugar no puede ser menor que el de aviso.")
    cfg.attachment_warn_mb = warn
    cfg.attachment_inplace_mb = inplace
    _save(cfg)
    return cfg


def set_image_thresholds(
    min_px: int | None = None, ask_count: int | None = None, ask_pct: int | None = None
) -> ProjectConfig:
    """REQ-007: guarda el filtro y el umbral de costo de la lectura de imagenes de PDF/Word. Un campo en None conserva el valor
    actual. ValueError con mensaje para personas si algo no es valido."""
    cfg = _load()
    min_px = cfg.image_min_px if min_px is None else min_px
    ask_count = cfg.image_ask_count if ask_count is None else ask_count
    ask_pct = cfg.image_ask_pct if ask_pct is None else ask_pct
    if min_px < 1:
        raise ValueError("El tamaño mínimo de una imagen tiene que ser de al menos 1 px.")
    if ask_count < 1:
        raise ValueError("La cantidad de imágenes a partir de la cual se pregunta tiene que ser de al menos 1.")
    if not 1 <= ask_pct <= 100:
        raise ValueError("El porcentaje de la ventana de contexto tiene que estar entre 1 y 100.")
    cfg.image_min_px, cfg.image_ask_count, cfg.image_ask_pct = min_px, ask_count, ask_pct
    _save(cfg)
    return cfg


def set_providers(
    *, models: dict[str, list[str]] | None = None, consent: dict[str, bool] | None = None
) -> ProjectConfig:
    """spec varios-proveedores REQ-003/006: modelos visibles y consentimiento
    por proveedor (solo se tocan los proveedores que vienen en el pedido)."""
    cfg = _load()
    if models is not None:
        for provider_id, ids in models.items():
            cfg.provider_models[provider_id] = [str(i) for i in ids]
    if consent is not None:
        for provider_id, ok in consent.items():
            cfg.provider_consent[provider_id] = bool(ok)
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
    truncated = False

    def walk(d: Path, prefix: str = "") -> None:
        nonlocal count, truncated
        try:
            # BL-020 (ADR-008, E2): antes `sorted(d.iterdir(), key=lambda c: (c.is_file(), ...))` hacia UN stat por cada
            # archivo de la carpeta aunque solo se muestren 400 (20.000 archivos: 1091 ms contra 19 ms). `os.scandir` entrega
            # el tipo (carpeta/archivo) junto con el listado, sin consultas extra. Mismo orden: carpetas primero, luego por
            # nombre sin distinguir mayusculas.
            with os.scandir(d) as it:
                children = sorted(it, key=lambda c: (c.is_file(), c.name.lower()))
        except PermissionError:
            return
        for child in children:
            if _should_ignore(child.name):
                continue
            if count >= MAX_TREE_ENTRIES:
                # BL-020 (ADR-008, E3): solo se marca; el aviso se escribe UNA vez al final. Antes cada nivel de la recursion
                # agregaba el suyo al volver (con 6 niveles salian 2, uno indentado).
                # E5: el filtro de ocultos/ignorados va ANTES del tope. Antes `walk` retornaba al entrar con el contador lleno
                # sin mirar la carpeta: con exactamente MAX carpetas, el hijo de la ultima se omitia sin ningun aviso. Ahora
                # se avisa si queda algo VISIBLE sin mostrar, y no hay falso aviso si solo quedaban ocultos/ignorados.
                truncated = True
                return
            if child.is_dir():
                lines.append(f"{prefix}{child.name}/")
                count += 1
                walk(Path(child.path), prefix + "  ")
            else:
                lines.append(f"{prefix}{child.name}")
                count += 1

    walk(root)
    if truncated:
        lines.append(f"... (arbol truncado, mas de {MAX_TREE_ENTRIES} entradas)")

    # BL-020 (ADR-008, E4): en Windows el FS no distingue mayusculas, asi que `README.md` y `readme.md` "existian" los dos para
    # un solo archivo y el README se leia (y se mandaba al modelo) DOS veces en cada turno. Se toma el nombre REAL del disco,
    # se deduplica sin distinguir mayusculas y se respeta el orden de KEY_DOC_NAMES.
    try:
        real_names = {n.lower(): n for n in os.listdir(root)}
    except OSError:
        real_names = {}
    key_docs: dict[str, str] = {}
    seen: set[str] = set()
    for doc_name in KEY_DOC_NAMES:
        real = real_names.get(doc_name.lower())
        if real is None or real.lower() in seen:
            continue
        seen.add(real.lower())
        doc_path = root / real
        if doc_path.is_file():
            try:
                text = doc_path.read_text(encoding="utf-8", errors="replace")
                key_docs[real] = text[:MAX_KEY_DOC_CHARS]
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
