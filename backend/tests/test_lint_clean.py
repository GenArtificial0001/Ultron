"""BL-002: el codigo de Vision queda limpio con las reglas de `backend/ruff.toml` (F: errores reales, E9: sintaxis, B: bugs probables).

Guardia contra regresiones: un import sin usar, una variable que nunca se lee o un `raise` dentro de `except` sin encadenar
suelen ser el rastro de un bug (un nombre mal escrito, un error tragado). Las reglas estilisticas (orden de imports, BLE001,
RUF100) quedan FUERA a proposito: `main.py` coloca codigo ENTRE imports (fija la politica de asyncio antes de importar
uvicorn) y el repo suprime BLE001 con `# noqa` deliberadamente (AGENTS.md). Ver .agent/DECISIONS.md ADR-005."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent
CONFIG = BACKEND / "ruff.toml"


def _ruff(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "ruff", *args],
        cwd=BACKEND, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
    )


pytestmark = pytest.mark.skipif(_ruff("--version").returncode != 0, reason="ruff no esta instalado")


def test_the_ruff_config_exists_and_selects_the_agreed_rules():
    assert CONFIG.is_file(), "falta backend/ruff.toml"
    text = CONFIG.read_text(encoding="utf-8")
    assert '"F"' in text and '"E9"' in text and '"B"' in text


def test_app_has_no_ruff_findings():
    out = _ruff("check", "app", "--config", str(CONFIG), "--output-format=concise")
    assert out.returncode == 0, "ruff encontro problemas en app/:\n" + out.stdout[-3000:]


def test_the_agent_tooling_has_no_ruff_findings():
    agent = REPO / ".agent"
    if not agent.is_dir():
        pytest.skip("sin .agent (ej. arbol de prod)")
    out = _ruff("check", str(agent), "--config", str(CONFIG), "--output-format=concise")
    assert out.returncode == 0, out.stdout[-2000:]


def test_the_guard_actually_catches_a_real_problem(tmp_path):
    """No es decorativo: sobre un archivo con un import sin usar y una variable muerta, ruff con ESTA config falla."""
    bad = tmp_path / "malo.py"
    bad.write_text("import os\n\n\ndef f():\n    x = 1\n    try:\n        pass\n    except ValueError:\n        raise KeyError('a')\n", encoding="utf-8")
    out = _ruff("check", str(bad), "--config", str(CONFIG), "--output-format=concise")
    assert out.returncode != 0
    assert "F401" in out.stdout and "F841" in out.stdout and "B904" in out.stdout
