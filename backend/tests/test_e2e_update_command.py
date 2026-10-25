"""E2E del comando `/update` en un Chromium REAL (spec comando-update-sesiones-publicas, REQ-008/010/011).

Pedido del usuario (2026-10-24): «/update solo traiga el contexto de lo nuevo que viene desde otro usuario, con el CSID de cada uno etiquetado». Backend REAL y
bases SQLite reales en `tmp_path`; la otra persona se simula escribiendo su conversacion publica en la base de la carpeta. Solo se reemplaza el MODELO del resumen
(`compaction._summarize`) y el Explorador de Windows (`/api/fs/native-picker`), porque un test no puede llamar al proveedor de IA ni manejar un dialogo del sistema.
LIMITE: se prueba el comportamiento y el texto, no que las frases le resulten claras al usuario."""

from __future__ import annotations

import json
import sqlite3

import pytest

from app.persistence import db, master_index
from app.persistence import service as svc
from app.session import compaction
from e2e_support import launch_chromium, open_app, serve_backend

pytestmark = pytest.mark.e2e

NOW = 1_800_000_000.0
ME = "tester"
PUB = "publica-de-marta"
OTHER_PUB = "publica-de-luis"


@pytest.fixture(scope="module")
def base_url():
    with serve_backend(None) as url:
        yield url


@pytest.fixture(scope="module")
def browser(tmp_path_factory, base_url):
    with launch_chromium(tmp_path_factory.mktemp("updatecmd"), base_url) as b:
        yield b


@pytest.fixture
def app(browser, base_url):
    with open_app(browser, base_url) as a:
        a.update_posts = []
        a.page.on(
            "request",
            lambda r: a.update_posts.append(r.url) if r.method == "POST" and "/api/commands/update/" in r.url else None,
        )
        a.open()
        yield a


@pytest.fixture(autouse=True)
def tester_is_the_current_user(monkeypatch):
    monkeypatch.setenv("USERNAME", ME)
    monkeypatch.setattr(svc, "current_windows_user", lambda: ME)


@pytest.fixture
def summaries(monkeypatch):
    seen: list[str] = []

    async def fake(transcript: str, model: str) -> str:
        seen.append(transcript)
        return f"RESUMEN-FALSO({len(seen)})"

    monkeypatch.setattr(compaction, "_summarize", fake)
    return seen


@pytest.fixture
def folder(tmp_path):
    f = tmp_path / "Equipo"
    f.mkdir()
    return f


def publish(folder, sid=PUB, who="marta", title="Plan de stock", texts=("hola", "dale")):
    """Una conversacion publica de OTRA persona: sin dueño, con `published_by`."""
    path = db.db_path_for(str(folder))
    db.ensure_session(path, sid, NOW)
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "UPDATE session SET windows_user_created = '', is_public = 1, published_by = ?, title = ?, title_manual = 1, updated_at = ? WHERE id = ?",
            (who, title, NOW + len(texts), sid),
        )
        conn.commit()
    finally:
        conn.close()
    for n, text in enumerate(texts):
        mid = f"{sid}-m{n:03d}"
        db.insert_message(path, mid, sid, "user" if n % 2 == 0 else "assistant", NOW + n)
        db.insert_part(path, f"{mid}-p", mid, sid, "text", {"text": text}, NOW + n)
    return path


def say_more(path, sid, text, n):
    mid = f"{sid}-m{n:03d}"
    db.insert_message(path, mid, sid, "user", NOW + n)
    db.insert_part(path, f"{mid}-p", mid, sid, "text", {"text": text}, NOW + n)


def session_id(app) -> str:
    app.page.wait_for_function("() => document.getElementById('meta-session-id').textContent.length > 8")
    return app.page.evaluate("() => document.getElementById('meta-session-id').textContent")


def attach_folder(app, folder) -> str:
    """Le asigna la carpeta a la sesion actual por el endpoint (como lo hace el modal) y la deja en el indice maestro."""
    sid = session_id(app)
    resp = app.page.evaluate(
        """async ([sid, path]) => {
            const r = await fetch(`/api/sessions/${sid}/project-path`, {method: 'POST', headers: {'Content-Type': 'application/json'},
              body: JSON.stringify({path, title: 'Mi sesion'})});
            return r.status;
        }""",
        [sid, str(folder)],
    )
    assert resp == 200
    app.page.evaluate("() => { window.__t = 1; }")
    return sid


def command_names(app) -> list[str]:
    app.page.fill("#chat-input", "/")
    app.page.wait_for_selector("#slash-menu:not(.hidden)")
    names = app.page.locator("#slash-menu .slash-name").all_inner_texts()
    app.page.fill("#chat-input", "")
    return names


