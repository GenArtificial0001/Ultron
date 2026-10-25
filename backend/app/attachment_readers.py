"""
Lectores de formatos para la tool `read` (spec adjuntar-archivos-chat).

Por que un modulo aparte: `tools/filesystem.py::_read_execute` ya era largo y
esta feature suma cuatro familias de formato (PDF, DOCX, binarios y la
deteccion de codificacion/separador de CSV y texto). Aca viven las funciones
PURAS o casi puras (sin sesion, sin estado): reciben una ruta y devuelven el
texto que "ve" el modelo. `read` solo decide a cual llamar.

Contrato de no regresion (spec, "Criterio de no regresion"): un archivo UTF-8
o un CSV UTF-8 con coma se lee EXACTAMENTE igual que antes. Lo nuevo solo
cambia los casos que hoy fallaban o daban mojibake (cp1252, `;`, PDF, DOCX,
binarios).

Tope de salida (REQ-005 f): los formatos NUEVOS (PDF, DOCX) devuelven como
maximo `MAX_NEW_FORMAT_CHARS` caracteres por llamada, con un aviso de como
pedir el resto (`pages` u `offset`/`limit`). Los formatos que ya existian
conservan su contrato.
"""

from __future__ import annotations

import csv
import logging
import re
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from app.document_images import DocImagesContext

logger = logging.getLogger(__name__)

# REQ-005 f: tope por llamada para PDF/DOCX.
MAX_NEW_FORMAT_CHARS = 60_000
# REQ-005 a: por defecto se leen las primeras 20 paginas de un PDF.
DEFAULT_PDF_PAGES = 20
# REQ-006 a: una pagina con menos de 20 caracteres no-espacio "no tiene texto".
MIN_PAGE_CHARS = 20
# REQ-005 e: cuantos bytes se miran para decidir si es binario.
BINARY_SNIFF_BYTES = 8192
# REQ-005 e: un .zip lista hasta 200 entradas.
MAX_ZIP_ENTRIES = 200

# Extensiones que se tratan como binario aunque no tengan un NUL en los
# primeros bytes (REQ-005 e: "por un byte NUL ... o por una extension conocida").
BINARY_EXTS = {
    ".zip",
    ".7z",
    ".rar",
    ".gz",
    ".tar",
    ".tgz",
    ".bz2",
    ".xz",
    ".exe",
    ".dll",
    ".msi",
    ".bin",
    ".iso",
    ".dmp",
    ".so",
    ".dylib",
    ".class",
    ".jar",
    ".pyc",
    ".pyd",
    ".o",
    ".obj",
    ".lib",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".bmp",
    ".tif",
    ".tiff",
    ".ico",
    ".webp",
    ".heic",
    ".mp3",
    ".mp4",
    ".wav",
    ".avi",
    ".mov",
    ".mkv",
    ".flac",
    ".ogg",
    ".sqlite",
    ".db",
    ".mdb",
    ".accdb",
    ".pst",
    ".ost",
}
# Fuera de alcance v1 (REQ-005 / spec "Inputs"): aviso explicito con la
# forma de convertirlos.
UNSUPPORTED_HINTS = {
    ".doc": "Word 97 (.doc): guardalo como .docx desde Word y adjuntalo de nuevo",
    ".ppt": "PowerPoint (.ppt): exportalo a PDF y adjuntalo de nuevo",
    ".pptx": "PowerPoint (.pptx): exportalo a PDF y adjuntalo de nuevo",
    ".msg": "correo de Outlook (.msg): guardalo como PDF o .txt y adjuntalo de nuevo",
    ".xlsb": "Excel binario (.xlsb): guardalo como .xlsx y adjuntalo de nuevo",
}

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def format_size(n: int) -> str:
    """Peso en formato es-AR ('2,3 MB'): coma decimal."""
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            text = f"{size:.0f}" if unit == "B" else f"{size:.1f}".replace(".", ",")
            return f"{text} {unit}"
        size /= 1024
    return f"{n} B"  # inalcanzable


# ---------------------------------------------------------------------------
# Codificacion y separador (QA-033 / REQ-005 c, d)
# ---------------------------------------------------------------------------
def read_text_auto(p: Path) -> str:
    """Lee un archivo de texto probando UTF-8 (con BOM tolerado) y despues
    cp1252. Un archivo UTF-8 valido se lee exactamente como antes; uno en ANSI
    (cp1252, el default de Excel/SAP en Windows es-AR) ya no sale con U+FFFD.
    Ultimo recurso: UTF-8 con reemplazo (no lanza)."""
    # Se usa `Path.read_text` (no `read_bytes().decode`) a proposito: es el
    # mismo punto de entrada que usaba `read` antes, asi un error de OS
    # (PermissionError, etc.) se propaga igual y `read` lo informa como
    # "Error leyendo archivo: ..." (contrato existente, test_read_reports_os_errors).
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return p.read_text(encoding=enc)
        except UnicodeDecodeError:
            continue
    return p.read_text(encoding="utf-8", errors="replace")


