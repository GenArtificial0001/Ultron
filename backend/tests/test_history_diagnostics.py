"""Diagnostico estructural del historial (`app/llm/diagnostics.py`).

Caso real 2026-09-24 (sesion 8619176d, gemini-3.1-pro): el proveedor de IA respondio 400
"the number of function response parts is equal to the number of function
call parts" y el log no guardaba COMO estaba armado el historial. Estos tests
fijan que el resumen detecta las anomalias que los proveedores rechazan y que
NUNCA incluye contenido (texto del usuario, argumentos ni resultados).
"""

from __future__ import annotations

import pytest

from app.llm.diagnostics import describe_history_shape

pytestmark = pytest.mark.unit

SECRET = "CONTENIDO-PRIVADO-DEL-USUARIO"
# Id real de Gemini: el sufijo `__thought__` trae la firma de pensamiento, con
# `+ / =` y miles de caracteres (5116 en el caso real).
GEMINI_ID = "call_1707955200_0__thought__" + "Ab+/9=" * 850


def call(call_id: str, name: str = "read") -> dict:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": SECRET}}


def assistant(*ids: str) -> dict:
    return {"role": "assistant", "content": SECRET, "tool_calls": [call(i) for i in ids]}


def tool(call_id: str) -> dict:
    return {"role": "tool", "tool_call_id": call_id, "content": SECRET}


def user(content: object = SECRET) -> dict:
    return {"role": "user", "content": content}


SYSTEM = {"role": "system", "content": SECRET}


def test_healthy_history_has_no_anomalies_and_compact_sequence() -> None:
    messages = [
        SYSTEM,
        user(),
        assistant("a1", "a2", "a3"),
        tool("a1"),
        tool("a2"),
        tool("a3"),
        {"role": "assistant", "content": SECRET},
        user(),
    ]
    shape, anomalies = describe_history_shape(messages)
    assert shape == "S U A3 T×3 A U"
    assert anomalies == []


def test_missing_tool_response_is_reported_where_it_breaks() -> None:
    # Exactamente lo que Gemini rechaza: 2 llamadas, 1 sola respuesta.
    shape, anomalies = describe_history_shape([SYSTEM, user(), assistant("a1", "a2"), tool("a1"), user()])
    assert shape == "S U A2 T U"
    assert anomalies == ["[4] user llega con 1 tool-call(s) sin respuesta: a2"]


def test_pending_calls_at_the_end_are_reported() -> None:
    _, anomalies = describe_history_shape([SYSTEM, user(), assistant("a1")])
    assert anomalies == ["[3] fin del historial llega con 1 tool-call(s) sin respuesta: a1"]


def test_orphan_tool_response_is_reported() -> None:
    _, anomalies = describe_history_shape([SYSTEM, user(), tool("fantasma")])
    assert anomalies == ["[2] respuesta de tool sin tool-call pendiente: fantasma"]


def test_ids_repeated_across_turns_are_reported() -> None:
    # Caso real: Gemini repite `call_1707955200_1` en turnos distintos.
    messages = [
        SYSTEM,
        user(),
        assistant("call_1707955200_1"),
        tool("call_1707955200_1"),
        user(),
        assistant("call_1707955200_1"),
        tool("call_1707955200_1"),
    ]
    _, anomalies = describe_history_shape(messages)
    assert anomalies == ["[5] id repetido (ya usado en [2]): call_1707955200_1"]


def test_long_gemini_signature_id_is_flagged_and_truncated_in_the_log() -> None:
    _, anomalies = describe_history_shape([SYSTEM, user(), assistant(GEMINI_ID), tool(GEMINI_ID)])
    assert len(anomalies) == 1
    assert "caracteres no aceptados" in anomalies[0]
    assert f"({len(GEMINI_ID)} caracteres)" in anomalies[0]
    assert len(anomalies[0]) < 150  # nunca la firma completa en el log


def test_system_message_in_the_middle_is_reported() -> None:
    # El guardrail de promesas vacias (0.17.5) inyecta `system` a mitad del turno.
    _, anomalies = describe_history_shape([SYSTEM, user(), {"role": "assistant", "content": "x"}, SYSTEM])
    assert anomalies == ["[3] mensaje system a mitad de la conversacion"]


def test_images_missing_ids_and_unknown_roles() -> None:
    image_user = user([{"type": "text", "text": SECRET}, {"type": "image_url", "image_url": {"url": "data:"}}])
    messages = [
        SYSTEM,
        image_user,
        {"role": "assistant", "content": None, "tool_calls": [{"type": "function"}]},
        {"role": "developer", "content": SECRET},
    ]
    shape, anomalies = describe_history_shape(messages)
    assert shape == "S U+img A1 ?"
    assert anomalies == ["[2] tool-call sin id", "[3] rol desconocido: 'developer'"]


def test_never_includes_any_content() -> None:
    messages = [SYSTEM, user(), assistant("a1", GEMINI_ID), tool("a1"), user(), assistant("a1")]
    shape, anomalies = describe_history_shape(messages)
    assert SECRET not in shape
    assert all(SECRET not in line for line in anomalies)


def test_anomalies_are_capped_for_huge_histories() -> None:
    messages = [SYSTEM] + [tool(f"x{i}") for i in range(200)]
    shape, anomalies = describe_history_shape(messages)
    assert shape == "S T×200"
    assert len(anomalies) == 20


def test_empty_history() -> None:
    assert describe_history_shape([]) == ("", [])
