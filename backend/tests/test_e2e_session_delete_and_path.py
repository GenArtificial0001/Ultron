"""E2E del modal «Eliminar sesion» y del chip de carpeta en un Chromium REAL (spec indice-resiliente-y-borrado-seguro,
REQ-011 y REQ-012).

Pedido del usuario (2026-10-22): «vision esta teniendo un error: perdio la referencia de ruta de todas las carpetas... y solo toma
ahora todas como C:\\Users\\...\\AppData\\Local\\Vision». Lo que se mide: (1) que el modal diga cuantos mensajes se van a borrar, de que
carpeta, y si el mismo id existe en otra, SIN agregar pasos ni botones, y (2) que una sesion sin carpeta registrada diga
«Sin carpeta registrada» en el chip en vez de la carpeta de Vision, y que el modal de edicion no se pre-llene con ese texto.

El backend es el real (sesiones sembradas con la fachada de persistencia sobre `tmp_path`); solo se espia, con `page.route`, la
respuesta de `/status` para el caso de la sesion sin carpeta. LIMITE: se prueba el texto y el comportamiento, no que las frases le
resulten claras al usuario: eso solo lo puede decir el mirandolas."""

from __future__ import annotations

import json

import pytest

from app.persistence import db, master_index
from app.persistence import service as svc
from e2e_support import launch_chromium, open_app, serve_backend

pytestmark = pytest.mark.e2e

NOW = 1_800_000_000.0
USER_TAG = "tester_20260101000000"


@pytest.fixture(scope="module")
def base_url():
    with serve_backend(None) as url:
        yield url


@pytest.fixture(scope="module")
def browser(tmp_path_factory, base_url):
    with launch_chromium(tmp_path_factory.mktemp("delpath"), base_url) as b:
        yield b


@pytest.fixture
def app(browser, base_url):
    with open_app(browser, base_url) as a:
        a.open()
        yield a


@pytest.fixture
def folders(tmp_path):
    a = tmp_path / "carpeta-a"
    b = tmp_path / "carpeta-b"
    a.mkdir()
    b.mkdir()
    return a, b


def seed(folder, sid, messages, title):
    """Sesion REAL en la base de `folder`, del usuario de la prueba, con `messages` mensajes."""
    path = db.db_path_for(str(folder))
    db.ensure_session(path, sid, NOW)
    import sqlite3

    conn = sqlite3.connect(path)
    try:
        conn.execute("UPDATE session SET windows_user_created = ?, title = ? WHERE id = ?", (USER_TAG, title, sid))
        conn.commit()
    finally:
        conn.close()
    for n in range(messages):
        db.insert_message(path, f"{sid}-m{n}", sid, "user", NOW + n)
        db.insert_part(path, f"{sid}-p{n}", f"{sid}-m{n}", sid, "text", {"text": f"hola {n}"}, NOW + n)


@pytest.fixture(autouse=True)
def tester_is_the_current_user(monkeypatch):
    monkeypatch.setenv("USERNAME", "tester")
    monkeypatch.setattr(svc, "current_windows_user", lambda: "tester")


def show_session_in_list(app, sid, name):
    """Pone la sesion en la lista del panel (la lista sale del indice liviano del navegador) y la dibuja."""
    app.page.evaluate(
        """([sid, name]) => {
          const idx = JSON.parse(localStorage.getItem('vision.sessionsIndex') || '{}');
          idx[sid] = { name, updatedAt: Date.now() + 1000, manuallyRenamed: true };
          localStorage.setItem('vision.sessionsIndex', JSON.stringify(idx));
        }""",
        [sid, name],
    )
    app.page.reload(wait_until="domcontentloaded")
    app.wait_ready()
    app.page.evaluate("document.getElementById('acc-session').open = true")


def click_delete_and_read_modal(app, name):
    row = app.page.locator("#session-list li", has_text=name).first
    row.hover()
    row.locator(".session-delete-btn").click()
    app.page.wait_for_selector("#confirm-overlay:not(.hidden)")
    text = app.page.inner_text("#confirm-message")
    buttons = app.page.locator("#confirm-overlay button:visible").all_inner_texts()
    return text, buttons


def cancel_modal(app):
    app.page.click("#confirm-cancel-btn")