def detect_encoding(p: Path) -> str:
    """'utf-8' o 'cp1252' para pasarle a pandas."""
    raw = p.read_bytes()
    try:
        raw.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        return "cp1252"


def detect_separator(sample: str, default: str = ",") -> str:
    """Elige entre `,` `;` tab `|` mirando las primeras lineas: gana el que
    aparece la MISMA cantidad (>0) de veces en la mayoria de las lineas
    (dentro de comillas no cuenta, por eso se usa csv.reader). Un CSV UTF-8
    con coma da ',' (igual que hoy)."""
    lines = [ln for ln in sample.splitlines() if ln.strip()][:10]
    if not lines:
        return default
    best, best_score = default, 0
    for sep in (",", ";", "\t", "|"):
        counts: list[int] = []
        for ln in lines:
            try:
                row = next(csv.reader([ln], delimiter=sep))
            except (csv.Error, StopIteration):
                row = [ln]
            counts.append(len(row) - 1)
        if not counts or counts[0] <= 0:
            continue
        consistent = sum(1 for c in counts if c == counts[0])
        score = consistent * 100 + counts[0]
        # Empate: se prefiere el separador por defecto (no cambia el comportamiento de hoy).
        if score > best_score or (score == best_score and sep == default):
            best, best_score = sep, score
    return best


def csv_read_options(p: Path, suffix: str) -> dict[str, Any]:
    """Opciones de `pd.read_csv` para `p`: codificacion y separador detectados."""
    encoding = detect_encoding(p)
    with p.open("r", encoding=encoding, errors="replace", newline="") as f:
        sample = f.read(64 * 1024)
    default = "\t" if suffix == ".tsv" else ","
    sep = detect_separator(sample, default=default)
    if suffix == ".tsv":
        sep = "\t"  # un .tsv es tab por definicion
    return {"sep": sep, "encoding": encoding}


# ---------------------------------------------------------------------------
# Binarios (REQ-005 e)
# ---------------------------------------------------------------------------
def looks_binary(p: Path) -> bool:
    """Extension conocida de binario, o un byte NUL en los primeros 8 KiB."""
    if p.suffix.lower() in BINARY_EXTS:
        return True
    try:
        with p.open("rb") as f:
            head = f.read(BINARY_SNIFF_BYTES)
    except OSError:
        return False
    return b"\x00" in head


def binary_notice(p: Path) -> str:
    """`[binario] <nombre> (<tipo>, <tamano>): no se puede leer como texto`
    + sugerencia. Sin un solo caracter de control ni de reemplazo. Un .zip
    lista sus entradas (nombre y tamano, hasta 200)."""
    ext = p.suffix.lower().lstrip(".") or "sin extension"
    try:
        size = format_size(p.stat().st_size)
    except OSError:
        size = "tamano desconocido"
    lines = [f"[binario] {p.name} ({ext}, {size}): no se puede leer como texto."]
    if p.suffix.lower() == ".zip":
        try:
            with zipfile.ZipFile(p) as z:
                infos = z.infolist()
                lines.append(f"Contiene {len(infos)} entradas:")
                for info in infos[:MAX_ZIP_ENTRIES]:
                    lines.append(
                        f"- {_clean(info.filename)} ({format_size(info.file_size)})"
                    )
                if len(infos) > MAX_ZIP_ENTRIES:
                    lines.append(f"... y {len(infos) - MAX_ZIP_ENTRIES} más")
        except (zipfile.BadZipFile, OSError):
            lines.append("(el .zip no se pudo abrir: puede estar dañado)")
    lines.append(
        "Sugerencia: usá `bash` para procesarlo (descomprimirlo, convertirlo o abrirlo con una "
        "librería), o pedile al usuario una versión en texto."
    )
    return _clean("\n".join(lines))


def unsupported_notice(p: Path) -> str | None:
    hint = UNSUPPORTED_HINTS.get(p.suffix.lower())
    if hint is None:
        return None
    return f"[formato no soportado] {p.name}: {hint}."


def _clean(text: str) -> str:
    """Saca caracteres de control y de reemplazo de un texto para el modelo."""
    return _CONTROL_RE.sub("", text).replace("\ufffd", "?")


