"""Migracion de la carpeta de datos de Vision-Personal a la de ULTRON (spec portar-desde-origen REQ-009).

Por que existe: ULTRON 2.0 guarda todo en `%LOCALAPPDATA%\\Ultron` (antes `%LOCALAPPDATA%\\Vision-personal`). Sin esto, la persona
abriria ULTRON y no veria su `.env`, sus claves cifradas, sus sesiones ni sus alarmas: tendria que configurar todo de nuevo.

Reglas:
- Solo COPIA: la carpeta vieja no se borra ni se modifica (si algo sale mal, sigue ahi).
- Solo una vez y solo si la nueva todavia no tiene datos propios (ni `.env`, ni `.env.enc`, ni `vision.db`, ni claves). Asi nunca pisa
  nada de ULTRON.
- No copia `app/` (el codigo extraido por el lanzador, con su entorno de Python: se vuelve a crear solo) ni `logs/`.
- Deja una nota (`migracion-desde-vision-personal.json`) con lo copiado, para que el chat lo diga UNA vez (`take_notice`).
- Solo usa la libreria estandar: corre al cargar la configuracion, antes que cualquier otro modulo abra la base.
"""

from __future__ import annotations

import json
import logging
import shutil
import time
from pathlib import Path

logger = logging.getLogger(__name__)

PREVIOUS_FOLDER_NAME = "Vision-personal"
NOTICE_FILE = "migracion-desde-vision-personal.json"
# Lo que dice que una carpeta ya tiene datos de la persona (si la nueva tiene alguno, no se copia nada).
_USER_DATA_MARKERS = (".env", ".env.enc", "vision.db", "keys/credentials.key", "connections.json")
# Lo que nunca se copia: el codigo extraido con su entorno de Python (se recrea solo) y los logs viejos.
_SKIP = {"app", "logs", "__pycache__"}


def _has_user_data(folder: Path) -> bool:
    return any((folder / marker).exists() for marker in _USER_DATA_MARKERS)


def migrate(old: Path, new: Path) -> list[str] | None:
    """Copia los datos de `old` a `new` si corresponde. Devuelve los nombres copiados, o None si no habia nada que hacer.

    Nunca lanza: un error de copia queda en el log y la app arranca igual (con lo que se haya podido copiar)."""
    try:
        if old.resolve() == new.resolve() or not old.is_dir() or not _has_user_data(old) or _has_user_data(new):
            return None
    except OSError:
        return None
    copied: list[str] = []
    new.mkdir(parents=True, exist_ok=True)
    for item in sorted(old.iterdir(), key=lambda p: p.name.lower()):
        if item.name in _SKIP or item.name == NOTICE_FILE:
            continue
        target = new / item.name
        try:
            if item.is_dir():
                shutil.copytree(item, target, dirs_exist_ok=True)
            elif not target.exists():
                shutil.copy2(item, target)
            else:
                continue
            copied.append(item.name)
        except OSError:
            logger.warning("No se pudo copiar %s desde la carpeta de Vision-Personal", item.name, exc_info=True)
    notice = {"from": str(old), "copied": copied, "at": time.strftime("%Y-%m-%d %H:%M:%S"), "notified": False}
    try:
        (new / NOTICE_FILE).write_text(json.dumps(notice, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        logger.warning("No se pudo guardar la nota de la migracion", exc_info=True)
    logger.info("Datos copiados desde %s: %s", old, ", ".join(copied) or "(nada)")
    return copied


def take_notice(base: Path) -> dict | None:
    """La nota de la migracion la PRIMERA vez que se pide (despues queda marcada como avisada). None si no hay nada que avisar."""
    path = base / NOTICE_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("notified"):
        return None
    data["notified"] = True
    try:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        return None  # sin poder marcarla, mejor no avisar que avisar en cada arranque
    copied = [str(c) for c in data.get("copied", [])]
    return {
        "copied": copied,
        "message": (
            "ULTRON trajo tu configuración y tus datos de Vision-Personal ("
            + (", ".join(copied) if copied else "nada que copiar")
            + "). La carpeta vieja quedó intacta: podés borrarla cuando confirmes que está todo."
        ),
    }
