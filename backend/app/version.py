"""
Version de Vision, como fuente unica de verdad para mostrarla en:
- `/api/status` (el frontend la pinta en el footer del sidebar).
- `FastAPI(version=...)` en `main.py` (aparece en /docs).
- La consola de arranque de `launch_vision.py` / `portable_main.py`.

Por que existe este archivo (bug real que motivo crearlo, 2026-09-05):
el usuario reporto que un bug ya arreglado en el codigo fuente
(`app/tools/question.py`, ver CHANGELOG.md) seguia reproduciendose. La
causa real NO era el fix (que andaba bien probado en aislado), sino que
el `Jarvis.exe` portable que estaba corriendo (`build_portable_exe.bat`)
habia sido compilado ANTES de guardar el fix -- ese exe empaqueta el
codigo del backend CONGELADO adentro (a diferencia de `Vision.exe`, que
solo referencia los .py en disco), asi que seguia sirviendo la version
vieja aunque el archivo fuente ya estuviera corregido. Sin un numero de
version visible en la UI/API, no habia forma rapida de confirmar "el exe
que esta corriendo AHORA es el que tiene mi fix" sin comparar timestamps
de archivos a mano.

Convencion: SemVer simplificado MAJOR.MINOR.PATCH, bump manual (no hay CI
que lo automatice). Subir PATCH en cada fix de bug, MINOR en cada feature
nueva con spec propia, MAJOR nunca hasta que haya una razon real de
romper compatibilidad (no aplica hoy, Vision es un prototipo de un solo
usuario).

2026-09-27: 1.0.0, primera version de Vision generico (spec vision-generico):
nace del codigo de Vision 0.19.3, sin nada propio de una empresa -- el
proveedor de IA, el dictado, el proxy y el nombre de la empresa salen del .env."""

from __future__ import annotations

__version__ = "2.1.1"
