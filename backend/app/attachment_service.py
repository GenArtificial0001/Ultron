"""
Servicio de adjuntos de archivo (spec adjuntar-archivos-chat): la logica de los
endpoints REST, separada de FastAPI para poder testearla y para no seguir
engordando `main.py` (que ya tiene miles de lineas).

`main.py` solo expone rutas finas que llaman a estas funciones y traducen
`AttachmentError` a `HTTPException`. Todo lo que toca disco o SQLite pasa por
aca, y toda ruta que viene del cliente se valida con `resolve()` contra
`<proyecto>/attachment/` (o una ruta registrada para leer en su lugar).

Decisiones de diseno:
- Subida en STREAMING del cuerpo crudo (sin multipart): `python-multipart` no
  esta en `requirements.txt` y agregarlo rompia el arranque en prod (REQ-002 b,
  REQ-011). Se escribe a `.<nombre>.part` en bloques, sin cargar el archivo en
  memoria, y se renombra al terminar (REQ-002 e).
- Todo el I/O de disco/SQLite corre fuera del event loop (`asyncio.to_thread`).
- Un mensaje de error es SIEMPRE para una persona no tecnica (regla de AGENTS.md):
  que paso y que hacer, sin jerga.
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
import subprocess
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from app import attachments as at
from app.attachment_readers import format_size
from app.persistence import db
from app.persistence import service as persistence

logger = logging.getLogger(__name__)

MB = 1024 * 1024
CHUNK_BYTES = 1024 * 1024  # bloques de escritura


class AttachmentError(Exception):
    """Error con un codigo HTTP y un mensaje listo para mostrar."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


# ---------------------------------------------------------------------------
# Umbrales (REQ-007)
# ---------------------------------------------------------------------------
def thresholds() -> dict[str, int]:
    """Umbrales efectivos en MB, desde ProjectConfig (editables en Configuracion)."""
    from app.project_context import get_project_config

    cfg = get_project_config()
    return {"warn_mb": cfg.attachment_warn_mb, "inplace_mb": cfg.attachment_inplace_mb}


def _project_root(session_id: str) -> Path:
    root = persistence.project_root_for_session(session_id)
    if root is None:
        raise AttachmentError(400, "Elegí una carpeta para la sesión antes de adjuntar archivos.")
    return root


def _metadata(real: Path, *, rel_path: str, inplace: bool, mime: str = "") -> dict[str, Any]:
    """Metadata de un adjunto tal como la ve el cliente (contrato tecnico)."""
    size = real.stat().st_size
    guessed = mime or mimetypes.guess_type(real.name)[0] or "application/octet-stream"
    return {
        "name": real.name,
        "path": rel_path,
        "size": size,
        "mime": guessed,
        "kind": at.kind_for(real.name),
        "inplace": inplace,
        "token_estimate": at.estimate_tokens_for_file(real, size),
    }


# ---------------------------------------------------------------------------
# REQ-002: subir
# ---------------------------------------------------------------------------
async def upload(
    session_id: str,
    raw_name: str,
    body: AsyncIterator[bytes],
    declared_length: int | None = None,
) -> dict[str, Any]:
    """Guarda el cuerpo crudo en `<proyecto>/attachment/<nombre saneado>`.

    Validaciones (REQ-002 f): carpeta de proyecto, tope de tamano (413 con la
    sugerencia de "Buscar…"), y que el destino, ya resuelto, quede dentro de la
    carpeta (incluso si `attachment/` es un junction). El tope se controla
    ANTES (Content-Length) y DURANTE (bytes reales) la escritura."""
    root = _project_root(session_id)
    limit = thresholds()["inplace_mb"] * MB
    if declared_length is not None and declared_length > limit:
        raise AttachmentError(413, _too_big_message(declared_length))

    folder = root / persistence.ATTACHMENT_SUBDIR
    name = at.sanitize_name(raw_name)
    final, part_file = await asyncio.to_thread(at.open_exclusive, folder, name)
    part = at.part_path_for(final)

    # El destino final ya reservado tiene que estar dentro del proyecto real
    # (un junction en `attachment/` podria apuntar afuera).
    if not at.is_within(root, final):
        part_file.close()
        _discard(final, part)
        raise AttachmentError(400, "No se puede guardar el archivo en esa ubicación.")

    written = 0
    try:
        async for chunk in body:
            if not chunk:
                continue
            written += len(chunk)
            if written > limit:
                raise AttachmentError(413, _too_big_message(written))
            await asyncio.to_thread(part_file.write, chunk)
        await asyncio.to_thread(part_file.close)
        # REQ-002 e: rename atomico; el agente nunca ve un archivo incompleto.
        await asyncio.to_thread(os.replace, part, final)
    except AttachmentError:
        part_file.close()
        _discard(final, part)
        raise
    except BaseException:
        # Corte de la subida (cliente cerro, cancelacion, error de disco).
        part_file.close()
        _discard(final, part)
        raise

    rel = final.relative_to(root.resolve() if root.is_absolute() else root).as_posix()
    meta = await asyncio.to_thread(_metadata, final, rel_path=rel, inplace=False)
    return meta


