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

## 2026-10-26: arreglo de un caso real sin internet (versión 2.1.1)

**Qué vio el usuario:** abrió ULTRON sin internet, con Ollama y `qwen3:4b` en su PC, y no respondió nada.

**Diagnóstico, con los logs de ULTRON y de Ollama:**
1. **Contexto corto.** Ollama corría con su ventana de fábrica: en el Windows del usuario no estaban `OLLAMA_CONTEXT_LENGTH`
   ni `OLLAMA_IGPU_ENABLE`. Su log dice `truncating input prompt limit=2050 prompt=11078`: recortó en silencio el pedido de
   ULTRON. `qwen3:4b` «pensó» 186 s y terminó sin escribir nada (`ttft_ms: null`), y el chat quedó en blanco.
2. **Compactación al proveedor equivocado.** Después, al compactar, `compaction._summarize` llamaba a `call_with_token_retry`
   sin `client_getter`. Con eso el pedido iba al proveedor principal, en la nube, con el nombre `ollama/qwen3:4b`. Sin internet
   falló, el salto automático le echó la culpa a Ollama y lo pausó 10 minutos: los «hola» siguientes no recibieron nada.
   `pdf_vision.complete_with_proveedor` tenía el mismo error.

**Arreglo:**
- **`client.call_model_with_token_retry(model, fn)`:** el cliente del proveedor del modelo, con el nombre que ese proveedor
  entiende. Lo usan la compactación y la lectura de PDF.
- **REQ-011, que había quedado incompleto** (solo se mostraba la ventana en el selector):
  - `local_ai.loaded_context_length` carga el modelo con `POST /api/generate` sin `prompt` si Ollama todavía no lo cargó, y
    lee su ventana.
  - `loop._local_context_problem` mide el pedido con las herramientas incluidas y una reserva de 1.024 tokens para la respuesta.
  - Si no entra, el aviso `MODELO-LOCAL-CONTEXTO-CORTO` lleva el arreglo. No se manda el pedido recortado y no se pausa el modelo.
  - El chequeo se hace al empezar el turno (antes de intentar compactar) y en cada paso.
- **Respuesta vacía:** una nota del sistema explica que el modelo terminó sin escribir nada, con la pista del contexto si es local.
- **LSP:** se lanza como `python -m pylsp`. Al renombrar la carpeta a `C:\Proyectos\Ultron`, el `pylsp.exe` del entorno virtual
  quedó apuntando a la ruta vieja y la batería mostró 6 fallos en `test_lsp_recovers`.

**Pruebas:**
- `tests/test_ia_local_sin_internet.py`: 14 tests contra un Ollama falso por HTTP, con un proveedor principal «en la nube» sin
  conexión, como en la PC del usuario. Con el código anterior fallaban 11 de 12; el que pasaba es el de «con contexto suficiente
  responde como siempre», que debe pasar en los dos casos.
- Más un test del LSP como módulo.
- Mutantes: 9 de 9 detectados. Dos sobrevivieron al principio y obligaron a dos tests más:
  - el chequeo del principio del turno y el de cada paso se cubrían entre sí;
  - el principal de los tests era Ollama, así que el cliente equivocado no se notaba.
- **Contra el Ollama real del usuario** (configuración de fábrica, modelo sin cargar): ULTRON lo cargó en 11,9 s, leyó 4.096 y
  dio el aviso con el arreglo.
