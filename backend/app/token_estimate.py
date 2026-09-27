"""
Estimacion APROXIMADA de tokens usados por una sesion, para el medidor de
"Contexto: X tokens / Y% usado" del sidebar (inspirado en el panel de
contexto de OpenCode). No usa un tokenizador exacto por proveedor (Claude/
Gemini/GPT-5.x cada uno tiene el suyo, y no todos exponen una libreria
publica equivalente a tiktoken) -- se usa la heuristica estandar de ~4
caracteres por token en ingles/codigo, documentada ampliamente como
aproximacion razonable para UIs de este tipo. Se etiqueta explicitamente
como estimado en la UI para no generar falsa precision.
"""

from __future__ import annotations

from typing import Any

CHARS_PER_TOKEN_ESTIMATE = 4


def estimate_tokens(messages: list[dict[str, Any]]) -> int:
    """Suma aproximada de tokens de todo el historial de mensajes
    (formato OpenAI chat.completions) que efectivamente se manda al LLM
    en cada request -- incluye system prompt, historial de usuario/
    asistente, y resultados de tools ya en el historial."""
    total_chars = 0
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, str):
            total_chars += len(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and "text" in part:
                    total_chars += len(str(part["text"]))
        # tool_calls tambien ocupan contexto (nombre + argumentos)
        for call in msg.get("tool_calls") or []:
            fn = call.get("function", {})
            total_chars += len(fn.get("name", "")) + len(fn.get("arguments", ""))

    return total_chars // CHARS_PER_TOKEN_ESTIMATE
