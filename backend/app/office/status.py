"""Estado de Office para Configuracion > Conexiones (spec office-com-automatizacion REQ-015, con el interruptor de REQ-009).

Responde «¿se puede usar Excel y Word?» SIN abrir ni lanzar nada: Excel solo se mira si ya esta corriendo (el mismo enganche de `core.list_workbooks`, que nunca lo lanza) y Word se
mira solo por el registro de Windows (decir «se puede usar» no exige enganchar su COM). Apagado el interruptor, no se toca COM ni para mirar. Consultar nunca lanza, nunca pasa del
plazo y nunca guarda ni abre un libro; cada estado trae una frase para la persona (sin texto crudo ni jerga)."""

from __future__ import annotations

import logging
import sys
from typing import Any

from app import switches
from app.office import core, errors

logger = logging.getLogger(__name__)

STATUS_TIMEOUT = 4.0  # una consulta de estado no puede colgar la pantalla: mas corto que el plazo de 15 s de una accion

_EXCEL = "Excel.Application"
_WORD = "Word.Application"


def _registry_has(progid: str) -> bool:
    """True si Windows tiene registrada esa aplicacion de Office (ProgID). No la arranca. Fuera de Windows o ante cualquier falla, False."""
    if sys.platform != "win32":
        return False
    try:
        import winreg  # import diferido: solo existe en Windows

        winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, rf"{progid}\CLSID"))
        return True
    except OSError:
        return False
    except Exception:  # noqa: BLE001 -- el registro nunca puede romper la pantalla
        logger.warning("Office: no se pudo leer el registro de Windows", exc_info=True)
        return False


def _registered(progid: str) -> bool:
    return _registry_has(progid)


def _has_pywin32() -> bool:
    """True si el componente que habla con Office esta en esta PC. Se importa de forma diferida: una PC sin el no rompe nada."""
    try:
        import pythoncom  # noqa: F401, PLC0415
        import win32com.client  # noqa: F401, PLC0415

        return True
    except ImportError:
        return False


def _item(state: str, message: str) -> dict[str, str]:
    return {"state": state, "message": message}


def _excel_status(timeout: float) -> dict[str, str]:
    if not _has_pywin32() or not _registered(_EXCEL):
        return _item(
            "missing",
            "Excel no está instalado en esta PC, o falta el componente que lo conecta con Vision. Avisá a soporte.",
        )
    try:
        books = core.list_workbooks(timeout=timeout)
    except errors.OfficeNotRunning:
        return _item("closed", "Excel está cerrado. Abrilo con el libro que quieras usar y Vision lo va a poder leer.")
    except (errors.OfficeBusy, errors.OfficeTimeout):
        return _item("busy", "Excel está ocupado (puede tener un cuadro de diálogo abierto). Cerralo y probá de nuevo.")
    except errors.OfficeUnavailable:
        return _item(
            "missing",
            "Excel no está instalado en esta PC, o falta el componente que lo conecta con Vision. Avisá a soporte.",
        )
    except Exception as exc:  # noqa: BLE001 -- el crudo va al log (solo el tipo: el mensaje puede traer celdas), la persona ve una frase
        logger.warning("Office: no se pudo consultar Excel (%s)", errors.technical_detail(exc))
        return _item(
            "unknown", "No pude saber si Excel está disponible. Probá de nuevo; si se repite, avisá a soporte."
        )
    if not books:
        return _item("running", "Excel está abierto, pero sin ningún libro. Abrí el libro que quieras usar.")
    n = len(books)
    return _item(
        "running",
        f"Excel está abierto con {n} libro{'s' if n != 1 else ''}. Vision puede leerlo{'s' if n != 1 else ''} cuando se lo pidas.",
    )


def _word_status() -> dict[str, str]:
    if not _has_pywin32() or not _registered(_WORD):
        return _item(
            "missing",
            "Word no está instalado en esta PC, o falta el componente que lo conecta con Vision. Avisá a soporte.",
        )
    return _item(
        "installed",
        "Word está instalado. Vision lo usa en segundo plano solo para convertir documentos; no hace falta abrirlo.",
    )


def snapshot(timeout: float = STATUS_TIMEOUT) -> dict[str, Any]:
    """`{enabled, excel: {state, message}, word: {state, message}}`. Con el interruptor apagado no se consulta nada."""
    enabled = switches.is_office_enabled()
    if not enabled:
        off = _item("off", "Office está desactivado: Vision no lo usa.")
        return {"enabled": False, "excel": dict(off), "word": dict(off)}
    return {"enabled": True, "excel": _excel_status(timeout), "word": _word_status()}
