"""spec dashboards-autonomos-sesion: tool `exportar_dashboard`.

- Unit (rapidos, sin navegador): reglas estaticas REQ-003, secretos REQ-006,
  slug/desambiguacion REQ-002, inyeccion `data-vendor`, prompt REQ-001/REQ-005.
- Integracion con Chromium/Edge REAL (marcador `e2e`): los casos de la tabla
  "Casos de aceptacion" de la spec (valido, wrapper invalido, vacio, con
  fetch/CDN/secreto y nombre repetido) y que la validacion corre sin red.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.persistence import master_index
from app.persistence import service as persistence
from app.tools import dashboard_export as de
from app.tools.question import _find_recommended_index

DATA = '<script type="application/json" id="datos">{"a": [1, 2, 3]}</script>'
TEXT = "Stock de playas por sector: " + "informacion visible del tablero " * 4


def page(body: str = "", head: str = "", script: str = "") -> str:
    return f"<!doctype html><html><head><meta charset='utf-8'>{head}</head><body>{body}{DATA}<script>{script}</script></body></html>"


# ---------------------------------------------------------------------------
# REQ-002: slug y nombre unico
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("nombre", "slug"),
    [
        ("Stock de Playas", "stock-de-playas"),
        ("Cobertura: Ñandú / Día 3", "cobertura-nandu-dia-3"),
        ("  ../../etc/passwd  ", "etc-passwd"),
        ("???", "dashboard"),
        ("a" * 200, "a" * 60),
    ],
)
def test_slugify(nombre, slug):
    assert de.slugify(nombre) == slug


def test_slug_never_contains_separators_or_dotdot():
    for nombre in ("..\\..\\x", "a/b", "con:colon", "x\\y"):
        s = de.slugify(nombre)
        assert not any(c in s for c in "\\/:.") and ".." not in s


def test_unique_target_never_overwrites(tmp_path):
    (tmp_path / "stock.html").write_text("1")
    assert de.unique_target(tmp_path, "stock").name == "stock (1).html"
    (tmp_path / "stock (1).html").write_text("2")
    assert de.unique_target(tmp_path, "stock").name == "stock (2).html"
    assert de.unique_target(tmp_path, "otro").name == "otro.html"


# ---------------------------------------------------------------------------
# REQ-003: reglas estaticas
# ---------------------------------------------------------------------------
def test_valid_page_has_no_problems():
    assert (
        de.validate_standalone_html(
            page(
                body="<h1>Tablero</h1>",
                script="const d=JSON.parse(document.getElementById('datos').textContent);",
            )
        )
        == []
    )


@pytest.mark.parametrize(
    ("script", "fragment"),
    [
        ("fetch('datos.json')", "fetch("),
        ("var x=new XMLHttpRequest()", "XMLHttpRequest"),
        ("import('./m.js')", "import("),
        ("new WebSocket('ws://x')", "WebSocket"),
        ("new EventSource('/e')", "EventSource"),
    ],
)
def test_forbidden_js_apis_are_reported(script, fragment):
    problems = de.validate_standalone_html(page(script=script))
    assert any(fragment in p and "regla 3" in p for p in problems), problems


def test_cdn_script_is_reported():
    html = page(head='<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>')
    assert any("regla 4" in p for p in de.validate_standalone_html(html))


def test_protocol_relative_and_css_urls_are_reported():
    assert any(
        "regla 4" in p
        for p in de.validate_standalone_html(
            page(head='<link rel="stylesheet" href="//fonts.x/a.css">')
        )
    )
    assert any(
        "regla 4" in p
        for p in de.validate_standalone_html(
            page(head="<style>@import url('https://x/a.css');</style>")
        )
    )
    assert any(
        "regla 4" in p
        for p in de.validate_standalone_html(
            page(head="<style>a{background:url(//x/i.png)}</style>")
        )
    )


def test_relative_file_reference_is_reported():
    html = page(head='<script src="app.js"></script>')
    assert any("relativo" in p for p in de.validate_standalone_html(html))


def test_data_uri_and_anchor_are_allowed():
    html = page(body='<a href="#top">x</a><img src="data:image/png;base64,AAAA">')
    assert de.validate_standalone_html(html) == []


def test_module_script_with_file_import_is_reported():
    html = page(head='<script type="module">import {a} from "./a.js";</script>')
    assert any("regla 6" in p for p in de.validate_standalone_html(html))


def test_fetch_text_inside_json_data_is_not_code():
    html = "<html><body><script type='application/json' id='datos'>{\"nota\": \"usar fetch( aqui\"}</script></body></html>"
    assert de.validate_standalone_html(html) == []


def test_unescaped_close_tag_inside_json_is_reported():
    html = "<html><body><script type='application/json' id='datos'>{\"x\": \"</div>\"}</script></body></html>"
    assert any("regla 2" in p for p in de.validate_standalone_html(html))


def test_empty_html_and_oversize_are_reported():
    assert de.validate_standalone_html("   ") == ["el HTML esta vacio"]
    big = "<html><body>" + "x" * (de.MAX_BYTES + 10) + "</body></html>"
    assert any("25 MB" in p for p in de.validate_standalone_html(big))


# ---------------------------------------------------------------------------
# REQ-006: secretos
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "line",
    [
        'const cfg = {client_secret: "abc123XYZ789"};',
        "password = hunter2secret",
        'api_key: "sk-1234567890abcdef"',
        "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.abc.def",
        "-----BEGIN RSA PRIVATE KEY-----",
        "scott/tiger123@ORCLDB",
        "headers['X-Jarvis-Token'] = t",
    ],
)
def test_secret_patterns_are_detected(line):
    assert de.find_secrets(f"<html>\n<script>\n{line}\n</script></html>")


def test_secret_report_never_shows_the_value():
    out = " ".join(
        de.find_secrets('<script>var client_secret = "SuperSecretValue123";</script>')
    )
    assert "SuperSecretValue123" not in out and "línea 1" in out


def test_clean_dashboard_has_no_secrets():
    assert de.find_secrets(page(body="<h1>Tablero de tokens de stock</h1>")) == []


# ---------------------------------------------------------------------------
# REQ-003 regla 5: data-vendor
# ---------------------------------------------------------------------------
def test_data_vendor_marker_is_replaced_by_chartjs_with_license():
    out, errors = de.inject_vendor('<html><script data-vendor="chart"></script></html>')
    assert errors == [] and "Chart.js v4" in out and "data-vendor-inlined" in out
    assert 'data-vendor="chart"' not in out


def test_unknown_vendor_is_an_explicit_error():
    _, errors = de.inject_vendor('<script data-vendor="nope"></script>')
    assert errors and "nope" in errors[0]


def test_injected_chartjs_does_not_trip_the_static_rules():
    out, _ = de.inject_vendor(page(head='<script data-vendor="chart"></script>'))
    assert de.validate_standalone_html(out) == []


# ---------------------------------------------------------------------------
# REQ-001 / REQ-005: prompt y pregunta
# ---------------------------------------------------------------------------
def test_export_question_without_recommended_suffix_is_never_auto_answered():
    # La pregunta que indica el prompt no lleva "(Recomendada)": el loop no la contesta solo.
    options = [{"label": "Si, guardarlo para compartir"}, {"label": "No por ahora"}]
    assert _find_recommended_index(options) is None


def test_system_prompt_names_the_tool_the_marker_and_the_rules():
    from app.llm.loop import _build_system_prompt

    p = _build_system_prompt("sid-prompt-dashboards")
    assert "exportar_dashboard" in p and 'data-vendor="chart"' in p
    assert "frontend" in p and "vendor" in p  # ruta absoluta de las librerias
    assert "fetch(" in p and "(Recomendada)" in p
    assert "NUNCA digas" in p  # no anunciar "listo" sin OK
    assert "bash" in p  # datos grandes vía script


def test_tool_is_registered_and_metadata():
    from app.tools import get_registry

    t = get_registry().get("exportar_dashboard")
    assert t is not None and not t.dangerous
    assert set(t.parameters["required"]) == {"path", "nombre"}


# ---------------------------------------------------------------------------
# Tool sin navegador: errores antes de abrirlo
# ---------------------------------------------------------------------------
class _Session:
    def __init__(self, sid):
        self.session_id = sid
        self.events: list[tuple[str, dict]] = []

    def push_event(self, kind, data):
        self.events.append((kind, data))


def _own(sid: str, project: Path) -> _Session:
    master_index.set_project_path(
        sid,
        str(project),
        windows_user_created=f"{persistence.current_windows_user()}_20260929120000",
    )
    return _Session(sid)


def run(args, session):
    return asyncio.run(de._export_execute(args, session))


def test_without_project_folder_it_asks_for_one(tmp_path, tmp_master_db):
    src = tmp_path / "d.html"
    src.write_text(page(body=TEXT), encoding="utf-8")
    s = _Session("sin-carpeta")

    out = run({"path": str(src), "nombre": "X"}, s)

    assert (
        out.startswith("NO EXPORTADO")
        and ("project-folder-required", {"action": "exportar_dashboard"}) in s.events
    )


def test_static_failures_do_not_open_the_browser_nor_leave_files(
    tmp_path, tmp_master_db, monkeypatch
):
    called = []
    monkeypatch.setattr(de, "_validate_in_browser", lambda p: called.append(p))
    project = tmp_path / "proj"
    project.mkdir()
    s = _own("estatico", project)
    src = tmp_path / "d.html"
    src.write_text(page(body=TEXT, script="fetch('x')"), encoding="utf-8")

    out = run({"path": str(src), "nombre": "Con fetch"}, s)

    assert out.startswith("NO EXPORTADO") and "regla 3" in out
    assert called == []
    assert (
        not list((project / "browser").glob("*"))
        if (project / "browser").exists()
        else True
    )


def test_secret_blocks_export_and_does_not_echo_it(tmp_path, tmp_master_db):
    project = tmp_path / "proj"
    project.mkdir()
    s = _own("secreto", project)
    src = tmp_path / "d.html"
    src.write_text(
        page(body=TEXT, script='var client_secret = "abc123SECRETO";'), encoding="utf-8"
    )

    out = run({"path": str(src), "nombre": "S"}, s)

    assert out.startswith("NO EXPORTADO") and "abc123SECRETO" not in out


def test_missing_args_and_missing_file(tmp_path, tmp_master_db):
    project = tmp_path / "proj"
    project.mkdir()
    s = _own("args", project)
    assert "falta" in run({"nombre": "x"}, s)
    assert "falta" in run({"path": "x.html"}, s)
    assert "no encuentro" in run(
        {"path": str(tmp_path / "nada.html"), "nombre": "x"}, s
    )
    (tmp_path / "a.txt").write_text("x")
    assert ".html" in run({"path": str(tmp_path / "a.txt"), "nombre": "x"}, s)


def test_failed_validation_leaves_no_temp_file(tmp_path, tmp_master_db, monkeypatch):
    async def _fail(p):
        return False, ["pageerror: SyntaxError"], ""

    monkeypatch.setattr(de, "_validate_in_browser", _fail)
    project = tmp_path / "proj"
    project.mkdir()
    s = _own("falla", project)
    src = tmp_path / "d.html"
    src.write_text(page(body=TEXT), encoding="utf-8")

    out = run({"path": str(src), "nombre": "Roto"}, s)

    assert (
        out.startswith("NO EXPORTADO")
        and "SyntaxError" in out
        and "2 reintentos" in out
    )
    assert list((project / "browser").iterdir()) == []


def test_successful_export_names_never_overwrite_and_report_path(
    tmp_path, tmp_master_db, monkeypatch
):
    async def _ok(p):
        return True, [], "validado con test"

    monkeypatch.setattr(de, "_validate_in_browser", _ok)
    project = tmp_path / "proj"
    project.mkdir()
    s = _own("exito", project)
    src = tmp_path / "d.html"
    src.write_text(page(body=TEXT), encoding="utf-8")

    first = run({"path": str(src), "nombre": "Stock de Playas"}, s)
    second = run({"path": str(src), "nombre": "Stock de Playas"}, s)

    assert first.startswith("OK: ") and second.startswith("OK: ")
    assert sorted(p.name for p in (project / "browser").iterdir()) == [
        "stock-de-playas (1).html",
        "stock-de-playas.html",
    ]
    assert str(project / "browser" / "stock-de-playas.html") in first


def test_export_does_not_touch_the_live_preview_folder(
    tmp_path, tmp_master_db, monkeypatch
):
    # El export vive en <carpeta-sesion>/browser/, NO en backend/browser/<sid>/
    # (por eso no dispara la auto-apertura del Navegador Integrado).
    from app.browser_workspace import find_index_file

    async def _ok(p):
        return True, [], "ok"

    monkeypatch.setattr(de, "_validate_in_browser", _ok)
    project = tmp_path / "proj"
    project.mkdir()
    s = _own("sin-preview", project)
    src = tmp_path / "d.html"
    src.write_text(page(body=TEXT), encoding="utf-8")

    run({"path": str(src), "nombre": "Aparte"}, s)

    assert find_index_file("sin-preview") is None


# ---------------------------------------------------------------------------
# REQ-004: casos de aceptacion con un navegador REAL (Edge o Chromium)
# ---------------------------------------------------------------------------
CHART_OK = (
    "<!doctype html><html><head><meta charset='utf-8'><title>Stock</title></head><body>"
    "<h1>Stock de playas</h1><div id='t'></div><canvas id='c' width='300' height='150'></canvas>"
    '<script type=\'application/json\' id=\'datos\'>{"labels":["A","B"],"values":[3,5]}</script>'
    "<script data-vendor='chart'></script>"
    "<script>const d=JSON.parse(document.getElementById('datos').textContent);"
    "document.getElementById('t').textContent='Sectores: '+d.labels.join(', ')+' - datos cargados desde el JSON embebido';"
    "new Chart(document.getElementById('c'),{type:'bar',data:{labels:d.labels,datasets:[{label:'Stock',data:d.values}]},options:{animation:false}});"
    "</script></body></html>"
)


@pytest.mark.e2e
def test_valid_dashboard_with_chartjs_is_exported(tmp_path, tmp_master_db):
    project = tmp_path / "proj"
    project.mkdir()
    s = _own("real-ok", project)
    src = tmp_path / "d.html"
    src.write_text(CHART_OK, encoding="utf-8")

    out = run({"path": str(src), "nombre": "Stock de Playas"}, s)

    assert out.startswith("OK: "), out
    final = project / "browser" / "stock-de-playas.html"
    assert final.exists() and "Chart.js v4" in final.read_text(encoding="utf-8")
    assert not list((project / "browser").glob(".validando-*"))


@pytest.mark.e2e
def test_invalid_iife_wrapper_is_caught_by_the_browser(tmp_path, tmp_master_db):
    project = tmp_path / "proj"
    project.mkdir()
    s = _own("real-iife", project)
    src = tmp_path / "d.html"
    src.write_text(
        page(
            body="<h1>" + TEXT + "</h1>",
            script="(function(d)=>{ document.title='x'; })({});",
        ),
        encoding="utf-8",
    )

    out = run({"path": str(src), "nombre": "Wrapper roto"}, s)

    # Playwright informa el MENSAJE del SyntaxError ("Unexpected token '=>'"),
    # no el nombre de la clase: lo que importa es que sea un pageerror.
    assert (
        out.startswith("NO EXPORTADO")
        and "pageerror" in out
        and "Unexpected token" in out
    )
    assert list((project / "browser").iterdir()) == []


@pytest.mark.e2e
def test_empty_body_is_rejected_as_not_rendered(tmp_path, tmp_master_db):
    project = tmp_path / "proj"
    project.mkdir()
    s = _own("real-vacio", project)
    src = tmp_path / "d.html"
    src.write_text("<!doctype html><html><body></body></html>", encoding="utf-8")

    out = run({"path": str(src), "nombre": "Vacio"}, s)

    assert out.startswith("NO EXPORTADO") and "sin contenido" in out.replace(
        "no se ve contenido renderizado", "sin contenido"
    )
    assert list((project / "browser").iterdir()) == []


@pytest.mark.e2e
def test_console_error_makes_validation_fail(tmp_path, tmp_master_db):
    project = tmp_path / "proj"
    project.mkdir()
    s = _own("real-console", project)
    src = tmp_path / "d.html"
    src.write_text(
        page(body="<h1>" + TEXT + "</h1>", script="console.error('algo salio mal');"),
        encoding="utf-8",
    )

    out = run({"path": str(src), "nombre": "Con error"}, s)

    assert out.startswith("NO EXPORTADO") and "console.error" in out


@pytest.mark.e2e
def test_network_attempt_is_blocked_and_reported(tmp_path, tmp_master_db):
    # Una imagen a una URL externa pasa la regla estatica solo si esta armada
    # por JS (no aparece como src literal); el navegador igual debe bloquearla.
    project = tmp_path / "proj"
    project.mkdir()
    s = _own("real-red", project)
    src = tmp_path / "d.html"
    src.write_text(
        page(
            body="<h1>" + TEXT + "</h1>",
            script="var i=new Image(); i.src='ht'+'tps://example.com/pixel.png'; document.body.appendChild(i);",
        ),
        encoding="utf-8",
    )

    out = run({"path": str(src), "nombre": "Red"}, s)

    assert out.startswith("NO EXPORTADO") and "red" in out.lower()


# ---------------------------------------------------------------------------
# Regresion: una validacion trabada NO debe colgar el proceso (bug real de la suite
# completa, 2026-09-29): con `asyncio.to_thread`, `asyncio.run` esperaba para siempre
# al hilo de Playwright al cerrar el executor, aunque `wait_for` ya habia dado timeout.
# ---------------------------------------------------------------------------
def test_a_stuck_validation_times_out_and_does_not_hang_the_process(monkeypatch, tmp_path):
    import threading
    import time

    release = threading.Event()

    def stuck(_path, on_verdict=None):
        release.wait(30)  # simula a Edge trabado
        return True, [], "nunca llega"

    monkeypatch.setattr(de, "_validate_in_browser_sync", stuck)
    monkeypatch.setattr(de, "VALIDATION_TIMEOUT_S", 0.2)
    monkeypatch.setattr(de, "_THREAD_GRACE_S", 0.1)
    started = time.monotonic()

    try:
        # asyncio.run() es justo lo que se colgaba: cierra el loop esperando a los hilos.
        ok, errors, _summary = asyncio.run(de._validate_in_browser(tmp_path / "x.html"))
        elapsed = time.monotonic() - started
    finally:
        release.set()  # libera el hilo de la prueba

    assert ok is False and "superó" in errors[0]
    assert elapsed < 5, f"tardo {elapsed:.1f} s: el cierre del loop esperó al hilo trabado"


def test_a_validation_that_raises_is_reported_not_raised(monkeypatch, tmp_path):
    def boom(_path, on_verdict=None):
        raise RuntimeError("Edge se cerro solo")

    monkeypatch.setattr(de, "_validate_in_browser_sync", boom)

    ok, errors, _summary = asyncio.run(de._validate_in_browser(tmp_path / "x.html"))

    assert ok is False and "la validación falló" in errors[0] and "Edge se cerro solo" in errors[0]


def test_a_finished_validation_still_returns_its_result(monkeypatch, tmp_path):
    monkeypatch.setattr(de, "_validate_in_browser_sync", lambda _p, on_verdict=None: (True, [], "validado con test"))
    assert asyncio.run(de._validate_in_browser(tmp_path / "x.html")) == (True, [], "validado con test")


def test_the_verdict_is_delivered_before_edge_finishes_closing(monkeypatch, tmp_path):
    """Cerrar Edge tarda 5-35 s y no cambia el veredicto: el export NO debe esperarlo. Antes un export
    correcto tardaba medio minuto y en la corrida completa superaba el limite de 60 s de la prueba."""
    import threading
    import time

    release = threading.Event()

    def slow_close(_path, on_verdict=None):
        on_verdict((True, [], "validado con test"))  # ya se midio la pagina...
        release.wait(30)  # ...pero cerrar Edge tarda (y la funcion real no devuelve hasta terminar de cerrar)
        return True, [], "validado con test"

    monkeypatch.setattr(de, "_validate_in_browser_sync", slow_close)
    started = time.monotonic()
    try:
        result = asyncio.run(de._validate_in_browser(tmp_path / "x.html"))
        elapsed = time.monotonic() - started
    finally:
        release.set()

    assert result == (True, [], "validado con test")
    assert elapsed < 5, f"tardo {elapsed:.1f} s: el export esperó a que Edge terminara de cerrarse"


@pytest.mark.e2e
def test_the_real_validator_reports_its_verdict_before_closing_the_browser(tmp_path):
    """Con Edge real: `on_verdict` se llama ANTES de que la funcion termine (o sea, antes de cerrar Edge)."""
    src = tmp_path / "d.html"
    html, problems = de.inject_vendor(CHART_OK)  # igual que la tool: inyecta Chart.js antes de validar
    assert not problems, problems
    src.write_text(html, encoding="utf-8")
    order: list[str] = []

    result = de._validate_in_browser_sync(src, on_verdict=lambda r: order.append("veredicto"))
    order.append("funcion terminada")

    assert order == ["veredicto", "funcion terminada"], order
    assert result[0] is True, result


def test_system_prompt_lists_named_connections_only_when_there_is_something_to_choose(monkeypatch, tmp_path):
    """spec conexiones-con-nombre REQ-005: el bloque aparece con 2+ conexiones del mismo tipo, y el prompt sigue armandose (str.format) sin romperse."""
    import json

    from app import connections as conns
    from app.llm.loop import _build_system_prompt

    path = tmp_path / "connections.json"
    block = lambda n, h: {"type": "oracle", "name": n, "fields": {"oracle_user": "u", "oracle_password": "SECRETO-PROMPT", "oracle_host": h, "oracle_service_name": "s"}}  # noqa: E731
    monkeypatch.setattr(conns, "CONNECTIONS_FILE", path)
    conns.reset_for_tests()
    try:
        path.write_text(json.dumps({"connections": [block("Datamart", "h1")]}), encoding="utf-8")
        assert "Conexiones disponibles" not in _build_system_prompt("sid-conn-1")

        path.write_text(json.dumps({"connections": [block("Datamart", "h1"), block("Planta", "h2")]}), encoding="utf-8")
        prompt = _build_system_prompt("sid-conn-2")
        assert "- Oracle: Datamart (predeterminada), Planta" in prompt
        assert "SECRETO-PROMPT" not in prompt and "h1" not in prompt.split("Conexiones disponibles")[1].split("\n\n")[0]
    finally:
        conns.reset_for_tests()
