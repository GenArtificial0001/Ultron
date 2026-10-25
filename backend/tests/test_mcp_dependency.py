"""`mcp` (cliente del Model Context Protocol) tiene que venir en TODAS las instalaciones (spec office-outlook-teams-mcp, REQ-M01).

Por que: Vision pasa a ser cliente MCP del conector de la empresa (EAKC `mcp-msgraph`, Outlook y Teams). Si el paquete solo estuviera en el entorno de dev, prod y el .exe arrancarian
sin poder iniciar sesion. Se vigila lo mismo que con python-pptx (`test_pptx_dependency.py`):
1. `backend/requirements.txt` lo declara con piso de version Y con tope (<2: la 2.0.0 cambia la API; se probo la 1.30.0);
2. ese archivo viaja a prod y al .exe, y el paquete NO esta en `requirements-dev.txt` (si estuviera solo ahi, prod no lo tendria);
3. se IMPORTA de verdad y expone lo que usa el cliente de Vision, no solo figura en un archivo."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
REQUIREMENTS = BACKEND / "requirements.txt"
REQUIREMENTS_DEV = BACKEND / "requirements-dev.txt"


def _declared(path: Path) -> list[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [
        re.split(r"[<>=!~\[ ;#,]", line.strip(), maxsplit=1)[0].lower()
        for line in lines
        if line.strip() and not line.lstrip().startswith("#")
    ]


def test_requirements_declares_mcp_with_a_floor_and_a_ceiling():
    text = REQUIREMENTS.read_text(encoding="utf-8")

    assert "mcp" in _declared(REQUIREMENTS)
    # piso: lo que trae la API que usa Vision; tope: la 2.0.0 existe en el indice de la empresa y NO se probo
    assert re.search(r"^mcp>=1\.25,<2\s*$", text, re.MULTILINE), "tiene que ser mcp>=1.25,<2"


def test_mcp_is_in_the_runtime_requirements_not_only_in_the_dev_ones():
    """Si solo estuviera en requirements-dev.txt, prod no lo tendria: ese archivo nunca viaja."""
    assert "mcp" not in _declared(REQUIREMENTS_DEV)


def test_mcp_imports_and_offers_what_the_vision_client_uses():
    pytest.importorskip("mcp", reason="mcp no esta instalado en este entorno: justo lo que este pedido evita")
    from mcp import ClientSession
    from mcp.client.auth import OAuthClientProvider, TokenStorage
    from mcp.client.streamable_http import streamable_http_client  # el vigente; el deprecado (`streamablehttp_client`) ya no se usa en Vision

    assert callable(streamable_http_client)
    assert {"initialize", "list_tools", "call_tool"} <= set(dir(ClientSession))
    assert {"get_tokens", "set_tokens", "get_client_info", "set_client_info"} <= set(dir(TokenStorage))
    assert OAuthClientProvider is not None
