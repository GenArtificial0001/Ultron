# Bitácora: ia-local

## 2026-10-25 — implementación (versión 2.1.0)

**Módulo nuevo `backend/app/llm/local_ai.py`.** Detección de Ollama y LM Studio solo con la librería estándar (urllib):
- **REQ-001:** busca cada motor en su dirección de fábrica; `OLLAMA_BASE_URL` / `LMSTUDIO_BASE_URL` la cambian y `LOCAL_AI=off`
  la apaga.
- **REQ-002:** tope de 1,5 s por consulta; el resultado se recuerda 300 s si responde y 30 s si no.
- **REQ-009:** direcciones validadas: `http(s)`, puerto válido y sin usuario ni clave embebidos.
- **REQ-011:** ventana de contexto del modelo cargado: Ollama `/api/ps` y LM Studio `/api/v0/models`, recordada 60 s.
- **Proxy:** las consultas van siempre directo (`ProxyHandler({})`). `ensure_direct` agrega el host a `NO_PROXY` para el
  cliente de OpenAI cuando hay proxy en el entorno.

**Conexión con el resto:**
- **`providers.py`:**
  - REQ-001/003: los motores detectados son proveedores (`local=True`, clave de relleno y `remote` si están en otra PC);
  - REQ-006: espera larga en la primera respuesta;
  - sin consentimiento de «gratis»;
  - sin los modelos de embeddings.
- **`model_fallback.py`:** REQ-004: un error de conexión de un modelo local cuenta como caída de ese modelo y el turno salta al
  siguiente. Los de la nube se comportan igual que antes (REQ-007).
- **`main.py`:**
  - `context_window` real y marca `local` por modelo;
  - aviso cuando el motor está en otra PC (REQ-005);
  - pista en Configuración cuando no hay ningún motor local;
  - los locales no se listan como «para agregar».
- **`app.js`:** etiqueta «local» en el selector, el aviso y el cartel de «está tardando» a los 8 s para modelos locales.
- **`conftest.py`:** la batería nunca busca un motor real (`LOCAL_AI=off` y el entorno `OLLAMA_*`/`LMSTUDIO_*` limpio). REQ-008.

**Arreglo durante la implementación:** `is_alive` y `context_length` tomaban el reloj como valor por defecto del argumento
(`now=time.monotonic`). Ese valor se fija al definir la función, así que un reloj simulado en el test no tenía efecto. Ahora
leen el reloj al llamarlas.

**Pruebas:**
- `tests/test_ia_local.py`: 22 tests contra un Ollama falso en 127.0.0.1 (ninguno usa un motor real).
- Mutantes: 6 de 6 detectados (proxy, `LOCAL_AI=off`, memoria del sondeo, usuario/clave en la URL, ventana de contexto y
  «en esta PC»).

**Prueba con el Ollama real de esta PC** (Ollama 0.40.11, `qwen3:4b`, `OLLAMA_CONTEXT_LENGTH=16384` y la gráfica integrada
activada), con el código de ULTRON y sin configurar nada:
1. **Detección:** «Ollama (local)» apareció solo, en `http://127.0.0.1:11434/v1`, con `local=True` y `remote=False`.
2. **Modelos:** `['qwen3:4b']`.
3. **Chat:** respondió en 27,0 s (primera respuesta, con el modelo cargándose).
4. **Herramientas:** llamó `hora_actual` en 25,2 s.
5. **Ventana de contexto:** 16.384, leída de `/api/ps`, y el selector la muestra (`context_window=16384`).

**Tiempos medidos en frío:**
- Ollama encendido respondió en 0,29 s. LM Studio, apagado (no está instalado), llegó al tope en 1,63 s.
- `detected()` en total: 1,86 s. Ya en caliente, 0,00 s.
- Los 4,4 s de la primera llamada son de la configuración general, que se carga una vez por proceso. Ya pasaba en la 2.0.0.

**Batería:**
- Sin e2e: 4.784 ok y 0 fallos.
- 51 e2e, cada uno en su proceso: 48 en verde.
  - `session_visibility`: un fallo intermitente con la PC cargada; repetido, 21/21.
  - Los dos más largos se repitieron con un tope de 15 min.
- Guardián 27/27 y qa_battery OK.

**Pedido del usuario durante esta versión:** la barra lateral dice ULTRON (decía VISION) y el logo de la «V» de al lado quedó
vacío. La animación de arranque también escribe ULTRON. Lo cubre `tests/test_marca_ultron.py`, que falló antes del cambio y pasa
después.
