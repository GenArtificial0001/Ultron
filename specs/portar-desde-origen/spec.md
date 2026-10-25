---
feature: portar-desde-origen
status: PENDING_APPROVAL       # NEW | IN_PROGRESS | PENDING_APPROVAL | APPROVED | IN_DEVELOPMENT | DONE
created: 2026-10-25
last_updated: 2026-10-25
approved_on: null
scores:
  objetivo: 100
  inputs: 90
  outputs: 90
  conexiones: 85
  criterios_aceptacion: 85
  reglas_negocio: 90
  seguridad: 95
  performance: 80
  ui_ux: 80
  mantenimiento: 90
viabilidad: Alta (trabajo grande: varias sesiones)
autonomia_estimada: 80% -- faltan decisiones de alcance (seccion «A decidir») y la prueba real del usuario
---

# Spec: ULTRON 2.0 — traer las mejoras de la versión de origen (0.19.4 → 0.36.2) con nombre propio

> Esta spec es la única fuente de verdad para esta feature. No se escribe código hasta que el estado sea `APPROVED`.

## Pedido original (usuario, 2026-10-25)

"Todo igual a [la versión de origen] [...] y comiteamos a la rama de github personal de Vision-Personal con todos los
cambios que vayamos aplicando, para no perdernos de ninguna mejora." Y: "Crear nuestra versión de Vision-Personal: ya
tenemos la mayoría hecho, así que comenzá, pero recordá que las conexiones por ahora son con mis proveedores
personales e IA locales descargadas."

## 🔴 Acciones Requeridas (bloqueos abiertos)

- [ ] **Aprobar esta spec** y contestar la sección «A decidir».
- [ ] Antes: commit de la 1.1.0, que hoy está sin commitear (paso 0).

## 1. Objetivo — `100%`
- Vision-Personal nació de la versión de origen 0.19.3. La de origen siguió hasta 0.36.2: son 74 versiones. Entre
  otras cosas, trae el gestor de conexiones, los interruptores, la persistencia SQLite, el Modo Cerebro con Espacio,
  el audio del sistema en el Modo reunión, Office por COM, el índice resiliente, las sesiones públicas y privadas,
  `/update`, los avisos de contexto y compactación y cientos de arreglos.
- Éxito: Vision-Personal 2.0 tiene todo eso, con la capa propia de Personal encima:
  - varios proveedores e IA local;
  - marca, carpeta de datos y puerto propios;
  - nada de la empresa de origen.

## 2. Estrategia (recomendada) — `90%`
**Nueva base + capa propia**, en lugar de traer 74 versiones de a una:
1. Se toma el código de aplicación de la versión de origen 0.36.2:
   - incluye: `backend/app`, `frontend`, `launch_vision.py`, `build_*`, los tests y `scripts`;
   - deja afuera: skills, specs, docs, historial, `.agent` y el flujo dev→prod (`sync_prod.py`, `test_prod_boundary.py`).
2. Encima se vuelve a aplicar lo que es solo de Personal:
   - `app/llm/client.py` y `app/llm/providers.py`: proveedores con clave e IA local (spec `ia-local`), en lugar del
     cliente del gateway de la empresa;
   - `config.py`: `LLM_*`, `VISION_COMPANY_NAME`, `DATA_FOLDER_NAME = "Vision-personal"` y el puerto 8030;
   - dictado `STT_*` (Groq/OpenAI) en lugar del de la empresa;
   - salto entre proveedores (`model_fallback`) e ids `proveedor/modelo`;
   - marca propia (logo, íconos, temas) y el guardián `test_vision_generico.py`.
3. La voz (Fish/ElevenLabs/clonación) y el informe técnico hablado ya están en la de origen desde 0.21.0 y 0.22.0
   (fueron portados desde acá), así que no hay que reaplicarlos: solo verificar que no se perdió nada propio.
- Por qué: la capa propia es chica (unas 2.000 líneas, la mayoría de voz, que ya viajó), mientras que las 74 versiones
  tocan miles de líneas de `app.js`, `main.py` y `loop.py`. Reaplicar la capa chica tiene muchos menos conflictos.

## 3. Qué se quita o se generaliza de la versión de origen — `85%`
| Pieza de la versión de origen | En Personal 2.0 |
|---|---|
| SDK y login del gateway de la empresa, `/models_metadata`, índice privado de paquetes en el lanzador (`lanzador-indice-...`) | Fuera: `providers.py` + `GET /models` |
| Skills internas de la empresa de origen, URLs internas, usuario de la PC original | Fuera (el guardián lo controla) |
| Conectores de datos (Oracle, SAP, SharePoint, BigQuery, Confluence) | Quedan, apagados y sin configurar (pedido: «todo igual»; las conexiones de datos se hacen después) |
| Outlook y Teams por MCP, Office por COM | **A decidir** (ver abajo) |
| Proxy corporativo y autocuración del proxy | Quedan, apagados sin `HTTP(S)_PROXY` |
| Sesiones públicas y privadas, `/update` | Quedan (sirven para una carpeta compartida) |

## 4. Criterios de aceptación — `85%`
- **REQ-001 Paso 0, nada se pierde**: la 1.1.0 actual queda commiteada en `dev` (y subida al repositorio de Gen) antes
  de empezar.
