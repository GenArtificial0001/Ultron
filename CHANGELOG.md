# Changelog

Formato: una entrada por versión, lo más nuevo arriba. Subir `__version__` en
`backend/app/version.py` y registrar acá cada vez que se toca `backend/app/` o `frontend/`.

## 2.1.1 (2026-10-26) — IA local sin internet

**Arreglo de un caso real:** ULTRON abierto sin internet, con Ollama y `qwen3:4b` en la PC, no respondía nada. Eran dos causas
encadenadas:
- **Contexto corto:** Ollama corría con su ventana de fábrica (4.096 tokens) y recortó en silencio el pedido de ULTRON (11.078
  tokens, casi todo instrucciones y herramientas). `qwen3:4b` «pensó» 3 minutos y terminó sin escribir nada.
  - Ahora, antes de mandar el pedido, ULTRON carga el modelo si hace falta, lee su ventana real y, si no entra, lo dice en el chat
    con el arreglo (`setx OLLAMA_CONTEXT_LENGTH 16384`). No lo manda recortado y no pausa el modelo, que no está caído.
  - El chequeo se hace al empezar el turno (antes de intentar compactar, que no lo arreglaría) y en cada paso.
- **Compactación y lectura de PDF:** mandaban su pedido al proveedor principal en la nube, no al del modelo elegido. Sin internet
  fallaba, el salto automático le echaba la culpa a Ollama y lo pausaba 10 minutos. Ahora van al proveedor del modelo, con el
  nombre que ese proveedor entiende.
- **Respuesta vacía:** si un modelo termina sin escribir nada, el chat lo explica en lugar de quedar en blanco.
- **Carpeta del proyecto:** ahora es `C:\Proyectos\Ultron`.
  - Al moverla, el `pylsp.exe` del entorno virtual quedó apuntando a la ruta vieja y los diagnósticos salían vacíos sin avisar.
  - El LSP ahora se lanza como `python -m pylsp`, que no depende de rutas guardadas.
- **README:** el paso 4 de la IA local (subir el contexto) es obligatorio, y la tabla de problemas suma el aviso nuevo y qué
  pasa sin internet.

## 2.1.0 (2026-10-25) — IA local

**Ollama y LM Studio aparecen solos** (spec `ia-local`). Con el motor abierto y un modelo descargado no hay que configurar nada:
- **Selector:** «Ollama (local)» o «LM Studio (local)» aparece con sus modelos de chat (los de embeddings no se listan), marcados
  «local» y con la **ventana de contexto real** con la que el motor cargó el modelo.
- **Sin demoras:** un motor apagado nunca frena a ULTRON. Cada consulta tiene un tope de 1,5 s y su resultado se recuerda: 5 min si
  responde y 30 s si no, así aparece solo poco después de abrirlo.
- **Salto automático:** si el motor local se cae a mitad de un turno, cuenta como caída de ese modelo y el turno sigue con el próximo
  de la lista.
- **Espera:** la primera respuesta de un modelo local tiene espera larga, porque el motor lo carga en memoria, y el aviso de
  «está tardando» sale a los 8 s.
- **Motor en otra PC:** `OLLAMA_BASE_URL` / `LMSTUDIO_BASE_URL` apuntan a otra PC de la red y ULTRON avisa que lo que se le manda
  sale de esta PC. `LOCAL_AI=off` apaga la búsqueda.
- **Seguridad:** las direcciones se validan (`http(s)://host[:puerto]`, nunca con usuario y clave adentro) y los motores locales se
  consultan siempre directo, sin el proxy.
- **README:** «Conexión a LLMs» sin el paso manual `CUSTOM_*` para la IA local.
- **Marca:** la barra lateral y la animación de arranque dicen **ULTRON** (decían VISION), y el logo de la «V» de Vision ya no
  aparece al lado del título.

## 2.0.0 (2026-10-25) — ULTRON

**Vision-Personal pasa a llamarse ULTRON** (spec `portar-desde-origen`) y trae todas las mejoras de la versión de origen hasta
su 0.36.2 (74 versiones), sin nada propio de esa empresa. El diseño de la interfaz no cambia.

**Lo propio de ULTRON**
- **Nombre:** ULTRON en todo lo visible: `Ultron.exe`, `Iniciar-Ultron.bat`, títulos, avisos y la presentación del
  asistente. Se lo llama por voz con «**Ultron**» (o «Jarvis»), ya no con «Vision».
- **Carpeta propia:** `%LOCALAPPDATA%\Ultron`. La primera vez copia lo de `%LOCALAPPDATA%\Vision-personal` (el `.env`, las
  claves, las sesiones, las alarmas y `connections.json`), sin tocar la carpeta vieja, y avisa una vez en el chat. Puerto 8030.
- **Apagados de fábrica:** Outlook y Teams, y Office. Se prenden en Configuración > Conexiones.
- **Outlook y Teams:** la dirección del conector MCP de Microsoft 365 se configura (antes era fija).
- **Configuración:** el proveedor de IA principal, el dictado y el nombre de la empresa se cargan desde Configuración >
  Conexiones > General, en «Inteligencia artificial». La configuración queda cifrada, sin necesidad de `.env`.
