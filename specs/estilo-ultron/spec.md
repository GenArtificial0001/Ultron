---
feature: estilo-ultron
status: CANCELLED       # NEW | IN_PROGRESS | PENDING_APPROVAL | APPROVED | IN_DEVELOPMENT | DONE
# 2026-10-25: CANCELADA. El usuario rechazó 3 maquetas (v1 por rectángulos; v2 y v3 por «muy hecho por IA, muy común,
# nada profesional») y decidió: «mantengamos el diseño original de Vision, o sea no cambiemos nada».
# En ULTRON ese tema es «Oscuro — Rojo» (mismo diseño, nombre genérico). Queda escrita por si se retoma.
created: 2026-10-25
last_updated: 2026-10-25
approved_on: null
scores:
  objetivo: 90
  inputs: 80
  outputs: 70
  conexiones: 100
  criterios_aceptacion: 60
  reglas_negocio: 70
  seguridad: 100
  performance: 70
  ui_ux: 60
  mantenimiento: 80
viabilidad: Alta
autonomia_estimada: 80% -- el gusto visual lo decide el usuario: hace falta que mire una maqueta antes de aprobar
---

# Spec: Estilo ULTRON (Matrix + Iron Man)

> Esta spec es la única fuente de verdad para esta feature. No se escribe código hasta que el estado sea `APPROVED`.

## Pedido original (usuario, 2026-10-25)

"[...] con un frontend más al estilo Matrix e Iron Man." Va junto con el cambio de nombre a ULTRON (spec
`portar-desde-origen`, REQ-008 a REQ-012) y se aplica sobre la base 2.0, no sobre la 1.1.0.

## 🔴 Acciones Requeridas (bloqueos abiertos)

- [ ] Contestar las preguntas de la sección «Entrevista».
- [ ] Mirar la maqueta (una página de muestra con la pantalla principal en el estilo nuevo) y aprobarla o corregirla.

## 1. Objetivo — `90%`
- Que ULTRON tenga identidad propia, distinta de la versión de origen: una consola de IA con lluvia de código verde
  tipo Matrix y un HUD holográfico tipo armadura de Iron Man (interfaz de JARVIS).
- Sin perder legibilidad ni velocidad: es la herramienta de todos los días.

## 2. Propuesta (borrador, a validar con la maqueta)
- **Paleta**:
  - fondo negro azulado;
  - verde fósforo Matrix (`#00FF41`) para el texto del sistema, los avisos y el código;
  - cian holográfico (`#3DF5FF`) para los paneles del HUD;
  - rojo y oro de la armadura (`#B3121A` y `#E8B04A`) para acentos y alertas.
  - Todos los textos con contraste AA como mínimo.
- **Tipografía**: monoespaciada para el sistema y el código (JetBrains Mono, incluida en el repo, sin CDN); sans
  geométrica para el chat (Rajdhani o Orbitron solo en títulos).
- **Fondo**: lluvia de código Matrix en un `<canvas>` muy tenue (opacidad ~6 %) detrás del chat. Se pausa con la
  pestaña oculta y se apaga con «reducir movimiento» de Windows o con un interruptor.
- **Reactor Arc**: el logo y el indicador de estado son un reactor circular que «late» mientras ULTRON piensa, gira
  mientras usa herramientas, se pone rojo ante un error y se apaga cuando no hay conexión. Reemplaza al ojo de la
  pantalla de bienvenida.
- **Paneles HUD**: barra lateral y tarjetas con esquinas cortadas en diagonal, líneas finas cian, marcas de escaneo y
  un brillo sutil.
- **Modo Cerebro y voz**: anillos concéntricos que reaccionan al audio (HUD de JARVIS) en lugar del medidor actual.
- **Temas**: «ULTRON» (Matrix + Iron Man, de fábrica), «Matrix puro» (solo verde) y «Armadura» (rojo y oro). Los
  temas actuales se mantienen como opción.

## 3. Criterios de aceptación — `60%` (se completan tras la entrevista)
- **REQ-001 Tema ULTRON de fábrica** con la paleta, la tipografía y el reactor Arc de la propuesta.
- **REQ-002 Rendimiento**:
  - la lluvia de código no supera el 3 % de CPU en reposo ni baja el chat de 60 cuadros por segundo;
  - con «reducir movimiento» de Windows, no hay animaciones.
- **REQ-003 Legibilidad**: contraste AA en todos los textos, medido con un e2e de estilos computados (como el de
  `error-estilo-system-note` en la versión de origen).
- **REQ-004 Nada cambia de lugar**: mismos ids y misma estructura. La batería (`qa_battery.py`) y los e2e existentes
  pasan sin tocarlos.
- **REQ-005 Sin dependencias externas**: fuentes y efectos incluidos en el repo, sin CDN.

## Entrevista: respuestas del usuario (2026-10-25)
- **Maqueta v1 rechazada**: «estaba todo muy estructurado en rectángulos». Iron Man no lo tenía así: era un panel de
  widgets organizados sin recuadros, como hologramas. **Regla: nada de cajas ni tarjetas**; arcos, anillos, nodos en
  órbita, texto con brillo y líneas finas.
- **Disposición mixta**: el inicio es un **Centro de mando** con widgets animados, y en el chat queda una **franja HUD** finita
  con hora, batería, conexiones con latencia, tiempo en línea y un botón de noticias.
- **Widgets**:
  - hora y fecha;
  - batería;
  - latencia en vivo de cada conexión (IA local, proveedores y Google);
  - tiempo en línea (ULTRON, sesión, internet);
  - CPU, RAM y GPU;
  - **temperatura del clima y de la CPU** (las dos).
- **Noticias de IA mezcladas** (inglés y español), por RSS, con resumen opcional del modelo local.
- **Google**: agenda (Calendar) y correo (Gmail). Se evalúa conectar todo con Google en una spec aparte (`conexion-google`).
- Maqueta v2: Centro de mando y chat con franja HUD, en https://claude.ai/artifact/S8fidgjn1WKHX9Vr5hQWEJ

## Entrevista (preguntas originales)
1. ¿Cuánto de Matrix y cuánto de Iron Man? Por ejemplo: Matrix de fondo y HUD de Iron Man en paneles (propuesta), o
   predominio de uno de los dos.
2. ¿La lluvia de código siempre visible, solo en la bienvenida, o solo mientras ULTRON piensa?
3. ¿Sonidos de interfaz estilo JARVIS (encendido, confirmación, alerta)? Apagados de fábrica.
4. ¿Logo nuevo de ULTRON? Propuesta: el reactor Arc con la U en el centro.
5. ¿Alguna referencia concreta (captura, película o app) que quieras imitar?

## Seguridad — `100%`
- Solo frontend: no cambia rutas, datos ni permisos.

## Mantenimiento — `80%`
- Variables CSS por tema en `styles.css`; la lluvia y el reactor en un módulo propio (`frontend/js/ultron-hud.js`), para que
  traer mejoras de `app.js` desde la versión de origen no choque con el estilo.
