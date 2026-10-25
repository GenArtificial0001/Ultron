"""
Conector propio de Vision a Google BigQuery, autenticado con Service
Account (archivo JSON permanente). Mismo patron que
DBManager/app/connectors/bigquery_connector.py, independiente.

Variables de entorno usadas:
    BQ_PROJECT_ID, BQ_DATASET_ID, BQ_TABLE_ID,
    GOOGLE_APPLICATION_CREDENTIALS, BQ_LOCATION
"""

from __future__ import annotations

import json
import logging
import os

import pandas as pd
from google.cloud import bigquery
from google.oauth2 import service_account

from app.config import BASE_DIR, Settings, get_settings

logger = logging.getLogger(__name__)


class BigQueryConnector:
    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._client: bigquery.Client | None = None

    def _key_info(self) -> dict | None:
        """spec gestor-conexiones REQ-008: la key importada desde la pantalla (guardada CIFRADA en vision.db, aplicada como `settings.bq_key_json`). None = no hay:
        se usa el archivo de siempre. Una key guardada que no es JSON valido es un error claro, nunca un salto silencioso al archivo."""
        raw = str(getattr(self._settings, "bq_key_json", "") or "").strip()
        if not raw:
            return None
        try:
            info = json.loads(raw)
        except ValueError as exc:
            raise ValueError("La key de BigQuery guardada en Vision no es un archivo de clave valido: volvé a importarla en Conexiones.") from exc
        if not isinstance(info, dict):
            raise ValueError("La key de BigQuery guardada en Vision no es un archivo de clave valido: volvé a importarla en Conexiones.")
        return info

    def _get_credentials(self):
        info = self._key_info()
        if info is not None:
            return service_account.Credentials.from_service_account_info(info)  # sin archivo en disco
        cred_path = self._settings.bq_credentials_path
        if not cred_path.exists():
            from app import config_store  # import diferido: config_store importa config y connections

            if config_store.is_active():  # spec gestor-conexiones: sin .env; la key se importa en el gestor (pedido del usuario 2026-10-09)
                raise FileNotFoundError(
                    "Todavía no hay una clave de BigQuery: importá el archivo .json de la cuenta de servicio en "
                    "Configuración > Conexiones > BigQuery."
                )
            raise FileNotFoundError(
                f"No se encontro la key de BigQuery en: {cred_path}. "
                "Genera una Service Account key y colocala en esa ruta, o cargala "
                "desde Configuracion > Credenciales (la ruta sale de "
                f"GOOGLE_APPLICATION_CREDENTIALS en {BASE_DIR / '.env'}; si es "
                f"relativa, se toma desde {BASE_DIR})."
            )
        return service_account.Credentials.from_service_account_file(str(cred_path))

    def _get_client(self) -> bigquery.Client:
        if self._client is not None:
            return self._client
        credentials = self._get_credentials()
        if self._key_info() is None:
            # solo con el archivo: con la key de la base no hay ruta que anunciar (y una ruta inexistente confundiria a otras librerias de Google)
            os.environ.setdefault(
                "GOOGLE_APPLICATION_CREDENTIALS", str(self._settings.bq_credentials_path)
            )
        self._client = bigquery.Client(
            project=self._settings.bq_project_id,
            credentials=credentials,
            location=self._settings.bq_location,
        )
        return self._client

    def test_connection(self) -> bool:
        try:
            client = self._get_client()
            next(iter(client.list_datasets(max_results=1)), None)
            return True
        except Exception as exc:
            logger.warning("BigQuery no disponible: %s", exc)
            return False

    def list_datasets(self) -> list[str]:
        client = self._get_client()
        return [ds.dataset_id for ds in client.list_datasets()]

    def list_tables(self, dataset_id: str | None = None) -> list[str]:
        client = self._get_client()
        dataset = dataset_id or self._settings.bq_dataset_id
        dataset_ref = f"{self._settings.bq_project_id}.{dataset}"
        return [tbl.table_id for tbl in client.list_tables(dataset_ref)]

    def fetch_table(
        self,
        dataset_id: str | None = None,
        table_id: str | None = None,
        limit: int | None = 1000,
    ) -> pd.DataFrame:
        full_ref = self._settings.bq_full_table_ref(dataset_id, table_id)
        query = f"SELECT * FROM `{full_ref}`"
        if limit:
            query += f" LIMIT {int(limit)}"
        return self.fetch_query(query)

    def fetch_query(
        self, query: str, maximum_bytes_billed: int | None = None
    ) -> pd.DataFrame:
        """REQ-002 (spec controles-seguridad-optimizacion-lightspeed-bigquery):
        `maximum_bytes_billed` es la segunda capa de proteccion de costo --
        ademas del guardrail de dry run que ya pauso ANTES de llegar aca
        (ver dbquery.py), este parametro le pide al motor de BigQuery que
        aborte el job con un error si el costo real termina superando el
        limite (defensa en profundidad ante el caso raro de que el plan
        estimado por el dry run difiera del costo real de ejecucion)."""
        client = self._get_client()
        job_config = None
        if maximum_bytes_billed is not None:
            job_config = bigquery.QueryJobConfig(
                maximum_bytes_billed=maximum_bytes_billed
            )
        return client.query(query, job_config=job_config).to_dataframe()

    def dry_run(self, query: str) -> int:
        """REQ-002: corre la consulta en modo `dry_run=True` -- BigQuery NO
        escanea ni devuelve datos, solo valida el plan y estima cuantos
        bytes procesaria una ejecucion real. Se usa ANTES de `fetch_query`
        para decidir si hace falta pausar y pedir confirmacion humana por
        costo (ver `_bigquery_dry_run_check` en app/tools/dbquery.py)."""
        client = self._get_client()
        job_config = bigquery.QueryJobConfig(dry_run=True, use_query_cache=False)
        job = client.query(query, job_config=job_config)
        return int(job.total_bytes_processed or 0)
