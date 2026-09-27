"""Investigacion de rendimiento 2026-09 ("Vision tarda mucho"): antes cada
OracleConnector conectaba de cero (TCP+auth) en cada query -- ahora usa un
pool cacheado a nivel de modulo (ver connectors/oracle_connector.py). Estos
tests confirman el caching SIN pegarle a un Oracle real: mockean
`oracledb.create_pool`."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from app.config import Settings
from app.connectors import oracle_connector as oc


def _settings(user="u1", host="h1") -> Settings:
    return Settings(oracle_user=user, oracle_host=host, oracle_port=1521, oracle_service_name="svc")


def setup_function(_fn):
    # El cache es un dict a nivel de modulo -- se limpia entre tests para
    # que no se pisen entre si (mismo criterio que un test de singleton).
    oc._pool_cache.clear()


def test_same_settings_reuse_the_same_pool():
    settings = _settings()
    with patch.object(oc.oracledb, "create_pool", return_value=MagicMock()) as mock_create:
        pool1 = oc._get_pool(settings)
        pool2 = oc._get_pool(settings)

    assert pool1 is pool2
    mock_create.assert_called_once()


def test_different_settings_create_separate_pools():
    with patch.object(oc.oracledb, "create_pool", side_effect=lambda **kw: MagicMock()) as mock_create:
        pool1 = oc._get_pool(_settings(user="u1"))
        pool2 = oc._get_pool(_settings(user="u2"))

    assert pool1 is not pool2
    assert mock_create.call_count == 2


def test_connector_acquire_uses_the_cached_pool():
    settings = _settings()
    fake_pool = MagicMock()
    with patch.object(oc.oracledb, "create_pool", return_value=fake_pool):
        connector = oc.OracleConnector(settings)
        connector._connect()
        connector._connect()

    assert fake_pool.acquire.call_count == 2
