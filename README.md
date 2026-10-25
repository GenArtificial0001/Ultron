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
| Voz | dictado (con un servicio de transcripción configurable), lectura en voz alta con la voz del navegador, Fish Audio, ElevenLabs o tu voz clonada (con salto automático entre ellas), Modo Cerebro y modo reunión |
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
| Más proveedores de IA a la vez | `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `DEEPSEEK_API_KEY`, `GROQ_API_KEY`, `OPENROUTER_API_KEY`, `OPENAI_API_KEY` (solo la clave: Vision sabe la dirección de cada uno) |
| Dictado por voz | `STT_BASE_URL`, `STT_API_KEY`, `STT_MODEL` (endpoint `/audio/transcriptions` compatible con OpenAI, ej. Whisper) |
| Voz de Vision | `FISH_API_KEY`, `ELEVENLABS_API_KEY` (y opcionalmente `FISH_VOICE_ID`, `ELEVENLABS_VOICE_ID`); el orden de las voces, el modelo y el efecto se eligen en **Configuración > Audio** |
| Empresa | `VISION_COMPANY_NAME`: nombre que aparece en la pantalla y en la presentación del asistente |
| Proxy corporativo | `HTTP_PROXY`, `HTTPS_PROXY`, `NO_PROXY` (localhost siempre va directo) |
| Conectores | `ORACLE_*`, `BQ_*`, `SHAREPOINT_*`/`AZURE_*`, `SAP_*` y sus `ENABLE_*` |

Las claves también se pueden cargar desde la app, en **Configuración > Credenciales**: quedan
cifradas en `.env.enc`, junto al `.env`. Nada de esto se versiona (ver `.gitignore`).

Sin `LLM_API_KEY`, Vision arranca igual y el chat explica qué falta configurar; nunca manda un
pedido a un servidor que no hayas configurado.

**`.env` o Credenciales:** lo que cargás en **Configuración > Credenciales** se aplica al instante.
Si en cambio editás el `.env`, cerrá y volvé a abrir Vision para que lo tome.

## Guías paso a paso

### Usar varios proveedores de IA a la vez (Claude, Gemini, Groq, DeepSeek…)

1. En el `.env` poné **solo la clave** de cada proveedor que quieras usar, por ejemplo
   `GEMINI_API_KEY=…` o `GROQ_API_KEY=…` (la lista completa está en `backend/.env.example`). No hace
   falta la dirección ni otros parámetros, y no hace falta reiniciar Vision.
2. En **Configuración > Modelo e inteligencia > Proveedores de IA** vas a ver cada proveedor con sus
   modelos: marcá cuáles querés en el selector y tocá **Guardar proveedores**.
3. Los **gratuitos** (Gemini, Groq) piden confirmar una vez: en su plan gratis pueden usar lo que les
   mandás para mejorar sus productos. No los uses con datos de tu empresa.
4. En **Salto automático de modelos** armá la lista de prioridad (arranca vacía), por ejemplo
   Claude → Gemini gratis → Groq. Si uno se queda sin crédito, llega a su límite o se cae, el chat
   sigue con el siguiente en el mismo mensaje y te avisa.

Dónde sacar cada clave: Gemini en https://aistudio.google.com/apikey, Groq en https://console.groq.com,
OpenRouter en https://openrouter.ai, Claude en https://console.anthropic.com.

### Activar el dictado por voz (micrófono, Modo Cerebro y modo reunión)

Vision necesita un servicio que pase tu voz a texto; Claude no lo hace. Sin configurarlo, el
micrófono avisa "El dictado por voz no está configurado". El audio que dictás se envía a ese servicio.

**Con Groq (tiene plan gratuito):**

1. Entrá a https://console.groq.com y creá la cuenta (podés entrar con Google).
2. En el menú, **API Keys → Create API Key**, con el nombre `Vision`. Copiala: empieza con `gsk_` y se
   muestra una sola vez.
3. En Vision, **Configuración > Credenciales > Inteligencia artificial (proveedor y dictado)**:
   - **Dictado: dirección** → `https://api.groq.com/openai/v1`
   - **Dictado: clave** → la que copiaste
   - **Dictado: modelo** → `whisper-large-v3-turbo`
