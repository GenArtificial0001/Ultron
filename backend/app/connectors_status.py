"""
Chequeo de estado de los conectores de datos para el panel 'Conectores' del
sidebar y la pestaña Conexiones -- reemplazo funcional del panel 'LSP' de
OpenCode (que no aplica a Vision, no es un editor de codigo). Cada
`test_connection()` hace una llamada real minima (no es decorativo), pero se
corren en paralelo con un timeout corto para que el panel no tarde en abrir
si un sistema esta caido.

spec conexiones-con-nombre (REQ-002): el chequeo es POR CONEXION, no por tipo.
Cada conexion con nombre (Oracle - Datamart, SharePoint - Sitio Logistico, ...)
sale con su propio estado, asi una caida de "Oracle - Planta" no pinta de rojo
a "Oracle - Datamart". Los tipos salen del registro de `app.connections`
(REQ-012): un tipo nuevo con `tester` aparece aca sin tocar este modulo.
BigQuery queda fuera de las conexiones con nombre (decision del usuario) y se
sigue chequeando con el `Settings` global.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

from app import connections, switches
from app.config import get_settings
from app.connectors import BigQueryConnector
from app.friendly_errors import describe_connector_error

logger = logging.getLogger(__name__)

CHECK_TIMEOUT = 8.0
# BigQuery es la mas lenta en frio: pide el token a Google y recien despues lista, todo por el proxy de la empresa. Medido el 2026-10-10 con la
# key del usuario: 3,4 s con la PC tranquila; con la PC cargada pasaba de los 8 s y salia «tardo demasiado» aunque la key anduviera.
BIGQUERY_CHECK_TIMEOUT = 20.0

# Nombres visibles de cada conector (los ids internos van en minuscula).
CONNECTOR_NAMES = {
    "oracle": "Oracle",
    "bigquery": "BigQuery",
    "sharepoint": "SharePoint",
    "sap": "SAP",
    "graph": "Microsoft Graph",
}


def _system_label(name: str) -> str:
    """Nombre de sistema para los mensajes: los tipos registrados traen su propia etiqueta."""
    ctype = connections.get_types().get(name)
    return ctype.label if ctype is not None else CONNECTOR_NAMES.get(name, name)


def _error(name: str, exc: BaseException, technical: str) -> dict:
    """`detail`: que paso y que hacer, en lenguaje simple (pedido del usuario
    2026-09-24, ver app/friendly_errors.py) -- es lo que muestran el panel y
    el tooltip. `technical`: el texto crudo recortado, para quien configura."""
    return {
        "name": name,
        "status": "error",
        "detail": describe_connector_error(exc, _system_label(name)).message,
        "technical": technical,
    }


class _WarningsOfThisThread(logging.Handler):
    """Junta los avisos (WARNING o mas) que deja el hilo que corre UN tester. Los conectores atrapan su propio error, lo dejan en el
    log y devuelven False: sin esto «Probar» y el panel solo sabian «no conecto» (pedido del usuario 2026-10-09: SAP fallaba por el
    proxy y la pantalla decia «No se pudo conectar.»). Se filtra por hilo: varios chequeos corren a la vez."""

    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.thread = threading.get_ident()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        if record.thread == self.thread:
            self.messages.append(record.getMessage())


def _run_tester(fn: Any) -> tuple[Any, str]:
    """Corre el tester (en su hilo) y devuelve (resultado, ultimo aviso que dejo)."""
    grab = _WarningsOfThisThread()
    target = logging.getLogger("app")  # los conectores loguean con `app.connectors.*`, que sube por "app"
    target.addHandler(grab)
    try:
        return fn(), (grab.messages[-1] if grab.messages else "")
    finally:
        target.removeHandler(grab)


async def _check_one(
    name: str, fn: Any, extra: dict | None = None, conn: connections.Connection | None = None, timeout: float | None = None
) -> dict:
    """`extra` (nombre y titulo de la conexion) se mezcla en el resultado, sea cual sea. Con `conn`, todo texto que sale hacia el navegador (`technical` y
    `detail`) se enmascara: un driver que falla puede traer la contrasena en su mensaje (REQ-004)."""
    try:
        limit = CHECK_TIMEOUT if timeout is None else timeout
        ok, last_warning = await asyncio.wait_for(asyncio.to_thread(_run_tester, fn), timeout=limit)
        result = {"name": name, "status": "online" if ok else "error"}
        if not ok and last_warning:  # el motivo, en lenguaje simple (abajo se enmascaran las contraseñas)
            result = _error(name, RuntimeError(last_warning), last_warning[:300])
    except asyncio.TimeoutError as exc:
        # antes un vencimiento no dejaba rastro: el 2026-10-08 BigQuery salio en rojo en el panel lateral y no se pudo confirmar por que
        logger.warning("Chequeo de conector '%s' (%s) vencio a los %.0f s", name, (extra or {}).get("display", name), limit)
        result = _error(name, exc, "timeout")
    except Exception as exc:
        # el log tambien sale enmascarado: un archivo de log se comparte con mas facilidad que la pantalla
        logger.warning("Chequeo de conector '%s' fallo: %s", name, connections.redact(conn, str(exc)) if conn else exc)
        result = _error(name, exc, str(exc)[:200])
    if conn is not None:
        for key in ("technical", "detail"):
            if key in result:
                result[key] = connections.redact(conn, str(result[key]))
    if extra:
        result.update(extra)
    return result


def _incomplete(conn: connections.Connection, missing: list[str]) -> dict:
    """Conexion con campos obligatorios vacios: se muestra en rojo SIN llamar al sistema (spec, categoria 6). Solo NOMBRES de campo, nunca valores."""
    return {
        "name": conn.type_id,
        "connection": conn.name,
        "display": conn.display,
        "status": "error",
        "detail": f"A esta conexion le faltan datos: {', '.join(missing)}. Completalos en connections.json.",
        "technical": "incomplete",
    }


async def test_draft_connection(type_id: str, fields: dict[str, str], base_fields: dict[str, str] | None = None, name: str = "") -> dict:
    """«Probar» ANTES de guardar (pedido del usuario 2026-10-10: «todos deberian tener un boton de probar antes de guardar»): prueba lo que se
    escribio en el formulario SIN guardar nada. `base_fields`: los datos guardados de la conexion que se esta editando; lo que no se toco (sobre
    todo las contraseñas, que la pantalla no tiene) sale de ahi. Mismo tester, mismo enmascarado y mismo motivo que el «Probar» de la tarjeta."""
    ctype = connections.get_types().get(type_id)
    if ctype is None or ctype.tester is None:
        return {"type": type_id, "ok": False, "detail": "Este tipo de conexion no se puede probar."}
    merged = {**(base_fields or {}), **fields}
    conn = connections.Connection(type_id=type_id, name=name.strip() or "borrador", fields=merged, origin="vision")
    missing = connections.missing_fields(conn)
    if missing:
        labels = {f.key: f.label for f in (*ctype.fields, *ctype.personal_fields)}
        return {"type": type_id, "ok": False, "detail": f"Faltan datos: {', '.join(labels.get(k, k) for k in missing)}."}
    conn_settings = connections.settings_for(conn)
    result = await _check_one(type_id, lambda: ctype.tester(conn_settings), {"connection": conn.name, "display": conn.display}, conn)
    return {
        "type": type_id,
        "ok": result["status"] == "online",
        "detail": result.get("detail", ""),
        "technical": result.get("technical", ""),
    }


# Lo que cambia A DONDE se conecta BigQuery; la key es la guardada (se importa y se guarda al instante, con su propio aviso).
BIGQUERY_DRAFT_KEYS = ("bq_project_id", "bq_location")


async def test_bigquery_draft(values: dict[str, str]) -> dict:
    """«Probar» de la seccion BigQuery antes de guardar (pedido del usuario 2026-10-10): con el proyecto y la ubicacion escritos sobre una COPIA de
    la configuracion (la que esta en uso no cambia) y la key guardada."""
    update = {k: v.strip() for k, v in values.items() if k in BIGQUERY_DRAFT_KEYS and v is not None and v.strip()}
    settings = get_settings().model_copy(update=update)
    result = await _check_one(
        "bigquery", lambda: BigQueryConnector(settings).test_connection(), {"display": connections.bigquery_display()}, timeout=BIGQUERY_CHECK_TIMEOUT
    )
    return {"type": "bigquery", "ok": result["status"] == "online", "detail": result.get("detail", ""), "technical": result.get("technical", "")}


async def check_all_connectors() -> list[dict]:
    """Estado de CADA conexion con nombre (y de BigQuery). Cada resultado trae `name` (el tipo, como siempre), `connection` y `display`."""
    settings = get_settings()
    checks: list[Any] = []
    ready: list[dict] = []

    off = switches.disabled_connection_keys()
    for conn in connections.list_connections():
        ctype = connections.get_types().get(conn.type_id)
        if ctype is None or ctype.tester is None or not connections.is_enabled(conn.type_id):
            continue
        if switches.connection_key(conn.type_id, conn.name) in off:
            continue  # apagada por el usuario: no se la prueba (ahorra una llamada de red) y no aparece en el sidebar (REQ-007)
        extra = {"connection": conn.name, "display": conn.display}
        missing = connections.missing_fields(conn)
        if missing:
            ready.append(_incomplete(conn, missing))
            continue
        conn_settings = connections.settings_for(conn)
        checks.append(_check_one(conn.type_id, lambda s=conn_settings, t=ctype.tester: t(s), extra, conn))

    if settings.enable_bigquery and switches.BIGQUERY_KEY not in off:
        checks.append(
            _check_one(
                "bigquery",
                lambda: BigQueryConnector(settings).test_connection(),
                {"display": connections.bigquery_display()},
                timeout=BIGQUERY_CHECK_TIMEOUT,
            )
        )

    results = await asyncio.gather(*checks)
    return ready + list(results)


async def test_one_connection(type_id: str, name: str | None) -> dict:
    """Prueba UNA conexion por nombre (boton «Probar» de su tarjeta). Lanza `ConnectionLookupError` si no existe."""
    conn = connections.resolve(type_id, name)
    ctype = connections.get_types()[type_id]
    if ctype.tester is None:
        return {
            "type": type_id,
            "connection": conn.name,
            "ok": False,
            "detail": "Este tipo de conexion no se puede probar.",
        }
    missing = connections.missing_fields(conn)
    if missing:
        return {"type": type_id, "connection": conn.name, "ok": False, "detail": f"Faltan datos: {', '.join(missing)}."}
    conn_settings = connections.settings_for(conn)
    result = await _check_one(
        type_id, lambda: ctype.tester(conn_settings), {"connection": conn.name, "display": conn.display}, conn
    )
    return {
        "type": type_id,
        "connection": conn.name,
        "ok": result["status"] == "online",
        "detail": result.get("detail", ""),
        "technical": result.get("technical", ""),
    }