def run_update(app) -> None:
    app.page.fill("#chat-input", "/update")
    app.page.wait_for_selector("#slash-menu:not(.hidden)")
    app.page.locator("#slash-menu .slash-item", has_text="/update").first.click()
    app.page.wait_for_selector("#command-modal-body .acc-action-btn:not(.secondary)")
    app.page.click("#command-modal-body .acc-action-btn:not(.secondary)")


def notes(app) -> list[str]:
    return app.page.locator("#messages .system-note").all_inner_texts()


class TestTheMenu:
    def test_it_is_not_offered_without_a_folder(self, app):
        assert not any("/update" in n for n in command_names(app))

    def test_it_is_not_offered_when_the_folder_has_no_public_sessions(self, app, folder):
        attach_folder(app, folder)
        assert not any("/update" in n for n in command_names(app))

    def test_it_is_offered_when_the_folder_has_a_public_session_of_someone_else(self, app, folder):
        publish(folder)
        attach_folder(app, folder)
        assert any("/update" in n for n in command_names(app))

    def test_it_is_not_offered_for_a_private_session_of_someone_else(self, app, folder):
        path = publish(folder)
        conn = sqlite3.connect(path)
        conn.execute(
            "UPDATE session SET is_public = 0, windows_user_created = 'marta_20260101000000', published_by = '' WHERE id = ?",
            (PUB,),
        )
        conn.commit()
        conn.close()
        attach_folder(app, folder)
        assert not any("/update" in n for n in command_names(app))


