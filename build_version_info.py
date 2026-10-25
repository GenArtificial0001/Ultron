"""Genera el bloque de version del `Vision.exe` para PyInstaller (`--version-file`).

Por que existe (spec exe-metadatos-version, caso real 2026-10-22/23): el antivirus de una companera marco un `Vision v0.35.x.exe`. Medido con `pefile`,
el `.exe` tenia icono pero NO bloque de version (sin empresa, producto, descripcion ni version): justo el perfil que los motores de aprendizaje automatico
desconfian mas en un ejecutable empaquetado que lanza `pip`/`python`. Esto agrega esos datos; NO firma el `.exe` (eso necesita un certificado de la empresa) y
NO garantiza que ningun antivirus deje de marcarlo.

La version sale de `backend/app/version.py` (una sola fuente de verdad), asi que nadie la edita a mano y nunca queda desfasada del codigo.

Uso (lo llama `build_exe.bat` ANTES de PyInstaller):
    python build_version_info.py <archivo-de-salida> [--version X.Y.Z]

Sale con codigo distinto de 0 y NO escribe nada si la version es invalida: es mejor que el build corte a publicar un `.exe` sin metadatos por un error callado.
Solo stdlib (corre en el venv de build, que tiene unicamente PyInstaller)."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VERSION_FILE = ROOT / "backend" / "app" / "version.py"

# Solo MAJOR.MINOR.PATCH numerico y sin nada alrededor: `" 1.2.3"`, `"v1.2.3"`, `"1.2"` o `"1.2.3.4"` son errores, no se "arreglan" en silencio.
_VERSION_RE = re.compile(r"\d+\.\d+\.\d+\Z")
_DECLARED_RE = re.compile(r'^__version__\s*=\s*"([^"]*)"', re.MULTILINE)

# Textos honestos: no se inventa un editor ni un copyright que no existan.
COMPANY = "la empresa - Planta Norte (uso interno)"
PRODUCT = "ULTRON"
DESCRIPTION = "ULTRON - asistente conversacional"
INTERNAL_NAME = "ULTRON"
ORIGINAL_FILENAME = "Ultron.exe"
COPYRIGHT = "Uso interno"

_TEMPLATE = """# UTF-8
# Generado por build_version_info.py desde backend/app/version.py -- NO editar a mano (spec exe-metadatos-version).
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={numeric},
    prodvers={numeric},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo(
      [
        StringTable(
          '040904B0',
          [
            StringStruct('CompanyName', '{company}'),
            StringStruct('FileDescription', '{description}'),
            StringStruct('FileVersion', '{dotted}'),
            StringStruct('InternalName', '{internal}'),
            StringStruct('LegalCopyright', '{copyright}'),
            StringStruct('OriginalFilename', '{original}'),
            StringStruct('ProductName', '{product}'),
            StringStruct('ProductVersion', '{dotted}')
          ]
        )
      ]
    ),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""


def read_project_version(path: Path = VERSION_FILE) -> str:
    """La `__version__` declarada en `backend/app/version.py` (lanza ValueError si no se encuentra)."""
    match = _DECLARED_RE.search(path.read_text(encoding="utf-8"))
    if not match:
        raise ValueError(f"No se encontro __version__ en {path}")
    return match.group(1)


def render(version: str) -> str:
    """El texto del bloque de version para `version` (X.Y.Z). Lanza ValueError si la version no es valida."""
    if not isinstance(version, str) or not _VERSION_RE.match(version):
        raise ValueError(
            f"La version debe ser MAJOR.MINOR.PATCH numerica (ej. 0.36.0); se recibio {version!r}"
        )
    major, minor, patch = (int(part) for part in version.split("."))
    return _TEMPLATE.format(
        numeric=f"({major}, {minor}, {patch}, 0)",
        dotted=f"{major}.{minor}.{patch}.0",
        company=COMPANY,
        description=DESCRIPTION,
        internal=INTERNAL_NAME,
        copyright=COPYRIGHT,
        original=ORIGINAL_FILENAME,
        product=PRODUCT,
    )


def write_version_file(version: str | None, out_path: Path | str) -> Path:
    """Escribe el bloque en `out_path`. `version=None` usa la del proyecto. No deja un archivo a medias: valida ANTES de escribir."""
    text = render(version if version is not None else read_project_version())
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Genera el bloque de version del Ultron.exe para PyInstaller."
    )
    parser.add_argument(
        "output", help="archivo de salida (se pasa a PyInstaller con --version-file)"
    )
    parser.add_argument(
        "--version",
        default=None,
        help="X.Y.Z; por defecto la de backend/app/version.py",
    )
    args = parser.parse_args(argv)
    try:
        out = write_version_file(args.version, args.output)
    except (ValueError, OSError) as exc:
        print(f"ERROR (version del .exe): {exc}", file=sys.stderr)
        return 1
    print(f"Bloque de version escrito en {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
