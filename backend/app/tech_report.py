"""
Informe tecnico hablado (spec informe-tecnico-hablado).

Arma UNA frase corta por accion de Vision, con lo que realmente ejecuta: el
comando, el archivo que escribe o edita (con el comentario del codigo que
explica que hace), las tablas que consulta o el sitio que abre (REQ-002). No
le pide nada al modelo: sin tokens ni costo extra (decision del usuario).

Solo narra ACCIONES (REQ-003): leer, buscar o listar archivos, skills,
preguntas, el plan de tareas y "pensando" devuelven None y quedan en silencio.

Antes de devolver la frase se tapan los secretos (REQ-007): la frase se habla
y, con un motor de voz en la nube, tambien sale a ese servicio.
"""

from __future__ import annotations

import json
import re
from pathlib import PurePath
from typing import Any
from urllib.parse import urlparse

MAX_WORDS = 20
HIDDEN = "dato oculto"

_SYSTEMS = {
    "oracle_query": "Oracle",
    "bigquery_query": "BigQuery",
}


# ---------------------------------------------------------------------------
# REQ-007: secretos. Se aplican de lo mas especifico a lo mas general.
# ---------------------------------------------------------------------------
_SECRET_RULES: list[tuple[re.Pattern[str], str]] = [
    # Formatos conocidos de claves y tokens (sk_, gsk_, ghp_, xoxb-, AKIA..., JWT).
    (re.compile(r"\b(?:sk|pk|rk|gsk|ghp|gho|github_pat|xox[abprs])[-_][A-Za-z0-9_-]{8,}"), HIDDEN),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), HIDDEN),
    (re.compile(r"\beyJ[\w-]+\.[\w-]+\.[\w-]+"), HIDDEN),
    (re.compile(r"(?i)\bBearer\s+[\w.~+/=-]+"), f"Bearer {HIDDEN}"),
    # Usuario y contraseña dentro de una direccion: https://user:pass@host.
    (re.compile(r"(\w+://)[^/\s:@]+:[^/\s@]+@"), rf"\1{HIDDEN}@"),
    # Cadena de conexion estilo Oracle: usuario/contraseña@host.
    (re.compile(r"\b[\w.-]+/[^\s/@]+@(?=[\w.-])"), f"{HIDDEN}@"),
    # clave=valor / clave: valor con nombres que delatan un secreto.
    (
        re.compile(
            r"(?i)\b([\w.-]*(?:password|passwd|pwd|secret|token|api[_-]?key|apikey|access[_-]?key|"
            r"clave|contraseña)[\w.-]*)\s*[=:]\s*(\"[^\"]*\"|'[^']*'|[^\s&;,]+)"
        ),
        rf"\1 {HIDDEN}",
    ),
    # Opciones de linea de comandos: --password xyz, --token xyz.
    (
        re.compile(r"(?i)(--?(?:password|passwd|pwd|token|secret|api[_-]?key))\s+(\"[^\"]*\"|'[^']*'|\S+)"),
        rf"\1 {HIDDEN}",
    ),
]
# Cadenas largas con letras y numeros mezclados: casi siempre claves o hashes.
_LONG_RANDOM = re.compile(r"\b[A-Za-z0-9+/_-]{32,}={0,2}")


def redact(text: str) -> str:
    """REQ-007: reemplaza claves, contraseñas y tokens por "dato oculto"."""
    for pattern, replacement in _SECRET_RULES:
        text = pattern.sub(replacement, text)

    def _hide_random(match: re.Match[str]) -> str:
        value = match.group(0)
        mixed = any(c.isdigit() for c in value) and any(c.isalpha() for c in value)
        return HIDDEN if mixed else value

    return _LONG_RANDOM.sub(_hide_random, text)


# ---------------------------------------------------------------------------
# REQ-002: el "por que" sale de los comentarios del codigo que escribe Vision.
# ---------------------------------------------------------------------------
_COMMENT = re.compile(r"^\s*(?:#(?!!)|//|--|/\*+|\*(?!/)|rem\s|\"\"\"|'''|<!--)\s*(.*?)\s*(?:\*/|\"\"\"|'''|-->)?\s*$", re.I)
_NOT_A_DESCRIPTION = re.compile(r"(?i)-\*-|coding[:=]|noqa|pylint|type:\s*ignore|^region\b|^endregion\b|^!")
_REQ_PREFIX = re.compile(r"(?i)^(?:spec\s+[\w-]+\s+)?REQ-\d+\s*[:\-–—]\s*")


