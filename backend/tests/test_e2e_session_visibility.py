"""E2E de sesion publica o privada en un Chromium REAL (spec sesion-publica-privada, REQ-008/009).

Pedido del usuario (2026-10-24): «por defecto siempre privada, configurable al inicio y modificable; cuando es publica, quien arma una sesion nueva sobre esa
carpeta puede importar todo, pero cada uno escribe en su fork». Backend REAL y bases SQLite reales en `tmp_path`. Solo se simula, con `page.route`, la
respuesta del Explorador de Windows (`/api/fs/native-picker`), un dialogo del sistema que un test no puede manejar.
LIMITE: se prueba el texto y el comportamiento, no que las frases le resulten claras al usuario."""

from __future__ import annotations

import json
import sqlite3

import pytest

from app.persistence import db, master_index
from app.persistence import service as svc
from e2e_support import launch_chromium, open_app, serve_backend

pytestmark = pytest.mark.e2e

NOW = 1_800_000_000.0
ME = "tester"
OTHER_TAG = "marta_20260101000000"
PUB = "sesion-publica-de-marta"
MAIL = "m365_search_messages"
SWITCH = "#session-edit-public"
WARN = "#session-edit-public-warning"


@pytest.fixture(scope="module")
def base_url():
    with serve_backend(None) as url:
        yield url


@pytest.fixture(scope="module")
def browser(tmp_path_factory, base_url):
    with launch_chromium(tmp_path_factory.mktemp("visibility"), base_url) as b:
        yield b


@pytest.fixture
def app(browser, base_url):
    with open_app(browser, base_url) as a:
        a.saves = []  # cada POST a project-path: para ver que viaja `public`
        a.page.on(
            "request",
            lambda r: a.saves.append(r.post_data) if r.method == "POST" and "/project-path" in r.url else None,
        )
        a.visibility_posts = []
        a.page.on(
            "request",
            lambda r: a.visibility_posts.append(r.post_data) if r.method == "POST" and "/visibility" in r.url else None,
        )
        a.open()
        yield a


@pytest.fixture(autouse=True)
def tester_is_the_current_user(monkeypatch):
    monkeypatch.setenv("USERNAME", ME)
    monkeypatch.setattr(svc, "current_windows_user", lambda: ME)


@pytest.fixture
def folder(tmp_path):
    f = tmp_path / "Carpeta Equipo"
    f.mkdir()
    return f


def seed_public(folder, sid=PUB, messages=4, title="Del equipo"):
    """Una conversacion publica de OTRA persona: sin dueño en la base y con un resultado de Outlook adentro."""
    path = db.db_path_for(str(folder))
    db.ensure_session(path, sid, NOW)
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "UPDATE session SET windows_user_created = '', is_public = 1, title = ?, title_manual = 1, updated_at = ? WHERE id = ?",
            (title, NOW + messages, sid),
        )
        conn.commit()
    finally:
        conn.close()
    for n in range(messages):
        db.insert_message(path, f"{sid}-m{n}", sid, "user" if n % 2 == 0 else "assistant", NOW + n)
        db.insert_part(path, f"{sid}-p{n}", f"{sid}-m{n}", sid, "text", {"text": f"mensaje {n} de {title}"}, NOW + n)
    db.insert_part(
        path,
        f"{sid}-t",
        f"{sid}-m0",
        sid,
        "tool",
        {"name": MAIL, "arguments": "{}", "result": "CORREO CONFIDENCIAL"},
        NOW,
    )
    return path


def q(path, sql, *params):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def session_id(app) -> str:
    app.page.wait_for_function("() => document.getElementById('meta-session-id').textContent.length > 8")
    return app.page.evaluate("() => document.getElementById('meta-session-id').textContent")


def pick_folder(app, folder) -> None:
    path = str(folder)
    app.page.route(
        "**/api/fs/native-picker*",
        lambda route: route.fulfill(status=200, content_type="application/json", body=json.dumps({"path": path})),
    )
    app.page.click("#session-edit-browse")
    app.page.wait_for_function("(p) => document.getElementById('session-edit-path').value === p", arg=path)


