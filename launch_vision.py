"""Lanzador liviano de Vision -- fuente de `Vision.exe`.

Este script es DELIBERADAMENTE dependiente solo de la libreria estandar de
Python (sin fastapi/uvicorn/pandas/etc.) para que, al compilarlo con
PyInstaller (`build_exe.bat`), el `.exe` resultante pese pocos MB en vez
de arrastrar todo `backend/.venv` (~1.2 GB) adentro.

Portabilidad real (pedido 2026-09-17: "lo movi a otra carpeta y no
funciono"): `build_exe.bat` corre primero `build_stage.py`, que arma una
copia LIMPIA del codigo fuente minimo (`backend/app/`,
`backend/requirements.txt`, `backend/.env.example`, `frontend/`
completo -- sin `__pycache__`, tests, `.venv`,
`.env` real, `keys/`, `logs/`, `data/`, `workspace/`, `browser/`) y la
embebe DENTRO del `.exe` con `--add-data`. Este lanzador, al arrancar
congelado (`sys.frozen`), EXTRAE ese codigo a `%LOCALAPPDATA%\\Vision-personal\\app`
la primera vez (o cuando el codigo embebido cambio -- se compara un hash
SHA256 combinado) y arranca todo desde ahi -- por eso `Vision.exe` se
puede mover a CUALQUIER carpeta (USB, OneDrive, escritorio, otra PC) y
sigue funcionando solo, sin llevar `backend/`/`frontend/` al lado. En modo
desarrollo (`python launch_vision.py`, sin compilar), en cambio, usa
directo el arbol fuente de este repo (sin extraccion) para poder editar y
probar sin recompilar.

Secuencia completa:
1. Verificar que el puerto 8030 no este ya ocupado por una instancia
   zombie de Vision (`_ensure_port_free`) -- corta ANTES de gastar tiempo
   creando/actualizando el `.venv`.
2. (Solo en el `.exe`) Extraer el codigo embebido a `%LOCALAPPDATA%\\
   Vision\\app` si hace falta.
3. Crear el `.venv` del backend ahi si no existe.
4. Instalar/actualizar `requirements.txt` en ese `.venv` solo si cambio
   (hash SHA256 cacheado) -- nunca reinstala si nada cambio. Si la empresa
   sale a internet por un proxy, se usa el `HTTP_PROXY`/`HTTPS_PROXY` del
   `.env` (spec vision-generico REQ-004).
5. Avisar (no bloqueante) si falta la clave del proveedor de IA o las
   credenciales de los conectores activos (`_check_credentials`).
6. Arrancar Uvicorn como subproceso, registrar un handler de
   `CTRL_CLOSE_EVENT` (WinAPI, sin pywin32) para que cerrar la ventana de
   consola MATE de verdad al subproceso (Windows no lo hace solo -- sin
   esto, uvicorn queda "zombie" en el puerto 8030), y abrir el navegador
   en `http://127.0.0.1:8030/` en cuanto responde.

Cualquier error en el camino se imprime COMPLETO y la consola espera un
Enter antes de cerrarse -- bug real encontrado 2026-09-17: al fallar
silenciosamente (ej. `backend/` no encontrada al lado del .exe movido a
otra carpeta), la consola se abria y cerraba tan rapido que el error nunca
se llegaba a leer."""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

# Misma carpeta de datos que app/config.py (este lanzador no puede importar `app`).
DATA_FOLDER_NAME = "Vision-personal"

LOCAL_HOST = "127.0.0.1"
LOCAL_PORT = 8030
LOCAL_URL = f"http://{LOCAL_HOST}:{LOCAL_PORT}/"

