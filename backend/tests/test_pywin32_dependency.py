"""`pywin32` (automatizacion de Excel, Word y conversion de documentos por COM) viaja en TODAS las instalaciones (spec office-com-automatizacion, REQ-001).

Por que: Vision va a controlar el Excel y el Word que cada persona tiene abiertos en SU PC. Eso se hace por COM, y la puerta a COM en Python es `pywin32`. Si el paquete solo estuviera en el entorno
de desarrollo, prod y el .exe arrancarian sin poder tocar Office. Se vigila lo mismo que con `mcp` y `python-pptx`:
1. `backend/requirements.txt` lo declara con piso de version;
2. solo en Windows (marcador `sys_platform == "win32"`): `pywin32` no existe para otros sistemas y un `pip install -r` ahi fallaria entero por un paquete que no se usa;
3. el lanzador (`launch_vision.py`) entiende esa linea con marcador (si no la entendiera, no instalaria el paquete o rompia al leer el archivo);
4. no esta solo en `requirements-dev.txt` (ese archivo nunca viaja a prod);
5. se IMPORTA de verdad y expone lo que usa Vision."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
REPO = BACKEND.parent
REQUIREMENTS = BACKEND / "requirements.txt"
REQUIREMENTS_DEV = BACKEND / "requirements-dev.txt"


def _declared(path: Path) -> list[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [
        re.split(r"[<>=!~\[ ;#,]", line.strip(), maxsplit=1)[0].lower()
        for line in lines
        if line.strip() and not line.lstrip().startswith("#")
    ]


def test_requirements_declares_pywin32_with_a_floor_and_a_windows_only_marker():
    text = REQUIREMENTS.read_text(encoding="utf-8")

    assert "pywin32" in _declared(REQUIREMENTS)
    assert re.search(r'^pywin32>=306\s*;\s*sys_platform\s*==\s*"win32"\s*$', text, re.MULTILINE), (
        'tiene que ser: pywin32>=306 ; sys_platform == "win32"'
    )


def test_pywin32_is_in_the_runtime_requirements_not_only_in_the_dev_ones():
    assert "pywin32" not in _declared(REQUIREMENTS_DEV)


def test_the_launcher_understands_the_requirement_line_with_the_platform_marker(tmp_path):
    """El lanzador saca los NOMBRES de los paquetes para saber cuales faltan. Si no entendiera la linea con marcador, no sabria que `pywin32` hay que instalarlo."""
    sys.path.insert(0, str(REPO))
    try:
        import launch_vision
    finally:
        sys.path.remove(str(REPO))

    names = [n.lower() for n in launch_vision._requirement_names(REQUIREMENTS)]

    assert "pywin32" in names
    assert "mcp" in names, "agregar pywin32 no puede hacer perder las demas lineas"


@pytest.mark.skipif(sys.platform != "win32", reason="pywin32 solo existe en Windows")
def test_pywin32_imports_and_offers_what_the_office_client_uses():
    pytest.importorskip(
        "win32com.client", reason="pywin32 no esta instalado en este entorno: justo lo que este pedido evita"
    )
    import pythoncom
    import win32com.client

    assert callable(win32com.client.GetActiveObject) and callable(win32com.client.Dispatch)
    assert callable(pythoncom.CoInitialize) and callable(pythoncom.CoUninitialize)


def test_the_marker_makes_pip_skip_the_package_off_windows():
    """Control del marcador con la misma libreria que usa pip: en Linux o macOS la linea no se instala; en Windows si."""
    from packaging.markers import Marker

    marker = Marker('sys_platform == "win32"')

    assert marker.evaluate({"sys_platform": "win32"}) is True
    assert marker.evaluate({"sys_platform": "linux"}) is False
    assert marker.evaluate({"sys_platform": "darwin"}) is False
