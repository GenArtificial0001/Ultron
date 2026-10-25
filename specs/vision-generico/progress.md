# Progress: Vision genérico

> Bitácora de implementación. Cada tarea se traza a un REQ-ID de `spec.md`.

## 2026-09-27 -- 1.0.0 (Claude Code)

- Base: código de aplicación de Vision 0.19.3 (backend, frontend, lanzadores y tests), sin skills,
  specs, docs ni historial de la versión de empresa.
- REQ-001: `app/llm/client.py` reescrito (`LLM_PROVIDER`/`LLM_BASE_URL`/`LLM_API_KEY`,
  `ProviderNotConfigured` → `MODELO-CONFIGURACION` en `friendly_errors.py`). Sin el SDK del gateway
  de la empresa ni login interactivo; `requirements.txt` sin dependencias de índices privados.
- REQ-002: `GET /api/models` con `LLM_MODELS` o `GET /models` del proveedor; salto automático
  precargado con claude-opus-5-5 → claude-sonnet-5 → claude-haiku-4-5-20251001.
- REQ-003: `/api/transcribe` con `get_stt_client()` (`STT_*`); sin configurar → `VOZ-NO-CONFIGURADA`.
- REQ-004: `config._apply_proxy` (proceso) y `launch_vision._proxy_env` / `Iniciar-Vision.bat` (pip).
- REQ-005: logo propio (`frontend/assets/logo.*`, íconos regenerados), `VISION_COMPANY_NAME` en
  `/api/status`, `applyCompanyName()` (app.js) y `{company_clause}` del prompt; temas Rojo/Azul.
- REQ-006: grupo "Inteligencia artificial" en Configuración > Credenciales (claves cifradas).
- REQ-007: sin menciones a la empresa de origen ni datos personales; guardián en
  `tests/test_vision_generico.py`.
- REQ-008: batería adaptada; `test_prod_boundary.py` eliminado (el flujo dev→prod de la versión de
  empresa no existe acá).

### Decisiones de implementación
- Carpeta de datos `%LOCALAPPDATA%\Vision-personal` (constante `DATA_FOLDER_NAME` en `config.py` y
  `launch_vision.py`), índice de sesiones dentro de ella (antes `~/.vision/vision.db`) y puerto
  8030: para que convivan dos instalaciones de Vision en la misma PC sin compartir `.env`, sesiones
  ni alarmas.
- Los super/subíndices y el resto de la app no cambian respecto de 0.19.3.

## Estado al 2026-09-27 (para retomar en otra conversacion)

- Hecho: todo lo de 1.0.0 (ver arriba). Batería rápida 715/715. Prueba REAL con Anthropic (clave del
  usuario en `backend/.env`, modo desarrollo): lista de modelos, streaming, tool-calling y un turno
  completo del servidor con la herramienta `bash` -- todo OK desde la red del usuario, sin proxy.
- E2E: los del salto de modelos pasan (8/8) tras adaptar el proveedor falso a `GET /models`. Falta
  volver a correr el resto; en esta PC Playwright se cuelga de forma intermitente al cerrar
  Chromium (no es un test en rojo; ver docs/qa del proyecto original, nota QA-028).
- `scripts/qa_battery.py` necesita `backend\.venv` (se crea con `Iniciar-Vision.bat`).

### Pendientes (en este orden, decisiones del usuario)
1. Generalizar Microsoft Graph: la región de Microsoft Search está fija en `NAM`
   (`app/connectors/graph_connector.py`) -> variable del `.env`; revisar otras suposiciones.
2. ~~Configuración > Credenciales cifra con una clave SSH de despliegue que solo existe en la PC del
   usuario original -> generar una clave local propia la primera vez.~~ **Hecho 2026-09-27** (versión
   1.1.0, `keys/credentials.key`; ver spec voz-fish-audio, dependencia de REQ-009).
3. Primer commit y push al repositorio de Gen (https://github.com/GenArtificial0001/Vision, PRIVADO,
   rama main, un solo commit sin historial). Firma: `GenArtificial0001 <genartificial0001@gmail.com>`.
   Antes de subir: mostrarle al usuario la lista completa de archivos y correr el guardián
   `test_no_company_specific_or_personal_data_in_the_repo`.
4. Specs futuras (NO empezar sin pedido): varios proveedores a la vez (`providers.json` con claves en
   el `.env`, modelos `proveedor/modelo`, salto entre proveedores) y conectores MySQL/MariaDB/MongoDB.

## 2026-09-27 -- arreglo (Claude Code, rama `dev`, versión 1.1.0)

- REQ-001: con `LLM_PROVIDER=anthropic`, el chat fallaba con 400 en cuanto había parámetros de generación
  guardados (`temperature` y `top_p`): "`top_p` is deprecated for this model" (claude-sonnet-5) y
  "`temperature` and `top_p` cannot both be specified" (Haiku 4.5, Opus 4.6). `main._valid_args_for`
  arma los parámetros por modelo: ninguno de muestreo para los modelos nuevos (y los desconocidos), solo
  `temperature` para Haiku 4.5 y Opus/Sonnet 4.6 o anteriores. Verificado con Claude real (una consulta
  mínima por modelo) y con `test_anthropic_models_only_get_the_sampling_params_they_accept`.
- REQ-003: el Modo Cerebro y el modo reunión mostraban un aviso genérico ("sigo intentando, apagá y
  volvé a prender el modo") aunque el dictado no estuviera configurado. `noticeTranscriptionProblem`
  (app.js) ahora muestra el motivo que manda el backend. E2E:
  `test_voice_modes_explain_that_dictation_is_not_configured`.
- REQ-003: dictado real verificado con Groq (`https://api.groq.com/openai/v1`,
  `whisper-large-v3-turbo`), cargado por el usuario en Configuración > Credenciales: transcripción
  200 OK (2026-09-27 18:47). Guía paso a paso en el README.
