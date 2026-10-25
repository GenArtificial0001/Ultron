"""spec aviso-contexto-largo (backend): estimacion conservadora, error de «prompt demasiado largo» SIN codigo HTTP,
`/compact` que dice la verdad al fallar y que usa el modelo correcto, y `level` en `/api/context`.

Caso REAL (sesion af35bb57, 2026-10-23): 2.971.848 caracteres; `chars/4` estimaba 742.962 tokens (74 %) y el proveedor de IA conto 1.015.295
(2,93 chars/token), asi que la compactacion al 85 % nunca se disparaba; el error llego crudo como `openai.APIError` sin status
y caia en MODELO-DESCONOCIDO; `/compact` fallo por un 429 de gasto y devolvia un 500 mudo."""

from __future__ import annotations

import asyncio

import openai
import pytest
from fastapi.testclient import TestClient

from app import main
from app.friendly_errors import describe_model_error
from app.persistence import db, master_index
from app.persistence import service as persistence
from app.session import compaction as cp
from app.session.events import get_session_store
from app.token_estimate import CHARS_PER_TOKEN_ESTIMATE, estimate_tokens

REAL_CHARS = 2_971_848
REAL_TOKENS = 1_015_295
REAL_ERROR = f"prompt is too long: {REAL_TOKENS} tokens > 1000000 maximum"
SID = "sesion-contexto-largo"


def _api_error(text: str) -> openai.APIError:
    import httpx

    return openai.APIError(text, httpx.Request("POST", "https://x.invalid/v1/chat/completions"), body=None)


# ---------------------------------------------------------------------------------------------------------------------
# REQ-001 / REQ-002: la estimacion deja de subestimar
# ---------------------------------------------------------------------------------------------------------------------
def test_chars_per_token_is_the_measured_conservative_value():
    assert CHARS_PER_TOKEN_ESTIMATE == 3


def test_three_thousand_chars_are_one_thousand_tokens():
    assert estimate_tokens([{"role": "user", "content": "a" * 3_000}]) == 1_000


def test_the_real_case_is_estimated_at_or_above_99_percent_not_74():
    tokens = estimate_tokens([{"role": "user", "content": "a" * REAL_CHARS}])
    assert tokens >= 990_000
    assert tokens / 1_000_000 * 100 >= 99


def test_auto_compaction_fires_for_the_real_case(monkeypatch):
    called: list[str] = []

    async def fake_compact(session_id, messages, window, model):
        called.append(session_id)
        return True

    monkeypatch.setattr(cp, "compact_session", fake_compact)
    messages = [{"role": "user", "content": "a" * REAL_CHARS}]
    assert asyncio.run(cp.maybe_compact("s", messages, 1_000_000, "m")) is True
    assert called == ["s"]


# ---------------------------------------------------------------------------------------------------------------------
# REQ-003 / REQ-004: el error sin codigo HTTP se reconoce; nada mas cambia
# ---------------------------------------------------------------------------------------------------------------------
def test_the_real_error_without_status_is_a_too_long_conversation():
    friendly = describe_model_error(_api_error(REAL_ERROR), "claude-sonnet-latest")
    assert friendly.code == "MODELO-CONVERSACION-LARGA"


@pytest.mark.parametrize(
    "text",
    [
        "input is too long for this model",
        "This model's maximum context length is 128000 tokens",
        "context_length_exceeded",
        "too many tokens in the request",
        "Input token count exceeds the limit",
    ],
)
def test_other_phrases_without_status_are_also_too_long(text):
    assert describe_model_error(_api_error(text), "m").code == "MODELO-CONVERSACION-LARGA"


def test_the_message_offers_compact_and_clear_without_raw_text_or_jargon():
    msg = describe_model_error(_api_error(REAL_ERROR), "claude-sonnet-latest").message
    assert "/compact" in msg and "/clear" in msg
    for jargon in ("1015295", "openai", "APIError", "tokens >", "HTTP", "JSON"):
        assert jargon not in msg


def test_the_old_400_still_gets_the_same_code_and_now_offers_clear_too():
    exc = _api_error("Error code: 400 - prompt is too long")
    exc.status_code = 400  # type: ignore[attr-defined]
    friendly = describe_model_error(exc, "m")
    assert friendly.code == "MODELO-CONVERSACION-LARGA" and friendly.status == 400
    assert "/compact" in friendly.message and "/clear" in friendly.message


