---
feature: informe-tecnico-hablado
status: IN_DEVELOPMENT       # NEW | IN_PROGRESS | PENDING_APPROVAL | APPROVED | IN_DEVELOPMENT | DONE
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
autonomia_estimada: 95% -- solo falta que el usuario lo escuche
---

# Spec: Informe técnico hablado

> Esta spec es la única fuente de verdad para esta feature. No se escribe código hasta que el estado sea `APPROVED`.

## Pedido original (usuario, 2026-09-27)

"Por detrás Vision, cuando le pido que haga algo, está haciendo un proceso técnico. Todo eso lo tendría
que decir. En la configuración tendría que flaggear 'hablar informe técnico'. Y que me diga, por
ejemplo, qué está haciendo, como los comentarios de código que pone o qué aplica para realizar todo."

## 🔴 Acciones Requeridas (bloqueos abiertos)

- [ ] (ninguna)

## Reapertura (2026-09-27, después de probarlo)

Pedido del usuario, textual: "No quiero que se quede callado y me vaya diciendo qué está haciendo en la
tarea que está realizando. Por ejemplo, cuando crea el HTML, que diga 'estoy armando la estructura del
dashboard con sidebar del lado derecho, tarjetas en tal lugar, el título que le pone', como si pasara
por los comentarios de un código de programación explicando cada funcionalidad, estructura y conexión
a base."

Cambia dos decisiones aprobadas: "una frase por paso" (ahora quiere el recorrido completo) y "solo
acciones" (también el avance de las tareas). REQ-001 a REQ-007 siguen valiendo; se suman requisitos.

Hallazgo técnico (prueba real con claude-haiku-4-5 por la capa compatible con OpenAI): los argumentos
de una herramienta llegan de a pedazos mientras el modelo los escribe (3.167 caracteres en 451
fragmentos a lo largo de 5 s). Se puede narrar el código **mientras se escribe**, no solo al final.

## Categorías críticas (deben llegar a 100%)

### 1. Objetivo — `100%`
- Problema que resuelve: mientras Vision trabaja (comandos, código, consultas), el usuario no sabe qué
  está haciendo; hoy solo escucha una etiqueta corta por paso ("Consultando Oracle…").
- Quién se beneficia: quien usa Vision con "Leer en voz alta" activo (en cualquier instalación).
- Qué significa éxito (medible): con el informe prendido, cada acción se escucha en una frase que dice
  qué hace (REQ-002), la voz nunca queda atrasada (REQ-004) y nunca se dice un secreto (REQ-007).
- Por qué ahora: pedido del usuario durante la spec `voz-fish-audio`.

### 2. Inputs — `100%`
- Fuente: lo que Vision ejecuta en cada acción (herramienta y sus argumentos: comando, archivo,
  comentarios del código que escribe, consulta, página). No se le pide al modelo que redacte nada
  aparte: sin tokens ni costo extra.
- Frecuencia: una frase por acción, en cada turno con acciones.

### 3. Outputs — `100%`
- Una frase hablada por acción. Solo en voz: el chat queda exactamente como hoy.

### 4. Conexiones — `100%`
- Ninguna nueva. La frase se habla con la cadena de voces de la spec `voz-fish-audio` (si hay un motor
  en la nube, el texto le llega a ese servicio: ver REQ-006).

### 5. Criterios de aceptación — `100%` (confirmados por el usuario el 2026-09-27)

- **REQ-001 Interruptor**: "Hablar informe técnico" en Configuración > Audio, **prendido de fábrica**.
  - Given: "Leer en voz alta" activo y el interruptor prendido.
  - When: Vision ejecuta una acción.
  - Then: se escucha la frase técnica. Con el interruptor apagado vuelve la narración corta de siempre;
    con "Leer en voz alta" apagado no se habla nada.
- **REQ-002 Una frase por acción, armada con lo que ejecuta**:
  - Given: Vision escribe `reporte.py`, cuyo código empieza con el comentario
    `# Lee el Excel de stock y agrupa por pieza`.
  - When: ejecuta esa escritura.
  - Then: dice "Escribo reporte.py: lee el Excel de stock y agrupa por pieza". Del mismo modo:
    comando → "Ejecuto: <comando resumido>" (con su comentario si tiene); editar → "Edito <archivo>:
    <comentario del cambio>"; consulta → "Consulto <sistema>: <tablas>"; página → "Abro <sitio>".
- **REQ-003 Solo las acciones**:
  - Given: el informe prendido.
  - When: Vision lee o busca archivos, carga una skill o piensa.
  - Then: no se narra.
- **REQ-004 En orden, sin atrasarse** (modificado en la reapertura; antes "decir lo último"):
  - Given: Vision genera frases del informe más rápido de lo que la voz alcanza a decir.
  - When: se acumulan más de 3 frases del informe sin decir.
  - Then: se descartan las más viejas y se dicen las 3 más nuevas, en orden. La frase que está sonando
    termina; nunca se corta ni se saltea la lectura de la respuesta.
- **REQ-005 El chat no cambia**:
  - Given: el informe prendido. When: Vision trabaja. Then: el chat se ve igual que hoy.
- **REQ-006 Consentimiento**:
  - Given: un motor de voz en la nube.
  - When: se lo agrega en Configuración > Audio.
  - Then: la confirmación aclara que también se envía el informe técnico (comandos, nombres de archivos
    y tablas).
- **REQ-007 Nunca se dice un secreto**:
  - Given: un comando o código con una clave, contraseña o token.
  - When: se arma la frase.
  - Then: ese valor se reemplaza por "dato oculto".

