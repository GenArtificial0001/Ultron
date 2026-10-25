"""
Tool `exportar_dashboard` (spec dashboards-autonomos-sesion): convierte un
dashboard HTML ya armado por el modelo en un ENTREGABLE AUTONOMO -- un solo
`.html` que abre sin servidor ni red -- y lo guarda en
`<carpeta-de-la-sesion>/browser/<slug>.html`, previa validacion en codigo.

Por que la validacion vive ACA y no solo en el system prompt (REQ-004): un
modelo puede saltearse pasos. Antecedente real: un dashboard con
`(function(d)=>{` (JS invalido) que impedia renderizar cualquier dato llego
al usuario sin detectarse. Aca ese error aparece como `pageerror` al abrir el
archivo en un navegador real y el export NO se entrega "como bueno".

Dos carpetas que NO hay que mezclar:
- Vista previa `backend/browser/<session_id>/` (temporal, la muestra el
  Navegador Integrado, puede usar `fetch` hacia su origen de datos).
- Export `<carpeta-sesion>/browser/` (este modulo): autonomo, funciona sin
  conexion y NO se borra al eliminar la sesion. Escribir aca nunca dispara la
  auto-apertura del panel (esa deteccion solo mira la vista previa).

Pipeline (en este orden, ver `_export_execute`):
1. Chequeos estaticos REQ-003 (`validate_standalone_html`) y escaneo de
   secretos REQ-006 (`find_secrets`). Si algo falla, no se abre el navegador.
2. Inyeccion del marcador `<script data-vendor="chart"></script>` por el
   contenido de `frontend/vendor/chart.js` (el modelo no copia 200 KB).
3. Validacion en Edge headless (`channel="msedge"`, o el Chromium de
   Playwright), abriendo la copia temporal como `file://` con TODA request de
   red abortada y registrada (REQ-004).
4. Rename atomico del temporal `.validando-<slug>.html` al nombre final solo
   si todo paso.

La tool nunca lanza (regla de las tools): devuelve `OK: <ruta>` o
`NO EXPORTADO: <motivos>` para que el modelo pueda corregir y reintentar.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
import unicodedata
from pathlib import Path
from typing import Any

from app.config import RESOURCE_ROOT
from app.tools.base import Tool

logger = logging.getLogger(__name__)

# REQ-003 regla 7: aviso a partir de 5 MB, tope duro a 25 MB.
WARN_BYTES = 5 * 1024 * 1024
MAX_BYTES = 25 * 1024 * 1024
# REQ-004: la validacion en el navegador tiene un tope de 20 s (un timeout
# cuenta como falla).
VALIDATION_TIMEOUT_S = 20
# Margen para CERRAR Edge despues de medir. Medido 2026-09-29 con Edge real: abrir y
# validar tarda menos de 1 s, pero `browser.close()` tarda entre 5 y 35 s (Edge espera a
# sus procesos hijos). Con solo 10 s de margen la validacion ya habia dado su veredicto y
# aun asi el export la daba por fallida ("la validacion supero 20 s"): un falso rechazo
# de un dashboard perfecto, y de ahi el test intermitente. Un Edge realmente trabado
# sigue cortando: VALIDATION_TIMEOUT_S + este margen.
_THREAD_GRACE_S = 60
# REQ-004 punto 4: minimo de texto visible para considerar que "renderizo".
MIN_VISIBLE_CHARS = 50
# REQ-002: largo maximo del slug del nombre de archivo.
MAX_SLUG_LEN = 60

VENDOR_LIBS = {"chart": "chart.js"}
_VENDOR_MARKER_RE = re.compile(
    r"<script\b[^>]*\bdata-vendor\s*=\s*[\"'](?P<lib>[a-z0-9_-]+)[\"'][^>]*>\s*</script>",
    re.IGNORECASE,
)


def vendor_dir() -> Path:
    """Carpeta `frontend/vendor/` (en dev y en el EXE, via RESOURCE_ROOT)."""
    return RESOURCE_ROOT / "frontend" / "vendor"


# ---------------------------------------------------------------------------
# REQ-002: nombre de archivo
# ---------------------------------------------------------------------------
def slugify(nombre: str) -> str:
    """'Stock de Playas' -> 'stock-de-playas'. Minusculas, sin tildes, espacios
    a '-', solo [a-z0-9-], hasta 60 caracteres. Sin separadores ni '..' por
    construccion (REQ-002/seguridad): nunca puede escapar de `browser/`."""
    base = unicodedata.normalize("NFKD", nombre or "")
    base = "".join(c for c in base if not unicodedata.combining(c)).lower()
    base = re.sub(r"[^a-z0-9]+", "-", base).strip("-")
    base = base[:MAX_SLUG_LEN].strip("-")
    return base or "dashboard"


def unique_target(folder: Path, slug: str) -> Path:
    """`slug.html`, o `slug (1).html`, `slug (2).html`... Nunca pisa."""
    candidate = folder / f"{slug}.html"
    n = 1
    while candidate.exists():
        candidate = folder / f"{slug} ({n}).html"
        n += 1
    return candidate


# ---------------------------------------------------------------------------
# REQ-003: reglas estaticas verificables
# ---------------------------------------------------------------------------
_FORBIDDEN_JS = (
    (re.compile(r"\bfetch\s*\("), "usa fetch("),
    (re.compile(r"\bXMLHttpRequest\b"), "usa XMLHttpRequest"),
    (re.compile(r"\bimport\s*\("), "usa import("),
    (re.compile(r"\bnew\s+WebSocket\b|\bWebSocket\s*\("), "usa WebSocket"),
    (re.compile(r"\bEventSource\b"), "usa EventSource"),
)
# URL externa (o protocolo-relativa) en atributos src/href, url(...) o @import.
_EXTERNAL_ATTR_RE = re.compile(
    r"""\b(?:src|href)\s*=\s*["']\s*(?:https?:)?//""", re.IGNORECASE
)
_EXTERNAL_CSS_RE = re.compile(
    r"""url\(\s*["']?\s*(?:https?:)?//|@import\s+(?:url\()?\s*["']?\s*(?:https?:)?//""",
    re.IGNORECASE,
)
# src/href a archivo RELATIVO (no data:, no ancla '#', no javascript:/mailto:).
_RELATIVE_ATTR_RE = re.compile(
    r"""<(?:script|img|link|iframe|source|audio|video)\b[^>]*?\b(?:src|href)\s*=\s*["'](?!\s*(?:data:|#|https?:|//|javascript:|mailto:))([^"']+)["']""",
    re.IGNORECASE,
)
_MODULE_IMPORT_RE = re.compile(
    r"""<script\b[^>]*\btype\s*=\s*["']module["'][^>]*>(?P<body>.*?)</script>""",
    re.IGNORECASE | re.DOTALL,
)
_MODULE_IMPORT_STMT_RE = re.compile(
    r"""^\s*import\s.+?from\s+["'][^"']+["']""", re.MULTILINE
)


