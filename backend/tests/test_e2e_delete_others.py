"""E2E del aviso de «Eliminar sesion» con quien mas usa la carpeta, en un Chromium REAL (spec aviso-eliminar-sesion-usuarios, REQ-006/007).

Pedido del usuario (2026-10-25): «cuando voy a eliminar una sesion que me muestre que usuarios y como se llama el nombre de sesion de cada usuario, solo como para saber».
Decision de privacidad: mis sesiones con nombre, las PUBLICAS con titulo y autor, las PRIVADAS de otras personas solo el usuario y la cantidad (nunca titulos).
Backend REAL y bases SQLite reales en `tmp_path`. LIMITE: se prueba el texto y el comportamiento, no que las frases le resulten claras al usuario."""

from __future__ import annotations

import sqlite3

import pytest

from app.persistence import db
from app.persistence import service as svc
from e2e_support import launch_chromium, open_app, serve_backend

pytestmark = pytest.mark.e2e

NOW = 1_800_000_000.0
ME = "tester"
DEL = "a-eliminar"


@pytest.fixture(scope="module")
def base_url():
    with serve_backend(None) as url:
        yield url


@pytest.fixture(scope="module")
def browser(tmp_path_factory, base_url):
    with launch_chromium(tmp_path_factory.mktemp("delothers"), base_url) as b:
        yield b


@pytest.fixture
def app(browser, base_url):
    with open_app(browser, base_url) as a:
        a.open()
        yield a


@pytest.fixture(autouse=True)
def tester_is_the_current_user(monkeypatch):
    monkeypatch.setenv("USERNAME", ME)
    monkeypatch.setattr(svc, "current_windows_user", lambda: ME)


@pytest.fixture
def folder(tmp_path):
    f = tmp_path / "carpeta-equipo"
    f.mkdir()
    return f


def seed(folder, sid, *, owner=ME, public=False, published_by="", title="Charla", messages=2):
    path = db.db_path_for(str(folder))
    db.ensure_session(path, sid, NOW)
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "UPDATE session SET title = ?, title_manual = 1, windows_user_created = ?, is_public = ?, published_by = ? WHERE id = ?",
            (title, "" if public else f"{owner}_20260101000000", 1 if public else 0, published_by, sid),
        )
        conn.commit()
    finally:
        conn.close()
    for n in range(messages):
        db.insert_message(path, f"{sid}-m{n}", sid, "user", NOW + n)
        db.insert_part(path, f"{sid}-p{n}", f"{sid}-m{n}", sid, "text", {"text": f"hola {n}"}, NOW + n)


def show_in_list(app, sid, name):
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


def delete_modal_text(app, name) -> str:
    row = app.page.locator("#session-list li", has_text=name).first
    row.hover()
    row.locator(".session-delete-btn").click()
    app.page.wait_for_selector("#confirm-overlay:not(.hidden)")
    return app.page.inner_text("#confirm-message")


def open_delete(app, folder, **extra) -> str:
    seed(folder, DEL, title="La que borro", messages=3)
    svc.assign_project_path(DEL, str(folder), "La que borro")
    show_in_list(app, DEL, "La que borro")
    return delete_modal_text(app, "La que borro")


class TestWhenThereIsNothingElse:
    def test_the_modal_is_exactly_as_before(self, app, folder):
        text = open_delete(app, folder)
        assert "Tiene 3 mensajes guardados" in text
        for fragment in ("también tenés", "públicas", "privadas", "otras personas"):
            assert fragment not in text


class TestMySessions:
    def test_it_lists_my_other_sessions_with_name_and_count(self, app, folder):
        seed(folder, "mia-1", title="Plan de stock", messages=5)
        seed(folder, "mia-2", title="Presentacion", messages=1)
        text = open_delete(app, folder)
        assert "Plan de stock" in text and "5 mensajes" in text
        assert "Presentacion" in text and "1 mensaje" in text and "1 mensajes" not in text

    def test_it_caps_the_names_at_five_and_says_how_many_more(self, app, folder):
        for n in range(8):
            seed(folder, f"mia-{n}", title=f"Sesion numero {n}", messages=1)
        text = open_delete(app, folder)
        shown = sum(1 for n in range(8) if f"Sesion numero {n}" in text)
        assert shown == 5 and "y 3 más" in text

    def test_it_never_repeats_the_session_being_deleted(self, app, folder):
        seed(folder, "mia-1", title="Otra mia", messages=2)
        text = open_delete(app, folder)
        assert text.count("La que borro") == 1  # solo en la pregunta inicial


