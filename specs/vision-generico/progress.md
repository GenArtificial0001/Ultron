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
2. Configuración > Credenciales cifra con una clave SSH de despliegue que solo existe en la PC del
   usuario original -> generar una clave local propia la primera vez.
3. Primer commit y push al repositorio de Gen (https://github.com/GenArtificial0001/Vision, PRIVADO,
   rama main, un solo commit sin historial). Firma: `GenArtificial0001 <genartificial0001@gmail.com>`.
   Antes de subir: mostrarle al usuario la lista completa de archivos y correr el guardián
   `test_no_company_specific_or_personal_data_in_the_repo`.
4. Specs futuras (NO empezar sin pedido): varios proveedores a la vez (`providers.json` con claves en
   el `.env`, modelos `proveedor/modelo`, salto entre proveedores) y conectores MySQL/MariaDB/MongoDB.