def first_comment(code: str) -> str | None:
    """El primer comentario que describe algo (3 palabras o mas), sin los
    marcadores de comentario ni el prefijo "REQ-00X:"."""
    for line in code.splitlines()[:60]:
        match = _COMMENT.match(line)
        if not match:
            continue
        text = _REQ_PREFIX.sub("", match.group(1).strip()).strip(" .:-–—")
        if _NOT_A_DESCRIPTION.search(text) or len(re.findall(r"[^\W\d_]{2,}", text)) < 3:
            continue
        if len(text) > 1 and text[0].isupper() and text[1].islower():
            text = text[0].lower() + text[1:]
        return text
    return None


def _tables(sql: str) -> list[str]:
    found: list[str] = []
    for name in re.findall(r"(?i)\b(?:from|join)\s+([\w.\"`\[\]]+)", sql):
        clean = re.sub(r"[\"`\[\]]", "", name).split(".")[-1]
        if clean and clean.lower() not in {"select", "dual"} and clean not in found:
            found.append(clean)
    return found[:3]


def _file_name(path: Any) -> str:
    return PurePath(str(path or "")).name or "un archivo"


def _first_line(text: str) -> str:
    return next((line.strip() for line in text.splitlines() if line.strip()), "")


def _limit(text: str) -> str:
    words = text.split()
    return " ".join(words[:MAX_WORDS]) + ("…" if len(words) > MAX_WORDS else "")


# ---------------------------------------------------------------------------
# REQ-008: recorrido del codigo EN VIVO. Los argumentos de una tool-call
# llegan de a pedazos mientras el modelo los escribe (medido: ~450
# fragmentos en 5 s para un HTML chico). `LiveNarrator` decodifica el campo
# de codigo del JSON parcial, y por cada LINEA completa dice sus comentarios
# y su estructura (titulo, sidebar, tarjetas, graficos, tablas, conexiones).
# ---------------------------------------------------------------------------
_CODE_FIELD = {"write": "content", "edit": "new_string", "bash": "command"}
_HASH_COMMENT_EXT = {".py", ".sh", ".ps1", ".psm1", ".r", ".yaml", ".yml", ".toml", ".rb", ".pl", ".ini", ".cfg"}
_ESCAPES = {"n": "\n", "t": "\t", "r": "", "b": "", "f": "", "/": "/", "\\": "\\", '"': '"'}
_DB_CONNECTIONS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?i)\b(?:oracledb|cx_oracle)\.connect\("), "Oracle"),
    (re.compile(r"\bsqlite3\.connect\("), "SQLite"),
    (re.compile(r"\bpsycopg2?\.connect\("), "PostgreSQL"),
    (re.compile(r"\bpyodbc\.connect\("), "SQL Server"),
    (re.compile(r"\b(?:mysql\.connector|pymysql)\.connect\("), "MySQL"),
    (re.compile(r"\bbigquery\.Client\("), "BigQuery"),
    (re.compile(r"\bMongoClient\(|\bmongoose\.connect\("), "MongoDB"),
    (re.compile(r"\bcreate_engine\("), "SQL"),
]
_FETCH = re.compile(r"""\b(?:fetch|axios\.(?:get|post)|requests\.(?:get|post))\(\s*[`'"]([^`'"]+)""")


# ---------------------------------------------------------------------------
# REQ-012 (reapertura 2): nivel del informe. "summary" = el informe de siempre (REQ-001 a REQ-011);
# "detailed" suma estructura de pagina, estilos y funciones (REQ-013/014). Un valor desconocido es
# "summary": ante la duda NO se habla mas de lo que el usuario pidio.
# ---------------------------------------------------------------------------
LEVEL_SUMMARY = "summary"
LEVEL_DETAILED = "detailed"
_LEVEL_ALIASES = {"detailed": LEVEL_DETAILED, "detallado": LEVEL_DETAILED, "summary": LEVEL_SUMMARY, "resumido": LEVEL_SUMMARY}