def open_modal(app, *, settled=True) -> None:
    """Abre el modal de sesion. Con `settled`, espera a que el interruptor termine de cargar el estado real: la app lo pide al abrir y, si el test lo
    toca antes de que llegue la respuesta, esa respuesta lo pisa (carrera del TEST, no de la app: una persona no lo toca en 5 ms)."""
    app.page.click("#meta-session")
    app.page.wait_for_selector("#session-edit-overlay:not(.hidden)")
    if settled:
        app.page.wait_for_function("() => document.getElementById('session-edit-public').dataset.loaded === 'true'")


def assign(app, folder) -> None:
    open_modal(app)
    pick_folder(app, folder)
    app.page.click("#session-edit-save")


# ---------------------------------------------------------------------------------------------------------------------
# El interruptor: privada por defecto, con aviso al activarlo
# ---------------------------------------------------------------------------------------------------------------------
class TestTheSwitch:
    def test_it_exists_and_is_off_by_default(self, app):
        open_modal(app)
        assert app.page.locator(SWITCH).count() == 1
        assert app.page.is_checked(SWITCH) is False

    def test_it_is_labelled_so_a_person_understands_it(self, app):
        open_modal(app)
        label = app.page.inner_text("label[for='session-edit-public']").lower()
        assert "pública" in label or "publica" in label

    def test_the_warning_is_hidden_while_private(self, app):
        open_modal(app)
        assert app.page.locator(f"{WARN}:not(.hidden)").count() == 0

    def test_turning_it_on_shows_the_warning_about_who_can_read_it(self, app):
        open_modal(app)
        app.page.check(SWITCH)
        app.page.wait_for_selector(f"{WARN}:not(.hidden)")
        text = app.page.inner_text(WARN).lower()
        assert "carpeta" in text and "outlook" in text and "teams" in text

    def test_turning_it_off_hides_the_warning_again(self, app):
        open_modal(app)
        app.page.check(SWITCH)
        app.page.uncheck(SWITCH)
        app.page.wait_for_selector(f"{WARN}.hidden", state="attached")

    def test_the_warning_has_no_jargon(self, app):
        open_modal(app)
        app.page.check(SWITCH)
        text = app.page.inner_text(WARN)
        for jargon in ("CSID", "BOLA", "SQLite", "is_public", "HTTP", "JSON", "fork"):
            assert jargon.lower() not in text.lower(), jargon


