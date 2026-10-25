"""E2E del cambio de carpeta con la pregunta «¿copiar el historial?» en un Chromium REAL (spec cambio-carpeta-historial, REQ-007..010).

Pedido del usuario (2026-10-23): «hacela» -- al cambiar la carpeta de una sesion, preguntar con dos botones: copiar el historial o dejarlo.
Lo que se mide, con el backend REAL y bases SQLite reales en `tmp_path`: que aparezca la pregunta SOLO cuando corresponde, que sus tres salidas
(copiar / dejarlo / cerrar con la ✕) hagan lo que dicen -- incluido que la ✕ NO cambie la carpeta -- y que `showConfirmModal` de siempre no cambie.
LIMITE: se prueba el texto y el comportamiento, no que las frases le resulten claras al usuario."""

from __future__ import annotations

import sqlite3

import pytest

from app.persistence import db, master_index
from app.persistence import service as svc
from e2e_support import launch_chromium, open_app, serve_backend

pytestmark = pytest.mark.e2e

NOW = 1_800_000_000.0
USER_TAG = "tester_20260101000000"
CHIP_IS = "(p) => document.getElementById('meta-session-path').textContent === p"


@pytest.fixture(scope="module")
def base_url():
    with serve_backend(None) as url:
        yield url


@pytest.fixture(scope="module")
def browser(tmp_path_factory, base_url):
    with launch_chromium(tmp_path_factory.mktemp("chghist"), base_url) as b:
        yield b


@pytest.fixture
def app(browser, base_url):
    with open_app(browser, base_url) as a:
        # Cada guardado de carpeta que llega al backend (POST .../project-path), para poder afirmar que NO hubo ninguno: mirar el estado justo
        # despues de cerrar la pregunta pasaria por tiempo (el guardado todavia no termino), no por logica.
        a.saves = []
        a.page.on(
            "request",
            lambda r: a.saves.append(r.post_data) if r.method == "POST" and "/project-path" in r.url else None,
        )
        a.open()
        yield a


@pytest.fixture(autouse=True)
def tester_is_the_current_user(monkeypatch):
    monkeypatch.setenv("USERNAME", "tester")
    monkeypatch.setattr(svc, "current_windows_user", lambda: "tester")


@pytest.fixture
def folders(tmp_path):
    a, b = tmp_path / "carpeta-a", tmp_path / "carpeta-b"
    a.mkdir()
    b.mkdir()
    return a, b


def seed(folder, sid, messages, files=0):
    """Sesion REAL en la base de `folder`, del usuario de la prueba, con `messages` mensajes y `files` adjuntos."""
    path = db.db_path_for(str(folder))
    db.ensure_session(path, sid, NOW)
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "UPDATE session SET windows_user_created = ?, title = 'Mi sesion', title_manual = 1 WHERE id = ?",
            (USER_TAG, sid),
        )
        conn.commit()
    finally:
        conn.close()
    for n in range(messages):
        db.insert_message(path, f"{sid}-m{n}", sid, "user", NOW + n)
        db.insert_part(path, f"{sid}-p{n}", f"{sid}-m{n}", sid, "text", {"text": f"hola {n}"}, NOW + n)
    for i in range(files):
        db.insert_part(
            path,
            f"{sid}-f{i}",
            f"{sid}-m0",
            sid,
            "file",
            {"name": f"x{i}.pdf", "path": f"attachment/x{i}.pdf", "size": 1},
            NOW,
        )
    return path


def count(folder, sid) -> int:
    # El hilo escritor crea el archivo ANTES que el esquema: contar sin esperarlo falla por tiempo con la maquina cargada (suite completa
    # del 2026-10-24: «no such table: message»). `flush` espera a que la cola termine, y una base sin esquema aun cuenta como 0 mensajes.
    from app.persistence import writer

    writer.flush()
    path = db.db_path_for(str(folder))
    if not path.exists():
        return 0
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT COUNT(*) FROM message WHERE session_id = ?", (sid,)).fetchone()[0]
    except sqlite3.OperationalError:
        return 0
    finally:
        conn.close()


def current_session_id(app) -> str:
    app.page.wait_for_function("() => document.getElementById('meta-session-id').textContent.length > 8")
    return app.page.evaluate("() => document.getElementById('meta-session-id').textContent")


def real_session(app, folder, messages, files=0) -> str:
    """Toma el id de la sesion que el navegador YA usa, le siembra historial en `folder` y la registra alli."""
    sid = current_session_id(app)
    seed(folder, sid, messages, files)
    svc.assign_project_path(sid, str(folder), "")
    app.page.reload(wait_until="domcontentloaded")
    app.wait_ready()
    app.page.wait_for_function(CHIP_IS, arg=str(folder))
    return sid


