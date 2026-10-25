"""Rutas `/api/m365/*` (spec office-outlook-teams-mcp REQ-M02, REQ-M09, REQ-M11, REQ-M12): lo que recibe y ve la pagina.

El login real abre el navegador y espera a la persona: aca `service` usa un `runner` falso. Se prueba por HTTP, igual que lo usa la pantalla. Lo que se vigila:
1. Estado: dice sin sesion, esperando, conectado o con error, y NUNCA lleva el token.
2. Iniciar y cerrar sesion CAMBIAN algo de la persona (abren el navegador, borran el token): un pedido de otra PC, de otra pagina o de un sitio ajeno se rechaza ANTES de tocar nada,
   igual que la pantalla de contraseñas y el audio (`secret_access.local_page_problem`).
3. El retorno del navegador (`/callback`) responde una pagina simple para la persona, solo cuenta si coincide con el login en curso y nunca muestra el codigo recibido.
4. Los mensajes de error son para una persona, no para soporte."""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from app import main, secret_store
from app.m365 import service, token_store

pytestmark = pytest.mark.skipif(not secret_store.is_available(), reason="DPAPI solo existe en Windows")

BASE = "http://127.0.0.1:8020"
ACCESS = "ACCESS-TOKEN-SECRETO-3318"


@pytest.fixture(autouse=True)
def _fresh_service():
    service.reset_for_tests()
    yield
    service.reset_for_tests()


@pytest.fixture
def api():
    return TestClient(main.app, base_url=BASE, client=("127.0.0.1", 50000))


def _save_token():
    from mcp.shared.auth import OAuthToken

    asyncio.run(
        token_store.M365TokenStorage().set_tokens(
            OAuthToken(access_token=ACCESS, token_type="Bearer", expires_in=4471, refresh_token="R" * 20)
        )
    )


def test_the_status_without_a_session_says_not_connected(api):
    resp = api.get("/api/m365/status")

    assert resp.status_code == 200
    assert resp.json()["state"] == "disconnected" and resp.json()["connected"] is False


def test_the_status_with_a_session_says_connected_and_never_shows_the_token(api):
    _save_token()

    resp = api.get("/api/m365/status")

    assert resp.json()["connected"] is True
    assert ACCESS not in resp.text


def test_signing_out_through_the_page_deletes_the_token(api):
    _save_token()

    resp = api.post("/api/m365/logout")

    assert resp.status_code == 200 and resp.json()["state"] == "disconnected"
    assert token_store.has_session() is False


def test_signing_out_twice_is_harmless(api):
    assert api.post("/api/m365/logout").status_code == 200
    assert api.post("/api/m365/logout").status_code == 200


def test_login_starts_in_the_background_and_reports_waiting(api, monkeypatch):
    started = []

    async def fake_start(port, runner=None):
        started.append(port)
        return {"state": "waiting", "connected": False, "message": "", "code": ""}

    monkeypatch.setattr(service, "start_login", fake_start)

    resp = api.post("/api/m365/login")

    assert resp.status_code == 200 and resp.json()["state"] == "waiting"
    assert started == [8020], "la direccion de retorno tiene que usar el puerto real de la pagina"


def test_login_uses_the_port_the_page_was_opened_on(monkeypatch):
    """Si la persona cambio el puerto de Vision, la direccion de retorno del login tiene que ser la de ese puerto."""
    seen = []

    async def fake_start(port, runner=None):
        seen.append(port)
        return {"state": "waiting", "connected": False, "message": "", "code": ""}

    monkeypatch.setattr(service, "start_login", fake_start)
    client = TestClient(main.app, base_url="http://127.0.0.1:9100", client=("127.0.0.1", 50000))

    client.post("/api/m365/login")

    assert seen == [9100]


# ---------------------------------------------------------------------------------------------------------------------
# Seguridad: solo esta PC y la propia pagina
# ---------------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("method", "path"),
    [("get", "/api/m365/status"), ("post", "/api/m365/login"), ("post", "/api/m365/logout")],
)
def test_a_request_from_another_pc_is_rejected_before_doing_anything(method: str, path: str):
    remote = TestClient(main.app, base_url=BASE, client=("10.20.30.40", 50000))
    _save_token()

    resp = getattr(remote, method)(path)

    assert resp.status_code == 403
    assert token_store.has_session() is True, "un pedido de otra PC no puede tocar la sesion"
    assert service.status()["state"] == "connected"


