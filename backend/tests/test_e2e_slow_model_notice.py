"""E2E del aviso de "el modelo esta tardando" (PQ-003 / BL-012, opcion (a), delegada por el usuario en ADR-018) en un Chromium REAL
contra el backend real.

Medicion en perf.log: 7 turnos esperaron mas de 2 minutos hasta la primera palabra (peor caso 733 s) y no habia ninguna senal de que
la demora era del MODELO. Ahora, si el modelo no empieza a responder en 30 s, aparece un aviso que ofrece esperar o cambiar de modelo.
NO cambia nada solo: ni salta de modelo, ni corta el pedido (esas eran las opciones (b) y (c), que no se eligieron).

El proveedor de IA es falso y TARDA de verdad (sleep real, mismo criterio que test_e2e_tech_report.py). Nunca hay red ni login (ADR-015).
"""

from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace

import pytest

from e2e_support import FakeWhisper, launch_chromium, open_app, serve_backend

pytestmark = pytest.mark.e2e

MODEL = "claude-sonnet-4-6"
SLOW_S = 33  # un poco mas que el umbral de 30 s
NOTICE = ".msg-slow-model-notice"


def _chunk(content=None, tool_calls=None, finish=None):
    delta = SimpleNamespace(content=content, tool_calls=tool_calls or [])
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=finish)])


def _call(index, call_id, name, arguments):
    return SimpleNamespace(index=index, id=call_id, function=SimpleNamespace(name=name, arguments=arguments))


class SlowProvider:
    """Segun lo que escribe el usuario: 'lento' tarda SLOW_S antes de la primera palabra; 'herramienta' hace una tool al instante y
    tarda SLOW_S recien en el paso siguiente (el tramo posterior a un resultado de tool); otro texto responde enseguida."""

    def __init__(self):
        self.whisper = FakeWhisper()
        self.audio = SimpleNamespace(transcriptions=self.whisper)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))
        self.models_requested: list[str | None] = []
        self._lock = threading.Lock()

    def get(self, path, cast_to=object):
        return {"data": [{"id": MODEL, "type": "LLM", "aliases": [], "valid_args": [], "reasoning_levels": []}]}

    def _create(self, **kwargs):
        with self._lock:
            self.models_requested.append(kwargs.get("model"))
        messages = kwargs["messages"]
        user = next(m for m in reversed(messages) if m.get("role") == "user")
        asked = user["content"] if isinstance(user["content"], str) else json.dumps(user["content"])
        after_tool = messages[-1].get("role") == "tool"
        if "muylento" in asked:
            time.sleep(SLOW_S * 2 + 8)  # dos veces el umbral: el aviso sale una vez y la espera sigue
        elif "lento" in asked or (after_tool and "herramienta" in asked):
            time.sleep(SLOW_S)
        if "parcial" in asked:
            return self._partial_stream()
        if "tareapesada" in asked and not after_tool:
            # El modelo responde AL INSTANTE con una herramienta que tarda mas que el umbral: la demora es de la HERRAMIENTA, no del modelo.
            # OJO: la tool corre bajo cmd.exe, donde "#" NO es un comentario (falla al instante con exit 1): el comando va solo.
            slow_cmd = "ping -n 61 127.0.0.1 > nul"
            return iter(
                [
                    # Sin texto previo a proposito: si el modelo dijera "Voy.", el evento de texto ya desarmaria el aviso y no se
                    # veria si el de tool-call hace su trabajo.
                    _chunk(tool_calls=[_call(0, "call_slow", "bash", json.dumps({"command": slow_cmd}))]),
                    _chunk(finish="tool_calls"),
                ]
            )
        if "herramienta" in asked and not after_tool:
            return iter(
                [
                    _chunk("[[TIPO: conversacional]]Voy."),
                    _chunk(tool_calls=[_call(0, "call_1", "bash", json.dumps({"command": "# Cuento\necho hola"}))]),
                    _chunk(finish="tool_calls"),
                ]
            )
        return iter([_chunk("[[TIPO: conversacional]]Listo."), _chunk(finish="stop")])


def _partial_stream_impl():
    """Empieza a responder AL INSTANTE y despues sigue largo: el modelo YA contesto, asi que el aviso de 'esta tardando' no corresponde."""
    yield _chunk("[[TIPO: conversacional]]Empiezo a contestar.")
    time.sleep(SLOW_S + 10)
    yield _chunk(" Y termino.")
    yield _chunk(finish="stop")


SlowProvider._partial_stream = staticmethod(_partial_stream_impl)


@pytest.fixture(scope="module")
def provider():
    return SlowProvider()


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
def app(browser, base_url, provider):
    provider.models_requested.clear()
    ctx = open_app(browser, base_url)
    a = ctx.__enter__()
    a.open()
    yield a
    ctx.__exit__(None, None, None)


def send(app, text):
    app.page.fill("#chat-input", text)
    app.page.click("#send-btn")


