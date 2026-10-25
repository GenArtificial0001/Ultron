"""E2E del panel de accesos rapidos: labels nuevos y submenu "Comandos" (spec
quick-panel-labels-comandos, REQ-001..005) en un Chromium REAL contra el
backend real en loopback.

Lo que se prueba es lo que ve y hace el usuario: los 4 textos exactos que
dicto, el submenu in-place (sin modal aparte) con sus 2 secciones, que un
comando del panel dispare EXACTAMENTE lo mismo que el menu "/", y el import de
una carpeta de `.md` con su confirmacion antes de sobrescribir. El Explorador
nativo de Windows (`/api/fs/native-picker`, tkinter) se simula con
`page.route`: en un test no hay nadie para elegir la carpeta.
Infraestructura compartida en `e2e_support.py`.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app import commands
from app.llm import loop as loop_module
from e2e_support import FakeWhisper, launch_chromium, open_app, serve_backend

pytestmark = pytest.mark.e2e

MODEL = "claude-sonnet-4-6"
SESSION_ID_KEY = "vision.currentSessionId"
PICKER_ROUTE = "**/api/fs/native-picker*"


class FakeLLM:
    """El proveedor de IA falso: roster de un modelo y un chat que registra lo que recibe."""

    def __init__(self):
        self.whisper = FakeWhisper()
        self.audio = SimpleNamespace(transcriptions=self.whisper)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))
        self.requests: list[list[dict]] = []

    def get(self, path, cast_to=object):
        return {
            "data": [
                {
                    "id": MODEL,
                    "type": "LLM",
                    "aliases": [],
                    "valid_args": [],
                    "reasoning_levels": [],
                }
            ]
        }

    def _create(self, **kwargs):
        self.requests.append(kwargs.get("messages") or [])
        delta = SimpleNamespace(content="[[TIPO: conversacional]]Listo", tool_calls=[])
        stop = SimpleNamespace(content=None, tool_calls=[])
        return iter(
            [
                SimpleNamespace(
                    choices=[SimpleNamespace(delta=delta, finish_reason=None)]
                ),
                SimpleNamespace(
                    choices=[SimpleNamespace(delta=stop, finish_reason="stop")]
                ),
            ]
        )

    def user_texts(self) -> list[str]:
        texts = []
        for messages in self.requests:
            for m in messages:
                if m.get("role") == "user":
                    content = m.get("content")
                    texts.append(
                        content if isinstance(content, str) else json.dumps(content)
                    )
        return texts


@pytest.fixture(scope="module")
def proveedor():
    return FakeLLM()


@pytest.fixture(scope="module")
def base_url(proveedor):
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(loop_module, "get_client", lambda *a, **k: proveedor)
        with serve_backend(proveedor) as url:
            yield url


@pytest.fixture(scope="module")
def browser(tmp_path_factory, base_url):
    with launch_chromium(tmp_path_factory.mktemp("audio"), base_url) as b:
        yield b


@pytest.fixture
def app(browser, base_url, proveedor):
    proveedor.requests.clear()
    with open_app(browser, base_url) as a:
        a.open()
        yield a


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def assign_folder(app, folder) -> str:
    """Asigna carpeta de proyecto a la sesion de la pestaña (mismo endpoint que
    el modal de sesion) y devuelve el session_id."""
    folder.mkdir(parents=True, exist_ok=True)
    sid = app.page.evaluate(f"localStorage.getItem({json.dumps(SESSION_ID_KEY)})")
    app.page.evaluate(
        """async ([sid, path]) => {
          const r = await fetch(`/api/sessions/${sid}/project-path`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ path, title: "e2e comandos" }),
          });
          if (!r.ok) throw new Error(await r.text());
        }""",
        [sid, str(folder)],
    )
    return sid


def open_menu(app) -> None:
    if app.page.is_hidden("#quick-panel-menu"):
        app.page.click("#quick-panel-btn")
    app.page.wait_for_selector("#quick-panel-main-view", state="visible")


def open_commands_view(app) -> None:
    open_menu(app)
    app.page.click("#quick-panel-commands-btn")
    app.page.wait_for_selector("#quick-panel-commands-view", state="visible")
    # La lista se arma tras el fetch de /api/commands: esperar la 1ra seccion.
    app.page.wait_for_selector("#quick-panel-commands-list .quick-panel-skills-section")


def sections(app) -> dict[str, list[str]]:
    """{titulo de seccion: [textos de sus items]} del submenu de Comandos."""
    return app.page.evaluate(
        """() => Object.fromEntries(
          [...document.querySelectorAll('#quick-panel-commands-list .quick-panel-skills-section')].map((s) => [
            s.querySelector('.quick-panel-skills-section-title span').textContent,
            [...s.querySelectorAll('.quick-panel-skill-item, .quick-panel-skills-empty')].map((i) => i.textContent.trim()),
          ])
        )"""
    )


def click_command(app, name: str) -> None:
    app.page.locator(
        "#quick-panel-commands-list .quick-panel-skill-item", has_text=f"/{name}"
    ).first.click()


def route_picker(app, path=None, status: int = 200) -> list[str]:
    """Simula el Explorador nativo: devuelve `path` (o falla con `status`).
    Devuelve la lista de URLs pedidas, para saber si se llego a abrir."""
    calls: list[str] = []

    def handler(route):
        calls.append(route.request.url)
        if status != 200:
            route.fulfill(
                status=status,
                content_type="application/json",
                body=json.dumps(
                    {"detail": "No se pudo abrir el Explorador de Windows."}
                ),
            )
        else:
            route.fulfill(
                content_type="application/json",
                body=json.dumps({"path": str(path) if path else ""}),
            )

    app.page.route(PICKER_ROUTE, handler)
    return calls


def click_import(app) -> None:
    app.page.locator("#quick-panel-commands-list button", has_text="Importar").click()


# ---------------------------------------------------------------------------
# REQ-001: labels exactos
# ---------------------------------------------------------------------------
def test_the_four_items_show_the_exact_labels_the_user_dictated(app):
    open_menu(app)
    labels = app.page.evaluate(
        """() => Object.fromEntries(['loop', 'looprun', 'loopauto', 'browser'].map((k) => {
          const b = document.getElementById(`quick-panel-${k}-btn`);
          return [k, { label: b.querySelector('.quick-panel-item-label').textContent, title: b.title }];
        }))"""
    )
    assert labels["loop"]["label"] == "Reproducir en Bucle"
    assert labels["looprun"]["label"] == "Loop de Tareas"
    assert labels["loopauto"]["label"] == "Auto-Confirmación"
    assert labels["browser"]["label"] == "Navegador Integrado"
    # Tooltips coherentes: cada uno arranca con su label nuevo (no el viejo).
    for key, item in labels.items():
        assert item["title"].startswith(item["label"]), (key, item)
    title = app.page.text_content("#quick-browser-panel .quick-browser-title")
    assert title.startswith("Navegador Integrado")


# ---------------------------------------------------------------------------
# REQ-002: item "Comandos" debajo de Skills, submenu in-place
# ---------------------------------------------------------------------------
def test_commands_item_is_right_below_skills_and_opens_in_place(app):
    open_menu(app)
    below_skills = app.page.evaluate(
        "() => document.getElementById('quick-panel-skills-btn').nextElementSibling?.id"
    )
    assert below_skills == "quick-panel-commands-btn"
    assert (
        app.page.text_content("#quick-panel-commands-btn .quick-panel-item-chevron")
        == "›"
    )

    open_commands_view(app)
    assert app.page.is_hidden("#quick-panel-main-view")
    assert app.page.is_visible("#quick-panel-menu")  # mismo panel, no un modal aparte
    assert app.page.is_hidden("#command-modal-overlay")

    app.page.click("#quick-panel-commands-back")
    assert app.page.text_content("#quick-panel-commands-back") == "‹ Accesos rápidos"
    app.page.wait_for_selector("#quick-panel-main-view", state="visible")
    assert app.page.is_hidden("#quick-panel-commands-view")


def test_reopening_the_menu_starts_on_the_main_view(app):
    # showQuickPanelMainView() oculta AMBOS submenus: cerrar parado en Comandos
    # y volver a abrir no debe dejar el submenu (ni las dos vistas) a la vista.
    open_commands_view(app)
    app.page.click("#quick-panel-btn")  # cierra
    app.page.wait_for_selector("#quick-panel-menu", state="hidden")
    app.page.click("#quick-panel-btn")  # abre de nuevo
    app.page.wait_for_selector("#quick-panel-main-view", state="visible")
    assert app.page.is_hidden("#quick-panel-commands-view")
    assert app.page.is_hidden("#quick-panel-skills-view")


# ---------------------------------------------------------------------------
# REQ-003: dos secciones + estado vacio explicito
# ---------------------------------------------------------------------------
def test_lists_system_commands_and_an_explicit_empty_state_for_own(app):
    open_commands_view(app)
    got = sections(app)

    assert list(got) == ["Comandos del sistema", "Comandos propios"]
    system_names = {text.split("/", 1)[1] for text in got["Comandos del sistema"]}
    # spec comando-update-sesiones-publicas REQ-010: `/update` es un comando RESERVADO pero solo se OFRECE si la carpeta tiene conversaciones
    # publicas de otras personas; esta sesion no tiene carpeta, asi que no aparece.
    assert system_names == commands.RESERVED_NAMES - {"update"}
    assert got["Comandos propios"] == ["Sin comandos propios en esta sesión"]


def test_own_commands_of_the_session_folder_are_listed(app, tmp_path):
    project = tmp_path / "proyecto"
    (project / "commands").mkdir(parents=True)
    (project / "commands" / "saludo.md").write_text("decí hola", encoding="utf-8")
    assign_folder(app, project)

    open_commands_view(app)

    assert sections(app)["Comandos propios"] == ["✎ /saludo"]


# ---------------------------------------------------------------------------
# REQ-004: mismo camino que el menu "/"
# ---------------------------------------------------------------------------
def _modal(app) -> dict:
    app.page.wait_for_selector("#command-modal-overlay", state="visible")
    return {
        "title": app.page.text_content("#command-modal-title"),
        "body": app.page.text_content("#command-modal-body"),
    }


def test_a_system_command_opens_the_same_modal_as_the_slash_menu(app):
    open_commands_view(app)
    click_command(app, "export")
    from_panel = _modal(app)
    assert app.page.is_hidden("#quick-panel-menu")  # el panel se cierra al elegir
    app.page.click("#command-modal-close")
    app.page.wait_for_selector("#command-modal-overlay", state="hidden")

    app.page.fill("#chat-input", "/export")
    app.page.dispatch_event("#chat-input", "input")
    app.page.locator("#slash-menu .slash-item", has_text="/export").first.click()
    from_slash = _modal(app)

    assert from_panel == from_slash
    assert from_panel["title"] == "/export"


def test_an_own_command_sends_its_prompt_like_the_slash_menu(app, proveedor, tmp_path):
    project = tmp_path / "proyecto"
    (project / "commands").mkdir(parents=True)
    (project / "commands" / "saludo.md").write_text(
        "decí hola al depósito", encoding="utf-8"
    )
    assign_folder(app, project)

    open_commands_view(app)
    click_command(app, "saludo")

    app.page.wait_for_function(
        "() => [...document.querySelectorAll('.msg.user')].some(e => e.textContent.includes('decí hola al depósito'))"
    )
    app.page.wait_for_function(
        "() => [...document.querySelectorAll('.msg.assistant')].some(e => e.textContent.includes('Listo'))"
    )
    assert any("decí hola al depósito" in t for t in proveedor.user_texts())


# ---------------------------------------------------------------------------
# REQ-005: importar desde una carpeta
# ---------------------------------------------------------------------------
def test_import_copies_the_md_files_and_refreshes_the_list(app, tmp_path):
    project = tmp_path / "proyecto"
    assign_folder(app, project)
    src = tmp_path / "origen"
    src.mkdir()
    (src / "Resumen.md").write_text("resumí el turno", encoding="utf-8")
    (src / "init.md").write_text("pisaria el sistema", encoding="utf-8")
    route_picker(app, src)

    open_commands_view(app)
    click_import(app)

    app.wait_note("Comandos importados: /resumen")
    app.wait_note("/init")  # reservado: se informa, no se importa
    assert (project / "commands" / "resumen.md").read_text(
        encoding="utf-8"
    ) == "resumí el turno"
    assert not (project / "commands" / "init.md").exists()
    app.page.wait_for_function(
        "() => [...document.querySelectorAll('#quick-panel-commands-list .quick-panel-skill-item')]"
        ".some(e => e.textContent.includes('/resumen'))"
    )


@pytest.mark.parametrize(("accept", "expected"), [(False, "original"), (True, "nuevo")])
def test_import_asks_before_overwriting_an_existing_command(
    app, tmp_path, accept, expected
):
    project = tmp_path / "proyecto"
    (project / "commands").mkdir(parents=True)
    (project / "commands" / "resumen.md").write_text("original", encoding="utf-8")
    assign_folder(app, project)
    src = tmp_path / "origen"
    src.mkdir()
    (src / "resumen.md").write_text("nuevo", encoding="utf-8")
    route_picker(app, src)
    dialogs: list[str] = []

    def answer(dialog):
        dialogs.append(dialog.message)
        dialog.accept() if accept else dialog.dismiss()

    app.page.on("dialog", answer)
    open_commands_view(app)
    click_import(app)

    app.page.wait_for_function(
        "() => [...document.querySelectorAll('.msg.system-note')].some(e => e.textContent.includes('importó') || e.textContent.includes('importados'))"
    )
    assert len(dialogs) == 1 and "/resumen" in dialogs[0]
    assert (project / "commands" / "resumen.md").read_text(encoding="utf-8") == expected


def test_import_without_a_session_folder_asks_for_one_first(app, tmp_path):
    # Regla §6: sin carpeta asignada el import se rechaza con mensaje claro --
    # en la UI, se abre el modal de sesion para asignarla (mismo criterio que
    # export/comandos propios) y el Explorador ni se abre.
    calls = route_picker(app, tmp_path)

    open_commands_view(app)
    click_import(app)

    app.page.wait_for_selector("#session-edit-overlay", state="visible")
    assert calls == []


def test_if_the_folder_picker_fails_the_user_is_told(app, tmp_path):
    # QA-031 CERRADO: importCommandsFromFolder ahora pasa un proxy de
    # statusEl que muestra el error en el chat.
    assign_folder(app, tmp_path / "proyecto")
    route_picker(app, status=500)
    open_commands_view(app)
    # Ya hay una nota ("Sesión iniciada ...") desde la carga: se espera una NUEVA.
    notes_before = len(app.notes())

    click_import(app)

    app.page.wait_for_function(
        "n => document.querySelectorAll('.msg.system-note, .msg.error').length > n",
        arg=notes_before,
        timeout=5_000,
    )
