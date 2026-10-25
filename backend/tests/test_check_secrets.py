"""scripts/check_secrets.py (BL-005): detector de secretos estricto para pre-commit. Este archivo es una de las excepciones declaradas
(ALLOWED_FIXTURES) porque contiene ejemplos de secretos A PROPOSITO para probar el detector; los valores son inventados."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "check_secrets.py"
spec = importlib.util.spec_from_file_location("check_secrets", SCRIPT)
cs = importlib.util.module_from_spec(spec)
sys.modules["check_secrets"] = cs
spec.loader.exec_module(cs)

FAKE_SK = "sk-" + "a" * 40
FAKE_AWS = "AKIA" + "A" * 16
FAKE_JWT = "eyJ" + "a" * 40 + "." + "b" * 30 + "." + "c" * 20
FAKE_GH = "ghp_" + "a" * 40
FAKE_SLACK = "xoxb-" + "1" * 24
BEGIN_KEY = "-----BEGIN " + "RSA PRIVATE KEY-----"


@pytest.mark.parametrize(
    ("line", "rule"),
    [
        (BEGIN_KEY, "clave privada"),
        ("-----BEGIN PRIVATE KEY-----", "clave privada"),
        (f"clave = '{FAKE_SK}'", "OpenAI"),
        (f"AWS_KEY={FAKE_AWS}", "AWS"),
        (f"Authorization: Bearer {FAKE_JWT}", "JWT"),
        (f"GITHUB_TOKEN={FAKE_GH}", "GitHub"),
        (f"SLACK={FAKE_SLACK}", "Slack"),
    ],
)
def test_real_looking_secrets_are_found(line, rule):
    found = cs.scan_text(line)

    assert found and rule in found[0][1]
    assert found[0][0] == 1


@pytest.mark.parametrize(
    "line",
    [
        "const token = getAccessToken();",  # los 49 falsos positivos del patron laxo
        "password=settings.oracle_password,",
        "api_key=token_fetcher.token,",
        "cached_token = _try_cached_user_token()",
        "const token = _playToken;",
        "sk-corto",  # demasiado corto para ser una clave
        "ASKIA no es AKIA" + "A" * 3,  # AKIA sin los 16 caracteres seguidos
        "eyJ.sin.firma",  # no es un JWT completo
        "ghp_corto",
        "",
    ],
)
def test_ordinary_code_is_not_flagged(line):
    assert cs.scan_text(line) == []


def test_it_reports_the_line_number_and_never_prints_the_secret(capsys, tmp_path):
    f = tmp_path / "config.py"
    f.write_text("a = 1\nb = 2\n" + f"k = '{FAKE_SK}'\n", encoding="utf-8")

    code = cs.main([str(f)])

    out = capsys.readouterr().out
    assert code == 1
    assert ":3:" in out and "OpenAI" in out
    assert FAKE_SK not in out and FAKE_SK[:12] not in out  # no copia el secreto a la terminal


def test_a_clean_file_gives_exit_code_zero(capsys, tmp_path):
    f = tmp_path / "ok.py"
    f.write_text("token = get()\n", encoding="utf-8")

    assert cs.main([str(f)]) == 0
    assert capsys.readouterr().out == ""


def test_declared_fixtures_and_binaries_are_skipped(tmp_path):
    (tmp_path / "backend" / "tests").mkdir(parents=True)
    fixture = tmp_path / "backend" / "tests" / "test_agent_avisar.py"
    fixture.write_text(f"k = '{FAKE_SK}'\n", encoding="utf-8")
    image = tmp_path / "logo.png"
    image.write_bytes(f"{FAKE_SK}".encode())

    assert cs.scan_file("backend/tests/test_agent_avisar.py", tmp_path) == []
    assert cs.scan_file("logo.png", tmp_path) == []
    (tmp_path / "otro").mkdir()
    (tmp_path / "otro" / "test_agent_avisar.py").write_text(f"k = '{FAKE_SK}'" + chr(10), encoding="utf-8")
    assert cs.scan_file("otro/test_agent_avisar.py", tmp_path) != []  # la excepcion es por ruta EXACTA: mismo nombre en otra carpeta SI se revisa


def test_a_missing_file_does_not_crash(tmp_path):
    assert cs.scan_file("no-existe.py", tmp_path) == []


def test_the_repository_itself_is_clean():
    """Es el motivo de tener reglas estrictas: el repo tiene que dar 0 hallazgos, o el hook se desactiva por ruidoso."""
    result = subprocess.run([sys.executable, str(SCRIPT), "--all"], cwd=ROOT, capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stdout


def test_all_really_scans_the_tracked_files(tmp_path):
    """Sin este test, `--all` podria no revisar nada y el test del repo limpio pasaria igual (dato: 1296 archivos versionados)."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "leak.py").write_text(f"k = '{FAKE_SK}'" + chr(10), encoding="utf-8")
    subprocess.run(["git", "add", "leak.py"], cwd=tmp_path, check=True)
    root_script = tmp_path / "scripts"
    root_script.mkdir()
    (root_script / "check_secrets.py").write_text(SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")

    result = subprocess.run([sys.executable, str(root_script / "check_secrets.py"), "--all"], cwd=tmp_path, capture_output=True, text=True, check=False)

    assert result.returncode == 1 and "leak.py:1:" in result.stdout
