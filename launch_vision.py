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
import sqlite3
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
LOCAL_PORT = 8030  # el de siempre de Vision-personal (convive con otro Vision en 8020); con la configuracion en Vision, el puerto elegido en Configuracion > Conexiones > General (ver `_configured_port`)
LOCAL_URL = f"http://{LOCAL_HOST}:{LOCAL_PORT}/"

# spec gestor-conexiones C2 (REQ-002/011): sin `.env`, el lanzador lee lo que necesita ANTES de arrancar el servidor desde la base global de Vision
# (`vision.db` de la carpeta de datos, tabla `app_settings`; nunca `~/.vision`, que es de otro Vision instalado). Solo dos claves NO secretas (la app las deja en claro a proposito; el resto esta cifrado y el lanzador no lo toca):
# si la configuracion esta en Vision y el puerto elegido. El lanzador no puede importar `app`: replica la ruta y el nombre de las claves.
_CONFIG_SOURCE_KEY = "config_source"
_CONFIG_SOURCE_VISION_DB = "vision_db"
_LAUNCHER_PORT_KEY = "launcher_app_port"


def _vision_db(base: Path | None = None) -> Path:
    """Misma ubicacion que `app/persistence/master_index.py:master_db_path` (la carpeta de datos), nunca `~/.vision`."""
    return (base or _data_dir(Path(__file__).resolve().parent / "backend")) / "vision.db"


def _read_app_setting(key: str, home: Path | None = None) -> str | None:
    """Lee UNA clave de `app_settings` en modo solo lectura (nunca crea ni modifica la base). None si no hay base, tabla o clave."""
    path = _vision_db(home)
    if not path.is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=2)
        try:
            row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    return row[0] if row and row[0] else None


def _vault_active(home: Path | None = None) -> bool:
    """True si la configuracion ya vive cifrada en Vision (el `.env` no se usa mas)."""
    return _read_app_setting(_CONFIG_SOURCE_KEY, home) == _CONFIG_SOURCE_VISION_DB


def _configured_port(home: Path | None = None) -> int:
    """Puerto elegido en Vision; cualquier valor raro (o ninguno) es el de siempre, 8030: el lanzador nunca deja de arrancar por esto."""
    raw = _read_app_setting(_LAUNCHER_PORT_KEY, home)
    try:
        port = int(raw) if raw else LOCAL_PORT
    except ValueError:
        return LOCAL_PORT
    return port if 1 <= port <= 65_535 else LOCAL_PORT


