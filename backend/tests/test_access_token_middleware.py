"""Integracion de `AccessTokenMiddleware` (spec jarvis-mobile-pwa): unica
barrera cuando el backend se expone en la LAN. Se monta sobre una app FastAPI
MINIMA (no `app.main`) para probar el middleware aislado y determinista."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import auth


def _client(monkeypatch, token: str) -> TestClient:
    monkeypatch.setattr(auth, "get_settings", lambda: SimpleNamespace(access_token=token))
    api = FastAPI()
    api.add_middleware(auth.AccessTokenMiddleware)

    @api.get("/api/secret")
    def secret() -> dict:
        return {"ok": True}

    @api.get("/api/status")
    def status() -> dict:
        return {"status": "ok"}

    @api.get("/")
    def index() -> dict:
        return {"page": True}

    return TestClient(api)


def test_no_token_configured_leaves_api_open(monkeypatch):
    assert _client(monkeypatch, "").get("/api/secret").status_code == 200


def test_missing_token_is_rejected(monkeypatch):
    r = _client(monkeypatch, "s3cr3t").get("/api/secret")
    assert r.status_code == 401 and "Token" in r.json()["detail"]


def test_wrong_token_is_rejected(monkeypatch):
    r = _client(monkeypatch, "s3cr3t").get("/api/secret", headers={"X-Jarvis-Token": "otro"})
    assert r.status_code == 401


def test_header_token_is_accepted(monkeypatch):
    r = _client(monkeypatch, "s3cr3t").get("/api/secret", headers={"X-Jarvis-Token": "s3cr3t"})
    assert r.status_code == 200


def test_query_param_fallback_is_accepted(monkeypatch):
    assert _client(monkeypatch, "s3cr3t").get("/api/secret?token=s3cr3t").status_code == 200


@pytest.mark.parametrize("path", ["/api/status", "/"])
def test_exempt_routes_do_not_need_token(monkeypatch, path):
    assert _client(monkeypatch, "s3cr3t").get(path).status_code == 200
