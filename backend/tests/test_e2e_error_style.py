"""E2E del estilo de los mensajes de error en un Chromium REAL (spec error-estilo-system-note, REQ-001..010).

Pedido del usuario (2026-10-22): «cambiar en el frontend el estilo del msg-error para que sea igual a msg system-note pero con letras rojas, con el detalle tecnico incluido y con el ::marker
del msg-error-technical open y no open».

Lo que se mide son estilos COMPUTADOS en un navegador real (color, fondo, borde, sombra, alineacion, tipografia, contenido del marcador), en los tres temas, y que `system-note` no cambio.
LIMITE: se prueba el estilo, NO la estetica: que el tamano de 11,5 px le resulte comodo al usuario para un error solo lo puede decir el usuario mirandolo.
La burbuja de error se provoca con el flujo real (El proveedor de IA falso que falla -> loop -> SSE -> burbuja), igual que `test_e2e_error_messages.py`."""

from __future__ import annotations

from types import SimpleNamespace

import openai
import pytest

from app.llm import loop as loop_module
from e2e_support import launch_chromium, open_app, serve_backend
from test_e2e_error_messages import FakeProvider as FakeLLM, last_error, send

pytestmark = pytest.mark.e2e

THEMES = ["dark", "light", "dark-blue"]
SPEND_429 = "Error code: 429 - Too much spend: model claude-sonnet-4-6 is limited to $50 per 600 seconds. RAW-PROVEEDOR"

STYLE_OF = """(sel) => {
  const el = [...document.querySelectorAll(sel)].at(-1);
  const s = getComputedStyle(el);
  return { color: s.color, bg: s.backgroundColor, border: s.borderTopWidth + ' ' + s.borderTopStyle, shadow: s.boxShadow,
           align: s.alignSelf, family: s.fontFamily, size: s.fontSize, weight: s.fontWeight };
}"""
ERROR_PARTS = """() => {
  const el = [...document.querySelectorAll('.msg.error')].at(-1);
  const sum = el.querySelector('details.msg-error-technical summary');
  const code = el.querySelector('details.msg-error-technical code');
  const text = el.querySelector('.msg-text');
  const mk = getComputedStyle(sum, '::marker');
  const details = el.querySelector('details.msg-error-technical');
  const out = { text: getComputedStyle(text).color, summary: getComputedStyle(sum).color, code: getComputedStyle(code).color,
                summaryFamily: getComputedStyle(sum).fontFamily, codeFamily: getComputedStyle(code).fontFamily,
                markerColor: mk.color, markerClosed: mk.content };
  details.open = true;
  out.markerOpen = getComputedStyle(sum, '::marker').content;
  out.markerOpenColor = getComputedStyle(sum, '::marker').color;
  details.open = false;
  return out;
}"""


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
    with launch_chromium(tmp_path_factory.mktemp("errstyle"), base_url) as b:
        yield b


@pytest.fixture
def app(browser, base_url, proveedor):
    proveedor.chat_exc = None
    with open_app(browser, base_url) as a:
        a.open()
        yield a


def make_error(app, proveedor) -> None:
    response = SimpleNamespace(status_code=429, headers={}, request=None)
    proveedor.chat_exc = openai.RateLimitError(SPEND_429, response=response, body=None)
    send(app)
    last_error(app)


def make_system_note(app) -> None:
    app.page.evaluate("document.querySelector('.msg.system-note') || 0")


def set_theme(app, theme: str) -> None:
    app.page.evaluate(
        "(t) => { if (t === 'dark') delete document.documentElement.dataset.theme; else document.documentElement.dataset.theme = t; }",
        theme,
    )


def parse_rgb(css: str) -> tuple[float, float, float, float]:
    inside = css[css.index("(") + 1 : css.index(")")].replace("/", " ").replace(",", " ").split()
    nums = [float(x) for x in inside]
    return (nums[0], nums[1], nums[2], nums[3] if len(nums) > 3 else 1.0)


def luminance(rgb: tuple[float, float, float, float]) -> float:
    def f(c: float) -> float:
        c /= 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    return 0.2126 * f(rgb[0]) + 0.7152 * f(rgb[1]) + 0.0722 * f(rgb[2])


