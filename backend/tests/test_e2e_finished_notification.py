"""REQ-001..007 (spec aviso-proceso-terminado, decisiones D1-D4 del usuario 2026-10-13), en un Chromium REAL contra el backend real en
loopback y un proveedor de IA falso (nunca hay red ni login).

Pedido del usuario: «generar en vision una notificacion abajo a la derecha cuando terminó un proceso solicitado y tenga todas las tareas
en completed». D1 = solo notificacion de Windows (la API `Notification` del navegador; Windows la muestra abajo a la derecha). D2 = en
el mismo momento en que Vision termina el turno (`done`), si la lista de tareas de ESE turno tiene al menos una y TODAS estan completed.
D3 = el sonido es el de Windows (Vision no agrega otro).

`window.Notification` se reemplaza por una clase FALSA antes de que cargue la app: anota cada aviso (titulo y opciones) en
`window.__notifs`, el permiso se elige por test y `requestPermission` queda espiado (con la marca de si lo pidio un clic del usuario).
Asi se prueba lo que Vision le pide a Windows sin depender del permiso real del navegador ni de «No molestar».

Contrato observable que se prueba (no la implementacion):
  * todas las tareas completed -> UNA notificacion: arriba «<sesion> · HH:MM» (hora del fin), abajo el comienzo de la respuesta en texto
    plano y «N de N tareas»; queda hasta que se la toque (requireInteraction), con el id del turno como `tag` y sin `silent` (suena el
    sonido de Windows);
  * con una tarea sin terminar, con Detener, con un error del modelo o sin lista de tareas en el turno -> ninguna;
  * un turno de OTRA sesion (de fondo) avisa con el nombre de SU sesion y tocar el aviso abre esa sesion (y cierra el aviso);
  * una sola vez por turno: aunque haya dos pestañas mirando la misma sesion, aunque el mismo `done` llegue dos veces o se recargue;
  * el interruptor «Avisarme cuando termine» (Configuracion > Audio, prendido de fabrica) apagado -> ninguna, y lo apagado sobrevive a F5;
  * permiso sin decidir -> se pide desde un clic (Enviar o el interruptor), nunca solo; negado -> Vision lo explica UNA vez y como
    habilitarlo; sin la API en el navegador -> nada se rompe;
  * HTML y markdown de la respuesta llegan al aviso como texto plano; una respuesta larga se corta en una palabra con «…».
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.request
from types import SimpleNamespace

import pytest

from app.llm import loop as loop_module
from app.tools import get_registry
from app.tools.base import Tool
from e2e_support import FakeWhisper, launch_chromium, open_app, serve_backend

pytestmark = pytest.mark.e2e

MODEL = "claude-sonnet-4-6"
ANSWER = "Listo: revisé los tres depósitos y el inventario cierra sin diferencias en las zonas que pediste."
LONG_ANSWER = (
    "Terminé la conciliación completa del inventario del CDM: revisé las 3 zonas de picking, comparé cada pallet contra el "
    "sistema, corregí las ubicaciones que estaban cruzadas entre los pasillos 4 y 5, y dejé anotadas las piezas que faltan "
    "reponer mañana a primera hora para que el turno siguiente arranque sin demoras."
)
HTML_ANSWER = (
    "# Resumen del cierre\n"
    "**Importante:** el <b>stock</b> de `NRC` quedó <i>completo</i>.\n"
    "- Ver [el informe](http://example.invalid/informe) para el detalle."
)
TODOS_DONE = [
    {"content": "Revisar depósito Motores", "status": "completed", "priority": "high"},
    {"content": "Revisar depósito Montaje", "status": "completed", "priority": "medium"},
    {"content": "Cerrar el inventario", "status": "completed", "priority": "medium"},
]
TODOS_ONE_PENDING = [
    {"content": "Revisar depósito Motores", "status": "completed", "priority": "high"},
    {"content": "Revisar depósito Montaje", "status": "completed", "priority": "medium"},
    {"content": "Cerrar el inventario", "status": "pending", "priority": "medium"},
]
DENIED_FRAGMENTS = ("candado", "Permisos del sitio", "Notificaciones", "Permitir")
LOCK_SCREEN_LINE = "Windows puede mostrar este aviso también en la pantalla bloqueada."

# Opener SIN proxy: el backend de prueba esta en loopback. `urllib.request.urlopen` toma el proxy del sistema (en esta PC, el PAC de la VPN
# de la empresa) y a veces intenta salir por `proxy.empresa.com` incluso para 127.0.0.1: el guardian de red de los tests lo bloquea (intermitente,
# depende de la VPN; 30 fallos en la bateria completa del 2026-10-14 y 0 al correr los mismos archivos aparte). Mismo arreglo que test_e2e_voice_engines.py.
_LOOPBACK = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _chunk(content=None, tool_calls=None, finish=None):
    delta = SimpleNamespace(content=content, tool_calls=tool_calls or [])
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=finish)])


def _reply(text: str):
    return iter([_chunk(f"[[TIPO: conversacional]]{text}"), _chunk(finish="stop")])


def _calls(calls: list[tuple[str, str, dict]], wait=None):
    """Generador: (opcional) espera `wait()` y despues el modelo pide esas herramientas. El loop lo consume en un hilo."""
    if wait is not None:
        wait()
    yield _chunk(
        tool_calls=[
            SimpleNamespace(index=i, id=call_id, function=SimpleNamespace(name=name, arguments=json.dumps(args)))
            for i, (call_id, name, args) in enumerate(calls)
        ]
    )
    yield _chunk(finish="tool_calls")


def _text(content) -> str:
    if isinstance(content, list):
        return " ".join(p.get("text", "") for p in content if isinstance(p, dict))
    return str(content or "")


class Provider:
    """El modelo falso arma la lista de tareas con `todowrite` (todas completed salvo que el mensaje diga «pendiente») y despues
    contesta. Palabras del mensaje del usuario: «sin tareas» = contesta directo, sin lista; «detené» = ademas pide una herramienta que
    no termina sola (para apretar Detener); «falla» = el segundo paso del modelo falla; «html» / «largo» = esa respuesta; «esperá» = no
    arma la lista hasta que el test abre `gate`."""

    def __init__(self):
        self.whisper = FakeWhisper()
        self.audio = SimpleNamespace(transcriptions=self.whisper)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))
        self.gate = threading.Event()
        self.gate.set()

    def get(self, path, cast_to=object):
        return {"data": [{"id": MODEL, "type": "LLM", "aliases": [], "valid_args": [], "reasoning_levels": []}]}

    def _create(self, **kwargs):
        messages = kwargs.get("messages") or []
        last_user = max(i for i, m in enumerate(messages) if m.get("role") == "user")
        text = _text(messages[last_user].get("content"))
        tools_after = sum(1 for m in messages[last_user:] if m.get("role") == "tool")
        if tools_after == 0:
            if "sin tareas" in text:
                return _reply(ANSWER)
            todos = TODOS_ONE_PENDING if "pendiente" in text else TODOS_DONE
            calls = [("call_todo", "todowrite", {"todos": todos})]
            if "detené" in text:
                calls.append(("call_hold", "hold_tool", {}))
            wait = (lambda: self.gate.wait(30)) if "esperá" in text else None
            return _calls(calls, wait)
        if "falla" in text:
            raise RuntimeError("el modelo de prueba se cayó a mitad del turno")
        if "html" in text:
            return _reply(HTML_ANSWER)
        if "largo" in text:
            return _reply(LONG_ANSWER)
        return _reply(ANSWER)


@pytest.fixture(scope="module")
def hold_release():
    """Herramienta que no termina sola: el turno sigue ocupado hasta que el usuario aprieta Detener."""
    release = threading.Event()

    def execute(args, session):
        release.wait(30)
        return "listo"

    registry = get_registry()
    registry.register(
        Tool(name="hold_tool", description="herramienta lenta de prueba", parameters={"type": "object", "properties": {}}, execute=execute)
    )
    yield release
    release.set()
    registry._tools.pop("hold_tool", None)


@pytest.fixture(scope="module")
def provider():
    return Provider()


@pytest.fixture(scope="module")
def base_url(provider, hold_release):
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(loop_module, "get_client", lambda *a, **k: provider)
        with serve_backend(provider) as url:
            yield url


@pytest.fixture(scope="module")
def browser(tmp_path_factory, base_url):
    with launch_chromium(tmp_path_factory.mktemp("audio"), base_url) as b:
        yield b


def fake_notification_script(permission: str = "granted", answer: str = "granted", missing: bool = False) -> str:
    """`window.Notification` FALSO (se inyecta antes de app.js). `permission` = el permiso de entrada; `answer` = lo que elige el
    usuario si Vision lo pide; `missing` = un navegador sin la API."""
    cfg = json.dumps({"permission": permission, "answer": answer, "missing": missing})
    return f"""
