"""E2E de la conexion «Office (Excel y Word)» en Configuracion > Conexiones (spec office-com-automatizacion REQ-009, REQ-015) en un Chromium REAL contra el backend en loopback.

Lo que ve la persona:
1. El menu «Conectores» trae «Office (Excel y Word)» y la fila de resumen dice si esta activo.
2. Su ventana dice si Excel esta abierto (con cuantos libros), cerrado u ocupado, y si Word esta instalado: en frases, sin abrir ni lanzar nada.
3. Un interruptor apaga y prende Office, y la pantalla repinta con lo que dijo el servidor.
4. Con Office apagado la ventana lo dice y no se consulta Excel (el Excel falso no recibe ninguna llamada).
5. Abrir la pantalla NO abre ni lanza Excel ni Word. Una falla se ve como frase, nunca como un codigo."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app import config
from app import config_store as cs
from app import connections as conns
from app import credentials, secret_store
from app import switches as sw
from app.office import core
from app.office import status as office_status
from e2e_support import FakeWhisper, launch_chromium, open_app, serve_backend
from tests.test_office_core import FakeOffice

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(not secret_store.is_available(), reason="DPAPI solo existe en Windows"),
]

MODEL = "claude-sonnet-4-6"


class Provider:
    def __init__(self):
        self.whisper = FakeWhisper()
        self.audio = SimpleNamespace(transcriptions=self.whisper)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=lambda **k: iter([])))

    def get(self, path, cast_to=object):
        return {"data": [{"id": MODEL, "type": "LLM", "aliases": [], "valid_args": [], "reasoning_levels": []}]}


@pytest.fixture(scope="module")
def excel():
    fake = FakeOffice()
    core.set_backend(fake)
    yield fake
    core.set_backend(None)


@pytest.fixture(scope="module")
def base_url(excel):
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(office_status, "_registered", lambda progid: True)
        mp.setattr(office_status, "_has_pywin32", lambda: True)
        with serve_backend(Provider()) as url:
            yield url


@pytest.fixture(scope="module")
def browser(tmp_path_factory, base_url):
    with launch_chromium(tmp_path_factory.mktemp("office"), base_url) as b:
        yield b


@pytest.fixture(autouse=True)
def clean_state(monkeypatch, tmp_path, excel):
    monkeypatch.setattr(config, "BASE_DIR", tmp_path)
    monkeypatch.setattr(credentials, "ENC_FILE", tmp_path / ".env.enc")
    monkeypatch.setattr(conns, "CONNECTIONS_FILE", tmp_path / "connections.json")
    monkeypatch.setitem(config.Settings.model_config, "env_file", str(tmp_path / ".env"))
    conns.reset_for_tests()
    cs.invalidate()
    sw.set_office_enabled(True)
    excel.running = True
    excel.dialog_open = False
    excel.calls.clear()
    yield
    sw.set_office_enabled(True)
    conns.reset_for_tests()
    cs.invalidate()
    config.get_settings.cache_clear()


@pytest.fixture()
def app(browser, base_url):
    with open_app(browser, base_url) as a:
        a.open()
        yield a


def open_connections(app):
    app.page.click("#settings-btn")
    app.page.click('.settings-tab[data-tab="connections"]')
    app.page.wait_for_selector("#vault-root .vault-head", timeout=20_000)


def open_office(app):
    open_connections(app)
    app.page.click("#vault-menu-btn")
    app.page.click('.vault-menu-item[data-connector="office"]')
    app.page.wait_for_selector("#vault-modal", timeout=10_000)
    app.page.wait_for_selector("#office-card", timeout=10_000)


def test_the_connectors_menu_offers_office(app):
    open_connections(app)
    app.page.click("#vault-menu-btn")

    assert app.page.locator('.vault-menu-item[data-connector="office"]').inner_text() == "Office (Excel y Word)"


def test_the_summary_row_shows_office_as_active(app):
    open_connections(app)
    app.page.wait_for_selector('.vault-summary-row[data-id="office"]', timeout=10_000)

    row = app.page.locator('.vault-summary-row[data-id="office"]')
    assert "Office" in row.inner_text() and "Activo" in row.inner_text()


def test_the_window_says_how_excel_and_word_are_in_plain_words(app, excel):
    open_office(app)

    text = app.page.inner_text("#vault-modal")
    assert "Excel está abierto con 1 libro" in text and "Word está instalado" in text
    assert "Traceback" not in text and "COM" not in text


def test_a_closed_excel_says_to_open_it_and_nothing_is_launched(app, excel):
    excel.running = False
    open_office(app)

    text = app.page.inner_text("#vault-modal")
    assert "Excel está cerrado" in text
    assert excel.launched is False and excel.saved is False


def test_an_excel_with_a_dialog_is_reported_as_busy(app, excel):
    excel.dialog_open = True
    open_office(app)

    assert "Excel está ocupado" in app.page.inner_text("#vault-modal")


def test_the_switch_turns_office_off_and_the_window_and_summary_follow(app, excel):
    open_office(app)
    app.page.click("#office-switch")
    app.page.wait_for_function(
        "document.getElementById('office-badge').textContent.includes('Apagado')", timeout=10_000
    )

    assert sw.is_office_enabled() is False
    assert "Office está desactivado" in app.page.inner_text("#vault-modal")
    app.page.click(".vault-modal-close")
    app.page.wait_for_selector('.vault-summary-row[data-id="office"] .vault-badge[data-state="idle"]', timeout=10_000)


def test_with_office_off_excel_is_not_consulted_at_all(app, excel):
    sw.set_office_enabled(False)
    excel.calls.clear()
    open_office(app)

    assert "Office está desactivado" in app.page.inner_text("#vault-modal")
    assert not excel.calls, "apagado, ni siquiera se mira Excel"


def test_the_switch_turns_office_back_on(app, excel):
    sw.set_office_enabled(False)
    open_office(app)
    app.page.click("#office-switch")
    app.page.wait_for_function("document.getElementById('office-badge').textContent.includes('Activo')", timeout=10_000)

    assert sw.is_office_enabled() is True and "Excel está abierto" in app.page.inner_text("#vault-modal")


def test_opening_the_screen_never_launches_or_saves_anything(app, excel):
    open_office(app)

    assert excel.launched is False and excel.saved is False
    assert all(c[0] in ("check",) for c in excel.calls), "solo se mira, no se lee ni se escribe ninguna celda"


def test_a_failing_status_is_a_sentence_not_a_code(app):
    open_connections(app)
    app.page.route(
        "**/api/office/status",
        lambda route: route.fulfill(
            status=500,
            content_type="application/json",
            body='{"detail": "No se pudo revisar el estado de Office. Probá de nuevo."}',
        ),
    )
    app.page.click("#vault-menu-btn")
    app.page.click('.vault-menu-item[data-connector="office"]')
    app.page.wait_for_selector("#vault-modal", timeout=10_000)
    # el modal se abre YA con la ayuda y el cuerpo se dibuja despues de consultar al servidor: se espera la frase, no se lee de inmediato (en la suite larga la lectura llegaba antes)
    app.page.wait_for_function("document.getElementById('vault-modal').innerText.includes('No pude consultar el estado de Office')", timeout=15_000)

    assert "500" not in app.page.inner_text("#vault-modal")


def test_a_refresh_button_asks_again_and_shows_the_new_state(app, excel):
    open_office(app)
    assert "Excel está abierto" in app.page.inner_text("#vault-modal")
    excel.running = False

    app.page.click("#office-refresh-btn")
    app.page.wait_for_function(
        "document.getElementById('vault-modal').innerText.includes('Excel está cerrado')", timeout=10_000
    )
