# Progress: Voz de Vision con Fish Audio

> Bitácora de implementación. Cada tarea se traza a un REQ-ID de `spec.md`.

## 2026-09-27 -- spec creada (Claude Code)

- Spec `IN_PROGRESS`: entrevista en curso. Todavía no hay código.
- Entrevista completa (4 rondas): todas las categorías al 100 %.
- Claves de Fish Audio y ElevenLabs cargadas por el usuario en `backend\.env` y verificadas con una
  consulta de solo lectura (Fish: la voz `3f45a7fd…` responde `trained`, idioma `es`; ElevenLabs:
  24 voces). Sin generar audio ni gastar créditos.
- Spec en `PENDING_APPROVAL`: espera la palabra `APPROVED` del usuario.

## 2026-09-27 -- desarrollo (Claude Code, rama `dev`)

- Spec `IN_DEVELOPMENT`.
- Pendiente 2 de vision-generico (dependencia de REQ-009): `credentials.py` genera una clave local
  propia (`keys/credentials.key`) si no existe la deploy key; las instalaciones que ya cifraron con
  la deploy key la siguen usando. 5 tests nuevos.
- REQ-001/003/005/006/007/009 (backend): paquete `app/voice/` (`engines.py`, `local_clone.py`,
  `settings.py`, `service.py`), endpoints `/api/voice/*`, claves en Credenciales (grupo "voz"),
  `FISH_*`/`ELEVENLABS_*` en `.env.example`, avisos en `friendly_errors.describe_voice_engine_error`.
  `tests/test_voice.py`: 42 tests con servicios falsos.
- Prueba real (una frase por motor, sin datos de la empresa): Fish `s2.1-pro-free` OK con la voz
  elegida; ElevenLabs `eleven_flash_v2_5` OK (0,8 s). Fish `s2.1-pro` → 402 sin crédito de API.
  Ver "Hallazgos" en spec.md (latencia del modelo gratuito de Fish).
- REQ-001/003/004/008/010 (frontend): `tts.js` parte el texto en tramos (`splitForSpeech`), pide cada
  uno a `/api/voice/speak` mientras suena el anterior, reproduce con Web Audio (efecto "IA" opcional)
  y cae a la voz del navegador dentro del mismo flujo; `isSpeaking()` reemplaza a
  `speechSynthesis.speaking` en ⏹, barge-in, atajo de espacio y la pausa del micrófono del Modo
  Cerebro. `app.js`: sección "Motor de voz" y "Clonación local" en Configuración > Audio.
- Arreglo encontrado por el e2e: la confirmación abierta desde Configuración quedaba tapada por el
  panel (`#confirm-overlay` con z-index propio). Afectaba también a "Eliminar skill".
- `scripts/qa_battery.py`: la regla de `aria-label` nunca contaba (comparaba textos con ids); corregida.
- Versión 1.1.0, CHANGELOG, README y AGENTS.md actualizados.
- Verificación: batería rápida 762/762; e2e de voz 3/3 (`test_e2e_voice_engines.py`); QA estática OK
  (139 checks).

### Hitos 100 % (2026-09-27)
- **REQ-002** — evidencia: `test_a_new_install_uses_the_browser_voice`,
  `test_a_cloud_engine_cannot_be_saved_without_consent`, `test_text_never_goes_to_a_cloud_engine_without_consent`,
  `test_testing_a_cloud_engine_needs_consent` y e2e `test_adding_a_cloud_engine_asks_for_consent`.
- **REQ-003** — evidencia: `test_out_of_credit_jumps_to_the_free_model_and_warns`,
  `test_a_failed_engine_is_paused_and_the_next_sentence_does_not_warn_again`,
  `test_when_everything_fails_the_browser_voice_speaks` y e2e
  `test_when_the_engine_fails_the_browser_voice_speaks_and_warns_once`; con el servicio real, Fish
  `s2.1-pro` devuelve 402 y se clasifica "sin crédito".
- **REQ-009** — evidencia: claves en Credenciales (`test_voice_keys_are_secret_and_reach_the_settings`),
  mensajes amables (`test_voices_endpoint_gives_a_friendly_error`, `test_without_key_or_voice_nothing_is_sent`),
  y ningún test sale a internet (conftest bloquea la red; motores falsos).

### Tablero de requisitos (2026-09-27)
| REQ | Estado | % | Qué falta |
|---|---|---|---|
| 001 Motores intercambiables | hecho; e2e de lectura automática y ▶ | 90 % | ver la narración y el Modo Cerebro con un motor real |
| 002 Apagado por defecto + consentimiento | verificado | 100 % | — |
| 003 Salto automático, nunca mudo | verificado | 100 % | — |
| 004 Menos de 2 s | hecho (tramos + prefetch); ElevenLabs 0,8 s | 70 % | decisión del usuario: Fish gratis tarda 1,6–6 s |
| 005 Fish Audio | frase real OK con la voz elegida | 90 % | escucha del usuario en la app |
| 006 ElevenLabs | frase real OK, 24 voces listadas | 90 % | elegir voz y escuchar en la app |
| 007 Clonación local | código + tests con falsos | 50 % | aprobar la descarga (varios GB), instalar y subir la muestra |
| 008 Efecto "IA" | hecho; corre en el e2e sin errores | 80 % | escucha del usuario |
| 009 Credenciales y errores amables | verificado | 100 % | — |
| 010 Controles de siempre | ⏹ verificado en e2e; silenciar/Modo Cerebro por código | 85 % | probar Modo Cerebro con un motor real |

Avance global estimado: ~85 %.
