# Vision

Asistente conversacional con herramientas reales para el trabajo diario de una empresa: lee y
escribe archivos, ejecuta comandos, consulta bases de datos (Oracle, BigQuery, SAP) y
SharePoint/Microsoft Graph, arma tableros y documentos, programa alarmas y agentes, y conversa por
voz. Backend Python/FastAPI y frontend web sin compilación (JavaScript puro).

Funciona con **Claude** (clave de Anthropic) o con cualquier servicio compatible con OpenAI
(OpenAI, Azure OpenAI o el gateway de IA de tu empresa), y detrás del proxy de cualquier red
corporativa. Todo se configura desde un archivo `.env`.

## Qué hace

| Área | Qué incluye |
|---|---|
| Chat con herramientas | bash, archivos (leer/escribir/editar/buscar), listas de tareas, skills bajo demanda, respuestas con fórmulas (KaTeX), gráficos y tablas |
| Datos | Oracle, BigQuery, SharePoint/Graph y SAP (OData v2), todos opcionales |
| Modelos | selector de modelo, parámetros de generación, **salto automático** al siguiente modelo de una lista si uno llega a su límite o se cae |
| Voz | dictado (con un servicio de transcripción configurable), lectura en voz alta, Modo Cerebro y modo reunión |
| Productividad | sesiones con historial en SQLite, alarmas y agentes programados, copiar fórmulas en texto legible (para WhatsApp o mail) |

## Requisitos

- Windows 10/11 con **Python 3.12 o superior** en el PATH (`python --version`).
- Una clave de un proveedor de IA: de Anthropic (https://console.anthropic.com) o de un servicio
  compatible con OpenAI.
- Opcional: credenciales de los conectores que quieras usar.

## Arrancar

**Modo desarrollo** (recarga sola con cada cambio):

1. Copiá `backend\.env.example` a `backend\.env` y completá al menos `LLM_API_KEY`.
2. Doble clic en `Iniciar-Vision.bat`. La primera vez crea `backend\.venv` e instala las
   dependencias (tarda unos minutos).
3. Se abre http://127.0.0.1:8030/.

**Vision.exe** (portable, un solo archivo): `build_exe.bat` lo compila. Al abrirlo, extrae el
código a `%LOCALAPPDATA%\Vision-personal\app`, prepara su entorno y arranca. Su configuración vive
en `%LOCALAPPDATA%\Vision-personal\.env` (copiá ahí el `.env.example`).

## Configurar Vision

Todo está explicado, variable por variable, en [`backend/.env.example`](backend/.env.example). Lo
principal:

| Qué | Variables |
|---|---|
| Proveedor de IA | `LLM_PROVIDER` (`anthropic` u `openai_compatible`), `LLM_API_KEY`, `LLM_BASE_URL` (solo `openai_compatible`), `LLM_MODEL`, `LLM_MODELS` |
| Dictado por voz | `STT_BASE_URL`, `STT_API_KEY`, `STT_MODEL` (endpoint `/audio/transcriptions` compatible con OpenAI, ej. Whisper) |
| Empresa | `VISION_COMPANY_NAME`: nombre que aparece en la pantalla y en la presentación del asistente |
| Proxy corporativo | `HTTP_PROXY`, `HTTPS_PROXY`, `NO_PROXY` (localhost siempre va directo) |
| Conectores | `ORACLE_*`, `BQ_*`, `SHAREPOINT_*`/`AZURE_*`, `SAP_*` y sus `ENABLE_*` |

Las claves también se pueden cargar desde la app, en **Configuración > Credenciales**: quedan
cifradas en `.env.enc`, junto al `.env`. Nada de esto se versiona (ver `.gitignore`).

Sin `LLM_API_KEY`, Vision arranca igual y el chat explica qué falta configurar; nunca manda un
pedido a un servidor que no hayas configurado.

## Dónde guarda los datos

- Modo desarrollo: `backend\` (`.env`, `keys\`, `logs\`, `data\`, índice de sesiones `vision.db`).
- Vision.exe: `%LOCALAPPDATA%\Vision-personal\`.
- `VISION_DATA_DIR` (variable de entorno) cambia esa carpeta, por ejemplo para tener dos
  instalaciones separadas en la misma PC.

## Tests

Desde `backend\`, con el entorno virtual:

```bash
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.venv\Scripts\python.exe -m pytest -m "not e2e"     # rapido: todo menos el navegador real
.venv\Scripts\python.exe -m pytest -m e2e           # frontend en un Chromium real
```

La batería es 100 % aislada: nunca usa tus credenciales ni sale a internet (el proveedor de IA y el
dictado son falsos). Los e2e necesitan el Chromium de Playwright
(`python -m playwright install chromium`). No corras dos baterías e2e a la vez.

## Estructura

```
backend/app/        API (main.py), loop del modelo (llm/), herramientas (tools/), conectores,
                    persistencia (SQLite), programador de alarmas y agentes
backend/tests/      unit, integración y e2e
frontend/           index.html, js/app.js (toda la UI), css/, vendor/ (librerías locales, sin CDN)
launch_vision.py    lanzador de Vision.exe (solo librería estándar)
specs/              especificaciones de cada cambio
```
