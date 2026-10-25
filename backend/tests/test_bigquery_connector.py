"""Conector de BigQuery (`app/connectors/bigquery_connector.py`, BL-006): credenciales de Service Account, cliente, listados, consulta y dry run.

Estaba en 27,7 % y lo usan `bigquery_query` y `bigquery_list_tables`. Lo que mas importa es el control de COSTO: `dry_run` estima los bytes sin
ejecutar (ni usar la cache de resultados) y `fetch_query` pasa `maximum_bytes_billed` para que el motor aborte si el costo real se pasa.
Sin red ni Google: `bigquery.Client` y las credenciales se reemplazan por falsos.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from app.config import Settings
from app.connectors import bigquery_connector as bq


class FakeJob:
    def __init__(self, df=None, bytes_processed=0):
        self._df, self.total_bytes_processed = df if df is not None else pd.DataFrame({"x": [1]}), bytes_processed

    def to_dataframe(self):
        return self._df


class FakeClient:
    instances: list = []

    def __init__(self, project=None, credentials=None, location=None):
        self.project, self.credentials, self.location = project, credentials, location
        self.queries: list = []
        self.datasets = [SimpleNamespace(dataset_id="ds_a"), SimpleNamespace(dataset_id="ds_b")]
        self.tables = [SimpleNamespace(table_id="t1"), SimpleNamespace(table_id="t2")]
        self.job = FakeJob()
        self.tables_asked: list = []
        self.fail_list = False
        FakeClient.instances.append(self)

    def list_datasets(self, max_results=None):
        if self.fail_list:
            raise RuntimeError("permiso denegado")
        return iter(self.datasets[:max_results] if max_results else self.datasets)

    def list_tables(self, dataset_ref):
        self.tables_asked.append(dataset_ref)
        return iter(self.tables)

    def query(self, query, job_config=None):
        self.queries.append((query, job_config))
        return self.job


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    FakeClient.instances = []
    monkeypatch.setattr(bq.bigquery, "Client", FakeClient)
    monkeypatch.setattr(bq.service_account.Credentials, "from_service_account_file", staticmethod(lambda path: f"CRED({Path(path).name})"))
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    yield
    os.environ.pop("GOOGLE_APPLICATION_CREDENTIALS", None)


def make(tmp_path, with_key=True, **kw):
    key = tmp_path / "key.json"
    if with_key:
        key.write_text("{}", encoding="utf-8")
    cfg = Settings(bq_project_id="proj", bq_dataset_id="ds_def", bq_table_id="tbl_def", bq_location="US", google_application_credentials=str(key), **kw)
    return bq.BigQueryConnector(cfg)


class TestKeyStoredInVision:
    """spec gestor-conexiones REQ-008: la key importada desde la pantalla vive CIFRADA en vision.db y llega como `settings.bq_key_json`: se usa directo, sin archivo."""

    KEY = '{"type": "service_account", "project_id": "psapmpldash-e-s-110", "client_email": "sa@proyecto.iam.gserviceaccount.com"}'

    @pytest.fixture(autouse=True)
    def from_info(self, monkeypatch):
        seen: list = []
        monkeypatch.setattr(bq.service_account.Credentials, "from_service_account_info", staticmethod(lambda info: seen.append(info) or f"INFO({info['project_id']})"))
        return seen

    def test_the_stored_key_is_used_without_any_file(self, tmp_path, from_info):
        c = make(tmp_path, with_key=False, bq_key_json=self.KEY)._get_client()

        assert c.credentials == "INFO(psapmpldash-e-s-110)" and from_info[0]["client_email"].startswith("sa@")

    def test_with_the_stored_key_no_key_path_is_announced_to_google_libraries(self, tmp_path):
        make(tmp_path, with_key=False, bq_key_json=self.KEY)._get_client()

        assert "GOOGLE_APPLICATION_CREDENTIALS" not in os.environ

    def test_the_stored_key_wins_over_an_existing_file(self, tmp_path):
        c = make(tmp_path, with_key=True, bq_key_json=self.KEY)._get_client()

        assert c.credentials.startswith("INFO(")

    @pytest.mark.parametrize("broken", ["{ no es json", '["lista"]', '"texto"'])
    def test_a_broken_stored_key_is_a_clear_error_and_never_falls_back_to_the_file(self, tmp_path, broken):
        conn = make(tmp_path, with_key=True, bq_key_json=broken)

        with pytest.raises(ValueError, match="volvé a importarla"):
            conn._get_client()
        assert FakeClient.instances == []

    def test_without_a_stored_key_the_file_works_as_always(self, tmp_path, from_info):
        c = make(tmp_path, bq_key_json="")._get_client()

        assert c.credentials == "CRED(key.json)" and from_info == []


class TestCredentialsAndClient:
    def test_it_builds_the_client_with_project_credentials_and_location(self, tmp_path):
        c = make(tmp_path)._get_client()

        assert (c.project, c.location, c.credentials) == ("proj", "US", "CRED(key.json)")

    def test_the_client_is_created_once_per_connector(self, tmp_path):
        conn = make(tmp_path)
        conn._get_client()
        conn._get_client()

        assert len(FakeClient.instances) == 1

    def test_a_missing_key_file_explains_how_to_fix_it(self, tmp_path):
        conn = make(tmp_path, with_key=False)

        with pytest.raises(FileNotFoundError, match=r"No se encontro la key de BigQuery.*key\.json.*Configuracion > Credenciales"):
            conn._get_client()
        assert FakeClient.instances == []  # y no se llega a crear el cliente

    def test_it_exports_the_key_path_for_google_libraries_without_overriding_an_existing_one(self, tmp_path, monkeypatch):
        make(tmp_path)._get_client()
        assert os.environ["GOOGLE_APPLICATION_CREDENTIALS"].endswith("key.json")

        monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "otra/ruta.json")
        make(tmp_path)._get_client()
        assert os.environ["GOOGLE_APPLICATION_CREDENTIALS"] == "otra/ruta.json"  # setdefault: no pisa lo que ya estaba


class TestConnectionAndListings:
    def test_test_connection_is_true_when_google_answers(self, tmp_path):
        assert make(tmp_path).test_connection() is True

    def test_test_connection_is_false_instead_of_raising_when_google_fails(self, tmp_path):
        conn = make(tmp_path)
        conn._get_client().fail_list = True

        assert conn.test_connection() is False

    def test_test_connection_is_false_without_a_key(self, tmp_path):
        assert make(tmp_path, with_key=False).test_connection() is False

    def test_it_asks_for_a_single_dataset_only(self, tmp_path):
        conn = make(tmp_path)
        seen = []
        client = conn._get_client()
        original = client.list_datasets
        client.list_datasets = lambda max_results=None: (seen.append(max_results), original(max_results))[1]

        conn.test_connection()

        assert seen == [1]  # un probe barato, no baja todos los datasets

    def test_list_datasets_returns_the_ids(self, tmp_path):
        assert make(tmp_path).list_datasets() == ["ds_a", "ds_b"]

    def test_list_tables_uses_the_default_dataset_with_the_project_prefix(self, tmp_path):
        conn = make(tmp_path)

        assert conn.list_tables() == ["t1", "t2"]
        assert conn._get_client().tables_asked == ["proj.ds_def"]

    def test_list_tables_accepts_another_dataset(self, tmp_path):
        conn = make(tmp_path)
        conn.list_tables("otro")

        assert conn._get_client().tables_asked == ["proj.otro"]


class TestQueries:
    def test_fetch_query_returns_the_dataframe_and_sends_no_job_config_by_default(self, tmp_path):
        conn = make(tmp_path)
        conn._get_client().job = FakeJob(pd.DataFrame({"a": [1, 2]}))

        df = conn.fetch_query("SELECT 1")

        assert list(df["a"]) == [1, 2]
        assert conn._get_client().queries == [("SELECT 1", None)]

    def test_fetch_query_caps_the_billed_bytes_when_asked(self, tmp_path):
        """Segunda capa de proteccion de costo: el motor aborta el job si el costo REAL supera el limite, aunque el dry run haya estimado menos."""
        conn = make(tmp_path)

        conn.fetch_query("SELECT 1", maximum_bytes_billed=123_456)

        job_config = conn._get_client().queries[0][1]
        assert job_config.maximum_bytes_billed == 123_456

    def test_a_zero_cap_is_still_sent(self, tmp_path):
        """`0` es un limite valido (bloquea todo cobro): no se confunde con 'sin limite'."""
        conn = make(tmp_path)

        conn.fetch_query("SELECT 1", maximum_bytes_billed=0)

        assert conn._get_client().queries[0][1].maximum_bytes_billed == 0

    def test_dry_run_estimates_without_executing_or_using_the_result_cache(self, tmp_path):
        conn = make(tmp_path)
        conn._get_client().job = FakeJob(bytes_processed=5_000_000)

        assert conn.dry_run("SELECT * FROM t") == 5_000_000
        job_config = conn._get_client().queries[0][1]
        assert job_config.dry_run is True and job_config.use_query_cache is False

    def test_dry_run_with_no_estimate_is_zero_not_none(self, tmp_path):
        conn = make(tmp_path)
        conn._get_client().job = FakeJob(bytes_processed=None)

        assert conn.dry_run("SELECT 1") == 0

    def test_fetch_table_uses_the_default_table_and_the_default_limit(self, tmp_path):
        conn = make(tmp_path)

        conn.fetch_table()

        assert conn._get_client().queries[0][0] == "SELECT * FROM `proj.ds_def.tbl_def` LIMIT 1000"

    def test_fetch_table_accepts_dataset_table_and_limit(self, tmp_path):
        conn = make(tmp_path)

        conn.fetch_table("d2", "t2", limit=5)

        assert conn._get_client().queries[0][0] == "SELECT * FROM `proj.d2.t2` LIMIT 5"

    def test_fetch_table_without_a_limit_reads_everything(self, tmp_path):
        conn = make(tmp_path)

        conn.fetch_table(limit=None)

        assert conn._get_client().queries[0][0] == "SELECT * FROM `proj.ds_def.tbl_def`"
