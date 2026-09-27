"""
Chequeo de estado de los conectores de datos (Oracle/BigQuery/SharePoint/
SAP/Graph) para el panel 'Conectores' del sidebar -- reemplazo funcional
del panel 'LSP' de OpenCode (que no aplica a Vision, no es un editor de
codigo). Cada `test_connection()` hace una llamada real minima (no es
decorativo), pero se corren en paralelo con un timeout corto para que el
panel no tarde en abrir si un sistema esta caido.
"""

from __future__ import annotations

import asyncio
import logging

from app.config import get_settings
from app.connectors import (
    BigQueryConnector,
    GraphConnector,
    OracleConnector,
    SapConnector,
    SharePointConnector,
)
from app.friendly_errors import describe_connector_error

logger = logging.getLogger(__name__)

CHECK_TIMEOUT = 8.0

# Nombres visibles de cada conector (los ids internos van en minuscula).
CONNECTOR_NAMES = {
    "oracle": "Oracle",
    "bigquery": "BigQuery",
    "sharepoint": "SharePoint",
    "sap": "SAP",
    "graph": "Microsoft Graph",
}


def _error(name: str, exc: BaseException, technical: str) -> dict:
    """`detail`: que paso y que hacer, en lenguaje simple (pedido del usuario
    2026-09-24, ver app/friendly_errors.py) -- es lo que muestran el panel y
    el tooltip. `technical`: el texto crudo recortado, para quien configura."""
    system = CONNECTOR_NAMES.get(name, name)
    return {
        "name": name,
        "status": "error",
        "detail": describe_connector_error(exc, system).message,
        "technical": technical,
    }


async def _check_one(name: str, fn) -> dict:
    try:
        ok = await asyncio.wait_for(asyncio.to_thread(fn), timeout=CHECK_TIMEOUT)
        return {"name": name, "status": "online" if ok else "error"}
    except asyncio.TimeoutError as exc:
        return _error(name, exc, "timeout")
    except Exception as exc:
        logger.warning("Chequeo de conector '%s' fallo: %s", name, exc)
        return _error(name, exc, str(exc)[:200])


async def check_all_connectors() -> list[dict]:
    settings = get_settings()
    checks = []

    if settings.enable_oracle:
        checks.append(("oracle", lambda: OracleConnector(settings).test_connection()))
    if settings.enable_bigquery:
        checks.append(
            ("bigquery", lambda: BigQueryConnector(settings).test_connection())
        )
    if settings.enable_sharepoint:
        checks.append(
            ("sharepoint", lambda: SharePointConnector(settings).test_connection())
        )
        checks.append(("graph", lambda: GraphConnector(settings).test_connection()))
    if settings.enable_sap:
        checks.append(("sap", lambda: SapConnector(settings).test_connection()))

    results = await asyncio.gather(*[_check_one(name, fn) for name, fn in checks])
    return list(results)
