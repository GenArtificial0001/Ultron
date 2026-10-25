---
feature: voz-fish-audio
status: IN_DEVELOPMENT       # NEW | IN_PROGRESS | PENDING_APPROVAL | APPROVED | IN_DEVELOPMENT | DONE
created: 2026-09-27
last_updated: 2026-09-27
approved_on: 2026-09-27   # "approved" (usuario, en el chat)
scores:
  objetivo: 100
  inputs: 100
  outputs: 100
  conexiones: 100
  criterios_aceptacion: 100
  reglas_negocio: 100
  seguridad: 100
  performance: 100
  ui_ux: 100
  mantenimiento: 100
viabilidad: Alta           # Alta | Media | Baja
autonomia_estimada: 90% -- falta la muestra de voz para la clonación local y escuchar el resultado (solo el usuario)
---

# Spec: Voz de Vision con motores intercambiables (Fish Audio, ElevenLabs, clonación local)

> Esta spec es la única fuente de verdad para esta feature. No se escribe código hasta que el estado sea `APPROVED`.

## 🔴 Acciones Requeridas (bloqueos abiertos)

- [x] **Cuenta y clave de Fish Audio** (bloquea Conexiones y la prueba real): crear la cuenta en
  fish.audio, generar una API key y guardarla en `backend\.env` como `FISH_API_KEY=...`. Nunca
  pegarla en el chat. Solo la puede hacer el usuario.
- [x] **Cuenta y clave de ElevenLabs** (bloquea Conexiones y su prueba real): mismo criterio, en
  `backend\.env` como `ELEVENLABS_API_KEY=...`, con permiso solo de Text to Speech (y lectura de
  voces). Solo la puede hacer el usuario.

## Categorías críticas (deben llegar a 100%)

### 1. Objetivo — `100%`
- Problema que resuelve: la voz actual (la del navegador) no convence; el usuario quiere una voz de
  asistente "estilo Jarvis", más natural y con más presencia, y poder cambiar de motor de voz.
- Por qué ahora: el usuario lo necesita **esta semana** (antes del domingo 2026-10-04).
- Quién se beneficia: el usuario y **cualquier instalación de Vision** (todo configurable, nada
  personal en el código).
- Qué significa éxito (medible): todo lo que habla Vision usa el motor elegido; con un motor en la
  nube empieza a hablar en menos de 2 s; si un motor falla, **nunca queda mudo** (salta al siguiente
  y, en última instancia, a la voz del navegador).

### 2. Inputs — `100%`
- Fuentes de datos: **todo lo que hoy habla Vision** (confirmado): lectura de respuestas, botón ▶ de
  un mensaje (y su bucle), narración de progreso y opiniones del Modo Cerebro / modo reunión. Para la
  clonación local, además, una **muestra de voz** que el usuario sube en Configuración > Audio.
- Formatos: texto plano (el que hoy recibe `speak()`); muestra de voz en WAV o MP3, de 10 s como
  mínimo (REQ-007, confirmado).
- Volumen/frecuencia esperada: **siempre prendida**: lee casi todas las respuestas.
- Origen (manual/automático, quién lo produce): automático, lo genera Vision en cada uno de esos
  puntos; la muestra de voz la sube el usuario una vez.

### 3. Outputs — `100%`
- Qué se produce: el audio hablado de todo lo anterior, con el motor de voz elegido.
- Formato/destino: audio reproducido en el navegador donde está abierto Vision (confirmado con los REQ).
- Quién lo consume: el usuario de cada instalación.

### 4. Conexiones / Bases de datos — `100%`
| Sistema | Read/Write | Credenciales disponibles | Notas |
|---|---|---|---|
| Voz del navegador (Web Speech API) | local | no necesita | Lo que hay hoy; último eslabón del salto automático |
| Fish Audio (API de texto a voz, en la nube) | envía texto, recibe audio | ✅ `FISH_API_KEY` (verificada 2026-09-27: la voz responde `trained`, idioma `es`) | Voz de la biblioteca `3f45a7fd7a614655a61eb7027b955783` ("voz de locutor k"); modelos `s2.1-pro` y `s2.1-pro-free` |
| ElevenLabs (API de texto a voz, en la nube) | envía texto, recibe audio | ✅ `ELEVENLABS_API_KEY` (verificada 2026-09-27: lista 24 voces) | Voz: lista de voces de la cuenta o ID a mano; streaming (`/v1/text-to-speech/{voice_id}/stream`) |
| Clonación local: Chatterbox Multilingual V3 (MIT, `pip install chatterbox-tts`) | local | no necesita | Se instala con un botón **en el entorno de Vision** (varios GB: `torch==2.6.0`, `transformers`, `gradio` fijos). Exige `numpy<2` en Python < 3.13: choca con Vision (`numpy>=2.5`) en Python 3.12; en Python 3.13 (esta PC: 3.13.5) no hay choque |

