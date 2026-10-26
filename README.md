# ULTRON

Asistente conversacional con herramientas reales para el trabajo diario: lee y escribe archivos, ejecuta comandos, consulta
bases de datos, arma tableros y documentos, programa alarmas y agentes, y conversa por voz. Backend Python/FastAPI y frontend
web sin compilación (JavaScript puro, todo local, sin CDN).

Funciona con **varios proveedores de IA a la vez** (Claude, Gemini, Groq, DeepSeek, OpenRouter, OpenAI o cualquier servicio
compatible con OpenAI) y con **IA local** en tu propia PC (Ollama o LM Studio). Se puede usar detrás del proxy de una red corporativa.

> ULTRON 2.0 nace de Vision-Personal 1.1.0 más todas las mejoras de la versión de origen hasta su 0.36.2. Si venías usando
> Vision-Personal, la primera vez que abras ULTRON copia tu configuración y tus sesiones a su carpeta nueva (ver
> [Migración desde Vision-Personal](#migración-desde-vision-personal)).

## Qué hace

| Área | Qué incluye |
|---|---|
| Chat con herramientas | bash, archivos (leer, escribir, editar, buscar), listas de tareas, skills bajo demanda, fórmulas (KaTeX), gráficos, tableros exportables |
| Modelos | selector agrupado por proveedor, IA local, **salto automático** al siguiente modelo (de cualquier proveedor) si uno se queda sin crédito, llega a su límite o se cae |
| Voz | dictado (servicio configurable), lectura en voz alta (voz del navegador, Fish Audio, ElevenLabs o tu voz clonada), **Modo Cerebro** (conversación continua, Espacio para hablar) y **Modo reunión** (también escucha el audio de la PC). Se lo llama «**Ultron**» (o «Jarvis») |
| Sesiones | historial en SQLite por carpeta de proyecto, sesiones públicas y privadas, `/update`, búsqueda, compactación con aviso |
| Datos (opcionales, apagados de fábrica) | Oracle, BigQuery, SharePoint/Microsoft Graph, SAP (OData v2), Confluence |
| Office y Microsoft 365 (opcionales, apagados de fábrica) | Excel y Word por COM; Outlook y Teams por un conector MCP de Microsoft 365 |
| Productividad | alarmas y agentes programados, avisos de Windows al terminar, copiar fórmulas en texto legible |

## Requisitos

- Windows 10/11 con **Python 3.12 o superior** en el PATH (`python --version`).
- Al menos **una forma de hablar con un modelo**: la clave de un proveedor o una IA local instalada (ver
  [Conexión a LLMs](#conexión-a-llms)). Sin eso ULTRON arranca, pero el chat te dice qué falta.

## Instalar y arrancar

**Ultron.exe** (portable, un solo archivo): `build_exe.bat` lo compila. Al abrirlo extrae el código a
`%LOCALAPPDATA%\Ultron\app`, prepara su entorno de Python (la primera vez tarda unos minutos) y abre
http://127.0.0.1:8030/. La configuración vive en `%LOCALAPPDATA%\Ultron\.env`: copiá ahí `backend\.env.example`.

**Modo desarrollo** (se recarga solo con cada cambio):

1. Copiá `backend\.env.example` a `backend\.env`.
2. Configurá al menos un LLM (sección siguiente).
3. Doble clic en `Iniciar-Ultron.bat`. La primera vez crea `backend\.venv` e instala las dependencias.
4. Se abre http://127.0.0.1:8030/.

## Conexión a LLMs

Es el paso más importante de la instalación: **sin un LLM conectado, ULTRON no responde**. Hay dos caminos y se pueden
combinar (por ejemplo, Claude como principal e IA local de respaldo en el salto automático).

### A. Proveedores en la nube (con tu clave)

Poné **solo la clave** en el `.env`. ULTRON ya conoce la dirección de cada proveedor y la toma sin reiniciar:

| Proveedor | Línea del `.env` | Dónde se saca la clave | Gratis |
|---|---|---|---|
| Claude (Anthropic) | `ANTHROPIC_API_KEY=` | https://console.anthropic.com | no |
| Gemini (Google) | `GEMINI_API_KEY=` | https://aistudio.google.com/apikey | sí, con límites |
| Groq (gpt-oss, Qwen, Llama) | `GROQ_API_KEY=` | https://console.groq.com | sí, con límites |
| DeepSeek | `DEEPSEEK_API_KEY=` | https://platform.deepseek.com | no (muy barato) |
| OpenRouter (muchos) | `OPENROUTER_API_KEY=` | https://openrouter.ai | algunos modelos |
| OpenAI | `OPENAI_API_KEY=` | https://platform.openai.com | no |
| Otro compatible con OpenAI | `CUSTOM_BASE_URL=` y `CUSTOM_API_KEY=` | el de tu servicio | según el servicio |

Después, en **Configuración > Modelo e inteligencia > Proveedores de IA**, marcá qué modelos querés ver en el selector. Los
**gratuitos** piden confirmar una vez: en su plan gratis pueden usar lo que les mandás para entrenar, así que no los uses con
datos de tu empresa. En **Salto automático de modelos** armá la lista de prioridad (arranca vacía).

El proveedor «principal» de siempre (`LLM_PROVIDER`, `LLM_API_KEY`, `LLM_BASE_URL`, `LLM_MODEL`) sigue funcionando, y también
se carga desde **Configuración > Conexiones > General**, en «Inteligencia artificial».

### B. IA local en tu PC (Ollama)

Lo que mandás **no sale de la PC**, no tiene costo por uso ni cupos. Pasos medidos en una instalación real (25/10/2026, Intel
Core Ultra 5 238V con gráfica integrada Arc, 32 GB de RAM, sin placa de video dedicada):

1. **Bajá Ollama** de https://ollama.com/download (o la release de https://github.com/ollama/ollama/releases): `OllamaSetup.exe`,
   unos **1,7 GB**. Está firmado por «Ollama Inc.».
2. **Instalalo**: es por usuario, **no pide permisos de administrador** (queda en `%LOCALAPPDATA%\Programs\Ollama`), queda en la
   bandeja y arranca con Windows.
3. **Bajá un modelo que admita herramientas**. Con 16 GB de RAM o más y sin placa de video, `qwen3:4b` (2,5 GB, tardó 87 s):
   ```bash
   ollama pull qwen3:4b
   ```
   Con menos RAM: `qwen3:1.7b`. Con placa de video de 8 GB o más: `qwen3:8b`.
4. **Subí el contexto y, si tu PC tiene gráfica integrada, activala.** Son dos variables de entorno de Windows. Después hay que
   cerrar Ollama desde la bandeja y volver a abrirlo:
   ```bash
   setx OLLAMA_CONTEXT_LENGTH 16384
   ```
   ```bash
   setx OLLAMA_IGPU_ENABLE 1
   ```
   - **Contexto:** Ollama arranca con 4.096 tokens. El prompt de ULTRON con sus herramientas no entra, y Ollama **recorta el
     principio sin avisar**.
   - **Gráfica integrada:** Ollama la detecta pero la descarta si no se lo pedís. En la PC de prueba, la respuesta en caliente
     bajó de 24,8 s a **9,4 s** y una llamada con herramienta de 150 s a **32 s**.
5. **Comprobá que responde**: abrí http://127.0.0.1:11434/v1/models en el navegador. Tiene que listar el modelo.
6. **Listo: no hay que configurar nada.** Con Ollama abierto, ULTRON lo encuentra solo. En el selector de modelos aparece
   «Ollama (local)», con sus modelos de chat marcados «local». Los de embeddings no se listan.
   - Si cerrás Ollama, desaparece del selector en unos minutos. Cuando lo volvés a abrir, aparece en unos 30 s.
   - Un motor apagado nunca frena a ULTRON: la consulta tiene un tope de 1,5 s y su resultado se recuerda.
   - El selector muestra la **ventana de contexto real** con la que Ollama cargó el modelo (la columna CONTEXT de `ollama ps`).
     Si dice 4.096, hacé el paso 4.
   - Si el motor local se cae a mitad de un turno y tenés el salto automático, sigue con el próximo modelo de la lista.

**LM Studio** (https://lmstudio.ai) funciona igual: activá su servidor local (pestaña *Developer*, puerto 1234) y aparece solo
como «LM Studio (local)».

**Opcional, en el `.env`:**

| Variable | Para qué |
|---|---|
| `OLLAMA_BASE_URL=http://192.168.0.20:11434/v1` | Ollama en otra PC de tu red, o en otro puerto. ULTRON avisa que lo que le mandás sale de esta PC. |
| `LMSTUDIO_BASE_URL=http://127.0.0.1:1234/v1` | Lo mismo para LM Studio. |
| `LOCAL_AI=off` | No buscar motores locales. |

Las direcciones deben ser `http(s)://host[:puerto]`. Una dirección con usuario y clave adentro se rechaza, y se usa la de fábrica.
Los motores locales se consultan siempre directo, sin pasar por el proxy.

### Si algo falla con el LLM

| Qué ves | Qué hacer |
|---|---|
| «No hay ningún proveedor de IA con clave» | Cargá al menos una clave (A) o la IA local (B). |
| «No se pudo consultar la lista de modelos de: …» | Revisá esa clave. Si dice algo del proxy, andá a **Configuración > Red** y tocá **Reconectar**. |
| «<modelo> no admite herramientas» | Ese modelo solo sirve para conversar: elegí otro (en Ollama, `qwen3` o `llama3.1` admiten herramientas). |
| La IA local tarda mucho en la primera respuesta | El motor carga el modelo en memoria (en la PC de prueba, 37 a 53 s). Las siguientes son más rápidas. Activá la gráfica integrada (paso 4). |
| La IA local «se olvida» de lo que le pediste | El contexto está en 4.096: subilo (paso 4). |
| «La cuenta del servicio de inteligencia artificial se quedó sin crédito» | Cargá crédito o armá el salto automático con un gratuito o con la IA local. |

## Dictado y voz

**Dictado** (micrófono, Modo Cerebro y Modo reunión): hace falta un servicio que pase la voz a texto. Con **Groq** (gratis con
límites): creá una clave en https://console.groq.com y cargá en **Configuración > Conexiones > General**, en «Inteligencia
artificial»:
- **Dictado: dirección**: `https://api.groq.com/openai/v1`
- **Dictado: modelo**: `whisper-large-v3-turbo`
- **Dictado: clave**: la de Groq

Con OpenAI: dirección `https://api.openai.com/v1` y modelo `whisper-1`.

**Voz de ULTRON**: de fábrica habla con la voz del navegador. En **Configuración > Audio > Motor de voz** podés sumar Fish
Audio, ElevenLabs o tu voz clonada en esta PC, en una lista de prioridad; al final siempre queda la voz del navegador. Las claves
(`FISH_API_KEY`, `ELEVENLABS_API_KEY`) van en el `.env` o en la configuración. El **informe técnico hablado** cuenta en voz alta lo
que ULTRON hace mientras trabaja.

**Modo Cerebro**: conversación continua por voz. Con **Espacio** se habla en turnos (o se mantiene apretado para hablar si hay
mucho ruido) y con **Esc** se apaga. Se lo llama diciendo «**Ultron**».

## Configuración

Todo está explicado, variable por variable, en [`backend/.env.example`](backend/.env.example), y casi todo se puede cargar desde
la app, en **Configuración > Conexiones**: queda cifrado y se aplica sin reiniciar.

| Qué | Dónde |
|---|---|
| Proveedores de IA y dictado | [Conexión a LLMs](#conexión-a-llms) |
| Nombre de la empresa | `VISION_COMPANY_NAME` (aparece en la pantalla y en la presentación del asistente) |
| Proxy corporativo | `HTTP_PROXY`, `HTTPS_PROXY`, `NO_PROXY` o **Configuración > Red** (localhost siempre va directo). Si el proxy guardado deja de responder, ULTRON prueba la PAC de Windows y salir directo, y te avisa |
| Conectores de datos | `ORACLE_*`, `BQ_*`, `SHAREPOINT_*`/`AZURE_*`, `SAP_*`, `CONFLUENCE_*` y sus `ENABLE_*` (todos apagados de fábrica), o conexiones con nombre en `connections.json` (ver `backend/connections.example.json`) |
| Office (Excel y Word) | apagado de fábrica: se prende en **Configuración > Conexiones** (necesita Office instalado) |
| Outlook y Teams | apagado de fábrica: hace falta un conector MCP de Microsoft 365. Cargá su dirección en «Outlook y Teams: dirección del conector», prendelo e iniciá sesión |

Las claves nunca se versionan (ver `.gitignore`) ni vuelven a la pantalla.

## Migración desde Vision-Personal

ULTRON guarda todo en `%LOCALAPPDATA%\Ultron` (antes `%LOCALAPPDATA%\Vision-personal`). La primera vez que lo abrís, si la
carpeta nueva no tiene datos propios:
- copia el `.env`, las claves, el índice de sesiones, las alarmas y agentes y `connections.json`;
- no copia el código extraído ni los logs viejos;
- avisa una vez en el chat qué copió;
- **no borra ni modifica la carpeta vieja**: la podés borrar cuando confirmes que está todo.

ULTRON usa el puerto 8030, así que puede convivir con otro Vision instalado en la misma PC.

## Dónde guarda los datos

- Ultron.exe: `%LOCALAPPDATA%\Ultron\` (`.env`, `keys\`, `logs\`, índice `vision.db`, `connections.json`).
- Modo desarrollo: `backend\`.
- `VISION_DATA_DIR` (variable de entorno) cambia esa carpeta.
- El historial de cada sesión vive en la carpeta de proyecto que le asignes (`<carpeta>\db\vision.db`).

## Herramientas disponibles (69 tools)

| Tool | Descripción | Peligrosa (auditada) |
|---|---|---|
| `read` | Lee archivos, directorios, PDF, Word, Excel y PowerPoint | no |
| `write` | Crea o sobrescribe archivos | sí |
| `edit` | Reemplaza texto en archivos | sí |
| `glob` | Busca archivos por patrón (sin carpetas de dependencias) | no |
| `grep` | Busca texto en archivos (con tope de tiempo) | no |
| `bash` | Ejecuta comandos de shell (pandas, DuckDB, Playwright disponibles) | sí |
| `webfetch` | Trae el contenido de una URL | no |
| `todowrite` | Arma y actualiza la lista de tareas visible | no |
| `question` | Repregunta con opciones cliqueables | no |
| `skill` | Carga una skill (SKILL.md) bajo demanda | no |
| `oracle_query` | SQL contra Oracle; lo que modifica datos pide confirmación | no |
| `bigquery_query` / `bigquery_list_tables` | SQL y listado de BigQuery, con guardas de riesgo y de costo | no |
| `sharepoint_list_items` / `sharepoint_list_lists` | Lectura de SharePoint | no |
| `sap_query` | OData genérico contra SAP | no |
| `graph_search` / `graph_directory_lookup` / `graph_directory_manager` | Búsqueda y directorio en Microsoft Graph | no |
| `render_chart` | Gráfico con Chart.js (local) | no |
| `record_decision` | Registra una decisión en `decisions/decisions.md` | no |
| `exportar_dashboard` | Exporta un tablero como HTML autónomo, validado en un navegador real y sin secretos | no |
| `confluence_*` (6 tools) | Espacios, búsqueda, árbol de páginas, contenido y adjuntos de Confluence (solo lectura) | no |
| `office_*` (4 tools) | Excel (lista libros abiertos, lee y escribe rangos) y conversión de documentos por COM | no |
| `m365_*` (37 tools) | Outlook y Teams por un conector MCP: correo (leer, buscar, borradores; enviar siempre pide confirmación), agenda, chats, canales, OneDrive | no |

Las tools «peligrosas» se ejecutan sin pedir confirmación, pero cada ejecución queda en `logs\audit.log`. Cuando un conector de
datos devuelve más filas que el umbral (50 de fábrica), ULTRON exporta el resultado completo a un CSV en la carpeta de la sesión
y deja el enlace en el chat.

## Tests

Desde `backend\`, con el entorno virtual:

```bash
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```
```bash
.venv\Scripts\python.exe -m pytest -m "not e2e"
```
```bash
.venv\Scripts\python.exe -m pytest -m e2e
```

La batería está 100 % aislada: nunca usa tus credenciales ni sale a internet (los proveedores de IA y el dictado son falsos). Los
e2e necesitan el Chromium de Playwright (`python -m playwright install chromium`); no corras dos baterías e2e a la vez. Antes de
subir algo, corré también `test_vision_generico.py`: frena cualquier dato de una empresa en particular.

## Estructura

```
backend/app/        API (main.py), loop del modelo y proveedores (llm/), herramientas (tools/), conectores, Microsoft 365 (m365/),
                    Office (office/), persistencia (SQLite), voz (voice/), alarmas y agentes (scheduler/)
backend/tests/      unit, integración y e2e
frontend/           index.html, js/app.js (toda la UI), css/, vendor/ (librerías locales, sin CDN)
launch_vision.py    lanzador de Ultron.exe (solo librería estándar)
docs/               guía para crear tools y conexiones
specs/              especificaciones de cada cambio
```
