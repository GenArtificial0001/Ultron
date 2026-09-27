"""E2E de copiar formulas LaTeX como Unicode legible en un Chromium REAL
contra el backend real (spec `copiar-formulas-unicode`, APPROVED 2026-09-26).

Pedido del usuario: poder copiar una respuesta con formulas matematicas y
pegarla en WhatsApp (u otra app que no interprete LaTeX) sin ver el LaTeX
crudo. Los botones 📋 (texto plano) y ⌗ (markdown crudo) de una respuesta del
asistente deben convertir las formulas via la libreria vendorizada
`unicodeit` antes de copiar al portapapeles.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from e2e_support import FakeWhisper, launch_chromium, open_app, serve_backend

pytestmark = pytest.mark.e2e

MODEL = "claude-sonnet-4-6"

# Respuesta fija con una formula generica (\frac, no soportada directo por
# unicodeit -- ejercita el fallback REQ-003) y una simple (soportada
# directo -- ejercita el camino feliz REQ-001).
RESPONSE_TEXT = (
    "[[TIPO: conversacional]]El rendimiento es "
    "$\\frac{m_{obtenida}}{m_{teorica}} \\times 100$ y tambien "
    "$\\alpha + \\sqrt{2}$."
)


class FakeProvider:
    def __init__(self):
        self.whisper = FakeWhisper()
        self.audio = SimpleNamespace(transcriptions=self.whisper)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

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
        delta = SimpleNamespace(content=RESPONSE_TEXT, tool_calls=[])
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


def send_and_wait_response(app):
    app.page.fill("#chat-input", "hola")
    app.page.click("#send-btn")
    app.page.wait_for_selector(".msg.assistant:not(.streaming) .msg-copy-btn")


def read_clipboard(app) -> str:
    return app.page.evaluate("navigator.clipboard.readText()")


def test_plain_copy_button_converts_generic_fraction_to_readable_slash_format(app):
    """REQ-001/003: \\frac{m_obtenida}{m_teorica} (fraccion generica, sin
    equivalente Unicode fijo) se copia como texto legible con "/" -- nunca
    con backslashes crudos."""
    send_and_wait_response(app)
    app.page.click(".msg.assistant:not(.streaming) .msg-copy-btn:not(.msg-play-btn)")
    app.page.wait_for_function(
        "() => document.querySelector('.msg.assistant:not(.streaming) .msg-copy-btn:not(.msg-play-btn)').textContent === '✓'"
    )
    copied = read_clipboard(app)
    assert "\\frac" not in copied, (
        "no debe quedar LaTeX crudo de \\frac en el texto copiado"
    )
    assert "m_obtenida/m_teorica" in copied
    assert "α" in copied, "\\alpha debe convertirse a la letra griega real"
    assert "√2" in copied, "\\sqrt{2} debe convertirse a √2 (sin llaves residuales)"
    assert "×" in copied, "\\times debe convertirse al signo real"


def test_raw_markdown_copy_button_also_converts_formulas(app):
    """REQ-002: el boton ⌗ (markdown crudo) TAMBIEN convierte -- ya no existe
    forma de copiar el LaTeX original desde la UI."""
    send_and_wait_response(app)
    app.page.click('.msg.assistant:not(.streaming) .msg-copy-btn[title*="markdown"]')
    app.page.wait_for_function(
        "() => document.querySelector('.msg.assistant:not(.streaming) .msg-copy-btn[title*=\"markdown\"]').textContent === '✓'"
    )
    copied = read_clipboard(app)
    assert "\\frac" not in copied
    assert "m_obtenida/m_teorica" in copied


def test_user_message_copy_is_never_converted(app):
    """REQ-005: un mensaje del USUARIO (aunque contenga LaTeX a mano) se
    copia tal cual -- la conversion es exclusiva de respuestas del
    asistente. El boton copiar de un mensaje de usuario vive en
    `.user-turn-controls` (grupo editar/eliminar/copiar), no en
    `.msg-copy-controls` (exclusivo de assistant/error)."""
    app.page.fill("#chat-input", "mi formula es $\\alpha + \\beta$")
    app.page.click("#send-btn")
    app.page.wait_for_selector(".user-turn-controls .user-turn-btn")
    app.page.click(".user-turn-controls .user-turn-btn")
    app.page.wait_for_function(
        "() => document.querySelector('.user-turn-controls .user-turn-btn').textContent === '✓'"
    )
    copied = read_clipboard(app)
    assert copied == "mi formula es $\\alpha + \\beta$", (
        "el mensaje de usuario no debe convertirse"
    )


def test_exponent_with_greek_letters_does_not_leave_stray_caret(app):
    """BUG REAL reportado por el usuario (2026-09-26): "en vez de 2pi me puso
    ²^π y en vez de itheta me puso i\\theta". Causa raiz: unicodeit SI
    soporta exponentes/subindices multi-caracter (ej. "^{2n}" -> "²ⁿ"), pero
    SOLO cuando TODOS los caracteres tienen superindice Unicode individual
    -- letras griegas comunes en la formula de Euler (pi, theta) no lo
    tienen, y unicodeit dejaba un "^" residual roto pegado adelante de la
    letra en vez de fallar limpio. Fix: se detecta el residuo y se cae a un
    fallback legible "^(2*pi*i*theta)" en vez de un caracter roto."""
    from app.llm import loop as loop_module

    def euler_create(**kwargs):
        delta = SimpleNamespace(
            content=r"[[TIPO: conversacional]]La formula de Euler es $e^{2\pi i \theta} = 1$.",
            tool_calls=[],
        )
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

    original = loop_module.get_client
    try:
        fake = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=euler_create))
        )
        loop_module.get_client = lambda *a, **k: fake
        send_and_wait_response(app)
        app.page.click(
            ".msg.assistant:not(.streaming) .msg-copy-btn:not(.msg-play-btn)"
        )
        app.page.wait_for_function(
            "() => document.querySelector('.msg.assistant:not(.streaming) .msg-copy-btn:not(.msg-play-btn)').textContent === '✓'"
        )
        copied = read_clipboard(app)
        # Ningun "^" ni "\theta"/"\pi" crudo debe quedar suelto y roto --
        # o bien se convirtio del todo, o cae al fallback legible "^(...)".
        assert "\\theta" not in copied, (
            f"no debe quedar \\theta crudo, se copio: {copied!r}"
        )
        assert "\\pi" not in copied, f"no debe quedar \\pi crudo, se copio: {copied!r}"
        assert "π" in copied, (
            f"debe contener la letra griega pi real, se copio: {copied!r}"
        )
        assert "θ" in copied, (
            f"debe contener la letra griega theta real, se copio: {copied!r}"
        )
        # El fallback legible es "^(...)" -- un "^" seguido de "(" es valido;
        # lo que NO debe pasar es un "^" pegado directo a otro caracter sin
        # parentesis (la firma exacta del bug: "²^π").
        import re

        assert not re.search(r"\^(?!\()", copied), (
            f"no debe quedar un '^' residual roto, se copio: {copied!r}"
        )
    finally:
        loop_module.get_client = original


def test_subscript_with_greek_letter_does_not_leave_stray_underscore(app):
    """Mismo bug que el test anterior, mas el caso de subindice (no solo
    exponente) -- verificado por separado porque son 2 caminos de codigo
    distintos en unicodeit (super vs sub), aunque comparten el mismo fix."""
    from app.llm import loop as loop_module

    def sub_create(**kwargs):
        delta = SimpleNamespace(
            content=r"[[TIPO: conversacional]]El angulo es $x_{\pi}$.", tool_calls=[]
        )
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

    original = loop_module.get_client
    try:
        fake = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=sub_create))
        )
        loop_module.get_client = lambda *a, **k: fake
        send_and_wait_response(app)
        app.page.click(
            ".msg.assistant:not(.streaming) .msg-copy-btn:not(.msg-play-btn)"
        )
        app.page.wait_for_function(
            "() => document.querySelector('.msg.assistant:not(.streaming) .msg-copy-btn:not(.msg-play-btn)').textContent === '✓'"
        )
        copied = read_clipboard(app)
        assert "\\pi" not in copied, f"no debe quedar \\pi crudo, se copio: {copied!r}"
        assert "π" in copied
        import re

        # 2026-09-27 (convertidor reescrito, ver latex-to-unicode.js): una sola
        # letra sin subindice Unicode queda "x_π", igual que "∮_C" o "σ_d" --
        # notacion limpia. Lo que no puede quedar es un residuo roto como el
        # "_|_z_|₌₁" que dejaba unicodeit: "_" seguido de algo que no sea "(" ni
        # una letra.
        assert "x_π" in copied or "x_(π)" in copied, f"se copio: {copied!r}"
        assert not re.search(r"_(?![(\w])", copied), (
            f"no debe quedar un '_' residual roto, se copio: {copied!r}"
        )
    finally:
        loop_module.get_client = original


def test_exponent_with_only_ascii_chars_still_uses_real_unicode_superscript(app):
    """Caso feliz -- NO debe activar el fallback: un exponente multi-caracter
    donde TODOS los caracteres si tienen superindice Unicode real (ej.
    "2n") debe seguir usando los caracteres Unicode autenticos, sin caer al
    fallback "^(...)" innecesariamente."""
    from app.llm import loop as loop_module

    def happy_create(**kwargs):
        delta = SimpleNamespace(
            content=r"[[TIPO: conversacional]]El termino es $x^{2n}$.", tool_calls=[]
        )
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

    original = loop_module.get_client
    try:
        fake = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=happy_create))
        )
        loop_module.get_client = lambda *a, **k: fake
        send_and_wait_response(app)
        app.page.click(
            ".msg.assistant:not(.streaming) .msg-copy-btn:not(.msg-play-btn)"
        )
        app.page.wait_for_function(
            "() => document.querySelector('.msg.assistant:not(.streaming) .msg-copy-btn:not(.msg-play-btn)').textContent === '✓'"
        )
        copied = read_clipboard(app)
        assert "x\u00b2\u207f" in copied, (
            f"debe usar los superindices Unicode reales x²ⁿ, se copio: {copied!r}"
        )
        assert "^(" not in copied, (
            "el caso feliz no debe caer al fallback -- unicodeit ya resuelve esto bien"
        )
    finally:
        loop_module.get_client = original


def test_greek_letter_exponent_inside_fraction_does_not_leave_raw_latex(app):
    """BUG REAL reportado por el usuario via una copia de WhatsApp real
    (2026-09-26): la integral de Euler completa
    \\int_0^{2\\pi} \\frac{i e^{i\\theta}}{e^{i\\theta}} d\\theta = ...
    dejaba "e^i\\theta" con el backslash crudo DENTRO del resultado de la
    fraccion. Causa raiz: `cleanFracOperand` (usada solo para los operandos
    de \\frac) tenia su propia logica de aplanado de super/subindices,
    separada de `extractSupSub` (que ya arregla este mismo problema FUERA de
    fracciones) -- nunca pasaba el contenido con backslash por unicodeit.
    Fix: `convertSupSubForFracOperand` distingue texto plano (se aplana
    literal, ej. "m_{obtenida}" -> "m_obtenida") de un comando LaTeX real
    con backslash (se resuelve con unicodeit, con el mismo fallback
    "marker(resuelto)" que ya usa extractSupSub)."""
    from app.llm import loop as loop_module

    def euler_integral_create(**kwargs):
        delta = SimpleNamespace(
            content=(
                r"[[TIPO: conversacional]]La integral es "
                r"$\int_0^{2\pi} \frac{i e^{i\theta}}{e^{i\theta}} d\theta = "
                r"\int_0^{2\pi} i \, d\theta = 2\pi i$."
            ),
            tool_calls=[],
        )
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

    original = loop_module.get_client
    try:
        fake = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=euler_integral_create)
            )
        )
        loop_module.get_client = lambda *a, **k: fake
        send_and_wait_response(app)
        app.page.click(
            ".msg.assistant:not(.streaming) .msg-copy-btn:not(.msg-play-btn)"
        )
        app.page.wait_for_function(
            "() => document.querySelector('.msg.assistant:not(.streaming) .msg-copy-btn:not(.msg-play-btn)').textContent === '✓'"
        )
        copied = read_clipboard(app)
        assert "\\theta" not in copied, (
            f"no debe quedar \\theta crudo DENTRO de la fraccion, se copio: {copied!r}"
        )
        assert "\\pi" not in copied, f"no debe quedar \\pi crudo, se copio: {copied!r}"
        assert "\\frac" not in copied, (
            f"no debe quedar \\frac crudo, se copio: {copied!r}"
        )
        # Debe contener multiples apariciones reales de theta y pi (dentro y
        # fuera de la fraccion) -- prueba fuerte de que TODO el backslash se
        # resolvio, no solo el que estaba fuera del \frac.
        assert copied.count("θ") >= 3, (
            f"debe contener theta real varias veces, se copio: {copied!r}"
        )
        assert copied.count("π") >= 2, (
            f"debe contener pi real varias veces, se copio: {copied!r}"
        )
        assert "2πi" in copied.replace(" ", ""), (
            f"el resultado final 2πi debe estar legible, se copio: {copied!r}"
        )
    finally:
        loop_module.get_client = original


def test_plain_text_without_formulas_is_unaffected(app):
    """REQ-006: una respuesta sin ninguna formula se copia exactamente igual
    que antes de esta feature."""
    from app.llm import loop as loop_module

    # Respuesta sin formulas para este test puntual.
    def plain_create(**kwargs):
        delta = SimpleNamespace(
            content="[[TIPO: conversacional]]Hola, sin formulas aca.", tool_calls=[]
        )
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

    original = loop_module.get_client
    try:
        fake = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=plain_create))
        )
        loop_module.get_client = lambda *a, **k: fake
        app.page.fill("#chat-input", "hola")
        app.page.click("#send-btn")
        app.page.wait_for_selector(".msg.assistant:not(.streaming) .msg-copy-btn")
        app.page.click(
            ".msg.assistant:not(.streaming) .msg-copy-btn:not(.msg-play-btn)"
        )
        app.page.wait_for_function(
            "() => document.querySelector('.msg.assistant:not(.streaming) .msg-copy-btn:not(.msg-play-btn)').textContent === '✓'"
        )
        assert read_clipboard(app) == "Hola, sin formulas aca."
    finally:
        loop_module.get_client = original
