"""E2E de la busqueda dentro de la conversacion abierta (spec busqueda-mensajes REQ-004)
en un Chromium REAL: la barra "N/M" con flechas que recorre las coincidencias.

Pedido del usuario (2026-09-29): "la busqueda de una palabra en la sesion tiene que decir
cuantas encontro y tener una flechita para iterar, 1/10, 2/10... 10/10 y vuelve a 1/10".
Reporto "no me aparece la barra": estos tests reproducen el escenario de punta a punta
(escribir en el buscador con la conversacion abierta) para ver si aparece y como se mueve."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.llm import loop as loop_module
from e2e_support import FakeWhisper, launch_chromium, open_app, serve_backend

pytestmark = pytest.mark.e2e

MODEL = "claude-sonnet-4-6"
WORD = "palabraclave"


class FakeLLM:
    def __init__(self):
        self.whisper = FakeWhisper()
        self.audio = SimpleNamespace(transcriptions=self.whisper)
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=lambda **k: iter([]))
        )

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


@pytest.fixture(scope="module")
def base_url():
    proveedor = FakeLLM()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(loop_module, "get_client", lambda *a, **k: proveedor)
        with serve_backend(proveedor) as url:
            yield url


@pytest.fixture(scope="module")
def browser(tmp_path_factory, base_url):
    with launch_chromium(tmp_path_factory.mktemp("audio"), base_url) as b:
        yield b


@pytest.fixture
def app(browser, base_url):
    with open_app(browser, base_url) as a:
        a.open()
        yield a


def fill_conversation(app, total: int) -> None:
    """Pinta `total` apariciones de la palabra repartidas en varias burbujas."""
    app.page.evaluate(
        """([word, total]) => {
          const box = document.getElementById("messages");
          box.innerHTML = "";
          let left = total, i = 0;
          while (left > 0) {
            const n = Math.min(left, 2);
            const div = document.createElement("div");
            div.className = "msg " + (i % 2 ? "assistant" : "user");
            div.textContent = `Mensaje ${i}: ` + Array(n).fill(word).join(" y otra vez ");
            box.appendChild(div);
            left -= n; i += 1;
          }
        }""",
        [WORD, total],
    )


def search(app, term: str) -> None:
    # El buscador vive dentro del acordeon "Sesion" (<details>), cerrado de fabrica.
    app.page.evaluate("document.getElementById('acc-session').open = true")
    app.page.fill("#session-search-input", term)
    # Se espera a que la busqueda TERMINE (debounce de 300 ms + consulta al backend + resaltado),
    # no un tiempo fijo: con la PC cargada 700 ms no alcanzaba y el test fallaba al azar. Termina
    # cuando aparece la lista de resultados o "Sin coincidencias" (hay resultados del backend) o,
    # si el termino esta en la conversacion abierta, cuando ya hay marcas resaltadas.
    app.page.wait_for_function(
        """() => {
          const results = document.getElementById("session-search-results");
          return document.querySelectorAll("mark.search-match").length > 0
            || (results && !results.classList.contains("hidden"));
        }""",
        timeout=15_000,
    )
    app.page.wait_for_timeout(150)  # deja que termine el resaltado que sigue al render de resultados


def nav_state(app) -> dict:
    return app.page.evaluate(
        """() => {
          const bar = document.getElementById("search-nav-bar");
          const r = bar.getBoundingClientRect();
          const cs = getComputedStyle(bar);
          return {
            hiddenClass: bar.classList.contains("hidden"),
            display: cs.display, visibility: cs.visibility,
            width: Math.round(r.width), height: Math.round(r.height),
            inViewport: r.bottom > 0 && r.top < window.innerHeight,
            text: document.getElementById("search-nav-count").textContent,
            marks: document.querySelectorAll("mark.search-match").length,
            active: document.querySelectorAll("mark.search-match-active").length,
          };
        }"""
    )


def test_the_bar_shows_how_many_matches_were_found(app):
    fill_conversation(app, 10)
    search(app, WORD)

    state = nav_state(app)

    assert state["marks"] == 10, state
    assert state["hiddenClass"] is False and state["display"] != "none", state
    assert state["width"] > 0 and state["height"] > 0 and state["inViewport"], state
    assert state["text"] == "1/10", state
    assert state["active"] == 1


def test_the_arrows_walk_through_all_matches_and_wrap_around(app):
    fill_conversation(app, 10)
    search(app, WORD)
    seen = [nav_state(app)["text"]]

    for _ in range(10):
        app.page.click("#search-nav-next")
        seen.append(nav_state(app)["text"])

    assert all("/10" in t and " de " not in t for t in seen), seen  # formato pedido: 1/10
    counts = [int(t.split("/")[0]) for t in seen]
    assert counts == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 1], (
        seen
    )  # 10 de 10 vuelve a 1 de 10
    assert nav_state(app)["active"] == 1  # siempre UNA sola activa


def test_the_previous_arrow_wraps_backwards(app):
    fill_conversation(app, 10)
    search(app, WORD)

    app.page.click("#search-nav-prev")

    assert nav_state(app)["text"] == "10/10"


def test_no_matches_hides_the_bar(app):
    fill_conversation(app, 3)
    search(app, "esta-palabra-no-existe-zzz")
    assert nav_state(app)["hiddenClass"] is True


def test_clearing_the_search_removes_marks_and_hides_the_bar(app):
    fill_conversation(app, 4)
    search(app, WORD)
    assert nav_state(app)["marks"] == 4

    app.page.click("#search-nav-close")

    state = nav_state(app)
    assert state["marks"] == 0 and state["hiddenClass"] is True
    assert app.page.input_value("#session-search-input") == ""


def test_a_single_match_still_shows_the_bar(app):
    fill_conversation(app, 1)
    search(app, WORD)
    state = nav_state(app)
    assert state["hiddenClass"] is False and state["text"] == "1/1"



# ---------------------------------------------------------------------------
# La palabra esta en la sesion ACTUAL y ya se guardo: el buscador llama al backend con el id
# de la sesion (antes iba sin id y la busqueda global no encontraba nada -- ver el fix en
# `searchAllSessionsGlobal`). Buscar en las OTRAS sesiones (otras carpetas) es un cambio de
# diseno aparte, pendiente de decision del usuario.
# ---------------------------------------------------------------------------
def test_the_search_sends_the_current_session_id_to_the_backend(app):
    sent: list[str] = []
    app.page.on("request", lambda req: sent.append(req.url) if "/api/sessions/search" in req.url else None)
    fill_conversation(app, 3)

    search(app, WORD)

    assert sent, "el buscador no llamo al backend"
    assert "session_id=" in sent[-1] and "q=" in sent[-1], sent[-1]


# ---------------------------------------------------------------------------
# REQ-007 (busqueda-mensajes): la palabra esta SOLO en otra sesion (otra carpeta). Al escribirla
# aparece esa sesion; al tocarla se cambia de sesion, se espera a que la conversacion este
# pintada y recien ahi se resalta y se muestra el contador. Antes el buscador no mandaba el id de
# la sesion, el backend devolvia siempre vacio y la barra nunca aparecia ("no me aparece la barra").
# ---------------------------------------------------------------------------
def seed_other_session(app, tmp_path, total: int) -> str:
    import time
    import uuid

    from app.persistence import db

    # Un id NUEVO en cada test: el backend (y su memoria de sesiones) es uno por modulo, pero el indice maestro es uno por test
    # (`tmp_master_db`). Con el mismo id, el segundo test le asignaba carpeta "por primera vez" a una sesion que todavia tenia en memoria
    # los mensajes del test anterior y, desde la revision de REQ-016 (spec fix-detener-y-progreso-visible: lo hablado antes de la carpeta
    # se guarda en su vision.db), esos 8 mensajes se sumaban a los 3 sembrados.
    other = f"sesion-con-la-palabra-{uuid.uuid4().hex[:8]}"
    project = tmp_path / "otro-proyecto"
    project.mkdir()
    app.page.evaluate(
        """async ([sid, path]) => {
          const r = await fetch(`/api/sessions/${sid}/project-path`, {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ path, title: "Sesion con la palabra" }),
          });
          if (!r.ok) throw new Error(await r.text());
        }""",
        [other, str(project)],
    )
    db_path = db.db_path_for(str(project))
    now = time.time()
    db.ensure_session(db_path, other, now)
    for i in range(total):
        mid = f"m{i}"
        db.insert_message(db_path, mid, other, "user" if i % 2 == 0 else "assistant", now + i)
        db.insert_part(db_path, f"{mid}-t", mid, other, "text", {"text": f"Fila {i} con la {WORD} adentro"}, now + i)
    return other


def test_a_word_that_only_exists_in_another_session_is_found_and_counted(app, tmp_path):
    other = seed_other_session(app, tmp_path, 8)
    app.page.evaluate("document.getElementById('acc-session').open = true")
    app.page.fill("#session-search-input", WORD)
    app.page.wait_for_selector(".session-search-results-item", timeout=10_000)

    app.page.click(".session-search-results-item")
    app.page.wait_for_function("document.querySelectorAll('mark.search-match').length === 8", timeout=10_000)

    state = nav_state(app)
    # Se cambio de verdad a la sesion que tenia la palabra (el id activo se guarda en localStorage).
    assert app.page.evaluate("localStorage.getItem('vision.currentSessionId')") == other
    assert state["marks"] == 8 and state["hiddenClass"] is False, state
    assert state["text"] == "1/8", state


def test_walking_the_matches_of_a_session_opened_from_a_result(app, tmp_path):
    seed_other_session(app, tmp_path, 3)
    app.page.evaluate("document.getElementById('acc-session').open = true")
    app.page.fill("#session-search-input", WORD)
    app.page.wait_for_selector(".session-search-results-item", timeout=10_000)
    app.page.click(".session-search-results-item")
    app.page.wait_for_function("document.querySelectorAll('mark.search-match').length === 3", timeout=10_000)

    seen = [nav_state(app)["text"]]
    for _ in range(3):
        app.page.click("#search-nav-next")
        seen.append(nav_state(app)["text"])

    nums = [int(t.split("/")[0]) for t in seen]
    assert nums == [1, 2, 3, 1], seen  # da la vuelta


def test_enter_and_shift_enter_in_the_search_box_walk_the_matches(app):
    fill_conversation(app, 4)
    search(app, WORD)
    assert nav_state(app)["text"] == "1/4"

    app.page.press("#session-search-input", "Enter")
    assert nav_state(app)["text"] == "2/4"
    app.page.press("#session-search-input", "Enter")
    app.page.press("#session-search-input", "Enter")
    app.page.press("#session-search-input", "Enter")
    assert nav_state(app)["text"] == "1/4"  # 4/4 -> vuelve a 1/4
    app.page.press("#session-search-input", "Shift+Enter")
    assert nav_state(app)["text"] == "4/4"  # y hacia atras tambien da la vuelta
    assert nav_state(app)["active"] == 1


def test_enter_with_no_matches_does_nothing(app):
    fill_conversation(app, 2)
    search(app, "esta-palabra-no-existe-zzz")
    app.page.press("#session-search-input", "Enter")
    assert nav_state(app)["hiddenClass"] is True


# ---------------------------------------------------------------------------
# spec fix-busqueda-limpia (pedido del usuario 2026-10-14): «cuando limpio o borro lo que puse se deberian eliminar los resultados y deberia
# quedar el session-search-results limpio sin resultados».
# ---------------------------------------------------------------------------
RESULTS_STATE_JS = """() => {
  const r = document.getElementById("session-search-results");
  return {
    items: r.querySelectorAll("li").length,
    hidden: r.classList.contains("hidden"),
    listHidden: document.getElementById("session-list").classList.contains("hidden"),
  };
}"""


def test_clearing_the_box_leaves_the_results_list_empty(app, tmp_path):
    """REQ-001: borrar la caja deja #session-search-results vacio (no solo oculto) y vuelve la lista de sesiones."""
    seed_other_session(app, tmp_path, 3)
    app.page.evaluate("document.getElementById('acc-session').open = true")
    app.page.fill("#session-search-input", WORD)
    app.page.wait_for_selector(".session-search-results-item", timeout=10_000)

    app.page.fill("#session-search-input", "")
    app.page.wait_for_timeout(800)  # mas que el debounce: nada pendiente vuelve a pintar

    state = app.page.evaluate(RESULTS_STATE_JS)
    assert state == {"items": 0, "hidden": True, "listHidden": False}, state


