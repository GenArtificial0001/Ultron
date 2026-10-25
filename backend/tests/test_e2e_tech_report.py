"""E2E del informe tecnico hablado (spec informe-tecnico-hablado) en un Chromium
REAL contra el backend real.

El proveedor de IA es falso y responde segun el mensaje:
  * "contá": pide ejecutar un comando inofensivo (`echo`) con un comentario, y
    despues responde.
  * "armá": arma un plan (todowrite) y escribe `demo.html` MANDANDO EL CODIGO DE
    A PEDAZOS, como el modelo real; despues tarda 17 s en responder (para el
    aviso de "Sigo con...").
La voz del navegador es un mock que registra lo que se le pidio decir. Nunca hay
red externa.
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

from e2e_support import FakeWhisper, launch_chromium, open_app, serve_backend

pytestmark = pytest.mark.e2e

MODEL = "claude-sonnet-4-6"
COMMAND = "# Cuento los archivos de prueba del informe\necho hola"
EXPECTED = "Ejecuto un comando: cuento los archivos de prueba del informe"
DASHBOARD = (
    "<!DOCTYPE html>\n<html>\n<head>\n<title>Panel de Salud</title>\n</head>\n<body>\n"
    "<!-- Sidebar a la derecha con el menú de secciones -->\n"
    '<aside class="sidebar sidebar-right"></aside>\n'
    "<!-- Tres tarjetas de KPIs: pacientes, consultas y diagnósticos -->\n"
    '<div class="card kpi">Pacientes</div>\n</body>\n</html>\n'
)
PLAN = [
    {
        "content": "Armar dashboard demo de salud",
        "status": "in_progress",
        "priority": "high",
        "subtasks": [
            {"content": "Diseñar la estructura", "status": "in_progress", "priority": "high"},
            {"content": "Escribir el HTML", "status": "pending", "priority": "high"},
        ],
    }
]

SPEECH_MOCK = """
(() => {
  window.__spoken = [];
  const fake = {
    speaking: false,
    getVoices: () => [{ name: "Fake ES", lang: "es-419", localService: true, voiceURI: "fake-es" }],
    speak: (utter) => {
      window.__spoken.push(utter.text);
      fake.speaking = true;
      setTimeout(() => { fake.speaking = false; utter.onend && utter.onend(); }, 150);
    },
    cancel: () => { fake.speaking = false; },
    onvoiceschanged: null,
  };
  Object.defineProperty(window, "speechSynthesis", { value: fake, configurable: true });
  window.SpeechSynthesisUtterance = function (text) { this.text = text; };
})();
"""


def _chunk(content=None, tool_calls=None, finish=None):
    delta = SimpleNamespace(content=content, tool_calls=tool_calls or [])
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=finish)])


def _call(index, call_id, name, arguments):
    return SimpleNamespace(index=index, id=call_id, function=SimpleNamespace(name=name, arguments=arguments))


def _fragment(index, arguments):
    """Un pedazo mas de los argumentos de una tool-call ya anunciada."""
    return SimpleNamespace(index=index, id=None, function=SimpleNamespace(name=None, arguments=arguments))


class FakeProvider:
    def __init__(self):
        self.whisper = FakeWhisper()
        self.audio = SimpleNamespace(transcriptions=self.whisper)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))
        self.user_messages: list[str] = []

    def get(self, path, cast_to=object):
        return {"data": [{"id": MODEL, "type": "LLM", "aliases": [], "valid_args": [], "reasoning_levels": []}]}

    def _create(self, **kwargs):
        messages = kwargs["messages"]
        user = next(m for m in reversed(messages) if m.get("role") == "user")
        content = user["content"] if isinstance(user["content"], str) else json.dumps(user["content"])
        self.user_messages.append(content)
        if messages[-1].get("role") == "tool":
            if "armá" in content:
                time.sleep(17)  # REQ-011: mas de 15 s sin novedades
            return iter([_chunk("Listo."), _chunk(finish="stop")])
        if "armá" in content:
            return self._dashboard_stream()
        return iter(
            [
                _chunk("[[TIPO: conversacional]]Voy a contarlos."),
                _chunk(tool_calls=[_call(0, "call_1", "bash", json.dumps({"command": COMMAND}))]),
                _chunk(finish="tool_calls"),
            ]
        )

    def _dashboard_stream(self):
        yield _chunk("[[TIPO: conversacional]]Arranco.")
        yield _chunk(tool_calls=[_call(0, "call_plan", "todowrite", json.dumps({"todos": PLAN}))])
        args = json.dumps({"path": "demo.html", "content": DASHBOARD})
        yield _chunk(tool_calls=[_call(1, "call_html", "write", args[:20])])
        for i in range(20, len(args), 40):
            time.sleep(0.2)  # el codigo llega de a pedazos, como con el modelo real
            yield _chunk(tool_calls=[_fragment(1, args[i : i + 40])])
        yield _chunk(finish="tool_calls")


@pytest.fixture(scope="module")
def provider():
    return FakeProvider()


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


def open_with(browser, base_url, *, tech_report: bool | None):
    ctx = open_app(browser, base_url)
    app = ctx.__enter__()
    app.page.add_init_script(SPEECH_MOCK)
    setup = "localStorage.setItem('vision.tts.enabled', 'true');"
    if tech_report is not None:
        setup += f"localStorage.setItem('vision.tts.techReport', '{str(tech_report).lower()}');"
    app.page.add_init_script(setup)
    app.open()
    return ctx, app


def run_turn(app, text="contá los archivos", timeout=30_000):
    app.page.fill("#chat-input", text)
    app.page.click("#send-btn")
    app.page.wait_for_selector(".msg.assistant:not(.streaming) .msg-play-btn", timeout=timeout)


def test_by_default_each_action_is_said_in_one_phrase_and_the_chat_does_not_change(browser, base_url):
    """REQ-001 (prendido de fabrica), REQ-002, REQ-005."""
    ctx, app = open_with(browser, base_url, tech_report=None)
    try:
        run_turn(app)
        app.page.wait_for_function(f"window.__spoken.includes({json.dumps(EXPECTED)})")
        spoken = app.page.evaluate("window.__spoken")
        assert "Ejecutando comando…" not in spoken  # reemplaza la narracion corta
        assert EXPECTED not in app.page.inner_text("#messages")  # solo en voz
        assert app.page_errors == []
    finally:
        ctx.__exit__(None, None, None)


def test_turned_off_the_short_narration_comes_back(browser, base_url, provider):
    """REQ-001: con el interruptor apagado vuelve la etiqueta de siempre, y al
    modelo no se le pide que comente (REQ-009)."""
    ctx, app = open_with(browser, base_url, tech_report=False)
    try:
        run_turn(app)
        app.page.wait_for_function("window.__spoken.includes('Ejecutando comando…')")
        assert EXPECTED not in app.page.evaluate("window.__spoken")
        assert "[Informe técnico hablado]" not in provider.user_messages[-1]
    finally:
        ctx.__exit__(None, None, None)


def test_the_queue_never_falls_more_than_three_phrases_behind(browser, base_url):
    """REQ-004: la que suena termina; de las que esperan quedan las 3 mas
    nuevas, en orden; la lectura de la respuesta nunca se descarta."""
    ctx, app = open_with(browser, base_url, tech_report=True)
    try:
        app.page.evaluate(
            """(async () => {
                const m = await import('/js/tts.js');
                m.speak('Respuesta que se lee entera.');
                for (const n of ['uno', 'dos', 'tres', 'cuatro', 'cinco']) {
                    m.speak(`paso ${n}`, { limitKey: 'tech-report', limit: 3 });
                }
            })()"""
        )
        app.page.wait_for_function("window.__spoken.length >= 4 && !window.speechSynthesis.speaking")
        app.page.wait_for_timeout(400)
        assert app.page.evaluate("window.__spoken") == [
            "Respuesta que se lee entera.",
            "paso tres",
            "paso cuatro",
            "paso cinco",
        ]
    finally:
        ctx.__exit__(None, None, None)


def test_the_code_is_narrated_live_the_plan_is_announced_and_it_never_goes_silent(browser, base_url, provider):
    """REQ-008 (recorrido en vivo), REQ-009 (se le pide al modelo que comente),
    REQ-010 (avance del plan) y REQ-011 (aviso tras ~15 s sin novedades)."""
    ctx, app = open_with(browser, base_url, tech_report=True)
    try:
        app.page.fill("#chat-input", "armá el dashboard")
        app.page.click("#send-btn")
        # El codigo se narra mientras llega; el plan, cuando se ejecuta la tool.
        app.page.wait_for_function("window.__spoken.includes('Título: Panel de Salud')", timeout=30_000)
        app.page.wait_for_function(
            "window.__spoken.includes('Paso 1 de 2: Diseñar la estructura')", timeout=15_000
        )
        assert "Empiezo: Armar dashboard demo de salud" in app.page.evaluate("window.__spoken")
        order = [
            "Escribo demo.html",
            "Título: Panel de Salud",
            "sidebar a la derecha con el menú de secciones",
            "Armo el sidebar del lado derecho",
        ]
        app.page.wait_for_function(f"window.__spoken.includes({json.dumps(order[-1])})", timeout=15_000)
        spoken = app.page.evaluate("window.__spoken")
        positions = [spoken.index(p) for p in order]
        assert positions == sorted(positions)  # en el orden del codigo
        dashboard_request = next(m for m in provider.user_messages if "armá" in m)
        assert "[Informe técnico hablado]" in dashboard_request
        app.page.wait_for_function(
            "window.__spoken.includes('Sigo con: Diseñar la estructura')", timeout=25_000
        )
        app.page.wait_for_selector(".msg.assistant:not(.streaming) .msg-play-btn", timeout=30_000)
        assert app.page_errors == []
    finally:
        ctx.__exit__(None, None, None)
