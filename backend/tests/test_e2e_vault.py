"""E2E de la pantalla del gestor de conexiones (spec gestor-conexiones Fase B: REQ-003/004b/005/006/007/007b/008/009/010) en un Chromium REAL contra el backend real.

Lo que ve el usuario en Configuracion > Conexiones: si tiene un .env sin importar, SOLO el aviso de importar; despues, el menu «Conectores» con su ventana de
credenciales: tarjetas sin secretos, alta con nombre obligatorio, el ojo que muestra una contrasena guardada SOLO despues de Windows Hello (aca un verificador FALSO:
ningun test abre la ventana real), cerrar la ventana bloquea de nuevo, borrar pide confirmacion y la key de BigQuery se importa pegada o como archivo.
Base maestra temporal por test (fixture autouse) y DPAPI real; los archivos viejos (.env, connections.json, .env.enc) viven en una carpeta temporal."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app import config
from app import config_store as cs
from app import connections as conns
from app import credentials
from app import secret_access as sa
from app import secret_store
from e2e_support import FakeWhisper, launch_chromium, open_app, serve_backend

pytestmark = [pytest.mark.e2e, pytest.mark.skipif(not secret_store.is_available(), reason="DPAPI solo existe en Windows")]

MODEL = "claude-sonnet-4-6"
PASSWORD = "Clave-Planta-4242"
KEY = {"type": "service_account", "project_id": "psapmpldash-e-s-110", "private_key": "CLAVE-PRIVADA-FALSA-E2E", "client_email": "sa@p.iam.gserviceaccount.com"}


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
def legacy_dir(monkeypatch, tmp_path):
    """Cada test arranca SIN archivos viejos y con la base inactiva (instalacion nueva); el test que necesita un .env lo escribe aca."""
    monkeypatch.setattr(config, "BASE_DIR", tmp_path)
    monkeypatch.setattr(credentials, "ENC_FILE", tmp_path / ".env.enc")
    monkeypatch.setattr(conns, "CONNECTIONS_FILE", tmp_path / "connections.json")
    monkeypatch.setitem(config.Settings.model_config, "env_file", str(tmp_path / ".env"))
    conns.reset_for_tests()
    cs.invalidate()
    yield tmp_path
    conns.reset_for_tests()
    cs.invalidate()
    config.get_settings.cache_clear()  # una importacion modifica el Settings vivo: no se arrastra a otros tests


@pytest.fixture()
def app(browser, base_url):
    with open_app(browser, base_url) as a:
        a.open()
        yield a


def open_connections_tab(app):
    app.page.click("#settings-btn")
    app.page.click('.settings-tab[data-tab="connections"]')
    app.page.wait_for_selector("#vault-root .vault-head", timeout=20_000)


def open_connector(app, title):
    app.page.click("#vault-menu-btn")
    app.page.click(f'.vault-menu-item[data-connector="{title}"]')
    app.page.wait_for_selector("#vault-modal", timeout=10_000)


def add_oracle(name="Planta", **over):
    fields = {"oracle_user": "u-planta", "oracle_password": PASSWORD, "oracle_host": "h-planta.empresa.com", "oracle_service_name": "SVC"}
    fields.update(over)
    return cs.add_connection("oracle", name, fields)


# ---------------------------------------------------------------------------------------------------------------------
# REQ-003: con un .env sin importar, solo el aviso de importar
# ---------------------------------------------------------------------------------------------------------------------
def test_with_an_env_not_imported_only_the_import_banner_is_shown_and_importing_works(app, legacy_dir):
    (legacy_dir / ".env").write_text("ORACLE_HOST=h\nORACLE_USER=u\nORACLE_PASSWORD=p\nORACLE_SERVICE_NAME=s\n", encoding="utf-8")
    open_connections_tab(app)

    assert app.page.locator("#vault-import-banner").is_visible()
    assert app.page.locator("#vault-menu-btn").count() == 0, "no se puede cargar nada antes de importar"

    app.page.click("#vault-import-btn")
    app.page.wait_for_selector("#vault-menu-btn", timeout=15_000)

    assert cs.is_active() is True and app.page.locator("#vault-import-banner").count() == 0
    assert "Oracle - Datamart: importada" in app.page.inner_text("#vault-import-done"), "el informe queda a la vista despues de importar"
    assert "Oracle - Datamart" in app.page.inner_text("#vault-summary")


# ---------------------------------------------------------------------------------------------------------------------
# REQ-011 (Fase C): despues de importar, borrar los archivos viejos con confirmacion
# ---------------------------------------------------------------------------------------------------------------------
def import_from_env(app, legacy_dir):
    (legacy_dir / ".env").write_text("ORACLE_HOST=h\nORACLE_USER=u\nORACLE_PASSWORD=p\nORACLE_SERVICE_NAME=s\n", encoding="utf-8")
    open_connections_tab(app)
    app.page.click("#vault-import-btn")
    app.page.wait_for_selector("#vault-delete-legacy-banner", timeout=15_000)


def test_after_importing_the_old_files_can_be_deleted_after_confirming(app, legacy_dir):
    import_from_env(app, legacy_dir)

    app.page.click("#vault-delete-legacy-btn")
    app.page.wait_for_selector("#confirm-overlay:not(.hidden)", timeout=5_000)
    app.page.click("#confirm-accept-btn")
    app.page.wait_for_function("document.getElementById('vault-delete-legacy-banner') === null", timeout=15_000)

    assert not (legacy_dir / ".env").exists(), "el .env ya no existe"
    assert [c.name for c in cs.list_stored()] == ["Datamart"], "lo importado sigue en Vision"


def test_cancelling_keeps_the_old_files(app, legacy_dir):
    import_from_env(app, legacy_dir)

    app.page.click("#vault-delete-legacy-btn")
    app.page.wait_for_selector("#confirm-overlay:not(.hidden)", timeout=5_000)
    app.page.click("#confirm-cancel-btn")
    app.page.wait_for_timeout(500)

    assert (legacy_dir / ".env").exists() and app.page.locator("#vault-delete-legacy-banner").is_visible()


# ---------------------------------------------------------------------------------------------------------------------
# B5: el proveedor de IA, Red, General y Graph (configuracion general)
# ---------------------------------------------------------------------------------------------------------------------
def save_section(app):
    app.page.click("#vault-modal #vault-save-btn")
    app.page.wait_for_function("document.querySelector('#vault-modal .vault-notice[data-state=ok]') !== null || document.querySelector('#vault-modal .vault-notice') === null", timeout=15_000)


def test_saving_a_general_section_keeps_the_confirmation_visible(app):
    """Revision 2026-10-09: guardar una seccion general volvia a dibujar la ventana y el aviso «Guardado.» desaparecia en el acto
    (el usuario no veia confirmacion; tambien hacia intermitente a `save_section`). La ventana redibujada lo conserva."""
    open_connections_tab(app)
    open_connector(app, "proveedor")
    app.page.fill("#vault-field-llm_base_url", "cliente-visible")
    app.page.click("#vault-modal #vault-save-btn")

    app.page.wait_for_function("document.querySelector('#vault-modal #vault-field-llm_base_url')?.value === 'cliente-visible'", timeout=15_000)
    app.page.wait_for_timeout(500)  # ya redibujada y con los datos del servidor
    notice = app.page.locator("#vault-modal .vault-notice")
    assert notice.inner_text() == "Guardado." and notice.get_attribute("data-state") == "ok"


def test_proveedor_credentials_are_saved_from_the_screen(app):
    open_connections_tab(app)
    open_connector(app, "proveedor")
    app.page.fill("#vault-field-llm_base_url", "cliente-123")
    app.page.fill("#vault-field-llm_api_key", "clave-de-prueba")
    save_section(app)

    app.page.wait_for_function("document.getElementById('vault-modal') !== null", timeout=5_000)
    general = cs.get_general()
    assert general["llm_base_url"] == "cliente-123" and general["llm_api_key"] == "clave-de-prueba"
    assert "clave-de-prueba" not in app.page.content()


def test_a_proxy_fixed_by_windows_is_shown_locked(app, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy-de-windows:80")
    open_connections_tab(app)
    open_connector(app, "red")
    app.page.wait_for_selector("#vault-modal #vault-save-btn", timeout=10_000)  # la seccion se arma despues de leer /api/vault/general

    text = app.page.inner_text("#vault-modal")
    assert "Lo fija una variable de entorno de Windows" in text
    assert app.page.locator("#vault-field-https_proxy").count() == 0, "no se ofrece editar lo que Windows manda"


def test_general_settings_eye_reveals_the_lan_token_only_after_windows_hello(app):
    cs.set_general({"access_token": "token-lan-777"})
    sa.set_verifier(lambda: True)
    open_connections_tab(app)
    open_connector(app, "general")

    app.page.click("#vault-modal .vault-secret .vault-eye")
    app.page.wait_for_function("document.getElementById('vault-field-access_token').value === 'token-lan-777'", timeout=10_000)


def test_graph_takes_the_sharepoint_chosen_in_its_card(app):
    sp = {"sharepoint_site_url": "https://empresa.sharepoint.com/sites/x", "azure_client_id": "c", "azure_client_secret": "s", "azure_tenant_id": "t"}
    cs.add_connection("sharepoint", "Sitio Logistico", sp)
    cs.add_connection("sharepoint", "Material Industrial", sp)
    open_connections_tab(app)
    open_connector(app, "graph")

    app.page.select_option("#vault-field-graph_source", "Material Industrial")
    save_section(app)

    app.page.wait_for_function("document.getElementById('vault-modal') !== null", timeout=5_000)
    assert cs.get_general()["graph_source"] == "Material Industrial"


# ---------------------------------------------------------------------------------------------------------------------
# REQ-004b: nombre obligatorio; REQ-005/010: tarjeta sin secretos
# ---------------------------------------------------------------------------------------------------------------------
def test_a_connection_cannot_be_saved_without_a_valid_name(app):
    open_connections_tab(app)
    open_connector(app, "oracle")
    app.page.click("#vault-add-btn")

    save = app.page.locator("#vault-save-btn")
    assert save.is_disabled() and "Poné un nombre" in app.page.inner_text("#vault-form")

    app.page.fill("#vault-field-name", "Con_guion_bajo")
    assert save.is_disabled(), "la misma regla que el servidor"

    app.page.fill("#vault-field-name", "Planta")
    assert save.is_enabled()


def test_a_new_connection_shows_up_as_a_card_without_its_password(app):
    open_connections_tab(app)
    open_connector(app, "oracle")
    app.page.click("#vault-add-btn")
    app.page.fill("#vault-field-name", "Planta")
    app.page.fill("#vault-field-oracle_user", "u-planta")
    app.page.fill("#vault-field-oracle_password", PASSWORD)
    app.page.fill("#vault-field-oracle_host", "h-planta.empresa.com")
    app.page.click("#vault-save-btn")

    app.page.wait_for_selector("#vault-modal .vault-card", timeout=15_000)
    card = app.page.inner_text("#vault-modal .vault-card")
    assert "Planta" in card and "u-planta" in card and "•••••• (guardada)" in card
    assert PASSWORD not in app.page.content(), "la contrasena no esta en ningun lado de la pagina"
    assert [c.name for c in cs.list_stored()] == ["Planta"]


# ---------------------------------------------------------------------------------------------------------------------
# REQ-007/007b: el ojo y el desbloqueo
# ---------------------------------------------------------------------------------------------------------------------
def edit_first_card(app):
    app.page.click('#vault-modal .vault-card [data-action="edit"]')
    app.page.wait_for_selector("#vault-form", timeout=10_000)


def test_the_eye_does_not_reveal_a_stored_password_without_windows_hello(app):
    add_oracle()
    open_connections_tab(app)
    open_connector(app, "oracle")
    edit_first_card(app)

    app.page.click("#vault-form .vault-secret .vault-eye")
    app.page.wait_for_function("document.querySelector('#vault-form .vault-notice').textContent.length > 0", timeout=10_000)

    pw = app.page.locator("#vault-field-oracle_password")
    assert pw.input_value() == "" and pw.get_attribute("type") == "password"
    assert "Windows Hello" in app.page.inner_text("#vault-form .vault-notice")


def test_the_eye_reveals_the_stored_password_after_windows_hello_and_closing_locks_again(app):
    add_oracle()
    sa.set_verifier(lambda: True)  # Windows Hello FALSO: confirma
    open_connections_tab(app)
    open_connector(app, "oracle")
    edit_first_card(app)

    app.page.click("#vault-form .vault-secret .vault-eye")
    app.page.wait_for_function(f"document.getElementById('vault-field-oracle_password').value === '{PASSWORD}'", timeout=10_000)
    assert app.page.locator("#vault-field-oracle_password").get_attribute("type") == "text"
    assert sa.is_unlocked() is True

    app.page.click(".vault-modal-close")
    app.page.wait_for_function("document.getElementById('vault-modal') === null", timeout=5_000)
    app.page.wait_for_timeout(500)
    assert sa.is_unlocked() is False, "cerrar la ventana bloquea de nuevo"


def test_changing_a_stored_password_asks_for_windows_hello_and_saves_it(app):
    stored = add_oracle()
    sa.set_verifier(lambda: True)
    open_connections_tab(app)
    open_connector(app, "oracle")
    edit_first_card(app)

    app.page.fill("#vault-field-oracle_password", "Nueva-Clave-1")
    app.page.click("#vault-save-btn")
    app.page.wait_for_selector("#vault-modal .vault-card", timeout=15_000)

    assert cs.get_stored(stored.id).fields["oracle_password"] == "Nueva-Clave-1"


# ---------------------------------------------------------------------------------------------------------------------
# REQ-009: borrar pide confirmacion
# ---------------------------------------------------------------------------------------------------------------------
def test_deleting_asks_for_confirmation_and_removes_the_card(app):
    add_oracle()
    open_connections_tab(app)
    open_connector(app, "oracle")

    app.page.click('#vault-modal .vault-card [data-action="delete"]')
    app.page.wait_for_selector("#confirm-overlay:not(.hidden)", timeout=5_000)
    assert app.page.locator("#confirm-accept-btn").is_visible(), "la confirmacion queda ARRIBA de la ventana del gestor"
    app.page.click("#confirm-accept-btn")

    app.page.wait_for_function("document.querySelectorAll('#vault-modal .vault-card').length === 0", timeout=10_000)
    assert cs.list_stored() == []


def test_cancelling_the_confirmation_keeps_the_connection(app):
    add_oracle()
    open_connections_tab(app)
    open_connector(app, "oracle")

    app.page.click('#vault-modal .vault-card [data-action="delete"]')
    app.page.wait_for_selector("#confirm-overlay:not(.hidden)", timeout=5_000)
    app.page.click("#confirm-cancel-btn")
    app.page.wait_for_timeout(500)

    assert len(cs.list_stored()) == 1 and app.page.locator("#vault-modal .vault-card").count() == 1


# ---------------------------------------------------------------------------------------------------------------------
# REQ-008: key de BigQuery pegada o como archivo
# ---------------------------------------------------------------------------------------------------------------------
def test_pasting_the_bigquery_key_saves_it_and_autocompletes_the_project(app):
    open_connections_tab(app)
    open_connector(app, "bigquery")
    app.page.fill("#vault-bq-paste", json.dumps(KEY))
    app.page.click("#vault-bq-paste-btn")

    app.page.wait_for_function("document.getElementById('vault-modal').textContent.includes('psapmpldash-e-s-110')", timeout=15_000)
    assert json.loads(cs.get_general()["bq_key_json"]) == KEY and cs.get_general()["bq_project_id"] == "psapmpldash-e-s-110"
    assert "CLAVE-PRIVADA-FALSA-E2E" not in app.page.content()


def test_choosing_the_bigquery_key_file_imports_it(app, tmp_path):
    key_file = tmp_path / "clave.json"
    key_file.write_text(json.dumps(KEY), encoding="utf-8")
    open_connections_tab(app)
    open_connector(app, "bigquery")

    app.page.set_input_files("#vault-bq-file", str(key_file))

    app.page.wait_for_function("document.getElementById('vault-modal').textContent.includes('sa@p.iam.gserviceaccount.com')", timeout=15_000)
    assert json.loads(cs.get_general()["bq_key_json"])["client_email"] == KEY["client_email"]


def test_something_that_is_not_a_key_is_refused_with_a_clear_message(app):
    open_connections_tab(app)
    open_connector(app, "bigquery")
    app.page.fill("#vault-bq-paste", '{"type": "authorized_user"}')
    app.page.click("#vault-bq-paste-btn")

    # Pedido del usuario 2026-10-10: el resultado se ve EN el recuadro de la key (antes, en el aviso de abajo de todo)
    app.page.wait_for_selector("#vault-bq-box[data-state=error]", timeout=10_000)
    assert "cuenta de servicio" in app.page.inner_text("#vault-bq-result")
    assert "bq_key_json" not in cs.get_general()


# ---------------------------------------------------------------------------------------------------------------------
# REQ-010 / XSS: lo que viene del servidor se pinta como TEXTO
# ---------------------------------------------------------------------------------------------------------------------
def test_a_field_value_with_html_is_shown_as_text_and_never_executes(app):
    add_oracle(oracle_host="<img src=x onerror=window.__xss_vault=1>")
    open_connections_tab(app)
    open_connector(app, "oracle")

    assert app.page.evaluate("window.__xss_vault") is None
    assert app.page.query_selector("#vault-modal img") is None
    assert "<img src=x" in app.page.inner_text("#vault-modal .vault-card")


# ---------------------------------------------------------------------------------------------------------------------
# Contraste (reporte del usuario 2026-10-09: «fondo marron feo y letras oscuras, casi ni se ve», en el tema claro). Los fondos
# --signal-*-bg son oscuros en TODOS los temas (el claro no los redefine) y --ink es oscuro en el claro: 1,4:1. Se mide de verdad
# en Chromium (WCAG: 4,5:1 minimo para texto normal), en el tema claro y en el oscuro.
# ---------------------------------------------------------------------------------------------------------------------
CONTRAST_JS = """(sel) => {
  const toRgb = (css) => {
    const c = document.createElement('canvas'); c.width = c.height = 1;
    const x = c.getContext('2d'); x.fillStyle = '#000'; x.fillStyle = css; x.fillRect(0, 0, 1, 1);
    const d = x.getImageData(0, 0, 1, 1).data; return [d[0], d[1], d[2]];
  };
  const lum = (rgb) => {
    const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; };
    return 0.2126 * f(rgb[0]) + 0.7152 * f(rgb[1]) + 0.0722 * f(rgb[2]);
  };
  return [...document.querySelectorAll(sel)].filter((el) => el.textContent.trim()).map((el) => {
    let bgEl = el, bg = getComputedStyle(el).backgroundColor;
    while (bgEl && (bg === 'rgba(0, 0, 0, 0)' || bg === 'transparent')) {
      bgEl = bgEl.parentElement;
      bg = bgEl ? getComputedStyle(bgEl).backgroundColor : 'rgb(255, 255, 255)';
    }
    const a = lum(toRgb(getComputedStyle(el).color)), b = lum(toRgb(bg));
    return [el.textContent.trim().slice(0, 50), Math.round(((Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05)) * 100) / 100];
  });
}"""


def set_theme(app, theme):
    app.page.evaluate("(t) => { if (t === 'dark') delete document.documentElement.dataset.theme; else document.documentElement.dataset.theme = t; }", theme)


def assert_readable(app, selector, minimum=4.5):
    measured = app.page.evaluate(CONTRAST_JS, selector)
    assert measured, f"no hay texto que medir en {selector}"
    unreadable = [m for m in measured if m[1] < minimum]
    assert not unreadable, f"texto ilegible (contraste < {minimum}:1): {unreadable}"


@pytest.mark.parametrize("theme", ["light", "dark", "dark-blue"])
def test_the_import_notices_are_readable_in_every_theme(app, legacy_dir, theme):
    (legacy_dir / ".env").write_text("ORACLE_HOST=h\nORACLE_USER=u\nORACLE_PASSWORD=p\nORACLE_SERVICE_NAME=s\n", encoding="utf-8")
    set_theme(app, theme)
    open_connections_tab(app)
    assert_readable(app, "#vault-import-banner p")

    app.page.click("#vault-import-btn")
    app.page.wait_for_selector("#vault-delete-legacy-banner", timeout=15_000)
    assert_readable(app, "#vault-import-done p, #vault-import-done li, #vault-delete-legacy-banner p")


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_the_saved_notice_and_the_badges_are_readable_in_every_theme(app, theme):
    sp = {"sharepoint_site_url": "https://empresa.sharepoint.com/sites/x", "azure_client_id": "c", "azure_client_secret": "s", "azure_tenant_id": "t"}
    cs.add_connection("sharepoint", "Sitio Logistico", sp)  # una conexion = una fila con su etiqueta de estado
    set_theme(app, theme)
    open_connections_tab(app)
    assert_readable(app, "#vault-root .vault-badge")
    open_connector(app, "proveedor")
    app.page.fill("#vault-field-llm_base_url", "cliente-contraste")
    app.page.click("#vault-modal #vault-save-btn")
    app.page.wait_for_selector("#vault-modal .vault-notice[data-state=ok]", timeout=15_000)
    assert_readable(app, "#vault-modal .vault-notice[data-state=ok]")


# ---------------------------------------------------------------------------------------------------------------------
# Pedidos del usuario 2026-10-09 (tras importar en DEV): la pestaña Credenciales ya no cumple ninguna funcion con la configuracion
# en Vision, y la voz (el ID) se elige en Configuracion > Audio, no en la tarjeta Voz.
# ---------------------------------------------------------------------------------------------------------------------
CREDENTIALS_TAB = '.settings-tab[data-tab="credentials"]'


def test_the_credentials_tab_is_hidden_once_the_configuration_is_in_vision(app):
    cs.set_general({"fish_api_key": "Clave-Fish"})  # cualquier guardado en el gestor activa la base
    app.page.click("#settings-btn")
    app.page.wait_for_function(f"getComputedStyle(document.querySelector('{CREDENTIALS_TAB}')).display === 'none'", timeout=10_000)
    assert app.page.locator('.settings-tab[data-tab="connections"]').is_visible()


def test_before_importing_the_credentials_tab_is_still_there(app, legacy_dir):
    (legacy_dir / ".env").write_text("ORACLE_HOST=h\nORACLE_USER=u\nORACLE_PASSWORD=p\nORACLE_SERVICE_NAME=s\n", encoding="utf-8")
    app.page.click("#settings-btn")
    app.page.wait_for_timeout(1500)  # tiempo de sobra para que la consulta del estado la oculte si estuviera mal
    assert app.page.locator(CREDENTIALS_TAB).is_visible(), "sin importar sigue siendo el lugar de las claves de voz y BigQuery"


def test_the_voice_card_has_only_the_keys_and_points_to_audio_for_the_voice(app):
    open_connections_tab(app)
    open_connector(app, "voz")
    app.page.wait_for_selector("#vault-field-fish_api_key", timeout=10_000)

    assert app.page.locator("#vault-field-elevenlabs_api_key").count() == 1
    assert app.page.locator("#vault-field-fish_voice_id").count() == 0
    assert app.page.locator("#vault-field-elevenlabs_voice_id").count() == 0
    assert "Configuración > Audio" in app.page.inner_text("#vault-modal")


# ---------------------------------------------------------------------------------------------------------------------
# Pedidos del usuario 2026-10-10: «Probar» antes de guardar, y la key de BigQuery con su resultado a la vista (verde/rojo) y probada sola
# ---------------------------------------------------------------------------------------------------------------------
def test_the_connection_form_can_be_tested_before_saving_and_nothing_is_saved(app):
    open_connections_tab(app)
    open_connector(app, "oracle")
    app.page.click("#vault-add-btn")
    app.page.fill("#vault-field-name", "Planta")
    for key, value in {"oracle_user": "u", "oracle_host": "h.empresa.com", "oracle_service_name": "s"}.items():
        app.page.fill(f"#vault-field-{key}", value)
    app.page.fill("#vault-field-oracle_password", "Clave-De-Prueba-E2E")

    app.page.click("#vault-form-test-btn")

    app.page.wait_for_function("['ok', 'error'].includes(document.getElementById('vault-form-test-result')?.dataset.state)", timeout=20_000)
    result = app.page.inner_text("#vault-form-test-result")
    assert result and "Clave-De-Prueba-E2E" not in result  # sin red en los tests: falla, pero dice por que
    assert cs.list_stored() == [], "probar no guarda nada"
    assert app.page.locator("#vault-form").is_visible(), "el formulario sigue abierto para guardar despues"


def test_a_good_key_file_turns_the_box_green_and_is_tested_right_away(app, tmp_path):
    key_file = tmp_path / "clave.json"
    key_file.write_text(json.dumps(KEY), encoding="utf-8")
    open_connections_tab(app)
    open_connector(app, "bigquery")

    app.page.set_input_files("#vault-bq-file", str(key_file))

    app.page.wait_for_selector("#vault-bq-box[data-state=ok]", timeout=15_000)
    assert "Clave importada" in app.page.inner_text("#vault-bq-result")
    app.page.wait_for_function("['ok', 'error'].includes(document.getElementById('vault-bq-test-result')?.dataset.state)", timeout=20_000)
    assert app.page.inner_text("#vault-bq-test-result"), "se probo sola apenas se guardo"


def test_a_wrong_key_file_turns_the_box_red(app, tmp_path):
    bad = tmp_path / "no-es-clave.json"
    bad.write_text('{"type": "authorized_user"}', encoding="utf-8")
    open_connections_tab(app)
    open_connector(app, "bigquery")

    app.page.set_input_files("#vault-bq-file", str(bad))

    app.page.wait_for_selector("#vault-bq-box[data-state=error]", timeout=15_000)
    assert "No se importó" in app.page.inner_text("#vault-bq-result")


def test_the_same_key_file_can_be_chosen_again(app, tmp_path):
    """Antes, elegir el MISMO archivo otra vez (p. ej. despues de cancelar Windows Hello) no hacia nada: el navegador no avisa un cambio."""
    sa.set_verifier(lambda: True)  # Windows Hello FALSO: confirma (reemplazar una key guardada pide desbloqueo)
    key_file = tmp_path / "clave.json"
    key_file.write_text(json.dumps(KEY), encoding="utf-8")
    open_connections_tab(app)
    open_connector(app, "bigquery")
    posts = []
    app.page.on("request", lambda r: posts.append(r.url) if r.method == "POST" and r.url.endswith("/api/vault/bigquery-key") else None)

    app.page.set_input_files("#vault-bq-file", str(key_file))
    app.page.wait_for_selector("#vault-bq-box[data-state=ok]", timeout=15_000)
    app.page.set_input_files("#vault-bq-file", str(key_file))

    app.page.wait_for_function("() => document.getElementById('vault-bq-result').textContent.includes('Clave importada')", timeout=15_000)
    app.page.wait_for_timeout(1500)
    assert len(posts) == 2, f"la segunda eleccion del mismo archivo tambien se importa (pedidos: {len(posts)})"


def test_the_bigquery_section_can_be_tested_before_saving(app):
    open_connections_tab(app)
    open_connector(app, "bigquery")
    app.page.fill("#vault-field-bq_project_id", "otro-proyecto")

    app.page.click("#vault-section-test-btn")

    app.page.wait_for_function("['ok', 'error'].includes(document.getElementById('vault-section-test-result')?.dataset.state)", timeout=20_000)
    assert app.page.inner_text("#vault-section-test-result")
    assert cs.get_general().get("bq_project_id") != "otro-proyecto", "probar no guarda"


# ---------------------------------------------------------------------------------------------------------------------
# Pedido del usuario 2026-10-10: «entre los conectores a probar no veo BigQuery», y despues de importar la key «Guardar» decia
# «No hay cambios para guardar» (la key ya se habia guardado al importarla, pero parecia que no la habia tomado).
# ---------------------------------------------------------------------------------------------------------------------
def summary_row(app, text):
    return app.page.locator("#vault-summary .vault-summary-row", has_text=text)


def test_bigquery_shows_up_in_the_summary_with_its_key_state_and_opens_its_section(app):
    cs.set_general({"bq_key_json": json.dumps(KEY), "bq_project_id": "psapmpldash-e-s-110"})
    open_connections_tab(app)

    row = summary_row(app, "BigQuery - psapmpldash-e-s-110")
    row.wait_for(timeout=10_000)
    assert row.locator(".vault-badge").inner_text() == "Clave guardada"
    row.click()
    app.page.wait_for_selector("#vault-modal[aria-label='Credenciales de BigQuery']", timeout=10_000)


def test_without_a_key_bigquery_says_so_in_the_summary(app):
    cs.set_general({"bq_project_id": "psapmpldash-e-s-110"})
    open_connections_tab(app)

    row = summary_row(app, "BigQuery")
    row.wait_for(timeout=10_000)
    assert row.locator(".vault-badge").inner_text() == "Sin clave"


def test_without_anything_of_bigquery_there_is_no_bigquery_row(app):
    sp = {"sharepoint_site_url": "https://empresa.sharepoint.com/sites/x", "azure_client_id": "c", "azure_client_secret": "s", "azure_tenant_id": "t"}
    cs.add_connection("sharepoint", "Sitio Logistico", sp)
    open_connections_tab(app)

    summary_row(app, "SharePoint").wait_for(timeout=10_000)
    assert summary_row(app, "BigQuery").count() == 0


def test_after_testing_bigquery_the_summary_shows_the_result(app):
    cs.set_general({"bq_key_json": json.dumps(KEY), "bq_project_id": "psapmpldash-e-s-110"})
    open_connections_tab(app)
    open_connector(app, "bigquery")

    app.page.click("#vault-section-test-btn")
    app.page.wait_for_function("['ok', 'error'].includes(document.getElementById('vault-section-test-result')?.dataset.state)", timeout=30_000)
    expected = "Probada" if app.page.get_attribute("#vault-section-test-result", "data-state") == "ok" else "Error"

    app.page.wait_for_function(
        "t => [...document.querySelectorAll('#vault-summary .vault-summary-row')].some(r => r.textContent.includes('BigQuery') && r.textContent.includes(t))",
        arg=expected,
        timeout=10_000,
    )


def test_after_importing_the_key_saving_says_it_was_already_saved(app, tmp_path):
    cs.set_general({"bq_project_id": KEY["project_id"]})  # como el usuario: el proyecto ya estaba, importar no cambia nada mas
    key_file = tmp_path / "clave.json"
    key_file.write_text(json.dumps(KEY), encoding="utf-8")
    open_connections_tab(app)
    open_connector(app, "bigquery")
    app.page.set_input_files("#vault-bq-file", str(key_file))
    app.page.wait_for_selector("#vault-bq-box[data-state=ok]", timeout=15_000)

    app.page.click("#vault-modal #vault-save-btn")

    app.page.wait_for_function("document.querySelector('#vault-modal .vault-notice')?.textContent.includes('ya quedó guardada')", timeout=10_000)
    assert "No hay cambios" not in app.page.inner_text("#vault-modal .vault-notice")
