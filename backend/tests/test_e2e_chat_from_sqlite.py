"""REQ-016 y REQ-017 (spec fix-detener-y-progreso-visible, D2 del usuario: «el chat se lee siempre de SQLite»), en un Chromium REAL
contra el backend real en loopback y un proveedor de IA falso (nunca hay red ni login).

Lo que el usuario vio (spec, secciones C y D): el medidor «Caché local» marcaba 100 % (la copia en el navegador de TODAS las
conversaciones contra 300 000 caracteres) y le sugeria compactar, aunque el contexto real del modelo estaba en 15 %; F5 leia SOLO esa
copia (cambiar de sesion leia SQLite), la copia crecia sin limite y, con el navegador lleno, las preferencias dejaban de guardarse en
silencio. Contrato observable que se prueba (no la implementacion):

  * F5 pinta el chat de la sesion activa desde SQLite (mismo camino que cambiar de sesion), aunque el navegador tenga una copia vieja,
    vacia, rota o con otra forma; y deja de guardar copias de las sesiones CON carpeta.
  * Una sesion SIN carpeta (sus mensajes NO estan en SQLite: viven en la memoria del backend) conserva una copia chica y acotada
    (ultimos 50 mensajes, maximo 200 000 caracteres) que se usa al recargar, tambien si el backend no responde.
  * El medidor «Caché local» y el boton «Liberar caché» ya no existen.
  * Una migracion de UNA sola vez borra las copias que ya no hacen falta (sesiones con carpeta y copias huerfanas) y las claves de
    funciones eliminadas (Pomodoro); nunca toca preferencias, el indice de sesiones ni el token.
  * Si el navegador no deja guardar una preferencia, se avisa UNA vez en pantalla y la preferencia vale hasta recargar.
  * REQ-017: el resumen «Contexto» muestra el % real aunque el acordeon este cerrado (al cargar, al cambiar de sesion y al terminar
    cada turno).

Revision (spec fix-detener-y-progreso-visible, revision; hallazgos confirmados con tests temporales del revisor):
  * F1: la migracion unica no borra la copia de una sesion con carpeta de la que SQLite todavia no tiene mensajes (era lo unico que
    habia de esa charla).
  * F2: si el usuario cambia de sesion mientras la carga espera el historial, el turno que seguia corriendo en la sesion anterior no se
    pinta en la nueva (ni su texto, ni el boton en «Detener», ni la copia de la nueva).
  * F3: ir a otra sesion y volver mientras la carga espera el historial no lo pinta dos veces.
  * F5: si «Contexto» no se puede leer, el resumen no se queda con el % de la sesion anterior.
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.request
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.llm import loop as loop_module
from app.persistence import writer
from e2e_support import FakeWhisper, launch_chromium, open_app, serve_backend

pytestmark = pytest.mark.e2e

MODEL = "claude-sonnet-4-6"
COPY_PREFIX = "vision.session."
COPY_MAX_MESSAGES = 50
COPY_MAX_CHARS = 200_000
PREF_NOTICE_TEXT = "No se pudo guardar tu preferencia en este navegador"

# Opener SIN proxy: el backend de prueba esta en loopback. `urllib.request.urlopen` toma el proxy del sistema (en esta PC, el PAC de la VPN
# de la empresa) y a veces intenta salir por `proxy.empresa.com` incluso para 127.0.0.1: el guardian de red de los tests lo bloquea (intermitente,
# depende de la VPN; 30 fallos en la bateria completa del 2026-10-14 y 0 al correr los mismos archivos aparte). Mismo arreglo que test_e2e_voice_engines.py.
_LOOPBACK = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _chunk(content=None, finish=None):
    delta = SimpleNamespace(content=content, tool_calls=[])
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=finish)])


SLOW_TEXT = "texto de la sesion A"
SLOW_TEXT_S = 12.0  # lo que tarda en escribirse la respuesta de un mensaje con «[lento]»


class _SlowText:
    """Stream falso que escribe «texto de la sesion A N.» de a pedazos durante `seconds`. `close()` (lo que hace Vision al Detener)
    lo corta sin 'generator already executing' (el SDK real tambien se puede cerrar desde otro hilo)."""

    def __init__(self, seconds: float, pieces: int = 20):
        self._items = [_chunk("[[TIPO: conversacional]]")] + [_chunk(f"{SLOW_TEXT} {i}. ") for i in range(pieces)] + [_chunk(finish="stop")]
        self._pause = seconds / pieces
        self._i = 0
        self._closed = threading.Event()

    def __iter__(self):
        return self

    def __next__(self):
        if 0 < self._i < len(self._items) - 1 and self._closed.wait(self._pause):
            raise StopIteration
        if self._closed.is_set() or self._i >= len(self._items):
            raise StopIteration
        item = self._items[self._i]
        self._i += 1
        return item

    def close(self):
        self._closed.set()


def _last_user_text(messages) -> str:
    for m in reversed(messages or []):
        if m.get("role") == "user":
            c = m.get("content")
            if isinstance(c, list):
                c = " ".join(p.get("text", "") for p in c if isinstance(p, dict))
            return str(c or "")
    return ""


class FakeProvider:
    """Contesta «eco [mN]» (la marca del ultimo mensaje del usuario): asi se compara pantalla vs SQLite sin depender de la hora.
    Anota el ultimo texto de usuario que recibio cada pedido."""

    def __init__(self):
        self.whisper = FakeWhisper()
        self.audio = SimpleNamespace(transcriptions=self.whisper)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))
        self.received: list[str] = []

    def get(self, path, cast_to=object):
        return {"data": [{"id": MODEL, "type": "LLM", "aliases": [], "valid_args": [], "reasoning_levels": []}]}

    def _create(self, **kwargs):
        user_text = _last_user_text(kwargs.get("messages"))
        self.received.append(user_text)
        if "[lento]" in user_text:
            return _SlowText(SLOW_TEXT_S)
        found = re.findall(r"\[m\d+\]", user_text)
        tag = found[-1] if found else "[m?]"
        return iter([_chunk(f"[[TIPO: conversacional]]eco {tag}. Revisé la cobertura y está todo en orden ✅."), _chunk(finish="stop")])


@pytest.fixture(scope="module")
def provider():
    return FakeProvider()


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


def assign_folder(base_url: str, sid: str, folder: Path, title: str) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    api(base_url, "POST", f"/api/sessions/{sid}/project-path", {"path": str(folder), "title": title})


def chat_via_api(base_url: str, sid: str, text: str) -> None:
    """Un turno completo de la sesion `sid` sin pagina (POST /api/chat y su stream hasta el final), y SQLite al dia."""
    body = json.dumps({"session_id": sid, "message": text, "message_id": str(uuid.uuid4())}).encode()
    req = urllib.request.Request(base_url + "/api/chat", data=body, method="POST", headers={"Content-Type": "application/json"})
    with _LOOPBACK.open(req, timeout=60) as r:
        r.read()
    writer.flush()


def session_busy(base_url: str, sid: str) -> bool:
    return bool(api(base_url, "GET", f"/api/session/{sid}/status").get("busy"))


def wait_until(pred, timeout_s: float, page=None) -> bool:
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        if pred():
            return True
        if page is not None:
            page.wait_for_timeout(100)
        else:
            time.sleep(0.1)
    return False


def hold_first_history_request(page, sid: str) -> list:
    """Retiene el primer GET del historial de `sid` (simula una sesion grande o la PC lenta); se suelta con `held[0].continue_()`."""
    held: list = []

    def hold(route):
        if f"/api/sessions/{sid}/messages" in route.request.url and not held:
            held.append(route)
            return
        route.continue_()

    page.route("**/api/sessions/**", hold)
    return held


def new_session(page, old_sid: str) -> str:
    page.evaluate("document.getElementById('acc-session').open = true")
    page.click("#session-new-btn")
    page.wait_for_function("old => document.getElementById('meta-session-id').textContent !== old", arg=old_sid)
    return page.evaluate("localStorage.getItem('vision.currentSessionId')")


TAG = re.compile(r"\[(?:m\d+|viejo|copia\d*)\]")


def dom_tags(page) -> list[str]:
    """Burbujas de usuario/asistente en pantalla, resumidas: ['U[m1]', 'A[m1]']."""
    lines = page.evaluate(
        """() => [...document.querySelectorAll('#messages .msg')]
              .filter(e => e.classList.contains('user') || e.classList.contains('assistant'))
              .map(e => (e.classList.contains('user') ? 'U' : 'A') + e.innerText.replace(/\\s+/g, ' '))"""
    )
    out = []
    for line in lines:
        m = TAG.search(line)
        out.append(line[0] + (m.group(0) if m else "?"))
    return out


def wait_dom_tags(page, expected: list[str], timeout: int = 20_000) -> None:
    page.wait_for_function(
        """exp => {
          const got = [...document.querySelectorAll('#messages .msg')]
            .filter(e => e.classList.contains('user') || e.classList.contains('assistant'))
            .map(e => { const m = e.innerText.match(/\\[(?:m\\d+|viejo|copia\\d*)\\]/);
                        return (e.classList.contains('user') ? 'U' : 'A') + (m ? m[0] : '?'); });
          return JSON.stringify(got) === JSON.stringify(exp);
        }""",
        arg=expected,
        timeout=timeout,
    )


def send(page, tag: str, text: str = "hola", timeout: int = 60_000) -> None:
    page.fill("#chat-input", f"{text} {tag}")
    page.click("#send-btn")
    page.wait_for_function(
        "t => [...document.querySelectorAll('.msg.assistant:not(.streaming)')].some(e => e.innerText.includes('eco ' + t))",
        arg=tag,
        timeout=timeout,
    )
    page.wait_for_selector("#send-btn[data-state=idle]", timeout=timeout)
    writer.flush()  # el backend corre en este mismo proceso: SQLite ya tiene el turno completo


def reload(app) -> None:
    app.page.reload(wait_until="domcontentloaded")
    app.wait_ready()


def wait_settled_without_folder(app) -> None:
    """La carga ya consulto /status de la sesion (sin carpeta: el banner «sin carpeta de proyecto» aparece). Recien ahi la pagina
    decide que hacer con la copia del navegador: sembrar una copia antes seria pisada por ese primer guardado (historial vacio)."""
    app.page.wait_for_selector("#persistence-banner:not(.hidden)", timeout=20_000)


def ls_get(page, key: str):
    return page.evaluate("k => localStorage.getItem(k)", key)


def ls_set(page, key: str, value: str) -> None:
    page.evaluate("([k, v]) => localStorage.setItem(k, v)", [key, value])


def fake_history(n: int, text_chars: int, tag: str) -> list[dict]:
    """`n` mensajes alternados usuario/asistente de ~`text_chars` caracteres; el ultimo lleva `tag`."""
    out = []
    for i in range(n):
        role = "user" if i % 2 == 0 else "assistant"
        mark = tag if i == n - 1 else ""
        out.append({"role": role, "text": f"Mensaje {i} {mark} " + "cobertura del depósito " * (text_chars // 23), "time": 1_700_000_000_000 + i})
    return out


# ---------------------------------------------------------------------------
# REQ-016: F5 lee de SQLite
# ---------------------------------------------------------------------------
STALE_COPIES = {
    # Una conversacion VIEJA que ya no es la verdad (la verdad esta en SQLite).
    "vieja": json.dumps([
        {"role": "user", "text": "pregunta vieja [viejo]", "messageId": "viejo-1", "time": 1_700_000_000_000},
        {"role": "assistant", "text": "respuesta vieja [viejo]", "time": 1_700_000_001_000},
    ]),
    # «Liberar caché» dejaba esto: despues de F5 el chat se veia vacio aunque SQLite tenia todo (spec D).
    "vacia": "[]",
    "rota": "{esto no es json",
    # JSON valido pero sin forma de lista: antes rompia la pagina entera al cargar (chatHistory no era iterable).
    "otra_forma": json.dumps({"role": "user", "text": "objeto suelto [viejo]"}),
}


@pytest.mark.parametrize("kind", list(STALE_COPIES))
def test_f5_paints_the_chat_from_sqlite_whatever_the_browser_copy_says(app, base_url, tmp_path, kind):
    assign_folder(base_url, app.sid, tmp_path / "proyecto", "Sesion con carpeta")
    reload(app)
    send(app.page, "[m1]", "primer mensaje")
    ls_set(app.page, COPY_PREFIX + app.sid, STALE_COPIES[kind])

    reload(app)

    wait_dom_tags(app.page, ["U[m1]", "A[m1]"])
    assert "[viejo]" not in app.page.inner_text("#messages"), "se pinto la copia del navegador en vez de SQLite"
    assert ls_get(app.page, COPY_PREFIX + app.sid) is None, "la sesion tiene carpeta: no se guarda (ni se deja) una copia del chat"


def test_regenerating_an_answer_after_f5_resends_the_question(app, base_url, tmp_path, provider):
    """F5 pinta ahora desde SQLite (como cambiar de sesion), donde cada respuesta tiene su PROPIO id: ↻ (volver a generar) tiene
    que seguir reenviando la PREGUNTA de ese turno. Con el id de la respuesta tomaba la propia respuesta como pregunta."""
    assign_folder(base_url, app.sid, tmp_path / "proyecto", "Sesion con carpeta")
    reload(app)
    send(app.page, "[m8]", "pregunta para regenerar")
    reload(app)
    wait_dom_tags(app.page, ["U[m8]", "A[m8]"])
    provider.received.clear()

    app.page.locator(".msg.assistant .btn-reset").first.click()
    app.page.wait_for_function(
        "() => [...document.querySelectorAll('.msg.assistant:not(.streaming)')].some(e => e.innerText.includes('eco [m8]'))",
        timeout=30_000,
    )
    app.page.wait_for_selector("#send-btn[data-state=idle]", timeout=30_000)
    assert provider.received, "↻ no le pidio nada al modelo"
    assert "pregunta para regenerar [m8]" in provider.received[-1], f"↻ reenvio otra cosa: {provider.received[-1][:120]!r}"


def test_a_session_with_folder_is_never_copied_to_the_browser(app, base_url, tmp_path):
    assign_folder(base_url, app.sid, tmp_path / "proyecto", "Sesion con carpeta")
    reload(app)
    send(app.page, "[m2]", "segundo mensaje")
    app.page.wait_for_timeout(500)  # cualquier guardado diferido ya habria corrido

    copies = app.page.evaluate(
        "p => Object.keys(localStorage).filter(k => k.startsWith(p) && localStorage.getItem(k).includes('[m2]'))", COPY_PREFIX
    )
    assert copies == [], f"el chat de una sesion con carpeta no se copia al navegador: {copies}"


def test_a_session_without_folder_keeps_a_small_bounded_copy_that_survives_f5(app):
    # Sin carpeta, SQLite no tiene sus mensajes (viven en la memoria del backend): la copia chica es lo unico que hay para F5.
    sid = app.sid
    wait_settled_without_folder(app)
    ls_set(app.page, COPY_PREFIX + sid, json.dumps(fake_history(120, 3_000, "[copia]")))  # ~360 000 caracteres
    reload(app)
    app.page.wait_for_function("() => document.querySelector('#messages').innerText.includes('[copia]')", timeout=20_000)

    send(app.page, "[m3]", "mensaje sin carpeta")

    raw = ls_get(app.page, COPY_PREFIX + sid)
    assert raw is not None, "la sesion sin carpeta tiene que conservar su copia (no esta en SQLite)"
    copy = json.loads(raw)
    assert len(copy) <= COPY_MAX_MESSAGES, f"copia de {len(copy)} mensajes: el tope es {COPY_MAX_MESSAGES}"
    assert len(raw) <= COPY_MAX_CHARS, f"copia de {len(raw)} caracteres: el tope es {COPY_MAX_CHARS}"
    assert copy[-1]["role"] == "assistant" and "eco [m3]" in copy[-1]["text"], "la copia tiene que terminar con lo ultimo"

    reload(app)
    app.page.wait_for_function("() => document.querySelector('#messages').innerText.includes('eco [m3]')", timeout=20_000)

    # Red de seguridad: con el backend sin responder, se usa la copia y la pantalla no se rompe.
    app.page.route("**/api/sessions/*/messages", lambda route: route.abort("failed"))
    reload(app)
    app.page.wait_for_function("() => document.querySelector('#messages').innerText.includes('eco [m3]')", timeout=20_000)
    app.page.wait_for_selector("#chat-input:not([disabled])")


def user_bubble_id(page, tag: str):
    """El id de mensaje de la burbuja de usuario que dice `tag` (el que usan ↻/🗑 para pedirle algo al backend)."""
    return page.evaluate(
        "t => { const w = [...document.querySelectorAll('#messages .user-turn-wrap')].find(e => e.innerText.includes(t));"
        " return w ? w.dataset.messageId || null : null; }",
        tag,
    )


def test_messages_from_before_the_folder_survive_f5_with_their_ids(app, base_url, tmp_path):
    """Revision (REQ-016, seguridad de datos): la sesion chateo SIN carpeta (sus mensajes viven solo en la memoria del backend y en la
    copia chica del navegador), despues le dieron carpeta (lo que pide el banner) y siguio. F5 lee de SQLite y borra la copia en cuanto
    SQLite tiene mensajes de la sesion: lo de antes de la carpeta desaparecia de la pantalla (y con el proximo reinicio, del todo). Ahora
    se guarda en SQLite al asignar la carpeta, con los mismos ids de mensaje (↻ y 🗑 lo siguen encontrando)."""
    wait_settled_without_folder(app)
    send(app.page, "[m1]", "mensaje sin carpeta")
    id_before = user_bubble_id(app.page, "[m1]")
    assert id_before, "la burbuja enviada tiene que tener su id"
    assign_folder(base_url, app.sid, tmp_path / "proyecto", "Sesion")
    send(app.page, "[m2]", "mensaje con carpeta")

    reload(app)

    expected = ["U[m1]", "A[m1]", "U[m2]", "A[m2]"]
    try:
        wait_dom_tags(app.page, expected)
    except Exception:  # noqa: BLE001 -- la asercion de abajo muestra que quedo en pantalla
        pass
    assert dom_tags(app.page) == expected, f"tras F5 faltan los mensajes de antes de la carpeta (copia: {ls_get(app.page, COPY_PREFIX + app.sid)!r})"
    assert user_bubble_id(app.page, "[m1]") == id_before, "tras F5 el mensaje de antes de la carpeta tiene otro id: ↻/🗑 no lo encuentran"
    assert ls_get(app.page, COPY_PREFIX + app.sid) is None, "con todo en SQLite la copia del navegador ya no hace falta"


def test_switching_session_while_f5_waits_for_the_history_never_paints_the_other_turn(app, base_url):
    """Revision F2 (REQ-016 + REQ-018): F5 en la sesion A con un turno largo en curso; mientras la carga espera el historial de A
    (sesion grande o PC lenta) el usuario abre una sesion nueva B. Antes, al llegar el historial, la carga se enganchaba al turno de A
    como si fuera de B: el texto de A aparecia en el chat de B, se guardaba en la copia de B y el boton de B quedaba en «Detener»."""
    page = app.page
    sid_a = app.sid
    page.fill("#chat-input", "contame algo [lento]")
    page.click("#send-btn")
    page.wait_for_function("t => document.querySelector('#messages').innerText.includes(t)", arg=SLOW_TEXT, timeout=20_000)

    held = hold_first_history_request(page, sid_a)
    reload(app)
    assert wait_until(lambda: bool(held), 10, page), "la carga nunca pidio el historial de A"
    sid_b = new_session(page, sid_a)
    assert sid_b != sid_a
    page.wait_for_timeout(1_500)  # B ya esta activada (historial vacio, /status sin turno)
    assert session_busy(base_url, sid_a), "el turno de A tiene que seguir corriendo cuando se suelta su historial"

    held[0].continue_()
    painted = wait_until(lambda: SLOW_TEXT in page.inner_text("#messages"), 4, page)
    assert not painted, "el turno de la sesion A se pinto en la sesion B"
    # Termina el turno de A: B sigue limpia, con su boton en «Enviar» y sin nada de A en su copia.
    assert wait_until(lambda: not session_busy(base_url, sid_a), SLOW_TEXT_S + 10, page), "el turno de A nunca termino"
    page.wait_for_timeout(1_500)
    assert page.evaluate("localStorage.getItem('vision.currentSessionId')") == sid_b
    assert SLOW_TEXT not in page.inner_text("#messages")
    assert page.get_attribute("#send-btn", "data-state") == "idle", "B nunca tuvo un turno: su boton tiene que quedar en «Enviar»"
    assert SLOW_TEXT not in (ls_get(page, COPY_PREFIX + sid_b) or ""), "la respuesta de A se guardo en la copia de B"
    page.unroute("**/api/sessions/**")


def test_going_to_another_session_and_back_while_f5_waits_never_paints_the_history_twice(app, base_url, tmp_path):
    """Revision F3 (REQ-016): F5 en A; antes de que llegue su historial el usuario va a una sesion nueva y vuelve a A (que ya se pinta
    entera al volver). Antes, cuando llegaba la respuesta de la carga, el historial se volvia a insertar: todo quedaba dos veces."""
    page = app.page
    sid_a = app.sid
    assign_folder(base_url, sid_a, tmp_path / "proyecto_a", "Sesion A")
    reload(app)
    send(page, "[m1]", "hola")

    held = hold_first_history_request(page, sid_a)
    reload(app)
    assert wait_until(lambda: bool(held), 10, page), "la carga nunca pidio el historial de A"
    new_session(page, sid_a)
    page.wait_for_timeout(800)
    page.locator("#session-list li", has_text="Sesion A").first.click()
    page.wait_for_function("id => document.getElementById('meta-session-id').textContent === id", arg=sid_a)
    wait_dom_tags(page, ["U[m1]", "A[m1]"], timeout=10_000)

    held[0].continue_()
    page.wait_for_timeout(2_000)  # la respuesta retenida de la carga ya llego y se proceso
    assert dom_tags(page) == ["U[m1]", "A[m1]"], "el historial quedo pintado dos veces"
    page.unroute("**/api/sessions/**")


def test_the_local_cache_meter_and_its_button_are_gone(app):
    for selector in ("#cache-pct", "#cache-fill", "#cache-clear-btn", ".cache-meter"):
        assert app.page.locator(selector).count() == 0, f"{selector} sigue en la pagina"
    sidebar = app.page.inner_text("#sidebar")
    assert "Caché local" not in sidebar and "Liberar caché" not in sidebar


# ---------------------------------------------------------------------------
# REQ-016: migracion de una sola vez
# ---------------------------------------------------------------------------
MIGRATION_MARKER = "vision.migrations.chatCopiesV1"
PRESERVED = {
    "vision.theme": "dark-blue",
    "vision.tts.enabled": "true",
    "vision.brain.autosendEnabled": "false",
    "vision.brain.autosendPauseMs": "5",
    "vision.stt.language": "es",
    "vision.loop.alarmType": "chime",
    "vision.accessToken": "valor-de-prueba-e2e",  # inventado: solo prueba que el token nunca se borra
    "otra.app.clave": "no es de Vision",
}


def test_one_time_migration_drops_unneeded_copies_and_keeps_preferences(app, base_url, tmp_path):
    folder_sid, orphan_sid, nofolder_sid = (str(uuid.uuid4()) for _ in range(3))
    assign_folder(base_url, folder_sid, tmp_path / "proyecto", "Sesion con carpeta")
    # Revision F1: la copia sobra solo si SQLite de verdad tiene la charla de esa sesion (ver el test de abajo).
    chat_via_api(base_url, folder_sid, "hola [m5]")
    assert api(base_url, "GET", f"/api/sessions/{folder_sid}/messages")["messages"], "SQLite tiene que tener la charla de la sesion"
    index = {
        folder_sid: {"name": "Sesion con carpeta", "updatedAt": 1_700_000_000_000},
        nofolder_sid: {"name": "Sesion sin carpeta", "updatedAt": 1_700_000_100_000},
    }
    seed = {
        COPY_PREFIX + folder_sid: json.dumps(fake_history(4, 100, "[viejo]")),  # ya esta en SQLite
        COPY_PREFIX + orphan_sid: json.dumps(fake_history(4, 100, "[viejo]")),  # sin carpeta y fuera del indice: nadie la abre
        COPY_PREFIX + nofolder_sid: json.dumps(fake_history(300, 1_500, "[copia]")),  # ~450 000 caracteres
        "vision.pomodoro.workMin": "25",  # funcion eliminada (spec configuracion-limpieza-credenciales-servidor REQ-101)
        "vision.pomodoro.restMin": "5",
        "vision.sessionsIndex": json.dumps(index),
        **PRESERVED,
    }
    wait_settled_without_folder(app)
    app.page.evaluate("s => { for (const [k, v] of Object.entries(s)) localStorage.setItem(k, v); }", seed)
    # Lo que tenia el navegador con la version anterior: todavia sin la marca (la primera carga de esta pestaña ya la puso).
    app.page.evaluate("k => localStorage.removeItem(k)", MIGRATION_MARKER)
    index_before = ls_get(app.page, "vision.sessionsIndex")

    reload(app)
    app.page.wait_for_function("k => localStorage.getItem(k) !== null", arg=MIGRATION_MARKER, timeout=20_000)

    left = app.page.evaluate("() => Object.fromEntries(Object.keys(localStorage).map(k => [k, localStorage.getItem(k)]))")
    assert COPY_PREFIX + folder_sid not in left, "la copia de una sesion con carpeta sobra: su chat esta en SQLite"
    assert COPY_PREFIX + orphan_sid not in left, "una copia huerfana (sin carpeta y fuera del indice) no la puede abrir nadie"
    assert not [k for k in left if k.startswith("vision.pomodoro.")], "claves de Pomodoro (funcion eliminada) siguen ahi"
    kept = json.loads(left[COPY_PREFIX + nofolder_sid])
    assert len(kept) <= COPY_MAX_MESSAGES and len(left[COPY_PREFIX + nofolder_sid]) <= COPY_MAX_CHARS
    assert "[copia]" in kept[-1]["text"], "se conservan los ULTIMOS mensajes de la sesion sin carpeta"
    for key, value in PRESERVED.items():
        assert left.get(key) == value, f"la migracion toco {key}"
    # El indice sigue con todas sus sesiones (la lista del backend puede actualizar la fecha/carpeta de una: eso es de antes).
    index_after = json.loads(left["vision.sessionsIndex"])
    for sid, info in json.loads(index_before).items():
        assert index_after.get(sid, {}).get("name") == info["name"], f"la migracion toco el indice de sesiones ({sid})"
    assert left.get("vision.currentSessionId") == app.sid

    # Una sola vez: lo que aparezca despues no lo vuelve a barrer.
    ls_set(app.page, "vision.pomodoro.workMin", "30")
    ls_set(app.page, COPY_PREFIX + orphan_sid, "[]")
    reload(app)
    app.page.wait_for_timeout(3_000)  # carga + historial + listado de sesiones: la migracion ya habria corrido
    assert ls_get(app.page, "vision.pomodoro.workMin") == "30"
    assert ls_get(app.page, COPY_PREFIX + orphan_sid) == "[]"


def test_the_one_time_migration_keeps_the_copy_of_a_folder_session_sqlite_has_nothing_of(app, base_url, tmp_path):
    """Revision F1 (REQ-016, seguridad de datos): la sesion X chateo SIN carpeta con la version anterior (su charla vive solo en la copia
    del navegador), despues se le asigno carpeta y no se volvio a escribir: SQLite no tiene nada de X. La migracion borraba la copia de
    toda sesion con carpeta y la charla desaparecia en el F5 siguiente. Ahora la conserva (acotada) mientras SQLite no tenga mensajes."""
    page = app.page
    wait_settled_without_folder(app)
    x = str(uuid.uuid4())
    assign_folder(base_url, x, tmp_path / "proyecto_x", "Sesion X")
    assert api(base_url, "GET", f"/api/sessions/{x}/messages")["messages"] == [], "SQLite no tiene mensajes de X"
    copy = [
        {"role": "user", "text": "pregunta [copia]", "messageId": "u1", "time": 1_700_000_000_000},
        {"role": "assistant", "text": "respuesta [copia]", "messageId": "u1", "time": 1_700_000_001_000},
    ]
    ls_set(page, COPY_PREFIX + x, json.dumps(copy))
    index = json.loads(ls_get(page, "vision.sessionsIndex") or "{}")
    index[x] = {"name": "Sesion X", "updatedAt": 1_700_000_000_000}
    ls_set(page, "vision.sessionsIndex", json.dumps(index))
    ls_set(page, "vision.currentSessionId", x)
    page.evaluate("k => localStorage.removeItem(k)", MIGRATION_MARKER)  # el navegador todavia tenia la version anterior

    reload(app)
    page.wait_for_function("k => localStorage.getItem(k) !== null", arg=MIGRATION_MARKER, timeout=20_000)
    page.wait_for_function("() => document.querySelector('#messages').innerText.includes('respuesta [copia]')", timeout=20_000)
    kept = ls_get(page, COPY_PREFIX + x)
    assert kept is not None and "respuesta [copia]" in kept, "la migracion borro la unica copia de la charla de X"

    reload(app)  # el F5 siguiente la sigue mostrando
    page.wait_for_function("() => document.querySelector('#messages').innerText.includes('respuesta [copia]')", timeout=20_000)
    wait_dom_tags(page, ["U[copia]", "A[copia]"])


# ---------------------------------------------------------------------------
# REQ-016: una preferencia que no se puede guardar se avisa una vez
# ---------------------------------------------------------------------------
FAIL_VISION_WRITES_JS = """() => {
  const orig = Storage.prototype.setItem;
  Storage.prototype.setItem = function (k, v) {
    if (String(k).startsWith('vision.')) throw new DOMException('lleno', 'QuotaExceededError');
    return orig.call(this, k, v);
  };
}"""


def notice_state(page) -> dict:
    return page.evaluate(
        """() => {
          const el = document.getElementById('pref-save-notice');
          if (!el) return { exists: false };
          const r = el.getBoundingClientRect();
          const top = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
          return { exists: true, visible: !el.classList.contains('hidden') && r.width > 0 && r.height > 0,
                   onTop: !!top && el.contains(top), text: el.innerText };
        }"""
    )


def test_a_preference_that_cannot_be_saved_is_announced_once_and_holds_until_reload(app):
    app.page.evaluate(FAIL_VISION_WRITES_JS)

    # Lo que NO es una preferencia (copia del chat, indice de sesiones) no dispara el aviso.
    send(app.page, "[m6]", "con el navegador lleno")
    assert not notice_state(app.page).get("visible"), "un mensaje del chat no es una preferencia"

    app.page.click("#settings-btn")
    app.page.click('.settings-tab[data-tab="appearance"]')
    app.page.select_option("#cfg-theme-select", "dark-blue")
    app.page.wait_for_function("() => !document.getElementById('pref-save-notice')?.classList.contains('hidden')", timeout=5_000)
    state = notice_state(app.page)
    assert state["visible"] and state["onTop"], f"el aviso tiene que verse encima de Configuracion: {state}"
    assert PREF_NOTICE_TEXT in state["text"] and "hasta que recargues" in state["text"], state["text"]
    assert app.page.evaluate("document.documentElement.dataset.theme") == "dark-blue", "el tema elegido rige igual"

    app.page.click("#pref-save-notice-close")
    assert not notice_state(app.page)["visible"]

    # Otra preferencia que tampoco se guarda: el aviso ya se dio (una sola vez) y la preferencia vale hasta recargar.
    app.page.click('.settings-tab[data-tab="audio"]')
    app.page.check("#cfg-tts-enabled")
    app.page.wait_for_timeout(500)
    assert not notice_state(app.page)["visible"], "el aviso es UNA sola vez por carga"
    app.page.click("#settings-close")
    app.page.click("#settings-btn")
    app.page.click('.settings-tab[data-tab="audio"]')
    assert app.page.is_checked("#cfg-tts-enabled"), "«Leer en voz alta» tiene que seguir activo hasta recargar"
    app.page.click('.settings-tab[data-tab="appearance"]')
    assert app.page.input_value("#cfg-theme-select") == "dark-blue"
    app.page.click("#settings-close")


# ---------------------------------------------------------------------------
# REQ-017: «Contexto» con el acordeon cerrado
# ---------------------------------------------------------------------------
def context_summary(page) -> str:
    return page.text_content("#context-summary").strip()


def test_context_summary_shows_the_real_percentage_with_the_accordion_closed(app):
    assert app.page.evaluate("document.getElementById('acc-context').open") is False
    app.page.wait_for_function(
        "() => /^\\d+%$/.test(document.getElementById('context-summary').textContent.trim())", timeout=15_000
    )

    # Un mensaje grande (~40 000 tokens estimados): al terminar el turno el % sube, sin abrir el acordeon.
    send(app.page, "[m7]", "analizá esto: " + "pieza 1234-ABC con 3,5 días de cobertura en el CDM. " * 3_000)
    app.page.wait_for_function(
        "() => parseInt(document.getElementById('context-summary').textContent, 10) > 0", timeout=15_000
    )
    assert app.page.evaluate("document.getElementById('acc-context').open") is False
    busy_pct = int(context_summary(app.page).rstrip("%"))

    # Al cambiar a una sesion nueva (vacia) el resumen muestra la de ESA sesion.
    app.page.evaluate("document.getElementById('acc-session').open = true")
    app.page.click("#session-new-btn")
    app.page.wait_for_function("old => document.getElementById('meta-session-id').textContent !== old", arg=app.sid)
    app.page.wait_for_function("() => document.getElementById('context-summary').textContent.trim() === '0%'", timeout=15_000)
    assert busy_pct > 0


def test_context_summary_does_not_keep_the_previous_session_pct_when_it_cannot_be_read(app):
    """Revision F5 (REQ-017): con el acordeon cerrado, «Contexto» es el unico medidor que se ve siempre. Si /api/context falla (503, sin
    red, Vision reiniciandose) al cambiar a una sesion nueva, el resumen y la barra se quedaban con el % de la sesion ANTERIOR."""
    page = app.page
    send(page, "[m9]", "analizá esto: " + "pieza 1234-ABC con 3,5 días de cobertura en el CDM. " * 3_000)
    page.wait_for_function("() => parseInt(document.getElementById('context-summary').textContent, 10) > 0", timeout=15_000)
    pct_a = context_summary(page)

    page.route("**/api/context/**", lambda route: route.fulfill(status=503, body="{}", content_type="application/json"))
    new_session(page, app.sid)
    page.wait_for_function("() => document.getElementById('context-detail').textContent.includes('No se pudo')", timeout=10_000)
    assert page.evaluate("document.getElementById('acc-context').open") is False
    shown = context_summary(page)
    assert shown != pct_a, f"el resumen de la sesion nueva sigue mostrando el {pct_a} de la anterior"
    assert shown == "—", shown
    bar = page.evaluate(
        "() => { const f = document.getElementById('context-fill'); return { width: f.style.width, cls: [...f.classList] }; }"
    )
    assert bar["width"] in ("0%", "0px", ""), bar
    assert "warn" not in bar["cls"] and "critical" not in bar["cls"], bar
    page.unroute("**/api/context/**")
