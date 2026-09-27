"""
Gestion de la carpeta de "vista previa en vivo" por sesion
(browser/{session_id}/), donde Vision puede escribir un mini-proyecto
(HTML/CSS/JS, dashboards generados con JS, etc.) que el usuario visualiza
en el panel de navegador integrado del frontend, con auto-refresh cuando
detecta cambios.

Ciclo de vida: la carpeta se crea la primera vez que se pide su ruta
(get_or_create_browser_dir), y se borra SOLO cuando el usuario elimina la
sesion o libera cache (delete_browser_dir) -- nunca por si sola. Es
contenido temporal por diseno: no tiene sentido persistirlo mas alla de la
conversacion que lo genero.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from app.config import get_settings

# Extensiones servidas como archivo est\u00e1tico real (sin esto, un intento de
# leer ../../../etc/passwd u otra ruta fuera de la carpeta de la sesion no
# tendria de todos modos permiso: ver _safe_join).
_INDEX_CANDIDATES = ("index.html", "dashboard.html", "index.htm")


def get_browser_dir(session_id: str) -> Path:
    """Ruta de la carpeta de vista previa de una sesion (sin crearla)."""
    return get_settings().browser_root_path / session_id


def get_or_create_browser_dir(session_id: str) -> Path:
    """Crea (si no existe) y devuelve la carpeta de vista previa de una
    sesion. Esta es la ruta que se le informa al agente en el system
    prompt para que sepa donde escribir con write/edit/bash cuando el
    usuario pide un dashboard/proyecto visualizable en vivo."""
    d = get_browser_dir(session_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def delete_browser_dir(session_id: str) -> bool:
    """Borra por completo la carpeta de vista previa de una sesion (al
    eliminar la sesion o liberar cache). Devuelve True si existia algo
    para borrar."""
    d = get_browser_dir(session_id)
    if not d.exists():
        return False
    shutil.rmtree(d, ignore_errors=True)
    return True


def _safe_join(session_id: str, relative_path: str) -> Path | None:
    """Resuelve `relative_path` dentro de la carpeta de la sesion,
    rechazando cualquier intento de escapar de ella (path traversal via
    '..' o rutas absolutas). Devuelve None si el resultado cae fuera."""
    base = get_browser_dir(session_id).resolve()
    candidate = (base / relative_path.lstrip("/\\")).resolve()
    try:
        candidate.relative_to(base)
    except ValueError:
        return None
    return candidate


def read_file(session_id: str, relative_path: str) -> tuple[bytes, str] | None:
    """Lee un archivo dentro de la carpeta de la sesion. Devuelve
    (contenido, content_type) o None si no existe / la ruta es invalida."""
    path = _safe_join(session_id, relative_path)
    if path is None or not path.exists() or not path.is_file():
        return None
    return path.read_bytes(), _guess_content_type(path)


def _guess_content_type(path: Path) -> str:
    ext = path.suffix.lower()
    return {
        ".html": "text/html; charset=utf-8",
        ".htm": "text/html; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".js": "application/javascript; charset=utf-8",
        ".mjs": "application/javascript; charset=utf-8",
        ".json": "application/json; charset=utf-8",
        ".svg": "image/svg+xml",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".ico": "image/x-icon",
        ".woff": "font/woff",
        ".woff2": "font/woff2",
        ".map": "application/json; charset=utf-8",
        ".txt": "text/plain; charset=utf-8",
    }.get(ext, "application/octet-stream")


def find_index_file(session_id: str) -> str | None:
    """Busca un archivo 'de entrada' razonable (index.html, dashboard.html)
    en la raiz de la carpeta de la sesion, para servir por defecto cuando
    el iframe pide '/'."""
    base = get_browser_dir(session_id)
    if not base.exists():
        return None
    for name in _INDEX_CANDIDATES:
        if (base / name).exists():
            return name
    # Fallback: cualquier .html en la raiz, el primero alfabeticamente.
    htmls = sorted(p.name for p in base.glob("*.html"))
    return htmls[0] if htmls else None


def get_fingerprint(session_id: str) -> str:
    """Huella liviana del estado actual de la carpeta (cantidad de
    archivos + suma de mtimes+tamanos), usada por el frontend para saber
    si algo cambio y disparar un auto-refresh del iframe SIN tener que
    comparar contenido byte a byte ni mantener un watcher persistente en
    el backend (polling simple y barato)."""
    base = get_browser_dir(session_id)
    if not base.exists():
        return "empty"
    total = 0.0
    count = 0
    for p in base.rglob("*"):
        if p.is_file():
            stat = p.stat()
            total += stat.st_mtime + stat.st_size
            count += 1
    return f"{count}:{total:.3f}"