def contrast(a: str, b: str) -> float:
    la, lb = luminance(parse_rgb(a)), luminance(parse_rgb(b))
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def is_reddish(css: str) -> bool:
    r, g, b, _ = parse_rgb(css)
    return r > 120 and r > g * 1.6 and r > b * 1.6


def page_bg(app) -> str:
    return app.page.evaluate("getComputedStyle(document.body).backgroundColor")


# ------------------------------------------------------------------------------------------------------------------ REQ-001 / REQ-002
def test_an_error_looks_like_a_system_note_centered_flat_and_monospace(app, proveedor):
    app.page.evaluate("window.__note = true")
    app.page.evaluate(
        "() => { const m = document.createElement('div'); m.className = 'msg system-note'; m.textContent = 'aviso'; document.querySelector('#messages, .messages').appendChild(m); }"
    )
    note = app.page.evaluate(STYLE_OF, ".msg.system-note")
    make_error(app, proveedor)

    err = app.page.evaluate(STYLE_OF, ".msg.error")

    assert err["bg"] == "rgba(0, 0, 0, 0)", f"sin fondo: {err['bg']}"
    assert err["border"].startswith("0px"), f"sin borde: {err['border']}"
    assert err["shadow"] == "none", f"sin sombra: {err['shadow']}"
    assert err["align"] == "center" == note["align"], "centrado como la nota del sistema"
    assert err["family"] == note["family"], "misma tipografia que la nota"
    assert err["size"] == note["size"] == "11.5px"


# ------------------------------------------------------------------------------------------------------------------ REQ-003: rojo y legible en los 3 temas
@pytest.mark.parametrize("theme", THEMES)
def test_the_error_text_is_red_and_readable_in_every_theme(app, proveedor, theme):
    make_error(app, proveedor)
    set_theme(app, theme)

    err = app.page.evaluate(STYLE_OF, ".msg.error")
    parts = app.page.evaluate(ERROR_PARTS)

    assert is_reddish(err["color"]), f"el texto del error tiene que ser rojo: {err['color']}"
    assert contrast(err["color"], page_bg(app)) >= 4.5, (
        f"contraste insuficiente en {theme}: {contrast(err['color'], page_bg(app)):.2f}:1"
    )
    assert is_reddish(parts["text"])


# ------------------------------------------------------------------------------------------------------------------ REQ-004
@pytest.mark.parametrize("theme", THEMES)
def test_the_technical_detail_has_the_same_red_and_a_monospace_font(app, proveedor, theme):
    make_error(app, proveedor)
    set_theme(app, theme)

    parts = app.page.evaluate(ERROR_PARTS)

    assert parts["summary"] == parts["code"] == parts["text"], "detalle tecnico del mismo rojo que el texto"
    assert "monospace" in parts["summaryFamily"].lower() or "mono" in parts["summaryFamily"].lower()
    assert parts["summaryFamily"] == parts["codeFamily"]
    assert contrast(parts["code"], page_bg(app)) >= 4.5


# ------------------------------------------------------------------------------------------------------------------ REQ-005: ::marker abierto y cerrado
@pytest.mark.parametrize("theme", THEMES)
def test_the_marker_is_red_and_changes_between_closed_and_open(app, proveedor, theme):
    make_error(app, proveedor)
    set_theme(app, theme)

    parts = app.page.evaluate(ERROR_PARTS)

    assert is_reddish(parts["markerColor"]), f"marcador rojo (cerrado): {parts['markerColor']}"
    assert is_reddish(parts["markerOpenColor"]), f"marcador rojo (abierto): {parts['markerOpenColor']}"
    assert "▸" in parts["markerClosed"], f"cerrado = ▸: {parts['markerClosed']!r}"
    assert "▾" in parts["markerOpen"], f"abierto = ▾: {parts['markerOpen']!r}"
    assert parts["markerClosed"] != parts["markerOpen"]


