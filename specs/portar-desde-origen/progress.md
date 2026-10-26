# Progress: ULTRON 2.0 (portar-desde-origen)

> Bitácora de implementación. Cada tarea se traza a un REQ-ID de `spec.md`.

## 2026-10-25: aprobación y trabajo (Claude Code, rama `ultron-2.0`)

**Aprobada** por el usuario con lo recomendado: Outlook/Teams y Office apagados de fábrica, conectores de datos apagados de
fábrica, primero la base 2.0 y después `ia-local`.

### REQ-001: nada se pierde
- La 1.1.0 quedó commiteada en `dev` (`c166853`) y subida antes de empezar. Ese mismo día el repositorio pasó a ser
  `GenArtificial0001/Ultron` (remoto `origin`, Deploy Key «ULTRON»); el anterior quedó como remoto `vision`.

### Cómo se hizo el portado (estrategia de la spec: nueva base + capa propia)
- **Merge de 3 vías en un repo aparte** (en la carpeta temporal), con árboles de archivos sin la historia de la versión de origen.
  Así ningún commit de origen viaja al repositorio de ULTRON.
  - base: versión de origen 0.19.3;
  - lado A: versión de origen 0.36.2;
  - lado B: Personal 1.1.0.
- **31 archivos en conflicto (~130 bloques)**, resueltos uno por uno con un ayudante que elige origen, Personal o ambos por bloque.
  - Criterio general: la versión de origen manda en todo lo que evolucionó. Ahí entran la voz, el informe técnico y la sesión,
    que la versión de origen ya había traído de Personal y siguió mejorando.
  - Personal manda en los proveedores de IA, el dictado, la marca, la carpeta de datos y el puerto.
- **Efecto del merge que git no marca:** la versión de origen y Personal habían agregado las mismas funciones en lugares distintos
  (`speakTechReport`, `isTechReportEnabled`, `describe_voice_engine_error`). Quedaban duplicadas, y en un módulo de JS eso rompe
  todo. Las detecta un script y se borró la copia vieja (la de Personal).

### Lo de la empresa de origen, afuera (REQ-006)
- Gateway de IA, su SDK y su login; el índice privado de paquetes y la recuperación del lanzador; el proxy fijo de la empresa.
- Barrido de textos con reglas ordenadas (dominios, gateway, VPN, marca, colores del tema, usuarios). Una segunda pasada se ocupó
  de los identificadores (dobles de prueba y parámetros con el nombre de la empresa o de su gateway).
- El guardián `test_no_company_specific_or_personal_data_in_the_repo` ahora revisa también `specs/`. El falso positivo
  una variable de `app.js` que contenía esas letras por casualidad se renombró a `_chatMainDropZone`.
- **Datos fuera de `~/.vision`** (REQ-004): `connections.json` y el índice del lanzador viven en la carpeta de datos.

### Generalizaciones
- **Configuración de IA:** la sección del gateway de la configuración general pasó a «Inteligencia artificial» (`llm_*`, `stt_*`,
  `vision_company_name`, `m365_connector_url`), y el modelo del gateway pasó a `llm_model` / `default_model_id()`.
- **`client.py`:** el de Personal (proveedores) más `call_with_token_retry`, que la versión de origen usa en el dictado, los PDF y
  la compactación. Con una clave fija, un 401 recrea el cliente una vez.
- **Proxy:** sin predeterminado de fábrica. Con predeterminado, se siguen las IPs de su nombre, como en la versión de origen; sin
  él, las del proxy guardado si es un nombre.
- **Outlook/Teams y Office apagados de fábrica:** solo un `0` guardado los prende; nada guardado o un valor roto = apagado.
- **Conector de Microsoft 365 configurable:** `NotConfigured` con la frase de qué falta. `CONNECTOR_URL` queda vacío y, si
  alguien lo fija (los tests), manda.
- **Aviso de proveedor caído:** frase amable con qué hacer (proxy → Configuración > Red), traída de proxy-autoheal REQ-006.

### REQ-008 a REQ-012: ULTRON
- **REQ-008:** renombre visible con un script que cambia «Vision» por «ULTRON» solo en strings no-docstring de Python, en
  literales de JS y en el texto del HTML. Nunca toca bytes (**la entropía DPAPI `b"Vision/gestor-conexiones/v1"` queda igual**:
  si cambiara, todo lo guardado cifrado quedaría ilegible). También: `Ultron.exe`, `Ultron.spec`, `--name Ultron`,
  `Iniciar-Ultron.bat` y el `User-Agent` `ULTRON-Agent`.
- **REQ-009:** `DATA_FOLDER_NAME = "Ultron"` en la app y en el lanzador.
  - `app/data_migration.py` copia de `Vision-personal` (sin `app/` ni `logs/`), una sola vez y solo si ULTRON no tiene datos.
  - Deja una nota y `/api/status` la manda UNA vez como `data_migration`; el chat la muestra como nota del sistema.
  - 8 tests, uno de ellos fija los dos nombres de carpeta para que ningún renombre automático los cruce. Pasó una vez: el script
    renombró la carpeta vieja de los tests a «Ultron» y se corrigió.