# Variables de backend/.env.example cuyo nombre sugiere una credencial real
# necesaria para que el conector correspondiente funcione -- si estan
# vacias (o si el .env completo no existe todavia), Vision arranca igual
# pero avisa ANTES de abrir el navegador, en vez de que el usuario lo
# descubra recien al ver fallar una consulta de Oracle/BigQuery/SharePoint
# en el chat. No bloqueante: solo un aviso impreso en consola.
_CREDENTIAL_HINTS = {
    "LLM_API_KEY": "el chat con inteligencia artificial no va a funcionar",
    "ORACLE_PASSWORD": "Oracle no va a poder conectarse",
    "BQ_PROJECT_ID": "BigQuery no va a poder conectarse",
    "AZURE_CLIENT_SECRET": "SharePoint/Graph no va a poder conectarse",
    "SAP_PASSWORD": "SAP no va a poder conectarse",
}
# Conector de cada credencial: con su ENABLE_* en false no se avisa.
_CREDENTIAL_SWITCH = {
    "ORACLE_PASSWORD": "ENABLE_ORACLE",
    "BQ_PROJECT_ID": "ENABLE_BIGQUERY",
    "AZURE_CLIENT_SECRET": "ENABLE_SHAREPOINT",
    "SAP_PASSWORD": "ENABLE_SAP",
}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    print(f"$ {' '.join(str(c) for c in cmd)}")
    return subprocess.run(cmd, **kwargs)


