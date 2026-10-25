"""E2E de la carrera del interruptor «Sesion publica» (spec sesion-publica-privada, REQ-008).

Al abrir el modal, la app le pregunta al servidor el estado real de la sesion. Si la persona toca el interruptor ANTES de que llegue esa respuesta, su
eleccion tiene que mandar: la respuesta tardia no puede pisarla. Aca la respuesta se retiene a proposito con `page.route` para que el orden sea
determinista. Backend REAL; solo se controla CUANDO llega esa respuesta. Archivo aparte del de visibilidad para poder mutar sin mezclar corridas."""

from __future__ import annotations

import json

import pytest

from app.persistence import service as svc
from e2e_support import launch_chromium, open_app, serve_backend

pytestmark = pytest.mark.e2e

SWITCH = "#session-edit-public"


@pytest.fixture(scope="module")
def base_url():
    with serve_backend(None) as url:
        yield url


@pytest.fixture(scope="module")
def browser(tmp_path_factory, base_url):
    with launch_chromium(tmp_path_factory.mktemp("visrace"), base_url) as b:
        yield b


@pytest.fixture
def app(browser, base_url):
    with open_app(browser, base_url) as a:
        a.open()
        yield a


@pytest.fixture(autouse=True)
def tester_is_the_current_user(monkeypatch):
    monkeypatch.setenv("USERNAME", "tester")
    monkeypatch.setattr(svc, "current_windows_user", lambda: "tester")


def hold_visibility_answer(app, *, public: bool) -> list:
    """Retiene la respuesta de `GET .../visibility`; devuelve la lista donde queda la ruta retenida para soltarla despues."""
    held: list = []

    def handler(route):
        if route.request.method == "GET":
            held.append(route)
            return
        route.continue_()

    app.page.route("**/api/sessions/*/visibility", handler)
    app._public_answer = public  # noqa: SLF001 -- lo usa `release`
    return held


def release(app, held) -> None:
    held[0].fulfill(status=200, content_type="application/json", body=json.dumps({"public": app._public_answer}))  # noqa: SLF001


def open_modal_without_waiting(app) -> None:
    app.page.click("#meta-session")
    app.page.wait_for_selector("#session-edit-overlay:not(.hidden)")


def test_a_late_answer_does_not_overwrite_what_the_person_already_chose(app):
    held = hold_visibility_answer(app, public=False)
    open_modal_without_waiting(app)
    app.page.wait_for_function("() => true")
    app.page.wait_for_timeout(300)
    assert held, "la consulta del estado no salio"

    app.page.check(SWITCH)  # la persona elige PUBLICA antes de que llegue la respuesta
    release(app, held)  # el servidor dice «privada» (lo que habia antes)
    app.page.wait_for_function("() => document.getElementById('session-edit-public').dataset.loaded === 'true'")

    assert app.page.is_checked(SWITCH) is True, "la respuesta tardia pisó la eleccion de la persona"
    app.page.wait_for_selector("#session-edit-public-warning:not(.hidden)")


def test_without_touching_it_the_late_answer_is_applied(app):
    """Contrapartida: si la persona NO toco nada, la respuesta del servidor si se refleja."""
    held = hold_visibility_answer(app, public=True)
    open_modal_without_waiting(app)
    app.page.wait_for_timeout(300)
    assert held

    release(app, held)
    app.page.wait_for_function("() => document.getElementById('session-edit-public').dataset.loaded === 'true'")

    assert app.page.is_checked(SWITCH) is True
    app.page.wait_for_selector("#session-edit-public-warning:not(.hidden)")


def test_the_switch_is_not_marked_loaded_until_the_answer_arrives(app):
    held = hold_visibility_answer(app, public=False)
    open_modal_without_waiting(app)
    app.page.wait_for_timeout(300)
    assert held

    assert app.page.evaluate("() => document.getElementById('session-edit-public').dataset.loaded") == "false"
    release(app, held)
    app.page.wait_for_function("() => document.getElementById('session-edit-public').dataset.loaded === 'true'")


def test_a_late_answer_from_the_previous_session_does_not_paint_the_new_one(app):
    """Cierra el mutante «sin la guarda de sesion»: se abre el modal en la sesion A (su respuesta queda retenida), se cambia a la B, se reabre el modal
    (la B responde «privada») y RECIEN ahi llega la respuesta de A diciendo «publica»: no puede pisar el interruptor de B."""
    held: list = []

    def handler(route):
        if route.request.method != "GET":
            route.continue_()
        elif not held:
            held.append(route)  # solo la primera consulta (la de A) queda retenida
        else:
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"public": False}))

    app.page.route("**/api/sessions/*/visibility", handler)
    session_a = app.page.evaluate("() => document.getElementById('meta-session-id').textContent")

    open_modal_without_waiting(app)
    app.page.wait_for_timeout(300)
    assert held, "la consulta de la sesion A no salio"
    app.page.click("#session-edit-close")

    app.page.evaluate("document.getElementById('acc-session').open = true")
    app.page.click("#session-new-btn")
    app.page.wait_for_function("(a) => document.getElementById('meta-session-id').textContent !== a", arg=session_a)

    open_modal_without_waiting(app)
    app.page.wait_for_function("() => document.getElementById('session-edit-public').dataset.loaded === 'true'")
    assert app.page.is_checked(SWITCH) is False

    held[0].fulfill(status=200, content_type="application/json", body=json.dumps({"public": True}))  # llega tarde la de A
    app.page.wait_for_timeout(800)

    assert app.page.is_checked(SWITCH) is False, "la respuesta de la sesion anterior pinto el interruptor de la nueva"