# ---- REQ-011: el modal de borrado informa -----------------------------------------------------------------------------
class TestTheDeleteModalSaysWhatWillBeLost:
    def test_it_names_the_message_count_and_the_folder(self, app, folders):
        a, _ = folders
        seed(a, "con-mensajes", 37, "Con mensajes")
        svc.assign_project_path("con-mensajes", str(a), "Con mensajes")
        show_session_in_list(app, "con-mensajes", "Con mensajes")
        text, _ = click_delete_and_read_modal(app, "Con mensajes")
        assert "37 mensajes guardados" in text
        assert str(a) in text
        assert "no se tocan" in text

    def test_it_keeps_its_original_text_and_buttons(self, app, folders):
        a, _ = folders
        seed(a, "original", 3, "Original")
        svc.assign_project_path("original", str(a), "Original")
        show_session_in_list(app, "original", "Original")
        text, buttons = click_delete_and_read_modal(app, "Original")
        assert text.startswith('¿Eliminar la sesión "Original"? Esta acción no se puede deshacer.')
        assert buttons == ["✕", "Cancelar", "Eliminar"]  # los mismos de siempre (cerrar, cancelar, eliminar): ningun paso ni boton nuevo

    def test_one_message_is_said_in_the_singular(self, app, folders):
        a, _ = folders
        seed(a, "uno", 1, "Uno")
        svc.assign_project_path("uno", str(a), "Uno")
        show_session_in_list(app, "uno", "Uno")
        text, _ = click_delete_and_read_modal(app, "Uno")
        assert "1 mensaje guardado en" in text and "1 mensajes" not in text

    def test_a_session_without_messages_or_folder_adds_no_sentence(self, app):
        show_session_in_list(app, "sin-nada", "Sin nada")
        text, _ = click_delete_and_read_modal(app, "Sin nada")
        assert text == '¿Eliminar la sesión "Sin nada"? Esta acción no se puede deshacer.'

    def test_it_warns_when_the_same_id_lives_in_another_folder(self, app, folders):
        a, b = folders
        seed(a, "doble", 5, "Doble")
        seed(b, "doble", 194, "Doble")
        svc.assign_project_path("doble", str(a), "Doble")
        master_index.set_project_path("otra", str(b), windows_user_created="tester")  # b es una carpeta conocida
        show_session_in_list(app, "doble", "Doble")
        text, _ = click_delete_and_read_modal(app, "Doble")
        assert "5 mensajes guardados" in text
        assert f"otra copia de esta conversación con el mismo identificador en {b}" in text
        assert "194 mensajes" in text and "no la elimina" in text

    def test_cancelling_deletes_nothing(self, app, folders):
        a, _ = folders
        seed(a, "sigue", 4, "Sigue")
        svc.assign_project_path("sigue", str(a), "Sigue")
        show_session_in_list(app, "sigue", "Sigue")
        click_delete_and_read_modal(app, "Sigue")
        cancel_modal(app)
        assert db.session_exists(db.db_path_for(str(a)), "sigue") is True
        assert master_index.get_project_path("sigue") == str(a)


# ---- REQ-012: el chip de carpeta dice la verdad -----------------------------------------------------------------------
STATUS_URL = "**/api/session/*/status"
VISION_HOME = "C:/Users/x/AppData/Local/Vision"  # lo que el backend responde como «referencia visual» sin carpeta registrada
MY_FOLDER = "C:/Proyectos/Mi carpeta"
CHIP_TEXT = "() => document.getElementById('meta-session-path').textContent"


def route_status(app, **fields):
    """Responde `/status` con el cuerpo real mas los campos pedidos (el resto del cuerpo es el del servidor real)."""

    def handler(route):
        real = route.fetch()
        body = real.json()
        body.update(fields)
        route.fulfill(response=real, body=json.dumps(body))

    app.page.route(STATUS_URL, handler)


def reload_and_wait_for_chip(app, expected):
    app.page.reload(wait_until="domcontentloaded")
    app.wait_ready()
    app.page.wait_for_function(f"() => ({CHIP_TEXT[6:]}) === {json.dumps(expected)}")


class TestTheFolderChipTellsTheTruth:
    def test_a_session_without_registered_folder_says_so(self, app):
        route_status(app, persistence_available=False, project_path=VISION_HOME)
        reload_and_wait_for_chip(app, "Sin carpeta registrada")
        assert app.page.inner_text("#meta-session-path") == "Sin carpeta registrada"

    def test_it_never_shows_the_vision_install_folder_as_the_project(self, app):
        route_status(app, persistence_available=False, project_path=VISION_HOME)
        reload_and_wait_for_chip(app, "Sin carpeta registrada")
        assert "AppData" not in app.page.inner_text("#meta-session-path")

    def test_a_session_with_a_folder_shows_its_path_as_always(self, app):
        route_status(app, persistence_available=True, project_path=MY_FOLDER)
        reload_and_wait_for_chip(app, MY_FOLDER)

    def test_the_edit_modal_is_not_filled_with_the_notice(self, app):
        route_status(app, persistence_available=False, project_path=VISION_HOME)
        reload_and_wait_for_chip(app, "Sin carpeta registrada")
        app.page.click("#meta-session")  # el camino real: tocar el chip de la sesion
        app.page.wait_for_selector("#session-edit-overlay:not(.hidden)")
        assert app.page.input_value("#session-edit-path") == ""

    def test_the_edit_modal_of_a_session_with_folder_is_filled_with_its_path(self, app):
        route_status(app, persistence_available=True, project_path=MY_FOLDER)
        reload_and_wait_for_chip(app, MY_FOLDER)
        app.page.click("#meta-session")
        app.page.wait_for_selector("#session-edit-overlay:not(.hidden)")
        assert app.page.input_value("#session-edit-path") == MY_FOLDER
