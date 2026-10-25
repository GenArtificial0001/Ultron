"""Rutas `/api/proxy/*` de Configuracion > Red > Proxy (spec proxy-autoheal REQ-007/008/011).

Lo que se vigila:
1. Solo esta PC y la propia pagina de Vision: un pedido de otra PC se rechaza (403) ANTES de probar o cambiar nada (cambiar el proxy cambia por donde sale todo Vision).
2. `status` no sale a la red: muestra lo ultimo chequeado. `test` prueba UNA direccion y no cambia nada. `reconnect` corre el chequeo completo con cura. `apply` guarda y aplica lo elegido.
3. Una direccion que no es un proxy es 422 con frase para una persona; ninguna respuesta lleva una contrasena.
4. Las rutas jamas devuelven texto crudo de una falla interna."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import main
from app import proxy_health as ph

BASE = "http://127.0.0.1:8020"


@pytest.fixture
def api():
    return TestClient(main.app, base_url=BASE, client=("127.0.0.1", 50000))


@pytest.fixture(autouse=True)
def _clean_state():
    ph.reset_for_tests()
    yield
    ph.reset_for_tests()


ROUTES = [
    ("get", "/api/proxy/status", None),
    ("post", "/api/proxy/test", {"address": "http://10.1.1.1:83"}),
    ("post", "/api/proxy/reconnect", None),
    ("post", "/api/proxy/apply", {"address": "http://10.1.1.1:83"}),
]


@pytest.mark.parametrize(("method", "path", "body"), ROUTES)
def test_a_request_from_another_pc_is_rejected_before_doing_anything(monkeypatch, method: str, path: str, body):
    calls: list[str] = []
    monkeypatch.setattr(ph, "check", lambda *a, **k: calls.append("check"))
    monkeypatch.setattr(ph, "probe_one", lambda *a, **k: calls.append("probe"))
    monkeypatch.setattr(ph, "apply_choice", lambda *a, **k: calls.append("apply"))
    remote = TestClient(main.app, base_url=BASE, client=("10.20.30.40", 50000))

    resp = getattr(remote, method)(path, **({"json": body} if body else {}))

    assert resp.status_code == 403 and calls == []


@pytest.mark.parametrize(("method", "path", "body"), ROUTES)
def test_a_request_from_another_website_is_rejected(monkeypatch, api, method: str, path: str, body):
    monkeypatch.setattr(ph, "check", lambda *a, **k: pytest.fail("no debia ejecutarse"))
    monkeypatch.setattr(ph, "apply_choice", lambda *a, **k: pytest.fail("no debia ejecutarse"))

    resp = getattr(api, method)(
        path,
        headers={"Origin": "https://sitio-malo.example.com", "Sec-Fetch-Site": "cross-site"},
        **({"json": body} if body else {}),
    )

    assert resp.status_code == 403


def test_status_shows_the_last_check_without_going_out_to_the_network(monkeypatch, api):
    monkeypatch.setattr(ph, "check", lambda *a, **k: pytest.fail("status no chequea"))
    monkeypatch.setattr(ph, "_saved_proxy", lambda: "")

    resp = api.get("/api/proxy/status")

    assert resp.status_code == 200
    body = resp.json()
    assert body["candidates"] == [] and body["action"] is None and body["watching"] is False
    assert resp.headers["cache-control"] == "no-store"


def test_reconnect_runs_the_full_check_and_returns_the_snapshot(monkeypatch, api):
    seen = []

    def fake_check(persist=True):
        seen.append(persist)
        ph._STATE.update(
            result=ph.Result("healed", "ULTRON pasó a usar otra salida."),
            candidates=[ph.Candidate("dns", "http://10.1.1.1:83", True, 0.02)],
        )
        return ph._STATE["result"]

    monkeypatch.setattr(ph, "check", fake_check)
    monkeypatch.setattr(ph, "_saved_proxy", lambda: "")

    resp = api.post("/api/proxy/reconnect")

    assert resp.status_code == 200 and seen == [True]
    body = resp.json()
    assert body["action"] == "healed" and body["message"] == "ULTRON pasó a usar otra salida."
    assert body["candidates"][0] == {"source": "dns", "url": "http://10.1.1.1:83", "alive": True, "ms": 20}


def test_test_probes_one_address_and_answers_with_its_state(monkeypatch, api):
    monkeypatch.setattr(ph, "_tcp_alive", lambda host, port: 0.05 if (host, port) == ("10.1.1.1", 83) else None)

    ok = api.post("/api/proxy/test", json={"address": "10.1.1.1:83"}).json()
    dead = api.post("/api/proxy/test", json={"address": "10.9.9.9:83"}).json()

    assert ok["alive"] is True and ok["url"] == "http://10.1.1.1:83"
    assert dead["alive"] is False


@pytest.mark.parametrize("bad", ["esto no es una direccion", "ftp://x:21", "http://", "javascript:alert(1)"])
def test_an_unusable_address_is_a_422_with_plain_words(api, bad: str):
    for path in ("/api/proxy/test", "/api/proxy/apply"):
        resp = api.post(path, json={"address": bad})

        assert resp.status_code == 422
        assert "dirección" in resp.json()["detail"] and "Traceback" not in resp.json()["detail"]


def test_apply_saves_what_was_chosen_and_returns_the_snapshot(monkeypatch, api):
    seen = []
    monkeypatch.setattr(
        ph,
        "apply_choice",
        lambda address, force=False: seen.append((address, force)) or {"applied": True, "saved": True, "alive": True},
    )
    monkeypatch.setattr(ph, "_saved_proxy", lambda: "")

    resp = api.post("/api/proxy/apply", json={"address": "http://10.1.1.1:83"})

    assert resp.status_code == 200 and seen == [("http://10.1.1.1:83", False)]
    assert resp.json()["applied"] is True and resp.json()["saved"] is True and "candidates" in resp.json()


def test_apply_a_dead_address_is_not_applied_and_says_so(monkeypatch, api):
    monkeypatch.setattr(ph, "_tcp_alive", lambda host, port: None)
    monkeypatch.setattr(ph, "_saved_proxy", lambda: "")

    resp = api.post("/api/proxy/apply", json={"address": "http://10.9.9.9:83"})

    assert resp.status_code == 200 and resp.json()["applied"] is False and resp.json()["alive"] is False


def test_apply_can_be_forced_for_an_address_that_does_not_answer_yet(monkeypatch, api):
    seen = []
    monkeypatch.setattr(
        ph,
        "apply_choice",
        lambda address, force=False: seen.append(force) or {"applied": True, "saved": True, "alive": False},
    )
    monkeypatch.setattr(ph, "_saved_proxy", lambda: "")

    api.post("/api/proxy/apply", json={"address": "http://10.9.9.9:83", "force": True})

    assert seen == [True]


def test_force_must_be_a_real_boolean(api):
    """Un valor mal escrito («no», 0) no puede convertirse en «forzar»."""
    resp = api.post("/api/proxy/apply", json={"address": "http://10.1.1.1:83", "force": "no"})

    assert resp.status_code == 422


def test_an_internal_failure_never_leaks_raw_text(monkeypatch, api):
    def boom(*_a, **_k):
        raise RuntimeError("detalle-interno-secreto C:\\ruta\\interna")

    monkeypatch.setattr(ph, "check", boom)

    resp = api.post("/api/proxy/reconnect")

    assert resp.status_code == 500 and "detalle-interno-secreto" not in resp.text and "C:" not in resp.text


def test_no_response_carries_a_password(monkeypatch, api):
    monkeypatch.setattr(ph, "_saved_proxy", lambda: "http://usuario:clave-secreta@19.12.64.237:83")
    monkeypatch.setenv("HTTPS_PROXY", "http://usuario:clave-secreta@19.12.64.237:83")
    monkeypatch.setattr(ph, "_tcp_alive", lambda host, port: None)

    status = api.get("/api/proxy/status")
    tested = api.post("/api/proxy/test", json={"address": "http://usuario:clave-secreta@19.12.64.237:83"})

    assert "clave-secreta" not in status.text and "clave-secreta" not in tested.text


def test_the_watch_starts_in_the_background_at_startup(monkeypatch):
    started = []
    monkeypatch.setattr(ph, "start_watch", lambda: started.append(1) or True)

    main._start_proxy_watch()

    assert started == [1]


def test_a_failure_starting_the_watch_never_stops_startup(monkeypatch):
    def boom():
        raise RuntimeError("sin hilos")

    monkeypatch.setattr(ph, "start_watch", boom)

    main._start_proxy_watch()  # no lanza


def test_an_unusable_address_gets_exactly_the_plain_sentence_and_nothing_else(api):
    """Ni el texto de la excepcion ni su nombre llegan a la pantalla (E6)."""
    for path in ("/api/proxy/test", "/api/proxy/apply"):
        resp = api.post(path, json={"address": "esto no es una direccion"})

        assert resp.status_code == 422 and resp.json()["detail"] == main._PROXY_INVALID
