"""Corpus de formulas: lo que llega a WhatsApp al copiar una respuesta con LaTeX
(spec `copiar-formulas-unicode`). Pedido del usuario (2026-09-27): "verificá mas
formulas, hacete un listado completo de formulas y verificá", despues de copiar
la formula integral de Cauchy y ver `2\\pi i`, `z_0` y `øperatorname` crudos.

Cada caso es una formula real (algebra, calculo, complejos, estadistica,
logistica, fisica, quimica, conjuntos) con el texto EXACTO esperado. Se prueba el
modulo del frontend (`js/latex-to-unicode.js`) dentro de un Chromium real, con la
misma libreria de simbolos (unicodeit) que usa la app. Criterios de formato:
- fracciones "a/b", con parentesis solo donde hacen falta para no cambiar el
  significado: "1/(2πi)", "(x − μ)/σ", pero "f(z)/(z − z₀)ⁿ⁺¹" y "dy/dx";
- limites de integrales, sumatorias y lim SIEMPRE entre parentesis con flechas
  (formato elegido por el usuario el 2026-09-27): "∫(↓0 ↑2π)", "∑(↓i=1 ↑n)",
  "∮(↓C)", "lim(↓x→0)" -- "∫₀^(2π)" se leia como "0 elevado a 2π";
- super/subindices Unicode cuando existen todos los caracteres ("ⁿ⁺¹", "z₀");
  si falta alguno, "^(iπ)"; una sola letra sin version Unicode queda "x_π";
- sin espacio despues de una letra griega pegada a otra ("2πi", "Δx"), como se
  ve en pantalla.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from e2e_support import FakeWhisper, launch_chromium, open_app, serve_backend

pytestmark = pytest.mark.e2e

MACRON = chr(0x304)  # x̄
HAT = chr(0x302)  # f̂
ARROW = chr(0x20D7)  # F⃗

CORPUS: list[tuple[str, str, str]] = [
    # --- algebra -----------------------------------------------------------
    ("pitagoras", r"a^2 + b^2 = c^2", "a² + b² = c²"),
    ("cuadratica", r"x = \frac{-b \pm \sqrt{b^2 - 4ac}}{2a}", "x = (−b ± √(b² − 4ac))/(2a)"),
    ("binomio", r"(a+b)^n = \sum_{k=0}^{n} \binom{n}{k} a^{n-k} b^k", "(a+b)ⁿ = ∑(↓k=0 ↑n) C(n, k) aⁿ⁻ᵏ bᵏ"),
    ("logaritmo", r"\log_b(xy) = \log_b x + \log_b y", "log_b(xy) = log_b x + log_b y"),
    ("ln", r"\ln(e^x) = x", "ln(eˣ) = x"),
    ("exponente_fraccion", r"x^{\frac{1}{2}} = \sqrt{x}", "x^(1/2) = √x"),
    ("raiz_cubica", r"\sqrt[3]{27} = 3", "∛27 = 3"),
    ("valor_absoluto", r"|x - a| < \varepsilon", "|x − a| < ε"),
    ("implica", r"x \geq 0 \Rightarrow \sqrt{x^2} = x", "x ≥ 0 ⇒ √(x²) = x"),
    ("para_todo", r"\forall x \in \mathbb{R},\ x^2 \geq 0", "∀x ∈ ℝ, x² ≥ 0"),
    ("interes_compuesto", r"A = P\left(1 + \frac{r}{n}\right)^{nt}", "A = P(1 + r/n)ⁿᵗ"),
    ("suma_gauss", r"\sum_{i=1}^{n} i = \frac{n(n+1)}{2}", "∑(↓i=1 ↑n) i = n(n+1)/2"),
    ("serie_geometrica", r"\sum_{k=0}^{\infty} r^k = \frac{1}{1-r}", "∑(↓k=0 ↑∞) rᵏ = 1/(1−r)"),
    ("factorial", r"n! = n \cdot (n-1)!", "n! = n ⋅ (n−1)!"),
    ("transpuesta", r"(AB)^T = B^T A^T", "(AB)ᵀ = Bᵀ Aᵀ"),
    ("congruencia", r"a \equiv b \pmod{n}", "a ≡ b (mod n)"),
    ("puntos", r"1 + 2 + \cdots + n", "1 + 2 + ⋯ + n"),
    # --- calculo -------------------------------------------------------------
    ("derivada", r"\frac{d}{dx} x^n = n x^{n-1}", "d/dx xⁿ = n xⁿ⁻¹"),
    ("leibniz", r"\frac{dy}{dx} = 2x", "dy/dx = 2x"),
    ("parcial", r"\frac{\partial f}{\partial x}", "∂f/∂x"),
    ("regla_cadena", r"\frac{dz}{dx} = \frac{dz}{dy} \cdot \frac{dy}{dx}", "dz/dx = dz/dy ⋅ dy/dx"),
    ("prima", r"f'(x) = 2x", "f′(x) = 2x"),
    ("limite", r"\lim_{x \to 0} \frac{\sin x}{x} = 1", "lim(↓x→0) (sin x)/x = 1"),
    ("integral_definida", r"\int_a^b f(x)\,dx = F(b) - F(a)", "∫(↓a ↑b) f(x) dx = F(b) − F(a)"),
    ("gauss", r"\int_{-\infty}^{\infty} e^{-x^2}\,dx = \sqrt{\pi}", "∫(↓−∞ ↑∞) e^(−x²) dx = √π"),
    ("taylor", r"e^x = \sum_{n=0}^{\infty} \frac{x^n}{n!}", "eˣ = ∑(↓n=0 ↑∞) xⁿ/n!"),
    ("trigonometria", r"\sin^2\theta + \cos^2\theta = 1", "sin²θ + cos²θ = 1"),
    ("gradiente", r"\nabla f = \left(\frac{\partial f}{\partial x}, \frac{\partial f}{\partial y}\right)", "∇f = (∂f/∂x, ∂f/∂y)"),
    # --- complejos (la respuesta que copio el usuario, mas otras) ---------------
    ("cauchy", r"f(z_0) = \frac{1}{2\pi i} \oint_C \frac{f(z)}{z - z_0}\, dz", "f(z₀) = 1/(2πi) ∮(↓C) f(z)/(z − z₀) dz"),
    (
        "cauchy_derivadas",
        r"f^{(n)}(z_0) = \frac{n!}{2\pi i} \oint_C \frac{f(z)}{(z - z_0)^{n+1}}\, dz",
        "f⁽ⁿ⁾(z₀) = n!/(2πi) ∮(↓C) f(z)/(z − z₀)ⁿ⁺¹ dz",
    ),
    ("cauchy_ejemplo", r"\oint_{|z|=1} \frac{e^z}{z}\, dz = 2\pi i \cdot e^0 = 2\pi i", "∮(↓|z|=1) eᶻ/z dz = 2πi ⋅ e⁰ = 2πi"),
    ("residuos", r"\oint_C f(z)\, dz = 2\pi i \sum_k \operatorname{Res}(f, z_k)", "∮(↓C) f(z) dz = 2πi ∑(↓k) Res(f, zₖ)"),
    ("identidad_euler", r"e^{i\pi} + 1 = 0", "e^(iπ) + 1 = 0"),
    ("forma_polar", r"z = re^{i\theta} = r(\cos\theta + i\sin\theta)", "z = re^(iθ) = r(cos θ + i sin θ)"),
    ("modulo", r"|z| = \sqrt{a^2 + b^2}", "|z| = √(a² + b²)"),
    ("conjugado", r"\overline{z} = a - bi", f"z{MACRON} = a − bi"),
    ("de_moivre", r"(\cos\theta + i\sin\theta)^n = \cos(n\theta) + i\sin(n\theta)", "(cos θ + i sin θ)ⁿ = cos(nθ) + i sin(nθ)"),
    (
        "fourier",
        r"\hat{f}(\xi) = \int_{-\infty}^{\infty} f(x)\, e^{-2\pi i x \xi}\, dx",
        f"f{HAT}(ξ) = ∫(↓−∞ ↑∞) f(x) e^(−2πixξ) dx",
    ),
    (
        "euler_integral",
        r"\int_0^{2\pi} \frac{i e^{i\theta}}{e^{i\theta}} d\theta = \int_0^{2\pi} i\, d\theta = 2\pi i",
        "∫(↓0 ↑2π) (i e^(iθ))/e^(iθ) dθ = ∫(↓0 ↑2π) i dθ = 2πi",
    ),
    # --- limites de integrales (pedido del usuario: "verificá cómo muestra el
    # limite superior y el inferior"): siempre "(↓inferior ↑superior)".
    ("int_letras", r"\int_a^b f(x)\,dx", "∫(↓a ↑b) f(x) dx"),
    ("int_numeros", r"\int_0^1 x^2\,dx", "∫(↓0 ↑1) x² dx"),
    ("int_mayuscula", r"\int_{0}^{T} v(t)\,dt", "∫(↓0 ↑T) v(t) dt"),
    ("int_negativo", r"\int_{-1}^{1} x\,dx", "∫(↓−1 ↑1) x dx"),
    ("int_dos_cifras", r"\int_{0}^{10} 5\,dt = 50", "∫(↓0 ↑10) 5 dt = 50"),
    ("int_suma_en_limite", r"\int_{a}^{b+h} g(x)\,dx", "∫(↓a ↑b+h) g(x) dx"),
    ("int_hasta_e", r"\int_{1}^{e} \frac{1}{x}\,dx", "∫(↓1 ↑e) 1/x dx"),
    ("int_dos_pi", r"\int_0^{2\pi} \sin\theta\,d\theta", "∫(↓0 ↑2π) sin θ dθ"),
    ("int_pi_medios", r"\int_0^{\pi/2} \cos x\,dx", "∫(↓0 ↑π/2) cos x dx"),
    ("int_infinito", r"\int_0^{\infty} e^{-x}\,dx", "∫(↓0 ↑∞) e⁻ˣ dx"),
    ("int_x0", r"\int_{x_0}^{x} f(t)\,dt", "∫(↓x₀ ↑x) f(t) dt"),
    ("int_t1_t2", r"\int_{t_1}^{t_2} Q(t)\,dt", "∫(↓t₁ ↑t₂) Q(t) dt"),
    ("int_region", r"\int_{\Omega} f\,dV", "∫(↓Ω) f dV"),
    ("int_doble", r"\iint_D f(x,y)\,dA", "∬(↓D) f(x,y) dA"),
    ("int_limits", r"\int\limits_a^b f(x)\,dx", "∫(↓a ↑b) f(x) dx"),
    ("int_evaluada", r"\left. F(x) \right|_a^b", "F(x)|ₐᵇ"),
    # --- estadistica ---------------------------------------------------------
    ("media", r"\bar{x} = \frac{1}{n}\sum_{i=1}^{n} x_i", f"x{MACRON} = 1/n ∑(↓i=1 ↑n) xᵢ"),
    ("varianza", r"s^2 = \frac{1}{n-1}\sum_{i=1}^{n}(x_i - \bar{x})^2", f"s² = 1/(n−1) ∑(↓i=1 ↑n) (xᵢ − x{MACRON})²"),
    ("desvio", r"\sigma = \sqrt{\frac{1}{N}\sum_{i=1}^{N}(x_i - \mu)^2}", "σ = √(1/N ∑(↓i=1 ↑N) (xᵢ − μ)²)"),
    ("normal", r"f(x) = \frac{1}{\sigma\sqrt{2\pi}} e^{-\frac{(x-\mu)^2}{2\sigma^2}}", "f(x) = 1/(σ√(2π)) e^(−(x−μ)²/(2σ²))"),
    ("z_score", r"z = \frac{x - \mu}{\sigma}", "z = (x − μ)/σ"),
    ("bayes", r"P(A|B) = \frac{P(B|A)\,P(A)}{P(B)}", "P(A|B) = (P(B|A) P(A))/P(B)"),
    ("esperanza", r"E[X] = \sum_{i} x_i\, p_i", "E[X] = ∑(↓i) xᵢ pᵢ"),
    ("intervalo", r"\bar{x} \pm z_{\alpha/2}\frac{\sigma}{\sqrt{n}}", f"x{MACRON} ± z_(α/2) σ/√n"),
    ("poisson", r"P(X = k) = \frac{\lambda^k e^{-\lambda}}{k!}", "P(X = k) = (λᵏ e^(−λ))/k!"),
    # --- logistica (lo que usa el usuario a diario) ----------------------------
    ("eoq", r"Q^* = \sqrt{\frac{2DS}{H}}", "Q* = √(2DS/H)"),
    ("stock_seguridad", r"SS = z \cdot \sigma_d \cdot \sqrt{L}", "SS = z ⋅ σ_d ⋅ √L"),
    ("stock_seguridad_combinado", r"SS = z\sqrt{L\,\sigma_d^2 + d^2\,\sigma_L^2}", "SS = z√(L σ_d² + d² σ_L²)"),
    ("punto_pedido", r"ROP = d \cdot L + SS", "ROP = d ⋅ L + SS"),
    (
        "cobertura",
        r"\text{Cobertura}_s = \frac{\text{Stock}_s}{\text{Demanda diaria}_s}",
        "Coberturaₛ = Stockₛ/(Demanda diariaₛ)",
    ),
    (
        "rotacion",
        r"\text{Rotación} = \frac{\text{Costo de ventas}}{\text{Inventario promedio}}",
        "Rotación = (Costo de ventas)/(Inventario promedio)",
    ),
    (
        "fill_rate",
        r"\text{Fill rate} = \frac{\text{Unidades entregadas}}{\text{Unidades pedidas}} \times 100\%",
        "Fill rate = (Unidades entregadas)/(Unidades pedidas) × 100%",
    ),
    ("little", r"L = \lambda W", "L = λW"),
    ("costo_total", r"CT = \frac{D}{Q}S + \frac{Q}{2}H", "CT = D/Q S + Q/2 H"),
    ("rendimiento", r"\frac{m_{obtenida}}{m_{teorica}} \times 100", "m_obtenida/m_teorica × 100"),
    # --- fisica --------------------------------------------------------------
    ("einstein", r"E = mc^2", "E = mc²"),
    ("newton", r"\vec{F} = m\vec{a}", f"F{ARROW} = ma{ARROW}"),
    ("heisenberg", r"\Delta x\, \Delta p \geq \frac{\hbar}{2}", "Δx Δp ≥ ℏ/2"),
    ("gas_ideal", r"PV = nRT", "PV = nRT"),
    ("schrodinger", r"i\hbar\frac{\partial}{\partial t}\Psi = \hat{H}\Psi", f"iℏ ∂/∂t Ψ = H{HAT}Ψ"),
    ("temperatura", r"T = 25^{\circ}\mathrm{C}", "T = 25°C"),
    ("coulomb", r"F = k\frac{q_1 q_2}{r^2}", "F = k (q₁ q₂)/r²"),
    # --- quimica (\ce de mhchem) -----------------------------------------------
    ("agua", r"\ce{H2O}", "H₂O"),
    ("acido_sulfurico", r"\ce{H2SO4}", "H₂SO₄"),
    ("fotosintesis", r"\ce{6CO2 + 6H2O -> C6H12O6 + 6O2}", "6CO₂ + 6H₂O → C₆H₁₂O₆ + 6O₂"),
    ("acido_carbonico", r"\ce{CO2 + H2O -> H2CO3}", "CO₂ + H₂O → H₂CO₃"),
    ("iones", r"\ce{Na+ + Cl- -> NaCl}", "Na⁺ + Cl⁻ → NaCl"),
    ("sulfato", r"\ce{SO4^2-}", "SO₄²⁻"),
    ("hierro", r"\ce{Fe^{3+}}", "Fe³⁺"),
    ("equilibrio", r"\ce{N2 + 3H2 <=> 2NH3}", "N₂ + 3H₂ ⇌ 2NH₃"),
    ("hidroxido", r"\ce{Ca(OH)2}", "Ca(OH)₂"),
    # --- conjuntos y logica ----------------------------------------------------
    ("union", r"A \cup B = \{x : x \in A \lor x \in B\}", "A ∪ B = {x : x ∈ A ∨ x ∈ B}"),
    ("vacio", r"A \cap \emptyset = \emptyset", "A ∩ ∅ = ∅"),
    ("naturales", r"n \in \mathbb{N}", "n ∈ ℕ"),
    ("aproximado", r"\pi \approx 3.14159", "π ≈ 3.14159"),
    ("distinto", r"a \neq b", "a ≠ b"),
    ("alineado", r"\begin{aligned} x &= 1 \\ y &= 2 \end{aligned}", "x = 1\ny = 2"),
]

# La respuesta que copio el usuario (boton ⌗, markdown crudo), de punta a punta.
CAUCHY_ANSWER = r"""Una fórmula clásica es la **fórmula integral de Cauchy**. Si $f(z)$ es analítica dentro y sobre una curva cerrada simple $C$, recorrida en sentido antihorario, y $z_0$ es un punto interior de $C$, entonces:

