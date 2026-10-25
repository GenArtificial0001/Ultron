"""spec varios-proveedores: varios proveedores de IA a la vez (app/llm/providers.py).

Sin red: `OpenAI` se reemplaza por un falso que solo sabe listar modelos, y el
`.env` que se lee es uno temporal."""

from __future__ import annotations

import os
import time
from types import SimpleNamespace

import openai
import pytest
from fastapi.testclient import TestClient

from app import config, main
from app import project_context as pc
from app.llm import client as client_module
from app.llm import model_fallback as mf
from app.llm import providers


@pytest.fixture
def env(tmp_path, monkeypatch):
    """`.env` temporal y sin proveedor principal (LLM_* vacio)."""
    monkeypatch.setattr(providers, "BASE_DIR", tmp_path)
    settings = config.get_settings()
    for name, value in (("llm_api_key", ""), ("llm_provider", "anthropic"), ("llm_base_url", ""), ("llm_models", "")):
        monkeypatch.setattr(settings, name, value)
    monkeypatch.setattr(main, "settings", settings)
    monkeypatch.setattr(main, "_MODELS_CACHE", {"data": None, "fetched_at": 0.0})
    providers.clear_caches()
    env_file = tmp_path / ".env"

    def write(**values):
        env_file.write_text("\n".join(f"{k}={v}" for k, v in values.items()) + "\n", encoding="utf-8")
        # la fecha de modificacion tiene que cambiar aunque se escriba dos veces seguidas
        stamp = time.time() + len(values)
        os.utime(env_file, (stamp, stamp))

    yield write
    providers.clear_caches()


def fake_openai(models_by_host):
    """`OpenAI` falso: `models.list()` segun la direccion del proveedor."""

    def factory(*, api_key, base_url, default_headers=None):
        host = base_url.split("/")[2]
        ids = models_by_host.get(host, [])
        return SimpleNamespace(
            api_key=api_key,
            base_url=base_url,
            models=SimpleNamespace(list=lambda: [SimpleNamespace(id=i) for i in ids]),
        )

    return factory


MODELS = {
    "generativelanguage.googleapis.com": ["models/gemini-2.5-flash", "models/gemini-2.5-pro", "models/gemini-2.5-flash-preview-tts", "models/text-embedding-004"],
    "api.groq.com": ["openai/gpt-oss-120b", "qwen/qwen3.8-27b", "whisper-large-v3", "meta-llama/llama-prompt-guard-2-22m"],
    "api.anthropic.com": ["claude-opus-5-5", "claude-sonnet-5", "claude-haiku-4-5-20251001"],
}


# ---------------------------------------------------------------------------
# REQ-001/002: proveedores por su clave en el .env, sin direcciones
# ---------------------------------------------------------------------------
def test_each_key_in_the_env_enables_its_provider_with_its_address(env):
    env(GEMINI_API_KEY="g-1", META_GROQ_API_KEY="q-1")  # alias de Groq que uso el usuario
    active = providers.active_providers()
    assert list(active) == ["gemini", "groq"]
    assert active["gemini"].base_url.startswith("https://generativelanguage.googleapis.com")
    assert active["groq"].api_key == "q-1"


def test_changes_to_the_env_are_seen_without_restarting(env):
    env(GEMINI_API_KEY="g-1")
    assert list(providers.active_providers()) == ["gemini"]
    env(GEMINI_API_KEY="g-1", DEEPSEEK_API_KEY="d-1")
    assert list(providers.active_providers()) == ["gemini", "deepseek"]
    env(DEEPSEEK_API_KEY="d-1")
    assert list(providers.active_providers()) == ["deepseek"]


def test_the_classic_configuration_still_works(env, monkeypatch):
    """REQ-007: LLM_PROVIDER/LLM_API_KEY es el proveedor principal."""
    settings = config.get_settings()
    monkeypatch.setattr(settings, "llm_api_key", "clave-clasica")
    assert providers.principal_id() == "anthropic"
    assert providers.active_providers()["anthropic"].api_key == "clave-clasica"
    assert providers.canonical("claude-sonnet-5") == "anthropic/claude-sonnet-5"


def test_a_custom_compatible_provider(env):
    env(CUSTOM_API_KEY="c-1", CUSTOM_BASE_URL="https://ia.empresa.test/v1", CUSTOM_LABEL="IA de la empresa")
    custom = providers.active_providers()["custom"]
    assert custom.label == "IA de la empresa" and custom.base_url == "https://ia.empresa.test/v1"


