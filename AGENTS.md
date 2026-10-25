# AGENTS.md -- Vision genérico

Guía para agentes (OpenCode, Claude Code) que trabajen en este repo. Todo el texto del proyecto
(código, docstrings, UI, commits) está en español.

## Reglas que no se negocian

- **Nada propio de una empresa en el código**: ni direcciones internas, ni proxies, ni logos, ni
  nombres de personas o usuarios. Todo lo específico va al `.env` (ver `backend/.env.example`).
  `tests/test_vision_generico.py::test_no_company_specific_or_personal_data_in_the_repo` lo vigila.
- **Nunca versionar** `.env`, `.env.enc`, `keys/`, `*.pem`, `*.har` ni bases de datos (`.gitignore`).
- **Nunca mandar un pedido a un servidor que no se configuró**: sin `LLM_API_KEY` (o sin
  `LLM_BASE_URL` en `openai_compatible`) se corta con `ProviderNotConfigured` y un mensaje claro.
- **Mensajes para personas no técnicas**: qué pasó + qué hacer; el detalle crudo va al log
  (`app/friendly_errors.py`, evento SSE `error` = `{message, code, technical}`).
- Después de tocar `backend/app/` o `frontend/`: subir `__version__` (`backend/app/version.py`) y
  registrar en `CHANGELOG.md`.
- Toda feature nueva pasa por una spec `APPROVED` en `specs/<slug>/spec.md` y se registra en su
  `progress.md`. Los comentarios `# REQ-00X:` son la trazabilidad: no se borran.

## Arquitectura

- **API**: un único `FastAPI()` en `backend/app/main.py`; su docstring de módulo es el índice. Un turno
  de chat corre en background independiente de la conexión SSE (`POST /api/chat` lo dispara,
  `GET /api/chat/stream/{id}` se re-engancha; estado vivo en `app/session/events.py`).
- **Loop del modelo**: `backend/app/llm/loop.py` (tool-calling en formato OpenAI, `MAX_STEPS = 100`, aviso en `WARN_STEPS = 20`,
  prompt en `SYSTEM_PROMPT_TEMPLATE` con `{company_clause}`). Cancelación siempre cooperativa
  (`stop_requested`), nunca `.cancel()`.
- **Varios proveedores** (spec varios-proveedores): `backend/app/llm/providers.py` (registro de proveedores
  por su clave en el `.env`, releído sin reiniciar). Los modelos viajan como `proveedor/modelo`;
  `get_client(model=...)` da el cliente de su proveedor y `api_model()` el nombre que entiende. Sin prefijo =
  proveedor principal (`LLM_*`).
- **Proveedor de IA**: `backend/app/llm/client.py` (`LLM_PROVIDER`: `anthropic` vía la capa compatible
  con OpenAI de Anthropic, u `openai_compatible` con `LLM_BASE_URL`). Dictado: `get_stt_client()`.
- **Salto automático de modelos**: `backend/app/llm/model_fallback.py` (lista de prioridad en
  Configuración; pausa el modelo que llega a su límite o se cae y sigue con el siguiente).
- **Configuración**: `backend/app/config.py` (pydantic-settings). `BASE_DIR` = `backend/` con
  `VISION_DEV_MODE=1`; si no, `VISION_DATA_DIR` o `%LOCALAPPDATA%\Vision-personal`. El proxy del `.env`
  se aplica al proceso en `_apply_proxy`.
- **Herramientas**: una por archivo en `backend/app/tools/`, registradas en `build_registry()`
  (`tools/__init__.py`). Nunca lanzan: `ToolRegistry.dispatch` devuelve el error como texto.
- **Voz** (spec voz-fish-audio): toda la voz pasa por `speak()` de `frontend/js/tts.js`. Con motores en la
  cadena (Configuración > Audio), cada frase se pide a `POST /api/voice/speak`; `backend/app/voice/`
  recorre la cadena (Fish Audio, ElevenLabs, clonación local), pausa los que fallan y la voz del
  navegador queda siempre al final. "¿Está hablando?" es `isSpeaking()`, no `speechSynthesis.speaking`.
  Informe técnico hablado: `backend/app/tech_report.py` arma la frase de cada acción (`speech` en el
  evento `progress-estimate`), recorre en vivo el código mientras llegan los fragmentos de la tool-call
  (`LiveNarrator`, evento `narration`), anuncia el plan desde `todowrite` y tapa los secretos. Solo
  corre si el pedido trae `narrate: true`.
- **Frontend**: todo en `frontend/js/app.js` (SSE parseado a mano, `handleEvent` con `switch`);
  librerías locales en `frontend/vendor/` (cero CDN).

## Comandos (desde `backend/`)

```bash
set VISION_DEV_MODE=1 && .venv\Scripts\python.exe -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8030
.venv\Scripts\python.exe -m pytest -m "not e2e"     # rapido
.venv\Scripts\python.exe -m pytest -m e2e           # Chromium real (no correr dos a la vez)
..\backend\.venv\Scripts\python.exe ..\scripts\qa_battery.py   # estatico: ids, sintaxis JS, cobertura viva
```

## Tests

`backend/tests/conftest.py` aísla todo antes de importar `app`: carpeta de datos y HOME temporales,
variables de credenciales y proxy borradas, red externa bloqueada. Los tests nuevos usan
`tmp_path` + `monkeypatch`, fakes del proveedor de IA y nunca una clave real. Un test rojo por un
defecto del código se reporta y se corrige en el código, no adaptando el test.