class TestPublicSessions:
    def test_a_public_session_shows_title_and_who_published_it(self, app, folder):
        seed(folder, "pub-1", public=True, published_by="marta", title="Del equipo", messages=4)
        text = open_delete(app, folder)
        assert "Del equipo" in text and "marta" in text and "pública" in text.lower() and "4 mensajes" in text

    def test_an_old_public_session_says_unknown_author(self, app, folder):
        seed(folder, "pub-vieja", public=True, published_by="", title="Vieja publica", messages=2)
        assert "desconocido" in open_delete(app, folder)


class TestPrivateSessionsOfOthers:
    def test_only_the_user_and_the_count_are_shown_never_a_title(self, app, folder):
        seed(folder, "p1", owner="marta", title="TITULO PRIVADO UNO", messages=3)
        seed(folder, "p2", owner="marta", title="TITULO PRIVADO DOS", messages=3)
        seed(folder, "p3", owner="luis", title="TITULO PRIVADO TRES", messages=3)
        text = open_delete(app, folder)
        assert "marta" in text and "luis" in text
        assert "TITULO PRIVADO" not in text
        assert "marta (2)" in text and "luis (1)" in text

    def test_it_says_these_conversations_are_private(self, app, folder):
        seed(folder, "p1", owner="marta", title="x", messages=3)
        text = open_delete(app, folder)
        assert "privadas" in text and "otras personas" in text


class TestEverythingTogether:
    def test_the_three_groups_appear_in_one_modal_without_new_buttons(self, app, folder):
        seed(folder, "mia-1", title="Mi otra", messages=2)
        seed(folder, "pub-1", public=True, published_by="marta", title="Publica de Marta", messages=2)
        seed(folder, "p1", owner="luis", title="NO DEBE VERSE", messages=2)
        text = open_delete(app, folder)
        buttons = app.page.locator("#confirm-overlay button:visible").all_inner_texts()
        assert buttons == ["✕", "Cancelar", "Eliminar"]
        assert "Mi otra" in text and "Publica de Marta" in text and "luis (1)" in text and "NO DEBE VERSE" not in text

    def test_the_text_has_no_jargon(self, app, folder):
        seed(folder, "pub-1", public=True, published_by="marta", title="Publica", messages=2)
        seed(folder, "p1", owner="luis", title="x", messages=2)
        text = open_delete(app, folder)
        for jargon in ("CSID", "SQLite", "is_public", "published_by", "HTTP", "JSON", "undefined", "null"):
            assert jargon not in text, jargon


class TestTextsAndEdges:
    def test_the_sentence_for_my_sessions_starts_with_the_agreed_wording(self, app, folder):
        """Cierra el mutante del texto «En esa carpeta también tenés:»."""
        seed(folder, "mia-1", title="Mi otra", messages=2)
        assert "En esa carpeta también tenés: «Mi otra» (2 mensajes)." in open_delete(app, folder)

    def test_the_private_sentence_says_the_names_are_not_shown(self, app, folder):
        """Cierra el mutante del texto «no se ven sus nombres»: la persona tiene que saber POR QUE no ve titulos."""
        seed(folder, "p1", owner="marta", title="x", messages=2)
        assert "(no se ven sus nombres)" in open_delete(app, folder)

    def test_exactly_five_sessions_show_no_and_n_more(self, app, folder):
        """Cierra el mutante del limite: con EXACTAMENTE 5 se listan las 5 y no aparece «y 0 más»."""
        for n in range(5):
            seed(folder, f"mia-{n}", title=f"Sesion numero {n}", messages=1)
        text = open_delete(app, folder)
        assert all(f"Sesion numero {n}" in text for n in range(5))
        assert " más" not in text

    def test_six_sessions_show_five_and_one_more(self, app, folder):
        for n in range(6):
            seed(folder, f"mia-{n}", title=f"Sesion numero {n}", messages=1)
        text = open_delete(app, folder)
        assert sum(1 for n in range(6) if f"Sesion numero {n}" in text) == 5 and "y 1 más" in text


