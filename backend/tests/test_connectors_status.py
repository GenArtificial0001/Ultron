"""Unit de `app/connectors_status.py`: chequeo paralelo de conectores con
timeout. Los conectores reales se reemplazan por fakes (nunca red)."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from app import connectors_status as cs


def _fake(result=True, exc=None, delay=0.0):
    class Fake:
        def __init__(self, settings):
            pass

        def test_connection(self):
            if delay:
                time.sleep(delay)
            if exc:
                raise exc
            return result

    return Fake


def _settings(**flags):
    base = {
        "enable_oracle": False,
        "enable_bigquery": False,
        "enable_sharepoint": False,
        "enable_sap": False,
    }
    base.update(flags)
    return SimpleNamespace(**base)


def _patch(monkeypatch, settings, **fakes):
    monkeypatch.setattr(cs, "get_settings", lambda: settings)
    for name, cls in fakes.items():
        monkeypatch.setattr(cs, name, cls)


def test_all_disabled_returns_empty(monkeypatch):
    _patch(monkeypatch, _settings())
    assert asyncio.run(cs.check_all_connectors()) == []


def test_online_and_error_statuses(monkeypatch):
    _patch(
        monkeypatch,
        _settings(enable_oracle=True, enable_bigquery=True, enable_sap=True),
        OracleConnector=_fake(True),
        BigQueryConnector=_fake(False),
        SapConnector=_fake(exc=RuntimeError("x" * 500)),
    )
    result = {r["name"]: r for r in asyncio.run(cs.check_all_connectors())}
    assert result["oracle"] == {"name": "oracle", "status": "online"}
    assert result["bigquery"]["status"] == "error"
    assert result["sap"]["status"] == "error" and len(result["sap"]["technical"]) == 200  # truncado
    # Pedido del usuario 2026-09-24: lo que se muestra es entendible, no el error crudo.
    assert result["sap"]["detail"].startswith("SAP devolvió un error inesperado.")


def test_sharepoint_flag_checks_sharepoint_and_graph(monkeypatch):
    _patch(
        monkeypatch,
        _settings(enable_sharepoint=True),
        SharePointConnector=_fake(True),
        GraphConnector=_fake(True),
    )
    names = {r["name"] for r in asyncio.run(cs.check_all_connectors())}
    assert names == {"sharepoint", "graph"}


def test_timeout_is_reported_without_blocking_others(monkeypatch):
    monkeypatch.setattr(cs, "CHECK_TIMEOUT", 0.05)
    _patch(
        monkeypatch,
        _settings(enable_oracle=True, enable_bigquery=True),
        OracleConnector=_fake(delay=0.5),
        BigQueryConnector=_fake(True),
    )
    result = {r["name"]: r for r in asyncio.run(cs.check_all_connectors())}
    assert result["oracle"]["status"] == "error" and result["oracle"]["technical"] == "timeout"
    assert result["oracle"]["detail"].startswith("Oracle tardó demasiado en responder.")
    assert result["bigquery"]["status"] == "online"


@pytest.mark.parametrize("value", [True, False])
def test_check_one_maps_truthiness(value):
    assert asyncio.run(cs._check_one("x", lambda: value))["status"] == ("online" if value else "error")