$$
f(z_0) = \frac{1}{2\pi i} \oint_C \frac{f(z)}{z - z_0}\, dz
$$

Su generalización para las derivadas de orden $n$ es:

$$
f^{(n)}(z_0) = \frac{n!}{2\pi i} \oint_C \frac{f(z)}{(z - z_0)^{n+1}}\, dz
$$

## Ejemplo

Tomemos $f(z) = e^z$, $z_0 = 0$ y $C$ la circunferencia $|z| = 1$:

$$
\oint_{|z|=1} \frac{e^z}{z}\, dz = 2\pi i \cdot e^0 = 2\pi i
$$

## Otra relacionada: el teorema de los residuos

$$
\oint_C f(z)\, dz = 2\pi i \sum_k \operatorname{Res}(f, z_k)
$$

Aquí la suma recorre las singularidades $z_k$ que quedan dentro de $C$."""

CAUCHY_EXPECTED = """Una fórmula clásica es la **fórmula integral de Cauchy**. Si f(z) es analítica dentro y sobre una curva cerrada simple C, recorrida en sentido antihorario, y z₀ es un punto interior de C, entonces:

f(z₀) = 1/(2πi) ∮(↓C) f(z)/(z − z₀) dz

Su generalización para las derivadas de orden n es:

f⁽ⁿ⁾(z₀) = n!/(2πi) ∮(↓C) f(z)/(z − z₀)ⁿ⁺¹ dz

