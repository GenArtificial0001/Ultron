"""E2E de «saber si Vision avanza» (spec fix-detener-y-progreso-visible, bloque 1: REQ-001 a REQ-006) en un Chromium REAL contra el
backend real.

Reporte del usuario (2026-10-11 ~23:06, captura «Ejecutando comando… 91%» quieta): «yo no sabia como estaba avanzando entonces no se si esta
en un bucle infinito». El turno avanzaba (34 herramientas, termino bien) pero la pantalla no lo mostraba. Aca se reproduce un turno igual de
«mudo» con un modelo FALSO que tarda de verdad (sleep real) y se mira LO QUE SE VE en la tarjeta de progreso:
  * REQ-001: el tiempo transcurrido de la operacion actual («lleva 3 s») cambia solo, con un latido; al pasar lo estimado dice
    «… · sigue trabajando · lleva X» sin un % inventado y sin el relleno clavado en 91 %; con animaciones reducidas el latido queda quieto
    pero el tiempo sigue cambiando;
  * REQ-002: cada operacion arranca de 0 % (no se arrastra el % de la anterior) y despues de cada resultado de herramienta se lee
    «Pensando el próximo paso… (paso N)»;
  * REQ-003: mientras el modelo escribe los argumentos de un `write` se lee «Escribiendo transcribir.txt… N KB» con N creciendo;
  * REQ-004: mientras corre un bash REAL que imprime lineas, la tarjeta tiene «Salida en vivo» con esas lineas;
  * REQ-005: el aviso de modelo lento no dice «sin responder», y desaparece apenas el modelo empieza a escribir una herramienta;
  * revision F4 (REQ-002): cuando el siguiente mensaje de la cola arranca enseguida, la tarjeta terminada del turno anterior no queda
    pegada.

Los presupuestos de tiempo se fijan chicos (monkeypatch de `get_budget_ms` del bucle) para forzar el «sigue trabajando» en segundos.
El proveedor de IA es falso: nunca hay red ni login (ADR-015)."""

from __future__ import annotations

import json
import re
import sys
import time
from types import SimpleNamespace

import pytest

from app.llm import loop as loop_module
from e2e_support import FakeWhisper, launch_chromium, open_app, serve_backend

pytestmark = pytest.mark.e2e

MODEL = "claude-sonnet-4-6"
NOTICE = ".msg-slow-model-notice"
CARD = ".command-progress"

# Presupuestos chicos y CON historial: el % sale de la curva y el «sigue trabajando» aparece en segundos.
BUDGETS = {"thinking": (4000.0, True), "bash": (1000.0, True), "write": (1500.0, True)}

FIRST_THINK_S = 3.0  # el «Pensando…» del paso 1 llega a ~74 % de su presupuesto de 4 s
WRITE_PIECES, WRITE_PAUSE_S = 10, 0.5  # ~5 s escribiendo ~40 KB de argumentos
SECOND_THINK_S = 2.0
BASH_LINES, BASH_PAUSE_S = 7, 0.7


def _chunk(content=None, tool_calls=None, finish=None):
    delta = SimpleNamespace(content=content, tool_calls=tool_calls or [])
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=finish)])


def _tc(index, call_id, name, arguments):
    return SimpleNamespace(index=index, id=call_id, function=SimpleNamespace(name=name, arguments=arguments))


def _slow_tool_call(call_id, name, arguments, pieces, pause, think_s):
    """El modelo piensa `think_s` y despues escribe los argumentos de a pedazos (como el proveedor de IA con un archivo grande)."""
    time.sleep(think_s)
    yield _chunk(tool_calls=[_tc(0, call_id, name, None)])
    size = len(arguments) // pieces + 1
    for i in range(0, len(arguments), size):
        time.sleep(pause)
        yield _chunk(tool_calls=[_tc(0, None, None, arguments[i : i + size])])
    yield _chunk(finish="tool_calls")


def _after(seconds, chunks):
    time.sleep(seconds)
    yield from chunks


def _slow_answer(text, seconds, pieces=10):
    """El modelo escribe `text N.` de a pedazos durante `seconds`."""
    yield _chunk("[[TIPO: conversacional]]")
    for i in range(pieces):
        time.sleep(seconds / pieces)
        yield _chunk(f"{text} {i}. ")
    yield _chunk(finish="stop")


