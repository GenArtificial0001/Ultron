"""E2E del aviso «esta carpeta ya tiene conversaciones anteriores» en un Chromium REAL (spec importar-historial-de-carpeta, REQ-010..012).

Caso real del usuario (2026-10-23 09:30): sesion NUEVA «Auto Vision» + la carpeta `Presentacion Vision`, que ya tenia la sesion vieja «Presentacion» (124 mensajes).
Nada le aviso. Pedido: «es importantisimo que la importe y que avise que hay historial anterior en esa carpeta y si quiere importarlo o no».

Backend REAL y bases SQLite reales en `tmp_path`. Solo se simula, con `page.route`, la respuesta del Explorador de Windows (`/api/fs/native-picker`), un dialogo del
sistema que un test no puede manejar. LIMITE: se prueba el texto y el comportamiento, no que las frases le resulten claras al usuario."""

from __future__ import annotations

import json
import sqlite3

import pytest

from app.persistence import db, master_index
from app.persistence import service as svc
from e2e_support import launch_chromium, open_app, serve_backend

pytestmark = pytest.mark.e2e

NOW = 1_800_000_000.0
USER_TAG = "tester_20260101000000"
OLD = "sesion-vieja-presentacion"
CHIP_IS = "(p) => document.getElementById('meta-session-path').textContent === p"


@pytest.fixture(scope="module")
def base_url():
    with serve_backend(None) as url:
        yield url


@pytest.fixture(scope="module")
def browser(tmp_path_factory, base_url):
    with launch_chromium(tmp_path_factory.mktemp("impfolder"), base_url) as b:
        yield b


@pytest.fixture
def app(browser, base_url):
    with open_app(browser, base_url) as a:
        a.saves = []  # cada guardado de carpeta que llega al servidor: para afirmar que NO hubo ninguno
        a.page.on("request", lambda r: a.saves.append(r.post_data) if r.method == "POST" and "/project-path" in r.url else None)
        a.open()
        yield a


@pytest.fixture(autouse=True)
def tester_is_the_current_user(monkeypatch):
    monkeypatch.setenv("USERNAME", "tester")
    monkeypatch.setattr(svc, "current_windows_user", lambda: "tester")


@pytest.fixture
def folder(tmp_path):
    f = tmp_path / "Presentacion Vision"
    f.mkdir()
    return f


def seed(folder, sid, messages, title="Presentacion", user_tag=USER_TAG):
    path = db.db_path_for(str(folder))
    db.ensure_session(path, sid, NOW)
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "UPDATE session SET windows_user_created = ?, title = ?, title_manual = 1, updated_at = ? WHERE id = ?",
            (user_tag, title, NOW + messages, sid),
        )
        conn.commit()
    finally:
        conn.close()
    for n in range(messages):
        db.insert_message(path, f"{sid}-m{n}", sid, "user" if n % 2 == 0 else "assistant", NOW + n)
        db.insert_part(path, f"{sid}-p{n}", f"{sid}-m{n}", sid, "text", {"text": f"mensaje {n} de {title}"}, NOW + n)
    return path


def count(folder, sid) -> int:
    path = db.db_path_for(str(folder))
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT COUNT(*) FROM message WHERE session_id = ?", (sid,)).fetchone()[0]
    finally:
        conn.close()


def current_session_id(app) -> str:
    app.page.wait_for_function("() => document.getElementById('meta-session-id').textContent.length > 8")
    return app.page.evaluate("() => document.getElementById('meta-session-id').textContent")


def choose_folder(app, folder) -> None:
    """Abre el modal de edicion, elige la carpeta con «Examinar» (simulando SOLO el Explorador de Windows) y toca «Guardar»."""
    path = str(folder)
    app.page.route(
        "**/api/fs/native-picker*",
        lambda route: route.fulfill(status=200, content_type="application/json", body=json.dumps({"path": path})),
    )
    app.page.click("#meta-session")
    app.page.wait_for_selector("#session-edit-overlay:not(.hidden)")
    app.page.click("#session-edit-browse")
    app.page.wait_for_function("(p) => document.getElementById('session-edit-path').value === p", arg=path)
    app.page.click("#session-edit-save")


def notice_visible(app) -> bool:
    return app.page.locator("#folder-history-overlay:not(.hidden)").count() == 1


def wait_notice(app) -> None:
    app.page.wait_for_selector("#folder-history-overlay:not(.hidden)")


