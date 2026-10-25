"""Conversion de documentos de Office y exportacion a PDF (spec office-com-automatizacion REQ-004, REQ-012, REQ-010).

Por que existe: la gente de la planta tiene `.doc` y `.xls` viejos que `python-docx` y `openpyxl` no leen, y necesita pasarlos a `.docx`/`.xlsx` o a PDF. Word, Excel y PowerPoint lo hacen por COM; aca se
orquesta con las reglas del usuario:
- el ORIGINAL no se toca: se abre de SOLO LECTURA y la copia nueva se escribe en OTRA carpeta, asi que ni su contenido ni su fecha cambian;
- el archivo nuevo queda en `exports/` de la carpeta de proyecto de la SESION (la misma de los CSV de las consultas y de las descargas de Confluence); esa carpeta se crea BAJO DEMANDA al guardar, no por adelantado
  (decision del usuario del 2026-09-09, que vigila `test_persistence_service.py`);
- NUNCA se pisa un archivo de `exports/`: si el nombre esta usado se agrega un numero (`Informe (2).docx`);
- solo se convierte lo que tiene sentido (`.doc`/`.docx` con Word, `.xls`/`.xlsx` con Excel, `.ppt`/`.pptx` con PowerPoint, a su formato nuevo o a PDF): lo demas se rechaza ANTES de abrir Office;
- convertir un archivo NO exige que Office este abierto: se abre una instancia PROPIA e invisible y se cierra al terminar (a diferencia de leer o escribir celdas, que solo se engancha al Excel de la persona);
- plazo de 60 s (una conversion puede tardar mas que leer un rango), y si Office se cuelga el hilo es daemon y no impide cerrar Vision.

El backend es inyectable (`set_backend`) para que la suite use un Office falso y nunca abra el de nadie."""

from __future__ import annotations

import logging
import re
import threading
from pathlib import Path
from typing import Any, Protocol

from app.office import errors

logger = logging.getLogger(__name__)

CONVERT_TIMEOUT_SECONDS = 60.0
MAX_SOURCE_BYTES = 100 * 1024 * 1024  # un documento de mas de 100 MB no se convierte desde el chat

# extension del origen -> (aplicacion, formatos de destino posibles)
_SOURCES: dict[str, tuple[str, tuple[str, ...]]] = {
    ".doc": ("word", ("docx", "pdf")),
    ".docx": ("word", ("pdf",)),
    ".xls": ("excel", ("xlsx", "pdf")),
    ".xlsx": ("excel", ("pdf",)),
    ".ppt": ("powerpoint", ("pptx", "pdf")),
    ".pptx": ("powerpoint", ("pdf",)),
}
_APP_LABEL = {"word": "Word", "excel": "Excel", "powerpoint": "PowerPoint"}
_PROGID = {"word": "Word.Application", "excel": "Excel.Application", "powerpoint": "PowerPoint.Application"}
# Constantes de formato de Office: Word wdFormatXMLDocument=12 / wdFormatPDF=17; Excel xlOpenXMLWorkbook=51 / xlTypePDF=0; PowerPoint ppSaveAsOpenXMLPresentation=24 / ppSaveAsPDF=32.
_FORMAT_CODE = {
    ("word", "docx"): 12,
    ("word", "pdf"): 17,
    ("excel", "xlsx"): 51,
    ("excel", "pdf"): 0,
    ("powerpoint", "pptx"): 24,
    ("powerpoint", "pdf"): 32,
}
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class ConvertBackend(Protocol):
    """Lo que la conversion necesita de Office. La implementacion real habla COM; la de las pruebas escribe un archivo falso."""

    def convert(self, source: Path, target: Path, kind: str, fmt: str) -> None: ...


_backend: ConvertBackend | None = None


def set_backend(backend: ConvertBackend | None) -> None:
    """Reemplaza el backend (los tests inyectan un Office falso). `None` vuelve al backend real de COM."""
    global _backend
    _backend = backend