def normalize_level(value: Any) -> str:
    """'detallado'/'detailed' -> 'detailed'; cualquier otra cosa (None, vacio, tipo raro) -> 'summary'."""
    return _LEVEL_ALIASES.get(value.strip().lower(), LEVEL_SUMMARY) if isinstance(value, str) else LEVEL_SUMMARY


# REQ-013: pistas de estilo que se pueden contar en una frase corta.
_DARK_BG = re.compile(r"(?i)(?:--[\w-]*(?:bg|background|fondo|canvas)[\w-]*|background(?:-color)?)\s*:\s*#(?:[0-3][0-9a-f]{5}|[0-3][0-9a-f]{2})\b")
_FONT_FAMILY = re.compile(r"""(?i)font-family\s*:\s*["']?([A-Za-z][A-Za-z0-9 \-]{1,30}?)["']?\s*[,;}]""")
# REQ-014: definiciones de funcion (JS/TS y Python) y listeners.
_FUNC_DEF = re.compile(
    r"""^\s*(?:export\s+)?(?:async\s+)?(?:function\s*\*?\s*([A-Za-z_$][\w$]*)\s*\("""
    r"""|(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>"""
    r"""|def\s+([A-Za-z_][\w]*)\s*\()"""
)
_LISTENER = re.compile(r"""\.addEventListener\(\s*["'](\w+)["']""")
_EVENT_WORDS = {"click": "el clic", "submit": "el envío del formulario", "change": "el cambio", "input": "lo que se escribe",
                "keydown": "una tecla", "load": "la carga de la página", "scroll": "el scroll", "resize": "el cambio de tamaño"}


def _decode_partial(raw: str, start: int) -> tuple[str, int, bool]:
    """Decodifica un string JSON desde `start` hasta donde llego. Devuelve
    (texto, nuevo inicio, si el string ya cerro). Una secuencia de escape
    cortada a la mitad se deja para el proximo fragmento."""
    out: list[str] = []
    i = start
    while i < len(raw):
        ch = raw[i]
        if ch == '"':
            return "".join(out), i + 1, True
        if ch == "\\":
            if i + 1 >= len(raw):
                break
            esc = raw[i + 1]
            if esc == "u":
                if i + 6 > len(raw):
                    break
                try:
                    out.append(chr(int(raw[i + 2 : i + 6], 16)))
                except ValueError:
                    pass
                i += 6
                continue
            out.append(_ESCAPES.get(esc, esc))
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out), i, False


def _clean_comment(text: str, min_words: int = 2) -> str | None:
    text = _REQ_PREFIX.sub("", text.strip()).strip(" .:-–—*")
    if not text or _NOT_A_DESCRIPTION.search(text) or len(re.findall(r"[^\W\d_]{2,}", text)) < min_words:
        return None
    if len(text) > 1 and text[0].isupper() and text[1].islower():
        text = text[0].lower() + text[1:]
    return text


def _humanize(identifier: str) -> str:
    words = re.sub(r"([a-z])([A-Z])", r"\1 \2", identifier).replace("-", " ").replace("_", " ")
    return words.lower().strip()


def _strip_tags(html: str) -> str:
    return re.sub(r"<[^>]+>", "", html).strip()


