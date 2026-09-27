"""Punto de entrada del Vision.exe autocontenido.

Ejecuta Uvicorn dentro del mismo proceso (sin depender de un Python instalado),
abre el navegador cuando el servidor está listo y ofrece un modo interno para
que el diagnóstico LSP pueda relanzarse como subproceso del mismo EXE.

spec jarvis-mobile-pwa: HOST/PORT ya NO son constantes fijas -- se leen de
`Settings` (APP_HOST/APP_PORT en el .env de %LOCALAPPDATA%\\Vision-personal\\.env,
ver app/config.py) para que este mismo .exe pueda usarse como "servidor
Jarvis" en la LAN (APP_HOST=0.0.0.0) sin tener que tocar codigo ni
recompilar -- solo editando ese .env. Default sigue siendo 127.0.0.1:8030,
identico al comportamiento de antes de esta spec si no se toca nada."""

from __future__ import annotations

import sys
import threading
import time
import urllib.request
import webbrowser


def _run_pylsp() -> None:
    """Arranca python-lsp-server desde el mismo binario congelado."""
    from pylsp.__main__ import main as pylsp_main

    sys.argv = ["pylsp"]
    pylsp_main()


def _open_browser_when_ready(local_url: str) -> None:
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(local_url, timeout=1) as response:
                if response.status < 500:
                    webbrowser.open(local_url)
                    return
        except Exception:  # El servidor todavía está arrancando.
            time.sleep(0.3)
    print(f"Aviso: el servidor no respondió a tiempo. Abrí manualmente {local_url}")


def main() -> None:
    if "--vision-pylsp" in sys.argv:
        _run_pylsp()
        return

    import uvicorn

    # Import explícito para que PyInstaller descubra toda la aplicación y para
    # evitar el import por string, que depende del directorio de trabajo.
    from app.config import get_settings
    from app.main import app

    settings = get_settings()
    host = settings.app_host
    port = settings.app_port
    # El navegador de ESTA maquina siempre abre por 127.0.0.1, nunca por
    # el valor real de `host`: si host="0.0.0.0" (bind a todas las
    # interfaces, necesario para que celulares en la LAN se conecten), un
    # browser no puede navegar literalmente a "0.0.0.0" -- esa direccion
    # solo tiene sentido como argumento de bind de un socket servidor, no
    # como destino de una peticion HTTP.
    local_url = f"http://127.0.0.1:{port}/"

    print("=" * 60)
    print(" VISION - ejecutable autocontenido")
    print("=" * 60)
    print(f"Escuchando en: {host}:{port}")
    if host == "0.0.0.0":  # bind intencional, no hardcode de credencial
        print("(modo LAN: otros dispositivos de esta red pueden conectarse)")
        if not settings.access_token:
            print(
                "AVISO: ACCESS_TOKEN no esta configurado -- cualquiera en esta "
                "red podria usar el API sin restriccion. Configuralo en el "
                ".env si vas a exponer esto fuera de una red 100% de confianza."
            )
    print(f"URL local: {local_url}")
    print("Los datos del usuario se guardan en %LOCALAPPDATA%\\Vision-personal.")
    print("Cerrar esta ventana apaga Vision.\n")

    threading.Thread(
        target=_open_browser_when_ready, args=(local_url,), daemon=True
    ).start()
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
