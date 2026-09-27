"""E2E de que las formulas LaTeX con subindices no se corrompen al pasar por
el parser de markdown propio de Vision (spec `fix-latex-corrompido-por-
markdown`, APPROVED 2026-09-26).

Bug real reportado por el usuario con evidencia (2 respuestas reales de
Vision): una formula LaTeX con un subindice tipo "_s" (\text{Cobertura}_s,
T_s, z_0) se mostraba CRUDA en el chat -- causa raiz confirmada leyendo el
codigo: `renderInline()` (markdown.js) interpreta "_x_" como *cursiva* de
markdown, corriendo ANTES de que KaTeX vea los delimitadores LaTeX. Estos
tests verifican en un Chromium REAL que:
1) KaTeX efectivamente renderiza la formula (aparece el elemento .katex,
   sin backslashes de LaTeX crudo visibles en el texto).
2) El texto copiado (boton 📋) conserva el LaTeX/subindices intactos.
3) Markdown normal (cursiva/negrita fuera de formulas) sigue funcionando.
4) Un "$..." dentro de un bloque de codigo no se toca.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from e2e_support import FakeWhisper, launch_chromium, open_app, serve_backend

pytestmark = pytest.mark.e2e

MODEL = "claude-sonnet-4-6"


class FakeProvider:
    """Cliente del proveedor de IA falso que devuelve un texto fijo (parametrizado por
    test via monkeypatch de `_response_text`)."""

    def __init__(self):
        self.whisper = FakeWhisper()
        self.audio = SimpleNamespace(transcriptions=self.whisper)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))
        self.response_text = ""

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

    def _create(self, **kwargs):
        delta = SimpleNamespace(content=self.response_text, tool_calls=[])
        stop = SimpleNamespace(content=None, tool_calls=[])
        return iter(
            [
                SimpleNamespace(
                    choices=[SimpleNamespace(delta=delta, finish_reason=None)]
                ),
                SimpleNamespace(
                    choices=[SimpleNamespace(delta=stop, finish_reason="stop")]
                ),
            ]
        )


@pytest.fixture(scope="module")
def proveedor():
    return FakeProvider()


@pytest.fixture(scope="module")
def base_url(proveedor):
    from app.llm import loop as loop_module

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


def send_and_wait_response(app, proveedor, text):
    proveedor.response_text = f"[[TIPO: conversacional]]{text}"
    app.page.fill("#chat-input", "hola")
    app.page.click("#send-btn")
    app.page.wait_for_selector(".msg.assistant:not(.streaming) .msg-copy-btn")


def read_clipboard(app) -> str:
    return app.page.evaluate("navigator.clipboard.readText()")


def get_msg_text_html(app) -> str:
    return app.page.evaluate(
        "document.querySelector('.msg.assistant:not(.streaming) .msg-text').innerHTML"
    )


def get_visible_katex_html(app) -> str:
    """Solo la parte VISIBLE de lo que renderiza KaTeX (`.katex-html`) --
    a diferencia de `get_msg_text_html`, este NO incluye
    `.katex-mathml annotation`, que es un `<span>` con `clip-path:
    inset(50%)` (oculto visualmente, mecanismo de accesibilidad de KaTeX
    para lectores de pantalla) donde KaTeX SIEMPRE re-incrusta el LaTeX
    fuente original con backslashes -- verificar el string ahi daria un
    falso positivo de "no se renderizo", cuando en realidad si se
    renderizo, solo que la fuente tambien queda anotada (oculta) para
    accesibilidad."""
    return app.page.eval_on_selector_all(
        ".msg.assistant:not(.streaming) .msg-text .katex-html",
        "els => els.map(e => e.innerHTML).join('')",
    )


def test_formula_with_single_subscript_renders_with_katex(app, proveedor):
    """REQ-001: $T_s = \\lfloor T_s \\rfloor$ (un subindice) debe renderizarse
    con KaTeX -- nunca quedar como texto crudo con backslashes VISIBLES.
    Nota: KaTeX SIEMPRE re-incrusta el LaTeX fuente en un
    `<annotation>` dentro de `.katex-mathml` (oculto, `clip-path:
    inset(50%)`, para lectores de pantalla) -- ese `<annotation>` sigue
    teniendo los backslashes a proposito, no es un bug; lo que importa es
    que la parte VISIBLE (`.katex-html`) los haya interpretado."""
    send_and_wait_response(
        app, proveedor, r"La cobertura es $T_s = \lfloor T_s \rfloor$ horas."
    )
    app.page.wait_for_selector(
        ".msg.assistant:not(.streaming) .msg-text .katex", timeout=5000
    )
    visible = get_visible_katex_html(app)
    assert "\\lfloor" not in visible, (
        "no debe quedar LaTeX crudo VISIBLE -- KaTeX debe haberlo interpretado"
    )
    assert "\\text" not in visible
    assert "<em>" not in get_msg_text_html(app), (
        "el _s no debe haberse interpretado como cursiva de markdown"
    )


def test_formula_with_underscore_subscript_inside_text_command(app, proveedor):
    """REQ-001: caso EXACTO reportado por el usuario --
    \\text{Cobertura}_s dentro de $$...$$ (display math) debe renderizarse
    completo, sin que el "_s" se coma como cursiva. Se verifica contra la
    parte VISIBLE de KaTeX (ver docstring del test anterior)."""
    send_and_wait_response(
        app,
        proveedor,
        r"$$\text{Cobertura}_s = \left\lfloor T_s \right\rfloor \text{ h}$$",
    )
    app.page.wait_for_selector(
        ".msg.assistant:not(.streaming) .msg-text .katex", timeout=5000
    )
    visible = get_visible_katex_html(app)
    assert "\\text{Cobertura}" not in visible
    assert "<em>" not in get_msg_text_html(app), (
        "el _s no debe haberse interpretado como apertura de cursiva"
    )


def test_multiple_formulas_with_subscripts_in_same_message(app, proveedor):
    """REQ-002: dos formulas separadas, cada una con su propio subindice, no
    deben interpretarse como un unico par de cursiva que abarca todo el
    texto entre medio."""
    send_and_wait_response(
        app,
        proveedor,
        r"Primero calculamos $z_0$ y despues $z_k$ para el resultado final.",
    )
    app.page.wait_for_timeout(400)  # debounce de typesetMath (250ms)
    katex_count = app.page.eval_on_selector_all(
        ".msg.assistant:not(.streaming) .msg-text .katex", "els => els.length"
    )
    assert katex_count == 2, "ambas formulas deben renderizarse por separado con KaTeX"
    html = get_msg_text_html(app)
    assert "<em>" not in html


def test_copy_button_preserves_formula_with_subscript(app, proveedor):
    """REQ-003: el texto copiado (📋) debe conservar el subindice intacto --
    nunca perder el "_" (el bug real corrompia el HTML ANTES de que
    cualquier boton de copiar entrara en juego). Con la spec
    `copiar-formulas-unicode` (tambien de esta sesion) ya activa, el
    resultado esperado no es el LaTeX crudo `$T_s = 5$` sino la version ya
    convertida a Unicode real -- "Tₛ" (subindice Unicode autentico), que es
    la prueba mas fuerte posible de que el "_" nunca se perdio: si el bug
    original siguiera presente, el resultado habria sido el ilegible "Ts"
    (sin subindice) en vez de "Tₛ" (con subindice Unicode real)."""
    send_and_wait_response(app, proveedor, r"El resultado es $T_s = 5$ horas exactas.")
    app.page.click(".msg.assistant:not(.streaming) .msg-copy-btn:not(.msg-play-btn)")
    app.page.wait_for_function(
        "() => document.querySelector('.msg.assistant:not(.streaming) .msg-copy-btn:not(.msg-play-btn)').textContent === '✓'"
    )
    copied = read_clipboard(app)
    assert "T\u209b = 5" in copied, (
        f"el subindice debe preservarse (como Unicode real), se copio: {copied!r}"
    )
    assert "Ts = 5" not in copied, (
        "si aparece 'Ts' sin subindice, el '_' se perdio -- bug original reproducido"
    )


def test_normal_markdown_emphasis_outside_formulas_still_works(app, proveedor):
    """REQ-004: cursiva/negrita normal FUERA de cualquier formula debe
    seguir funcionando exactamente igual (cero regresion)."""
    send_and_wait_response(
        app, proveedor, "Esto es *cursiva* y esto es **negrita** normal."
    )
    app.page.wait_for_selector(".msg.assistant:not(.streaming) .msg-text em")
    html = get_msg_text_html(app)
    assert "<em>cursiva</em>" in html
    assert "<strong>negrita</strong>" in html


def test_code_block_with_dollar_sign_is_not_touched_as_formula(app, proveedor):
    """REQ-005: un "$x_1$" DENTRO de un bloque de codigo no debe
    interpretarse como formula real ni pasar por KaTeX."""
    send_and_wait_response(app, proveedor, "Ejemplo:\n```python\nprint('$x_1$')\n```")
    app.page.wait_for_selector(".msg.assistant:not(.streaming) .msg-text .md-codeblock")
    katex_count = app.page.eval_on_selector_all(
        ".msg.assistant:not(.streaming) .msg-text .katex", "els => els.length"
    )
    assert katex_count == 0, (
        "el contenido de un bloque de codigo no debe renderizarse como formula"
    )
    html = get_msg_text_html(app)
    assert (
        "$x_1$" in html or "x_1" in html
    )  # el codigo se muestra tal cual, sin interpretar


def test_formula_inside_list_item_renders_correctly(app, proveedor):
    """REQ-006: una formula con subindice dentro de un item de lista debe
    convivir bien con el renderizado de listas (ninguno de los 2 mecanismos
    se pisa). Se verifica el "\\frac" contra la parte VISIBLE de KaTeX
    (ver docstring de test_formula_with_single_subscript_renders_with_katex
    -- el `<annotation>` oculto de accesibilidad SIEMPRE conserva el LaTeX
    fuente a proposito)."""
    send_and_wait_response(
        app, proveedor, r"- La energía es $E_k = \frac{1}{2}mv^2$ en julios"
    )
    app.page.wait_for_selector(".msg.assistant:not(.streaming) .msg-text li")
    app.page.wait_for_selector(
        ".msg.assistant:not(.streaming) .msg-text .katex", timeout=5000
    )
    html = get_msg_text_html(app)
    assert "<li>" in html
    assert "\\frac" not in get_visible_katex_html(app)
