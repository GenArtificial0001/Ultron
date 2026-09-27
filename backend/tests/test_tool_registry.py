"""REQ-003 (spec bateria-qa-integracion): el registry de tools carga sin
errores y con la cantidad esperada. Para no falsar por drift (el conteo real
crece con features nuevas), se exige un piso (`>= 21`, el vigente) y la
presencia de tools conocidas -- si sube, el test no rompe; si baja, falla
(porque algo dejó de registrarse, bug real).
"""

from __future__ import annotations

from app.tools import build_registry


def test_registry_loads_known_tools():
    tools = build_registry().all()
    assert len(tools) >= 21, f"registry bajo de tools: {len(tools)}"
    names = {t.name for t in tools}
    for known in ("read", "write", "bash", "grep", "glob", "skill"):
        assert known in names, f"falta la tool '{known}' en el registry"
