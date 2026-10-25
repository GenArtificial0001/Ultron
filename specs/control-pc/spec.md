---
feature: control-pc
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

# Spec: Control de la PC

> Esta spec es la única fuente de verdad para esta feature. No se escribe código hasta que el estado sea `APPROVED`.

## Pedido original (usuario, 2026-09-27)

"Debemos agregar una librería de Python que maneje toda mi PC como si lo haría yo mismo."

La entrevista todavía no empezó: queda en `NEW` hasta terminar la spec `voz-fish-audio`, salvo que el
usuario pida otra cosa.

## Notas para la entrevista

- Vision ya puede hacer mucho con la tool `bash` (PowerShell: abrir programas y archivos, programar
  tareas) y con Playwright (navegador). Lo nuevo sería ver la pantalla y usar mouse y teclado en
  cualquier programa de escritorio.
- Candidatas a evaluar: `pywinauto` (Windows UI Automation: maneja ventanas y controles por nombre,
  más robusto) y `pyautogui` (mouse, teclado y capturas de pantalla), más un modelo con visión que
  mire las capturas para decidir el próximo paso.
- Seguridad a definir sí o sí: confirmación antes de acciones peligrosas, un botón o gesto para
  frenarlo al instante, nunca escribir contraseñas ni datos de pago, y registro de cada acción.
