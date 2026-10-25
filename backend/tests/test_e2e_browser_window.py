"""E2E de la ventana del Navegador Integrado (spec navegador-integrado-ventana
REQ-003/004/005/006) en un Chromium REAL: mover, redimensionar, limites,
persistencia, restablecer y aislamiento a pedido."""

from __future__ import annotations

import json

import pytest

from app.browser_workspace import get_browser_dir
from e2e_support import FakeWhisper, launch_chromium, open_app, serve_backend
from types import SimpleNamespace

from app.llm import loop as loop_module

pytestmark = pytest.mark.e2e

MODEL = "claude-sonnet-4-6"
SESSION_ID_KEY = "vision.currentSessionId"
GEOMETRY_KEY = "vision.browserPanel.geometry"
ISOLATED_KEY = "vision.browserPanel.isolated"


class _FakeLLM:
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
    proveedor = _FakeLLM()
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
        sid = a.page.evaluate(f"localStorage.getItem({json.dumps(SESSION_ID_KEY)})")
        preview = get_browser_dir(sid)
        preview.mkdir(parents=True, exist_ok=True)
        (preview / "index.html").write_text(
            "<html><body>Panel</body></html>", encoding="utf-8"
        )
        a.page.evaluate(f"localStorage.removeItem({json.dumps(GEOMETRY_KEY)})")
        a.page.evaluate(f"localStorage.removeItem({json.dumps(ISOLATED_KEY)})")
        a.sid = sid
        yield a


def open_panel(app) -> None:
    app.page.click("#quick-panel-btn")
    app.page.click("#quick-panel-browser-btn")
    app.page.wait_for_selector("#quick-browser-panel", state="visible")


def box(app) -> dict:
    return app.page.evaluate(
        "() => { const r = document.getElementById('quick-browser-panel').getBoundingClientRect();"
        " return {left: r.left, top: r.top, width: r.width, height: r.height}; }"
    )


def drag(app, selector: str, dx: int, dy: int) -> None:
    b = app.page.locator(selector).bounding_box()
    x, y = (
        b["x"] + 40,
        b["y"] + b["height"] / 2,
    )  # lejos de los botones (a la izquierda)
    app.page.mouse.move(x, y)
    app.page.mouse.down()
    app.page.mouse.move(x + dx / 2, y + dy / 2, steps=4)
    app.page.mouse.move(x + dx, y + dy, steps=4)
    app.page.mouse.up()


# ---------------------------------------------------------------------------
# REQ-003: mover
# ---------------------------------------------------------------------------
def test_dragging_the_header_moves_the_panel(app):
    open_panel(app)
    before = box(app)

    drag(app, ".quick-browser-header", 120, -80)

    after = box(app)
    assert abs((after["left"] - before["left"]) - 120) <= 2
    assert abs((after["top"] - before["top"]) - (-80)) <= 2
    assert after["width"] == before["width"]


def test_header_buttons_do_not_start_a_drag(app):
    open_panel(app)
    before = box(app)
    # Un arrastre que EMPIEZA sobre un boton (el de restablecer, que no abre
    # nada) no mueve el panel.
    b = app.page.locator("#quick-browser-reset-btn").bounding_box()
    app.page.mouse.move(b["x"] + 5, b["y"] + 5)
    app.page.mouse.down()
    app.page.mouse.move(b["x"] + 105, b["y"] + 65, steps=5)
    app.page.mouse.up()
    assert box(app)["left"] == before["left"]


def test_panel_never_leaves_the_screen_and_keeps_48px_of_header(app):
    open_panel(app)

    drag(app, ".quick-browser-header", 5000, 5000)

    vw = app.page.evaluate("window.innerWidth")
    vh = app.page.evaluate("window.innerHeight")
    b = box(app)
    assert b["left"] <= vw - 48
    assert b["top"] <= vh - 48
    assert b["top"] >= 0


def test_maximized_panel_is_not_draggable(app):
    open_panel(app)
    app.page.click("#quick-browser-max-btn")
    before = box(app)

    drag(app, ".quick-browser-header", 100, 100)

    assert box(app)["left"] == before["left"]
    app.page.click("#quick-browser-max-btn")  # restaura


# ---------------------------------------------------------------------------
# REQ-004: redimensionar
# ---------------------------------------------------------------------------
def test_resize_handle_changes_size_within_limits(app):
    open_panel(app)
    before = box(app)

    drag_handle = app.page.locator("#quick-browser-resize-handle").bounding_box()
    x, y = drag_handle["x"] + 8, drag_handle["y"] + 8
    app.page.mouse.move(x, y)
    app.page.mouse.down()
    app.page.mouse.move(x + 60, y + 40, steps=5)
    app.page.mouse.up()

    after = box(app)
    assert after["width"] > before["width"] + 40
    assert after["height"] > before["height"] + 20