class LiveNarrator:
    """Recorre en vivo el codigo de UNA tool-call (write/edit/bash)."""

    def __init__(self, name: str, level: str = LEVEL_SUMMARY) -> None:
        self.name = name
        self.level = normalize_level(level)
        # Estado de los bloques multi-linea (REQ-013): <style> y <script> se resumen UNA vez cada uno.
        self._in_style = False
        self._style_seen: set[str] = set()
        self._last_comment: str | None = None
        # REQ-015: la PRIMERA frase de comentario de un archivo detallado es la presentacion del diseno;
        # se marca como `lead` para que el tope de la cola de voz nunca la descarte.
        self.lead_phrase: str | None = None
        self.field = _CODE_FIELD.get(name)
        self.raw = ""
        self.ext = ""
        self.path_said = name == "bash"
        self.value_at: int | None = None
        self.pending = ""
        self.closed = False
        self.said: set[str] = set()
        self.narrated = False

    @property
    def active(self) -> bool:
        return self.field is not None

    def feed(self, fragment: str) -> list[str]:
        """Frases nuevas (ya sin secretos) que aparecieron con este fragmento."""
        if not self.active or self.closed or not fragment:
            return []
        self.raw += fragment
        phrases: list[str] = []
        if not self.path_said:
            match = re.search(r'"path"\s*:\s*"((?:[^"\\]|\\.)*)"', self.raw)
            if match:
                path, _, _ = _decode_partial(match.group(1) + '"', 0)
                self.ext = PurePath(path).suffix.lower()
                phrases.append(f"{'Escribo' if self.name == 'write' else 'Edito'} {_file_name(path)}")
                self.path_said = True
        if self.value_at is None:
            match = re.search(rf'"{self.field}"\s*:\s*"', self.raw)
            if match:
                self.value_at = match.end()
        if self.value_at is not None:
            text, self.value_at, closed = _decode_partial(self.raw, self.value_at)
            self.pending += text
            *lines, self.pending = self.pending.split("\n")
            if closed:
                lines.append(self.pending)
                self.pending = ""
                self.closed = True
            for line in lines:
                phrases.extend(self._describe(line))
        # Un comando se presenta como tal la primera vez ("Ejecuto un comando: ...").
        if self.name == "bash" and phrases and not self.narrated:
            phrases[0] = f"Ejecuto un comando: {phrases[0]}"
        result = []
        for phrase in phrases:
            spoken = _limit(redact(phrase))
            if spoken.lower() not in self.said:
                self.said.add(spoken.lower())
                result.append(spoken)
        self.narrated = self.narrated or bool(result)
        return result

    def _comment(self, stripped: str) -> str | None:
        ext = self.ext
        html_comment = re.search(r"<!--\s*(.*?)\s*(?:-->|$)", stripped)
        if html_comment and ext in ("", ".html", ".htm", ".xml", ".vue", ".svelte", ".md"):
            return _clean_comment(html_comment.group(1))
        if stripped.startswith("#") and not stripped.startswith("#!"):
            if ext in _HASH_COMMENT_EXT or self.name == "bash" or ext == "":
                return _clean_comment(stripped.lstrip("#"))
            return None  # en HTML/CSS/JS un "#" es un selector o un color, no un comentario
        for marker in ("//", "/*", "--", "rem ", "::", '"""', "'''"):
            if stripped.lower().startswith(marker):
                if marker == "--" and ext not in (".sql", ""):
                    return None
                return _clean_comment(re.sub(r"\*/\s*$|\"\"\"|'''", "", stripped[len(marker) :]))
        if stripped.startswith("* ") and ext in (".js", ".ts", ".jsx", ".tsx", ".java", ".css", ".html", ""):
            text = stripped[2:]
            return None if text.startswith("@") else _clean_comment(text, min_words=3)
        return None

    def _describe(self, line: str) -> list[str]:
        stripped = line.strip()
        if not stripped:
            return []
        found: list[str] = []
        comment = self._comment(stripped)
        if comment:
            found.append(comment)
            if self.level == LEVEL_DETAILED and self.lead_phrase is None and self.name != "bash":
                self.lead_phrase = _limit(redact(comment))
        title = re.search(r"(?i)<title>(.*?)</title>", stripped)
        if title and _strip_tags(title.group(1)):
            found.append(f"Título: {_strip_tags(title.group(1))}")
        heading = re.search(r"(?i)<h1[^>]*>(.*?)</h1>", stripped)
        if heading and _strip_tags(heading.group(1)):
            found.append(f"Encabezado: {_strip_tags(heading.group(1))}")
        if re.search(r"(?i)<(?:aside|nav|div|section)\b[^>]*(?:sidebar|side-bar|barra-lateral|menu-lateral)", stripped):
            side = ""
            if re.search(r"(?i)right|derech", stripped):
                side = " del lado derecho"
            elif re.search(r"(?i)left|izquierd", stripped):
                side = " del lado izquierdo"
            found.append(f"Armo el sidebar{side}")
        if re.search(r'(?i)class="[^"]*\b(?:card|kpi|tarjeta)', stripped):
            found.append("Agrego tarjetas de indicadores")
        chart = re.search(r'(?i)<canvas\b[^>]*\bid="([^"]+)"', stripped)
        if chart:
            found.append(f"Agrego un gráfico: {_humanize(chart.group(1))}")
        elif re.search(r"\bnew Chart\(", stripped):
            found.append("Configuro un gráfico")
        if re.search(r"(?i)<table\b", stripped):
            found.append("Agrego una tabla")
        if re.search(r"(?i)<form\b", stripped):
            found.append("Agrego un formulario")
        for pattern, system in _DB_CONNECTIONS:
            if pattern.search(stripped):
                found.append(f"Conecto con la base de datos {system}" if system != "SQL" else "Conecto con la base de datos")
        target = _FETCH.search(stripped)
        if target:
            host = urlparse(target.group(1)).netloc or target.group(1)
            found.append(f"Pido datos a {host}")
        if self.level == LEVEL_DETAILED:
            found.extend(self._describe_detailed(stripped, comment))
        self._last_comment = comment
        return found

    # -- REQ-013 / REQ-014: solo nivel "detailed" ------------------------------------------------
    def _describe_detailed(self, stripped: str, comment: str | None) -> list[str]:
        """Estructura de la pagina, estilos y funciones. Solo para archivos de pagina/codigo: un
        `bash` que escribe texto no es una pagina, asi que no se inventa 'pie de pagina' por una palabra."""
        if self.name == "bash":
            return []
        found: list[str] = []
        lower = stripped.lower()
        if re.search(r"<header\b", lower):
            found.append("Armo el encabezado de la página")
        if re.search(r"<nav\b", lower) and not re.search(r"sidebar|side-bar|barra-lateral|menu-lateral", lower):
            found.append("Armo el menú de navegación")
        if re.search(r"<footer\b", lower):
            found.append("Armo el pie de página")
        if re.search(r"<main\b", lower):
            found.append("Armo el contenido principal")
        if re.search(r"<section\b", lower):
            klass = re.search(r"""class\s*=\s*["']([\w-]+)""", stripped)
            found.append(f"Defino una sección: {_humanize(klass.group(1))}" if klass else "Defino una sección")
        # estilos: <style> abre un bloque; cada pista se dice UNA vez
        if "<style" in lower:
            self._in_style = True
        if self._in_style:
            hints = self._style_hints(stripped)
            fresh = [h for h in hints if h not in self._style_seen]
            self._style_seen.update(fresh)
            if fresh:
                found.append("Aplico estilos: " + ", ".join(fresh))
        if "</style>" in lower:
            self._in_style = False
        # funciones JS/Python y listeners
        func = _FUNC_DEF.match(stripped)
        if func:
            name = _humanize(next(g for g in func.groups() if g))
            # El comentario suele empezar con un verbo ("renderiza la tabla..."): se une con ", que" para no
            # decir "para renderiza".
            purpose = f", que {self._last_comment}" if self._last_comment else ""
            found.append(f"Defino la función {name}{purpose}")
        listener = _LISTENER.search(stripped)
        if listener:
            what = _EVENT_WORDS.get(listener.group(1).lower(), f"el evento {listener.group(1)}")
            found.append(f"Conecto {what} de un elemento")
        return found

    @staticmethod
    def _style_hints(line: str) -> list[str]:
        hints: list[str] = []
        if _DARK_BG.search(line):
            hints.append("fondo oscuro")
        font = _FONT_FAMILY.search(line)
        if font and font.group(1).strip().lower() not in {"sans-serif", "serif", "monospace", "system-ui", "inherit"}:
            hints.append(f"tipografía {font.group(1).strip()}")
        if re.search(r"(?i)display\s*:\s*grid\b", line):
            hints.append("diseño en grilla")
        elif re.search(r"(?i)display\s*:\s*flex\b", line):
            hints.append("diseño flexible")
        if re.search(r"(?i)@media\b[^{]*(?:max|min)-width", line):
            hints.append("adaptable a celular")
        return hints


