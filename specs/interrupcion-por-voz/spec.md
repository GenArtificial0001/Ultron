---
feature: interrupcion-por-voz
status: NEW               # NEW | IN_PROGRESS | PENDING_APPROVAL | APPROVED | IN_DEVELOPMENT | DONE
created: 2026-09-27
last_updated: 2026-09-27
approved_on: null
scores:
  objetivo: 0
  inputs: 0
  outputs: 0
  conexiones: 0
  criterios_aceptacion: 0
  reglas_negocio: 0
  seguridad: 0
  performance: 0
  ui_ux: 0
  mantenimiento: 0
viabilidad: null
autonomia_estimada: null
---

# Spec: Interrupción por voz (hablarle encima a Vision)

> Esta spec es la única fuente de verdad para esta feature. No se escribe código hasta que el estado sea `APPROVED`.

## Pedido original (usuario, 2026-09-27)

"¿Podemos hacer que pueda interrumpirse lo que habla, como lo hace hoy Gemini, ChatGPT y Claude?"

## Notas para la entrevista

- Hoy se interrumpe con ⏹, con la barra espaciadora en el Modo Cerebro (spec atajo-espacio-modo-cerebro)
  y diciendo "Vision" en el modo reunión. No se puede hablándole encima: mientras Vision habla, el
  detector de voz del Modo Cerebro se pausa (más ~900 ms) para no escucharse a sí mismo (`vadTick`,
  `BRAIN_TTS_COOLDOWN_MS` en `frontend/js/app.js`), porque la voz del navegador suena fuera de la
  página y la cancelación de eco no la ve.
- Con los motores de la spec `voz-fish-audio` (Fish Audio, ElevenLabs, clonación local), el audio sale
  por Web Audio dentro de la página: la cancelación de eco del navegador (`echoCancellation`) puede
  separarlo del micrófono. Eso habilita escuchar mientras Vision habla y cortarla si se detecta voz.
- A medir con una prueba real: parlantes vs auriculares, voz del navegador vs motores, falsos cortes
  (ruido, tos) y qué pasa con lo que el usuario dijo al interrumpir (¿se envía como mensaje nuevo?).
