"""E2E de conexiones con nombre (spec conexiones-con-nombre REQ-002/010/011) en un Chromium REAL contra el backend real.

Se verifica lo que ve el usuario: el sidebar y la pestaña Conexiones muestran «Tipo - nombre» (ej. «SharePoint - Sitio Logistico»), cada conexion con SU estado
(una caida de «Oracle - Planta» no pinta de rojo a «Oracle - Datamart»), el arbol de SharePoint tiene una rama por sitio con SUS listas, y editar connections.json
se refleja al volver a abrir sin reiniciar. Los conectores son falsos (nunca hay red ni credenciales) y el archivo de conexiones es temporal."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app import connections as conns
from e2e_support import FakeWhisper, launch_chromium, open_app, serve_backend

pytestmark = pytest.mark.e2e

MODEL = "claude-sonnet-4-6"
SECRET = "SECRETO-E2E-5d1c"


class Provider:
    def __init__(self):
        self.whisper = FakeWhisper()
        self.audio = SimpleNamespace(transcriptions=self.whisper)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=lambda **k: iter([])))

    def get(self, path, cast_to=object):
        return {"data": [{"id": MODEL, "type": "LLM", "aliases": [], "valid_args": [], "reasoning_levels": []}]}


def oracle(name, host):
    return {
        "type": "oracle",
        "name": name,
        "fields": {"oracle_user": "u", "oracle_password": SECRET, "oracle_host": host, "oracle_service_name": "svc"},
    }


def sharepoint(name, site):
    return {
        "type": "sharepoint",
        "name": name,
        "fields": {
            "sharepoint_site_url": site,
            "azure_client_id": "cid",
            "azure_client_secret": SECRET,
            "azure_tenant_id": "tid",
        },
    }


class FakeOracle:
    def __init__(self, settings):
        self.host = settings.oracle_host

    def test_connection(self):
        return self.host != "h-planta"  # «Planta» esta caida; «Datamart» no


class FakeSharePoint:
    def __init__(self, settings):
        self.site = settings.sharepoint_site_url

    def test_connection(self):
        return True

    def list_lists(self):
        return ["Pendientes Calidad"] if self.site.endswith("/cal") else ["Medios", "Rutas"]


class FakeGraph:
    def __init__(self, settings):
        pass

    def test_connection(self):
        return True


class FakeBigQuery:
    def __init__(self, settings):
        pass

    def test_connection(self):
        return False


@pytest.fixture(scope="module")
def connections_file(tmp_path_factory):
    path = tmp_path_factory.mktemp("conns") / "connections.json"
    path.write_text(
        json.dumps(
            {
                "connections": [
                    oracle("Datamart", "h-datamart"),
                    oracle("Planta", "h-planta"),
                    sharepoint("Sitio Logistico", "https://x/sites/log"),
                    sharepoint("Sitio Calidad", "https://x/sites/cal"),
                ]
            }
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture(scope="module")
def base_url(connections_file):
    import app.connectors.graph_connector as gc
    import app.connectors.oracle_connector as oc
    import app.connectors.sharepoint_connector as sc
    from app import connectors_status as cs
    import app.main as main_module

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(conns, "CONNECTIONS_FILE", connections_file)
        conns.reset_for_tests()
        mp.setattr(oc, "OracleConnector", FakeOracle)
        mp.setattr(sc, "SharePointConnector", FakeSharePoint)
        mp.setattr(gc, "GraphConnector", FakeGraph)
        mp.setattr(cs, "BigQueryConnector", FakeBigQuery)
        mp.setattr(main_module, "SharePointConnector", FakeSharePoint)
        with serve_backend(Provider()) as url:
            yield url
    conns.reset_for_tests()


@pytest.fixture(scope="module")
def browser(tmp_path_factory, base_url):
    with launch_chromium(tmp_path_factory.mktemp("audio"), base_url) as b:
        yield b


@pytest.fixture()
def app(browser, base_url):
    with open_app(browser, base_url) as a:
        a.open()
        yield a


def open_connections_tab(app):
    app.page.click("#settings-btn")
    app.page.click('.settings-tab[data-tab="connections"]')
    app.page.wait_for_selector(".conn-status-row .conn-status-label", timeout=15_000)


def status_rows(app) -> dict[str, str]:
    """{etiqueta visible: estado del punto} de la pestaña Conexiones."""
    return app.page.evaluate(
        """() => Object.fromEntries([...document.querySelectorAll('#conn-status-list .conn-status-row')].map(r => [
            r.querySelector('.conn-status-label').textContent,
            r.querySelector('.connector-dot').className.replace('connector-dot', '').trim()]))"""
    )


def test_the_connections_tab_shows_type_dash_name_with_its_own_status(app):
    open_connections_tab(app)
    app.page.wait_for_function(
        "document.querySelectorAll('#conn-status-list .conn-status-row').length >= 5", timeout=15_000
    )

    rows = status_rows(app)

    assert rows["Oracle - Datamart"] == "online"
    assert rows["Oracle - Planta"] == "error", "la caida de Planta no pinta de rojo a Datamart"
    assert rows["SharePoint - Sitio Logistico"] == "online" and rows["SharePoint - Sitio Calidad"] == "online"
    assert rows["BigQuery"] == "error"


def test_the_sidebar_lists_the_same_names(app):
    app.page.evaluate("document.getElementById('acc-connectors').open = true")
    app.page.wait_for_function("document.querySelectorAll('#connectors-list li').length >= 5", timeout=15_000)

    names = app.page.eval_on_selector_all("#connectors-list li", "els => els.map(e => e.textContent.trim())")

    assert {
        "Oracle - Datamart",
        "Oracle - Planta",
        "SharePoint - Sitio Logistico",
        "SharePoint - Sitio Calidad",
    } <= set(names)


def test_the_sharepoint_tree_has_one_branch_per_site_with_its_own_lists(app):
    open_connections_tab(app)
    app.page.click("#conn-sp-lists-toggle")
    app.page.wait_for_function("document.querySelectorAll('#conn-sp-lists-list > li').length === 2", timeout=15_000)

    app.page.click("#conn-sp-lists-list > li:has-text('Sitio Calidad')")
    app.page.wait_for_selector("#conn-sp-lists-list li:has-text('Pendientes Calidad')", timeout=15_000)
    app.page.click("#conn-sp-lists-list > li:has-text('Sitio Logistico')")
    app.page.wait_for_selector("#conn-sp-lists-list li:has-text('Medios')", timeout=15_000)

    text = app.page.inner_text("#conn-sp-lists-list")
    assert "Pendientes Calidad" in text and "Medios" in text and "Rutas" in text


def test_no_secret_reaches_the_page(app):
    open_connections_tab(app)
    app.page.click("#conn-sp-lists-toggle")
    app.page.wait_for_function("document.querySelectorAll('#conn-sp-lists-list > li').length === 2", timeout=15_000)

    html = app.page.content()

    assert SECRET not in html and "h-datamart" not in html and "cid" not in app.page.inner_text("#conn-status-list")


def test_the_pane_says_where_the_file_lives(app, connections_file):
    open_connections_tab(app)
    app.page.wait_for_function("document.getElementById('conn-file-note').textContent.length > 0", timeout=10_000)

    note = app.page.inner_text("#conn-file-note")

    assert "connections.json" in note and "se releen solas" in note.lower() or "releen" in note


def test_editing_the_file_shows_the_new_connection_without_restarting(app, connections_file):
    original = connections_file.read_text(encoding="utf-8")
    data = json.loads(original)
    data["connections"].append(oracle("Nueva Base", "h-nueva"))
    connections_file.write_text(json.dumps(data), encoding="utf-8")
    try:
        open_connections_tab(app)
        app.page.wait_for_function(
            "[...document.querySelectorAll('#conn-status-list .conn-status-label')].some(e => e.textContent === 'Oracle - Nueva Base')",
            timeout=15_000,
        )
    finally:
        connections_file.write_text(original, encoding="utf-8")


def test_a_broken_file_shows_a_warning_and_keeps_the_last_good_connections(app, connections_file):
    original = connections_file.read_text(encoding="utf-8")
    open_connections_tab(app)
    app.page.wait_for_function(
        "document.querySelectorAll('#conn-status-list .conn-status-row').length >= 5", timeout=15_000
    )
    connections_file.write_text('{"connections": [ {"type": ', encoding="utf-8")
    try:
        app.page.click("#conn-status-refresh")
        app.page.click('.settings-tab[data-tab="model"]')
        app.page.click('.settings-tab[data-tab="connections"]')
        app.page.wait_for_function(
            "document.getElementById('conn-file-note').textContent.includes('no se pudo leer')", timeout=15_000
        )

        assert "Oracle - Datamart" in app.page.inner_text("#conn-status-list"), "lo que ya andaba sigue andando"
    finally:
        connections_file.write_text(original, encoding="utf-8")


def test_the_credentials_tab_only_offers_bigquery_and_voice(app):
    app.page.click("#settings-btn")
    app.page.click('.settings-tab[data-tab="credentials"]')
    app.page.wait_for_selector("#cred-accordion summary", timeout=15_000)

    titles = app.page.eval_on_selector_all("#cred-accordion summary", "els => els.map(e => e.textContent)")
    joined = " ".join(titles)

    assert "BigQuery" in joined and "Voz" in joined
    assert "Oracle" not in joined and "SAP" not in joined and "SharePoint" not in joined


def test_the_sharepoint_tree_picks_up_a_site_added_to_the_file_after_it_was_first_opened(app, connections_file):
    """Sin volver a armar la rama, un sitio agregado al archivo no aparecia hasta recargar la pagina (REQ-011: sin reiniciar)."""
    original = connections_file.read_text(encoding="utf-8")
    open_connections_tab(app)
    app.page.click("#conn-sp-lists-toggle")
    app.page.wait_for_function("document.querySelectorAll('#conn-sp-lists-list > li').length === 2", timeout=15_000)
    data = json.loads(original)
    data["connections"].append(sharepoint("Sitio Nuevo", "https://x/sites/nuevo"))
    connections_file.write_text(json.dumps(data), encoding="utf-8")
    try:
        app.page.click("#conn-sp-lists-toggle")  # cierra
        app.page.click('.settings-tab[data-tab="model"]')
        app.page.click('.settings-tab[data-tab="connections"]')  # reabre la pestaña: relee el archivo
        app.page.wait_for_function("document.getElementById('conn-file-note').textContent.length > 0", timeout=10_000)
        app.page.click("#conn-sp-lists-toggle")
        app.page.wait_for_function("document.querySelectorAll('#conn-sp-lists-list > li').length === 3", timeout=15_000)
    finally:
        connections_file.write_text(original, encoding="utf-8")


def test_the_sharepoint_branch_is_not_stale_when_the_file_reread_arrives_late(app, connections_file):
    """El mismo defecto que tuvo Confluence: si el usuario abre la rama de SharePoint ANTES de que llegue la relectura de connections.json, no puede ver la lista vieja.
    La primera respuesta de `/api/connections` tras volver a la pestaña se retiene hasta despues del clic."""
    original = connections_file.read_text(encoding="utf-8")
    open_connections_tab(app)
    app.page.click("#conn-sp-lists-toggle")
    app.page.wait_for_function("document.querySelectorAll('#conn-sp-lists-list > li').length === 2", timeout=15_000)
    data = json.loads(original)
    data["connections"].append(sharepoint("Sitio Tardio", "https://x/sites/tardio"))
    connections_file.write_text(json.dumps(data), encoding="utf-8")

    held: list = []

    def hold_first(route):
        if not held:
            held.append(route)
        else:
            route.continue_()

    app.page.route("**/api/connections", hold_first)
    try:
        app.page.click("#conn-sp-lists-toggle")  # cierra
        app.page.click('.settings-tab[data-tab="model"]')
        app.page.click('.settings-tab[data-tab="connections"]')
        app.page.wait_for_function("true")
        app.page.click("#conn-sp-lists-toggle")  # abre ANTES de que llegue la relectura
        for route in held:
            route.continue_()
        app.page.wait_for_function("document.querySelectorAll('#conn-sp-lists-list > li').length === 3", timeout=15_000)
    finally:
        app.page.unroute("**/api/connections")
        connections_file.write_text(original, encoding="utf-8")
