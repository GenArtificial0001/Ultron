"""Detector de secretos para usar como hook de pre-commit o a mano (BL-005, ADR-018).

`gitleaks` no esta disponible en el mirror de la empresa, asi que este script trae sus propias reglas. Son DELIBERADAMENTE estrictas (claves privadas,
tokens con formato propio, JWT completos): el patron laxo de `.agent/avisar.py` (`password=...`, `token=...`) daba 49 falsos positivos en este repo
(codigo como `token = getAccessToken()`), y un hook que grita por todo termina desactivado. Con estas reglas el repo da 0 hallazgos.

Uso:
    python scripts/check_secrets.py             # revisa los archivos YA agregados al commit (staged); sale con 1 si encuentra algo
    python scripts/check_secrets.py --all       # revisa todo lo versionado
    python scripts/check_secrets.py a.py b.py   # revisa esos archivos

NO imprime el secreto completo (solo el tipo de regla, el archivo y la linea), para no copiarlo a una terminal o a un log.

Para activarlo en tu clon (no se activa solo; es una decision de cada uno):
    echo "python scripts/check_secrets.py" > .git/hooks/pre-commit
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

# (nombre de la regla, patron). Solo formatos que no se confunden con codigo normal.
RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("clave privada", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("clave tipo OpenAI (sk-...)", re.compile(r"\bsk-[A-Za-z0-9]{32,}")),
    ("clave de acceso de AWS (AKIA...)", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("JWT completo", re.compile(r"\beyJ[A-Za-z0-9_-]{30,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}")),
    ("token de GitHub", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}")),
    ("token de Slack", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}")),
)

# Archivos cuyo trabajo es justamente contener ejemplos de secretos para probar detectores. Se listan por ruta exacta, no por patron,
# para que agregar uno nuevo sea una decision visible en el diff.
ALLOWED_FIXTURES = frozenset(
    {
        "backend/tests/test_agent_avisar.py",
        "backend/tests/test_dashboard_export.py",
        "backend/tests/test_check_secrets.py",
    }
)

# No se leen: binarios y archivos de datos.
SKIP_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2", ".exe", ".wav", ".mp3", ".webm", ".pdf", ".zip", ".db", ".sqlite")


def scan_text(text: str) -> list[tuple[int, str]]:
    """(numero de linea, nombre de la regla) por cada coincidencia. Una sola por linea y por regla."""
    found: list[tuple[int, str]] = []
    for number, line in enumerate(text.splitlines(), start=1):
        for name, pattern in RULES:
            if pattern.search(line):
                found.append((number, name))
    return found


def scan_file(path: str, root: Path) -> list[tuple[int, str]]:
    normalized = path.replace("\\", "/")
    if normalized in ALLOWED_FIXTURES or normalized.lower().endswith(SKIP_SUFFIXES):
        return []
    try:
        text = (root / path).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []
    return scan_text(text)


def _git_lines(args: list[str], root: Path) -> list[str]:
    out = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=False).stdout
    return [line for line in out.splitlines() if line.strip()]


def main(argv: list[str]) -> int:
    root = Path(__file__).resolve().parent.parent
    if argv and argv[0] == "--all":
        files = _git_lines(["ls-files"], root)
    elif argv:
        files = argv
    else:
        files = _git_lines(["diff", "--cached", "--name-only", "--diff-filter=ACM"], root)

    problems = 0
    for path in files:
        for number, rule in scan_file(path, root):
            problems += 1
            print(f"{path}:{number}: posible secreto ({rule})")
    if problems:
        print(f"\nSe encontraron {problems} posibles secretos. Si es un ejemplo a proposito, agregá el archivo a ALLOWED_FIXTURES en este script.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
