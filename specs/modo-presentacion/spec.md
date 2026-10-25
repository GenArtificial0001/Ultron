---
feature: modo-presentacion
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

# Spec: Modo presentación estilo Jarvis (doble aplauso)

> Esta spec es la única fuente de verdad para esta feature. No se escribe código hasta que el estado sea `APPROVED`.

## Pedido original (usuario, 2026-09-27)

"También me gustaría una versión Jarvis modo presentación, para que cuando aplauda dos veces seguidas
fuerte (frecuencia y volumen detectado según patrón de aplauso) y el modo presentación esté activo,
genere una presentación completa del sistema al estilo Jarvis."

## Notas para la entrevista

- Detección posible en el navegador con Web Audio (`AnalyserNode`) sobre el micrófono, 100 % local: un
  aplauso es un transitorio muy corto y fuerte, de banda ancha (energía alta entre ~1 y 5 kHz); el
  patrón son dos transitorios separados por ~0,15–0,7 s y después silencio. Hay que medir falsos
  positivos (golpes en el escritorio, portazos, tos) y dar una sensibilidad configurable.
- El micrófono queda escuchando mientras el modo esté activo (como el Modo Cerebro): hay que decidir
  si convive con el Modo Cerebro y con la interrupción por voz (spec `interrupcion-por-voz`).
- A definir: qué es "presentación completa del sistema" (¿hablada, visual tipo HUD, o las dos?), qué
  datos muestra (hora, estado de conectores, modelo, voz, alarmas, tareas...) y el tono (spec
  `trato-asistente`: de usted, "señor/señora" según lo que elija cada uno).
