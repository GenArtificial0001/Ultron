"""Conftest compartido de la suite pytest de Vision (spec bateria-qa-integracion).

**SOLO DESARROLLO.** Este archivo, `backend/tests/`, `backend/pytest.ini`,
`backend/.coveragerc` y `backend/requirements-dev.txt` viven unicamente en la
rama `dev`; `sync_prod.py` / `build_stage.py` nunca los copian a `prod/` y
`tests/test_prod_boundary.py` lo verifica en cada corrida.

Que garantiza este conftest (hermeticidad, patron DBManager -- ver AGENTS.md):

1. **Datos aislados.** ANTES de importar cualquier modulo de `app`, redirige
   `VISION_DATA_DIR` y el HOME del usuario a una carpeta temporal de la sesion.
   Bug real detectado (2026-09-21): sin `VISION_DEV_MODE=1`, `app.config`
   resuelve `BASE_DIR` a `%LOCALAPPDATA%\\Vision-personal` -- el directorio de datos de
   PRODUCCION (con el `.env` real y las credenciales) -- y la suite leia esa
   config y le escribia logs/`tool_timings.json`. Ahora es imposible: si
   `BASE_DIR` no cae dentro del temp de la sesion, la corrida aborta.
2. **Sin credenciales reales.** Se borran del entorno las variables que
   `Settings` leeria (LLM_*, STT_*, ORACLE_*, ...) para que ningun test dependa
   de -- ni filtre -- secretos de la maquina donde corre.
3. **Sin red externa.** Fixture autouse que bloquea `connect`/DNS hacia
   cualquier host que no sea loopback (`@pytest.mark.network` la desactiva
   en el test puntual que de verdad la necesite; hoy ninguno).
4. **Guarda anti-prod.** La suite se niega a correr desde un ejecutable
   congelado o desde el worktree `prod/`.
5. **Marcadores automaticos** (`unit`/`integration`) y orden aleatorio
   opcional (`--shuffle`, reproducible con `--shuffle-seed`) para detectar
   tests acoplados por estado global -- `pytest-randomly` no esta disponible
   en el mirror interno de la empresa, por eso se implementa aca en ~15 lineas.
"""

from __future__ import annotations

import inspect
import os
import random
import shutil
import socket
import sys
import tempfile
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# 1) Aislamiento del entorno -- DEBE ejecutarse antes de `import app...`.
#    (pytest importa conftest.py antes que los modulos de test.)
# ---------------------------------------------------------------------------
_BACKEND_DIR = Path(__file__).resolve().parents[1]
_REPO_ROOT = _BACKEND_DIR.parent

_SESSION_TMP = Path(tempfile.mkdtemp(prefix="vision-pytest-"))
_DATA_DIR = _SESSION_TMP / "data-dir"
_HOME_DIR = _SESSION_TMP / "home"
_DATA_DIR.mkdir()
_HOME_DIR.mkdir()

# Los tests e2e (Playwright) buscan su Chromium en %LOCALAPPDATA%\ms-playwright.
# Se fija la ruta REAL antes de redirigir LOCALAPPDATA abajo: son binarios de
# solo lectura (no datos del usuario), y sin esto los e2e se saltean en
# silencio con "Executable doesn't exist".
if "PLAYWRIGHT_BROWSERS_PATH" not in os.environ and os.environ.get("LOCALAPPDATA"):
    _real_pw = Path(os.environ["LOCALAPPDATA"]) / "ms-playwright"
    if _real_pw.is_dir():
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(_real_pw)

# Nunca modo dev: en modo dev BASE_DIR seria `backend/` (el repo real).
os.environ.pop("VISION_DEV_MODE", None)
os.environ["VISION_DATA_DIR"] = str(_DATA_DIR)
for _var in ("USERPROFILE", "HOME"):
    os.environ[_var] = str(_HOME_DIR)
os.environ["LOCALAPPDATA"] = str(_HOME_DIR / "AppData" / "Local")
os.environ["APPDATA"] = str(_HOME_DIR / "AppData" / "Roaming")

# Prefijos de las variables que `Settings` / los conectores leen del entorno.
_SECRET_PREFIXES = (
    "LLM_",
    "STT_",
    "VISION_COMPANY",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "ORACLE_",
    "BIGQUERY_",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "SHAREPOINT_",
    "SAP_",
    "AZURE_",
    "GRAPH_",
    "ACCESS_TOKEN",
    "OPENAI_",
    "ANTHROPIC_",
    # spec varios-proveedores: claves de los demas proveedores de IA.
    "GEMINI_",
    "DEEPSEEK_",
    "GROQ_",
    "META_GROQ_",
    "OPENROUTER_",
    "CUSTOM_",
    "FISH_",
    "ELEVENLABS_",
)
for _name in list(os.environ):
    if _name.upper().startswith(_SECRET_PREFIXES):
        del os.environ[_name]

if str(_BACKEND_DIR) not in sys.path:  # `import app` sin depender del cwd
    sys.path.insert(0, str(_BACKEND_DIR))

from app.persistence import master_index as mi  # tras aislar el entorno


# ---------------------------------------------------------------------------
# 2) Opciones de linea de comandos, guarda anti-prod, marcadores, orden.
# ---------------------------------------------------------------------------
def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("vision")
    group.addoption(
        "--shuffle",
        action="store_true",
        default=False,
        help="Ejecuta los tests en orden aleatorio (detecta acoplamiento por estado global).",
    )
    group.addoption(
        "--shuffle-seed",
        type=int,
        default=None,
        help="Semilla del orden aleatorio (para reproducir una corrida que fallo).",
    )