def change_folder_to(app, new_path: str) -> None:
    """Abre el modal de edicion, elige la carpeta nueva y toca «Guardar».

    El campo de carpeta es de SOLO LECTURA a proposito: la persona la elige con «Examinar», que abre el Explorador de Windows
    (`/api/fs/native-picker`, un dialogo del sistema que un test no puede manejar). Se simula SOLO esa respuesta con `page.route`;
    todo lo demas (la pregunta, la copia, el indice, las bases) es el codigo y el backend reales."""
    app.page.route(
        "**/api/fs/native-picker*",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body='{"path": %s}' % __import__("json").dumps(new_path)
        ),
    )
    app.page.click("#meta-session")
    app.page.wait_for_selector("#session-edit-overlay:not(.hidden)")
    app.page.click("#session-edit-browse")
    app.page.wait_for_function("(p) => document.getElementById('session-edit-path').value === p", arg=new_path)
    app.page.click("#session-edit-save")


def question_text(app) -> str:
    app.page.wait_for_selector("#confirm-overlay:not(.hidden)")
    return app.page.inner_text("#confirm-message")


def question_buttons(app) -> list[str]:
    return app.page.locator("#confirm-overlay button:visible").all_inner_texts()


def no_question(app) -> bool:
    return app.page.locator("#confirm-overlay:not(.hidden)").count() == 0


def nothing_was_saved_after_dismissing(app) -> None:
    """Tras cerrar la pregunta (✕ o clic fuera) NO debe haber guardado nada. Se espera la senal propia del camino correcto (el modal de edicion
    limpia su estado «Guardando…») y un margen para que, si SI se guardara, el POST ya hubiera salido: un POST en localhost tarda milisegundos."""
    app.page.wait_for_selector("#confirm-overlay.hidden", state="attached")
    app.page.wait_for_function("() => document.getElementById('session-edit-status').textContent === ''")
    app.page.wait_for_timeout(1500)
    assert app.saves == [], f"se guardo la carpeta aunque se cerro la pregunta: {app.saves}"


class TestTheQuestion:
    def test_it_asks_with_the_numbers_and_the_two_folders(self, app, folders):
        a, b = folders
        real_session(app, a, 5, files=2)
        change_folder_to(app, str(b))
        text = question_text(app)
        assert "5 mensajes guardados" in text and str(a) in text and str(b) in text
        assert "no borra nada" in text
        assert "2 archivos adjuntos no se copian" in text
        assert "no recordará" in text

    def test_it_has_the_two_labelled_buttons_and_the_close(self, app, folders):
        a, b = folders
        real_session(app, a, 3)
        change_folder_to(app, str(b))
        question_text(app)
        assert question_buttons(app) == ["\u2715", "Dejarlo donde está", "Copiar el historial"]

    def test_one_message_is_in_the_singular(self, app, folders):
        a, b = folders
        real_session(app, a, 1)
        change_folder_to(app, str(b))
        assert "1 mensaje guardado en" in question_text(app)

    def test_no_attachments_sentence_without_attachments(self, app, folders):
        a, b = folders
        real_session(app, a, 2)
        change_folder_to(app, str(b))
        assert "adjunto" not in question_text(app)


class TestTheThreeAnswers:
    def test_copy_saves_with_the_history(self, app, folders):
        a, b = folders
        sid = real_session(app, a, 4)
        change_folder_to(app, str(b))
        question_text(app)
        app.page.click("#confirm-accept-btn")
        app.page.wait_for_function(CHIP_IS, arg=str(b))
        assert count(b, sid) == 4 and count(a, sid) == 4  # copiado y la carpeta vieja intacta
        assert master_index.get_project_path(sid) == str(b)

    def test_leave_it_saves_without_copying(self, app, folders):
        a, b = folders
        sid = real_session(app, a, 4)
        change_folder_to(app, str(b))
        question_text(app)
        app.page.click("#confirm-cancel-btn")
        app.page.wait_for_function(CHIP_IS, arg=str(b))
        assert count(b, sid) == 0 and count(a, sid) == 4
        assert master_index.get_project_path(sid) == str(b)

    def test_the_close_button_changes_nothing(self, app, folders):
        a, b = folders
        sid = real_session(app, a, 4)
        change_folder_to(app, str(b))
        question_text(app)
        app.page.click("#confirm-close")
        nothing_was_saved_after_dismissing(app)
        assert master_index.get_project_path(sid) == str(a)
        assert count(b, sid) == 0
        assert app.page.inner_text("#meta-session-path") == str(a)

    def test_a_click_outside_the_question_also_changes_nothing(self, app, folders):
        a, b = folders
        sid = real_session(app, a, 4)
        change_folder_to(app, str(b))
        question_text(app)
        app.page.evaluate("document.getElementById('confirm-overlay').click()")
        nothing_was_saved_after_dismissing(app)
        assert master_index.get_project_path(sid) == str(a)
        assert count(b, sid) == 0

    def test_leaving_it_is_a_save_without_copy_but_closing_is_not_a_save_at_all(self, app, folders):
        """«Dejarlo donde esta» y la ✕ se parecen y NO son lo mismo: la primera guarda la carpeta nueva, la segunda no guarda nada."""
        a, b = folders
        real_session(app, a, 4)
        change_folder_to(app, str(b))
        question_text(app)
        app.page.click("#confirm-cancel-btn")
        app.page.wait_for_function(CHIP_IS, arg=str(b))
        assert len(app.saves) == 1 and '"copy_history":false' in app.saves[0].replace(" ", "")

    def test_the_copy_answer_sends_copy_history_true(self, app, folders):
        a, b = folders
        real_session(app, a, 4)
        change_folder_to(app, str(b))
        question_text(app)
        app.page.click("#confirm-accept-btn")
        app.page.wait_for_function(CHIP_IS, arg=str(b))
        assert len(app.saves) == 1 and '"copy_history":true' in app.saves[0].replace(" ", "")

    def test_the_second_use_of_the_modal_has_its_usual_cancel_label_again(self, app, folders):
        """El boton secundario vuelve a decir «Cancelar» despues de la pregunta del cambio de carpeta (no se queda con «Dejarlo donde esta»)."""
        a, b = folders
        sid = real_session(app, a, 2)
        change_folder_to(app, str(b))
        question_text(app)
        app.page.click("#confirm-cancel-btn")
        app.page.wait_for_function(CHIP_IS, arg=str(b))
        assert app.page.locator("#confirm-cancel-btn").inner_text() == "Cancelar"
        assert master_index.get_project_path(sid) == str(b)


