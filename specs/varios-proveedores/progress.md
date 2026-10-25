# Progress: Varios proveedores de IA a la vez

> Bitácora de implementación. Cada tarea se traza a un REQ-ID de `spec.md`.

## 2026-09-27 -- desarrollo (Claude Code, rama `dev`)

- REQ-001/002: `backend/app/llm/providers.py`: registro de proveedores (Claude, Gemini, DeepSeek, Groq,
  OpenRouter, OpenAI, otro compatible) con su dirección y los nombres de clave aceptados (Groq también
  `META_GROQ_API_KEY`). El `.env` se relee cuando cambia su fecha: agregar una clave suma el proveedor sin
  reiniciar.
- REQ-004: modelos como `proveedor/modelo`; `get_client(model=...)` da el cliente de su proveedor y
  `api_model()` el nombre que entiende. El loop pide el cliente de CADA paso; la compactación, el del modelo
  que usa. Selector agrupado por proveedor, con "gratis"; nombres legibles en el chip y los avisos
  (`display_name`, `prettyModel`).
- REQ-003/006: `/api/providers` (GET/POST) y sección "Proveedores de IA" en Configuración > Modelo e
  inteligencia: modelos a mostrar por proveedor (sugeridos: Gemini los Flash nuevos, Groq gpt-oss/Qwen,
  Claude los cuatro más nuevos) y confirmación de los gratuitos (sin ella no aparecen ni se usan).
  Se esconden los modelos que no son de chat (voz, imágenes, música, embeddings...).
- REQ-005: la lista del salto arranca vacía y admite cualquier proveedor. "Sin crédito" dispara el salto y
  pausa a TODO ese proveedor. Una lista vieja sin prefijo se lee como del proveedor principal.
- REQ-007: `LLM_*` sigue siendo el proveedor principal (sus modelos se ven sin prefijo, como antes).
- REQ-008: `history_compat` ya adaptaba el historial entre modelos; `is_gemini` ahora mira el nombre del
  modelo (con o sin prefijo).
- REQ-009: `tests/test_providers.py` (16 tests, sin red) y tests existentes adaptados; `conftest` borra
  las claves nuevas del entorno.

### Hallazgos con los servicios reales (2026-09-27, solo lectura o una frase de prueba)
- Gemini: `gemini-2.5-flash` ya no está disponible para cuentas nuevas (404, sugiere 3.8); los Pro no
  tienen cupo gratuito (429 "exceeded your current quota": se clasifica como límite del modelo, no de la
  cuenta). `gemini-flash-latest` y `gemini-3.8-flash` funcionan con las 21 herramientas de Vision (~5 s).
- Groq: `openai/gpt-oss-120b` funciona con las herramientas de Vision (1,8 s). No ofrece Llama para chat.
- DeepSeek: la red de esta PC corta la conexión (TLS), no es la clave.
- Anthropic: clave válida, sin crédito.

### Verificación (2026-09-27)
- Batería rápida 826/826 (16 nuevos en `test_providers.py`); e2e completa **175/175** en Chromium real
  (incluye `test_e2e_model_fallback.py` adaptado a un Vision "clásico" con `LLM_*`); QA estática OK.

### Tablero de requisitos
| REQ | % | Evidencia |
|---|---|---|
| 001 Proveedores conocidos + otro compatible | 100 % | tests + claves reales de Gemini, Groq y Anthropic |
| 002 Solo la clave en el `.env`, sin reiniciar | 100 % | `test_changes_to_the_env_are_seen_without_restarting` |
| 003 Modelos elegidos por proveedor | 100 % | `test_the_user_chooses_which_models_to_see` + lista real (Gemini 19 de chat) |
| 004 Selector agrupado | 100 % | tests + Gemini y Groq reales con las 21 herramientas de Vision |
| 005 Salto entre proveedores | 100 % | `test_out_of_credit_pauses_the_whole_provider_and_jumps_to_another` + e2e del salto |
| 006 Aviso en los gratuitos | 100 % | `test_free_providers_are_hidden_until_the_user_confirms` |
| 007 Nada se rompe | 100 % | e2e del salto como Vision clásico (8/8) |
| 008 La conversación sigue al cambiar de proveedor | 90 % | `history_compat` + unit; falta un salto real a mitad de una tarea |
| 009 Tests sin servicios reales | 100 % | conftest borra las claves nuevas; OpenAI falso |

Pendiente para `DONE`: que el usuario confirme Gemini/Groq en Configuración, arme la lista y chatee.