- **REQ-008 Recorrido del código en vivo**:
  - Given: el informe prendido y el modelo escribiendo `demo.html` con
    `<!-- Sidebar a la derecha con el menú de secciones -->` y `<title>Panel de Salud</title>`.
  - When: llegan esos fragmentos, **antes de que el archivo termine de escribirse**.
  - Then: se escucha "Escribo demo.html", "sidebar a la derecha con el menú de secciones" y "Título:
    Panel de Salud". Se narra, en orden y sin repetir: cada comentario del código, el título y los
    encabezados, el sidebar (y de qué lado), las tarjetas, los gráficos, las tablas y formularios, y cada
    conexión a una base de datos o a una API. Vale para escribir y editar archivos y para comandos.
- **REQ-009 El modelo comenta el código**:
  - Given: el informe prendido.
  - When: el modelo escribe o edita código.
  - Then: comenta cada sección y funcionalidad en español, con una línea que dice qué hace (HTML
    `<!-- -->`, JS/CSS `//` o `/* */`, Python `#`, SQL `--`). Con el informe apagado no se le pide.
- **REQ-010 Avance del plan de tareas**:
  - Given: un plan de 4 tareas.
  - When: una tarea empieza o termina.
  - Then: se escucha "Paso 2 de 4: generar los datos ficticios de salud" al empezar y "Listo: generar
    los datos ficticios de salud" al terminar.
- **REQ-011 Nunca callado**:
  - Given: un turno en curso con el informe prendido.
  - When: pasan unos 15 s sin ninguna frase nueva.
  - Then: se escucha "Sigo con: <tarea en curso>" (o "Sigo trabajando" si no hay plan), y se repite cada
    ~15 s mientras no haya novedades.

## Categorías secundarias (deben llegar a ≥90%)

### 6. Reglas de negocio — `100%`
- Acciones que se narran: comandos (`bash`), escribir y editar archivos, consultas (Oracle, BigQuery,
  SAP, SharePoint/Graph), páginas web, gráficos. No se narran: leer, buscar o listar archivos, skills,
  preguntas, plan de tareas ni "pensando".
- Frases cortas: el comando o el comentario se recortan para que la frase no pase de ~20 palabras.

### 7. Seguridad y permisos — `100%`
- El informe puede nombrar archivos, tablas y comandos: con un motor en la nube, ese texto sale a otro
  servicio (REQ-006). Claves, contraseñas y tokens se reemplazan por "dato oculto" antes de hablar o
  de mandar el texto al motor de voz (REQ-007).

### 8. Performance y volumen — `100%`
- "Decir lo último": la voz nunca queda atrasada respecto de lo que hace Vision (REQ-004).

### 9. UI/UX y plataforma destino — `100%`
- Interruptor en Configuración > Audio, junto a "Leer en voz alta", prendido de fábrica.

### 10. Mantenimiento y ciclo de vida — `100%`
- Mismo criterio que el resto del proyecto: lo mantiene el usuario (Gen), se desarrolla en `dev`.

## Contexto técnico (relevado del código; no suma al puntaje)

- Hoy existe la narración de progreso (spec narracion-agente-efimera REQ-008): el evento `progress`
  trae un `label` corto (`tool_timings.describe_operation`) y `app.js` lo lee con `speak()`.
- El loop (`backend/app/llm/loop.py`) conoce cada tool-call con sus argumentos: la frase técnica se
  arma en el backend y viaja en el mismo evento `progress`; el frontend elige cuál decir.
- `speak()` (`frontend/js/tts.js`) tiene una cola: REQ-004 necesita poder reemplazar las frases del
  informe que todavía no empezaron.

## Dashboard de viabilidad

- **Viabilidad**: Alta — reusa el evento de progreso y la cola de voz existentes; sin servicios nuevos.
- **Autonomía estimada**: 95% — solo falta que el usuario lo escuche.

## Skills sugeridas

- Skill de revisión de código seguro: REQ-007 (que nunca se diga un secreto) conviene revisarlo con
  casos reales de comandos y cadenas de conexión.

## Decisiones registradas

- 2026-09-27 — **Una frase por paso** (no el detalle completo ni un resumen al final).
- 2026-09-27 — **La arma Vision con lo que ejecuta** (comando, archivo, comentarios del código), no el
  modelo: sin tokens extra.
- 2026-09-27 — **Solo en voz**; el chat no cambia.
- 2026-09-27 — **Solo las acciones** (no lecturas ni búsquedas de archivos).
- 2026-09-27 — **Decir lo último** si Vision va más rápido que la voz.
- 2026-09-27 — **Reemplaza la narración corta** mientras está prendido.
- 2026-09-27 — **Interruptor en Configuración > Audio, prendido de fábrica** (solo habla si "Leer en
  voz alta" también está activo).
- 2026-09-27 — **REQ-001 a REQ-007 confirmados**; se tapan los secretos (REQ-007).
- 2026-09-27 — **Se hace junto con la spec `voz-fish-audio`**.
- 2026-09-27 — **Spec aprobada** por el usuario ("APPROVED").
- 2026-09-27 — **Reapertura**: recorrido del código **en vivo**, sección por sección (REQ-008); el
  modelo **comenta todo** en español cuando el informe está prendido (REQ-009); avance del plan **al
  empezar y al terminar** cada tarea (REQ-010); aviso cada **~15 s** sin novedades (REQ-011). REQ-004
  pasa de "decir lo último" a "en orden, con un tope de 3 frases pendientes" (si no, el recorrido se
  perdería).
- 2026-09-27 — **Reapertura aprobada** por el usuario ("approved"): REQ-004 modificado y REQ-008 a REQ-011.