def _too_big_message(size: int) -> str:
    return (
        f"El archivo pesa {format_size(size)} y supera el límite para copiarlo a la sesión. "
        "Usá «Buscar…» para leerlo en su lugar, sin copiarlo."
    )


def _discard(final: Path, part: Path) -> None:
    """Borra el `.part` y el nombre reservado (vacio) tras una subida fallida."""
    for f in (part, final):
        try:
            f.unlink(missing_ok=True)
        except OSError:
            logger.warning("No se pudo borrar '%s' tras una subida fallida", f, exc_info=True)


# ---------------------------------------------------------------------------
# REQ-002 a: quitar un adjunto pendiente
# ---------------------------------------------------------------------------
def remove_pending(session_id: str, name: str) -> dict[str, Any]:
    """Borra un adjunto que ningun mensaje referencia (el usuario quito el chip
    antes de enviar). Si algun mensaje lo referencia: 409, se usa "Limpiar
    adjuntos" (que respeta lo que usa otra sesion)."""
    root = _project_root(session_id)
    real = at.resolve_attachment_path(root, f"{persistence.ATTACHMENT_SUBDIR}/{Path(name).name}")
    if real is None or not real.is_file():
        return {"removed": False}
    if _referenced_anywhere(root, real):
        raise AttachmentError(
            409, "Ese archivo ya está en un mensaje. Usá «Limpiar adjuntos» para borrarlo."
        )
    real.unlink(missing_ok=True)
    _forget_pending_inplace(session_id, str(real))
    return {"removed": True}


def _referenced_anywhere(root: Path, real: Path) -> bool:
    """Algun part 'file' (de CUALQUIER sesion de la misma carpeta) apunta a `real`."""
    db_path = db.db_path_for(str(root))
    for part in db.list_file_parts(db_path):
        p = part.get("path") or ""
        cand = Path(p) if Path(p).is_absolute() else root / p
        try:
            if cand.resolve() == real.resolve():
                return True
        except OSError:
            continue
    return False


# ---------------------------------------------------------------------------
# REQ-007: leer en su lugar
# ---------------------------------------------------------------------------
# Rutas registradas y todavia sin enviar en un mensaje, por sesion.
PENDING_INPLACE: dict[str, set[str]] = {}


def _forget_pending_inplace(session_id: str, path: str) -> None:
    PENDING_INPLACE.get(session_id, set()).discard(path)


def register_path(session_id: str, path: str) -> dict[str, Any]:
    """Registra una ruta absoluta para leerla en su lugar (sin copiarla).
    Se valida que exista y sea un archivo. Nunca se copia ni se borra."""
    _project_root(session_id)  # exige carpeta de proyecto, igual que subir
    if not path or "\x00" in path:
        raise AttachmentError(400, "Escribí la ruta completa del archivo (ej. C:\\Datos\\reporte.csv).")
    p = Path(path.strip().strip('"'))
    if not p.is_absolute():
        raise AttachmentError(400, "Escribí la ruta completa del archivo (ej. C:\\Datos\\reporte.csv).")
    real: Path | None = None
    try:
        candidate = p.resolve()
        if candidate.is_file():
            real = candidate
    except OSError:
        real = None
    if real is None:
        raise AttachmentError(400, "No encuentro ese archivo. Revisá la ruta y volvé a intentar.")
    PENDING_INPLACE.setdefault(session_id, set()).add(str(real))
    return _metadata(real, rel_path=str(real), inplace=True)