class TestAnOlderOrOddBackend:
    """Con un backend viejo (sin `others_in_folder`) o con campos con otra forma, el aviso NO se rompe ni dice «undefined»: queda como siempre."""

    def impact(self, app, payload):
        def handler(route):
            route.fulfill(status=200, content_type="application/json", body=__import__("json").dumps(payload))

        app.page.route("**/api/session/*/agent-impact", handler)

    def delete_text(self, app, folder):
        seed(folder, DEL, title="La que borro", messages=3)
        svc.assign_project_path(DEL, str(folder), "La que borro")
        show_in_list(app, DEL, "La que borro")
        return delete_modal_text(app, "La que borro")

    BASE = {"message_count": 3, "project_path": "X", "copies_elsewhere": [], "agent_count": 0, "is_fallback_session": False}

    def test_a_missing_field_leaves_the_modal_as_before(self, app, folder):
        """Cierra el mutante «sin guarda de objeto»: el campo falta del todo."""
        self.impact(app, dict(self.BASE))
        text = self.delete_text(app, folder)
        assert "Tiene 3 mensajes" in text and "undefined" not in text and "TypeError" not in text
        assert app.page.locator("#confirm-overlay:not(.hidden)").count() == 1

    def test_a_null_field_leaves_the_modal_as_before(self, app, folder):
        self.impact(app, {**self.BASE, "others_in_folder": None})
        text = self.delete_text(app, folder)
        assert "undefined" not in text and app.page.locator("#confirm-overlay:not(.hidden)").count() == 1

    def test_a_group_that_is_not_a_list_is_ignored_without_breaking_the_others(self, app, folder):
        """Cierra los mutantes de `Array.isArray`: un grupo con forma rara (no lista) se ignora y los demas se siguen mostrando."""
        self.impact(
            app,
            {
                **self.BASE,
                "others_in_folder": {
                    "mine": "no-soy-una-lista",
                    "public": {"no": "soy-lista"},
                    "private_by_user": [{"user": "marta", "count": 2}],
                },
            },
        )
        text = self.delete_text(app, folder)
        assert "marta (2)" in text, "el grupo valido tiene que seguir mostrandose"
        assert "no-soy-una-lista" not in text and "undefined" not in text
        assert app.page.locator("#confirm-overlay:not(.hidden)").count() == 1


class TestEachGroupWithAStringInsteadOfAList:
    """Cierra los mutantes de `Array.isArray` de `public` y `private_by_user`: un TEXTO no vacio tiene `length` pero no `map`, asi que sin la guarda el modal se
    rompe (un objeto no, porque no tiene `length`). Cada grupo se prueba por separado, con OTRO grupo valido que debe seguir mostrandose."""

    VALID = {
        "mine": [{"id": "m", "title": "Mi otra", "message_count": 2}],
        "public": [{"id": "p", "title": "Publica", "published_by": "marta", "message_count": 3}],
        "private_by_user": [{"user": "luis", "count": 1}],
    }
    EXPECT = {"mine": "«Mi otra» (2 mensajes)", "public": "«Publica» de marta", "private_by_user": "luis (1)"}

    @pytest.mark.parametrize("broken", ["mine", "public", "private_by_user"])
    def test_a_string_group_is_ignored_and_the_valid_ones_still_show(self, app, folder, broken):
        others = {**self.VALID, broken: "esto-no-es-una-lista"}
        base = {"message_count": 3, "project_path": "X", "copies_elsewhere": [], "agent_count": 0, "is_fallback_session": False}
        app.page.route(
            "**/api/session/*/agent-impact",
            lambda r: r.fulfill(status=200, content_type="application/json", body=__import__("json").dumps({**base, "others_in_folder": others})),
        )
        seed(folder, DEL, title="La que borro", messages=3)
        svc.assign_project_path(DEL, str(folder), "La que borro")
        show_in_list(app, DEL, "La que borro")
        text = delete_modal_text(app, "La que borro")
        assert "esto-no-es-una-lista" not in text and "undefined" not in text
        for group, fragment in self.EXPECT.items():
            if group != broken:
                assert fragment in text, f"el grupo valido «{group}» dejo de mostrarse cuando «{broken}» llego como texto"