def pytest_configure(config: pytest.Config) -> None:
    from app import config as app_config

    if getattr(sys, "frozen", False) or app_config.IS_FROZEN:
        pytest.exit("La suite de tests es solo de desarrollo: no corre en el .exe.", 2)
    if _REPO_ROOT.parent.name.lower() == "prod":
        pytest.exit(
            f"'{_REPO_ROOT}' es el worktree de PRODUCCION: los tests viven solo en dev/.",
            2,
        )
    base = app_config.BASE_DIR.resolve()
    if _SESSION_TMP.resolve() not in base.parents and base != _SESSION_TMP.resolve():
        pytest.exit(
            f"Aislamiento roto: BASE_DIR={base} no esta dentro del temp de la sesion "
            f"({_SESSION_TMP}). Se aborta para no tocar datos reales.",
            2,
        )
    # `get_settings` esta cacheado con lru_cache: garantizar que se construya
    # con el entorno ya aislado, no con uno creado por un import anterior.
    app_config.get_settings.cache_clear()


def pytest_report_header(config: pytest.Config) -> list[str]:
    return [
        f"vision: datos aislados en {_SESSION_TMP}",
        "vision: red externa bloqueada (loopback permitido)",
    ]


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    # Marcado automatico: un test que usa TestClient o el DB maestro es de
    # integracion; el resto es unit. Asi `-m unit` da un ciclo de feedback rapido.
    for item in items:
        if any(item.get_closest_marker(m) for m in ("unit", "integration", "e2e")):
            continue
        module_globals = getattr(item.module, "__dict__", {})
        # `tmp_master_db` es autouse (siempre en fixturenames): se mira la firma
        # de la funcion para saber si el test lo pidio de forma explicita.
        func = getattr(item, "function", None)
        asks_db = func is not None and "tmp_master_db" in inspect.signature(func).parameters
        uses_client = "TestClient" in module_globals or asks_db
        item.add_marker(pytest.mark.integration if uses_client else pytest.mark.unit)

    if config.getoption("--shuffle") or config.getoption("--shuffle-seed") is not None:
        seed = config.getoption("--shuffle-seed")
        if seed is None:
            seed = random.SystemRandom().randrange(1, 2**31)
        random.Random(seed).shuffle(items)
        config._vision_shuffle_seed = seed  # type: ignore[attr-defined]


def pytest_terminal_summary(terminalreporter, exitstatus, config: pytest.Config) -> None:
    seed = getattr(config, "_vision_shuffle_seed", None)
    if seed is not None:
        terminalreporter.write_line(
            f"orden aleatorio -- para reproducir: pytest --shuffle-seed={seed}"
        )


def pytest_unconfigure(config: pytest.Config) -> None:
    shutil.rmtree(_SESSION_TMP, ignore_errors=True)


# ---------------------------------------------------------------------------
# 3) Fixtures autouse: sin red externa, DB maestro siempre en temp.
# ---------------------------------------------------------------------------
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0", "", None}


def _host_of(address) -> object:
    if isinstance(address, (tuple, list)) and address:
        return address[0]
    return address  # AF_UNIX (str) u otros: no es red externa


def _is_loopback(host) -> bool:
    if isinstance(host, bytes):
        host = host.decode("ascii", "ignore")
    if not isinstance(host, str) and host is not None:
        return True
    return host in _LOOPBACK_HOSTS or (isinstance(host, str) and host.startswith("127."))


@pytest.fixture(autouse=True)
def _block_external_network(request, monkeypatch):
    """Falla ruidosamente si un test intenta salir a la red (Oracle, SAP,
    BigQuery, SharePoint, el proveedor de IA...): la suite es 100% mockeada."""
    if request.node.get_closest_marker("network"):
        return

    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_getaddrinfo = socket.getaddrinfo

    def _deny(host) -> None:
        raise RuntimeError(
            f"Acceso a red bloqueado en tests (destino: {host!r}). Mockea el conector "
            "o marca el test con @pytest.mark.network si es imprescindible."
        )

    def guarded_connect(self, address):
        if not _is_loopback(_host_of(address)):
            _deny(address)
        return real_connect(self, address)

    def guarded_connect_ex(self, address):
        if not _is_loopback(_host_of(address)):
            _deny(address)
        return real_connect_ex(self, address)

    def guarded_getaddrinfo(host, *args, **kwargs):
        if not _is_loopback(host):
            _deny(host)
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)


@pytest.fixture(autouse=True)
def _isolated_project_config(tmp_path, monkeypatch):
    """`ProjectConfig` (umbrales, params de generacion, skills...) se persiste en
    UN `project_config.json` y se relee en cada `get_project_config()`. Aislar
    solo por sesion de pytest (BASE_DIR temporal) NO alcanza: un test que guarda
    un override lo deja para todos los siguientes. Bug real (2026-09-21):
    `test_connections_config_endpoint` dejaba `bq_cost_guardrail_bytes=512MB`
    y `test_bigquery_guardrails` fallaba SOLO bajo ciertos ordenes (--shuffle).
    Ahora cada test tiene su propio archivo de configuracion."""
    from app import project_context as pc

    monkeypatch.setattr(pc, "CONFIG_FILE", tmp_path / "project_config.json")


@pytest.fixture(autouse=True)
def tmp_master_db(tmp_path, monkeypatch):
    """Redirige el DB maestro global (`<carpeta de datos>\\vision.db`) a un
    archivo volatil del test. Es autouse desde 2026-09-21: antes cada test
    tenia que pedirlo y uno que lo olvidara ensuciaba el DB real del usuario.
    Los tests que necesitan la ruta lo siguen pidiendo por nombre."""
    db = tmp_path / "vision.db"
    monkeypatch.setattr(mi, "master_db_path", lambda: db)
    return db
