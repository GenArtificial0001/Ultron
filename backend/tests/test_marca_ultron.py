"""Marca visible de ULTRON en la pantalla (spec portar-desde-origen, renombre visible).

Pedido del usuario: el titulo de la barra lateral dice ULTRON (no VISION) y el logo de la "V" de Vision que estaba a su
lado queda vacio. La animacion de arranque escribe el mismo titulo. Se leen los archivos tal cual los sirve el backend.
"""

from __future__ import annotations

import re
from pathlib import Path

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"


def _brand_block() -> str:
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    match = re.search(r'<div class="brand">(.*?)<button id="sidebar-close"', html, re.S)
    assert match, "no se encontro el bloque de la marca en index.html"
    return match.group(1)


def test_the_sidebar_title_says_ultron():
    titles = re.findall(r"<h1>(.*?)</h1>", _brand_block())
    assert titles == ["ULTRON"]


def test_the_vision_logo_next_to_the_title_is_gone():
    block = _brand_block()
    assert "logo.svg" not in block
    assert "<img" not in block


def test_the_startup_animation_writes_ultron():
    js = (FRONTEND / "js" / "splash-screen.js").read_text(encoding="utf-8")
    assert re.search(r'^const TITLE_TEXT = "ULTRON";$', js, re.M)