def notice(app):
    return app.page.query_selector(NOTICE)


def done(app, timeout=60_000):
    app.page.wait_for_selector(".msg.assistant:not(.streaming) .msg-play-btn", timeout=timeout)


def test_a_fast_answer_never_shows_the_notice(app):
    send(app, "hola")
    done(app)
    assert notice(app) is None


def test_a_slow_first_word_shows_the_notice_after_30_seconds_and_not_before(app, provider):
    send(app, "respondeme lento")
    app.page.wait_for_timeout(20_000)
    assert notice(app) is None  # a los 20 s todavia es una espera normal

    app.page.wait_for_selector(NOTICE, timeout=20_000)  # aparece a los ~30 s
    text = app.page.inner_text(NOTICE)
    assert "tardando" in text and "30" in text
    assert "esperar" in text and "selector de modelos" in text  # ofrece esperar o cambiar de modelo

    done(app)  # la respuesta llega sola: el aviso se va
    assert notice(app) is None
    assert len(provider.models_requested) == 1  # y NO se salto a otro modelo ni se reintento: solo aviso
    assert "Listo." in app.page.inner_text("#messages")


def test_the_notice_comes_back_when_the_wait_is_after_a_tool_result(app):
    """Una demora despues de ejecutar una tool tambien es el modelo (y puede ser el tramo mas largo del turno)."""
    send(app, "usa una herramienta")  # sin la palabra 'lento': el primer paso es rapido, la demora es la del paso SIGUIENTE a la tool
    app.page.wait_for_selector(".tool-call-badge.done", timeout=20_000)  # la tool corre al instante
    assert notice(app) is None  # el aviso NO se arma por la primera espera, que fue corta
    app.page.wait_for_selector(NOTICE, timeout=45_000)  # y aparece ~30 s despues del resultado de la tool
    done(app)
    assert notice(app) is None


def test_once_the_model_starts_answering_the_notice_never_appears(app):
    """Si la primera palabra llega rapido, una respuesta LARGA no es 'el modelo tarda en empezar': no corresponde avisar."""
    send(app, "respondeme parcial")
    app.page.wait_for_function("document.querySelector('.msg.assistant')?.innerText.includes('Empiezo a contestar')", timeout=20_000)
    app.page.wait_for_timeout(SLOW_S * 1000)  # pasa el umbral de 30 s con la respuesta ya en curso
    assert notice(app) is None
    done(app, timeout=40_000)


def test_the_notice_is_shown_once_even_if_the_wait_goes_on_for_minutes(app):
    send(app, "respondeme muylento")
    app.page.wait_for_selector(NOTICE, timeout=45_000)
    app.page.wait_for_timeout(8_000)  # la espera sigue (el modelo tarda el doble del umbral)
    assert len(app.page.query_selector_all(NOTICE)) == 1  # sigue habiendo UN solo aviso, no se apila otro
    done(app, timeout=90_000)
    assert notice(app) is None


def test_a_turn_that_ends_with_an_error_leaves_no_notice(app, provider):
    """Que el turno termine por un error del proveedor tambien limpia el aviso (no queda 'tardando' pegado)."""
    from types import SimpleNamespace as NS

    original = provider.chat
    def boom(**kwargs):
        time.sleep(SLOW_S)
        raise RuntimeError("falla simulada del proveedor")
    provider.chat = NS(completions=NS(create=boom))
    try:
        send(app, "respondeme cualquier cosa")
        app.page.wait_for_selector(NOTICE, timeout=45_000)
        app.page.wait_for_selector(".msg.error", timeout=60_000)
        app.page.wait_for_function(f"!document.querySelector('{NOTICE}')", timeout=10_000)
    finally:
        provider.chat = original


def test_a_slow_tool_is_not_blamed_on_the_model(app):
    """Una herramienta que tarda mas de 30 s no es 'el modelo esta tardando': el modelo ya contesto (pidio la herramienta) y el aviso
    seria falso. Es el unico caso en que el cancelar-al-recibir-la-tool-call importa: sin ese paso, el temporizador armado al enviar
    vencia con la herramienta todavia corriendo."""
    send(app, "tareapesada")
    app.page.wait_for_selector(".tool-call-badge.running", timeout=20_000)
    app.page.wait_for_timeout(34_000)  # pasa el umbral de 30 s con la herramienta (~60 s) todavia corriendo
    assert app.page.query_selector(".tool-call-badge.running") is not None  # (la herramienta sigue en curso)
    assert notice(app) is None
    done(app, timeout=90_000)


def test_stopping_the_turn_removes_the_notice(app):
    send(app, "respondeme lento")
    app.page.wait_for_selector(NOTICE, timeout=45_000)

    app.page.click("#send-btn")  # en este estado el boton es Detener
    app.page.wait_for_function(f"!document.querySelector('{NOTICE}')", timeout=10_000)
