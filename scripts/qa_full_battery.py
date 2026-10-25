"""Bateria COMPLETA de Vision en un solo comando, para cualquier agente (Claude Code, OpenCode...) o persona.

Por que existe: la regla del proyecto es cerrar todo cambio con la bateria completa antes/despues sin regresiones, y hasta hoy los pasos vivian en notas sueltas y
scripts temporales de cada agente. El usuario pidio (2026-10-08) que cualquier agente pueda continuar y correrla.

Que corre (en serie, nunca en paralelo, para no cargar la maquina):
  1. `pytest -m "not e2e" --cov` (piso de .coveragerc)
  2. `pytest -m "not e2e" --shuffle` (orden aleatorio; la semilla queda en el informe)
  3. cada `tests/test_e2e_*.py` en su PROPIO proceso (un cuelgue de Chromium al cerrar no tapa al resto)
  4. `scripts/qa_battery.py` (estatico) y `ruff check backend/app`

Uso (desde la raiz del repo):
  backend\\.venv\\Scripts\\python.exe scripts\\qa_full_battery.py --out .agent\\artifacts\\full_suite_v23
  ... --skip-e2e                 # solo 1, 2 y 4 (~10 min)
  ... --e2e-filter switches      # solo los e2e cuyo archivo contiene ese texto
Deja un `_resumen.txt` con una linea por paso y sale con 1 si algo fallo. Verificar SIEMPRE que los e2e no digan `skipped` (falta Chromium de Playwright)
y que la duracion sea razonable (si la PC se suspende, los tests con reloj fallan)."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BACKEND = REPO / "backend"
PY = BACKEND / ".venv" / "Scripts" / "python.exe"
_SUMMARY_RE = re.compile(r"(\d+ (passed|failed|error|errors|skipped|xfailed|deselected)[^\n]*) in [\d.]+s")


def _last_summary(text: str) -> str:
    found = _SUMMARY_RE.findall(text)
    return found[-1][0] if found else "(sin resumen de pytest)"


def _run(label: str, cmd: list[str], cwd: Path, out_file: Path, lines: list[str]) -> bool:
    start = time.monotonic()
    # check=False a proposito: un paso rojo se anota en el resumen y la bateria sigue con los demas
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
    out_file.write_text(proc.stdout + proc.stderr, encoding="utf-8")
    seconds = time.monotonic() - start
    text = proc.stdout + proc.stderr
    detail = _last_summary(text) if "pytest" in cmd else (text.strip().splitlines() or ["(sin salida)"])[-1]
    extra = ""
    seed = re.search(r"--shuffle-seed=(\d+)", text)
    if seed:
        extra = f" | seed {seed.group(1)}"
    cover = re.search(r"Total coverage: ([\d.]+%)", text)
    if cover:
        extra += f" | cobertura {cover.group(1)}"
    line = f"{label} rc={proc.returncode} {seconds:.0f}s | {detail}{extra}"
    lines.append(line)
    print(line, flush=True)
    return proc.returncode == 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", required=True, help="carpeta de salida (relativa a la raiz del repo)")
    parser.add_argument("--skip-e2e", action="store_true")
    parser.add_argument("--e2e-filter", default="", help="solo los e2e cuyo nombre de archivo contiene este texto")
    args = parser.parse_args()

    if not PY.exists():
        print(f"No encuentro el Python del proyecto en {PY}", file=sys.stderr)
        return 2
    out = (REPO / args.out).resolve()
    (out / "e2e").mkdir(parents=True, exist_ok=True)
    lines: list[str] = [f"inicio {time.strftime('%Y-%m-%d %H:%M:%S')}"]
    pytest = [str(PY), "-m", "pytest", "--timeout=300", "-q", "-p", "no:cacheprovider"]
    ok = True

    ok &= _run("sin_e2e_cobertura", [*pytest, "-m", "not e2e", "--cov"], BACKEND, out / "nonE2E_cov.txt", lines)
    ok &= _run("sin_e2e_orden_aleatorio", [*pytest, "-m", "not e2e", "--shuffle"], BACKEND, out / "nonE2E_shuffle.txt", lines)
    if not args.skip_e2e:
        files = sorted(p for p in (BACKEND / "tests").glob("test_e2e_*.py") if args.e2e_filter in p.name)
        if not files:
            lines.append(f"e2e: ningun archivo coincide con «{args.e2e_filter}»")
            ok = False
        for path in files:
            ok &= _run(f"e2e {path.stem}", [*pytest, f"tests/{path.name}"], BACKEND, out / "e2e" / f"{path.stem}.txt", lines)
    ok &= _run("qa_battery", [str(PY), "scripts/qa_battery.py"], REPO, out / "qa_battery.txt", lines)
    ok &= _run("ruff", [str(PY), "-m", "ruff", "check", "backend/app"], REPO, out / "ruff.txt", lines)

    lines.append(f"fin {time.strftime('%Y-%m-%d %H:%M:%S')} | RESULTADO: {'TODO VERDE' if ok else 'HAY FALLOS (ver los .txt de cada paso)'}")
    (out / "_resumen.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(lines[-1])
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
