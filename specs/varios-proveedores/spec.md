---
feature: varios-proveedores
status: IN_DEVELOPMENT        # NEW | IN_PROGRESS | PENDING_APPROVAL | APPROVED | IN_DEVELOPMENT | DONE
created: 2026-09-27
last_updated: 2026-09-27
approved_on: 2026-09-27   # "APPROVED" (usuario, en el chat)
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
viabilidad: Alta
autonomia_estimada: 85% -- faltan las claves de cada proveedor (solo el usuario)
---

# Spec: Varios proveedores de IA a la vez

> Esta spec es la única fuente de verdad para esta feature. No se escribe código hasta que el estado sea `APPROVED`.

## Pedido original (usuario, 2026-09-27)

"Sumemos Gemini, DeepSeek, Meta y algún modelo de IA gratuito." Contexto: la cuenta de Anthropic se quedó
sin crédito y el chat dejó de responder.

Ya estaba anotada como spec futura en `vision-generico` (pendiente 4): `providers.json` con las claves en
el `.env`, modelos "proveedor/modelo" en el selector y salto automático entre proveedores.

## 🔴 Acciones Requeridas (bloqueos abiertos)

- [ ] (ninguna para aprobar) Para la prueba real de cada proveedor hace falta su clave: Gemini y Groq ya
  existen; Anthropic está sin crédito; DeepSeek y OpenRouter, si el usuario los quiere, las crea él.

## 1. Objetivo — `100%`
- Problema: Vision habla con un solo proveedor por vez; si la cuenta se queda sin crédito (pasó con
  Anthropic el 2026-09-27) el chat deja de responder.
- Qué quiere el usuario (textual): "Tengo que tener un selector de modelos de IA; quiero cargar todos
  juntos y además quiero la funcionalidad de salto de IA con el armado de prioridad."
- Éxito: con varios proveedores cargados, el selector muestra sus modelos agrupados y, si el modelo en
  uso se queda sin crédito, llega a su límite o se cae, el chat sigue con el siguiente de la lista
  (aunque sea de otro proveedor) sin que el usuario haga nada.

## 2. Inputs — `100%`
- Claves de cada proveedor, puestas a mano en el `.env` con nombre fijo (ej. `GEMINI_API_KEY`).
- Lista de modelos de cada proveedor (`GET /models` compatible con OpenAI).
- Elección del usuario: qué modelos ver por proveedor y el orden del salto.

## 3. Outputs — `100%`
- Selector de modelos agrupado por proveedor; el chat usa el proveedor de cada modelo.
- Salto automático entre proveedores con un aviso por salto (como el salto entre modelos de hoy).

## 4. Conexiones — `100%`
| Proveedor | Dirección (compatible con OpenAI) | Clave en el `.env` | Gratis |
|---|---|---|---|
| Claude (Anthropic) | `https://api.anthropic.com/v1/` | `ANTHROPIC_API_KEY` | no |
| Gemini (Google) | `https://generativelanguage.googleapis.com/v1beta/openai/` | `GEMINI_API_KEY` | sí, con límites |
| DeepSeek | `https://api.deepseek.com` | `DEEPSEEK_API_KEY` | no (muy barato) |
| Groq (modelos abiertos: gpt-oss, Qwen) | `https://api.groq.com/openai/v1` | `GROQ_API_KEY` (también se acepta `META_GROQ_API_KEY`) | sí, con límites |
| OpenRouter (muchos) | `https://openrouter.ai/api/v1` | `OPENROUTER_API_KEY` | algunos modelos |
| OpenAI | `https://api.openai.com/v1` | `OPENAI_API_KEY` | no |
| Otro compatible | la que cargue el usuario | `CUSTOM_API_KEY` | según el servicio |

## 5. Criterios de aceptación — `100%` (a confirmar por el usuario con la aprobación)

- **REQ-001 Proveedores conocidos + otro compatible**: los de la tabla, listos para usar con solo la clave.
- **REQ-002 Solo la clave en el `.env`, sin direcciones ni parámetros** (cambiado a pedido del usuario:
  "es incomodísima la carga de URL y demás parámetros; se debe adaptar a cada proveedor"):
  - Given: agrego `GEMINI_API_KEY=...` al `.env` y guardo el archivo, con Vision abierto.
  - When: abro el selector de modelos.
  - Then: aparecen los modelos de Gemini sin reiniciar Vision. Vision ya conoce la dirección y el formato
    de cada proveedor de la tabla; el usuario solo pone la clave. Borrar la línea saca al proveedor.
- **REQ-003 Modelos elegidos por proveedor**:
  - Given: un proveedor con clave.
  - When: abro su sección.
  - Then: en Configuración > Modelo e inteligencia veo la lista de sus modelos con una selección sugerida y
    marco cuáles aparecen en el selector.
- **REQ-004 Selector agrupado**:
  - Given: Claude, Gemini y Groq cargados.
  - When: abro el selector de modelos.
  - Then: veo un grupo por proveedor, con la marca "gratis" donde corresponde, y "Automático" arriba si el
    salto está activo; al elegir un modelo, el chat le habla a su proveedor.
