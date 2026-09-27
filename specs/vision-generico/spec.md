---
feature: vision-generico
status: APPROVED    # NEW | IN_PROGRESS | PENDING_APPROVAL | APPROVED | IN_DEVELOPMENT | DONE
created: 2026-09-27
last_updated: 2026-09-27
approved_on: 2026-09-27   # "Si, todo eso avanza" (usuario, sobre las decisiones a-d de abajo)
viabilidad: Alta
---

# Spec: Vision genérico (para cualquier empresa)

> Única fuente de verdad del proyecto `C:\Proyectos\Vision-personal` (repositorio de Gen,
> https://github.com/GenArtificial0001/Vision, PRIVADO). Nace de Vision 0.19.3 (la versión de una empresa,
> en su propio repositorio), que sigue su propio camino y NO se toca desde acá.

## 1. Objetivo
- **Problema**: Vision está atado a la empresa de origen: el proveedor de IA es su gateway interno (login con un
  SDK propio, que solo se instala desde el índice privado de la empresa), el dictado usa el Whisper de ese
  gateway, la marca y el prompt son de la planta de origen y el proxy de la empresa está fijo en el código. No se
  puede usar en otra empresa ni publicar.
- **Qué significa éxito**: (1) un clon del repo, con solo copiar `backend/.env.example` a `.env` y completarlo,
  arranca y conversa con Claude (o con cualquier servicio compatible con OpenAI); (2) cero datos de la empresa de origen, cero
  contraseñas y cero datos personales en el repo; (3) funciona detrás del proxy de cualquier empresa; (4) la
  batería de tests pasa.

## 2. Decisiones (aprobadas por el usuario el 2026-09-27)
- **a) Carpeta**: `C:\Proyectos\Vision-personal`, git propio, remoto único = repositorio de Gen.
- **b) Base**: código de la aplicación de Vision 0.19.3 (backend, frontend, lanzadores y tests). Sin skills,
  specs, docs ni historial de la versión de empresa. Primera versión del proyecto nuevo: **1.0.0**, un solo commit inicial.
- **c) Proveedor de IA**: configurable desde `.env`:
  - `LLM_PROVIDER=anthropic` (Claude con clave de Anthropic, vía su capa compatible con OpenAI:
    `https://api.anthropic.com/v1/`; el loop ya habla ese formato). Etapa 2 (fuera de esta spec): adaptador
    nativo de Anthropic para caché de prompt y razonamiento extendido.
  - `LLM_PROVIDER=openai_compatible` + `LLM_BASE_URL`: OpenAI, Azure OpenAI o el gateway propio de otra empresa.
  - `LLM_API_KEY`, `LLM_MODEL` (modelo por defecto) y `LLM_MODELS` (lista para el selector si el proveedor no
    publica `/models`).
- **d) Marca**: "Vision" a secas, con el nombre de la empresa configurable (`VISION_COMPANY_NAME`), usado en la
  pantalla y en el prompt. Sin logo, nombre ni colores de la empresa de origen (su logo es marca registrada).

## 3. Requisitos
- **REQ-001 Proveedor configurable**: el cliente del modelo sale de `LLM_PROVIDER`/`LLM_BASE_URL`/`LLM_API_KEY`.
  Sin clave o sin dirección (en `openai_compatible`), Vision lo explica en lenguaje simple y nunca manda el
  pedido a otro servidor por defecto.
- **REQ-002 Modelos**: el selector lista los modelos del proveedor (`GET /models`); si no responde, usa
  `LLM_MODELS`. El salto automático usa esa lista; sin conceptos propios del gateway de la empresa de origen (modelos on-prem gratis,
  aptitud para datos secretos), que simplemente no aparecen si el proveedor no los informa.
- **REQ-003 Dictado**: la transcripción usa su propio proveedor (`STT_BASE_URL`, `STT_API_KEY`, `STT_MODEL`,
  compatible con el endpoint de audio de OpenAI). Sin configurar, el micrófono explica que el dictado no está
  configurado (no falla en silencio ni con un error técnico).
- **REQ-004 Proxy de cualquier empresa**: `HTTP_PROXY`, `HTTPS_PROXY` y `NO_PROXY` en el `.env` los usa la app
  (llamadas al proveedor y a conectores) y el lanzador al instalar dependencias. Sin proxy, conexión directa.
- **REQ-005 Marca neutra**: título, pantalla de inicio, íconos y colores sin la marca de origen; `VISION_COMPANY_NAME` en el
  subtítulo y en el prompt ("Sos Vision, un asistente para <empresa>").
- **REQ-006 Conectores**: Oracle, BigQuery, SharePoint/Graph y SAP quedan, todos opcionales y configurados
  desde el `.env` o desde Configuración > Credenciales (cifradas en `.env.enc`, como en Vision).
- **REQ-007 Sin datos sensibles**: el repo no contiene `.env`, `.env.enc`, claves, bases de datos, logs, `.exe`,
  direcciones internas de la empresa de origen, usuarios ni correos personales. Se verifica con búsqueda antes de cada push.
- **REQ-008 Calidad**: la batería de Vision se adapta al proveedor genérico y pasa completa (unit, integración y
  e2e en Chromium real con proveedores falsos). Nunca pega a un servicio real.

## 4. Fuera de alcance
- Adaptador nativo de Anthropic (etapa 2). Publicar el repo (queda privado). Cambios en la versión de empresa.

## 5. Seguridad
- Claves solo en `.env`/`.env.enc` (nunca versionados, ver `.gitignore`). El `.env.example` trae nombres de
  variables y explicación, sin valores reales.
