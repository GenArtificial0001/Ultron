# Changelog

Formato: una entrada por versión, lo más nuevo arriba. Subir `__version__` en
`backend/app/version.py` y registrar acá cada vez que se toca `backend/app/` o `frontend/`.

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