# Variables de backend/.env.example cuyo nombre sugiere una credencial real
# necesaria para que el conector correspondiente funcione -- si estan
# vacias (o si el .env completo no existe todavia), Vision arranca igual
# pero avisa ANTES de abrir el navegador, en vez de que el usuario lo
# descubra recien al ver fallar una consulta de Oracle/BigQuery/SharePoint
# en el chat. No bloqueante: solo un aviso impreso en consola.
#
# spec conexiones-con-nombre (2026-10-05): Oracle, SharePoint/Graph y SAP ya NO se configuran en `.env` ni en `.env.enc`: viven en
# `~/.vision/connections.json` (un bloque por conexion). Por eso este aviso solo vigila lo que SIGUE viniendo de `.env`/`.env.enc` (BigQuery)
# y mira aparte si hay un `connections.json` con conexiones (`_connections_file_summary`). Avisar "ORACLE_PASSWORD vacio" seria un aviso FALSO.
_CREDENTIAL_HINTS = {
    # spec vision-generico: sin clave del proveedor principal el chat no responde (salvo otro proveedor o una IA local).
    "LLM_API_KEY": "el chat con inteligencia artificial no va a funcionar",
    "BQ_PROJECT_ID": "BigQuery no va a poder conectarse",
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
        f"o 'Vision.exe', o simplemente abri http://{host}:{port}/ en el navegador "
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


def _connections_file(home: Path | None = None) -> Path:
    """Misma ubicacion que `app/connections.py` (el lanzador no puede importar `app`): `connections.json` en la carpeta de datos, o
    `VISION_CONNECTIONS_FILE`. `home`: otra carpeta de datos (tests)."""
    override = os.environ.get("VISION_CONNECTIONS_FILE", "").strip()
    return Path(override) if override else (home or _data_dir(Path(__file__).resolve().parent / "backend")) / "connections.json"


def _connections_file_summary(path: Path | None = None) -> tuple[int, str | None]:
    """(cantidad de conexiones en el archivo, problema o None). Solo cuenta bloques: NUNCA lee ni muestra campos, y no valida (eso lo hace la app)."""
    path = path or _connections_file()
    if not path.exists():
        return 0, None
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        return 0, f"{path} no se pudo leer ({type(exc).__name__}); Vision arranca sin esas conexiones hasta que lo arregles."
    items = data.get("connections") if isinstance(data, dict) else data
    return (len(items) if isinstance(items, list) else 0), None
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
    values: dict[str, str] = {}
    if env_path.exists():
        try:
            env_text = env_path.read_text(encoding="utf-8")
        except OSError:
            return
        for line in env_text.splitlines():
            line = line.split("#", 1)[0].strip()
            if not line or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    elif not stored:
        count, problem = _connections_file_summary()
        if problem:
            print()
            print(f"AVISO: {problem}")
        if count:
            return  # hay conexiones con nombre: lo unico que falta es BigQuery/voz, que es opcional
        print()
        print(
            f"AVISO: todavia no hay conexiones configuradas: no existe {env_path} ni {_connections_file()}. "
            "Vision va a arrancar; cargalas desde Vision, en Configuracion > Conexiones (quedan cifradas, "
            "sin ningun archivo .env)."
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
            "AVISO: faltan credenciales de BigQuery (Vision arranca igual). Cargalas en "
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


# spec lanzador-extraccion-segura: la extraccion del codigo ya no puede dejar la instalacion a medias (caso real 2026-10-21: 77 archivos menos y `llm/loop.py` en 0 bytes).
# Antes: se borraba `app/` y se copiaba de nuevo SIN red de seguridad, el marcador de version no se invalidaba, «al dia» solo miraba que la carpeta existiera y el puerto se
# chequeaba DESPUES de extraer. Ahora: se copia a una carpeta hermana, se verifica, se reemplaza y recien al final se escribe el marcador; un candado evita dos extracciones.
_EXTRACT_LOCK_NAME = ".vision-extract.lock"
_EXTRACT_LOCK_STALE_SECONDS = 600  # un candado de mas de 10 minutos es huerfano: nunca impide arrancar
_STAGING_NAME = "app.nuevo"
_LOCK_BUSY_MESSAGE = (
    "Vision no puede actualizar su codigo ahora porque hay otra copia de Vision haciendolo. "
    "Espera unos segundos y volve a abrir Vision (si no se abrio ninguna otra, cerra todas las ventanas de Vision y proba de nuevo)."
)


def _is_safe_relative(rel: Path) -> bool:
    """REQ (seguridad): una ruta del codigo embebido solo vale si es relativa y no sale de su carpeta (`..`, unidad, ruta absoluta)."""
    if rel.is_absolute() or rel.drive or rel.anchor:
        return False
    return ".." not in rel.parts


def _embedded_code_files(embed_src: Path) -> list[Path]:
    """REQ-002: rutas (relativas a `embed/`) de TODO el codigo de Vision que viaja en el `.exe`: `backend/app`, `backend/scripts`, `frontend` y `backend/requirements.txt`.
    Sin `__pycache__` ni `.pyc` (no son codigo fuente y cambian solos)."""
    files: list[Path] = []
    for root in (embed_src / "backend" / "app", embed_src / "backend" / "scripts", embed_src / "frontend"):
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            rel = path.relative_to(embed_src)
            if _is_safe_relative(rel):
                files.append(rel)
    requirements = embed_src / "backend" / "requirements.txt"
    if requirements.is_file():
        files.append(requirements.relative_to(embed_src))
    return files


def _missing_or_empty(base: Path, embed_src: Path) -> list[Path]:
    """REQ-002: que archivos del codigo embebido FALTAN en `base`, o estan en 0 bytes cuando el original tiene contenido. Un `__init__.py` que ya es vacio en el `.exe` NO cuenta
    (es normal). Solo existencia y tamano: lo que fallo en el caso real; no se compara el contenido byte a byte en cada arranque."""
    bad: list[Path] = []
    for rel in _embedded_code_files(embed_src):
        try:
            size = (base / rel).stat().st_size
        except OSError:
            bad.append(rel)
            continue
        if size == 0 and (embed_src / rel).stat().st_size > 0:
            bad.append(rel)
    return bad


def _pid_alive(pid: int) -> bool:
    """Si el proceso `pid` sigue vivo (solo para decidir si un candado es huerfano). Ante la duda («acceso denegado») se lo da por vivo."""
    if pid <= 0:
        return False
    if os.name == "nt":
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5  # 5 = acceso denegado: existe pero no es mio
        try:
            code = wintypes.DWORD()
            kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _lock_is_stale(lock: Path) -> bool:
    """REQ-008: un candado es huerfano si es viejo, si no se entiende o si el proceso que lo puso ya no existe. Un candado huerfano NUNCA impide arrancar."""
    try:
        age = time.time() - lock.stat().st_mtime
    except OSError:
        return True
    if age > _EXTRACT_LOCK_STALE_SECONDS:
        return True
    try:
        pid = int(lock.read_text(encoding="utf-8").split()[0])
    except (OSError, ValueError, IndexError):
        return True
    return not _pid_alive(pid)


def _acquire_extract_lock(vision_dir: Path) -> Path:
    """REQ-008: crea `.vision-extract.lock` de forma atomica (`O_EXCL`). Si otro lanzador VIVO ya lo tiene, corta con un mensaje; si es huerfano, lo reemplaza."""
    vision_dir.mkdir(parents=True, exist_ok=True)
    lock = vision_dir / _EXTRACT_LOCK_NAME
    for _attempt in range(2):
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if _lock_is_stale(lock):
                try:
                    lock.unlink()
                except OSError:
                    pass
                continue
            raise RuntimeError(_LOCK_BUSY_MESSAGE) from None
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(f"{os.getpid()} {time.time()}")
        return lock
    raise RuntimeError(_LOCK_BUSY_MESSAGE)


def _release_extract_lock(lock: Path) -> None:
    """REQ-009: se libera SIEMPRE (tambien si la extraccion fallo): un candado colgado bloquearia el siguiente arranque."""
    try:
        lock.unlink()
    except OSError:
        pass


def _stage_new_code(embed_src: Path, staging_dir: Path) -> None:
    """REQ-005: copia el codigo a una carpeta HERMANA (`app.nuevo`), sin tocar la instalacion que anda."""
    if staging_dir.exists():
        shutil.rmtree(staging_dir, ignore_errors=True)  # resto de una extraccion anterior que se corto
    embed_backend = embed_src / "backend"
    for name in ("app", "scripts"):
        src = embed_backend / name
        if src.exists():
            shutil.copytree(src, staging_dir / "backend" / name)
    requirements = embed_backend / "requirements.txt"
    if requirements.exists():
        (staging_dir / "backend").mkdir(parents=True, exist_ok=True)
        shutil.copy2(requirements, staging_dir / "backend" / "requirements.txt")
    if (embed_src / "frontend").exists():
        shutil.copytree(embed_src / "frontend", staging_dir / "frontend")
    # spec gestor-conexiones C3 (REQ-011): ya no se copia `.env.example`: la configuracion se carga en Vision > Configuracion > Conexiones.


_REPLACE_ATTEMPTS = 6  # reintentos totales de un renombre: acotado, nunca infinito
_REPLACE_WAIT_SECONDS = 0.25  # espera entre intentos; crece (0,25, 0,5, 0,75 ...): en total ~3,75 s


def _replace_with_retry(src: Path, dst: Path) -> None:
    """spec lanzador-extraccion-segura REQ-012 (0.35.6): `os.replace` que reintenta unas pocas veces SOLO si Windows responde «acceso denegado» (`PermissionError`).
    Caso real (suite del 2026-10-22, `WinError 5` al renombrar `app.nuevo\frontend`): un antivirus o el indexador de Windows tienen abierto, por instantes, un archivo recien copiado
    y el renombre falla aunque nada este roto. Un fallo pasajero no puede impedir que Vision abra. Cualquier otro error (disco lleno, ruta invalida) falla de inmediato, como antes;
    y si el bloqueo no se va despues de los intentos, se vuelve a lanzar el error original (el llamador restaura lo que andaba)."""
    for attempt in range(1, _REPLACE_ATTEMPTS + 1):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == _REPLACE_ATTEMPTS:
                raise
            time.sleep(_REPLACE_WAIT_SECONDS * attempt)


def _swap_in_new_code(install_dir: Path, staging_dir: Path) -> None:
    """REQ-004/005: reemplaza el codigo instalado por el ya verificado en `staging_dir`, carpeta por carpeta y con RENOMBRE (no borra primero). Si un renombre falla, esa carpeta
    vuelve a su lugar. Solo toca `backend/app`, `backend/scripts`, `frontend` y `backend/requirements.txt`: `.venv`, keys, data, logs, workspace y browser nunca (REQ-006)."""
    backend_dir = install_dir / "backend"
    backend_dir.mkdir(parents=True, exist_ok=True)
    pairs = (
        (staging_dir / "backend" / "app", backend_dir / "app"),
        (staging_dir / "backend" / "scripts", backend_dir / "scripts"),
        (staging_dir / "frontend", install_dir / "frontend"),
    )
    for src, dst in pairs:
        if not src.exists():
            continue
        old = dst.with_name(dst.name + ".viejo")
        if old.exists():
            shutil.rmtree(old, ignore_errors=True)  # resto de un reemplazo anterior
        if dst.exists():
            _replace_with_retry(dst, old)
        try:
            _replace_with_retry(src, dst)
        except OSError:
            if old.exists() and not dst.exists():
                _replace_with_retry(old, dst)  # no se pudo poner la nueva: vuelve la que andaba
            raise
        if old.exists():
            shutil.rmtree(old, ignore_errors=True)
    staged_requirements = staging_dir / "backend" / "requirements.txt"
    if staged_requirements.exists():
        _replace_with_retry(staged_requirements, backend_dir / "requirements.txt")


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
    embed_hash = embed_hash_file.read_text(encoding="utf-8").strip() if embed_hash_file.exists() else ""
    marker = install_dir / ".vision-embed-hash"
    backend_dir = install_dir / "backend"
    frontend_dir = install_dir / "frontend"

    def marker_matches() -> bool:
        installed_hash = marker.read_text(encoding="utf-8").strip() if marker.exists() else ""
        return bool(embed_hash and embed_hash == installed_hash and backend_dir.exists() and frontend_dir.exists())

    # REQ-001/002: «al dia» ya no es solo «el marcador coincide y la carpeta existe»: ademas no puede faltar ni estar vacio ningun archivo del codigo.
    broken = _missing_or_empty(install_dir, embed_src) if marker_matches() else []
    if marker_matches() and not broken:
        print("Codigo de Vision al dia en %LOCALAPPDATA%\\Vision\\app -- sin reextraer.")
        return backend_dir, frontend_dir

    if marker_matches():
        print(f"La instalacion de Vision estaba incompleta ({len(broken)} archivos faltantes o vacios): la reparo ahora.")
    else:
        print("Actualizando el codigo de Vision en %LOCALAPPDATA%\\Vision\\app ...")

    lock = _acquire_extract_lock(install_dir.parent)
    try:
        # Otro lanzador pudo terminar justo antes de que tomara el candado: se vuelve a mirar.
        if marker_matches() and not _missing_or_empty(install_dir, embed_src):
            print("Codigo de Vision al dia en %LOCALAPPDATA%\\Vision\\app -- sin reextraer.")
            return backend_dir, frontend_dir

        # REQ-004: el marcador se borra ANTES de tocar nada y se escribe SOLO al final. Una copia interrumpida deja la carpeta sin marcador y la proxima apertura la repara sola.
        marker.unlink(missing_ok=True)
        staging_dir = install_dir.parent / _STAGING_NAME
        try:
            _stage_new_code(embed_src, staging_dir)
            incomplete = _missing_or_empty(staging_dir, embed_src)
            if incomplete:
                raise RuntimeError(
                    f"La copia del codigo de Vision quedo incompleta ({len(incomplete)} archivos). No se cambio nada de lo instalado; "
                    "proba abrir Vision de nuevo (si se repite, revisa el espacio libre en disco)."
                )
            _swap_in_new_code(install_dir, staging_dir)
        finally:
            shutil.rmtree(staging_dir, ignore_errors=True)  # REQ-005: no deja basura, salga bien o mal

        if embed_hash:
            marker.write_text(embed_hash, encoding="utf-8")
    finally:
        _release_extract_lock(lock)

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


# spec lanzador-dependencias-verificadas: lo que Vision necesita para ARRANCAR y para las herramientas de datos. Se verifica IMPORTANDOLO (no con
# `pip show`, que da «instalado» si existe la carpeta aunque falte una DLL). Lista corta y fija a proposito: probar todo `requirements.txt` tardaria
# y daria falsas alarmas con paquetes opcionales. modulo importable -> nombre de la distribucion en `requirements.txt` (un test vigila que este declarada).
ESSENTIAL_DISTRIBUTIONS = {
    "pandas": "pandas",
    "numpy": "numpy",
    "openpyxl": "openpyxl",
    "duckdb": "duckdb",
    "fastapi": "fastapi",
    "uvicorn": "uvicorn",
    "openai": "openai",
    "pydantic_settings": "pydantic-settings",
}
ESSENTIAL_IMPORTS = tuple(ESSENTIAL_DISTRIBUTIONS)


def _missing_importable(venv_python: Path, backend_dir: Path) -> list[str]:
    """spec lanzador-dependencias-verificadas REQ-001/007: los esenciales que NO se pueden importar con el python del `.venv` (lista vacia si todos
    importan). Corre en un SUBPROCESO del venv (jamas importa dentro del lanzador). Nunca lanza: si el subproceso no se puede ejecutar, devuelve
    `[]` (no se pudo verificar; no se inventa una falta ni se bloquea el arranque)."""
    probe = (
        "import importlib\n"
        f"for name in {list(ESSENTIAL_IMPORTS)!r}:\n"
        "    try:\n"
        "        importlib.import_module(name)\n"
        "    except Exception:\n"
        "        print(name)\n"
    )
    try:
        result = subprocess.run(
            [str(venv_python), "-c", probe],
            cwd=backend_dir,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except Exception:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip() in ESSENTIAL_IMPORTS]


def _warn_missing_dependencies(missing: list[str]) -> None:
    """spec lanzador-dependencias-verificadas REQ-003/005: aviso VISIBLE (no una linea perdida entre la salida de pip) que nombra lo que falta."""
    print()
    print("=" * 62)
    print(" ATENCION: a este Vision le faltan componentes esenciales:")
    print("   " + ", ".join(missing))
    print(" Probablemente se corto la descarga (VPN o proxy de la empresa).")
    print(" Conectate a la VPN de tu empresa y volve a abrir Vision: va a")
    print(" reintentar la instalacion solo.")
    print("=" * 62)
    print()


def _ensure_requirements(backend_dir: Path, venv_python: Path) -> None:
    """Instala `requirements.txt` en el `.venv`, con un fallback real ante
    un problema encontrado en produccion (2026-09-17): `win11toast` (toast
    nativo de Windows para alarmas) NO esta en el proxy privado de
    Artifactory de la empresa (indice `pip` global de la maquina) -- y como
    `pip install -r requirements.txt` es TODO O NADA, ese UN paquete
    faltante hacia fallar la instalacion COMPLETA (fastapi, uvicorn,
    pandas, todo) dejando el `.venv` vacio y a Vision sin poder arrancar.
    Si el intento normal falla, se detectan los paquetes que realmente
    faltan (`pip show`) y se instalan UNO POR UNO contra
    `--index-url https://pypi.org/simple` con el proxy corporativo de la empresa
    (`HTTP_PROXY`/`HTTPS_PROXY`, ver skill skill-interna) -- nunca se
    reintenta el `requirements.txt` completo con `--extra-index-url`,
    porque eso hace que pip evalue AMBOS indices para CADA paquete y el
    proxy necesario para pypi.org rompe la conexion directa al indice
    interno de la empresa (503/timeout en todo, no solo en lo que falta)."""
    requirements = backend_dir / "requirements.txt"
    if not requirements.exists():
        raise RuntimeError(f"No se encontro '{requirements}'.")

    # spec lanzador-dependencias-verificadas: todo lo de aca instala con el python del `.venv`. Si no existe NO se instala nada (jamas con el del sistema) y se
    # dice por que; `_ensure_venv` ya intento crearlo antes, asi que llegar aca sin el es una falla real que la persona tiene que ver.
    if not venv_python.exists():
        print("ADVERTENCIA: no se encontro el entorno virtual de Vision (.venv); no se instala nada fuera de el.")
        print("Cerra Vision y volve a abrirlo: lo va a crear de nuevo.")
        return

    marker = venv_python.parent.parent / ".vision-deps-ok"

    req_hash = _sha256_file(requirements)
    old_hash = marker.read_text(encoding="utf-8").strip() if marker.exists() else ""

    if req_hash == old_hash:
        # spec lanzador-dependencias-verificadas REQ-004: el marcador dice «al dia», pero una instalacion puede haberse roto DESPUES (el
        # antivirus borra una DLL, se corta una descarga). Se verifica importando; si algo no importa se invalida el marcador y se reinstala.
        broken = _missing_importable(venv_python, backend_dir)
        if not broken:
            print("Dependencias al dia -- salteando pip install.")
            return
        print(f"Dependencias incompletas ({', '.join(broken)}) -- reinstalando...")
        marker.unlink(missing_ok=True)
    else:
        print("requirements.txt cambio -- instalando/actualizando dependencias...")
    print(
        "(la primera vez puede tardar varios minutos; las siguientes son casi instantaneas)"
    )
    env = _proxy_env(_data_dir(backend_dir))  # REQ-004 (spec vision-generico): el proxy del `.env` para pip
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
        # NO se reintenta "-r requirements.txt" completo con
        # --extra-index-url: pip evalua AMBOS indices para CADA paquete, y
        # el proxy corporativo que hace falta para llegar a pypi.org rompe
        # la conexion directa al indice interno de la empresa (timeouts/503 en
        # CADA paquete, no solo en el que realmente falta). En cambio, se
        # detecta que falta instalado (`pip show`) y se instala ESE
        # paquete puntual contra el indice publico -- mucho mas rapido y
        # no interfiere con lo que el indice de la empresa ya resolvio bien.
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
        # REQ-005: ademas de la linea de arriba, se NOMBRA lo que falta de verdad (antes el primer `import pandas` explotaba mas tarde, lejos de la causa).
        still_missing = _missing_importable(venv_python, backend_dir)
        if still_missing:
            _warn_missing_dependencies(still_missing)
        return

    # REQ-002/003: pip termino bien, pero que termine NO prueba que se pueda importar (un paquete cortado a la mitad). Se verifica; si algo falta se
    # reintenta UNA vez, y el marcador solo se escribe si despues todo importa (asi el proximo arranque reintenta en vez de creerse «al dia»).
    not_importable = _missing_importable(venv_python, backend_dir)
    if not_importable:
        print(f"pip termino bien pero no se pueden importar: {', '.join(not_importable)} -- reintentando la instalacion una vez...")
        _run(
            [str(venv_python), "-m", "pip", "install", "-r", "requirements.txt"],
            cwd=backend_dir,
        )
        not_importable = _missing_importable(venv_python, backend_dir)
        if not_importable:
            _warn_missing_dependencies(not_importable)
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

    # spec lanzador-extraccion-segura REQ-007: PRIMERO se mira si Vision ya esta corriendo y RECIEN DESPUES se extrae el codigo. Antes era al reves: un segundo Vision abierto con el
    # primero en marcha borraba y reescribia la carpeta en uso (caso real 2026-10-21) y recien despues se daba cuenta de que el puerto estaba ocupado.
    port = _configured_port()  # spec gestor-conexiones C2: el elegido en Vision, o 8020
    url = f"http://{LOCAL_HOST}:{port}/"
    _ensure_port_free(LOCAL_HOST, port)

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

    _ensure_venv(backend_dir, venv_python)
    _ensure_requirements(backend_dir, venv_python)
    if _vault_active():
        print("Configuracion: guardada cifrada en Vision (Configuracion > Conexiones). No hace falta ningun .env.")
    else:
        _check_credentials(backend_dir, _data_dir(backend_dir))

    print()
    print(f"Levantando Vision en {url} ...")
    print("Cerrar esta ventana apaga el servidor.")
    print()

    threading.Thread(
        target=_open_browser_when_ready, args=(url,), daemon=True
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
            str(port),
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