@pytest.mark.parametrize(
    "text, status, code",
    [
        ("Error code: 429 - spend limit $3 per 600 seconds", 429, "MODELO-LIMITE-USUARIO"),
        ("Error code: 400 - content_filter", 400, "MODELO-FILTRO"),
        ("Error code: 400 - This model does not support tools argument", 400, "MODELO-SIN-HERRAMIENTAS"),
        ("Error code: 401 - unauthorized", 401, "MODELO-SESION"),
        ("Error code: 403 - forbidden", 403, "MODELO-PERMISO"),
        ("Error code: 404 - not found", 404, "MODELO-NO-DISPONIBLE"),
        ("Error code: 500 - boom", 500, "MODELO-CAIDO"),
        ("algo raro que nadie conoce", None, "MODELO-DESCONOCIDO"),
    ],
)
def test_nothing_else_changes_its_classification(text, status, code):
    exc = _api_error(text)
    if status is not None:
        exc.status_code = status  # type: ignore[attr-defined]
    assert describe_model_error(exc, "m").code == code


# ---------------------------------------------------------------------------------------------------------------------
# Sesion guardada en SQLite para los endpoints
# ---------------------------------------------------------------------------------------------------------------------
@pytest.fixture
def client():
    return TestClient(main.app)


@pytest.fixture
def saved(tmp_path, tmp_master_db):
    root = tmp_path / "proyecto"
    root.mkdir()
    master_index.set_project_path(
        SID, str(root), windows_user_created=f"{persistence.current_windows_user()}_20261023000000"
    )
    path = db.db_path_for(str(root))
    db.ensure_session(path, SID, 1.0)
    db.insert_message(path, "m1", SID, "user", 1.0)
    db.insert_part(path, "m1-t", "m1", SID, "text", {"text": "x" * 6_000}, 1.0)
    db.insert_message(path, "a1", SID, "assistant", 2.0)
    db.insert_part(path, "a1-t", "a1", SID, "text", {"text": "respuesta"}, 2.0)
    store = get_session_store()
    store.delete(SID)
    yield path
    store.delete(SID)


# ---------------------------------------------------------------------------------------------------------------------
# REQ-005 / REQ-006: /compact
# ---------------------------------------------------------------------------------------------------------------------
def _spend_429() -> openai.APIError:
    exc = _api_error("Error code: 429 - spend limit exceeded: $3 per 600 seconds")
    exc.status_code = 429  # type: ignore[attr-defined]
    return exc


def test_compact_with_a_spend_429_returns_a_friendly_error_and_keeps_the_session(saved, client, monkeypatch):
    async def failing(*_a, **_k):
        raise _spend_429()

    monkeypatch.setattr(main, "compact_session", failing)
    before = len(get_session_store().get_or_restore(SID).messages)

    resp = client.post(f"/api/commands/compact/{SID}")

    assert resp.status_code in (429, 502, 503)
    detail = resp.json()["detail"]
    assert isinstance(detail, str) and "tope de consumo" in detail
    for jargon in ("$3", "600 seconds", "openai", "APIError", "Traceback"):
        assert jargon not in detail
    assert len(get_session_store().get_or_restore(SID).messages) == before


def test_compact_with_any_other_model_failure_uses_the_friendly_text(saved, client, monkeypatch):
    async def failing(*_a, **_k):
        raise ConnectionError("boom crudo")

    monkeypatch.setattr(main, "compact_session", failing)
    resp = client.post(f"/api/commands/compact/{SID}")
    assert resp.status_code >= 400
    assert "boom crudo" not in resp.text
    assert "No pude conectarme" in resp.json()["detail"]


def test_compact_without_a_body_behaves_as_always(saved, client, monkeypatch):
    seen: list[str] = []

    async def fake(session_id, messages, window, model):
        seen.append(model)
        return True

    monkeypatch.setattr(main, "compact_session", fake)
    monkeypatch.setattr(main.settings, "llm_model", "modelo-de-casa", raising=False)
    monkeypatch.setattr(
        main.model_fallback, "load_settings", lambda: main.model_fallback.FallbackSettings(False, (), 5)
    )

    resp = client.post(f"/api/commands/compact/{SID}")

    assert resp.status_code == 200 and resp.json() == {"compacted": True}
    assert seen == ["modelo-de-casa"]