class TestRunningIt:
    def test_it_asks_for_confirmation_and_cancelling_does_nothing(self, app, folder, summaries):
        publish(folder)
        attach_folder(app, folder)
        app.page.fill("#chat-input", "/update")
        app.page.wait_for_selector("#slash-menu:not(.hidden)")
        app.page.locator("#slash-menu .slash-item", has_text="/update").first.click()
        app.page.wait_for_selector("#command-modal-body .confirm-message")
        assert "OneDrive" in app.page.inner_text("#command-modal-body .confirm-message")
        app.page.click("#command-modal-body .acc-action-btn.secondary")
        app.page.wait_for_timeout(500)
        assert app.update_posts == [] and summaries == []

    def test_it_summarizes_the_news_and_shows_the_note_with_the_csid(self, app, folder, summaries):
        publish(folder, who="marta", title="Plan de stock")
        attach_folder(app, folder)
        run_update(app)
        app.page.wait_for_function(
            "() => [...document.querySelectorAll('#messages .system-note')].some(e => e.textContent.includes('Actualicé'))"
        )
        assert len(summaries) == 1 and "hola" in summaries[0] and "dale" in summaries[0]
        app.page.wait_for_function("() => document.getElementById('messages').textContent.includes('CSID marta')")
        text = app.page.inner_text("#messages")
        assert "Plan de stock" in text and "RESUMEN-FALSO(1)" in text

    def test_the_summary_message_names_who_it_came_from(self, app, folder, summaries):
        publish(folder, who="marta")
        publish(folder, sid=OTHER_PUB, who="luis", title="Otra", texts=("uno", "dos", "tres"))
        attach_folder(app, folder)
        run_update(app)
        app.page.wait_for_function(
            "() => [...document.querySelectorAll('#messages .system-note')].some(e => e.textContent.includes('Actualicé'))"
        )
        final = [n for n in notes(app) if "Actualicé" in n][0]
        assert "marta" in final and "luis" in final and "2 conversaciones" in final

    def test_a_second_update_without_news_says_so_and_calls_no_model(self, app, folder, summaries):
        publish(folder)
        attach_folder(app, folder)
        run_update(app)
        app.page.wait_for_function(
            "() => [...document.querySelectorAll('#messages .system-note')].some(e => e.textContent.includes('Actualicé'))"
        )
        run_update(app)
        app.page.wait_for_function(
            "() => [...document.querySelectorAll('#messages .system-note')].some(e => e.textContent.includes('No hay novedades'))"
        )
        assert len(summaries) == 1

    def test_the_second_update_brings_only_what_is_new(self, app, folder, summaries):
        path = publish(folder, texts=("vieja pregunta",))
        attach_folder(app, folder)
        run_update(app)
        app.page.wait_for_function(
            "() => [...document.querySelectorAll('#messages .system-note')].some(e => e.textContent.includes('Actualicé'))"
        )
        say_more(path, PUB, "pregunta nueva", 50)
        run_update(app)
        app.page.wait_for_function("() => document.getElementById('messages').textContent.includes('RESUMEN-FALSO(2)')")
        assert "pregunta nueva" in summaries[1] and "vieja pregunta" not in summaries[1]

    def test_the_files_the_other_person_created_are_listed_with_their_csid(self, app, folder, summaries):
        path = publish(folder, who="marta")
        (folder / "exports").mkdir()
        (folder / "exports" / "reporte.xlsx").write_text("x")
        db.insert_part(
            path,
            f"{PUB}-m000-w",
            f"{PUB}-m000",
            PUB,
            "tool",
            {
                "call_id": "c1",
                "name": "write",
                "arguments": json.dumps({"path": str(folder / "exports" / "reporte.xlsx"), "content": "SECRETO"}),
                "result": "ok",
            },
            NOW,
        )
        attach_folder(app, folder)
        run_update(app)
        app.page.wait_for_function(
            "() => document.getElementById('messages').textContent.includes('Archivos nuevos de CSID marta')"
        )
        text = app.page.inner_text("#messages")
        assert "exports/reporte.xlsx" in text and "creó" in text and "SECRETO" not in text

    def test_bash_is_only_counted_and_its_text_never_appears(self, app, folder, summaries):
        path = publish(folder, who="marta")
        db.insert_part(
            path,
            f"{PUB}-m000-b",
            f"{PUB}-m000",
            PUB,
            "tool",
            {
                "call_id": "c1",
                "name": "bash",
                "arguments": json.dumps({"command": "curl -u marta:CLAVE-SECRETA https://x"}),
                "result": "ok",
            },
            NOW,
        )
        attach_folder(app, folder)
        run_update(app)
        app.page.wait_for_function(
            "() => document.getElementById('messages').textContent.includes('comando de consola')"
        )
        text = app.page.inner_text("#messages")
        assert "ejecutó 1 comando de consola; no sé qué archivos crearon" in text and "CLAVE-SECRETA" not in text

    def test_a_model_failure_is_said_with_a_sentence_and_the_chat_keeps_working(self, app, folder, monkeypatch):
        import openai

        async def boom(transcript: str, model: str) -> str:
            exc = openai.APIError("spend limit $3 per 600 seconds", None, body=None)  # type: ignore[arg-type]
            exc.status_code = 429  # type: ignore[attr-defined]
            raise exc

        monkeypatch.setattr(compaction, "_summarize", boom)
        publish(folder, who="marta", title="Plan de stock")
        attach_folder(app, folder)
        run_update(app)
        app.page.wait_for_function(
            "() => [...document.querySelectorAll('#messages .system-note')].some(e => e.textContent.includes('No pude resumir'))"
        )
        text = " ".join(notes(app))
        assert "tope de consumo" in text and "CSID marta" in text
        for jargon in ("$3", "600 seconds", "openai", "APIError", "Traceback"):
            assert jargon not in text
        assert app.page.is_enabled("#chat-input")

    def test_the_marks_are_stored_so_a_reload_does_not_resend(self, app, folder, summaries):
        publish(folder)
        sid = attach_folder(app, folder)
        run_update(app)
        app.page.wait_for_function(
            "() => [...document.querySelectorAll('#messages .system-note')].some(e => e.textContent.includes('Actualicé'))"
        )
        conn = sqlite3.connect(db.db_path_for(str(folder)))
        try:
            assert conn.execute("SELECT COUNT(*) FROM public_follow WHERE session_id = ?", (sid,)).fetchone()[0] == 1
        finally:
            conn.close()
        assert master_index.get_project_path(sid) == str(folder)