### 5. Criterios de aceptación — `100%` (confirmados por el usuario el 2026-09-27)

- **REQ-001 Motores intercambiables**: un único punto de entrada de voz con motores enchufables
  (navegador, Fish Audio, ElevenLabs, clonación local) y lugar para sumar otros.
  - Given: en Configuración > Audio está elegido el motor X.
  - When: Vision habla en cualquiera de sus cuatro puntos (respuesta, ▶, narración, Modo Cerebro).
  - Then: se escucha con el motor X.
- **REQ-002 Apagado por defecto y activación consciente**:
  - Given: una instalación nueva.
  - When: Vision habla.
  - Then: usa la voz del navegador. Al elegir un motor en la nube, Configuración explica una vez que
    el texto leído se envía a ese proveedor y pide confirmarlo; sin confirmar, no se activa.
- **REQ-003 Salto automático sin quedar mudo**:
  - Given: la lista de prioridad "Fish pago → Fish gratis → navegador" y Fish pago responde "sin
    crédito" o no responde.
  - When: Vision habla.
  - Then: la frase sale con el siguiente motor de la lista, se avisa una sola vez (qué pasó + qué
    hacer) y el detalle técnico va al log. Si fallan todos, habla con la voz del navegador.
- **REQ-004 Latencia**:
  - Given: un motor en la nube y una respuesta de varios párrafos.
  - When: aparece la respuesta.
  - Then: el audio empieza en menos de 2 s (se genera y se reproduce frase por frase). La clonación
    local queda exenta; al elegirla, Configuración avisa que en esa PC puede tardar.
- **REQ-005 Fish Audio**:
  - Given: `FISH_API_KEY` cargada y el ID de voz `3f45a7fd7a614655a61eb7027b955783` en Configuración.
  - When: Vision habla con Fish Audio.
  - Then: se escucha "voz de locutor k", con el modelo elegido (`s2.1-pro` o `s2.1-pro-free`).
- **REQ-006 ElevenLabs**:
  - Given: la clave de ElevenLabs cargada y una voz elegida.
  - When: Vision habla con ElevenLabs.
  - Then: se escucha esa voz, con el modelo elegido. La voz se elige de la lista de voces de la
    cuenta o se pega su ID a mano (lo mismo vale para Fish Audio).
- **REQ-007 Clonación local**:
  - Given: Configuración > Audio sin la clonación local instalada.
  - When: el usuario toca "Instalar clonación local", sube su muestra de voz y elige ese motor.
  - Then: se instala Chatterbox Multilingual (con progreso visible), la muestra se guarda en la
    carpeta de datos (nunca en el repo) y Vision habla con la voz clonada. La muestra se acepta en
    WAV o MP3, de 10 s como mínimo (se recomienda 1 a 2 min); si es más corta, se explica por qué no
    se acepta.
- **REQ-008 Efecto "IA"**:
  - Given: el interruptor "Efecto IA" encendido y un motor que devuelve audio.
  - When: Vision habla.
  - Then: se escucha con eco corto y ecualización suave. De fábrica viene apagado.
- **REQ-009 Credenciales y errores amables**:
  - Given: un motor en la nube sin clave.
  - When: se lo elige o se intenta hablar con él.
  - Then: un mensaje simple dice qué falta y dónde cargarlo; nunca se manda un pedido a un servidor
    no configurado. Los tests usan motores falsos y nunca salen a internet.
- **REQ-010 Los controles de siempre siguen andando**:
  - Given: Vision hablando con un motor que devuelve audio.
  - When: el usuario toca ⏹, silencia la voz o está en Modo Cerebro.
  - Then: ⏹ y silenciar cortan al instante, y el Modo Cerebro no se escucha a sí mismo (hoy eso
    depende de `speechSynthesis.speaking`, que con audio propio hay que reemplazar).

## Categorías secundarias (deben llegar a ≥90%)

### 6. Reglas de negocio — `100%`
- **Respaldo**: si el motor elegido no responde (sin clave, sin internet, sin crédito), Vision sigue
  hablando y muestra un aviso amable **una sola vez** (qué pasó + qué hacer).
- **Salto automático de motores**: lista de prioridad en Configuración (ej. Fish pago → Fish gratis
  → navegador), igual que el salto automático de modelos del chat. El navegador siempre es el último
  eslabón.