# ---------------------------------------------------------------------------------------------------------------------
# Crear (primera carpeta) y modificar despues
# ---------------------------------------------------------------------------------------------------------------------
class TestCreateAndChange:
    def test_a_new_session_assigned_without_touching_the_switch_stays_private(self, app, folder):
        sid = session_id(app)
        assign(app, folder)
        app.page.wait_for_function(
            "(p) => document.getElementById('meta-session-path').textContent === p", arg=str(folder)
        )
        assert '"public"' not in app.saves[0] or '"public":false' in app.saves[0].replace(" ", "")
        flags = q(db.db_path_for(str(folder)), "SELECT is_public FROM session WHERE id = ?", sid)
        assert flags in ([], [(0,)])

    def test_a_new_session_can_be_created_public_from_the_start(self, app, folder):
        sid = session_id(app)
        open_modal(app)
        pick_folder(app, folder)
        app.page.check(SWITCH)
        app.page.click("#session-edit-save")
        app.page.wait_for_function(
            "(p) => document.getElementById('meta-session-path').textContent === p", arg=str(folder)
        )
        assert '"public":true' in app.saves[0].replace(" ", "")
        assert q(
            db.db_path_for(str(folder)), "SELECT is_public, windows_user_created FROM session WHERE id = ?", sid
        ) == [(1, "")]

    def test_it_can_be_changed_later_from_the_same_modal_without_touching_the_folder(self, app, folder):
        sid = session_id(app)
        assign(app, folder)
        app.page.wait_for_function(
            "(p) => document.getElementById('meta-session-path').textContent === p", arg=str(folder)
        )
        app.saves.clear()
        app.page.wait_for_selector("#session-edit-overlay.hidden", state="attached")
        open_modal(app)
        app.page.check(SWITCH)
        app.page.click("#session-edit-save")
        app.page.wait_for_function(
            "() => document.getElementById('session-edit-status').textContent.includes('Guardado')"
        )
        assert any('"public":true' in (p or "").replace(" ", "") for p in app.visibility_posts)
        assert app.saves == [], "cambiar solo la visibilidad no debe reasignar la carpeta"
        assert q(db.db_path_for(str(folder)), "SELECT is_public FROM session WHERE id = ?", sid) == [(1,)]

    def test_saving_a_new_folder_with_the_switch_on_does_not_send_the_visibility_a_second_time(self, app, folder):
        """Cierra el mutante «_sessionPublicAtOpen sin actualizar»: el campo `public` ya viajo con la carpeta; reenviarlo es un pedido de mas."""
        session_id(app)
        open_modal(app)
        pick_folder(app, folder)
        app.page.check(SWITCH)
        app.page.click("#session-edit-save")
        app.page.wait_for_function("() => document.getElementById('session-edit-status').textContent.includes('Guardado')")
        assert '"public":true' in app.saves[0].replace(" ", "")
        assert app.visibility_posts == [], app.visibility_posts

    def test_without_a_folder_turning_it_on_asks_for_the_folder_and_saves_nothing(self, app):
        """Cierra el mutante de la guarda «sin carpeta»: no se llama al endpoint (daria 400) ni se dice «Guardado»."""
        open_modal(app)
        app.page.check(SWITCH)
        app.page.click("#session-edit-save")
        app.page.wait_for_function("() => document.getElementById('session-edit-status').textContent.includes('carpeta')")
        status = app.page.inner_text("#session-edit-status")
        assert "Primero elegí la carpeta" in status and "Guardado" not in status
        assert app.visibility_posts == []

    def test_it_can_be_made_private_again(self, app, folder):
        sid = session_id(app)
        open_modal(app)
        pick_folder(app, folder)
        app.page.check(SWITCH)
        app.page.click("#session-edit-save")
        app.page.wait_for_function(
            "(p) => document.getElementById('meta-session-path').textContent === p", arg=str(folder)
        )
        app.page.wait_for_selector("#session-edit-overlay.hidden", state="attached")
        open_modal(app)
        app.page.uncheck(SWITCH)
        app.page.click("#session-edit-save")
        app.page.wait_for_function(
            "() => document.getElementById('session-edit-status').textContent.includes('Guardado')"
        )
        ((flag, owner),) = q(
            db.db_path_for(str(folder)), "SELECT is_public, windows_user_created FROM session WHERE id = ?", sid
        )
        assert flag == 0 and owner.startswith(f"{ME}_")

    def test_reopening_the_modal_shows_the_real_state(self, app, folder):
        sid = session_id(app)
        open_modal(app)
        pick_folder(app, folder)
        app.page.check(SWITCH)
        app.page.click("#session-edit-save")
        app.page.wait_for_function(
            "(p) => document.getElementById('meta-session-path').textContent === p", arg=str(folder)
        )
        app.page.wait_for_selector("#session-edit-overlay.hidden", state="attached")
        open_modal(app)
        app.page.wait_for_function("() => document.getElementById('session-edit-public').checked === true")
        assert q(db.db_path_for(str(folder)), "SELECT is_public FROM session WHERE id = ?", sid) == [(1,)]

    def test_a_session_without_a_folder_shows_the_switch_off_and_saving_only_the_name_does_not_call_visibility(
        self, app
    ):
        open_modal(app)
        app.page.fill("#session-edit-name", "Solo cambio el nombre")
        app.page.click("#session-edit-save")
        app.page.wait_for_function(
            "() => document.getElementById('session-edit-status').textContent.includes('Guardado')"
        )
        assert app.visibility_posts == []