# ---------------------------------------------------------------------------
# REQ-004: ids "proveedor/modelo" y cliente de cada proveedor
# ---------------------------------------------------------------------------
def test_model_ids_carry_their_provider(env):
    env(GROQ_API_KEY="q-1")
    assert providers.split_model("groq/openai/gpt-oss-120b") == ("groq", "openai/gpt-oss-120b")
    assert providers.api_model("gemini/gemini-2.5-flash") == "gemini-2.5-flash"


def test_each_model_is_sent_to_its_own_provider(env, monkeypatch):
    env(GEMINI_API_KEY="g-1", GROQ_API_KEY="q-1")
    monkeypatch.setattr(providers, "OpenAI", fake_openai({}))
    gemini = client_module.get_client(model="gemini/gemini-2.5-flash")
    groq = client_module.get_client(model="groq/openai/gpt-oss-120b")
    assert "generativelanguage" in gemini.base_url and gemini.api_key == "g-1"
    assert "api.groq.com" in groq.base_url and groq.api_key == "q-1"


def test_a_model_of_a_provider_without_key_explains_what_is_missing(env):
    env(GEMINI_API_KEY="g-1")
    with pytest.raises(client_module.ProviderNotConfigured, match="deepseek"):
        client_module.get_client(model="deepseek/deepseek-chat")


# ---------------------------------------------------------------------------
# REQ-003/004/006: selector agrupado, modelos elegidos y consentimiento
# ---------------------------------------------------------------------------
@pytest.fixture
def api(env, monkeypatch):
    monkeypatch.setattr(providers, "OpenAI", fake_openai(MODELS))
    return TestClient(main.app)


def test_the_selector_groups_models_by_provider_and_hides_non_chat_models(api, env):
    env(ANTHROPIC_API_KEY="a-1", GEMINI_API_KEY="g-1")
    pc.set_providers(consent={"gemini": True})
    models = api.get("/api/models").json()["models"]
    ids = [m["id"] for m in models]
    assert "gemini/gemini-2.5-flash" in ids  # el Flash mas nuevo del catalogo de prueba
    assert "gemini/gemini-2.5-pro" not in ids  # los Pro no se sugieren (sin cupo gratuito)
    assert not any("tts" in i or "embedding" in i for i in ids)
    flash = next(m for m in models if m["id"] == "gemini/gemini-2.5-flash")
    assert flash["provider_label"] == "Gemini" and flash["free"] is True
    assert any(m["provider_label"] == "Claude" for m in models)


def test_free_providers_are_hidden_until_the_user_confirms(api, env):
    """REQ-006: sin confirmar, Groq (gratuito) no aparece ni se puede usar."""
    env(GROQ_API_KEY="q-1", ANTHROPIC_API_KEY="a-1")
    ids = [m["id"] for m in api.get("/api/models").json()["models"]]
    assert not any(i.startswith("groq/") for i in ids)
    assert main.model_needs_consent("groq/openai/gpt-oss-120b") == "Groq"
    api.post("/api/providers", json={"consent": {"groq": True}})
    ids = [m["id"] for m in api.get("/api/models").json()["models"]]
    assert "groq/openai/gpt-oss-120b" in ids
    assert main.model_needs_consent("groq/openai/gpt-oss-120b") is None


def test_the_user_chooses_which_models_to_see(api, env):
    """REQ-003: la lista trae todos los de chat, con una sugerencia editable."""
    env(ANTHROPIC_API_KEY="a-1")
    status = {p["id"]: p for p in api.get("/api/providers").json()["providers"]}
    assert status["anthropic"]["models"] == MODELS["api.anthropic.com"]
    assert "claude-haiku-4-5-20251001" not in status["anthropic"]["suggested"]
    api.post("/api/providers", json={"models": {"anthropic": ["claude-sonnet-5"]}})
    ids = [m["id"] for m in api.get("/api/models").json()["models"]]
    assert ids == ["anthropic/claude-sonnet-5"]


def test_the_providers_endpoint_lists_the_ones_still_without_key(api, env):
    env(GEMINI_API_KEY="g-1")
    data = api.get("/api/providers").json()
    assert [p["id"] for p in data["providers"]] == ["gemini"]
    groq = next(p for p in data["available"] if p["id"] == "groq")
    assert groq["key_names"] == ["GROQ_API_KEY", "META_GROQ_API_KEY"]