def test_a_slow_search_that_arrives_after_clearing_does_not_bring_results_back(app, tmp_path):
    """REQ-002: la busqueda ya salio (espera al backend) y el usuario borra la caja: su respuesta tardia se descarta."""
    import threading

    seed_other_session(app, tmp_path, 3)
    app.page.evaluate("document.getElementById('acc-session').open = true")
    released = threading.Event()

    def slow_search(route):
        released.wait(timeout=5)  # retiene la respuesta hasta que el test borre la caja
        route.continue_()

    app.page.route("**/api/sessions/search**", slow_search)
    app.page.fill("#session-search-input", WORD)
    app.page.wait_for_timeout(700)  # vencio el debounce de 300 ms: el pedido ya salio y espera
    app.page.fill("#session-search-input", "")
    released.set()
    app.page.wait_for_timeout(1500)  # la respuesta tardia ya llego

    state = app.page.evaluate(RESULTS_STATE_JS)
    app.page.unroute("**/api/sessions/search**")
    assert state == {"items": 0, "hidden": True, "listHidden": False}, state


def test_changing_the_word_while_the_previous_search_travels_shows_only_the_new_one(app, tmp_path):
    """REQ-002: la respuesta de la palabra vieja (que llega ULTIMA) no pisa los resultados de la nueva."""
    seed_other_session(app, tmp_path, 3)
    app.page.evaluate("document.getElementById('acc-session').open = true")
    # La demora la pone la propia pagina (las rutas de Playwright se atienden de a una y frenarian tambien la segunda busqueda).
    app.page.evaluate(
        """() => {
          const original = window.fetch;
          window.fetch = async (url, opts) => {
            if (String(url).includes("/api/sessions/search") && String(url).includes("q=esta-palabra-no-existe-zzz")) {
              await new Promise((r) => setTimeout(r, 2000));
            }
            return original(url, opts);
          };
        }"""
    )
    app.page.fill("#session-search-input", "esta-palabra-no-existe-zzz")
    app.page.wait_for_timeout(700)  # salio la busqueda vieja (demorada 2 s)
    app.page.fill("#session-search-input", WORD)
    app.page.wait_for_selector(".session-search-results-item", timeout=10_000)
    app.page.wait_for_timeout(2500)  # ya llego la respuesta vieja

    items = app.page.evaluate("document.querySelectorAll('#session-search-results .session-search-results-item').length")
    assert items >= 1, "la respuesta vieja («Sin coincidencias») piso los resultados de la palabra nueva"
