"""E2E de los interruptores de conexiones y skills (spec interruptores-conexiones-skills REQ-006/007/008/009) en un Chromium REAL contra el backend real.

Lo que ve el usuario: cada conexion tiene un interruptor en Configuracion > Conexiones y en el panel rapido; apagar una la saca del panel lateral derecho; y lo apagado
SOBREVIVE a recargar la pagina (viene del servidor, no de la memoria del navegador). Tambien un interruptor por skill, al lado del boton que la ejecuta, sin que apagarla
dispare la skill. La base maestra es TEMPORAL y los conectores son falsos."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app import connections as conns
from app.config import Settings
from app.persistence import master_index
from app.tools import skill as skill_mod
from e2e_support import FakeWhisper, launch_chromium, open_app, serve_backend

pytestmark = pytest.mark.e2e

MODEL = "claude-sonnet-4-6"


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


def make_skill(root: Path, name: str) -> None:
    folder = root / name
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(f"---\nname: {name}\ndescription: d\n---\ncuerpo de {name}\n", encoding="utf-8")


@pytest.fixture(scope="module")
def store():
    return {}


@pytest.fixture(scope="module")
def base_url(store, tmp_path_factory):
    import app.connectors.confluence_connector as cc
    import app.connectors.oracle_connector as oc
    import app.connectors.sharepoint_connector as sc
    from app import connectors_status as cs

    globales = tmp_path_factory.mktemp("skills_sw")
    for name in ("auditoria", "slotting"):
        make_skill(globales, name)
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
        sap_base_url="https://sap-interno",
        sap_user="usuario-sap",
        sap_password="clave-sap",
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
        store[skill_mod.SKILLS_GLOBAL_ENABLED_KEY] = "true"
        mp.setattr(skill_mod, "discover_skills", lambda: skill_mod._scan_skills_dir(globales))
        mp.setattr(skill_mod, "discover_local_skills", lambda session_id: [])
        # `main.py` hizo `from app.tools.skill import discover_skills`: tiene SU PROPIO nombre. Parchear solo `skill_mod` funcionaba unicamente si `main` se importaba
        # DESPUES del parche; con otro archivo e2e antes (que ya importo `main`) el endpoint seguia viendo las skills reales y la lista salia vacia. Se parchea tambien alli.
        import app.main as main_module

        mp.setattr(main_module, "discover_skills", lambda: skill_mod._scan_skills_dir(globales))
        with serve_backend(Provider()) as url:
            yield url
    conns.reset_for_tests()


@pytest.fixture(scope="module")
def browser(tmp_path_factory, base_url):
    with launch_chromium(tmp_path_factory.mktemp("audio"), base_url) as b:
        yield b


@pytest.fixture(autouse=True)
def _all_on(store):
    """Cada test arranca con todo ACTIVO (como una instalacion nueva)."""
    store.pop("connections_disabled", None)
    store.pop("skills_disabled", None)
    yield


@pytest.fixture()
def app(browser, base_url):
    with open_app(browser, base_url) as a:
        a.open()
        yield a


def open_connections_tab(app):
    app.page.click("#settings-btn")
    app.page.click('.settings-tab[data-tab="connections"]')
    # La lista muestra primero UNA fila «Verificando…» con la MISMA clase `.conn-status-row` que las reales: esperar solo esa clase contaba la fila de carga como si fuera
    # el resultado (carrera: fallaba 1 de cada 3). Se espera a que no quede ninguna fila en estado `checking` y haya al menos una real.
    app.page.wait_for_function(
        "document.querySelectorAll('#conn-status-list .conn-status-row').length > 0 && !document.querySelector('#conn-status-list .connector-dot.checking')",
        timeout=20_000,
    )


def open_quick_connections(app):
    app.page.click("#quick-panel-btn")
    app.page.click("#quick-panel-connections-btn")
    app.page.wait_for_selector("#quick-panel-connections-list .sw-row", timeout=15_000)


def quick_row(app, display):
    return app.page.locator(
        "#quick-panel-connections-list .sw-row", has=app.page.locator(f".sw-row-name:text-is('{display}')")
    )


def sidebar_names(app):
    app.page.evaluate("document.getElementById('acc-connectors').open = true")
    # el panel muestra primero «Verificando…» (una fila con el punto `checking`) y recien despues la lista real: se espera a que no quede ninguna fila en ese estado
    app.page.wait_for_function(
        "document.querySelectorAll('#connectors-list li').length > 0 && !document.querySelector('#connectors-list .connector-dot.checking')", timeout=20_000
    )
    return app.page.eval_on_selector_all("#connectors-list li", "els => els.map(e => e.textContent.trim())")


# ---------------------------------------------------------------------------------------------------------------------
# panel rapido > Conexiones (REQ-008)
# ---------------------------------------------------------------------------------------------------------------------
def test_the_quick_panel_lists_every_connection_with_its_switch_all_on(app):
    open_quick_connections(app)

    names = app.page.eval_on_selector_all(
        "#quick-panel-connections-list .sw-row-name", "els => els.map(e => e.textContent)"
    )
    states = app.page.eval_on_selector_all(
        "#quick-panel-connections-list .sw-row:not(:has(.sw-toggle:disabled)) .sw-toggle",
        "els => els.map(e => e.getAttribute('aria-checked'))",
    )

    assert {"Oracle - Datamart", "SharePoint - Sitio Logistico", "Confluence - Principal", "SAP - Principal"} <= set(names)
    assert set(states) == {"true"}, "todas las que el usuario puede manejar arrancan activas"


def refresh_status(app):
    """«Actualizar estado» (Configuracion > Conexiones): lo UNICO que vuelve a probar, y actualiza la lista y el panel lateral juntos (decision del usuario 2026-10-08)."""
    if not app.page.locator('.settings-pane[data-pane="connections"]').is_visible():
        open_connections_tab(app)
    app.page.click("#conn-status-refresh")
    app.page.wait_for_function(
        "!document.querySelector('#conn-status-list .connector-dot.checking') && !document.querySelector('#connectors-list .connector-dot.checking')"
        " && document.querySelectorAll('#conn-status-list .conn-status-row').length > 0",
        timeout=20_000,
    )


def test_turning_a_connection_off_in_the_quick_panel_saves_it_but_the_side_panel_waits_for_the_refresh_button(app):
    """Decision del usuario (2026-10-08): «primero configuremos todo y luego presiono el boton Actualizar estado y ahi recien se actualiza». Antes el panel lateral
    se actualizaba solo al tocar el interruptor."""
    assert "Confluence - Principal" in sidebar_names(app)
    open_quick_connections(app)

    quick_row(app, "Confluence - Principal").locator(".sw-toggle").click()
    app.page.wait_for_function(
        "[...document.querySelectorAll('#quick-panel-connections-list .sw-row')].some(r => r.textContent.includes('Confluence - Principal') && r.dataset.state === 'off')",
        timeout=10_000,
    )
    assert quick_row(app, "Confluence - Principal").locator(".sw-row-note").inner_text() == "desactivada"
    app.page.wait_for_timeout(800)
    assert "Confluence - Principal" in sidebar_names(app), "el panel lateral NO se actualiza solo"

    app.page.click("#quick-panel-btn")  # cierra el panel rapido (Escape no lo cierra)
    refresh_status(app)

    names = sidebar_names(app)
    assert "Confluence - Principal" not in names and "Oracle - Datamart" in names, "con «Actualizar estado» sale del panel lateral; las demas siguen"


def test_an_off_connection_stays_listed_in_the_quick_panel_so_it_can_be_turned_on_again(app):
    open_quick_connections(app)
    quick_row(app, "SharePoint - Sitio Logistico").locator(".sw-toggle").click()
    app.page.wait_for_function(
        "[...document.querySelectorAll('#quick-panel-connections-list .sw-row')].some(r => r.textContent.includes('SharePoint - Sitio Logistico') && r.dataset.state === 'off')",
        timeout=10_000,
    )

    quick_row(app, "SharePoint - Sitio Logistico").locator(".sw-toggle").click()
    app.page.wait_for_function(
        "[...document.querySelectorAll('#quick-panel-connections-list .sw-row')].some(r => r.textContent.includes('SharePoint - Sitio Logistico') && r.dataset.state === 'on')",
        timeout=10_000,
    )

    assert quick_row(app, "SharePoint - Sitio Logistico").locator(".sw-toggle").get_attribute("aria-checked") == "true"


def test_the_state_survives_reloading_the_page_because_it_lives_on_the_server(app):
    open_quick_connections(app)
    quick_row(app, "Oracle - Datamart").locator(".sw-toggle").click()
    app.page.wait_for_function(
        "document.querySelector('#quick-panel-connections-list .sw-row[data-state=off]') !== null", timeout=10_000
    )

    app.page.reload()
    app.page.wait_for_selector("#chat-input", timeout=30_000)
    open_quick_connections(app)

    assert quick_row(app, "Oracle - Datamart").locator(".sw-toggle").get_attribute("aria-checked") == "false"


def test_the_button_summary_counts_active_over_configured(app):
    open_quick_connections(app)
    total = app.page.eval_on_selector_all("#quick-panel-connections-list .sw-row", "els => els.length")
    active = app.page.eval_on_selector_all("#quick-panel-connections-list .sw-row[data-state=on]", "els => els.length")
    assert total - active == 1, "la unica apagada es la que el .env bloquea (SAP): no cuenta como activa"
    assert app.page.inner_text("#quick-panel-connections-summary") == f"{active}/{total}"

    quick_row(app, "Oracle - Datamart").locator(".sw-toggle").click()
    app.page.wait_for_function(
        f"document.getElementById('quick-panel-connections-summary').textContent === '{active - 1}/{total}'",
        timeout=10_000,
    )


def test_the_switch_is_a_real_accessible_switch(app):
    open_quick_connections(app)

    toggle = quick_row(app, "Oracle - Datamart").locator(".sw-toggle")

    assert toggle.get_attribute("role") == "switch" and "Desactivar Oracle - Datamart" in toggle.get_attribute(
        "aria-label"
    )


# ---------------------------------------------------------------------------------------------------------------------
# Configuracion > Conexiones (REQ-006)
# ---------------------------------------------------------------------------------------------------------------------
def test_each_status_row_in_settings_has_its_switch(app):
    open_connections_tab(app)

    rows = app.page.locator("#conn-status-list .conn-status-row")
    assert rows.count() >= 3
    assert app.page.locator("#conn-status-list .conn-status-row .sw-toggle").count() == rows.count()


def settings_row(app, display):
    return app.page.locator("#conn-status-list .conn-status-row", has=app.page.locator(f".conn-status-label:text-is('{display}')"))


def settings_labels(app):
    return app.page.eval_on_selector_all("#conn-status-list .conn-status-label", "els => els.map(e => e.textContent)")


def test_turning_one_off_in_settings_marks_it_pending_until_refresh_and_then_disabled_and_not_probed(app):
    open_connections_tab(app)
    settings_row(app, "Confluence - Principal").locator(".sw-toggle").click()

    app.page.wait_for_function(
        "[...document.querySelectorAll('#conn-status-list .conn-status-row')].some(r => r.textContent.includes('Confluence - Principal') && r.dataset.pending === 'true')",
        timeout=15_000,
    )
    pending = settings_row(app, "Confluence - Principal")
    assert pending.get_attribute("data-enabled") == "false" and "Actualizar estado" in pending.inner_text(), "avisa que falta actualizar"
    assert "Actualizar estado" in app.page.inner_text("#conn-status-status")

    refresh_status(app)

    off_row = settings_row(app, "Confluence - Principal")
    assert "Desactivada" in off_row.inner_text() and off_row.get_attribute("data-pending") is None
    assert off_row.locator(".connector-dot.off").count() == 1, "no se muestra ni verde ni rojo: no se la probo"


def test_toggling_never_reorders_the_list_and_it_is_alphabetical(app):
    """Antes apagar una conexion la mandaba al final de la lista («se desordena», reporte del usuario 2026-10-08)."""
    open_connections_tab(app)
    before = settings_labels(app)
    assert before == sorted(before, key=str.lower), "orden alfabetico"

    settings_row(app, before[1]).locator(".sw-toggle").click()
    app.page.wait_for_function("document.querySelector('#conn-status-list .conn-status-row[data-pending=true]') !== null", timeout=15_000)
    assert settings_labels(app) == before, "apagar no mueve ninguna fila"

    refresh_status(app)
    assert settings_labels(app) == before, "ni despues de actualizar"


def test_one_refresh_updates_settings_and_the_side_panel_with_the_same_single_probe(app):
    sidebar_names(app)
    open_connections_tab(app)
    probes = []
    app.page.on("request", lambda r: probes.append(r.url) if "/api/connectors/status" in r.url else None)

    refresh_status(app)
    app.page.wait_for_timeout(500)

    assert len(probes) == 1, "UNA sola prueba alimenta los dos lugares"
    side = app.page.eval_on_selector_all("#connectors-list li", "els => els.map(e => [e.textContent.trim(), e.querySelector('.connector-dot').className])")
    rows = app.page.eval_on_selector_all(
        "#conn-status-list .conn-status-row", "els => els.map(e => [e.querySelector('.conn-status-label').textContent, e.querySelector('.connector-dot').className])"
    )
    assert set(map(tuple, side)) <= set(map(tuple, rows)), "el panel lateral y Conexiones muestran el MISMO estado de cada conexion"


def test_opening_the_side_panel_or_the_tab_again_does_not_probe_again(app):
    sidebar_names(app)
    probes = []
    app.page.on("request", lambda r: probes.append(r.url) if "/api/connectors/status" in r.url else None)

    open_connections_tab(app)
    app.page.click("#settings-close")
    app.page.evaluate("document.getElementById('acc-connectors').open = false")
    sidebar_names(app)
    app.page.wait_for_timeout(500)

    assert probes == [], "abrir de nuevo muestra la ultima prueba; solo «Actualizar estado» vuelve a probar"


def test_settings_and_quick_panel_show_the_same_state(app):
    open_connections_tab(app)
    app.page.locator(
        "#conn-status-list .conn-status-row",
        has=app.page.locator(".conn-status-label:text-is('SharePoint - Sitio Logistico')"),
    ).locator(".sw-toggle").click()
    app.page.wait_for_function(
        "document.querySelector('#conn-status-list .conn-status-row[data-enabled=false]') !== null", timeout=15_000
    )
    app.page.click("#settings-close-btn") if app.page.query_selector(
        "#settings-close-btn"
    ) else app.page.keyboard.press("Escape")

    open_quick_connections(app)

    assert quick_row(app, "SharePoint - Sitio Logistico").locator(".sw-toggle").get_attribute("aria-checked") == "false"


def test_no_secret_reaches_the_page_through_the_switches(app):
    open_quick_connections(app)

    html = app.page.content()

    assert "tok-123456" not in html and "a@b.com" not in html


# ---------------------------------------------------------------------------------------------------------------------
# skills (REQ-009)
# ---------------------------------------------------------------------------------------------------------------------
def open_quick_skills(app):
    app.page.click("#quick-panel-btn")
    app.page.click("#quick-panel-skills-btn")
    app.page.wait_for_selector("#quick-panel-skills-list .sw-row-skill", timeout=15_000)


def skill_row(app, name):
    return app.page.locator(
        "#quick-panel-skills-list .sw-row-skill", has=app.page.locator(f".quick-panel-skill-item:text-is('{name}')")
    )


def test_every_global_skill_has_its_own_switch_next_to_the_button_that_runs_it(app, store):
    store[skill_mod.SKILLS_GLOBAL_ENABLED_KEY] = "true"
    open_quick_skills(app)

    names = app.page.eval_on_selector_all(
        "#quick-panel-skills-list .sw-row-skill .quick-panel-skill-item", "els => els.map(e => e.textContent)"
    )

    assert {"auditoria", "slotting"} <= set(names)
    assert app.page.locator("#quick-panel-skills-list .sw-row-skill .sw-toggle").count() >= 2


def test_turning_a_skill_off_does_not_run_it_and_is_remembered(app, store):
    open_quick_skills(app)
    sent = []
    app.page.on("request", lambda r: sent.append(r.url) if r.url.endswith("/api/chat") and r.method == "POST" else None)

    skill_row(app, "slotting").locator(".sw-toggle").click()
    app.page.wait_for_function(
        "[...document.querySelectorAll('#quick-panel-skills-list .sw-row-skill')].some(r => r.textContent.includes('slotting') && r.dataset.state === 'off')",
        timeout=10_000,
    )
    app.page.wait_for_timeout(500)

    assert sent == [], "apagar el interruptor NO dispara la skill (el clic no llega al boton que la ejecuta)"
    assert "slotting" in store.get("skills_disabled", "")


def test_the_general_switch_is_still_there_and_independent(app):
    open_quick_skills(app)

    assert (
        app.page.locator(
            "#quick-panel-skills-list .quick-panel-skills-section-title", has_text="Skill Globales"
        ).count()
        == 1
    )
    assert (
        app.page.locator("#quick-panel-skills-list .quick-panel-skills-section-title button", has_text="ON").count()
        == 1
    )


def test_a_skill_turned_off_stays_off_after_reopening_the_panel(app):
    open_quick_skills(app)
    skill_row(app, "auditoria").locator(".sw-toggle").click()
    app.page.wait_for_function(
        "document.querySelector('#quick-panel-skills-list .sw-row-skill[data-state=off]') !== null", timeout=10_000
    )

    app.page.reload()
    app.page.wait_for_selector("#chat-input", timeout=30_000)
    open_quick_skills(app)

    assert skill_row(app, "auditoria").locator(".sw-toggle").get_attribute("aria-checked") == "false"
    assert skill_row(app, "slotting").locator(".sw-toggle").get_attribute("aria-checked") == "true"


# ---------------------------------------------------------------------------------------------------------------------
# lo que el .env apaga (ENABLE_*) no se puede prender desde la pantalla
# ---------------------------------------------------------------------------------------------------------------------
def test_a_connection_that_the_env_switches_off_is_shown_disabled_and_its_switch_cannot_be_used(app):
    open_quick_connections(app)

    row = quick_row(app, "SAP - Principal")
    toggle = row.locator(".sw-toggle")

    assert row.locator(".sw-row-note").inner_text() == "desactivada en el .env"
    assert toggle.get_attribute("aria-checked") == "false" and toggle.is_disabled(), "el .env manda: la pantalla no puede prenderla"


def test_clicking_the_locked_switch_changes_nothing(app, store):
    open_quick_connections(app)
    sent = []
    app.page.on("request", lambda r: sent.append(r.url) if "/api/switches/connections" in r.url and r.method == "POST" else None)

    quick_row(app, "SAP - Principal").locator(".sw-toggle").click(force=True)
    app.page.wait_for_timeout(500)

    assert sent == [], "no sale ningun pedido: el interruptor bloqueado no hace nada"
    assert "connections_disabled" not in store


def test_the_other_connections_are_not_locked(app):
    open_quick_connections(app)

    assert quick_row(app, "Oracle - Datamart").locator(".sw-toggle").is_enabled()


def test_the_sensitive_values_of_the_locked_connection_never_reach_the_page(app):
    open_quick_connections(app)

    html = app.page.content()

    assert "clave-sap" not in html and "usuario-sap" not in html and "sap-interno" not in html


# ---------------------------------------------------------------------------------------------------------------------
# el texto que viene del servidor (nombres de conexion y de skill) se pinta como TEXTO, nunca como HTML
# ---------------------------------------------------------------------------------------------------------------------
def test_a_connection_name_with_html_is_shown_as_text_and_never_executes(app, store, monkeypatch):
    """Un nombre que llegue con etiquetas (por una via que se saltee la validacion, o un tipo registrado a futuro) no puede ejecutarse en la pantalla."""
    from app import switches as sw

    evil = '<img src=x onerror="window.__xss_sw=1">Oracle'
    real = sw.describe_switches

    def with_evil_name():
        rows = real()
        rows[0] = {**rows[0], "display": evil}
        return rows

    monkeypatch.setattr(sw, "describe_switches", with_evil_name)

    open_quick_connections(app)

    assert app.page.evaluate("window.__xss_sw") is None, "el onerror NO se ejecuto"
    assert app.page.query_selector("#quick-panel-connections-list img") is None, "no se creo ningun <img> a partir del nombre"
    assert "<img src=x" in app.page.inner_text("#quick-panel-connections-list"), "el HTML se ve como TEXTO"


# ---------------------------------------------------------------------------------------------------------------------
# el panel NO se cierra al usar un interruptor
# ---------------------------------------------------------------------------------------------------------------------
def test_the_quick_panel_stays_open_after_using_a_connection_switch(app):
    """Al apagar una conexion la lista se vuelve a pintar y el boton clicado queda desconectado del menu: el manejador de «cerrar al clickear afuera» (document) lo vería como
    un clic AFUERA y cerraria el panel. El interruptor corta la propagacion del clic para que eso no pase y el usuario pueda apagar varias conexiones seguidas."""
    open_quick_connections(app)

    quick_row(app, "Oracle - Datamart").locator(".sw-toggle").click()
    app.page.wait_for_function(
        "[...document.querySelectorAll('#quick-panel-connections-list .sw-row')].some(r => r.textContent.includes('Oracle - Datamart') && r.dataset.state === 'off')",
        timeout=10_000,
    )

    assert app.page.locator("#quick-panel-menu").is_visible(), "el panel rapido se cerro solo despues de usar un interruptor"
    quick_row(app, "SharePoint - Sitio Logistico").locator(".sw-toggle").click()
    app.page.wait_for_function(
        "[...document.querySelectorAll('#quick-panel-connections-list .sw-row')].some(r => r.textContent.includes('SharePoint - Sitio Logistico') && r.dataset.state === 'off')",
        timeout=10_000,
    )


def test_the_quick_panel_stays_open_after_using_a_skill_switch(app):
    open_quick_skills(app)

    skill_row(app, "auditoria").locator(".sw-toggle").click()
    app.page.wait_for_function(
        "[...document.querySelectorAll('#quick-panel-skills-list .sw-row-skill')].some(r => r.textContent.includes('auditoria') && r.dataset.state === 'off')",
        timeout=10_000,
    )

    assert app.page.locator("#quick-panel-menu").is_visible()


# ---------------------------------------------------------------------------------------------------------------------
# Pedido del usuario 2026-10-10: «las pelotitas del toggle deben ser del mismo color como las que teniamos, blancos» — «para todos los
# toggles de la aplicacion». Los interruptores de conexiones y skills tenian la pelotita del color del texto (casi negra en el tema claro).
# ---------------------------------------------------------------------------------------------------------------------
KNOBS_JS = """() => ({
    sw: [...document.querySelectorAll('.sw-toggle-knob')].map(k => getComputedStyle(k).backgroundColor),
    quick: [...document.querySelectorAll('.quick-panel-toggle-knob')].map(k => getComputedStyle(k).backgroundColor),
})"""


@pytest.mark.parametrize("theme", ["light", "dark", "dark-blue"])
def test_every_switch_knob_is_white_in_every_theme(app, theme):
    open_quick_connections(app)  # arma los interruptores de conexiones
    app.page.evaluate(
        "(t) => { if (t === 'dark') delete document.documentElement.dataset.theme; else document.documentElement.dataset.theme = t; }", theme
    )

    knobs = app.page.evaluate(KNOBS_JS)

    assert knobs["sw"] and knobs["quick"], "hay interruptores de los dos tipos en pantalla"
    assert set(knobs["sw"]) == {"rgb(255, 255, 255)"}, f"conexiones/skills: {set(knobs['sw'])}"
    assert set(knobs["quick"]) == {"rgb(255, 255, 255)"}, f"panel rapido: {set(knobs['quick'])}"

