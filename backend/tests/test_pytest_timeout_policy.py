"""Politica de timeouts de la suite: 60 s para todos (guardian contra cuelgues) y 300 s para los e2e.

Por que: `test_e2e_slow_model_notice.py` duerme 74 s A PROPOSITO (el aviso de modelo lento sale a los 30 s). Con el `timeout = 60`
de pytest.ini, `pytest` a secas moria con 'Timeout' en la mitad de la suite y solo andaba pasando `--timeout=300` a mano (asi se
corrieron full_suite_v5/v6/v8). `conftest.py` ahora marca los e2e con `E2E_TIMEOUT_S`. Estos tests corren pytest en un SUBPROCESO
sobre un directorio temporal con su propio conftest copiado, porque el marcador solo se ve a la hora de colectar."""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
CONFTEST = BACKEND / "tests" / "conftest.py"


def _real_constant() -> int:
    for line in CONFTEST.read_text(encoding="utf-8").splitlines():
        if line.startswith("E2E_TIMEOUT_S"):
            return int(line.split("=")[1])
    raise AssertionError("E2E_TIMEOUT_S no esta definido en tests/conftest.py")


def _real_hook() -> str:
    src = CONFTEST.read_text(encoding="utf-8").replace("\r\n", "\n")
    start = src.index("def pytest_collection_modifyitems")
    end = src.index("\ndef pytest_terminal_summary")
    hook = src[start:end]
    # el hook usa --shuffle; en el directorio temporal no hay esas opciones: se registran vacias.
    opts = (
        "def pytest_addoption(parser):\n"
        "    g = parser.getgroup('v')\n"
        "    g.addoption('--shuffle', action='store_true', default=False)\n"
        "    g.addoption('--shuffle-seed', type=int, default=None)\n\n"
    )
    return opts + hook


def test_the_e2e_limit_is_long_enough_for_the_slow_model_test():
    """El e2e mas lento duerme 74 s (SLOW_S * 2 + 8): el limite tiene que dejarle margen real."""
    assert _real_constant() >= 150


def test_the_e2e_limit_is_not_the_global_default():
    ini = (BACKEND / "pytest.ini").read_text(encoding="utf-8")
    assert "timeout = 60" in ini, "el guardian de 60 s para unit/integration no debe cambiar"
    assert _real_constant() > 60


def test_the_hook_adds_a_timeout_marker_to_e2e_and_leaves_others_alone(tmp_path):
    """Se inspecciona el marcador de verdad: un plugin de un archivo imprime el timeout que ve cada item."""
    (tmp_path / "pytest.ini").write_text(
        "[pytest]\ntimeout = 60\nmarkers =\n    e2e: x\n    unit: x\n    integration: x\n", encoding="utf-8"
    )
    (tmp_path / "test_x.py").write_text(
        textwrap.dedent(
            """
            import pytest

            @pytest.mark.e2e
            def test_e2e():
                pass

            @pytest.mark.e2e
            @pytest.mark.timeout(7)
            def test_e2e_own_limit():
                pass

            def test_plain():
                pass
            """
        ),
        encoding="utf-8",
    )
    (tmp_path / "conftest.py").write_text(
        "import inspect, random, pytest\n"
        f"E2E_TIMEOUT_S = {_real_constant()}\n" + _real_hook() + "\n\ndef pytest_collection_finish(session):\n"
        "    for it in session.items:\n"
        "        m = it.get_closest_marker('timeout')\n"
        "        print('LIMIT', it.name, m.args[0] if m else None)\n",
        encoding="utf-8",
    )
    out = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "--collect-only", "-q", "-s"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    ).stdout
    limits = dict(line.split()[1:3] for line in out.splitlines() if line.startswith("LIMIT"))
    assert limits["test_e2e"] == str(_real_constant())
    assert limits["test_e2e_own_limit"] == "7", "un e2e con su propio @pytest.mark.timeout lo conserva"
    assert limits["test_plain"] == "None", "los no-e2e siguen con el global de pytest.ini (60 s)"