def nothing_was_saved_after_dismissing(app) -> None:
    app.page.wait_for_selector("#folder-history-overlay.hidden", state="attached")
    app.page.wait_for_function("() => document.getElementById('session-edit-status').textContent === ''")
    app.page.wait_for_timeout(1500)
    assert app.saves == [], f"se guardo la carpeta aunque se cerro el aviso: {app.saves}"


class TestTheNotice:
    def test_it_warns_and_lists_the_previous_conversation_with_title_and_count(self, app, folder):
        seed(folder, OLD, 124, title="Presentacion")
        choose_folder(app, folder)
        wait_notice(app)
        assert "ya tiene 1 conversación anterior tuya" in app.page.inner_text("#folder-history-intro")
        item = app.page.locator("#folder-history-list li").first
        assert "Presentacion" in item.inner_text() and "124 mensajes" in item.inner_text()

    def test_it_offers_the_three_ways_out(self, app, folder):
        seed(folder, OLD, 5)
        choose_folder(app, folder)
        wait_notice(app)
        texts = app.page.locator("#folder-history-overlay button:visible").all_inner_texts()
        assert "Importar a esta sesión" in texts and "Abrir esa sesión" in texts and "Seguir con esta sesión vacía" in texts

    def test_it_says_import_copies_and_leaves_the_old_one_intact(self, app, folder):
        seed(folder, OLD, 5)
        choose_folder(app, folder)
        wait_notice(app)
        assert "COPIA" in app.page.inner_text("#folder-history-note")

    def test_one_message_is_in_the_singular(self, app, folder):
        seed(folder, OLD, 1)
        choose_folder(app, folder)
        wait_notice(app)
        assert "1 mensaje ·" in app.page.locator("#folder-history-list li").first.inner_text()

    def test_several_conversations_are_all_listed_most_recent_first(self, app, folder):
        seed(folder, "vieja-a", 3, title="Primera")
        seed(folder, "vieja-b", 9, title="Segunda")
        choose_folder(app, folder)
        wait_notice(app)
        titles = app.page.locator("#folder-history-list .folder-history-title").all_inner_texts()
        assert titles == ["Segunda", "Primera"]  # la de mas mensajes tiene la actividad mas reciente


class TestTheAnswers:
    def test_import_copies_the_history_into_this_session_and_shows_it(self, app, folder):
        seed(folder, OLD, 6, title="Presentacion")
        sid = current_session_id(app)
        choose_folder(app, folder)
        wait_notice(app)
        app.page.locator("#folder-history-list .folder-history-import").first.click()
        app.page.wait_for_function(CHIP_IS, arg=str(folder))
        assert count(folder, sid) == 6 and count(folder, OLD) == 6  # copiado y la anterior intacta
        assert master_index.get_project_path(sid) == str(folder)
        app.page.wait_for_function("() => document.querySelectorAll('#messages .msg').length >= 6")
        assert '"import_from_session_id"' in app.saves[0]

    def test_open_goes_to_the_previous_session_and_leaves_this_one_alone(self, app, folder):
        seed(folder, OLD, 6)
        master_index.set_project_path(OLD, str(folder), windows_user_created="tester")
        sid = current_session_id(app)
        choose_folder(app, folder)
        wait_notice(app)
        app.page.locator("#folder-history-list .folder-history-open").first.click()
        app.page.wait_for_function(f"() => document.getElementById('meta-session-id').textContent.startsWith('{OLD[:8]}')")
        assert master_index.get_project_path(sid) is None  # la sesion vacia no se toco
        assert count(folder, OLD) == 6

    def test_open_registers_a_previous_session_the_index_did_not_know(self, app, folder):
        seed(folder, OLD, 4)  # en la base, pero SIN fila en el indice
        choose_folder(app, folder)
        wait_notice(app)
        app.page.locator("#folder-history-list .folder-history-open").first.click()
        app.page.wait_for_function(f"() => document.getElementById('meta-session-id').textContent.startsWith('{OLD[:8]}')")
        assert master_index.get_project_path(OLD) == str(folder)
        assert count(folder, OLD) == 4

    def test_keep_assigns_the_folder_without_importing(self, app, folder):
        seed(folder, OLD, 6)
        sid = current_session_id(app)
        choose_folder(app, folder)
        wait_notice(app)
        app.page.click("#folder-history-keep")
        app.page.wait_for_function(CHIP_IS, arg=str(folder))
        assert master_index.get_project_path(sid) == str(folder)
        assert count(folder, sid) == 0 and count(folder, OLD) == 6
        assert "import_from_session_id" not in app.saves[0]

    def test_the_close_button_changes_nothing(self, app, folder):
        seed(folder, OLD, 6)
        sid = current_session_id(app)
        choose_folder(app, folder)
        wait_notice(app)
        app.page.click("#folder-history-close")
        nothing_was_saved_after_dismissing(app)
        assert master_index.get_project_path(sid) is None
        assert count(folder, sid) == 0

    def test_a_click_outside_also_changes_nothing(self, app, folder):
        seed(folder, OLD, 6)
        sid = current_session_id(app)
        choose_folder(app, folder)
        wait_notice(app)
        app.page.evaluate("document.getElementById('folder-history-overlay').click()")
        nothing_was_saved_after_dismissing(app)
        assert master_index.get_project_path(sid) is None


