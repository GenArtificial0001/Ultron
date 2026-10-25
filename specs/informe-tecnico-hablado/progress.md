# Progress: Informe técnico hablado

> Bitácora de implementación. Cada tarea se traza a un REQ-ID de `spec.md`.

## 2026-09-27 -- spec (Claude Code)

- Entrevista completa: todas las categorías al 100 %. Spec en `PENDING_APPROVAL`, esperando la palabra
  `APPROVED` del usuario. Todavía no hay código.

## 2026-09-27 -- desarrollo (Claude Code, rama `dev`)

- Spec `IN_DEVELOPMENT`.
- REQ-002/003/007: `backend/app/tech_report.py` (`describe_call`, `first_comment`, `redact`); el loop
  agrega `speech` al evento `progress-estimate` (`backend/app/llm/loop.py`).
- REQ-001/004/005: `tts.js` (`isTechReportEnabled`, `speak(..., { replaceKey })`), `app.js` (el progreso
  dice `speech` en vez de la etiqueta si el interruptor está prendido) e interruptor
  `cfg-tts-tech-report` en Configuración > Audio.
- REQ-006: el texto de la confirmación de las voces en la nube menciona el informe técnico.
- Verificación: `tests/test_tech_report.py` 26/26; e2e `tests/test_e2e_tech_report.py` 3/3 y
  `tests/test_e2e_voice_engines.py` 3/3 (con la aserción de REQ-006); batería rápida 799/799.

### Hitos 100 % (2026-09-27)
| REQ | % | Evidencia |
|---|---|---|
| 001 Interruptor, prendido de fábrica | 100 % | e2e `test_by_default_each_action_is_said...` y `test_turned_off_the_short_narration_comes_back` |
| 002 Una frase por acción | 100 % | `test_writing_a_file_says_what_its_comment_says`, `test_queries_name_the_system_and_the_tables` y el e2e con un comando real |
| 003 Solo las acciones | 100 % | `test_non_actions_are_silent` |
| 004 Decir lo último | 100 % | e2e `test_when_vision_goes_faster_only_the_latest_phrase_is_said` |
| 005 El chat no cambia | 100 % | e2e: la frase no aparece en `#messages` |
| 006 Consentimiento | 100 % | e2e `test_adding_a_cloud_engine_asks_for_consent` (texto de la confirmación) |
| 007 Nunca un secreto | 100 % | `test_secrets_are_hidden` (8 casos) y `test_redaction_reaches_the_spoken_phrase` |

Pendiente para cerrar (`DONE`): que el usuario lo escuche con su Vision.

## 2026-09-27 -- reapertura: informe detallado y en vivo (Claude Code, rama `dev`)

- REQ-008: `LiveNarrator` (`tech_report.py`) decodifica el JSON parcial de cada tool-call write/edit/bash
  mientras llega y narra por línea: comentarios (según el tipo de archivo; en HTML/CSS/JS un `#` no es
  comentario), título, encabezado, sidebar y su lado, tarjetas, gráficos, tablas, formularios,
  conexiones a bases y pedidos a APIs. El loop emite eventos `narration`; si una tool-call ya se narró en
  vivo, su frase de acción no se repite.
- REQ-009: con `narrate`, el turno le pide al modelo que comente cada sección en español (misma vía que
  la instrucción del Modo Cerebro).
- REQ-010: `todowrite` anuncia las tareas y sub-tareas que empiezan o terminan (`task_transitions`).
- REQ-011: `app.js` dice "Sigo con: <tarea>" si pasan ~15 s sin frases mientras dura el turno.
- REQ-004 (modificado): `speak(..., { limitKey, limit: 3 })`: en orden y con tope de 3 frases esperando.
- Verificación: `test_tech_report.py` 34/34; e2e `test_e2e_tech_report.py` 4/4 (incluye un HTML mandado
  de a pedazos y 17 s sin novedades); e2e de voz y play/stop 12/12; batería rápida 807/807; QA OK.

### Tablero (2026-09-27)
| REQ | % | Evidencia |
|---|---|---|
| 001 Interruptor | 100 % | e2e prendido de fábrica / apagado |
| 002 Una frase por acción | 100 % | unit + e2e con un comando real |
| 003 Solo las acciones | 100 % | `test_non_actions_are_silent` |
| 004 En orden, sin atrasarse | 100 % | e2e `test_the_queue_never_falls_more_than_three_phrases_behind` |
| 005 El chat no cambia | 100 % | e2e: nada en `#messages` |
| 006 Consentimiento | 100 % | e2e del texto de la confirmación |
| 007 Nunca un secreto | 100 % | 8 casos + narrador en vivo con una clave en un comando |
| 008 Recorrido en vivo | 100 % | unit (fragmentos de 7 y 3 caracteres, antes de terminar el archivo) + e2e en orden |
| 009 El modelo comenta | 100 % | e2e: la instrucción llega con el informe prendido y no llega apagado |
| 010 Avance del plan | 100 % | unit de transiciones + e2e "Empiezo…" / "Paso 1 de 2…" |
| 011 Nunca callado | 100 % | e2e: "Sigo con: Diseñar la estructura" tras 17 s sin novedades |

Pendiente para `DONE`: que el usuario lo escuche con un pedido real.
