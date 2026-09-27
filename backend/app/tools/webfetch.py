"""
Tool `webfetch`: trae el contenido de una URL y lo convierte a Markdown
legible, replicando el rol de packages/core/src/tool/webfetch.ts de
OpenCode (que usa `turndown`) con `markdownify` como equivalente Python.
"""

from __future__ import annotations

from typing import Any

import requests
from markdownify import markdownify

from app.tools.base import Tool

MAX_BYTES = 5_000_000
TIMEOUT = 30


def _webfetch_execute(args: dict, session: Any = None) -> str:
    url = args.get("url", "")
    fmt = args.get("format", "markdown")
    if not url:
        return "Error: falta el argumento 'url'."
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    try:
        resp = requests.get(
            url,
            timeout=TIMEOUT,
            headers={"User-Agent": "Mozilla/5.0 (Vision-Agent)"},
            stream=True,
        )
    except requests.RequestException as exc:
        return f"Error obteniendo la URL: {exc}"

    if resp.status_code >= 400:
        return f"Error: la URL respondio con status {resp.status_code}."

    content = resp.raw.read(MAX_BYTES + 1, decode_content=True)
    truncated = len(content) > MAX_BYTES
    if truncated:
        content = content[:MAX_BYTES]

    text = content.decode(resp.encoding or "utf-8", errors="replace")

    if fmt == "text" or fmt == "html":
        result = text
    else:
        try:
            result = markdownify(text)
        except Exception:
            result = text

    if truncated:
        result += "\n\n... (contenido truncado, excedio 5MB) ..."
    return result[:20000]


webfetch_tool = Tool(
    name="webfetch",
    description="Obtiene el contenido de una URL y lo devuelve como markdown, texto plano o html crudo.",
    parameters={
        "type": "object",
        "properties": {
            "url": {"type": "string"},
            "format": {
                "type": "string",
                "enum": ["markdown", "text", "html"],
                "default": "markdown",
            },
        },
        "required": ["url"],
    },
    execute=_webfetch_execute,
)
