---
feature: ia-local
status: DONE           # NEW | IN_PROGRESS | PENDING_APPROVAL | APPROVED | IN_DEVELOPMENT | DONE
created: 2026-10-25
last_updated: 2026-10-25
approved_on: 2026-10-25   # aprobada junto con portar-desde-origen; se construye despues de la base 2.0
scores:
  objetivo: 100
  inputs: 95
  outputs: 95
  conexiones: 90
  criterios_aceptacion: 90
  reglas_negocio: 90
  seguridad: 95
  performance: 80
  ui_ux: 85
  mantenimiento: 95
viabilidad: Alta
autonomia_estimada: 85% -- la prueba real necesita Ollama o LM Studio con un modelo descargado (PC personal del usuario)
---

# Spec: IA local (Ollama y LM Studio) como proveedores de Vision

> Esta spec es la única fuente de verdad para esta feature. No se escribe código hasta que el estado sea `APPROVED`.

## Pedido original (usuario, 2026-10-25)

"Las conexiones por ahora son con mis proveedores personales e IA locales descargadas." Se suma a la spec
`varios-proveedores` (Claude, Gemini, DeepSeek, Groq, OpenRouter, OpenAI y otro compatible), que ya está
hecha: acá solo se agregan los motores que corren en la propia PC.

## 🔴 Acciones Requeridas (bloqueos abiertos)

