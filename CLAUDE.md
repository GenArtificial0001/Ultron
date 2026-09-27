# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Leé primero [AGENTS.md](AGENTS.md): reglas que no se negocian, arquitectura, comandos y tests. Todo
el texto del proyecto (código, docstrings, UI, commits) está en español. Acá va solo lo que
AGENTS.md no dice.

Recordatorios rápidos:
- Nada propio de una empresa ni datos personales en el código: va al `.env` (`backend/.env.example`).
- Nunca versionar `.env`, `.env.enc`, `keys/`, `*.pem`, `*.har` ni bases de datos.
- Tras tocar `backend/app/` o `frontend/`: subir `backend/app/version.py` y anotar en `CHANGELOG.md`.
- Features nuevas: spec `APPROVED` en `specs/<slug>/` antes de programar.
- Commit y push solo cuando el usuario lo pide. Remoto: repositorio de Gen
  (https://github.com/GenArtificial0001/Vision, privado).

## Comandos en PowerShell (desde `backend/`)

AGENTS.md usa sintaxis de `cmd`. En PowerShell:

```powershell
$env:VISION_DEV_MODE='1'; .venv\Scripts\python.exe -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8030
.venv\Scripts\python.exe -m pytest tests/test_model_fallback.py::nombre_del_test   # un solo test
.venv\Scripts\python.exe -m pytest -m unit       # lo más rápido (conftest asigna unit/integration solo)
.venv\Scripts\python.exe -m pytest --shuffle     # orden aleatorio; reproducir con --shuffle-seed=N
.venv\Scripts\python.exe -m pytest --cov         # piso de cobertura 62 % (.coveragerc): nunca bajarlo
```

- `backend\.venv` lo crea `Iniciar-Vision.bat` (reinstala `requirements.txt` solo si cambió su hash).
  Para tests: `pip install -r requirements-dev.txt` y `python -m playwright install chromium`.
- Sin `VISION_DEV_MODE=1`, `BASE_DIR` es `%LOCALAPPDATA%\Vision-personal` aunque se corra desde el
  repo: el servidor leería el `.env` y las sesiones de la instalación real.
- `pytest.ini`: `--strict-markers` (un marcador nuevo se declara ahí) y `timeout = 60` por test.

## Qué más hay que tocar al cambiar algo

- **Tool nueva**: archivo en `backend/app/tools/`, registrarla en `build_registry()` y subir el
  `expect` de `tool_registry()` en `scripts/qa_battery.py` (hoy 21).
- **Feature nueva**: su `slug` y tokens en `scripts/qa_coverage.json`; la batería marca
  "SIN COBERTURA" a toda feature cuyo token no aparece en el código ni en `backend/tests/`.
- **Frontend**: todo `getElementById("x")` de `frontend/js/*.js` tiene que existir como `id` en
  `frontend/index.html` (lo controla `qa_battery.py`). `app.js` es un solo archivo de ~11 000 líneas.
- **Algo nuevo que la app necesite en runtime fuera de `backend/app/` y `frontend/`**: sumarlo a
  `FILES_TO_COPY`/`DIRS_TO_COPY` en `build_stage.py`, o `Vision.exe` no lo lleva.
- **`launch_vision.py`** usa solo la librería estándar (PyInstaller lo compila en un venv que no
  tiene nada más): no importar `app` ni dependencias de `requirements.txt`.

## Arquitectura que cruza varios archivos

- **Contrato de tool** (`app/tools/base.py`): `execute(args, session) -> str`. Las sincrónicas
  corren en `asyncio.to_thread`; si la tool llama a `session.push_event()` (cola asyncio, no
  thread-safe) lleva `thread_safe=False`. `dangerous=True` la manda al log de auditoría y exige
  confirmación cuando el pedido llega por voz (Modo Cerebro).
- **Planificar primero** (`_dispatch_or_intercept` en `app/llm/loop.py`): las tools de
  `_PLAN_FIRST_TOOLS` se interceptan sin ejecutarse mientras `session.todos` esté vacío, salvo que
  el modelo se haya clasificado `conversacional` o `analitica_simple` con el marcador
  `[[TIPO: ...]]` de su primer token (el loop lo saca del stream antes de mandarlo al frontend).
- **Sesiones, carpeta por sesión** (`app/persistence/`): `<BASE_DIR>\vision.db` es solo el índice
  `session_id → carpeta de proyecto`; los mensajes viven en `<carpeta>\db\vision.db`. Sin entrada en
  el índice, la sesión queda solo en memoria. Toda escritura pasa por el hilo único de
  `persistence/writer.py` (orden de las FK).
- **Event loop en Windows**: `main.py` fija `WindowsProactorEventLoopPolicy` al importarse; sin eso
  la tool `bash` y el LSP (`app/lsp.py`) no pueden abrir subprocesos. No sacarlo.
- **Vision.exe**: `build_exe.bat` → `build_stage.py` arma `build\embed\` con un hash →
  `launch_vision.py` lo extrae a `%LOCALAPPDATA%\Vision-personal\app` solo si el hash cambió, crea
  ahí su venv y arranca uvicorn como subproceso. `/api/status` devuelve `__version__`: así se sabe
  qué código está corriendo.

## Trampas conocidas

- El guardián `test_no_company_specific_or_personal_data_in_the_repo` busca subcadenas, sin
  distinguir mayúsculas, en `.py/.js/.html/.css/.bat/.ini/.json/.md/.txt/.example` (fuera de
  `specs/` y `vendor/`), este archivo incluido: un término prohibido dentro de otra palabra también
  lo dispara. El `CLAUDE.md` de la carpeta padre describe otra empresa: nada de ese contexto se
  trae acá.
- `pytest.ini`, `requirements-dev.txt`, `tests/conftest.py` y algunos comentarios nombran
  `sync_prod.py`, `test_prod_boundary.py` y un worktree `prod/`: vienen del proyecto de origen y no
  existen en este repo.
- Los e2e se saltean solos si falta el Chromium de Playwright. En esta PC Playwright a veces se
  cuelga al cerrar Chromium: no es un test en rojo.