def validate_standalone_html(html: str) -> list[str]:
    """REQ-003: reglas estaticas del HTML autonomo. Devuelve la lista de
    problemas (vacia = cumple). Funcion PURA, sin I/O.

    Se aplica al HTML DESPUES de inyectar `data-vendor` (asi el codigo de
    Chart.js embebido no se evalua contra estas reglas: se sacan los
    <script data-vendor> antes de buscar `fetch(`, que la libreria si
    menciona)."""
    problems: list[str] = []
    if not html or not html.strip():
        return ["el HTML esta vacio"]

    size = len(html.encode("utf-8"))
    if size > MAX_BYTES:
        problems.append(
            f"pesa {size / 1024 / 1024:.1f} MB (tope 25 MB): reducí los datos embebidos"
        )

    # Los bloques <script type="application/json"> son DATOS, no codigo: un
    # texto como "fetch(" dentro de un dato no debe bloquear el export.
    code_html = re.sub(
        r"""<script\b[^>]*\btype\s*=\s*["']application/json["'][^>]*>.*?</script>""",
        "",
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    # La libreria vendorizada ya inyectada es codigo de terceros conocido.
    code_html = re.sub(
        r"""<script\b[^>]*\bdata-vendor-inlined\b[^>]*>.*?</script>""",
        "",
        code_html,
        flags=re.IGNORECASE | re.DOTALL,
    )

    for pattern, msg in _FORBIDDEN_JS:
        if pattern.search(code_html):
            problems.append(f"regla 3: {msg}")

    if _EXTERNAL_ATTR_RE.search(code_html):
        problems.append(
            "regla 4: hay un src/href a una URL externa (http://, https:// o //); "
            "embebé la libreria con data-vendor o inline"
        )
    if _EXTERNAL_CSS_RE.search(code_html):
        problems.append("regla 4: hay url(...) o @import a una URL externa")
    rel = _RELATIVE_ATTR_RE.search(code_html)
    if rel:
        problems.append(
            f"regla 4: referencia un archivo relativo ('{rel.group(1)[:60]}'); "
            "todo tiene que ir inline en un solo .html"
        )

    for m in _MODULE_IMPORT_RE.finditer(code_html):
        if _MODULE_IMPORT_STMT_RE.search(m.group("body")):
            problems.append(
                'regla 6: <script type="module"> con import de archivos (falla bajo file://)'
            )
            break

    # Un dato con "</" dentro del JSON puede cerrar el <script> antes de tiempo.
    for m in re.finditer(
        r"""<script\b[^>]*\btype\s*=\s*["']application/json["'][^>]*>(?P<body>.*?)</script>""",
        html,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        # Si el body contiene "</" el regex lo habria cortado ahi; lo detectamos
        # buscando un cierre de otra etiqueta suelta dentro del bloque.
        if re.search(r"</(?!script)", m.group("body"), flags=re.IGNORECASE):
            problems.append(
                "regla 2: el JSON de datos contiene '</' sin escapar; escribilo como '<\\/'"
            )
            break

    return problems


# ---------------------------------------------------------------------------
# REQ-006: secretos
# ---------------------------------------------------------------------------
_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"""(?i)\b(password|passwd|pwd|client[_-]?secret|api[_-]?key|access[_-]?token|token)\b["']?\s*[:=]\s*["']?[^\s"',;<>]{4,}"""
        ),
        "un valor asignado a password/pwd/client_secret/api_key/token",
    ),
    (re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}"), "un token 'Bearer ...'"),
    (
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
        "una clave privada (BEGIN PRIVATE KEY)",
    ),
    (
        re.compile(r"""\b[A-Za-z0-9_.-]{2,}/[^\s@/'"]{3,}@[A-Za-z0-9._-]{2,}"""),
        "una cadena de conexion usuario/clave@host",
    ),
    (re.compile(r"(?i)X-Jarvis-Token"), "el header X-Jarvis-Token"),
)