def registered_inplace(session_id: str) -> set[str]:
    """Rutas registradas para leer en su lugar: las pendientes (en memoria) mas
    las de mensajes ya enviados (parts 'file' con `inplace`)."""
    paths = set(PENDING_INPLACE.get(session_id, set()))
    db_path = persistence.get_active_db_path(session_id)
    if db_path is not None:
        for part in db.list_file_parts(db_path, session_id):
            if part.get("inplace") and part.get("path"):
                try:
                    paths.add(str(Path(part["path"]).resolve()))
                except OSError:
                    continue
    return paths


# ---------------------------------------------------------------------------
# REQ-004 d/e: abrir con la app de Windows
# ---------------------------------------------------------------------------
def open_attachment(session_id: str, path: str) -> dict[str, Any]:
    """Abre un adjunto REGISTRADO de la sesion con su app de Windows
    (`os.startfile`). Un ejecutable NUNCA se ejecuta: se muestra en el
    Explorador seleccionado (`explorer /select,`). Rechaza cualquier ruta que
    no sea un adjunto de esta sesion."""
    root = _project_root(session_id)
    real = at.resolve_attachment_path(root, path, registered_inplace(session_id))
    if real is None or not real.is_file():
        raise AttachmentError(404, "No encuentro ese archivo. Puede que se haya movido o borrado.")

    from app.tools import base as tool_base  # auditoria pasiva, igual que run_file

    try:
        if at.is_executable_name(real.name):
            subprocess.Popen(["explorer", f"/select,{real}"])  # noqa: S603,S607 -- ruta ya validada
            tool_base._audit("open_attachment", {"path": str(real), "mode": "explorer"})
            return {"opened": False, "revealed": True, "path": str(real)}
        os.startfile(real)  # type: ignore[attr-defined]  # noqa: S606 -- solo Windows; ruta validada
        tool_base._audit("open_attachment", {"path": str(real), "mode": "startfile"})
    except OSError as exc:
        logger.warning("No se pudo abrir '%s': %s", real, exc)
        raise AttachmentError(
            400, "Windows no pudo abrir el archivo. Probá abrirlo desde el Explorador."
        ) from exc
    return {"opened": True, "revealed": False, "path": str(real)}


# ---------------------------------------------------------------------------
# REQ-009: limpieza
# ---------------------------------------------------------------------------
def _sessions_in_folder(root: Path) -> set[str]:
    db_path = db.db_path_for(str(root))
    return {p["session_id"] for p in db.list_file_parts(db_path)}


def cleanup_preview(session_id: str) -> dict[str, Any]:
    """REQ-009 a: que se puede borrar. Filas de 4 tipos: archivos de esta
    sesion, imagenes base64 de esta sesion, huerfanos (sin referencia en
    ninguna sesion de la carpeta) y archivos usados por OTRA sesion (visibles
    pero no borrables desde aca)."""
    root = _project_root(session_id)
    db_path = db.db_path_for(str(root))
    all_parts = db.list_file_parts(db_path)
    att_dir = root / persistence.ATTACHMENT_SUBDIR

    def resolved(p: dict[str, Any]) -> Path | None:
        path = p.get("path") or ""
        cand = Path(path) if Path(path).is_absolute() else root / path
        try:
            return cand.resolve()
        except OSError:
            return None

    by_path_sessions: dict[Path, set[str]] = {}
    for p in all_parts:
        rp = resolved(p)
        if rp is not None and not p.get("deleted"):
            by_path_sessions.setdefault(rp, set()).add(p["session_id"])

    rows: list[dict[str, Any]] = []
    seen: set[Path] = set()
    for p in db.list_file_parts(db_path, session_id):
        if p.get("deleted"):
            continue
        rp = resolved(p)
        if rp is None:
            continue
        inplace = bool(p.get("inplace"))
        others = by_path_sessions.get(rp, set()) - {session_id}
        exists = rp.is_file()
        size = rp.stat().st_size if exists and not inplace else 0
        rows.append(
            {
                "id": f"file:{p['part_id']}",
                "kind": "file",
                "name": p.get("name", rp.name),
                "path": p.get("path", ""),
                "bytes": size,
                "inplace": inplace,  # solo se borra la referencia, nunca el archivo
                "used_by_other_session": bool(others),
                "deletable": not others,
                "missing": not exists,
            }
        )
        seen.add(rp)

    images = [i for i in db.list_image_parts(db_path, session_id) if not i["deleted"]]
    if images:
        rows.append(
            {
                "id": "images:all",
                "kind": "images",
                "name": f"{len(images)} imagen(es) pegadas en la conversación",
                "count": len(images),
                "bytes": sum(i["bytes"] for i in images),
                "deletable": True,
            }
        )

    referenced = set(by_path_sessions)
    if att_dir.is_dir():
        for f in sorted(att_dir.iterdir()):
            if not f.is_file() or f.name.startswith("."):
                continue
            rp = f.resolve()
            if rp in referenced or rp in seen:
                continue
            rows.append(
                {
                    "id": f"orphan:{f.name}",
                    "kind": "orphan",
                    "name": f.name,
                    "path": f"{persistence.ATTACHMENT_SUBDIR}/{f.name}",
                    "bytes": f.stat().st_size,
                    "deletable": True,
                }
            )
    total = sum(r["bytes"] for r in rows if r.get("deletable"))
    return {"rows": rows, "total_bytes": total, "total_label": format_size(total)}