def _import_com() -> tuple[Any, Any]:
    """Importa pywin32 de forma DIFERIDA: una PC sin el paquete no rompe el arranque de Vision."""
    import pythoncom  # noqa: PLC0415 -- diferido a proposito
    import win32com.client  # noqa: PLC0415

    return pythoncom, win32com.client


class _ComConverter:
    """Backend real: abre una instancia PROPIA e invisible de la aplicacion, convierte y la cierra. No toca el Word o Excel que la persona tenga abierto."""

    def convert(self, source: Path, target: Path, kind: str, fmt: str) -> None:
        _, client = _import_com()
        code = _FORMAT_CODE[(kind, fmt)]
        app = client.DispatchEx(_PROGID[kind])  # DispatchEx = instancia NUEVA (no la que la persona esta usando)
        try:
            app.Visible = False
            app.DisplayAlerts = False  # sin cuadros de dialogo que dejarian la llamada colgada
            if kind == "word":
                doc = app.Documents.Open(str(source), ReadOnly=True, AddToRecentFiles=False, ConfirmConversions=False)
                try:
                    doc.SaveAs2(str(target), FileFormat=code)
                finally:
                    doc.Close(False)
            elif kind == "excel":
                book = app.Workbooks.Open(str(source), ReadOnly=True, UpdateLinks=0)
                try:
                    if fmt == "pdf":
                        book.ExportAsFixedFormat(code, str(target))
                    else:
                        book.SaveAs(str(target), FileFormat=code)
                finally:
                    book.Close(False)
            else:
                pres = app.Presentations.Open(str(source), ReadOnly=True, WithWindow=False)
                try:
                    pres.SaveAs(str(target), code)
                finally:
                    pres.Close()
        finally:
            try:
                app.Quit()
            except Exception:  # noqa: BLE001 -- si Quit falla no hay nada mas que hacer; lo importante es no dejar el error crudo
                logger.warning("Office: no se pudo cerrar la instancia propia de %s", kind, exc_info=True)


