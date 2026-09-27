"""Espera a que Vision responda en localhost y abre el navegador -- pieza
que le faltaba a `Iniciar-Vision.bat` de ESTE worktree (`dev/`).

Bug real reportado por el usuario (2026-09-19): "el Iniciar-Vision.bat y
los .exe deberian lanzar el navegador" -- en `prod/Iniciar-Vision.bat` y
`Vision.exe` esto ya funcionaba (ambos corren `launch_vision.py`, que tiene
su propio `_open_browser_when_ready` en un thread daemon), pero
`dev/Iniciar-Vision.bat` es un `.bat` plano que arranca
`uvicorn --reload` directo en la ultima linea y NUNCA tuvo ningun paso que
abra el navegador -- no es una regresion, es algo que faltó desde que
existe ese `.bat` (a diferencia de `prod/`, que delega todo a
`launch_vision.py`).

Fix: este script standalone (misma logica de polling que
`launch_vision.py:_open_browser_when_ready`, deliberadamente duplicada en
vez de importada -- este script se llama ANTES de que uvicorn levante y
sin depender de que `app/` ya sea importable) se lanza en paralelo
(`start /B`) desde `Iniciar-Vision.bat`, justo antes de la linea de
`uvicorn --reload`, y termina solo apenas abre el navegador (o si se acaba
el timeout). No requiere ninguna dependencia de `requirements.txt` (solo
stdlib), asi que corre incluso durante la primera instalacion del `.venv`.

Uso (ver Iniciar-Vision.bat):
    start /B "" ".venv\\Scripts\\python.exe" scripts\\wait_and_open_browser.py
"""

from __future__ import annotations

import time
import urllib.error
import urllib.request
import webbrowser

LOCAL_URL = "http://127.0.0.1:8030/"


def main() -> None:
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(LOCAL_URL, timeout=1) as response:
                if response.status < 500:
                    webbrowser.open(LOCAL_URL)
                    return
        except (urllib.error.URLError, OSError):
            time.sleep(0.3)
    # Timeout silencioso a proposito: este script corre en paralelo sin
    # consola propia (start /B) -- un mensaje impreso aca no lo veria nadie,
    # y Iniciar-Vision.bat ya deja la URL visible en su propia consola.


if __name__ == "__main__":
    main()