class TestEdgeCases:
    def hold_update(self, app):
        """Retiene la respuesta de `POST /api/commands/update/...` para controlar CUANDO llega; devuelve la lista donde queda la ruta."""
        held: list = []

        def handler(route):
            if route.request.method == "POST":
                held.append(route)
            else:
                route.continue_()

        app.page.route("**/api/commands/update/**", handler)
        return held

    def test_it_shows_a_searching_note_while_it_waits(self, app, folder, summaries):
        publish(folder)
        attach_folder(app, folder)
        held = self.hold_update(app)
        run_update(app)
        app.page.wait_for_function("() => [...document.querySelectorAll('#messages .system-note')].some(e => e.textContent.includes('Buscando novedades'))")
        assert held, "el pedido no salio"
        held[0].continue_()
        app.page.wait_for_function("() => [...document.querySelectorAll('#messages .system-note')].some(e => e.textContent.includes('Actualicé'))")

    def test_a_server_error_is_said_with_a_sentence_and_no_summary_is_shown(self, app, folder, summaries):
        publish(folder)
        attach_folder(app, folder)
        app.page.route(
            "**/api/commands/update/**",
            lambda r: r.fulfill(status=500, content_type="application/json", body=json.dumps({"detail": "No pude traer las novedades de las conversaciones públicas. Probá de nuevo en un momento."})),
        )
        run_update(app)
        app.page.wait_for_function("() => [...document.querySelectorAll('#messages .system-note')].some(e => e.textContent.includes('No pude traer las novedades.'))")
        text = " ".join(notes(app))
        assert "Probá de nuevo" in text and "Actualicé" not in text and "Listo" not in text
        assert app.page.is_enabled("#chat-input")

    def test_a_network_failure_is_said_with_a_friendly_sentence(self, app, folder, summaries):
        publish(folder)
        attach_folder(app, folder)
        app.page.route("**/api/commands/update/**", lambda r: r.abort())
        run_update(app)
        app.page.wait_for_function("() => [...document.querySelectorAll('#messages .system-note')].some(e => e.textContent.includes('No pude traer las novedades.'))")
        text = " ".join(notes(app))
        assert "TypeError" not in text and "Failed to fetch" not in text

    def test_the_answer_without_a_message_still_closes_with_a_sentence(self, app, folder, summaries):
        publish(folder)
        attach_folder(app, folder)
        app.page.route("**/api/commands/update/**", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps({"updated": 0})))
        run_update(app)
        app.page.wait_for_function("() => [...document.querySelectorAll('#messages .system-note')].some(e => e.textContent === 'Listo.')")

    def test_an_answer_with_no_updates_does_not_reload_the_chat(self, app, folder, summaries):
        """`updated = 0` no repinta la sesion: lo que la persona tenia en pantalla (aunque no se persista) se conserva."""
        publish(folder)
        attach_folder(app, folder)
        app.page.evaluate("() => { const d = document.createElement('div'); d.id = 'marca-de-prueba'; d.className = 'msg'; document.getElementById('messages').appendChild(d); }")
        app.page.route(
            "**/api/commands/update/**",
            lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps({"updated": 0, "message": "No hay novedades en las conversaciones públicas de esta carpeta."})),
        )
        run_update(app)
        app.page.wait_for_function("() => [...document.querySelectorAll('#messages .system-note')].some(e => e.textContent.includes('No hay novedades'))")
        assert app.page.locator("#marca-de-prueba").count() == 1

    def test_an_answer_with_updates_reloads_the_chat_from_sqlite(self, app, folder, summaries):
        publish(folder)
        attach_folder(app, folder)
        app.page.evaluate("() => { const d = document.createElement('div'); d.id = 'marca-de-prueba'; d.className = 'msg'; document.getElementById('messages').appendChild(d); }")
        run_update(app)
        app.page.wait_for_function("() => document.getElementById('messages').textContent.includes('CSID marta')")
        assert app.page.locator("#marca-de-prueba").count() == 0, "con novedades el chat se repinta desde SQLite"

    def test_switching_session_while_it_runs_does_not_paint_the_old_answer_on_the_new_one(self, app, folder, summaries):
        publish(folder)
        attach_folder(app, folder)
        held = self.hold_update(app)
        run_update(app)
        app.page.wait_for_function("() => [...document.querySelectorAll('#messages .system-note')].some(e => e.textContent.includes('Buscando novedades'))")
        assert held
        old = session_id(app)
        app.page.evaluate("document.getElementById('acc-session').open = true")
        app.page.click("#session-new-btn")
        app.page.wait_for_function("(o) => document.getElementById('meta-session-id').textContent !== o", arg=old)
        held[0].fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"updated": 1, "message": "Actualicé 1 conversación pública (2 mensajes nuevos) de: marta."}),
        )
        app.page.wait_for_timeout(1200)
        text = app.page.inner_text("#messages")
        assert "Actualicé" not in text and "Buscando novedades" not in text

    def test_switching_session_while_it_fails_does_not_paint_the_error_on_the_new_one(self, app, folder, summaries):
        publish(folder)
        attach_folder(app, folder)
        held = self.hold_update(app)
        run_update(app)
        app.page.wait_for_function("() => [...document.querySelectorAll('#messages .system-note')].some(e => e.textContent.includes('Buscando novedades'))")
        assert held
        old = session_id(app)
        app.page.evaluate("document.getElementById('acc-session').open = true")
        app.page.click("#session-new-btn")
        app.page.wait_for_function("(o) => document.getElementById('meta-session-id').textContent !== o", arg=old)
        held[0].fulfill(status=500, content_type="application/json", body=json.dumps({"detail": "Falla de prueba"}))
        app.page.wait_for_timeout(1200)
        assert "No pude traer las novedades" not in app.page.inner_text("#messages")