## Ejemplo

Tomemos f(z) = eᶻ, z₀ = 0 y C la circunferencia |z| = 1:

∮(↓|z|=1) eᶻ/z dz = 2πi ⋅ e⁰ = 2πi

## Otra relacionada: el teorema de los residuos

∮(↓C) f(z) dz = 2πi ∑(↓k) Res(f, zₖ)

Aquí la suma recorre las singularidades zₖ que quedan dentro de C."""


class _NoChat:
    def __init__(self):
        self.audio = SimpleNamespace(transcriptions=FakeWhisper())

    def get(self, path, cast_to=object):
        return {"data": []}


@pytest.fixture(scope="module")
def page(tmp_path_factory):
    client = _NoChat()
    with serve_backend(client) as url, launch_chromium(tmp_path_factory.mktemp("audio"), url) as browser:
        with open_app(browser, url) as app:
            app.open()
            app.page.evaluate(
                "async () => { window.__toUnicode = (await import('./js/latex-to-unicode.js')).convertLatexFormulasToUnicode; }"
            )
            yield app.page


def convert(page, text: str) -> str:
    return page.evaluate("t => window.__toUnicode(t)", text)


@pytest.mark.parametrize(("latex", "expected"), [c[1:] for c in CORPUS], ids=[c[0] for c in CORPUS])
def test_formula_copies_as_readable_unicode(page, latex, expected):
    assert convert(page, f"$${latex}$$") == expected


def test_no_formula_of_the_corpus_leaves_latex_behind(page):
    """Red de seguridad sobre TODO el corpus: ni barras invertidas, ni llaves de
    LaTeX, ni comandos confundidos por prefijo (`øperatorname`, `łim`)."""
    leftovers = {}
    for name, latex, _ in CORPUS:
        out = convert(page, f"${latex}$")
        bad = [mark for mark in ("\\", "øperator", "łim", "{", "}") if mark in out and not (mark in "{}" and name == "union")]
        if bad:
            leftovers[name] = (out, bad)
    assert leftovers == {}


def test_inline_formula_keeps_the_sentence_around_it(page):
    text = r"Si $z_0$ es interior de $C$, entonces $f(z_0) = 0$ (con $n \geq 1$)."
    assert convert(page, text) == "Si z₀ es interior de C, entonces f(z₀) = 0 (con n ≥ 1)."


def test_the_cauchy_answer_the_user_copied_to_whatsapp(page):
    """Caso real del 2026-09-27: con la version anterior salian `1/(2\\pi i)`,
    `(z - z_0)`, `^n+1` (cambiaba el significado) y `øperatorname{Res}`."""
    assert convert(page, CAUCHY_ANSWER) == CAUCHY_EXPECTED