def _ensure_port_free(host: str, port: int) -> None:
    """Chequea que el puerto de Vision no este ya ocupado por una
    instancia zombie (ej. un `uvicorn` que quedo corriendo en segundo
    plano porque una ejecucion anterior se cerro mal, sin pasar por el
    `atexit`/`CTRL_CLOSE_EVENT` de este mismo lanzador). Si esta ocupado,
    corta ANTES de arrancar nada -- el error de uvicorn
    ("address already in use") es mucho mas cripticо para alguien que no
    conoce Python, y en ese punto ya se gastaron minutos en crear/
    actualizar el `.venv` primero."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        in_use = sock.connect_ex((host, port)) == 0

    if not in_use:
        return

    raise RuntimeError(
        f"El puerto {port} ya esta en uso -- probablemente hay OTRA "
        f"instancia de Vision corriendo (quiza quedo abierta de una "
        f"ejecucion anterior sin cerrarse bien). Cerrala primero: "
        f"revisa el Administrador de tareas por un proceso 'python.exe' "
        f"o 'Vision.exe', o simplemente abri {LOCAL_URL} en el navegador "
        f"-- si responde, Vision YA esta corriendo y no hace falta "
        f"abrir otra instancia."
    )


def _data_dir(backend_dir: Path) -> Path:
    """Carpeta donde Vision guarda `.env`, `.env.enc` y `keys/`: el MISMO
    criterio que `backend/app/config.py:BASE_DIR` (el lanzador no puede
    importar `app`: corre antes de que exista el `.venv`). En el `.exe` y en
    `prod/` es `%LOCALAPPDATA%\\Vision-personal`; con VISION_DEV_MODE=1, `backend/`.

    Bug real (2026-09-25): el aviso de credenciales seguia buscando
    `%LOCALAPPDATA%\\Vision-personal\\app\\backend\\.env` -- la ubicacion de ANTES del
    fix de config.py del 2026-09-19 -- y avisaba "no existe backend\\.env"
    aunque el `.env` real estaba completo en `%LOCALAPPDATA%\\Vision-personal`, que es
    de donde la app si lo lee."""
    if os.environ.get("VISION_DEV_MODE") == "1":
        return backend_dir
    if os.environ.get("VISION_DATA_DIR"):
        return Path(os.environ["VISION_DATA_DIR"]).resolve()
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / DATA_FOLDER_NAME


def _stored_credential_names(data_dir: Path) -> set[str]:
    """Campos guardados CIFRADOS desde Configuracion > Credenciales (`.env.enc`
    es un JSON {campo: valor_cifrado}): solo se leen los NOMBRES, nunca se
    descifra nada."""
    try:
        raw = json.loads((data_dir / ".env.enc").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    return {str(name).lower() for name in raw} if isinstance(raw, dict) else set()


def _env_values(data_dir: Path) -> dict[str, str]:
    """`CLAVE -> valor` del `.env` de la carpeta de datos (vacio si no existe).
    Los valores nunca se imprimen: solo se mira si estan completos."""
    values: dict[str, str] = {}
    try:
        env_text = (data_dir / ".env").read_text(encoding="utf-8")
    except OSError:
        return values
    for line in env_text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def _proxy_env(data_dir: Path) -> dict[str, str]:
    """REQ-004 (spec vision-generico): entorno para pip con el proxy de la
    empresa del `.env` (lo ya definido en Windows manda). localhost siempre
    directo."""
    env = dict(os.environ)
    values = _env_values(data_dir)
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"):
        if values.get(var) and not env.get(var):
            env[var] = values[var]
    if (env.get("HTTP_PROXY") or env.get("HTTPS_PROXY")) and not env.get("NO_PROXY"):
        env["NO_PROXY"] = "localhost,127.0.0.1,::1"
    return env


def _check_credentials(backend_dir: Path, data_dir: Path) -> None:
    """Avisa (NO bloqueante) si todavia no hay credenciales de conectores, ni
    en `<data_dir>\\.env` ni guardadas cifradas desde Configuracion >
    Credenciales -- ver `_CREDENTIAL_HINTS`. Nunca impide que Vision arranque:
    bash/filesystem/skills/chat siguen funcionando sin ningun conector
    configurado, asi que esto es solo informativo."""
    env_path = data_dir / ".env"
    stored = _stored_credential_names(data_dir)
    if not env_path.exists() and not stored:
        print()
        print(
            f"AVISO: Vision todavia no esta configurado: no existe {env_path} ni se "
            "guardaron claves desde Configuracion > Credenciales. Vision va a arrancar, "
            "pero el chat con inteligencia artificial no va a responder hasta que cargues "
            "la clave del proveedor (LLM_API_KEY) desde esa pestaña, o copiando "
            f"{backend_dir / '.env.example'} a {env_path} (ver README.md, seccion "
            "'Configurar Vision')."
        )
        return
    values = _env_values(data_dir)

    warnings = []
    for key, hint in _CREDENTIAL_HINTS.items():
        switch = _CREDENTIAL_SWITCH.get(key)
        if switch and values.get(switch, "true").lower() == "false":
            continue  # conector desactivado: no hace falta su credencial
        if not values.get(key) and key.lower() not in stored:
            warnings.append(f"  - {key} vacio: {hint}.")

    if warnings:
        print()
        print(
            "AVISO: faltan credenciales (Vision arranca igual). Cargalas en "
            f"Configuracion > Credenciales o en {env_path}:"
        )
        for line in warnings:
            print(line)
        print(
            "  Si no vas a usar alguno de esos conectores, ignora el aviso "
            "correspondiente o desactivalo con su ENABLE_* en .env."
        )


def _resolve_backend_frontend_dirs() -> tuple[Path, Path]:
    """Devuelve (backend_dir, frontend_dir) listos para usar.

    - Modo desarrollo (`python launch_vision.py` desde el repo, sin
      compilar): usa directo el arbol fuente al lado de este script.
    - Modo compilado (`Vision.exe`, `sys.frozen`): extrae el codigo
      embebido (ver `build_stage.py`/`build_exe.bat`) a
      `%LOCALAPPDATA%\\Vision-personal\\app` y devuelve rutas ahi -- por eso el
      `.exe` funciona movido a cualquier carpeta.
    """
    if not getattr(sys, "frozen", False):
        root = Path(__file__).resolve().parent
        return root / "backend", root / "frontend"

    return _extract_embedded_code()


def _extract_embedded_code() -> tuple[Path, Path]:
    meipass = Path(getattr(sys, "_MEIPASS"))
    embed_src = meipass / "embed"
    if not embed_src.exists():
        raise RuntimeError(
            f"El ejecutable no tiene codigo embebido en '{embed_src}'. "
            "Esto indica que Vision.exe se compilo mal (build_exe.bat "
            "deberia correr build_stage.py antes de PyInstaller). "
            "Recompila con build_exe.bat."
        )

    install_dir = Path(os.environ["LOCALAPPDATA"]) / DATA_FOLDER_NAME / "app"
    install_dir.mkdir(parents=True, exist_ok=True)

    embed_hash_file = embed_src / ".embed-hash"
    embed_hash = (
        embed_hash_file.read_text(encoding="utf-8").strip()
        if embed_hash_file.exists()
        else ""
    )
    marker = install_dir / ".vision-embed-hash"
    installed_hash = (
        marker.read_text(encoding="utf-8").strip() if marker.exists() else ""
    )

    backend_dir = install_dir / "backend"
    frontend_dir = install_dir / "frontend"

    if (
        embed_hash
        and embed_hash == installed_hash
        and backend_dir.exists()
        and frontend_dir.exists()
    ):
        print(
            "Codigo de Vision al dia en %LOCALAPPDATA%\\Vision-personal\\app -- sin reextraer."
        )
        return backend_dir, frontend_dir

    print("Actualizando el codigo de Vision en %LOCALAPPDATA%\\Vision-personal\\app ...")

    # Solo se reemplazan el CODIGO (app/, scripts/, requirements.txt,
    # frontend/) -- nunca .venv/keys/data/logs/workspace/browser/.env, que
    # son datos del usuario y viven aparte, sin tocarse entre versiones.
    embed_backend = embed_src / "backend"
    for name in ("app", "scripts"):
        src = embed_backend / name
        if not src.exists():
            continue
        dst = backend_dir / name
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(src, dst)

    for filename in ("requirements.txt",):
        src = embed_backend / filename
        if src.exists():
            backend_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, backend_dir / filename)

    # .env.example es solo referencia -- no pisar uno que el usuario ya
    # tenga (aunque no deberia haber tocado esta copia nunca).
    env_example_src = embed_backend / ".env.example"
    env_example_dst = backend_dir / ".env.example"
    if env_example_src.exists() and not env_example_dst.exists():
        shutil.copy2(env_example_src, env_example_dst)

    embed_frontend = embed_src / "frontend"
    if embed_frontend.exists():
        if frontend_dir.exists():
            shutil.rmtree(frontend_dir)
        shutil.copytree(embed_frontend, frontend_dir)

    if embed_hash:
        marker.write_text(embed_hash, encoding="utf-8")

    print("Codigo actualizado.")
    return backend_dir, frontend_dir


def _ensure_venv(backend_dir: Path, venv_python: Path) -> None:
    if venv_python.exists():
        return

    print("No se encontro el entorno virtual del backend.")
    print("Creandolo ahora (esto puede tardar varios minutos)...")

    result = _run(["python", "-m", "venv", ".venv"], cwd=backend_dir)
    if result.returncode != 0 or not venv_python.exists():
        raise RuntimeError(
            "No se pudo crear el entorno virtual. Verifica que Python este "
            "instalado y disponible como 'python' en el PATH (python --version)."
        )


def _requirement_names(requirements: Path) -> list[str]:
    """Extrae los nombres de paquete (sin el especificador de version) de
    un requirements.txt simple -- suficiente para este archivo, que solo
    usa lineas `paquete>=x.y` o `paquete[extra]>=x.y`, sin marcadores de
    entorno ni URLs directas."""
    import re

    names = []
    for line in requirements.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        match = re.match(r"^([A-Za-z0-9_.\-]+)", line)
        if match:
            names.append(match.group(1))
    return names


def _missing_requirements(backend_dir: Path, venv_python: Path) -> list[str]:
    """Devuelve los nombres de `requirements.txt` que NO estan instalados
    todavia en el `.venv` -- usado para el fallback de PyPI publico, asi
    solo se reintenta lo que realmente falta (ej. win11toast) en vez de
    reinstalar todo de nuevo contra un indice mas lento para esta red."""
    requirements = backend_dir / "requirements.txt"
    names = _requirement_names(requirements)
    if not names:
        return []

    result = subprocess.run(
        [str(venv_python), "-m", "pip", "show", *names],
        cwd=backend_dir,
        capture_output=True,
        text=True,
    )
    installed = set()
    for line in result.stdout.splitlines():
        if line.startswith("Name:"):
            installed.add(line.split(":", 1)[1].strip().lower())

    return [n for n in names if n.lower() not in installed]


def _ensure_requirements(backend_dir: Path, venv_python: Path, data_dir: Path) -> None:
    """Instala `requirements.txt` en el `.venv` (con el proxy de la empresa del
    `.env`, si hay). Fallback real: si el indice de pip de la empresa no tiene
    algun paquete (ej. `win11toast`), `pip install -r` falla ENTERO -- todo o
    nada -- y deja el `.venv` sin fastapi/uvicorn. Entonces se detectan los
    paquetes que realmente faltan (`pip show`) y se instalan UNO POR UNO contra
    el PyPI publico, sin reintentar el archivo completo contra dos indices a la
    vez (pip evalua ambos para CADA paquete y se vuelve lentisimo)."""
    requirements = backend_dir / "requirements.txt"
    if not requirements.exists():
        raise RuntimeError(f"No se encontro '{requirements}'.")

    marker = venv_python.parent.parent / ".vision-deps-ok"

    req_hash = _sha256_file(requirements)
    old_hash = marker.read_text(encoding="utf-8").strip() if marker.exists() else ""

    if req_hash == old_hash:
        print("Dependencias al dia -- salteando pip install.")
        return

    print("requirements.txt cambio -- instalando/actualizando dependencias...")
    print(
        "(la primera vez puede tardar varios minutos; las siguientes son casi instantaneas)"
    )
    env = _proxy_env(data_dir)
    result = _run(
        [str(venv_python), "-m", "pip", "install", "-r", "requirements.txt"],
        cwd=backend_dir,
        env=env,
    )

    if result.returncode != 0:
        print(
            "El pip install normal fallo (posiblemente un paquete que no esta en "
            "el indice de pip de tu empresa, ej. win11toast) -- reintentando SOLO "
            "los paquetes que falten, contra PyPI publico, uno por uno..."
        )
        missing = _missing_requirements(backend_dir, venv_python)
        for pkg in missing:
            print(f"  instalando '{pkg}' desde PyPI publico...")
            _run(
                [
                    str(venv_python),
                    "-m",
                    "pip",
                    "install",
                    "--index-url",
                    "https://pypi.org/simple",
                    pkg,
                ],
                cwd=backend_dir,
                env=env,
            )

        result = _run(
            [str(venv_python), "-m", "pip", "install", "-r", "requirements.txt"],
            cwd=backend_dir,
            env=env,
        )

    if result.returncode != 0:
        print(
            "ADVERTENCIA: no se pudieron instalar/actualizar todas las "
            "dependencias (revisa tu conexion, la VPN o el proxy de tu empresa: "
            "HTTP_PROXY/HTTPS_PROXY en el .env). Vision va a arrancar igual con lo "
            "que ya este instalado, pero puede fallar si falta algo esencial."
        )
        return

    marker.write_text(req_hash, encoding="utf-8")


# Referencia global al callback WinAPI, ver comentario en
# _install_console_close_handler sobre por que es necesario mantenerla viva.
_console_handler_ref = None


def _install_console_close_handler(process_holder: list[subprocess.Popen]) -> None:
    """Registra un handler de `SetConsoleCtrlHandler` (WinAPI, via
    ctypes -- sin dependencia de pywin32) para que cerrar la ventana de
    consola con la X MATE de verdad al subproceso de uvicorn.

    Bug real (pedido explicito del usuario, 2026-09-19): Windows NO
    propaga automaticamente el cierre de la
    consola a los procesos hijos lanzados con `subprocess.Popen` -- sin
    este handler, cerrar la ventana deja a uvicorn corriendo "zombie" en
    el puerto 8030, invisible, hasta que se lo mate a mano desde el
    Administrador de tareas. `CTRL_CLOSE_EVENT` (codigo 2) es la señal que
    Windows manda especificamente cuando el USUARIO cierra la ventana (a
    diferencia de `CTRL_C_EVENT`, que ya se maneja con el
    `except KeyboardInterrupt` normal de Python)."""
    if os.name != "nt":
        return

    CTRL_CLOSE_EVENT = 2
    CTRL_LOGOFF_EVENT = 5
    CTRL_SHUTDOWN_EVENT = 6

    HANDLER_TYPE = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_uint)

    def _handler(ctrl_type: int) -> bool:
        if ctrl_type in (CTRL_CLOSE_EVENT, CTRL_LOGOFF_EVENT, CTRL_SHUTDOWN_EVENT):
            process = process_holder[0] if process_holder else None
            if process is not None and process.poll() is None:
                print("\nCerrando Vision (consola cerrada por el usuario)...")
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
            return True  # manejado -- no dejar que Windows mate el proceso a la fuerza sin este cleanup
        return False

    # Mantener una referencia viva del callback en el modulo (si se
    # recolecta por GC, WinAPI puede terminar llamando a un puntero
    # invalido) -- por eso se guarda en la global `_console_handler_ref`
    # en vez de una variable local descartable.
    global _console_handler_ref
    _console_handler_ref = HANDLER_TYPE(_handler)
    ctypes.windll.kernel32.SetConsoleCtrlHandler(_console_handler_ref, True)


def _open_browser_when_ready(url: str) -> None:
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status < 500:
                    webbrowser.open(url)
                    return
        except (urllib.error.URLError, OSError):
            time.sleep(0.3)
    print(f"Aviso: el servidor no respondio a tiempo. Abri manualmente {url}")


def _main() -> None:
    print("=" * 60)
    print(" VISION - lanzador")
    print("=" * 60)

    backend_dir, frontend_dir = _resolve_backend_frontend_dirs()

    if not backend_dir.exists() or not frontend_dir.exists():
        raise RuntimeError(
            f"No se encontraron 'backend' ({backend_dir}) y/o 'frontend' "
            f"({frontend_dir}). Si estas en modo desarrollo, corre este "
            "script desde la raiz de vision-web. Si es Vision.exe "
            "compilado, esto es un bug de compilacion -- recompila con "
            "build_exe.bat."
        )

    venv_python = backend_dir / ".venv" / "Scripts" / "python.exe"

    _ensure_port_free(LOCAL_HOST, LOCAL_PORT)
    _ensure_venv(backend_dir, venv_python)
    data_dir = _data_dir(backend_dir)
    _ensure_requirements(backend_dir, venv_python, data_dir)
    _check_credentials(backend_dir, data_dir)

    print()
    print(f"Levantando Vision en {LOCAL_URL} ...")
    print("Cerrar esta ventana apaga el servidor.")
    print()

    threading.Thread(
        target=_open_browser_when_ready, args=(LOCAL_URL,), daemon=True
    ).start()

    process = subprocess.Popen(
        [
            str(venv_python),
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            LOCAL_HOST,
            "--port",
            str(LOCAL_PORT),
        ],
        cwd=backend_dir,
    )

    # `process_holder` es una lista de 1 elemento en vez de una variable
    # simple para que el closure de `_install_console_close_handler` pueda
    # leer la referencia actualizada del proceso incluso si esta funcion
    # (`_main`) sigue ejecutandose despues del registro del handler --
    # ver esa funcion para el detalle completo del bug que resuelve
    # (cerrar la consola con la X no mataba a uvicorn).
    process_holder = [process]
    _install_console_close_handler(process_holder)

    try:
        process.wait()
    except KeyboardInterrupt:
        process.terminate()
        process.wait()


def main() -> None:
    """Envuelve `_main()` para que CUALQUIER error se vea y la consola no
    se cierre sola -- bug real 2026-09-17 (ver docstring del modulo)."""
    try:
        _main()
    except SystemExit:
        raise
    except Exception:
        print()
        print("=" * 60)
        print(" ERROR -- Vision no pudo arrancar")
        print("=" * 60)
        traceback.print_exc()
        print()
        input("Presiona Enter para cerrar esta ventana...")
        sys.exit(1)


if __name__ == "__main__":
    main()