def find_secrets(html: str) -> list[str]:
    """REQ-006: patrones con forma de secreto. Devuelve mensajes con la LINEA
    y el TIPO de patron, NUNCA el valor completo. Funcion pura."""
    found: list[str] = []
    lines = html.splitlines()
    for lineno, line in enumerate(lines, start=1):
        for pattern, label in _SECRET_PATTERNS:
            if pattern.search(line):
                msg = f"línea {lineno}: parece {label}"
                if msg not in found:
                    found.append(msg)
    return found[:10]


# ---------------------------------------------------------------------------
# REQ-003 regla 5: marcador data-vendor
# ---------------------------------------------------------------------------
def inject_vendor(html: str) -> tuple[str, list[str]]:
    """Reemplaza `<script data-vendor="chart"></script>` por el contenido de
    `frontend/vendor/chart.js` (con su comentario de licencia). Devuelve
    (html, errores). Un data-vendor desconocido es un error explicito."""
    errors: list[str] = []

    def _sub(m: re.Match[str]) -> str:
        lib = m.group("lib").lower()
        fname = VENDOR_LIBS.get(lib)
        if fname is None:
            errors.append(
                f'data-vendor="{lib}" no existe (disponibles: {", ".join(sorted(VENDOR_LIBS))})'
            )
            return m.group(0)
        path = vendor_dir() / fname
        try:
            code = path.read_text(encoding="utf-8")
        except OSError as exc:
            errors.append(f"no se pudo leer la libreria '{lib}' ({exc})")
            return m.group(0)
        code = code.replace(
            "</script", "<\\/script"
        )  # nunca cierra el <script> por accidente
        return f'<script data-vendor-inlined="{lib}">\n{code}\n</script>'

    return _VENDOR_MARKER_RE.sub(_sub, html), errors