- [ ] **Aprobar esta spec** (escribir «APPROVED» en el chat, con o sin cambios).
- [ ] Para la prueba real: tener Ollama (https://ollama.com) o LM Studio (https://lmstudio.ai) abierto en la PC
  personal con al menos un modelo descargado. En la PC donde se escribió la spec no hay ninguno (los puertos
  11434 y 1234 no responden), así que el código se prueba con un servidor local falso.

## 1. Objetivo — `100%`
- Problema: hoy Vision necesita una clave y una conexión a internet para chatear. Un modelo descargado en la
  PC no tiene costo por uso, no tiene límites de cupo y lo que se le manda no sale de la máquina.
- Éxito: con Ollama o LM Studio abiertos, sus modelos aparecen solos en el selector, en un grupo propio, y se
  pueden usar para chatear, con herramientas cuando el modelo las admite, y dentro de la lista del salto automático.

## 2. Inputs — `95%`
- Nada obligatorio: Vision busca los dos motores en sus direcciones de fábrica.
  - Ollama: `http://127.0.0.1:11434/v1`.
  - LM Studio: `http://127.0.0.1:1234/v1`.
- Opcional en el `.env`:
  - `OLLAMA_BASE_URL` / `LMSTUDIO_BASE_URL`: otra dirección o puerto, por ejemplo otra PC de la casa.
  - `LOCAL_AI=off`: no buscar motores locales.
- Los motores locales no usan clave. Se manda una de relleno (`local`), porque la librería de OpenAI exige una.

## 3. Outputs — `95%`
- Grupos «Ollama (local)» y «LM Studio (local)» en el selector, con la marca «local».
- Los ids siguen el formato de `varios-proveedores`: `ollama/qwen3:8b`, `lmstudio/qwen2.5-7b-instruct`.

## 4. Conexiones — `90%`
| Motor | Dirección de fábrica (compatible con OpenAI) | Lista de modelos | Clave |
|---|---|---|---|
| Ollama | `http://127.0.0.1:11434/v1` | `GET /v1/models` | ninguna |
| LM Studio | `http://127.0.0.1:1234/v1` | `GET /v1/models` | ninguna |
- `localhost` y `127.0.0.1` ya van siempre directos, sin proxy (`config._LOCAL_HOSTS`).
- Con otra PC en `OLLAMA_BASE_URL`, esa dirección se suma a `NO_PROXY` mientras esté configurada.

## 5. Criterios de aceptación — `90%`
- **REQ-001 Detección sola, sin configurar nada**:
  - Given: Ollama abierto con `qwen3:8b` descargado y nada en el `.env`.
  - When: abro el selector de modelos.
  - Then: veo el grupo «Ollama (local)» con `qwen3:8b`. Si cierro Ollama, el grupo desaparece en la próxima
    actualización de la lista, sin errores en el chat.
- **REQ-002 La búsqueda no frena a Vision**: cada motor se consulta con un tiempo máximo de 1,5 s. Un motor
  apagado no demora la lista de los proveedores en la nube ni el arranque.
- **REQ-003 Chat y herramientas**: un modelo local recibe las mismas herramientas que cualquier otro. Si el motor
  rechaza las herramientas («does not support tools»), el aviso dice «<modelo> no admite herramientas: elegí otro
  modelo o usalo solo para conversar» (mismo criterio que el aviso `MODELO-SIN-HERRAMIENTAS` de la versión de origen).
  Reenviar no se ofrece como solución.
- **REQ-004 En la lista del salto**: los modelos locales se pueden sumar a la lista del salto automático como
  cualquier otro. Si el motor está apagado o no responde, eso es una «caída»: se pausa ese modelo y se sigue con el
  siguiente de la lista.
- **REQ-005 Sin aviso de privacidad**: a diferencia de los gratuitos en la nube (REQ-006 de `varios-proveedores`),
  los locales no piden confirmación: lo que se manda no sale de la PC. Sí se avisa si la dirección configurada NO es
  de la propia PC (otra máquina de la red): ahí sí sale de la PC.
- **REQ-006 Primera respuesta lenta, sin cortes falsos**: el primer pedido a un modelo local puede tardar mientras el
  motor lo carga en memoria. El tiempo de espera de la primera respuesta es de 180 s para los locales (para los de la
  nube no cambia), y mientras tanto el chat muestra «cargando el modelo local…».
- **REQ-007 Nada se rompe**: sin motores locales, Vision se comporta exactamente como 1.1.0. Los tests de
  `varios-proveedores` siguen en verde sin tocarlos.
- **REQ-009 «Conexión a LLMs» en el README, como parte central de la instalación** (pedido del usuario, 2026-10-25:
  «sin eso no funciona, es una de las partes más importantes»). Sección propia, cerca del principio, con:
  - qué paquete instalar: Ollama o LM Studio, con el enlace oficial, el tamaño y si pide permisos de administrador;
  - cómo instalarlo paso a paso;
  - qué modelo bajar según la PC (RAM, si hay placa de video o no), con el comando exacto (`ollama pull <modelo>`) y
    cuáles admiten herramientas;
  - cómo comprobar que responde (`http://127.0.0.1:11434/v1/models`);
  - qué ve el usuario en Vision cuando funciona y qué hacer ante los errores comunes: motor cerrado, modelo sin
    herramientas, primera respuesta lenta, proxy;
  - en la misma sección, los proveedores con clave (`varios-proveedores`): dónde se saca cada clave y qué línea va en el `.env`.
  Los pasos y los tiempos se escriben con lo medido en una instalación real, no de memoria.
- **REQ-008 Tests sin servicios reales**: un servidor local falso compatible con OpenAI (lista de modelos, streaming,
  tool-calling y un modelo que rechaza herramientas). Ningún test llama a un Ollama o LM Studio real.

## 6. Reglas de negocio — `90%`
- Los locales nunca cuentan como «gratuitos con entrenamiento» (no piden la confirmación de REQ-006 de `varios-proveedores`).
- Un motor apagado no es un error para el usuario: simplemente no aparece.
- El salto trata «motor apagado» como caída del modelo: se pausa ese modelo, no el motor entero. Así, si después se
  abre el motor, vuelve solo cuando termina la pausa.

## 7. Seguridad — `95%`
- Sin claves nuevas.
- Las direcciones del `.env` se validan: solo `http(s)://`, y nunca se muestran con credenciales embebidas.
- Ninguna dirección se arma con texto que venga de la pantalla: solo del `.env`.
- REQ-005: aviso cuando el motor configurado está en otra máquina.

## 8. Performance — `80%`
- La detección usa la misma caché de 5 minutos de la lista de modelos (`MODELS_TTL_SECONDS`). Un motor que no
  responde se vuelve a buscar recién en 30 s, para no sumar 1,5 s a cada apertura del selector.
- Queda abierto: medir en la PC personal cuánto tarda la carga del primer modelo, para ajustar REQ-006.

## 9. UI/UX — `85%`
- Selector: grupos locales debajo de los de la nube, con la marca «local» (en lugar de «gratis»).
- Configuración > Modelo e inteligencia: los locales aparecen como un proveedor más, para elegir qué modelos mostrar.
  Selección sugerida: todos los de chat, hasta 6, sin los de embeddings.
- Sin pantalla nueva: la dirección, si hace falta cambiarla, va en el `.env`, como las claves (decisión de `varios-proveedores`).

## 10. Mantenimiento — `95%`
- Dos filas nuevas en el registro de proveedores (`app/llm/providers.py::KNOWN`), con un campo `local=True` y una
  dirección de fábrica que el `.env` puede cambiar.
- Lo mantiene el usuario (Gen), en la rama `dev` de Vision-Personal.

## Medido en una instalación real (2026-10-25, PC de pruebas)
- PC: Intel Core Ultra 5 238V (8 hilos, gráfica integrada Arc), 31,5 GB de RAM, sin placa de video dedicada.
- Ollama 0.40.11:
  - `OllamaSetup.exe` pesa 1,66 GB; se bajó en 86 s y su SHA-256 coincide con el de la release de GitHub;
  - firma digital válida de «Ollama Inc.»;
  - instalación silenciosa (`/VERYSILENT`) por usuario, sin permisos de administrador, en `%LOCALAPPDATA%\Programs\Ollama`;
  - queda abierto en la bandeja y arranca con Windows.
- `ollama pull qwen3:4b`: 2,5 GB en 87 s.
- Capa compatible con OpenAI (`/v1`), con la librería `openai` de Vision:
  - lista de modelos: 0,24 s;
  - chat y tool-calling: correctos (`read` con `{"path": "README.md"}`);
  - streaming con herramientas: correcto.
- Tiempos con el procesador ocupado al 80 % por otra tarea, solo CPU:
  | Prueba | Tiempo |
  |---|---|
  | Primera respuesta en frío | 42 s |
  | Respuesta trivial en caliente | 34 s |
  | Con una herramienta | 127 s |
  | Primer token del streaming | 155 s |
  Hay que volver a medir con la PC libre.
- **Ollama arranca con contexto de 4.096 tokens** (`ollama ps`, columna CONTEXT). El prompt de sistema de Vision con
  sus herramientas lo supera, y Ollama recorta el principio sin avisar. Esto suma dos requisitos:
  - **REQ-010 Contexto suficiente**: el README explica cómo subirlo (`OLLAMA_CONTEXT_LENGTH`, variable de entorno de
    Windows, y reiniciar Ollama).
  - **REQ-011 Aviso de contexto corto**: Vision avisa en el chat si el contexto del modelo local (cuando el motor lo
    informa) es menor que lo que va a mandar, en lugar de dejar que el motor recorte en silencio.
- Queda por medir: los tiempos con la PC libre, la aceleración con la gráfica integrada y un turno real de Vision con el
  prompt completo.

## Decisiones registradas (recomendadas, a confirmar con la aprobación)

- 2026-10-25 — **Ollama y LM Studio**, los dos más usados y los dos compatibles con OpenAI. Cualquier otro motor local
  (Jan, llama.cpp server, vLLM) entra por «otro compatible» (`CUSTOM_BASE_URL`), que ya existe.
- 2026-10-25 — **Detección automática** en las direcciones de fábrica, sin configurar nada.
- 2026-10-25 — **Sin aviso de privacidad** para los locales en la PC; con aviso si están en otra máquina.

## Fuera de alcance (posibles specs futuras)
- Dictado por voz local (Whisper en la PC) y voz local: hoy el dictado usa `STT_*` (Groq u OpenAI).
- Descargar o administrar modelos desde Vision: se hace en Ollama o LM Studio.
- Portar las mejoras de la versión de origen posteriores a 0.19.3: se planifica aparte.