def test_resize_respects_the_minimum_size(app):
    open_panel(app)
    h = app.page.locator("#quick-browser-resize-handle").bounding_box()
    x, y = h["x"] + 8, h["y"] + 8
    app.page.mouse.move(x, y)
    app.page.mouse.down()
    app.page.mouse.move(x - 3000, y - 3000, steps=6)
    app.page.mouse.up()

    after = box(app)
    assert after["width"] >= 320 - 1
    assert after["height"] >= 200 - 1


def test_iframe_ignores_the_mouse_while_dragging(app):
    open_panel(app)
    b = app.page.locator(".quick-browser-header").bounding_box()
    app.page.mouse.move(b["x"] + 40, b["y"] + 10)
    app.page.mouse.down()
    app.page.mouse.move(b["x"] + 80, b["y"] + 40, steps=3)
    pe = app.page.evaluate(
        "getComputedStyle(document.getElementById('quick-browser-iframe')).pointerEvents"
    )
    app.page.mouse.up()
    assert pe == "none"
    after = app.page.evaluate(
        "getComputedStyle(document.getElementById('quick-browser-iframe')).pointerEvents"
    )
    assert after != "none"


# ---------------------------------------------------------------------------
# REQ-005: persistencia + restablecer
# ---------------------------------------------------------------------------
def test_geometry_is_saved_and_restored_after_reload(app):
    open_panel(app)
    drag(app, ".quick-browser-header", 90, -60)
    moved = box(app)
    saved = json.loads(
        app.page.evaluate(f"localStorage.getItem({json.dumps(GEOMETRY_KEY)})")
    )
    assert set(saved) == {"left", "top", "width", "height"}

    app.page.reload(wait_until="domcontentloaded")
    app.wait_ready()
    open_panel(app)

    restored = box(app)
    assert abs(restored["left"] - moved["left"]) <= 2
    assert abs(restored["top"] - moved["top"]) <= 2


def test_corrupt_saved_geometry_falls_back_to_default_without_error(app):
    app.page.evaluate(
        f"localStorage.setItem({json.dumps(GEOMETRY_KEY)}, '{{esto no es json')"
    )
    app.page.reload(wait_until="domcontentloaded")
    app.wait_ready()

    open_panel(app)

    assert box(app)["width"] > 0
    assert app.page.get_attribute("#quick-browser-panel", "style") in (None, "")


def test_offscreen_saved_geometry_is_pulled_back_into_view(app):
    bad = {"left": 99999, "top": 99999, "width": 480, "height": 360}
    app.page.evaluate(
        f"localStorage.setItem({json.dumps(GEOMETRY_KEY)}, {json.dumps(json.dumps(bad))})"
    )
    app.page.reload(wait_until="domcontentloaded")
    app.wait_ready()

    open_panel(app)

    vw = app.page.evaluate("window.innerWidth")
    assert box(app)["left"] <= vw - 48


def test_reset_button_restores_default_and_clears_the_key(app):
    open_panel(app)
    default = box(app)
    drag(app, ".quick-browser-header", 100, -70)
    assert box(app)["left"] != default["left"]

    app.page.click("#quick-browser-reset-btn")

    assert abs(box(app)["left"] - default["left"]) <= 2
    assert (
        app.page.evaluate(f"localStorage.getItem({json.dumps(GEOMETRY_KEY)})") is None
    )


def test_maximized_state_is_not_saved(app):
    open_panel(app)
    app.page.click("#quick-browser-max-btn")
    app.page.reload(wait_until="domcontentloaded")
    app.wait_ready()

    open_panel(app)

    cls = app.page.get_attribute("#quick-browser-panel", "class")
    assert "maximized" not in cls


# ---------------------------------------------------------------------------
# REQ-006: aislamiento a pedido
# ---------------------------------------------------------------------------
def test_panel_is_not_isolated_by_default(app):
    open_panel(app)
    assert app.page.get_attribute("#quick-browser-iframe", "sandbox") is None
    assert (
        app.page.get_attribute("#quick-browser-isolate-btn", "aria-pressed") == "false"
    )


def test_isolate_toggle_sets_sandbox_without_same_origin_and_persists(app):
    open_panel(app)

    app.page.click("#quick-browser-isolate-btn")

    sandbox = app.page.get_attribute("#quick-browser-iframe", "sandbox")
    assert "allow-scripts" in sandbox
    assert "allow-same-origin" not in sandbox
    assert (
        app.page.evaluate(f"localStorage.getItem({json.dumps(ISOLATED_KEY)})") == "true"
    )

    app.page.click("#quick-browser-isolate-btn")  # lo apaga de nuevo
    assert app.page.get_attribute("#quick-browser-iframe", "sandbox") is None
    assert (
        app.page.evaluate(f"localStorage.getItem({json.dumps(ISOLATED_KEY)})")
        == "false"
    )
