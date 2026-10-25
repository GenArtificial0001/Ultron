"""El `.exe` lleva metadatos de version (spec exe-metadatos-version, REQ-001..006).

Caso real (2026-10-22/23): el antivirus de una companera marco un `Vision v0.35.x.exe`. Medido con `pefile` sobre el 0.36.0: tenia icono pero NO bloque de version
(sin empresa, producto, descripcion ni version). Aca se prueba el generador (`build_version_info.py`) sin compilar un `.exe`: el archivo se valida con el propio
parser de PyInstaller (el mismo que lo lee con `--version-file`) y `build_exe.bat` se lee como texto (ULTRON no tiene `sync_prod.py`)."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = ROOT / "build_version_info.py"

if _SCRIPT.exists():
    _spec = importlib.util.spec_from_file_location("build_version_info_under_test", _SCRIPT)
    gen = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(gen)
else:  # el test debe FALLAR (rojo) si falta el generador, no ser omitido
    gen = None

pytestmark = pytest.mark.skipif(False, reason="")


def _need() -> None:
    assert gen is not None, "falta build_version_info.py"


def _write(tmp_path: Path, version: str) -> str:
    _need()
    out = tmp_path / "version_info.txt"
    gen.write_version_file(version, out)
    return out.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------------------------------------------------
# REQ-001: la version
# ---------------------------------------------------------------------------------------------------------------------
def test_the_numeric_version_is_split_in_four_parts(tmp_path):
    text = _write(tmp_path, "0.36.0")
    assert "filevers=(0, 36, 0, 0)" in text
    assert "prodvers=(0, 36, 0, 0)" in text


def test_the_string_versions_have_four_parts(tmp_path):
    text = _write(tmp_path, "0.36.0")
    assert "StringStruct('FileVersion', '0.36.0.0')" in text
    assert "StringStruct('ProductVersion', '0.36.0.0')" in text


@pytest.mark.parametrize(
    "version, parts", [("1.2.3", "(1, 2, 3, 0)"), ("10.20.30", "(10, 20, 30, 0)"), ("0.0.1", "(0, 0, 1, 0)")]
)
def test_other_versions(tmp_path, version, parts):
    assert f"filevers={parts}" in _write(tmp_path, version)


def test_it_uses_the_real_project_version_by_default(tmp_path):
    _need()
    out = tmp_path / "v.txt"
    gen.write_version_file(None, out)
    real = (ROOT / "backend" / "app" / "version.py").read_text(encoding="utf-8")
    import re

    declared = re.search(r'__version__\s*=\s*"(\d+)\.(\d+)\.(\d+)"', real).groups()
    assert f"filevers=({declared[0]}, {declared[1]}, {declared[2]}, 0)" in out.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------------------------------------------------
# REQ-002: el texto honesto
# ---------------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "key, value",
    [
        ("CompanyName", "la empresa - Planta Norte (uso interno)"),
        ("ProductName", "Vision"),
        ("FileDescription", "Vision - asistente conversacional"),
        ("InternalName", "Vision"),
        ("OriginalFilename", "Vision.exe"),
        ("LegalCopyright", "Uso interno"),
    ],
)
def test_the_descriptive_strings(tmp_path, key, value):
    assert f"StringStruct('{key}', '{value}')" in _write(tmp_path, "0.36.0")


# ---------------------------------------------------------------------------------------------------------------------
# REQ-003: una version mal formada falla claro y no deja un archivo a medias
# ---------------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("bad", ["", "1.2", "1.2.x", "v1.2.3", "1.2.3.4", " 1.2.3", "1.2.-3", "a.b.c"])
def test_a_malformed_version_is_rejected_without_writing(tmp_path, bad):
    _need()
    out = tmp_path / "v.txt"
    with pytest.raises(ValueError, match="version"):
        gen.write_version_file(bad, out)
    assert not out.exists()


def test_the_command_line_exits_non_zero_with_a_clear_message(tmp_path):
    _need()
    out = tmp_path / "v.txt"
    proc = subprocess.run(
        [sys.executable, str(_SCRIPT), str(out), "--version", "no-es-version"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode != 0
    assert "version" in (proc.stderr + proc.stdout).lower()
    assert not out.exists()


def test_the_command_line_writes_the_file_and_exits_zero(tmp_path):
    _need()
    out = tmp_path / "v.txt"
    proc = subprocess.run([sys.executable, str(_SCRIPT), str(out)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert out.is_file() and "StringStruct('ProductName', 'Vision')" in out.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------------------------------------------------
# REQ-004: PyInstaller lo acepta y lo interpreta igual
# ---------------------------------------------------------------------------------------------------------------------
def test_pyinstaller_parses_the_generated_file(tmp_path):
    pytest.importorskip("PyInstaller")
    from PyInstaller.utils.win32 import versioninfo

    out = tmp_path / "v.txt"
    _need()
    gen.write_version_file("7.8.9", out)  # partes distintas y parche != 0: detecta una parte en la posicion equivocada
    info = versioninfo.load_version_info_from_text_file(str(out))
    strings = {k: v for table in info.kids[0].kids for k, v in ((s.name, s.val) for s in table.kids)}
    assert strings["ProductName"] == "Vision"
    assert strings["FileVersion"] == "7.8.9.0"
    assert strings["ProductVersion"] == "7.8.9.0"
    assert strings["OriginalFilename"] == "Vision.exe"
    # FixedFileInfo guarda la version como dos palabras de 32 bits: MS = (mayor << 16) | menor, LS = (parche << 16) | build.
    assert (info.ffi.fileVersionMS, info.ffi.fileVersionLS) == ((7 << 16) | 8, (9 << 16) | 0)
    assert (info.ffi.productVersionMS, info.ffi.productVersionLS) == ((7 << 16) | 8, (9 << 16) | 0)


# ---------------------------------------------------------------------------------------------------------------------
# REQ-005 / REQ-006: el build y el sync
# ---------------------------------------------------------------------------------------------------------------------
def _bat() -> str:
    return (ROOT / "build_exe.bat").read_text(encoding="utf-8", errors="replace")


def test_the_bat_generates_the_file_before_compiling_and_passes_it_to_pyinstaller():
    bat = _bat()
    assert "build_version_info.py" in bat
    assert "--version-file" in bat
    assert bat.index("build_version_info.py") < bat.index("-m PyInstaller")


def test_the_bat_stops_if_the_generator_fails():
    bat = _bat()
    start = bat.index("build_version_info.py")
    following = bat[start : bat.index("-m PyInstaller")]
    assert "errorlevel" in following and "exit /b 1" in following