# ---------------------------------------------------------------------------
# Rangos de paginas ("3", "1-5", "2,4-6")
# ---------------------------------------------------------------------------
def parse_pages(spec: str | None, total: int) -> list[int]:
    """Devuelve los indices de pagina (desde 1) pedidos, acotados a `total`,
    sin repetir y en orden. Sin `spec`: las primeras `DEFAULT_PDF_PAGES`.
    Lanza ValueError con un mensaje para personas si el formato es invalido."""
    if spec is None or not str(spec).strip():
        return list(range(1, min(total, DEFAULT_PDF_PAGES) + 1))
    pages: list[int] = []
    for part in str(spec).replace(" ", "").split(","):
        if not part:
            continue
        if re.fullmatch(r"\d+", part):
            a = b = int(part)
        elif re.fullmatch(r"\d+-\d+", part):
            a, b = (int(x) for x in part.split("-"))
            if a > b:
                a, b = b, a
        else:
            raise ValueError(
                f'\'{part}\' no es un rango de páginas válido (ej. "3", "1-5", "2,4-6")'
            )
        for n in range(a, b + 1):
            if 1 <= n <= total and n not in pages:
                pages.append(n)
    pages.sort()
    return pages


# ---------------------------------------------------------------------------
# PDF (REQ-005 a, REQ-006)
# ---------------------------------------------------------------------------
def _truncate(text: str, hint: str) -> str:
    if len(text) <= MAX_NEW_FORMAT_CHARS:
        return text
    return text[:MAX_NEW_FORMAT_CHARS] + f"\n\n[resultado truncado: {hint}]"


def page_has_text(text: str) -> bool:
    """REQ-006 a: menos de 20 caracteres no-espacio => sin capa de texto."""
    return len("".join(text.split())) >= MIN_PAGE_CHARS


def read_pdf(
    p: Path,
    pages: str | None = None,
    offset: int = 1,
    limit: int = 2000,
    on_progress: Callable[[int], None] | None = None,
    transcribe_pages: Callable[[Path, list[int]], tuple[dict[int, str], str | None]]
    | None = None,
    images: DocImagesContext | None = None,
) -> str:
    """Texto de un PDF digital con marcas `--- Página k ---`.

    - `pages`: rango pedido (por defecto las primeras 20).
    - `offset`/`limit`: lineas dentro de lo que se devuelve.
    - PDF cifrado: aviso, sin excepcion.
    - Paginas sin texto (escaneadas): si viene `transcribe_pages`
      (REQ-006) se le delegan; si no, se avisa que no tienen capa de texto.
    - `on_progress(pct)`: 0-100, pagina k de N (REQ-008 b).
    - `images`: contexto de la lectura de imagenes incrustadas (spec leer-imagenes-pdf-word). None = como siempre."""
    try:
        from pypdf import PdfReader
        from pypdf.errors import PdfReadError
    except ImportError:
        return "Error: falta la librería `pypdf` para leer PDF."

    try:
        reader = PdfReader(str(p))
        if reader.is_encrypted:
            try:
                ok = reader.decrypt("")  # muchos PDFs "cifrados" abren con clave vacia
            except Exception:  # noqa: BLE001 -- cualquier falla de descifrado => protegido
                ok = 0
            if not ok:
                return f"PDF: {p.name} — protegido con contraseña: no se puede leer."
        total = len(reader.pages)
    except (PdfReadError, OSError, ValueError) as exc:
        return f"Error: no pude abrir '{p.name}' como PDF ({_clean(str(exc))[:200]})."

    try:
        wanted = parse_pages(pages, total)
    except ValueError as exc:
        return f"Error: {exc}."
    if not wanted:
        return f"PDF: {p.name} — {total} páginas. Ninguna de las páginas pedidas existe (el PDF tiene {total})."

    texts: dict[int, str] = {}
    for i, n in enumerate(wanted, start=1):
        try:
            texts[n] = reader.pages[n - 1].extract_text() or ""
        except Exception as exc:  # noqa: BLE001 -- una pagina rota no debe tirar todo el PDF
            logger.warning("No se pudo extraer la página %s de %s: %s", n, p.name, exc)
            texts[n] = ""
        if on_progress:
            on_progress(int(i * 100 / len(wanted)))

    scanned = [n for n in wanted if not page_has_text(texts[n])]
    note_lines: list[str] = []
    if scanned and transcribe_pages is not None:
        transcribed, notice = transcribe_pages(p, scanned)
        for n, t in transcribed.items():
            texts[n] = t
        if notice:
            note_lines.append(notice)
        scanned = [n for n in scanned if n not in transcribed]
    if scanned:
        note_lines.append(
            "Las páginas "
            + ", ".join(str(n) for n in scanned)
            + " no tienen texto extraíble (parecen escaneadas)."
        )

    # spec leer-imagenes-pdf-word: imagenes incrustadas de las paginas pedidas. Las paginas escaneadas se EXCLUYEN: ya se
    # transcribieron enteras (o se informo que no se pudo) y leer ademas su imagen seria pagar dos veces lo mismo.
    image_outcome = None
    image_by_page: dict[int, list[str]] = {}
    if images is not None:
        from app import document_images as di  # import diferido: evita ciclo

        scanned_pages = {n for n in wanted if not page_has_text(texts[n])}
        readable = [n for n in wanted if n not in scanned_pages]
        try:
            extracted = di.extract_pdf_images(p, readable, images.thresholds.min_px)
        except Exception as exc:  # noqa: BLE001 -- pypdfium2 ausente o PDF que no abre: el texto se entrega igual
            logger.warning("No se pudieron extraer las imagenes de %s: %s", p.name, exc)
            extracted = di.Extracted()
        image_outcome = di.plan(extracted, images, is_word=False)
        for image in extracted.images:
            marker = image_outcome.marker(image)
            if marker is not None:
                image_by_page.setdefault(image.page or 0, []).append(marker)
        note_lines.extend(image_outcome.tail)

    out = [f"PDF: {p.name} — {total} páginas"]
    if len(wanted) < total:
        out[0] += (
            f" (mostrando {len(wanted)}: {_compact_pages(wanted)}; usá `pages` para pedir otras)"
        )
    body: list[str] = []
    for n in wanted:
        body.append(f"--- Página {n} ---")
        body.extend(_clean(texts[n]).splitlines())
        for marker in image_by_page.get(n, []):
            body.extend(_clean(marker).splitlines())
    start = max(offset - 1, 0)
    chunk = body[start : start + limit]
    text = "\n".join(out + chunk + note_lines)
    return _truncate(text, "pedí `pages` u `offset`/`limit` para leer el resto")