# REQ-010: avance del plan de tareas (lo usa la tool `todowrite`).
def _task_states(tasks: list[dict[str, Any]], parent: str = "") -> dict[tuple[str, str], tuple[str, int, int]]:
    """(tarea madre, tarea) -> (estado, numero, total entre sus hermanas). Las
    sub-tareas se numeran dentro de su tarea madre ("Paso 2 de 4")."""
    states: dict[tuple[str, str], tuple[str, int, int]] = {}
    for number, task in enumerate(tasks, 1):
        content = str(task.get("content", "")).strip()
        if not content:
            continue
        states[(parent, content)] = (str(task.get("status", "")), number, len(tasks))
        subtasks = task.get("subtasks")
        if isinstance(subtasks, list) and subtasks:
            states.update(_task_states(subtasks, parent=content))
    return states


def task_transitions(before: list[dict[str, Any]], after: list[dict[str, Any]]) -> list[str]:
    """Frases por las tareas (y sub-tareas) que empezaron o terminaron entre dos
    versiones del plan, en el orden del plan."""
    previous = _task_states(before)
    phrases = []
    for key, (status, number, total) in _task_states(after).items():
        if previous.get(key, ("",))[0] == status:
            continue
        content = key[1]
        if status == "in_progress":
            phrases.append(f"Paso {number} de {total}: {content}" if total > 1 else f"Empiezo: {content}")
        elif status == "completed":
            phrases.append(f"Listo: {content}")
    return [_limit(redact(p)) for p in phrases]


