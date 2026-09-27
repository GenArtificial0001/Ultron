"""
Conector propio de Vision a Oracle, modo THIN de `oracledb` (no requiere
Oracle Instant Client). Mismo patron que DBManager/app/connectors/oracle_connector.py
(conector independiente, sin llamar a DBManager por HTTP -- ver AGENTS.md).

Variables de entorno usadas (definidas en backend/.env / app.config.Settings):
    ORACLE_USER, ORACLE_PASSWORD, ORACLE_HOST, ORACLE_PORT,
    ORACLE_SERVICE_NAME, ORACLE_SCHEMA
"""

from __future__ import annotations

import logging
import threading
from typing import Any

import oracledb
import pandas as pd

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

# Investigacion de rendimiento 2026-09 ("Vision tarda mucho"): antes cada
# `OracleConnector()._connect()` abria una conexion TCP+auth NUEVA -- y
# `tools/dbquery.py` crea una instancia de `OracleConnector` por cada
# `oracle_query` tool-call, asi que CADA consulta pagaba el handshake
# completo de Oracle de cero. Un pool cacheado a nivel de MODULO (una vez
# por combinacion usuario/dsn, no por instancia/llamada) deja conexiones
# calientes listas para reusar -- mismo criterio que `llm/client.py` ya usa
# para cachear el cliente del proveedor de IA. `oracledb.ConnectionPool` es thread-safe
# (documentado), necesario porque ahora las tool-calls sync corren en
# threads separados via `asyncio.to_thread` (ver tools/base.py).
_pool_cache: dict[tuple[str, str], oracledb.ConnectionPool] = {}
_pool_lock = threading.Lock()


def _get_pool(settings: Settings) -> oracledb.ConnectionPool:
    key = (settings.oracle_user, settings.oracle_dsn)
    pool = _pool_cache.get(key)
    if pool is not None:
        return pool
    with _pool_lock:
        pool = _pool_cache.get(key)
        if pool is None:
            pool = oracledb.create_pool(
                user=settings.oracle_user,
                password=settings.oracle_password,
                dsn=settings.oracle_dsn,
                min=1,
                max=4,
                increment=1,
            )
            _pool_cache[key] = pool
    return pool


class OracleConnector:
    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()

    def _connect(self) -> oracledb.Connection:
        """Toma una conexion del pool cacheado (rapido, ya autenticada) en
        vez de conectar de cero. `with self._connect() as conn:` sigue
        funcionando igual -- para una conexion de pool, `close()`/salir del
        `with` la DEVUELVE al pool en vez de cerrar el socket (comportamiento
        documentado de oracledb), asi que ningun otro codigo necesita
        cambiar."""
        return _get_pool(self._settings).acquire()

    def test_connection(self) -> bool:
        try:
            with self._connect() as conn:
                cur = conn.cursor()
                cur.execute("SELECT 1 FROM dual")
                cur.fetchone()
            return True
        except Exception as exc:
            logger.warning("Oracle no disponible: %s", exc)
            return False

    def fetch_query(
        self, query: str, params: dict[str, Any] | None = None
    ) -> pd.DataFrame:
        """Ejecuta cualquier query SQL (SELECT) y devuelve un DataFrame."""
        with self._connect() as conn:
            return pd.read_sql(query, conn, params=params)

    def fetch_table(
        self, table_name: str, schema: str | None = None, limit: int | None = 200
    ) -> pd.DataFrame:
        schema = schema or self._settings.oracle_schema
        full_name = f"{schema}.{table_name}" if schema else table_name
        query = f"SELECT * FROM {full_name}"
        if limit:
            query = f"SELECT * FROM {full_name} FETCH FIRST {int(limit)} ROWS ONLY"
        return self.fetch_query(query)
