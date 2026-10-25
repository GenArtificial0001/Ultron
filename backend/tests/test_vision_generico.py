"""Vision generico (spec `vision-generico`, APPROVED 2026-09-27): el proveedor de
IA, la lista de modelos, el proxy de la empresa y el nombre de la empresa salen
del .env; nada propio de una empresa en el codigo. Cada test cita su REQ.
Sin red: el cliente de OpenAI se construye pero nunca llama a nadie."""

from __future__ import annotations

import os
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import config, main
from app.friendly_errors import describe_model_error
from app.llm import client as client_module
from app.llm import providers as llm_providers
from app.llm.client import ANTHROPIC_BASE_URL, ProviderNotConfigured, get_client

pytestmark = pytest.mark.unit


@pytest.fixture
def llm(monkeypatch):
    """Ajusta la configuracion del proveedor sobre la instancia cacheada."""
    settings = config.get_settings()
    monkeypatch.setattr(client_module, "_CLIENT", None)
    monkeypatch.setattr(client_module, "_CLIENT_CONFIG", None)

    def configure(provider="anthropic", base_url="", api_key="clave-de-prueba", model="claude-sonnet-5", models=""):
        for name, value in (
            ("llm_provider", provider),
            ("llm_base_url", base_url),
            ("llm_api_key", api_key),
            ("llm_model", model),
            ("llm_models", models),
        ):
            monkeypatch.setattr(settings, name, value)
        return settings

    return configure


# ---------------------------------------------------------------------------
# REQ-001: proveedor configurable, sin servidor por defecto
# ---------------------------------------------------------------------------
def test_anthropic_uses_its_compatible_endpoint_and_key_headers(llm):
    llm(provider="anthropic", api_key="sk-ant-prueba")
    client = get_client()
    assert str(client.base_url) == ANTHROPIC_BASE_URL
    assert client.api_key == "sk-ant-prueba"
    assert client._custom_headers["x-api-key"] == "sk-ant-prueba"
    assert client._custom_headers["anthropic-version"]


def test_openai_compatible_uses_the_configured_address(llm):
    llm(provider="openai_compatible", base_url="https://gateway.empresa.test/v1", api_key="k")
    client = get_client()
    assert str(client.base_url).rstrip("/") == "https://gateway.empresa.test/v1"
    assert "x-api-key" not in client._custom_headers


@pytest.mark.parametrize(
    ("kwargs", "fragment"),
    [
        ({"api_key": ""}, "LLM_API_KEY"),
        ({"provider": "openai_compatible", "base_url": ""}, "LLM_BASE_URL"),
        ({"provider": "otro"}, "LLM_PROVIDER"),
    ],
    ids=["sin-clave", "compatible-sin-direccion", "proveedor-invalido"],
)
def test_incomplete_configuration_stops_before_calling_anyone(llm, kwargs, fragment):
    llm(**kwargs)
    with pytest.raises(ProviderNotConfigured, match=fragment):
        get_client()


def test_missing_configuration_reaches_the_chat_in_plain_language(llm):
    llm(api_key="")
    with pytest.raises(ProviderNotConfigured) as info:
        get_client()
    friendly = describe_model_error(info.value)
    assert friendly.code == "MODELO-CONFIGURACION"
    assert friendly.message.startswith("Vision todavía no tiene configurado el servicio de inteligencia artificial.")


def test_client_is_reused_until_the_configuration_changes(llm):
    settings = llm(api_key="clave-1")
    first = get_client()
    assert get_client() is first
    settings.llm_api_key = "clave-2"  # ej. cargada desde Configuracion > Credenciales
    second = get_client()
    assert second is not first and second.api_key == "clave-2"
    assert get_client(force_refresh=True) is not second


# ---------------------------------------------------------------------------
# REQ-002: modelos del selector
# ---------------------------------------------------------------------------
@pytest.fixture
def api(monkeypatch):
    monkeypatch.setattr(main, "_MODELS_CACHE", {"data": None, "fetched_at": 0.0})
    llm_providers.clear_caches()
    return TestClient(main.app)


def _fake_provider(ids=None, exc=None):
    def _list():
        if exc:
            raise exc
        return [SimpleNamespace(id=i) for i in ids or []]

    return SimpleNamespace(models=SimpleNamespace(list=_list))


