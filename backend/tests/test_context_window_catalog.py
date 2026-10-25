"""Ventana de contexto real del modelo (spec leer-imagenes-pdf-word, decision D1 = A).

Hallazgo medido el 2026-10-03: `list_models` no guardaba `context_window` en el catalogo y
`resolve_context_window` buscaba por `entry["id"]`, asi que con un ALIAS (el modelo por defecto de Vision
es `claude-sonnet-latest`) tampoco lo encontraba. Resultado: TODO modelo se veia con la ventana de
respaldo (200.000), aunque declarara 1.000.000. La compactacion del historial usa esa misma funcion.

Cero red: el cliente del proveedor de IA es un fake.
"""

from __future__ import annotations

import pytest

from app import main

ROSTER = [
    {
        "id": "claude-sonnet-5",
        "aliases": ["claude-sonnet-latest"],
        "type": "LLM",
        "context_window": 1_000_000,
        "supports_image_input": True,
    },
    {"id": "gpt-5-mini", "aliases": [], "type": "LLM", "context_window": 400_000, "supports_image_input": True},
    {"id": "deepseekv4-flash", "aliases": [], "type": "LLM", "context_window": 128_000, "supports_image_input": False},
    {"id": "sin-dato", "aliases": ["sin-dato-latest"], "type": "LLM", "supports_image_input": False},
    {"id": "dato-nulo", "aliases": [], "type": "LLM", "context_window": None},
    {"id": "dato-basura", "aliases": [], "type": "LLM", "context_window": "grande"},
    {"id": "whisper-large", "aliases": [], "type": "Audio", "context_window": 999},
]


@pytest.fixture()
def catalog(monkeypatch):
    """Catalogo armado como lo deja `list_models` (misma entrada bajo el id y bajo cada alias), con la ventana que informa el
    proveedor. ULTRON: la version de origen la leia del catalogo de su gateway; aca la entrada sale de `_model_entry` y el dato
    se completa cuando el proveedor lo da (spec ia-local)."""
    models, by_id = [], {}
    for m in ROSTER:
        if m.get("type") != "LLM":
            continue
        entry = main._model_entry(m["id"])
        entry["aliases"] = m.get("aliases", [])
        entry["context_window"] = main._valid_context_window(m.get("context_window"))
        models.append(entry)
        by_id[entry["id"]] = entry
        for alias in entry["aliases"]:
            by_id[alias] = entry
    monkeypatch.setattr(main, "_MODELS_CACHE", {"data": {"models": models}, "fetched_at": 0.0, "by_id": by_id})
    return main._MODELS_CACHE["by_id"]


def test_the_catalog_keeps_the_context_window_of_each_model(catalog):
    assert catalog["claude-sonnet-5"]["context_window"] == 1_000_000
    assert catalog["gpt-5-mini"]["context_window"] == 400_000


def test_resolves_the_real_window_by_canonical_id(catalog):
    assert main.resolve_context_window("claude-sonnet-5") == 1_000_000
    assert main.resolve_context_window("deepseekv4-flash") == 128_000


def test_resolves_the_real_window_by_alias(catalog):
    assert main.resolve_context_window("claude-sonnet-latest") == 1_000_000


def test_unknown_model_falls_back_to_200k(catalog):
    assert main.resolve_context_window("no-existe") == 200_000


def test_without_a_catalog_it_falls_back_to_200k(monkeypatch):
    monkeypatch.setattr(main, "_MODELS_CACHE", {"data": None, "fetched_at": 0.0, "by_id": None})
    assert main.resolve_context_window("claude-sonnet-latest") == 200_000


@pytest.mark.parametrize("model", ["sin-dato", "sin-dato-latest", "dato-nulo", "dato-basura"])
def test_a_missing_or_invalid_window_falls_back_to_200k(catalog, model):
    """Sin el dato, nulo o con basura: el respaldo, nunca 0 ni una excepcion (dividiria por cero en la compactacion)."""
    assert main.resolve_context_window(model) == 200_000


def test_the_models_endpoint_payload_exposes_the_window_for_the_ui(catalog):
    """El selector del frontend recibe la lista de `list_models`; el dato tambien tiene que estar ahi."""
    payload = main._MODELS_CACHE["data"]
    by_id = {m["id"]: m for m in payload["models"]}
    assert by_id["claude-sonnet-5"].get("context_window") == 1_000_000
    assert by_id["sin-dato"].get("context_window") is None
