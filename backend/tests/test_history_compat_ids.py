"""BL-027: ids de tool-call que se mandan a modelos que no son Gemini (`llm/history_compat.py`).

`test_model_fallback.py` cubre el caso feliz de `history_for_model`. Aca se fijan los bordes que el sondeo del 2026-10-03 midio: el tope de 40 caracteres tambien
con sufijos de repeticion, la firma de pensamiento de Gemini repetida entre turnos, ids vacios, y que `_safe_id` sea ESTABLE (el mismo id da siempre el mismo
resultado: la llamada y su respuesta tienen que seguir apuntandose entre si). El hash solo ACORTA ids; no protege nada, por eso va con `usedforsecurity=False`."""

from __future__ import annotations

import re

import pytest

from app.llm import history_compat as hc

VALID = re.compile(r"^[a-zA-Z0-9_-]{1,40}$")
GEMINI_SIG = "call_1707955200_1__thought__" + "A+/=" * 400


def call(call_id: str) -> dict:
    return {"id": call_id, "type": "function", "function": {"name": "x", "arguments": "{}"}}


def turn(call_id: str) -> list[dict]:
    return [
        {"role": "assistant", "content": None, "tool_calls": [call(call_id)]},
        {"role": "tool", "tool_call_id": call_id, "content": "r"},
    ]


class TestSafeId:
    @pytest.mark.parametrize("call_id", ["call_1", "abc-DEF_123", "a" * 40, "x"])
    def test_a_valid_id_is_returned_untouched(self, call_id):
        assert hc._safe_id(call_id) == call_id

    @pytest.mark.parametrize("call_id", ["a" * 41, GEMINI_SIG, "con espacio", "con+mas/signos=", "ñandú", ""])
    def test_an_invalid_id_becomes_a_short_valid_one(self, call_id):
        out = hc._safe_id(call_id)

        assert VALID.match(out) and out.startswith("call_") and len(out) == 29

    def test_the_replacement_is_stable_across_calls(self):
        assert hc._safe_id(GEMINI_SIG) == hc._safe_id(GEMINI_SIG)

    def test_different_invalid_ids_get_different_replacements(self):
        assert hc._safe_id(GEMINI_SIG) != hc._safe_id(GEMINI_SIG + "x")

    def test_the_hash_is_declared_as_not_security_related(self, monkeypatch):
        """El sha1 solo acorta: se llama con `usedforsecurity=False` (bandit S324; ademas, en un Python en modo FIPS un SHA-1 "de seguridad" lanzaria).
        Se observa la LLAMADA real a `hashlib.sha1`, no el texto del codigo: una docstring que mencione el argumento no puede engañar a este test."""
        seen: list[dict] = []
        real = hc.hashlib.sha1

        def spy(data=b"", **kwargs):
            seen.append(kwargs)
            return real(data, **kwargs)

        monkeypatch.setattr(hc.hashlib, "sha1", spy)

        hc._safe_id("id con espacios que hay que reemplazar")

        assert seen == [{"usedforsecurity": False}]

    def test_a_valid_id_never_touches_the_hash(self, monkeypatch):
        monkeypatch.setattr(hc.hashlib, "sha1", lambda *a, **k: pytest.fail("un id valido no debe hashearse"))

        assert hc._safe_id("call_1") == "call_1"


class TestUnique:
    def test_a_free_candidate_is_kept(self):
        assert hc._unique("call_a", set()) == "call_a"

    def test_repeats_get_numeric_suffixes_and_stay_valid(self):
        seen: set[str] = set()
        base = "call_" + "a" * 24

        ids = [hc._unique(base, seen) for _ in range(1200)]

        assert len(set(ids)) == 1200 and all(VALID.match(i) for i in ids)

    def test_the_seen_set_is_updated(self):
        seen: set[str] = set()
        hc._unique("call_a", seen)
        assert seen == {"call_a"}


class TestHistoryForModel:
    def test_gemini_gets_the_very_same_list(self):
        msgs = turn(GEMINI_SIG)
        assert hc.history_for_model(msgs, "gemini-3.1-pro") is msgs

    def test_a_clean_history_is_returned_as_is_for_other_models(self):
        msgs = [
            {"role": "assistant", "content": "ok", "tool_calls": [call("call_1")]},
            {"role": "tool", "tool_call_id": "call_1", "content": "r"},
        ]
        assert hc.history_for_model(msgs, "claude-opus") is msgs

    def test_the_same_gemini_id_repeated_across_turns_gets_unique_valid_ids(self):
        msgs = turn(GEMINI_SIG) + turn(GEMINI_SIG) + turn(GEMINI_SIG)

        out = hc.history_for_model(msgs, "claude-opus")

        ids = [c["id"] for m in out if m["role"] == "assistant" for c in m["tool_calls"]]
        assert len(set(ids)) == 3 and all(VALID.match(i) for i in ids)

    def test_each_tool_result_points_to_the_id_of_its_own_call(self):
        msgs = turn(GEMINI_SIG) + turn(GEMINI_SIG)

        out = hc.history_for_model(msgs, "gpt-5")

        pairs = [
            (m["tool_calls"][0]["id"], out[i + 1]["tool_call_id"])
            for i, m in enumerate(out)
            if m["role"] == "assistant"
        ]
        assert all(a == b for a, b in pairs) and len({a for a, _ in pairs}) == 2

    def test_an_empty_id_still_pairs_up(self):
        out = hc.history_for_model(turn(""), "gpt-5")

        new_id = out[0]["tool_calls"][0]["id"]
        assert VALID.match(new_id) and out[1]["tool_call_id"] == new_id

    def test_none_content_becomes_an_empty_string_for_claude(self):
        out = hc.history_for_model(turn("call_1"), "claude-opus")

        assert out[0]["content"] == ""

    def test_the_original_history_is_never_modified(self):
        msgs = turn(GEMINI_SIG)
        snapshot = [dict(m) for m in msgs]

        hc.history_for_model(msgs, "claude-opus")

        assert msgs == snapshot and msgs[0]["content"] is None and msgs[0]["tool_calls"][0]["id"] == GEMINI_SIG

    def test_a_tool_result_without_a_matching_call_still_gets_a_valid_id(self):
        out = hc.history_for_model([{"role": "tool", "tool_call_id": GEMINI_SIG, "content": "r"}], "claude-opus")

        assert VALID.match(out[0]["tool_call_id"])

    def test_text_only_messages_pass_through_untouched(self):
        msgs = [{"role": "user", "content": "hola"}, {"role": "assistant", "content": "chau"}]
        assert hc.history_for_model(msgs, "claude-opus") is msgs