# ---------------------------------------------------------------------------------------------------------------------
# Importar una publica de otra persona
# ---------------------------------------------------------------------------------------------------------------------
class TestImportingAPublicSession:
    def choose(self, app, folder):
        open_modal(app)
        pick_folder(app, folder)
        app.page.click("#session-edit-save")
        app.page.wait_for_selector("#folder-history-overlay:not(.hidden)")

    def test_the_notice_offers_it_and_says_it_is_public_and_from_someone_else(self, app, folder):
        seed_public(folder, title="Del equipo")
        self.choose(app, folder)
        item = app.page.locator("#folder-history-list li").first
        text = item.inner_text()
        assert "Del equipo" in text and "pública" in text.lower()

    def test_the_label_says_it_is_someone_elses_public_conversation(self, app, folder):
        """Cierra el mutante de la etiqueta: una publica AJENA dice «de otra persona»; una propia solo «publica»."""
        seed_public(folder, title="Del equipo")
        self.choose(app, folder)
        badge = app.page.locator("#folder-history-list .folder-history-badge").first.inner_text().lower()
        assert "otra persona" in badge

    def test_the_intro_says_they_are_public_and_from_other_people_not_yours(self, app, folder):
        """Cierra el mutante de `allForeign`: con todas ajenas el texto no dice «tuya»."""
        seed_public(folder, title="Del equipo")
        self.choose(app, folder)
        intro = app.page.inner_text("#folder-history-intro")
        assert "pública de otras personas" in intro and "tuya" not in intro

    def test_with_a_mix_the_intro_mentions_that_some_are_public_from_others(self, app, folder):
        seed_public(folder, sid="ajena-publica", title="Ajena")
        mine = "sesion-mia-privada"
        path = db.db_path_for(str(folder))
        db.ensure_session(path, mine, NOW)
        conn = sqlite3.connect(path)
        conn.execute(
            "UPDATE session SET windows_user_created = ?, title = 'Mia', title_manual = 1, updated_at = ? WHERE id = ?",
            (f"{ME}_20260101000000", NOW + 9, mine),
        )
        conn.commit()
        conn.close()
        db.insert_message(path, f"{mine}-m0", mine, "user", NOW)
        db.insert_part(path, f"{mine}-p0", f"{mine}-m0", mine, "text", {"text": "hola"}, NOW)
        self.choose(app, folder)
        intro = app.page.inner_text("#folder-history-intro")
        assert "algunas públicas de otras personas" in intro
        badges = app.page.locator("#folder-history-list .folder-history-badge").all_inner_texts()
        assert len(badges) == 1, "solo la ajena publica lleva etiqueta; la mia privada no"

    def test_the_notice_tells_that_outlook_and_teams_content_is_not_copied(self, app, folder):
        seed_public(folder)
        self.choose(app, folder)
        note = app.page.inner_text("#folder-history-note").lower()
        assert "outlook" in note and "teams" in note

    def test_importing_it_copies_the_text_hides_the_mail_and_makes_a_private_fork(self, app, folder):
        seed_public(folder, messages=6)
        sid = session_id(app)
        self.choose(app, folder)
        app.page.locator("#folder-history-list .folder-history-import").first.click()
        app.page.wait_for_function(
            "(p) => document.getElementById('meta-session-path').textContent === p", arg=str(folder)
        )
        path = db.db_path_for(str(folder))
        assert q(path, "SELECT COUNT(*) FROM message WHERE session_id = ?", sid) == [(6,)]
        assert q(path, "SELECT COUNT(*) FROM message WHERE session_id = ?", PUB) == [(6,)]  # la original, intacta
        blob = json.dumps(q(path, "SELECT data FROM part WHERE session_id = ?", sid))
        assert "CORREO CONFIDENCIAL" not in blob and "contenido privado omitido" in blob
        ((flag, owner),) = q(path, "SELECT is_public, windows_user_created FROM session WHERE id = ?", sid)
        assert flag == 0 and owner.startswith(f"{ME}_")
        assert q(path, "SELECT is_public, windows_user_created FROM session WHERE id = ?", PUB) == [(1, "")]

    def test_a_private_session_of_someone_else_is_never_offered(self, app, folder):
        path = seed_public(folder)
        conn = sqlite3.connect(path)
        conn.execute("UPDATE session SET is_public = 0, windows_user_created = ? WHERE id = ?", (OTHER_TAG, PUB))
        conn.commit()
        conn.close()
        open_modal(app)
        pick_folder(app, folder)
        app.page.click("#session-edit-save")
        app.page.wait_for_function(
            "(p) => document.getElementById('meta-session-path').textContent === p", arg=str(folder)
        )
        assert app.page.locator("#folder-history-overlay:not(.hidden)").count() == 0
