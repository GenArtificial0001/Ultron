"""
Adjuntos de archivo del chat (spec adjuntar-archivos-chat): reglas PURAS.

Aca vive todo lo que no toca FastAPI ni SQLite, para poder testearlo aislado:
saneo del nombre (REQ-002 c), nombre unico exclusivo (REQ-002 d), tipo/icono
del archivo (REQ-004 b), aviso de rutas para el agente (REQ-003), decision de
que se puede abrir (REQ-004 e), estimacion de tokens (REQ-008 c) y umbrales
(REQ-007). Los endpoints (`main.py`) y la persistencia (`persistence/db.py`)
solo orquestan estas funciones.

Seguridad: ninguna funcion de este modulo confia en una ruta del cliente. El
llamador siempre resuelve con `resolve()` y verifica que quede DENTRO de
`<proyecto>/attachment/` (o sea una ruta registrada para leer en su lugar).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from app.attachment_readers import format_size

# REQ-002 c: caracteres invalidos en Windows + control.
_INVALID_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_NAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}
MAX_NAME_LEN = 150
MAX_FILES_PER_MESSAGE = 10  # REQ-001 g (tope duro; lo aplica tambien el backend)

# REQ-004 e: nunca se ejecutan.
EXECUTABLE_EXTS = {
    ".exe",
    ".bat",
    ".cmd",
    ".ps1",
    ".vbs",
    ".js",
    ".msi",
    ".com",
    ".scr",
    ".lnk",
    ".hta",
    ".wsf",
    ".jar",
}
# REQ-004 f: el chip avisa "puede contener secretos".
SECRET_EXTS = {".env", ".pem", ".key"}

_KIND_BY_EXT: dict[str, str] = {}
for _kind, _exts in {
    "excel": (".xlsx", ".xlsm", ".xls", ".xlsb"),
    "csv": (".csv", ".tsv", ".parquet"),
    "pdf": (".pdf",),
    "word": (".docx", ".doc"),
    "text": (
        ".txt",
        ".md",
        ".log",
        ".json",
        ".yaml",
        ".yml",
        ".xml",
        ".html",
        ".htm",
        ".py",
        ".js",
        ".ts",
        ".sql",
        ".css",
        ".sass",
        ".scss",
        ".less",
        ".ini",
        ".cfg",
        ".env",
    ),
    "archive": (".zip", ".7z", ".rar", ".gz", ".tar", ".tgz"),
}.items():
    for _e in _exts:
        _KIND_BY_EXT[_e] = _kind

KIND_LABELS = {
    "excel": "Excel",
    "csv": "CSV",
    "pdf": "PDF",
    "word": "Word",
    "text": "Texto",
    "archive": "Comprimido",
    "file": "Archivo",
}


def kind_for(name: str) -> str:
    """Familia del archivo para el icono del chip (REQ-004 b)."""
    return _KIND_BY_EXT.get(Path(name).suffix.lower(), "file")


# ---------------------------------------------------------------------------
# REQ-002 c: nombre saneado
# ---------------------------------------------------------------------------
def sanitize_name(raw: str) -> str:
    """Solo el nombre (sin ruta), sin caracteres invalidos de Windows, sin
    puntos/espacios finales, nombres reservados con `_` delante y hasta 150
    caracteres conservando la extension. Nunca devuelve vacio."""
    name = Path(raw.replace("\\", "/")).name  # descarta cualquier ruta
    name = _INVALID_CHARS_RE.sub("_", name).strip()
    name = name.rstrip(". ")
    if not name:
        name = "archivo"
    stem, dot, _ext = name.rpartition(".")
    if not dot:
        stem = name
    if stem.lower() in _RESERVED_NAMES:
        name = "_" + name
    if len(name) > MAX_NAME_LEN:
        suffix = Path(name).suffix[:20]
        name = name[: MAX_NAME_LEN - len(suffix)].rstrip(". ") + suffix
    return name


def open_exclusive(folder: Path, name: str) -> tuple[Path, Any]:
    """REQ-002 d: crea `name`, o `name (1)`, `name (2)`... con creacion
    EXCLUSIVA (`open(..., "xb")`): dos subidas simultaneas con el mismo nombre
    nunca se pisan. Devuelve (ruta_final, archivo_abierto_en_.part).

    Se reserva el nombre FINAL con un archivo vacio exclusivo y se escribe a
    `.<nombre>.part`; el llamador renombra el `.part` sobre el final al
    terminar (REQ-002 e: el agente nunca ve un archivo incompleto)."""
    folder.mkdir(parents=True, exist_ok=True)
    path = Path(name)
    stem, suffix = path.stem, path.suffix
    n = 0
    while True:
        candidate = folder / (name if n == 0 else f"{stem} ({n}){suffix}")
        try:
            with open(candidate, "xb"):
                pass  # reserva el nombre final
        except FileExistsError:
            n += 1
            continue
        part = folder / f".{candidate.name}.part"
        return candidate, open(part, "wb")


def part_path_for(final: Path) -> Path:
    return final.parent / f".{final.name}.part"


# ---------------------------------------------------------------------------
# REQ-004 d/e: abrir
# ---------------------------------------------------------------------------
def is_executable_name(name: str) -> bool:
    return Path(name).suffix.lower() in EXECUTABLE_EXTS


def is_secret_name(name: str) -> bool:
    return Path(name).suffix.lower() in SECRET_EXTS or Path(name).name.lower() == ".env"


# ---------------------------------------------------------------------------
# REQ-008 c: estimacion de tokens
# ---------------------------------------------------------------------------
CHARS_PER_TOKEN = 4  # heuristica estandar para texto en español/inglés


def estimate_tokens_from_text(text: str) -> int:
    return max(1, len(text) // CHARS_PER_TOKEN) if text else 0


def estimate_tokens_for_file(path: Path, size: int | None = None) -> int:
    """Tokens aproximados de leer `path` entero. Sobre una MUESTRA extrapolada
    por tamaño, para no parsear un archivo de cientos de MB solo para estimar.
    Binarios y comprimidos: 0 (no se leen como texto)."""
    from app import attachment_readers as readers

    if size is None:
        try:
            size = path.stat().st_size
        except OSError:
            return 0
    kind = kind_for(path.name)
    if (
        kind == "archive"
        or readers.looks_binary(path)
        and kind not in ("excel", "pdf", "word")
    ):
        return 0
    if kind in ("excel", "pdf", "word"):
        # Formatos comprimidos/binarios: el texto ocupa bastante menos que el archivo.
        return max(1, int(size * 0.25) // CHARS_PER_TOKEN)
    sample_bytes = 256 * 1024
    try:
        with path.open("rb") as f:
            head = f.read(sample_bytes)
    except OSError:
        return 0
    if not head:
        return 0
    sample_tokens = estimate_tokens_from_text(head.decode("utf-8", errors="ignore"))
    if size <= sample_bytes:
        return sample_tokens
    return int(sample_tokens * (size / len(head)))


def format_tokens(n: int) -> str:
    """'≈ 35 k tokens' / '≈ 850 tokens'."""
    if n >= 1000:
        return f"≈ {round(n / 1000):d} k tokens"
    return f"≈ {n} tokens"


# ---------------------------------------------------------------------------
# REQ-003: aviso de rutas para el agente
# ---------------------------------------------------------------------------
NOTICE_HEADER = "[Archivos adjuntos en la carpeta de la sesión — leelos con la tool `read` usando estas rutas]"
BIG_TOKENS = 100_000  # REQ-008 c


def describe_attachment(att: dict[str, Any]) -> str:
    """Una linea del aviso: `- attachment/reporte.pdf (PDF, 2,3 MB, 14 páginas)`."""
    name = att.get("name", "")
    path = att.get("path") or f"attachment/{name}"
    kind = att.get("kind") or kind_for(name)
    bits = [KIND_LABELS.get(kind, "Archivo")]
    size = att.get("size")
    if isinstance(size, int) and size >= 0:
        bits.append(format_size(size))
    if att.get("pages"):
        bits.append(f"{att['pages']} páginas")
    if att.get("inplace"):
        bits.append("se lee en su lugar")
    if att.get("deleted"):
        bits.append("eliminado")
    line = f"- {path} ({', '.join(bits)})"
    tokens = att.get("token_estimate")
    if isinstance(tokens, int) and tokens > BIG_TOKENS:
        line += f" (≈ {tokens} tokens: leelo por partes)"
    return line


def build_notice(attachments: list[dict[str, Any]]) -> str:
    """El bloque completo, o '' si no hay adjuntos vigentes. Los eliminados no
    se le ofrecen al agente como algo para leer."""
    live = [a for a in attachments if not a.get("deleted")]
    if not live:
        return ""
    return "\n".join([NOTICE_HEADER] + [describe_attachment(a) for a in live])


def with_notice(text: str, attachments: list[dict[str, Any]]) -> str:
    """Texto del turno para el LLM: el texto del usuario + el aviso. El texto
    PERSISTIDO/visible sigue limpio (REQ-003 a)."""
    notice = build_notice(attachments)
    if not notice:
        return text
    return f"{text}\n\n{notice}" if text.strip() else notice


def autoname_from(attachments: list[dict[str, Any]]) -> str:
    """REQ-003 d: sin texto, el autonombrado usa el nombre del primer archivo."""
    for a in attachments:
        if a.get("name"):
            return str(a["name"])
    return ""


# ---------------------------------------------------------------------------
# Validacion de rutas (REQ-003 b / REQ-002 f / REQ-004 d / REQ-009 b)
# ---------------------------------------------------------------------------
def is_within(base: Path, candidate: Path) -> bool:
    """`candidate` (ya con resolve) queda dentro de `base`. Cubre junctions y
    symlinks: se compara la ruta REAL."""
    try:
        candidate.resolve().relative_to(base.resolve())
        return True
    except (ValueError, OSError):
        return False


def resolve_attachment_path(
    project_root: Path, path: str, registered_inplace: set[str] | None = None
) -> Path | None:
    """Ruta absoluta y real de un adjunto de la sesion, o None si NO es valida:
    tiene que estar dentro de `<proyecto>/attachment/`, o ser una ruta
    registrada para leer en su lugar. Una ruta relativa se interpreta desde la
    raiz del proyecto (`attachment/x.pdf`)."""
    from app.persistence.service import ATTACHMENT_SUBDIR

    if not path or "\x00" in path:
        return None
    p = Path(path)
    if not p.is_absolute():
        p = project_root / p
    try:
        real = p.resolve()
    except OSError:
        return None
    if is_within(project_root / ATTACHMENT_SUBDIR, real):
        return real
    if registered_inplace and str(real) in registered_inplace:
        return real
    return None


def is_onedrive_path(path: Path) -> bool:
    return "onedrive" in str(path).lower()