# ---------------------------------------------------------------------------
# REQ-004: validacion en un navegador real
# ---------------------------------------------------------------------------
def _validate_in_browser_sync(
    file_path: Path, on_verdict: Any = None
) -> tuple[bool, list[str], str]:
    """Abre `file_path` como file:// con la red bloqueada. Devuelve
    (ok, errores, resumen). Corre en un hilo aparte (la API sync de Playwright
    no puede correr dentro del loop de asyncio).

    `on_verdict(resultado)`: se llama en cuanto se MIDIO la pagina y ANTES de cerrar Edge. Cerrar Edge
    tarda entre 5 y 35 s (espera a sus procesos hijos) y no cambia el veredicto: esperarlo hacia que
    un export correcto tardara medio minuto y, con la maquina cargada, superara el limite de la
    corrida completa de tests (timeout de 60 s en test_valid_dashboard_with_chartjs_is_exported)."""
    try:
        from playwright.sync_api import Error as PWError
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False, ["playwright no está instalado: no se pudo validar"], ""

    errors: list[str] = []
    with sync_playwright() as pw:
        browser = None
        engine = ""
        for channel in ("msedge", None):
            try:
                browser = (
                    pw.chromium.launch(channel=channel, headless=True)
                    if channel
                    else pw.chromium.launch(headless=True)
                )
                engine = channel or "chromium"
                break
            except PWError as exc:
                logger.debug("No se pudo lanzar %s: %s", channel or "chromium", exc)
        if browser is None:
            return (
                False,
                ["no hay Edge ni Chromium disponibles: no se pudo validar"],
                "",
            )

        try:
            page = browser.new_page()
            blocked: list[str] = []

            def _route(route: Any) -> None:
                url = route.request.url
                # Solo el propio archivo y data:/blob:/about: son legitimos.
                if url.startswith(("file:", "data:", "blob:", "about:")):
                    route.continue_()
                else:
                    blocked.append(url[:120])
                    route.abort()

            page.route("**/*", _route)
            page.on(
                "pageerror", lambda exc: errors.append(f"pageerror: {str(exc)[:200]}")
            )
            page.on(
                "console",
                lambda msg: (
                    errors.append(f"console.error: {msg.text[:200]}")
                    if msg.type == "error"
                    else None
                ),
            )
            page.goto(
                file_path.as_uri(),
                wait_until="load",
                timeout=VALIDATION_TIMEOUT_S * 1000,
            )
            page.wait_for_timeout(1000)  # load + 1 s (REQ-004 punto 4)

            if blocked:
                errors.append(f"intentó acceder a la red: {blocked[0]}")

            visible = page.evaluate(
                """() => {
                    const text = (document.body && document.body.innerText || '').trim().length;
                    const canvas = [...document.querySelectorAll('canvas')]
                        .filter(c => c.width > 0 && c.height > 0).length;
                    const svg = [...document.querySelectorAll('svg')]
                        .filter(s => s.children.length > 0).length;
                    return {text, canvas, svg};
                }"""
            )
        except PWError as exc:
            errors.append(f"el navegador no pudo abrir el archivo: {str(exc)[:200]}")
            failed = (False, errors, "")
            if on_verdict is not None:
                on_verdict(failed)
            browser.close()
            return failed
        verdict = _verdict(errors, visible, engine)
        if on_verdict is not None:
            on_verdict(verdict)  # veredicto ya conocido: no hace falta esperar el cierre de Edge
        browser.close()

    return verdict


