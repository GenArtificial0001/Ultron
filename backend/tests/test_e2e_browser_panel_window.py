"""E2E de la ventana del Navegador Integrado (spec navegador-integrado-ventana,
REQ-003 mover, REQ-004 redimensionar, REQ-005 recordar y restablecer) en un
Chromium REAL contra el backend real en loopback.

Contrato de la spec que se prueba (no la implementacion): se arrastra desde el
header (sus botones no arrastran), siempre quedan a la vista al menos 48 px
del header, el asa de la esquina (`#quick-browser-resize-handle`) redimensiona
entre 320x200 y la ventana menos 16 px por lado, la geometria se guarda en
`vision.browserPanel.geometry` y el boton `#quick-browser-reset-btn` vuelve al
default. Maximizar/minimizar siguen funcionando igual (no regresion).

Viewport de Playwright por defecto: 1280x720. Geometria default del panel
(CSS de siempre): left 24, bottom 156, 480x360 -> caja (24, 204, 480, 360).
Infraestructura en `e2e_support.py`.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.llm import loop as loop_module
from e2e_support import FakeWhisper, launch_chromium, open_app, serve_backend

pytestmark = pytest.mark.e2e

MODEL = "claude-sonnet-4-6"
GEOMETRY_KEY = "vision.browserPanel.geometry"
DEFAULT_BOX = (24, 204, 480, 360)
HEADER_H = 30
TOL = 2  # px de tolerancia por redondeo subpixel


class FakeLLM:
    def __init__(self):
        self.whisper = FakeWhisper()
        self.audio = SimpleNamespace(transcriptions=self.whisper)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def get(self, path, cast_to=object):
        return {"data": [{"id": MODEL, "type": "LLM", "aliases": [], "valid_args": [], "reasoning_levels": []}]}

    def _create(self, **kwargs):
        delta = SimpleNamespace(content="[[TIPO: conversacional]]Listo", tool_calls=[])
        stop = SimpleNamespace(content=None, tool_calls=[])
        return iter(
            [
                SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=None)]),
                SimpleNamespace(choices=[SimpleNamespace(delta=stop, finish_reason="stop")]),
            ]
        )


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
def app(browser, base_url):
    with open_app(browser, base_url) as a:
        a.open()
        yield a


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def open_panel(app) -> None:
    app.page.click("#quick-panel-btn")
    app.page.click("#quick-panel-browser-btn")
    app.page.wait_for_selector("#quick-browser-panel", state="visible")


def box(app) -> tuple[float, float, float, float]:
    b = app.page.locator("#quick-browser-panel").bounding_box()
    return (b["x"], b["y"], b["width"], b["height"])


def close_to(actual, expected, tol: float = TOL) -> bool:
    return all(abs(a - e) <= tol for a, e in zip(actual, expected, strict=True))


def drag(app, selector: str, dx: float, dy: float, steps: int = 12) -> None:
    """Arrastra desde el centro de `selector` hasta (dx, dy) mas alla."""
    b = app.page.locator(selector).bounding_box()
    x, y = b["x"] + b["width"] / 2, b["y"] + b["height"] / 2
    app.page.mouse.move(x, y)
    app.page.mouse.down()
    app.page.mouse.move(x + dx, y + dy, steps=steps)
    app.page.mouse.up()


def drag_to(app, selector: str, to_x: float, to_y: float, steps: int = 12) -> None:
    b = app.page.locator(selector).bounding_box()
    drag(app, selector, to_x - (b["x"] + b["width"] / 2), to_y - (b["y"] + b["height"] / 2), steps)


def saved_geometry(app):
    raw = app.page.evaluate(f"localStorage.getItem({json.dumps(GEOMETRY_KEY)})")
    return json.loads(raw) if raw else None


# ---------------------------------------------------------------------------
# REQ-003: mover
# ---------------------------------------------------------------------------
def test_the_default_geometry_is_the_same_as_always(app):
    # No regresion: sin nada guardado, el panel abre donde y como siempre.
    open_panel(app)
    assert close_to(box(app), DEFAULT_BOX), box(app)


def test_dragging_the_header_moves_the_panel_and_keeps_its_size(app):
    open_panel(app)
    x, y, w, h = box(app)

    drag(app, ".quick-browser-title", 300, -100)

    assert close_to(box(app), (x + 300, y - 100, w, h)), box(app)


def test_header_buttons_do_not_start_a_drag(app):
    open_panel(app)
    before = box(app)

    drag(app, "#quick-browser-min-btn", -200, 150)

    assert close_to(box(app)[:2], before[:2]), box(app)


def test_the_header_always_stays_reachable(app):
    open_panel(app)

    # Contra el borde de abajo: el header entero sigue a la vista.
    drag_to(app, ".quick-browser-title", 640, 719)
    x, y, w, h = box(app)
    assert y + HEADER_H <= 720 + TOL, box(app)

    # La ventana se achica con el panel a la derecha: se reacomoda solo.
    drag_to(app, ".quick-browser-title", 1100, 300)
    app.page.set_viewport_size({"width": 700, "height": 500})
    app.page.wait_for_function(
        "() => document.getElementById('quick-browser-panel').getBoundingClientRect().left <= 700 - 48"
    )
    x, y, w, h = box(app)
    assert x <= 700 - 48 + TOL and x + w >= 48 - TOL, box(app)
    assert 0 - TOL <= y <= 500 - HEADER_H + TOL, box(app)


def test_a_maximized_panel_is_not_dragged(app):
    open_panel(app)
    app.page.click("#quick-browser-max-btn")
    before = box(app)

    drag(app, ".quick-browser-title", 200, 100)

    assert close_to(box(app), before), box(app)


# ---------------------------------------------------------------------------
# REQ-004: redimensionar
# ---------------------------------------------------------------------------
def test_resizing_from_the_corner_changes_the_size_within_limits(app):
    open_panel(app)
    x, y, w, h = box(app)

    drag(app, "#quick-browser-resize-handle", 120, 80)
    assert close_to(box(app), (x, y, w + 120, h + 80)), box(app)

    # Achicar de mas (el puntero pasa sobre el iframe): se frena en el minimo.
    drag_to(app, "#quick-browser-resize-handle", 5, 5, steps=20)
    assert close_to(box(app), (x, y, 320, 200)), box(app)


def test_the_resize_handle_is_hidden_when_minimized_or_maximized(app):
    open_panel(app)
    assert app.page.is_visible("#quick-browser-resize-handle")
    app.page.click("#quick-browser-min-btn")
    assert app.page.is_hidden("#quick-browser-resize-handle")
    app.page.click("#quick-browser-min-btn")
    app.page.click("#quick-browser-max-btn")
    assert app.page.is_hidden("#quick-browser-resize-handle")


# ---------------------------------------------------------------------------
# REQ-005: recordar y restablecer
# ---------------------------------------------------------------------------
def test_position_and_size_survive_a_reload(app):
    open_panel(app)
    drag(app, ".quick-browser-title", 250, -60)
    drag(app, "#quick-browser-resize-handle", 100, 50)
    moved = box(app)
    saved = saved_geometry(app)
    assert set(saved) == {"left", "top", "width", "height"}
    assert all(isinstance(v, int) for v in saved.values()), saved

    app.page.reload(wait_until="domcontentloaded")
    app.wait_ready()
    open_panel(app)

    assert close_to(box(app), moved), (box(app), moved)


def test_the_reset_button_restores_the_default_and_forgets_it(app):
    open_panel(app)
    drag(app, ".quick-browser-title", 250, -60)
    drag(app, "#quick-browser-resize-handle", 100, 50)
    assert saved_geometry(app) is not None

    app.page.click("#quick-browser-reset-btn")

    assert close_to(box(app), DEFAULT_BOX), box(app)
    assert saved_geometry(app) is None


def test_a_corrupt_saved_geometry_falls_back_to_the_default(app):
    app.page.evaluate(f"localStorage.setItem({json.dumps(GEOMETRY_KEY)}, '{{roto')")
    app.page.reload(wait_until="domcontentloaded")
    app.wait_ready()

    open_panel(app)

    assert close_to(box(app), DEFAULT_BOX), box(app)  # y sin errores de JS (open_app)


def test_maximize_and_minimize_still_work_after_moving(app):
    open_panel(app)
    drag(app, ".quick-browser-title", 300, -100)
    moved = box(app)

    app.page.click("#quick-browser-max-btn")
    assert close_to(box(app), (20, 20, 1240, 680)), box(app)
    app.page.click("#quick-browser-max-btn")  # restaurar
    assert close_to(box(app), moved), box(app)

    app.page.click("#quick-browser-min-btn")
    x, y, w, h = box(app)
    assert close_to((x, y, w, h), (moved[0], moved[1], moved[2], HEADER_H)), box(app)
    app.page.click("#quick-browser-min-btn")  # restaurar
    assert close_to(box(app), moved), box(app)
