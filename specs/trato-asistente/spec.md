---
feature: trato-asistente
status: IN_PROGRESS           # NEW | IN_PROGRESS | PENDING_APPROVAL | APPROVED | IN_DEVELOPMENT | DONE
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

# Spec: Trato del asistente (estilo mayordomo tecnológico)

> Esta spec es la única fuente de verdad para esta feature. No se escribe código hasta que el estado sea `APPROVED`.

## Pedido original (usuario, 2026-09-27)

"Quiero que Vision me trate como Jarvis lo trataba a Iron Man, de señor. Si detecta nombre de hombre en
el inicio de sesión, señor; si no, señora si el nombre es de mujer. Y el trato debe ser como de un
sirviente asistente tecnológico. Jarvis debe poder acceder a cualquier función de mi PC."

La parte de "acceder a cualquier función de mi PC" vive en la spec `control-pc`.

## Notas para la entrevista

- Hoy el prompt está en `SYSTEM_PROMPT_TEMPLATE` (`backend/app/llm/loop.py`), con voseo ("Sos Vision")
  y `{company_clause}`. Vision no tiene cuentas de usuario: no hay un "nombre de inicio de sesión".
- Deducir el género por el nombre se equivoca (Andrea, Alex, Guadalupe, Noa...) y trataría mal a
  alguien; Vision es para cualquier empresa. Opción a evaluar: cada persona elige su trato en
  Configuración, con la deducción solo como sugerencia a confirmar.
- "Jarvis" es un personaje de Marvel: para el nombre visible de un Vision que se comparte, conviene
  que sea configurable y no fijo.

## Decisiones registradas

- 2026-09-27 — **Trato elegido por cada persona** en Configuración: nombre y trato (señor, señora o
  por el nombre). No se deduce del nombre.
- 2026-09-27 — **De usted, formal**: mayordomo tecnológico, cortés, preciso, con un poco de ironía
  fina y siempre servicial ("Sí, señor. Ya tengo el informe listo; ¿desea que se lo lea?").
- 2026-09-27 — **Nombre del asistente configurable** por instalación (el usuario puede ponerle Jarvis
  en la suya; en el código no queda fijo).
- 2026-09-27 — **Orden**: primero se cierra la spec `voz-fish-audio`, después esta y al final
  `control-pc`.