def describe_call(name: str, raw_args: Any) -> str | None:
    """Frase del informe tecnico para una tool-call, o None si no es una accion."""
    try:
        args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args or {})
    except (json.JSONDecodeError, TypeError, ValueError):
        args = {}
    if not isinstance(args, dict):
        args = {}

    phrase: str | None = None
    if name == "bash":
        command = str(args.get("command", ""))
        comment = first_comment(command)
        line = _first_line(command)
        if comment:
            phrase = f"Ejecuto un comando: {comment}"
        else:
            phrase = f"Ejecuto: {line}" if line else "Ejecuto un comando"
    elif name == "write":
        comment = first_comment(str(args.get("content", "")))
        phrase = f"Escribo {_file_name(args.get('path'))}" + (f": {comment}" if comment else "")
    elif name == "edit":
        comment = first_comment(str(args.get("new_string", "")))
        phrase = f"Edito {_file_name(args.get('path'))}" + (f": {comment}" if comment else "")
    elif name in _SYSTEMS:
        tables = _tables(str(args.get("query", "")))
        label = "tabla" if len(tables) == 1 else "tablas"
        phrase = f"Consulto {_SYSTEMS[name]}" + (f": {label} {', '.join(tables)}" if tables else "")
    elif name == "bigquery_list_tables":
        dataset = str(args.get("dataset_id", "")).strip()
        phrase = "Listo las tablas de BigQuery" + (f" del dataset {dataset}" if dataset else "")
    elif name == "sap_query":
        entity = str(args.get("entity_name", "")).strip()
        phrase = "Consulto SAP" + (f": {entity}" if entity else "")
    elif name == "sharepoint_list_items":
        list_name = str(args.get("list_name", "")).strip()
        phrase = "Consulto SharePoint" + (f": lista {list_name}" if list_name else "")
    elif name == "sharepoint_list_lists":
        phrase = "Listo las listas de SharePoint"
    elif name == "graph_search":
        query = str(args.get("query", "")).strip()
        phrase = "Busco en Microsoft 365" + (f": {query}" if query else "")
    elif name in ("graph_directory_lookup", "graph_directory_manager"):
        phrase = "Consulto el directorio de la empresa"
    elif name == "webfetch":
        host = urlparse(str(args.get("url", ""))).netloc
        phrase = f"Abro la página {host}" if host else "Abro una página web"
    elif name == "render_chart":
        title = str(args.get("title", "")).strip()
        phrase = "Armo un gráfico" + (f": {title}" if title else "")
    if phrase is None:
        return None
    return _limit(redact(phrase))