def _verdict(
    errors: list[str], visible: dict[str, int], engine: str
) -> tuple[bool, list[str], str]:
    """(ok, errores, resumen) a partir de lo medido en la pagina."""
    rendered = (
        visible["text"] >= MIN_VISIBLE_CHARS
        or visible["canvas"] > 0
        or visible["svg"] > 0
    )
    if not rendered:
        errors.append(
            "no se ve contenido renderizado (menos de 50 caracteres de texto y sin gráfico)"
        )
    summary = (
        f"validado con {engine}: {visible['text']} caracteres visibles, "
        f"{visible['canvas']} canvas, {visible['svg']} svg, 0 errores de consola"
    )
    return (not errors), errors, summary


async def _validate_in_browser(file_path: Path) -> tuple[bool, list[str], str]:
    """REQ-004 punto 5: hilo aparte + timeout (un timeout es falla).

    Bug real (suite completa, 2026-09-29): antes se usaba `asyncio.to_thread`, que
    corre en el executor por defecto. Si Edge se trababa, `wait_for` daba el timeout
    pero el hilo de Playwright seguia vivo, y `asyncio.run` espera a ese hilo al
    cerrar (`shutdown_default_executor`) -- el proceso quedaba colgado para siempre
    en vez de informar la falla. Ahora corre en un hilo PROPIO de tipo daemon: si
    se traba, se abandona (no bloquea el cierre) y el export informa el timeout."""
    loop = asyncio.get_running_loop()
    future: asyncio.Future[tuple[bool, list[str], str]] = loop.create_future()

    def _deliver(result: tuple[bool, list[str], str]) -> None:
        if not future.done():
            future.set_result(result)

    def _push(result: tuple[bool, list[str], str]) -> None:
        try:
            loop.call_soon_threadsafe(_deliver, result)
        except RuntimeError:  # el loop ya se cerro: nadie espera el resultado
            logger.debug("Se descarta el resultado de la validacion: el loop se cerro")

    def _worker() -> None:
        try:
            result = _validate_in_browser_sync(file_path, on_verdict=_push)
        except Exception as exc:  # noqa: BLE001 -- la tool nunca lanza
            logger.warning("La validacion en el navegador fallo", exc_info=True)
            result = (False, [f"la validación falló: {str(exc)[:200]}"], "")
        _push(result)  # si ya se entrego antes de cerrar Edge, `_deliver` lo ignora

    threading.Thread(target=_worker, name="dashboard-validate", daemon=True).start()
    try:
        return await asyncio.wait_for(
            future, timeout=VALIDATION_TIMEOUT_S + _THREAD_GRACE_S
        )
    except asyncio.TimeoutError:
        return False, [f"la validación superó {VALIDATION_TIMEOUT_S} s"], ""


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------
def _resolve_source(path: str, session: Any) -> Path | None:
    from app.tools.filesystem import _resolve  # import diferido: evita ciclo

    try:
        return _resolve(path, session)
    except Exception:  # noqa: BLE001 -- ruta invalida => el caller informa
        return None


