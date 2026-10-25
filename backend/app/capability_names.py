"""Nombre de «Outlook y Teams» y de «Office» elegido por la PERSONA (spec conexiones-office-m365-visibles REQ-008, REQ-009, REQ-010).

Por que existe: las conexiones del gestor (Oracle, SharePoint, SAP, Confluence) tienen un nombre que escribe la persona y se muestran como «Tipo - nombre». Outlook y Teams y Office
no son conexiones del gestor (no tienen campos ni credenciales), pero el usuario pidio lo mismo (2026-10-19): «yo soy el que debe asignar el nombre de esa conexion y poder editarla,
como todas las demas». La 0.33.1 los mostraba con un nombre fijo; esto lo corrige.

Reglas: son las MISMAS que las de las demas conexiones (`connections.valid_name`), asi un nombre valido aca lo es alla y la pantalla y el servidor no pueden discrepar. Sin nombre
guardado se llaman «Principal» (el nombre por defecto de las conexiones armadas desde el `.env`). Se guarda en la base maestra (no es un secreto). Un valor guardado roto, ilegible
o que no pase la regla NUNCA rompe la pantalla ni se muestra: se usa «Principal». Como la regla no deja letras raras, un nombre no puede traer HTML ni una marca de contenido."""

from __future__ import annotations

import logging

from app import connections
from app.persistence import master_index

logger = logging.getLogger(__name__)

DEFAULT_NAME = "Principal"
# capacidad -> (tipo que se muestra delante del nombre, clave donde se guarda el nombre)
_CAPABILITIES: dict[str, tuple[str, str]] = {
    "m365": ("Outlook y Teams", "m365_connection_name"),
    "office": ("Office", "office_connection_name"),
}


def _spec(capability: str) -> tuple[str, str]:
    try:
        return _CAPABILITIES[capability]
    except KeyError as exc:
        raise ValueError(f"Capacidad desconocida: {capability!r}") from exc


def problem(name: object) -> str:
    """Lo que la pantalla le dice a la persona si el nombre no sirve («» si sirve). Usa la misma regla que `connections.valid_name`."""
    if not isinstance(name, str) or not name.strip():
        return "Poné un nombre antes de guardarlo."
    if not connections.valid_name(name):
        return "Solo letras, números, espacios y guion (hasta 60), sin espacios al principio ni al final."
    return ""


def get(capability: str) -> str:
    """El nombre que la persona le puso, o «Principal». Un valor guardado roto no se muestra y no lanza."""
    _, key = _spec(capability)
    try:
        saved = master_index.get_app_setting(key)
    except Exception:  # noqa: BLE001 -- sin base legible el nombre por defecto sigue andando
        logger.warning("No se pudo leer el nombre de %s (se usa «%s»)", capability, DEFAULT_NAME, exc_info=True)
        return DEFAULT_NAME
    if isinstance(saved, str) and connections.valid_name(saved):
        return saved
    return DEFAULT_NAME


def display(capability: str) -> str:
    """«Tipo - nombre», igual que «Oracle - Datamart»."""
    label, _ = _spec(capability)
    return f"{label} - {get(capability)}"


def set_name(capability: str, name: str) -> str:
    """Guarda el nombre (ya validado con la regla de las demas conexiones) y devuelve el `display` nuevo. Lanza `ValueError` con la frase para la persona si no sirve."""
    _, key = _spec(capability)
    message = problem(name)
    if message:
        raise ValueError(message)
    master_index.set_app_setting(key, name)
    return display(capability)