- **REQ-002 Todo lo de origen funciona**: la batería de la versión de origen, adaptada, pasa en Personal 2.0. Se quitan
  solo los tests de lo que se sacó (gateway, skills internas, prod). Cada test quitado queda listado en `progress.md`
  con su motivo.
- **REQ-003 Todo lo de Personal sigue**: pasan los tests de `varios-proveedores`, `voz-fish-audio`,
  `informe-tecnico-hablado`, `vision-generico` (incluido el guardián) e `ia-local`.
- **REQ-004 Convive con la instalación de origen**: carpeta `%LOCALAPPDATA%\Vision-personal` y puerto 8030. Nunca
  lee ni escribe `~/.vision` ni los datos de la otra instalación.
- **REQ-005 Migración de datos de la 1.1.0**: las sesiones, el `.env` y `keys/credentials.key` de una instalación 1.1.0
  se abren en 2.0. Las tablas nuevas se agregan sin borrar nada.
- **REQ-006 Nada de la empresa de origen**: el guardián pasa; además, una revisión manual de la lista de archivos antes
  de cada push.
- **REQ-007 Versión 2.0.0** con `CHANGELOG.md` que resume lo heredado (por tema, no las 74 entradas) y lo propio.
- **REQ-008 Se llama ULTRON** (pedido del usuario, 2026-10-25: «no va a compartir carpeta de instalación llamada Vision»).
  Todo lo visible:
  - el lanzador `Ultron.exe` (antes `Vision.exe`) y `Iniciar-Ultron.bat`;
  - el título de la ventana y de la pestaña, el logo, los avisos y los textos de la interfaz;
  - el README y cómo se presenta el asistente en su prompt.
  Los nombres internos del código (módulos, variables, `launch_vision.py`) **no cambian**, para que traer las próximas
  mejoras de la versión de origen siga siendo un diff limpio.
- **REQ-009 Carpeta propia `%LOCALAPPDATA%\Ultron`** (`DATA_FOLDER_NAME`), con migración en el primer arranque:
  - si existe `%LOCALAPPDATA%\Vision-personal` y `Ultron` no tiene nada, se COPIAN el `.env`, `keys/`, el índice de
    sesiones y las alarmas/agentes;
  - la carpeta vieja no se borra ni se modifica;
  - se avisa una vez en el chat qué se copió.
  Puerto 8030 (sin cambios).
- **REQ-010 Se lo llama «Ultron»**: el Modo Cerebro y el Modo reunión (`meeting_gate.py`) responden a «Ultron» y a
  «Jarvis» (como hoy), y ya no a «Vision».
- **REQ-011 Carpeta del proyecto**: `C:\Proyectos\Vision-personal` pasa a `C:\Proyectos\Ultron`. El repositorio sigue
  siendo https://github.com/GenArtificial0001/Vision.git. Hay que actualizar la tarea programada `status-vision`, que lee
  la ruta vieja.
- **REQ-012 El diseño no cambia** (decisión del usuario, 2026-10-25: «mantengamos el diseño original, o sea no cambiemos nada»).
  ULTRON usa la interfaz de la versión de origen tal cual; su tema oscuro de fábrica es «Oscuro — Rojo». Solo se renombran los
  textos con marcas de la empresa de origen (nombres de temas, comentarios de colores) para que pase el guardián. La spec
  `estilo-ultron` quedó cancelada.

## 5. Reglas de negocio — `90%`
- Las mejoras futuras de la versión de origen se traen **por versión publicada**: diff del tag anterior al nuevo,
  sacando lo de la empresa. Cada una es un commit propio en `dev` con su referencia («origen 0.36.3»).
- Push al repositorio de Gen (privado) después de cada tanda con la batería en verde.
- La carpeta de la versión de origen se lee, nunca se modifica (pedido del usuario, 2026-10-25).

## 6. Seguridad — `95%`
- El guardián `test_no_company_specific_or_personal_data_in_the_repo` corre en cada tanda. El 2026-10-25 detectó un
  doble de prueba con el nombre del gateway en `test_e2e_tts_queue.py`: renombrado.
- Las claves siguen en el `.env` o en Credenciales cifradas con `keys/credentials.key`. Nada de eso se versiona.

## 7. Performance — `80%`
- Igual que la de origen. La detección de IA local no suma más de 1,5 s (spec `ia-local`, REQ-002).

## 8. UI/UX — `80%`
- La de la versión de origen 0.36.2, con la marca de Personal y el selector agrupado por proveedor y por IA local.

## 9. Mantenimiento — `90%`
- Lo mantiene el usuario (Gen) con Claude Code, en `C:\Proyectos\Vision-personal`, rama `dev`. `main` solo avanza
  cuando el usuario lo pide.

## A decidir (antes de aprobar)
1. **Outlook/Teams (MCP de Microsoft 365) y Office por COM**:
   - (a) dejarlos, apagados de fábrica: sirven con una cuenta personal de Microsoft y con el Office de casa;
   - (b) sacarlos.
   Recomendado: (a).
2. **Conectores de datos de empresa** (Oracle, SAP, SharePoint, BigQuery, Confluence):
   - (a) dejarlos apagados, como dijo el usuario («todo igual»);
   - (b) sacarlos hasta que se necesiten.
   Recomendado: (a).
3. **Orden**:
   - (a) primero `ia-local` sobre la 1.1.0 y después la nueva base;
   - (b) primero la nueva base y después `ia-local` sobre la 2.0.
   Recomendado: (b), para no hacer `ia-local` dos veces.
