"""La guia `docs/COMO-CREAR-TOOLS-Y-CONEXIONES.md` no puede mentir (spec office-outlook-teams-mcp, REQ-M13).

Una guia que cita un archivo o una funcion que ya no existe es peor que no tener guia: manda a un agente a buscar algo que no esta. Por eso la guia dice que «todo lo que se cita existe en el codigo» y este test
lo comprueba de verdad:
1. Cada ruta de archivo entre comillas invertidas (`app/...`, `frontend/...`, `scripts/...`, `tests/...`) existe en el repo.
2. Cada `modulo.py:funcion` (o `Clase`) citado existe de verdad en ese archivo.
3. Cada nombre de funcion, clase o constante que la guia explica en detalle se puede IMPORTAR o esta definido donde dice.
4. Las cifras que da (limite de destinatarios, plazo, nombre de la tabla de tools) coinciden con el codigo.
5. La guia sigue siendo dev-only: no viaja a prod ni al .exe."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
GUIDE = ROOT / "docs" / "COMO-CREAR-TOOLS-Y-CONEXIONES.md"


@pytest.fixture(scope="module")
def guide_text() -> str:
    return GUIDE.read_text(encoding="utf-8")


def _backticked(text: str) -> list[str]:
    return re.findall(r"`([^`\n]+)`", text)


def _resolve(path: str) -> Path | None:
    """Las rutas de la guia vienen relativas a `backend/` (`app/...`, `tests/...`), a la raiz (`frontend/...`, `scripts/...`, `docs/...`, `specs/...`, `.agent/...`) o sueltas (`sync_prod.py`)."""
    for base in (BACKEND, ROOT):
        candidate = base / path
        if candidate.exists():
            return candidate
    return None


FILE_RE = re.compile(
    r"^(?:app|tests|frontend|scripts|docs|specs|\.agent)/[\w./\-<>*]+\.(?:py|js|json|md|html|css)$|^(?:sync_prod|build_stage|launch_vision)\.py$"
)


def test_the_guide_exists_and_is_not_empty(guide_text):
    assert len(guide_text) > 4000


def test_every_file_path_cited_in_the_guide_exists(guide_text):
    missing = []
    for item in _backticked(guide_text):
        base = item.split(":")[0].strip()
        if (
            not FILE_RE.match(base) or "<" in base or "*" in base
        ):  # los marcadores como `<nombre>_connector.py` o `specs/<slug>/spec.md` son plantillas, no rutas reales
            continue
        if _resolve(base) is None:
            missing.append(base)

    assert not missing, f"la guia cita archivos que no existen: {sorted(set(missing))}"


def _defined_names(path: Path) -> set[str]:
    """Funciones, clases y constantes definidas a nivel de modulo (y metodos) de un .py; para un .js, las funciones `function x` / `const X`."""
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".py":
        names: set[str] = set()
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
            elif isinstance(node, ast.Assign):
                names.update(t.id for t in node.targets if isinstance(t, ast.Name))
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                names.add(node.target.id)
        return names
    return set(re.findall(r"(?:function|const|let)\s+([A-Za-z_]\w*)", text))


def test_every_file_colon_name_reference_points_at_something_that_exists(guide_text):
    broken = []
    for item in _backticked(guide_text):
        match = re.match(r"^((?:app|tests|frontend|scripts)/[\w./\-]+\.(?:py|js)):(\w+)$", item.strip())
        if not match:
            continue
        path = _resolve(match.group(1))
        if path is None:
            broken.append(f"{item} (archivo)")
        elif match.group(2) not in _defined_names(path):
            broken.append(f"{item} (nombre)")

    assert not broken, f"la guia cita nombres que no existen en su archivo: {broken}"


# Nombres que la guia EXPLICA y que el lector va a buscar. Cada uno: (archivo donde tiene que estar definido, nombre).
EXPLAINED = [
    ("app/tools/base.py", "Tool"), ("app/tools/base.py", "ToolRegistry"),
    ("app/tools/__init__.py", "build_registry"),
    ("app/connections.py", "ConnectionType"), ("app/connections.py", "ConnectionField"), ("app/connections.py", "register_type"),
    ("app/connections.py", "MAX_PER_TYPE"), ("app/connections.py", "resolve"),
    ("app/switches.py", "TOOL_CONNECTION_TYPE"), ("app/switches.py", "tool_is_available"), ("app/switches.py", "describe_switches"),
    ("app/switches.py", "is_m365_enabled"), ("app/switches.py", "set_m365_enabled"),
    ("app/connectors_status.py", "CONNECTOR_NAMES"), ("app/connectors_status.py", "check_all_connectors"),
    ("app/secret_store.py", "encrypt_json"), ("app/secret_store.py", "decrypt_json"),
    ("app/secret_access.py", "local_page_problem"),
    ("app/m365/token_store.py", "M365TokenStorage"), ("app/m365/token_store.py", "issued_at"),
    ("app/m365/auth.py", "build_provider"), ("app/m365/auth.py", "LoginFlow"), ("app/m365/auth.py", "VisionOAuthProvider"),
    ("app/m365/auth.py", "http_client_factory"), ("app/m365/auth.py", "redirect_uri"),
    ("app/m365/client.py", "list_tools"), ("app/m365/client.py", "call_tool"),
    ("app/m365/errors.py", "describe"), ("app/m365/errors.py", "redact"), ("app/m365/errors.py", "technical_detail"),
    ("app/m365/content.py", "wrap"), ("app/m365/content.py", "neutralize"), ("app/m365/content.py", "redact_secrets"),
    ("app/m365/policy.py", "check"), ("app/m365/policy.py", "audit"), ("app/m365/policy.py", "new_turn"),
    ("app/m365/policy.py", "_clearly_true"), ("app/m365/policy.py", "_not_clearly_false"),
    ("app/main.py", "_m365_guard"), ("app/main.py", "_launch_turn"),
    ("app/tools/dbquery.py", "_risk_check"),
    ("app/friendly_errors.py", "describe_connector_error"),
    ("frontend/js/vault.js", "CONNECTORS"), ("frontend/js/vault.js", "renderM365"),
]  # fmt: skip


@pytest.mark.parametrize(("file", "name"), EXPLAINED, ids=[f"{f}:{n}" for f, n in EXPLAINED])
def test_every_name_the_guide_explains_exists_where_it_says(file, name, guide_text):
    path = _resolve(file)
    assert path is not None, f"no existe {file}"
    assert name in _defined_names(path), f"{name} no esta definido en {file}"
    short = file.rsplit("/", 1)[-1]
    assert name in guide_text or short in guide_text, (
        f"{name} (de {file}) ya no se menciona en la guia: o se saco de la lista o falta explicarlo"
    )


def test_the_limits_the_guide_states_match_the_code():
    from app.m365 import client, policy

    guide = GUIDE.read_text(encoding="utf-8")

    assert policy.MAX_RECIPIENTS == 20 and "Mas de 20 destinatarios" in guide
    assert client.DEFAULT_TIMEOUT_SECONDS == 15.0 and "15 s" in guide


def test_the_vestigial_invalid_expiry_value_the_guide_warns_about_is_what_the_code_uses():
    from app.m365 import auth

    guide = GUIDE.read_text(encoding="utf-8")

    assert auth._ALREADY_EXPIRED == 1.0, "la guia advierte que «ya vencido» es 1.0 y no 0.0"
    assert "`1.0`" in guide and "`0.0`" in guide


def test_the_tool_count_the_guide_tells_you_to_update_is_where_the_guide_says():
    """La guia manda actualizar el conteo en tres lugares: tienen que existir, o la guia manda a buscar algo que no esta."""
    wiring = (BACKEND / "tests" / "test_switches_wiring.py").read_text(encoding="utf-8")
    qa = (ROOT / "scripts" / "qa_battery.py").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "CORE_TOOLS" in wiring and "M365_TOOLS" in wiring
    assert "def tool_registry(expect" in qa
    assert re.search(r"## Herramientas disponibles \(\d+ tools\)", readme)


def test_the_guide_is_dev_only_and_never_travels_in_the_exe():
    """ULTRON no tiene el flujo dev->prod de la version de origen (`sync_prod.py`): lo que viaja es lo que copia `build_stage.py` al
    armar el `.exe`, y `docs/` nunca tiene que estar ahi."""
    import ast

    tree = ast.parse((ROOT / "build_stage.py").read_text(encoding="utf-8"))
    copied = [
        node.value
        for assign in tree.body
        if isinstance(assign, ast.Assign) and any(getattr(t, "id", "") in ("FILES_TO_COPY", "DIRS_TO_COPY") for t in assign.targets)
        for node in ast.walk(assign.value)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    assert copied, "build_stage.py tiene que declarar lo que copia"
    assert not any(path == "docs" or path.startswith("docs/") for path in copied), "docs/ tiene que seguir siendo dev-only"


def test_the_guide_warns_about_every_trap_it_promises_to_cover(guide_text):
    """La seccion de trampas es lo que mas le sirve al proximo agente: si alguien la recorta sin querer, este test avisa."""
    for trap in (
        "CRLF",
        "suite completa",
        "HTTPS_PROXY",
        "kwargs",
        "monkeypatch.undo",
        "ThreadPoolExecutor",
        "check_secrets",
        "issued_at",
    ):
        assert trap in guide_text, f"la guia ya no menciona «{trap}»"