def cleanup_apply(session_id: str, delete_ids: list[str]) -> dict[str, Any]:
    """REQ-009 b/c/d: borra del DISCO los archivos elegidos (solo dentro de
    `attachment/`, solo si ninguna otra sesion los usa), deja tombstones,
    reemplaza las imagenes por un marcador y compacta la base (VACUUM). Devuelve
    lo liberado REAL, midiendo la base antes y despues."""
    root = _project_root(session_id)
    db_path = db.db_path_for(str(root))
    preview = {r["id"]: r for r in cleanup_preview(session_id)["rows"]}
    att_dir = root / persistence.ATTACHMENT_SUBDIR
    now = time.time()
    size_before = db.database_size(db_path)
    freed_files = 0
    deleted: list[str] = []
    kept: list[str] = []

    for rid in delete_ids:
        row = preview.get(rid)
        if row is None or not row.get("deletable"):
            kept.append(rid)
            continue
        if row["kind"] == "file":
            part_id = rid.split(":", 1)[1]
            if not row["inplace"] and not row.get("missing"):
                target = at.resolve_attachment_path(root, row["path"])
                # Solo dentro de attachment/ (chequeado con resolve()); un leido en su
                # lugar nunca se borra del disco, solo su referencia.
                if target is not None and at.is_within(att_dir, target):
                    try:
                        freed_files += target.stat().st_size
                        target.unlink()
                    except OSError:
                        logger.warning("No se pudo borrar '%s'", target, exc_info=True)
                        kept.append(rid)
                        continue
            db.tombstone_file_part(db_path, part_id, now)
            deleted.append(rid)
        elif row["kind"] == "images":
            for img in db.list_image_parts(db_path, session_id):
                if not img["deleted"]:
                    db.tombstone_image_part(db_path, img["part_id"], now)
            deleted.append(rid)
        elif row["kind"] == "orphan":
            target = at.resolve_attachment_path(root, row["path"])
            if target is not None and at.is_within(att_dir, target):
                try:
                    freed_files += target.stat().st_size
                    target.unlink()
                    deleted.append(rid)
                except OSError:
                    logger.warning("No se pudo borrar '%s'", target, exc_info=True)
                    kept.append(rid)

    db.compact_database(db_path)
    freed_db = max(0, size_before - db.database_size(db_path))
    return {
        "freed_bytes": freed_files + freed_db,
        "freed_files_bytes": freed_files,
        "freed_db_bytes": freed_db,
        "freed_label": format_size(freed_files + freed_db),
        "deleted": deleted,
        "kept": kept,
    }


def has_attachments(session_id: str) -> bool:
    """REQ-009 g: la sesion tiene algo para limpiar (archivos vigentes o imagenes)."""
    root = persistence.project_root_for_session(session_id)
    if root is None:
        return False
    db_path = db.db_path_for(str(root))
    if any(not p.get("deleted") for p in db.list_file_parts(db_path, session_id)):
        return True
    return any(not i["deleted"] for i in db.list_image_parts(db_path, session_id))
