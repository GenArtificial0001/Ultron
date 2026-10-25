"""E2E del Navegador Integrado (spec navegador-integrado-ventana, REQ-001/002)
en un Chromium REAL contra el backend real en loopback.

Bug reportado por el usuario (2026-09-28, QA-034, CERRADO): "el panel se abre
solo despues de generar un dashboard, cuando luego se hace una pregunta".
Causa: `maybeAutoRevealBrowser()` corria en CADA `done` y decidia con
`_browserSeenFingerprint`, una variable en memoria -- despues de recargar la
pestaña, la primera pregunta de cualquier tipo encontraba una huella "nueva" y
abria el panel. Fix real: la apertura automatica ahora depende del evento
puntual `browser-page-created` que el backend emite SOLO cuando el turno
completo crea la pagina de entrada (`_browser_index_snapshot` en
`llm/loop.py`, comparado antes/despues del turno) -- nunca de re-consultar el
estado del filesystem contra memoria volatil del frontend.

Los otros tests son guardianes de NO REGRESION del arreglo: crear una pagina
en el turno la sigue abriendo (una sola vez) y el boton "Navegador Integrado"
sigue mostrando la vista previa. Infraestructura en `e2e_support.py`.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.browser_workspace import get_browser_dir
from app.llm import loop as loop_module
from e2e_support import FakeWhisper, launch_chromium, open_app, serve_backend

pytestmark = pytest.mark.e2e

MODEL = "claude-sonnet-4-6"
SESSION_ID_KEY = "vision.currentSessionId"
ANSWER = "Listo, respondido."
# Tiempo para que un auto-revelado (fetch de huella + abrir) ya hubiera pasado:
# hoy tarda milisegundos despues de `done`.
SETTLE_MS = 1_500


def _chunk(content=None, tool_calls=None, finish=None):
    delta = SimpleNamespace(content=content, tool_calls=tool_calls or [])
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=finish)])


def _answer():
    return [_chunk(content=f"[[TIPO: conversacional]]{ANSWER}"), _chunk(finish="stop")]


def _write_call(path, content):
    call = SimpleNamespace(
        index=0,
        id="call_1",
        function=SimpleNamespace(
            name="write", arguments=json.dumps({"path": str(path), "content": content})
        ),
    )
    return [_chunk(tool_calls=[call]), _chunk(finish="tool_calls")]


class FakeLLM:
    """El proveedor de IA falso con guion: cada llamada consume la proxima respuesta de
    `script`; sin guion, contesta texto."""

    def __init__(self):
        self.whisper = FakeWhisper()
        self.audio = SimpleNamespace(transcriptions=self.whisper)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))
        self.script: list[list] = []

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
        return iter(self.script.pop(0) if self.script else _answer())


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
    proveedor.script.clear()
    with open_app(browser, base_url) as a:
        a.open()
        yield a


def session_id(app) -> str:
    return app.page.evaluate(f"localStorage.getItem({json.dumps(SESSION_ID_KEY)})")


def ask_and_wait(app, text: str, answers_before: int) -> None:
    """Manda un mensaje y espera a que termine el turno (llega la respuesta)."""
    app.page.fill("#chat-input", text)
    app.page.click("#send-btn")
    app.page.wait_for_function(
        "n => [...document.querySelectorAll('.msg.assistant')].filter(e => e.textContent.includes('Listo')).length > n",
        arg=answers_before,
    )
    app.page.wait_for_timeout(SETTLE_MS)


def answers(app) -> int:
    return app.page.evaluate(
        "() => [...document.querySelectorAll('.msg.assistant')].filter(e => e.textContent.includes('Listo')).length"
    )


def panel_visible(app) -> bool:
    return app.page.is_visible("#quick-browser-panel")


# ---------------------------------------------------------------------------
# REQ-001: el bug reportado
# ---------------------------------------------------------------------------
def test_a_question_after_reload_does_not_open_the_panel(app):
    # QA-034 CERRADO: la apertura automatica ya depende del evento puntual
    # 'browser-page-created' del turno (ver handleEvent en app.js), no de
    # comparar la huella del filesystem contra una variable en memoria que
    # se reiniciaba en cada carga de pestaña.
    # Un dashboard generado antes (en otra carga de la pestaña).
    preview = get_browser_dir(session_id(app))
    preview.mkdir(parents=True, exist_ok=True)
    (preview / "index.html").write_text(
        "<html><body>Tablero de stock</body></html>", encoding="utf-8"
    )
    app.page.reload(wait_until="domcontentloaded")
    app.wait_ready()
    assert not panel_visible(app)

    ask_and_wait(app, "¿cuántos pallets hay en playa?", answers(app))

    assert not panel_visible(app), (
        "una pregunta que no crea ninguna página abrió el Navegador Integrado"
    )


# ---------------------------------------------------------------------------
# REQ-002 y comportamiento existente: guardianes de NO REGRESION
# ---------------------------------------------------------------------------
def test_creating_a_page_in_the_turn_opens_the_panel_and_later_turns_do_not_reopen_it(
    app, proveedor
):
    sid = session_id(app)
    page = get_browser_dir(sid) / "index.html"
    proveedor.script = [
        _write_call(page, "<html><body>Tablero nuevo</body></html>"),
        _answer(),
    ]

    ask_and_wait(app, "armame un tablero de stock", answers(app))

    assert page.exists()
    app.page.wait_for_selector("#quick-browser-panel", state="visible", timeout=5_000)
    assert f"/browser/{sid}/" in (
        app.page.get_attribute("#quick-browser-iframe", "src") or ""
    )

    # El usuario lo cierra: un turno posterior que no crea nada no lo reabre.
    app.page.click("#quick-browser-close-btn")
    assert not panel_visible(app)
    ask_and_wait(app, "gracias, ¿algo más?", answers(app))
    assert not panel_visible(app)


def test_the_browser_button_shows_the_live_preview(app):
    sid = session_id(app)
    preview = get_browser_dir(sid)
    preview.mkdir(parents=True, exist_ok=True)
    (preview / "index.html").write_text(
        "<html><body>Vista previa</body></html>", encoding="utf-8"
    )

    app.page.click("#quick-panel-btn")
    app.page.click("#quick-panel-browser-btn")

    app.page.wait_for_selector("#quick-browser-panel", state="visible")
    # El `src` se asigna dentro de una consulta asincrona a la huella de la carpeta: leerlo
    # justo al abrir el panel fallaba al azar con la PC cargada (suite completa, 2026-09-30).
    app.page.wait_for_function(
        "(sid) => (document.getElementById('quick-browser-iframe').getAttribute('src') || '').includes('/browser/' + sid + '/')",
        arg=sid,
        timeout=15_000,
    )
    assert f"/browser/{sid}/" in (
        app.page.get_attribute("#quick-browser-iframe", "src") or ""
    )
    app.page.click("#quick-browser-close-btn")
    assert not panel_visible(app)