async def _export_execute(args: dict, session: Any = None) -> str:
    path = (args.get("path") or "").strip()
    nombre = (args.get("nombre") or "").strip()
    if not path:
        return "NO EXPORTADO: falta el argumento 'path' (el HTML autónomo ya armado)."
    if not nombre:
        return (
            "NO EXPORTADO: falta el argumento 'nombre' (título legible del dashboard)."
        )
    if session is None or not getattr(session, "session_id", None):
        return "NO EXPORTADO: no hay sesión activa."

    # REQ-002: la carpeta de export es la de la sesion; sin ella se reusa el
    # flujo project-folder-required (spec carpeta-por-sesion).
    from app.persistence import service as persistence  # import diferido: evita ciclo

    project_root = persistence.project_root_for_session(session.session_id)
    if project_root is None:
        session.push_event("project-folder-required", {"action": "exportar_dashboard"})
        return (
            "NO EXPORTADO: esta sesión no tiene carpeta de proyecto asignada. Se le pidió al "
            "usuario elegir una; cuando la elija, volvé a llamar a exportar_dashboard."
        )

    src = _resolve_source(path, session)
    if src is None or not src.is_file():
        return f"NO EXPORTADO: no encuentro el archivo '{path}'."
    if src.suffix.lower() not in (".html", ".htm"):
        return "NO EXPORTADO: el archivo tiene que ser un .html."
    try:
        html = src.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return f"NO EXPORTADO: no pude leer '{src.name}' como UTF-8 ({exc})."

    # 1) Secretos primero (sobre el HTML del modelo, sin la libreria inyectada).
    secrets = find_secrets(html)
    if secrets:
        return (
            "NO EXPORTADO: el HTML parece contener secretos y no se puede compartir:\n- "
            + "\n- ".join(secrets)
            + "\nSacalos del HTML y reintentá."
        )

    # 2) Inyeccion de librerias vendorizadas + 3) reglas estaticas.
    html, vendor_errors = inject_vendor(html)
    problems = vendor_errors + validate_standalone_html(html)
    if problems:
        return (
            "NO EXPORTADO: no cumple las reglas del HTML autónomo:\n- "
            + "\n- ".join(problems)
        )

    size = len(html.encode("utf-8"))
    warn = ""
    if size > WARN_BYTES:
        warn = f" Aviso: pesa {size / 1024 / 1024:.1f} MB; para compartir por mail conviene reducir datos."

    # 4) Escritura con nombre temporal + validacion + rename.
    export_dir = project_root / "browser"
    slug = slugify(nombre)
    tmp = export_dir / f".validando-{slug}.html"
    try:
        export_dir.mkdir(parents=True, exist_ok=True)
        tmp.write_text(html, encoding="utf-8")
    except OSError as exc:
        return f"NO EXPORTADO: no pude escribir en '{export_dir}' ({exc})."

    try:
        ok, errors, summary = await _validate_in_browser(tmp)
        if not ok:
            return (
                "NO EXPORTADO: el navegador encontró problemas al abrirlo:\n- "
                + "\n- ".join(errors)
                + "\nCorregí el HTML y reintentá (máximo 2 reintentos; después avisale al usuario)."
            )
        final = unique_target(export_dir, slug)
        tmp.replace(final)
    except OSError as exc:
        return f"NO EXPORTADO: no pude dejar el archivo final ({exc})."
    finally:
        tmp.unlink(missing_ok=True)  # nada a medias (REQ-004 punto 6)

    return f"OK: {final}\n{summary}.{warn}"


exportar_dashboard_tool = Tool(
    name="exportar_dashboard",
    description=(
        "Exporta un dashboard HTML como ARCHIVO AUTONOMO para compartir sin conexion, en la carpeta "
        "browser/ de la sesion. Valida en codigo (reglas del HTML autonomo, sin secretos) y lo abre en "
        "un navegador real con la red bloqueada; solo entrega si carga sin errores y muestra contenido. "
        "Usalo SOLO cuando el usuario acepto guardarlo para compartir. `path` es el HTML ya armado "
        '(un solo archivo: CSS/JS inline, datos en <script type="application/json" id="datos">, '
        'sin fetch ni URLs externas; para Chart.js usa <script data-vendor="chart"></script>). '
        "`nombre` es el titulo legible (de ahi sale el nombre del archivo). Devuelve 'OK: <ruta>' o "
        "'NO EXPORTADO: <motivos>' -- nunca digas 'listo para compartir' sin un OK."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Ruta del .html autonomo ya armado (en la vista previa o en la carpeta de la sesion).",
            },
            "nombre": {
                "type": "string",
                "description": "Titulo legible del dashboard, ej. 'Stock de Playas'.",
            },
        },
        "required": ["path", "nombre"],
    },
    execute=_export_execute,
)