4. Tocá **Guardar** en esa sección y probá el botón del micrófono.

**Con OpenAI (pago, barato):** lo mismo, con la dirección `https://api.openai.com/v1`, una clave de
https://platform.openai.com y el modelo `whisper-1`.

### Darle a Vision una voz mejor (Fish Audio, ElevenLabs o tu voz clonada)

De fábrica Vision habla con la voz del navegador. Para sumar otras:

1. **Creá la clave del servicio:**
   - **Fish Audio** (https://fish.audio): en el panel, sección de desarrolladores → **API Keys** → crear.
     Ojo: los créditos que muestra el panel son de la plataforma web; para el modelo pago (`s2.1-pro`)
     desde Vision hace falta **crédito de API**, que se compra aparte. El modelo gratuito
     (`s2.1-pro-free`) no lo necesita, pero puede tardar varios segundos en empezar a hablar.
   - **ElevenLabs** (https://elevenlabs.io): **Create API Key** con **Restrict Key** activado,
     **Text to Speech → Access**, **Voices → Read** (para elegir voces de tu cuenta), **User → Read**
     (opcional) y todo lo demás en **No Access**. Conviene ponerle un tope de créditos.
2. **Cargala en Vision:** **Configuración > Credenciales > Voz de Vision** (o `FISH_API_KEY` /
   `ELEVENLABS_API_KEY` en el `.env`). Nunca la pegues en un chat.
3. **Armá la lista en Configuración > Audio > Motor de voz:**
   - Elegí el servicio y el modelo y tocá **Agregar**. Vision te pide confirmar que el texto que lee
     en voz alta se le envía a ese servicio.
   - Pegá el **ID de la voz** (en Fish es el código de la dirección `fish.audio/m/<ID>`) o elegila
     de las voces de tu cuenta.
   - **La primera de la lista es la voz principal**; ordenala con ↑ ↓. Las de abajo son respaldo si
     la principal falla o se queda sin crédito, y al final siempre queda la voz del navegador.
   - Tocá **Guardar voz** y probá cada voz con ▶.
4. Opcional: el **Efecto «IA»** (eco corto y ecualización suave, estilo Jarvis).
5. **Hablar informe técnico** (Configuración > Audio, prendido de fábrica): mientras trabaja, Vision
   cuenta lo que hace. Cuando escribe código lo recorre en vivo (comentarios, título, sidebar,
   tarjetas, gráficos, conexiones a bases), anuncia cada paso del plan y, si pasan ~15 s sin
   novedades, dice en qué sigue. Las claves y contraseñas nunca se dicen. Solo suena si «Leer en voz
   alta» está activo.

**Tu voz clonada en esta PC:** en **Configuración > Audio > Clonación local de tu voz**, tocá
**Instalar clonación local** (descarga varios GB; antes revisa que no cambie nada de lo que Vision ya
usa) y subí una muestra de tu voz en WAV o MP3 (10 s como mínimo; ideal 1 a 2 minutos, sin ruido).
Sin placa de video NVIDIA tarda varios segundos por frase.

### Si algo falla

| Qué ves | Qué hacer |
|---|---|
| En la página de un servicio (Groq, etc.): "no cf token" o la verificación no aparece | Es la verificación de Cloudflare ("no soy un robot"). Esperá a que termine sola antes de continuar; si no aparece, probá en una ventana de incógnito, en otro navegador o desde otra red (los datos del celular): la red de la empresa o una extensión del navegador pueden bloquearla. |
| "El dictado por voz no está configurado" | Seguí la guía de dictado de arriba. |
| "Fish Audio se quedó sin crédito" | Es el crédito de API (aparte del de la plataforma web): cargalo, o usá el modelo gratuito `s2.1-pro-free`. |
| "El modelo … no aceptó este pedido" | Revisá en "Detalle técnico" el motivo; si se repite, elegí otro modelo en el selector de arriba. |
| "La cuenta del servicio de inteligencia artificial se quedó sin crédito" | Cargá crédito en ese servicio o armá el salto automático con un proveedor gratuito (Gemini, Groq). |
| Un proveedor dice "no respondió" en Configuración | Revisá su clave en el `.env`. Si la clave está bien, puede que la red de tu empresa bloquee ese servicio. |

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
