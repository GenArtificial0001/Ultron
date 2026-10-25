"""spec informe-tecnico-hablado REQ-012 a REQ-015 (reapertura 2, nivel "Detallado").

El nivel Resumido NO cambia (REQ-001 a REQ-011): estos tests tambien fijan que no se cuelan frases
nuevas ahi. Todo se prueba contra `LiveNarrator` con el codigo mandado de a pedazos, como llega del
modelo."""

from __future__ import annotations

import json

import pytest

from app.tech_report import LiveNarrator, normalize_level

PAGE = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<title>Tablero de Stock</title>
<style>
  :root { --bg: #0b0f14; --accent: #7cc4ff; }
  body { font-family: "Inter", sans-serif; background: var(--bg); }
  .grid { display: grid; grid-template-columns: repeat(4, 1fr); }
  @media (max-width: 700px) { .grid { grid-template-columns: 1fr; } }
</style>
</head>
<body>
<header class="topbar"><h1>Tablero de Stock</h1></header>
<nav class="menu-lateral">enlaces</nav>
<nav class="menu-superior"><a href="#">Inicio</a></nav>
<main>
<section class="kpis"><div class="card">1</div></section>
<section class="detalle"><table><tr><td>x</td></tr></table></section>
</main>
<footer>la empresa - la planta</footer>
<script>
// renderiza la tabla de piezas con el filtro activo
function renderTabla(datos) { return datos; }
const filtrar = (texto) => texto.trim();
document.getElementById("buscar").addEventListener("click", () => filtrar("x"));
</script>
</body>
</html>
"""


def narrate(code, level, name="write", path="C:/demo/tablero.html", size=7):
    raw = json.dumps({"path": path, "content": code})
    n = LiveNarrator(name, level=level)
    out: list[str] = []
    for i in range(0, len(raw), size):
        out.extend(n.feed(raw[i : i + size]))
    return out


# ---------------------------------------------------------------------------
# REQ-012: niveles
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("raw", "expected"),
    [("detallado", "detailed"), ("DETAILED", "detailed"), ("resumido", "summary"), ("summary", "summary"),
     (None, "summary"), ("", "summary"), ("cualquier cosa", "summary"), (3, "summary")],
)
def test_unknown_or_missing_level_is_summary(raw, expected):
    assert normalize_level(raw) == expected


def test_the_default_level_of_the_narrator_is_summary():
    assert LiveNarrator("write").level == "summary"


def test_summary_level_does_not_add_the_new_phrases():
    spoken = narrate(PAGE, "summary")
    joined = " | ".join(spoken).lower()
    for new in ("pie de página", "menú de navegación", "encabezado de la página", "aplico estilos", "defino la función", "conecto el clic", "contenido principal", "defino una sección"):
        assert new not in joined, (new, spoken)


# ---------------------------------------------------------------------------
# REQ-013: estructura y estilos
# ---------------------------------------------------------------------------
def test_detailed_tells_the_structure_in_order_without_repeating():
    spoken = narrate(PAGE, "detailed")
    order = [
        next(i for i, s in enumerate(spoken) if "encabezado" in s.lower() and "armo" in s.lower()),
        next(i for i, s in enumerate(spoken) if "menú de navegación" in s.lower()),
        next(i for i, s in enumerate(spoken) if "pie de página" in s.lower()),
    ]
    assert order == sorted(order), spoken
    assert sum("pie de página" in s.lower() for s in spoken) == 1  # sin repetir
    assert sum("menú de navegación" in s.lower() for s in spoken) == 1


def test_detailed_describes_the_styles_it_detects():
    joined = " | ".join(narrate(PAGE, "detailed")).lower()
    assert "aplico estilos" in joined
    assert "fondo oscuro" in joined  # variable de color oscuro --bg: #0b0f14
    assert "tipografía inter" in joined  # font-family: "Inter"
    assert "grilla" in joined  # display: grid
    assert "celular" in joined  # @media (max-width: 700px)


def test_sections_are_announced_but_not_every_div():
    spoken = narrate(PAGE, "detailed")
    assert any("sección" in s.lower() for s in spoken)
    assert sum("sección" in s.lower() for s in spoken) <= 2  # una por <section>, deduplicada por texto


def test_detailed_speaks_while_the_file_is_being_written_not_after():
    raw = json.dumps({"path": "C:/demo/t.html", "content": PAGE})
    n = LiveNarrator("write", level="detailed")
    cut = raw.index("<footer>")  # el archivo todavia no llego al pie
    said = []
    for i in range(0, cut, 9):
        said.extend(n.feed(raw[i : i + 9]))
    assert any("encabezado" in s.lower() for s in said)
    assert not any("pie de página" in s.lower() for s in said)  # aun no se escribio
    assert not n.closed


def test_secrets_in_styles_or_code_are_still_hidden():
    code = '<style>body{font-family:Arial}</style>\n<script>\n// conecta con la API\nconst token = "sk-abc1234567890SECRETO";\n</script>\n'
    joined = " | ".join(narrate(code, "detailed"))
    assert "SECRETO" not in joined and "sk-abc" not in joined


# ---------------------------------------------------------------------------
# REQ-014: funciones JavaScript
# ---------------------------------------------------------------------------
def test_each_javascript_function_is_named_readably():
    joined = " | ".join(narrate(PAGE, "detailed")).lower()
    assert "defino la función render tabla" in joined
    assert "defino la función filtrar" in joined


def test_a_comment_above_the_function_says_what_it_is_for():
    spoken = narrate(PAGE, "detailed")
    assert "renderiza la tabla de piezas con el filtro activo" in spoken  # el comentario ya se decia
    func = next(s for s in spoken if "render tabla" in s.lower())
    assert ", que renderiza la tabla" in func.lower() and "para renderiza" not in func.lower()


def test_event_listeners_are_announced():
    assert any("conecto el clic" in s.lower() for s in narrate(PAGE, "detailed"))


def test_python_and_arrow_functions_in_other_files():
    py = "# calcula la cobertura por pieza\ndef calcular_cobertura(piezas):\n    return piezas\n"
    spoken = narrate(py, "detailed", path="C:/demo/cobertura.py")
    assert any("defino la función calcular cobertura" in s.lower() for s in spoken)
    # Resumido: solo el comentario, como antes
    assert not any("defino la función" in s.lower() for s in narrate(py, "summary", path="C:/demo/cobertura.py"))


def test_phrases_stay_short():
    long_fn = "function " + "nombreMuyLargo" * 6 + "(a) { return a; }\n"
    for s in narrate(long_fn, "detailed", path="C:/demo/a.js"):
        assert len(s.split()) <= 21  # el tope de ~20 palabras de la spec (+ el "…")


def test_bash_commands_in_detailed_do_not_start_describing_html_structure():
    # Un `bash` que solo escribe texto no debe inventar "pie de pagina" por una palabra suelta.
    spoken = narrate("echo 'el footer del reporte'\n", "detailed", name="bash", path="")
    assert not any("pie de página" in s.lower() for s in spoken)


# ---------------------------------------------------------------------------
# REQ-015: la presentacion del diseno (comentario de cabecera) es la frase `lead`
# ---------------------------------------------------------------------------
def _lead_of(code, level, **kw):
    raw = json.dumps({"path": kw.get("path", "C:/demo/t.html"), "content": code})
    n = LiveNarrator(kw.get("name", "write"), level=level)
    for i in range(0, len(raw), 9):
        n.feed(raw[i : i + 9])
    return n.lead_phrase


def test_the_first_comment_of_a_detailed_file_is_the_lead_phrase():
    code = "<!-- tablero de stock: encabezado, menu e indicadores -->\n<header><h1>X</h1></header>\n<!-- otro comentario -->\n"
    assert _lead_of(code, "detailed") == "tablero de stock: encabezado, menu e indicadores"


def test_summary_and_bash_never_mark_a_lead_phrase():
    code = "<!-- tablero de stock: encabezado y menu -->\n<header></header>\n"
    assert _lead_of(code, "summary") is None
    assert _lead_of("# cuento los archivos del informe\necho hola\n", "detailed", name="bash", path="") is None


def test_a_lead_phrase_never_contains_a_secret():
    code = '<!-- conecta con la API usando password="hunter2hunter2" para el tablero -->\n<header></header>\n'
    lead = _lead_of(code, "detailed")
    assert lead is None or "hunter2" not in lead