def test_compact_uses_the_selected_model(saved, client, monkeypatch):
    seen: list[str] = []

    async def fake(session_id, messages, window, model):
        seen.append(model)
        return True

    monkeypatch.setattr(main, "compact_session", fake)
    monkeypatch.setattr(main.settings, "llm_model", "modelo-de-casa", raising=False)
    monkeypatch.setattr(
        main.model_fallback, "load_settings", lambda: main.model_fallback.FallbackSettings(False, (), 5)
    )

    resp = client.post(f"/api/commands/compact/{SID}", json={"model": "el-elegido"})

    assert resp.status_code == 200 and resp.json() == {"compacted": True}
    assert seen == ["el-elegido"]


def test_compact_with_fallback_active_uses_the_first_available_model(saved, client, monkeypatch):
    seen: list[str] = []

    async def fake(session_id, messages, window, model):
        seen.append(model)
        return True

    class FakePicker:
        def __init__(self, *_a, **_k) -> None:
            pass

        def first_available(self) -> str:
            return "el-primero-disponible"

    monkeypatch.setattr(main, "compact_session", fake)
    monkeypatch.setattr(main.settings, "llm_model", "modelo-de-casa", raising=False)
    monkeypatch.setattr(
        main.model_fallback, "load_settings", lambda: main.model_fallback.FallbackSettings(True, ("a", "b"), 5)
    )
    monkeypatch.setattr(main.model_fallback, "TurnModelPicker", FakePicker)

    resp = client.post(f"/api/commands/compact/{SID}", json={"model": "el-elegido"})

    assert resp.status_code == 200
    assert seen == ["el-primero-disponible"]


def test_compact_still_404_for_unknown_session_and_403_for_another_user(saved, client, monkeypatch):
    assert client.post("/api/commands/compact/no-existe-en-ningun-lado", json={"model": "x"}).status_code == 404
    monkeypatch.setattr(persistence, "current_windows_user", lambda: "otro.usuario")
    assert client.post(f"/api/commands/compact/{SID}", json={"model": "x"}).status_code == 403


# ---------------------------------------------------------------------------------------------------------------------
# REQ-007: /api/context suma `level`
# ---------------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "pct, level",
    [
        (0, "ok"),
        (59, "ok"),
        (60, "warn"),
        (74, "warn"),
        (75, "high"),
        (89, "high"),
        (90, "critical"),
        (100, "critical"),
    ],
)
def test_the_level_for_each_boundary(pct, level):
    assert main._context_level(pct) == level


def test_context_keeps_the_four_old_fields_and_adds_level(saved, client, monkeypatch):
    monkeypatch.setattr(main, "resolve_context_window", lambda model_id: 10_000)
    data = client.get(f"/api/context/{SID}").json()
    assert {"tokens", "context_window", "pct_used", "estimated"} <= set(data)
    assert data["estimated"] is True and data["context_window"] == 10_000
    assert data["tokens"] > 0
    assert data["level"] == main._context_level(data["pct_used"])


def test_context_measures_against_the_selected_model_window(saved, client, monkeypatch):
    windows = {"chico": 10_000, "grande": 1_000_000}
    monkeypatch.setattr(main.settings, "llm_model", "chico", raising=False)
    monkeypatch.setattr(main, "resolve_context_window", lambda model_id: windows.get(model_id, 200_000))

    default = client.get(f"/api/context/{SID}").json()
    chosen = client.get(f"/api/context/{SID}", params={"model": "grande"}).json()

    assert default["context_window"] == 10_000
    assert chosen["context_window"] == 1_000_000
    assert chosen["pct_used"] < default["pct_used"]


def test_context_of_an_unknown_session_has_level_ok(client):
    data = client.get("/api/context/sesion-que-no-existe-jamas").json()
    assert data["tokens"] == 0 and data["level"] == "ok"


@pytest.mark.parametrize("window, level", [(1_000_000, "ok"), (3_200, "warn"), (2_500, "high"), (2_200, "critical")])
def test_context_endpoint_reports_the_level_of_the_real_percentage(saved, client, monkeypatch, window, level):
    """Cierra el mutante «level fijo en ok»: con una ventana chica el porcentaje REAL es alto y el endpoint lo dice."""
    monkeypatch.setattr(main, "resolve_context_window", lambda model_id: window)
    data = client.get(f"/api/context/{SID}").json()
    assert data["level"] == level, data