- **REQ-010:** palabra de voz «Ultron» (más «Jarvis») en `meeting_gate.py` y en los dos detectores de `app.js` (reunión y
  Cerebro). «Visión» ya no llama: queda en la lista de lo que no dispara.
- **REQ-011:** la carpeta del proyecto sigue siendo `C:\Proyectos\Vision-personal` hasta cerrar la 2.0 (para no cortar las
  rutas abiertas). Pendiente: renombrarla a `C:\Proyectos\Ultron` y actualizar la tarea programada `status-vision`.
- **REQ-012:** el diseño no cambia (el tema «Oscuro — Rojo» es el de siempre).

### Hallazgos que dejaron arreglos de producto
- **LSP:** el cliente mandaba el archivo sin esperar la respuesta de `initialize`. En esta PC `pylsp` tardó 10 s en arrancar en
  el entorno recién instalado, contra 3 s en el de la versión de origen, así que el primer diagnóstico salía vacío siempre.
  - Ahora espera el arranque con `_INIT_TIMEOUT = 30 s`, aparte de los 5 s por archivo.
  - Test con un `pylsp` falso que tarda 6 s: falla con el mutante (sin la espera) y pasa con el arreglo.
  - Se descartaron midiendo: `ujson` 6, `pycodestyle` 2.15 y la falta de `setuptools`.

### Tests (REQ-002/003)
- **Adaptados al comportamiento de ULTRON** (sin quitar lo que prueban):
  - dobles de `get_client` que no aceptaban `model=`;
  - `proveedor_model` → `llm_model`;
  - interruptores prendidos en los tests de comportamiento de Office/M365 (los del interruptor pasan a esperar «apagado de fábrica»);
  - conector M365 de prueba en `conftest`;
  - puerto 8030, carpeta de datos en vez de `~/.vision`;
  - catálogo de modelos de proveedores en vez de `/models_metadata`;
  - dictado con `get_stt_client`;
  - proxy simulado con predeterminado.
- **Quitados, con su motivo:**
  - Los tres tests del login, el cliente y el índice privado del gateway (`test_ensure_*_login.py`, `test_*_client.py`, `test_launcher_*_index.py`): login, cliente e índice privado
    del gateway de la versión de origen. No existen en ULTRON.
  - `test_agent_avisar.py`, `test_agent_notify_outlook.py`: scripts de `.agent/` del flujo de desarrollo de la versión de origen
    (avisos de OpenCode). No viajaron.
  - `test_prod_boundary.py` y `test_sync_prod_ships_the_generator`: flujo dev→prod con `sync_prod.py`. En ULTRON lo que viaja lo
    decide `build_stage.py` (lo vigilan `test_the_guide_is_dev_only_and_never_travels_in_the_exe` y
    `test_the_runtime_requirements_file_is_what_prod_and_the_exe_ship`).
- **Renombrado:** el test del corte del gateway → `test_loop_corte_proveedor.py` (el reintento ante un corte es genérico).
- **Nuevos:**
  - `test_data_migration.py` (8);
  - en `test_m365_auth`, el conector configurable y el aviso sin configurar (2);
  - fábrica sin proxy (2);
  - fábrica sin Office ni M365 en las herramientas (1);
  - `pylsp` lento (1);
  - en `test_e2e_brain_autosend`, «ultrón» llama y «visión» ya no (2).
- **Portados con barrido:** `docs/COMO-CREAR-TOOLS-Y-CONEXIONES.md` y `backend/connections.example.json`.

### Mediciones
| Corrida | Resultado |
|---|---|
| Primera, sin e2e | 4.378 ok, 316 fallos, 99 errores |
| Antes del renombre | 4.744 ok, 2 fallos (la tabla del README pendiente y un test dependiente del orden, ya corregido) |
| Después del renombre | 4.744 ok, 9 fallos (los de la versión rota de los tests de migración y la palabra de voz), corregidos |

- QA estática (`qa_battery.py`) OK.
- e2e: en curso al escribir esto.

## 2026-10-26: REQ-011 hecho, spec en DONE
- **Carpeta del proyecto** renombrada a `C:\Proyectos\Ultron`. El 2026-10-25 Windows no dejó renombrarla porque OpenCode del
  usuario la tenía abierta; se hizo cuando él lo cerró. La tarea programada `status-vision` lee la carpeta nueva.
- **Efecto del renombre:** los `.exe` de `backend\.venv\Scripts` guardan la ruta del Python viejo.
  - `pylsp.exe` dejó de arrancar y la batería dio 6 fallos en `test_lsp_recovers`.
  - El LSP ahora se lanza como `python -m pylsp` (con test).
  - El arranque (`Iniciar-Ultron.bat` y `launch_vision.py`) ya usaba `python.exe -m`, así que no se afectó.