def test_models_from_the_env_list_win_and_the_provider_is_not_asked(api, llm, monkeypatch):
    """spec varios-proveedores REQ-007: con LLM_MODELS completa, esa lista manda
    para el proveedor principal (ahora con prefijo "proveedor/modelo")."""
    llm(model="claude-sonnet-5", models="claude-opus-5-5, claude-haiku-4-5-20251001")
    monkeypatch.setattr(main, "settings", config.get_settings())
    monkeypatch.setattr(llm_providers, "OpenAI", lambda *a, **k: pytest.fail("no debe consultar al proveedor"))
    data = api.get("/api/models").json()
    assert [m["id"] for m in data["models"]] == [
        "anthropic/claude-opus-5-5",
        "anthropic/claude-haiku-4-5-20251001",
        "anthropic/claude-sonnet-5",
    ]
    assert data["current_default"] == "anthropic/claude-sonnet-5"
    by_id = {m["id"]: m["valid_args"] for m in data["models"]}
    assert by_id["anthropic/claude-haiku-4-5-20251001"] == ["temperature", "max_tokens"]
    assert by_id["anthropic/claude-sonnet-5"] == ["max_tokens"]


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("claude-haiku-4-5-20251001", ["temperature", "max_tokens"]),
        ("claude-haiku-4-5", ["temperature", "max_tokens"]),
        ("claude-opus-4-6", ["temperature", "max_tokens"]),
        ("claude-sonnet-4-20250514", ["temperature", "max_tokens"]),
        ("claude-3-7-sonnet-20250219", ["temperature", "max_tokens"]),
        ("claude-opus-4-7", ["max_tokens"]),
        ("claude-opus-4-8", ["max_tokens"]),
        ("claude-sonnet-5", ["max_tokens"]),
        ("claude-opus-5-5", ["max_tokens"]),
        ("claude-fable-5-1", ["max_tokens"]),
    ],
)
def test_anthropic_models_only_get_the_sampling_params_they_accept(llm, monkeypatch, model, expected):
    """Bug real (2026-09-27): un "hola" rechazado por los tres modelos porque se
    mandaban temperature y top_p juntos ("`top_p` is deprecated for this model",
    "cannot both be specified"). Con Anthropic, nunca top_p; temperature solo a
    los modelos que todavia lo aceptan."""
    llm()
    monkeypatch.setattr(main, "settings", config.get_settings())
    assert main._valid_args_for(model) == expected


def test_other_providers_keep_the_generic_params(llm, monkeypatch):
    llm(provider="openai_compatible", base_url="https://gateway.test/v1")
    monkeypatch.setattr(main, "settings", config.get_settings())
    assert main._valid_args_for("gpt-mini") == ["temperature", "top_p", "max_tokens"]


def _fake_openai(ids=None, exc=None):
    """Reemplazo de `OpenAI` en app/llm/providers.py: solo `models.list()`."""

    def factory(*args, **kwargs):
        def _list():
            if exc:
                raise exc
            return [SimpleNamespace(id=i) for i in ids or []]

        return SimpleNamespace(models=SimpleNamespace(list=_list))

    return factory


def test_without_env_list_the_provider_list_is_used(api, llm, monkeypatch):
    llm(model="gpt-mini", models="")
    monkeypatch.setattr(main, "settings", config.get_settings())
    monkeypatch.setattr(llm_providers, "OpenAI", _fake_openai(["gpt-a", "gpt-b"]))
    ids = [m["id"] for m in api.get("/api/models").json()["models"]]
    assert ids == ["anthropic/gpt-a", "anthropic/gpt-b", "anthropic/gpt-mini"]  # el por defecto siempre esta


def test_provider_failure_leaves_the_default_model_and_a_plain_warning(api, llm, monkeypatch):
    llm(model="claude-sonnet-5", models="")
    monkeypatch.setattr(main, "settings", config.get_settings())
    monkeypatch.setattr(llm_providers, "OpenAI", _fake_openai(exc=RuntimeError("sin red")))
    data = api.get("/api/models").json()
    assert [m["id"] for m in data["models"]] == ["anthropic/claude-sonnet-5"]
    # El aviso nombra al proveedor y suma una frase con que hacer (spec proxy-autoheal REQ-006, traida de la version de origen).
    assert data["warning"].startswith("No se pudo consultar la lista de modelos de: Claude.") and "sin red" not in data["warning"]
    assert main._MODELS_CACHE["data"] is None  # se reintenta en la proxima apertura