def test_opening_the_detail_with_a_real_click_flips_the_marker(app, proveedor):
    make_error(app, proveedor)
    summary = app.page.locator(".msg.error details.msg-error-technical summary").last

    def marker() -> str:
        return app.page.evaluate(
            "() => getComputedStyle([...document.querySelectorAll('.msg.error summary')].at(-1), '::marker').content"
        )

    assert "▸" in marker()
    summary.click()
    assert app.page.evaluate("() => [...document.querySelectorAll('.msg.error details')].at(-1).open") is True
    assert "▾" in marker()
    summary.click()
    assert "▸" in marker()


# ------------------------------------------------------------------------------------------------------------------ REQ-006: system-note intacta
@pytest.mark.parametrize("theme", THEMES)
def test_the_system_note_did_not_change(app, theme):
    app.page.evaluate(
        "() => { const m = document.createElement('div'); m.className = 'msg system-note'; m.textContent = 'aviso'; document.querySelector('#messages, .messages').appendChild(m); }"
    )
    set_theme(app, theme)

    note = app.page.evaluate(STYLE_OF, ".msg.system-note")

    assert note["align"] == "center" and note["bg"] == "rgba(0, 0, 0, 0)" and note["shadow"] == "none"
    assert note["size"] == "11.5px"
    assert not is_reddish(note["color"]), f"la nota del sistema NO es roja: {note['color']}"


# ------------------------------------------------------------------------------------------------------------------ REQ-007: resuelto sigue verde
def test_a_solved_error_stays_green_and_a_failing_one_stays_red(app, proveedor):
    make_error(app, proveedor)
    app.page.evaluate(
        "() => { const el = [...document.querySelectorAll('.msg.error')].at(-1); el.classList.add('msg-solved'); }"
    )

    solved = app.page.evaluate(
        "() => getComputedStyle([...document.querySelectorAll('.msg.error .msg-text')].at(-1)).color"
    )

    r, g, b, _ = parse_rgb(solved)
    assert g > r and g > b, f"un error resuelto se ve verde, no rojo: {solved}"
    app.page.evaluate(
        "() => { const el = [...document.querySelectorAll('.msg.error')].at(-1); el.classList.remove('msg-solved'); }"
    )
    again = app.page.evaluate(
        "() => getComputedStyle([...document.querySelectorAll('.msg.error .msg-text')].at(-1)).color"
    )
    assert is_reddish(again)


# ------------------------------------------------------------------------------------------------------------------ REQ-008: el contenido no cambia
def test_the_error_content_and_order_are_unchanged(app, proveedor):
    make_error(app, proveedor)

    shape = app.page.evaluate(
        """() => {
          const el = [...document.querySelectorAll('.msg.error')].at(-1);
          return { kids: [...el.children].map(c => c.tagName.toLowerCase() + '.' + (c.className || '')).filter(x => !x.includes('msg-copy')),
                   kind: el.dataset.errorKind, summary: el.querySelector('summary').textContent,
                   technical: el.querySelector('code').textContent, hasRetry: !!el.querySelector('.btn-reset') };
        }"""
    )

    assert shape["summary"] == "Detalle técnico"
    assert shape["technical"].startswith("MODELO-LIMITE · HTTP 429")
    assert shape["kind"] == "other"
    assert any("msg-error-technical" in k for k in shape["kids"])


# ------------------------------------------------------------------------------------------------------------------ REQ-009: tokens
def test_the_semaphore_red_does_not_change_between_themes_but_the_error_ink_does(app):
    def token(name: str) -> str:
        return app.page.evaluate(
            "(n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim().toLowerCase()", name
        )

    signal = {}
    ink = {}
    for theme in THEMES:
        set_theme(app, theme)
        signal[theme] = token("--signal-red")
        ink[theme] = token("--error-ink")

    assert len(set(signal.values())) == 1, f"el rojo del semaforo no cambia entre temas: {signal}"
    assert all(ink.values()), f"existe --error-ink en los tres temas: {ink}"
    assert ink["light"] != ink["dark"], "en el tema claro el rojo es mas oscuro para que se lea"
    assert ink["dark"] == ink["dark-blue"]