def _run_with_timeout(work, timeout: float):
    """Corre `work` en un hilo propio (con COM inicializado si es el backend real) y espera como maximo `timeout` s."""
    box: dict[str, Any] = {}

    def runner() -> None:
        pythoncom = None
        try:
            if _backend is None:
                try:
                    pythoncom, _ = _import_com()
                except ImportError as exc:
                    box["error"] = errors.OfficeUnavailable(str(exc))
                    return
                pythoncom.CoInitialize()
            try:
                work()
            finally:
                if pythoncom is not None:
                    pythoncom.CoUninitialize()
        except errors.OfficeError as exc:
            box["error"] = exc
        except BaseException as exc:  # noqa: BLE001 -- cualquier falla de COM se traduce; nada crudo llega a la persona
            box["error"] = (
                errors.from_com_error(exc, "Office")
                if type(exc).__name__ == "com_error"
                else errors.OfficeError(errors.describe(exc))
            )
            logger.warning("Office: fallo una conversion (%s)", errors.technical_detail(exc))

    thread = threading.Thread(target=runner, name="office-convert", daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        logger.warning("Office: una conversion supero %.0f s", timeout)
        raise errors.OfficeTimeout()
    if "error" in box:
        raise box["error"]


def _safe_stem(name: str) -> str:
    """Nombre base del archivo nuevo, a partir del del origen: sin caracteres que Windows no admite, sin espacios ni puntos al final, sin nombres reservados y con tope de largo."""
    stem = _BAD_CHARS.sub("_", Path(name).stem).strip(" .")[:100] or "documento"
    if stem.split(".")[0].upper() in _RESERVED:
        stem = f"{stem}_"
    return stem


def _free_target(directory: Path, stem: str, ext: str) -> Path:
    """Primer nombre libre de `directory`: `Informe.docx`, `Informe (2).docx`, ... NUNCA se pisa lo que ya existe."""
    candidate = directory / f"{stem}{ext}"
    n = 2
    while candidate.exists():
        candidate = directory / f"{stem} ({n}){ext}"
        n += 1
        if n > 999:
            raise errors.OfficeUserError(
                "Hay demasiados archivos con ese nombre en exports/. Borrá algunos o pedime otro nombre."
            )
    return candidate


def convert_file(
    source: str,
    fmt: str,
    *,
    project_root: Path | None,
    session_id: str | None = None,
    timeout: float = CONVERT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Convierte `source` al formato `fmt` y deja el resultado en `exports/` de la carpeta de proyecto. El original no se modifica."""
    text = (source or "").strip()
    wanted = (fmt or "").strip().lower().lstrip(".")
    if not text:
        raise errors.OfficeUserError(
            "Decime qué archivo querés convertir (la ruta completa, por ejemplo C:\\Documentos\\Informe.doc)."
        )
    if _BAD_CHARS.search(text.replace("\\", "").replace("/", "").replace(":", "", 1)):
        raise errors.OfficeUserError(
            "La ruta del archivo tiene caracteres que Windows no permite. Revisá que esté bien escrita y volvé a pedirlo."
        )
    path = Path(text)
    if not path.is_absolute() or ".." in path.parts or text.startswith("~"):
        raise errors.OfficeUserError(
            "Necesito la ruta completa del archivo (desde la unidad, por ejemplo C:\\Documentos\\Informe.doc), sin «..» ni «~»."
        )

    ext = path.suffix.lower()
    if ext not in _SOURCES:
        raise errors.OfficeUserError(
            f"No sé convertir archivos «{ext or 'sin extensión'}». Solo convierto Word (.doc, .docx), Excel (.xls, .xlsx) y PowerPoint (.ppt, .pptx)."
        )
    kind, allowed = _SOURCES[ext]
    if wanted not in allowed:
        options = ", ".join(allowed)
        raise errors.OfficeUserError(
            f"Un archivo {ext} se puede convertir a: {options}. «{wanted or 'vacío'}» no está entre las opciones."
        )

    if not path.exists():
        raise errors.OfficeUserError(f"No encuentro el archivo «{path.name}». Revisá la ruta y que no lo hayan movido.")
    if not path.is_file():
        raise errors.OfficeUserError(f"«{path.name}» no es un archivo sino una carpeta. Decime cuál es el archivo.")
    if path.stat().st_size > MAX_SOURCE_BYTES:
        raise errors.OfficeUserError(
            f"«{path.name}» es demasiado grande para convertirlo desde el chat (más de {MAX_SOURCE_BYTES // (1024 * 1024)} MB). Abrilo en {_APP_LABEL[kind]} y exportalo vos."
        )
    if project_root is None:
        raise errors.OfficeUserError(
            "Esta sesión todavía no tiene una carpeta de proyecto: no sé dónde guardar el archivo convertido. Asignale una carpeta a la sesión y volvé a pedirlo."
        )

    exports = Path(project_root) / "exports"
    exports.mkdir(parents=True, exist_ok=True)  # BAJO DEMANDA: recien aca, cuando ya se va a guardar algo
    target = _free_target(exports, _safe_stem(path.name), f".{wanted}")
    backend: ConvertBackend = _backend if _backend is not None else _ComConverter()
    try:
        _run_with_timeout(lambda: backend.convert(path, target, kind, wanted), timeout)
    except BaseException:
        # Una conversion fallida o cortada no deja un archivo a medias: lo que Office haya alcanzado a escribir se borra.
        try:
            target.unlink(missing_ok=True)
        except OSError:
            logger.warning("Office: no se pudo borrar un archivo parcial", exc_info=True)
        raise
    if not target.exists() or target.stat().st_size == 0:
        target.unlink(missing_ok=True)
        raise errors.OfficeUserError(
            f"{_APP_LABEL[kind]} no generó el archivo convertido. Probá de nuevo; si se repite, abrí «{path.name}» en {_APP_LABEL[kind]} y exportalo vos."
        )
    result: dict[str, Any] = {"source": path.name, "format": wanted, "path": str(target), "file": target.name}
    if session_id:
        result["download_url"] = f"/api/sessions/{session_id}/exports/{target.name}"
    return result
