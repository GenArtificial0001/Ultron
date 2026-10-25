"""El lanzador verifica que las dependencias esenciales se puedan IMPORTAR de verdad (spec lanzador-dependencias-verificadas, REQ-001..008).

Reporte del usuario (2026-10-23, de la PC de una companera): «a algunas versiones le sale que falta pandas». `requirements.txt` ya declara pandas; el hueco era
que, si `pip install` fallaba (VPN, proxy, una descarga grande cortada), el lanzador arrancaba igual y el primer `import pandas` explotaba despues.

Sin pip, sin red y sin venv real: `_run` (pip) y `subprocess.run` (la verificacion de imports) se reemplazan por un FAKE que lleva la cuenta de lo que
se pidio y responde segun el estado simulado de cada paquete (instalado / roto / ausente)."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

_LAUNCHER = Path(__file__).resolve().parents[2] / "launch_vision.py"
_spec = importlib.util.spec_from_file_location("launch_vision_verified_deps_under_test", _LAUNCHER)
launcher = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(launcher)

ESSENTIALS = list(launcher.ESSENTIAL_IMPORTS)
REQUIREMENTS = Path(__file__).resolve().parents[1] / "requirements.txt"


class FakeEnv:
    """Estado simulado del `.venv`: que paquetes importan y que hace cada `pip install`."""

    def __init__(self) -> None:
        self.broken: set[str] = set()  # esenciales que NO importan
        self.pip_results: list[int] = []  # codigo de salida de cada `pip install -r` (el ultimo se repite)
        self.heals_on_pip: bool = True  # un pip exitoso deja todo importable
        self.pip_calls: list[list[str]] = []
        self.check_calls = 0
        self.check_crashes = False

    def run(self, cmd, **kwargs):
        self.pip_calls.append([str(c) for c in cmd])
        code = self.pip_results.pop(0) if len(self.pip_results) > 1 else (self.pip_results[0] if self.pip_results else 0)
        if code == 0 and self.heals_on_pip and "-r" in cmd:
            self.broken.clear()
        return subprocess.CompletedProcess(cmd, code)

    def sub_run(self, cmd, **kwargs):
        text = " ".join(str(c) for c in cmd)
        if "pip" in text and "show" in text:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        self.check_calls += 1
        if self.check_crashes:
            raise OSError("no se pudo lanzar el python del venv")
        missing = [n for n in ESSENTIALS if n in self.broken]
        return subprocess.CompletedProcess(cmd, 0, stdout="\n".join(missing), stderr="")


@pytest.fixture
def env(tmp_path, monkeypatch):
    backend = tmp_path / "backend"
    (backend / ".venv" / "Scripts").mkdir(parents=True)
    py = backend / ".venv" / "Scripts" / "python.exe"
    py.write_text("falso", encoding="utf-8")
    (backend / "requirements.txt").write_text("fastapi\npandas\n", encoding="utf-8")
    fake = FakeEnv()
    fake.backend, fake.py = backend, py
    monkeypatch.setattr(launcher, "_run", fake.run)
    monkeypatch.setattr(launcher.subprocess, "run", fake.sub_run)
    return fake


def ensure(env) -> None:
    launcher._ensure_requirements(env.backend, env.py)


def marker(env) -> Path:
    return env.backend / ".venv" / ".vision-deps-ok"


# ---- REQ-001: la verificacion ------------------------------------------------------------------------------------------------
def test_missing_importable_lists_what_does_not_import(env):
    env.broken = {"pandas", "numpy"}
    assert launcher._missing_importable(env.py, env.backend) == ["numpy", "pandas"] or set(
        launcher._missing_importable(env.py, env.backend)
    ) == {"pandas", "numpy"}


def test_missing_importable_is_empty_when_everything_imports(env):
    assert launcher._missing_importable(env.py, env.backend) == []


def test_the_check_uses_the_venvs_python_not_the_launchers_own(env, monkeypatch):
    seen: list[str] = []
    real = env.sub_run
    monkeypatch.setattr(launcher.subprocess, "run", lambda cmd, **k: (seen.append(str(cmd[0])), real(cmd, **k))[1])
    launcher._missing_importable(env.py, env.backend)
    assert seen and seen[0] == str(env.py)


def test_the_check_never_raises_even_if_the_subprocess_fails(env):
    env.check_crashes = True
    assert launcher._missing_importable(env.py, env.backend) == []  # no se pudo verificar: no se inventa una falta


def test_the_check_imports_every_essential(env, monkeypatch):
    captured: list[str] = []
    monkeypatch.setattr(launcher.subprocess, "run", lambda cmd, **k: (captured.append(" ".join(map(str, cmd))), subprocess.CompletedProcess(cmd, 0, stdout="", stderr=""))[1])
    launcher._missing_importable(env.py, env.backend)
    code = captured[0]
    for name in ESSENTIALS:
        assert name in code


# ---- REQ-002: tras instalar --------------------------------------------------------------------------------------------------
def test_a_good_install_with_everything_importable_writes_the_marker(env, capsys):
    env.pip_results = [0]
    ensure(env)
    assert marker(env).exists()
    assert sum("-r" in c for c in env.pip_calls) == 1  # un solo pip
    assert "ADVERTENCIA" not in capsys.readouterr().out


def test_pip_succeeds_but_pandas_does_not_import_so_it_retries_once_and_then_writes_the_marker(env):
    env.pip_results = [0, 0]
    env.heals_on_pip = False  # la primera vez no lo arregla
    state = {"n": 0}
    real_run = env.run

    def run(cmd, **k):
        r = real_run(cmd, **k)
        if "-r" in cmd:
            state["n"] += 1
            if state["n"] == 2:
                env.broken.clear()  # el reintento SI lo arregla
        return r

    env.broken = {"pandas"}
    launcher._run = run
    ensure(env)
    assert sum("-r" in c for c in env.pip_calls) == 2
    assert marker(env).exists()


# ---- REQ-003/005: si sigue faltando ------------------------------------------------------------------------------------------
def test_if_it_still_does_not_import_there_is_no_marker_and_a_clear_notice_naming_it(env, capsys):
    env.broken = {"pandas"}
    env.heals_on_pip = False
    env.pip_results = [0]
    ensure(env)  # no lanza
    out = capsys.readouterr().out
    assert not marker(env).exists()
    assert "pandas" in out and "VPN" in out
    assert sum("-r" in c for c in env.pip_calls) == 2  # el intento y UN reintento, no un bucle


def test_when_pip_fails_the_notice_names_what_is_missing(env, capsys):
    env.broken = {"pandas", "openpyxl"}
    env.heals_on_pip = False
    env.pip_results = [1]
    ensure(env)
    out = capsys.readouterr().out
    assert "pandas" in out and "openpyxl" in out
    assert not marker(env).exists()


def test_it_never_raises_and_vision_still_starts(env):
    env.broken = set(ESSENTIALS)
    env.heals_on_pip = False
    env.pip_results = [1]
    ensure(env)  # si lanzara, el arranque se cortaria


# ---- REQ-004: el marcador «al dia» -------------------------------------------------------------------------------------------
def prime_marker(env) -> None:
    marker(env).write_text(launcher._sha256_file(env.backend / "requirements.txt"), encoding="utf-8")


def test_an_up_to_date_marker_with_everything_importable_runs_no_pip(env, capsys):
    prime_marker(env)
    ensure(env)
    assert env.pip_calls == []
    assert "Dependencias al dia -- salteando pip install." in capsys.readouterr().out


def test_an_up_to_date_marker_but_a_broken_package_reinstalls(env, capsys):
    prime_marker(env)
    env.broken = {"numpy"}
    env.pip_results = [0]
    ensure(env)
    assert sum("-r" in c for c in env.pip_calls) >= 1
    assert marker(env).exists()  # tras reinstalar y verificar, vuelve a estar al dia


def test_a_broken_package_with_a_matching_marker_removes_the_marker_before_installing(env, monkeypatch):
    prime_marker(env)
    env.broken = {"numpy"}
    env.pip_results = [0]
    existed_at_install: list[bool] = []
    real = env.run
    monkeypatch.setattr(launcher, "_run", lambda cmd, **k: (existed_at_install.append(marker(env).exists()), real(cmd, **k))[1])
    ensure(env)
    assert existed_at_install and existed_at_install[0] is False


# ---- REQ-006: lo de siempre no cambia ---------------------------------------------------------------------------------------
def test_the_normal_messages_are_unchanged_for_a_healthy_first_install(env, capsys):
    env.pip_results = [0]
    ensure(env)
    out = capsys.readouterr().out
    assert "requirements.txt cambio -- instalando/actualizando dependencias..." in out
    assert "(la primera vez puede tardar varios minutos" in out


def test_the_verification_is_done_once_when_all_is_well(env):
    prime_marker(env)
    ensure(env)
    assert env.check_calls == 1


# ---- REQ-008: requirements.txt declara los esenciales -----------------------------------------------------------------------
@pytest.mark.parametrize("module", ESSENTIALS)
def test_requirements_declares_every_essential(module):
    """Quitar pandas (u otro esencial) de requirements.txt tiene que ser un error visible, no un «falta pandas» en la PC de alguien."""
    dist = launcher.ESSENTIAL_DISTRIBUTIONS[module]
    names = {n.lower().replace("_", "-") for n in launcher._requirement_names(REQUIREMENTS)}
    assert dist.lower().replace("_", "-") in names, f"{dist} (para importar `{module}`) no esta en requirements.txt"


def test_every_essential_has_a_distribution_name():
    assert set(launcher.ESSENTIAL_DISTRIBUTIONS) == set(ESSENTIALS)


# ---- huecos cerrados tras las mutaciones ---------------------------------------------------------------------------------------
def test_the_check_ignores_unrelated_output_lines(env, monkeypatch):
    """Si el python del venv imprime algo que NO es un esencial (un aviso de pip, un DeprecationWarning...), eso no cuenta como paquete faltante."""
    out = "DeprecationWarning: algo viejo\npandas\nruido cualquiera\n"
    monkeypatch.setattr(launcher.subprocess, "run", lambda cmd, **k: subprocess.CompletedProcess(cmd, 0, stdout=out, stderr=""))
    assert launcher._missing_importable(env.py, env.backend) == ["pandas"]


def test_the_notice_tells_the_person_to_connect_to_the_vpn_and_reopen(env, capsys):
    launcher._warn_missing_dependencies(["pandas", "numpy"])
    text = capsys.readouterr().out
    assert "pandas" in text and "numpy" in text
    assert "VPN de tu empresa" in text and "volve a abrir Vision" in text


def test_the_probe_code_imports_each_essential_and_prints_the_failures(env, monkeypatch):
    captured: list[str] = []
    monkeypatch.setattr(
        launcher.subprocess,
        "run",
        lambda cmd, **k: (captured.append(cmd[2]), subprocess.CompletedProcess(cmd, 0, stdout="", stderr=""))[1],
    )
    launcher._missing_importable(env.py, env.backend)
    probe = captured[0]
    assert "import_module" in probe and "print(name)" in probe and "except Exception" in probe
    for name in ESSENTIALS:
        assert repr(name) in probe  # cada esencial, entre comillas, en la lista que se recorre


def test_a_real_python_reports_a_module_that_does_not_exist(tmp_path, monkeypatch):
    """Sin doble: el codigo de prueba corre en un Python REAL. Con un esencial inventado que no existe, lo informa; con modulos que si existen, no."""
    import sys

    monkeypatch.setattr(launcher, "ESSENTIAL_IMPORTS", ("json", "este_modulo_no_existe_xyz", "os"))
    real_run = subprocess.run
    monkeypatch.setattr(launcher.subprocess, "run", real_run)
    assert launcher._missing_importable(Path(sys.executable), tmp_path) == ["este_modulo_no_existe_xyz"]