(() => {{
  const cfg = {cfg};
  window.__notifs = [];
  window.__permRequests = [];
  window.__focusCalls = 0;
  const origFocus = window.focus.bind(window);
  window.focus = () => {{ window.__focusCalls += 1; return origFocus(); }};
  if (cfg.missing) {{
    delete window.Notification;
    return;
  }}
  let permission = cfg.permission;
  class FakeNotification {{
    constructor(title, options = {{}}) {{
      this.title = title;
      this.options = {{ ...options }};
      this.closed = false;
      this.onclick = null;
      window.__notifs.push(this);
    }}
    static get permission() {{ return permission; }}
    static requestPermission(callback) {{
      window.__permRequests.push({{ userActivation: !!(navigator.userActivation && navigator.userActivation.isActive) }});
      permission = cfg.answer;
      if (typeof callback === "function") callback(permission);
      return Promise.resolve(permission);
    }}
    close() {{ this.closed = true; }}
  }}
  window.Notification = FakeNotification;
}})();
"""


def open_with(browser, base_url, **notif):
    """Pestaña nueva (storage limpio) con el Notification falso y la app lista."""
    cm = open_app(browser, base_url)
    a = cm.__enter__()
    a.page.add_init_script(fake_notification_script(**notif))
    a.open()
    a.sid = a.page.evaluate("localStorage.getItem('vision.currentSessionId')")
    return cm, a


@pytest.fixture
def app(browser, base_url, provider):
    provider.gate.set()
    cm, a = open_with(browser, base_url)
    try:
        yield a
    finally:
        cm.__exit__(None, None, None)


@pytest.fixture
def app_factory(browser, base_url, provider):
    """Para los tests que eligen el permiso (o un navegador sin la API)."""
    provider.gate.set()
    opened = []

    def make(**notif):
        cm, a = open_with(browser, base_url, **notif)
        opened.append(cm)
        return a

    yield make
    for cm in reversed(opened):
        cm.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# Ayudantes
# ---------------------------------------------------------------------------
def api(base_url: str, path: str) -> dict:
    with _LOOPBACK.open(base_url + path, timeout=30) as r:
        return json.loads(r.read().decode() or "{}")


def wait_until(page, pred, timeout_s: float) -> bool:
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        if pred():
            return True
        page.wait_for_timeout(100)
    return False


def wait_turn_end(page, base_url: str, sid: str, timeout_s: float = 30) -> None:
    busy = lambda: bool(api(base_url, f"/api/session/{sid}/status").get("busy"))  # noqa: E731
    assert wait_until(page, lambda: not busy(), timeout_s), f"el turno de {sid} nunca termino"
    page.wait_for_timeout(900)  # los ultimos eventos del stream ya se procesaron en la pagina (y el aviso, que es asincrono)


def start(page, text: str) -> None:
    """Manda `text` desde el compositor (clic en Enviar) y vuelve apenas Vision empezo."""
    page.fill("#chat-input", text)
    page.click("#send-btn")
    page.wait_for_selector("#send-btn[data-state=active]", timeout=10_000)


def run_turn(app, base_url: str, text: str) -> None:
    start(app.page, text)
    wait_turn_end(app.page, base_url, app.sid)
    app.page.wait_for_selector("#send-btn[data-state=idle]", timeout=15_000)


def notifs(page) -> list[dict]:
    return page.evaluate(
        """() => window.__notifs.map(n => ({
             title: n.title, body: n.options.body, tag: n.options.tag, icon: n.options.icon,
             requireInteraction: n.options.requireInteraction, silent: n.options.silent, closed: n.closed,
           }))"""
    )


def permission_requests(page) -> list[dict]:
    return page.evaluate("window.__permRequests")


def user_turn_ids(page) -> list[str]:
    return page.evaluate("[...document.querySelectorAll('.user-turn-wrap[data-message-id]')].map(e => e.dataset.messageId)")


def active_session_name(page) -> str:
    page.evaluate("document.getElementById('acc-session').open = true")
    return page.inner_text("#session-list li.active .session-name").strip()


def minutes_now(page) -> int:
    return page.evaluate("(() => { const d = new Date(); return d.getHours() * 60 + d.getMinutes(); })()")


def assert_title(page, title: str, name: str) -> None:
    match = re.fullmatch(r"(.+) · (\d\d):(\d\d)", title)
    assert match, f"el titulo tiene que ser «<sesion> · HH:MM»: {title!r}"
    assert match.group(1) == name, f"arriba va el nombre de la sesion ({name!r}): {title!r}"
    got = int(match.group(2)) * 60 + int(match.group(3))
    assert (minutes_now(page) - got) % 1440 in (0, 1), f"la hora tiene que ser la del fin del turno: {title!r}"


def denied_notice(page) -> dict:
    return page.evaluate(
        """() => { const el = document.getElementById('notify-denied-notice');
                   return { visible: !!el && !el.classList.contains('hidden'), text: el ? el.textContent : '' }; }"""
    )


def new_session(page, old_sid: str) -> str:
    page.evaluate("document.getElementById('acc-session').open = true")
    page.click("#session-new-btn")
    page.wait_for_function("old => document.getElementById('meta-session-id').textContent !== old", arg=old_sid)
    return page.evaluate("localStorage.getItem('vision.currentSessionId')")


def open_audio_settings(page) -> None:
    page.click("#settings-btn")
    page.click('.settings-tab[data-tab="audio"]')
    page.wait_for_selector('.settings-pane[data-pane="audio"]:not(.hidden)')


def close_settings(page) -> None:
    page.click("#settings-close")
    page.wait_for_selector("#settings-overlay", state="hidden")


# ---------------------------------------------------------------------------
# REQ-001 / REQ-002 / REQ-004: todas completed -> una notificacion de Windows
# ---------------------------------------------------------------------------
def test_all_tasks_completed_shows_one_windows_notification_with_session_time_answer_and_count(app, base_url):
    text = "cerrá el inventario del CDM"
    run_turn(app, base_url, text)

    shown = notifs(app.page)
    assert len(shown) == 1, f"tiene que haber exactamente un aviso: {shown}"
    notice = shown[0]
    assert_title(app.page, notice["title"], active_session_name(app.page))
    assert notice["body"] == f"{ANSWER}\n3 de 3 tareas", notice["body"]
    assert notice["tag"] == user_turn_ids(app.page)[-1], "el `tag` es el id del turno (Windows reemplaza los del mismo turno)"
    assert notice["requireInteraction"] is True, "queda hasta que se la toque o se la cierre"
    assert not notice["silent"], "REQ-004: suena el sonido de notificacion de Windows (sin `silent`)"
    assert notice["icon"] and notice["icon"].startswith("http"), notice["icon"]
    assert not notice["closed"]
    assert permission_requests(app.page) == [], "con el permiso ya concedido no se vuelve a pedir"


# ---------------------------------------------------------------------------
# REQ-001: cuando NO se dispara
# ---------------------------------------------------------------------------
def test_a_task_still_pending_means_no_notification(app, base_url):
    run_turn(app, base_url, "cerrá el inventario, una queda pendiente")
    assert notifs(app.page) == []


def test_a_turn_without_its_own_task_list_does_not_notify_even_after_a_completed_one(app, base_url):
    run_turn(app, base_url, "cerrá el inventario del CDM")
    assert len(notifs(app.page)) == 1
    run_turn(app, base_url, "gracias, sin tareas esta vez")
    assert len(notifs(app.page)) == 1, "un turno sin lista propia no repite el aviso del turno anterior"


def test_stop_means_no_notification(app, base_url):
    page = app.page
    start(page, "cerrá el inventario y detené cuando quieras")
    page.wait_for_selector("#todo-list li:not(.todo-empty)", timeout=15_000)  # la lista (toda completed) ya llego
    page.click("#send-btn")  # Detener
    page.wait_for_selector("#send-btn[data-state=idle]", timeout=15_000)
    wait_turn_end(page, base_url, app.sid)
    assert notifs(page) == [], "un turno detenido no avisa aunque todas sus tareas esten completed"


def test_a_model_error_means_no_notification(app, base_url):
    page = app.page
    start(page, "cerrá el inventario aunque falla")
    page.wait_for_selector(".msg.error", timeout=20_000)
    wait_turn_end(page, base_url, app.sid)
    assert notifs(page) == [], "un turno que termina en error no avisa"


# ---------------------------------------------------------------------------
# REQ-002 / REQ-003: turno de otra sesion (de fondo)
# ---------------------------------------------------------------------------
def test_a_background_session_notifies_with_its_name_and_the_click_opens_it(app, base_url, provider):
    page = app.page
    sid_a = app.sid
    run_turn(app, base_url, "hola A, sin tareas")  # A ya tiene nombre en la lista
    assert notifs(page) == []
    provider.gate.clear()
    start(page, "esperá y cerrá el inventario de A")
    sid_b = new_session(page, sid_a)  # el usuario se va a otra sesion antes de que A termine
    provider.gate.set()
    wait_turn_end(page, base_url, sid_a)

    shown = notifs(page)
    assert len(shown) == 1, f"el turno de la sesion de fondo tiene que avisar: {shown}"
    assert_title(page, shown[0]["title"], "hola A, sin tareas")
    assert shown[0]["body"] == f"{ANSWER}\n3 de 3 tareas"
    assert page.inner_text("#meta-session-id") == sid_b, "avisar no cambia de sesion por su cuenta"
    assert ANSWER not in page.inner_text("#messages"), "la respuesta de A no se pinta en B"

    page.evaluate("window.__notifs[0].onclick({ target: window.__notifs[0] })")  # el usuario toca el aviso
    page.wait_for_function("id => document.getElementById('meta-session-id').textContent === id", arg=sid_a, timeout=10_000)
    page.wait_for_function("t => document.getElementById('messages').innerText.includes(t)", arg=ANSWER, timeout=10_000)
    assert notifs(page)[0]["closed"], "al tocarlo, el aviso se cierra"
    assert page.evaluate("window.__focusCalls") >= 1, "al tocarlo, Vision pasa al frente"


# ---------------------------------------------------------------------------
# REQ-003: una sola vez por turno
# ---------------------------------------------------------------------------
def test_two_tabs_on_the_same_session_show_a_single_notification(app, base_url, provider):
    page1 = app.page
    provider.gate.clear()
    start(page1, "esperá y cerrá el inventario")
    page2 = page1.context.new_page()
    errors2: list[str] = []
    page2.on("pageerror", lambda e: errors2.append(str(e)))
    try:
        page2.add_init_script(fake_notification_script())
        page2.goto(base_url + "/", wait_until="domcontentloaded")
        page2.wait_for_function("window.__deviceChangeListeners > 0")
        page2.wait_for_selector("#conn-status[data-state=busy]", timeout=20_000)  # la 2da pestaña se engancho al turno en curso
        provider.gate.set()
        wait_turn_end(page1, base_url, app.sid)
        page2.wait_for_timeout(500)
        both = notifs(page1) + notifs(page2)
        assert len(both) == 1, f"dos pestañas, un solo aviso: {both}"
        turn_id = user_turn_ids(page1)[-1]
        assert page1.evaluate("k => localStorage.getItem(k) !== null", f"vision.notified.{turn_id}"), "la marca del turno avisado"
    finally:
        page2.close()
    assert errors2 == [], f"errores de JS en la segunda pestaña: {errors2}"


def test_the_same_done_twice_or_after_reloading_notifies_once(app, base_url):
    """El mismo `done` dos veces (dos conexiones al mismo turno) o la misma respuesta otra vez despues de F5: un solo aviso. El stream
    se simula con `page.route` para repetir EXACTAMENTE el mismo turno."""
    page = app.page
    turn_id = "turno-repetido-1"

    def sse(*events) -> str:
        return "".join(f"data: {json.dumps(e, ensure_ascii=False)}\n\n" for e in events)

    body = sse(
        {"type": "todo", "data": {"todos": TODOS_DONE}, "turn_id": turn_id},
        {"type": "text-delta", "data": {"text": ANSWER}, "turn_id": turn_id},
        {"type": "done", "data": {"reason": "stop"}, "turn_id": turn_id},
        {"type": "done", "data": {"reason": "stop"}, "turn_id": turn_id},
    )

    def fulfill(route):
        if route.request.method == "POST":
            route.fulfill(status=200, headers={"Content-Type": "text/event-stream"}, body=body)
        else:
            route.continue_()

    page.route("**/api/chat", fulfill)
    page.fill("#chat-input", "cerrá el inventario")
    page.click("#send-btn")
    page.wait_for_selector("#send-btn[data-state=idle]", timeout=15_000)
    page.wait_for_timeout(800)
    shown = notifs(page)
    assert len(shown) == 1 and shown[0]["tag"] == turn_id, shown

    page.reload(wait_until="domcontentloaded")
    app.wait_ready()
    page.fill("#chat-input", "cerrá el inventario otra vez")
    page.click("#send-btn")
    page.wait_for_selector("#send-btn[data-state=idle]", timeout=15_000)
    page.wait_for_timeout(800)
    assert notifs(page) == [], "el mismo turno ya avisado no vuelve a avisar despues de recargar"


# ---------------------------------------------------------------------------
# REQ-006 / REQ-005: el interruptor de Configuracion
# ---------------------------------------------------------------------------
def test_the_switch_is_on_by_default_and_turning_it_off_stops_the_notification(app, base_url):
    page = app.page
    open_audio_settings(page)
    switch = page.locator("#cfg-notify-finish")
    assert switch.is_checked(), "prendido de fabrica"
    # text_content (el texto real): las etiquetas de Configuracion se ven en mayusculas por CSS, como todas las casillas.
    field_text = " ".join((page.locator(".settings-field", has=switch).text_content() or "").split())
    assert "Avisarme cuando termine" in field_text
    assert LOCK_SCREEN_LINE in field_text, "REQ-005: junto al interruptor se dice lo de la pantalla bloqueada"
    switch.uncheck()
    close_settings(page)
    run_turn(app, base_url, "cerrá el inventario del CDM")
    assert notifs(page) == [], "con el interruptor apagado no hay aviso"

    page.reload(wait_until="domcontentloaded")
    app.wait_ready()
    open_audio_settings(page)
    assert not page.locator("#cfg-notify-finish").is_checked(), "lo apagado sobrevive a F5"
    page.locator("#cfg-notify-finish").check()
    close_settings(page)
    run_turn(app, base_url, "cerrá el inventario del CDM otra vez")
    assert len(notifs(page)) == 1, "prendido de nuevo, vuelve a avisar"


# ---------------------------------------------------------------------------
# REQ-007: el permiso del navegador
# ---------------------------------------------------------------------------
def test_an_undecided_permission_is_asked_on_the_first_send_click_and_never_alone(app_factory, base_url):
    a = app_factory(permission="default", answer="granted")
    page = a.page
    page.wait_for_timeout(500)
    assert permission_requests(page) == [], "Vision no pide permiso sola al abrir"
    run_turn(a, base_url, "cerrá el inventario del CDM")
    requests = permission_requests(page)
    assert len(requests) == 1, f"se pide en el primer clic de Enviar: {requests}"
    assert requests[0]["userActivation"], "el pedido sale de un clic del usuario"
    assert len(notifs(page)) == 1, "concedido el permiso, el turno avisa"
    run_turn(a, base_url, "cerrá el inventario otra vez")
    assert len(permission_requests(page)) == 1, "no se vuelve a pedir en cada envio"


def test_an_undecided_permission_is_asked_when_turning_the_switch_on(app_factory):
    a = app_factory(permission="default", answer="denied")
    page = a.page
    open_audio_settings(page)
    switch = page.locator("#cfg-notify-finish")
    switch.uncheck()
    assert permission_requests(page) == [], "apagarlo no pide nada"
    switch.check()
    requests = permission_requests(page)
    assert len(requests) == 1 and requests[0]["userActivation"], f"prenderlo pide el permiso, desde el clic: {requests}"
    # El usuario lo nego: Vision lo explica en lenguaje claro y como habilitarlo.
    page.wait_for_function("() => !document.getElementById('notify-denied-notice').classList.contains('hidden')", timeout=5_000)
    status = page.inner_text("#cfg-notify-finish-permission")
    assert all(f in status for f in DENIED_FRAGMENTS), status


def test_a_denied_permission_is_explained_once(app_factory, base_url):
    a = app_factory(permission="denied")
    page = a.page
    run_turn(a, base_url, "cerrá el inventario del CDM")
    assert notifs(page) == [], "sin permiso no se puede mostrar nada"
    assert permission_requests(page) == [], "negado no se puede volver a pedir"
    notice = denied_notice(page)
    assert notice["visible"], "Vision tiene que decir que el navegador bloqueo los avisos"
    assert all(f in notice["text"] for f in DENIED_FRAGMENTS), notice["text"]
    assert "Edge" in notice["text"]
    page.click("#notify-denied-notice-close")
    assert not denied_notice(page)["visible"]

    run_turn(a, base_url, "cerrá el inventario otra vez")
    assert not denied_notice(page)["visible"], "se dice UNA vez"
    page.reload(wait_until="domcontentloaded")
    a.wait_ready()
    run_turn(a, base_url, "cerrá el inventario una vez mas")
    assert not denied_notice(page)["visible"], "tampoco vuelve despues de recargar"


def test_without_the_notification_api_nothing_breaks(app_factory, base_url):
    a = app_factory(missing=True)
    page = a.page
    run_turn(a, base_url, "cerrá el inventario del CDM")
    assert ANSWER in page.inner_text("#messages"), "el turno se ve como siempre"
    open_audio_settings(page)
    assert page.locator("#cfg-notify-finish").is_visible()
    assert "no puede mostrar" in page.inner_text("#cfg-notify-finish-permission")
    # open_app falla al cerrar si la pagina tiro algun error de JS


# ---------------------------------------------------------------------------
# REQ-005 / REQ-002: el cuerpo es texto plano y corto
# ---------------------------------------------------------------------------
def test_html_and_markdown_reach_the_notification_as_plain_text(app, base_url):
    run_turn(app, base_url, "cerrá el inventario con html")
    shown = notifs(app.page)
    assert len(shown) == 1
    summary, tasks = shown[0]["body"].split("\n")
    assert tasks == "3 de 3 tareas"
    for mark in ("<", ">", "**", "`", "](", "#"):
        assert mark not in summary, f"quedo {mark!r} en el aviso: {summary!r}"
    for word in ("Resumen del cierre", "Importante:", "stock", "NRC", "completo", "el informe"):
        assert word in summary, f"falta {word!r}: {summary!r}"


def test_a_long_answer_is_cut_at_a_word_with_an_ellipsis(app, base_url):
    run_turn(app, base_url, "cerrá el inventario, respuesta largo")
    shown = notifs(app.page)
    assert len(shown) == 1
    summary, tasks = shown[0]["body"].split("\n")
    assert tasks == "3 de 3 tareas"
    assert summary.endswith("…") and len(summary) <= 181, summary
    kept = summary[:-1]
    assert LONG_ANSWER.startswith(kept), summary
    assert LONG_ANSWER[len(kept)] in " ,.;:", f"tiene que cortar en una palabra, no en el medio: {summary!r}"
    assert len(kept) > 120, "recortado a unas 3 lineas, no a un par de palabras"