class TestItAsksOnlyWhenItApplies:
    def test_a_folder_without_previous_conversations_does_not_ask(self, app, folder):
        sid = current_session_id(app)
        choose_folder(app, folder)
        app.page.wait_for_function(CHIP_IS, arg=str(folder))
        assert not notice_visible(app)
        assert master_index.get_project_path(sid) == str(folder)

    def test_conversations_of_another_user_are_never_offered(self, app, folder):
        seed(folder, "de-otra-persona", 8, user_tag="marta_20260101000000")
        choose_folder(app, folder)
        app.page.wait_for_function(CHIP_IS, arg=str(folder))
        assert not notice_visible(app)

    def test_empty_previous_sessions_are_not_offered(self, app, folder):
        seed(folder, "vacia", 0)
        choose_folder(app, folder)
        app.page.wait_for_function(CHIP_IS, arg=str(folder))
        assert not notice_visible(app)

    def test_if_the_query_fails_it_saves_as_always(self, app, folder):
        seed(folder, OLD, 6)
        sid = current_session_id(app)
        app.page.route("**/folder-sessions*", lambda route: route.abort())
        choose_folder(app, folder)
        app.page.wait_for_function(CHIP_IS, arg=str(folder))
        assert not notice_visible(app)
        assert master_index.get_project_path(sid) == str(folder) and count(folder, sid) == 0

    def test_if_the_query_answers_with_an_http_error_it_saves_as_always(self, app, folder):
        seed(folder, OLD, 6)
        sid = current_session_id(app)
        app.page.route(
            "**/folder-sessions*",
            lambda route: route.fulfill(status=500, content_type="application/json", body='{"applicable": true, "sessions": [{"id": "x"}]}'),
        )
        choose_folder(app, folder)
        app.page.wait_for_function(CHIP_IS, arg=str(folder))
        assert not notice_visible(app)
        assert master_index.get_project_path(sid) == str(folder)

    def test_a_session_that_changes_folder_gets_the_older_question_not_this_one(self, app, tmp_path):
        """Si la sesion YA tenia otra carpeta con historial, manda la pregunta de 0.35.9 y NO aparece esta (nunca dos avisos seguidos)."""
        a, b = tmp_path / "a", tmp_path / "b"
        a.mkdir()
        b.mkdir()
        sid = current_session_id(app)
        seed(a, sid, 4)
        svc.assign_project_path(sid, str(a), "")
        seed(b, "otra-vieja", 7, title="Otra")  # la carpeta nueva TAMBIEN tiene una conversacion anterior
        app.page.reload(wait_until="domcontentloaded")
        app.wait_ready()
        app.page.wait_for_function(CHIP_IS, arg=str(a))
        choose_folder(app, b)
        app.page.wait_for_selector("#confirm-overlay:not(.hidden)")  # la pregunta de siempre de 0.35.9
        assert not notice_visible(app)
        app.page.click("#confirm-cancel-btn")  # «Dejarlo donde esta»
        app.page.wait_for_function(CHIP_IS, arg=str(b))
        assert not notice_visible(app)  # y tampoco aparece despues


class TestAnImportFailureIsNeverHidden:
    def test_a_failed_import_says_so_instead_of_saved(self, app, folder):
        seed(folder, OLD, 6)
        choose_folder(app, folder)
        wait_notice(app)

        def fail(route):
            real = route.fetch()
            body = real.json()
            body["import_failed"] = True
            route.fulfill(response=real, body=json.dumps(body))

        app.page.route("**/project-path", fail)
        app.page.locator("#folder-history-list .folder-history-import").first.click()
        app.page.wait_for_function(
            "() => document.getElementById('session-edit-status').textContent.includes('no pude importar')"
        )
        assert "Guardado" not in app.page.inner_text("#session-edit-status")