- **Efecto "IA" estilo Jarvis** (eco corto + ecualización suave): interruptor en Configuración >
  Audio, apagado de fábrica. Solo aplica a los motores que devuelven audio (no al navegador).
- **Clonación local exenta del límite de 2 s**, con aviso al elegirla.
- Muestra de voz: WAV o MP3, 10 s como mínimo (se recomienda 1 a 2 min); si no cumple, se explica
  por qué.

### 7. Seguridad y permisos — `100%`
- **Apagado por defecto**: una instalación nueva habla con la voz del navegador. Un motor en la nube
  solo se usa si se activa a conciencia desde Configuración, porque el texto leído sale a otra
  empresa.
- La muestra de voz es un dato personal: vive en la carpeta de datos de Vision y nunca se versiona.
- Quién puede usarlo: el usuario de cada instalación; todo configurable, nada personal en el código.
- Claves: en Configuración > Credenciales (cifradas) o en el `.env`, igual que el dictado. **Depende
  del pendiente 2 de `vision-generico`** (hoy el cifrado usa una clave que solo existe en la PC
  original): se resuelve antes de esta spec.

### 8. Performance y volumen — `100%`
- **Latencia**: menos de 2 s para empezar a hablar con motores en la nube (frase por frase). La
  clonación local queda exenta (en esta PC, sin placa NVIDIA, tarda varios segundos por frase).
- **Uso**: siempre prendida, lee casi todas las respuestas → consumo alto de crédito en los motores
  pagos; de ahí el salto automático a los modelos gratis.

### 9. UI/UX y plataforma destino — `100%`
- Plataforma: Vision, **Configuración** (pantalla), no solo el `.env`.
- Expectativa: en Configuración > Audio elegir el motor, la voz y el orden del salto automático;
  interruptor del efecto "IA"; botón "Instalar clonación local" y subida de la muestra de voz.
- Referencia visual: el panel actual de Configuración > Audio y el del salto automático de modelos.

### 10. Mantenimiento y ciclo de vida — `100%`
- Responsable: el usuario (Gen).
- Ramas: el desarrollo va en una rama `dev`; `main` solo recibe cambios cuando el usuario lo pide
  ("subamos a main").
- Frecuencia de cambio esperada: **seguido** (probar motores y voces nuevas): la arquitectura
  enchufable de REQ-001 es clave; sumar un motor no debe obligar a tocar los demás.

## Contexto técnico (relevado del código y de la documentación; no suma al puntaje)

- Hoy toda la voz pasa por `speak()` de `frontend/js/tts.js` (Web Speech API del navegador, voz
  elegida en Configuración > Audio). La llaman: la lectura de respuestas, el botón ▶ de un mensaje
  (y su modo bucle), la narración de progreso y las opiniones del Modo Cerebro / modo reunión.
  `app.js` consulta `speechSynthesis.speaking` en varios lugares (⏹, Modo Cerebro): ver REQ-010.
- Patrón a imitar: el dictado usa su propio proveedor (`STT_*` en el `.env`, `get_stt_client()` en
  `app/llm/client.py`, grupo "Inteligencia artificial" en Configuración > Credenciales, error
  amable `VOZ-NO-CONFIGURADA` si falta). El salto automático de modelos vive en
  `app/llm/model_fallback.py`.
- Fish Audio: SDK de Python `fish-audio-sdk` (`from fishaudio import FishAudio`, clave en
  `FISH_API_KEY`), `client.tts.convert(text=..., reference_id=<id de voz>)`; modelo
  `s2.1-pro-free` a $0 para pruebas, desarrollo y empresas chicas (sin garantías de latencia);
  streaming en tiempo real por WebSocket.

## Dashboard de viabilidad

- **Viabilidad**: Alta para navegador + Fish Audio + ElevenLabs (claves verificadas, APIs
  documentadas y un único punto de entrada en el frontend, `speak()`). Media para la clonación
  local: varios GB de dependencias, lenta sin placa NVIDIA y con choque de `numpy` en Python 3.12
  (mitigado por la simulación previa del botón).
- **Plazo**: ajustado para diez requisitos antes del domingo 2026-10-04; el de más riesgo es
  REQ-007 (clonación local).
- **Autonomía estimada**: 90% — lo que necesita al usuario: grabar la muestra de voz para la
  clonación local, aprobar la descarga de varios GB y escuchar el resultado de cada motor.

## Hallazgos durante el desarrollo (a revisar con el usuario)

