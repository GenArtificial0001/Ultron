"""Unit de `app/token_estimate.py`: heuristica ~4 chars/token del medidor de
contexto. Es la base de la decision de compactar (85%), asi que un cambio
silencioso de formula debe romper un test."""

from __future__ import annotations

import pytest

from app.token_estimate import CHARS_PER_TOKEN_ESTIMATE, estimate_tokens


def test_empty_history_is_zero():
    assert estimate_tokens([]) == 0


def test_string_content_uses_chars_per_token():
    msgs = [{"role": "user", "content": "a" * 400}]
    assert estimate_tokens(msgs) == 400 // CHARS_PER_TOKEN_ESTIMATE


def test_multimodal_content_counts_only_text_parts():
    msgs = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "x" * 40},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
            ],
        }
    ]
    assert estimate_tokens(msgs) == 10


def test_tool_calls_count_name_and_arguments():
    msgs = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"function": {"name": "bash", "arguments": "y" * 36}}],
        }
    ]
    assert estimate_tokens(msgs) == (4 + 36) // 4


def test_messages_without_content_or_calls_are_ignored():
    assert estimate_tokens([{"role": "assistant"}, {"role": "tool", "content": None}]) == 0


@pytest.mark.parametrize("n_chars", [3, 4, 7, 8, 1001])
def test_integer_division_never_rounds_up(n_chars):
    assert estimate_tokens([{"role": "user", "content": "z" * n_chars}]) == n_chars // 4