def _compact_pages(nums: list[int]) -> str:
    """[1,2,3,5] -> '1-3,5'."""
    if not nums:
        return ""
    ranges: list[str] = []
    start = prev = nums[0]
    for n in nums[1:]:
        if n == prev + 1:
            prev = n
            continue
        ranges.append(str(start) if start == prev else f"{start}-{prev}")
        start = prev = n
    ranges.append(str(start) if start == prev else f"{start}-{prev}")
    return ",".join(ranges)


# ---------------------------------------------------------------------------
# DOCX (REQ-005 b)
# ---------------------------------------------------------------------------
def _md_table(rows: list[list[str]]) -> str:
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    norm = [
        [(c or "").replace("\n", " ").replace("|", "\\|").strip() for c in r]
        + [""] * (width - len(r))
        for r in rows
    ]
    lines = [
        "| " + " | ".join(norm[0]) + " |",
        "| " + " | ".join("---" for _ in range(width)) + " |",
    ]
    lines += ["| " + " | ".join(r) + " |" for r in norm[1:]]
    return "\n".join(lines)


def read_docx(p: Path, offset: int = 1, limit: int = 2000, images: DocImagesContext | None = None) -> str:
    """Parrafos y tablas EN EL ORDEN DEL DOCUMENTO (python-docx los separa, asi
    que se recorre el cuerpo), con las tablas como tablas markdown."""
    try:
        import docx
        from docx.table import Table
        from docx.text.paragraph import Paragraph
    except ImportError:
        return "Error: falta la librería `python-docx` para leer Word."
    try:
        document = docx.Document(str(p))
    except Exception as exc:  # noqa: BLE001 -- archivo danado / no es un docx real
        return f"Error: no pude abrir '{p.name}' como Word ({_clean(str(exc))[:200]})."

    # spec leer-imagenes-pdf-word: imagenes del cuerpo, ubicadas en su bloque (incluidas las de las tablas).
    image_outcome = None
    extracted = None
    if images is not None:
        from app import document_images as di  # import diferido: evita ciclo

        try:
            extracted = di.extract_docx_images(p, document, images.thresholds.min_px)
        except Exception as exc:  # noqa: BLE001 -- un Word con medios raros: el texto se entrega igual
            logger.warning("No se pudieron extraer las imagenes de %s: %s", p.name, exc)
            extracted = di.Extracted()
        image_outcome = di.plan(extracted, images, is_word=True)

    blocks: list[str] = []
    for block_index, child in enumerate(document.element.body.iterchildren()):
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            text = Paragraph(child, document).text
            if text.strip():
                blocks.append(text)
        elif tag == "tbl":
            table = Table(child, document)
            rows = [[cell.text for cell in row.cells] for row in table.rows]
            md = _md_table(rows)
            if md:
                blocks.append(md)
        if image_outcome is not None and extracted is not None and block_index < len(extracted.placement):
            by_number = {image.number: image for image in extracted.images}
            for number in extracted.placement[block_index]:
                marker = image_outcome.marker(by_number[number])
                if marker is not None:
                    blocks.append(marker)
    lines = _clean("\n\n".join(blocks)).splitlines()
    start = max(offset - 1, 0)
    tail = image_outcome.tail if image_outcome is not None else []
    text = "\n".join([f"Word: {p.name}"] + lines[start : start + limit] + ([""] + tail if tail else []))
    return _truncate(text, "pedí `offset`/`limit` para leer el resto")