QUEUE_QUESTION = {"question": "Cual preferis?", "options": [{"label": "Primera opcion"}, {"label": "Segunda opcion"}]}


class Provider:
    def __init__(self, workdir):
        self.workdir = workdir
        self.whisper = FakeWhisper()
        self.audio = SimpleNamespace(transcriptions=self.whisper)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))
        script = workdir / "imprime.py"
        script.write_text(
            "import time\n"
            f"for i in range({BASH_LINES}):\n"
            "    print(f'linea {i} <b>no es html</b>', flush=True)\n"
            f"    time.sleep({BASH_PAUSE_S})\n",
            encoding="utf-8",
        )
        self.bash_command = f'"{sys.executable}" -u "{script}"'

    def get(self, path, cast_to=object):
        return {"data": [{"id": MODEL, "type": "LLM", "aliases": [], "valid_args": [], "reasoning_levels": []}]}

    def _write_args(self, kb=40):
        return json.dumps({"path": str(self.workdir / "transcribir.txt"), "content": "texto transcripto\n" * (kb * 1024 // 18)})

    def _create(self, **kwargs):
        messages = kwargs["messages"]
        last_user = max(i for i, m in enumerate(messages) if m.get("role") == "user")
        asked = json.dumps(messages[last_user]["content"])
        tools_done = sum(1 for m in messages[last_user:] if m.get("role") == "tool")
        if "flujo" in asked:
            if tools_done == 0:
                return _slow_tool_call("call_write", "write", self._write_args(), WRITE_PIECES, WRITE_PAUSE_S, FIRST_THINK_S)
            if tools_done == 1:
                call = _tc(0, "call_bash", "bash", json.dumps({"command": self.bash_command}))
                return _after(SECOND_THINK_S, [_chunk(tool_calls=[call]), _chunk(finish="tool_calls")])
            return _after(0.5, [_chunk("[[TIPO: conversacional]]Listo."), _chunk(finish="stop")])
        if "quieto" in asked:
            return _after(5.5, [_chunk("[[TIPO: conversacional]]Listo."), _chunk(finish="stop")])
        if "aviso" in asked and tools_done == 0:
            return _slow_tool_call("call_aviso", "write", self._write_args(kb=24), 6, 0.8, 32.0)
        # Revision F4: un turno que pregunta (tool `question`) y sigue escribiendo; la opcion tocada queda en cola y se responde despues.
        if "pregunta en cola" in asked:
            if tools_done == 0:
                call = _tc(0, "call_q", "question", json.dumps(QUEUE_QUESTION))
                return iter([_chunk(tool_calls=[call]), _chunk(finish="tool_calls")])
            return _slow_answer("Sigo con lo que pediste", 4.0)
        if "Primera opcion" in asked:
            return _slow_answer("Respuesta a tu opcion", 2.0)
        return iter([_chunk("[[TIPO: conversacional]]Listo."), _chunk(finish="stop")])


@pytest.fixture(scope="module")
def workdir(tmp_path_factory):
    path = tmp_path_factory.mktemp("trabajo")
    if " " in str(path) or " " in sys.executable:
        pytest.skip("rutas con espacios: el comando de cmd necesitaria otro quoteo")
    return path


@pytest.fixture(scope="module")
def provider(workdir):
    return Provider(workdir)


@pytest.fixture(scope="module")
def base_url(provider):
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(loop_module, "get_client", lambda *a, **k: provider)
        mp.setattr(loop_module, "get_budget_ms", lambda op: BUDGETS.get(op, (4000.0, False)))
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
        yield a


# Una foto de la tarjeta cada 40 ms (timers REALES), solo cuando cambia algo: lo que el usuario habria visto, en orden.
WATCH_CARD = """() => {
  window.__cardLog = [];
  let last = "";
  setInterval(() => {
    const el = document.querySelector(".command-progress");
    if (!el || el.classList.contains("hidden")) return;
    const q = (s) => el.querySelector(s);
    const beat = q(".progress-beat");
    const snap = {
      state: el.dataset.state || "",
      label: q(".progress-label")?.textContent || "",
      elapsed: q(".progress-elapsed")?.textContent || "",
      pct: q(".progress-pct")?.textContent || "",
      fillWidth: q(".progress-fill")?.style.width || "",
      liveSummary: q(".progress-live summary")?.textContent || "",
      live: q(".progress-live pre")?.textContent || "",
      liveHtml: q(".progress-live pre")?.innerHTML || "",
      text: el.textContent,
      beatAnim: beat ? getComputedStyle(beat).animationName : null,
      notice: !!document.querySelector(".msg-slow-model-notice"),
    };
    const key = JSON.stringify(snap);
    if (key !== last) { last = key; snap.t = performance.now(); window.__cardLog.push(snap); }
  }, 40);
}"""


def send(app, text):
    app.page.fill("#chat-input", text)
    app.page.click("#send-btn")


def done(app, timeout=60_000):
    app.page.wait_for_selector(".msg.assistant:not(.streaming) .msg-play-btn", timeout=timeout)


def seconds(elapsed_text: str) -> int | None:
    """«lleva 12 s» -> 12; «lleva 1 min 05 s» -> 65."""
    m = re.search(r"lleva (?:(\d+) min )?(\d+) s", elapsed_text)
    if not m:
        return None
    return int(m.group(1) or 0) * 60 + int(m.group(2))


def pct(snap) -> int | None:
    m = re.fullmatch(r"(\d+)%", snap["pct"].strip())
    return int(m.group(1)) if m else None


def test_a_long_turn_always_shows_what_is_happening_now(app):
    app.page.evaluate(WATCH_CARD)
    send(app, "hace el flujo largo")
    done(app, timeout=90_000)
    log = app.page.evaluate("window.__cardLog")
    assert log, "la tarjeta de progreso nunca se vio"

    # --- REQ-001: tiempo transcurrido que avanza solo + latido -------------------------------------------------------------
    first = [s for s in log if s["label"] == "Pensando…"]
    secs = [seconds(s["elapsed"]) for s in first if seconds(s["elapsed"]) is not None]
    assert secs and secs == sorted(secs) and max(secs) >= 2, [s["elapsed"] for s in first]
    assert any(s["beatAnim"] == "progress-beat" for s in first), "el latido tiene que pulsar"
    assert max(p for p in map(pct, first) if p is not None) >= 55  # la primera operacion llego alto

    # --- REQ-003: «Escribiendo transcribir.txt… N KB» con N creciendo ----------------------------------------------------
    writing = [s for s in log if s["label"].startswith("Escribiendo transcribir.txt…")]
    kbs = [int(m.group(1)) for s in writing if (m := re.search(r"(\d+) KB", s["label"]))]
    assert len(set(kbs)) >= 3 and kbs == sorted(kbs), [s["label"] for s in writing]
    assert all(seconds(s["elapsed"]) is not None for s in writing), "mientras escribe tambien se ve cuanto lleva"

    # --- REQ-002: despues del resultado de una herramienta, «Pensando el próximo paso… (paso 2)» que arranca de 0 % --------
    step2 = [s for s in log if s["label"] == "Pensando el próximo paso… (paso 2)"]
    assert step2, sorted({s["label"] for s in log})
    step2_pcts = [p for p in map(pct, step2) if p is not None]
    assert step2_pcts and step2_pcts[0] < 30, step2_pcts  # no hereda el ~74 % de «Pensando…»
    assert step2_pcts == sorted(step2_pcts)  # dentro de la misma operacion nunca retrocede
    assert any(s["label"] == "Pensando el próximo paso… (paso 3)" for s in log)

    # --- REQ-001 + REQ-002 en el bash: arranca bajo y, al pasar lo estimado, «sigue trabajando» sin % --------------------------
    bash = [s for s in log if s["label"] == "Ejecutando comando…"]
    bash_pcts = [p for p in map(pct, bash) if p is not None]
    assert bash_pcts and bash_pcts[0] < 30, bash_pcts
    overrun = [s for s in bash if "sigue trabajando" in s["text"]]
    assert overrun, [s["text"] for s in bash]
    for s in overrun:
        assert s["state"] == "overrun" and s["pct"] == "" and "%" not in s["text"]
        assert s["fillWidth"] in ("", "100%"), f"el relleno no puede quedar clavado: {s['fillWidth']}"
        assert re.search(r"Ejecutando comando… · sigue trabajando · lleva \d+ s", s["text"]), s["text"]
    over_secs = [seconds(s["elapsed"]) for s in overrun]
    assert max(over_secs) > min(over_secs), "en «sigue trabajando» el tiempo sigue corriendo"

    # --- REQ-004: «Salida en vivo» con las lineas del comando, como TEXTO (nunca HTML) -------------------------------------
    live = [s for s in bash if s["liveSummary"]]
    assert live and all(s["liveSummary"] == "Salida en vivo" for s in live)
    texts = [s["live"] for s in live]
    assert any("linea 0" in t for t in texts) and any("linea 3" in t for t in texts), texts
    assert all("<b>" not in s["liveHtml"] for s in live) and any("&lt;b&gt;" in s["liveHtml"] for s in live)

    # al terminar el turno la tarjeta se va (como siempre: se completa y se quita a los ~400 ms)
    app.page.wait_for_selector(CARD, state="detached", timeout=5_000)
    assert "Listo." in app.page.inner_text("#messages")


def test_with_reduced_motion_the_beat_stays_still_but_the_time_keeps_moving(app):
    app.page.emulate_media(reduced_motion="reduce")
    app.page.evaluate(WATCH_CARD)
    send(app, "respondeme quieto")
    done(app, timeout=30_000)
    log = app.page.evaluate("window.__cardLog")

    thinking = [s for s in log if s["label"] == "Pensando…"]
    assert thinking and all(s["beatAnim"] in ("none", None) for s in thinking), {s["beatAnim"] for s in thinking}
    secs = {seconds(s["elapsed"]) for s in thinking} - {None}
    assert len(secs) >= 3, secs  # el reloj NO depende de las animaciones
    overrun = [s for s in thinking if "sigue trabajando" in s["text"]]
    assert overrun and all(s["pct"] == "" for s in overrun)


def test_the_slow_model_notice_is_truthful_and_goes_away_when_the_model_starts_writing(app):
    app.page.evaluate(WATCH_CARD)
    send(app, "aviso de modelo lento")
    app.page.wait_for_selector(NOTICE, timeout=45_000)
    text = app.page.inner_text(NOTICE)
    assert "sin responder" not in text
    assert "tardando" in text and "30 segundos" in text and "selector de modelos" in text

    # El modelo empieza a escribir los argumentos de un archivo (~5 s): el aviso se va ANTES de que exista la herramienta.
    app.page.wait_for_function(f"!document.querySelector('{NOTICE}')", timeout=10_000)
    assert app.page.query_selector(".tool-call-badge") is None, "lo cancelo la escritura, no el tool-call"
    app.page.wait_for_function(
        "[...document.querySelectorAll('.progress-label')].some(e => e.textContent.startsWith('Escribiendo transcribir.txt…'))",
        timeout=10_000,
    )
    done(app, timeout=60_000)


def test_the_finished_card_of_a_turn_goes_away_when_the_next_one_in_the_queue_starts(app):
    """Revision F4 (REQ-002): un turno termina y el siguiente de la cola arranca enseguida (antes de los 400 ms del cierre diferido).
    La tarjeta del que termino («… · tardó X 100%») quedaba pegada para siempre debajo de su respuesta: la nueva se creaba en el lugar
    del otro mensaje y nadie quitaba la vieja."""
    page = app.page
    send(app, "hace una pregunta en cola")
    page.wait_for_selector("#send-btn[data-state=active]", timeout=10_000)
    page.locator("#question-panel:not(.hidden) .q-options button", has_text="Primera opcion").click(timeout=15_000)
    queued = page.locator(".user-turn-wrap").filter(has=page.locator(".msg.user", has_text="Primera opcion"))
    queued.locator(".user-turn-badge").filter(has_text="En cola").wait_for(timeout=10_000)

    # Termina el que pregunto, arranca el de la cola, y termina tambien.
    page.wait_for_function(
        "() => [...document.querySelectorAll('.msg.assistant:not(.streaming)')].some(e => e.innerText.includes('Respuesta a tu opcion 9'))",
        timeout=40_000,
    )
    page.wait_for_selector("#send-btn[data-state=idle]", timeout=15_000)
    page.wait_for_timeout(2_000)  # mucho mas que los 400 ms del cierre diferido
    cards = page.eval_on_selector_all(
        CARD,
        "els => els.map(e => ({ text: e.innerText.replace(/\\s+/g, ' '), state: e.dataset.state,"
        " turn: e.closest('.turn-container')?.querySelector('.msg.user')?.innerText || null }))",
    )
    assert cards == [], f"quedo pegada la tarjeta de un turno que ya termino: {cards}"
