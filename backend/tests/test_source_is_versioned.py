"""Guardia de repositorio: NINGUN archivo de codigo de Vision puede quedar ignorado por git.

Bug real (descubierto 2026-10-01): la regla `session/` del `.gitignore` ignoraba tambien el paquete de CODIGO
`backend/app/session/` (SessionState, compaction). Nunca llego al repo: en un clon limpio de cualquier version
publicada Vision no arrancaba (`ModuleNotFoundError: No module named 'app.session'`) y los tags de rollback eran
inutilizables. En la PC del desarrollador todo funcionaba porque la carpeta existia en disco, asi que ninguna prueba
lo detectaba. Este test lo detecta: si agregas una regla al `.gitignore` que se come codigo, falla aqui."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
# Carpetas donde TODO archivo de codigo debe versionarse (no hay nada generado dentro).
CODE_ROOTS = ("backend/app", "backend/tests", "frontend/js", "frontend/css", "frontend/index.html", "scripts")
CODE_SUFFIXES = (".py", ".js", ".css", ".html")


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=REPO, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60
    )


def _in_a_git_checkout() -> bool:
    return shutil.which("git") is not None and (REPO / ".git").exists() and _git("rev-parse").returncode == 0


pytestmark = pytest.mark.skipif(not _in_a_git_checkout(), reason="no es un checkout de git (ej. prod/ empaquetado)")


def ignored_code_files() -> list[str]:
    """Archivos de codigo que existen en disco, NO estan versionados y git IGNORA a proposito."""
    out = _git("ls-files", "--others", "--ignored", "--exclude-standard", "--", *CODE_ROOTS)
    assert out.returncode == 0, out.stderr
    return sorted(
        line
        for line in out.stdout.splitlines()
        if line.endswith(CODE_SUFFIXES) and "__pycache__" not in line and "/.venv/" not in line
    )


def test_no_source_file_is_hidden_by_gitignore():
    hidden = ignored_code_files()
    assert not hidden, (
        "Estos archivos de codigo existen pero git los IGNORA, asi que un clon limpio no los tendria "
        "(y los tags de rollback no arrancarian). Corregi la regla de .gitignore que los captura:\n  "
        + "\n  ".join(hidden)
    )


def test_the_session_package_is_versioned():
    """El caso concreto que fallo: el paquete de SessionState tiene que estar en git."""
    tracked = set(_git("ls-files", "backend/app/session").stdout.split())
    assert {"backend/app/session/__init__.py", "backend/app/session/events.py"} <= tracked


def test_the_guard_detects_a_hidden_file(tmp_path):
    """El guardia no es decorativo: con una regla que se come codigo, tiene que verlo.
    Usa un repo de juguete para no tocar el real."""
    repo = tmp_path / "juguete"
    (repo / "backend" / "app" / "session").mkdir(parents=True)
    (repo / "backend" / "app" / "session" / "events.py").write_text("x = 1\n", encoding="utf-8")
    (repo / ".gitignore").write_text("session/\n", encoding="utf-8")  # la regla que fallo
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)

    out = subprocess.run(
        ["git", "ls-files", "--others", "--ignored", "--exclude-standard", "--", "backend/app"],
        cwd=repo, capture_output=True, text=True, check=True,
    )

    assert "backend/app/session/events.py" in out.stdout
