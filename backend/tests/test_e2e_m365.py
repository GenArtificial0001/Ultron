"""E2E de la conexion «Outlook y Teams» en Configuracion > Conexiones (spec office-outlook-teams-mcp REQ-M09, REQ-M11, REQ-M14) en un Chromium REAL contra el backend en loopback.

Decision del usuario (2026-10-17): el inicio de sesion se hace DESDE Conexiones, con un boton («desde la opcion conexiones es la mejor manera, ya que tenemos la posibilidad de presionar un
boton»). Vision NO muestra avisos ni abre el navegador por su cuenta. Lo que se ve y se hace:
1. El menu «Conectores» trae «Outlook y Teams» y su ventana dice en que estado esta (sin sesion, esperando, conectado, con error) con UN boton de la accion que corresponde.
2. Tocar «Iniciar sesion» le pide al backend que empiece (el login real abre el navegador: aca el servicio se reemplaza) y la ventana pasa sola a «Esperando...» y despues a «Conectado».
3. «Cerrar sesion» borra la sesion y la ventana vuelve a pedir iniciar.
4. Un interruptor apaga y prende la conexion sin tocar la sesion.
5. Un error del login se muestra en lenguaje claro; el token no aparece nunca en pantalla.
6. Abrir la pantalla NO inicia nada por si sola (ninguna llamada a /api/m365/login hasta que la persona toca el boton)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app import config
from app import config_store as cs
from app import connections as conns
from app import credentials, secret_store
from app.m365 import service, token_store
from e2e_support import FakeWhisper, launch_chromium, open_app, serve_backend

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(not secret_store.is_available(), reason="DPAPI solo existe en Windows"),
]

MODEL = "claude-sonnet-4-6"
ACCESS = "ACCESS-TOKEN-SECRETO-9921"


class Provider:
    def __init__(self):
        self.whisper = FakeWhisper()
        self.audio = SimpleNamespace(transcriptions=self.whisper)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=lambda **k: iter([])))

    def get(self, path, cast_to=object):
        return {"data": [{"id": MODEL, "type": "LLM", "aliases": [], "valid_args": [], "reasoning_levels": []}]}


@pytest.fixture(scope="module")
def base_url():
    with serve_backend(Provider()) as url:
        yield url


@pytest.fixture(scope="module")
def browser(tmp_path_factory, base_url):
    with launch_chromium(tmp_path_factory.mktemp("audio"), base_url) as b:
        yield b


@pytest.fixture(autouse=True)
def clean_state(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "BASE_DIR", tmp_path)
    monkeypatch.setattr(credentials, "ENC_FILE", tmp_path / ".env.enc")
    monkeypatch.setattr(conns, "CONNECTIONS_FILE", tmp_path / "connections.json")
    monkeypatch.setitem(config.Settings.model_config, "env_file", str(tmp_path / ".env"))
    conns.reset_for_tests()
    cs.invalidate()
    service.reset_for_tests()
    yield
    service.reset_for_tests()
    conns.reset_for_tests()
    cs.invalidate()
    config.get_settings.cache_clear()


@pytest.fixture()
def app(browser, base_url):
    with open_app(browser, base_url) as a:
        a.open()
        yield a


def _run_async(coro):
    """Playwright (API sincrona) deja un bucle de eventos corriendo en este hilo: `asyncio.run` ahi falla. Se corre en un hilo aparte."""
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def _save_token():
    from mcp.shared.auth import OAuthToken

    _run_async(
        token_store.M365TokenStorage().set_tokens(
            OAuthToken(access_token=ACCESS, token_type="Bearer", expires_in=4471, refresh_token="R" * 20)
        )
    )


def open_m365(app):
    app.page.click("#settings-btn")
    app.page.click('.settings-tab[data-tab="connections"]')
    app.page.wait_for_selector("#vault-root .vault-head", timeout=20_000)
    app.page.click("#vault-menu-btn")
    app.page.click('.vault-menu-item[data-connector="m365"]')
    app.page.wait_for_selector("#vault-modal", timeout=10_000)
    # La ventana se abre YA, pero el estado llega despues (consulta al servidor): se espera a que se dibuje.
    app.page.wait_for_selector("#m365-card", timeout=10_000)


@pytest.fixture
def login_calls(monkeypatch):
    """Cuenta cuantas veces el backend recibio la orden de iniciar sesion. El login real abre el navegador y espera a la persona: aca solo se registra."""
    calls = []

    async def fake_start(port, runner=None):
        calls.append(port)
        service._state.update({"phase": "waiting", "message": "", "code": ""})
        return service.status()

    monkeypatch.setattr(service, "start_login", fake_start)
    return calls


def test_the_connectors_menu_offers_outlook_and_teams(app):
    app.page.click("#settings-btn")
    app.page.click('.settings-tab[data-tab="connections"]')
    app.page.wait_for_selector("#vault-root .vault-head", timeout=20_000)
    app.page.click("#vault-menu-btn")

    assert app.page.locator('.vault-menu-item[data-connector="m365"]').inner_text() == "Outlook y Teams"


def test_without_a_session_the_window_says_so_and_offers_a_single_sign_in_button(app, login_calls):
    open_m365(app)

    text = app.page.inner_text("#vault-modal")
    assert "Sin sesión" in text
    assert app.page.locator("#m365-login-btn").is_visible()
    assert app.page.locator("#m365-logout-btn").count() == 0, "sin sesion no hay nada que cerrar"
    assert login_calls == [], "abrir la ventana no inicia nada por si sola"


def test_opening_the_connections_screen_never_starts_a_login_by_itself(app, login_calls):
    """Decision del usuario: nada de avisos ni navegador automatico. Solo el boton inicia sesion."""
    app.page.click("#settings-btn")
    app.page.click('.settings-tab[data-tab="connections"]')
    app.page.wait_for_selector("#vault-root .vault-head", timeout=20_000)
    app.page.wait_for_timeout(1500)

    assert login_calls == []


def test_touching_sign_in_asks_the_backend_to_start_and_shows_waiting(app, login_calls):
    open_m365(app)

    app.page.click("#m365-login-btn")
    app.page.wait_for_function("document.querySelector('#vault-modal').innerText.includes('Esperando')", timeout=10_000)

    assert len(login_calls) == 1
    assert app.page.locator("#m365-login-btn").count() == 0 or app.page.locator("#m365-login-btn").is_disabled()


def test_the_window_turns_to_connected_by_itself_when_the_login_finishes(app, login_calls):
    open_m365(app)
    app.page.click("#m365-login-btn")
    app.page.wait_for_function("document.querySelector('#vault-modal').innerText.includes('Esperando')", timeout=10_000)

    _save_token()  # la persona termino de entrar con su cuenta de la empresa en el navegador
    service._state.update({"phase": "idle", "message": "", "code": ""})
    app.page.wait_for_function("document.querySelector('#vault-modal').innerText.includes('Conectado')", timeout=15_000)

    assert app.page.locator("#m365-logout-btn").is_visible()
    assert ACCESS not in app.page.inner_text("#vault-modal"), "el token no se muestra nunca"


def test_with_a_session_the_window_says_connected_and_offers_sign_out(app, login_calls):
    _save_token()
    open_m365(app)

    assert "Conectado" in app.page.inner_text("#vault-modal")
    assert app.page.locator("#m365-logout-btn").is_visible() and app.page.locator("#m365-login-btn").count() == 0
    assert ACCESS not in app.page.content()


def test_signing_out_deletes_the_session_and_the_window_asks_to_sign_in_again(app, login_calls):
    _save_token()
    open_m365(app)

    app.page.click("#m365-logout-btn")
    app.page.wait_for_selector("#m365-login-btn", timeout=10_000)

    assert token_store.has_session() is False
    assert "Sin sesión" in app.page.inner_text("#vault-modal")


def test_a_failed_login_shows_the_friendly_message_and_lets_the_person_try_again(app, login_calls):
    from app.friendly_errors import FriendlyError  # noqa: F401  (el mensaje ya viene de app.m365.errors)

    # El intento anterior de iniciar sesion fallo (la persona cancelo en el navegador): al abrir la ventana ya esta el error guardado, como en el uso real.
    service._state.update(
        {
            "phase": "error",
            "message": "No se pudo iniciar sesión con tu cuenta de la empresa: se canceló o no se autorizó.",
            "code": "M365-LOGIN-RECHAZADO",
        }
    )
    open_m365(app)

    text = app.page.inner_text("#vault-modal")
    assert "se canceló o no se autorizó" in text
    assert "M365-LOGIN-RECHAZADO" not in text and "Traceback" not in text
    assert app.page.locator("#m365-login-btn").is_visible(), "tiene que poder volver a intentar"


def test_the_switch_turns_the_connection_off_and_on_without_touching_the_session(app, login_calls):
    from app import switches

    _save_token()
    open_m365(app)

    app.page.click("#m365-switch")
    app.page.wait_for_function(
        "document.querySelector('#m365-switch').getAttribute('aria-checked') === 'false'", timeout=10_000
    )
    assert switches.is_m365_enabled() is False and token_store.has_session() is True

    app.page.click("#m365-switch")
    app.page.wait_for_function(
        "document.querySelector('#m365-switch').getAttribute('aria-checked') === 'true'", timeout=10_000
    )
    assert switches.is_m365_enabled() is True and token_store.has_session() is True


def test_the_summary_row_shows_the_state_next_to_the_other_connections(app, login_calls):
    _save_token()
    app.page.click("#settings-btn")
    app.page.click('.settings-tab[data-tab="connections"]')
    app.page.wait_for_selector("#vault-root .vault-head", timeout=20_000)
    app.page.wait_for_selector('#vault-summary [data-id="m365"]', timeout=10_000)

    row = app.page.inner_text('#vault-summary [data-id="m365"]')
    assert "Outlook y Teams" in row and "Conectado" in row


def test_the_summary_row_says_off_when_the_connection_is_turned_off_even_with_a_session(app, login_calls):
    """Apagada y conectada son cosas distintas: la persona tiene que ver que Vision NO la esta usando aunque su sesion siga guardada."""
    from app import switches

    _save_token()
    switches.set_m365_enabled(False)
    app.page.click("#settings-btn")
    app.page.click('.settings-tab[data-tab="connections"]')
    app.page.wait_for_selector("#vault-root .vault-head", timeout=20_000)
    app.page.wait_for_selector('#vault-summary [data-id="m365"]', timeout=10_000)

    row = app.page.inner_text('#vault-summary [data-id="m365"]')
    assert "Apagada" in row and "Conectado" not in row


def test_a_backend_failure_while_signing_in_is_shown_as_a_friendly_message(app, monkeypatch):
    async def boom(port, runner=None):
        raise RuntimeError("fallo interno con access_token=ABC")

    monkeypatch.setattr(service, "start_login", boom)
    open_m365(app)

    app.page.click("#m365-login-btn")
    app.page.wait_for_function(
        "document.querySelector('#vault-modal .vault-notice').innerText.length > 0", timeout=10_000
    )

    text = app.page.inner_text("#vault-modal")
    assert "access_token" not in text and "RuntimeError" not in text