# ---------------------------------------------------------------------------
# REQ-004: proxy de la empresa
# ---------------------------------------------------------------------------
def test_env_proxy_applies_to_the_whole_process_and_localhost_stays_direct(monkeypatch):
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"):
        monkeypatch.delenv(var, raising=False)
    settings = SimpleNamespace(http_proxy="", https_proxy="http://proxy.empresa.test:8080", no_proxy="intranet.local")
    config._apply_proxy(settings)
    assert os.environ["HTTPS_PROXY"] == "http://proxy.empresa.test:8080"
    assert os.environ["NO_PROXY"].split(",")[0] == "intranet.local"
    assert "127.0.0.1" in os.environ["NO_PROXY"]


def test_proxy_already_defined_in_windows_is_not_overwritten(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.windows.test:3128")
    monkeypatch.setenv("NO_PROXY", "ya-definido")
    config._apply_proxy(SimpleNamespace(http_proxy="", https_proxy="http://otro.test:1", no_proxy=""))
    assert os.environ["HTTPS_PROXY"] == "http://proxy.windows.test:3128"
    assert os.environ["NO_PROXY"] == "ya-definido"


def test_without_proxy_nothing_is_touched(monkeypatch):
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"):
        monkeypatch.delenv(var, raising=False)
    config._apply_proxy(SimpleNamespace(http_proxy="", https_proxy="", no_proxy=""))
    assert not any(os.environ.get(v) for v in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"))


# ---------------------------------------------------------------------------
# REQ-005: nombre de la empresa
# ---------------------------------------------------------------------------
def test_company_name_reaches_the_status_and_the_prompt(monkeypatch):
    from app.llm import loop as loop_module

    settings = config.get_settings()
    # Otro test pudo recrear la configuracion (`get_settings.cache_clear`): `main` usa la suya, se la alinea con la de aca.
    monkeypatch.setattr(main, "settings", settings)
    monkeypatch.setattr(settings, "vision_company_name", "Acme Logistica")
    assert TestClient(main.app).get("/api/status").json()["company_name"] == "Acme Logistica"
    monkeypatch.setattr(loop_module, "list_available_skills_xml", lambda session_id: "")
    monkeypatch.setattr(loop_module, "build_project_context_prompt", lambda session_id: "")
    monkeypatch.setattr(loop_module, "get_or_create_browser_dir", lambda session_id: "C:/tmp/browser")
    prompt = loop_module._build_system_prompt("sesion-x")
    assert prompt.startswith("Sos Vision, un asistente superinteligente para Acme Logistica.")


def test_without_company_name_the_prompt_has_no_placeholder(monkeypatch):
    from app.llm import loop as loop_module

    monkeypatch.setattr(config.get_settings(), "vision_company_name", "")
    monkeypatch.setattr(loop_module, "list_available_skills_xml", lambda session_id: "")
    monkeypatch.setattr(loop_module, "build_project_context_prompt", lambda session_id: "")
    monkeypatch.setattr(loop_module, "get_or_create_browser_dir", lambda session_id: "C:/tmp/browser")
    prompt = loop_module._build_system_prompt("sesion-x")
    assert prompt.startswith("Sos Vision, un asistente superinteligente. ")
    assert "{company_clause}" not in prompt


# ---------------------------------------------------------------------------
# REQ-007: nada propio de una empresa ni datos personales en el codigo
# ---------------------------------------------------------------------------
_REPO = Path(__file__).resolve().parents[2]
_FORBIDDEN = re.compile(
    "|".join(
        [
            "fo" + "rd",  # partido para que este archivo no se detecte a si mismo
            "must" + "ang",
            "pach" + "eco",
            "jgam" + "ez",
            "piv" + "pn",
            r"internet\.[a-z]+\.com:83",
        ]
    ),
    re.IGNORECASE,
)


def test_no_company_specific_or_personal_data_in_the_repo():
    hits = []
    for path in _REPO.rglob("*"):
        parts = set(path.parts)
        if not path.is_file() or parts & {".venv", "vendor", "__pycache__", ".git", "node_modules", "logs", ".pytest_cache"}:
            continue
        if path.suffix.lower() not in {".py", ".js", ".html", ".css", ".bat", ".ini", ".json", ".md", ".txt", ".example"}:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if _FORBIDDEN.search(line):
                hits.append(f"{path.relative_to(_REPO)}:{number}: {line.strip()[:100]}")
    assert hits == []