- **Proxy:** sin proxy predeterminado de fábrica. La autocuración prueba lo guardado, su DNS, la PAC de Windows y salir directo.
- **README:** sección **«Conexión a LLMs»** con los proveedores y la IA local (Ollama), con pasos y tiempos medidos.

**Heredado de la versión de origen (por tema)**
- **Conexiones:** gestor de conexiones cifrado dentro de la app (sin `.env` obligatorio), conexiones con nombre (varias del
  mismo tipo), interruptor por conexión, «Probar» antes de guardar, Confluence (solo lectura) y SharePoint con listas completas.
- **Chat y sesiones:**
  - historial en SQLite que sobrevive a reinicios;
  - Detener inmediato y cola de mensajes, con barra de progreso real («Escribiendo archivo… N KB»);
  - aviso de contexto largo con un chip de uso, y aviso de compactación automática;
  - índice de sesiones resiliente (copia diaria y re-escaneo) y borrado seguro con rastro;
  - al cambiar de carpeta, pregunta si copia el historial, y se puede importar el historial de una carpeta;
  - sesiones públicas y privadas, y `/update` con lo nuevo de las públicas de otros;
  - aviso al eliminar una sesión que otros usan.
- **Archivos:** adjuntar PDF, Word, Excel y PowerPoint (también las imágenes de adentro), con lectura de PDF escaneados.
- **Modelos:** ventana de contexto real por modelo cuando el proveedor la informa, aviso claro cuando un modelo no admite
  herramientas, y reintento automático cuando el proveedor corta una respuesta a la mitad.
- **Voz:**
  - Modo Cerebro con Espacio como botón de turnos o para mantener y hablar, sensibilidad del micrófono y cartel de estado;
  - Modo reunión que escucha el audio de la PC sin silenciarlo (en mono, para que el dictado lo acepte);
  - control del micrófono de Windows (silenciado y volumen);
  - informe técnico hablado con nivel resumido o detallado.
- **Office por COM:** leer y escribir el Excel que tenés abierto y convertir documentos.
- **Microsoft 365 por MCP:** correo, agenda, chats y canales de Teams y OneDrive; enviar siempre pide confirmación.
- **Robustez:**
  - lanzador que no deja la instalación a medias (y la repara), con dependencias verificadas;
  - LSP que se recupera solo, `grep` que no se cuelga y tiempos de herramientas seguros;
  - errores con estilo de nota del sistema en rojo y luz de conexión en vivo;
  - aviso de Windows al terminar y metadatos de versión en el `.exe`.

**Arreglos propios de esta versión**
- **LSP:** el cliente esperaba la respuesta de `initialize` solo 5 s. En una PC lenta, o con el antivirus revisando un entorno
  recién instalado, `pylsp` tardó 10 s y el primer diagnóstico salía vacío siempre. Ahora espera el arranque con su propio margen.
- **Proveedores:** cuando un proveedor no responde, el aviso dice qué hacer (si es el proxy, Configuración > Red); el texto
  crudo de la falla queda solo en el log.
- **Rendimiento del chat:** en cada paso, armar la lista de herramientas abría la base una vez por herramienta (69 por paso) para
  mirar los interruptores. Con la PC cargada, un turno largo pasaba minutos solo en eso. Ahora se leen una vez por paso: el test
  de 100 pasos bajó de 194 s a 15 s.

**Quitado (era solo de la versión de origen):** el SDK y el login de su gateway de IA, su índice privado de paquetes, sus skills
internas, su flujo dev→prod y sus herramientas internas de desarrollo.

## 1.1.0 (2026-09-27)

**Voz de Vision con motores intercambiables** (spec `voz-fish-audio`) y **varios proveedores de IA a la vez**
(spec `varios-proveedores`).

- **Varios proveedores de IA a la vez**: Claude, Gemini, DeepSeek, Groq, OpenRouter, OpenAI y otro compatible,
  con solo poner la clave en el `.env` (Vision sabe la dirección de cada uno y la toma sin reiniciar). El
  selector los agrupa por proveedor, en Configuración se eligen los modelos a mostrar y el salto automático
  salta entre proveedores (por falta de crédito, límite o caída; sin crédito pausa al proveedor entero). Los
  gratuitos piden confirmación. La lista del salto arranca vacía. La configuración de siempre (`LLM_*`) sigue
  funcionando como proveedor principal.
- Arreglo: "sin crédito" en Anthropic ahora se explica como tal, y el cupo agotado de un modelo de Gemini se
  trata como límite de ese modelo, no de la cuenta.

- **Configuración > Audio > Motor de voz**: una lista de prioridad con Fish Audio, ElevenLabs y la
  clonación local de tu voz. Vision usa la primera que responda; la voz del navegador queda siempre
  al final, así nunca se queda mudo. De fábrica la lista está vacía (voz del navegador).
- **Envío de texto consciente**: sumar un servicio en la nube pide confirmar que el texto leído se
  le envía; sin esa confirmación el backend nunca le manda nada.
