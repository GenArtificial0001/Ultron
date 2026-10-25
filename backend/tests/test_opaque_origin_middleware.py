"""spec navegador-integrado-ventana REQ-006: `OpaqueOriginMiddleware` rechaza
con 403 toda request a `/api/*` con `Origin: null` (iframe con sandbox sin
`allow-same-origin`). Se prueba sobre una app FastAPI minima, aislada."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import auth


def _client() -> TestClient:
    api = FastAPI()
    api.add_middleware(auth.OpaqueOriginMiddleware)

    @api.get("/api/datos")
    def datos() -> dict:
        return {"ok": True}

    @api.get("/browser/x/")
    def pagina() -> dict:
        return {"page": True}

    return TestClient(api)


def test_api_request_with_null_origin_is_rejected_with_403():
    r = _client().get("/api/datos", headers={"Origin": "null"})
    assert r.status_code == 403
    # Mensaje para personas, sin jerga tecnica.
    assert "aislada" in r.json()["detail"]


def test_api_request_without_origin_passes():
    assert _client().get("/api/datos").status_code == 200


def test_api_request_with_normal_origin_passes():
    r = _client().get("/api/datos", headers={"Origin": "http://127.0.0.1:8020"})
    assert r.status_code == 200


def test_non_api_paths_are_not_affected_by_null_origin():
    # El propio iframe aislado tiene que poder cargar su pagina.
    assert _client().get("/browser/x/", headers={"Origin": "null"}).status_code == 200


def test_middleware_is_registered_in_the_real_app():
    from app.main import app

    assert any(m.cls is auth.OpaqueOriginMiddleware for m in app.user_middleware)
