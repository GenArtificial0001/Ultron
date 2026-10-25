"""E2E de «Outlook y Teams» y «Office» en las DOS listas de conexiones (spec conexiones-office-m365-visibles REQ-001..007) en un Chromium REAL contra el backend real.

Pedido del usuario (2026-10-18): que aparezcan junto al resto en el panel lateral «Conectores» y en Configuracion > Conexiones > «Estado de las conexiones», con un nombre que diga que
se puede hacer (correo, agenda, mensajes; Excel, Word y documentos). El estado, los servicios y el Excel son falsos; el backend, las rutas y el front son los reales.

Lo que se vigila:
1. Las dos filas aparecen en las dos listas, con su nombre descriptivo, ordenadas con las demas.
2. El punto de color dice la verdad: sin sesion = rojo, con sesion = verde, esperando = ambar, apagada = gris; Excel cerrado NO es rojo (Office se puede usar).
3. El interruptor de la fila enciende y apaga DE VERDAD (el del servidor) y se refleja tambien en la ventana del gestor.
4. NO se rompe nada: las demas filas, su orden y los conteos `X/Y` siguen igual; si una consulta falla, la lista de siempre se ve completa.
5. Todo texto del servidor se pinta como texto (nunca como HTML)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import connections as conns
from app import switches as sw
from app.config import Settings
from app.m365 import service as m365_service
from app.m365 import token_store
from app.office import core
from app.office import status as office_status
from app.persistence import master_index
from e2e_support import FakeWhisper, launch_chromium, open_app, serve_backend
from tests.test_office_core import FakeOffice

pytestmark = pytest.mark.e2e

@pytest.fixture(autouse=True)
def _ultron_m365_on():
    """ULTRON trae Outlook y Teams APAGADO de fabrica (spec portar-desde-origen): estos tests prueban el comportamiento con el interruptor
    prendido, como lo deja la persona que lo activo. Los que prueban el apagado lo apagan ellos mismos."""
    from app import switches as _sw

    _sw.set_m365_enabled(True)
    yield


@pytest.fixture(autouse=True)
def _ultron_office_on():
    """ULTRON trae Office APAGADO de fabrica (spec portar-desde-origen): estos tests prueban el comportamiento con el interruptor
    prendido, como lo deja la persona que lo activo. Los que prueban el apagado lo apagan ellos mismos."""
    from app import switches as _sw

    _sw.set_office_enabled(True)
    yield


MODEL = "claude-sonnet-4-6"
M365_NAME = "Outlook y Teams - Principal"  # nombre POR DEFECTO: la persona lo cambia (spec conexiones-office-m365-visibles, correccion 2026-10-19)
OFFICE_NAME = "Office - Principal"


class Provider:
    def __init__(self):
        self.whisper = FakeWhisper()
        self.audio = SimpleNamespace(transcriptions=self.whisper)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=lambda **k: iter([])))

    def get(self, path, cast_to=object):
        return {"data": [{"id": MODEL, "type": "LLM", "aliases": [], "valid_args": [], "reasoning_levels": []}]}


class Online:
    def __init__(self, settings):
        pass

    def test_connection(self):
        return True


@pytest.fixture(scope="module")
def store():
    return {}


@pytest.fixture(scope="module")
def excel():
    fake = FakeOffice()
    core.set_backend(fake)
    yield fake
    core.set_backend(None)


@pytest.fixture(scope="module")
def base_url(store, excel, tmp_path_factory):
    import app.connectors.confluence_connector as cc
    import app.connectors.oracle_connector as oc
    import app.connectors.sharepoint_connector as sc
    from app import connectors_status as cs

    env = Settings(
        _env_file=None,
        oracle_host="db",
        oracle_user="u",
        oracle_password="p",
        oracle_service_name="s",
        sharepoint_site_url="https://x/sites/l",
        azure_client_id="c",
        azure_client_secret="s",
        azure_tenant_id="t",
        confluence_email="a@b.com",
        confluence_api_token="tok-123456",
        enable_sap=False,
        enable_bigquery=False,
    )
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(master_index, "get_app_setting", lambda k: store.get(k))
        mp.setattr(master_index, "set_app_setting", lambda k, v: store.__setitem__(k, v))
        mp.setattr(master_index, "unset_app_setting", lambda k: store.pop(k, None))
        mp.setattr(conns, "CONNECTIONS_FILE", Path("no-existe-connections.json"))
        mp.setattr(conns, "get_settings", lambda: env)
        mp.setattr("app.config.get_settings", lambda: env)
        mp.setattr(cs, "get_settings", lambda: env)
        conns.reset_for_tests()
        mp.setattr(oc, "OracleConnector", Online)
        mp.setattr(sc, "SharePointConnector", Online)
        mp.setattr(cc, "ConfluenceConnector", Online)
        mp.setattr(office_status, "_registered", lambda progid: True)
        mp.setattr(office_status, "_has_pywin32", lambda: True)
        with serve_backend(Provider()) as url:
            yield url
    conns.reset_for_tests()


@pytest.fixture(scope="module")
def browser(tmp_path_factory, base_url):
    with launch_chromium(tmp_path_factory.mktemp("caps"), base_url) as b:
        yield b


@pytest.fixture(autouse=True)
def _clean(store, excel, monkeypatch):
    """Cada test arranca con todo ACTIVO, sin sesion de Outlook y Teams y con Excel abierto."""
    store.pop("connections_disabled", None)
    store.pop(sw.M365_DISABLED_KEY, None)
    store.pop(sw.OFFICE_DISABLED_KEY, None)
    store.pop("m365_connection_name", None)
    store.pop("office_connection_name", None)
    excel.running = True
    excel.dialog_open = False
    monkeypatch.setattr(token_store, "has_session", lambda: False)
    m365_service.reset_for_tests()
    yield
    m365_service.reset_for_tests()


@pytest.fixture()
def app(browser, base_url):
    with open_app(browser, base_url) as a:
        a.open()
        yield a


def open_connections_tab(app, expect: str = M365_NAME):
    """Abre Conexiones y espera la fila de Outlook y Teams con SU nombre (por defecto «Principal»; si el test ya lo renombro, se pasa el nuevo en `expect`)."""
    app.page.click("#settings-btn")
    app.page.click('.settings-tab[data-tab="connections"]')
    app.page.wait_for_function(
        "document.querySelectorAll('#conn-status-list .conn-status-row').length > 0 && !document.querySelector('#conn-status-list .connector-dot.checking')",
        timeout=20_000,
    )
    app.page.wait_for_selector(f"#conn-status-list .conn-status-label:text-is('{expect}')", timeout=15_000)


def sidebar_names(app) -> list[str]:
    app.page.evaluate("document.getElementById('acc-connectors').open = true")
    app.page.wait_for_function(
        "document.querySelectorAll('#connectors-list li').length > 0 && !document.querySelector('#connectors-list .connector-dot.checking')",
        timeout=20_000,
    )
    return app.page.eval_on_selector_all("#connectors-list li", "els => els.map(e => e.textContent.trim())")


def row(app, name: str):
    return app.page.locator(
        "#conn-status-list .conn-status-row", has=app.page.locator(f".conn-status-label:text-is('{name}')")
    )


def dot(app, name: str) -> str:
    return row(app, name).locator(".connector-dot").get_attribute("class").replace("connector-dot", "").strip()


def labels(app) -> list[str]:
    return app.page.eval_on_selector_all("#conn-status-list .conn-status-label", "els => els.map(e => e.textContent)")


def refresh(app):
    app.page.click("#conn-status-refresh")
    app.page.wait_for_function(
        "!document.querySelector('#conn-status-list .connector-dot.checking') && document.querySelectorAll('#conn-status-list .conn-status-row').length > 0",
        timeout=20_000,
    )


# ------------------------------------------------------------------------------------------------------------------ REQ-001: aparecen
def test_both_rows_appear_in_settings_with_their_descriptive_names(app):
    open_connections_tab(app)

    assert M365_NAME in labels(app) and OFFICE_NAME in labels(app)


def test_both_rows_appear_in_the_side_panel_with_the_same_names(app):
    names = sidebar_names(app)

    assert M365_NAME in names and OFFICE_NAME in names


def test_they_are_sorted_with_the_rest_not_pinned_to_an_end(app):
    open_connections_tab(app)
    got = labels(app)

    assert got == sorted(got, key=lambda s: s.lower()) or got == sorted(got, key=str.casefold), got


def test_the_default_names_are_principal_like_the_other_connections_not_a_fixed_description(app):
    """Correccion del usuario: el nombre lo pone la persona; mientras no ponga uno se llaman «Principal», como las demas conexiones armadas desde el `.env`."""
    open_connections_tab(app)

    assert M365_NAME in labels(app) and OFFICE_NAME in labels(app)
    assert not any("correo, agenda" in g for g in labels(app))


# ------------------------------------------------------------------------------------------------------------------ REQ-002: el color dice la verdad
def test_outlook_and_teams_without_a_session_is_red_and_says_how_to_sign_in(app):
    open_connections_tab(app)

    assert dot(app, M365_NAME) == "error"
    assert "sesión" in row(app, M365_NAME).inner_text().lower()


def test_outlook_and_teams_with_a_session_is_green_and_says_connected(app, monkeypatch):
    monkeypatch.setattr(token_store, "has_session", lambda: True)
    open_connections_tab(app)

    assert dot(app, M365_NAME) == "online" and "Conectado" in row(app, M365_NAME).inner_text()


def test_outlook_and_teams_waiting_for_the_browser_is_amber(app, monkeypatch):
    monkeypatch.setitem(m365_service._state, "phase", "waiting")
    open_connections_tab(app)

    assert dot(app, M365_NAME) == "waiting"
    assert "navegador" in row(app, M365_NAME).inner_text().lower()


def test_outlook_and_teams_with_a_failed_sign_in_is_red_with_the_plain_sentence(app, monkeypatch):
    monkeypatch.setitem(m365_service._state, "phase", "error")
    monkeypatch.setitem(m365_service._state, "message", "la empresa no dejó entrar con esa cuenta. Probá de nuevo.")
    monkeypatch.setitem(m365_service._state, "code", "X")
    open_connections_tab(app)

    assert dot(app, M365_NAME) == "error" and "la empresa no dejó entrar" in row(app, M365_NAME).inner_text()


def test_office_with_excel_open_is_green(app):
    open_connections_tab(app)

    assert dot(app, OFFICE_NAME) == "online" and "Excel está abierto" in row(app, OFFICE_NAME).inner_text()


def test_office_with_excel_closed_is_still_green_because_it_can_be_used(app, excel):
    """Marcarlo rojo seria mentir: lo unico que falta es abrir el libro."""
    excel.running = False
    open_connections_tab(app)

    assert dot(app, OFFICE_NAME) == "online" and "Excel está cerrado" in row(app, OFFICE_NAME).inner_text()


def test_office_with_a_dialog_open_is_amber(app, excel):
    excel.dialog_open = True
    open_connections_tab(app)

    assert dot(app, OFFICE_NAME) == "waiting" and "ocupado" in row(app, OFFICE_NAME).inner_text().lower()


def test_office_not_installed_is_red(app, monkeypatch):
    monkeypatch.setattr(office_status, "_registered", lambda progid: False)
    open_connections_tab(app)

    assert dot(app, OFFICE_NAME) == "error"


# ------------------------------------------------------------------------------------------------------------------ REQ-003/006: el interruptor
def test_each_new_row_has_its_switch(app):
    open_connections_tab(app)

    for name in (M365_NAME, OFFICE_NAME):
        assert row(app, name).locator(".sw-toggle").count() == 1, name


def test_the_outlook_switch_really_turns_it_off_and_on_in_the_server(app):
    open_connections_tab(app)
    row(app, M365_NAME).locator(".sw-toggle").click()
    app.page.wait_for_function(
        f"[...document.querySelectorAll('#conn-status-list .conn-status-row')].some(r => r.textContent.includes('{M365_NAME}') && r.dataset.enabled === 'false')",
        timeout=15_000,
    )

    assert sw.is_m365_enabled() is False and dot(app, M365_NAME) == "off"
    assert "Desactivada" in row(app, M365_NAME).inner_text()

    row(app, M365_NAME).locator(".sw-toggle").click()
    app.page.wait_for_function(
        f"[...document.querySelectorAll('#conn-status-list .conn-status-row')].some(r => r.textContent.includes('{M365_NAME}') && r.dataset.enabled === 'true')",
        timeout=15_000,
    )
    assert sw.is_m365_enabled() is True


def test_the_office_switch_really_turns_it_off_and_on_in_the_server(app):
    open_connections_tab(app)
    row(app, OFFICE_NAME).locator(".sw-toggle").click()
    app.page.wait_for_function(
        f"[...document.querySelectorAll('#conn-status-list .conn-status-row')].some(r => r.textContent.includes('{OFFICE_NAME}') && r.dataset.enabled === 'false')",
        timeout=15_000,
    )

    assert sw.is_office_enabled() is False and dot(app, OFFICE_NAME) == "off"

    row(app, OFFICE_NAME).locator(".sw-toggle").click()
    app.page.wait_for_function(
        f"[...document.querySelectorAll('#conn-status-list .conn-status-row')].some(r => r.textContent.includes('{OFFICE_NAME}') && r.dataset.enabled === 'true')",
        timeout=15_000,
    )
    assert sw.is_office_enabled() is True


def test_an_off_switch_survives_a_reload_because_it_comes_from_the_server(app):
    sw.set_office_enabled(False)
    open_connections_tab(app)

    assert dot(app, OFFICE_NAME) == "off" and row(app, OFFICE_NAME).get_attribute("data-enabled") == "false"


def test_turning_it_off_here_is_seen_in_the_manager_window_too(app):
    open_connections_tab(app)
    row(app, OFFICE_NAME).locator(".sw-toggle").click()
    app.page.wait_for_function(
        "document.querySelector('#conn-status-list .conn-status-row[data-enabled=false]') !== null", timeout=15_000
    )
    app.page.click("#vault-menu-btn")
    app.page.click('.vault-menu-item[data-connector="office"]')
    app.page.wait_for_selector("#office-card", timeout=10_000)

    assert "Office está desactivado" in app.page.inner_text("#vault-modal")


# ------------------------------------------------------------------------------------------------------------------ REQ-004/005: una sola prueba, sin romper nada
def test_one_probe_feeds_both_places_and_they_show_the_same_state(app):
    sidebar_names(app)
    open_connections_tab(app)
    probes = []
    app.page.on("request", lambda r: probes.append(r.url) if "/api/connectors/status" in r.url else None)

    refresh(app)
    app.page.wait_for_timeout(500)

    side = app.page.eval_on_selector_all(
        "#connectors-list li",
        "els => els.map(e => [e.textContent.trim(), e.querySelector('.connector-dot').className])",
    )
    rows = app.page.eval_on_selector_all(
        "#conn-status-list .conn-status-row",
        "els => els.map(e => [e.querySelector('.conn-status-label').textContent, e.querySelector('.connector-dot').className])",
    )
    assert len(probes) == 1, "una sola prueba de conexiones"
    assert set(map(tuple, side)) <= set(map(tuple, rows)), "el panel lateral y Conexiones muestran el MISMO estado"
    assert any(n == M365_NAME for n, _ in side) and any(n == OFFICE_NAME for n, _ in side)


def test_the_existing_connections_are_unchanged(app):
    open_connections_tab(app)
    got = labels(app)

    for name in ("Confluence - Principal", "Oracle - Principal", "SharePoint - Principal"):
        assert name in got or any(name.split(" - ")[0] in g for g in got), name
    assert sum(1 for g in got if g.startswith(("Oracle", "SharePoint", "Confluence"))) >= 3


def test_the_side_panel_counter_still_counts_only_data_connections(app):
    """«X/Y» del panel lateral son conexiones de DATOS: las dos capacidades nuevas no lo cambian (decision 6 de la spec)."""
    names = sidebar_names(app)
    summary = app.page.inner_text("#connectors-summary")
    online, total = (int(n) for n in summary.split("/"))
    data_rows = [n for n in names if n not in (M365_NAME, OFFICE_NAME)]

    assert total == len(data_rows), (summary, names)
    assert online <= total


def test_a_broken_status_query_leaves_the_usual_list_complete(app):
    app.page.route(
        "**/api/m365/status",
        lambda route: route.fulfill(status=500, content_type="application/json", body='{"detail": "x"}'),
    )
    app.page.route("**/api/office/status", lambda route: route.abort())
    app.page.click("#settings-btn")
    app.page.click('.settings-tab[data-tab="connections"]')
    app.page.wait_for_function(
        "document.querySelectorAll('#conn-status-list .conn-status-row').length > 0 && !document.querySelector('#conn-status-list .connector-dot.checking')",
        timeout=20_000,
    )

    got = labels(app)
    assert M365_NAME not in got and OFFICE_NAME not in got, "si no hay respuesta, la fila no aparece"
    assert any(g.startswith("Oracle") for g in got) and any(g.startswith("SharePoint") for g in got)
    assert app.page.locator("#conn-status-list .connector-dot.checking").count() == 0


# ------------------------------------------------------------------------------------------------------------------ REQ-007: texto, nunca HTML
def test_a_status_phrase_with_html_is_drawn_as_text(app, monkeypatch):
    monkeypatch.setitem(m365_service._state, "phase", "error")
    monkeypatch.setitem(m365_service._state, "message", "<img src=x onerror=window.__pwned=1> fallo")
    monkeypatch.setitem(m365_service._state, "code", "X")
    open_connections_tab(app)

    assert app.page.evaluate("window.__pwned") is None
    assert app.page.locator("#conn-status-list img").count() == 0
    assert "<img" in row(app, M365_NAME).inner_text()


def test_no_token_or_secret_is_ever_drawn(app, monkeypatch):
    monkeypatch.setattr(token_store, "has_session", lambda: True)
    open_connections_tab(app)
    html = app.page.content()

    assert "ACCESS-TOKEN" not in html and "refresh_token" not in html.lower()
    assert json.dumps(sidebar_names(app))  # no lanza


# ------------------------------------------------------------------------------------------------------------------ REQ-008/009/010: el nombre lo pone la persona
def open_window(app, connector: str, ready: str):
    app.page.click("#vault-menu-btn")
    app.page.click(f'.vault-menu-item[data-connector="{connector}"]')
    app.page.wait_for_selector("#vault-modal", timeout=10_000)
    app.page.wait_for_selector(ready, timeout=10_000)


def rename(app, field: str, button: str, value: str):
    app.page.fill(field, value)
    app.page.click(button)


def wait_modal_text(app, text: str):
    app.page.wait_for_function(f"document.getElementById('vault-modal').innerText.includes({text!r})", timeout=10_000)


def test_the_m365_window_has_a_name_field_with_the_current_name(app):
    open_connections_tab(app)
    open_window(app, "m365", "#m365-card")

    assert app.page.input_value("#m365-name-input") == "Principal"
    assert app.page.locator("#m365-name-save").is_visible()


def test_the_office_window_has_a_name_field_with_the_current_name(app):
    open_connections_tab(app)
    open_window(app, "office", "#office-card")

    assert app.page.input_value("#office-name-input") == "Principal"
    assert app.page.locator("#office-name-save").is_visible()


def test_the_person_renames_outlook_and_teams_and_both_lists_show_it(app):
    open_connections_tab(app)
    open_window(app, "m365", "#m365-card")

    rename(app, "#m365-name-input", "#m365-name-save", "Mi correo de trabajo")
    wait_modal_text(app, "Outlook y Teams - Mi correo de trabajo")
    app.page.click(".vault-modal-close")
    app.page.click("#conn-status-refresh")
    app.page.wait_for_selector("#conn-status-list .conn-status-label:text-is('Outlook y Teams - Mi correo de trabajo')", timeout=15_000)

    assert "Outlook y Teams - Principal" not in labels(app)
    assert "Outlook y Teams - Mi correo de trabajo" in sidebar_names(app)


def test_the_person_renames_office_and_both_lists_show_it(app):
    open_connections_tab(app)
    open_window(app, "office", "#office-card")

    rename(app, "#office-name-input", "#office-name-save", "Mis planillas")
    wait_modal_text(app, "Office - Mis planillas")
    app.page.click(".vault-modal-close")
    app.page.click("#conn-status-refresh")
    app.page.wait_for_selector("#conn-status-list .conn-status-label:text-is('Office - Mis planillas')", timeout=15_000)

    assert "Office - Principal" not in labels(app) and "Office - Mis planillas" in sidebar_names(app)


def test_the_saved_name_survives_reloading_the_page(app):
    open_connections_tab(app)
    open_window(app, "m365", "#m365-card")
    rename(app, "#m365-name-input", "#m365-name-save", "Correo de planta")
    wait_modal_text(app, "Correo de planta")

    app.page.reload()
    open_connections_tab(app, expect="Outlook y Teams - Correo de planta")


def test_the_summary_row_uses_the_name(app):
    open_connections_tab(app)
    open_window(app, "office", "#office-card")
    rename(app, "#office-name-input", "#office-name-save", "Mis planillas")
    wait_modal_text(app, "Office - Mis planillas")
    app.page.click(".vault-modal-close")

    app.page.wait_for_selector(".vault-summary-row[data-id='office']", timeout=10_000)
    assert "Mis planillas" in app.page.inner_text(".vault-summary-row[data-id='office']")


@pytest.mark.parametrize("bad", ["con_guion_bajo", "signos!", "x" * 61, " empieza con espacio", "termina con espacio ", "<b>negrita</b>"])
def test_an_invalid_name_cannot_be_saved_and_shows_the_same_message_as_the_other_connections(app, store, bad):
    open_connections_tab(app)
    open_window(app, "m365", "#m365-card")

    app.page.evaluate("(v) => { const i = document.getElementById('m365-name-input'); i.removeAttribute('maxlength'); i.value = v; i.dispatchEvent(new Event('input', {bubbles: true})); }", bad)

    assert app.page.is_disabled("#m365-name-save")
    assert "Solo letras, números, espacios y guion" in app.page.inner_text("#m365-name-hint")
    assert "m365_connection_name" not in store


def test_an_empty_name_cannot_be_saved(app, store):
    open_connections_tab(app)
    open_window(app, "office", "#office-card")

    app.page.fill("#office-name-input", "")

    assert app.page.is_disabled("#office-name-save") and "Poné un nombre" in app.page.inner_text("#office-name-hint")
    assert "office_connection_name" not in store


def test_renaming_does_not_change_the_switch_or_the_state(app):
    open_connections_tab(app)
    open_window(app, "m365", "#m365-card")
    app.page.click("#m365-switch")
    app.page.wait_for_function("document.getElementById('m365-badge').textContent.includes('Apagada')", timeout=10_000)

    rename(app, "#m365-name-input", "#m365-name-save", "Otro nombre")
    wait_modal_text(app, "Otro nombre")

    assert sw.is_m365_enabled() is False and "Apagada" in app.page.inner_text("#m365-badge")


def test_a_server_rejection_is_shown_as_a_sentence_not_a_code(app):
    open_connections_tab(app)
    open_window(app, "m365", "#m365-card")
    app.page.route(
        "**/api/m365/name",
        lambda route: route.fulfill(
            status=422,
            content_type="application/json",
            body='{"detail": "Ese nombre no sirve. Solo letras, números, espacios y guion (hasta 60), sin espacios al principio ni al final."}',
        ),
    )

    rename(app, "#m365-name-input", "#m365-name-save", "Nombre valido")
    wait_modal_text(app, "Ese nombre no sirve")

    assert "422" not in app.page.inner_text("#vault-modal")


def test_a_broken_saved_name_is_ignored_and_never_drawn(app, store):
    store["m365_connection_name"] = "<img src=x onerror=window.__pwned=1>"  # valor guardado roto: se ignora y se usa Principal
    open_connections_tab(app)

    assert app.page.evaluate("window.__pwned") is None and app.page.locator("#conn-status-list img").count() == 0
    assert M365_NAME in labels(app)


def test_the_sort_order_uses_the_name_the_person_chose(app):
    open_connections_tab(app)
    open_window(app, "office", "#office-card")
    rename(app, "#office-name-input", "#office-name-save", "AAA primera")
    wait_modal_text(app, "AAA primera")
    app.page.click(".vault-modal-close")
    app.page.click("#conn-status-refresh")
    app.page.wait_for_selector("#conn-status-list .conn-status-label:text-is('Office - AAA primera')", timeout=15_000)
    got = labels(app)

    assert got == sorted(got, key=str.casefold), got