- 2026-09-27 — **REQ-004 con el modelo gratuito de Fish**: medido en esta PC, `s2.1-pro-free` tarda
  entre 1,6 s y 6,1 s por frase (Fish no garantiza la latencia del plan gratuito). ElevenLabs
  (`eleven_flash_v2_5`) tardó 0,8 s. Con el modelo gratuito de Fish como motor principal, el
  "menos de 2 s" no está garantizado.
- 2026-09-27 — **Crédito de API de Fish**: el modelo pago (`s2.1-pro`) responde 402 "Insufficient API
  credit" (el crédito de API es aparte del de la plataforma). Confirma que la clasificación
  "sin crédito" y el salto al modelo gratuito funcionan con el servicio real.

## Pendiente para la verificación final (no bloquea la aprobación)

- Muestra de voz del usuario (WAV o MP3, 1 a 2 min) para probar REQ-007 de verdad.
- Escucha del usuario de cada motor (la calidad de una voz no se verifica con tests).

## Skills sugeridas

- Skill de revisión de código seguro: la feature maneja claves de dos servicios nuevos y manda texto a
  terceros; conviene revisar el manejo de secretos, los endpoints nuevos y la subida de la muestra
  de voz antes de cerrar.
- Skill de revisión de planes de implementación: con el plazo ajustado, revisar el diseño de los motores
  enchufables antes de programar evita rehacerlo a mitad de semana.

## Decisiones registradas

- 2026-09-27 — **Fish Audio en vez de solo un clonador local** (decisión explícita del usuario): en
  esta PC (sin placa NVIDIA) un modelo local tarda varios segundos por frase; Fish Audio responde
  rápido y no suma gigas de dependencias.
- 2026-09-27 — **Voz**: `3f45a7fd7a614655a61eb7027b955783` ("voz de locutor k", biblioteca pública
  de Fish Audio: masculina, madura, narración) — elegida por el usuario.
- 2026-09-27 — **Alcance**: todo lo que hoy habla Vision usa el motor elegido (una sola voz).
- 2026-09-27 — **Motores intercambiables**: navegador, Fish Audio, ElevenLabs, clonación local con
  Python, y los que vengan. Se elige en Configuración.
- 2026-09-27 — **Apagado por defecto** y activación consciente desde Configuración.
- 2026-09-27 — **Respaldo**: nunca mudo; voz del navegador como último eslabón + un aviso.
- 2026-09-27 — **Los cuatro motores en esta versión** (decisión explícita del usuario, advertido del
  peso y la lentitud de la clonación local en esta PC).
- 2026-09-27 — **Salto automático** entre motores/modelos al quedarse sin crédito o fallar.
- 2026-09-27 — **Menos de 2 s** para empezar a hablar con motores en la nube; la local queda exenta
  con aviso.
- 2026-09-27 — **Efecto "IA"** con interruptor, apagado de fábrica.
- 2026-09-27 — **Clonación local con Chatterbox Multilingual** (MIT), instalada aparte con un botón.
- 2026-09-27 — **Uso**: siempre prendida.
- 2026-09-27 — **Para el usuario y cualquier instalación de Vision**.
- 2026-09-27 — **Requisitos REQ-001 a REQ-010 confirmados** por el usuario.
- 2026-09-27 — **Claves en Credenciales (cifradas) + `.env`**; antes se resuelve el pendiente 2 de
  `vision-generico` (clave de cifrado propia de cada PC).
- 2026-09-27 — **Voces**: lista de la cuenta o ID a mano, en ElevenLabs y en Fish Audio.
- 2026-09-27 — **Ramas**: `dev` para desarrollar, `main` solo al promover.
- 2026-09-27 — **Frecuencia de cambio**: seguido.
- 2026-09-27 — **No se aprueba sin las claves** de Fish Audio y ElevenLabs cargadas en `backend\.env`
  (decisión explícita del usuario).
- 2026-09-27 — **Clonación local en el entorno de Vision** (decisión explícita del usuario, en vez
  de un entorno aparte). Hallazgo: `chatterbox-tts` 0.1.7 pide `numpy<2` en Python < 3.13, y Vision
  pide `numpy>=2.5`; en Python 3.12 la instalación rompería Vision. Mitigación: el botón simula la instalación antes (`pip install --dry-run`) y, si fuera a bajar o
  reemplazar paquetes de Vision, no instala nada y explica por qué. **Confirmada por el usuario.**
- 2026-09-27 — **Fecha**: esta semana (antes del domingo 2026-10-04).
- 2026-09-27 — **Claves cargadas y verificadas** con una consulta de solo lectura a cada servicio.
- 2026-09-27 — **Spec aprobada** por el usuario ("approved").