def test_a_provider_that_does_not_answer_does_not_hide_the_others(api, env, monkeypatch):
    env(ANTHROPIC_API_KEY="a-1", GEMINI_API_KEY="g-1")
    pc.set_providers(consent={"gemini": True})
    real = fake_openai(MODELS)

    def flaky(*, api_key, base_url, default_headers=None):
        client = real(api_key=api_key, base_url=base_url)
        if "anthropic" in base_url:
            client.models.list = lambda: (_ for _ in ()).throw(RuntimeError("sin red"))
        return client

    monkeypatch.setattr(providers, "OpenAI", flaky)
    data = api.get("/api/models").json()
    assert any(m["id"].startswith("gemini/") for m in data["models"])
    assert "Claude" in data["warning"]


# ---------------------------------------------------------------------------
# REQ-005: salto entre proveedores
# ---------------------------------------------------------------------------
def _no_credit(model):
    """El 400 real de Anthropic sin credito (mismo armado que test_friendly_errors)."""
    request = SimpleNamespace(method="POST", url="https://api.anthropic.com/v1/chat/completions")
    response = SimpleNamespace(status_code=400, headers={}, request=request)
    return openai.BadRequestError(
        "Error code: 400 - Your credit balance is too low to access the Anthropic API.",
        response=response,
        body=None,
    )


def test_out_of_credit_pauses_the_whole_provider_and_jumps_to_another(env, monkeypatch):
    env(ANTHROPIC_API_KEY="a-1", GEMINI_API_KEY="g-1")
    order = ("anthropic/claude-opus-5-5", "anthropic/claude-sonnet-5", "gemini/gemini-2.5-flash")
    catalog = {m: {"id": m} for m in order}
    settings = mf.FallbackSettings(enabled=True, order=order, pause_minutes=10)
    pauses = mf.ModelPauses()
    picker = mf.TurnModelPicker(settings, None, order[0], catalog, pauses)
    assert picker.choose(task_started=False).model == order[0]
    assert picker.register_failure(order[0], _no_credit(order[0])) is True
    choice = picker.choose(task_started=False)
    assert choice.model == "gemini/gemini-2.5-flash"  # Sonnet (mismo proveedor) tambien quedo en pausa
    assert "se quedó sin crédito" in choice.notice
    assert pauses.paused_until("anthropic/claude-sonnet-5") is not None


def test_an_old_fallback_list_without_prefix_is_read_as_the_principal(env, monkeypatch):
    settings = config.get_settings()
    monkeypatch.setattr(settings, "llm_api_key", "clave-clasica")
    pc.set_model_fallback(order=["claude-opus-5-5", "gemini/gemini-2.5-flash"])
    assert mf.load_settings().order == ("anthropic/claude-opus-5-5", "gemini/gemini-2.5-flash")


def test_a_gemini_model_without_free_quota_is_a_limit_not_out_of_credit():
    """Medido 2026-09-27: los Pro de Gemini no tienen cupo gratuito y responden 429
    "exceeded your current quota". Es un limite de ESE modelo: no debe pausar a
    todo Gemini como si la cuenta no tuviera credito."""
    from app.friendly_errors import describe_model_error

    request = SimpleNamespace(method="POST", url="https://generativelanguage.googleapis.com/v1beta/openai/chat")
    response = SimpleNamespace(status_code=429, headers={}, request=request)
    exc = openai.RateLimitError(
        "Error code: 429 - You exceeded your current quota, please check your plan and billing details.",
        response=response,
        body=None,
    )
    assert describe_model_error(exc, "gemini/gemini-pro-latest").code != "MODELO-SIN-CREDITO"


def test_gemini_suggests_the_newest_free_flash_models():
    ids = ["gemini-2.5-flash", "gemini-2.5-pro", "gemini-flash-latest", "gemini-3.7-flash", "gemini-3.8-flash",
           "gemini-3.10-flash", "gemini-flash-lite-latest", "lyria-3.5", "nano-banana-pro-preview"]
    chat = providers.chat_models(ids)
    assert "lyria-3.5" not in chat and "nano-banana-pro-preview" not in chat
    assert providers._gemini_suggest(chat) == ["gemini-flash-latest", "gemini-3.10-flash", "gemini-flash-lite-latest"]