@pytest.mark.parametrize(("method", "path"), [("post", "/api/m365/login"), ("post", "/api/m365/logout")])
def test_a_request_from_another_website_is_rejected(api, method: str, path: str):
    _save_token()

    resp = getattr(api, method)(path, headers={"Origin": "https://sitio-ajeno.com", "Sec-Fetch-Site": "cross-site"})

    assert resp.status_code == 403
    assert token_store.has_session() is True


def test_the_rejection_message_is_for_a_person(api):
    resp = api.post("/api/m365/logout", headers={"Origin": "https://sitio-ajeno.com"})

    detail = resp.json()["detail"]
    assert "ULTRON" in detail and "Traceback" not in detail and "403" not in detail


# ---------------------------------------------------------------------------------------------------------------------
# El retorno del navegador
# ---------------------------------------------------------------------------------------------------------------------
def test_the_callback_hands_the_code_to_the_login_in_progress_and_shows_a_friendly_page(api):
    delivered = []
    original = service.deliver_callback
    service.deliver_callback = lambda code, state: delivered.append((code, state)) or True
    try:
        resp = api.get("/api/m365/callback", params={"code": "el-codigo-123", "state": "estado-1"})
    finally:
        service.deliver_callback = original

    assert resp.status_code == 200 and "text/html" in resp.headers["content-type"]
    assert delivered == [("el-codigo-123", "estado-1")]
    assert "el-codigo-123" not in resp.text, "la pagina no puede repetir el codigo recibido"
    assert "cerrar" in resp.text.lower()


def test_a_callback_that_does_not_match_a_login_shows_a_page_that_says_so_and_changes_nothing(api):
    resp = api.get("/api/m365/callback", params={"code": "x", "state": "no-es-mio"})

    assert resp.status_code == 200
    assert "no" in resp.text.lower() and service.status()["state"] == "disconnected"


def test_a_callback_that_reports_a_refusal_is_passed_on_and_explained(api):
    failed = []
    original = service.deliver_error
    service.deliver_error = lambda state, reason: failed.append((state, reason)) or True
    try:
        resp = api.get("/api/m365/callback", params={"error": "access_denied", "state": "estado-1"})
    finally:
        service.deliver_error = original

    assert resp.status_code == 200 and failed == [("estado-1", "access_denied")]
    assert "access_denied" not in resp.text, "el motivo crudo no se le muestra a la persona"


def test_a_refusal_that_belongs_to_the_login_in_progress_says_it_could_not_sign_in(api):
    original = service.deliver_error
    service.deliver_error = lambda state, reason: True
    try:
        resp = api.get("/api/m365/callback", params={"error": "access_denied", "state": "estado-1"})
    finally:
        service.deliver_error = original

    assert "No se pudo iniciar sesión" in resp.text and "no corresponde" not in resp.text.lower()


def test_a_refusal_that_does_not_belong_to_any_login_does_not_pretend_it_did(api):
    """Sin login en curso, un `error=...` ajeno cae en la pagina de «no corresponde»: no le dice a la persona que fallo un inicio de sesion que ella nunca hizo."""
    resp = api.get("/api/m365/callback", params={"error": "access_denied", "state": "ajeno"})

    assert "No se pudo iniciar sesión" not in resp.text and "no corresponde" in resp.text.lower()


def test_a_callback_with_html_in_its_parameters_does_not_inject_it_into_the_page(api):
    resp = api.get(
        "/api/m365/callback", params={"code": "<script>alert(1)</script>", "state": "<img src=x onerror=alert(1)>"}
    )

    assert "<script>alert(1)</script>" not in resp.text and "onerror=alert(1)" not in resp.text


def test_the_callback_only_accepts_the_local_page_too(monkeypatch):
    """El navegador de la persona vuelve desde el sitio de la empresa hacia 127.0.0.1: el pedido llega de SU PC, asi que el `Origin` puede ser el de la empresa; lo que no puede ser es otra PC."""
    remote = TestClient(main.app, base_url=BASE, client=("10.20.30.40", 50000))

    resp = remote.get("/api/m365/callback", params={"code": "x", "state": "y"})

    assert resp.status_code == 403
