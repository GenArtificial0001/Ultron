"""REQ-018 (spec fix-detener-y-progreso-visible, bloque 4 «Cada sesion con lo suyo», D5 = «Marca en la lista»), en un Chromium REAL
contra el backend real en loopback y un proveedor de IA falso (nunca hay red ni login).

Reporte del usuario (2026-10-13): «estaba escribiendo en una sesion y mande una pregunta, esperando la respuesta, pero me surgio otra duda y
salte a otra sesion sin guardar y me hizo una pregunta referida a la sesion de la cual venia sin haber escrito nada en la nueva ventana de
sesion. no deberian cruzarse conversaciones de una sesion a otra». Causa: el panel de preguntas (tool `question`) es uno solo para toda la
pagina, se dibujaba sin mirar de que sesion era la pregunta, y la opcion elegida se mandaba a la sesion ACTIVA (la respuesta de A terminaba
en B). `chart-ready` (graficos) tampoco miraba la sesion.

Contrato observable que se prueba (no la implementacion):
  * una pregunta de la sesion A que llega estando el usuario en B no aparece en B; en la lista de sesiones A muestra «❓ te hizo una
    pregunta» (en la sesion donde esta el usuario no aparece nada); al volver a A aparece la pregunta; contestarla manda el mensaje a A
    (verificado en el backend: queda en A y no en B) y la marca se va;
  * si el usuario se va de A con la pregunta en pantalla, el panel se oculta, A queda marcada, tocar una opcion del panel oculto no manda
    nada a ningun lado, y al volver la pregunta esta ahi; mandar otro mensaje en A tambien la da por contestada;
  * un grafico de A no se abre en B (en la sesion activa si);
  * una pregunta que el loop autonomo contesto solo sigue mostrandose como siempre en SU sesion, pero no en otra y sin dejar marca;
  * un loop de A que espera una respuesta no muestra su banner (ni su alarma) en B; al volver a A estan el banner y la pregunta;
  * con F5, la pregunta todavia sin contestar de la sesion abierta se recupera del historial de SQLite (y ya no, despues de contestarla).
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.llm import loop as loop_module
from app.persistence import writer
from app.session.events import get_session_store
from e2e_support import FakeWhisper, launch_chromium, open_app, serve_backend

pytestmark = pytest.mark.e2e

MODEL = "claude-sonnet-4-6"
QUESTION_TEXT = "¿Sigo con el inventario del CDM?"
OPTIONS = ["Seguí con el inventario", "Pará acá"]
LOOP_OPTIONS = ["Seguí con el inventario (Recomendada)", "Pará acá"]
ASK_DELAY_S = 3.0  # el modelo tarda en llamar a la tool: tiempo de sobra para que el usuario se vaya a otra sesion
CHART_TITLE = "Pallets por zona de la sesion A"
CHART_ARGS = {
    "chart_type": "bar",
    "category_column": "zona",
    "value_column": "pallets",
    "title": CHART_TITLE,
    "csv_fallback": "zona,pallets\nNRC,12\nNRT,7\nNRK,3\n",
}

# Opener SIN proxy: el backend de prueba esta en loopback. `urllib.request.urlopen` toma el proxy del sistema (en esta PC, el PAC de la VPN
# de la empresa) y a veces intenta salir por `proxy.empresa.com` incluso para 127.0.0.1: el guardian de red de los tests lo bloquea (intermitente,
# depende de la VPN; 30 fallos en la bateria completa del 2026-10-14 y 0 al correr los mismos archivos aparte). Mismo arreglo que test_e2e_voice_engines.py.
_LOOPBACK = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _chunk(content=None, tool_calls=None, finish=None):
    delta = SimpleNamespace(content=content, tool_calls=tool_calls or [])
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=finish)])


def _tool_call(call_id: str, name: str, args: dict, delay: float):
    """Generador: el modelo piensa `delay` segundos y llama a la tool `name` (el loop lo consume en un hilo)."""
    time.sleep(delay)
    call = SimpleNamespace(index=0, id=call_id, function=SimpleNamespace(name=name, arguments=json.dumps(args)))
    yield _chunk(tool_calls=[call])
    yield _chunk(finish="tool_calls")


def _text(content) -> str:
    if isinstance(content, list):
        return " ".join(p.get("text", "") for p in content if isinstance(p, dict))
    return str(content or "")


class Provider:
    """«pregunta» -> tool `question` (con «recomendada»: una opcion «(Recomendada)», la que el loop elige solo); «grafico» -> tool
    `render_chart`; cualquier otro mensaje -> «eco [mN]» con la marca del mensaje. Anota los textos de usuario que le llegan."""

    def __init__(self):
        self.whisper = FakeWhisper()
        self.audio = SimpleNamespace(transcriptions=self.whisper)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))
        self.received: list[str] = []
        self._lock = threading.Lock()

    def get(self, path, cast_to=object):
        return {"data": [{"id": MODEL, "type": "LLM", "aliases": [], "valid_args": [], "reasoning_levels": []}]}

    def got(self, text: str) -> bool:
        with self._lock:
            return any(text in r for r in self.received)

    def _create(self, **kwargs):
        messages = kwargs.get("messages") or []
        last_user = max(i for i, m in enumerate(messages) if m.get("role") == "user")
        user_text = _text(messages[last_user].get("content"))
        tools_after = sum(1 for m in messages[last_user:] if m.get("role") == "tool")
        if tools_after == 0:
            with self._lock:
                self.received.append(user_text)
        if "pregunta" in user_text:
            if tools_after == 0:
                labels = LOOP_OPTIONS if "recomendada" in user_text else OPTIONS
                args = {"question": QUESTION_TEXT, "options": [{"label": lab, "description": ""} for lab in labels]}
                return _tool_call("call_q1", "question", args, ASK_DELAY_S)
            return iter([_chunk("[[TIPO: conversacional]]Te dejé una pregunta para seguir."), _chunk(finish="stop")])
        if "grafico" in user_text:
            if tools_after == 0:
                return _tool_call("call_chart", "render_chart", CHART_ARGS, ASK_DELAY_S)
            return iter([_chunk("[[TIPO: conversacional]]Listo el gráfico."), _chunk(finish="stop")])
        found = re.findall(r"\[m\d+\]", user_text)
        tag = found[-1] if found else "[m?]"
        return iter([_chunk(f"[[TIPO: conversacional]]eco {tag}."), _chunk(finish="stop")])


@pytest.fixture(scope="module")
def provider():
    return Provider()


@pytest.fixture(scope="module")
def base_url(provider):
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(loop_module, "get_client", lambda *a, **k: provider)
        with serve_backend(provider) as url:
            yield url


@pytest.fixture(scope="module")
def browser(tmp_path_factory, base_url):
    with launch_chromium(tmp_path_factory.mktemp("audio"), base_url) as b:
        yield b


@pytest.fixture
def app(browser, base_url):
    with open_app(browser, base_url) as a:
        a.open()
        a.sid = a.page.evaluate("localStorage.getItem('vision.currentSessionId')")
        yield a


# ---------------------------------------------------------------------------
# Ayudantes
# ---------------------------------------------------------------------------
def api(base_url: str, method: str, path: str, body=None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base_url + path, data=data, method=method, headers={"Content-Type": "application/json"})
    with _LOOPBACK.open(req, timeout=30) as r:
        return json.loads(r.read().decode() or "{}")


def session_busy(base_url: str, sid: str) -> bool:
    return bool(api(base_url, "GET", f"/api/session/{sid}/status").get("busy"))


def wait_until(page, pred, timeout_s: float) -> bool:
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        if pred():
            return True
        page.wait_for_timeout(100)
    return False


def user_texts(sid: str) -> list[str]:
    """Lo que el backend tiene como mensajes del USUARIO de esa sesion (memoria del turno: la verdad de lo que ve el modelo)."""
    session = get_session_store().get(sid)
    if session is None:
        return []
    return [_text(m.get("content")) for m in list(session.messages) if m.get("role") == "user"]


def send(page, tag: str, text: str = "hola") -> None:
    page.fill("#chat-input", f"{text} {tag}")
    page.click("#send-btn")
    page.wait_for_function(
        "t => [...document.querySelectorAll('.msg.assistant:not(.streaming)')].some(e => e.innerText.includes('eco ' + t))",
        arg=tag,
        timeout=30_000,
    )
    page.wait_for_selector("#send-btn[data-state=idle]", timeout=30_000)


def start(page, text: str) -> None:
    """Manda `text` y vuelve apenas Vision empezo (sin esperar la respuesta)."""
    page.fill("#chat-input", text)
    page.click("#send-btn")
    page.wait_for_selector("#send-btn[data-state=active]", timeout=10_000)


def new_session(page, old_sid: str) -> str:
    page.evaluate("document.getElementById('acc-session').open = true")
    page.click("#session-new-btn")
    page.wait_for_function("old => document.getElementById('meta-session-id').textContent !== old", arg=old_sid)
    return page.evaluate("localStorage.getItem('vision.currentSessionId')")


def go_to(page, name: str, sid: str) -> None:
    """Toca la sesion `name` en la lista y espera a que quede activa y pintada."""
    page.evaluate("document.getElementById('acc-session').open = true")
    page.locator("#session-list li", has_text=name).first.click()
    page.wait_for_function("id => document.getElementById('meta-session-id').textContent === id", arg=sid)
    page.wait_for_timeout(800)  # historial, /status y lista ya pintados


def wait_turn_end(page, base_url: str, sid: str, timeout_s: float = 25) -> None:
    assert wait_until(page, lambda: not session_busy(base_url, sid), timeout_s), f"el turno de {sid} nunca termino"
    page.wait_for_timeout(700)  # los ultimos eventos del stream ya se procesaron en la pagina


def question_visible(page) -> bool:
    return page.evaluate(
        """() => { const p = document.getElementById('question-panel');
                   return !!p && !p.classList.contains('hidden') && p.getBoundingClientRect().height > 0; }"""
    )


def question_text(page) -> str:
    return page.evaluate("() => document.querySelector('#question-panel .q-text')?.textContent || ''")


def row(page, name: str):
    return page.locator("#session-list li", has_text=name).first


def mark_of(page, name: str) -> str | None:
    """El texto de la marca de la sesion `name` en la lista, o None si no tiene."""
    mark = row(page, name).locator(".session-question-mark")
    return mark.text_content() if mark.count() else None


# ---------------------------------------------------------------------------
# REQ-018: la pregunta de una sesion se ve y se contesta solo en esa sesion
# ---------------------------------------------------------------------------
def test_a_question_that_arrives_while_in_another_session_waits_for_its_own(app, base_url, provider):
    """El caso del usuario: mando una pregunta en A, se va a una sesion nueva B antes de que llegue, y la pregunta de A aparecia en B."""
    page = app.page
    sid_a = app.sid
    send(page, "[m1]", "hola")  # A ya figura en la lista de sesiones
    start(page, "hacé una pregunta [q1]")
    sid_b = new_session(page, sid_a)  # el usuario se va antes de que el modelo pregunte
    wait_turn_end(page, base_url, sid_a)
    assert any("Pregunta mostrada al usuario" in str(m.get("content")) for m in get_session_store().get(sid_a).messages), (
        "el modelo de A tiene que haber hecho la pregunta"
    )

    assert not question_visible(page), f"la pregunta de la sesion A aparecio en la sesion B: {question_text(page)!r}"
    assert QUESTION_TEXT not in page.inner_text("#messages")
    assert mark_of(page, "hola [m1]") == "❓ te hizo una pregunta", "A tiene que mostrar la marca en la lista"
    # B sigue normal: se puede usar y no hay nada de A.
    send(page, "[m2]", "hola")
    assert page.locator("#session-list li.active .session-question-mark").count() == 0, "en la sesion donde esta el usuario no va marca"
    assert mark_of(page, "hola [m2]") is None

    go_to(page, "hola [m1]", sid_a)
    assert question_visible(page), "al volver a A tiene que aparecer su pregunta"
    assert question_text(page) == QUESTION_TEXT
    assert mark_of(page, "hola [m1]") is None, "en la sesion donde esta el usuario no aparece la marca"

    page.locator("#question-panel:not(.hidden) .q-options button", has_text=OPTIONS[0]).click()
    page.wait_for_function(
        "() => [...document.querySelectorAll('.msg.assistant:not(.streaming)')].some(e => e.innerText.includes('eco [m?]'))",
        timeout=20_000,
    )
    wait_turn_end(page, base_url, sid_a)
    assert OPTIONS[0] in user_texts(sid_a), "la respuesta tiene que quedar en la sesion que pregunto"
    assert not [t for t in user_texts(sid_b) if OPTIONS[0] in t], "la respuesta de A termino en B"

    go_to(page, "hola [m2]", sid_b)
    assert mark_of(page, "hola [m1]") is None, "contestada la pregunta, la marca se va"
    assert not question_visible(page)


def test_leaving_a_session_with_its_question_on_screen_keeps_it_there(app, base_url, provider):
    page = app.page
    sid_a = app.sid
    send(page, "[m1]", "hola")
    start(page, "hacé una pregunta [q2]")
    page.wait_for_selector("#question-panel:not(.hidden) .q-options button", timeout=15_000)  # el usuario la ve, en A
    wait_turn_end(page, base_url, sid_a)
    received_before = len(provider.received)

    sid_c = new_session(page, sid_a)
    assert not question_visible(page), "al irse de A el panel se oculta"
    assert mark_of(page, "hola [m1]") == "❓ te hizo una pregunta"
    # Una opcion del panel oculto que se dispara igual (teclado, un script): no manda nada, ni a la sesion activa ni a A.
    clicked = page.evaluate(
        "() => { const b = document.querySelector('#question-panel .q-options button'); if (b) b.click(); return !!b; }"
    )
    page.wait_for_timeout(1_500)
    assert not [t for t in user_texts(sid_c) if OPTIONS[0] in t], "una opcion de la pregunta de A se mando a otra sesion"
    assert not [t for t in user_texts(sid_a) if OPTIONS[0] in t], "una opcion tocada fuera de A se mando igual"
    assert len(provider.received) == received_before, "le llego algo al modelo"
    assert page.get_attribute("#send-btn", "data-state") == "idle"
    assert page.locator(".msg.user").count() == 0, f"en la sesion nueva aparecio un mensaje (opcion disparada: {clicked})"

    go_to(page, "hola [m1]", sid_a)
    assert question_visible(page) and question_text(page) == QUESTION_TEXT, "al volver a A la pregunta sigue ahi"
    # Mandar otro mensaje en A tambien la da por contestada.
    send(page, "[m3]", "mejor otra cosa")
    assert not question_visible(page)
    new_session(page, sid_a)
    assert mark_of(page, "hola [m1]") is None, "otro mensaje en A tambien saca la marca"


# ---------------------------------------------------------------------------
# REQ-018: lo demas que pinta en pantalla (graficos)
# ---------------------------------------------------------------------------
def chart_shown(page) -> bool:
    return page.evaluate(
        "t => !document.getElementById('dashboard-overlay').classList.contains('hidden')"
        " && document.getElementById('dashboard-title').textContent === t",
        CHART_TITLE,
    )


def test_a_chart_from_another_session_does_not_open_in_this_one(app, base_url):
    page = app.page
    sid_a = app.sid
    start(page, "hacé un grafico [g1]")
    new_session(page, sid_a)
    wait_turn_end(page, base_url, sid_a)
    assert not chart_shown(page), "el grafico de la sesion A se abrio en la sesion B"

    # En la sesion activa el grafico se abre como siempre.
    sid_b = page.evaluate("localStorage.getItem('vision.currentSessionId')")
    start(page, "hacé un grafico [g2]")
    assert wait_until(page, lambda: chart_shown(page), 15), "el grafico de la sesion activa tiene que abrirse"
    wait_turn_end(page, base_url, sid_b)


# ---------------------------------------------------------------------------
# REQ-018: el loop autonomo
# ---------------------------------------------------------------------------
def test_a_question_the_loop_answered_alone_stays_in_its_session_and_leaves_no_mark(app, base_url):
    page = app.page
    sid_a = app.sid
    send(page, "[m1]", "hola")
    get_session_store().get_or_create(sid_a).loop_active = True  # el loop autonomo de A esta corriendo
    start(page, "hacé una pregunta recomendada [q3]")
    page.wait_for_selector("#question-panel.auto-answered:not(.hidden)", timeout=15_000)  # en SU sesion, como siempre
    assert "ya eligió esta opción sola" in page.inner_text("#question-panel")
    wait_turn_end(page, base_url, sid_a)

    get_session_store().get(sid_a).loop_active = True
    start(page, "otra pregunta recomendada [q4]")
    sid_b = new_session(page, sid_a)
    wait_turn_end(page, base_url, sid_a)
    assert not question_visible(page), "la pregunta que el loop de A contesto solo aparecio en B"
    assert mark_of(page, "hola [m1]") is None, "lo que el loop ya contesto no deja nada pendiente"
    assert sid_b != sid_a
    go_to(page, "hola [m1]", sid_a)
    assert not question_visible(page), "una pregunta ya contestada por el loop no vuelve a aparecer"


def loop_banner_visible(page) -> bool:
    return page.evaluate("() => !document.getElementById('loop-banner').classList.contains('hidden')")


def test_a_loop_waiting_for_an_answer_in_another_session_does_not_show_its_banner_here(app, base_url):
    page = app.page
    sid_a = app.sid
    send(page, "[m1]", "hola")
    get_session_store().get_or_create(sid_a).loop_active = True
    start(page, "hacé una pregunta [q5]")  # sin «(Recomendada)»: el loop se pausa y pide ayuda
    new_session(page, sid_a)
    wait_turn_end(page, base_url, sid_a)
    assert get_session_store().get(sid_a).loop_paused_for_question, "el loop de A tiene que estar esperando la respuesta"
    assert not loop_banner_visible(page), "el banner «El loop necesita tu ayuda» de A aparecio en otra sesion"
    assert not question_visible(page)
    assert mark_of(page, "hola [m1]") == "❓ te hizo una pregunta"

    go_to(page, "hola [m1]", sid_a)
    assert loop_banner_visible(page), "en A tiene que verse que su loop espera la respuesta"
    assert question_visible(page) and question_text(page) == QUESTION_TEXT
    page.locator("#question-panel:not(.hidden) .q-options button", has_text=OPTIONS[1]).click()
    assert not loop_banner_visible(page)
    wait_turn_end(page, base_url, sid_a)
    assert OPTIONS[1] in user_texts(sid_a)


# ---------------------------------------------------------------------------
# REQ-018: F5 recupera la pregunta pendiente de la sesion abierta (SQLite)
# ---------------------------------------------------------------------------
def reload(app) -> None:
    app.page.reload(wait_until="domcontentloaded")
    app.wait_ready()


def test_f5_brings_back_the_unanswered_question_of_the_open_session_from_sqlite(app, base_url, tmp_path: Path):
    page = app.page
    sid_a = app.sid
    folder = tmp_path / "proyecto"
    folder.mkdir()
    api(base_url, "POST", f"/api/sessions/{sid_a}/project-path", {"path": str(folder), "title": "Sesion A"})
    reload(app)
    start(page, "hacé una pregunta [q6]")
    page.wait_for_selector("#question-panel:not(.hidden) .q-options button", timeout=15_000)
    wait_turn_end(page, base_url, sid_a)
    writer.flush()

    reload(app)
    assert wait_until(page, lambda: question_visible(page), 10), "tras F5 la pregunta sin contestar de la sesion abierta no volvio"
    assert question_text(page) == QUESTION_TEXT
    page.locator("#question-panel:not(.hidden) .q-options button", has_text=OPTIONS[0]).click()
    wait_turn_end(page, base_url, sid_a)
    writer.flush()

    reload(app)
    page.wait_for_function("() => document.querySelector('#messages').innerText.includes('eco [m?]')", timeout=15_000)
    page.wait_for_timeout(1_500)
    assert not question_visible(page), "una pregunta ya contestada no vuelve con F5"