class TestItAsksOnlyWhenItApplies:
    def test_first_assignment_does_not_ask(self, app, folders):
        _, b = folders
        sid = current_session_id(app)
        change_folder_to(app, str(b))
        app.page.wait_for_function(CHIP_IS, arg=str(b))
        assert no_question(app)
        assert master_index.get_project_path(sid) == str(b)

    def test_a_session_without_history_does_not_ask(self, app, folders):
        a, b = folders
        real_session(app, a, 0)
        change_folder_to(app, str(b))
        app.page.wait_for_function(CHIP_IS, arg=str(b))
        assert no_question(app)

    def test_a_new_folder_that_already_has_the_history_does_not_ask(self, app, folders):
        a, b = folders
        sid = real_session(app, a, 4)
        seed(b, sid, 2)
        change_folder_to(app, str(b))
        app.page.wait_for_function(CHIP_IS, arg=str(b))
        assert no_question(app)
        assert count(b, sid) == 2  # no se mezcla

    def test_if_the_preview_fails_it_saves_as_always(self, app, folders):
        a, b = folders
        sid = real_session(app, a, 4)
        app.page.route("**/folder-change-preview*", lambda route: route.abort())
        change_folder_to(app, str(b))
        app.page.wait_for_function(CHIP_IS, arg=str(b))
        assert no_question(app)
        assert master_index.get_project_path(sid) == str(b) and count(b, sid) == 0

    def test_if_the_preview_answers_with_an_http_error_it_saves_as_always(self, app, folders):
        """La consulta puede responder 500 con un cuerpo que NO es el esperado: el guardado sigue sin preguntar y sin error de pagina."""
        a, b = folders
        sid = real_session(app, a, 4)
        app.page.route(
            "**/folder-change-preview*",
            lambda route: route.fulfill(
                status=500, content_type="application/json", body='{"applicable": true, "message_count": 4}'
            ),
        )
        change_folder_to(app, str(b))
        app.page.wait_for_function(CHIP_IS, arg=str(b))
        assert no_question(app)
        assert master_index.get_project_path(sid) == str(b) and count(b, sid) == 0

    def test_saving_only_the_name_does_not_ask(self, app, folders):
        a, _ = folders
        real_session(app, a, 4)
        app.page.click("#meta-session")
        app.page.wait_for_selector("#session-edit-overlay:not(.hidden)")
        app.page.fill("#session-edit-name", "Otro nombre")
        app.page.click("#session-edit-save")
        app.page.wait_for_selector("#session-edit-overlay.hidden", state="attached")
        assert no_question(app)


class TestTheOldConfirmModalIsUnchanged:
    """REQ-010: `showConfirmModal` sin parametros nuevos se comporta igual que siempre (lo usa, por ejemplo, borrar una sesion)."""

    def test_the_delete_modal_keeps_its_buttons_and_its_close_returns_no(self, app, folders):
        a, _ = folders
        sid = real_session(app, a, 2)
        app.page.evaluate(
            """(sid) => {
              const idx = JSON.parse(localStorage.getItem('vision.sessionsIndex') || '{}');
              idx[sid] = { name: 'Mi sesion', updatedAt: Date.now() + 1000, manuallyRenamed: true };
              localStorage.setItem('vision.sessionsIndex', JSON.stringify(idx));
            }""",
            sid,
        )
        app.page.reload(wait_until="domcontentloaded")
        app.wait_ready()
        app.page.evaluate("document.getElementById('acc-session').open = true")
        row = app.page.locator("#session-list li", has_text="Mi sesion").first
        row.hover()
        row.locator(".session-delete-btn").click()
        app.page.wait_for_selector("#confirm-overlay:not(.hidden)")
        assert question_buttons(app) == ["\u2715", "Cancelar", "Eliminar"]
        app.page.click("#confirm-close")  # la ✕ devuelve false como siempre: no se borra nada
        app.page.wait_for_selector("#confirm-overlay.hidden", state="attached")
        assert master_index.get_project_path(sid) == str(a)
        assert count(a, sid) == 2
