"""Arma `build/embed/` -- una copia LIMPIA del codigo fuente minimo que
`Vision.exe` necesita para funcionar, para que `build_exe.bat` la embeba
adentro del `.exe` con `--add-data`.

Corre ANTES de PyInstaller. Objetivo explicito: "sin basura" -- se copia
UNICAMENTE lo que el server necesita para arrancar (backend/app,
backend/requirements.txt,
backend/.env.example, frontend/ completo). Se excluye todo lo demas:
`__pycache__`, `.pytest_cache`, `.ruff_cache`, `backend/.venv*`,
`backend/tests/`, `backend/keys/` (credenciales), `backend/data/`,
`backend/logs/`, `backend/workspace/`, `backend/browser/`,
`backend/.env` (credenciales reales del usuario que compila -- nunca se
embeben), `backend/portable_main.py` (arquitectura alternativa no usada
por este lanzador) y los scripts de debugging de `backend/scripts/`
(`check_sizes.py`, `perf_probe.py`, etc, solo para desarrollo).

Al final escribe `build/embed/.embed-hash`: un hash SHA256 combinado de
TODO el contenido copiado, que `launch_vision.py` compara contra la ultima
extraccion en `%LOCALAPPDATA%\\Vision-personal\\app\\.vision-embed-hash` para saber
si hace falta resincronizar (o si el codigo embebido es identico al ya
extraido, y puede saltear la copia)."""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STAGE_DIR = ROOT / "build" / "embed"

# (origen relativo a ROOT, destino relativo a STAGE_DIR)
FILES_TO_COPY = [
    ("backend/requirements.txt", "backend/requirements.txt"),
    ("backend/.env.example", "backend/.env.example"),
]

DIRS_TO_COPY = [
    ("backend/app", "backend/app"),
    ("frontend", "frontend"),
]

# Nunca copiar estos nombres, sin importar en que subcarpeta aparezcan.
EXCLUDE_NAMES = {"__pycache__", ".pytest_cache", ".ruff_cache"}
EXCLUDE_SUFFIXES = {".pyc", ".pyo"}


def _ignore(_dir: str, names: list[str]) -> set[str]:
    return {
        n
        for n in names
        if n in EXCLUDE_NAMES or Path(n).suffix in EXCLUDE_SUFFIXES
    }


def _hash_tree(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_dir():
            continue
        digest.update(str(path.relative_to(root)).replace("\\", "/").encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def main() -> None:
    if STAGE_DIR.exists():
        shutil.rmtree(STAGE_DIR)
    STAGE_DIR.mkdir(parents=True)

    for src_rel, dst_rel in FILES_TO_COPY:
        src = ROOT / src_rel
        dst = STAGE_DIR / dst_rel
        if not src.exists():
            print(f"AVISO: {src_rel} no existe -- se omite.")
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        print(f"  copiado: {src_rel}")

    for src_rel, dst_rel in DIRS_TO_COPY:
        src = ROOT / src_rel
        dst = STAGE_DIR / dst_rel
        if not src.exists():
            print(f"AVISO: {src_rel}/ no existe -- se omite.")
            continue
        shutil.copytree(src, dst, ignore=_ignore)
        print(f"  copiado: {src_rel}/")

    combined_hash = _hash_tree(STAGE_DIR)
    (STAGE_DIR / ".embed-hash").write_text(combined_hash, encoding="utf-8")
    print(f"\nHash combinado: {combined_hash}")

    total_bytes = sum(p.stat().st_size for p in STAGE_DIR.rglob("*") if p.is_file())
    print(f"Tamano total embebido: {total_bytes / 1024:.0f} KB")


if __name__ == "__main__":
    main()