- **Salto automático**: si una voz se queda sin crédito, sin clave o no responde, sigue con la
  siguiente (por ejemplo, del modelo pago de Fish al gratuito), la pone en pausa y avisa una vez.
- **Frase por frase**: la respuesta se pide por tramos y el siguiente se genera mientras suena el
  actual, para empezar a hablar antes.
- **Efecto «IA»** estilo Jarvis (eco corto y ecualización suave), opcional.
- **Clonación local** (Chatterbox): se instala con un botón que primero simula la instalación y no
  instala nada si fuera a cambiar paquetes que Vision ya usa; la muestra de voz (WAV o MP3, 10 s como
  mínimo) queda en la carpeta de datos.
- Claves `FISH_API_KEY` y `ELEVENLABS_API_KEY` en el `.env` o en Configuración > Credenciales.
- **Informe técnico hablado** (spec `informe-tecnico-hablado`): mientras trabaja, Vision cuenta en voz
  alta lo que hace. Al escribir código lo recorre **en vivo**, a medida que el modelo lo escribe: cada
  comentario, el título, el sidebar (y de qué lado), las tarjetas, los gráficos, las tablas y las
  conexiones a bases o APIs; con el informe prendido le pide al modelo que comente cada sección en
  español. Anuncia el plan ("Paso 2 de 4: …", "Listo: …"), dice una frase por cada acción ("Consulto
  Oracle: tablas STOCK, PIEZAS"), avisa "Sigo con: …" si pasan ~15 s sin novedades, nunca se atrasa
  más de 3 frases y nunca dice una clave. Solo en voz. Interruptor en Configuración > Audio, prendido
  de fábrica (habla si «Leer en voz alta» está activo).
- **Credenciales en una instalación nueva**: si no existe la clave SSH de la PC original, se genera
  una clave de cifrado propia (`keys/credentials.key`) la primera vez que se guarda algo.
- **Arreglo: el chat con Claude rechazaba hasta un "hola"** ("no aceptó este pedido"). Vision mandaba
  `temperature` y `top_p` juntos, y Anthropic rechaza `top_p` en sus modelos nuevos y ambos juntos en los
  anteriores. Ahora cada modelo de Claude recibe solo lo que acepta: Sonnet 5, Opus 4.7 en adelante y
  los que salgan, ningún parámetro de muestreo; Haiku 4.5 y Opus/Sonnet 4.6 o anteriores, solo `temperature`.
- En Configuración > Audio, la primera voz de la lista dice «principal» y cada fila muestra qué voz usa.
- **Arreglo: el Modo Cerebro y el modo reunión explican por qué no transcriben.** Sin el dictado
  configurado decían "sigo intentando, apagá y volvé a prender el modo"; ahora muestran el motivo
  real (por ejemplo, que el dictado no está configurado).
- Arreglo: una confirmación abierta desde Configuración (por ejemplo, eliminar una skill) quedaba
  tapada por el panel y no se podía tocar.
- **README: guías paso a paso** para activar el dictado (Groq o OpenAI), sumar voces (Fish Audio,
  ElevenLabs, clonación local) y qué hacer ante los errores más comunes (verificación de Cloudflare,
  crédito de API de Fish, dictado sin configurar).
- QA: la regla de campos con `aria-label` de `scripts/qa_battery.py` nunca contaba; corregida.

## 1.0.0 (2026-09-27)

Primera versión de **Vision genérico** (spec `vision-generico`), nacida del código de Vision
0.19.3 sin nada propio de una empresa en particular.

- **Proveedor de IA configurable**: Claude con clave de Anthropic (`LLM_PROVIDER=anthropic`, vía su
  capa compatible con OpenAI) o cualquier servicio compatible con OpenAI (`openai_compatible` +
  `LLM_BASE_URL`: OpenAI, Azure OpenAI, el gateway de una empresa). Sin clave o sin dirección, el
  chat explica qué falta configurar y nunca manda el pedido a otro servidor.
- **Lista de modelos**: `LLM_MODELS` si está completa; si no, la que publica el proveedor
  (`GET /models`). El salto automático viene precargado con la familia de Claude.
- **Dictado por voz** con su propio proveedor (`STT_BASE_URL`, `STT_API_KEY`, `STT_MODEL`). Sin
  configurar, el micrófono avisa que el dictado no está disponible.
- **Proxy de cualquier empresa**: `HTTP_PROXY`/`HTTPS_PROXY`/`NO_PROXY` del `.env` valen para la app
  y para instalar dependencias; localhost siempre directo.
- **Marca neutra**: logo propio, nombre de la empresa configurable (`VISION_COMPANY_NAME`) en la
  pantalla y en la presentación del asistente, temas "Rojo" y "Azul".
- **Configuración > Credenciales** suma el proveedor de IA y el dictado (claves cifradas).
- Carpeta de datos propia (`%LOCALAPPDATA%\Vision-personal`, índice de sesiones incluido) y puerto
  8030, para convivir con otras instalaciones de Vision en la misma PC.
- Tests: la batería de Vision adaptada al proveedor genérico, más `test_vision_generico.py`
  (proveedor, modelos, proxy, empresa y un guardián que falla si vuelve a aparecer contenido propio
  de una empresa en el código).
