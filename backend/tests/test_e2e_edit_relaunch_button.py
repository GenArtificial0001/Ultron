"""E2E: tras EDITAR un mensaje ya respondido, el boton Detener vuelve a Enviar cuando el turno relanzado termina.

Hallazgo (2026-10-04): `test_e2e_attachments.py::test_editing_a_sent_message_resends_it_with_its_files` fallaba 2 veces de ~25 en la
suite. Causa: `attachToRelaunchedTurn` se engancha al stream del turno nuevo, `handleEvent` pone el boton en "Detener" con el primer
evento, pero nada lo devolvia a "Enviar" al terminar (solo lo hace el `finally` de `sendMessage`). Si el turno ya habia terminado
cuando el frontend se enganchaba, el stream venia vacio y el boton quedaba bien; si el turno seguia en curso, el boton quedaba pegado
en "Detener" hasta recargar la pagina. Era una carrera, por eso fallaba "a veces". Aca el proveedor TARDA a proposito para que el
turno siga en curso cuando el frontend se engancha: asi la carrera deja de ser azar y el bug se reproduce siempre.

El proveedor de IA es falso (nunca hay red ni login, ADR-015)."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from e2e_support import FakeWhisper, launch_chromium, open_app, serve_backend

pytestmark = pytest.mark.e2e

MODEL = "claude-sonnet-4-6"
SLOW_S = 3  # el turno relanzado sigue en curso cuando el frontend se engancha a su stream


def _chunk(content=None, finish=None):
    delta = SimpleNamespace(content=content, tool_calls=[])
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=finish)])


class Provider:
    """Responde enseguida a 'hola'; cualquier otro texto (la edicion) tarda SLOW_S antes de contestar."""

    def __init__(self):
        self.whisper = FakeWhisper()
        self.audio = SimpleNamespace(transcriptions=self.whisper)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))
        self._lock = threading.Lock()

    def get(self, path, cast_to=object):
        return {"data": [{"id": MODEL, "type": "LLM", "aliases": [], "valid_args": [], "reasoning_levels": []}]}

    def _create(self, **kwargs):
        user = next(m for m in reversed(kwargs["messages"]) if m.get("role") == "user")
        asked = user["content"] if isinstance(user["content"], str) else str(user["content"])
        if "editado" in asked:
            time.sleep(SLOW_S)
        return iter([_chunk("[[TIPO: conversacional]]Listo."), _chunk(finish="stop")])


@pytest.fixture(scope="module")
def provider():
    return Provider()


@pytest.fixture(scope="module")
def base_url(provider):
    from app.llm import loop as loop_module

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(loop_module, "get_client", lambda *a, **k: provider)
        with serve_backend(provider) as url:
            yield url


@pytest.fixture(scope="module")
def browser(tmp_path_factory, base_url):
    with launch_chromium(tmp_path_factory.mktemp("audio"), base_url) as b:
        yield b


@pytest.fixture()
def app(browser, base_url):
    with open_app(browser, base_url) as a:
        a.open()
        # El poll de fondo (cada 8 s) tambien engancha turnos en curso y, con su propio arreglo, deja el boton en "Enviar": sin esto
        # rescataba el boton por casualidad de fase y los tests no probaban `attachToRelaunchedTurn`. Se lo anula: siempre "no ocupada".
        a.page.route(
            "**/api/session/*/status",
            lambda route: route.fulfill(
                status=200, content_type="application/json", body='{"busy": false, "exists": true, "active_turn_id": null}'
            ),
        )
        yield a


def _send(app, text):
    app.page.fill("#chat-input", text)
    app.page.click("#send-btn")
    app.page.wait_for_selector(".msg.assistant:not(.streaming) .msg-play-btn", timeout=30_000)
    app.page.wait_for_selector("#send-btn[data-state=idle]", timeout=20_000)


def test_the_button_returns_to_send_after_editing_while_the_relaunched_turn_is_slow(app):
    _send(app, "hola")

    app.page.evaluate(
        "document.querySelector('.msg.user').closest('.turn-container').querySelector('.user-turn-btn[title^=Editar]').click()"
    )
    app.page.fill(".user-edit-textarea", "hola editado")
    app.page.click(".user-edit-actions .acc-action-btn:not(.secondary)")

    # mientras el modelo (falso) tarda, el turno esta en curso: el boton dice Detener
    app.page.wait_for_selector("#send-btn[data-state=active]", timeout=10_000)
    # y cuando el turno relanzado termina tiene que volver a Enviar SOLO, sin recargar la pagina
    app.page.wait_for_function(
        "document.querySelector('.msg.assistant:not(.streaming) .msg-play-btn') !== null && "
        "!document.querySelector('.msg.assistant.streaming')",
        timeout=30_000,
    )
    app.page.wait_for_selector("#send-btn[data-state=idle]", timeout=10_000)
    assert app.page.inner_text("#send-btn-label") == "Enviar"


def test_after_that_edit_the_user_can_type_and_send_again(app):
    """Consecuencia visible del bug: con el boton pegado en Detener, el submit queda bloqueado y no se puede mandar otro mensaje."""
    _send(app, "hola")
    app.page.evaluate(
        "document.querySelector('.msg.user').closest('.turn-container').querySelector('.user-turn-btn[title^=Editar]').click()"
    )
    app.page.fill(".user-edit-textarea", "hola editado")
    app.page.click(".user-edit-actions .acc-action-btn:not(.secondary)")
    # primero se confirma que el turno relanzado ARRANCO (boton en Detener); esperar "idle" antes seria trivial: todavia no cambio
    app.page.wait_for_selector("#send-btn[data-state=active]", timeout=10_000)
    app.page.wait_for_selector("#send-btn[data-state=idle]", timeout=40_000)
    app.page.wait_for_function("!document.querySelector('.msg.assistant.streaming')", timeout=10_000)

    app.page.fill("#chat-input", "hola otra vez")
    app.page.click("#send-btn")

    app.page.wait_for_function(
        "[...document.querySelectorAll('.msg.user')].some(m => m.innerText.includes('hola otra vez'))", timeout=10_000
    )