- **REQ-005 Salto entre proveedores**:
  - Given: la lista "Claude Opus → Gemini 2.5 Flash → Llama en Groq" (vacía de fábrica; la arma el usuario).
  - When: Claude responde "sin crédito", llega a su límite o se cae.
  - Then: el turno sigue con Gemini en el mismo mensaje, con un aviso. Sin crédito pausa **todos** los
    modelos de ese proveedor; límite o caída, solo ese modelo. Pasada la pausa, vuelve a probar.
- **REQ-006 Aviso en los gratuitos**:
  - Given: un proveedor o modelo gratuito.
  - When: lo activo por primera vez.
  - Then: Vision explica que puede usar lo que se le manda para entrenar y pide confirmar; sin confirmar
    no se usa (ni a mano ni en el salto).
- **REQ-007 Nada se rompe**: una instalación con `LLM_PROVIDER`/`LLM_API_KEY` sigue funcionando igual;
  ese proveedor queda como el "principal".
- **REQ-008 La conversación sigue al cambiar de proveedor**: el historial se adapta al proveedor nuevo
  (mensajes de herramientas e imágenes) y el turno no se corta.
- **REQ-009 Tests sin servicios reales**: proveedores falsos; la batería borra las claves nuevas del
  entorno antes de correr.

## 6. Reglas de negocio — `100%`
- Salto por sin crédito, límite o caída; sin crédito pausa el proveedor entero.
- La lista del salto arranca vacía.
- Los gratuitos requieren confirmación antes de usarse.

## 7. Seguridad — `100%`
- **Claves en el `.env`, en texto plano** (decisión explícita del usuario): el `.env` nunca se versiona y
  las claves nunca vuelven al navegador ni aparecen en la pantalla.
- Aviso de privacidad y confirmación para los proveedores gratuitos (REQ-006).

## 8. Performance — `100%`
- La lista de modelos de cada proveedor se guarda unos minutos y se renueva al cambiar una clave.

## 9. UI/UX — `100%`
- En la pantalla solo se eligen los modelos a mostrar de cada proveedor y la lista del salto; las claves
  van en el `.env` (el README y el `.env.example` explican dónde conseguir cada una).
- Configuración > Modelo e inteligencia: la lista del salto admite modelos de cualquier proveedor.
- Selector del chat agrupado por proveedor.

## 10. Mantenimiento — `100%`
- Lo mantiene el usuario (Gen), en la rama `dev`. Sumar un proveedor conocido es agregar una fila al
  registro de proveedores.

## Decisiones registradas

- 2026-09-27 — **Claves solo en el `.env`, sin direcciones ni parámetros**: Vision conoce la dirección de
  cada proveedor y detecta los cambios del `.env` sin reiniciar (el usuario descartó cargarlas desde la
  pantalla por incómodo).
- 2026-09-27 — **Selector agrupado por proveedor**.
- 2026-09-27 — **Salto por sin crédito, límite y caída**.
- 2026-09-27 — **Modelos elegidos por proveedor** (con selección sugerida).
- 2026-09-27 — **Lista del salto vacía de fábrica**.
- 2026-09-27 — **Aviso y confirmación** para los proveedores gratuitos.

## Notas para la entrevista

- Hoy Vision usa UN proveedor por vez (`LLM_PROVIDER`, `LLM_BASE_URL`, `LLM_API_KEY`). Mientras tanto se
  puede cambiar a Gemini desde Configuración > Credenciales (capa compatible con OpenAI de Google).
- Opciones compatibles con OpenAI (sin SDK nuevo):
  - **Gemini** (Google AI Studio, plan gratuito con límites): `https://generativelanguage.googleapis.com/v1beta/openai/`.
    En el plan gratuito Google puede usar lo que se le manda para mejorar sus productos: no apto para
    datos de la empresa.
  - **Meta (Llama) y otros abiertos, gratis con límites en Groq** (el usuario ya tiene clave, la del
    dictado): `https://api.groq.com/openai/v1`.
  - **DeepSeek**: API propia, paga pero muy barata: `https://api.deepseek.com`.
  - **OpenRouter**: una sola clave para muchos modelos (Gemini, DeepSeek, Llama, Claude...), algunos
    gratuitos con límites: `https://openrouter.ai/api/v1`.
- A definir: lista de proveedores y modelos, cómo se nombran en el selector, salto automático entre
  proveedores (ej. Claude → Gemini gratis cuando se acaba el crédito, como las voces), qué avisos de
  privacidad mostrar por proveedor gratuito, y cómo convive con `model_fallback.py` (que ya trata distinto
  a Gemini a mitad de una tarea).
- 2026-09-27 — **Nombres de clave flexibles**: Groq acepta `GROQ_API_KEY` o `META_GROQ_API_KEY` (el usuario la cargó con ese nombre).
- 2026-09-27 — **Spec aprobada** por el usuario ("APPROVED").
