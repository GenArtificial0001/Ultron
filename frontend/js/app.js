// Vision - frontend vanilla JS (sin build step, consistente con el resto
// del workspace). Habla con el backend FastAPI (app/main.py) via
// POST /api/chat, que responde un stream Server-Sent Events (SSE) real
// sobre el body del POST -- se parsea a mano con el Streams API del
// navegador (fetch + ReadableStream), ya que EventSource nativo del
// browser NO soporta POST con body.

import { renderMarkdown, toPlainText } from "./markdown.js";
import { initVoiceEye, destroyVoiceEye } from "./voice-eye.js";
import { initSplashScreen } from "./splash-screen.js";
import { isTtsEnabled, setTtsEnabled, isTechReportEnabled, setTechReportEnabled, speak, cancelSpeech, speakTestPhrase, pickBestSpanishMaleVoice, getAvailableSpanishVoices, setSavedVoiceURI, playActivationSound, isVoiceMuted, setVoiceMuted, onSpeechEnd, isSpeaking, configureVoiceBackend, applyVoiceConfig, playVoiceSample, TEST_PHRASE } from "./tts.js";
import { typesetMath } from "./math.js";
import { convertLatexFormulasToUnicode } from "./latex-to-unicode.js";

// spec jarvis-mobile-pwa: API_BASE ya no es una constante fija a
// 127.0.0.1 -- eso solo funciona cuando el frontend se abre en la MISMA
// notebook que corre el backend. Para que un celular en la misma LAN
// pueda usar el chat (abriendo http://<ip-notebook>:8030/), el JS que
// corre en el celular necesita saber la IP real de la notebook, no la
// suya propia (127.0.0.1 del celular no es la notebook).
//
// Regla de resolucion, en orden:
//   1. Valor guardado en localStorage (el usuario lo configuro a mano una
//      vez, ver ensureServerConnection()/pantalla "Conectar a servidor").
//   2. window.location.origin -- funciona automaticamente en el caso mas
//      comun: si el celular abrio http://192.168.1.50:8030/, el origin YA
//      es esa misma IP:puerto (el navegador la resolvio al cargar la
//      pagina), sin que el usuario tenga que tipear nada.
//   3. Fallback final http://127.0.0.1:8030 (comportamiento identico al
//      de antes de esta spec, para file:// u otros casos raros).
const API_BASE_KEY = "vision.apiBase";
const ACCESS_TOKEN_KEY = "vision.accessToken";

function getApiBase() {
  try {
    const saved = localStorage.getItem(API_BASE_KEY);
    if (saved) return saved;
  } catch {
    // localStorage inaccesible -- sigue a los fallbacks
  }
  if (
    typeof window !== "undefined" &&
    window.location &&
    window.location.origin &&
    window.location.origin.startsWith("http")
  ) {
    return window.location.origin;
  }
  return "http://127.0.0.1:8030";
}

function getAccessToken() {
  try {
    return localStorage.getItem(ACCESS_TOKEN_KEY) || "";
  } catch {
    return "";
  }
}

function setServerConnection(apiBase, token) {
  try {
    localStorage.setItem(API_BASE_KEY, apiBase);
    if (token) localStorage.setItem(ACCESS_TOKEN_KEY, token);
    else localStorage.removeItem(ACCESS_TOKEN_KEY);
  } catch {
    // noop -- si no hay localStorage, la conexion solo dura esta carga
  }
}

const API_BASE = getApiBase();

// Wrapper de fetch que inyecta el header X-Jarvis-Token en cada llamada al
// backend (ver backend/app/auth.py) -- centralizado aca en vez de tocar
// las ~55 llamadas apiFetch() sueltas de este archivo. Cuando no hay token
// configurado (uso local de siempre en 127.0.0.1, sin ACCESS_TOKEN en el
// .env del backend) el header simplemente no se manda, comportamiento
// identico al de antes de esta spec.
function apiFetch(url, options = {}) {
  const token = getAccessToken();
  if (!token) return window.fetch(url, options);
  const headers = new Headers(options.headers || {});
  headers.set("X-Jarvis-Token", token);
  return window.fetch(url, { ...options, headers });
}

// spec voz-fish-audio REQ-001/003: tts.js pide el audio de cada frase al
// backend con este mismo fetch (lleva el token de acceso) y los avisos de
// "esta voz fallo, sigo con otra" van al chat como nota del sistema.
configureVoiceBackend({
  fetchFn: apiFetch,
  apiBase: API_BASE,
  onNotice: (text) => appendMessage("system-note", text, { persist: false }),
});

// -------------------------------------------------------------------------
// spec informe-tecnico-hablado: todas las frases del informe van por la misma
// cola con un tope de 3 esperando (REQ-004: en orden, sin atrasarse), y si
// pasan ~15 s sin ninguna, se dice en que tarea sigue (REQ-011: nunca callado).
// -------------------------------------------------------------------------
const TECH_REPORT_PENDING_LIMIT = 3;
const TECH_REPORT_KEEPALIVE_MS = 15_000;
let _techKeepAliveTimer = null;
let _techKeepAliveSessionId = null;
let _techLastSpokenAt = 0;
let _techCurrentTask = "";

function speakTechReport(text) {
  if (!text) return;
  _techLastSpokenAt = Date.now();
  speak(text, { limitKey: "tech-report", limit: TECH_REPORT_PENDING_LIMIT });
}

// La tarea mas especifica en curso del plan (una sub-tarea gana a su madre).
function currentTaskOf(todos) {
  let current = "";
  for (const task of todos || []) {
    if (task.status !== "in_progress") continue;
    current = task.content || current;
    const sub = (task.subtasks || []).find((s) => s.status === "in_progress");
    if (sub) return sub.content;
  }
  return current;
}

function ensureTechKeepAlive() {
  if (_techKeepAliveTimer || !isTtsEnabled() || !isTechReportEnabled()) return;
  _techKeepAliveSessionId = state.sessionId;
  _techLastSpokenAt = Date.now();
  _techKeepAliveTimer = setInterval(() => {
    if (state.sessionId !== _techKeepAliveSessionId || !isTechReportEnabled()) {
      stopTechKeepAlive();
      return;
    }
    // Mientras suena algo (la frase anterior, la respuesta) no hay silencio.
    if (isSpeaking()) _techLastSpokenAt = Date.now();
    if (Date.now() - _techLastSpokenAt < TECH_REPORT_KEEPALIVE_MS) return;
    speakTechReport(_techCurrentTask ? `Sigo con: ${_techCurrentTask}` : "Sigo trabajando");
  }, 1000);
}

// `sessionId`: solo frena si el turno que termino es el de la sesion que se
// esta narrando (un agente en otra sesion no la corta).
function stopTechKeepAlive(sessionId = null) {
  if (sessionId && sessionId !== _techKeepAliveSessionId) return;
  clearInterval(_techKeepAliveTimer);
  _techKeepAliveTimer = null;
  _techKeepAliveSessionId = null;
  _techCurrentTask = "";
}

// -------------------------------------------------------------------------
// Errores entendibles para personas no técnicas (pedido del usuario
// 2026-09-24: "mejora el manejo de errores para que los mensajes lo
// entiendan las personas no técnicas"). Antes la UI mostraba cosas como
// "Error: HTTP 500", "Error de conexion: Failed to fetch" o el texto crudo
// de una excepción, y varias pantallas ni siquiera avisaban (una lista que
// fallaba quedaba vacía; "Guardado ✓" aunque el guardado fallara).
// Regla: lo que se ve dice QUÉ pasó y QUÉ hacer. El backend ya manda
// `detail` en lenguaje simple (backend/app/friendly_errors.py); acá se
// traduce lo que genera el navegador por su cuenta (sin conexión, un status
// sin detalle, una respuesta ilegible). El detalle técnico va a la consola.
// -------------------------------------------------------------------------
const OFFLINE_ERROR_MESSAGE =
  "No hay conexión con Vision. Puede que se haya cerrado o se esté reiniciando: esperá unos segundos y volvé a intentar. Si no vuelve, abrí Vision de nuevo.";
const UNEXPECTED_ERROR_MESSAGE =
  "Ocurrió un problema inesperado. Volvé a intentar; si se repite, cerrá y volvé a abrir Vision.";

const HTTP_STATUS_MESSAGES = {
  400: "Vision no pudo procesar el pedido: falta un dato o alguno no es válido. Revisalo y volvé a intentar.",
  401: "Este navegador no tiene la clave de acceso de Vision, o no es la correcta. Cargala en Configuración > Servidor, en «Token de acceso».",
  403: "No tenés permiso para hacer esto. Si lo necesitás, avisá a soporte.",
  404: "No se encontró lo que buscabas: puede que se haya borrado o movido. Recargá la página y volvé a intentar.",
  408: "La operación tardó demasiado. Volvé a intentar.",
  409: "Vision está ocupada con otra tarea en esta conversación. Esperá a que termine y volvé a intentar.",
  413: "Lo que intentaste enviar es demasiado grande. Probá con un archivo o una imagen más chica.",
  422: "Falta un dato o alguno no tiene el formato esperado. Revisalo y volvé a intentar.",
  429: "Hay demasiados pedidos seguidos. Esperá unos segundos y volvé a intentar.",
  500: "Vision tuvo un problema interno. Volvé a intentar; si se repite, cerrá y volvé a abrir Vision.",
  502: "Un servicio externo (la inteligencia artificial o una base de datos) no respondió bien. Volvé a intentar en unos minutos.",
  503: "Vision o un servicio externo no está disponible en este momento. Volvé a intentar en unos minutos.",
  504: "Un servicio externo tardó demasiado en responder. Volvé a intentar en unos minutos.",
};

function httpStatusMessage(status) {
  if (HTTP_STATUS_MESSAGES[status]) return HTTP_STATUS_MESSAGES[status];
  return status >= 500 ? HTTP_STATUS_MESSAGES[500] : HTTP_STATUS_MESSAGES[400];
}

// Error listo para mostrar a partir de una respuesta HTTP no-ok. Usa el
// `detail` del backend solo si es TEXTO: el 422 de validación de FastAPI
// trae una lista de objetos técnicos (antes terminaba en "[object Object]").
function httpError(status, body = null) {
  const detail = body && typeof body.detail === "string" ? body.detail.trim() : "";
  const err = new Error(detail || httpStatusMessage(status));
  err.status = status;
  err.friendly = true;
  return err;
}

async function httpErrorFromResponse(resp) {
  let body = null;
  try {
    body = await resp.json();
  } catch {
    // cuerpo vacío o no-JSON (ej. un 502 de un proxy): alcanza con el status
  }
  return httpError(resp.status, body);
}

// fetch para ACCIONES (guardar, borrar, ejecutar): si el backend responde con
// error, lanza uno ya traducido en vez de seguir como si hubiera andado.
async function apiFetchOk(url, options = {}) {
  const resp = await apiFetch(url, options);
  if (!resp.ok) throw await httpErrorFromResponse(resp);
  return resp;
}

// fetch() rechaza con TypeError SOLO cuando no pudo hablar con el servidor
// (el texto cambia según el navegador). Otros TypeError son bugs de JS y no
// deben disfrazarse de "sin conexión".
function isOfflineError(err) {
  return err instanceof TypeError && /failed to fetch|networkerror|load failed|network request failed/i.test(err.message || "");
}

function friendlyErrorMessage(err) {
  if (err && err.friendly) return err.message;
  if (isOfflineError(err)) return OFFLINE_ERROR_MESSAGE;
  if (err && (err.name === "AbortError" || err.name === "TimeoutError")) {
    return "La operación se cortó porque tardó demasiado. Volvé a intentar.";
  }
  // Ni red ni HTTP: un bug o una respuesta ilegible. El detalle real queda
  // en la consola para diagnosticarlo.
  console.error("Error inesperado en la interfaz:", err);
  if (err instanceof SyntaxError) {
    return "Vision devolvió una respuesta que no se pudo leer. Volvé a intentar; si se repite, cerrá y volvé a abrir Vision.";
  }
  return UNEXPECTED_ERROR_MESSAGE;
}

// Línea corta para "Detalle técnico" (plegado en la burbuja de error): sirve
// para pasarle a soporte sin ensuciar el mensaje principal.
function technicalErrorLine(err) {
  if (!err) return "";
  if (err.status) return `HTTP ${err.status}`;
  return `${err.name || "Error"}: ${err.message || ""}`.trim();
}

// Motivo entendible de por qué no se pudo abrir el micrófono (getUserMedia).
function describeMicError(err) {
  const name = err && err.name;
  if (name === "NotAllowedError" || name === "SecurityError") {
    return "Vision no tiene permiso para usar el micrófono. Habilitalo desde el candado de la barra de direcciones y volvé a intentar.";
  }
  if (name === "NotFoundError" || name === "OverconstrainedError") {
    return "No hay ningún micrófono conectado. Conectá uno y volvé a intentar.";
  }
  if (name === "NotReadableError" || name === "AbortError") {
    return "El micrófono está ocupado por otro programa (por ejemplo Teams) o Windows lo bloqueó. Cerrá ese programa y volvé a intentar.";
  }
  return "No se pudo usar el micrófono. Revisá que esté conectado y volvé a intentar.";
}

function isMicPermissionError(err) {
  return !!err && (err.name === "NotAllowedError" || err.name === "SecurityError");
}

// Lista (ul) que en vez de datos muestra UN aviso de error -- siempre como
// texto, nunca innerHTML con el mensaje adentro.
function renderListError(listEl, text, className = "conn-tree-error") {
  const li = document.createElement("li");
  li.className = className;
  li.textContent = text;
  listEl.replaceChildren(li);
}

// El sessionId se reutiliza entre recargas de pagina (guardado bajo una
// clave fija en localStorage) para que la persistencia de conversacion
// tenga sentido real: sin esto, cada F5 generaria un id nuevo y jamas
// encontraria el historial guardado. Pensado para el futuro despliegue como
// SharePoint Full-Page App, donde el usuario puede recargar la pagina del
// sitio sin querer perder el hilo de la conversacion.
const SESSION_ID_KEY = "vision.currentSessionId";

function getOrCreateSessionId() {
  try {
    const existing = localStorage.getItem(SESSION_ID_KEY);
    if (existing) return existing;
  } catch {
    // localStorage inaccesible -- cae a un id efimero por esta carga
  }
  const fresh = crypto.randomUUID();
  try {
    localStorage.setItem(SESSION_ID_KEY, fresh);
  } catch {
    // noop
  }
  return fresh;
}

const state = {
  sessionId: getOrCreateSessionId(),
  sending: 0, // contador, no booleano -- ver sendMessage() para el por que
  selectedModel: "",
  // REQ-006 (spec persistencia-sqlite-sesiones): null = todavia no se
  // consulto el backend; true/false = ultimo valor conocido de
  // persistence_available (ver updatePersistenceBanner).
  persistenceAvailable: null,
  // spec modo-plan-loop REQ-001/005: "plan" | "active" -- espejo del
  // valor real de session.mode en el backend (fuente de verdad), se
  // sincroniza al activar/reconectar una sesion (ver activateSession) y
  // se actualiza optimista al togglear (setMode).
  mode: "active",
  // spec salto-automatico-modelos: si el salto esta activo (lo informa
  // /api/status), el modelo por defecto del backend y el modelo que respondio
  // de verdad el ultimo turno (evento `model-switch`), para el chip.
  fallbackEnabled: false,
  defaultModel: "",
  activeModel: "",
  activeModelJumped: false,
};

// Contador de tools ejecutadas en la sesion actual (resumen del acordeon
// "Herramientas"). Declarado aca arriba -- aunque solo se usa mas abajo en
// el wrapper de logTool y en clearMessagesDom -- para que ambos lugares lo
// referencien sin depender del orden de ejecucion del resto del archivo.
let _toolsExecutedCount = 0;

// Limite de caché en localStorage para ESTA sesion (aprox., en caracteres
// UTF-16 -- localStorage tipicamente permite ~5MB por origen = ~5,000,000
// caracteres; se usa un limite mas chico y conservador pensado para el
// futuro despliegue como SharePoint Full-Page App, donde el origen puede
// compartirse con otras apps del mismo sitio).
const CACHE_SOFT_LIMIT_CHARS = 300_000;
// Antes era un `const` fijo a state.sessionId -- ahora es una funcion,
// porque cambiar de sesion (ver switchToSession) actualiza state.sessionId
// en caliente sin recargar la pagina, y cada sesion necesita su propia
// clave de historial en localStorage.
function cacheStorageKey() {
  return cacheStorageKeyForSession(state.sessionId);
}

// Escapa a una clave PASADA explicitamente, no a la sesion activa en vivo.
// Necesario para persistir el resultado de un turno en la sesion que LO
// ORIGINO aunque el usuario ya este viendo otra (bug real reportado: al
// cambiar de sesion a mitad de un turno, la respuesta se escribia en la
// sesion equivocada porque el bloque de completado leia state.sessionId
// en vivo -- ver el bloque de completado de renderIncomingStream).
function cacheStorageKeyForSession(sessionId) {
  return `vision.session.${sessionId}`;
}

const messagesEl = document.getElementById("messages");
const todoListEl = document.getElementById("todo-list");
const toolLogEl = document.getElementById("tool-log");
const questionPanelEl = document.getElementById("question-panel");
const devVersionEl = document.getElementById("dev-version");
const formEl = document.getElementById("chat-form");
const inputEl = document.getElementById("chat-input");
const sendBtn = document.getElementById("send-btn");
const connStatusEl = document.getElementById("conn-status");
const connStatusTextEl = document.getElementById("conn-status-text");
const metaModelBtnEl = document.getElementById("meta-model");
const metaModelEl = document.querySelector("#meta-model .meta-val");
const modelDropdownEl = document.getElementById("model-dropdown");
// spec proyecto-por-sesion REQ-006: metaSessionEl es el CHIP entero
// (clickeable, abre el modal de edicion), con 3 sub-elementos internos --
// nombre, Session ID completo (restaurado), y ruta de carpeta asignada.
const metaSessionEl = document.getElementById("meta-session");
const metaSessionNameEl = document.getElementById("meta-session-name");
const metaSessionIdEl = document.getElementById("meta-session-id");
const metaSessionPathEl = document.getElementById("meta-session-path");
const sidebarEl = document.getElementById("sidebar");
const scrimEl = document.getElementById("scrim");
const menuBtnEl = document.getElementById("menu-btn");
const sidebarCloseEl = document.getElementById("sidebar-close");
const cachePctEl = document.getElementById("cache-pct");
const cacheFillEl = document.getElementById("cache-fill");
const cacheMeterEl = document.querySelector(".cache-meter");
const cacheClearBtnEl = document.getElementById("cache-clear-btn");
const dashboardOverlayEl = document.getElementById("dashboard-overlay");
const dashboardTitleEl = document.getElementById("dashboard-title");
const dashboardCloseEl = document.getElementById("dashboard-close");
const dashboardChartWrapEl = document.getElementById("dashboard-chart-wrap");
const dashboardChartCanvasEl = document.getElementById("dashboard-chart");
// Boton unico Enviar/Detener (pedido explicito del usuario): reemplaza los
// 2 botones independientes que habia antes (#send-btn siempre visible +
// #stop-btn aparte). `sendOrStopBtnEl` es el MISMO nodo que antes se leia
// como `sendBtn` -- unificado bajo un solo nombre para reflejar que ahora
// alterna funcion segun `dataset.state` (ver setSendButtonState).
const sendOrStopBtnEl = document.getElementById("send-btn");
const sendOrStopLabelEl = document.getElementById("send-btn-label");
const persistenceBannerEl = document.getElementById("persistence-banner");
const persistenceBannerSettingsLinkEl = document.getElementById("persistence-banner-settings-link");
// spec modo-plan-loop: chip de modo + banner del loop pausado.
const modeChipEl = document.getElementById("mode-chip");
const modeChipValEl = document.getElementById("mode-chip-val");
const loopBannerEl = document.getElementById("loop-banner");
const loopBannerStopEl = document.getElementById("loop-banner-stop");

function setConnStatus(stateName, text) {
  connStatusEl.dataset.state = stateName;
  connStatusTextEl.textContent = text;
}

// -------------------------------------------------------------------------
// Barra de progreso adaptativa (spec barra-progreso-adaptativa, REQ-003/004/005)
//
// El backend emite `progress-estimate` al ARRANCAR cada operacion, con el
// presupuesto estimado en ms (promedio de las ultimas N ejecuciones reales
// de esa misma operacion) y `has_history`. Aca vive el lado visual: un
// unico timer que interpola el % en vivo.
//
// REUBICADA (pedido explicito del usuario): ya no es un elemento fijo del
// command-bar -- ahora se crea/destruye DENTRO del .turn-response-slot
// del turno que esta procesandose (mismo mecanismo que appendToolBadge/
// appendMessage con mountEl), y se re-inserta como ULTIMO hijo de ese slot
// en cada actualizacion (`mountEl.appendChild` sobre un nodo ya adjunto lo
// MUEVE al final) para quedar siempre pegada debajo de lo ya renderizado
// (tool-calls + texto de la respuesta en curso), no arriba de todo.
//
// Criterio de diseno explicito de la spec: "nunca exacta, pero visualmente
// creible". Una barra que salta erraticamente es PEOR que no tener barra,
// asi que:
//   - El avance se calcula con una curva que DESACELERA cerca del final
//     (asintotica), nunca llega sola al 100%: sin evidencia real de que
//     la operacion termino, quedarse en 99% esperando es honesto; llegar
//     a 100% y seguir cargando es exactamente la mentira que genera
//     desconfianza en una barra de progreso.
//   - El % NUNCA retrocede (`Math.max` contra el ultimo valor mostrado),
//     aunque cambie la operacion o el presupuesto a mitad de camino.
//   - Al superar el presupuesto por OVERRUN_FACTOR, se pasa a modo
//     "sobrepasado" (sigue trabajando, mas de lo esperado) en vez de
//     fingir precision -- umbral que la spec §6 dejo abierto para ajustar
//     empiricamente.
// -------------------------------------------------------------------------

// Cada cuanto se repinta la barra. 100ms es suficientemente fluido para el
// ojo y despreciable para el CPU; mas rapido no se percibe.
const PROGRESS_TICK_MS = 100;

// Cuanto se puede pasar del presupuesto estimado antes de admitir que la
// estimacion fallo y pasar a modo "sobrepasado" (spec §6, a ajustar con
// uso real). 2x es el punto donde el usuario ya siente que "tarda mas de
// lo normal", pero todavia no que se colgo.
const OVERRUN_FACTOR = 2;

// Techo del avance estimado: la barra se acerca asintoticamente a esto y
// solo llega a 100% cuando el turno REALMENTE termina.
const PROGRESS_CEILING = 92;

// Umbral por debajo del cual NO se muestra barra (hallazgo empirico de la
// validacion, ver spec §6): tools locales como `read`/`grep`/`todowrite`
// promedian 0-1.3ms de historial real. Mostrar una barra para algo que
// dura menos que un frame produce un parpadeo que se lee como un glitch,
// no como progreso. Solo se aplica cuando HAY historial: sin historial no
// se sabe si va a ser rapida, y ahi si corresponde el modo indeterminado.
const MIN_VISIBLE_BUDGET_MS = 250;

const progressState = { timer: null, startedAt: 0, budgetMs: 0, pct: 0, indeterminate: false };

// Referencia a la barra montada del turno actual -- null cuando no hay
// ninguna operacion en curso (mismo patron que `activeToolRow`).
let activeProgressBar = null;

function getOrCreateProgressBar(mountEl) {
  const target = mountEl || messagesEl;
  if (activeProgressBar && activeProgressBar.root.parentElement === target) {
    // Re-adjuntar (appendChild sobre un nodo ya presente lo MUEVE al
    // final) para que quede siempre debajo del contenido mas reciente
    // (tool-calls y/o texto de la respuesta ya renderizado).
    target.appendChild(activeProgressBar.root);
    return activeProgressBar;
  }
  const root = document.createElement("div");
  root.className = "command-progress hidden";
  root.dataset.state = "idle";
  root.setAttribute("role", "status");
  root.setAttribute("aria-live", "polite");

  const head = document.createElement("div");
  head.className = "progress-head";
  const label = document.createElement("span");
  label.className = "progress-label";
  const pct = document.createElement("span");
  pct.className = "progress-pct";
  head.append(label, pct);

  const track = document.createElement("div");
  track.className = "progress-track";
  const fill = document.createElement("div");
  fill.className = "progress-fill";
  track.appendChild(fill);

  root.append(head, track);
  target.appendChild(root);

  activeProgressBar = { root, labelEl: label, pctEl: pct, fillEl: fill };
  return activeProgressBar;
}

function startProgress({ label, budgetMs, hasHistory }, mountEl) {
  // Operacion conocida y sub-perceptual: no se toca la barra. Si ya hay
  // una visible de una operacion anterior mas lenta, se la deja seguir en
  // vez de pisarla con una que va a durar 1ms.
  if (hasHistory && budgetMs < MIN_VISIBLE_BUDGET_MS) return;

  const bar = getOrCreateProgressBar(mountEl);
  bar.labelEl.textContent = label || "Trabajando…";
  bar.root.classList.remove("hidden");

  // REQ-004: sin ninguna medicion previa de esta operacion no hay base
  // para inventar un %. Se muestra animacion indeterminada + el texto
  // contextual, que es informacion honesta y util igual.
  progressState.indeterminate = !hasHistory;
  progressState.startedAt = performance.now();
  progressState.budgetMs = budgetMs > 0 ? budgetMs : 4000;

  if (progressState.indeterminate) {
    bar.root.dataset.state = "indeterminate";
    bar.pctEl.textContent = "";
    bar.fillEl.style.width = "";
    stopProgressTimer();
    return;
  }

  // Operacion nueva dentro del mismo turno: el % arranca desde donde
  // estaba, no vuelve a cero (retroceder visualmente se lee como un
  // error del sistema, aunque internamente sea una operacion distinta).
  bar.root.dataset.state = "running";
  if (!progressState.timer) {
    progressState.timer = setInterval(tickProgress, PROGRESS_TICK_MS);
  }
  tickProgress();
}

function tickProgress() {
  if (!activeProgressBar) return;
  const elapsed = performance.now() - progressState.startedAt;
  const ratio = elapsed / progressState.budgetMs;

  if (ratio >= OVERRUN_FACTOR) {
    // REQ-003: tarda mucho mas de lo estimado. No se miente con un % que
    // ya se sabe malo: se congela el avance y se avisa. El registro de
    // esta duracion real (que el backend hace igual) hara que la proxima
    // estimacion de esta misma operacion sea mas generosa.
    activeProgressBar.root.dataset.state = "overrun";
    activeProgressBar.pctEl.textContent = "";
    return;
  }

  // Curva asintotica: 1 - e^(-k·t). Avanza rapido al principio (donde el
  // usuario mas necesita feedback de que algo pasa) y desacelera cerca
  // del techo, sin llegar nunca solo al 100%.
  const target = PROGRESS_CEILING * (1 - Math.exp(-2.2 * ratio));
  progressState.pct = Math.max(progressState.pct, target);

  const shown = Math.round(progressState.pct);
  activeProgressBar.fillEl.style.width = `${shown}%`;
  activeProgressBar.pctEl.textContent = `${shown}%`;
}

function stopProgressTimer() {
  if (progressState.timer) {
    clearInterval(progressState.timer);
    progressState.timer = null;
  }
}

function finishProgress() {
  stopProgressTimer();
  // Cierre visual: se completa a 100% y recien ahi se oculta, para que el
  // usuario vea que termino en vez de que la barra desaparezca a mitad.
  if (activeProgressBar && !progressState.indeterminate) {
    activeProgressBar.root.dataset.state = "done";
    activeProgressBar.fillEl.style.width = "100%";
    activeProgressBar.pctEl.textContent = "100%";
  }
  setTimeout(resetProgress, 400);
}

function resetProgress() {
  stopProgressTimer();
  // A diferencia de resetToolRow (que solo limpia la referencia y deja
  // los badges "done" visibles como historial del turno), la barra de
  // progreso SI se remueve del DOM al terminar -- es un indicador
  // transitorio de actividad, no un registro de lo que se hizo.
  if (activeProgressBar) {
    activeProgressBar.root.remove();
    activeProgressBar = null;
  }
  progressState.pct = 0;
  progressState.indeterminate = false;
}

function openSidebar() {
  sidebarEl.classList.add("mobile-open");
  scrimEl.classList.add("active");
}
function closeSidebar() {
  sidebarEl.classList.remove("mobile-open");
  scrimEl.classList.remove("active");
}
menuBtnEl.addEventListener("click", openSidebar);
sidebarCloseEl.addEventListener("click", closeSidebar);
scrimEl.addEventListener("click", closeSidebar);

// spec vision-generico REQ-005: el nombre de la empresa (VISION_COMPANY_NAME del
// .env) va en la pantalla de inicio, el panel lateral, el pie y la pestaña.
// Sin nombre, queda "Asistente Inteligente" / "Vision".
function applyCompanyName(name) {
  const company = (name || "").trim();
  const subtitle = company ? `Asistente Inteligente · ${company}` : "Asistente Inteligente";
  for (const id of ["splash-subtitle", "brand-subtitle"]) {
    const el = document.getElementById(id);
    if (el) el.textContent = subtitle;
  }
  const footer = document.getElementById("footer-company");
  if (footer) footer.textContent = company || "Vision";
  document.title = company ? `Vision · ${company}` : "Vision · Asistente Inteligente";
}

async function checkBackendStatus() {
  setConnStatus("connecting", "Conectando…");
  try {
    const resp = await apiFetch(`${API_BASE}/api/status`);
    if (!resp.ok) throw await httpErrorFromResponse(resp);
    const data = await resp.json();
    // El chip solo se pisa con el default del backend si el usuario todavia
    // no eligio un modelo propio en el selector (ver loadSelectedModel()).
    // spec salto-automatico-modelos: con el salto activo, lo que queda
    // elegido por defecto es "Automático"; si el salto se apago (ej. desde
    // otra pestaña), "Automático" vuelve al default del backend.
    state.defaultModel = data.model || "";
    state.fallbackEnabled = Boolean(data.model_fallback_enabled);
    if (!state.selectedModel) {
      state.selectedModel = state.fallbackEnabled ? AUTO_MODEL : data.model;
      renderModelChip();
    } else if (state.selectedModel === AUTO_MODEL && !state.fallbackEnabled) {
      saveSelectedModel(data.model || "");
    }
    // spec salto-automatico-modelos REQ-010: si se recargo la pagina durante
    // una pausa, el chip vuelve a mostrar el modelo del salto y la vuelta se
    // avisa igual cuando la pausa vence.
    if (state.fallbackEnabled) checkFallbackResume();
    // Ver comentario en index.html junto a #dev-version: hace visible en la
    // UI que codigo backend tiene REALMENTE cargado este proceso, para
    // detectar rapido un .exe portable desactualizado sin comparar
    // timestamps de archivos a mano.
    if (devVersionEl && data.version) devVersionEl.textContent = `v${data.version}`;
    applyCompanyName(data.company_name);
    setConnStatus("online", "Vision en línea");
  } catch (err) {
    setConnStatus("error", "Sin conexión al backend");
  }
}

// -------------------------------------------------------------------------
// Selector de modelo: el chip MODELO del command-bar despliega la lista de
// modelos LLM disponibles en el proveedor de IA (roster en vivo via GET /api/models,
// cacheado 5 min en el backend). La eleccion se persiste en localStorage y
// se manda como `model` en cada POST /api/chat -- si no se eligio ninguno,
// el backend usa su default de .env (LLM_MODEL).
// -------------------------------------------------------------------------
const MODEL_STORAGE_KEY = "vision.selectedModel";
// spec salto-automatico-modelos: "Automático" = el backend elige el modelo
// segun la lista de prioridad de Configuracion (el POST /api/chat va SIN
// `model`) y salta al siguiente si uno llega a su limite o se cae.
const AUTO_MODEL = "auto";

// spec varios-proveedores REQ-004: los modelos viajan como "proveedor/modelo";
// en pantalla se muestran como "Gemini · gemini-2.5-flash".
const PROVIDER_LABELS = {
  anthropic: "Claude",
  gemini: "Gemini",
  deepseek: "DeepSeek",
  groq: "Groq",
  openrouter: "OpenRouter",
  openai: "OpenAI",
};

function prettyModel(modelId) {
  const id = modelId || "";
  const slash = id.indexOf("/");
  if (slash < 0) return id;
  const provider = id.slice(0, slash);
  // El proveedor "clasico" (LLM_*) u "otro compatible": solo el nombre, como antes.
  if (provider === "principal" || provider === "custom") return id.slice(slash + 1);
  const label = PROVIDER_LABELS[provider];
  return label ? `${label} · ${id.slice(slash + 1)}` : id;
}

// Un modelo guardado antes de esta spec no trae prefijo: coincide por nombre.
function isSelectedModel(model) {
  const selected = state.selectedModel || "";
  return model.id === selected || (!selected.includes("/") && model.name === selected);
}

function selectedModelLabel() {
  if (state.selectedModel === AUTO_MODEL) return "Automático";
  return prettyModel(state.selectedModel) || "—";
}

// El chip muestra el modelo que responde DE VERDAD cuando hubo salto: en
// Automatico, "Automático · gpt-6-sol"; con un modelo elegido a mano que
// llego a su limite, "gpt-6-sol (salto)".
function renderModelChip() {
  let text = selectedModelLabel();
  if (state.selectedModel === AUTO_MODEL && state.activeModel) {
    text = `Automático · ${prettyModel(state.activeModel)}`;
  } else if (state.activeModelJumped && state.activeModel) {
    text = `${prettyModel(state.activeModel)} (salto)`;
  }
  metaModelEl.textContent = text;
  metaModelBtnEl.title = state.activeModel ? `Respondiendo con ${prettyModel(state.activeModel)}` : "";
}

// Lo que va en `model` del POST /api/chat: nada en Automatico.
function selectedModelForRequest() {
  return state.selectedModel && state.selectedModel !== AUTO_MODEL ? state.selectedModel : undefined;
}

function loadSelectedModel() {
  try {
    return localStorage.getItem(MODEL_STORAGE_KEY) || "";
  } catch {
    return "";
  }
}

function saveSelectedModel(modelId) {
  state.selectedModel = modelId;
  state.activeModel = "";
  state.activeModelJumped = false;
  renderModelChip();
  try {
    localStorage.setItem(MODEL_STORAGE_KEY, modelId);
  } catch {
    // noop
  }
}

async function openModelDropdown() {
  const isOpen = !modelDropdownEl.classList.contains("hidden");
  if (isOpen) {
    closeModelDropdown();
    return;
  }

  modelDropdownEl.innerHTML = '<div class="model-dropdown-empty">Cargando modelos…</div>';
  modelDropdownEl.classList.remove("hidden");
  metaModelBtnEl.setAttribute("aria-expanded", "true");

  try {
    const resp = await apiFetch(`${API_BASE}/api/models`);
    if (!resp.ok) throw await httpErrorFromResponse(resp);
    const data = await resp.json();
    renderModelOptions(data.models || []);
  } catch (err) {
    const empty = document.createElement("div");
    empty.className = "model-dropdown-empty";
    empty.textContent = `No se pudo cargar la lista de modelos. ${friendlyErrorMessage(err)}`;
    modelDropdownEl.replaceChildren(empty);
  }
}

function closeModelDropdown() {
  modelDropdownEl.classList.add("hidden");
  metaModelBtnEl.setAttribute("aria-expanded", "false");
}

function renderModelOptions(models) {
  modelDropdownEl.innerHTML = "";
  if (models.length === 0) {
    modelDropdownEl.innerHTML = '<div class="model-dropdown-empty">Sin modelos disponibles</div>';
    return;
  }
  // spec salto-automatico-modelos: primera opcion cuando el salto esta activo.
  if (state.fallbackEnabled) {
    const autoBtn = document.createElement("button");
    autoBtn.type = "button";
    autoBtn.className = "model-option" + (state.selectedModel === AUTO_MODEL ? " active" : "");
    autoBtn.setAttribute("role", "option");
    autoBtn.dataset.model = AUTO_MODEL;
    const autoId = document.createElement("span");
    autoId.className = "model-id";
    autoId.textContent = "Automático";
    const autoSub = document.createElement("span");
    autoSub.className = "model-meta";
    autoSub.textContent = "según tu lista de prioridad";
    autoBtn.append(autoId, autoSub);
    autoBtn.onclick = () => {
      saveSelectedModel(AUTO_MODEL);
      closeModelDropdown();
    };
    modelDropdownEl.appendChild(autoBtn);
  }
  // spec varios-proveedores REQ-004: un grupo por proveedor.
  let lastGroup = null;
  models.forEach((m) => {
    const group = m.provider_label || "";
    if (group && group !== lastGroup) {
      const header = document.createElement("div");
      header.className = "model-group-label";
      header.textContent = group;
      modelDropdownEl.appendChild(header);
      lastGroup = group;
    }
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "model-option" + (isSelectedModel(m) ? " active" : "");
    btn.setAttribute("role", "option");

    const idSpan = document.createElement("span");
    idSpan.className = "model-id";
    idSpan.textContent = m.name || m.id;
    btn.appendChild(idSpan);

    const sub = [m.free ? "gratis" : null, m.hosted_on_prem ? "on-prem" : null, (m.aliases || [])[0]]
      .filter(Boolean)
      .join(" · ");
    if (sub) {
      const subSpan = document.createElement("span");
      subSpan.className = "model-meta";
      subSpan.textContent = sub;
      btn.appendChild(subSpan);
    }

    btn.onclick = () => {
      saveSelectedModel(m.id);
      closeModelDropdown();
    };
    modelDropdownEl.appendChild(btn);
  });
}

metaModelBtnEl.addEventListener("click", openModelDropdown);
document.addEventListener("click", (e) => {
  if (!modelDropdownEl.contains(e.target) && e.target !== metaModelBtnEl && !metaModelBtnEl.contains(e.target)) {
    closeModelDropdown();
  }
});

// -------------------------------------------------------------------------
// Persistencia local (localStorage): pensado para el futuro despliegue como
// SharePoint Full-Page App (sin backend de sesion propio accesible desde
// cualquier equipo) -- el historial de ESTA conversacion vive en el
// navegador, no en el servidor. `chatHistory` guarda los mensajes ya
// renderizados (rol + texto) para poder restaurarlos si se recarga la
// pagina en la misma sesion de navegador.
// -------------------------------------------------------------------------
const chatHistory = loadChatHistory();

function loadChatHistory() {
  return loadChatHistoryForSession(state.sessionId);
}

// Carga el historial de localStorage para UNA sesion puntual (no la
// sesion activa) -- parte del fix del bug de cambiar de sesion a mitad de
// turno: al completarse el turno con la sesion ya cambiada, el bloque de
// completado necesita leer/escribir el slot de la sesion que ORIGINO el
// turno, no el de la sesion que hay en pantalla en ese momento.
function loadChatHistoryForSession(sessionId) {
  try {
    const raw = localStorage.getItem(cacheStorageKeyForSession(sessionId));
    return raw ? JSON.parse(raw) : [];
  } catch {
    return [];
  }
}

function persistChatHistory() {
  persistHistoryForSession(state.sessionId, chatHistory);
}

// Persiste `history` en el slot de localStorage de UNA sesion puntual
// (no necesariamente la activa) -- centro del fix del bug de cambiar de
// sesion a mitad de turno (ver loadChatHistoryForSession arriba).
function persistHistoryForSession(sessionId, history) {
  try {
    localStorage.setItem(cacheStorageKeyForSession(sessionId), JSON.stringify(history));
  } catch (err) {
    // localStorage lleno (QuotaExceededError) -- no rompe el chat, solo
    // deja de persistir hasta que el usuario libere cache.
    console.warn("No se pudo persistir en localStorage:", err);
  }
  updateCacheMeter();
}

function updateCacheMeter() {
  let totalChars = 0;
  try {
    for (let i = 0; i < localStorage.length; i++) {
      const key = localStorage.key(i);
      if (!key || !key.startsWith("vision.")) continue;
      totalChars += (localStorage.getItem(key) || "").length;
    }
  } catch {
    // localStorage inaccesible (ej. modo privado estricto) -- medidor en 0
  }

  const pct = Math.min(100, Math.round((totalChars / CACHE_SOFT_LIMIT_CHARS) * 100));
  cachePctEl.textContent = `${pct}%`;
  cacheFillEl.style.width = `${pct}%`;
  cacheMeterEl.classList.toggle("warn", pct >= 60 && pct < 85);
  cacheMeterEl.classList.toggle("critical", pct >= 85);
}

function clearSessionCache() {
  try {
    localStorage.removeItem(cacheStorageKey());
  } catch {
    // noop
  }
  chatHistory.length = 0;
  updateCacheMeter();
  appendMessage("system-note", "Caché de esta conversación liberada.");
}
cacheClearBtnEl.addEventListener("click", clearSessionCache);

// Solo los mensajes del asistente se interpretan como Markdown (headers,
// negrita, listas, bloques de codigo, tablas, links...) -- ver
// js/markdown.js. Los del usuario, notas de sistema y errores se muestran
// como texto plano tal cual, sin interpretar ningun simbolo.
const MARKDOWN_ROLES = new Set(["assistant"]);

// =========================================================================
// Timestamps de mensajes (spec timestamps-mensajes, REQ-001 a REQ-004)
//
// Timestamp del lado del CLIENTE (Date.now()), capturado en el momento
// exacto en que appendMessage() crea la burbuja -- funciona siempre, con
// o sin persistencia SQLite disponible (ver Decisiones registradas de la
// spec: sincronizar con el created_at real del backend queda fuera de
// alcance de esta iteracion).
// =========================================================================

/**
 * REQ-002: 'HH:MM' si `ts` es de HOY, 'DD/MM HH:MM' si es de otro dia.
 * @param {number} ts epoch ms
 */
function formatMessageTime(ts) {
  const d = new Date(ts);
  const now = new Date();
  // REQ-002: formato de 24hs explicito -- `es-AR` sin `hour12:false` puede
  // devolver "10:41 p. m." segun la configuracion regional del navegador
  // (bug real detectado en validacion E2E), en vez del "HH:MM" pedido.
  const hhmm = d.toLocaleTimeString("es-AR", { hour: "2-digit", minute: "2-digit", hour12: false });
  const isToday = d.toDateString() === now.toDateString();
  if (isToday) return hhmm;
  const ddmm = d.toLocaleDateString("es-AR", { day: "2-digit", month: "2-digit" });
  return `${ddmm} ${hhmm}`;
}

/**
 * REQ-001/004: arma el <span> de hora para una burbuja, o `null` si no
 * hay timestamp (mensaje viejo guardado antes de esta feature -- no se
 * inventa un dato que no existe).
 * @param {number|undefined} ts
 */
function buildResponseTypeBadge(type) {
  const el = document.createElement("div");
  el.className = "msg-response-type";
  // El backend manda la etiqueta cruda del marcador `[[TIPO: xxx]]`
  // (minusculas, sin tildes, ver loop.py) -- normalizar antes de comparar,
  // el label mostrado sigue siendo el texto prolijo de siempre (bug real
  // detectado en validacion QA: comparar contra "Conversacional"/etc con
  // mayuscula+tilde nunca matcheaba, el badge quedaba SIEMPRE en "Sin
  // Clasificacion").
  const normalized = (type || "").toLowerCase().trim();
  let icon = "";
  let label;
  if (normalized === "conversacional") {
    label = "Conversacional";
    icon = `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"></path></svg>`;
  } else if (normalized === "analitica_simple") {
    label = "Analítica Simple";
    icon = `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="20" x2="18" y2="10"></line><line x1="12" y1="20" x2="12" y2="4"></line><line x1="6" y1="20" x2="6" y2="14"></line></svg>`;
  } else if (normalized === "planificable") {
    label = "Planificable";
    icon = `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"></circle><polyline points="12 6 12 12 16 14"></polyline></svg>`;
  } else {
    label = "Sin Clasificación";
    icon = `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"></circle><line x1="12" y1="8" x2="12" y2="12"></line><line x1="12" y1="16" x2="12.01" y2="16"></line></svg>`;
  }
  el.innerHTML = `${icon}<span>${label}</span>`;
  return el;
}

function buildMessageTimeEl(ts) {
  if (!ts) return null;
  const span = document.createElement("span");
  span.className = "msg-time";
  span.textContent = formatMessageTime(ts);
  span.title = new Date(ts).toLocaleString("es-AR", { hour12: false });
  return span;
}

// =========================================================================
// Copiar mensajes (spec copiar-mensajes, REQ-001 a REQ-004)
//
// Tres niveles: (1) texto plano de cualquier mensaje (user/assistant),
// (2) markdown crudo (solo assistant, unico rol con markdown real), y
// (3) bloques de código individuales dentro de una respuesta. Los 3
// comparten `copyToClipboard` para el mismo patrón de confirmación
// visual (ícono → ✓ por 1.5s → vuelve al ícono original, REQ-004).
// =========================================================================
const COPY_FEEDBACK_MS = 1500;

// =========================================================================
// Reproducir respuesta por voz, completa o por rango (spec
// reproducir-respuesta-rangos, REQ-001 a REQ-007). El boton 🔊 usa el motor
// de voz configurado (speak()/tts.js: la Web Speech API nativa del navegador).
// Los marcadores de banda (inicio/fin) se ponen con click sobre el texto.
// =========================================================================
const _msgRangeState = new WeakMap(); // key: elemento .msg-text -> { start, end }
let _activePlayBtn = null; // boton 🔊 que esta reproduciendo AHORA (uno solo a la vez)

function getMsgRange(textEl) {
  if (!_msgRangeState.has(textEl)) _msgRangeState.set(textEl, { start: null, end: null });
  return _msgRangeState.get(textEl);
}

// Devuelve el indice (en caracteres de `el.textContent`) donde cayo el click.
function charOffsetAtClick(el, evt) {
  const sel = window.getSelection();
  if (!sel || !sel.rangeCount) return 0;
  const range = sel.getRangeAt(0);
  const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
  let offset = 0;
  let node;
  while ((node = walker.nextNode())) {
    if (node === range.startContainer) return offset + range.startOffset;
    offset += node.textContent.length;
  }
  return offset;
}

// REQ-003: click sobre la respuesta alterna las banderas de inicio/fin.
function handleMessageTextClick(evt, textEl) {
  if (evt.target.closest("a")) return; // REQ-006: no interceptar enlaces
  const st = getMsgRange(textEl);
  const off = charOffsetAtClick(textEl, evt);
  if (st.start === null) {
    st.start = off; // 1er click: bandera de inicio
  } else if (st.end === null) {
    if (off === st.start) st.start = null; // click de nuevo en el inicio lo quita
    else st.end = off; // 2do click distinto: bandera de fin
  } else {
    st.start = off; // ya habia rango -> reinicia gama desde este punto
    st.end = null;
  }
  updateMsgRangeVisual(textEl, st);
}

function updateMsgRangeVisual(textEl, st) {
  const msg = textEl.closest(".msg");
  if (!msg) return;
  msg.classList.toggle("range-start", st.start !== null);
  msg.classList.toggle("range-end", st.end !== null);
  let hint = msg.querySelector(".msg-range-hint");
  if (!hint) {
    hint = document.createElement("div");
    hint.className = "msg-range-hint";
    msg.appendChild(hint);
  }
  if (st.start === null && st.end === null) {
    hint.textContent = "";
    hint.style.display = "none";
  } else if (st.end === null) {
    hint.textContent = "▶ inicio marcado — hacé click para marcar fin (o click en inicio para quitarlo)";
    hint.style.display = "block";
  } else {
    hint.textContent = "▶► rango listo — tocá 🔊 para reproducir solo este fragmento";
    hint.style.display = "block";
  }
}

// Dibuja el boton en play/stop y su icono (bocinita). `playing` alterna la
// apariencia para que se sepa que volver a tocar detiene (REQ-007).
function setPlayBtnState(btn, playing) {
  btn.dataset.playing = playing ? "true" : "false";
  btn.textContent = playing ? "⏹" : "🔊";
  btn.title = playing ? "Detener reproducción" : "Reproducir respuesta (o un rango si marcas inicio/fin)";
  btn.classList.toggle("playing", playing);
}

// -------------------------------------------------------------------------
// Bucle de reproduccion (pedido explicito del usuario, 2026-09-12): un
// toggle "Repetir en bucle" (icono 🔁, estilo reproductor) en el panel de
// accesos rapidos. Cuando esta ON y se reproduce un fragmento con la
// bocinita 🔊, al terminar vuelve a empezar desde cero, indefinidamente,
// hasta que el usuario DETENGA (tocar 🔊 de nuevo) o APAGUE el bucle. Con
// el toggle OFF, reproduce una sola vez (comportamiento normal).
// Preferencia persistida en localStorage; estado de corrida (`_looping`/
// `_loopText`) en memoria.
// -------------------------------------------------------------------------
const TTS_LOOP_KEY = "vision.tts.loop";

function _loadLoopPref() {
  try { return localStorage.getItem(TTS_LOOP_KEY) === "true"; } catch { return false; }
}
let _loopEnabled = _loadLoopPref();
let _looping = false;
let _loopText = null;

// Devuelve el texto PLAIN a hablar (completo si no hay banderas, o el
// rango `start..end` si hay ambas), o `null` si falta una bandera (el
// caller muestra el aviso). NO llama a speak() aqui: el dueño decide
// usar el texto (y si entra en bucle). Los indices se miden sobre
// `textEl.textContent`, asi el slice casa con lo que se reproducira.
function speakMessageRange(textEl, rawText) {
  const st = _msgRangeState.get(textEl) || { start: null, end: null };
  const full = textEl.textContent || "";
  if (st.start === null && st.end === null) {
    return full; // REQ-002: sin banderas -> texto completo
  } else if (st.start !== null && st.end !== null) {
    const a = Math.min(st.start, st.end);
    const b = Math.max(st.start, st.end);
    return full.slice(a, b); // REQ-004: solo el rango marcado
  } else {
    return null; // REQ-005: solo una bandera -> no reproduce, avisa
  }
}

// Habla `text` desde la bocinita. Si el bucle esta ON, lo registra y la
// reproduccion se repite hasta que se detenga o se apague el bucle.
function _speakFromPlayBtn(text, btn) {
  stopActivePlayback({ immediate: true }); // frena cualquier otra reproduccion previa, arranca esta ya mismo
  if (_loopEnabled) {
    _looping = true;
    _loopText = text;
  }
  speak(text);
  setPlayBtnState(btn, true);
  _activePlayBtn = btn;
}

// REQ-001 (spec fix-boton-play-stop), bug real 2026-09-27: la lectura
// AUTOMATICA de una respuesta ("Leer en voz alta") no quedaba atada a su
// boton -- mientras Vision hablaba el boton seguia en 🔊, y tocarlo para
// detener arrancaba OTRA lectura desde el principio (cancel + speak) en vez de
// cortar. Ahora, cuando esa lectura empieza a sonar, su boton pasa a ⏹ y queda
// como el activo: tocarlo corta la voz por completo (stopActivePlayback), y
// tocarlo de nuevo lee desde el inicio o solo el rango marcado.
function showMessageSpeaking(msgEl) {
  const btn = msgEl ? msgEl.querySelector(".msg-play-btn") : null;
  if (!btn || !btn.isConnected) return; // el chat se re-dibujo (ej. cambio de sesion)
  setPlayBtnState(btn, true);
  _activePlayBtn = btn;
}

// REQ-002 (spec fix-boton-play-stop): bug real reportado por el usuario --
// al presionar ⏹ el icono pasaba a 🔊 (detenido) ANTES de que el audio
// realmente se cortara, mas notorio en respuestas largas. Causa raiz:
// cancelSpeech() (tts.js) notifica el fin de la reproduccion de forma
// OPTIMISTA e inmediata (necesario para otros flujos -- Stop de turno,
// apagar TTS, mute -- que no deben tener ninguna demora, ver REQ-005), pero
// `speechSynthesis.cancel()` de la Web Speech API no garantiza corte
// instantaneo del audio en todos los navegadores/utterances. Se verifica el
// estado REAL de `speechSynthesis.speaking` con un polling corto antes de
// soltar el icono del boton de reproduccion -- si se agota el tope de
// espera igual se suelta (mejor un "listo" tardio que un boton trabado para
// siempre). Acotado a este camino puntual, sin tocar cancelSpeech() en si.
const STOP_POLL_INTERVAL_MS = 40;
const STOP_POLL_MAX_MS = 1500;

function waitForSpeechReallyStopped(cb) {
  // spec voz-fish-audio REQ-010: isSpeaking() tambien ve el audio de los
  // motores del backend, no solo la voz del navegador.
  if (!isSpeaking()) {
    cb();
    return;
  }
  const deadline = performance.now() + STOP_POLL_MAX_MS;
  const check = () => {
    if (!isSpeaking() || performance.now() >= deadline) {
      cb();
      return;
    }
    setTimeout(check, STOP_POLL_INTERVAL_MS);
  };
  setTimeout(check, STOP_POLL_INTERVAL_MS);
}

// `immediate`: true cuando el caller va a arrancar OTRA reproduccion justo
// despues (ej. _speakFromPlayBtn re-usando el mismo boton, o el toggle de
// rango) -- ahi el icono debe pasar a "reproduciendo" de inmediato sin
// esperar nada, esperar la confirmacion real solo estorbaria (condicion de
// carrera: el audio nuevo ya arranco para cuando el polling de abajo
// revisaria `speechSynthesis.speaking`, dando un falso "sigue sonando").
// false (default) es el caso REQ-002: el usuario pidio DETENER y no sigue
// nada despues -- ahi si conviene esperar la confirmacion real.
function stopActivePlayback({ immediate = false } = {}) {
  _looping = false; // un stop rompe el bucle
  _loopText = null;
  const btnToRelease = _activePlayBtn;
  // Se libera `_activePlayBtn` ANTES de cancelar: el `onSpeechEnd` generico
  // (mas abajo) tambien dispara aca, de forma sincronica, via cancelSpeech()
  // -- al estar ya en null, ese handler no vuelve a tocar el icono.
  _activePlayBtn = null;
  // cancelSpeech() frena la voz en curso (Web Speech) sin mas preambulo.
  cancelSpeech();
  if (!btnToRelease) return;
  if (immediate) {
    setPlayBtnState(btnToRelease, false);
    return;
  }
  // REQ-002: el icono queda en ⏹ hasta que speechSynthesis confirma el
  // corte real (o vence el tope de espera) -- nunca "miente" mostrando 🔊
  // mientras el audio todavia suena.
  waitForSpeechReallyStopped(() => setPlayBtnState(btnToRelease, false));
}

function flashMissingFlag(btn, which) {
  btn.textContent = "⚠";
  btn.title = `Falta bandera de ${which}`;
  setTimeout(() => {
    if (btn.dataset.playing !== "true") setPlayBtnState(btn, false);
  }, 1500);
}

// REQ-002/004/005/007 + bucle: play/stop alternado, una sola reproduccion a
// la vez, y repetir en bucle si el toggle esta ON.
function handlePlayMessageClick(btn, textEl, rawText) {
  if (btn.dataset.playing === "true") {
    // REQ-007: represionar detiene (y rompe el bucle).
    stopActivePlayback();
    return;
  }
  if (!isTtsEnabled()) {
    flashMissingFlag(btn, "fin (y activá 'Leer en voz alta' en Configuración > Audio)");
    return;
  }
  // BUG real (2026-09-12, repetir con 🔊): se llamaba a stopActivePlayback()
  // DESPUES de speakMessageRange(), que ya habia arrancado la voz -- el
  // cancelSpeech() interno mataba la reproduccion recien lanzada. El orden
  // correcto: PRIMERO detener, LUEGO hablar (lo hace _speakFromPlayBtn).
  const textToSpeak = speakMessageRange(textEl, rawText);
  if (textToSpeak === null) {
    const st = _msgRangeState.get(textEl) || { start: null, end: null };
    flashMissingFlag(btn, st.end === null ? "fin" : "inicio");
    return;
  }
  if (!textToSpeak.trim()) return; // nada que sonar, no marcar como reproduciendo
  _speakFromPlayBtn(textToSpeak, btn);
}

// Al terminar naturalmente (o cancelarse) la voz: si hay bucle activo se
// repite el fragmento (el boton sigue "playing"); si no, se suelta el boton.
onSpeechEnd(() => {
  if (_looping && _loopText) {
    speak(_loopText); // bucle: vuelve a empezar, sin soltar el boton
    return;
  }
  if (_activePlayBtn) {
    setPlayBtnState(_activePlayBtn, false);
    _activePlayBtn = null;
  }
});

// REQ-003 (spec reproducir-respuesta-rangos): delegacion de click para las
// banderas de inicio/fin sobre el texto de respuestas del asistente. Se hace
// a nivel DOCUMENTO (y no por elemento) porque el streaming re-renderiza
// `innerHTML` de `.msg-text` en cada delta -- un listener por elemento se
// perderia; la delegacion sobrevive a cualquier re-render. Solo reacciona
// sobre `.msg-text` dentro de un `.msg.assistant`; clicks en botones del
// `.msg-copy-controls` caen fuera de `.msg-text` y no se marcan.
document.addEventListener("click", (evt) => {
  const textEl = evt.target.closest(".msg-text");
  if (!textEl) return;
  const msg = textEl.closest(".msg");
  if (!msg || !msg.classList.contains("assistant")) return;
  handleMessageTextClick(evt, textEl);
});

async function copyToClipboard(text, btnEl, originalLabel) {
  try {
    await navigator.clipboard.writeText(text);
    btnEl.textContent = "✓";
    btnEl.classList.add("copied");
    setTimeout(() => {
      btnEl.textContent = originalLabel;
      btnEl.classList.remove("copied");
    }, COPY_FEEDBACK_MS);
  } catch {
    // navigator.clipboard requiere contexto seguro -- localhost lo
    // cumple, pero se deja un fallback silencioso por si el navegador
    // deniega el permiso puntualmente (no rompe el resto de la UI).
    btnEl.textContent = "✗";
    setTimeout(() => { btnEl.textContent = originalLabel; }, COPY_FEEDBACK_MS);
  }
}

/**
 * REQ-001/002 (spec copiar-mensajes) + REQ-001 (spec eliminar-respuestas):
 * arma el bloque de botones de UN mensaje -- copiar (texto plano/markdown)
 * y, para respuestas del asistente, tambien eliminar.
 * `role`: 'user' o 'assistant' -- el markdown crudo (REQ-002) y el borrado
 * quirurgico solo tienen sentido para 'assistant'.
 * `userTurnId`: id del turno de usuario que origino esta respuesta (ver
 * turnContainers/renderIncomingStream) -- requerido para el boton
 * eliminar, ya que session.messages no tiene ids propios de assistant
 * (se identifica la respuesta por el turno que la origino, ver spec).
 */
function buildMessageCopyControls(role, rawText, userTurnId = null) {
  const wrap = document.createElement("div");
  wrap.className = "msg-copy-controls";

  if (role === "error") {
    // Mensajes de error/sistema (ej. "Error de conexion: network error") --
    // mismo menu que una respuesta pero adaptado: copiar, eliminar y
    // "recargar" (= revalidar si el problema persiste; si se resolvio se
    // muestra en verde "Solucionado: ..."). Pedido explicito del usuario.
    const copyBtn = document.createElement("button");
    copyBtn.type = "button";
    copyBtn.className = "msg-copy-btn";
    copyBtn.title = "Copiar texto";
    copyBtn.textContent = "📋";
    copyBtn.onclick = () => copyToClipboard(rawText, copyBtn, "📋");
    wrap.appendChild(copyBtn);

    const reloadBtn = document.createElement("button");
    reloadBtn.type = "button";
    reloadBtn.className = "msg-copy-btn btn-reset";
    reloadBtn.title = "Revalidar (comprobar si el error persiste)";
    reloadBtn.textContent = "↻";
    reloadBtn.onclick = () => {
      const msg = wrap.closest(".msg");
      revalidateErrorMessage(msg, rawText, reloadBtn);
    };
    wrap.appendChild(reloadBtn);

    const deleteBtn = document.createElement("button");
    deleteBtn.type = "button";
    deleteBtn.className = "msg-copy-btn danger";
    deleteBtn.title = "Eliminar este mensaje";
    deleteBtn.textContent = "🗑";
    deleteBtn.onclick = () => {
      const msg = wrap.closest(".msg");
      if (msg) msg.remove(); // los errores no se persisten; solo quitar del DOM
    };
    wrap.appendChild(deleteBtn);

    return wrap;
  }

  if (role === "assistant") {
    // REQ-001 (spec reproducir-respuesta-rangos): boton 🔊 play/stop. Solo
    // en respuestas del asistente (las unicas con contenido "narrable").
    const playBtn = document.createElement("button");
    playBtn.type = "button";
    playBtn.className = "msg-copy-btn msg-play-btn";
    setPlayBtnState(playBtn, false);
    playBtn.onclick = () => {
      const msg = wrap.closest(".msg");
      const textEl = msg ? msg.querySelector(".msg-text") : null;
      if (textEl) handlePlayMessageClick(playBtn, textEl, rawText);
    };
    wrap.appendChild(playBtn);
  }

  const plainBtn = document.createElement("button");
  plainBtn.type = "button";
  plainBtn.className = "msg-copy-btn";
  plainBtn.title = "Copiar texto";
  plainBtn.textContent = "📋";
  plainBtn.onclick = async () => {
    // REQ-001/002/005 (spec copiar-formulas-unicode): solo en respuestas
    // del asistente -- las formulas LaTeX las genera el LLM, un mensaje de
    // usuario (rol distinto de "assistant") se copia siempre tal cual.
    const plain = role === "assistant" ? await convertLatexFormulasToUnicode(toPlainText(rawText)) : rawText;
    copyToClipboard(plain, plainBtn, "📋");
  };
  wrap.appendChild(plainBtn);

  if (role === "assistant") {
    const mdBtn = document.createElement("button");
    mdBtn.type = "button";
    mdBtn.className = "msg-copy-btn";
    mdBtn.title = "Copiar markdown crudo (fórmulas convertidas a Unicode legible)";
    mdBtn.textContent = "⌗";
    // REQ-002: ⌗ tambien convierte -- ya NO copia el LaTeX crudo original
    // (decision explicita del usuario: ambos botones dan texto legible).
    mdBtn.onclick = async () => copyToClipboard(await convertLatexFormulasToUnicode(rawText), mdBtn, "⌗");
    wrap.appendChild(mdBtn);

    // REQ-001 (spec eliminar-respuestas): solo si se conoce el turno de
    // usuario que origino esta respuesta -- sin eso no hay forma de
    // decirle al backend CUAL respuesta borrar.
    if (userTurnId) {
      const regenerateBtn = document.createElement("button");
      regenerateBtn.type = "button";
      regenerateBtn.className = "msg-copy-btn btn-reset";
      regenerateBtn.title = "Recargar respuesta";
      regenerateBtn.textContent = "↻";
      regenerateBtn.onclick = () => regenerateResponse(userTurnId, wrap.closest(".msg"));
      wrap.appendChild(regenerateBtn);

      const deleteBtn = document.createElement("button");
      deleteBtn.type = "button";
      deleteBtn.className = "msg-copy-btn danger";
      deleteBtn.title = "Eliminar esta respuesta";
      deleteBtn.textContent = "🗑";
      deleteBtn.onclick = () => deleteAssistantResponse(userTurnId, wrap.closest(".msg"));
      wrap.appendChild(deleteBtn);
    }
  }

  return wrap;
}

/**
 * REQ-003: agrega un botón de copiar a CADA bloque de código
 * `.md-codeblock` dentro de un mensaje ya renderizado -- se llama después
 * de asignar `innerHTML = renderMarkdown(...)`, nunca antes.
 */
function attachCodeBlockCopyButtons(container) {
  for (const block of container.querySelectorAll(".md-codeblock")) {
    if (block.querySelector(".code-copy-btn")) continue; // ya tiene boton (re-render de un delta)
    const rawCode = (block.dataset.rawCode || "").replace(/&#10;/g, "\n");
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "code-copy-btn";
    btn.title = "Copiar código";
    btn.textContent = "📋";
    btn.onclick = () => copyToClipboard(rawCode, btn, "📋");
    block.appendChild(btn);
  }
}

// Cada mensaje de USUARIO arma un "turno" propio: un contenedor
// (.turn-container) que agrupa la burbuja del usuario + un slot vacio
// (.turn-response-slot) donde despues se van a insertar SUS tool-calls y
// SU respuesta del asistente. Esto es lo que garantiza que, aunque haya
// varios mensajes encolados (A, B, C) y sus respuestas lleguen en
// cualquier orden relativo entre si, la respuesta de B siempre aparece
// pegada debajo de B -- nunca al final de la lista completa, ni mezclada
// con el turno de otro mensaje (bug real reportado por el usuario: los
// mensajes en cola quedaban "anclados abajo" separados de su respuesta).
//
// `turnContainers`: Map messageId -> { container, responseSlot } para que
// renderIncomingStream/appendToolBadge sepan DONDE insertar el contenido
// de cada turno especifico en vez de asumir siempre "al final de #messages".
const turnContainers = new Map();

// REQ-004/005/007/009: cada mensaje de USUARIO se persiste con un
// `messageId` propio (uuid generado por el frontend) para poder despues
// pedirle al backend que lo detenga/edite/elimine/reordene sin ambiguedad
// -- ver ChatRequest.message_id en app/main.py. Mensajes de otros roles
// (assistant/system-note/error) no lo necesitan.
function appendMessage(role, text, { persist = true, messageId = null, mountEl = null, time = null, images = null, responseType = null, technical = null, errorKind = null } = {}) {
  // REQ-001/003 (spec timestamps-mensajes): se captura UNA sola vez aca
  // (no en cada punto donde se hace chatHistory.push mas abajo) para que
  // el mismo valor se use tanto en el <span> visible como en lo que se
  // persiste -- si el caller viene reconstruyendo un mensaje ya existente
  // (ej. al recargar la pagina), pasa su `time` original en vez de generar
  // uno nuevo.
  const msgTime = time || (persist ? Date.now() : null);

  const div = document.createElement("div");
  div.className = `msg ${role}`;

  // REQ-007 (spec contexto-multimodal): miniaturas ARRIBA del texto,
  // dentro de la MISMA burbuja -- clickeables para ver a tamano completo.
  // Solo aplica a mensajes de usuario (el asistente no "adjunta" imagenes,
  // solo las recibe).
  if (role === "user" && images && images.length > 0) {
    const imagesRow = document.createElement("div");
    imagesRow.className = "msg-images-row";
    for (const dataUri of images) {
      const thumb = document.createElement("img");
      thumb.src = dataUri;
      thumb.className = "msg-image-thumb";
      thumb.onclick = () => window.open(dataUri, "_blank");
      imagesRow.appendChild(thumb);
    }
    div.appendChild(imagesRow);
  }

  // BUG PREEXISTENTE corregido de paso (detectado al construir la spec
  // contexto-multimodal): buildMessageCopyControls/buildMessageTimeEl ya
  // se agregaban como hijos DIRECTOS de `div` (mismo nodo que
  // startEditingUserMessage lee/sobreescribe via `bubble.textContent`) --
  // sin aislar el texto en su propio nodo, editar un mensaje de usuario
  // ya habria capturado tambien "📋⌗🗑23:05" (el texto de los controles y
  // el timestamp) como parte del texto editable. `.msg-text` aisla el
  // texto real; startEditingUserMessage ahora apunta a ESE span.
const textSpan = document.createElement("span");
  textSpan.className = "msg-text";
  if (MARKDOWN_ROLES.has(role)) {
    textSpan.innerHTML = renderMarkdown(text);
    div.appendChild(textSpan);
    attachCodeBlockCopyButtons(div); // REQ-003: boton propio por bloque de codigo
    // Pedido explicito del usuario: formulas matematicas/quimicas/
    // estadisticas/griegas deben verse EXACTAS, no como LaTeX crudo --
    // este mensaje ya esta completo (no streaming), tipsetea de inmediato.
    typesetMath(div, { immediate: true });
  } else {
    textSpan.textContent = text;
    div.appendChild(textSpan);
  }

  // Errores entendibles (2026-09-24): el texto principal es para cualquier
  // persona; el código de soporte / HTTP / hora queda plegado aparte.
  if (role === "error") {
    div.dataset.errorKind = errorKind || "other";
    if (technical) {
      const details = document.createElement("details");
      details.className = "msg-error-technical";
      const summary = document.createElement("summary");
      summary.textContent = "Detalle técnico";
      const code = document.createElement("code");
      code.textContent = technical;
      details.append(summary, code);
      div.appendChild(details);
    }
  }

  // REQ-001/002 (spec copiar-mensajes): solo user/assistant tienen
  // contenido "propio" que tenga sentido copiar -- system-note/error son
  // avisos del sistema, no contenido de la conversacion. Para 'assistant'
  // reconstruido desde chatHistory (recarga de pagina), `messageId` no
  // esta disponible aca -- el boton eliminar simplemente no aparece en
  // ese caso (mismo criterio ya usado para mensajes de usuario viejos sin
  // controles, ver buildUserTurnControls).
if (role === "error" || role === "assistant") {
    // 'user' se excluye a proposito: su boton copiar vive en
    // `.user-turn-controls` (grupo editar/eliminar), no dentro de la burbuja
    // (Pedido del usuario 2026-09 -- antes quedaba flotando en la pregunta).
    div.appendChild(
      buildMessageCopyControls(role, text, role === "assistant" ? messageId : null)
    );
  }

  // REQ-001/004 (spec timestamps-mensajes): solo user/assistant -- los
  // avisos del sistema (system-note/error) no son "mensajes de la
  // conversacion" con hora propia relevante para el usuario.
  const timeEl = (role === "user" || role === "assistant") ? buildMessageTimeEl(msgTime) : null;
  if (timeEl) div.appendChild(timeEl);
  if (role === "assistant") {
    // REQ-003/004 (spec clasificador-tipo-respuesta): el badge se muestra
    // SIEMPRE en toda respuesta del asistente; si el modelo no emitio el
    // marcador, sale "Sin Clasificacion" (visible, no ocultar).
    div.insertBefore(buildResponseTypeBadge(responseType || null), textSpan);
  }

  // `mountEl`: usado por la respuesta del asistente para insertarse DENTRO
  // del .turn-response-slot del turno que la origino, en vez de siempre
  // al final de #messages (ver turnContainers). Si se pasa, tiene
  // prioridad sobre el comportamiento por rol de mas abajo.
  if (mountEl) {
    mountEl.appendChild(div);
    messagesEl.scrollTop = messagesEl.scrollHeight;
    if (persist) {
      chatHistory.push({ role, text, messageId: messageId || undefined, time: msgTime || undefined });
      persistChatHistory();
    }
    return div;
  }

  if (role === "user") {
    // Envuelve la burbuja en .user-turn-wrap para poder mostrar el badge
    // "En cola" y los controles editar/eliminar/reordenar sin tocar el
    // estilo de la burbuja en si (ver .user-turn-wrap en styles.css).
const wrap = document.createElement("div");
    wrap.className = "user-turn-wrap";
    if (messageId) wrap.dataset.messageId = messageId;
    wrap.appendChild(div);
    wrap.appendChild(buildUserTurnControls(messageId, text));

    const container = document.createElement("div");
    container.className = "turn-container";
    if (messageId) container.dataset.messageId = messageId;
    const responseSlot = document.createElement("div");
    responseSlot.className = "turn-response-slot";
    container.append(wrap, responseSlot);

    messagesEl.appendChild(container);
    messagesEl.scrollTop = messagesEl.scrollHeight;
    if (messageId) turnContainers.set(messageId, { container, responseSlot });

    if (persist) {
      chatHistory.push({ role, text, messageId: messageId || undefined, time: msgTime || undefined });
      persistChatHistory();
    }
    return wrap;
  }

  // Mensajes que NO son de un turno de usuario (system-note, error,
  // system-orphan): se agregan al final de #messages, tal cual antes --
  // no pertenecen a ningun turno especifico que "anclarlos" debajo.
  messagesEl.appendChild(div);
  messagesEl.scrollTop = messagesEl.scrollHeight;
  if (persist) {
    chatHistory.push({
      role,
      text,
      messageId: messageId || undefined,
      time: msgTime || undefined,
      technical: technical || undefined,
      errorKind: errorKind || undefined,
    });
    persistChatHistory();
  }
  return div;
}

// Fila de tool-calls: todas las tool-calls de UN MISMO turno se agrupan en
// un solo contenedor flex (una fila), en vez de un badge suelto por tool
// apilado verticalmente -- ver .tool-call-row en styles.css.
//
// `mountEl`: el .turn-response-slot del turno actual (ver turnContainers
// mas arriba) -- asi la fila de tool-calls queda DENTRO del turno
// correspondiente, pegada debajo de su propio mensaje, en vez de siempre
// al final de #messages (que era el bug real: turnos encolados quedaban
// con sus tool-calls/respuesta mezclados al final de todo, sin relacion
// visual con el mensaje que los origino). Si no se pasa `mountEl`
// (ej. flujo viejo sin turno asociado), cae a `messagesEl` como antes.
let activeToolRow = null;
function getOrCreateToolRow(mountEl) {
  const target = mountEl || messagesEl;
  if (!activeToolRow || activeToolRow.parentElement !== target) {
    activeToolRow = document.createElement("div");
    activeToolRow.className = "tool-call-row";
    target.appendChild(activeToolRow);
  }
  return activeToolRow;
}
function resetToolRow() {
  activeToolRow = null;
}

function appendToolBadge(name, args, mountEl) {
  const div = document.createElement("div");
  div.className = "tool-call-badge running";
  div.dataset.name = name;

  // Nodos DOM en vez de innerHTML con datos interpolados: `args` puede
  // contener cualquier cosa que el LLM haya generado como argumento de la
  // tool-call -- se trata como texto plano no confiable, nunca como HTML.
  const dot = document.createElement("span");
  dot.textContent = "●";
  const nameSpan = document.createElement("span");
  nameSpan.className = "name";
  nameSpan.textContent = name;
  const argsSpan = document.createElement("span");
  argsSpan.className = "args";
  argsSpan.textContent = truncate(args, 60);
  div.append(dot, nameSpan, argsSpan);

  getOrCreateToolRow(mountEl).appendChild(div);
  messagesEl.scrollTop = messagesEl.scrollHeight;
  return div;
}

function truncate(text, n) {
  if (!text) return "";
  return text.length > n ? text.slice(0, n) + "..." : text;
}

function logTool(name, resultPreview) {
  const empty = toolLogEl.querySelector(".tool-empty");
  if (empty) empty.remove();
  const li = document.createElement("li");
  // `resultPreview` es el resultado crudo de una tool (puede venir de
  // SharePoint/Oracle/webfetch, datos NO confiables) -- se trata como
  // texto plano via textContent, nunca como HTML interpolado.
  const nameSpan = document.createElement("span");
  nameSpan.className = "tool-name";
  nameSpan.textContent = name;
  li.appendChild(nameSpan);
  li.appendChild(document.createElement("br"));
  li.appendChild(document.createTextNode(truncate(resultPreview, 80)));
  toolLogEl.prepend(li);
}

// =========================================================================
// Panel de "hilo de procesamiento" (pedido explicito del usuario). Vive
// DENTRO de la pantalla del Modo reunion (#voice-eye-overlay). 2da
// correccion 2026-09-07 (ver Decisiones registradas en spec.md): ya NO es
// un unico panel con caja -- son 2 paneles (izquierda/derecha), SIN
// fondo/borde, texto flotante tipo holograma. Cada linea nueva se reparte
// ALTERNADA entre ambos lados (ver `_processingFeedSide` mas abajo).
// Coloreado con var(--accent) (rojo oscuro / azul azul
// claro). Se alimenta de los MISMOS eventos SSE que ya procesa
// handleEvent() mas abajo (tool-call/tool-result/todo/progress-estimate)
// -- no requiere tocar el backend en absoluto.
// =========================================================================
// REQ-002 (spec asistente-contextual-voz): panel removido junto con el
// overlay de pantalla completa (ver Decisiones registradas en spec.md,
// 2026-09-10) -- la opinion de Vision en Modo reunion ahora aparece
// directo como mensaje del chat, sin panel dedicado de "hilo de
// procesamiento" en vivo. Las funciones pushProcessingFeedLine/
// setProcessingFeedActive/formatToolArgsForFeed y sus call sites se
// removieron junto con esto.

// Pedido explicito del usuario: sistema de sub-tareas (2 niveles fijos --
// investigado el codigo real de OpenCode antes de extenderlo, ver
// backend/app/tools/todowrite.py: OpenCode usa una lista plana, sin
// jerarquia). Cada tarea principal con `subtasks` se renderiza como un
// arbol colapsable (▶/▼, mismo patron visual que la TUI de OpenCode) --
// se auto-expande sola si alguna de sus sub-tareas esta "in_progress",
// para que el usuario vea de inmediato en cual sub-paso especifico esta
// trabajando Vision sin tener que ir a buscarla a mano.
function buildTodoLi(t) {
  const li = document.createElement("li");
  li.dataset.status = t.status;
  const statusSpan = document.createElement("span");
  statusSpan.className = `todo-status ${t.status}`;
  statusSpan.textContent = t.status;
  li.appendChild(statusSpan);
  li.appendChild(document.createTextNode(t.content)); // texto del LLM, nunca HTML
  return li;
}

function renderTodos(todos) {
  todoListEl.innerHTML = "";
  if (!todos || todos.length === 0) {
    todoListEl.innerHTML = '<li class="todo-empty">Sin tareas activas</li>';
    return;
  }
  for (const t of todos) {
    const li = buildTodoLi(t);
    const subtasks = Array.isArray(t.subtasks) ? t.subtasks : [];

    if (subtasks.length > 0) {
      const hasInProgressChild = subtasks.some((s) => s.status === "in_progress");
      li.classList.add("todo-has-subtasks");

      const toggle = document.createElement("button");
      toggle.type = "button";
      toggle.className = "todo-toggle";
      toggle.setAttribute("aria-label", "Mostrar/ocultar sub-tareas");
      li.insertBefore(toggle, li.firstChild);

      const subList = document.createElement("ul");
      subList.className = "todo-sublist";
      for (const sub of subtasks) {
        subList.appendChild(buildTodoLi(sub));
      }

      const setExpanded = (expanded) => {
        toggle.textContent = expanded ? "▼" : "▶";
        subList.classList.toggle("hidden", !expanded);
      };
      // Auto-expandida si hay una sub-tarea en curso -- el usuario no
      // deberia tener que abrir manualmente para ver en que esta Vision.
      setExpanded(hasInProgressChild);
      toggle.addEventListener("click", () => setExpanded(subList.classList.contains("hidden")));

      const wrapper = document.createElement("li");
      wrapper.className = "todo-parent-wrapper";
      wrapper.appendChild(li);
      wrapper.appendChild(subList);
      todoListEl.appendChild(wrapper);
    } else {
      todoListEl.appendChild(li);
    }
  }
}

// `autoAnswered`: label de la opcion que el loop autonomo YA selecciono
// solo (ver comentario en el caso "question" de handleEvent) -- si viene
// seteado, el panel se pinta como INFORMATIVO (nada que clickear, ya se
// resolvio) en vez de como una pausa real esperando al usuario.
function renderQuestion(question, options, autoAnswered) {
  questionPanelEl.classList.remove("hidden");
  questionPanelEl.classList.toggle("auto-answered", !!autoAnswered);
  questionPanelEl.innerHTML = "";

  const qText = document.createElement("div");
  qText.className = "q-text";
  qText.textContent = question;
  questionPanelEl.appendChild(qText);

  if (autoAnswered) {
    const note = document.createElement("div");
    note.className = "q-auto-note";
    note.textContent = `✓ El loop autónomo ya eligió esta opción sola, sin esperar tu confirmación: "${autoAnswered}"`;
    questionPanelEl.appendChild(note);
  }

  const optsDiv = document.createElement("div");
  optsDiv.className = "q-options";
  (options || []).forEach((opt) => {
    const btn = document.createElement("button");
    btn.textContent = opt.label;
    btn.title = opt.description || "";
    if (autoAnswered) {
      // Panel informativo: nada que clickear -- el turno ya siguio
      // adelante con `autoAnswered`, dejar los botones activos solo
      // confundiria (pareceria que todavia hace falta elegir).
      btn.disabled = true;
      if (opt.label === autoAnswered) btn.classList.add("q-auto-picked");
    } else {
      btn.onclick = () => {
        questionPanelEl.classList.add("hidden");
        sendMessage(opt.label);
      };
    }
    optsDiv.appendChild(btn);
  });
  questionPanelEl.appendChild(optsDiv);

  // El panel auto-respondido no necesita quedar pegado en pantalla --
  // se retira solo despues de un rato, mismo criterio que showAlarmToast
  // (7s) para no competir por atencion con una pausa real que SI la
  // necesita.
  if (autoAnswered) {
    setTimeout(() => {
      if (questionPanelEl.classList.contains("auto-answered")) {
        questionPanelEl.classList.add("hidden");
      }
    }, 7000);
  }
}

// Consume una respuesta SSE (fetch + ReadableStream) y renderiza los
// eventos que llegan, sea la respuesta de un POST /api/chat nuevo o de un
// GET /api/chat/stream/{id} (re-enganche a un turno que sigue corriendo en
// background en el backend -- ver mas abajo `attachIfSessionBusy`).
// Factorizado de sendMessage para poder reusar EXACTAMENTE la misma logica
// de parseo/renderizado/persistencia en ambos casos, sin duplicar codigo.
//
// `turnMessageId`: el messageId del turno al que pertenece este stream --
// se usa para resolver el .turn-response-slot correspondiente
// (turnContainers) y que la respuesta/tool-calls queden pegadas DEBAJO
// del mensaje de usuario que las origino, en vez de siempre al final de
// #messages (bug real reportado: turnos encolados quedaban con su
// respuesta "anclada abajo", separada de su propia pregunta).
async function renderIncomingStream(resp, turnMessageId = null, turnSessionId = state.sessionId) {
  if (!resp.ok || !resp.body) {
    if (resp.status !== 0) {
      const err = await httpErrorFromResponse(resp);
      appendMessage("error", err.message, { technical: technicalErrorLine(err) });
    }
    return;
  }

  const turnInfo = turnMessageId ? turnContainers.get(turnMessageId) : null;
  const responseSlot = turnInfo ? turnInfo.responseSlot : null;

  let assistantDiv = null;
  let assistantText = "";
  let pendingResponseType = null; // REQ-002 clasificador: llega ANTES del primer text-delta
  const toolBadges = new Map();
  // Investigacion de rendimiento 2026-09 ("Vision tarda mucho, se siente
  // trabado hacia el final de una respuesta larga"): antes cada text-delta
  // reprocesaba el markdown ACUMULADO completo (escapeHtml + split de
  // lineas + varios regex.replace por linea) y reemplazaba el subarbol DOM
  // entero -- costo que crece con el largo de la respuesta (~O(n²) total en
  // una respuesta streameada token a token). Mismo criterio ya aplicado a
  // typesetMath (debounce de 250ms, ver math.js) -- se agrupan varios
  // deltas seguidos en un solo render, disparado cuando el texto deja de
  // cambiar por RENDER_DEBOUNCE_MS. Timer propio de ESTE stream (no
  // global), para no pisarse con otra sesion respondiendo en paralelo.
  let _renderDebounceTimer = null;
  const RENDER_DEBOUNCE_MS = 80; // corto: sigue leyendose "en vivo", pero agrupa rafagas de tokens cortos

  function renderAssistantBubbleNow() {
    if (!assistantDiv) return;
    // BUG real corregido (deteccion QA 2026-09-14): antes esto hacia
    // `assistantDiv.innerHTML = renderMarkdown(...)` directo sobre el div,
    // SIN envolver el texto en `<span class="msg-text">` (a diferencia de
    // `appendMessage`, que si lo hace). El boton 🔊 "Reproducir respuesta"
    // y el marcado de rango buscan esa clase (`msg.querySelector(".msg-text")`)
    // y no la encontraban en NINGUN mensaje que llegara por streaming (o
    // sea, en la practica totalidad de las respuestas reales) -- fallaban
    // en silencio, sin error visible.
    const textSpan = document.createElement("span");
    textSpan.className = "msg-text";
    textSpan.innerHTML = renderMarkdown(assistantText);
    assistantDiv.replaceChildren(textSpan);
    // Pedido explicito del usuario: formulas matematicas/quimicas/
    // estadisticas/griegas deben verse EXACTAS -- con debounce propio
    // (ver math.js) porque este bloque ya de por si corre agrupado, no en
    // cada delta individual.
    typesetMath(assistantDiv);
    // REQ-001/002/003 (spec copiar-mensajes) + REQ-001/003 (spec
    // timestamps-mensajes) + REQ-001 (spec eliminar-respuestas):
    // `replaceChildren` de arriba borro los controles de copiar/eliminar Y
    // el <span> de hora que appendMessage ya habia agregado al crear
    // assistantDiv -- se re-adjuntan en cada render (barato: solo crea
    // unos pocos elementos chicos). `turnMessageId` viene del parametro de
    // renderIncomingStream (turno que origino esta respuesta en curso).
    attachCodeBlockCopyButtons(assistantDiv);
    assistantDiv.appendChild(buildMessageCopyControls("assistant", assistantText, turnMessageId));
    const timeEl = buildMessageTimeEl(Number(assistantDiv.dataset.msgTime));
    if (timeEl) assistantDiv.appendChild(timeEl);
    const rtEl = buildResponseTypeBadge(assistantDiv.dataset.responseType || null);
    assistantDiv.insertBefore(rtEl, assistantDiv.querySelector(".msg-text"));
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  function scheduleAssistantRender({ immediate = false } = {}) {
    if (_renderDebounceTimer) {
      clearTimeout(_renderDebounceTimer);
      _renderDebounceTimer = null;
    }
    if (immediate) {
      renderAssistantBubbleNow();
      return;
    }
    _renderDebounceTimer = setTimeout(() => {
      _renderDebounceTimer = null;
      renderAssistantBubbleNow();
    }, RENDER_DEBOUNCE_MS);
  }

  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  // Bug real reportado por el usuario (2026-09-20, "Error de conexion:
  // closeCurrentAssistantTurn is not defined"): esta funcion estaba
  // declarada con `const` DENTRO del `for (const part of parts)` (mas
  // abajo), o sea con scope de BLOQUE -- pero se usaba tambien como "red
  // de seguridad final" DESPUES del `while` (ver el ultimo llamado de esta
  // funcion, al final de renderIncomingStream). Si el stream se cortaba
  // ANTES de que el `for` interno llegara a ejecutarse ni una sola vez
  // (ej. la conexion se cerro con 0 bytes utiles, o nunca llego ningun
  // "data:" valido), esa `const` nunca se declaraba en ningun scope
  // alcanzable desde la red de seguridad -> `ReferenceError:
  // closeCurrentAssistantTurn is not defined`, capturado como "Error de
  // conexion" en el catch de sendMessage. Fix: se declara UNA sola vez
  // aca, en el scope de TODA la funcion (mismo nivel que assistantDiv/
  // assistantText, que son `let` de esta funcion y a los que esta
  // funcion accede por closure) -- asi queda accesible tanto dentro del
  // loop como en la red de seguridad final.
  const closeCurrentAssistantTurn = () => {
    if (!assistantDiv) return;
    scheduleAssistantRender({ immediate: true });
    assistantDiv.classList.remove("streaming");
    const savedTime = Number(assistantDiv.dataset.msgTime) || undefined;
    const assistantEntry = { role: "assistant", text: assistantText, time: savedTime, messageId: turnMessageId || undefined };
    if (assistantDiv.dataset.responseType) assistantEntry.responseType = assistantDiv.dataset.responseType;
    const stillOnOriginSession = state.sessionId === turnSessionId;
    if (stillOnOriginSession) {
      chatHistory.push(assistantEntry);
      persistChatHistory();
      const spokenMsg = assistantDiv; // spec fix-boton-play-stop: su 🔊 pasa a ⏹ mientras se lee
      speak(toPlainText(assistantText), { onStart: () => showMessageSpeaking(spokenMsg) });
    } else {
      const originHistory = loadChatHistoryForSession(turnSessionId);
      originHistory.push(assistantEntry);
      persistHistoryForSession(turnSessionId, originHistory);
    }
    // Reset para el PROXIMO turno sintetico (si lo hay) dentro de esta
    // misma conexion -- sin esto, el siguiente text-delta seguiria
    // escribiendo en assistantDiv (ya cerrado) en vez de crear uno
    // nuevo.
    assistantDiv = null;
    assistantText = "";
    pendingResponseType = null;
    toolBadges.clear();
  };

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    const parts = buffer.split("\n\n");
    buffer = parts.pop() || "";

    for (const part of parts) {
      const line = part.trim();
      if (!line.startsWith("data:")) continue;
      const jsonStr = line.slice(5).trim();
      if (!jsonStr) continue;

      let event;
      try {
        event = JSON.parse(jsonStr);
      } catch {
        continue;
      }

// Bug real reportado por el usuario (2026-09-20, uso del modo Loop
      // autonomo): "la respuesta de las 16:15 se coloco encima de la
      // respuesta de las 16:14... tendria que tener coherencia de
      // ordenamiento". Causa raiz: `_drain_session_queue` (backend) deja
      // que UNA SOLA conexion SSE atraviese VARIOS turnos sinteticos
      // consecutivos del loop sin cortar entre ellos (mientras la cola
      // no este vacia, diseño intencional para el modo Loop -- ver su
      // propio comentario extenso en main.py). Pero esta funcion
      // resolvia `assistantDiv`/`responseSlot` UNA SOLA VEZ al principio
      // (arriba de este bucle), como si toda la conexion perteneciera a
      // un unico turno -- el evento "done" tampoco trae ningun
      // messageId/turn_id (`{"reason": ...}` nomas, ver llm/loop.py) para
      // poder distinguir "aca termina un turno y arranca el siguiente".
      // Fix: al llegar "done" (ver mas abajo en handleEvent), se cierra/
      // persiste el turno actual Y SE RESETEA todo el estado local
      // (assistantDiv/assistantText/toolBadges/pendingResponseType) --
      // asi, si la conexion sigue viva y llega mas texto (turno
      // sintetico siguiente del loop), `appendAssistantText` crea una
      // burbuja NUEVA al final de #messages en vez de seguir escribiendo
      // en la posicion vieja del turno anterior. `closeCurrentAssistantTurn`
      // esta declarada arriba, en el scope de TODA la funcion (ver el
      // comentario grande antes del `while` de mas arriba) -- no aca
      // adentro, para que tambien sea alcanzable desde la red de
      // seguridad final despues del `while`.
      handleEvent(event, {
        // Sesion que origino este stream -- usada por handleEvent para NO
        // alterar el boton/estado global si el usuario cambio de sesion
        // mientras este turno sigue procesando en background (bug real del
        // bloqueo de escritura en la sesion nueva, 2026-09-12).
        turnSessionId,
        closeCurrentAssistantTurn,
        setResponseType: (type) => {
          // El evento suele llegar ANTES del primer text-delta (la burbuja
          // todavia no existe). Se guarda en pendingResponseType y se aplica
          // cuando se cree/reescriba la burbuja; si ya existe, se aplica ya.
          pendingResponseType = type;
          if (!assistantDiv) return;
          assistantDiv.dataset.responseType = type;
          const rtEl = buildResponseTypeBadge(type || null);
          const oldBadge = assistantDiv.querySelector(".msg-response-type");
          const anchor = assistantDiv.querySelector(".msg-text");
          if (oldBadge) oldBadge.remove();
          if (anchor) assistantDiv.insertBefore(rtEl, anchor);
        },
        appendAssistantText: (delta) => {
          if (!assistantDiv) {
            // REQ-003 (spec timestamps-mensajes): timestamp PROPIO de la
            // respuesta -- se captura aca (momento real en que arranca a
            // llegar texto), no el mismo que el mensaje de usuario que la
            // origino. `persist:false` (se persiste al final del turno,
            // ver mas abajo) asi que hay que pasar `time` explicito o se
            // perderia (appendMessage solo auto-genera Date.now() si
            // persist=true). Se guarda tambien en el dataset para poder
            // reinsertar el <span> de hora en cada delta (ver abajo, el
            // innerHTML = de cada delta lo borra junto con todo lo demas).
            const assistantTime = Date.now();
            assistantDiv = appendMessage("assistant", "", { persist: false, mountEl: responseSlot, time: assistantTime });
            // La barra de progreso de "Pensando..." (step 0) puede haberse
            // montado ANTES de que llegue el primer text-delta -- se
            // re-adjunta para quedar DEBAJO de la burbuja recien creada
            // (appendChild sobre un nodo ya presente lo mueve al final).
            if (activeProgressBar && activeProgressBar.root.parentElement === responseSlot) {
              responseSlot.appendChild(activeProgressBar.root);
            }
            assistantDiv.classList.add("streaming");
            assistantDiv.dataset.msgTime = String(assistantTime);
            if (pendingResponseType) assistantDiv.dataset.responseType = pendingResponseType;
          }
          assistantText += delta;
          // Se re-renderiza el Markdown acumulado (no solo el delta nuevo)
          // para que headers/negrita/listas/bloques de codigo ya se vean
          // formateados mientras el texto llega en vivo -- pero AGRUPADO
          // (debounce), no en cada delta individual (ver
          // scheduleAssistantRender/renderAssistantBubbleNow mas arriba).
          scheduleAssistantRender();
        },
        // REQ-004: al editar un mensaje MIENTRAS el asistente esta a mitad
        // de generar texto (no una tool-call), el backend descarta ese
        // texto parcial de su lado (nunca lo agrega a session.messages) y
        // arranca de nuevo -- pero sin esto, el FRONTEND seguia mostrando
        // la burbuja vieja y le pegaba el texto nuevo A CONTINUACION (bug
        // real reportado: "2+2=2+3=5" en vez de "2+3=5"). Este callback
        // descarta la burbuja/texto acumulado en curso para que el
        // proximo text-delta arranque una burbuja nueva de cero.
        resetAssistant: () => {
          if (_renderDebounceTimer) {
            clearTimeout(_renderDebounceTimer);
            _renderDebounceTimer = null;
          }
          if (assistantDiv) {
            assistantDiv.remove();
          }
          assistantDiv = null;
          assistantText = "";
          resetToolRow();
        },
        toolBadges,
        responseSlot,
      });
    }
  }

// Red de seguridad final: si la conexion se corto (fin natural del
  // `while` de lectura, sin que llegara un "done" explicito antes -- ej.
  // el backend cerro el generador SSE por su cuenta) y todavia queda un
  // turno sin cerrar, se cierra/persiste aca. En el camino normal (llega
  // "done" antes de que el stream se corte) `closeCurrentAssistantTurn`
  // ya dejo `assistantDiv = null`, asi que este bloque es un no-op.
  closeCurrentAssistantTurn();
}

// Investigacion de rendimiento 2026-09: indicador de "pensando" optimista --
// aparece en el mismo click de Enviar (ver sendMessage), no atado a ningun
// evento del backend. Un solo indicador global a la vez (mismo criterio que
// activeProgressBar): si ya hay uno de un turno anterior sin limpiar, se
// reusa en vez de duplicar.
//
// Tambien se reusa DESPUES de cada "tool-result" (ver handleEvent): el
// backend solo emite `progress-estimate` ("Pensando…") para el PRIMER paso
// del turno (`_step == 0`, ver llm/loop.py) -- en los pasos siguientes (el
// modelo generando su respuesta despues de ver el resultado de una tool),
// no hay NINGUN indicador visual mientras dura esa espera. Caso real medido
// (2026-09-15): un pedido de optimizacion/calculo tardo 13:44 min de los
// cuales solo 37ms fueron tools de Vision -- los ~13 minutos restantes el
// usuario no tenia ninguna señal de que algo seguia en curso.
let _thinkingEl = null;

function showOptimisticThinking() {
  if (_thinkingEl) {
    // Ya esta visible (ej. se re-dispara justo despues de un tool-result) --
    // no duplicar el indicador.
    return;
  }
  const div = document.createElement("div");
  div.className = "msg assistant";
  const dots = document.createElement("span");
  dots.className = "msg-thinking-dots";
  dots.innerHTML = "<span></span><span></span><span></span>";
  div.appendChild(dots);
  messagesEl.appendChild(div);
  messagesEl.scrollTop = messagesEl.scrollHeight;
  _thinkingEl = div;
}

function hideOptimisticThinking() {
  if (!_thinkingEl) return;
  _thinkingEl.remove();
  _thinkingEl = null;
}

// BUG REAL detectado por el usuario (2026-09-16, screenshot): el tip de
// arriba SOLO se enganchaba a los puntitos optimistas (_thinkingEl) -- pero
// en el caso real de 13:44 min (ver comentario arriba), lo que se ve todo
// ese tiempo es la barra REAL de "Pensando…" (`progress-estimate` del PRIMER
// paso del turno, `activeProgressBar`/startProgress mas arriba), porque esa
// barra reemplaza a los puntitos apenas llega evidencia real de actividad
// (ver hideOptimisticThinking() en handleEvent). El tip nunca llegaba a
// mostrarse porque estaba atado al elemento equivocado. Fix: el timer
// arranca UNA VEZ en el click (cuenta el tiempo total de espera del turno,
// no se reinicia en cada tool-result) y, al vencer, se engancha a CUALQUIER
// indicador que siga visible en ese momento (puntitos o barra real) -- si
// ninguno sigue visible, el turno ya devolvio contenido real y no se
// muestra nada.
let _thinkingTipTimer = null;
let _thinkingTipEl = null;
// Si la espera se extiende bastante, sugiere el atajo que confirmamos que
// funciona (mismo caso real de arriba: pedir el resultado directo bajo esa
// consulta de 13:44 min a 10s cuando se le pidio "sin explicar el
// razonamiento paso a paso"). Umbral generoso: no aparece en una espera
// corta normal, solo cuando ya se nota.
const THINKING_TIP_DELAY_MS = 15000;

function scheduleThinkingTip() {
  clearTimeout(_thinkingTipTimer);
  _thinkingTipTimer = setTimeout(() => {
    if (_thinkingTipEl) return;
    if (!_thinkingEl && !activeProgressBar) return;
    const wrap = document.createElement("div");
    wrap.className = "msg assistant";
    const tip = document.createElement("div");
    tip.className = "msg-thinking-tip";
    tip.textContent = "💡 Para cálculos u optimizaciones complejas, agregá \"dame el resultado directo, sin explicar el razonamiento paso a paso\" — puede ser mucho más rápido.";
    wrap.appendChild(tip);
    messagesEl.appendChild(wrap);
    _thinkingTipEl = wrap;
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }, THINKING_TIP_DELAY_MS);
}

function cancelThinkingTip() {
  clearTimeout(_thinkingTipTimer);
  _thinkingTipTimer = null;
  if (!_thinkingTipEl) return;
  _thinkingTipEl.remove();
  _thinkingTipEl = null;
}

async function sendMessage(text, { messageId = null, images = null, voiceConfirm = false } = {}) {
  // spec contexto-multimodal REQ-004: un mensaje con SOLO imagenes (sin
  // texto) es valido -- el chequeo original solo miraba el texto.
  if (!text.trim() && (!images || images.length === 0)) return;
  // NOTA sobre el cambio de comportamiento (pedido explicito del usuario):
  // el TEXTAREA/boton "Enviar" ya no permite mandar un mensaje nuevo
  // mientras Vision esta ocupada (ver el guard en formEl "submit" mas
  // abajo) -- pero esta funcion en si sigue permitiendo llamadas
  // concurrentes internamente (ej. responder una pregunta de la tool
  // `question` via renderQuestion, o un comando propio) sin ese guard,
  // porque esos flujos no pasan por el textarea. `state.sending` sigue
  // siendo un CONTADOR (no booleano) por eso: puede haber mas de un
  // sendMessage en curso aunque el usuario ya no pueda disparar uno nuevo
  // el mismo desde el compositor mientras el boton esta en "active".
  const wasAlreadyBusy = state.sending > 0;
  state.sending += 1;
  questionPanelEl.classList.add("hidden");
  if (!wasAlreadyBusy) setConnStatus("busy", "Vision está trabajando…");

  const msgId = messageId || crypto.randomUUID();
  appendMessage("user", text, { messageId: msgId, images });
  if (!wasAlreadyBusy) resetToolRow();

  // Investigacion de rendimiento 2026-09 ("Vision tarda mucho, se siente sin
  // respuesta"): feedback INSTANTANEO en el mismo click, antes de mandar el
  // fetch -- estilo "boton de play de Spotify" (pedido explicito del
  // usuario): la UI reacciona al toque, no a la latencia real del backend.
  // Antes el boton Enviar/Detener se quedaba en "idle" (inerte, re-enviable)
  // hasta que volvia el primer evento SSE util -- ver el gateo por sesion en
  // handleEvent, que sigue aplicando para turnos en OTRAS sesiones.
  if (!wasAlreadyBusy) {
    setSendButtonState("active");
    showOptimisticThinking();
    scheduleThinkingTip();
  }

try {
    // BUG REAL (cambiar de sesion a mitad de turno, 2026-09-12): se
    // captura la sesion que ORIGINA este turno ANTES del fetch. El backend
    // sigue procesando por mas que el usuario cambie de sesion (no aborta
    // al desconectarse el SSE), asi que renderIncomingStream necesita saber
    // a que sesion pertenece el resultado para persistirlo ahi, no en la
    // sesion que este activa cuando el turno termina.
    const turnSessionId = state.sessionId;
    const resp = await apiFetch(`${API_BASE}/api/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        session_id: turnSessionId,
        message: text,
        message_id: msgId,
        model: selectedModelForRequest(),
        images: images && images.length > 0 ? images : undefined,
        // REQ-006/009 (spec asistente-contextual-voz): marca este turno
        // como originado en el modo Cerebro -- activa la confirmacion
        // hablada del lado del backend antes de ejecutar tools peligrosas
        // (bash/write/edit). false (default) = chat de texto normal, sin
        // cambio de comportamiento.
        voice_confirm: voiceConfirm || undefined,
        // spec informe-tecnico-hablado REQ-008/009/010: con el informe
        // prendido el backend narra el codigo en vivo, pide comentarios y
        // anuncia el avance del plan.
        narrate: (isTtsEnabled() && isTechReportEnabled()) || undefined,
      }),
    });
    await renderIncomingStream(resp, msgId, turnSessionId);
  } catch (err) {
    hideOptimisticThinking();
    cancelThinkingTip();
    // Solo un corte de red real es "sin conexión": un bug de la interfaz
    // (ej. el ReferenceError de 2026-09-20) no debe disfrazarse de eso.
    const offline = isOfflineError(err);
    appendMessage("error", friendlyErrorMessage(err), {
      technical: technicalErrorLine(err),
      errorKind: offline ? "offline" : "other",
    });
    if (offline) setConnStatus("error", "Sin conexión con Vision");
  } finally {
    state.sending -= 1;
    if (state.sending <= 0) {
      state.sending = 0;
      hideOptimisticThinking(); // red de seguridad: nunca debe sobrevivir al fin del turno
      cancelThinkingTip(); // idem -- el tip tampoco debe sobrevivir al turno
      if (connStatusEl.dataset.state === "busy") setConnStatus("online", "Vision en línea");
      setSendButtonState("idle");
      // Red de seguridad: si el stream murio sin emitir 'done'/'error'
      // (caida de red, backend reiniciado a mitad de turno), el
      // setInterval de la barra quedaria corriendo para siempre contra
      // una barra visible y congelada. Se limpia junto al resto del
      // estado de "ocupado", no solo en los eventos terminales.
      resetProgress();
    }
  }
}

function handleEvent(event, ctx) {
// REQ-001: el boton unico Enviar/Detener pasa a "Detener" apenas hay
  // evidencia de que un turno esta REALMENTE corriendo (no solo encolado)
  // -- cualquier evento que no sea 'queued'/'stopped'/'done'/'error'
  // implica actividad del modelo.
  // BUG REAL (cambiar de sesion a mitad de turno, 2026-09-12): este bloque
  // ponia el boton global en "active" ante CUALQUIER evento, sin importar
  // de que sesion viniera el stream. Si la sesion A seguia procesando
  // mientras el usuario creaba la sesion B, los eventos de A volvian a
  // poner el boton en "active" en B -- y con el boton en "active" el guard
  // de submit (abajo) bloqueaba ESCRIBIR/enviar en B ("no me deja
  // escribir mientras procesa la otra consulta"). Fix: solo se dispara el
  // boton si el stream es de la sesion ACTIVA (ctx.turnSessionId).
  if (!["queued", "stopped", "done", "error"].includes(event.type)) {
    if (ctx.turnSessionId === state.sessionId) {
      if (sendOrStopBtnEl.dataset.state === "idle") setSendButtonState("active");
      // El indicador optimista ya cumplio su funcion (feedback instantaneo
      // al click) -- apenas hay evidencia REAL de actividad de este turno
      // (tool-call, texto, progress-estimate, etc.) se retira para no
      // pisar/duplicar la UI real que sigue (badge de tool, barra de
      // progreso, texto en streaming).
      hideOptimisticThinking();
    }
  }

  switch (event.type) {
    case "text-delta":
      ctx.appendAssistantText(event.data.text);
      break;

    case "response-type":
      if (ctx.setResponseType) ctx.setResponseType(event.data.type || event.data.response_type);
      break;

    case "tool-call": {
      const badge = appendToolBadge(event.data.name, event.data.arguments, ctx.responseSlot);
      ctx.toolBadges.set(event.data.id, badge);
      break;
    }

    case "tool-result": {
      const badge = ctx.toolBadges.get(event.data.id);
      if (badge) {
        badge.classList.remove("running");
        badge.classList.add("done");
        badge.innerHTML = `<span>&#10003;</span><span class="name">${event.data.name}</span>`;
        // El boton "Ver dashboard" se quito de aca: el dashboard ahora solo
        // se abre cuando el usuario lo pide explicitamente en el chat
        // ("haceme un dashboard", "mostrame un kpi de...") -- eso ya lo
        // resuelve la tool `render_chart` (backend/app/tools/chart.py) via
        // el evento SSE "chart-ready" -> renderChart(), sin depender de
        // este boton manual en el badge de la tool-call.
      }
      logTool(event.data.name, event.data.result);
      // Investigacion de rendimiento 2026-09: apenas termina una tool, el
      // backend no vuelve a avisar "Pensando…" para el proximo paso (ese
      // progress-estimate es SOLO del primer paso del turno, ver
      // llm/loop.py) -- se re-muestra el indicador optimista para no dejar
      // ese tramo en silencio total (puede ser el tramo mas largo del
      // turno, ver comentario de showOptimisticThinking).
      if (ctx.turnSessionId === state.sessionId) showOptimisticThinking();
      break;
    }

    case "todo":
      // BUG real corregido (deteccion QA 2026-09-14): a diferencia de
      // text-delta/response-type (que escriben dentro de la burbuja de ESE
      // turno, aislada por sesion), este handler pintaba directo el panel
      // GLOBAL del sidebar sin chequear de que sesion venia el evento. Si la
      // sesion A seguia procesando en background mientras el usuario ya
      // estaba en la sesion B, los eventos "todo" de A pisaban el panel de
      // tareas que B esta mirando (y viceversa al volver a A) -- reportado
      // por el usuario como "la lista de tareas se rompio/desaparecio".
      if (ctx.turnSessionId === state.sessionId) {
        renderTodos(event.data.todos);
        _techCurrentTask = currentTaskOf(event.data.todos); // spec informe-tecnico-hablado REQ-011
        ensureTechKeepAlive();
      }
      break;

    // spec informe-tecnico-hablado REQ-008/010: recorrido del codigo en vivo y
    // avance del plan, armados por el backend (ya sin secretos). Solo en voz.
    case "narration":
      if (ctx.turnSessionId !== state.sessionId) break;
      if (isTechReportEnabled()) speakTechReport(event.data.text);
      ensureTechKeepAlive();
      break;

    // REQ-001/003/004/005 (spec dashboard-graficos): el backend ya arma
    // labels/values completos (sin el truncado a 4000 chars del SSE,
    // resuelto server-side via SQLite) -- el frontend solo dibuja.
    case "chart-ready":
      renderChart(event.data);
      break;

    // REQ-002/003 (spec update-skills): el backend emite este evento
    // fuera-de-banda (mismo mecanismo que "chart-ready") cuando la tool
    // `write` escribe un archivo `SKILL.md` -- BUG REAL reportado por el
    // usuario: cuando Vision crea una skill nueva sola, durante el chat
    // (ej. "creame una skill de X"), ninguna de las 3 listas de skills se
    // actualizaba (quick-panel-skills-list, cfg-skills-global-list/
    // cfg-skills-local-list, menu "/") hasta recargar la pagina, porque
    // nada avisaba al frontend que algo habia cambiado en el filesystem de
    // skills. Se invalida SIEMPRE ambas caches del menu "/" (sin importar
    // `event.data.scope`: una skill nueva puede afectar la lista global o
    // la local segun donde cayo, y es mas seguro invalidar de mas que dejar
    // pasar un caso) y, si alguno de los 2 paneles visuales esta
    // efectivamente abierto en este instante, se re-renderiza en vivo --
    // decision del usuario: comportamiento silencioso, sin toast/aviso
    // adicional (Vision ya cuenta en el texto de su respuesta que creo la
    // skill).
    case "skill-created":
      _skillsCache = null;
      _localSkillsCache = null;
      if (!settingsOverlayEl.classList.contains("hidden")) {
        refreshSkillsLists();
      }
      if (!quickPanelMenuEl.classList.contains("hidden") && !quickPanelSkillsViewEl.classList.contains("hidden")) {
        showQuickPanelSkillsView();
      }
      break;

    case "question":
      // Bug real reportado por el usuario 2026-09-16 ("tengo loop y no
      // tomo la recomendada ni sono alarma"): cuando el loop autonomo
      // auto-selecciona la opcion "(Recomendada)" (ver
      // app/tools/question.py:_question_execute), el turno SIGUE solo --
      // no hay alarma porque no hace falta intervencion humana, eso es lo
      // esperado. Pero el backend igual manda este mismo evento "question"
      // con `auto_answered` seteado (para transparencia de que paso), y
      // renderQuestion() ignoraba ese campo por completo -- dibujaba el
      // panel EXACTAMENTE igual que una pausa real, con botones cliqueables
      // esperando al usuario, cuando en realidad ya no hacia falta ningun
      // click (el modelo ya siguio adelante). El usuario vela ese panel
      // "sin resolver" y asumia que la recomendada nunca se tomo.
      renderQuestion(event.data.question, event.data.options, event.data.auto_answered);
      break;

    // REQ-002/004/005 (spec barra-progreso-adaptativa): el backend avisa
    // que ARRANCA una operacion, con el presupuesto estimado segun su
    // propio historial real. El frontend solo interpola visualmente.
    // `ctx.responseSlot`: mismo .turn-response-slot que usan appendToolBadge/
    // appendAssistantText -- la barra queda pegada DEBAJO de la respuesta
    // que se esta generando, no en un lugar fijo del header.
    case "progress-estimate":
      // Bug real reportado por el usuario (prueba de agentes, 2026-09-20):
      // "cuando cambio de sesion... en las sesiones a las que cambio se ve
      // el command-progress de la sesion original" -- a diferencia del
      // efecto de voz de este mismo evento (6 lineas mas abajo) y de
      // "todo"/el gate del boton (mas arriba en este switch), ESTE
      // startProgress() nunca chequeaba `ctx.turnSessionId ===
      // state.sessionId` -- la barra de progreso de un agente corriendo en
      // background en la sesion X se montaba en el DOM de la sesion Y que
      // el usuario esta mirando en ese momento. Mismo criterio ya aplicado
      // en el resto del switch: si el evento no es de la sesion activa, se
      // ignora del todo (el resultado real se ve al volver a esa sesion,
      // que lee de SQLite -- ver activateSession).
      if (ctx.turnSessionId !== state.sessionId) break;
      startProgress(
        {
          label: event.data.label,
          budgetMs: event.data.budget_ms,
          hasHistory: event.data.has_history,
        },
        ctx.responseSlot
      );
      // REQ-008 (spec narracion-agente-efimera): el mismo `label` en
      // lenguaje humano que ya se muestra en TEXTO en la barra de
      // progreso (ej. "Consultando Oracle…", "Cargando skill X…", ver
      // tool_timings.describe_operation) se narra tambien en VOZ si el
      // usuario tiene TTS activado -- complementa el texto, no lo
      // reemplaza (misma decision ya tomada para el chat en si). Se
      // excluye "thinking" (label "Pensando…") a proposito: ese progress-
      // estimate generico se dispara en TODO turno (incluso sin ninguna
      // tool), narrarlo siempre seria ruidoso e inconsistente con lo que
// pide REQ-008 ("Jarvis esta ejecutando una tool o tarea").
      // BUG REAL (cambiar de sesion a mitad de turno, 2026-09-12): se gatea
      // por sesion activa -- si la sesion A sigue con tool-calls mientras el
      // usuario esta en B, no debe narrar por voz el progreso de A ("si la
      // voz esta activada sigue leyendo lo de la sesion anterior").
      // spec informe-tecnico-hablado REQ-001/003/004: con "Hablar informe
      // tecnico" prendido, en vez de la etiqueta corta se dice la frase que
      // arma el backend con lo que Vision ejecuta (`speech`), solo para las
      // acciones (`speech` null = leer/buscar/pensar: silencio). Las frases
      // viejas que no llegaron a sonar se reemplazan por la mas nueva.
      if (ctx.turnSessionId !== state.sessionId) break;
      if (isTechReportEnabled()) {
        if (event.data.speech) speakTechReport(event.data.speech);
        ensureTechKeepAlive();
      } else if (event.data.label && event.data.operation !== "thinking") {
        speak(event.data.label);
      }
      break;

    case "error":
      // Bug real (2026-09-20, modo Loop): el backend sintetiza el
      // PROXIMO turno del loop en el `finally` de `_run_in_background`
      // sin importar si el turno anterior termino en "done" o "error"
      // (ver `_maybe_continue_loop`, se llama siempre que `loop_active`
      // siga en true) -- mismo reset que en "done", y por el mismo
      // motivo: si no se cierra/resetea aca, el proximo turno sintetico
      // arrastraria el `assistantText` del que fallo.
      ctx.closeCurrentAssistantTurn();
      stopTechKeepAlive(ctx.turnSessionId); // spec informe-tecnico-hablado REQ-011
      // Mismo bug/fix que "progress-estimate" arriba: un error de un turno
      // en background (sesion X) no debe aparecer en la sesion Y que el
      // usuario esta mirando -- appendMessage() pinta en el DOM global sin
      // saber de que sesion viene, asi que hay que filtrar ANTES de
      // llamarlo (no tiene forma de filtrar por si solo).
      if (ctx.turnSessionId !== state.sessionId) break;
      // El backend ya manda el texto en lenguaje simple + `technical`
      // (código de soporte, modelo y hora) para el "Detalle técnico".
      appendMessage("error", event.data.message, { technical: event.data.technical || null });
      finishProgress();
      break;

case "done":
      // Bug real (2026-09-20, modo Loop): `closeCurrentAssistantTurn()`
      // debe correr SIEMPRE aca, sin importar la sesion activa -- ya tiene
      // su propio manejo interno (`stillOnOriginSession`) para decidir
      // DONDE persistir. Si esto quedara detras del guard de sesion de
      // abajo, un loop corriendo en background (sesion no activa)
      // acumularia TODOS sus turnos sinteticos en un solo assistantText
      // sin resetear entre ellos -- el mismo bug de desorden de turnos,
      // pero para sesiones que ni siquiera se estan mirando.
      ctx.closeCurrentAssistantTurn();
      stopTechKeepAlive(ctx.turnSessionId); // spec informe-tecnico-hablado REQ-011
      // Mismo bug/fix que "progress-estimate"/"error"/"stopped" arriba: si
      // el turno que termina es de otra sesion (background, ej. un
      // agente), no corresponde cerrar la barra de progreso NI auto-abrir
      // el panel Navegador de la sesion que el usuario esta mirando --
      // ambos efectos SI son visuales sobre la UI activa, a diferencia de
      // closeCurrentAssistantTurn (que es persistencia de datos).
      if (ctx.turnSessionId !== state.sessionId) break;
      // Fin del turno -- el mensaje ya se fue renderizando
      // incrementalmente; solo queda cerrar la barra en 100% (REQ-003:
      // el 100% se alcanza por evidencia real de fin, nunca por la
      // interpolacion, que tiene techo en PROGRESS_CEILING).
      finishProgress();
      // Auto-revelar el panel "Navegador" si ahora hay contenido nuevo
      // (dashboards/pages que Vision escribio en este turno).
      maybeAutoRevealBrowser();
      break;

    // --- REQ-001/003/004: eventos nuevos de la spec parada-y-cola-mensajes ---
    case "queued":
      renderQueueBadge(event.data.message_id, event.data.position);
      break;

    case "stopped":
      stopTechKeepAlive(ctx.turnSessionId); // spec informe-tecnico-hablado REQ-011
      // Mismo bug/fix que "progress-estimate"/"error" arriba: si el turno
      // detenido es de otra sesion (no la activa), no corresponde tocar
      // NADA de la UI que el usuario esta mirando -- ni el mensaje, ni el
      // boton Enviar/Detener, ni los indicadores optimistas, ni la barra.
      if (ctx.turnSessionId !== state.sessionId) break;
      appendMessage("system-note", "⏹ Turno detenido por el usuario.", { persist: false });
      setSendButtonState("idle");
      // BUG REAL detectado por el usuario (2026-09-16, screenshot): faltaban
      // estas dos lineas -- el indicador optimista (puntitos) y el tip de
      // 15s (ver showOptimisticThinking/scheduleThinkingTip) podian quedar
      // pegados en pantalla despues de "Turno detenido por el usuario.",
      // como si el turno siguiera en curso.
      hideOptimisticThinking();
      cancelThinkingTip();
      // El turno se corto: la barra se oculta SIN completarse a 100%
      // (completarla sugeriria que la operacion termino bien, que es
      // justo lo contrario de lo que paso).
      resetProgress();
      // REQ-007/008: si Jarvis estaba narrando algo por voz (respuesta o
      // progreso), se corta al instante -- seguir hablando despues de que
      // el usuario ya presiono "Detener" seria confuso/contradictorio.
      cancelSpeech();
      break;

    case "message-edited":
      // Bug real corregido: el backend descarta el texto parcial que
      // estaba generando (nunca lo agrega a session.messages), pero el
      // FRONTEND seguia mostrando esa burbuja a medio escribir y le
      // pegaba el texto nuevo A CONTINUACION en vez de reemplazarla. Se
      // descarta la burbuja/texto acumulado en curso para que la
      // respuesta que sigue arranque de cero, visualmente consistente
      // con lo que el backend ya hizo.
      ctx.resetAssistant();
      break;

    // spec modo-plan-loop REQ-009: la tool `question` pauso el loop
    // porque ninguna opcion traia el sufijo "(Recomendada)" -- suena la
    // alarma y muestra el banner hasta que el usuario responda o pare.
    case "loop-paused":
      showLoopBanner();
      break;

    // spec modo-plan-loop REQ-007: el loop se detuvo (exito, STOP, o
    // limite de 30 turnos) -- se apaga cualquier alarma que estuviera
    // sonando (caso max_turns tambien pausa+alarma, pero via el evento
    // 'loop-paused' de arriba, no este) y se avisa el motivo si no fue
    // un simple exito silencioso.
case "loop-stopped":
      hideLoopBanner();
      _loopRunning = false; // sincroniza el toggle "Loop" del panel de accesos rapidos
      if (quickPanelLooprunToggleEl) syncQuickPanelLoopState();
      maybeAutoRevealBrowser(); // si el loop dejo dashboards/pages, revelarlos
      // Pedido del usuario (2026-09-13): si el loop termino TODAS las tareas
      // (reason "completed"), suena la señal de EXITO para saberlo estando
      // lejos de la PC.
      if (event.data.reason === "completed") playSuccessAlarm();
      if (event.data.reason === "max_turns") {
        appendMessage(
          "system-note",
          "⏹ El loop alcanzó el límite de 30 turnos sin completar todo — revisá el progreso.",
          { persist: false }
        );
      }
      break;

    // spec carpeta-por-sesion REQ-008: una tool que el LLM disparo
    // (hoy solo record_decision -- las otras 3 acciones de la spec son
    // por click humano, se validan directo en el frontend antes de
    // llamarlas) necesita carpeta y esta sesion no tiene ninguna. Abre
    // automaticamente el modal de asignar carpeta -- reusa el modal de
    // EDICION de sesion (no el de "nueva sesion", que es para crear una
    // sesion desde cero), ya que esta sesion ya existe y solo le falta
    // la carpeta.
    case "project-folder-required":
      openSessionEditModal();
      break;

    // spec gestion-contexto-conversacion: solo la compactacion completa
    // genera un aviso; el pruning incremental permanece silencioso.
    case "system-note":
      appendMessage("system-note", event.data.text, { persist: false });
      break;

    case "model-switch":
      // spec salto-automatico-modelos REQ-002/003: que modelo responde de
      // verdad (el chip lo muestra) y, si hubo salto, el aviso en el chat.
      // Mismo filtro de sesion que "error": un turno en background de otra
      // sesion no debe tocar lo que el usuario esta mirando.
      if (ctx.turnSessionId !== state.sessionId) break;
      state.activeModel = event.data.model || "";
      state.activeModelJumped = Boolean(event.data.home) && event.data.model !== event.data.home;
      renderModelChip();
      if (event.data.text) appendMessage("system-note", event.data.text, { persist: false });
      scheduleFallbackResume(event.data.resume_at); // REQ-010: la vuelta se ve al vencer la pausa
      break;

    default:
      break;
  }
}

// -------------------------------------------------------------------------
// Dashboard modal: unica vista activa es el grafico (ver renderChart mas
// abajo), disparado por la tool `render_chart` cuando el usuario lo pide
// explicitamente en el chat ("hacer un grafico/dashboard de..."). La vista
// previa de KPIs genericos+tabla completa (openDashboard/parseCsv/
// computeGenericKpis) se elimino junto con el boton "Ver dashboard" del
// badge de tool-call que era su unico disparador -- sin eso, abria el
// modal automaticamente en toda tool de datos sin que el usuario lo
// pidiera.
// -------------------------------------------------------------------------
function closeDashboard() {
  dashboardOverlayEl.classList.add("hidden");
}
dashboardCloseEl.addEventListener("click", closeDashboard);
dashboardOverlayEl.addEventListener("click", (e) => {
  if (e.target === dashboardOverlayEl) closeDashboard();
});

// -------------------------------------------------------------------------
// REQ-001/003/004/005 (spec dashboard-graficos): dibuja dona/barras con
// Chart.js VENDORIZADO (frontend/vendor/chart.js, sin CDN -- ver
// AGENTS.md) dentro de #dashboard-overlay (el <canvas> ya vive en el
// header de ese modal).
// -------------------------------------------------------------------------
let _activeChart = null; // instancia de Chart.js -- se destruye antes de redibujar (REQ-005)

// Paleta del sistema rojo (negro + rojo) para los datasets
// de Chart.js -- coherente con el resto de la UI en vez de los colores
// default de la libreria.
const CHART_PALETTE = ["#E10600", "#FF6B57", "#7A0400", "#3D3D42", "#B0B0B8", "#5C1A17"];

function renderChart(data) {
  if (!window.Chart) {
    console.warn("Chart.js no esta cargado -- no se puede dibujar el grafico.");
    return;
  }

  dashboardTitleEl.textContent = data.title || "Gráfico";
  dashboardChartWrapEl.classList.remove("hidden");

  if (_activeChart) {
    _activeChart.destroy(); // REQ-005: nunca se acumulan graficos, se reemplaza
    _activeChart = null;
  }

  const colors = data.labels.map((_, i) => CHART_PALETTE[i % CHART_PALETTE.length]);
  const isDonut = data.chart_type === "donut";

  _activeChart = new Chart(dashboardChartCanvasEl, {
    type: isDonut ? "doughnut" : "bar",
    data: {
      labels: data.labels,
      datasets: [{
        label: data.value_column,
        data: data.values,
        backgroundColor: colors,
        borderColor: isDonut ? "#0B0B0D" : colors,
        borderWidth: isDonut ? 2 : 0,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: isDonut, labels: { color: "#E8E8EA" } },
        tooltip: {
          callbacks: isDonut ? {
            // REQ-003: cantidad Y porcentaje juntos en el tooltip de la
            // dona (pedido explicito del ejemplo original: "vacias/llenas
            // con cantidad y porcentaje").
            label: (ctx) => {
              const total = ctx.dataset.data.reduce((a, b) => a + b, 0);
              const pct = total > 0 ? ((ctx.parsed / total) * 100).toFixed(1) : "0.0";
              return `${ctx.label}: ${ctx.parsed} (${pct}%)`;
            },
          } : {},
        },
      },
      scales: isDonut ? {} : {
        x: { ticks: { color: "#B0B0B8" }, grid: { color: "#2A2A2E" } },
        y: { ticks: { color: "#B0B0B8" }, grid: { color: "#2A2A2E" }, beginAtZero: true },
      },
    },
  });

  dashboardOverlayEl.classList.remove("hidden");
}

// -------------------------------------------------------------------------
// REQ-006: el modal se puede ARRASTRAR por su header -- solo reposiciona
// el modal completo, no reordena contenido interno. Se aplica al MISMO
// .dashboard-modal que ya usan KPIs/tabla/grafico (un solo listener sirve
// para las 3 vistas).
// -------------------------------------------------------------------------
(function enableDashboardDrag() {
  const modal = dashboardOverlayEl.querySelector(".dashboard-modal");
  const header = dashboardOverlayEl.querySelector(".dashboard-header");
  if (!modal || !header) return;

  let dragging = false;
  let startX = 0, startY = 0, startLeft = 0, startTop = 0;

  header.addEventListener("mousedown", (e) => {
    // Nunca arrastrar si el click fue sobre el boton de cerrar.
    if (e.target.closest(".dashboard-close")) return;
    dragging = true;
    const rect = modal.getBoundingClientRect();
    startX = e.clientX;
    startY = e.clientY;
    startLeft = rect.left;
    startTop = rect.top;
    // Pasa de "centrado por flexbox" a posicionamiento absoluto explicito
    // SOLO al empezar a arrastrar -- antes de eso el modal sigue
    // centrado como los demas modales del proyecto.
    modal.style.position = "fixed";
    modal.style.left = `${startLeft}px`;
    modal.style.top = `${startTop}px`;
    modal.style.margin = "0";
    header.style.cursor = "grabbing";
    e.preventDefault();
  });

  document.addEventListener("mousemove", (e) => {
    if (!dragging) return;
    const dx = e.clientX - startX;
    const dy = e.clientY - startY;
    modal.style.left = `${startLeft + dx}px`;
    modal.style.top = `${startTop + dy}px`;
  });

  document.addEventListener("mouseup", () => {
    dragging = false;
    header.style.cursor = "";
  });

  // REQ-006: al cerrar, se resetea la posicion -- la proxima apertura
  // vuelve a arrancar centrada (no persiste la posicion entre usos).
  dashboardCloseEl.addEventListener("click", () => {
    modal.style.position = "";
    modal.style.left = "";
    modal.style.top = "";
    modal.style.margin = "";
  });
})();

// =========================================================================
// Contexto multi-modal: pegar/arrastrar/seleccionar imagenes (spec
// contexto-multimodal, REQ-001 a REQ-008).
//
// Flujo: paste/drop/file-picker -> validarModelo -> comprimir en Canvas
// (REQ-003) -> agregar a pendingImages (REQ-004/005) -> al enviar, viajan
// como data URIs base64 en el body de /api/chat (REQ-006) -> se muestran
// como miniaturas en la burbuja ya enviada (REQ-007).
// =========================================================================
const pendingImagesRowEl = document.getElementById("pending-images-row");
const imageBlockedNoticeEl = document.getElementById("image-blocked-notice");
const imageFileInputEl = document.getElementById("image-file-input");
const attachImageBtnEl = document.getElementById("attach-image-btn");

const MAX_IMAGE_DIMENSION = 1568; // mismo limite practico que usan Claude/GPT internamente
const IMAGE_JPEG_QUALITY = 0.82;

let pendingImages = []; // [{ dataUri, id }]

function showImageBlockedNotice(message) {
  imageBlockedNoticeEl.textContent = message;
  imageBlockedNoticeEl.classList.remove("hidden");
  setTimeout(() => imageBlockedNoticeEl.classList.add("hidden"), 4000);
}

/**
 * REQ-002: consulta si el modelo ACTUALMENTE seleccionado soporta
 * imagenes -- reusa `_allModelsCache` si ya esta poblado (Configuracion
 * ya se abrio), sino hace un fetch propio bajo demanda (no todos los
 * usuarios abren Configuracion antes de intentar pegar una imagen).
 */
async function activeModelSupportsImages() {
  let models = _allModelsCache;
  if (!models) {
    try {
      const resp = await apiFetch(`${API_BASE}/api/models`);
      const data = await resp.json();
      models = data.models || [];
    } catch {
      models = [];
    }
  }
  const current = models.find((m) => isSelectedModel(m));
  // Sin modelo seleccionado explicitamente (usa el default del backend) o
  // sin dato del roster: se permite intentar -- el backend es la fuente
  // de verdad final: es preferible una falla explicita del proveedor de IA antes
  // que bloquear un caso legitimo por un roster que todavia no cargo.
  if (!current) return true;
  return Boolean(current.supports_image_input);
}

/**
 * REQ-003: redimensiona (maximo MAX_IMAGE_DIMENSION de lado mas largo) y
 * recomprime a JPEG en el navegador via Canvas -- reduce el payload antes
 * de que exista cualquier limite real de tamano, sin depender de que el
 * archivo original ya viniera liviano (una captura 4K sin comprimir
 * pesaria varios MB en base64 sin este paso).
 */
function compressImageFile(file) {
  return new Promise((resolve, reject) => {
    const img = new Image();
    const reader = new FileReader();
    reader.onerror = () => reject(new Error("No se pudo leer el archivo"));
    reader.onload = () => {
      img.onerror = () => reject(new Error("No se pudo decodificar la imagen"));
      img.onload = () => {
        let { width, height } = img;
        if (width > MAX_IMAGE_DIMENSION || height > MAX_IMAGE_DIMENSION) {
          const scale = MAX_IMAGE_DIMENSION / Math.max(width, height);
          width = Math.round(width * scale);
          height = Math.round(height * scale);
        }
        const canvas = document.createElement("canvas");
        canvas.width = width;
        canvas.height = height;
        canvas.getContext("2d").drawImage(img, 0, 0, width, height);
        resolve(canvas.toDataURL("image/jpeg", IMAGE_JPEG_QUALITY));
      };
      img.src = reader.result;
    };
    reader.readAsDataURL(file);
  });
}

/** REQ-005: refresca la fila de miniaturas pendientes de enviar. */
function renderPendingImages() {
  pendingImagesRowEl.innerHTML = "";
  if (pendingImages.length === 0) {
    pendingImagesRowEl.classList.add("hidden");
    return;
  }
  pendingImagesRowEl.classList.remove("hidden");
  for (const img of pendingImages) {
    const wrap = document.createElement("div");
    wrap.className = "pending-image-thumb";
    const thumb = document.createElement("img");
    thumb.src = img.dataUri;
    const removeBtn = document.createElement("button");
    removeBtn.type = "button";
    removeBtn.className = "pending-image-remove";
    removeBtn.title = "Quitar imagen";
    removeBtn.textContent = "✕";
    removeBtn.onclick = () => {
      pendingImages = pendingImages.filter((p) => p.id !== img.id);
      renderPendingImages();
    };
    wrap.append(thumb, removeBtn);
    pendingImagesRowEl.appendChild(wrap);
  }
}

/**
 * Punto de entrada compartido por paste/drop/file-picker (REQ-001).
 * `files`: FileList o array de File -- se filtra a solo imagenes.
 */
async function addImageFiles(files) {
  const imageFiles = Array.from(files).filter((f) => f.type.startsWith("image/"));
  if (imageFiles.length === 0) return;

  // REQ-002: se valida UNA vez por lote (no por archivo) -- si el modelo
  // activo no soporta imagenes, se bloquean TODAS antes de procesar
  // ninguna (evita comprimir de mas para nada).
  const supported = await activeModelSupportsImages();
  if (!supported) {
    showImageBlockedNotice(
      "Este modelo no soporta imágenes — cambiá a gpt-5-mini/gemini/claude-sonnet-5 para usar esta función."
    );
    return;
  }

  for (const file of imageFiles) {
    try {
      const dataUri = await compressImageFile(file);
      pendingImages.push({ dataUri, id: crypto.randomUUID() });
    } catch (err) {
      console.warn("No se pudo procesar una imagen:", err);
    }
  }
  renderPendingImages(); // REQ-004: multiples imagenes se acumulan, no se reemplazan
}

// REQ-001: pegar (Ctrl+V) -- el evento 'paste' del textarea trae los
// archivos en clipboardData.files/items.
inputEl.addEventListener("paste", (e) => {
  const files = e.clipboardData?.files;
  if (files && files.length > 0) {
    const hasImage = Array.from(files).some((f) => f.type.startsWith("image/"));
    if (hasImage) {
      e.preventDefault(); // evita que el navegador intente "pegar" el binario como texto
      addImageFiles(files);
    }
  }
});

// REQ-001: arrastrar y soltar sobre el area del chat completo (no solo el
// textarea) -- mismo criterio que WhatsApp Web/Telegram Web.
["dragover", "drop"].forEach((evt) => {
  document.querySelector(".chat-main")?.addEventListener(evt, (e) => e.preventDefault());
});
document.querySelector(".chat-main")?.addEventListener("drop", (e) => {
  const files = e.dataTransfer?.files;
  if (files && files.length > 0) addImageFiles(files);
});

// REQ-001: boton de adjuntar -> selector de archivos nativo.
attachImageBtnEl.addEventListener("click", () => imageFileInputEl.click());
imageFileInputEl.addEventListener("change", () => {
  if (imageFileInputEl.files.length > 0) addImageFiles(imageFileInputEl.files);
  imageFileInputEl.value = ""; // permite volver a elegir el mismo archivo dos veces seguidas
});

formEl.addEventListener("submit", (e) => {
  e.preventDefault();
  // Pedido explicito del usuario: ya no se puede encolar un mensaje nuevo
  // mientras Vision sigue procesando el turno anterior -- hay que esperar
  // a que termine (vuelve a "idle" via setSendButtonState) o presionar
  // "Detener" primero. Antes esto SI se permitia (ver REQ-003 de la spec
  // parada-y-cola-mensajes, comentario viejo en sendMessage sobre
  // `state.sending` como contador) -- cambio de comportamiento deliberado.
  if (sendOrStopBtnEl.dataset.state !== "idle") return;

  const text = inputEl.value;
  const images = pendingImages.map((p) => p.dataUri);
  inputEl.value = "";
  pendingImages = [];
  renderPendingImages();
  // spec compositor-expandible REQ-004: NO se llama toggleComposerExpanded
  // aca -- el estado expandido persiste entre mensajes a proposito. Si
  // esta en auto-grow normal, se recalcula la altura (vuelve a chico
  // porque el texto ya se vacio arriba).
  autoGrowComposer();
  sendMessage(text, { images: images.length > 0 ? images : null });
});

inputEl.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    formEl.requestSubmit();
  }
});

// =========================================================================
// Compositor expandible (spec compositor-expandible, REQ-001 a REQ-005):
// auto-grow con techo mientras se escribe + boton ampliar/reducir que
// fuerza una altura grande fija. REQ-005 (Enter/Shift+Enter sin cambios)
// ya esta cubierto arriba -- este bloque solo toca `height`, nunca el
// manejo de teclas de envio.
// =========================================================================
const composerExpandBtnEl = document.getElementById("composer-expand-btn");

const COMPOSER_NORMAL_MAX_RATIO = 0.4; // REQ-002: techo del auto-grow (~40% del alto visible)
const COMPOSER_EXPANDED_HEIGHT_VH = 50; // REQ-003: altura fija al ampliar (~50vh)

let composerExpanded = false;

/**
 * REQ-001/002: crece linea por linea con el contenido real (scrollHeight),
 * respetando un techo -- nunca se aplica mientras esta EXPANDIDO (REQ-003:
 * la altura expandida es fija, independiente del texto).
 */
function autoGrowComposer() {
  if (composerExpanded) return;
  inputEl.style.height = "auto"; // resetea para medir el scrollHeight real (si no, solo crece, nunca achica al borrar texto)
  const chatMainEl = document.querySelector(".chat-main");
  const maxHeight = (chatMainEl ? chatMainEl.clientHeight : window.innerHeight) * COMPOSER_NORMAL_MAX_RATIO;
  const newHeight = Math.min(inputEl.scrollHeight, maxHeight);
  inputEl.style.height = `${newHeight}px`;
}

/**
 * REQ-003: alterna entre auto-grow normal y una altura GRANDE fija --
 * click en ↕ expande (icono pasa a ↙), click en ↙ vuelve a auto-grow
 * (icono vuelve a ↕). REQ-004: el estado persiste hasta que el usuario lo
 * reduzca -- no hay ningun reset automatico al enviar un mensaje (ver
 * ausencia deliberada de esta funcion en el submit handler).
 */
function toggleComposerExpanded() {
  composerExpanded = !composerExpanded;
  composerExpandBtnEl.classList.toggle("expanded", composerExpanded);
  composerExpandBtnEl.textContent = composerExpanded ? "↙" : "↕";
  composerExpandBtnEl.title = composerExpanded ? "Reducir compositor" : "Ampliar compositor";

  if (composerExpanded) {
    inputEl.style.height = `${COMPOSER_EXPANDED_HEIGHT_VH}vh`;
  } else {
    autoGrowComposer(); // vuelve al auto-grow segun el contenido actual
  }
}

inputEl.addEventListener("input", autoGrowComposer);
composerExpandBtnEl.addEventListener("click", toggleComposerExpanded);

// Restaura mensajes previos de esta sesion (si la pagina se recargo sin
// cambiar de sessionId -- ej. F5 dentro de SharePoint) antes de agregar la
// nota de "sesion iniciada". Se renderiza sin volver a persistir (ya estan
// guardados) para no reescribir localStorage con lo mismo que se acaba de leer.
for (const entry of chatHistory) {
  appendMessage(entry.role, entry.text, { persist: false, messageId: entry.messageId, time: entry.time, responseType: entry.responseType || null, technical: entry.technical || null, errorKind: entry.errorKind || null });
}

// REQ-002 (spec gestion-sesiones): nombre real si ya existe en el
// indice (sesion retomada), sino fallback a las siglas del UUID hasta
// que haya un primer mensaje o un renombre manual.
updateMetaSessionName(loadSessionsIndex()[state.sessionId]?.name || state.sessionId.slice(0, 8));
appendMessage("system-note", `Sesión iniciada (${state.sessionId.slice(0, 8)}). Backend: ${API_BASE}`, { persist: false });

const storedModel = loadSelectedModel();
if (storedModel) {
  state.selectedModel = storedModel;
  renderModelChip();
}

updateCacheMeter();
checkBackendStatus();

// =========================================================================
// NUEVO: sidebar tipo OpenCode (Sesion/Contexto/Conectores), Configuracion
// (modal con tabs), exploradores de filesystem/SharePoint, comando "/" de
// skills, y controles de UI (engranaje/ojo/pantalla completa). Todo lo de
// arriba (markdown, dashboard, cache, selector de modelo, todos, tool-log)
// sigue intacto -- este bloque solo agrega funcionalidad nueva.
// =========================================================================

// -- Referencias DOM nuevas ------------------------------------------------
const settingsBtnEl = document.getElementById("settings-btn");
const fullscreenBtnEl = document.getElementById("fullscreen-btn");
const eyeBtnEl = document.getElementById("eye-btn");
const appShellEl = document.getElementById("app-shell");
const footerProjectBtnEl = document.getElementById("footer-project-path");
const footerProjectTextEl = document.getElementById("footer-project-text");
const footerModelBtnEl = document.getElementById("footer-model");
const footerModelTextEl = document.getElementById("footer-model-text");

const sessionSummaryEl = document.getElementById("session-summary");
const sessionListEl = document.getElementById("session-list");
const sessionNewBtnEl = document.getElementById("session-new-btn");
const sessionSearchInputEl = document.getElementById("session-search-input");
const sessionSearchResultsEl = document.getElementById("session-search-results");
const searchNavBarEl = document.getElementById("search-nav-bar");
const searchNavCountEl = document.getElementById("search-nav-count");
const searchNavPrevEl = document.getElementById("search-nav-prev");
const searchNavNextEl = document.getElementById("search-nav-next");
const searchNavCloseEl = document.getElementById("search-nav-close");

const contextSummaryEl = document.getElementById("context-summary");
const contextFillEl = document.getElementById("context-fill");
const contextDetailEl = document.getElementById("context-detail");

const connectorsSummaryEl = document.getElementById("connectors-summary");
const connectorsListEl = document.getElementById("connectors-list");

const todoSummaryEl = document.getElementById("todo-summary");
const toolsSummaryEl = document.getElementById("tools-summary");

const settingsOverlayEl = document.getElementById("settings-overlay");
const settingsCloseEl = document.getElementById("settings-close");
const settingsTabs = document.querySelectorAll(".settings-tab");
const settingsPanes = document.querySelectorAll(".settings-pane");

const explorerOverlayEl = document.getElementById("explorer-overlay");
const explorerCloseEl = document.getElementById("explorer-close");
const explorerTitleEl = document.getElementById("explorer-title");
const explorerEyebrowEl = document.getElementById("explorer-eyebrow");
const explorerBreadcrumbEl = document.getElementById("explorer-breadcrumb");
const explorerListEl = document.getElementById("explorer-list");
const explorerSelectBtnEl = document.getElementById("explorer-select-btn");
const explorerStatusEl = document.getElementById("explorer-status");
const explorerNewFolderBtnEl = document.getElementById("explorer-new-folder-btn");

const slashMenuEl = document.getElementById("slash-menu");

// -------------------------------------------------------------------------
// Sesiones: un indice liviano en localStorage (id -> {name, updatedAt})
// separado del historial completo (que ya vive en `vision.session.<id>`,
// ver CACHE_STORAGE_KEY mas arriba). Permite listar/click sin cargar todo
// el historial de cada sesion.
// -------------------------------------------------------------------------
const SESSIONS_INDEX_KEY = "vision.sessionsIndex";

function loadSessionsIndex() {
  try {
    return JSON.parse(localStorage.getItem(SESSIONS_INDEX_KEY) || "{}");
  } catch {
    return {};
  }
}

function saveSessionsIndex(index) {
  try {
    localStorage.setItem(SESSIONS_INDEX_KEY, JSON.stringify(index));
  } catch {
    // noop
  }
}

function touchCurrentSessionInIndex(sessionId = state.sessionId, fallbackName = null) {
  // BUG real reportado por el usuario 2026-09-16 ("pregunte en una sesion
  // nueva, me cambie de sesion, y al volver no estaba listada"): esta
  // funcion tocaba SIEMPRE `state.sessionId` (la sesion ACTIVA en el
  // momento en que se ejecuta), no la sesion que realmente origino el
  // turno. Los callers que esperan (`await`) el fin de una respuesta antes
  // de llamar aca (ver el wrapper de `sendMessage` y los reenganches SSE
  // mas abajo) corren en ese punto DESPUES de que el usuario pudo haberse
  // cambiado de sesion -- si eso pasaba, la sesion que hizo la pregunta
  // nunca se agregaba al indice (quedaba fantasma, invisible en el panel
  // "Sesion"), y en cambio se pisaba el timestamp de la sesion nueva que
  // ni siquiera tuvo actividad. Fix: cada caller pasa explicitamente el id
  // de la sesion dueña de ese turno (capturado ANTES del await), igual
  // que ya hace el backend con `turnSessionId` para no persistir en la
  // sesion equivocada. El default preserva el comportamiento viejo para
  // cualquier caller que todavia no pueda pasarlo.
  const index = loadSessionsIndex();
  const existing = index[sessionId] || {};
  // REQ-006 (spec gestion-sesiones): `existing.name` ya cubre "no pisar un
  // nombre existente" (incluye tanto un renombre manual como un nombre
  // autogenerado antes), pero se preserva `manuallyRenamed` explicitamente
  // ademas -- sin esto, cada llamada reescribe la entrada completa del
  // indice y se perderia ese flag en la proxima vez que se consulte.
  //
  // `chatHistory` (global) solo es fuente valida para el nombre cuando
  // `sessionId` coincide con la sesion ACTIVA -- si el caller esta
  // tocando una sesion distinta (el mismo motivo por el que ahora recibe
  // `sessionId` explicito), `chatHistory` ya tiene los mensajes de la
  // sesion nueva, no los de esta. Por eso el caller puede pasar
  // `fallbackName` (ej. el texto tal cual se mando) para ese caso.
  const firstUserMsg = sessionId === state.sessionId ? chatHistory.find((m) => m.role === "user") : null;
  const name = existing.name || (firstUserMsg ? firstUserMsg.text.slice(0, 40) : null) || fallbackName || "Nueva conversación";
  index[sessionId] = { ...existing, name, updatedAt: Date.now() };
  saveSessionsIndex(index);
  renderSessionsPanel();
}

// Bug real reportado por el usuario: el sidebar solo listaba sesiones del
// indice liviano de localStorage (`vision.sessionsIndex`), que es propio
// de cada navegador/perfil -- abrir Vision en un navegador DISTINTO en la
// misma maquina mostraba el sidebar vacio pese a que esas sesiones ya
// estaban persistidas en SQLite (compartida a nivel de maquina via
// `%USERPROFILE%\.vision\vision.db`). `renderSessionsPanel()` sigue
// pintando SINCRONO desde localStorage primero (cache, responde al
// instante) y dispara en paralelo `refreshSessionsFromBackend()`, que
// fusiona el listado real de GET /api/sessions/all -- localStorage sigue
// existiendo como cache, ya NO como unica fuente de verdad.
let _sessionsBackendSyncInFlight = false;
async function refreshSessionsFromBackend() {
  if (_sessionsBackendSyncInFlight) return; // evita fetches solapados si se llama seguido
  _sessionsBackendSyncInFlight = true;
  try {
    const resp = await apiFetch(`${API_BASE}/api/sessions/all`);
    if (!resp.ok) return;
    const data = await resp.json();
    const remoteSessions = data.sessions || [];
    if (remoteSessions.length === 0) return;

    const index = loadSessionsIndex();
    let changed = false;
    for (const s of remoteSessions) {
      const updatedAtMs = (s.updated_at || 0) * 1000; // SQLite guarda epoch segundos
      const existing = index[s.id];
      // No pisa un nombre puesto a mano ni una entrada mas reciente que
      // la que ya tenia el cache local (ej. la pestaña actual acaba de
      // mandar un mensaje que el backend todavia no termino de persistir).
      if (existing && existing.updatedAt >= updatedAtMs) continue;
      index[s.id] = {
        name: existing?.manuallyRenamed ? existing.name : (s.title || existing?.name || s.id.slice(0, 8)),
        updatedAt: updatedAtMs,
        manuallyRenamed: existing?.manuallyRenamed || false,
        projectPath: s.project_path || existing?.projectPath,
      };
      changed = true;
    }
    if (changed) {
      saveSessionsIndex(index);
      renderSessionsPanel();
    }
  } catch {
    // Sin conexion o el backend no responde -- el sidebar sigue mostrando
    // el cache local, no es un error bloqueante.
  } finally {
    _sessionsBackendSyncInFlight = false;
  }
}

function renderSessionsPanel() {
  const index = loadSessionsIndex();
  const entries = Object.entries(index).sort((a, b) => b[1].updatedAt - a[1].updatedAt);

  const currentName = index[state.sessionId]?.name || "nueva";
  sessionSummaryEl.textContent = currentName.slice(0, 14);
  updateMetaSessionName(currentName);

  sessionListEl.innerHTML = "";
  if (entries.length === 0) {
    sessionListEl.innerHTML = '<li class="session-list-empty">Sin sesiones guardadas aún</li>';
    return;
  }
  for (const [id, info] of entries) {
    const li = document.createElement("li");
    li.className = id === state.sessionId ? "active" : "";
    li.title = "Ir a esta sesión";
    const nameSpan = document.createElement("span");
    nameSpan.className = "session-name";
    nameSpan.textContent = info.name;
    const dateSpan = document.createElement("span");
    dateSpan.className = "session-date";
    dateSpan.textContent = new Date(info.updatedAt).toLocaleDateString("es-AR", { day: "2-digit", month: "2-digit" });

    // REQ-001 (spec gestion-sesiones): icono de renombrar, ANTES del de
    // basura (orden de lectura "editar, despues eliminar").
    const renameBtn = document.createElement("button");
    renameBtn.type = "button";
    renameBtn.className = "session-rename-btn";
    renameBtn.title = "Renombrar esta sesión";
    renameBtn.textContent = "✎";
    renameBtn.onclick = (e) => {
      e.stopPropagation();
      startEditingSessionName(id, nameSpan);
    };

    // Icono de basura: elimina esta sesion por completo (backend +
    // historial local + indice). Se agrega ahora -- quedo pendiente de
    // una tarea anterior que no llego a implementarse en el sidebar.
    const deleteBtn = document.createElement("button");
    deleteBtn.type = "button";
    deleteBtn.className = "session-delete-btn";
    deleteBtn.title = "Eliminar esta sesión";
    deleteBtn.textContent = "🗑";
    deleteBtn.onclick = (e) => {
      e.stopPropagation(); // no disparar el li.onclick (cambiar de sesion)
      deleteSession(id, info.name);
    };

    li.append(nameSpan, dateSpan, renameBtn, deleteBtn);
    li.onclick = () => switchToSession(id);
    sessionListEl.appendChild(li);
  }
}

// =========================================================================
// Búsqueda de conversaciones (spec busqueda-mensajes, REQ-001 a REQ-006)
//
// Dos capas: (1) busqueda GLOBAL contra `/api/sessions/search` (SQLite, si
// hay persistencia) con fallback a recorrer `vision.session.<id>` de
// localStorage; (2) resaltado + navegacion DENTRO de la sesion abierta si
// esta cae entre los resultados -- ambas activadas por el MISMO input.
// =========================================================================
const SEARCH_DEBOUNCE_MS = 300;
let searchDebounceTimer = null;
let searchState = { term: "", matches: [], activeIndex: -1 };

function highlightText(text, term) {
  // Escapa el termino para uso literal en RegExp (no se ofrece busqueda
  // por regex, ver spec REQ-005 -- cualquier caracter especial del
  // usuario debe tratarse como texto plano).
  const escaped = term.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return text.replace(new RegExp(`(${escaped})`, "gi"), "<mark>$1</mark>");
}

async function searchAllSessionsGlobal(term) {
  try {
    const resp = await apiFetch(`${API_BASE}/api/sessions/search?q=${encodeURIComponent(term)}`);
    const data = await resp.json();
    if (data.persistence_available) return data.results;
  } catch {
    // sin backend disponible -- cae al fallback de localStorage abajo
  }

  // REQ-006: sin persistencia SQLite, se recorre el TEXTO completo de
  // cada sesion indexada en localStorage (vision.session.<id> guarda el
  // chatHistory completo de esa sesion, ver cacheStorageKey()).
  const index = loadSessionsIndex();
  const lowerTerm = term.toLowerCase();
  const results = [];
  for (const [id, info] of Object.entries(index)) {
    let history;
    try {
      history = JSON.parse(localStorage.getItem(`vision.session.${id}`) || "[]");
    } catch {
      continue;
    }
    const match = history.find((m) => (m.text || "").toLowerCase().includes(lowerTerm));
    if (!match) continue;
    const idx = match.text.toLowerCase().indexOf(lowerTerm);
    const start = Math.max(0, idx - 60);
    const end = Math.min(match.text.length, idx + term.length + 60);
    let snippet = match.text.slice(start, end).trim();
    if (start > 0) snippet = "…" + snippet;
    if (end < match.text.length) snippet = snippet + "…";
    results.push({ id, title: info.name, snippet });
    if (results.length >= 50) break;
  }
  return results;
}

function renderSearchResults(results, term) {
  sessionListEl.classList.add("hidden");
  sessionSearchResultsEl.classList.remove("hidden");
  sessionSearchResultsEl.innerHTML = "";

  if (results.length === 0) {
    sessionSearchResultsEl.innerHTML = '<li class="session-list-empty">Sin coincidencias</li>';
    return;
  }

  for (const r of results) {
    const li = document.createElement("li");
    li.className = "session-search-results-item";
    const nameEl = document.createElement("span");
    nameEl.className = "session-search-result-name";
    nameEl.textContent = r.title;
    const snippetEl = document.createElement("span");
    snippetEl.className = "session-search-result-snippet";
    snippetEl.innerHTML = highlightText(r.snippet, term);
    li.append(nameEl, snippetEl);
    li.onclick = () => openSearchResult(r.id, term);
    sessionSearchResultsEl.appendChild(li);
  }
}

async function openSearchResult(sessionId, term) {
  if (sessionId !== state.sessionId) {
    switchToSession(sessionId);
    // activateSession reconstruye el DOM de forma asincrona (fetch de
    // status incluido) -- se espera un tick antes de buscar matches en un
    // DOM que todavia se esta armando.
    await new Promise((resolve) => setTimeout(resolve, 200));
  }
  highlightMatchesInOpenSession(term);
}

// REQ-004: resalta cada ocurrencia de `term` dentro de los mensajes ya
// renderizados de la sesion ABIERTA (recorre .msg dentro de #messages) y
// arma la barra de navegacion "N de M".
function highlightMatchesInOpenSession(term) {
  clearSearchHighlights();
  if (!term) return;

  const escaped = term.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const regex = new RegExp(escaped, "gi");
  const matches = [];

  const bubbles = messagesEl.querySelectorAll(".msg");
  for (const bubble of bubbles) {
    // Solo se resalta dentro de nodos de texto -- evita corromper markup
    // ya renderizado (negritas, bloques de codigo, links) mutando su
    // innerHTML a ciegas.
    const walker = document.createTreeWalker(bubble, NodeFilter.SHOW_TEXT);
    const textNodes = [];
    let node;
    while ((node = walker.nextNode())) textNodes.push(node);

    for (const textNode of textNodes) {
      const text = textNode.nodeValue;
      if (!regex.test(text)) { regex.lastIndex = 0; continue; }
      regex.lastIndex = 0;

      const frag = document.createDocumentFragment();
      let lastIndex = 0;
      let m;
      while ((m = regex.exec(text))) {
        frag.appendChild(document.createTextNode(text.slice(lastIndex, m.index)));
        const mark = document.createElement("mark");
        mark.className = "search-match";
        mark.textContent = m[0];
        frag.appendChild(mark);
        matches.push(mark);
        lastIndex = m.index + m[0].length;
      }
      frag.appendChild(document.createTextNode(text.slice(lastIndex)));
      textNode.parentNode.replaceChild(frag, textNode);
    }
  }

  searchState = { term, matches, activeIndex: matches.length > 0 ? 0 : -1 };
  updateSearchNavBar();
  if (matches.length > 0) focusSearchMatch(0);
}

function clearSearchHighlights() {
  for (const mark of messagesEl.querySelectorAll("mark.search-match")) {
    mark.replaceWith(document.createTextNode(mark.textContent));
  }
  messagesEl.normalize(); // reune los text nodes partidos de vuelta en uno solo
  searchState = { term: "", matches: [], activeIndex: -1 };
  searchNavBarEl.classList.add("hidden");
}

function updateSearchNavBar() {
  const { matches, activeIndex } = searchState;
  if (matches.length === 0) {
    searchNavBarEl.classList.add("hidden");
    return;
  }
  searchNavBarEl.classList.remove("hidden");
  searchNavCountEl.textContent = `${activeIndex + 1} de ${matches.length}`;
}

function focusSearchMatch(index) {
  const { matches } = searchState;
  if (matches.length === 0) return;
  if (searchState.activeIndex >= 0) {
    matches[searchState.activeIndex].classList.remove("search-match-active");
  }
  searchState.activeIndex = ((index % matches.length) + matches.length) % matches.length;
  const active = matches[searchState.activeIndex];
  active.classList.add("search-match-active");
  active.scrollIntoView({ behavior: "smooth", block: "center" });
  updateSearchNavBar();
}

searchNavPrevEl.addEventListener("click", () => focusSearchMatch(searchState.activeIndex - 1));
searchNavNextEl.addEventListener("click", () => focusSearchMatch(searchState.activeIndex + 1));
searchNavCloseEl.addEventListener("click", () => {
  clearSearchHighlights();
  sessionSearchInputEl.value = "";
  sessionListEl.classList.remove("hidden");
  sessionSearchResultsEl.classList.add("hidden");
});

sessionSearchInputEl.addEventListener("input", () => {
  clearTimeout(searchDebounceTimer);
  const term = sessionSearchInputEl.value.trim();

  if (!term) {
    sessionListEl.classList.remove("hidden");
    sessionSearchResultsEl.classList.add("hidden");
    clearSearchHighlights();
    return;
  }

  // REQ-001: debounce de 300ms -- no se dispara una consulta por cada tecla.
  searchDebounceTimer = setTimeout(async () => {
    const results = await searchAllSessionsGlobal(term);
    renderSearchResults(results, term);
    // REQ-004: si la sesion ABIERTA esta entre los resultados (o el
    // termino aparece en ella aunque el backend no la liste, ej. un
    // mensaje reciente que todavia no se guardo en SQLite), se resalta
    // igual sin esperar a que el usuario haga click en un resultado.
    highlightMatchesInOpenSession(term);
  }, SEARCH_DEBOUNCE_MS);
});

// -------------------------------------------------------------------------
// REQ-001/002/003/004/005/006 (spec gestion-sesiones): renombrar una
// sesion, desde CUALQUIERA de los dos puntos de entrada (fila del sidebar
// o chip #meta-session del command-bar) -- ambos terminan en
// `commitSessionRename`, unica fuente de verdad de la escritura.
// -------------------------------------------------------------------------
const SESSION_NAME_MAX_LEN = 80;

// spec proyecto-por-sesion REQ-006: el chip pasa a 3 lineas -- nombre,
// Session ID completo (restaurado, ver Decisiones registradas de esa
// spec), y ruta de carpeta de proyecto. `updateMetaSessionName` sigue
// existiendo (mismo nombre, ya usada por varios callers) pero ahora solo
// toca la linea de nombre -- las otras 2 se actualizan con
// `refreshSessionChip()` mas abajo, que consulta el status real.
function updateMetaSessionName(name) {
  metaSessionNameEl.textContent = name;
  metaSessionNameEl.title = name;
}

function updateMetaSessionId(sessionId) {
  metaSessionIdEl.textContent = sessionId;
  metaSessionIdEl.title = sessionId;
}

function updateMetaSessionPath(path) {
  metaSessionPathEl.textContent = path;
  metaSessionPathEl.title = path;
}

/**
 * Reemplaza `nameSpan` (el <span class="session-name"> de una fila del
 * sidebar) por un <input> editable. `li` no se pasa: se navega con
 * `nameSpan.closest("li")` para no depender de que el caller lo tenga.
 */
function startEditingSessionName(sessionId, nameSpan) {
  if (nameSpan.querySelector("input")) return; // ya esta editando
  const originalName = nameSpan.textContent;

  const input = document.createElement("input");
  input.type = "text";
  input.className = "session-name-edit-input";
  input.value = originalName;
  input.maxLength = SESSION_NAME_MAX_LEN;

  nameSpan.textContent = "";
  nameSpan.appendChild(input);
  input.focus();
  input.select();

  let settled = false; // evita doble-commit entre blur y keydown Enter
  const finish = async (commit) => {
    if (settled) return;
    settled = true;
    const newName = input.value;
    nameSpan.textContent = originalName; // vuelve al span de texto plano
    if (commit) await commitSessionRename(sessionId, newName, originalName);
  };

  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); finish(true); }
    if (e.key === "Escape") { e.preventDefault(); finish(false); }
  });
  input.addEventListener("blur", () => finish(true));
}

// =========================================================================
// Modal de edicion de sesion -- click en el chip #meta-session abre este
// modal en vez de la edicion inline directa que tenia antes (specs
// gestion-sesiones/comandos-slash). Arquitectura carpeta-por-sesion
// (definitiva): permite renombrar Y cambiar la carpeta de proyecto
// PROPIA de esta sesion -- ya no hay una carpeta global unica.
// =========================================================================
const sessionEditOverlayEl = document.getElementById("session-edit-overlay");
const sessionEditNameEl = document.getElementById("session-edit-name");
const sessionEditIdEl = document.getElementById("session-edit-id");
const sessionEditPathEl = document.getElementById("session-edit-path");
const sessionEditBrowseEl = document.getElementById("session-edit-browse");
const sessionEditSaveEl = document.getElementById("session-edit-save");
const sessionEditStatusEl = document.getElementById("session-edit-status");
const sessionEditCloseEl = document.getElementById("session-edit-close");

function openSessionEditModal() {
  sessionEditNameEl.value = metaSessionNameEl.textContent;
  sessionEditIdEl.value = state.sessionId;
  sessionEditPathEl.value = metaSessionPathEl.textContent;
  sessionEditStatusEl.textContent = "";
  sessionEditOverlayEl.classList.remove("hidden");
  sessionEditNameEl.focus();
}
function closeSessionEditModal() {
  sessionEditOverlayEl.classList.add("hidden");
}
metaSessionEl.addEventListener("click", openSessionEditModal);
sessionEditCloseEl.addEventListener("click", closeSessionEditModal);
sessionEditOverlayEl.addEventListener("click", (e) => {
  if (e.target === sessionEditOverlayEl) closeSessionEditModal();
});

// Explorador de Windows nativo (mismo look que cualquier programa de
// escritorio) -- ver docstring de openNativeFolderPicker mas abajo.
sessionEditBrowseEl.addEventListener("click", () => {
  openNativeFolderPicker(sessionEditPathEl.value, (path) => {
    sessionEditPathEl.value = path;
  }, sessionEditStatusEl);
});

async function assignSessionProjectPath(sessionId, path, title) {
  const resp = await apiFetch(`${API_BASE}/api/sessions/${sessionId}/project-path`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path, title: title || "" }),
  });
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}));
    throw httpError(resp.status, err);
  }
}

// spec carpeta-por-sesion REQ-008: valida ANTES de disparar una accion
// que NECESITA carpeta (export/comandos custom/local-skills, todas
// disparadas por click humano -- record_decision es la unica de las 4
// que dispara el LLM, esa se valida del lado del backend + evento SSE
// 'project-folder-required', ver handleEvent). Si no hay carpeta, abre
// el modal de edicion de sesion y CANCELA la accion en curso -- el
// usuario simplemente reintenta el comando despues de asignarla.
async function validateProjectFolderOrPrompt() {
  try {
    const resp = await apiFetch(`${API_BASE}/api/session/${state.sessionId}/status`);
    const statusData = await resp.json();
    if (!statusData.persistence_available) {
      openSessionEditModal();
      return false;
    }
    return true;
  } catch {
    // Si ni siquiera se pudo consultar el status, se deja seguir -- el
    // error real (si lo hay) va a aparecer igual al intentar la accion.
    return true;
  }
}

// Bug real reportado por el usuario (2026-09-08): /export abria el
// dialogo nativo "Guardar como" del navegador aunque la sesion YA
// tuviera carpeta asignada -- el backend ya sabe donde guardar, no debia
// preguntar de nuevo. Nuevo flujo, en 2 caminos segun si hay carpeta:
// (1) CON carpeta asignada: fetch directo al endpoint (que ahora escribe
//     a disco del lado del backend, ver export_session_command_endpoint
//     en main.py) -- CERO dialogos, ni el del navegador ni el interno de
//     Vision, y se muestra la ruta real en el chat.
// (2) SIN carpeta asignada: SOLO en este caso se abre el explorador
//     INTERNO de Vision (openSessionEditModal, mismo componente ya usado
//     para asignar carpeta a cualquier sesion) para elegir una por
//     primera vez -- una vez asignada, se reintenta el export automatico
//     sin que el usuario tenga que volver a pedirlo.
async function exportCurrentSession() {
  const hasFolder = await validateProjectFolderOrPrompt();
  if (!hasFolder) {
    // openSessionEditModal ya quedo abierto (dentro de
    // validateProjectFolderOrPrompt) -- se reintenta el export solo
    // cuando el usuario efectivamente asigna una carpeta y guarda, no
    // con un timer ni polling: se engancha al mismo boton "Guardar" del
    // modal de sesion, una unica vez.
    const retryExportOnce = async () => {
      sessionEditSaveEl.removeEventListener("click", retryExportOnce);
      // Pequena espera: el listener real de sessionEditSaveEl (mas
      // arriba en este archivo) todavia esta corriendo su propio
      // assignSessionProjectPath async en este mismo tick de evento --
      // sin esperar, `validateProjectFolderOrPrompt` de abajo podria
      // consultar el status ANTES de que la carpeta quede guardada.
      await new Promise((resolve) => setTimeout(resolve, 500));
      const nowHasFolder = await validateProjectFolderOrPrompt();
      if (nowHasFolder) exportCurrentSession();
    };
    sessionEditSaveEl.addEventListener("click", retryExportOnce);
    return;
  }

  try {
    const resp = await apiFetch(`${API_BASE}/api/commands/export/${state.sessionId}`);
    if (!resp.ok) {
      const detail = (await httpErrorFromResponse(resp)).message;
      appendMessage("error", `No se pudo exportar la conversación. ${detail}`);
      return;
    }
    const data = await resp.json();
    appendMessage("system-note", `✓ Sesión exportada a: ${data.path}`, { persist: false });
  } catch (err) {
    appendMessage("error", `No se pudo exportar la conversación. ${friendlyErrorMessage(err)}`, { errorKind: isOfflineError(err) ? "offline" : "other" });
  }
}

sessionEditSaveEl.addEventListener("click", async () => {
  const newName = sessionEditNameEl.value.trim();
  const newPath = sessionEditPathEl.value.trim();
  sessionEditStatusEl.textContent = "Guardando…";
  sessionEditStatusEl.className = "settings-status";
  try {
    if (newName && newName !== metaSessionNameEl.textContent) {
      await commitSessionRename(state.sessionId, newName, metaSessionNameEl.textContent);
    }
    if (newPath && newPath !== metaSessionPathEl.textContent) {
      await assignSessionProjectPath(state.sessionId, newPath, newName || metaSessionNameEl.textContent);
      updateMetaSessionPath(newPath);
      updatePersistenceBanner(true);
      // BUG real detectado: faltaba refrescar el footer ("Sin carpeta
      // asignada a esta sesión") tras asignar carpeta desde este modal --
      // solo se actualizaba metaSessionPathEl (el chip de arriba), el
      // footer de abajo dependia unicamente de activateSession()/status.
      refreshProjectFooter();
    }
    sessionEditStatusEl.textContent = "Guardado ✓";
    sessionEditStatusEl.className = "settings-status ok";
    setTimeout(closeSessionEditModal, 600);
  } catch (err) {
    sessionEditStatusEl.textContent = friendlyErrorMessage(err);
    sessionEditStatusEl.className = "settings-status error";
  }
});

// spec carpeta-por-sesion REQ-008: el modal BLOQUEANTE de "Nueva sesion"
// que exigia carpeta obligatoria desde el inicio se ELIMINO -- el chat
// funciona libre sin carpeta asignada (100% en memoria). Recien al primer
// intento real de usar record_decision/comandos custom/export/
// local-skills SIN carpeta, se pide una (ver validateProjectFolderOrPrompt
// para las 3 acciones disparadas por click humano, y el evento SSE
// "project-folder-required" en handleEvent para record_decision, que lo
// dispara el LLM).

/**
 * REQ-004/005/006/007: unica fuente de verdad de un renombre. Valida
 * (REQ-005), actualiza el indice local (siempre) + SQLite via backend (si
 * hay persistencia, REQ-007), y refresca sidebar + chip sin recargar nada
 * (REQ-004). Si la validacion falla, restaura `originalName` en el
 * indice -- no hace falta tocar el DOM aca porque `finish()` ya lo dejo
 * mostrando `originalName` antes de llamar a esta funcion.
 */
async function commitSessionRename(sessionId, newName, originalName) {
  const trimmed = newName.trim();
  if (!trimmed || trimmed === originalName) return; // REQ-005: vacio o sin cambios -> no-op
  const finalName = trimmed.slice(0, SESSION_NAME_MAX_LEN);

  // REQ-007: el indice local es la fuente de verdad cuando no hay
  // persistencia SQLite -- se actualiza SIEMPRE, con o sin backend.
  const index = loadSessionsIndex();
  const existing = index[sessionId] || { updatedAt: Date.now() };
  index[sessionId] = { ...existing, name: finalName, manuallyRenamed: true };
  saveSessionsIndex(index);
  renderSessionsPanel();

  try {
    await apiFetch(`${API_BASE}/api/sessions/${sessionId}/rename`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title: finalName }),
    });
  } catch {
    // Sin persistencia disponible (backend sin carpeta de proyecto, o
    // caida de red puntual) -- el indice local ya quedo actualizado
    // arriba, que es la unica fuente de verdad posible en ese caso.
  }
}

// -------------------------------------------------------------------------
// Modal de confirmacion generico -- reemplaza confirm()/alert() nativos
// del navegador (rompian la estetica de Vision, ver pedido del usuario).
// Devuelve una Promise<boolean> para poder hacer `if (await showConfirmModal(...))`
// igual que se hacia con el confirm() nativo, sin cambiar el resto del
// flujo de las funciones que lo usan.
// -------------------------------------------------------------------------
const confirmOverlayEl = document.getElementById("confirm-overlay");
const confirmTitleEl = document.getElementById("confirm-title");
const confirmMessageEl = document.getElementById("confirm-message");
const confirmCloseEl = document.getElementById("confirm-close");
const confirmCancelBtnEl = document.getElementById("confirm-cancel-btn");
const confirmAcceptBtnEl = document.getElementById("confirm-accept-btn");

function showConfirmModal({
  title = "¿Estás seguro?",
  message = "",
  acceptLabel = "Confirmar",
  // REQ-004 (spec agentes-huerfanos-y-gestion-skills): modo "aviso puro"
  // sin opcion de continuar -- usado cuando la accion esta BLOQUEADA (ej.
  // borrar la sesion de reserva con agentes dependientes), no cuando hay
  // una decision real que tomar. Default false: no cambia el comportamiento
  // de ningun llamador existente.
  hideCancel = false,
} = {}) {
  return new Promise((resolve) => {
    confirmTitleEl.textContent = title;
    confirmMessageEl.textContent = message;
    confirmAcceptBtnEl.textContent = acceptLabel;
    confirmCancelBtnEl.classList.toggle("hidden", hideCancel);
    confirmOverlayEl.classList.remove("hidden");

    const cleanup = (result) => {
      confirmOverlayEl.classList.add("hidden");
      confirmCancelBtnEl.classList.remove("hidden");  // restaura para el proximo uso normal
      confirmAcceptBtnEl.onclick = null;
      confirmCancelBtnEl.onclick = null;
      confirmCloseEl.onclick = null;
      resolve(result);
    };

    confirmAcceptBtnEl.onclick = () => cleanup(true);
    confirmCancelBtnEl.onclick = () => cleanup(false);
    confirmCloseEl.onclick = () => cleanup(false);
  });
}
confirmOverlayEl.addEventListener("click", (e) => {
  if (e.target === confirmOverlayEl) confirmCancelBtnEl.click();
});

// Elimina una sesion por completo: backend (cancela turno en curso si
// hubiera + borra estado en memoria + borra su carpeta de vista previa),
// historial persistido en localStorage, y su entrada en el indice de
// sesiones. Si la sesion eliminada era la activa, activa una nueva vacia
// para no dejar la UI en un estado inconsistente.
async function deleteSession(sessionId, sessionName) {
  const label = sessionName || sessionId.slice(0, 8);

  // REQ-002/003/004 (spec agentes-huerfanos-y-gestion-skills): antes de
  // pedir confirmacion, se consulta cuantos agentes se verian afectados --
  // sin esto el usuario no tiene forma de saber, ANTES de confirmar, que
  // borrar esta sesion puede romper una automatizacion diaria en uso.
  let impact = { agent_count: 0, is_fallback_blocked: false, will_reassign_to_fallback: false };
  try {
    const resp = await apiFetch(`${API_BASE}/api/session/${sessionId}/agent-impact`);
    impact = await resp.json();
  } catch {
    // Si el chequeo falla (backend caido, etc.), se sigue con el flujo
    // normal sin el aviso extra -- no debe bloquear el borrado por esto.
  }

  if (impact.is_fallback_blocked) {
    // REQ-004: bloqueo explicito, decision del usuario -- nunca se deja
    // borrar la sesion de reserva mientras tenga agentes dependientes
    // reales en este momento (evita que los ya reasignados vuelvan a
    // quedar huerfanos en cadena). Mismo modal, sin boton de continuar.
    await showConfirmModal({
      title: "No se puede eliminar",
      message:
        `Esta sesión es tu sesión de reserva de agentes y tiene ${impact.agent_count} agente(s) ` +
        `asignados. Reasigná esos agentes o cambiá la sesión de reserva en Configuración > ` +
        `Agentes antes de poder eliminarla.`,
      acceptLabel: "Entendido",
      hideCancel: true,
    });
    return;
  }

  let message = `¿Eliminar la sesión "${label}"? Esta acción no se puede deshacer.`;
  if (impact.agent_count > 0) {
    message += impact.will_reassign_to_fallback
      ? ` Esta sesión tiene ${impact.agent_count} agente(s) asociado(s): se reasignarán ` +
        `automáticamente a la sesión de reserva configurada.`
      : ` Esta sesión tiene ${impact.agent_count} agente(s) asociado(s). Al no haber sesión de ` +
        `reserva configurada, quedarán marcados como "sin sesión" hasta que la configures o los ` +
        `edites a mano.`;
  }

  const confirmed = await showConfirmModal({
    title: "Eliminar sesión",
    message,
    acceptLabel: "Eliminar",
  });
  if (!confirmed) return;

  try {
    await apiFetch(`${API_BASE}/api/session/${sessionId}`, { method: "DELETE" });
  } catch (err) {
    console.warn("No se pudo eliminar la sesión en el backend:", err);
    // Se continua igual con la limpieza local -- mejor eliminarla del
    // lado del usuario aunque el backend no haya respondido, que dejarla
    // fantasma en la lista sin ninguna forma de sacarla.
  }

  try {
    localStorage.removeItem(`vision.session.${sessionId}`);
  } catch {
    // noop
  }

  const index = loadSessionsIndex();
  delete index[sessionId];
  saveSessionsIndex(index);

  if (sessionId === state.sessionId) {
    // Se elimino la sesion que estaba activa -- arranca una nueva limpia.
    await activateSession(crypto.randomUUID());
  } else {
    renderSessionsPanel();
  }
}

// Cambiar de sesion (o crear una nueva) YA NO recarga la pagina.
//
// Por que importa: `location.reload()` corta de un tajo la conexion HTTP
// del fetch de /api/chat en curso. Antes, el backend cancelaba el turno
// real al perder esa conexion (ver fix en app/main.py: el generador SSE
// tenia `finally: task.cancel()`). Ya arreglamos el lado del backend
// (el turno ahora corre en una tarea de fondo independiente de la
// conexion SSE), pero igual conviene no recargar del lado del frontend:
// asi el usuario puede ir y volver entre sesiones sin perder scroll,
// sin parpadeo de pantalla, y reconectandose al turno en progreso si
// vuelve a una sesion que sigue "pensando".
function clearMessagesDom() {
  // .turn-container reemplaza a .user-turn-wrap como unidad de limpieza
  // (cada turno = burbuja de usuario + su respuesta/tool-calls agrupadas,
  // ver turnContainers) -- limpiar solo .user-turn-wrap dejaba huerfano
  // el .turn-response-slot con la respuesta/tool-calls adentro.
  messagesEl.querySelectorAll(".turn-container, .msg, .tool-call-row, .queue-warning").forEach((el) => el.remove());
  toolLogEl.innerHTML = '<li class="tool-empty">Aún no se ejecutó ninguna tool</li>';
  todoListEl.innerHTML = '<li class="todo-empty">Sin tareas activas</li>';
  resetToolRow();
  questionPanelEl.classList.add("hidden");
  todoSummaryEl.textContent = "0";
  toolsSummaryEl.textContent = "0";
  _toolsExecutedCount = 0;
  queuedTurns.clear();
  queueWarningShown = false;
  turnContainers.clear();
  setSendButtonState("idle");
  // La barra de progreso (si habia una activa) ya se removio del DOM
  // junto con .turn-container de arriba -- se limpia tambien la
  // referencia JS para no dejarla apuntando a un nodo huerfano.
  activeProgressBar = null;
  stopProgressTimer();
  // spec busqueda-mensajes: los .msg de abajo ya se removieron arriba
  // (junto con cualquier <mark> adentro) -- se resetea el estado JS/barra
  // de navegacion para no dejarlos apuntando a nodos que ya no existen.
  searchState = { term: "", matches: [], activeIndex: -1 };
  searchNavBarEl.classList.add("hidden");
}

async function activateSession(sessionId) {
  state.sessionId = sessionId;
  try {
    localStorage.setItem(SESSION_ID_KEY, sessionId);
  } catch {
    // noop
  }
  // Arquitectura carpeta-por-sesion: el footer de ruta de proyecto
  // depende de state.sessionId -- se refresca en cada cambio de sesion,
  // no solo al cargar la pagina.
  refreshProjectFooter();
  // spec agentes-programados: el panel lateral "Agentes" es por-sesion --
  // se refiltra sobre el cache ya cargado, sin golpear la API de nuevo.
  renderSessionAgentsSidebar();

clearMessagesDom();
  chatHistory.length = 0;
  // AUTORIDAD del historial: el SQLite local de la sesion
  // (`<proyecto>/session/vision.db`, spec persistencia-sqlite-sesiones y
  // carpeta-por-sesion), no la cache del navegador. Al cambiar de sesion
  // (o volver a una cuyo turno termino en background mientras estabamos en
  // otra) se lee de SQLite SIEMPRE -- asi los resultados de los turnos
  // procesados en background por-sesion quedan visibles al volver, sin
  // depender de que la cache localStorage se haya escrito bien. La cache
  // local queda SOLO como fallback para el caso sin conexion. Bug real
  // reportado por el usuario: "deberia procesar todo en background pero
  // visualizar las cosas de la sesion en la que estoy parado".
  let history;
  try {
    const resp = await apiFetch(`${API_BASE}/api/sessions/${sessionId}/messages`);
    const data = resp.ok ? await resp.json() : null;
    const messages = data?.messages;
    history = messages && messages.length > 0 ? messages : loadChatHistoryForSession(sessionId);
  } catch {
    // Sin conexion al backend -- se cae a la cache local de este navegador.
    history = loadChatHistoryForSession(sessionId);
  }
  chatHistory.push(...history);
  persistChatHistory(); // sincroniza la cache local con lo que devolvio SQLite (o el fallback)
  for (const entry of chatHistory) {
    appendMessage(entry.role, entry.text, { persist: false, messageId: entry.messageId, time: entry.time, responseType: entry.responseType || null, technical: entry.technical || null, errorKind: entry.errorKind || null });
  }

  // BUG real corregido (deteccion QA 2026-09-14): el panel "Tareas" solo se
  // actualizaba via eventos SSE "todo" en vivo -- al activar/volver a una
  // sesion no se re-consultaba su plan real, asi que si esa sesion procesaba
  // en background mientras el usuario estaba en OTRA (donde el evento se
  // ignoraba, ver el guard nuevo en handleEvent), al volver el panel
  // quedaba vacio/desactualizado aunque el backend SI tenia el plan guardado
  // (`GET /api/todos/{id}`, ya existente). Se re-sincroniza siempre al
  // activar cualquier sesion, sea que este ocupada o no.
  try {
    const todosResp = await apiFetch(`${API_BASE}/api/todos/${sessionId}`);
    const todosData = todosResp.ok ? await todosResp.json() : null;
    renderTodos(todosData?.todos || []);
  } catch {
    renderTodos([]);
  }

  // spec proyecto-por-sesion REQ-006: el Session ID se muestra INSTANTANEO
  // (no depende de red) -- la ruta de carpeta si necesita el fetch de
  // status de abajo, porque se resuelve del lado del backend.
  updateMetaSessionId(sessionId);

  // REQ-002 (spec gestion-sesiones): renderSessionsPanel() ya actualiza
  // el chip con el nombre real (updateMetaSessionName) -- no hace falta
  // pisarlo aca con las siglas del UUID.
  updateCacheMeter();
  renderSessionsPanel();
  if (document.getElementById("acc-context").open) refreshContextUsage();

  // Si esta sesion sigue procesando un turno en background (ver fix del
  // bug: crear/cambiar de sesion ya no lo cancela), nos re-enganchamos al
  // stream SSE en curso en vez de dejar al usuario sin ver nada hasta que
  // "misteriosamente" aparezca la respuesta completa la proxima vez que
  // entre a esta sesion.
  try {
    const statusResp = await apiFetch(`${API_BASE}/api/session/${sessionId}/status`);
    const statusData = await statusResp.json();
    // REQ-006 (spec persistencia-sqlite-sesiones): el status ya trae
    // `persistence_available` -- se reusa esta misma consulta (que ya se
    // hace al activar cualquier sesion) en vez de agregar un fetch extra.
    updatePersistenceBanner(statusData.persistence_available);
    // spec proyecto-por-sesion REQ-006: idem para `project_path` -- ya
    // resuelto del lado del backend (carpeta propia, default, o raiz de
    // Vision), no se recalcula nada aca.
    if (statusData.project_path) updateMetaSessionPath(statusData.project_path);
    // spec modo-plan-loop REQ-001/005/009: el chip de modo y el banner de
    // alarma se sincronizan con el estado REAL de la sesion (backend),
    // nunca con un valor local que se desincronizaria al cambiar de
    // sesion o recargar la pagina.
    updateModeChip(statusData.mode || "active");
    // BUG real corregido (deteccion QA 2026-09-14): `_loopRunning`/
    // `_loopAutoPref` (el estado del toggle "Loop"/"Auto" de Accesos
    // rapidos) NUNCA se sincronizaban con el backend aca -- solo se
    // seteaban dentro de `startLoop()` mismo. Al cambiar de sesion (o
    // recargar la pagina, ver el mismo fix en `reattachIfBusyOnLoad` mas
    // abajo), el toggle SIEMPRE arrancaba en su default `false`, aunque el
    // loop siguiera realmente activo (corriendo o pausado esperando una
    // pregunta) del lado del backend. Reportado por el usuario: "quedan
    // prendidos los loop cuando refresco la pagina" -- y si tocaba "Loop"
    // pensando que lo estaba INICIANDO (porque el toggle mentia mostrando
    // apagado), en realidad llamaba a `startLoop()` de nuevo sobre un loop
    // YA activo, abriendo una SEGUNDA conexion SSE redundante al mismo
    // turno -- eso duplicaba la renderizacion de la respuesta (cada
    // conexion dibuja su propia burbuja/badges para los mismos eventos,
    // via el fan-out de `session.subscribe()`), el otro sintoma reportado.
    _loopRunning = !!statusData.loop_active;
    _loopAutoPref = !!statusData.loop_auto;
    if (quickPanelLooprunToggleEl && quickPanelLoopautoToggleEl) syncQuickPanelLoopState();
    if (statusData.loop_active && statusData.loop_paused_for_question) {
      showLoopBanner();
    } else {
      hideLoopBanner();
    }
    // Bug real reportado por el usuario 2026-09-16 ("dos respuestas
    // paralelas"): este reenganche NO sumaba a `state.sending`, asi que el
    // poll de fondo (pollActiveSessionForExternalActivity, ver su propio
    // comentario) no tenia forma de saber que YA habia una conexion viva
    // aca y podia abrir una segunda -- dos streams a la misma sesion
    // duplican cada burbuja. Ahora se suma/resta igual que sendMessage().
    if (statusData.busy && state.sending === 0) {
      state.sending += 1;
      try {
        setConnStatus("busy", "Vision sigue pensando en esta sesión…");
        resetToolRow();
        const resp = await apiFetch(`${API_BASE}/api/chat/stream/${sessionId}`);
        await renderIncomingStream(resp, statusData.active_turn_id);
        if (connStatusEl.dataset.state === "busy") setConnStatus("online", "Vision en línea");
        touchCurrentSessionInIndex(sessionId);
      } finally {
        state.sending -= 1;
        if (state.sending < 0) state.sending = 0;
      }
    }
  } catch {
    // Si falla la reconexion no es critico -- el usuario puede reintentar
    // simplemente reabriendo el acordeon de Sesion.
  }
}

// -------------------------------------------------------------------------
// REQ-006 (spec persistencia-sqlite-sesiones): banner persistente + aviso
// nativo del navegador mientras NO haya carpeta de proyecto elegida (nada
// que guardar el historial en disco). `state.persistenceAvailable` se
// actualiza cada vez que se consulta /status (activar sesion, reconexion,
// polling periodico) y el listener de `beforeunload` lo lee en el momento
// de cerrar/recargar -- sin depender de una unica consulta desactualizada.
// -------------------------------------------------------------------------
function updatePersistenceBanner(available) {
  state.persistenceAvailable = available;
  persistenceBannerEl.classList.toggle("hidden", available !== false);
}

// Arquitectura carpeta-por-sesion: la carpeta ya no vive en Configuracion
// (tab "project" eliminado) -- el link del banner ahora abre el modal de
// edicion de ESTA sesion, donde SI se puede asignar/cambiar su carpeta.
persistenceBannerSettingsLinkEl.addEventListener("click", (e) => {
  e.preventDefault();
  openSessionEditModal();
});

window.addEventListener("beforeunload", (e) => {
  // Solo interrumpe el cierre si YA sabemos que no hay donde persistir
  // (evita falsos positivos mientras la primera consulta a /status
  // todavia no respondio, ej. justo al abrir la pagina).
  if (state.persistenceAvailable === false && chatHistory.length > 0) {
    e.preventDefault();
    e.returnValue = "";
  }
});

function switchToSession(sessionId) {
  if (sessionId === state.sessionId) return;
  activateSession(sessionId);
}

// spec carpeta-por-sesion REQ-008: sin modal bloqueante -- se activa la
// sesion nueva directo (100% en memoria hasta que se le asigne carpeta,
// ya sea manualmente desde el chip de sesion o automaticamente cuando
// haga falta, ver validateProjectFolderOrPrompt/evento SSE).
sessionNewBtnEl.addEventListener("click", () => {
  activateSession(crypto.randomUUID());
});

// -------------------------------------------------------------------------
// Contexto/tokens: consulta el backend (estimacion, ver app/token_estimate.py)
// -------------------------------------------------------------------------
async function refreshContextUsage() {
  try {
    const resp = await apiFetch(`${API_BASE}/api/context/${state.sessionId}`);
    const data = await resp.json();
    const pct = data.pct_used || 0;
    contextSummaryEl.textContent = `${pct}%`;
    contextFillEl.style.width = `${pct}%`;
    contextFillEl.classList.toggle("warn", pct >= 60 && pct < 85);
    contextFillEl.classList.toggle("critical", pct >= 85);
    contextDetailEl.textContent = `${data.tokens.toLocaleString("es-AR")} / ${data.context_window.toLocaleString("es-AR")} tokens (estimado)`;
  } catch {
    contextDetailEl.textContent = "No se pudo calcular el contexto.";
  }
}

// -------------------------------------------------------------------------
// Conectores: estado online/error de Oracle/BigQuery/SharePoint/SAP/Graph
// (reemplazo funcional del panel "LSP" de OpenCode -- ver connectors_status.py)
// -------------------------------------------------------------------------
const CONNECTOR_LABELS = {
  oracle: "Oracle", bigquery: "BigQuery", sharepoint: "SharePoint", sap: "SAP", graph: "Graph",
};

async function refreshConnectorsStatus() {
  connectorsListEl.innerHTML = '<li><span class="connector-dot checking"></span>Verificando…</li>';
  try {
    const resp = await apiFetch(`${API_BASE}/api/connectors/status`);
    const data = await resp.json();
    const connectors = data.connectors || [];
    const onlineCount = connectors.filter((c) => c.status === "online").length;
    connectorsSummaryEl.textContent = `${onlineCount}/${connectors.length}`;

    connectorsListEl.innerHTML = "";
    connectors.forEach((c) => {
      const li = document.createElement("li");
      const dot = document.createElement("span");
      dot.className = `connector-dot ${c.status}`;
      const label = document.createElement("span");
      label.textContent = CONNECTOR_LABELS[c.name] || c.name;
      li.append(dot, label);
      li.title = c.detail || c.status;
      connectorsListEl.appendChild(li);
    });
  } catch (err) {
    connectorsSummaryEl.textContent = "sin datos";
    renderListError(connectorsListEl, `No se pudo consultar el estado de las conexiones. ${friendlyErrorMessage(err)}`, "");
  }
}

// Los paneles de acordeon consultan datos reales recien al abrirse (evita
// pegarle a 5 conectores + estimar tokens en cada carga de pagina si el
// usuario nunca los mira).
document.getElementById("acc-context").addEventListener("toggle", function () {
  if (this.open) refreshContextUsage();
});
document.getElementById("acc-connectors").addEventListener("toggle", function () {
  if (this.open) refreshConnectorsStatus();
});

// -------------------------------------------------------------------------
// Resumenes compactos de Tareas/Herramientas en el summary del <details>
// (se actualizan enganchando las funciones ya existentes renderTodos/logTool
// via un pequeño wrapper, sin modificar su firma ni su uso previo).
// -------------------------------------------------------------------------
const _originalRenderTodos = renderTodos;
renderTodos = function (todos) {
  _originalRenderTodos(todos);
  const active = (todos || []).filter((t) => t.status !== "cancelled").length;
  todoSummaryEl.textContent = String(active);
  // Bug real reportado por el usuario ("no veo ni el panel holografico ni
  // las tareas"): el acordeon "Tareas" nunca se auto-abria, quedaba
  // colapsado mostrando solo el numero del summary -- si el usuario no
  // sabia que hacer click ahi, literalmente no veia ninguna tarea/sub-
  // tarea. Pedido explicito: auto-abrir SIEMPRE que lleguen tareas nuevas
  // (no solo la 1ra vez), para que sea imposible perderselo.
  const accTodoEl = document.getElementById("acc-todo");
  if (accTodoEl && active > 0) accTodoEl.open = true;
};

const _originalLogTool = logTool;
logTool = function (name, resultPreview) {
  _originalLogTool(name, resultPreview);
  _toolsExecutedCount += 1;
  toolsSummaryEl.textContent = String(_toolsExecutedCount);
};

// -------------------------------------------------------------------------
// Footer: ruta de proyecto de la SESION ACTUAL (arquitectura
// carpeta-por-sesion: ya no hay "proyecto global" en /api/config/project,
// se usa /api/session/{id}/status que ya resuelve la carpeta propia de
// cada sesion) + motor/modelo, sincronizado con el selector ya existente
// (metaModelEl).
// -------------------------------------------------------------------------
async function refreshProjectFooter() {
  try {
    const resp = await apiFetch(`${API_BASE}/api/session/${state.sessionId}/status`);
    const statusData = await resp.json();
    footerProjectTextEl.textContent = statusData.persistence_available
      ? statusData.project_path
      : "Sin carpeta asignada a esta sesión";
  } catch {
    footerProjectTextEl.textContent = "Sin carpeta asignada a esta sesión";
  }
}

function refreshModelFooter() {
  footerModelTextEl.textContent = selectedModelLabel();
}

// El click en el footer ahora abre el modal de edicion de ESTA sesion
// (donde se puede ver/cambiar su carpeta propia), no Configuracion.
footerProjectBtnEl.addEventListener("click", () => openSessionEditModal());
footerModelBtnEl.addEventListener("click", () => openSettings("model"));

// Envuelve saveSelectedModel (definida mas arriba) para mantener el footer
// sincronizado sin duplicar la logica de persistencia.
const _originalSaveSelectedModel = saveSelectedModel;
saveSelectedModel = function (modelId) {
  _originalSaveSelectedModel(modelId);
  refreshModelFooter();
};

// -------------------------------------------------------------------------
// Boton "ojo": muestra/oculta el panel lateral.
//
// Bug real reportado por el usuario: el "ojo" SIEMPRE togglaba
// `.sidebar-hidden` (mecanismo de grid de escritorio, `display:none`
// directo sobre `.sidebar`), sin importar el ancho de ventana. En movil
// (<=900px, mismo breakpoint que MOBILE_BREAKPOINT_MQ) el sidebar ya es
// un drawer `position:fixed` controlado por `.mobile-open`/`.scrim`
// (menu-btn) -- pero `display:none` gana sobre cualquier `transform`, asi
// que si se tocaba el ojo en CUALQUIER ancho y despues se achicaba la
// ventana, el sidebar quedaba bloqueado invisible para siempre: ni el
// propio drawer movil podia volver a mostrarlo. Fix: por debajo del
// breakpoint, el ojo reusa exactamente el mismo mecanismo que el boton de
// menu hamburguesa (openSidebar/closeSidebar, overlay POR ENCIMA del chat,
// sin achicar su ancho) en vez del de escritorio -- nunca los dos a la vez.
// -------------------------------------------------------------------------
const MOBILE_BREAKPOINT_MQ = window.matchMedia("(max-width: 900px)");
let sidebarHiddenDesktop = false;

function setEyeIcon(hidden) {
  eyeBtnEl.querySelector(".ic-eye-open").classList.toggle("hidden", hidden);
  eyeBtnEl.querySelector(".ic-eye-closed").classList.toggle("hidden", !hidden);
  eyeBtnEl.title = hidden ? "Mostrar panel lateral" : "Ocultar panel lateral";
}

eyeBtnEl.addEventListener("click", () => {
  if (MOBILE_BREAKPOINT_MQ.matches) {
    // Mismo drawer que menu-btn: si ya esta abierto lo cierra, si no lo abre.
    const isOpen = sidebarEl.classList.contains("mobile-open");
    if (isOpen) closeSidebar();
    else openSidebar();
    setEyeIcon(isOpen);
    return;
  }
  sidebarHiddenDesktop = !sidebarHiddenDesktop;
  appShellEl.classList.toggle("sidebar-hidden", sidebarHiddenDesktop);
  setEyeIcon(sidebarHiddenDesktop);
});

// Si la ventana cruza el breakpoint mientras el sidebar esta oculto por
// el mecanismo de ESCRITORIO (`.sidebar-hidden`), hay que soltarlo al
// entrar a movil -- si no, la regla `display:none` (ahora acotada a
// `min-width:901px` en CSS, ver styles.css) dejaria de aplicar recien
// cuando displayNone ya no se necesita, pero el estado `sidebarHiddenDesktop`
// seguiria "true" y confundiria al siguiente click en escritorio.
MOBILE_BREAKPOINT_MQ.addEventListener("change", (e) => {
  if (e.matches && sidebarHiddenDesktop) {
    sidebarHiddenDesktop = false;
    appShellEl.classList.remove("sidebar-hidden");
    setEyeIcon(false);
  }
});

// =========================================================================
// REQ-001/002/003/011 (spec narracion-agente-efimera, "Interaccion por
// voz"): 2 botones DIRECTOS junto al chip de modo -- microfono (Modo chat
// por voz) y Record (Modo reunion, con "el ojo" a pantalla completa).
// Backend de STT/gate/resumen (REQ-004/005/006) todavia NO implementado en
// esta primera pasada -- se arranca por la pieza visual (REQ-011, pedido
// explicito del usuario: "por el ojo primero"). Los botones ya quedan
// completamente cableados (activar/desactivar, overlay, confirmacion de
// salida) para que conectar el backend despues sea un cambio acotado.
// =========================================================================
const voiceMicBtnEl = document.getElementById("voice-mic-btn");
const voiceRecordBtnEl = document.getElementById("voice-record-btn");
// REQ-002 (spec asistente-contextual-voz): renombrado de "overlay" a
// "widget" -- ya no es una pantalla completa (#voice-eye-overlay), es un
// contenedor flotante chico y arrastrable (#voice-eye-widget, ver
// dragElement() y su CSS en styles.css).
const voiceEyeWidgetEl = document.getElementById("voice-eye-widget");
const voiceEyeContainerEl = document.getElementById("voice-eye-container");
const voiceEyeDragHandleEl = document.getElementById("voice-eye-drag-handle");
const voiceEyeExitConfirmEl = document.getElementById("voice-eye-exit-confirm");
const voiceEyeExitYesEl = document.getElementById("voice-eye-exit-yes");
const voiceEyeExitNoEl = document.getElementById("voice-eye-exit-no");
const voiceEyeExitDiscardEl = document.getElementById("voice-eye-exit-discard");

// REQ-002: drag-and-drop del widget flotante del "ojo" -- arrastre libre
// por toda la ventana, sin libreria nueva (pointer events nativos ya
// alcanzan para este caso simple). Se ancla por `left/top` en vez de
// `right/bottom` (el CSS por defecto usa right/bottom para el posicionamiento
// inicial) recien la PRIMERA vez que se arrastra, para no pelear con el
// posicionamiento default mientras el usuario no lo movio nunca.
function dragElement(dragHandleEl, widgetEl) {
  let startX = 0;
  let startY = 0;

  function onPointerMove(e) {
    const dx = startX - e.clientX;
    const dy = startY - e.clientY;
    startX = e.clientX;
    startY = e.clientY;
    const rect = widgetEl.getBoundingClientRect();
    // Primer movimiento: pasa de right/bottom (CSS default) a left/top
    // absolutos, anclados a la posicion actual real del widget -- evita
    // un "salto" visual al soltar el anclaje por defecto.
    widgetEl.style.right = "auto";
    widgetEl.style.bottom = "auto";
    widgetEl.style.left = `${Math.max(0, Math.min(window.innerWidth - rect.width, rect.left - dx))}px`;
    widgetEl.style.top = `${Math.max(0, Math.min(window.innerHeight - rect.height, rect.top - dy))}px`;
  }
  function onPointerUp() {
    document.removeEventListener("pointermove", onPointerMove);
    document.removeEventListener("pointerup", onPointerUp);
  }
  dragHandleEl.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    startX = e.clientX;
    startY = e.clientY;
    document.addEventListener("pointermove", onPointerMove);
    document.addEventListener("pointerup", onPointerUp);
  });
}
dragElement(voiceEyeDragHandleEl, voiceEyeWidgetEl);

let meetingModeActive = false;

// -------------------------------------------------------------------------
// Bug real reportado por el usuario (modo Cerebro, pero afecta a los 3
// modos de voz por igual: mic puntual, Record y Cerebro): Whisper
// "alucina" texto sobre fragmentos de audio que son silencio puro o ruido
// de fondo sin voz real -- lo mas comun son comillas sueltas (`""`, `'`),
// puntuacion aislada, o frases genericas de "relleno" que Whisper devuelve
// cuando no hay habla real que transcribir (fenomeno conocido del modelo,
// no un bug de la integracion). Sin filtrar esto, en el modo Cerebro (que
// auto-envia sin necesitar la palabra "Vision") cada silencio generaba un
// "mensaje" fantasma -> Vision respondia "no escuché nada" por TTS -> esa
// misma respuesta hablada volvia a ser captada por el microfono en el
// siguiente fragmento -> nueva alucinacion -> loop infinito.
// Filtro en 2 capas (ambas necesarias, ver Decisiones registradas de la
// spec): (1) esta funcion descarta post-transcripcion cualquier resultado
// que sea SOLO puntuacion/comillas o matchee una frase de relleno tipica;
// (2) `hasRealVoiceEnergy` (mas abajo) evita directamente LLAMAR a Whisper
// si el audio capturado no tuvo energia RMS por encima del piso de ruido
// -- ataca la causa raiz (no gastar una transcripcion en silencio puro),
// mientras que esta funcion es la red de seguridad para cuando SI hubo
// algo de energia (ruido de fondo real, no silencio absoluto) pero igual
// no fue habla entendible.
const _WHISPER_HALLUCINATION_RE =
  /^(gracias por ver(\s+el video)?|suscr[ií]bete|subt[ií]tulos? (realizados? )?por|www\.\S+|\.{3,}|amara\.org)\.?$/i;

function isWhisperHallucination(text) {
  const trimmed = (text || "").trim();
  if (!trimmed) return true;
  // Solo puntuacion/comillas/simbolos, sin ninguna letra o numero real --
  // el caso reportado explicitamente por el usuario ("solo genera comillas").
  if (!/[\p{L}\p{N}]/u.test(trimmed)) return true;
  if (_WHISPER_HALLUCINATION_RE.test(trimmed)) return true;
  return false;
}

// Primera barrera del fix de silencio, compartida por los 3 modos de voz:
// decodifica localmente el blob WebM/Opus y mide energia RMS + pico antes
// de gastar una llamada a Whisper. Si el navegador no puede decodificar
// ese formato, degrada de forma permisiva (true): el filtro de texto de
// arriba sigue protegiendo contra comillas/frases alucinadas.
async function hasRealVoiceEnergy(blob) {
  const AudioCtx = window.AudioContext || window.webkitAudioContext;
  if (!AudioCtx || blob.size === 0) return false;
  const ctx = new AudioCtx();
  try {
    const buffer = await ctx.decodeAudioData(await blob.arrayBuffer());
    let sumSquares = 0;
    let peak = 0;
    let samples = 0;
    for (let channel = 0; channel < buffer.numberOfChannels; channel++) {
      const data = buffer.getChannelData(channel);
      // Muestrear 1 de cada 8 valores alcanza para distinguir silencio de
      // voz y reduce mucho el trabajo sobre clips largos de Record.
      for (let i = 0; i < data.length; i += 8) {
        const abs = Math.abs(data[i]);
        sumSquares += abs * abs;
        peak = Math.max(peak, abs);
        samples += 1;
      }
    }
    const rms = samples > 0 ? Math.sqrt(sumSquares / samples) : 0;
    return rms >= 0.004 || peak >= 0.025;
  } catch {
    return true;
  } finally {
    ctx.close().catch(() => {});
  }
}

// -- REQ-002: Modo chat por voz (microfono) ------------------------------
// Flujo real: click arranca MediaRecorder -> click de nuevo (o el usuario
// deja de hablar y toca otra vez) para parar la grabacion -> el audio
// capturado se manda a /api/transcribe (whisper-large) -> el texto
// resultante se pone en el campo editable del compositor.
//
// ENVIO CONDICIONAL (pedido explicito del usuario, 2026-09-12):
// originalmente este mic auto-enviaba el texto a los 2s fijos
// (VOICE_AUTO_SEND_MS/scheduleVoiceAutoSend) y el usuario pidio quitarlo
// por un bug real (el timer de un ciclo previo quedaba vivo y disparaba
// el envio solo al volver a tocar el mic). Luego pidio reintroducirlo
// PERO condicionado al toggle "Auto-envio" (isBrainAutosendEnabled, el
// mismo estado que comparten el panel flotante y Configuracion > Audio
// del Modo Cerebro):
//   - Auto-envio TRUE  -> el texto dictado se envia solo tras la pausa
//     configurada (getBrainAutosendPauseMs()).
//   - Auto-envio FALSE -> queda manual en el compositor (Enviar/Enter).
// La causa del bug original se mantiene resuelta: arrancar un nuevo ciclo
// de grabacion SIEMPRE cancela cualquier timer pendiente, asi nunca se
// dispara un envio sin relacion con lo que el usuario hace en el momento.
let voiceMicRecorder = null;
let voiceMicChunks = [];
// true mientras se espera el micrófono. BUG real (detección QA 2026-09-24,
// test e2e): un doble click rápido llegaba mientras el primer click todavía
// esperaba el micrófono (el botón aún decía "no escuchando") y arrancaba una
// SEGUNDA grabación; la primera quedaba grabando para siempre, sin
// transcribirse, con el micrófono encendido.
let voiceMicStarting = false;
let voiceAutoSendTimer = null;

function scheduleVoiceAutoSend() {
  clearTimeout(voiceAutoSendTimer);
  voiceAutoSendTimer = setTimeout(() => {
    if (sendOrStopBtnEl.dataset.state === "idle" && inputEl.value.trim()) {
      formEl.requestSubmit();
    }
  }, Math.max(0, getBrainAutosendPauseMs()));
}

async function startVoiceMicRecording() {
  if (voiceMicStarting) return; // ver voiceMicStarting: ignora el 2do click de un doble click
  voiceMicStarting = true;
  // Cancelar cualquier auto-envio pendiente de un ciclo ANTERIOR de
  // grabacion: volver a grabar es senal explicita de "todavia no quiero
  // enviar lo ya transcripto" (evita el bug original reportado).
  clearTimeout(voiceAutoSendTimer);
  try {
    // QA-007: el microfono elegido en Configuracion > Audio (ver openMicStream).
    const stream = await openMicStream();
    noticeMicFallbackOnce();
    // QA-008: si el microfono se desenchufa a mitad de la grabacion, el
    // MediaRecorder se detiene solo (y transcribe lo grabado); aca se
    // devuelve el boton a "no escuchando" para que no quede trabado.
    watchMicTrackEnd(stream, stopVoiceMicRecording);
    voiceMicChunks = [];
    voiceMicRecorder = new MediaRecorder(stream);
    voiceMicRecorder.ondataavailable = (e) => voiceMicChunks.push(e.data);
    voiceMicRecorder.onstop = async () => {
      stream.getTracks().forEach((track) => track.stop()); // libera el microfono apenas termina de grabar
      const blob = new Blob(voiceMicChunks, { type: "audio/webm" });
      await transcribeAndFillComposer(blob);
    };
    voiceMicRecorder.start();
    voiceMicBtnEl.dataset.listening = "true";
  } catch (err) {
    // Permiso denegado o sin microfono -- no hay nada que grabar, el
    // boton vuelve a su estado normal sin romper el resto del chat.
    // Errores entendibles (2026-09-24): antes esto era mudo (el usuario
    // apretaba el microfono y no pasaba nada); ahora se dice por que.
    voiceMicBtnEl.dataset.listening = "false";
    appendMessage("system-note", describeMicError(err), { persist: false });
  } finally {
    voiceMicStarting = false;
  }
}

function stopVoiceMicRecording() {
  if (voiceMicRecorder && voiceMicRecorder.state !== "inactive") {
    voiceMicRecorder.stop();
  }
  voiceMicBtnEl.dataset.listening = "false";
}

async function transcribeAndFillComposer(blob) {
  if (!(await hasRealVoiceEnergy(blob))) return;
  const reader = new FileReader();
  const dataUri = await new Promise((resolve, reject) => {
    reader.onload = () => resolve(reader.result);
    reader.onerror = reject;
    reader.readAsDataURL(blob);
  });
  try {
    const resp = await apiFetch(`${API_BASE}/api/transcribe`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ audio: dataUri, language: getSavedSttLanguage() || undefined }),
    });
    if (!resp.ok) {
      // El backend ya explica el motivo en lenguaje simple (VOZ-*).
      appendMessage("system-note", (await httpErrorFromResponse(resp)).message, { persist: false });
      return;
    }
    const data = await resp.json();
    const text = (data.text || "").trim();
    if (!text || isWhisperHallucination(text)) return; // silencio/nada transcrito -- no se toca el compositor
    inputEl.value = inputEl.value ? `${inputEl.value} ${text}` : text;
    autoGrowComposer();
    // Auto-envio CONDICIONAL (ver comentario de seccion mas arriba): si el
    // toggle "Auto-envio" esta en true, el texto dictado se manda solo tras
    // la pausa configurada; si esta en false, queda manual en el compositor
    // y el usuario decide con Enviar/Enter.
    if (isBrainAutosendEnabled()) scheduleVoiceAutoSend();
  } catch (err) {
    // Sin conexion/error del backend -- el usuario puede seguir
    // escribiendo a mano, no es un error bloqueante (pero se le avisa).
    appendMessage("system-note", `No pude pasar tu audio a texto. ${friendlyErrorMessage(err)}`, { persist: false });
  }
}

// Modos que transcriben un fragmento cada pocos segundos (reunion, Cerebro):
// perder UNO no es grave, pero si fallan todos el usuario habla y no pasa
// nada, sin saber por que. Se avisa como mucho una vez por minuto para no
// inundar el chat (errores entendibles, 2026-09-24).
const TRANSCRIPTION_NOTICE_INTERVAL_MS = 60_000;
let _lastTranscriptionNoticeAt = 0;

// `resp`: la respuesta con error del backend, si la hubo. Bug real
// (2026-09-27, spec vision-generico REQ-003): sin el dictado configurado, el
// Modo Cerebro decia "sigo intentando, apaga y volve a prender el modo" --
// falso, reintentar no lo arregla. El backend ya explica el motivo real
// (VOZ-NO-CONFIGURADA, clave rechazada, limite...): se muestra ese texto.
async function noticeTranscriptionProblem(modeLabel, err = null, resp = null) {
  const now = Date.now();
  if (now - _lastTranscriptionNoticeAt < TRANSCRIPTION_NOTICE_INTERVAL_MS) return;
  _lastTranscriptionNoticeAt = now;
  let text = `${modeLabel}: no estoy pudiendo pasar el audio a texto, así que puede que me pierda partes de lo que se dice. Sigo intentando; si continúa, apagá y volvé a prender el modo.`;
  if (isOfflineError(err)) {
    text = `${modeLabel}: perdí la conexión con Vision y no puedo pasar el audio a texto. ${OFFLINE_ERROR_MESSAGE}`;
  } else if (resp) {
    text = `${modeLabel}: ${(await httpErrorFromResponse(resp)).message}`;
  }
  appendMessage("system-note", text, { persist: false });
}

voiceMicBtnEl.addEventListener("click", () => {
  const listening = voiceMicBtnEl.dataset.listening === "true";
  if (listening) stopVoiceMicRecording();
  else startVoiceMicRecording();
});

// -- Spec panel-flotante-control-rapido (2026-09-12): boton circular -----
// flotante de accesos rapidos (REQ-001 a REQ-005). Reune 4 acciones que
// hoy viven dispersas en distintas partes de la UI (Configuracion > Audio,
// el menu "/", nada para el navegador embebido todavia) en un unico punto
// de acceso fijo, arriba del compositor. Cada capacidad reusa mecanismos
// YA EXISTENTES y probados -- este bloque NO introduce ningun estado
// nuevo por su cuenta, salvo la visibilidad del menu/panel en si.
const quickPanelBtnEl = document.getElementById("quick-panel-btn");
const quickPanelMenuEl = document.getElementById("quick-panel-menu");
const quickPanelMainViewEl = document.getElementById("quick-panel-main-view");
const quickPanelSkillsViewEl = document.getElementById("quick-panel-skills-view");
const quickPanelSkillsBackEl = document.getElementById("quick-panel-skills-back");
const quickPanelSkillsListEl = document.getElementById("quick-panel-skills-list");
const quickPanelMuteBtnEl = document.getElementById("quick-panel-mute-btn");
const quickPanelMuteToggleEl = document.getElementById("quick-panel-mute-toggle");
const quickPanelAutosendBtnEl = document.getElementById("quick-panel-autosend-btn");
const quickPanelAutosendToggleEl = document.getElementById("quick-panel-autosend-toggle");
const quickPanelLoopBtnEl = document.getElementById("quick-panel-loop-btn");
const quickPanelLoopToggleEl = document.getElementById("quick-panel-loop-toggle");
const quickPanelLooprunBtnEl = document.getElementById("quick-panel-looprun-btn");
const quickPanelLooprunToggleEl = document.getElementById("quick-panel-looprun-toggle");
const quickPanelLoopautoBtnEl = document.getElementById("quick-panel-loopauto-btn");
const quickPanelLoopautoToggleEl = document.getElementById("quick-panel-loopauto-toggle");
const quickPanelBrowserBtnEl = document.getElementById("quick-panel-browser-btn");
const quickPanelSkillsBtnEl = document.getElementById("quick-panel-skills-btn");
const quickBrowserPanelEl = document.getElementById("quick-browser-panel");
const quickBrowserCloseBtnEl = document.getElementById("quick-browser-close-btn");
const quickBrowserUrlEl = document.getElementById("quick-browser-url");
const quickBrowserOpenBtnEl = document.getElementById("quick-browser-open-btn");
const quickBrowserMinBtnEl = document.getElementById("quick-browser-min-btn");
const quickBrowserMaxBtnEl = document.getElementById("quick-browser-max-btn");
const quickBrowserIframeEl = document.getElementById("quick-browser-iframe");

// REQ-001: abrir/cerrar el menu principal -- siempre vuelve a la vista de
// 4 opciones al abrir (nunca arranca en el submenu de skills).
function openQuickPanelMenu() {
  quickPanelMenuEl.classList.remove("hidden");
  quickPanelBtnEl.setAttribute("aria-expanded", "true");
  showQuickPanelMainView();
  quickPanelAutosendToggleEl.dataset.checked = String(isBrainAutosendEnabled());
  quickPanelMuteToggleEl.dataset.checked = String(isVoiceMuted());
  quickPanelLoopToggleEl.dataset.checked = String(_loopEnabled);
  if (quickPanelLooprunToggleEl && quickPanelLoopautoToggleEl) syncQuickPanelLoopState();
}
function closeQuickPanelMenu() {
  quickPanelMenuEl.classList.add("hidden");
  quickPanelBtnEl.setAttribute("aria-expanded", "false");
}
quickPanelBtnEl.addEventListener("click", (e) => {
  e.stopPropagation();
  const isOpen = !quickPanelMenuEl.classList.contains("hidden");
  if (isOpen) closeQuickPanelMenu();
  else openQuickPanelMenu();
});
// Cerrar al clickear afuera -- mismo patron que cualquier menu/dropdown
// del proyecto (ver #model-dropdown).
document.addEventListener("click", (e) => {
  if (quickPanelMenuEl.classList.contains("hidden")) return;
  if (quickPanelMenuEl.contains(e.target) || quickPanelBtnEl.contains(e.target)) return;
  closeQuickPanelMenu();
});

// REQ-002: silenciar voz -- TOGGLE de mute. Misma semantica de toggle que
// el de auto-envio de abajo, con aislacion total entre ambos. Mientras el
// toggle esta ACTIVO, Vision esta silenciado: corta la voz en curso y
// bloquea todos los speak() futuros (lo maneja isVoiceMuted()/setVoiceMuted()
// en tts.js, que filtran en _pump). NUNCA toca la preferencia permanente de
// Configuracion > Audio (cfg-tts-enabled) -- desactivarlo restaura el TTS
// normal. Estado en memoria de sesion (se resetea al recargar la pagina),
// reflejado en el menu al abrir (ver openQuickPanelMenu).
quickPanelMuteBtnEl.addEventListener("click", (e) => {
  e.stopPropagation();
  const next = !isVoiceMuted();
  setVoiceMuted(next);
  quickPanelMuteToggleEl.dataset.checked = String(next);
});

// REQ-003: auto-envio -- ESPEJO del mismo estado que el checkbox real de
// Configuracion > Audio (misma clave localStorage via
// isBrainAutosendEnabled/setBrainAutosendEnabled, ya existentes) -- nunca
// un estado independiente. Togglea y sincroniza visualmente el checkbox
// de Configuracion si esa pestana esta abierta en el momento.
quickPanelAutosendBtnEl.addEventListener("click", (e) => {
  e.stopPropagation();
  const next = !isBrainAutosendEnabled();
  setBrainAutosendEnabled(next);
  quickPanelAutosendToggleEl.dataset.checked = String(next);
  // Al apagar el auto-envio, cancelar cualquier timer pendiente del mic
  // por voz del compositor -- si el usuario ya dicto texto y lo dejo
  // esperando el auto-envio, apagar el toggle a mitad de la pausa lo
  // convierte en manual (no debe seguir corriendo el timer).
  if (!next) clearTimeout(voiceAutoSendTimer);
  if (cfgBrainAutosendEnabledEl) cfgBrainAutosendEnabledEl.checked = next;
});

// Bucle de reproduccion 🔁 (pedido del usuario 2026-09-12): toggle estilo
// reproductor en el panel de accesos rapidos. ON = al reproducir un
// fragmento con la bocinita 🔊 se repite indefinidamente hasta detenerlo o
// apagar el bucle; OFF = se reproduce una sola vez. Preferencia persistida.
// Apagar el bucle con una reproduccion en curso DETIENE el loop actual.
quickPanelLoopBtnEl.addEventListener("click", (e) => {
  e.stopPropagation();
  _loopEnabled = !_loopEnabled;
  try { localStorage.setItem(TTS_LOOP_KEY, _loopEnabled ? "true" : "false"); } catch { /* noop */ }
  quickPanelLoopToggleEl.dataset.checked = String(_loopEnabled);
  if (!_loopEnabled && _looping) stopActivePlayback();
});

// -------------------------------------------------------------------------
// Loop y Auto del /loop como toggles del panel de accesos rapidos (pedido
// del usuario 2026-09): "Loop" inicia/detiene la ejecucion automatica de la
// lista de tareas; "Auto" activa el modo auto (Vision decide las opciones
// "(Recomendada)"; pausa+alarma si hay "(Riesgo)" o ninguna recomendada).
// `_loopRunning` espeja `loop_active` del backend (se setea al iniciar el
// loop y se apaga con el evento loop-stopped y al detener). La preferencia
// de Auto se persiste y se usa como default del modal y de startLoop().
// -------------------------------------------------------------------------
const LOOP_AUTO_PREF_KEY = "vision.loop.auto";

function _loadLoopAutoPref() {
  try { return localStorage.getItem(LOOP_AUTO_PREF_KEY) === "true"; } catch { return false; }
}
let _loopRunning = false;
let _loopAutoPref = _loadLoopAutoPref();

function syncQuickPanelLoopState() {
  quickPanelLooprunToggleEl.dataset.checked = String(_loopRunning);
  quickPanelLoopautoToggleEl.dataset.checked = String(_loopAutoPref);
}

quickPanelLooprunBtnEl.addEventListener("click", (e) => {
  e.stopPropagation();
  if (_loopRunning) {
    // Detener: sama ruta que el boton STOP (POST /api/chat/{id}/stop).
    apiFetch(`${API_BASE}/api/chat/${state.sessionId}/stop`, { method: "POST" })
      .catch(() => {});
    _loopRunning = false;
    syncQuickPanelLoopState();
  } else {
    startLoop(_loopAutoPref); // usa la preferencia de Auto persistida
  }
});

quickPanelLoopautoBtnEl.addEventListener("click", (e) => {
  e.stopPropagation();
  _loopAutoPref = !_loopAutoPref;
  try { localStorage.setItem(LOOP_AUTO_PREF_KEY, _loopAutoPref ? "true" : "false"); } catch { /* noop */ }
  syncQuickPanelLoopState();
});

// REQ-005: submenu in-place de skills locales -- reemplaza el CONTENIDO
// del mismo menu (nunca un modal aparte), lista solo skills LOCALES del
// proyecto de la sesion actual (getLocalSkillsList() ya existente, mismo
// endpoint que usa el menu "/"). Estado vacio explicito si el proyecto no
// tiene ninguna definida -- nunca una lista en blanco sin contexto ni un
// fallback silencioso a skills globales.
function showQuickPanelMainView() {
  quickPanelSkillsViewEl.classList.add("hidden");
  quickPanelMainViewEl.classList.remove("hidden");
}
async function showQuickPanelSkillsView() {
  quickPanelMainViewEl.classList.add("hidden");
  quickPanelSkillsViewEl.classList.remove("hidden");
  quickPanelSkillsListEl.innerHTML = "";

  // Pedido del usuario (2026-09): dos secciones -- "Skill Globales" (con
  // toggle ON/OFF que persiste a nivel Vision via /api/skills/config, para
  // REALMENTE habilitar/deshabilitar la precarga global en el LLM) y "Skill
  // Locales" (del proyecto de la sesion, siempre disponibles).
  let globalEnabled = false;
  try {
    const resp = await apiFetch(`${API_BASE}/api/skills/config`);
    if (resp.ok) globalEnabled = (await resp.json()).global_enabled === true;
  } catch { /* fallback: no habilitadas */ }

  const makeBtn = (name) => {
    const it = document.createElement("button");
    it.type = "button";
    it.className = "quick-panel-skill-item";
    it.textContent = name;
    it.onclick = () => {
      // Pedido del usuario (2026): contratar una skill desde el panel de
      // accesos rapidos debe EJECUTARLA (como si se hubiera escrito en el
      // compositor y enviado), no solo insertarla.
      insertSlashSkill(name);
      formEl.requestSubmit();
      closeQuickPanelMenu();
    };
    return it;
  };

  const gSec = document.createElement("div");
  gSec.className = "quick-panel-skills-section";
  const gHead = document.createElement("div");
  gHead.className = "quick-panel-skills-section-title";
  const gLabel = document.createElement("span");
  gLabel.textContent = "Skill Globales";
  gHead.appendChild(gLabel);
  const gToggle = document.createElement("button");
  gToggle.type = "button";
  gToggle.className = "msg-copy-btn";
  gToggle.textContent = globalEnabled ? "ON" : "OFF";
  gToggle.onclick = async () => {
    globalEnabled = !globalEnabled;
    gToggle.textContent = globalEnabled ? "ON" : "OFF";
    try {
      await apiFetch(`${API_BASE}/api/skills/config`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ global_enabled: globalEnabled }),
      });
    } catch { /* noop */ }
    await reRenderGList();
  };
  gHead.appendChild(gToggle);
  gSec.appendChild(gHead);
  const gList = document.createElement("div");
  gList.className = "quick-panel-skills-list";
  gSec.appendChild(gList);
  quickPanelSkillsListEl.appendChild(gSec);

  async function reRenderGList() {
    gList.innerHTML = "";
    if (!globalEnabled) return;
    const skills = await getSkillsList();
    if (!skills.length) {
      const e = document.createElement("div");
      e.className = "quick-panel-skills-empty";
      e.textContent = "Sin skills globales";
      gList.appendChild(e);
      return;
    }
    skills.forEach((s) => gList.appendChild(makeBtn(s.name)));
  }
  await reRenderGList();

  const lSec = document.createElement("div");
  lSec.className = "quick-panel-skills-section";
  const lHead = document.createElement("div");
  lHead.className = "quick-panel-skills-section-title";
  lHead.textContent = "Skill Locales";
  lSec.appendChild(lHead);
  const lList = document.createElement("div");
  lList.className = "quick-panel-skills-list";
  lSec.appendChild(lList);
  quickPanelSkillsListEl.appendChild(lSec);

  const local = await getLocalSkillsList();
  if (!local.length) {
    const e = document.createElement("div");
    e.className = "quick-panel-skills-empty";
    e.textContent = "Sin skills del proyecto definidas";
    lList.appendChild(e);
  } else {
    local.forEach((s) => lList.appendChild(makeBtn(s.name)));
  }
}


quickPanelSkillsBtnEl.addEventListener("click", () => showQuickPanelSkillsView());
quickPanelSkillsBackEl.addEventListener("click", () => showQuickPanelMainView());

// REQ-004: panel del "lienzo" embebido -- reusa el backend YA EXISTENTE
// de vista previa en vivo (browser_workspace.py + endpoints
// /api/browser/{id}/fingerprint + /browser/{id}/...). El iframe apunta
// SIEMPRE al mismo origen (contenido que el propio LLM escribio en
// browser/{session_id}/ via write/edit/bash) -- nunca una URL externa
// arbitraria (alcance descartado explicitamente en la spec). El polling
// de fingerprint SOLO corre mientras el panel esta VISIBLE, mismo
// criterio que el medidor de nivel de microfono de Configuracion > Audio
// (evita gasto de red/CPU de fondo si el usuario nunca abre este panel).
let quickBrowserPollTimer = null;
let quickBrowserLastFingerprint = null;

async function pollQuickBrowserFingerprint() {
  try {
    const resp = await apiFetch(`${API_BASE}/api/browser/${encodeURIComponent(state.sessionId)}/fingerprint`);
    const data = await resp.json();
    if (data.fingerprint !== quickBrowserLastFingerprint) {
      quickBrowserLastFingerprint = data.fingerprint;
      quickBrowserIframeEl.src = `${API_BASE}/browser/${encodeURIComponent(state.sessionId)}/?t=${Date.now()}`;
    }
  } catch {
    // Sin conexion/error puntual -- el proximo poll reintenta solo, no es bloqueante.
  }
}
function browserPageUrl() {
  return `${API_BASE}/browser/${encodeURIComponent(state.sessionId)}/`;
}

function syncQuickBrowserUrl() {
  if (!quickBrowserUrlEl) return;
  quickBrowserUrlEl.textContent = `/browser/${encodeURIComponent(state.sessionId)}/`;
}

function copyQuickBrowserUrl() {
  const url = browserPageUrl();
  if (quickBrowserUrlEl) {
    quickBrowserUrlEl.classList.add("copied");
    setTimeout(() => quickBrowserUrlEl.classList.remove("copied"), 1200);
  }
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(url).catch(() => {});
  }
}

function openQuickBrowserPanel() {
  quickBrowserPanelEl.classList.remove("hidden");
  quickBrowserLastFingerprint = null; // fuerza la primera carga
  syncQuickBrowserUrl();
  pollQuickBrowserFingerprint();
  clearInterval(quickBrowserPollTimer);
  quickBrowserPollTimer = setInterval(pollQuickBrowserFingerprint, 3000);
}
function closeQuickBrowserPanel() {
  quickBrowserPanelEl.classList.add("hidden");
  clearInterval(quickBrowserPollTimer);
  quickBrowserPollTimer = null;
}

// -------------------------------------------------------------------------
// Auto-revelado del panel "Navegador". Pedido del usuario (2026-09): el panel
// SOLO se abre solo cuando Vision diseno una pagina que "debe verse en un
// navegador" (la sesion tiene un index.html real, `has_index`), NO por
// cualquier contenido. `_browserSeenFingerprint` evita reabrirlo para
// contenido ya visto. Siempre se muestra la URL copiable + boton ↗ para
// abrir en una pestana del navegador externo.
// -------------------------------------------------------------------------
let _browserSeenFingerprint = null;

async function maybeAutoRevealBrowser() {
  try {
    const resp = await apiFetch(`${API_BASE}/api/browser/${encodeURIComponent(state.sessionId)}/fingerprint`);
    const data = await resp.json();
    // Solo abrir si Vision diseno una PAGINA (hay index.html) y el contenido
    // es nuevo aun no visto.
    if (!data.has_index) return;
    const fp = data.fingerprint;
    if (!fp || fp === "empty") return;
    if (_browserSeenFingerprint === fp) return;
    _browserSeenFingerprint = fp;
    if (quickBrowserPanelEl.classList.contains("hidden")) openQuickBrowserPanel();
  } catch {
    // no bloqueante
  }
}

if (quickBrowserUrlEl) {
  quickBrowserUrlEl.addEventListener("click", () => copyQuickBrowserUrl());
}
if (quickBrowserOpenBtnEl) {
  quickBrowserOpenBtnEl.addEventListener("click", () => {
    const url = browserPageUrl();
    if (url) window.open(url, "_blank");
  });
}

quickPanelBrowserBtnEl.addEventListener("click", () => {
  const isOpen = !quickBrowserPanelEl.classList.contains("hidden");
  if (isOpen) closeQuickBrowserPanel();
  else openQuickBrowserPanel();
  closeQuickPanelMenu();
});
quickBrowserCloseBtnEl.addEventListener("click", () => closeQuickBrowserPanel());

// REQ-004 (evolucion 2026-09-12): controles de ventana -- Maximizar/
// Restaurar y Minimizar, conmutando clases que definen el estado visual
// (ver styles.css .quick-browser-panel.maximized/.minimized). Cualquiera
// de los 3 cierra primero cualquier estado de minimizacion (se totalizan
// los estados de modo que maximizar mientras esta minimizado restaura el
// contenido sin perderse). Minimizar deja el panel como solo barra de
// titulo; el polling sigue activo (el panel sigue "abierto", solo
// colapsado) hasta que el usuario lo cierre de verdad.
quickBrowserMaxBtnEl.addEventListener("click", () => {
  const isMax = quickBrowserPanelEl.classList.toggle("maximized");
  quickBrowserMaxBtnEl.dataset.action = isMax ? "restore" : "maximize";
  quickBrowserMaxBtnEl.title = isMax ? "Restaurar" : "Maximizar";
  quickBrowserMaxBtnEl.setAttribute("aria-label", isMax ? "Restaurar" : "Maximizar");
  quickBrowserPanelEl.classList.remove("minimized");
});
quickBrowserMinBtnEl.addEventListener("click", () => {
  // Toggle minimizar/restaurar: minimizar colapsa el panel a solo la
  // barra de titulo (ocultando el iframe); restaurar lo vuelve al tamano
  // normal recargando el contenido. Al minimizar tambien se desmaximiza,
  // de modo que el estado tras restaurar sea siempre el tamano por
  // defecto, sin confundir los dos estados visuales.
  const isMin = quickBrowserPanelEl.classList.toggle("minimized");
  quickBrowserMinBtnEl.dataset.action = isMin ? "restore-min" : "minimize";
  quickBrowserMinBtnEl.title = isMin ? "Restaurar" : "Minimizar";
  quickBrowserMinBtnEl.setAttribute("aria-label", isMin ? "Restaurar" : "Minimizar");
  if (isMin) {
    quickBrowserPanelEl.classList.remove("maximized");
    quickBrowserMaxBtnEl.dataset.action = "maximize";
    quickBrowserMaxBtnEl.title = "Maximizar";
    quickBrowserMaxBtnEl.setAttribute("aria-label", "Maximizar");
  } else {
    // Salio de minimizado -- recarga para asegurar contenido fresco.
    quickBrowserIframeEl.src = `${API_BASE}/browser/${encodeURIComponent(state.sessionId)}/?t=${Date.now()}`;
  }
});

// -- REQ-003/004/005/006/011: Modo reunion (Record) ----------------------
// Captura continua en FRAGMENTOS CORTOS (no un solo archivo largo al
// final) -- necesario para poder detectar el comando de activacion EN
// VIVO durante la reunion (REQ-004/005), no recien al terminar. Cada
// fragmento: se transcribe (whisper-large) -> se acumula al transcript
// completo (para el resumen final, REQ-006) -> se manda al gate
// heuristico (/api/meeting/analyze) -> si aprueba, la opinion del LLM se
// muestra en pantalla (TTS real llega en la siguiente pasada, REQ-007).
// Bajado de 8000 a 4000 (pedido explicito del usuario): con 8s de ventana,
// decir "Vision, que opinas?" al PRINCIPIO de un fragmento podia tardar
// hasta 8s solo en cortarse antes de siquiera empezar a transcribirse --
// sumado a la transcripcion + la respuesta del LLM, la demora total se
// sentia excesiva. Con 4s se duplica la cantidad de llamadas a
// whisper-large por minuto de reunion (mas costo/uso de API), a cambio de
// la mitad de latencia en el peor caso -- trade-off aceptado explicitamente.
const MEETING_FRAGMENT_MS = 4000; // ventana de captura por fragmento

let meetingRecorder = null;
let meetingStream = null;
let meetingFullTranscript = "";
let meetingFragmentTimer = null;

// Bug real reportado por el usuario: al salir guardando, a veces "no da
// resumen, ni nada" -- causa: el ULTIMO fragmento de audio (los ~8s
// justo antes de tocar salir) se transcribe de forma asincrona
// (processMeetingFragment nunca se espera desde recordNextFragment, a
// proposito, para no bloquear el arranque del siguiente fragmento
// mientras la reunion sigue). Si el usuario sale ANTES de que ese ultimo
// fragmento termine de transcribirse, `stopMeetingModeConfirmed` ya
// evaluaba `meetingFullTranscript` sin ese texto final -- en una reunion
// corta (menos de 1 tanda completa) eso podia significar transcript
// vacio y el resumen se saltaba en silencio. Se trackea la promesa del
// fragmento en curso para poder esperarla antes de decidir si hay algo
// para resumir.
let meetingLastFragmentPromise = null;

async function processMeetingFragment(blob) {
  if (!(await hasRealVoiceEnergy(blob))) return;
  const reader = new FileReader();
  const dataUri = await new Promise((resolve, reject) => {
    reader.onload = () => resolve(reader.result);
    reader.onerror = reject;
    reader.readAsDataURL(blob);
  });

  let fragmentText = "";
  try {
    const transcribeResp = await apiFetch(`${API_BASE}/api/transcribe`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ audio: dataUri, language: getSavedSttLanguage() || undefined }),
    });
    if (!transcribeResp.ok) {
      noticeTranscriptionProblem("Modo reunión", null, transcribeResp);
      return;
    }
    const transcribeData = await transcribeResp.json();
    fragmentText = (transcribeData.text || "").trim();
  } catch (err) {
    noticeTranscriptionProblem("Modo reunión", err);
    return; // fragmento perdido por error de red -- la reunion sigue, no es fatal
  }
  if (!fragmentText || isWhisperHallucination(fragmentText)) return;

  meetingFullTranscript += (meetingFullTranscript ? " " : "") + fragmentText;
  if (!meetingModeActive) return; // se salio mientras se transcribia -- no invoca al gate de opinion

  // REQ-002 (spec asistente-contextual-voz): ya NO hay transcript en vivo
  // ni resaltado visual del activador (decision registrada en spec.md,
  // 2026-09-10 -- "sin panel dedicado, solo la opinion final aparece en
  // el chat"). El regex de deteccion se mantiene SOLO para decidir el
  // feedback auditivo/barge-in inmediato (mismo `_ASSISTANT_MENTION_RE`
  // del backend, ver meeting_gate.py) -- ya no arma HTML resaltado.
  const activatorDetected = /\b(jarvis|visi[oó]n)\b/i.test(fragmentText);
  // Pedido explicito del usuario: sonido activador estilo Alexa/Google
  // Assistant apenas se detecta la mencion -- feedback AUDITIVO inmediato,
  // independiente de si el TTS de la respuesta esta activado o no (este
  // beep suena SIEMPRE que se detecta el activador, para confirmar "te
  // escuché" incluso mientras se espera la respuesta del LLM).
  if (activatorDetected) {
    // Barge-in (pedido explicito del usuario, patron Alexa/Google
    // Assistant): si Vision estaba hablando (TTS en curso) y se detecta
    // la mencion de nuevo, se corta la voz al instante en vez de esperar
    // a que termine -- "solo si vuelve a detectar mientras el habla que
    // vuelvo a decir Vision". `isSpeaking()` (tts.js) dice si Vision esta
    // hablando ahora, con la voz del navegador o con un motor de voz.
    if (isSpeaking()) {
      cancelSpeech();
    }
    playActivationSound();
  }

  // REQ-004/005 (narracion-agente-efimera): gate heuristico + invocacion
  // al LLM si corresponde -- se manda SIEMPRE (el gate barato decide del
  // lado del backend si amerita invocar al LLM real, ver meeting_gate.py).
  try {
    const analyzeResp = await apiFetch(`${API_BASE}/api/meeting/analyze`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      // REQ-004/005 revisado: se manda TAMBIEN toda la transcripcion
      // acumulada (`meetingFullTranscript`) como contexto -- pedido
      // explicito del usuario ("razona sobre todo lo que entendio"), no
      // solo sobre el ultimo fragmento aislado que disparo la invocacion.
      body: JSON.stringify({
        session_id: state.sessionId,
        fragment: fragmentText,
        full_transcript: meetingFullTranscript,
      }),
    });
    if (!analyzeResp.ok) return;
    const analyzeData = await analyzeResp.json();
    if (analyzeData.opinion && meetingModeActive) {
      // REQ-002: la opinion ya quedo persistida en SQLite por el backend
      // (run_turn() la agrega a session.messages) -- aca solo se refleja
      // en el DOM visible del chat normal (persist:false, evita duplicar
      // la escritura), en vez de mostrarse en un panel/transcript
      // separado del Modo reunion que ya no existe.
      appendMessage("assistant", analyzeData.opinion, { persist: false });
      speak(toPlainText(analyzeData.opinion));
    }
  } catch {
    // Sin conexion -- la reunion sigue escuchando igual, no es fatal.
  }
}

function scheduleNextMeetingFragment() {
  if (!meetingModeActive || !meetingRecorder || meetingRecorder.state === "inactive") return;
  meetingRecorder.stop(); // dispara onstop -> arranca el siguiente fragmento
}

// Nuevo REQ (pedido explicito del usuario): click DENTRO del circulo del
// ojo durante el Modo reunion equivale a activar diciendo "Vision", pero
// manual -- Vision opina de lo que grabo hasta el momento, mostrando todo
// el mismo proceso (resaltado, sonido, TTS) que el camino automatico por
// voz. Reusa `/api/meeting/analyze` con `force: true` (bypasea el gate de
// mencion Y el throttle anti-rafaga -- una accion explicita del usuario
// no deberia bloquearse por "no dijiste la palabra" ni por el cooldown).
async function analyzeManualClick() {
  // Bug real reportado por el usuario ("el click no funcionó... no pasó
  // nada en absoluto"): las 2 causas mas probables investigadas antes
  // (cache viejo de app.js, politica de autoplay de audio) no se pudieron
  // reproducir con Playwright. Se agrega instrumentacion visible en cada
  // paso (en vez de fallar en silencio) para que, si vuelve a pasar, se
  // sepa EXACTAMENTE en que paso se corto -- ya sea por un guard early-
  // return sin feedback, o por un fetch que fallo sin avisar.
  if (!meetingModeActive) {
    // Este guard es el sospechoso mas probable de "no pasó nada en
    // absoluto": si por cualquier motivo el listener se dispara con el
    // modo reunion ya cerrado (ej. un doble-click justo al salir), antes
    // esto retornaba sin ningun rastro. Ahora al menos queda logueado.
    console.warn("[Vision] click en el ojo ignorado: meetingModeActive es false");
    return;
  }
  // Mismo sonido/feedback inmediato que el activador por voz -- confirma
  // "te escuché" sin esperar la respuesta del LLM (que puede tardar).
  playActivationSound();
  if (isSpeaking()) {
    cancelSpeech(); // mismo barge-in que el activador por voz, por si Vision ya estaba hablando
  }

  try {
    const analyzeResp = await apiFetch(`${API_BASE}/api/meeting/analyze`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        session_id: state.sessionId,
        fragment: "", // no hay un fragmento puntual que disparo esto -- fue un click, no una mencion hablada
        full_transcript: meetingFullTranscript,
        force: true,
      }),
    });
    if (!analyzeResp.ok) {
      // Bug real corregido: un error HTTP (ej. 409 sesion ocupada, 502
      // del LLM) se tragaba en silencio -- coincide exactamente con "no
      // pasó nada en absoluto" reportado por el usuario. Mismo patron ya
      // usado en stopMeetingModeConfirmed() para /api/meeting/summarize.
      // REQ-002: ya no hay transcript propio del overlay -- el aviso se
      // muestra como mensaje de error normal del chat.
      const detail = (await httpErrorFromResponse(analyzeResp)).message;
      appendMessage("error", detail);
      return;
    }
    const analyzeData = await analyzeResp.json();
    if (analyzeData.opinion && meetingModeActive) {
      // REQ-002: opinion ya persistida por el backend (run_turn) -- solo
      // se refleja en el DOM del chat visible.
      appendMessage("assistant", analyzeData.opinion, { persist: false });
      speak(toPlainText(analyzeData.opinion));
    } else if (!analyzeData.opinion) {
      // opinion=null puede pasar si session.is_busy (chat de texto
      // corriendo en paralelo) -- antes quedaba indistinguible de "no
      // pasó nada".
      appendMessage(
        "system-note",
        "Vision no pudo responder ahora (¿hay otro turno en curso?)",
        { persist: false }
      );
    }
  } catch (err) {
    // Bug real corregido: error de red/conexion tambien se tragaba en
    // silencio antes -- ahora se avisa explicitamente, mismo criterio que
    // el resto del Modo reunion.
    appendMessage("error", `Modo reunión: no pude procesar lo último que se dijo. ${friendlyErrorMessage(err)}`, { errorKind: isOfflineError(err) ? "offline" : "other" });
  }
}

// Click DENTRO del circulo del ojo (el propio contenedor del canvas de
// Three.js, `#voice-eye-container`, ahora dentro del widget flotante
// REQ-002) -- registrado UNA sola vez (el elemento nunca se destruye
// entre aperturas del Modo reunion, solo se le agrega/quita el canvas
// adentro via appendChild en voice-eye.js). No interfiere con el drag del
// widget (maneja el handle superior, elemento distinto) ni con el click
// del boton "Record" ni con el modal de confirmacion de salida.
voiceEyeContainerEl.addEventListener("click", () => analyzeManualClick());

async function startMeetingMode() {
  meetingModeActive = true;
  meetingFullTranscript = "";
  voiceRecordBtnEl.dataset.recording = "true";
  // REQ-002 (spec asistente-contextual-voz): se muestra el widget
  // flotante chico (ya no la pantalla completa) -- el "ojo" arranca en su
  // posicion por defecto (esquina inferior derecha, ver CSS) salvo que el
  // usuario ya lo haya arrastrado antes en esta misma sesion de pagina.
  voiceEyeWidgetEl.classList.remove("hidden");
  await initVoiceEye(voiceEyeContainerEl);

  let stream;
  try {
    stream = await openMicStream(); // QA-007: el microfono elegido en Configuracion
  } catch (err) {
    // Sin microfono real -- el modo reunion sigue "activo" visualmente
    // (el ojo ya cayo a su propio modo simulado dentro de voice-eye.js),
    // pero sin audio real no hay nada que transcribir/analizar. QA-008: ya
    // no en silencio -- se avisa, y `refreshActiveMicStreams` retoma la
    // escucha sola en cuanto se conecte un microfono.
    appendMessage(
      "system-note",
      isMicPermissionError(err)
        ? "Modo reunión: Vision no tiene permiso para usar el micrófono. Habilitalo desde el candado de la barra de direcciones; la reunión queda abierta."
        : "Modo reunión: no se pudo acceder al micrófono. La reunión queda abierta y empieza a escuchar apenas conectes uno.",
      { persist: false },
    );
    return;
  }
  // El usuario pudo haber salido del modo mientras el navegador pedia el
  // microfono: no dejarlo abierto sin nadie que lo use.
  if (!meetingModeActive) {
    stream.getTracks().forEach((track) => track.stop());
    return;
  }
  meetingStream = stream;
  noticeMicFallbackOnce();
  watchMicTrackEnd(meetingStream, reacquireMeetingMic);
  recordNextMeetingFragment();
}

// Graba el siguiente fragmento de ~MEETING_FRAGMENT_MS del modo reunion.
// Antes era una funcion anidada dentro de startMeetingMode; se saco a nivel
// de modulo para que la reconexion de microfono (QA-008, reacquireMeetingMic)
// pueda retomar la cadena de fragmentos sobre un stream NUEVO.
function recordNextMeetingFragment() {
  // Sin una pista viva (microfono desenchufado) no se arranca nada: la
  // cadena la retoma reacquireMeetingMic cuando haya microfono otra vez.
  if (!meetingModeActive || !hasLiveAudioTrack(meetingStream)) return;
  // Idempotente: tanto el `onstop` del fragmento anterior como la
  // reconexion pueden pedir el siguiente fragmento casi a la vez -- nunca
  // dos grabadores sobre el mismo stream.
  if (meetingRecorder && meetingRecorder.state === "recording" && meetingRecorder.stream === meetingStream) return;
  const chunks = [];
  meetingRecorder = new MediaRecorder(meetingStream);
  meetingRecorder.ondataavailable = (e) => chunks.push(e.data);
  meetingRecorder.onstop = () => {
    const blob = new Blob(chunks, { type: "audio/webm" });
    // No se espera (await) aca -- no bloquea el arranque del siguiente
    // fragmento mientras la reunion sigue. Se guarda la promesa en
    // `meetingLastFragmentPromise` para que stopMeetingModeConfirmed
    // pueda esperarla explicitamente antes de decidir si hay algo para
    // resumir (ver comentario arriba de processMeetingFragment).
    meetingLastFragmentPromise = processMeetingFragment(blob);
    if (meetingModeActive) recordNextMeetingFragment();
  };
  meetingRecorder.start();
  clearTimeout(meetingFragmentTimer);
  meetingFragmentTimer = setTimeout(scheduleNextMeetingFragment, MEETING_FRAGMENT_MS);
}

// QA-008: el microfono del modo reunion desaparecio (desenchufado) o hay que
// pasar al elegido en Configuracion (se volvio a conectar, o se cambio el
// selector). La reunion NO se corta ni pierde su transcripcion: solo se
// cambia el stream y la cadena de fragmentos sigue sobre el nuevo.
let meetingMicReacquiring = false;

async function reacquireMeetingMic() {
  if (!meetingModeActive || meetingMicReacquiring) return;
  meetingMicReacquiring = true;
  try {
    let stream = null;
    try {
      stream = await openMicStream();
    } catch {
      stream = null;
    }
    if (!meetingModeActive) {
      stream?.getTracks().forEach((track) => track.stop());
      return;
    }
    if (!stream) {
      appendMessage(
        "system-note",
        "Modo reunión: se desconectó el micrófono y no hay otro conectado. La reunión sigue abierta y retoma la escucha apenas conectes uno.",
        { persist: false },
      );
      return;
    }
    const previousStream = meetingStream;
    meetingStream = stream;
    watchMicTrackEnd(stream, reacquireMeetingMic);
    if (meetingRecorder && meetingRecorder.state !== "inactive" && meetingRecorder.stream !== stream) {
      // El fragmento en curso quedo sobre el stream viejo: se corta ya. Su
      // `onstop` procesa lo grabado y arranca el siguiente sobre el nuevo.
      meetingRecorder.stop();
    } else {
      recordNextMeetingFragment();
    }
    if (previousStream && previousStream !== stream) {
      previousStream.getTracks().forEach((track) => track.stop());
    }
    noticeMicFallbackOnce();
    appendMessage("system-note", `Modo reunión: escuchando por «${micLabel(stream)}».`, { persist: false });
  } finally {
    meetingMicReacquiring = false;
  }
}

function showMeetingExitConfirm() {
  voiceEyeExitConfirmEl.classList.remove("hidden");
}
function hideMeetingExitConfirm() {
  voiceEyeExitConfirmEl.classList.add("hidden");
}

// Detiene la captura de audio/camara y oculta el widget flotante del
// "ojo" (REQ-002) -- COMUN a las 2 formas de salir (guardando o
// descartando). Devuelve la promesa del ultimo fragmento en curso (si
// habia uno) para que quien llama pueda esperarlo antes de decidir que
// hacer con el transcript.
function teardownMeetingCapture() {
  meetingModeActive = false;
  clearTimeout(meetingFragmentTimer);
  if (meetingRecorder && meetingRecorder.state !== "inactive") meetingRecorder.stop();
  if (meetingStream) {
    meetingStream.getTracks().forEach((track) => track.stop());
    meetingStream = null;
  }
  voiceRecordBtnEl.dataset.recording = "false";
  hideMeetingExitConfirm();
  voiceEyeWidgetEl.classList.add("hidden");
  destroyVoiceEye();
  return meetingLastFragmentPromise;
}

async function stopMeetingModeConfirmed() {
  const pendingFragment = teardownMeetingCapture();
  // Bug real corregido: se espera el ULTIMO fragmento en transcripcion
  // (si `meetingRecorder.stop()` de arriba disparo uno recien) antes de
  // evaluar el transcript -- sin esto, en una reunion corta el ultimo
  // tramo de audio podia quedar afuera del resumen o directamente hacer
  // que el transcript pareciera vacio.
  if (pendingFragment) {
    try {
      await pendingFragment;
    } catch {
      // El propio processMeetingFragment ya maneja sus errores
      // internamente (nunca rechaza) -- este catch es solo defensivo.
    }
  }

  // REQ-006: genera el resumen estructurado y lo persiste como mensaje de
  // la sesion (el backend ya lo hace via run_turn -- no hace falta que el
  // frontend lo agregue de nuevo al chatHistory, el proximo refresh de la
  // sesion lo va a traer desde SQLite igual que cualquier otro mensaje).
  if (meetingFullTranscript.trim()) {
    try {
      const resp = await apiFetch(`${API_BASE}/api/meeting/summarize`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: state.sessionId, transcript: meetingFullTranscript }),
      });
      if (resp.ok) {
        const data = await resp.json();
        if (data.summary) {
          // Se muestra de inmediato en el chat visible (ademas de haber
          // quedado persistido en SQLite por el backend) -- evita que el
          // usuario tenga que recargar la sesion para verlo.
          appendMessage("assistant", data.summary, { persist: false });
        } else {
          // Bug real corregido: esto se tragaba en silencio -- el
          // usuario reporto "no da resumen, ni nada" sin ningun aviso de
          // que algo habia fallado. Ahora se avisa explicitamente.
          appendMessage(
            "system-note",
            "⚠ El Modo reunión terminó pero no se pudo generar un resumen (la transcripción puede haber quedado vacía o poco clara).",
            { persist: false }
          );
        }
      } else {
        // Bug real corregido: un error del backend (409 sesion ocupada,
        // 502 error del LLM) se tragaba en silencio -- ahora se muestra
        // como error visible, igual que cualquier otro error del chat.
        const detail = (await httpErrorFromResponse(resp)).message;
        appendMessage("error", detail);
      }
    } catch (err) {
      // Bug real corregido: error de red/conexion tambien se tragaba en
      // silencio antes -- la transcripcion ya se perdio (por diseno,
      // nunca se guarda cruda) pero ahora al menos se avisa.
      appendMessage("error", `No se pudo generar el resumen de la reunión. ${friendlyErrorMessage(err)}`, { errorKind: isOfflineError(err) ? "offline" : "other" });
    }
  }
  meetingFullTranscript = "";
  meetingLastFragmentPromise = null;
}

// Bug real reportado por el usuario: no habia forma de salir DESCARTANDO
// la grabacion -- solo "guardar y salir" (llama a /api/meeting/summarize)
// o "cancelar" (sigue grabando). Este 3er camino sale sin llamar al
// backend en absoluto: borra meetingFullTranscript sin generar ni
// persistir ningun resumen.
function stopMeetingModeDiscarded() {
  teardownMeetingCapture();
  meetingFullTranscript = "";
  meetingLastFragmentPromise = null;
}

voiceRecordBtnEl.addEventListener("click", () => {
  if (!meetingModeActive) {
    startMeetingMode();
    return;
  }
  // Ya estaba activo -- volver a tocar Record es uno de los 2 disparadores
  // de salida equivalentes (REQ-003), igual que Escape mas abajo.
  showMeetingExitConfirm();
});

voiceEyeExitYesEl.addEventListener("click", stopMeetingModeConfirmed);
voiceEyeExitNoEl.addEventListener("click", hideMeetingExitConfirm);
voiceEyeExitDiscardEl.addEventListener("click", stopMeetingModeDiscarded);

// REQ-003: Escape es el otro disparador de salida equivalente a tocar
// Record de nuevo -- ambos abren la MISMA confirmacion, nunca salen directo
// sin preguntar. Si la confirmacion ya esta abierta, Escape la cierra
// (equivalente a "No") en vez de abrir una segunda.
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape" || !meetingModeActive) return;
  if (!voiceEyeExitConfirmEl.classList.contains("hidden")) {
    hideMeetingExitConfirm();
  } else {
    showMeetingExitConfirm();
  }
});

// =========================================================================
// REQ-004/005/006/007/009/010/011/013 (spec asistente-contextual-voz):
// Modo Cerebro -- conversacion por voz continua CON acceso a las mismas
// tools/contexto que el chat de texto (bash/write/edit/Oracle/BigQuery/
// etc., respetando Modo Plan/Activo de la sesion, REQ-008 -- eso ya lo
// resuelve el backend via session.mode, no requiere logica propia aca).
// A diferencia del Modo reunion (Record), CADA intervencion es un turno de
// chat REAL (se persiste, se muestra en el historial, con TTS) -- no hay
// resumen aparte al salir, y NO hay overlay/widget: el UNICO feedback
// visual es el estado del propio icono #brain-btn (REQ-013, data-state).
//
// A DIFERENCIA de Record (que graba en fragmentos de duracion FIJA,
// MEETING_FRAGMENT_MS = 4s, y siempre espera esos 4s completos antes de
// transcribir), el modo Cerebro usa deteccion de voz en tiempo real (VAD
// por energia RMS, Web Audio API) para saber EXACTAMENTE cuando el usuario
// dejo de hablar -- bug real reportado por el usuario ("me esta faltando
// velocidad", "mientras espera esos segundos de silencio para enviar ya
// tendria que haber procesado lo que dije"): con el mecanismo de fragmento
// fijo, la transcripcion recien arrancaba a correr en el fragmento
// SIGUIENTE al que contenia la voz real, sumando hasta 4s de latencia
// injustificada antes de siquiera empezar a llamar a Whisper. Ahora se
// analiza el audio en vivo mientras se graba: apenas la energia cae por
// debajo del piso de silencio durante BRAIN_SILENCE_MS consecutivos
// (mismo criterio que un VAD simple), se corta la grabacion Y ARRANCA la
// transcripcion al instante -- el usuario deja de hablar y la transcripcion
// ya esta en vuelo mientras recien empiezan a correr los segundos de pausa
// de auto-envio (REQ-005), no despues.
// =========================================================================
const brainBtnEl = document.getElementById("brain-btn");
const brainWaveformEl = document.getElementById("brain-waveform");
const brainWaveformBarEls = brainWaveformEl
  ? brainWaveformEl.querySelectorAll(".brain-waveform-bar")
  : [];

let brainModeActive = false;
let brainStream = null;
let brainRecorder = null;
let brainAutoSendTimer = null;
let brainStateInterval = null;
let brainPendingText = ""; // acumulado desde el ultimo envio (auto o por mencion)
let brainSending = false; // true mientras un turno de voz esta en curso -- evita solapar 2 envios
let brainTranscriptionsPending = 0; // Whisper en vuelo -- tambien es estado "procesando" (amarillo)
let brainAudioCtx = null;
let brainAnalyser = null;
let brainVadRafId = null;
let brainSpeechActive = false; // true mientras el VAD detecta energia por encima del piso de silencio
let brainSilenceStartedAt = 0; // timestamp (performance.now()) de cuando empezo el silencio actual

// REQ-007 (revisado, pedido explicito del usuario 2026-09-10: "no debe
// apagarse si esta hablando el... solo termina si presiono escape tal
// cual como copilot/chatgpt/claude/gemini etc"): SE ELIMINO por completo
// el auto-apagado por 20s de inactividad -- el mecanismo original apagaba
// el modo a mitad de una respuesta larga de Vision (mientras el TTS
// segu\u00eda hablando, ning\u00fan fragmento nuevo llegaba a resetear el timer
// porque el propio usuario no estaba hablando en ese momento, solo
// escuchando). El modo Cerebro ahora se mantiene activo indefinidamente
// una vez encendido -- la UNICA forma de apagarlo es Escape o volver a
// tocar el icono (ver stopBrainMode mas abajo), igual que el resto de
// asistentes de voz de referencia mencionados por el usuario.

// VAD (Voice Activity Detection): mismo umbral RMS que el medidor de nivel
// de mic en Configuracion > Audio (startMicMeter, mas abajo en este mismo
// archivo) para consistencia -- por debajo de este valor se considera
// silencio. 600ms de silencio sostenido confirma "dejo de hablar" (ni bien
// hay una pausa natural entre palabras, que dura mucho menos que eso, para
// no cortar a media frase).
// 0.012: suficientemente por encima del piso habitual con supresion de
// ruido (~0.002-0.008), pero no tan alto como el 0.02 inicial que en
// microfonos corporativos con ganancia baja podia no detectar ninguna voz
// y, por lo tanto, nunca cerrar/enviar una intervencion.
const BRAIN_SILENCE_THRESHOLD = 0.012;
const BRAIN_SILENCE_CONFIRM_MS = 600;
// Limite duro por si el VAD nunca detecta silencio (ej. ruido de fondo
// constante) -- evita un blob de audio creciendo sin fin.
const BRAIN_MAX_UTTERANCE_MS = 15000;

// REQ-013 (redefinido por el usuario tras probar la paleta original de
// --accent): verde mientras escucha activamente (habla detectada por el
// VAD), amarillo mientras procesa (transcribiendo o esperando la
// respuesta del LLM), rojo en cualquier otro estado del modo activo (en
// silencio esperando que el usuario hable, o hablando la respuesta por
// TTS) -- el usuario explicito que NO quiere azul porque se confunde con
// los acentos del navbar en el tema oscuro.
function setBrainState(state) {
  brainBtnEl.dataset.state = state;
}

// REQ-013: refleja en vivo, cada 250ms, en que estado visual cae el modo
// -- prioridad: procesando (esperando Whisper/LLM) > escuchando activo
// (VAD detecto voz ahora mismo) > resto (rojo: en silencio esperando, o
// TTS hablando la respuesta).
function tickBrainState() {
  if (!brainModeActive) return;
  if (brainSending || brainTranscriptionsPending > 0) {
    setBrainState("processing"); // amarillo
  } else if (brainSpeechActive) {
    setBrainState("listening"); // verde -- VAD detecto energia de voz ahora mismo
  } else {
    setBrainState("idle-active"); // rojo -- modo activo pero sin habla detectada ahora
  }
}

// REQ-010/013: pedido explicito del usuario -- animacion de ondas de
// sonido que confirma en tiempo real que el microfono esta captando la
// voz (no solo un color de estado). Se llama en CADA frame del VAD
// (vadTick, ~60fps via requestAnimationFrame) con el nivel RMS real; cada
// una de las 4 barras responde con una ganancia/desfasaje levemente
// distinto para que se vea como una forma de onda real y no 4 barras
// identicas subiendo y bajando en sincro perfecto (mas organico visualmente).
const _BRAIN_WAVEFORM_GAINS = [0.7, 1.15, 1.0, 0.85];
function updateBrainWaveform(rms) {
  // Compatibilidad defensiva con cache agresivo: si el navegador cargo
  // app.js nuevo pero todavia conserva index.html anterior, no se rompe
  // TODO el modulo por faltar temporalmente el nuevo elemento visual.
  if (!brainWaveformEl) return;
  if (rms <= 0) {
    brainWaveformEl.classList.add("hidden");
    return;
  }
  brainWaveformEl.classList.remove("hidden");
  brainWaveformBarEls.forEach((bar, i) => {
    const pct = Math.min(1, rms * 12 * _BRAIN_WAVEFORM_GAINS[i]); // ganancia visual -- RMS crudo de voz suele ser <0.25
    const heightPx = 4 + pct * 12; // entre 4px (piso) y 16px (tope, alto del contenedor)
    bar.style.height = `${heightPx}px`;
  });
}

// REQ-005: auto-envio configurable -- se dispara tras N segundos de
// silencio (Modo Cerebro es conversacion continua, necesita auto-envio
// por diseño). El microfono de mensaje puntual del compositor comparte el
// MISMO toggle "Auto-envio" (isBrainAutosendEnabled) y la misma pausa
// (getBrainAutosendPauseMs), ver comentario de esa seccion mas arriba. El
// valor viene de Configuracion > Audio, REQ-012.
function scheduleBrainAutoSend(delayMs = getBrainAutosendPauseMs()) {
  clearTimeout(brainAutoSendTimer);
  brainAutoSendTimer = setTimeout(() => {
    sendBrainIntervention();
  }, Math.max(0, delayMs));
}

// REQ-006: envia lo acumulado como un turno de chat REAL -- reusa
// sendMessage() tal cual (mismas tools, mismo streaming, misma
// persistencia y TTS de la respuesta) con `voiceConfirm:true` para que el
// backend sepa que este turno viene del modo Cerebro (activa REQ-009/011
// del lado del servidor, ver app/llm/loop.py).
async function sendBrainIntervention() {
  // Si Vision todavia procesa el turno anterior, NO vaciar el texto ya
  // transcripto: queda en cola y se envia apenas termine. El orden previo
  // copiaba+vaciaba brainPendingText ANTES de este guard y perdia por
  // completo cualquier intervencion pronunciada durante una respuesta.
  if (brainSending) return;
  const text = brainPendingText.trim();
  brainPendingText = "";
  clearTimeout(brainAutoSendTimer);
  if (!text) return;
  brainSending = true;
  try {
    await sendMessage(text, { voiceConfirm: true });
  } finally {
    brainSending = false;
    // Barge-in/turno hablado mientras el anterior estaba en curso: queda
    // acumulado arriba y se despacha ahora, sin perderlo ni solapar dos
    // streams de la misma conversacion.
    if (brainModeActive && brainPendingText.trim()) scheduleBrainAutoSend(0);
  }
}

// Se llama apenas el VAD confirma que el usuario dejo de hablar (o se
// llego al limite duro de duracion) -- YA con el audio de esa "frase"
// completo, arranca la transcripcion de inmediato (sin esperar ningun
// timer adicional de fragmento fijo, a diferencia de Record).
async function processBrainUtterance(blob, speechEndedAt) {
  brainTranscriptionsPending += 1;
  let fragmentText = "";
  try {
    if (!(await hasRealVoiceEnergy(blob))) return;
    const reader = new FileReader();
    const dataUri = await new Promise((resolve, reject) => {
      reader.onload = () => resolve(reader.result);
      reader.onerror = reject;
      reader.readAsDataURL(blob);
    });
    const resp = await apiFetch(`${API_BASE}/api/transcribe`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ audio: dataUri, language: getSavedSttLanguage() || undefined }),
    });
    if (!resp.ok) {
      noticeTranscriptionProblem("Modo Cerebro", null, resp);
      return;
    }
    const data = await resp.json();
    fragmentText = (data.text || "").trim();
  } catch (err) {
    noticeTranscriptionProblem("Modo Cerebro", err);
    return; // fragmento perdido por error de red -- el modo sigue escuchando, no es fatal
  } finally {
    brainTranscriptionsPending = Math.max(0, brainTranscriptionsPending - 1);
  }
  if (!fragmentText || isWhisperHallucination(fragmentText) || !brainModeActive) return;

  // REQ-010: barge-in -- a diferencia de Record (que solo corta la voz si
  // detecta la MENCION), en el modo Cerebro CUALQUIER fragmento con texto
  // real mientras Vision habla se interpreta como "el usuario quiere decir
  // algo nuevo ahora" -- se corta la voz al instante y se sigue escuchando
  // (la nueva intervencion se procesa igual, por auto-envio o mencion).
  if (isSpeaking()) {
    cancelSpeech();
  }

  brainPendingText = brainPendingText ? `${brainPendingText} ${fragmentText}` : fragmentText;

  if (isBrainAutosendEnabled()) {
    // El tiempo configurado se cuenta desde que el usuario DEJO de
    // hablar, no desde que Whisper termino. Asi la transcripcion ocurre
    // durante la pausa de auto-envio: si tarda menos que la pausa, espera
    // solo el remanente; si tarda mas, envia inmediatamente al quedar el
    // texto disponible.
    const elapsedSinceSpeech = performance.now() - speechEndedAt;
    scheduleBrainAutoSend(getBrainAutosendPauseMs() - elapsedSinceSpeech);
    return;
  }

  // REQ-005: auto-envio desactivado -- se necesita decir "Vision"
  // (case-insensitive) para forzar el envio de lo dicho hasta el momento.
  if (/\bvisi[oó]n\b/i.test(fragmentText)) {
    sendBrainIntervention();
  }
}

// REQ-007: cada activacion toma contexto COMPLETO desde cero -- no hace
// falta ningun mecanismo explicito de "recordar" (session.messages nunca
// se resetea, solo se prende/apaga la captura del microfono), por eso esta
// funcion no necesita leer ni restaurar nada del historial: simplemente
// arranca a escuchar, y el PROXIMO turno que se envie ya va a incluir todo
// el historial acumulado de la sesion (mismo run_turn() de siempre).
async function startBrainMode({ notifyMicError = true } = {}) {
  brainModeActive = true;
  brainPendingText = "";
  brainSending = false;
  brainSpeechActive = false;
  setBrainState("idle-active");
  brainStateInterval = setInterval(tickBrainState, 250);

  try {
    // REQ-010 (bug real reportado por el usuario: "no debe escucharse a
    // si mismo"): sin cancelacion de eco, la propia voz de Vision saliendo
    // por los parlantes podia ser recapturada por el microfono como si
    // fuera el usuario hablando -- disparando falsos positivos de VAD y
    // barge-in contra si mismo. `echoCancellation` es soporte NATIVO del
    // navegador (WebRTC APM) que resta la senal de salida conocida de la
    // entrada del microfono -- exactamente el mismo mecanismo que usan
    // Meet/Zoom/etc. `noiseSuppression` ayuda ademas a que el VAD no
    // dispare con ruido de fondo constante (aire acondicionado, etc.).
    // QA-007: con el microfono elegido en Configuracion (ver openMicStream).
    const stream = await openMicStream({ echoCancellation: true, noiseSuppression: true, autoGainControl: true });
    // Escape pudo apagar el modo mientras el navegador pedia el microfono:
    // no dejarlo abierto (y escuchando) sin ningun modo que lo use.
    if (!brainModeActive) {
      stream.getTracks().forEach((track) => track.stop());
      return;
    }
    brainStream = stream;
  } catch (err) {
    // Sin microfono real -- el modo queda "activo" visualmente (el icono
    // ya cambio de estado) pero sin audio real no hay nada que transcribir.
    // QA-008: ya no en silencio -- se avisa, y `refreshActiveMicStreams`
    // lo vuelve a arrancar solo en cuanto se conecte un microfono.
    if (notifyMicError) {
      appendMessage(
        "system-note",
        isMicPermissionError(err)
          ? "Modo Cerebro: Vision no tiene permiso para usar el micrófono. Habilitalo desde el candado de la barra de direcciones; el modo sigue encendido."
          : "Modo Cerebro: no se pudo acceder al micrófono. Sigue encendido y empieza a escuchar apenas conectes uno.",
        { persist: false },
      );
    }
    return;
  }
  noticeMicFallbackOnce();
  watchMicTrackEnd(brainStream, restartBrainModeForMic);

  // VAD por energia RMS (Web Audio API), mismo criterio que el medidor de
  // nivel de Configuracion > Audio (ver startMicMeter) -- analiza el
  // stream en vivo, SIN esperar a que un MediaRecorder termine un
  // fragmento de duracion fija, que es la fuente real de la latencia
  // reportada por el usuario.
  const AudioCtx = window.AudioContext || window.webkitAudioContext;
  brainAudioCtx = new AudioCtx();
  const source = brainAudioCtx.createMediaStreamSource(brainStream);
  brainAnalyser = brainAudioCtx.createAnalyser();
  brainAnalyser.fftSize = 512;
  source.connect(brainAnalyser);
  const vadData = new Uint8Array(brainAnalyser.fftSize);

  let utteranceChunks = [];
  let utteranceStartedAt = 0;
  // BUG real reportado por el usuario 2026-09-14 (segunda vuelta, seguia
  // pasando con solo el chequeo de "speaking" en vivo): apenas
  // `speechSynthesis.speaking` vuelve a `false` al terminar una frase, el
  // audio de Vision puede seguir sonando/reverberando un instante en el
  // ambiente (latencia de los parlantes, eco de la sala) -- el VAD retomaba
  // ESCUCHANDO exactamente ese resto y lo capturaba como una utterance
  // nueva. Se agrega un cooldown despues de que `speaking` deja de estar
  // activo antes de volver a escuchar.
  const BRAIN_TTS_COOLDOWN_MS = 900;
  let brainLastSpeakingAt = 0;

  function beginUtterance() {
    if (!brainStream) return;
    utteranceChunks = [];
    brainRecorder = new MediaRecorder(brainStream);
    brainRecorder.ondataavailable = (e) => utteranceChunks.push(e.data);
    brainRecorder.onstop = () => {
      const blob = new Blob(utteranceChunks, { type: "audio/webm" });
      brainRecorder = null;
      // Escape puede haber apagado el modo mientras MediaRecorder emitia
      // su evento final. No transcribir ni enviar ese fragmento tardio.
      if (!brainModeActive) return;
      // No se espera (await) aca -- no bloquea seguir escuchando mientras
      // esta "frase" recien capturada se transcribe en paralelo.
      processBrainUtterance(blob, performance.now());
    };
    brainRecorder.start();
    utteranceStartedAt = performance.now();
  }

  function endUtterance() {
    if (brainRecorder && brainRecorder.state !== "inactive") {
      brainRecorder.stop();
    }
  }

  function vadTick() {
    if (!brainModeActive || !brainAnalyser) return;
    // BUG real reportado por el usuario (2026-09-14): "se auto escucha
    // cuando responde". El `echoCancellation` de getUserMedia (mas arriba)
    // resuelve el eco de audio/video de la propia pestaña, pero NO cancela
    // de forma confiable la salida de `speechSynthesis` -- el motor de voz
    // nativo del sistema operativo, que en la mayoria de los navegadores
    // reproduce FUERA del grafo de Web Audio/WebRTC de la pestaña (no hay
    // referencia de esa senal para restarla). Mientras Vision esta
    // hablando, se pausa el VAD por completo -- no arranca ninguna
    // utterance nueva -- y se retoma recien pasado `BRAIN_TTS_COOLDOWN_MS`
    // desde la ULTIMA vez que se la vio hablando (no apenas `speaking`
    // vuelve a false), para darle tiempo al audio real de dejar de sonar/
    // reverberar en el ambiente.
    // spec voz-fish-audio REQ-010: con un motor de voz del backend el audio
    // sale por Web Audio de esta pestaña; se pausa igual que con el navegador.
    const nowSpeaking = isSpeaking();
    if (nowSpeaking) brainLastSpeakingAt = performance.now();
    if (nowSpeaking || performance.now() - brainLastSpeakingAt < BRAIN_TTS_COOLDOWN_MS) {
      updateBrainWaveform(0);
      brainVadRafId = requestAnimationFrame(vadTick);
      return;
    }
    brainAnalyser.getByteTimeDomainData(vadData);
    let sumSquares = 0;
    for (let i = 0; i < vadData.length; i++) {
      const normalized = (vadData[i] - 128) / 128;
      sumSquares += normalized * normalized;
    }
    const rms = Math.sqrt(sumSquares / vadData.length);
    const now = performance.now();
    const hasVoice = rms >= BRAIN_SILENCE_THRESHOLD;

    if (hasVoice) {
      brainSpeechActive = true;
      brainSilenceStartedAt = 0;
      if (!brainRecorder) beginUtterance(); // arranca a grabar apenas detecta la primera energia real
      // Limite duro: si el usuario habla sin pausas por mas de
      // BRAIN_MAX_UTTERANCE_MS, se corta igual para no acumular un blob
      // gigante ni una latencia de transcripcion excesiva.
      if (brainRecorder && now - utteranceStartedAt >= BRAIN_MAX_UTTERANCE_MS) {
        endUtterance();
      }
    } else {
      brainSpeechActive = false;
      if (brainRecorder) {
        // Recien confirma "dejo de hablar" tras BRAIN_SILENCE_CONFIRM_MS
        // de silencio sostenido -- una pausa breve entre palabras no debe
        // cortar la frase a la mitad.
        if (brainSilenceStartedAt === 0) brainSilenceStartedAt = now;
        if (now - brainSilenceStartedAt >= BRAIN_SILENCE_CONFIRM_MS) {
          endUtterance();
          brainSilenceStartedAt = 0;
        }
      }
    }
    updateBrainWaveform(hasVoice ? rms : 0);
    brainVadRafId = requestAnimationFrame(vadTick);
  }
  vadTick();
}

// REQ-004/007 (revisado): apaga el modo COMPLETO -- unica forma de
// llegar aca es Escape o volver a tocar el icono (ya NO hay auto-apagado
// por inactividad, ver comentario en la declaracion de variables mas
// arriba). La proxima activacion (nuevo click) siempre vuelve a arrancar
// desde cero via startBrainMode(), tomando contexto completo del
// historial real.
function stopBrainMode() {
  brainModeActive = false;
  clearTimeout(brainAutoSendTimer);
  clearInterval(brainStateInterval);
  if (brainVadRafId) cancelAnimationFrame(brainVadRafId);
  brainVadRafId = null;
  if (brainRecorder && brainRecorder.state !== "inactive") brainRecorder.stop();
  brainRecorder = null;
  if (brainAudioCtx) {
    brainAudioCtx.close().catch(() => {});
    brainAudioCtx = null;
  }
  brainAnalyser = null;
  brainSpeechActive = false;
  if (brainStream) {
    brainStream.getTracks().forEach((track) => track.stop());
    brainStream = null;
  }
  brainPendingText = "";
  brainSending = false;
  brainTranscriptionsPending = 0;
  setBrainState("idle");
  updateBrainWaveform(0); // oculta las ondas -- ya no hay audio que graficar
}

// QA-008: el microfono del modo Cerebro desaparecio o hay que pasar al
// elegido en Configuracion. El pipeline (VAD sobre un AudioContext + un
// MediaRecorder por frase) esta atado al stream original, asi que en vez de
// recablearlo pieza por pieza se reinicia el modo completo sobre el
// microfono nuevo -- se pierde, como mucho, la frase que se estaba diciendo
// justo al desenchufar. Si no hay ningun microfono, el modo queda encendido
// y avisado; el proximo `devicechange` lo reintenta.
let brainMicRestarting = false;

async function restartBrainModeForMic() {
  if (!brainModeActive || brainMicRestarting) return;
  brainMicRestarting = true;
  try {
    stopBrainMode();
    await startBrainMode({ notifyMicError: false });
    if (!brainModeActive) return; // Escape durante el reinicio
    if (!brainStream) {
      appendMessage(
        "system-note",
        "Modo Cerebro: se desconectó el micrófono y no hay otro conectado. Sigue encendido y vuelve a escuchar apenas conectes uno.",
        { persist: false },
      );
      return;
    }
    appendMessage("system-note", `Modo Cerebro: escuchando por «${micLabel(brainStream)}».`, { persist: false });
  } finally {
    brainMicRestarting = false;
  }
}

// REQ-004: click activa/desactiva -- sin modal de confirmacion (a
// diferencia de Record, el modo Cerebro no genera un resumen aparte al
// salir: cada intervencion ya quedo persistida como turno de chat real en
// el momento en que se disparo, no hay nada que "perder" al apagar).
brainBtnEl.addEventListener("click", () => {
  // Pedido explicito posterior a REQ-007: una vez activo, SOLO Escape lo
  // apaga (mismo patron de una conversacion continua en Copilot/ChatGPT/
  // Claude/Gemini). Un click accidental sobre el icono activo no corta el
  // microfono ni pierde una intervencion que se estaba transcribiendo.
  if (!brainModeActive) startBrainMode();
});

// REQ-004/007: Escape es el otro disparador de salida, equivalente a
// volver a tocar el icono -- ambos apagan el modo por completo de inmediato.
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && brainModeActive) stopBrainMode();
});

// REQ-001/007/008 (spec atajo-espacio-modo-cerebro, 2026-09-26): la barra
// espaciadora, mientras Modo Cerebro esta activo y el atajo habilitado en
// Configuracion > Audio, (1) corta el audio de Vision si esta hablando
// (mismo mecanismo confiable de la spec fix-boton-play-stop -- cancelSpeech()
// + verificacion real de speechSynthesis.speaking en vez del optimismo viejo)
// y (2) reinicia la RONDA de escucha en curso: descarta brainPendingText
// acumulado y cancela el timer de auto-envio pendiente, "como apagar y
// prender" pero SIN cerrar el stream de microfono (reset LIVIANO, pedido
// explicito del usuario: "para que no demore y me escuche de una" -- cerrar
// y reabrir el hardware tiene latencia real que aca se evita a proposito).
//
// BLINDAJE REQ-007 (pedido explicito: "Modo Cerebro funciona muy bien no
// rompas nada"): este handler es la UNICA excepcion permitida a la regla de
// "cero cambios en la logica interna de Modo Cerebro" -- solo LEE
// brainModeActive/speechSynthesis.speaking, solo ESCRIBE brainPendingText
// (vaciandolo) y cancela brainAutoSendTimer (mismo patron de limpieza que ya
// usa stopBrainMode() en su propio apagado, app.js linea ~5592). NUNCA toca
// brainStream, brainRecorder, brainAnalyser, brainAudioCtx,
// utteranceChunks, brainSending, brainTranscriptionsPending, ni ningun timer
// propio del VAD (brainVadRafId) -- el stream de audio y el analisis de
// energia siguen corriendo exactamente igual que antes del atajo.
//
// Sin importar el foco del teclado (decision explicita del usuario: en
// Modo Cerebro normalmente no se esta escribiendo a mano, se habla por el
// microfono) -- a diferencia de Escape (arriba), que si aplica siempre.
document.addEventListener("keydown", (e) => {
  if (e.code !== "Space" && e.key !== " ") return;
  if (!brainModeActive || !isBrainSpaceInterruptEnabled()) return;
  e.preventDefault(); // evita el scroll de pagina / activar un boton enfocado con espacio
  if (isSpeaking()) { // spec voz-fish-audio REQ-010: tambien corta el audio de los motores de voz
    cancelSpeech();
  }
  // REQ-008: reset liviano de la ronda en curso -- NO toca brainStream/VAD.
  clearTimeout(brainAutoSendTimer);
  brainPendingText = "";
});

// -------------------------------------------------------------------------
// Boton de pantalla completa
// -------------------------------------------------------------------------
fullscreenBtnEl.addEventListener("click", async () => {
  try {
    if (!document.fullscreenElement) {
      await document.documentElement.requestFullscreen();
    } else {
      await document.exitFullscreen();
    }
  } catch (err) {
    console.warn("No se pudo cambiar pantalla completa:", err);
  }
});
document.addEventListener("fullscreenchange", () => {
  const isFull = Boolean(document.fullscreenElement);
  fullscreenBtnEl.querySelector(".ic-expand").classList.toggle("hidden", isFull);
  fullscreenBtnEl.querySelector(".ic-collapse").classList.toggle("hidden", !isFull);
  fullscreenBtnEl.title = isFull ? "Salir de pantalla completa" : "Pantalla completa";
});

// =========================================================================
// Configuracion (modal con tabs): Modelo e inteligencia / Sitio SharePoint /
// Skills / Comandos / Credenciales. ("Proyecto local" se elimino -- ahora
// es siempre por sesion, ver modal de nueva sesion / edicion de sesion.)
// =========================================================================
function openSettings(tab) {
  settingsOverlayEl.classList.remove("hidden");
  if (tab) switchSettingsTab(tab);
  loadModelSettingsPane();
  loadAppearanceSettingsPane();
  loadAudioSettingsPane();
  loadSkillsSettingsPane();
  loadCommandsSettingsPane();
  loadAgentsSettingsPane();
  loadCredentialsSettingsPane();
  loadServerSettingsPane();
}
function closeSettings() {
  settingsOverlayEl.classList.add("hidden");
  // REQ-009: el medidor de nivel NUNCA debe seguir capturando audio con
  // el modal de Configuracion cerrado -- se libera siempre aca, sin
  // importar en que pestaña haya quedado abierto el modal.
  stopMicMeter();
  // spec agentes-programados: mismo criterio -- el polling de "ejecutando
  // ahora"/historial no debe seguir corriendo con el modal cerrado.
  stopAgentsPolling();
}
settingsBtnEl.addEventListener("click", () => openSettings());
settingsCloseEl.addEventListener("click", closeSettings);
settingsOverlayEl.addEventListener("click", (e) => {
  if (e.target === settingsOverlayEl) closeSettings();
});

function switchSettingsTab(tabName) {
  settingsTabs.forEach((btn) => btn.classList.toggle("active", btn.dataset.tab === tabName));
  settingsPanes.forEach((pane) => pane.classList.toggle("hidden", pane.dataset.pane !== tabName));
  // REQ-009: "bajo demanda" -- el medidor solo captura mientras la
  // pestaña Audio esta REALMENTE visible, se corta apenas se cambia a
  // cualquier otra pestaña (Modelo, Apariencia, etc).
  if (tabName === "audio") startMicMeter();
  else stopMicMeter();
  // spec agentes-programados: al abrir "Agentes" se refresca la lista
  // unificada (agentes+alarmas) y arranca el polling "bajo demanda" de
  // estado/historial SOLO mientras esta pestaña esta visible.
  if (tabName === "agentes") { loadAgentsSettingsPane(); startAgentsPolling(); }
  else stopAgentsPolling();
  // Pestaña "Conexiones": bajo demanda, mismo criterio que Agentes/Audio
  // arriba -- evita pegarle a BigQuery/SharePoint/Oracle/SAP solo por
  // abrir el modal de Configuracion en otra pestaña.
  if (tabName === "connections") loadConnectionsSettingsPane();
}
settingsTabs.forEach((btn) => btn.addEventListener("click", () => switchSettingsTab(btn.dataset.tab)));

// -------------------------------------------------------------------------
// Tab: Apariencia -- 3 temas visuales, pedido explicito del usuario:
//   "dark"      Oscuro Rojo (rojo intenso, original)
//   "light"     Claro Azul (#0000FF, fondos blancos) -- default actual
//   "dark-blue" Oscuro Azul (#0000FF, MISMOS fondos oscuros que
//               "dark" -- agregado 2026-09-08, pedido explicito)
// Se aplica via `data-theme` en <html>, que dispara los bloques
// [data-theme="light"]/[data-theme="dark-blue"] de styles.css (variables
// --accent/--canvas/--surface-*/--ink-* nuevas; "dark" es el default de
// :root, sin atributo). Los colores de semaforo (--signal-red/-amber/
// -green) NUNCA cambian entre NINGUN tema -- el chip Plan/Activo y el
// boton Enviar/Detener quedan intactos siempre, tal como se pidio
// explicitamente.
//
// Aplicado tambien de forma sincronica en un <script> chico en el <head>
// de index.html (ANTES de este modulo, que carga diferido) para evitar
// el flash del tema oscuro por defecto al recargar con otro tema guardado
// -- esta es la fuente real (persiste + sincroniza el <select>), esa
// copia inline es solo el "primer pintado" rapido.
// -------------------------------------------------------------------------
const THEME_KEY = "vision.theme";
const VALID_THEMES = ["dark", "light", "dark-blue"];
const cfgThemeSelectEl = document.getElementById("cfg-theme-select");

function applyTheme(theme) {
  const resolved = VALID_THEMES.includes(theme) ? theme : "light";
  if (resolved === "dark") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = resolved;
  try {
    localStorage.setItem(THEME_KEY, resolved);
  } catch {
    // noop -- sin localStorage el tema no persiste entre recargas, pero sigue aplicado en esta sesion
  }
}

function getSavedTheme() {
  // Pedido explicito del usuario (2026-09-08): "azul" (claro) pasa a
  // ser el tema default -- mismo criterio que el script inline de
  // index.html (fuente de verdad duplicada ahi solo para evitar el flash
  // de tema oscuro antes de que este modulo cargue). Si el usuario ya
  // eligio "dark" o "dark-blue" explicitamente alguna vez, esa
  // preferencia se respeta.
  try {
    const saved = localStorage.getItem(THEME_KEY);
    return VALID_THEMES.includes(saved) ? saved : "light";
  } catch {
    return "light";
  }
}

function loadAppearanceSettingsPane() {
  if (cfgThemeSelectEl) cfgThemeSelectEl.value = getSavedTheme();
}

if (cfgThemeSelectEl) {
  cfgThemeSelectEl.addEventListener("change", () => applyTheme(cfgThemeSelectEl.value));
}

// Re-aplica al cargar el modulo (redundante con el <script> inline del
// <head> en el caso normal, pero es la unica via si localStorage cambio
// en otra pestaña/dispositivo, o si ese script inline no llego a correr).
applyTheme(getSavedTheme());

// -------------------------------------------------------------------------
// Tab: Audio (REQ-009/010, spec narracion-agente-efimera) -- activar/
// desactivar TTS (REQ-007/008), elegir voz en español, probar la voz
// configurada, elegir microfono, y medidor de nivel EN VIVO que solo
// captura mientras esta pestaña/modal esta abierto (patron de Meetily, ver
// Decisiones registradas en spec.md -- evita gastar CPU/bateria de fondo).
// -------------------------------------------------------------------------
const cfgTtsEnabledEl = document.getElementById("cfg-tts-enabled");
const cfgTtsTechReportEl = document.getElementById("cfg-tts-tech-report"); // spec informe-tecnico-hablado
const cfgTtsVoiceSelectEl = document.getElementById("cfg-tts-voice-select");
const cfgTtsTestBtnEl = document.getElementById("cfg-tts-test-btn");
const cfgMicSelectEl = document.getElementById("cfg-mic-select");
const cfgMicMeterFillEl = document.getElementById("cfg-mic-meter-fill");
const cfgMicStatusEl = document.getElementById("cfg-mic-status");
const cfgSttLanguageSelectEl = document.getElementById("cfg-stt-language-select");
const cfgLoopAlarmTypeEl = document.getElementById("cfg-loop-alarm-type");
const cfgLoopAlarmIntervalEl = document.getElementById("cfg-loop-alarm-interval");
const cfgLoopAlarmVolumeEl = document.getElementById("cfg-loop-alarm-volume");
const cfgLoopAlarmVolumeValEl = document.getElementById("cfg-loop-alarm-volume-val");
const cfgLoopAlarmTestEl = document.getElementById("cfg-loop-alarm-test");

// REQ-012 (spec asistente-contextual-voz): controles de Configuracion >
// Audio para el modo Cerebro (auto-envio + pausa configurable).
const cfgBrainAutosendEnabledEl = document.getElementById("cfg-brain-autosend-enabled");
const cfgBrainAutosendPauseEl = document.getElementById("cfg-brain-autosend-pause");
const cfgBrainAutosendPauseFieldEl = document.getElementById("cfg-brain-autosend-pause-field");
// REQ-004 (spec atajo-espacio-modo-cerebro): checkbox del atajo de espacio.
const cfgBrainSpaceInterruptEnabledEl = document.getElementById("cfg-brain-space-interrupt-enabled");

// Bug real reportado por el usuario: hablando en español, Whisper a veces
// transcribia en INGLES -- sin forzar el idioma, Whisper hace auto-
// deteccion por cada clip de audio, y con fragmentos cortos/ruido puede
// "adivinar mal" (comportamiento conocido de Whisper). Se persiste la
// preferencia igual que THEME_KEY -- "" (vacio) = auto-detectar (default
// explicito pedido por el usuario), o un codigo ISO-639-1 que fuerza el
// idioma en el backend (ver TranscribeRequest.language en main.py).
const STT_LANGUAGE_KEY = "vision.stt.language";

function getSavedSttLanguage() {
  try {
    return localStorage.getItem(STT_LANGUAGE_KEY) || "";
  } catch {
    return "";
  }
}

function setSavedSttLanguage(lang) {
  try {
    localStorage.setItem(STT_LANGUAGE_KEY, lang || "");
  } catch {
    // noop -- sin localStorage la preferencia no persiste entre recargas, pero sigue aplicada en esta sesion
  }
}

if (cfgSttLanguageSelectEl) {
  cfgSttLanguageSelectEl.addEventListener("change", () => setSavedSttLanguage(cfgSttLanguageSelectEl.value));
}

// -- Micrófono elegido + plug and play (QA-007 / QA-008) -----------------
// BUG real corregido (QA-007, detección QA 2026-09-21): el selector
// "Micrófono" de Configuración > Audio solo alimentaba el MEDIDOR de nivel.
// Los 3 caminos que graban de verdad (mic puntual, modo reunión y modo
// Cerebro) pedían `getUserMedia` sin `deviceId`, así que grababan SIEMPRE
// del predeterminado de Windows: el usuario elegía su micrófono USB, veía
// moverse el medidor, y Vision lo escuchaba por otro. Además la elección no
// se guardaba y se perdía al recargar. Ahora la elección se persiste y TODO
// acceso al micrófono pasa por `openMicStream()`.
//
// QA-008 (misma detección): no había plug and play. No existía ningún
// `devicechange` (un micrófono enchufado no aparecía en la lista hasta
// reabrir la pestaña) ni manejo de `ended` en los streams largos de
// Cerebro/reunión (desenchufar el micrófono los dejaba "activos" en
// pantalla, sin audio y sin aviso). Ver `refreshActiveMicStreams`.
//
// Se guarda el `deviceId` Y el NOMBRE del micrófono: el navegador puede
// cambiar el `deviceId` de un mismo micrófono (lo permite la spec: se
// regenera al borrar cookies/datos del sitio, en ventanas privadas, o con
// políticas corporativas que limpian el navegador al salir; en Chromium de
// prueba incluso cambia en cada recarga -- verificado en los tests e2e).
// Guardando solo el id, Vision "olvidaría" el micrófono elegido y avisaría
// en falso que está desconectado. Si el id ya no existe, se lo reconoce
// por el nombre (mismo criterio que usan las apps de videollamada).
const MIC_DEVICE_KEY = "vision.mic.deviceId";
const MIC_LABEL_KEY = "vision.mic.label";
const MIC_DISCONNECTED_STATUS =
  "El micrófono elegido no está conectado. Vision usa el predeterminado y vuelve al elegido apenas lo conectes.";
// true cuando el micrófono guardado no está conectado y se abrió el
// predeterminado en su lugar -- lo lee el aviso "una sola vez" de abajo.
let _micUsingFallback = false;
let _micFallbackNoticeShown = false;

function getSavedMic() {
  try {
    return {
      deviceId: localStorage.getItem(MIC_DEVICE_KEY) || "",
      label: localStorage.getItem(MIC_LABEL_KEY) || "",
    };
  } catch {
    return { deviceId: "", label: "" };
  }
}

function setSavedMic(deviceId, label) {
  try {
    localStorage.setItem(MIC_DEVICE_KEY, deviceId || "");
    localStorage.setItem(MIC_LABEL_KEY, deviceId ? label || "" : "");
  } catch {
    // noop -- sin localStorage la elección no persiste entre recargas, pero sigue aplicada en esta sesión
  }
}

// Busca el micrófono guardado entre los conectados AHORA (`mics`, entradas
// `audioinput` de enumerateDevices). Devuelve:
//   ""   -> no hay preferencia: usar el predeterminado;
//   id   -> el deviceId ACTUAL del elegido (si se lo encontró por nombre
//           porque el navegador le cambió el id, se actualiza lo guardado);
//   null -> hay preferencia pero ese micrófono no está conectado.
function matchSavedMic(mics) {
  const saved = getSavedMic();
  if (!saved.deviceId) return "";
  if (mics.some((mic) => mic.deviceId === saved.deviceId)) return saved.deviceId;
  const byLabel = saved.label ? mics.find((mic) => mic.label === saved.label) : null;
  if (byLabel) {
    setSavedMic(byLabel.deviceId, byLabel.label);
    return byLabel.deviceId;
  }
  // Sin permiso de micrófono los nombres vienen vacíos y no se puede
  // afirmar que no esté: se intenta con el id guardado tal cual.
  if (mics.length > 0 && mics.every((mic) => !mic.label)) return saved.deviceId;
  return null;
}

async function listAudioInputs() {
  const devices = await navigator.mediaDevices.enumerateDevices();
  return devices.filter((d) => d.kind === "audioinput");
}

async function resolveSavedMicDeviceId() {
  try {
    return matchSavedMic(await listAudioInputs());
  } catch {
    return getSavedMic().deviceId; // sin enumerateDevices: se intenta el guardado
  }
}

// Único punto por el que Vision abre el micrófono. `extraAudio` son las
// constraints propias de cada modo (ej. cancelación de eco del modo
// Cerebro). Si el micrófono guardado no está conectado cae al
// predeterminado en vez de dejar al usuario sin voz (también si desaparece
// justo entre la búsqueda y la apertura: OverconstrainedError/NotFoundError);
// cualquier otro error (permiso denegado) se propaga igual que antes para
// que cada modo lo maneje.
async function openMicStream(extraAudio = {}) {
  const deviceId = await resolveSavedMicDeviceId();
  if (deviceId) {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: { ...extraAudio, deviceId: { exact: deviceId } },
      });
      _micUsingFallback = false;
      _micFallbackNoticeShown = false;
      return stream;
    } catch (err) {
      if (err?.name !== "OverconstrainedError" && err?.name !== "NotFoundError") throw err;
    }
  }
  const hasExtra = Object.keys(extraAudio).length > 0;
  const stream = await navigator.mediaDevices.getUserMedia({ audio: hasExtra ? extraAudio : true });
  _micUsingFallback = deviceId !== ""; // null (no conectado) o un id que no se pudo abrir
  return stream;
}

// Aviso en el chat, UNA sola vez por episodio (hasta que el elegido vuelva a
// abrirse), de que se está escuchando por el predeterminado -- sin esto el
// usuario no tiene forma de saber que Vision no lo escucha por su micrófono.
function noticeMicFallbackOnce() {
  if (!_micUsingFallback || _micFallbackNoticeShown) return;
  _micFallbackNoticeShown = true;
  appendMessage(
    "system-note",
    "El micrófono elegido en Configuración > Audio no está conectado: Vision escucha por el predeterminado hasta que lo vuelvas a conectar.",
    { persist: false },
  );
}

// `ended` se dispara cuando desaparece la FUENTE (micrófono desenchufado,
// driver reiniciado) -- NUNCA cuando el propio código llama `track.stop()`,
// así que apagar un modo a propósito no dispara ninguna reconexión.
function watchMicTrackEnd(stream, onEnded) {
  stream.getAudioTracks().forEach((track) => track.addEventListener("ended", onEnded, { once: true }));
}

function hasLiveAudioTrack(stream) {
  return Boolean(stream) && stream.getAudioTracks().some((track) => track.readyState === "live");
}

function streamDeviceId(stream) {
  const track = stream?.getAudioTracks()[0];
  return track?.getSettings?.().deviceId || "";
}

function micLabel(stream) {
  return stream?.getAudioTracks()[0]?.label || "el micrófono predeterminado";
}

function isAudioSettingsPaneOpen() {
  return Boolean(settingsPanes && document.querySelector('.settings-pane[data-pane="audio"]:not(.hidden)'));
}

// Pasa los modos de escucha continua (Cerebro, reunión) al micrófono que
// corresponde AHORA: si su stream murió, o si el micrófono elegido está
// conectado y no es el que están usando (se volvió a enchufar, o se cambió
// el selector). El mic puntual no lo necesita: abre un stream nuevo en
// cada click.
async function refreshActiveMicStreams() {
  if (!brainModeActive && !meetingModeActive) return;
  const savedId = await resolveSavedMicDeviceId(); // "" | id actual | null (desconectado)
  const needsSwitch = (stream) =>
    !hasLiveAudioTrack(stream) || (Boolean(savedId) && streamDeviceId(stream) !== savedId);
  if (brainModeActive && needsSwitch(brainStream)) restartBrainModeForMic();
  if (meetingModeActive && needsSwitch(meetingStream)) reacquireMeetingMic();
}

async function handleMicDeviceChange() {
  // La lista de Configuración > Audio se actualiza sola (antes había que
  // cerrar y reabrir la pestaña), y el medidor se reengancha si su
  // micrófono desapareció o apareció el elegido.
  populateMicOptions();
  if (isAudioSettingsPaneOpen()) startMicMeter();
  refreshActiveMicStreams();
}

if (navigator.mediaDevices?.addEventListener) {
  navigator.mediaDevices.addEventListener("devicechange", handleMicDeviceChange);
}

// REQ-012 (spec asistente-contextual-voz): persistencia de las
// preferencias del modo Cerebro -- mismo patron que STT_LANGUAGE_KEY de
// arriba (localStorage, con catch silencioso si el storage no esta
// disponible). Auto-envio ACTIVADO por defecto (pedido explicito del
// usuario); pausa 1-10s, default 2s (desde 2026-09-12 el MICROFONO de
// mensaje puntual del compositor comparte este MISMO toggle y pausa: si
// auto-envio esta en true, el texto dictado se manda solo tras la pausa;
// si esta en false, queda manual).
const BRAIN_AUTOSEND_ENABLED_KEY = "vision.brain.autosendEnabled";
const BRAIN_AUTOSEND_PAUSE_KEY = "vision.brain.autosendPauseMs";
const BRAIN_AUTOSEND_PAUSE_DEFAULT_MS = 2000;

function isBrainAutosendEnabled() {
  try {
    const raw = localStorage.getItem(BRAIN_AUTOSEND_ENABLED_KEY);
    return raw === null ? true : raw === "true"; // default: activado
  } catch {
    return true;
  }
}

function setBrainAutosendEnabled(enabled) {
  try {
    localStorage.setItem(BRAIN_AUTOSEND_ENABLED_KEY, String(enabled));
  } catch {
    // noop -- sin localStorage la preferencia no persiste entre recargas, pero sigue aplicada en esta sesion
  }
}

function getBrainAutosendPauseMs() {
  try {
    const raw = parseInt(localStorage.getItem(BRAIN_AUTOSEND_PAUSE_KEY), 10);
    if (Number.isFinite(raw) && raw >= 1 && raw <= 10) return raw * 1000;
    return BRAIN_AUTOSEND_PAUSE_DEFAULT_MS;
  } catch {
    return BRAIN_AUTOSEND_PAUSE_DEFAULT_MS;
  }
}

function setBrainAutosendPauseSeconds(seconds) {
  const clamped = Math.max(1, Math.min(10, parseInt(seconds, 10) || 2));
  try {
    localStorage.setItem(BRAIN_AUTOSEND_PAUSE_KEY, String(clamped));
  } catch {
    // noop -- ver comentario de setBrainAutosendEnabled
  }
  return clamped;
}

// REQ-004 (spec atajo-espacio-modo-cerebro): preferencia del atajo de
// espacio -- mismo patron de persistencia y default (activado) que
// BRAIN_AUTOSEND_ENABLED_KEY de arriba.
const BRAIN_SPACE_INTERRUPT_KEY = "vision.brain.spaceInterruptEnabled";

function isBrainSpaceInterruptEnabled() {
  try {
    const raw = localStorage.getItem(BRAIN_SPACE_INTERRUPT_KEY);
    return raw === null ? true : raw === "true"; // default: activado
  } catch {
    return true;
  }
}

function setBrainSpaceInterruptEnabled(enabled) {
  try {
    localStorage.setItem(BRAIN_SPACE_INTERRUPT_KEY, String(enabled));
  } catch {
    // noop -- sin localStorage la preferencia no persiste entre recargas, pero sigue aplicada en esta sesion
  }
}

if (cfgBrainAutosendEnabledEl) {
  cfgBrainAutosendEnabledEl.addEventListener("change", () => {
    setBrainAutosendEnabled(cfgBrainAutosendEnabledEl.checked);
    if (cfgBrainAutosendPauseFieldEl) {
      cfgBrainAutosendPauseFieldEl.classList.toggle("settings-field-disabled", !cfgBrainAutosendEnabledEl.checked);
    }
  });
}
if (cfgBrainAutosendPauseEl) {
  cfgBrainAutosendPauseEl.addEventListener("change", () => {
    cfgBrainAutosendPauseEl.value = setBrainAutosendPauseSeconds(cfgBrainAutosendPauseEl.value);
  });
}
if (cfgBrainSpaceInterruptEnabledEl) {
  cfgBrainSpaceInterruptEnabledEl.addEventListener("change", () => {
    setBrainSpaceInterruptEnabled(cfgBrainSpaceInterruptEnabledEl.checked);
  });
}

function populateTtsVoiceOptions() {
  if (!cfgTtsVoiceSelectEl) return;
  const voices = getAvailableSpanishVoices();
  const currentValue = cfgTtsVoiceSelectEl.value;
  // La 1ra opcion ("automatico") ya esta fija en el HTML -- se preserva y
  // se reconstruye solo el resto, para no perder la seleccion "automatica".
  cfgTtsVoiceSelectEl.length = 1;
  for (const voice of voices) {
    const opt = document.createElement("option");
    opt.value = voice.voiceURI;
    opt.textContent = `${voice.name} (${voice.lang})`;
    cfgTtsVoiceSelectEl.appendChild(opt);
  }
  cfgTtsVoiceSelectEl.value = currentValue || "";
}

// En Chrome la lista de voces no esta lista de entrada (se carga async) --
// mismo patron documentado en MDN para SpeechSynthesis.getVoices().
if ("speechSynthesis" in window && "onvoiceschanged" in window.speechSynthesis) {
  window.speechSynthesis.onvoiceschanged = populateTtsVoiceOptions;
}

function loadAudioSettingsPane() {
  if (cfgTtsEnabledEl) cfgTtsEnabledEl.checked = isTtsEnabled();
  if (cfgTtsTechReportEl) cfgTtsTechReportEl.checked = isTechReportEnabled();
  loadVoiceSettings(); // spec voz-fish-audio (no bloquea el resto del panel)
  if (cfgSttLanguageSelectEl) cfgSttLanguageSelectEl.value = getSavedSttLanguage();
  // Alarma del loop (configurable): refleja el estado guardado.
  if (cfgLoopAlarmTypeEl) cfgLoopAlarmTypeEl.value = loopAlarmType();
  if (cfgLoopAlarmIntervalEl) cfgLoopAlarmIntervalEl.value = String(loopAlarmIntervalMs());
  if (cfgLoopAlarmVolumeEl) cfgLoopAlarmVolumeEl.value = String(loopAlarmVolume());
  if (cfgLoopAlarmVolumeValEl) cfgLoopAlarmVolumeValEl.textContent = `${Math.round(loopAlarmVolume() * 100)}%`;
  // REQ-012: refleja el estado guardado de auto-envio del modo Cerebro.
  if (cfgBrainAutosendEnabledEl) {
    cfgBrainAutosendEnabledEl.checked = isBrainAutosendEnabled();
    if (cfgBrainAutosendPauseFieldEl) {
      cfgBrainAutosendPauseFieldEl.classList.toggle("settings-field-disabled", !cfgBrainAutosendEnabledEl.checked);
    }
  }
  if (cfgBrainAutosendPauseEl) cfgBrainAutosendPauseEl.value = getBrainAutosendPauseMs() / 1000;
  // REQ-004: refleja el estado guardado del atajo de espacio.
  if (cfgBrainSpaceInterruptEnabledEl) cfgBrainSpaceInterruptEnabledEl.checked = isBrainSpaceInterruptEnabled();
  populateTtsVoiceOptions();
  const bestVoice = pickBestSpanishMaleVoice();
  if (cfgTtsVoiceSelectEl && !cfgTtsVoiceSelectEl.value && bestVoice) {
    // Muestra CUAL seria la voz automatica (sin fijarla como seleccion
    // explicita) -- puramente informativo para que el usuario sepa que
    // hay una voz en espanol disponible incluso sin elegir ninguna a mano.
    cfgTtsVoiceSelectEl.title = `Automático elegiría: ${bestVoice.name}`;
  }
  populateMicOptions();
}

if (cfgTtsEnabledEl) {
  cfgTtsEnabledEl.addEventListener("change", () => setTtsEnabled(cfgTtsEnabledEl.checked));
}
if (cfgTtsTechReportEl) {
  cfgTtsTechReportEl.addEventListener("change", () => setTechReportEnabled(cfgTtsTechReportEl.checked));
}
if (cfgTtsVoiceSelectEl) {
  cfgTtsVoiceSelectEl.addEventListener("change", () => setSavedVoiceURI(cfgTtsVoiceSelectEl.value));
}
if (cfgLoopAlarmTypeEl) {
  cfgLoopAlarmTypeEl.addEventListener("change", () => {
    try { localStorage.setItem(LOOP_ALARM_TYPE_KEY, cfgLoopAlarmTypeEl.value); } catch { /* noop */ }
  });
}
if (cfgLoopAlarmIntervalEl) {
  cfgLoopAlarmIntervalEl.addEventListener("change", () => {
    try { localStorage.setItem(LOOP_ALARM_INTERVAL_KEY, cfgLoopAlarmIntervalEl.value); } catch { /* noop */ }
  });
}
if (cfgLoopAlarmVolumeEl && cfgLoopAlarmVolumeValEl) {
  cfgLoopAlarmVolumeEl.addEventListener("input", () => {
    cfgLoopAlarmVolumeValEl.textContent = Math.round(parseFloat(cfgLoopAlarmVolumeEl.value) * 100) + "%";
    try { localStorage.setItem(LOOP_ALARM_VOLUME_KEY, cfgLoopAlarmVolumeEl.value); } catch { /* noop */ }
  });
}
if (cfgLoopAlarmTestEl) {
  cfgLoopAlarmTestEl.addEventListener("click", playLoopAlarm);
}

if (cfgTtsTestBtnEl) {
  cfgTtsTestBtnEl.addEventListener("click", () => speakTestPhrase());
}

// -------------------------------------------------------------------------
// spec voz-fish-audio REQ-001/002/003/005/006/007/008: Configuracion > Audio
// > Motor de voz. La cadena se edita en memoria (agregar, subir/bajar,
// quitar, probar) y se guarda con "Guardar voz", igual que la lista del salto
// automatico de modelos. Un servicio en la nube pide confirmar el envio del
// texto antes de entrar a la lista (REQ-002).
// -------------------------------------------------------------------------
const cfgVoiceChainEl = document.getElementById("cfg-voice-chain");
const cfgVoiceAddEngineEl = document.getElementById("cfg-voice-add-engine");
const cfgVoiceAddModelEl = document.getElementById("cfg-voice-add-model");
const cfgVoiceAddEl = document.getElementById("cfg-voice-add");
const cfgVoiceFishIdEl = document.getElementById("cfg-voice-fish-id");
const cfgVoiceFishPickEl = document.getElementById("cfg-voice-fish-pick");
const cfgVoiceElevenlabsIdEl = document.getElementById("cfg-voice-elevenlabs-id");
const cfgVoiceElevenlabsPickEl = document.getElementById("cfg-voice-elevenlabs-pick");
const cfgVoiceEffectEl = document.getElementById("cfg-voice-effect");
const cfgVoicePauseEl = document.getElementById("cfg-voice-pause");
const cfgVoiceConsentFishEl = document.getElementById("cfg-voice-consent-fish");
const cfgVoiceConsentElevenlabsEl = document.getElementById("cfg-voice-consent-elevenlabs");
const cfgVoiceSaveEl = document.getElementById("cfg-voice-save");
const cfgVoiceStatusEl = document.getElementById("cfg-voice-status");
const cfgVoiceLocalStateEl = document.getElementById("cfg-voice-local-state");
const cfgVoiceLocalLogEl = document.getElementById("cfg-voice-local-log");
const cfgVoiceLocalInstallEl = document.getElementById("cfg-voice-local-install");
const cfgVoiceSampleEl = document.getElementById("cfg-voice-sample");
const cfgVoiceSampleStatusEl = document.getElementById("cfg-voice-sample-status");

const VOICE_CONSENT_ELS = { fish: cfgVoiceConsentFishEl, elevenlabs: cfgVoiceConsentElevenlabsEl };
const VOICE_PICKERS = [
  { engine: "fish", selectEl: cfgVoiceFishPickEl, inputEl: cfgVoiceFishIdEl },
  { engine: "elevenlabs", selectEl: cfgVoiceElevenlabsPickEl, inputEl: cfgVoiceElevenlabsIdEl },
];
const VOICE_LOCAL_BUSY = ["checking", "installing", "downloading"];
const VOICE_FREE_MODELS = ["s2.1-pro-free"];

let _voiceChain = [];
let _voiceItems = new Map();
let _voiceEngines = [];
let _voiceLocalSample = null;
let _voiceLocalPollTimer = null;

const voiceLinkKey = (link) => `${link.engine}:${link.model}`;
const voiceEngineInfo = (name) => _voiceEngines.find((engine) => engine.name === name);

function setVoiceStatus(text, kind = "") {
  cfgVoiceStatusEl.textContent = text;
  cfgVoiceStatusEl.className = kind ? `settings-status ${kind}` : "settings-status";
}

async function loadVoiceSettings() {
  for (const picker of VOICE_PICKERS) delete picker.selectEl.dataset.loaded;
  try {
    const resp = await apiFetchOk(`${API_BASE}/api/voice/config`);
    applyVoicePayload(await resp.json());
    setVoiceStatus("");
  } catch (err) {
    setVoiceStatus(`No se pudo cargar la configuración de voz. ${friendlyErrorMessage(err)}`, "error");
  }
}

function applyVoicePayload(data) {
  _voiceEngines = data.engines || [];
  _voiceChain = (data.chain || []).map((link) => ({ ...link }));
  _voiceItems = new Map((data.items || []).map((item) => [voiceLinkKey(item), item]));
  cfgVoiceFishIdEl.value = data.fish_voice_id || "";
  cfgVoiceElevenlabsIdEl.value = data.elevenlabs_voice_id || "";
  cfgVoiceEffectEl.checked = Boolean(data.effect);
  cfgVoicePauseEl.value = String(data.pause_minutes ?? 30);
  for (const [engine, el] of Object.entries(VOICE_CONSENT_ELS)) el.checked = Boolean(data.consent?.[engine]);
  renderVoiceChain();
  renderVoiceEngineOptions();
  applyVoiceLocalStatus(data.local);
}

// Bug de uso real (2026-09-27): el usuario cargo el ID de su voz de Fish
// esperando que fuera la principal, pero Fish habia quedado ultimo en la
// lista y hablaba ElevenLabs. Cada fila muestra ahora QUE voz usa y la
// primera dice que es la principal.
function voiceIdFor(engineName) {
  const id = engineName === "fish" ? cfgVoiceFishIdEl.value : engineName === "elevenlabs" ? cfgVoiceElevenlabsIdEl.value : "";
  return id.trim();
}

function voiceLinkName(link) {
  if (link.engine === "local") return "Tu voz clonada (en esta PC)";
  const engine = voiceEngineInfo(link.engine);
  const free = VOICE_FREE_MODELS.includes(link.model) ? " (gratis)" : "";
  const voiceId = voiceIdFor(link.engine);
  const voice = voiceId ? ` · voz ${voiceId.length > 10 ? `${voiceId.slice(0, 8)}…` : voiceId}` : "";
  return `${engine ? engine.label : link.engine} · ${link.model}${free}${voice}`;
}

function describeVoiceLink(link, idx) {
  const item = _voiceItems.get(voiceLinkKey(link));
  let state = "lista";
  if (!item) state = "sin guardar todavía";
  else if (item.missing) state = item.missing;
  else if (item.paused_until) state = `en pausa hasta ${item.paused_until}`;
  return idx === 0 ? `principal · ${state}` : `respaldo · ${state}`;
}

function voiceLinkState(link) {
  const item = _voiceItems.get(voiceLinkKey(link));
  if (!item) return "new";
  if (item.missing) return "missing";
  return item.paused_until ? "paused" : "ok";
}

function makeVoiceListButton(label, title, disabled, onClick) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "msg-copy-btn";
  btn.textContent = label;
  btn.title = title;
  btn.disabled = disabled;
  btn.onclick = onClick;
  return btn;
}

function renderVoiceChain() {
  cfgVoiceChainEl.replaceChildren();
  _voiceChain.forEach((link, idx) => {
    const li = document.createElement("li");
    li.className = "fallback-item";
    li.dataset.state = voiceLinkState(link);
    const name = document.createElement("span");
    name.className = "fallback-name";
    name.textContent = voiceLinkName(link);
    const info = document.createElement("span");
    info.className = "fallback-info";
    info.textContent = describeVoiceLink(link, idx);
    const actions = document.createElement("span");
    actions.className = "fallback-actions";
    const testBtn = makeVoiceListButton("▶", "Probar esta voz", false, () => testVoiceLink(link, testBtn));
    actions.append(
      testBtn,
      makeVoiceListButton("↑", "Subir", idx === 0, () => moveVoiceLink(idx, -1)),
      makeVoiceListButton("↓", "Bajar", idx === _voiceChain.length - 1, () => moveVoiceLink(idx, 1)),
      makeVoiceListButton("✕", "Quitar de la lista", false, () => {
        _voiceChain.splice(idx, 1);
        renderVoiceChain();
        setVoiceStatus("Sin guardar: tocá «Guardar voz».");
      }),
    );
    li.append(name, info, actions);
    cfgVoiceChainEl.appendChild(li);
  });
  // REQ-003: la voz del navegador es siempre el ultimo eslabon (no se quita).
  const last = document.createElement("li");
  last.className = "fallback-item";
  last.dataset.state = "ok";
  const lastName = document.createElement("span");
  lastName.className = "fallback-name";
  lastName.textContent = "Voz del navegador";
  const lastInfo = document.createElement("span");
  lastInfo.className = "fallback-info";
  lastInfo.textContent = _voiceChain.length
    ? "siempre al final: habla si ninguna de arriba puede"
    : "es la voz que se usa ahora";
  last.append(lastName, lastInfo);
  cfgVoiceChainEl.appendChild(last);
}

function moveVoiceLink(idx, delta) {
  const target = idx + delta;
  if (target < 0 || target >= _voiceChain.length) return;
  [_voiceChain[idx], _voiceChain[target]] = [_voiceChain[target], _voiceChain[idx]];
  renderVoiceChain();
  setVoiceStatus("Sin guardar: tocá «Guardar voz».");
}

async function testVoiceLink(link, btn) {
  if (!_voiceItems.has(voiceLinkKey(link))) {
    setVoiceStatus("Guardá la voz antes de probarla.", "error");
    return;
  }
  btn.disabled = true;
  setVoiceStatus(`Probando ${voiceLinkName(link)}…`);
  const result = await playVoiceSample(link, TEST_PHRASE);
  btn.disabled = false;
  setVoiceStatus(result.ok ? "" : result.message, result.ok ? "" : "error");
}

function renderVoiceEngineOptions() {
  const current = cfgVoiceAddEngineEl.value;
  cfgVoiceAddEngineEl.replaceChildren();
  for (const engine of _voiceEngines) {
    const opt = document.createElement("option");
    opt.value = engine.name;
    opt.textContent = engine.name === "local" ? "Clonación local (tu voz)" : engine.label;
    cfgVoiceAddEngineEl.appendChild(opt);
  }
  if (current && voiceEngineInfo(current)) cfgVoiceAddEngineEl.value = current;
  renderVoiceModelOptions();
}

function renderVoiceModelOptions() {
  const engine = voiceEngineInfo(cfgVoiceAddEngineEl.value);
  cfgVoiceAddModelEl.replaceChildren();
  for (const model of engine?.models || []) {
    const opt = document.createElement("option");
    opt.value = model;
    opt.textContent = VOICE_FREE_MODELS.includes(model) ? `${model} (gratis)` : model;
    cfgVoiceAddModelEl.appendChild(opt);
  }
  cfgVoiceAddModelEl.classList.toggle("hidden", (engine?.models || []).length < 2);
}

cfgVoiceAddEngineEl.addEventListener("change", renderVoiceModelOptions);

cfgVoiceAddEl.addEventListener("click", async () => {
  const engine = voiceEngineInfo(cfgVoiceAddEngineEl.value);
  if (!engine) return;
  const link = { engine: engine.name, model: cfgVoiceAddModelEl.value || engine.models[0] };
  if (_voiceChain.some((l) => voiceLinkKey(l) === voiceLinkKey(link))) {
    setVoiceStatus("Esa voz ya está en la lista.");
    return;
  }
  // REQ-002: el texto no sale a un servicio en la nube sin confirmarlo.
  if (engine.cloud && !VOICE_CONSENT_ELS[engine.name].checked) {
    const accepted = await showConfirmModal({
      title: `Usar ${engine.label}`,
      message:
        `Para hablar con ${engine.label}, Vision le envía el texto que lee en voz alta: las respuestas, ` +
        "la narración de progreso, el Modo Cerebro y el informe técnico (comandos, nombres de archivos y " +
        `de tablas; las claves nunca). ${engine.label} es un servicio externo; no lo actives si ese texto ` +
        "puede incluir datos que no deben salir de tu empresa. ¿Confirmás?",
      acceptLabel: "Sí, enviar el texto",
    });
    if (!accepted) return;
    VOICE_CONSENT_ELS[engine.name].checked = true;
  }
  // REQ-004: la clonacion local queda exenta del limite de 2 s, pero se avisa.
  if (engine.name === "local") {
    await showConfirmModal({
      title: "Clonación local",
      message:
        "En una PC sin placa de video NVIDIA, tu voz clonada tarda varios segundos por frase: Vision va a " +
        "tardar en empezar a hablar. Si querés respuestas rápidas, ponela después de una voz en la nube.",
      acceptLabel: "Entendido",
      hideCancel: true,
    });
  }
  _voiceChain.push(link);
  renderVoiceChain();
  setVoiceStatus("Sin guardar: tocá «Guardar voz».");
});

// Retirar el consentimiento saca ese servicio de la lista (el backend hace lo mismo).
for (const [engineName, el] of Object.entries(VOICE_CONSENT_ELS)) {
  el.addEventListener("change", () => {
    if (!el.checked) {
      _voiceChain = _voiceChain.filter((link) => link.engine !== engineName);
      renderVoiceChain();
    }
    setVoiceStatus("Sin guardar: tocá «Guardar voz».");
  });
}

cfgVoiceSaveEl.addEventListener("click", async () => {
  setVoiceStatus("Guardando…");
  try {
    const resp = await apiFetchOk(`${API_BASE}/api/voice/config`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        chain: _voiceChain,
        fish_voice_id: cfgVoiceFishIdEl.value.trim(),
        elevenlabs_voice_id: cfgVoiceElevenlabsIdEl.value.trim(),
        effect: cfgVoiceEffectEl.checked,
        pause_minutes: Number(cfgVoicePauseEl.value),
        consent: { fish: cfgVoiceConsentFishEl.checked, elevenlabs: cfgVoiceConsentElevenlabsEl.checked },
      }),
    });
    const data = await resp.json();
    applyVoicePayload(data);
    applyVoiceConfig(data); // tts.js: la proxima frase ya usa la cadena nueva
    setVoiceStatus("Guardado ✓", "ok");
  } catch (err) {
    setVoiceStatus(friendlyErrorMessage(err), "error");
  }
});

// REQ-006: las voces de la cuenta se piden al abrir el desplegable (no antes:
// sin clave cargada fallaria al abrir Configuracion).
async function loadVoicePicker({ engine, selectEl }) {
  if (selectEl.dataset.loaded === "true") return;
  selectEl.dataset.loaded = "true";
  selectEl.replaceChildren(new Option("Cargando voces…", ""));
  try {
    const resp = await apiFetchOk(`${API_BASE}/api/voice/voices/${engine}`);
    const { voices = [] } = await resp.json();
    selectEl.replaceChildren(
      new Option(voices.length ? "Elegir de mi cuenta…" : "Tu cuenta no tiene voces propias", ""),
      ...voices.map((voice) => new Option(voice.name || voice.id, voice.id)),
    );
  } catch (err) {
    delete selectEl.dataset.loaded;
    selectEl.replaceChildren(new Option("Elegir de mi cuenta…", ""));
    setVoiceStatus(friendlyErrorMessage(err), "error");
  }
}

for (const picker of VOICE_PICKERS) {
  picker.selectEl.addEventListener("focus", () => loadVoicePicker(picker));
  picker.selectEl.addEventListener("change", () => {
    if (!picker.selectEl.value) return;
    picker.inputEl.value = picker.selectEl.value;
    renderVoiceChain();
    setVoiceStatus("Sin guardar: tocá «Guardar voz».");
  });
  picker.inputEl.addEventListener("input", () => {
    renderVoiceChain();
    setVoiceStatus("Sin guardar: tocá «Guardar voz».");
  });
}

// REQ-007: estado de la clonacion local (instalacion y muestra).
function applyVoiceLocalStatus(local) {
  if (!local) return;
  if (local.sample) _voiceLocalSample = local.sample;
  const busy = VOICE_LOCAL_BUSY.includes(local.status);
  let stateText = "No instalada.";
  if (busy || local.status === "error" || local.status === "done") stateText = local.message;
  else if (local.installed) stateText = "Instalada en esta PC.";
  cfgVoiceLocalStateEl.textContent = stateText;
  cfgVoiceLocalInstallEl.disabled = busy || local.installed;
  cfgVoiceLocalInstallEl.textContent = local.installed
    ? "Clonación local instalada"
    : busy
      ? "Instalando…"
      : "Instalar clonación local";
  const showLog = (busy || local.status === "error") && (local.log || []).length > 0;
  cfgVoiceLocalLogEl.classList.toggle("hidden", !showLog);
  cfgVoiceLocalLogEl.textContent = showLog ? local.log.slice(-8).join("\n") : "";
  const sample = _voiceLocalSample;
  cfgVoiceSampleStatusEl.textContent = sample?.present
    ? `Muestra cargada: ${sample.seconds ?? "?"} s (${sample.format}). Subí otra para reemplazarla.`
    : "Todavía no subiste tu muestra de voz.";
  clearTimeout(_voiceLocalPollTimer);
  if (busy) _voiceLocalPollTimer = setTimeout(pollVoiceLocalStatus, 2000);
}

async function pollVoiceLocalStatus() {
  if (!isAudioSettingsPaneOpen() || settingsOverlayEl.classList.contains("hidden")) return;
  try {
    const resp = await apiFetchOk(`${API_BASE}/api/voice/local/status`);
    applyVoiceLocalStatus(await resp.json());
  } catch {
    _voiceLocalPollTimer = setTimeout(pollVoiceLocalStatus, 5000);
  }
}

cfgVoiceLocalInstallEl.addEventListener("click", async () => {
  const accepted = await showConfirmModal({
    title: "Instalar la clonación local",
    message:
      "Se descargan varios GB (PyTorch y el modelo de voz) y puede tardar bastante. Antes reviso que la " +
      "instalación no cambie nada de lo que Vision ya usa: si cambiaría algo, no instalo nada y te explico " +
      "por qué. ¿Seguimos?",
    acceptLabel: "Instalar",
  });
  if (!accepted) return;
  try {
    const resp = await apiFetchOk(`${API_BASE}/api/voice/local/install`, { method: "POST" });
    applyVoiceLocalStatus(await resp.json());
  } catch (err) {
    cfgVoiceLocalStateEl.textContent = friendlyErrorMessage(err);
  }
});

cfgVoiceSampleEl.addEventListener("change", () => {
  const file = cfgVoiceSampleEl.files?.[0];
  if (!file) return;
  if (file.size > 25 * 1024 * 1024) {
    cfgVoiceSampleStatusEl.textContent = "La muestra pesa más de 25 MB. Con 1 a 2 minutos de voz alcanza.";
    cfgVoiceSampleEl.value = "";
    return;
  }
  cfgVoiceSampleStatusEl.textContent = "Subiendo la muestra…";
  const reader = new FileReader();
  reader.onload = async () => {
    try {
      const resp = await apiFetchOk(`${API_BASE}/api/voice/local/sample`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ audio: reader.result, filename: file.name }),
      });
      _voiceLocalSample = await resp.json();
      cfgVoiceSampleStatusEl.textContent = `Muestra guardada: ${_voiceLocalSample.seconds ?? "?"} s. Ya podés sumar tu voz clonada a la lista.`;
    } catch (err) {
      cfgVoiceSampleStatusEl.textContent = friendlyErrorMessage(err);
    } finally {
      cfgVoiceSampleEl.value = "";
    }
  };
  reader.onerror = () => {
    cfgVoiceSampleStatusEl.textContent = "No pude leer el archivo. Probá de nuevo.";
  };
  reader.readAsDataURL(file);
});

// -------------------------------------------------------------------------
// spec agentes-programados, UNIFICADA (pedido explicito del usuario 2026-09:
// "fusion en una sola pestaña" de la vieja Tiempo/Agenda + Agentes; la
// pestaña Pomodoro que en su momento se separo aparte fue ELIMINADA despues,
// ver REQ-101 de spec configuracion-limpieza-credenciales-servidor).
// Dos motores de disparo conviven detras de una UI unica:
//   - Alarmas/Recordatorios ("schedules", /api/alarms): motor FRONTEND
//     (timers del navegador, REQ-005 original) -- necesitan una pantalla
//     mirando para sonar/mostrar el toast. UN solo intervalo de 1s que
//     recorre los programados contra Date.now() (no un setTimeout por
//     item: evita cientos de timers y tolera el throttling en pestaña de
//     fondo).
//   - Comando(s)/Skill/Archivo ("agents", /api/agents): motor BACKEND (ver
//     app/scheduler/agents.py) -- disparan aunque no haya navegador
//     abierto, con tal de que el proceso backend siga vivo.
// -------------------------------------------------------------------------
const alarmToastEl = document.getElementById("alarm-toast");
const alarmToastTextEl = document.getElementById("alarm-toast-text");

// -- pestaña "Agentes" (lista unificada + formulario con selector de tipo) --
const agentsListEl = document.getElementById("agents-list");
const agentLabelEl = document.getElementById("agent-label");
const agentTypeEl = document.getElementById("agent-type");
const agentSessionFieldEl = document.getElementById("agent-session-field");
const agentSessionEl = document.getElementById("agent-session");
const agentCommandFieldsEl = document.getElementById("agent-command-fields");
const agentCommandPickerEl = document.getElementById("agent-command-picker");
const agentCommandAddEl = document.getElementById("agent-command-add");
const agentCommandSequenceEl = document.getElementById("agent-command-sequence");
const agentSkillFieldsEl = document.getElementById("agent-skill-fields");
const agentSkillPickerEl = document.getElementById("agent-skill-picker");
const agentFileFieldsEl = document.getElementById("agent-file-fields");
const agentFileEl = document.getElementById("agent-file");
const agentFileBrowseEl = document.getElementById("agent-file-browse");
const agentAlarmFieldsEl = document.getElementById("agent-alarm-fields");
const agentSoundEl = document.getElementById("agent-sound");
const agentAlarmFileEl = document.getElementById("agent-alarm-file");
const agentAlarmFileBrowseEl = document.getElementById("agent-alarm-file-browse");
const agentKindEl = document.getElementById("agent-kind");
const agentOnceFieldsEl = document.getElementById("agent-once-fields");
const agentRecurringFieldsEl = document.getElementById("agent-recurring-fields");
const agentDatetimeEl = document.getElementById("agent-datetime");
const agentRuleEl = document.getElementById("agent-rule");
const agentWeekdayFieldEl = document.getElementById("agent-weekday-field");
const agentWeekdayEl = document.getElementById("agent-weekday");
const agentNFieldEl = document.getElementById("agent-n-field");
const agentNEl = document.getElementById("agent-n");
const agentTimeEl = document.getElementById("agent-time");
const agentSaveEl = document.getElementById("agent-save");
const agentCancelEditEl = document.getElementById("agent-cancel-edit");
const agentStatusEl = document.getElementById("agent-status");
const agentSnoozeMinutesEl = document.getElementById("agent-snooze-minutes");
const agentSnoozeStatusEl = document.getElementById("agent-snooze-status");
const agentFallbackSessionEl = document.getElementById("agent-fallback-session");
const agentFallbackSessionStatusEl = document.getElementById("agent-fallback-session-status");
const agentRunsListEl = document.getElementById("agent-runs-list");
const agentRunsClearEl = document.getElementById("agent-runs-clear");
const agentRunsPrevEl = document.getElementById("agent-runs-prev");
const agentRunsNextEl = document.getElementById("agent-runs-next");
const agentRunsPageEl = document.getElementById("agent-runs-page");

// -- sidebar: "Agentes" de la sesion activa (pedido explicito del usuario) --
const sessionAgentsListEl = document.getElementById("session-agents-list");
const sessionAgentsSummaryEl = document.getElementById("session-agents-summary");
const sessionAgentsConfigBtnEl = document.getElementById("session-agents-config-btn");
const sessionAlarmsListEl = document.getElementById("session-alarms-list");
const sessionAlarmsSummaryEl = document.getElementById("session-alarms-summary");
const sessionAlarmsConfigBtnEl = document.getElementById("session-alarms-config-btn");

const AGENT_RUNS_PAGE_SIZE = 10;

let _schedules = []; // caché en memoria de las alarmas/recordatorios (globales)
let _agents = []; // caché en memoria de los agentes (comando/skill/archivo, globales)
let _agentSessionsCache = []; // [{id, title, project_path, ...}] de GET /api/sessions/all
let _editingId = null; // id en edicion (de un agente O una alarma, segun _editingKind)
let _editingKind = null; // 'agent' | 'schedule' | null (creando nuevo)
let _commandSequence = []; // nombres de comando, en el orden elegido (solo tipo "command")
let _alarmToastTimer = null;
let _agentRunsOffset = 0;
let _agentRunsTotal = 0;

// -- sonido por evento (REQ-004) -------------------------------------------
// Mismo motor Web Audio que la alarma del loop, pero con el tono ELEGIDO por
// el evento (no el global del panel de Audio). Comparte scheduleAlarmTone().
function playAlarmSound(kind) {
  playAlarmWav(kind);
}

// -- toast de aviso (REQ-004) ----------------------------------------------
function showAlarmToast(text) {
  alarmToastTextEl.textContent = text;
  alarmToastEl.classList.remove("hidden");
  if (_alarmToastTimer) clearTimeout(_alarmToastTimer);
  _alarmToastTimer = setTimeout(() => alarmToastEl.classList.add("hidden"), 7000);
}

// -- descripcion humana de un programado (REQ-001/003) ----------------------
const WEEKDAY_NAMES = ["domingo", "lunes", "martes", "miércoles", "jueves", "viernes", "sábado"];

function describeSchedule(s) {
  const t = s.trigger || {};
  if (s.kind === "once") {
    const ms = Date.parse(t.target_iso || "");
    return "Una vez · " + (Number.isFinite(ms)
      ? new Date(ms).toLocaleString()
      : (t.target_iso || "—"));
  }
  // recurring
  const hm = t.time || "08:30";
  const rule = t.rule;
  if (rule === "daily") return `Diario a las ${hm}`;
  if (rule === "weekdays") return `Días hábiles a las ${hm}`;
  if (rule === "weekly") return `Todos los ${WEEKDAY_NAMES[Number(t.weekday) || 1]} a las ${hm}`;
  if (rule === "ndays") return `Cada ${t.n || 1} día(s) a las ${hm}`;
  if (rule === "nhours") return `Cada ${t.n || 1} hora(s)`;
  return `Recurrente (${JSON.stringify(t)})`;
}

// -- carga (solo datos -- el render de la lista unificada esta mas abajo) --
async function loadSchedules() {
  try {
    const resp = await apiFetch(`${API_BASE}/api/alarms`);
    const data = await resp.json();
    _schedules = Array.isArray(data.schedules) ? data.schedules : [];
  } catch (err) {
    console.warn("No se pudieron cargar las alarmas:", err);
  }
}

// REQ-101 (spec configuracion-limpieza-credenciales-servidor, 2026-09-19):
// motor de Pomodoro ELIMINADO -- era 100% frontend/localStorage sin ningun
// backend real detras (scheduler/alarms.py ya lo ignoraba explicitamente),
// sin valor diferencial que justificara mantenerlo. Si hace falta a futuro,
// ver el historial de git de esta seccion para recuperar la implementacion.
// REQ-001/005 (programador-alarmas, persistencia GLOBAL): cargar los
// programados al arrancar para que la lista de Configuracion los muestre.
// El disparo (sonido/toast) ya NO pasa por aca (ver app/scheduler/alarms.py
// en el backend) -- esto solo alimenta la lista visual.
loadSchedules();

// -------------------------------------------------------------------------
// pestaña "Agentes" unificada: tipo de automatización + formulario +
// lista unica (agentes + alarmas) + historial paginado. El reloj de
// disparo de comando/skill/archivo/alarma vive TODO en el BACKEND (ver
// app/scheduler/agents.py y app/scheduler/alarms.py) -- aca solo queda el
// CRUD (crear/editar/pausar/borrar) sobre /api/agents y /api/alarms.
// -------------------------------------------------------------------------
function updateAgentTypeVisibility() {
  const type = agentTypeEl.value;
  agentSessionFieldEl.classList.toggle("hidden", type === "alarm");
  agentCommandFieldsEl.classList.toggle("hidden", type !== "command");
  agentSkillFieldsEl.classList.toggle("hidden", type !== "skill");
  agentFileFieldsEl.classList.toggle("hidden", type !== "file");
  agentAlarmFieldsEl.classList.toggle("hidden", type !== "alarm");
}
agentTypeEl.addEventListener("change", () => {
  updateAgentTypeVisibility();
  if (agentTypeEl.value !== "alarm") refreshAgentTypeOptions();
});

function updateAgentKindVisibility() {
  const kind = agentKindEl.value;
  agentOnceFieldsEl.classList.toggle("hidden", kind !== "once");
  agentRecurringFieldsEl.classList.toggle("hidden", kind !== "recurring");
}
function updateAgentRuleVisibility() {
  const rule = agentRuleEl.value;
  agentWeekdayFieldEl.classList.toggle("hidden", rule !== "weekly");
  agentNFieldEl.classList.toggle("hidden", rule !== "ndays" && rule !== "nhours");
}
agentKindEl.addEventListener("change", updateAgentKindVisibility);
agentRuleEl.addEventListener("change", updateAgentRuleVisibility);

// -- vista previa EN EL MISMO FORMATO que el widget nativo -----------------
// Pedido del usuario 2026-09-17: reemplazar los <input type="datetime-local">
// / type="time"> NATIVOS por flatpickr (vendorizado, MIT gratis -- se
// descarto dhtmlx por licencia paga). Los nativos mostraban mm/dd/yyyy o
// dd/mm/yyyy segun la Configuracion Regional de Windows del usuario (2
// vueltas de parches antes de esto: primero se asumio que dependia del
// idioma del navegador, despues se corrigio a "sigue el regional del SO" --
// ver historial en git). Con flatpickr el formato queda FIJO en el propio
// codigo (dateFormat/altFormat abajo), sin depender de ninguna
// configuracion externa.
//
// `#agent-datetime` usa altInput: el input ORIGINAL (oculto) guarda el
// valor en el mismo formato ISO "YYYY-MM-DDTHH:mm" que ya esperaba el
// resto del codigo (agentSaveEl -> trigger.target_iso, ver mas abajo) --
// el input VISIBLE (altInput) es el que el usuario ve/edita, en dd/mm/yyyy
// 24hs. Asi no hizo falta tocar como se lee/guarda el valor en ningun
// otro lado del archivo.
const _dtPicker = window.flatpickr(agentDatetimeEl, {
  enableTime: true,
  time_24hr: true,
  dateFormat: "Y-m-dTH:i",
  altInput: true,
  altFormat: "d/m/Y H:i",
  minuteIncrement: 5,
  locale: "es",
  disableMobile: true,
});
// `#agent-time` (modo Recurrente) es SOLO hora -- 24hs alcanza para sacar
// la ambiguedad AM/PM, no hace falta altInput (no hay orden de campos que
// confundir en un HH:MM suelto).
const _timePicker = window.flatpickr(agentTimeEl, {
  enableTime: true,
  noCalendar: true,
  time_24hr: true,
  dateFormat: "H:i",
  minuteIncrement: 5,
  locale: "es",
  disableMobile: true,
});
function _setDatetimeValue(iso) {
  if (iso) _dtPicker.setDate(iso, true);
  else _dtPicker.clear();
}
function _setTimeValue(hm) {
  _timePicker.setDate(hm || "08:30", true);
}

function _setAgentStatus(text, isError) {
  agentStatusEl.textContent = text;
  agentStatusEl.className = "settings-hint" + (isError ? " error" : "");
}

// -- resaltado del campo puntual con error (pedido del usuario 2026-09-16:
// antes solo se veia el texto del hint -- ver fix de .settings-hint.error
// en styles.css -- y ni siquiera ese tenia color, asi que una validacion
// fallida pasaba desapercibida). `AGENT_VALIDATABLE_FIELDS` son TODOS los
// elementos que _invalidateAgentField puede llegar a marcar -- se limpian
// todos al arrancar cada intento de guardar, para no dejar un campo viejo
// marcado en rojo por una validacion de un intento anterior que ya no
// aplica (ej. cambiaste de tipo "command" a "file" entre un intento y el
// siguiente).
const AGENT_VALIDATABLE_FIELDS = [
  agentLabelEl, agentDatetimeEl, agentSessionEl,
  agentCommandFieldsEl, agentSkillPickerEl, agentFileEl,
];
function _clearAgentFieldErrors() {
  for (const el of AGENT_VALIDATABLE_FIELDS) el.classList.remove("field-invalid");
}
function _invalidateAgentField(el, message) {
  el.classList.add("field-invalid");
  _setAgentStatus(message, true);
}
// Apenas el usuario toca el campo marcado, se le saca el rojo -- no hace
// falta esperar al proximo click en Guardar para que la señal desaparezca.
for (const el of AGENT_VALIDATABLE_FIELDS) {
  el.addEventListener("input", () => el.classList.remove("field-invalid"));
  el.addEventListener("change", () => el.classList.remove("field-invalid"));
}

// -- sesion destino + comandos/skills disponibles en ESA sesion -----------
// Separado en dos: refrescar el CACHE (nombres para los labels de la lista
// de agentes) vs reconstruir el <select> del formulario "Nuevo / editar".
// Bug real reportado por el usuario 2026-09-16: el poll periodico de 8s
// (refreshAgentsAndList, ver mas abajo) empezo a llamar a esto para que los
// nombres de sesion de la LISTA no quedaran viejos (ver fix anterior) --
// pero el <select> vive DENTRO del formulario de creacion/edicion, asi que
// reconstruirle el innerHTML cada 8s (aunque el usuario este ahi mismo
// completando el formulario) le reseteaba el foco/scroll de la pagina
// entera cada vez. El cache SI necesita refrescarse periodicamente; el
// <select> del formulario NO -- solo hace falta reconstruirlo al abrir la
// pestaña o al arrancar a crear/editar un agente puntual.
async function fetchAgentSessionsCache() {
  try {
    const resp = await apiFetch(`${API_BASE}/api/sessions/all`);
    const data = await resp.json();
    _agentSessionsCache = Array.isArray(data.sessions) ? data.sessions : [];
  } catch {
    _agentSessionsCache = [];
  }
}

function rebuildAgentSessionSelect() {
  const prev = agentSessionEl.value;
  agentSessionEl.innerHTML = "";
  if (!_agentSessionsCache.length) {
    agentSessionEl.innerHTML = '<option value="">(sin sesiones guardadas todavía)</option>';
    return;
  }
  for (const s of _agentSessionsCache) {
    const opt = document.createElement("option");
    opt.value = s.id;
    opt.textContent = s.title || s.id.slice(0, 8);
    agentSessionEl.appendChild(opt);
  }
  if (prev && _agentSessionsCache.some((s) => s.id === prev)) agentSessionEl.value = prev;
}

async function loadAgentSessionsSelect() {
  await fetchAgentSessionsCache();
  rebuildAgentSessionSelect();
}

// REQ-001 (spec agentes-huerfanos-y-gestion-skills): mismo <select> de
// sesiones que el formulario de agentes (_agentSessionsCache), pero para
// elegir la sesion de RESERVA global -- reconstruido cada vez que se abre
// la pestaña Agentes (mismo momento que rebuildAgentSessionSelect).
function rebuildAgentFallbackSessionSelect() {
  if (!agentFallbackSessionEl) return;
  const prev = agentFallbackSessionEl.value;
  agentFallbackSessionEl.innerHTML = '<option value="">(sin configurar)</option>';
  for (const s of _agentSessionsCache) {
    const opt = document.createElement("option");
    opt.value = s.id;
    opt.textContent = s.title || s.id.slice(0, 8);
    agentFallbackSessionEl.appendChild(opt);
  }
  if (prev && _agentSessionsCache.some((s) => s.id === prev)) agentFallbackSessionEl.value = prev;
}

async function loadAgentCommandPicker() {
  const sessionId = agentSessionEl.value;
  agentCommandPickerEl.innerHTML = "";
  if (!sessionId) {
    agentCommandPickerEl.innerHTML = '<option value="">(elegí primero una sesión)</option>';
    return;
  }
  try {
    const resp = await apiFetch(`${API_BASE}/api/commands?session_id=${encodeURIComponent(sessionId)}`);
    const data = await resp.json();
    const custom = Array.isArray(data.custom) ? data.custom : [];
    if (!custom.length) {
      agentCommandPickerEl.innerHTML = '<option value="">(esa sesión no tiene comandos propios todavía)</option>';
      return;
    }
    for (const c of custom) {
      const opt = document.createElement("option");
      opt.value = c.name;
      opt.textContent = `/${c.name}`;
      agentCommandPickerEl.appendChild(opt);
    }
  } catch {
    agentCommandPickerEl.innerHTML = '<option value="">(no se pudieron cargar los comandos)</option>';
  }
}

// Skills GLOBALES + LOCALES de la sesion elegida (mismo criterio que el
// panel de accesos rapidos, ver showQuickPanelSkillsView).
async function loadAgentSkillPicker() {
  const sessionId = agentSessionEl.value;
  const options = [];
  try {
    const resp = await apiFetch(`${API_BASE}/api/skills`);
    const data = await resp.json();
    for (const s of data.skills || []) options.push({ name: s.name, label: `${s.name} (global)` });
  } catch { /* noop */ }
  if (sessionId) {
    try {
      const resp = await apiFetch(`${API_BASE}/api/local-skills/${encodeURIComponent(sessionId)}`);
      const data = await resp.json();
      for (const s of data.skills || []) options.push({ name: s.name, label: `${s.name} (local)` });
    } catch { /* noop */ }
  }
  agentSkillPickerEl.innerHTML = "";
  if (!options.length) {
    agentSkillPickerEl.innerHTML = '<option value="">(sin skills disponibles)</option>';
    return;
  }
  for (const o of options) {
    const opt = document.createElement("option");
    opt.value = o.name;
    opt.textContent = o.label;
    agentSkillPickerEl.appendChild(opt);
  }
}

async function refreshAgentTypeOptions() {
  await Promise.all([loadAgentCommandPicker(), loadAgentSkillPicker()]);
}
agentSessionEl.addEventListener("change", refreshAgentTypeOptions);

// -- secuencia de comandos (tipo "command"): arma/reordena a mano, pedido
// explicito del usuario ("varios comandos que se ejecuten de manera
// secuencial en el orden que yo elija") ------------------------------------
function renderCommandSequence() {
  agentCommandSequenceEl.innerHTML = "";
  if (!_commandSequence.length) {
    agentCommandSequenceEl.innerHTML = '<li class="session-list-empty">Sin comandos agregados todavía</li>';
    return;
  }
  // Apenas hay al menos un comando en la secuencia, se saca el resaltado
  // rojo que _invalidateAgentField pudo haber puesto en este contenedor.
  agentCommandFieldsEl.classList.remove("field-invalid");
  _commandSequence.forEach((name, idx) => {
    const li = document.createElement("li");
    const nameSpan = document.createElement("span");
    nameSpan.className = "session-name";
    nameSpan.textContent = `${idx + 1}. /${name}`;
    const upBtn = document.createElement("button");
    upBtn.type = "button";
    upBtn.className = "session-rename-btn";
    upBtn.textContent = "↑";
    upBtn.title = "Subir";
    upBtn.disabled = idx === 0;
    upBtn.onclick = () => {
      [_commandSequence[idx - 1], _commandSequence[idx]] = [_commandSequence[idx], _commandSequence[idx - 1]];
      renderCommandSequence();
    };
    const downBtn = document.createElement("button");
    downBtn.type = "button";
    downBtn.className = "session-rename-btn";
    downBtn.textContent = "↓";
    downBtn.title = "Bajar";
    downBtn.disabled = idx === _commandSequence.length - 1;
    downBtn.onclick = () => {
      [_commandSequence[idx + 1], _commandSequence[idx]] = [_commandSequence[idx], _commandSequence[idx + 1]];
      renderCommandSequence();
    };
    const delBtn = document.createElement("button");
    delBtn.type = "button";
    delBtn.className = "session-delete-btn";
    delBtn.textContent = "🗑";
    delBtn.title = "Quitar";
    delBtn.onclick = () => {
      _commandSequence.splice(idx, 1);
      renderCommandSequence();
    };
    li.append(nameSpan, upBtn, downBtn, delBtn);
    agentCommandSequenceEl.appendChild(li);
  });
}
agentCommandAddEl.addEventListener("click", () => {
  const name = agentCommandPickerEl.value;
  if (!name) return;
  _commandSequence.push(name);
  renderCommandSequence();
});

// Explorador de Windows para los campos de archivo (tipo "file" y el
// archivo opcional de "alarm") ---------------------------------------------
if (agentFileBrowseEl) {
  agentFileBrowseEl.addEventListener("click", () => {
    openNativePicker(agentFileEl.value, "file", (p) => { agentFileEl.value = p; });
  });
}
if (agentAlarmFileBrowseEl) {
  agentAlarmFileBrowseEl.addEventListener("click", () => {
    openNativePicker(agentAlarmFileEl.value, "file", (p) => { agentAlarmFileEl.value = p; });
  });
}

// -- descripcion corta de que automatiza un agente -------------------------
function describeAgentType(a) {
  if (a.automation_type === "skill") return `Skill: ${a.skill_name || "?"}`;
  if (a.automation_type === "file") return `Archivo: ${a.file_path || "?"}`;
  const cmds = (a.commands || []).map((c) => `/${c}`).join(" → ");
  return cmds || "(sin comandos)";
}

// -- render de la lista UNIFICADA (agentes + alarmas, reusa describeSchedule:
// mismo vocabulario de horarios, kind/trigger tienen la misma forma) -------
// Pedido explicito del usuario: separar agentes/alarmas por estado, MISMOS
// 4 colores que ya usa el TodoWrite (ver .todo-status en styles.css --
// pending=gris, in_progress=ambar, completed=verde, cancelled=rojo) para
// que el vocabulario visual sea el mismo en toda la app. "Pausado" reusa el
// rojo de 'cancelled': un agente deshabilitado esta tan inactivo como una
// tarea cancelada, y no hay un 5to color propio definido para no inventar
// una paleta nueva.
const AGENT_STATUS_META = {
  in_progress: { label: "Ejecutando ahora", cls: "in_progress" },
  pending: { label: "Pendiente", cls: "pending" },
  paused: { label: "Pausado", cls: "cancelled" },
  completed: { label: "Completado", cls: "completed" },
};
const AGENT_STATUS_ORDER = ["in_progress", "pending", "paused", "completed"];

function computeItemStatus(kind, data, busyMap) {
  if (kind === "agent" && busyMap[data.session_id]) return "in_progress";
  if (data.enabled === false) return "paused";
  // "once" ya disparado: no le queda ningun horario futuro, es un
  // one-shot terminado (ver el mismo criterio que usa el reloj de
  // agentes en backend, app/scheduler/agents.py:_check_and_fire_due_agents).
  if (data.kind === "once" && data.last_fired_at) return "completed";
  return "pending";
}

async function renderUnifiedAgentsList() {
  // Bug real reportado por el usuario 2026-09-16 ("la barra lateral sube
  // sola"/"el formulario te lleva hasta arriba"): esta funcion vaciaba
  // #agents-list ACA, ANTES del `await` de mas abajo (que espera una
  // respuesta de red por cada sesion) -- durante esa espera la lista
  // quedaba VACIA (altura 0), el contenedor scrolleable
  // (.settings-body) se achicaba y el navegador clampeaba el scroll a 0
  // para no dejarlo "flotando" fuera de rango. Al repoblarse la lista el
  // scroll NO vuelve solo a donde estaba el usuario -- confirmado
  // instrumentando scrollTop cada 500ms: se mantiene estable y salta a 0
  // justo cuando corre este refresh (cada 8s via startAgentsPolling). El
  // fix es simple: no tocar el DOM hasta tener TODO listo para pintar de
  // una sola vez (clear + repoblar sincronico, sin ningun `await` en el
  // medio que deje el contenedor vacio).
  // REQ-101 (spec configuracion-limpieza-credenciales-servidor): el filtro
  // `kind !== "pomodoro"` se mantiene como defensivo aunque la UI ya no
  // genera ese kind (motor de Pomodoro eliminado) -- la columna `kind` de
  // `schedules` en el backend sigue aceptando ese valor por compatibilidad
  // con filas viejas ya persistidas (ver master_index.py), asi que filtrar
  // evita que una fila legado reaparezca en esta lista.
  const items = [
    ..._agents.map((a) => ({ kind: "agent", data: a })),
    ..._schedules.filter((s) => s.kind !== "pomodoro").map((s) => ({ kind: "schedule", data: s })),
  ];
  if (!items.length) {
    agentsListEl.innerHTML = '<div class="schedule-empty">Sin agentes ni alarmas. Creá uno abajo.</div>';
    return;
  }

  // "ejecutando ahora": una consulta de estado por SESION distinta (no por
  // agente -- varios agentes pueden compartir sesion destino). Esto corre
  // ANTES de tocar el DOM -- ver comentario de arriba.
  const sessionIds = [...new Set(_agents.map((a) => a.session_id))];
  const busyMap = {};
  await Promise.all(sessionIds.map(async (sid) => {
    try {
      const resp = await apiFetch(`${API_BASE}/api/session/${encodeURIComponent(sid)}/status`);
      const data = await resp.json();
      busyMap[sid] = !!data.busy;
    } catch { busyMap[sid] = false; }
  }));

  agentsListEl.innerHTML = "";

  // Agrupa por estado ANTES de renderizar (en vez de una lista plana) --
  // orden fijo: lo que esta corriendo primero, despues lo que espera su
  // turno, despues lo pausado, y al final lo ya terminado (menos
  // relevante para accionar).
  const groups = { in_progress: [], pending: [], paused: [], completed: [] };
  for (const it of items) {
    groups[computeItemStatus(it.kind, it.data, busyMap)].push(it);
  }

  for (const statusKey of AGENT_STATUS_ORDER) {
    const groupItems = groups[statusKey];
    if (!groupItems.length) continue;
    const meta = AGENT_STATUS_META[statusKey];
    const header = document.createElement("div");
    header.className = `agent-group-header ${meta.cls}`;
    header.textContent = `${meta.label} (${groupItems.length})`;
    agentsListEl.appendChild(header);
    groupItems.forEach(({ kind, data }) => renderAgentItem(kind, data, statusKey, busyMap));
  }
}

function renderAgentItem(kind, data, statusKey, busyMap) {
    const item = document.createElement("div");
    item.className = "schedule-item";
    // REQ-005 (spec agentes-huerfanos-y-gestion-skills): `data.broken` viene
    // del backend (GET /api/agents, ver compute_agent_broken_status) --
    // `data-broken` dispara el mismo patron visual border-left rojo ya
    // usado para agentes pausados (#session-agents-list li[data-status=
    // "paused"], styles.css) via un selector hermano nuevo en #agents-list.
    if (kind === "agent" && data.broken) item.dataset.broken = data.broken.field;
    const main = document.createElement("div");
    main.className = "schedule-main";
    const label = document.createElement("div");
    label.className = "schedule-label";
    const statusPill = document.createElement("span");
    statusPill.className = `todo-status ${AGENT_STATUS_META[statusKey].cls}`;
    statusPill.textContent = AGENT_STATUS_META[statusKey].label;
    label.appendChild(statusPill);
    label.appendChild(document.createTextNode(data.label || "(sin etiqueta)"));
    const desc = document.createElement("div");
    desc.className = "schedule-desc";
    if (kind === "agent") {
      const sessionMeta = _agentSessionsCache.find((s) => s.id === data.session_id);
      const sessionLabel = sessionMeta ? (sessionMeta.title || data.session_id.slice(0, 8)) : data.session_id.slice(0, 8);
      desc.textContent = `${describeAgentType(data)} · ${describeSchedule(data)} · sesión: ${sessionLabel}`;
      if (data.broken) {
        // Texto corto y explicito -- el border-left rojo solo no basta si
        // el usuario no mira con atencion (mismo criterio que el resto de
        // la app: nunca dejar un estado roto sin una señal legible).
        const brokenLabels = {
          session_id: "⚠ sin sesión",
          commands: "⚠ comando inválido",
          skill_name: "⚠ skill inválida",
        };
        desc.textContent += " · " + (brokenLabels[data.broken.field] || "⚠ configuración inválida");
      }
    } else {
      desc.textContent = `Alarma · ${describeSchedule(data)}`;
      if (data.file_path && data.file_path.trim()) desc.textContent += " ⚙ ejecuta: " + data.file_path;
    }
    main.appendChild(label);
    main.appendChild(desc);

    const tag = document.createElement("span");
    tag.className = "schedule-sound" + (data.enabled === false ? " disabled" : "");
    tag.textContent = kind === "agent" ? data.automation_type : "alarma";

    const actions = document.createElement("div");
    actions.className = "schedule-actions";

    if (kind === "agent") {
      if (busyMap[data.session_id]) {
        // Pedido explicito del usuario: nunca dejar un agente "colgado"
        // sin forma de pararlo -- corta la secuencia ENTERA (no solo el
        // paso activo), ver docstring de stop_agent en scheduler/agents.py.
        const stopBtn = document.createElement("button");
        stopBtn.type = "button";
        stopBtn.className = "danger";
        stopBtn.textContent = "⏹";
        stopBtn.title = "Detener ejecución en curso";
        stopBtn.onclick = async () => {
          try {
            await apiFetchOk(`${API_BASE}/api/agents/${data.id}/stop`, { method: "POST" });
            await refreshAgentsAndList();
          } catch (err) {
            console.warn("No se pudo detener el agente:", err);
            _setAgentStatus(`No se pudo detener el agente. ${friendlyErrorMessage(err)}`, true);
          }
        };
        actions.appendChild(stopBtn);
      } else {
        const runBtn = document.createElement("button");
        runBtn.type = "button";
        runBtn.textContent = "▶️";
        runBtn.title = "Ejecutar ahora";
        runBtn.onclick = async () => {
          try {
            await apiFetchOk(`${API_BASE}/api/agents/${data.id}/run`, { method: "POST" });
            await refreshAgentsAndList();
          } catch (err) {
            console.warn("No se pudo ejecutar el agente:", err);
            _setAgentStatus(`No se pudo ejecutar el agente. ${friendlyErrorMessage(err)}`, true);
          }
        };
        actions.appendChild(runBtn);
      }
    }

    const editBtn = document.createElement("button");
    editBtn.type = "button";
    editBtn.textContent = "✎";
    editBtn.title = "Editar";
    editBtn.onclick = () => (kind === "agent" ? startEditAgent(data) : startEditSchedule(data));

    const toggleBtn = document.createElement("button");
    toggleBtn.type = "button";
    toggleBtn.textContent = data.enabled === false ? "▶" : "⏸";
    toggleBtn.title = data.enabled === false ? "Activar" : "Pausar";
    toggleBtn.onclick = async () => {
      try {
        const url = kind === "agent" ? `${API_BASE}/api/agents/${data.id}` : `${API_BASE}/api/alarms/${data.id}`;
        await apiFetchOk(url, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ enabled: data.enabled === false }) });
        await refreshAgentsAndList();
      } catch (err) {
        console.warn("No se pudo alternar:", err);
        _setAgentStatus(`No se pudo ${data.enabled === false ? "activar" : "pausar"}. ${friendlyErrorMessage(err)}`, true);
      }
    };

    const delBtn = document.createElement("button");
    delBtn.type = "button";
    delBtn.className = "danger";
    delBtn.textContent = "🗑";
    delBtn.title = "Eliminar";
    delBtn.onclick = async () => {
      try {
        const url = kind === "agent" ? `${API_BASE}/api/agents/${data.id}` : `${API_BASE}/api/alarms/${data.id}`;
        await apiFetchOk(url, { method: "DELETE" });
        await refreshAgentsAndList();
      } catch (err) {
        console.warn("No se pudo eliminar:", err);
        _setAgentStatus(`No se pudo eliminar. ${friendlyErrorMessage(err)}`, true);
      }
    };

    actions.appendChild(editBtn);
    actions.appendChild(toggleBtn);
    actions.appendChild(delBtn);
    item.appendChild(main);
    item.appendChild(tag);
    item.appendChild(actions);
    agentsListEl.appendChild(item);
}

async function loadAgents() {
  try {
    const resp = await apiFetch(`${API_BASE}/api/agents`);
    const data = await resp.json();
    _agents = Array.isArray(data.agents) ? data.agents : [];
  } catch (err) {
    console.warn("No se pudieron cargar los agentes:", err);
  }
}

// Refresca TODO lo que depende de _agents/_schedules: la lista unificada de
// la pestaña Configuracion Y el panel lateral de la sesion activa.
async function refreshAgentsAndList() {
  // fetchAgentSessionsCache() (NO loadAgentSessionsSelect/rebuildAgentSessionSelect)
  // entra aca tambien porque este refresh corre cada 8s mientras la pestaña
  // esta abierta (startAgentsPolling) -- si una sesion se renombra (a mano,
  // o por auto-titulo del LLM) despues de abrir la pestaña, sin esto el
  // "sesión: X" de renderUnifiedAgentsList queda con el nombre viejo para
  // siempre hasta cerrar y reabrir la pestaña.
  //
  // Bug real reportado por el usuario 2026-09-16: llamar aca a
  // loadAgentSessionsSelect() completo (cache + reconstruir el <select> del
  // formulario) le reseteaba el scroll/foco de la pagina cada 8s mientras
  // el usuario estaba completando el formulario de "Nuevo / editar" -- el
  // <select> vive ahi, no en la lista. Solo se refresca el CACHE (que es lo
  // unico que necesita la lista para mostrar nombres al dia); el <select>
  // del formulario se reconstruye aparte, solo cuando hace falta de verdad
  // (abrir la pestaña, o arrancar a crear/editar un agente puntual).
  await Promise.all([loadSchedules(), loadAgents(), fetchAgentSessionsCache()]);
  await renderUnifiedAgentsList();
  renderSessionAgentsSidebar();
  renderSessionAlarmsSidebar();
}

// -- formulario UNICO (agente O alarma, segun #agent-type) -----------------
function resetAgentForm() {
  _editingId = null;
  _editingKind = null;
  _commandSequence = [];
  agentLabelEl.value = "";
  agentTypeEl.value = "command";
  _setDatetimeValue("");
  agentRuleEl.value = "daily";
  agentWeekdayEl.value = "1";
  agentNEl.value = "1";
  _setTimeValue("08:30");
  agentKindEl.value = "once";
  agentSoundEl.value = "beep";
  agentAlarmFileEl.value = "";
  agentFileEl.value = "";
  agentCancelEditEl.classList.add("hidden");
  agentSaveEl.textContent = "Guardar";
  _setAgentStatus("", false);
  _clearAgentFieldErrors();
  updateAgentTypeVisibility();
  updateAgentKindVisibility();
  updateAgentRuleVisibility();
  renderCommandSequence();
}

function applyTriggerToForm(item) {
  const t = item.trigger || {};
  if (item.kind === "once") {
    agentKindEl.value = "once";
    _setDatetimeValue(t.target_iso || "");
  } else {
    agentKindEl.value = "recurring";
    agentRuleEl.value = t.rule || "daily";
    agentWeekdayEl.value = String(t.weekday == null ? 1 : t.weekday);
    agentNEl.value = String(t.n == null ? 1 : t.n);
    _setTimeValue(t.time || "08:30");
  }
  updateAgentKindVisibility();
  updateAgentRuleVisibility();
}

// REQ-005 (spec agentes-huerfanos-y-gestion-skills): mapea el `field` que
// devuelve el backend (`broken.field`, ver compute_agent_broken_status en
// agents.py) al elemento de UI concreto a resaltar -- distinto segun
// automation_type, ya que "commands" es un contenedor (secuencia), no un
// unico <select> como "session_id"/"skill_name".
function _agentBrokenFieldEl(field) {
  if (field === "session_id") return agentSessionEl;
  if (field === "skill_name") return agentSkillPickerEl;
  if (field === "commands") return agentCommandFieldsEl;
  return null;
}

function startEditAgent(a) {
  _editingId = a.id;
  _editingKind = "agent";
  agentLabelEl.value = a.label || "";
  agentTypeEl.value = a.automation_type || "command";
  agentSessionEl.value = a.session_id;
  _commandSequence = [...(a.commands || [])];
  agentFileEl.value = a.file_path || "";
  refreshAgentTypeOptions().then(() => {
    if (a.automation_type === "skill") agentSkillPickerEl.value = a.skill_name || "";
  });
  applyTriggerToForm(a);
  agentCancelEditEl.classList.remove("hidden");
  agentSaveEl.textContent = "Actualizar";
  _clearAgentFieldErrors();
  if (a.broken) {
    // Reusa el mismo mecanismo de resaltado ya existente para errores de
    // validacion al guardar (.field-invalid + _setAgentStatus error) --
    // asi el usuario ve EXACTAMENTE que campo tiene que corregir, no solo
    // que "algo esta mal" en el agente.
    const el = _agentBrokenFieldEl(a.broken.field);
    if (el) _invalidateAgentField(el, a.broken.detail || "Esta configuración ya no es válida.");
    else _setAgentStatus(a.broken.detail || "Esta configuración ya no es válida.", true);
  } else {
    _setAgentStatus("Editando: " + (a.label || "(sin etiqueta)"), false);
  }
  updateAgentTypeVisibility();
  renderCommandSequence();
  agentLabelEl.focus();
}

function startEditSchedule(s) {
  _editingId = s.id;
  _editingKind = "schedule";
  agentLabelEl.value = s.label || "";
  agentTypeEl.value = "alarm";
  agentSoundEl.value = s.sound || "beep";
  agentAlarmFileEl.value = s.file_path || "";
  applyTriggerToForm(s);
  agentCancelEditEl.classList.remove("hidden");
  agentSaveEl.textContent = "Actualizar";
  _setAgentStatus("Editando: " + (s.label || "(sin etiqueta)"), false);
  updateAgentTypeVisibility();
  agentLabelEl.focus();
}
agentCancelEditEl.addEventListener("click", resetAgentForm);

agentSaveEl.addEventListener("click", async () => {
  // Pedido explicito del usuario 2026-09-16: la validacion existia (el
  // guardado SI se frenaba), pero era invisible -- solo un texto de hint
  // sin color (ver fix de .settings-hint.error en styles.css) y ningun
  // resaltado en el campo puntual que faltaba. Se limpian los marcados
  // viejos de un intento anterior ANTES de validar de nuevo.
  _clearAgentFieldErrors();

  const label = agentLabelEl.value.trim();
  const type = agentTypeEl.value;
  const kind = agentKindEl.value;
  if (!label) return _invalidateAgentField(agentLabelEl, "Falta la etiqueta.");

  let trigger;
  if (kind === "once") {
    if (!agentDatetimeEl.value) return _invalidateAgentField(agentDatetimeEl, "Falta la fecha y hora.");
    trigger = { target_iso: agentDatetimeEl.value };
  } else {
    const rule = agentRuleEl.value;
    trigger = { rule, time: agentTimeEl.value || "08:30" };
    if (rule === "weekly") trigger.weekday = Number(agentWeekdayEl.value);
    if (rule === "ndays" || rule === "nhours") trigger.n = Math.max(1, Number(agentNEl.value) || 1);
  }

  if (type === "alarm") {
    const payload = { label, sound: agentSoundEl.value, kind, trigger, enabled: true, file_path: agentAlarmFileEl.value.trim() };
    await _saveScheduleOrAgent("schedule", payload);
    return;
  }

  const sessionId = agentSessionEl.value;
  if (!sessionId) return _invalidateAgentField(agentSessionEl, "Elegí una sesión destino.");
  const payload = { label, session_id: sessionId, automation_type: type, kind, trigger, enabled: true, commands: [], skill_name: "", file_path: "" };
  if (type === "command") {
    if (!_commandSequence.length) return _invalidateAgentField(agentCommandFieldsEl, "Agregá al menos un comando a la secuencia.");
    payload.commands = [..._commandSequence];
  } else if (type === "skill") {
    if (!agentSkillPickerEl.value) return _invalidateAgentField(agentSkillPickerEl, "Elegí una skill.");
    payload.skill_name = agentSkillPickerEl.value;
  } else if (type === "file") {
    if (!agentFileEl.value.trim()) return _invalidateAgentField(agentFileEl, "Elegí un archivo.");
    payload.file_path = agentFileEl.value.trim();
  }
  await _saveScheduleOrAgent("agent", payload);
});

async function _saveScheduleOrAgent(targetKind, payload) {
  try {
    const base = targetKind === "agent" ? `${API_BASE}/api/agents` : `${API_BASE}/api/alarms`;
    // Bug real reportado por el usuario 2026-09-16 ("no me guarda el
    // agente"): apiFetch es un wrapper finito sobre fetch() -- fetch() NO
    // rechaza la promesa por un status HTTP de error (400/422/500), solo
    // por fallos de red. Sin chequear `resp.ok` aca, un rechazo de
    // validacion del backend (ej. sesion invalida) caia derecho al camino
    // de "exito": mostraba "Guardado ✓" y limpiaba el formulario aunque en
    // el backend no se hubiera guardado nada.
    const resp = _editingId && _editingKind === targetKind
      ? await apiFetch(`${base}/${_editingId}`,
          { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) })
      : await apiFetch(base,
          { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
    if (!resp.ok) {
      _setAgentStatus(`No se pudo guardar. ${(await httpErrorFromResponse(resp)).message}`, true);
      return;
    }
    // Bug real reportado por el usuario 2026-09-16 ("no me guarda tampoco",
    // tras el fix de arriba): el mensaje de exito se pisaba a si mismo --
    // resetAgentForm() (llamada a continuacion) tiene su PROPIO
    // `_setAgentStatus("", false)` para limpiar el hint al arrancar un
    // formulario en blanco, asi que "Guardado ✓" quedaba escrito y
    // borrado en el mismo tick, antes de que el usuario llegara a verlo --
    // el guardado SI funcionaba (confirmado contra la API), pero nunca
    // habia ninguna confirmacion visible ni de exito ni de error. Se
    // calcula el mensaje ANTES de resetear (resetAgentForm pone
    // _editingId/_editingKind en null) y se aplica DESPUES, para que sea
    // lo ULTIMO que toca el hint.
    const successMsg = _editingId && _editingKind === targetKind ? "Actualizado ✓" : "Guardado ✓";
    resetAgentForm();
    _setAgentStatus(successMsg, false);
    await refreshAgentsAndList();
  } catch (err) {
    _setAgentStatus(`No se pudo guardar. ${friendlyErrorMessage(err)}`, true);
  }
}

// -- historial de ejecuciones, PAGINADO (pedido explicito del usuario: no
// dejar crecer la ventana a lo alto) + boton de limpieza -------------------
function describeAgentRunStatus(r) {
  if (r.status === "running") return "En curso…";
  if (r.status === "done") return "Completado";
  if (r.status === "cancelled") return "Detenido";
  if (r.status === "error") return "Error" + (r.error ? `: ${r.error}` : "");
  return r.status;
}

function renderAgentRunsPage(runs) {
  agentRunsListEl.innerHTML = "";
  if (!runs.length) {
    agentRunsListEl.innerHTML = '<div class="schedule-empty">Todavía no se disparó ningún agente.</div>';
  } else {
    runs.forEach((r) => {
      const item = document.createElement("div");
      item.className = "schedule-item";
      const main = document.createElement("div");
      main.className = "schedule-main";
      const agent = _agents.find((a) => a.id === r.agent_id);
      const label = document.createElement("div");
      label.className = "schedule-label";
      label.textContent = agent ? (agent.label || "(sin etiqueta)") : r.agent_id;
      const desc = document.createElement("div");
      desc.className = "schedule-desc";
      const started = new Date(r.started_at * 1000).toLocaleString();
      const steps = (r.commands || []).length;
      desc.textContent = `${started} · ${describeAgentRunStatus(r)}` + (steps > 1 ? ` · ${steps} pasos` : "");
      main.appendChild(label);
      main.appendChild(desc);
      item.appendChild(main);
      agentRunsListEl.appendChild(item);
    });
  }
  const totalPages = Math.max(1, Math.ceil(_agentRunsTotal / AGENT_RUNS_PAGE_SIZE));
  const currentPage = Math.floor(_agentRunsOffset / AGENT_RUNS_PAGE_SIZE) + 1;
  agentRunsPageEl.textContent = `Página ${currentPage} de ${totalPages}`;
  agentRunsPrevEl.disabled = _agentRunsOffset <= 0;
  agentRunsNextEl.disabled = _agentRunsOffset + AGENT_RUNS_PAGE_SIZE >= _agentRunsTotal;
}

async function loadAgentRuns() {
  try {
    const resp = await apiFetch(`${API_BASE}/api/agents/runs?limit=${AGENT_RUNS_PAGE_SIZE}&offset=${_agentRunsOffset}`);
    const data = await resp.json();
    _agentRunsTotal = data.total || 0;
    renderAgentRunsPage(Array.isArray(data.runs) ? data.runs : []);
  } catch (err) {
    console.warn("No se pudo cargar el historial de agentes:", err);
  }
}
agentRunsPrevEl.addEventListener("click", () => {
  _agentRunsOffset = Math.max(0, _agentRunsOffset - AGENT_RUNS_PAGE_SIZE);
  loadAgentRuns();
});
agentRunsNextEl.addEventListener("click", () => {
  _agentRunsOffset += AGENT_RUNS_PAGE_SIZE;
  loadAgentRuns();
});
agentRunsClearEl.addEventListener("click", async () => {
  try {
    await apiFetch(`${API_BASE}/api/agents/runs`, { method: "DELETE" });
    _agentRunsOffset = 0;
    await loadAgentRuns();
  } catch (err) { console.warn("No se pudo limpiar el historial:", err); }
});

async function loadAgentsSettingsPane() {
  await loadAgentSessionsSelect();
  rebuildAgentFallbackSessionSelect();  // REQ-001: reusa el cache que loadAgentSessionsSelect ya trajo
  await refreshAgentTypeOptions();
  await refreshAgentsAndList();
  _agentRunsOffset = 0;
  await loadAgentRuns();
  updateAgentTypeVisibility();
  updateAgentKindVisibility();
  updateAgentRuleVisibility();
}

// Polling "bajo demanda" (mismo criterio que el medidor de microfono, REQ-009
// de audio): solo mientras la pestaña Agentes esta REALMENTE visible, para
// no martillar el backend con /api/session/*/status en segundo plano.
let _agentsPollTimer = null;
function startAgentsPolling() {
  if (_agentsPollTimer) return;
  _agentsPollTimer = setInterval(() => { refreshAgentsAndList(); loadAgentRuns(); }, 8000);
}
function stopAgentsPolling() {
  if (_agentsPollTimer) { clearInterval(_agentsPollTimer); _agentsPollTimer = null; }
}

// -- sidebar: agentes de la sesion ACTIVA (pedido explicito del usuario) --
async function renderSessionAgentsSidebar() {
  const mine = _agents.filter((a) => a.session_id === state.sessionId);
  sessionAgentsSummaryEl.textContent = String(mine.length);
  if (!mine.length) {
    sessionAgentsListEl.innerHTML = '<li class="session-list-empty">Sin agentes en esta sesión</li>';
    return;
  }

  // Pedido explicito del usuario 2026-09-16: mismo lenguaje visual que el
  // acordeon "Tareas" (pill de estado + tarjeta con borde de color, ver
  // .todo-status/AGENT_STATUS_META) -- reusa el MISMO computeItemStatus que
  // ya usa la lista unificada de Configuracion, para que un agente se vea
  // clasificado igual en los dos lugares. Solo hace falta el busy de ESTA
  // sesion (todos los `mine` comparten session_id = state.sessionId).
  let busy = false;
  try {
    const resp = await apiFetch(`${API_BASE}/api/session/${encodeURIComponent(state.sessionId)}/status`);
    const data = await resp.json();
    busy = !!data.busy;
  } catch { /* si falla, se listan como si no estuvieran corriendo */ }
  const busyMap = { [state.sessionId]: busy };

  sessionAgentsListEl.innerHTML = "";
  mine.forEach((a) => {
    const statusKey = computeItemStatus("agent", a, busyMap);
    const meta = AGENT_STATUS_META[statusKey];
    const li = document.createElement("li");
    li.dataset.status = statusKey;

    const row = document.createElement("div");
    row.className = "session-agent-row";
    const statusPill = document.createElement("span");
    statusPill.className = `todo-status ${meta.cls}`;
    statusPill.textContent = meta.label;
    const nameSpan = document.createElement("span");
    nameSpan.className = "session-name";
    nameSpan.title = describeAgentType(a);
    nameSpan.textContent = a.label || "(sin etiqueta)";
    row.append(statusPill, nameSpan);

    const desc = document.createElement("div");
    desc.className = "session-agent-desc";
    desc.textContent = describeSchedule(a);

    const actions = document.createElement("div");
    actions.className = "session-agent-actions";

    const runBtn = document.createElement("button");
    runBtn.type = "button";
    runBtn.className = "session-rename-btn";
    runBtn.textContent = "▶️";
    runBtn.title = "Ejecutar ahora";
    runBtn.onclick = async (e) => {
      e.stopPropagation();
      try {
        await apiFetchOk(`${API_BASE}/api/agents/${a.id}/run`, { method: "POST" });
        await refreshAgentsAndList();
      } catch (err) {
        console.warn("No se pudo ejecutar el agente:", err);
        appendMessage("error", `No se pudo ejecutar el agente. ${friendlyErrorMessage(err)}`, { persist: false });
      }
    };

    const toggleBtn = document.createElement("button");
    toggleBtn.type = "button";
    toggleBtn.className = "session-delete-btn";
    toggleBtn.textContent = a.enabled === false ? "▶" : "⏸";
    toggleBtn.title = a.enabled === false ? "Activar" : "Pausar";
    toggleBtn.onclick = async (e) => {
      e.stopPropagation();
      try {
        await apiFetchOk(`${API_BASE}/api/agents/${a.id}`,
          { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ enabled: a.enabled === false }) });
        await refreshAgentsAndList();
      } catch (err) {
        console.warn("No se pudo alternar el agente:", err);
        appendMessage("error", `No se pudo ${a.enabled === false ? "activar" : "pausar"} el agente. ${friendlyErrorMessage(err)}`, { persist: false });
      }
    };

    actions.append(runBtn, toggleBtn);
    li.append(row, desc, actions);
    // Pedido del usuario: "lanzar la programación programada en la
    // configuración" -- click en la fila abre Ajustes ya en la pestaña Agentes.
    li.addEventListener("click", () => openSettings("agentes"));
    sessionAgentsListEl.appendChild(li);
  });
}
sessionAgentsConfigBtnEl.addEventListener("click", () => openSettings("agentes"));

// pedido del usuario 2026-09-17: mismo panel lateral, pero para alarmas --
// a diferencia de los agentes, las alarmas son GLOBALES (no por sesion), asi
// que se listan TODAS. Filtro `kind !== "pomodoro"` defensivo (REQ-101,
// spec configuracion-limpieza-credenciales-servidor): la pestaña Pomodoro
// ya no existe, pero la columna `kind` del backend sigue aceptando ese
// valor por compatibilidad con filas viejas ya persistidas.
function renderSessionAlarmsSidebar() {
  const mine = _schedules.filter((s) => s.kind !== "pomodoro");
  sessionAlarmsSummaryEl.textContent = String(mine.length);
  if (!mine.length) {
    sessionAlarmsListEl.innerHTML = '<li class="session-list-empty">Sin alarmas programadas</li>';
    return;
  }

  sessionAlarmsListEl.innerHTML = "";
  mine.forEach((s) => {
    const statusKey = computeItemStatus("schedule", s, {});
    const meta = AGENT_STATUS_META[statusKey];
    const li = document.createElement("li");
    li.dataset.status = statusKey;

    const row = document.createElement("div");
    row.className = "session-agent-row";
    const statusPill = document.createElement("span");
    statusPill.className = `todo-status ${meta.cls}`;
    statusPill.textContent = meta.label;
    const nameSpan = document.createElement("span");
    nameSpan.className = "session-name";
    nameSpan.textContent = s.label || "(sin etiqueta)";
    row.append(statusPill, nameSpan);

    const desc = document.createElement("div");
    desc.className = "session-agent-desc";
    desc.textContent = describeSchedule(s);

    const actions = document.createElement("div");
    actions.className = "session-agent-actions";

    const toggleBtn = document.createElement("button");
    toggleBtn.type = "button";
    toggleBtn.className = "session-delete-btn";
    toggleBtn.textContent = s.enabled === false ? "▶" : "⏸";
    toggleBtn.title = s.enabled === false ? "Activar" : "Pausar";
    toggleBtn.onclick = async (e) => {
      e.stopPropagation();
      try {
        await apiFetchOk(`${API_BASE}/api/alarms/${s.id}`,
          { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ enabled: s.enabled === false }) });
        await refreshAgentsAndList();
      } catch (err) {
        console.warn("No se pudo alternar la alarma:", err);
        appendMessage("error", `No se pudo ${s.enabled === false ? "activar" : "pausar"} la alarma. ${friendlyErrorMessage(err)}`, { persist: false });
      }
    };

    actions.append(toggleBtn);
    li.append(row, desc, actions);
    li.addEventListener("click", () => openSettings("agentes"));
    sessionAlarmsListEl.appendChild(li);
  });
}
sessionAlarmsConfigBtnEl.addEventListener("click", () => openSettings("agentes"));

// spec agentes-programados: cargar los agentes al arrancar (sin depender de
// abrir Configuracion) para que el panel lateral de la sesion activa
// muestre algo desde el primer render.
loadAgents().then(renderSessionAgentsSidebar);
loadSchedules().then(renderSessionAlarmsSidebar);

// -- "posponer" (Zzz), pedido del usuario 2026-09-17: un solo numero
// global (agentes Y alarmas comparten la misma alerta nativa) -----------
(async () => {
  try {
    const resp = await apiFetch(`${API_BASE}/api/settings/snooze`);
    const data = await resp.json();
    agentSnoozeMinutesEl.value = String(data.minutes ?? 10);
  } catch { /* deja el default del HTML (10) si falla la carga */ }
})();
agentSnoozeMinutesEl.addEventListener("change", async () => {
  const minutes = Math.max(1, Math.min(60, parseInt(agentSnoozeMinutesEl.value, 10) || 10));
  agentSnoozeMinutesEl.value = String(minutes);
  try {
    await apiFetch(`${API_BASE}/api/settings/snooze`,
      { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ minutes }) });
    agentSnoozeStatusEl.textContent = "Guardado ✓";
    agentSnoozeStatusEl.className = "settings-hint";
  } catch (err) {
    agentSnoozeStatusEl.textContent = "No se pudo guardar.";
    agentSnoozeStatusEl.className = "settings-hint error";
    console.warn("No se pudo guardar el tiempo de posponer:", err);
  }
});

// REQ-001 (spec agentes-huerfanos-y-gestion-skills): sesion de reserva --
// mismo patron de carga/guardado que el snooze de arriba.
if (agentFallbackSessionEl) {
  (async () => {
    try {
      const resp = await apiFetch(`${API_BASE}/api/settings/agent-fallback-session`);
      const data = await resp.json();
      agentFallbackSessionEl.value = data.session_id || "";
    } catch { /* deja "(sin configurar)" si falla la carga */ }
  })();
  agentFallbackSessionEl.addEventListener("change", async () => {
    const sessionId = agentFallbackSessionEl.value;
    try {
      await apiFetch(`${API_BASE}/api/settings/agent-fallback-session`,
        { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ session_id: sessionId }) });
      agentFallbackSessionStatusEl.textContent = "Guardado ✓";
      agentFallbackSessionStatusEl.className = "settings-hint";
    } catch (err) {
      agentFallbackSessionStatusEl.textContent = "No se pudo guardar.";
      agentFallbackSessionStatusEl.className = "settings-hint error";
      console.warn("No se pudo guardar la sesión de reserva:", err);
    }
  });
}

// -- REQ-009: selector de microfono + medidor de nivel bajo demanda ------
let micMeterStream = null;
let micMeterAudioCtx = null;
let micMeterRafId = null;
// Cada arranque/parada del medidor invalida los arranques que todavía están
// esperando el micrófono. BUG real (detección QA 2026-09-24, test e2e): al
// enchufar un headset USB Windows dispara varios `devicechange` seguidos
// (entrada + salida); con la pestaña Audio abierta cada uno reiniciaba el
// medidor, y como `openMicStream` es async, dos arranques en vuelo
// terminaban asignando dos streams -- el primero quedaba huérfano,
// capturando para siempre (indicador de micrófono encendido) aun con
// Configuración cerrada.
let micMeterGeneration = 0;

async function populateMicOptions() {
  if (!cfgMicSelectEl || !navigator.mediaDevices?.enumerateDevices) return;
  try {
    // enumerateDevices() sin haber pedido permiso antes solo devuelve
    // labels vacios ("") -- se pide un permiso minimo primero (mismo
    // stream que despues arranca el medidor) para poder listar nombres
    // reales de microfono, no solo "Micrófono 1"/"Micrófono 2" genericos.
    const mics = await listAudioInputs();
    // QA-007: la selección mostrada es SIEMPRE la guardada (antes era la
    // que casualmente tuviera el <select>, y arrancaba en "por defecto" en
    // cada recarga aunque el usuario hubiera elegido otro). Se resuelve por
    // id o por nombre (ver matchSavedMic).
    const resolvedId = matchSavedMic(mics);
    const saved = getSavedMic();
    cfgMicSelectEl.length = 1; // preserva "(dispositivo por defecto)"
    mics.forEach((mic, i) => {
      const opt = document.createElement("option");
      opt.value = mic.deviceId;
      opt.textContent = mic.label || `Micrófono ${i + 1}`;
      opt.dataset.label = mic.label; // se guarda junto al id al elegirlo
      cfgMicSelectEl.appendChild(opt);
    });
    // QA-008: si el elegido está desenchufado NO se pierde la preferencia --
    // queda visible como "(desconectado)" y se vuelve a usar solo apenas se
    // conecte (ver refreshActiveMicStreams).
    const savedConnected = resolvedId !== null;
    if (!savedConnected) {
      const opt = document.createElement("option");
      opt.value = saved.deviceId;
      opt.textContent = `${saved.label || "Micrófono elegido"} (desconectado)`;
      opt.dataset.label = saved.label;
      cfgMicSelectEl.appendChild(opt);
    }
    cfgMicSelectEl.value = savedConnected ? resolvedId : saved.deviceId;
    if (cfgMicStatusEl) {
      // Solo toca SU propio mensaje: no pisa un error de acceso que haya
      // puesto el medidor (ambos corren en paralelo tras un devicechange).
      if (!savedConnected) cfgMicStatusEl.textContent = MIC_DISCONNECTED_STATUS;
      else if (cfgMicStatusEl.textContent === MIC_DISCONNECTED_STATUS) cfgMicStatusEl.textContent = "";
    }
  } catch {
    // Sin permiso/dispositivo -- el selector queda solo con la opcion "por defecto"
  }
}

async function startMicMeter() {
  stopMicMeter(); // por si ya habia uno corriendo (ej. se cambio de microfono)
  const generation = micMeterGeneration;
  if (!navigator.mediaDevices?.getUserMedia) {
    if (cfgMicStatusEl) cfgMicStatusEl.textContent = "Este navegador no soporta acceso al micrófono.";
    return;
  }
  try {
    // Mismo camino que los modos de voz (QA-007): lo que muestra el medidor
    // es exactamente lo que Vision va a escuchar.
    const stream = await openMicStream();
    if (generation !== micMeterGeneration) {
      // Mientras se esperaba el micrófono llegó otro arranque, o se cerró
      // Configuración: este stream ya no le sirve a nadie.
      stream.getTracks().forEach((track) => track.stop());
      return;
    }
    micMeterStream = stream;
    if (cfgMicStatusEl) cfgMicStatusEl.textContent = "";
    // Refresca la lista con labels reales ahora que ya hay permiso otorgado.
    populateMicOptions();

  const AudioCtx = window.AudioContext || window.webkitAudioContext;
    micMeterAudioCtx = new AudioCtx();
    const source = micMeterAudioCtx.createMediaStreamSource(micMeterStream);
    const analyser = micMeterAudioCtx.createAnalyser();
    analyser.fftSize = 512;
    source.connect(analyser);
    const dataArray = new Uint8Array(analyser.fftSize);

    const poll = () => {
      analyser.getByteTimeDomainData(dataArray);
      // RMS simple (mismo criterio ya usado en voice-eye.js para el
      // "onset" de audio) -- sin FFT, barato, alcanza para un medidor visual.
      let sumSquares = 0;
      for (let i = 0; i < dataArray.length; i++) {
        const normalized = (dataArray[i] - 128) / 128;
        sumSquares += normalized * normalized;
      }
      const rms = Math.sqrt(sumSquares / dataArray.length);
      const pct = Math.min(100, Math.round(rms * 300)); // ganancia visual -- RMS crudo es casi siempre <0.3
      if (cfgMicMeterFillEl) cfgMicMeterFillEl.style.width = `${pct}%`;
      micMeterRafId = requestAnimationFrame(poll);
    };
    poll();
  } catch (err) {
    if (generation !== micMeterGeneration) return; // un arranque más nuevo ya informa su propio estado
    if (cfgMicStatusEl) cfgMicStatusEl.textContent = describeMicError(err);
  }
}

function stopMicMeter() {
  micMeterGeneration += 1; // invalida cualquier arranque todavía en vuelo
  if (micMeterRafId) cancelAnimationFrame(micMeterRafId);
  micMeterRafId = null;
  if (micMeterStream) {
    micMeterStream.getTracks().forEach((track) => track.stop());
    micMeterStream = null;
  }
  if (micMeterAudioCtx) {
    micMeterAudioCtx.close().catch(() => {});
    micMeterAudioCtx = null;
  }
  if (cfgMicMeterFillEl) cfgMicMeterFillEl.style.width = "0%";
}

if (cfgMicSelectEl) {
  cfgMicSelectEl.addEventListener("change", () => {
    // QA-007: la elección se guarda al instante (mismo patrón que el idioma
    // de STT) y la usan TODOS los modos de voz, no solo el medidor.
    setSavedMic(cfgMicSelectEl.value, cfgMicSelectEl.selectedOptions[0]?.dataset.label || "");
    _micFallbackNoticeShown = false;
    // Cambiar de microfono mientras la pestaña esta abierta reinicia el
    // medidor sobre el nuevo dispositivo elegido.
    if (isAudioSettingsPaneOpen()) startMicMeter();
    // Y si Cerebro o una reunión están escuchando, pasan al nuevo ya.
    refreshActiveMicStreams();
  });
}

// -- Tab: Modelo e inteligencia (panel DINAMICO segun valid_args reales) --
const cfgModelSelectEl = document.getElementById("cfg-model-select");
const cfgSmallModelSelectEl = document.getElementById("cfg-small-model-select");
const cfgDynamicParamsEl = document.getElementById("cfg-dynamic-params");
const cfgModelSaveBtnEl = document.getElementById("cfg-model-save");
const cfgModelStatusEl = document.getElementById("cfg-model-status");

let _allModelsCache = null;

async function loadModelSettingsPane() {
  cfgModelStatusEl.textContent = "";
  loadProvidersSettings(); // spec varios-proveedores (no bloquea el resto del panel)
  loadFallbackSettings(); // spec salto-automatico-modelos (no bloquea el resto del panel)
  if (!_allModelsCache) {
    try {
      const resp = await apiFetch(`${API_BASE}/api/models`);
      const data = await resp.json();
      _allModelsCache = data.models || [];
    } catch {
      _allModelsCache = [];
    }
  }

  const fillSelect = (selectEl, selectedId) => {
    selectEl.innerHTML = "";
    _allModelsCache.forEach((m) => {
      const opt = document.createElement("option");
      opt.value = m.id;
      opt.textContent = m.id;
      if (m.id === selectedId) opt.selected = true;
      selectEl.appendChild(opt);
    });
  };
  fillSelect(cfgModelSelectEl, state.selectedModel);

  let savedGen = {};
  try {
    const resp = await apiFetch(`${API_BASE}/api/config/generation`);
    savedGen = await resp.json();
  } catch {
    savedGen = {};
  }
  fillSelect(cfgSmallModelSelectEl, savedGen.small_model);

  renderDynamicParams(cfgModelSelectEl.value, savedGen);
  cfgModelSelectEl.onchange = () => renderDynamicParams(cfgModelSelectEl.value, savedGen);
}

// -------------------------------------------------------------------------
// spec varios-proveedores REQ-002/003/006: proveedores con clave en el .env.
// Por cada uno se eligen los modelos que aparecen en el selector y, si es
// gratuito, se confirma que puede usar lo que se le manda (sin confirmar, sus
// modelos no aparecen ni se usan en el salto). Las claves nunca pasan por aca.
// -------------------------------------------------------------------------
const cfgProvidersListEl = document.getElementById("cfg-providers-list");
const cfgProvidersAvailableEl = document.getElementById("cfg-providers-available");
const cfgProvidersSaveEl = document.getElementById("cfg-providers-save");
const cfgProvidersRefreshEl = document.getElementById("cfg-providers-refresh");
const cfgProvidersStatusEl = document.getElementById("cfg-providers-status");

function setProvidersStatus(text, kind = "") {
  cfgProvidersStatusEl.textContent = text;
  cfgProvidersStatusEl.className = kind ? `settings-status ${kind}` : "settings-status";
}

async function loadProvidersSettings() {
  try {
    const resp = await apiFetchOk(`${API_BASE}/api/providers`);
    renderProviders(await resp.json());
  } catch (err) {
    setProvidersStatus(`No se pudo cargar la lista de proveedores. ${friendlyErrorMessage(err)}`, "error");
  }
}

function renderProviders(data) {
  cfgProvidersListEl.replaceChildren();
  const providers = data.providers || [];
  if (providers.length === 0) {
    const empty = document.createElement("p");
    empty.className = "settings-hint";
    empty.textContent = "Todavía no hay ningún proveedor con clave en el .env.";
    cfgProvidersListEl.appendChild(empty);
  }
  for (const provider of providers) {
    const card = document.createElement("div");
    card.className = "provider-card";
    card.dataset.provider = provider.id;
    const head = document.createElement("div");
    head.className = "provider-card-head";
    const name = document.createElement("span");
    name.className = "provider-card-name";
    name.textContent = provider.label + (provider.principal ? " (principal)" : "") + (provider.free ? " · gratis" : "");
    const stateEl = document.createElement("span");
    stateEl.className = "provider-card-state" + (provider.error ? " error" : "");
    stateEl.textContent = provider.error
      ? provider.error
      : `${provider.visible.length} de ${provider.models.length} modelos en el selector`;
    head.append(name, stateEl);
    card.appendChild(head);
    if (provider.free && !provider.principal) {
      const consentLabel = document.createElement("label");
      consentLabel.className = "settings-checkbox-label";
      const consent = document.createElement("input");
      consent.type = "checkbox";
      consent.className = "provider-consent";
      consent.checked = Boolean(provider.consent);
      consent.addEventListener("change", async () => {
        if (!consent.checked) return;
        const accepted = await showConfirmModal({
          title: `Usar ${provider.label} gratis`,
          message:
            `En su plan gratuito, ${provider.label} puede usar lo que le mandás para mejorar sus productos. ` +
            "No lo uses con datos que no deben salir de tu empresa. ¿Confirmás?",
          acceptLabel: "Sí, lo uso",
        });
        if (!accepted) consent.checked = false;
        setProvidersStatus("Sin guardar: tocá «Guardar proveedores».");
      });
      consentLabel.append(consent, ` Acepto que ${provider.label} (gratis) pueda usar lo que le mando`);
      card.appendChild(consentLabel);
    }
    const list = document.createElement("div");
    list.className = "provider-models";
    for (const model of provider.models) {
      const label = document.createElement("label");
      const box = document.createElement("input");
      box.type = "checkbox";
      box.className = "provider-model";
      box.value = model;
      box.checked = provider.visible.includes(model);
      box.addEventListener("change", () => setProvidersStatus("Sin guardar: tocá «Guardar proveedores»."));
      label.append(box, ` ${model}`);
      list.appendChild(label);
    }
    card.appendChild(list);
    cfgProvidersListEl.appendChild(card);
  }
  const available = (data.available || []).map(
    (p) => `${p.label}${p.free ? " (gratis)" : ""}: ${p.key_names.join(" o ")}`,
  );
  cfgProvidersAvailableEl.textContent = available.length
    ? `Para sumar otro, poné su clave en el .env — ${available.join(" · ")}.`
    : "";
}

cfgProvidersSaveEl.addEventListener("click", async () => {
  const models = {};
  const consent = {};
  for (const card of cfgProvidersListEl.querySelectorAll(".provider-card")) {
    const id = card.dataset.provider;
    models[id] = [...card.querySelectorAll(".provider-model:checked")].map((box) => box.value);
    const consentBox = card.querySelector(".provider-consent");
    if (consentBox) consent[id] = consentBox.checked;
  }
  setProvidersStatus("Guardando…");
  try {
    const resp = await apiFetchOk(`${API_BASE}/api/providers`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ models, consent }),
    });
    renderProviders(await resp.json());
    _allModelsCache = null; // el selector se vuelve a pedir con la eleccion nueva
    loadFallbackSettings(); // la lista del salto ofrece los modelos nuevos
    setProvidersStatus("Guardado ✓", "ok");
  } catch (err) {
    setProvidersStatus(friendlyErrorMessage(err), "error");
  }
});

cfgProvidersRefreshEl.addEventListener("click", () => {
  setProvidersStatus("");
  loadProvidersSettings();
  loadFallbackSettings();
});

// -------------------------------------------------------------------------
// spec salto-automatico-modelos REQ-001/003: lista de prioridad que arma el
// usuario. Se edita en memoria (agregar, subir/bajar, quitar) y se guarda con
// "Guardar prioridad"; cada modelo muestra a que apunta hoy (alias), si esta
// en pausa y si es apto para datos secretos.
// -------------------------------------------------------------------------
const cfgFallbackEnabledEl = document.getElementById("cfg-fallback-enabled");
const cfgFallbackListEl = document.getElementById("cfg-fallback-list");
const cfgFallbackAddSelectEl = document.getElementById("cfg-fallback-add-select");
const cfgFallbackAddEl = document.getElementById("cfg-fallback-add");
const cfgFallbackPauseEl = document.getElementById("cfg-fallback-pause");
const cfgFallbackSaveEl = document.getElementById("cfg-fallback-save");
const cfgFallbackStatusEl = document.getElementById("cfg-fallback-status");

let _fallbackOrder = [];
let _fallbackItems = new Map();
let _fallbackChoices = [];

async function loadFallbackSettings() {
  try {
    const resp = await apiFetch(`${API_BASE}/api/config/model-fallback`);
    if (!resp.ok) throw await httpErrorFromResponse(resp);
    applyFallbackPayload(await resp.json());
    cfgFallbackStatusEl.textContent = "";
  } catch (err) {
    cfgFallbackStatusEl.textContent = `No se pudo cargar la lista de prioridad. ${friendlyErrorMessage(err)}`;
    cfgFallbackStatusEl.className = "settings-status error";
  }
}

function applyFallbackPayload(data) {
  cfgFallbackEnabledEl.checked = Boolean(data.enabled);
  cfgFallbackPauseEl.value = String(data.pause_minutes ?? 10);
  _fallbackOrder = [...(data.order || [])];
  _fallbackItems = new Map((data.items || []).map((item) => [item.model, item]));
  _fallbackChoices = data.choices || [];
  renderFallbackList();
}

function describeFallbackItem(item) {
  if (!item) return "sin guardar todavía";
  if (item.missing) return "el proveedor ya no lo ofrece: se saltea";
  const parts = [];
  if (item.resolved && item.resolved !== item.model) parts.push(`hoy: ${item.resolved}`);
  parts.push(item.paused_until ? `en pausa hasta ${item.paused_until}` : "disponible");
  if (item.secret_data_compliant === false) parts.push("no apto para datos secretos");
  return parts.join(" · ");
}

function renderFallbackList() {
  cfgFallbackListEl.replaceChildren();
  if (_fallbackOrder.length === 0) {
    const empty = document.createElement("li");
    empty.className = "fallback-empty";
    empty.textContent = "La lista está vacía: agregá al menos un modelo.";
    cfgFallbackListEl.appendChild(empty);
  }
  _fallbackOrder.forEach((model, idx) => {
    const item = _fallbackItems.get(model);
    const li = document.createElement("li");
    li.className = "fallback-item";
    li.dataset.model = model;
    li.dataset.state = !item ? "new" : item.missing ? "missing" : item.paused_until ? "paused" : "ok";
    const name = document.createElement("span");
    name.className = "fallback-name";
    name.textContent = item?.label || prettyModel(model); // spec varios-proveedores
    const info = document.createElement("span");
    info.className = "fallback-info";
    info.textContent = describeFallbackItem(item);
    const actions = document.createElement("span");
    actions.className = "fallback-actions";
    const makeBtn = (label, title, disabled, onClick) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "msg-copy-btn";
      btn.textContent = label;
      btn.title = title;
      btn.disabled = disabled;
      btn.onclick = onClick;
      return btn;
    };
    actions.append(
      makeBtn("↑", "Subir", idx === 0, () => moveFallbackItem(idx, -1)),
      makeBtn("↓", "Bajar", idx === _fallbackOrder.length - 1, () => moveFallbackItem(idx, 1)),
      makeBtn("✕", "Quitar de la lista", false, () => {
        _fallbackOrder.splice(idx, 1);
        renderFallbackList();
      }),
    );
    li.append(name, info, actions);
    cfgFallbackListEl.appendChild(li);
  });
  renderFallbackChoices();
}

function moveFallbackItem(idx, delta) {
  const target = idx + delta;
  if (target < 0 || target >= _fallbackOrder.length) return;
  [_fallbackOrder[idx], _fallbackOrder[target]] = [_fallbackOrder[target], _fallbackOrder[idx]];
  renderFallbackList();
}

function renderFallbackChoices() {
  cfgFallbackAddSelectEl.replaceChildren();
  const available = _fallbackChoices.filter((c) => !_fallbackOrder.includes(c.value));
  const groups = [
    ["Siguen al modelo más nuevo", available.filter((c) => c.value !== c.resolved)],
    ["Modelo puntual", available.filter((c) => c.value === c.resolved)],
  ];
  for (const [label, choices] of groups) {
    if (choices.length === 0) continue;
    const group = document.createElement("optgroup");
    group.label = label;
    for (const choice of choices) {
      const opt = document.createElement("option");
      opt.value = choice.value;
      opt.textContent = choice.label;
      group.appendChild(opt);
    }
    cfgFallbackAddSelectEl.appendChild(group);
  }
  cfgFallbackAddEl.disabled = available.length === 0;
}

cfgFallbackAddEl.addEventListener("click", () => {
  const value = cfgFallbackAddSelectEl.value;
  if (!value || _fallbackOrder.includes(value)) return;
  _fallbackOrder.push(value);
  renderFallbackList();
});

cfgFallbackSaveEl.addEventListener("click", async () => {
  cfgFallbackStatusEl.textContent = "Guardando…";
  cfgFallbackStatusEl.className = "settings-status";
  try {
    const resp = await apiFetchOk(`${API_BASE}/api/config/model-fallback`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        enabled: cfgFallbackEnabledEl.checked,
        order: _fallbackOrder,
        pause_minutes: Number(cfgFallbackPauseEl.value),
      }),
    });
    const data = await resp.json();
    applyFallbackPayload(data);
    const wasEnabled = state.fallbackEnabled;
    state.fallbackEnabled = Boolean(data.enabled);
    // Al activarlo queda elegido "Automático"; al apagarlo, si estaba en
    // Automatico, vuelve al modelo por defecto del backend.
    if (state.fallbackEnabled && !wasEnabled) saveSelectedModel(AUTO_MODEL);
    if (!state.fallbackEnabled && state.selectedModel === AUTO_MODEL) saveSelectedModel(state.defaultModel);
    refreshModelFooter();
    // REQ-010: con la lista o el interruptor nuevos se recalcula el chip.
    if (state.fallbackEnabled) checkFallbackResume();
    else scheduleFallbackResume(null);
    cfgFallbackStatusEl.textContent = "Guardado ✓";
    cfgFallbackStatusEl.className = "settings-status ok";
  } catch (err) {
    cfgFallbackStatusEl.textContent = friendlyErrorMessage(err);
    cfgFallbackStatusEl.className = "settings-status error";
  }
});

// -------------------------------------------------------------------------
// spec salto-automatico-modelos REQ-010: la vuelta al modelo de casa se ve
// cuando vence la pausa, sin esperar al proximo mensaje. Bug real (2026-09-26,
// prueba del usuario): la pausa vencia a las 10:58 pero el chip seguia en el
// modelo del salto y el aviso "Ya pasó la pausa de..." recien salia al
// escribir de nuevo (11:02), porque el backend solo mira las pausas al
// arrancar un turno. Ahora el backend informa en `resume_at` (epoch, en
// segundos) cuando vence la proxima pausa, y a esa hora se le pregunta con que
// modelo seguiria el chat (POST /api/model-fallback/check).
// -------------------------------------------------------------------------
const FALLBACK_RESUME_MARGIN_MS = 1000; // al preguntar, la pausa ya vencio seguro
const FALLBACK_BUSY_RETRY_MS = 3000; // respuesta en curso: se pregunta apenas termine
const FALLBACK_ERROR_RETRY_MS = 30000; // backend sin respuesta: se reintenta
let _fallbackResumeTimer = null;

// `resumeAt` null = nada que esperar; `delayMs` fuerza un reintento.
function scheduleFallbackResume(resumeAt, delayMs = null) {
  clearTimeout(_fallbackResumeTimer);
  _fallbackResumeTimer = null;
  if (delayMs === null) {
    if (!resumeAt) return;
    delayMs = Math.max(0, resumeAt * 1000 - Date.now()) + FALLBACK_RESUME_MARGIN_MS;
  }
  _fallbackResumeTimer = setTimeout(checkFallbackResume, delayMs);
}

async function checkFallbackResume() {
  scheduleFallbackResume(null);
  // Con una respuesta en curso, el propio turno vuelve al modelo en su
  // proximo paso (y avisa con `model-switch`); si termina antes, se pregunta
  // apenas quede libre -- asi el aviso nunca cae en medio de una respuesta.
  if (state.sending > 0) {
    scheduleFallbackResume(null, FALLBACK_BUSY_RETRY_MS);
    return;
  }
  let data;
  try {
    const resp = await apiFetchOk(`${API_BASE}/api/model-fallback/check`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model: selectedModelForRequest() ?? null }),
    });
    data = await resp.json();
  } catch {
    scheduleFallbackResume(null, FALLBACK_ERROR_RETRY_MS);
    return;
  }
  if (!data.enabled) return;
  // El chip se toca solo si hay algo que mostrar: un salto en curso, una
  // vuelta, o un modelo que ya venia mostrando (al cargar sin pausas queda
  // "Automático", como siempre).
  if (data.model && (data.text || state.activeModel || data.model !== data.home)) {
    state.activeModel = data.model;
    state.activeModelJumped = data.model !== data.home;
    renderModelChip();
  }
  if (data.text) appendMessage("system-note", data.text, { persist: false });
  if (!settingsOverlayEl.classList.contains("hidden")) refreshFallbackStatuses();
  scheduleFallbackResume(data.resume_at);
}

// Solo el estado de cada modelo (disponible / en pausa): el orden y los
// cambios sin guardar del usuario en la lista quedan como estan.
async function refreshFallbackStatuses() {
  try {
    const resp = await apiFetchOk(`${API_BASE}/api/config/model-fallback`);
    const data = await resp.json();
    _fallbackItems = new Map((data.items || []).map((item) => [item.model, item]));
    renderFallbackList();
  } catch {
    // no es critico: al reabrir Configuracion se recarga todo
  }
}

function renderDynamicParams(modelId, savedGen) {
  const model = (_allModelsCache || []).find((m) => m.id === modelId);
  const validArgs = model?.valid_args || [];
  const reasoningLevels = model?.reasoning_levels || [];

  cfgDynamicParamsEl.innerHTML = "";

  if (validArgs.includes("temperature")) {
    cfgDynamicParamsEl.appendChild(
      buildRangeField("temperature", "Temperature", 0, 2, 0.1, savedGen.temperature ?? 1)
    );
  }
  if (validArgs.includes("top_p")) {
    cfgDynamicParamsEl.appendChild(
      buildRangeField("top_p", "Top P", 0, 1, 0.05, savedGen.top_p ?? 1)
    );
  }
  if (validArgs.includes("max_tokens")) {
    cfgDynamicParamsEl.appendChild(
      buildNumberField("max_tokens", "Max output tokens", savedGen.max_tokens ?? "")
    );
  }
  if (reasoningLevels.length > 0) {
    cfgDynamicParamsEl.appendChild(
      buildSelectField("reasoning_effort", "Reasoning effort", reasoningLevels, savedGen.reasoning_effort)
    );
  }
  if (validArgs.includes("verbosity")) {
    cfgDynamicParamsEl.appendChild(
      buildSelectField("verbosity", "Verbosity", ["none", "minimal", "low", "medium", "high"], savedGen.verbosity)
    );
  }

  if (!cfgDynamicParamsEl.children.length) {
    cfgDynamicParamsEl.innerHTML = '<p class="settings-hint">Este modelo no expone parámetros de generación configurables.</p>';
  }
}

function buildRangeField(id, label, min, max, step, value) {
  const wrap = document.createElement("div");
  wrap.className = "param-field";
  const labelEl = document.createElement("label");
  labelEl.textContent = label;
  const valSpan = document.createElement("span");
  valSpan.className = "param-val";
  valSpan.textContent = value;
  labelEl.appendChild(valSpan);
  const input = document.createElement("input");
  input.type = "range";
  input.id = `param-${id}`;
  input.min = min; input.max = max; input.step = step; input.value = value;
  input.oninput = () => { valSpan.textContent = input.value; };
  wrap.append(labelEl, input);
  return wrap;
}

function buildNumberField(id, label, value) {
  const wrap = document.createElement("div");
  wrap.className = "param-field";
  const labelEl = document.createElement("label");
  labelEl.textContent = label;
  const input = document.createElement("input");
  input.type = "number";
  input.id = `param-${id}`;
  input.min = 1;
  input.value = value;
  input.placeholder = "(default del modelo)";
  wrap.append(labelEl, input);
  return wrap;
}

function buildSelectField(id, label, options, selected) {
  const wrap = document.createElement("div");
  wrap.className = "param-field";
  const labelEl = document.createElement("label");
  labelEl.textContent = label;
  const select = document.createElement("select");
  select.id = `param-${id}`;
  const noneOpt = document.createElement("option");
  noneOpt.value = "";
  noneOpt.textContent = "(default del modelo)";
  select.appendChild(noneOpt);
  options.forEach((opt) => {
    const o = document.createElement("option");
    o.value = opt;
    o.textContent = opt;
    if (opt === selected) o.selected = true;
    select.appendChild(o);
  });
  wrap.append(labelEl, select);
  return wrap;
}

cfgModelSaveBtnEl.addEventListener("click", async () => {
  const modelId = cfgModelSelectEl.value;
  const smallModel = cfgSmallModelSelectEl.value;

  const readParam = (id) => {
    const el = document.getElementById(`param-${id}`);
    return el ? el.value : "";
  };

  const payload = {
    temperature: readParam("temperature") !== "" ? Number(readParam("temperature")) : null,
    top_p: readParam("top_p") !== "" ? Number(readParam("top_p")) : null,
    max_tokens: readParam("max_tokens") !== "" ? Number(readParam("max_tokens")) : null,
    reasoning_effort: readParam("reasoning_effort") || "",
    verbosity: readParam("verbosity") || "",
    small_model: smallModel || "",
  };

  cfgModelStatusEl.textContent = "Guardando…";
  cfgModelStatusEl.className = "settings-status";
  try {
    await apiFetchOk(`${API_BASE}/api/config/generation`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    saveSelectedModel(modelId);
    cfgModelStatusEl.textContent = "Guardado ✓";
    cfgModelStatusEl.className = "settings-status ok";
  } catch (err) {
    cfgModelStatusEl.textContent = friendlyErrorMessage(err);
    cfgModelStatusEl.className = "settings-status error";
  }
});

// -- Tab: Credenciales (cifradas, ver app/credentials.py) -------------------
// REQ-103/104/105/106 (spec configuracion-limpieza-credenciales-servidor,
// 2026-09-19): rediseño DATA-DRIVEN -- antes habia 6 <input> hardcodeados
// a mano en index.html, cubriendo solo 6 de los 22 campos reales que los
// 4 conectores necesitan. Ahora el acordeon se arma 100% en runtime desde
// GET /api/config/credentials/schema (backend/app/credentials.py:
// CONNECTOR_SCHEMA) -- agregar un campo nuevo a futuro es extender ese
// schema en el backend, nunca este archivo.
const credAccordionEl = document.getElementById("cred-accordion");
const credClearEl = document.getElementById("cred-clear");
const credStatusEl = document.getElementById("cred-status");

// spec carpeta-por-sesion REQ-010 (preservado tal cual del diseño
// original, ahora aplicado a inputs generados dinamicamente): icono de
// ojito "mostrar SOLO mientras se mantiene presionado" -- a diferencia de
// un toggle persistente, el valor NUNCA queda visible mas tiempo del
// estrictamente necesario. Se llama una vez por cada input password
// generado (en vez de un querySelectorAll global al cargar el modulo,
// porque estos elementos no existen todavia en ese momento).
function _wireCredEyeButton(btn, targetEl) {
  const reveal = () => {
    targetEl.type = "text";
    btn.classList.add("revealing");
  };
  const hide = () => {
    targetEl.type = "password";
    btn.classList.remove("revealing");
  };
  btn.addEventListener("mousedown", reveal);
  btn.addEventListener("mouseup", hide);
  btn.addEventListener("mouseleave", hide);
  btn.addEventListener("touchstart", (e) => { e.preventDefault(); reveal(); });
  btn.addEventListener("touchend", hide);
  btn.addEventListener("touchcancel", hide);
  btn.addEventListener("click", (e) => e.preventDefault());
}

const CRED_EYE_SVG =
  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" ' +
  'stroke-linecap="round" stroke-linejoin="round"><path d="M2 12s3.6-7 10-7 10 7 10 ' +
  '7-3.6 7-10 7-10-7-10-7Z"/><circle cx="12" cy="12" r="3"/></svg>';

// Cache del schema (no cambia en runtime, se pide una sola vez por
// apertura de Configuracion) + cache de referencias a los <input>
// generados, indexados por field key -- para leer sus valores al guardar
// sin tener que volver a buscar en el DOM.
let _credSchema = null;
const _credFieldEls = {}; // { field_key: <input>|<textarea> }

function _buildCredField(field) {
  const wrap = document.createElement("div");
  wrap.className = "settings-field";
  const label = document.createElement("label");
  label.setAttribute("for", `cred-${field.key}`);
  label.textContent = field.label;
  wrap.appendChild(label);

  let inputEl;
  if (field.type === "textarea") {
    inputEl = document.createElement("textarea");
    inputEl.rows = 4;
    if (field.key === "bq_key_json") {
      inputEl.placeholder = "Pegá acá el contenido del archivo bigquery_key.json";
    }
  } else {
    inputEl = document.createElement("input");
    inputEl.type = field.type === "number" ? "number" : (field.type === "password" ? "password" : "text");
    inputEl.autocomplete = "off";
    if (field.default) inputEl.placeholder = `ej. ${field.default}`;
  }
  inputEl.id = `cred-${field.key}`;
  _credFieldEls[field.key] = inputEl;

  if (field.type === "password") {
    const row = document.createElement("div");
    row.className = "cred-password-row";
    inputEl.placeholder = "••••••••";
    row.appendChild(inputEl);
    const eyeBtn = document.createElement("button");
    eyeBtn.type = "button";
    eyeBtn.className = "cred-eye-btn";
    eyeBtn.title = "Mantené presionado para ver";
    eyeBtn.innerHTML = CRED_EYE_SVG;
    row.appendChild(eyeBtn);
    _wireCredEyeButton(eyeBtn, inputEl);
    wrap.appendChild(row);
  } else {
    wrap.appendChild(inputEl);
  }
  return wrap;
}

async function _testConnectorConnection(connectorKey, statusEl) {
  statusEl.textContent = "Probando conexión…";
  statusEl.className = "settings-status";
  try {
    const resp = await apiFetch(`${API_BASE}/api/config/credentials/test/${connectorKey}`, { method: "POST" });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) throw httpError(resp.status, data);
    statusEl.textContent = data.ok ? "✓ Conexión exitosa" : "✗ No se pudo conectar (revisá las credenciales)";
    statusEl.className = data.ok ? "settings-status ok" : "settings-status error";
  } catch (err) {
    statusEl.textContent = friendlyErrorMessage(err);
    statusEl.className = "settings-status error";
  }
}

async function _saveConnectorCredentials(connectorKey, fields, statusEl) {
  statusEl.textContent = "Guardando…";
  statusEl.className = "settings-status";
  const payload = {};
  for (const field of fields) {
    payload[field.key] = _credFieldEls[field.key].value;
  }
  try {
    const resp = await apiFetch(`${API_BASE}/api/config/credentials`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ fields: payload }),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw httpError(resp.status, err);
    }
    // Limpia los campos tras guardar -- nunca se re-muestra el valor real.
    for (const field of fields) _credFieldEls[field.key].value = "";
    statusEl.textContent = "Guardado (cifrado) ✓";
    statusEl.className = "settings-status ok";
    await _refreshCredFieldTitles();
  } catch (err) {
    statusEl.textContent = friendlyErrorMessage(err);
    statusEl.className = "settings-status error";
  }
}

function _buildCredAccordionSection(connectorKey, connector) {
  const details = document.createElement("details");
  details.className = "acc-section";
  const summary = document.createElement("summary");
  const titleEl = document.createElement("span");
  titleEl.className = "acc-title";
  titleEl.textContent = connector.label;
  summary.appendChild(titleEl);
  details.appendChild(summary);

  const body = document.createElement("div");
  body.className = "acc-body";
  for (const field of connector.fields) {
    body.appendChild(_buildCredField(field));
  }

  const actions = document.createElement("div");
  actions.className = "settings-actions";
  const saveBtn = document.createElement("button");
  saveBtn.type = "button";
  saveBtn.className = "acc-action-btn";
  saveBtn.textContent = "Guardar";
  const testBtn = document.createElement("button");
  testBtn.type = "button";
  testBtn.className = "acc-action-btn secondary";
  testBtn.textContent = "Probar conexión";
  const statusEl = document.createElement("span");
  statusEl.className = "settings-status";

  saveBtn.addEventListener("click", () => _saveConnectorCredentials(connectorKey, connector.fields, statusEl));
  testBtn.addEventListener("click", () => _testConnectorConnection(connectorKey, statusEl));

  actions.append(saveBtn, testBtn, statusEl);
  body.appendChild(actions);
  details.appendChild(body);
  return details;
}

// Los inputs quedan VACIOS aunque haya un valor guardado (el backend
// nunca devuelve el valor real) -- se usa el `title` del campo (tooltip)
// para indicar "ya tiene un valor guardado", mismo criterio ya usado
// antes del rediseño data-driven.
async function _refreshCredFieldTitles() {
  try {
    const resp = await apiFetch(`${API_BASE}/api/config/credentials`);
    const status = await resp.json();
    for (const [key, el] of Object.entries(_credFieldEls)) {
      el.title = status[key] ? "Ya tiene un valor guardado" : "";
    }
  } catch {
    // noop
  }
}

async function loadCredentialsSettingsPane() {
  if (_credSchema) {
    await _refreshCredFieldTitles();
    return; // el acordeon ya esta armado, solo se refresca el estado de "tiene valor"
  }
  try {
    const resp = await apiFetch(`${API_BASE}/api/config/credentials/schema`);
    _credSchema = await resp.json();
  } catch (err) {
    credAccordionEl.innerHTML = '<p class="settings-hint error">No se pudo cargar el esquema de credenciales.</p>';
    console.warn("No se pudo cargar /api/config/credentials/schema:", err);
    return;
  }
  credAccordionEl.innerHTML = "";
  for (const [connectorKey, connector] of Object.entries(_credSchema)) {
    credAccordionEl.appendChild(_buildCredAccordionSection(connectorKey, connector));
  }
  await _refreshCredFieldTitles();
}

// -- Tab: Conexiones (spec controles-seguridad-optimizacion-lightspeed- ---
// bigquery, pedido del usuario 2026-09-21): status por color de cada
// conector (reusa CONNECTOR_LABELS/refreshConnectorsStatus del sidebar),
// umbral configurable de "consulta pesada", y precarga simple de
// datasets/tablas de BigQuery + listas de SharePoint. El bytes<->MB se
// convierte en la UI porque pensar en 1073741824 bytes es incomodo --
// el backend sigue guardando/leyendo bytes siempre (ver
// app/tools/dbquery.py: _effective_bq_cost_threshold).
// ---------------------------------------------------------------------------
const connStatusListEl = document.getElementById("conn-status-list");
const connStatusRefreshEl = document.getElementById("conn-status-refresh");
const connStatusStatusEl = document.getElementById("conn-status-status");
const connCostThresholdMbEl = document.getElementById("conn-cost-threshold-mb");
const connExportThresholdEl = document.getElementById("conn-export-threshold");
const connPreviewRowsEl = document.getElementById("conn-preview-rows");
const connThresholdsSaveEl = document.getElementById("conn-thresholds-save");
const connThresholdsStatusEl = document.getElementById("conn-thresholds-status");
const connBqDatasetsToggleEl = document.getElementById("conn-bq-datasets-toggle");
const connBqDatasetsListEl = document.getElementById("conn-bq-datasets-list");
const connSpListsToggleEl = document.getElementById("conn-sp-lists-toggle");
const connSpListsListEl = document.getElementById("conn-sp-lists-list");

const MB_IN_BYTES = 1024 * 1024;

async function _refreshConnStatusList() {
  connStatusListEl.innerHTML = '<div class="conn-status-row"><span class="connector-dot checking"></span>Verificando…</div>';
  try {
    const resp = await apiFetch(`${API_BASE}/api/connectors/status`);
    const data = await resp.json();
    const connectors = data.connectors || [];
    connStatusListEl.innerHTML = "";
    connectors.forEach((c) => {
      const row = document.createElement("div");
      row.className = "conn-status-row";
      const dot = document.createElement("span");
      dot.className = `connector-dot ${c.status}`;
      const label = document.createElement("span");
      label.className = "conn-status-label";
      label.textContent = CONNECTOR_LABELS[c.name] || c.name;
      const detail = document.createElement("span");
      detail.className = "conn-status-detail";
      detail.textContent = c.status === "online" ? "Conectado" : (c.detail || "Sin conexión");
      if (c.technical) detail.title = `Detalle técnico: ${c.technical}`;
      row.append(dot, label, detail);
      connStatusListEl.appendChild(row);
    });
    connStatusStatusEl.textContent = "";
  } catch (err) {
    connStatusListEl.innerHTML = "";
    connStatusStatusEl.textContent = friendlyErrorMessage(err);
    connStatusStatusEl.className = "settings-status error";
  }
}
connStatusRefreshEl.addEventListener("click", _refreshConnStatusList);

async function _loadConnThresholds() {
  connThresholdsStatusEl.textContent = "";
  try {
    const resp = await apiFetch(`${API_BASE}/api/config/connections`);
    if (!resp.ok) throw await httpErrorFromResponse(resp);
    const data = await resp.json();
    const overrides = data.overrides || {};
    const defaults = data.defaults || {};
    connCostThresholdMbEl.value = overrides.bq_cost_guardrail_bytes != null
      ? Math.round(overrides.bq_cost_guardrail_bytes / MB_IN_BYTES) : "";
    connCostThresholdMbEl.placeholder = `(default: ${Math.round((defaults.bq_cost_guardrail_bytes || 0) / MB_IN_BYTES)} MB)`;
    connExportThresholdEl.value = overrides.export_row_threshold ?? "";
    connExportThresholdEl.placeholder = `(default: ${defaults.export_row_threshold ?? 50})`;
    connPreviewRowsEl.value = overrides.export_preview_rows ?? "";
    connPreviewRowsEl.placeholder = `(default: ${defaults.export_preview_rows ?? 50})`;
  } catch (err) {
    connThresholdsStatusEl.textContent = `No se pudieron cargar los umbrales. ${friendlyErrorMessage(err)}`;
    connThresholdsStatusEl.className = "settings-status error";
  }
}

connThresholdsSaveEl.addEventListener("click", async () => {
  const costMb = connCostThresholdMbEl.value.trim();
  const exportRows = connExportThresholdEl.value.trim();
  const previewRows = connPreviewRowsEl.value.trim();
  const payload = {
    bq_cost_guardrail_bytes: costMb !== "" ? Math.round(Number(costMb) * MB_IN_BYTES) : null,
    export_row_threshold: exportRows !== "" ? Number(exportRows) : null,
    export_preview_rows: previewRows !== "" ? Number(previewRows) : null,
  };
  connThresholdsStatusEl.textContent = "Guardando…";
  connThresholdsStatusEl.className = "settings-status";
  try {
    await apiFetchOk(`${API_BASE}/api/config/connections`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    connThresholdsStatusEl.textContent = "Guardado ✓";
    connThresholdsStatusEl.className = "settings-status ok";
  } catch (err) {
    connThresholdsStatusEl.textContent = friendlyErrorMessage(err);
    connThresholdsStatusEl.className = "settings-status error";
  }
});

// Precarga bajo demanda (recien al expandir, mismo criterio que los
// acordeones "Contexto"/"Conectores" del sidebar -- evita pegarle a
// BigQuery/SharePoint solo por abrir la pestaña de Configuracion).
function _buildConnTreeItem(text, onExpand) {
  const li = document.createElement("li");
  li.textContent = text;
  if (onExpand) {
    li.classList.add("conn-tree-item-expandable");
    li.addEventListener("click", () => onExpand(li));
  }
  return li;
}

let _bqDatasetsLoaded = false;
connBqDatasetsToggleEl.addEventListener("click", async () => {
  const expanded = connBqDatasetsToggleEl.getAttribute("aria-expanded") === "true";
  connBqDatasetsToggleEl.setAttribute("aria-expanded", String(!expanded));
  connBqDatasetsToggleEl.querySelector(".conn-tree-caret").textContent = expanded ? "▶" : "▼";
  connBqDatasetsListEl.classList.toggle("hidden", expanded);
  if (expanded || _bqDatasetsLoaded) return;

  connBqDatasetsListEl.innerHTML = "<li>Cargando datasets…</li>";
  try {
    const resp = await apiFetch(`${API_BASE}/api/config/connections/bigquery/datasets`);
    if (!resp.ok) throw await httpErrorFromResponse(resp);
    const data = await resp.json();
    connBqDatasetsListEl.innerHTML = "";
    (data.datasets || []).forEach((ds) => {
      connBqDatasetsListEl.appendChild(
        _buildConnTreeItem(`📁 ${ds}`, (li) => _expandBqTables(li, ds))
      );
    });
    _bqDatasetsLoaded = true;
  } catch (err) {
    renderListError(connBqDatasetsListEl, friendlyErrorMessage(err));
  }
});

async function _expandBqTables(datasetLi, datasetId) {
  // Toggle simple: si ya tiene una sub-lista de tablas, la muestra/oculta
  // en vez de volver a pedirla al backend cada vez que se hace click.
  const existing = datasetLi.querySelector(".conn-tree-sublist");
  if (existing) {
    existing.classList.toggle("hidden");
    return;
  }
  const sublist = document.createElement("ul");
  sublist.className = "conn-tree-list conn-tree-sublist";
  sublist.innerHTML = "<li>Cargando tablas…</li>";
  datasetLi.appendChild(sublist);
  try {
    const resp = await apiFetch(
      `${API_BASE}/api/config/connections/bigquery/tables?dataset_id=${encodeURIComponent(datasetId)}`
    );
    if (!resp.ok) throw await httpErrorFromResponse(resp);
    const data = await resp.json();
    sublist.innerHTML = "";
    (data.tables || []).forEach((tbl) => {
      sublist.appendChild(_buildConnTreeItem(`📄 ${tbl}`));
    });
    if (!(data.tables || []).length) sublist.innerHTML = "<li>(sin tablas)</li>";
  } catch (err) {
    renderListError(sublist, friendlyErrorMessage(err));
  }
}

let _spListsLoaded = false;
connSpListsToggleEl.addEventListener("click", async () => {
  const expanded = connSpListsToggleEl.getAttribute("aria-expanded") === "true";
  connSpListsToggleEl.setAttribute("aria-expanded", String(!expanded));
  connSpListsToggleEl.querySelector(".conn-tree-caret").textContent = expanded ? "▶" : "▼";
  connSpListsListEl.classList.toggle("hidden", expanded);
  if (expanded || _spListsLoaded) return;

  connSpListsListEl.innerHTML = "<li>Cargando listas…</li>";
  try {
    const resp = await apiFetch(`${API_BASE}/api/config/connections/sharepoint/lists`);
    if (!resp.ok) throw await httpErrorFromResponse(resp);
    const data = await resp.json();
    connSpListsListEl.innerHTML = "";
    (data.lists || []).forEach((name) => {
      connSpListsListEl.appendChild(_buildConnTreeItem(`📋 ${name}`));
    });
    _spListsLoaded = true;
  } catch (err) {
    renderListError(connSpListsListEl, friendlyErrorMessage(err));
  }
});

async function loadConnectionsSettingsPane() {
  await Promise.all([_refreshConnStatusList(), _loadConnThresholds()]);
}

credClearEl.addEventListener("click", async () => {
  const confirmed = await showConfirmModal({
    title: "Quitar todas las credenciales",
    message: "Se van a borrar TODAS las credenciales guardadas de los 4 conectores. Esta acción no se puede deshacer.",
    acceptLabel: "Quitar todo",
  });
  if (!confirmed) return;
  try {
    await apiFetchOk(`${API_BASE}/api/config/credentials`, { method: "DELETE" });
  } catch (err) {
    credStatusEl.textContent = `No se pudieron quitar las credenciales. ${friendlyErrorMessage(err)}`;
    credStatusEl.className = "settings-status error";
    return;
  }
  credStatusEl.textContent = "Credenciales quitadas";
  credStatusEl.className = "settings-status";
  await _refreshCredFieldTitles();
});

// -- Tab: Servidor (spec jarvis-mobile-pwa) ----------------------------------
// A diferencia de las demas pestañas, esta NO persiste nada en el backend
// -- API_BASE/token viven solo en el localStorage de ESTE navegador/
// dispositivo (ver getApiBase/getAccessToken/setServerConnection mas
// arriba). Tiene sentido: cada celular/notebook que abre Vision puede
// necesitar apuntar a un backend distinto.
const cfgServerUrlEl = document.getElementById("cfg-server-url");
const cfgServerTokenEl = document.getElementById("cfg-server-token");
const cfgServerTestEl = document.getElementById("cfg-server-test");
const cfgServerSaveEl = document.getElementById("cfg-server-save");
const cfgServerStatusEl = document.getElementById("cfg-server-status");

function loadServerSettingsPane() {
  cfgServerUrlEl.value = API_BASE;
  cfgServerTokenEl.value = getAccessToken();
  cfgServerStatusEl.textContent = "";
  cfgServerStatusEl.className = "settings-status";
}

// REQ-107/108 ELIMINADOS (spec configuracion-limpieza-credenciales-servidor,
// 2026-09-20): se habia agregado una lista de IPs locales detectadas para
// ayudar a conectarse desde el celular, pero el servidor sigue bindeado a
// 127.0.0.1 -- exponerlo a la red exigia ademas una regla de firewall de
// Windows con privilegios de administrador, y el usuario decidio
// explicitamente NO automatizar la apertura de un puerto de entrada en una
// notebook corporativa por riesgo real de politica de Cyber
// Security. Sin el bind a la red, la lista de IPs no tenia uso practico --
// se elimino toda la feature (ver tambien app/network_info.py, eliminado).

async function testServerConnection(url, token) {
  const headers = token ? { "X-Jarvis-Token": token } : {};
  const resp = await fetch(`${url}/api/status`, { headers });
  if (!resp.ok) throw await httpErrorFromResponse(resp);
  return resp.json();
}

cfgServerTestEl.addEventListener("click", async () => {
  const url = cfgServerUrlEl.value.trim().replace(/\/$/, "");
  const token = cfgServerTokenEl.value.trim();
  cfgServerStatusEl.textContent = "Probando…";
  cfgServerStatusEl.className = "settings-status";
  try {
    const data = await testServerConnection(url, token);
    cfgServerStatusEl.textContent = `Conectado ✓ (modelo: ${data.model || "?"})`;
    cfgServerStatusEl.className = "settings-status ok";
  } catch (err) {
    cfgServerStatusEl.textContent = `No se pudo conectar con ese servidor. ${friendlyErrorMessage(err)}`;
    cfgServerStatusEl.className = "settings-status error";
  }
});

cfgServerSaveEl.addEventListener("click", async () => {
  const url = cfgServerUrlEl.value.trim().replace(/\/$/, "");
  const token = cfgServerTokenEl.value.trim();
  if (!url) {
    cfgServerStatusEl.textContent = "La dirección no puede estar vacía";
    cfgServerStatusEl.className = "settings-status error";
    return;
  }
  try {
    await testServerConnection(url, token);
  } catch (err) {
    const proceed = await showConfirmModal({
      title: "No se pudo confirmar la conexión",
      message: `No se pudo confirmar que ${url} responda${token ? " con esa clave de acceso" : ""}. ${friendlyErrorMessage(err)} ¿Guardar de todas formas?`,
      acceptLabel: "Guardar igual",
    });
    if (!proceed) return;
  }
  setServerConnection(url, token);
  cfgServerStatusEl.textContent = "Guardado. Recargando…";
  cfgServerStatusEl.className = "settings-status ok";
  // Recarga completa: API_BASE es una constante fijada al cargar el
  // modulo (ver arriba), asi que el unico camino simple y confiable para
  // que TODO el archivo (los ~55 fetch existentes) empiece a usar el
  // nuevo valor es recargar la pagina.
  setTimeout(() => window.location.reload(), 600);
});

// REQ-102 (spec configuracion-limpieza-credenciales-servidor, 2026-09-19):
// pestaña "Sitio SharePoint" ELIMINADA de la UI -- confirmado leyendo
// graph_search/sharepoint_connector.py que la carpeta/biblioteca elegida
// aca NUNCA acotaba ninguna busqueda real del LLM (solo agregaba una frase
// decorativa al system prompt via project_context.py:build_project_context_prompt).
// El backend (ProjectConfig.sharepoint_*, los endpoints
// GET/POST/DELETE /api/config/project/sharepoint, GET /api/sharepoint/
// drives|children) se dejo INTACTO a proposito -- solo se quito el acceso
// desde esta UI. Si hace falta recuperar el frontend, ver el historial de
// git de esta seccion.

// -- Tab: Skills -------------------------------------------------------------
// REQ-007 (spec agentes-huerfanos-y-gestion-skills): se elimino el flujo
// Git/SSH ("Sincronizar repo GitHub", backend/app/skills_repo.py) --
// descubierto durante el build que era innecesario: este input + boton
// "Explorar…" con el picker nativo YA hacian todo lo que hacia falta
// (discover_skills() escanea esta ruta en vivo, sin ningun boton de sync).
const cfgSkillsPathEl = document.getElementById("cfg-skills-path");
const cfgSkillsBrowseEl = document.getElementById("cfg-skills-browse");
const cfgSkillsSaveEl = document.getElementById("cfg-skills-save");
const cfgSkillsStatusEl = document.getElementById("cfg-skills-status");
const cfgSkillsGlobalListEl = document.getElementById("cfg-skills-global-list");
const cfgSkillsLocalListEl = document.getElementById("cfg-skills-local-list");
const cfgLocalSkillNewEl = document.getElementById("cfg-local-skill-new");

async function loadSkillsSettingsPane() {
  try {
    const resp = await apiFetch(`${API_BASE}/api/config/project`);
    const cfg = await resp.json();
    cfgSkillsPathEl.value = cfg.skills_repo_path || "";
  } catch {
    // noop
  }
  await refreshSkillsLists();
}

cfgSkillsBrowseEl.addEventListener("click", () => {
  openNativeFolderPicker(cfgSkillsPathEl.value, (path) => {
    cfgSkillsPathEl.value = path;
  }, cfgSkillsStatusEl);
});

cfgSkillsSaveEl.addEventListener("click", async () => {
  cfgSkillsStatusEl.textContent = "Guardando…";
  try {
    await apiFetchOk(`${API_BASE}/api/skills/repo-path`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: cfgSkillsPathEl.value }),
    });
    cfgSkillsStatusEl.textContent = "Guardado ✓";
    cfgSkillsStatusEl.className = "settings-status ok";
    await refreshSkillsLists();  // la carpeta cambio -- refresca el listado global
  } catch (err) {
    cfgSkillsStatusEl.textContent = friendlyErrorMessage(err);
    cfgSkillsStatusEl.className = "settings-status error";
  }
});

// -- REQ-008: listado global (solo lectura) + local (CRUD) ------------------
// Bug real encontrado en QA manual (2026-09-19, Playwright detecto "Identifier
// '_localSkillsCache' has already been declared" -- SyntaxError que rompia
// TODO app.js como modulo, incluida la inicializacion del splash/ojo de
// bienvenida al final del archivo): ya existia una `_localSkillsCache`
// (mas abajo, seccion del menu "/" del chat, proposito distinto). Nombre
// especifico para esta, del panel de Configuracion, para no volver a
// chocar.
let _localSkillsSettingsCache = [];

// REQ-001 (spec update-skills): invalida las caches del menu "/" del chat
// (`_skillsCache`/`_localSkillsCache`, declaradas mas abajo -- seccion del
// menu "/") ademas de refrescar este panel de Configuracion. BUG REAL
// reportado por el usuario: esas dos caches nunca se invalidaban despues de
// guardar, asi que el panel de Configuracion quedaba al dia pero el menu
// "/" y el panel rapido de Skills (que leen de esas caches via
// getSkillsList()/getLocalSkillsList()) seguian mostrando la lista vieja
// hasta recargar la pagina.
async function refreshSkillsLists() {
  try {
    const resp = await apiFetch(`${API_BASE}/api/skills`);
    const data = await resp.json();
    renderGlobalSkillsList(Array.isArray(data.skills) ? data.skills : []);
  } catch {
    renderGlobalSkillsList([]);
  }
  try {
    const resp = await apiFetch(`${API_BASE}/api/local-skills/${encodeURIComponent(state.sessionId)}`);
    const data = await resp.json();
    _localSkillsSettingsCache = Array.isArray(data.skills) ? data.skills : [];
    renderLocalSkillsList(_localSkillsSettingsCache);
  } catch {
    _localSkillsSettingsCache = [];
    renderLocalSkillsList([]);
  }
  // REQ-001: invalidacion explicita -- la proxima vez que se abra el menu
  // "/" o el panel rapido de Skills, getSkillsList()/getLocalSkillsList()
  // vuelven a pedir la lista real en vez de servir la cache vieja.
  _skillsCache = null;
  _localSkillsCache = null;
}

function renderGlobalSkillsList(skills) {
  cfgSkillsGlobalListEl.innerHTML = "";
  if (!skills.length) {
    cfgSkillsGlobalListEl.innerHTML = '<li class="session-list-empty">Sin skills globales todavía -- configurá una carpeta arriba</li>';
    return;
  }
  for (const s of skills) {
    const li = document.createElement("li");
    const nameSpan = document.createElement("span");
    nameSpan.className = "session-name";
    nameSpan.textContent = s.name;
    nameSpan.title = s.description || s.path || "";
    li.appendChild(nameSpan);
    cfgSkillsGlobalListEl.appendChild(li);
  }
}

function renderLocalSkillsList(skills) {
  cfgSkillsLocalListEl.innerHTML = "";
  if (!skills.length) {
    cfgSkillsLocalListEl.innerHTML = '<li class="session-list-empty">Sin skills locales todavía en esta sesión</li>';
    return;
  }
  for (const s of skills) {
    const li = document.createElement("li");
    const nameSpan = document.createElement("span");
    nameSpan.className = "session-name";
    nameSpan.textContent = s.name;
    nameSpan.title = s.description || "";

    const editBtn = document.createElement("button");
    editBtn.type = "button";
    editBtn.className = "session-rename-btn";
    editBtn.title = "Editar";
    editBtn.textContent = "✎";
    editBtn.onclick = () => openLocalSkillEditor(s);

    const deleteBtn = document.createElement("button");
    deleteBtn.type = "button";
    deleteBtn.className = "session-delete-btn";
    deleteBtn.title = "Eliminar";
    deleteBtn.textContent = "🗑";
    deleteBtn.onclick = () => deleteLocalSkill(s.name);

    li.append(nameSpan, editBtn, deleteBtn);
    cfgSkillsLocalListEl.appendChild(li);
  }
}

// REQ-009: aviso de agentes en uso, MISMO patron ya aplicado a comandos --
// se calcula del lado del frontend (cruzando `_agents` ya cacheado, ver
// refreshAgentsAndList) antes de pedir confirmacion, ya que el backend no
// necesita saberlo para borrar (el aviso es 100% informativo para el
// usuario, el agente queda marcado `broken` solo al proximo GET /api/agents).
function _countAgentsUsingSkill(skillName) {
  if (!Array.isArray(_agents)) return 0;
  return _agents.filter((a) => a.automation_type === "skill" && a.skill_name === skillName).length;
}

async function deleteLocalSkill(name) {
  const affected = _countAgentsUsingSkill(name);
  const message = affected > 0
    ? `¿Eliminar la skill "${name}"? ${affected} agente(s) la usan y quedarán marcados como ` +
      `"configuración inválida" hasta que los edites.`
    : `¿Eliminar la skill "${name}"? Esta acción no se puede deshacer.`;
  const confirmed = await showConfirmModal({ title: "Eliminar skill", message, acceptLabel: "Eliminar" });
  if (!confirmed) return;
  try {
    await apiFetch(
      `${API_BASE}/api/local-skills/${encodeURIComponent(state.sessionId)}/${encodeURIComponent(name)}`,
      { method: "DELETE" }
    );
  } catch (err) {
    console.warn("No se pudo eliminar la skill local:", err);
  }
  await refreshSkillsLists();
}

// -- REQ-008: editor (crear/editar) -----------------------------------------
const localSkillEditorOverlayEl = document.getElementById("local-skill-editor-overlay");
const localSkillEditorTitleEl = document.getElementById("local-skill-editor-title");
const localSkillEditorCloseEl = document.getElementById("local-skill-editor-close");
const localSkillNameEl = document.getElementById("local-skill-name");
const localSkillDescriptionEl = document.getElementById("local-skill-description");
const localSkillContentEl = document.getElementById("local-skill-content");
const localSkillSaveEl = document.getElementById("local-skill-save");
const localSkillCancelEl = document.getElementById("local-skill-cancel");
const localSkillStatusEl = document.getElementById("local-skill-status");
let _editingLocalSkillOriginalName = "";

// Ejemplo precargado (pedido explicito del usuario: "podriamos... una guia
// con ejemplo") -- solo se precarga en modo CREAR (edicion muestra el
// contenido real de la skill, nunca pisa lo que el usuario ya escribio).
const LOCAL_SKILL_EXAMPLE_CONTENT =
  "1. Leé los últimos mensajes de la sesión.\n" +
  "2. Identificá los KPIs o datos clave mencionados.\n" +
  "3. Armá un resumen de máximo 5 bullets, en español, sin tecnicismos innecesarios.";

function openLocalSkillEditor(skill) {
  // REQ-008: `skill.content` viene precargado desde
  // GET /api/local-skills/{id} (ver discover_local_skills_with_body en
  // tools/skill.py) -- sin esto, editar perderia el cuerpo real de la
  // skill cada vez que se abre el editor.
  _editingLocalSkillOriginalName = skill ? skill.name : "";
  localSkillEditorTitleEl.textContent = skill ? `Editar: ${skill.name}` : "Nueva skill local";
  localSkillNameEl.value = skill ? skill.name : "";
  localSkillDescriptionEl.value = skill ? (skill.description || "") : "";
  localSkillContentEl.value = skill ? (skill.content || "") : "";
  localSkillContentEl.placeholder = "";
  localSkillStatusEl.textContent = "";
  localSkillStatusEl.className = "settings-status";
  localSkillEditorOverlayEl.classList.remove("hidden");
}

function closeLocalSkillEditor() {
  localSkillEditorOverlayEl.classList.add("hidden");
}
localSkillEditorCloseEl.addEventListener("click", closeLocalSkillEditor);
localSkillCancelEl.addEventListener("click", closeLocalSkillEditor);
localSkillEditorOverlayEl.addEventListener("click", (e) => {
  if (e.target === localSkillEditorOverlayEl) closeLocalSkillEditor();
});
cfgLocalSkillNewEl.addEventListener("click", () => {
  openLocalSkillEditor(null);
  localSkillContentEl.placeholder = LOCAL_SKILL_EXAMPLE_CONTENT;
});

localSkillSaveEl.addEventListener("click", async () => {
  const name = localSkillNameEl.value.trim();
  const description = localSkillDescriptionEl.value.trim();
  const content = localSkillContentEl.value.trim();
  localSkillStatusEl.textContent = "Guardando…";
  localSkillStatusEl.className = "settings-status";
  try {
    const resp = await apiFetch(`${API_BASE}/api/local-skills`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        session_id: state.sessionId,
        name,
        description,
        content,
        original_name: _editingLocalSkillOriginalName,
      }),
    });
    if (!resp.ok) {
      const errBody = await resp.json().catch(() => ({}));
      throw httpError(resp.status, errBody);
    }
    closeLocalSkillEditor();
    await refreshSkillsLists();
  } catch (err) {
    localSkillStatusEl.textContent = friendlyErrorMessage(err);
    localSkillStatusEl.className = "settings-status error";
  }
});

// =========================================================================
// REQ-009 (spec comandos-slash): tab "Comandos" en Configuracion -- CRUD
// de comandos propios (nombre + prompt), mismo patron que el tab Skills.
// =========================================================================
const cfgCommandNameEl = document.getElementById("cfg-command-name");
const cfgCommandPromptEl = document.getElementById("cfg-command-prompt");
const cfgCommandSaveEl = document.getElementById("cfg-command-save");
const cfgCommandStatusEl = document.getElementById("cfg-command-status");
const cfgCommandListEl = document.getElementById("cfg-command-list");

async function loadCommandsSettingsPane() {
  try {
    // Arquitectura carpeta-por-sesion: los comandos propios listados aca
    // son los de la carpeta de LA SESION ACTUAL, no una carpeta global.
    const resp = await apiFetch(`${API_BASE}/api/commands?session_id=${encodeURIComponent(state.sessionId)}`);
    const data = await resp.json();
    renderCommandsSettingsList(data.custom || []);
  } catch {
    renderCommandsSettingsList([]);
  }
}

function renderCommandsSettingsList(customCommands) {
  cfgCommandListEl.innerHTML = "";
  if (customCommands.length === 0) {
    cfgCommandListEl.innerHTML = '<li class="session-list-empty">Sin comandos propios todavía</li>';
    return;
  }
  for (const c of customCommands) {
    const li = document.createElement("li");
    const nameSpan = document.createElement("span");
    nameSpan.className = "session-name";
    nameSpan.textContent = `/${c.name}`;
    nameSpan.title = c.prompt;

    const editBtn = document.createElement("button");
    editBtn.type = "button";
    editBtn.className = "session-rename-btn";
    editBtn.title = "Editar";
    editBtn.textContent = "✎";
    editBtn.onclick = () => {
      cfgCommandNameEl.value = c.name;
      cfgCommandPromptEl.value = c.prompt;
    };

    const deleteBtn = document.createElement("button");
    deleteBtn.type = "button";
    deleteBtn.className = "session-delete-btn";
    deleteBtn.title = "Eliminar";
    deleteBtn.textContent = "🗑";
    deleteBtn.onclick = async () => {
      await apiFetch(
        `${API_BASE}/api/commands/custom/${encodeURIComponent(c.name)}?session_id=${encodeURIComponent(state.sessionId)}`,
        { method: "DELETE" }
      );
      loadCommandsSettingsPane();
    };

    li.append(nameSpan, editBtn, deleteBtn);
    cfgCommandListEl.appendChild(li);
  }
}

cfgCommandSaveEl.addEventListener("click", async () => {
  const name = cfgCommandNameEl.value.trim();
  const prompt = cfgCommandPromptEl.value.trim();
  cfgCommandStatusEl.textContent = "Guardando…";
  cfgCommandStatusEl.className = "settings-status";
  try {
    const resp = await apiFetch(`${API_BASE}/api/commands/custom`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: state.sessionId, name, prompt }),
    });
    if (!resp.ok) {
      const errBody = await resp.json().catch(() => ({}));
      throw httpError(resp.status, errBody);
    }
    const data = await resp.json();
    renderCommandsSettingsList(data.custom || []);
    cfgCommandNameEl.value = "";
    cfgCommandPromptEl.value = "";
    cfgCommandStatusEl.textContent = "Guardado ✓";
    cfgCommandStatusEl.className = "settings-status ok";
  } catch (err) {
    cfgCommandStatusEl.textContent = friendlyErrorMessage(err);
    cfgCommandStatusEl.className = "settings-status error";
  }
});

// =========================================================================
// Explorador generico: filesystem local (/api/fs/*) o SharePoint
// (/api/sharepoint/*), reusado por las 3 pestañas de Configuracion que
// necesitan elegir una carpeta.
// =========================================================================
const explorerState = { mode: "local", currentPath: "", driveId: "", driveName: "", crumbs: [], onSelect: null };

// Pedido explicito del usuario (2026-09-09): navegar carpeta por carpeta
// en el explorador interno de arriba (openExplorer) es tedioso comparado
// con el Explorador de Windows real -- crear/eliminar/ir atras no es
// fluido ahi. Como Vision corre 100% local (backend y navegador en la
// MISMA maquina), el backend puede abrir el dialogo NATIVO de Windows
// (ver /api/fs/native-picker en main.py, tkinter.filedialog) y devolver
// la ruta absoluta real -- algo que un <input webkitdirectory> del
// navegador NO puede hacer por seguridad del propio browser. Se usa para
// las 2 carpetas que son filesystem LOCAL real (proyecto de sesion,
// skills); SharePoint sigue con el explorador interno de arriba porque
// ahi no hay "carpeta de Windows" que abrir.
async function openNativeFolderPicker(currentPath, onSelect, statusEl) {
  if (statusEl) {
    statusEl.textContent = "Abriendo Explorador de Windows…";
    statusEl.className = "settings-status";
  }
  try {
    const params = currentPath ? `?initial_path=${encodeURIComponent(currentPath)}` : "";
    const resp = await apiFetch(`${API_BASE}/api/fs/native-picker${params}`);
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw httpError(resp.status, err);
    }
    const data = await resp.json();
    if (statusEl) statusEl.textContent = "";
    if (!data.path) return; // usuario cancelo el dialogo -- no es un error
    onSelect(data.path);
  } catch (err) {
    if (statusEl) {
      statusEl.textContent = friendlyErrorMessage(err);
      statusEl.className = "settings-status error";
    }
  }
}

// spec programador-alarmas 0.12.0: variante para elegir un ARCHIVO (con
// filtro de ejecutables/scripts) alimentando el campo "Archivo a ejecutar"
// de una tarea -- mismo endpoint nativo (tkinter) pero con mode=file.
async function openNativePicker(initialPath, mode, onSelect) {
  const params = `?mode=${encodeURIComponent(mode)}` + (initialPath ? `&initial_path=${encodeURIComponent(initialPath)}` : "");
  try {
    const resp = await apiFetch(`${API_BASE}/api/fs/native-picker${params}`);
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw httpError(resp.status, err);
    }
    const data = await resp.json();
    if (data.path) onSelect(data.path);
  } catch (err) {
    console.warn("No se pudo abrir el explorador nativo:", err);
  }
}

function openExplorer({ mode, title, onSelect }) {
  explorerState.mode = mode;
  explorerState.onSelect = onSelect;
  explorerState.currentPath = "";
  explorerState.driveId = "";
  explorerState.driveName = "";
  explorerState.crumbs = [];

  explorerTitleEl.textContent = title;
  explorerEyebrowEl.textContent = mode === "local" ? "SISTEMA DE ARCHIVOS" : "SITIO SHAREPOINT";
  explorerStatusEl.textContent = "";
  explorerOverlayEl.classList.remove("hidden");

  // El boton "+ Nueva carpeta" solo aplica al modo local (crear carpetas
  // en SharePoint via Graph requeriria otra logica distinta, fuera de
  // alcance por ahora) -- se oculta por completo en modo sharepoint.
  explorerNewFolderBtnEl.classList.toggle("hidden", mode !== "local");
  closeNewFolderRow();

  if (mode === "local") loadLocalRoots();
  else loadSharepointDrives();
}
function closeExplorer() {
  explorerOverlayEl.classList.add("hidden");
}
explorerCloseEl.addEventListener("click", closeExplorer);
explorerOverlayEl.addEventListener("click", (e) => {
  if (e.target === explorerOverlayEl) closeExplorer();
});

function renderBreadcrumb() {
  explorerBreadcrumbEl.innerHTML = "";
  explorerState.crumbs.forEach((crumb, idx) => {
    const span = document.createElement("span");
    span.className = "crumb";
    span.textContent = crumb.label;
    span.onclick = () => crumb.onClick();
    explorerBreadcrumbEl.appendChild(span);
    if (idx < explorerState.crumbs.length - 1) {
      explorerBreadcrumbEl.appendChild(document.createTextNode("  /  "));
    }
  });
}

function renderExplorerList(items, onItemClick) {
  explorerListEl.innerHTML = "";
  if (!items || items.length === 0) {
    explorerListEl.innerHTML = '<li class="explorer-list-empty">Carpeta vacía o sin subcarpetas visibles</li>';
    return;
  }
  items.forEach((item) => {
    const li = document.createElement("li");
    const icon = document.createElement("span");
    icon.className = "ex-icon";
    icon.textContent = item.isFolder ? "📁" : "📄";
    const name = document.createElement("span");
    name.className = "ex-name";
    name.textContent = item.name;
    li.append(icon, name);
    if (item.meta) {
      const meta = document.createElement("span");
      meta.className = "ex-meta";
      meta.textContent = item.meta;
      li.appendChild(meta);
    }
    li.onclick = () => onItemClick(item);
    explorerListEl.appendChild(li);
  });
}

async function loadLocalRoots() {
  closeNewFolderRow();
  explorerState.currentPath = ""; // "Unidades" no es una carpeta real -- no se puede crear nada aca
  explorerState.crumbs = [{ label: "Unidades", onClick: loadLocalRoots }];
  renderBreadcrumb();
  try {
    const resp = await apiFetch(`${API_BASE}/api/fs/roots`);
    if (!resp.ok) throw await httpErrorFromResponse(resp);
    const data = await resp.json();
    renderExplorerList(
      (data.roots || []).map((r) => ({ name: r.name, isFolder: true, path: r.path })),
      (item) => loadLocalChildren(item.path)
    );
  } catch (err) {
    explorerStatusEl.textContent = friendlyErrorMessage(err);
    explorerStatusEl.className = "settings-status error";
  }
}

async function loadLocalChildren(path) {
  closeNewFolderRow();
  explorerState.currentPath = path;
  try {
    const resp = await apiFetch(`${API_BASE}/api/fs/children?path=${encodeURIComponent(path)}`);
    if (!resp.ok) throw await httpErrorFromResponse(resp);
    const data = await resp.json();

    const parts = path.replace(/\\/g, "/").split("/").filter(Boolean);
    explorerState.crumbs = [{ label: "Unidades", onClick: loadLocalRoots }];
    let acc = "";
    parts.forEach((part, idx) => {
      acc += (idx === 0 ? part + "/" : part + "/");
      const thisPath = acc;
      explorerState.crumbs.push({ label: part, onClick: () => loadLocalChildren(thisPath) });
    });
    renderBreadcrumb();

    renderExplorerList(
      (data.children || []).map((c) => ({ name: c.name, isFolder: true, path: c.path })),
      (item) => loadLocalChildren(item.path)
    );
  } catch (err) {
    explorerStatusEl.textContent = friendlyErrorMessage(err);
    explorerStatusEl.className = "settings-status error";
  }
}

// -------------------------------------------------------------------------
// "+ Nueva carpeta": crea una subcarpeta DENTRO de la carpeta que se esta
// navegando ahora mismo (explorerState.currentPath) -- nunca en la raiz de
// unidades ("Unidades" no tiene un path real donde crear nada). Usa una
// fila inline con input en vez de prompt() nativo, para mantener el mismo
// lenguaje visual del resto de modales propios del proyecto.
// -------------------------------------------------------------------------
function closeNewFolderRow() {
  const existing = document.getElementById("explorer-new-folder-row");
  if (existing) existing.remove();
}

function openNewFolderRow() {
  if (!explorerState.currentPath) {
    explorerStatusEl.textContent = "Navegá dentro de una carpeta primero.";
    explorerStatusEl.className = "settings-status error";
    return;
  }
  closeNewFolderRow(); // evita duplicar si ya estaba abierta

  const row = document.createElement("div");
  row.id = "explorer-new-folder-row";
  row.className = "explorer-new-folder-row";

  const input = document.createElement("input");
  input.type = "text";
  input.placeholder = "Nombre de la carpeta nueva";

  const createBtn = document.createElement("button");
  createBtn.type = "button";
  createBtn.className = "acc-action-btn";
  createBtn.textContent = "Crear";

  const cancelBtn = document.createElement("button");
  cancelBtn.type = "button";
  cancelBtn.className = "acc-action-btn secondary";
  cancelBtn.textContent = "Cancelar";

  row.append(input, createBtn, cancelBtn);
  explorerListEl.parentElement.insertBefore(row, explorerListEl);
  input.focus();

  const submit = () => submitNewFolder(input.value);
  createBtn.onclick = submit;
  cancelBtn.onclick = closeNewFolderRow;
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); submit(); }
    if (e.key === "Escape") closeNewFolderRow();
  });
}

async function submitNewFolder(name) {
  const folderName = (name || "").trim();
  if (!folderName) return;

  try {
    const resp = await apiFetch(`${API_BASE}/api/fs/create-folder`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ parent_path: explorerState.currentPath, folder_name: folderName }),
    });
    if (!resp.ok) {
      const errBody = await resp.json().catch(() => ({}));
      throw httpError(resp.status, errBody);
    }
    closeNewFolderRow();
    explorerStatusEl.textContent = "Carpeta creada ✓";
    explorerStatusEl.className = "settings-status ok";
    // Recarga la carpeta actual para que la nueva subcarpeta aparezca en
    // el listado, sin perder la posicion de navegacion (breadcrumb).
    await loadLocalChildren(explorerState.currentPath);
  } catch (err) {
    explorerStatusEl.textContent = friendlyErrorMessage(err);
    explorerStatusEl.className = "settings-status error";
  }
}

explorerNewFolderBtnEl.addEventListener("click", openNewFolderRow);

async function loadSharepointDrives() {
  explorerState.crumbs = [{ label: "Bibliotecas", onClick: loadSharepointDrives }];
  renderBreadcrumb();
  try {
    const resp = await apiFetch(`${API_BASE}/api/sharepoint/drives`);
    if (!resp.ok) throw await httpErrorFromResponse(resp);
    const data = await resp.json();
    renderExplorerList(
      (data.drives || []).map((d) => ({ name: d.name, isFolder: true, driveId: d.id })),
      (item) => loadSharepointChildren(item.driveId, item.name, null, "")
    );
  } catch (err) {
    explorerStatusEl.textContent = friendlyErrorMessage(err);
    explorerStatusEl.className = "settings-status error";
  }
}

async function loadSharepointChildren(driveId, driveName, itemId, folderPath) {
  explorerState.driveId = driveId;
  explorerState.driveName = driveName;
  explorerState.currentPath = folderPath;
  explorerState.currentFolderId = itemId;

  try {
    const url = itemId
      ? `${API_BASE}/api/sharepoint/children?drive_id=${encodeURIComponent(driveId)}&item_id=${encodeURIComponent(itemId)}`
      : `${API_BASE}/api/sharepoint/children?drive_id=${encodeURIComponent(driveId)}`;
    const resp = await apiFetch(url);
    if (!resp.ok) throw await httpErrorFromResponse(resp);
    const data = await resp.json();

    explorerState.crumbs = [
      { label: "Bibliotecas", onClick: loadSharepointDrives },
      { label: driveName, onClick: () => loadSharepointChildren(driveId, driveName, null, "") },
    ];
    if (folderPath) {
      const parts = folderPath.split("/").filter(Boolean);
      // Solo se muestra el path completo como ultimo crumb (no navegable
      // paso a paso sin id de cada nivel intermedio) -- suficiente para
      // ubicarse; navegar hacia atras se hace desde "Bibliotecas"/raiz.
      explorerState.crumbs.push({ label: parts.join("/"), onClick: () => {} });
    }
    renderBreadcrumb();

    renderExplorerList(
      (data.children || [])
        .filter((c) => c.is_folder)
        .map((c) => ({
          name: c.name,
          isFolder: true,
          itemId: c.id,
          meta: c.child_count != null ? `${c.child_count} items` : "",
        })),
      (item) => {
        const newPath = folderPath ? `${folderPath}/${item.name}` : item.name;
        loadSharepointChildren(driveId, driveName, item.itemId, newPath);
      }
    );
  } catch (err) {
    explorerStatusEl.textContent = friendlyErrorMessage(err);
    explorerStatusEl.className = "settings-status error";
  }
}

explorerSelectBtnEl.addEventListener("click", () => {
  if (!explorerState.onSelect) return;
  if (explorerState.mode === "local") {
    if (!explorerState.currentPath) {
      explorerStatusEl.textContent = "Navegá a una carpeta primero.";
      explorerStatusEl.className = "settings-status error";
      return;
    }
    explorerState.onSelect(explorerState.currentPath);
  } else {
    if (!explorerState.driveId) {
      explorerStatusEl.textContent = "Elegí una biblioteca primero.";
      explorerStatusEl.className = "settings-status error";
      return;
    }
    explorerState.onSelect(explorerState.currentPath, {
      driveId: explorerState.driveId,
      driveName: explorerState.driveName,
      folderId: explorerState.currentFolderId || "",
      folderPath: explorerState.currentPath || "",
    });
  }
  closeExplorer();
});

// =========================================================================
// Comando "/" en el chat: al escribir "/" al inicio del input (o despues de
// un espacio), muestra un menu con 2 secciones (spec comandos-slash):
//   - "Comandos" (REQ-001): sistema (init/export/rename/undo/compact/
//     share/unshare/exit) + propios del usuario -- elegir uno ABRE UN
//     MODAL (REQ-002), nunca inserta texto.
//   - "Skills" (comportamiento SIN CAMBIOS): inserta "/skill <nombre>" en
//     el input para que el LLM lo interprete via su tool `skill`.
// =========================================================================
let _skillsCache = null;
let _localSkillsCache = null;
let _commandsCache = null;
let slashActiveIndex = -1;

async function getSkillsList() {
  if (_skillsCache) return _skillsCache;
  try {
    const resp = await apiFetch(`${API_BASE}/api/skills`);
    const data = await resp.json();
    _skillsCache = data.skills || [];
  } catch {
    _skillsCache = [];
  }
  return _skillsCache;
}

// Skills LOCALES de la carpeta de proyecto de ESTA sesion (`skills/`,
// arquitectura carpeta-por-sesion) -- separado de getSkillsList (siempre
// globales) a pedido explicito del usuario: "/local-skills" es un comando
// nuevo y distinto de "/skills". Sin cache larga (a diferencia de
// getSkillsList) porque puede cambiar si el usuario cambia de sesion sin
// recargar la pagina.
async function getLocalSkillsList() {
  try {
    const resp = await apiFetch(`${API_BASE}/api/local-skills/${encodeURIComponent(state.sessionId)}`);
    const data = await resp.json();
    _localSkillsCache = data.skills || [];
  } catch {
    _localSkillsCache = [];
  }
  return _localSkillsCache;
}

// REQ-001: sin cache larga (a diferencia de skills) -- los comandos
// propios pueden crearse/borrarse desde Configuracion mientras el menu
// "/" ya esta en uso, y la lista es chica (fetch barato).
async function getCommandsList() {
  try {
    const resp = await apiFetch(`${API_BASE}/api/commands?session_id=${encodeURIComponent(state.sessionId)}`);
    const data = await resp.json();
    _commandsCache = data;
  } catch {
    _commandsCache = { system: [], custom: [] };
  }
  return _commandsCache;
}

function detectSlashQuery(value, cursorPos) {
  const upToCursor = value.slice(0, cursorPos);
  const match = upToCursor.match(/(?:^|\s)\/(\S*)$/);
  return match ? match[1] : null;
}

async function handleSlashInput() {
  const query = detectSlashQuery(inputEl.value, inputEl.selectionStart);
  if (query === null) {
    slashMenuEl.classList.add("hidden");
    return;
  }

  const [commandsData, skills, localSkills] = await Promise.all([
    getCommandsList(),
    getSkillsList(),
    getLocalSkillsList(),
  ]);
  const lowerQuery = query.toLowerCase();
  const allCommands = [
    ...commandsData.system.map((c) => ({ ...c, isCustom: false })),
    ...commandsData.custom.map((c) => ({ ...c, isCustom: true, kind: "custom" })),
  ];
  const filteredCommands = allCommands.filter((c) => c.name.toLowerCase().includes(lowerQuery));
  const filteredSkills = skills.filter((s) => s.name.toLowerCase().includes(lowerQuery));
  const filteredLocalSkills = localSkills.filter((s) => s.name.toLowerCase().includes(lowerQuery));

  slashMenuEl.innerHTML = "";
  let hasAnyItem = false;

  // REQ-001: seccion "Comandos" PRIMERO (sistema + propios), con un
  // encabezado de seccion -- distinto de "Skills" en icono y accion.
  if (filteredCommands.length > 0) {
    hasAnyItem = true;
    const header = document.createElement("div");
    header.className = "slash-section-header";
    header.textContent = "Comandos";
    slashMenuEl.appendChild(header);

    filteredCommands.slice(0, 10).forEach((c) => {
      const item = document.createElement("div");
      item.className = "slash-item";
      const nameEl = document.createElement("div");
      nameEl.className = "slash-name";
      // Icono por tipo: comando propio (usuario) vs sistema funcional vs
      // placeholder -- distincion visual pedida en la spec (UI/UX §9).
      const icon = c.isCustom ? "✎" : c.kind === "unavailable" ? "○" : "⚡";
      nameEl.textContent = `${icon} /${c.name}`;
      const descEl = document.createElement("div");
      descEl.className = "slash-desc";
      descEl.textContent = c.description || "Comando propio";
      item.append(nameEl, descEl);
      item.onclick = () => selectSlashCommand(c);
      slashMenuEl.appendChild(item);
    });
  }

  // Seccion "Skills" -- SIN CAMBIOS de comportamiento (inserta texto).
  if (filteredSkills.length > 0) {
    hasAnyItem = true;
    const header = document.createElement("div");
    header.className = "slash-section-header";
    header.textContent = "Skills";
    slashMenuEl.appendChild(header);

    filteredSkills.slice(0, 10).forEach((s) => {
      const item = document.createElement("div");
      item.className = "slash-item";
      const nameEl = document.createElement("div");
      nameEl.className = "slash-name";
      nameEl.textContent = `/${s.name}`;
      const descEl = document.createElement("div");
      descEl.className = "slash-desc";
      descEl.textContent = s.description || "";
      item.append(nameEl, descEl);
      item.onclick = () => insertSlashSkill(s.name);
      slashMenuEl.appendChild(item);
    });
  }

  // Seccion "Skills locales" -- arquitectura carpeta-por-sesion: skills
  // propias de la carpeta de proyecto de ESTA sesion (skills/), comando
  // NUEVO y separado de "Skills" (que siempre son las globales del
  // usuario). Mismo comportamiento de insercion (via tool `skill`, que ya
  // busca en ambas fuentes) -- solo cambia de donde vienen los datos.
  if (filteredLocalSkills.length > 0) {
    hasAnyItem = true;
    const header = document.createElement("div");
    header.className = "slash-section-header";
    header.textContent = "Skills locales";
    slashMenuEl.appendChild(header);

    filteredLocalSkills.slice(0, 10).forEach((s) => {
      const item = document.createElement("div");
      item.className = "slash-item";
      const nameEl = document.createElement("div");
      nameEl.className = "slash-name";
      nameEl.textContent = `📁 /${s.name}`;
      const descEl = document.createElement("div");
      descEl.className = "slash-desc";
      descEl.textContent = s.description || "";
      item.append(nameEl, descEl);
      item.onclick = () => insertSlashSkill(s.name);
      slashMenuEl.appendChild(item);
    });
  }

  if (!hasAnyItem) {
    slashMenuEl.innerHTML = '<div class="slash-menu-empty">Sin comandos ni skills que coincidan</div>';
  } else {
    const firstItem = slashMenuEl.querySelector(".slash-item");
    if (firstItem) firstItem.classList.add("active");
    slashActiveIndex = 0;
  }
  slashMenuEl.classList.remove("hidden");
}

// =========================================================================
// REQ-002 a REQ-008 (spec comandos-slash): modal centrado especifico por
// comando -- a diferencia de una skill, elegir un comando NO inserta
// texto en el input. `#command-modal-body` se arma dinamicamente segun
// `kind`: confirm / export / rename / unavailable / custom.
// =========================================================================
const commandModalOverlayEl = document.getElementById("command-modal-overlay");
const commandModalTitleEl = document.getElementById("command-modal-title");
const commandModalBodyEl = document.getElementById("command-modal-body");
const commandModalCloseEl = document.getElementById("command-modal-close");

function closeCommandModal() {
  commandModalOverlayEl.classList.add("hidden");
  commandModalBodyEl.innerHTML = "";
}
commandModalCloseEl.addEventListener("click", closeCommandModal);
commandModalOverlayEl.addEventListener("click", (e) => {
  if (e.target === commandModalOverlayEl) closeCommandModal();
});

function selectSlashCommand(command) {
  // Limpia el "/query" tipeado del input -- un comando nunca deja texto
  // pendiente (a diferencia de una skill, que SI inserta "/skill nombre").
  const value = inputEl.value;
  const cursorPos = inputEl.selectionStart;
  const upToCursor = value.slice(0, cursorPos);
  const cleaned = upToCursor.replace(/(?:^|\s)\/(\S*)$/, (m) => (m.startsWith(" ") ? " " : ""));
  inputEl.value = cleaned + value.slice(cursorPos);
  slashMenuEl.classList.add("hidden");

  renderCommandModal(command);
}

function renderCommandModal(command) {
  commandModalTitleEl.textContent = `/${command.name}`;
  commandModalBodyEl.innerHTML = "";

  if (command.kind === "custom") {
    // REQ-008 (comandos-slash) + REQ-008 (carpeta-por-sesion, mismo
    // numero de REQ en specs distintas, no confundir): un comando propio
    // vive como archivo DENTRO de la carpeta de la sesion -- sin carpeta
    // asignada no puede ni haberse creado, pero se valida igual por si la
    // sesion se quedo sin carpeta entre que se abrio el menu y se
    // clickeo (ej. se reasigno desde otra pestana).
    validateProjectFolderOrPrompt().then((ok) => {
      if (ok) sendMessage(command.prompt);
    });
    return;
  }

  if (command.kind === "unavailable") {
    // REQ-006: placeholder honesto -- nunca finge ejecutar algo.
    const p = document.createElement("p");
    p.className = "confirm-message";
    p.textContent = command.unavailable_reason;
    commandModalBodyEl.appendChild(p);
    const actions = document.createElement("div");
    actions.className = "confirm-actions";
    const okBtn = document.createElement("button");
    okBtn.type = "button";
    okBtn.className = "acc-action-btn";
    okBtn.textContent = "Entendido";
    okBtn.onclick = closeCommandModal;
    actions.appendChild(okBtn);
    commandModalBodyEl.appendChild(actions);
    commandModalOverlayEl.classList.remove("hidden");
    return;
  }

  if (command.kind === "confirm") {
    // REQ-003 (/init) y REQ-007 (/exit) comparten este tipo -- ambos son
    // "confirmar y ejecutar", solo cambia LA ACCION disparada.
    const p = document.createElement("p");
    p.className = "confirm-message";
    p.textContent = command.confirm_message;
    commandModalBodyEl.appendChild(p);
    const actions = document.createElement("div");
    actions.className = "confirm-actions";
    const cancelBtn = document.createElement("button");
    cancelBtn.type = "button";
    cancelBtn.className = "acc-action-btn secondary";
    cancelBtn.textContent = "Cancelar";
    cancelBtn.onclick = closeCommandModal;
    const acceptBtn = document.createElement("button");
    acceptBtn.type = "button";
    acceptBtn.className = "acc-action-btn";
    acceptBtn.textContent = "Confirmar";
    acceptBtn.onclick = async () => {
      closeCommandModal();
      if (command.action === "new_session") {
        // REQ-007: /exit == "+ Nueva sesion" (nunca cierra la pestaña).
        activateSession(crypto.randomUUID());
        return;
      }
      if (command.action === "compact") {
        // spec gestion-contexto-conversacion REQ-002: accion manual real
        // sobre session.messages, sin crear un turno de chat artificial.
        try {
          const resp = await apiFetch(
            `${API_BASE}/api/commands/compact/${state.sessionId}`,
            { method: "POST" }
          );
          const data = await resp.json();
          if (!resp.ok) {
            throw httpError(resp.status, data);
          }
          appendMessage(
            "system-note",
            data.compacted
              ? "Historial compactado."
              : "No había suficiente historial para compactar.",
            { persist: false }
          );
refreshContextUsage();
        } catch (err) {
          appendMessage(
            "system-note",
            `No se pudo resumir la conversación. ${friendlyErrorMessage(err)}`,
            { persist: false }
          );
        }
        return;
      }
      if (command.action === "qa_audit") {
        // spec bateria-qa-integracion REQ-004: corre la bateria de QA y
        // vuelca los FAIL / SIN COBERTURA como tareas pending en /todo.
        try {
          const resp = await apiFetch(
            `${API_BASE}/api/commands/qa-audit/${state.sessionId}`,
            { method: "POST" }
          );
          const data = await resp.json();
          if (!resp.ok) {
            throw httpError(resp.status, data);
          }
          appendMessage(
            "system-note",
            data.added_todos > 0
              ? `QA: ${data.issues.length} hallazgo(s), ${data.added_todos} cargada(s) en la lista de tareas.`
              : "QA: batería en verde, sin tareas nuevas.",
            { persist: false }
          );
        } catch (err) {
          appendMessage(
            "system-note",
            `No se pudo correr la batería QA. ${friendlyErrorMessage(err)}`,
            { persist: false }
          );
        }
        return;
      }
      if (command.name === "init") {
        // REQ-003: el prompt de /init se manda como turno de chat NORMAL
        // -- el LLM lo ejecuta con sus tools ya disponibles.
        try {
          const resp = await apiFetch(`${API_BASE}/api/commands/init-prompt`);
          const data = await resp.json();
          sendMessage(data.prompt);
        } catch (err) {
          console.warn("No se pudo obtener el prompt de /init:", err);
        }
      }
    };
    actions.append(cancelBtn, acceptBtn);
    commandModalBodyEl.appendChild(actions);
    commandModalOverlayEl.classList.remove("hidden");
    return;
  }

  if (command.kind === "export") {
    // Bug real reportado por el usuario (2026-09-08): antes esto navegaba
    // directo a una URL con Content-Disposition: attachment -- el
    // navegador podia disparar SU PROPIO dialogo nativo "Guardar como"
    // (segun config de descargas del usuario) incluso con la sesion YA
    // con carpeta asignada, contradiciendo el proposito de guardar
    // automatico sin preguntar. Ahora usa fetch() al endpoint (que
    // escribe DIRECTO a disco del lado del backend, ver
    // export_session_command_endpoint en main.py) y muestra la ruta real
    // en el chat -- sin ningun dialogo cuando ya hay carpeta asignada.
    const p = document.createElement("p");
    p.className = "confirm-message";
    p.textContent = command.confirm_message;
    commandModalBodyEl.appendChild(p);
    const actions = document.createElement("div");
    actions.className = "confirm-actions";
    const cancelBtn = document.createElement("button");
    cancelBtn.type = "button";
    cancelBtn.className = "acc-action-btn secondary";
    cancelBtn.textContent = "Cancelar";
    cancelBtn.onclick = closeCommandModal;
    const exportBtn = document.createElement("button");
    exportBtn.type = "button";
    exportBtn.className = "acc-action-btn";
    exportBtn.textContent = "Exportar";
    exportBtn.onclick = () => {
      closeCommandModal();
      exportCurrentSession();
    };
    actions.append(cancelBtn, exportBtn);
    commandModalBodyEl.appendChild(actions);
    commandModalOverlayEl.classList.remove("hidden");
    return;
  }

if (command.kind === "loop") {
    // spec modo-plan-loop REQ-010: mensaje dinamico con la cantidad real
    // de tareas pendientes -- a diferencia de "confirm" (texto fijo), acá
    // hace falta consultar session.todos antes de armar el modal.
    renderLoopCommandModal();
    return;
  }

  if (command.kind === "clear") {
    renderClearConversationModal();
    return;
  }

  if (command.kind === "local-skills") {
    // Arquitectura carpeta-por-sesion: modal de solo-lectura listando las
    // skills de `skills/` de la carpeta de proyecto de esta sesion.
    renderLocalSkillsCommandModal();
    return;
  }

  if (command.kind === "rename") {
    // REQ-005: input prellenado -- tercera via de entrada a renombrar,
    // reusa el MISMO endpoint que ya construyo la spec gestion-sesiones.
    const currentName = document.getElementById("meta-session-name")?.textContent || "";
    const label = document.createElement("label");
    label.textContent = "Nuevo nombre de la sesión";
    label.style.display = "block";
    label.style.marginBottom = "8px";
    const input = document.createElement("input");
    input.type = "text";
    input.value = currentName;
    input.maxLength = SESSION_NAME_MAX_LEN;
    input.className = "settings-field-input";
    input.style.width = "100%";
    input.style.marginBottom = "12px";
    commandModalBodyEl.append(label, input);

    const actions = document.createElement("div");
    actions.className = "confirm-actions";
    const cancelBtn = document.createElement("button");
    cancelBtn.type = "button";
    cancelBtn.className = "acc-action-btn secondary";
    cancelBtn.textContent = "Cancelar";
    cancelBtn.onclick = closeCommandModal;
    const saveBtn = document.createElement("button");
    saveBtn.type = "button";
    saveBtn.className = "acc-action-btn";
    saveBtn.textContent = "Guardar";
    saveBtn.onclick = async () => {
      await commitSessionRename(state.sessionId, input.value, currentName);
      closeCommandModal();
    };
    actions.append(cancelBtn, saveBtn);
    commandModalBodyEl.appendChild(actions);
    commandModalOverlayEl.classList.remove("hidden");
    input.focus();
    input.select();
    return;
  }
}

// spec modo-plan-loop REQ-010: modal de /loop -- consulta session.todos
// para armar el mensaje "N pendientes" ANTES de mostrar el modal (a
// diferencia de "confirm", que usa un confirm_message fijo del backend).
async function renderLoopCommandModal() {
  // Reglas de negocio: /loop solo tiene sentido en Modo Activo -- en
  // Modo Plan no hay tools mutantes disponibles para ejecutar ninguna
  // tarea, asi que se informa en vez de dejar arrancar algo que no puede
  // avanzar.
  if (state.mode === "plan") {
    const p = document.createElement("p");
    p.className = "confirm-message";
    p.textContent =
      "El loop necesita Modo Activo para poder ejecutar tareas — cambiá de modo (Tab o el chip MODO) e intentá de nuevo.";
    commandModalBodyEl.appendChild(p);
    const actions = document.createElement("div");
    actions.className = "confirm-actions";
    const okBtn = document.createElement("button");
    okBtn.type = "button";
    okBtn.className = "acc-action-btn";
    okBtn.textContent = "Entendido";
    okBtn.onclick = closeCommandModal;
    actions.appendChild(okBtn);
    commandModalBodyEl.appendChild(actions);
    commandModalOverlayEl.classList.remove("hidden");
    return;
  }

  let pendingCount = 0;
  try {
    const resp = await apiFetch(`${API_BASE}/api/todos/${state.sessionId}`);
    const data = await resp.json();
    pendingCount = (data.todos || []).filter((t) => t.status === "pending").length;
  } catch (err) {
    console.warn("No se pudo consultar la lista de tareas para /loop:", err);
  }

  const p = document.createElement("p");
  p.className = "confirm-message";
  p.textContent =
    pendingCount > 0
      ? `Se va a ejecutar automáticamente la lista de tareas actual (${pendingCount} pendiente${pendingCount === 1 ? "" : "s"}). Se detendrá si necesita tu ayuda.`
      : "No hay tareas pendientes en la lista actual — armá un plan con la tool de tareas antes de iniciar el loop.";
commandModalBodyEl.appendChild(p);

  // REQ-011: check "Modo auto" -- Vision auto-selecciona las opciones
  // "(Recomendada)" y corre mas desatendido; pausa + alarma igual si hay
  // "(Riesgo)" en alguna opcion o ninguna es "(Recomendada)".
  const autoWrap = document.createElement("label");
  autoWrap.className = "settings-checkbox-label command-modal-check";
  const autoChk = document.createElement("input");
  autoChk.type = "checkbox";
  autoChk.checked = _loopAutoPref; // arranca con la preferencia del panel de accesos rapidos
  autoWrap.appendChild(autoChk);
  autoWrap.appendChild(
    document.createTextNode(" Modo auto (Vision decide las opciones recomendadas, sin detenerse)")
  );
  commandModalBodyEl.appendChild(autoWrap);

  const actions = document.createElement("div");
  actions.className = "confirm-actions";
  const cancelBtn = document.createElement("button");
  cancelBtn.type = "button";
  cancelBtn.className = "acc-action-btn secondary";
  cancelBtn.textContent = "Cancelar";
  cancelBtn.onclick = closeCommandModal;
  actions.appendChild(cancelBtn);

  if (pendingCount > 0) {
    const confirmBtn = document.createElement("button");
    confirmBtn.type = "button";
    confirmBtn.className = "acc-action-btn";
    confirmBtn.textContent = "Confirmar";
    confirmBtn.onclick = () => {
      closeCommandModal();
      startLoop(autoChk.checked);
    };
    actions.appendChild(confirmBtn);
  }

  commandModalBodyEl.appendChild(actions);
  commandModalOverlayEl.classList.remove("hidden");
}

// Clear conversacion (pedido del usuario 2026-09): borra TODA la conversacion
// de la sesion (preguntas y respuestas, viejas y nuevas). Modal de confirmacion
// porque es destructivo; al confirmar llama POST /api/session/{id}/clear y
// resetea el DOM, la caché de localStorage y la cola.
function renderClearConversationModal() {
  const p = document.createElement("p");
  p.className = "confirm-message";
  p.textContent =
    "Se va a borrar TODA la conversación de esta sesión (preguntas y respuestas, viejas y nuevas). Esta acción no se puede deshacer.";
  commandModalBodyEl.appendChild(p);

  const actions = document.createElement("div");
  actions.className = "confirm-actions";
  const cancelBtn = document.createElement("button");
  cancelBtn.type = "button";
  cancelBtn.className = "acc-action-btn secondary";
  cancelBtn.textContent = "Cancelar";
  cancelBtn.onclick = closeCommandModal;
  actions.appendChild(cancelBtn);

  const confirmBtn = document.createElement("button");
  confirmBtn.type = "button";
  confirmBtn.className = "acc-action-btn danger";
  confirmBtn.textContent = "Borrar conversación";
  confirmBtn.onclick = () => {
    closeCommandModal();
    clearConversation();
  };
  actions.appendChild(confirmBtn);

  commandModalBodyEl.appendChild(actions);
  commandModalOverlayEl.classList.remove("hidden");
}

async function clearConversation() {
  try {
    const resp = await apiFetch(`${API_BASE}/api/session/${state.sessionId}/clear`, {
      method: "POST",
    });
    if (!resp.ok) {
      const data = await resp.json().catch(() => ({}));
      appendMessage("error", `No se pudo limpiar la conversación. ${httpError(resp.status, data).message}`);
      return;
    }
    // Reset del estado visible: DOM, caché de historial y cola de turnos.
    clearMessagesDom();
    chatHistory.length = 0;
    persistChatHistory();
    if (typeof queuedTurns !== "undefined" && queuedTurns.clear) queuedTurns.clear();
    appendMessage("system-note", "Conversación de la sesión borrada.");
  } catch (err) {
    appendMessage("error", `No se pudo limpiar la conversación. ${friendlyErrorMessage(err)}`, { errorKind: isOfflineError(err) ? "offline" : "other" });
  }
}

// Arquitectura carpeta-por-sesion: modal de solo-lectura del comando
// "/local-skills" -- lista las skills de `skills/` dentro de la carpeta
// de proyecto de ESTA sesion (distinto de la seccion "Skills" del menu
// "/", que siempre muestra las globales del usuario).
async function renderLocalSkillsCommandModal() {
  // spec carpeta-por-sesion REQ-008: sin carpeta asignada no hay
  // 'skills/' donde buscar -- se avisa el motivo real en vez de mostrar
  // "sin skills locales" (que sugeriria que la carpeta existe pero esta
  // vacia).
  const hasFolder = await validateProjectFolderOrPrompt();
  if (!hasFolder) return;

  let skills = [];
  try {
    const resp = await apiFetch(`${API_BASE}/api/local-skills/${encodeURIComponent(state.sessionId)}`);
    const data = await resp.json();
    skills = data.skills || [];
  } catch (err) {
    console.warn("No se pudo consultar las skills locales:", err);
  }

  if (skills.length === 0) {
    const p = document.createElement("p");
    p.className = "confirm-message";
    p.textContent =
      "Esta sesión todavía no tiene skills locales — creá archivos SKILL.md dentro de la subcarpeta \"skills\" de su carpeta de proyecto.";
    commandModalBodyEl.appendChild(p);
  } else {
    const list = document.createElement("ul");
    list.className = "session-list";
    for (const s of skills) {
      const li = document.createElement("li");
      const nameSpan = document.createElement("span");
      nameSpan.className = "session-name";
      nameSpan.textContent = `/${s.name}`;
      nameSpan.title = s.description || "";
      li.appendChild(nameSpan);
      if (s.description) {
        const descSpan = document.createElement("span");
        descSpan.className = "acc-summary";
        descSpan.textContent = s.description;
        li.appendChild(descSpan);
      }
      list.appendChild(li);
    }
    commandModalBodyEl.appendChild(list);
  }

  const actions = document.createElement("div");
  actions.className = "confirm-actions";
  const okBtn = document.createElement("button");
  okBtn.type = "button";
  okBtn.className = "acc-action-btn";
  okBtn.textContent = "Cerrar";
  okBtn.onclick = closeCommandModal;
  actions.appendChild(okBtn);
  commandModalBodyEl.appendChild(actions);
  commandModalOverlayEl.classList.remove("hidden");
}

function insertSlashSkill(skillName) {
  const value = inputEl.value;
  const cursorPos = inputEl.selectionStart ?? value.length;
  const upToCursor = value.slice(0, cursorPos);
  // Bug real reportado por el usuario (2026-09): clickear una skill desde el
  // acceso rapido con el compositor VACIO (sin un "/cmd" en curso) no enviaba
  // nada al chat. Causa: el regex de reemplazo solo matchea si ya hay un
  // "/..." al final; sin match, `replace` devuelve "" y el input quedaba vacio.
  // Ahora, si no hay token "/..." en el cursor, se INSERTA "/skill <nombre> ".
  const hasSlashToken = /(?:^|\s)\/(\S*)$/.test(upToCursor);
  const replaced = hasSlashToken
    ? upToCursor.replace(/(?:^|\s)\/(\S*)$/, (m) => (m.startsWith(" ") ? " " : "") + `/skill ${skillName} `)
    : upToCursor + `/skill ${skillName} `;
  inputEl.value = replaced + value.slice(cursorPos);
  slashMenuEl.classList.add("hidden");
  inputEl.focus();
}

inputEl.addEventListener("input", handleSlashInput);
inputEl.addEventListener("keydown", (e) => {
  if (slashMenuEl.classList.contains("hidden")) return;
  const items = Array.from(slashMenuEl.querySelectorAll(".slash-item"));
  if (items.length === 0) return;

  if (e.key === "ArrowDown") {
    e.preventDefault();
    slashActiveIndex = Math.min(slashActiveIndex + 1, items.length - 1);
    items.forEach((it, i) => it.classList.toggle("active", i === slashActiveIndex));
  } else if (e.key === "ArrowUp") {
    e.preventDefault();
    slashActiveIndex = Math.max(slashActiveIndex - 1, 0);
    items.forEach((it, i) => it.classList.toggle("active", i === slashActiveIndex));
  } else if (e.key === "Enter" && slashActiveIndex >= 0) {
    e.preventDefault();
    items[slashActiveIndex].click();
  } else if (e.key === "Escape") {
    slashMenuEl.classList.add("hidden");
  }
});
document.addEventListener("click", (e) => {
  if (!slashMenuEl.contains(e.target) && e.target !== inputEl) {
    slashMenuEl.classList.add("hidden");
  }
});

// Escape omite (oculta) el panel de pregunta de la tool `question` sin
// responderla -- el LLM sigue esperando la respuesta en el proximo
// mensaje de chat normal (mismo comportamiento que ya tiene ignorarlo
// sin clickear ninguna opcion), esto solo lo saca de la vista.
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !questionPanelEl.classList.contains("hidden")) {
    questionPanelEl.classList.add("hidden");
  }
});

// -------------------------------------------------------------------------
// Inicializacion final del bloque nuevo: sesiones, footer, y refresco de
// contexto/conectores tras cada turno completo (envuelve sendMessage sin
// tocar su cuerpo interno).
// -------------------------------------------------------------------------
const _originalSendMessage = sendMessage;
// BUG PREEXISTENTE corregido (detectado al construir la spec
// contexto-multimodal): este wrapper llamaba a `_originalSendMessage(text)`
// con UN SOLO argumento, descartando en silencio el segundo parametro
// `opts` (messageId/images) que cualquier caller le pasara -- el
// `formEl.addEventListener("submit", ...)` de la spec multimodal manda
// `{ images }` y se perdia sin ningun error visible. `...args` propaga
// TODOS los argumentos tal cual, sin asumir cuantos son.
sendMessage = async function (...args) {
  // Captura ANTES del await: si el usuario cambia de sesion mientras
  // espera la respuesta, `state.sessionId` ya no es esta cuando el await
  // termina (ver el fix extenso en touchCurrentSessionInIndex).
  const turnSessionId = state.sessionId;
  const turnText = typeof args[0] === "string" ? args[0] : null;
  await _originalSendMessage(...args);
  touchCurrentSessionInIndex(turnSessionId, turnText);
  if (document.getElementById("acc-context").open) refreshContextUsage();
};

renderSessionsPanel();
refreshSessionsFromBackend();
refreshProjectFooter();
refreshModelFooter();

// REQ-001/002/003/004 (spec pantalla-bienvenida-ojo): splash screen que se
// ve EN CADA carga de pagina, antes de que el usuario vea/use el chat.
initSplashScreen();

// Si la pagina se abre/recarga (F5, reapertura de pestaña) mientras esta
// MISMA sesion sigue procesando un turno en background en el backend (ver
// fix del bug: crear/cambiar de sesion ya no cancela el turno de otra
// sesion), nos re-enganchamos automaticamente al stream en vez de dejar
// al usuario esperando sin ningun indicio de que Vision sigue trabajando.
(async function reattachIfBusyOnLoad() {
  // Capturado ANTES del primer await -- mismo motivo que en sendMessage:
  // si el usuario cambia de sesion mientras esta funcion espera, no hay
  // que tocar el indice de la sesion NUEVA con la actividad de esta.
  const loadSessionId = state.sessionId;
  try {
    const statusResp = await apiFetch(`${API_BASE}/api/session/${loadSessionId}/status`);
    const statusData = await statusResp.json();
    // spec proyecto-por-sesion REQ-006: rellena ID/ruta al cargar la
    // pagina (F5), no solo al crear/cambiar de sesion.
    updateMetaSessionId(state.sessionId);
    if (statusData.project_path) updateMetaSessionPath(statusData.project_path);
    // BUG REAL corregido: este handler de F5/recarga (a diferencia de
    // activateSession, que SI lo hacia) nunca llamaba a
    // updatePersistenceBanner -- el banner "sin carpeta de proyecto"
    // quedaba pegado con el valor de una sesion anterior (ej. si en algun
    // momento activaste una sesion sin carpeta propia, el banner quedaba
    // visible para SIEMPRE en cualquier recarga posterior, aunque la
    // sesion actual SI tenga carpeta asignada -- persistence_available
    // siempre viene fresco en cada respuesta de /status, hay que pintarlo
    // aca tambien).
    updatePersistenceBanner(statusData.persistence_available);
    // spec carpeta-por-sesion REQ-008: sin bloqueo aca -- una sesion sin
    // carpeta sigue funcionando en memoria; el banner de persistencia de
    // arriba ya avisa, y el modal se abre solo cuando haga falta de
    // verdad (ver validateProjectFolderOrPrompt/evento SSE).
    // spec modo-plan-loop REQ-001/005/009: mismo tratamiento que
    // activateSession -- una recarga de pagina (F5) tambien debe
    // reflejar el modo/alarma reales de la sesion, no reiniciarlos.
    updateModeChip(statusData.mode || "active");
    // BUG real corregido (deteccion QA 2026-09-14): mismo fix que
    // activateSession (ver ese comentario extenso) -- SIN esto, F5 con un
    // loop activo/pausado dejaba el toggle "Loop" mintiendo "apagado",
    // y tocarlo abria una SEGUNDA conexion SSE redundante (duplicaba la
    // respuesta en pantalla) en vez de simplemente reflejar el estado real
    // o dejar que "Detener loop" lo frenara.
    _loopRunning = !!statusData.loop_active;
    _loopAutoPref = !!statusData.loop_auto;
    if (quickPanelLooprunToggleEl && quickPanelLoopautoToggleEl) syncQuickPanelLoopState();
    if (statusData.loop_active && statusData.loop_paused_for_question) {
      showLoopBanner();
    } else {
      hideLoopBanner();
    }
    // Mismo fix que activateSession (ver su comentario): sumar a
    // `state.sending` mientras dura el reenganche, para que el poll de
    // fondo (pollActiveSessionForExternalActivity) no abra una SEGUNDA
    // conexion SSE a esta misma sesion y duplique la respuesta.
    if (statusData.busy && state.sending === 0) {
      state.sending += 1;
      try {
        setConnStatus("busy", "Vision sigue pensando en esta sesión…");
        const resp = await apiFetch(`${API_BASE}/api/chat/stream/${loadSessionId}`);
        await renderIncomingStream(resp, statusData.active_turn_id);
        if (connStatusEl.dataset.state === "busy") setConnStatus("online", "Vision en línea");
        touchCurrentSessionInIndex(loadSessionId);
      } finally {
        state.sending -= 1;
        if (state.sending < 0) state.sending = 0;
      }
    }
    // REQ-006: si `exists` viene false, esta sesion tiene historial
    // persistido en localStorage que el backend (probablemente reiniciado)
    // ya no reconoce. Se avisa una sola vez, con estilo distinto a un
    // error comun.
    if (statusData.exists === false && chatHistory.length > 0) {
      appendMessage(
        "system-orphan",
        "⚠️ Esta sesión se perdió (el servidor se reinició). Se preserva tu historial visual, pero Vision no recuerda el contexto anterior — el próximo mensaje arranca una conversación nueva del lado del servidor.",
        { persist: false }
      );
    }
  } catch {
    // No es critico si falla -- checkBackendStatus() ya corrio y da el
    // estado general de conexion.
  }
})();

// Bug real reportado por el usuario 2026-09-16: un agente programado (u
// otra pestaña) puede escribir turnos en la sesion que TENES ABIERTA sin
// que vos hagas nada -- reattachIfBusyOnLoad (arriba) y el chequeo de
// activateSession solo corren UNA VEZ (al cargar la pagina, o al hacer
// click para entrar a la sesion), asi que un turno que arranca DESPUES,
// mientras seguis parado ahi, quedaba invisible hasta salir y volver a
// entrar. Poll liviano cada 8s (mismo intervalo que ya usa
// startAgentsPolling contra SQLite real sin problema -- este pega contra
// /status, que ni siquiera toca SQLite para el flag `busy`, solo lee un
// booleano en memoria): si la sesion activa paso a "busy" sin que ESTE tab
// lo haya pedido (`state.sending === 0`), se reengancha al stream en vivo
// -- misma UX que activateSession, sin que el usuario tenga que hacer nada.
let _idleReattachInFlight = false;
async function pollActiveSessionForExternalActivity() {
  // Bug real reportado por el usuario 2026-09-16 ("a veces se generan dos
  // respuestas paralelas"): un turno LARGO (varias tool calls) puede
  // seguir 'busy' durante varios ticks de este poll de 8s. El guard viejo
  // (`state.sending > 0`) NO alcanzaba porque activateSession y
  // reattachIfBusyOnLoad reenganchan su PROPIO stream sin tocar
  // `state.sending` -- asi que si ESE reenganche seguia en curso cuando
  // este poll volvia a tickear, `state.sending` todavia leia 0 y este poll
  // abria una SEGUNDA conexion SSE a la MISMA sesion -- cada conexion
  // dibuja su propia burbuja para los mismos eventos (fan-out de
  // session.subscribe()), de ahi las dos respuestas identicas en paralelo.
  // Fix: este poll (y activateSession/reattachIfBusyOnLoad, ver sus
  // propios comentarios) ahora suman a `state.sending` MIENTRAS dura el
  // reenganche, igual que sendMessage() -- asi los 3 caminos se excluyen
  // entre si a traves del mismo contador compartido, no solo contra si
  // mismos.
  if (state.sending > 0 || _idleReattachInFlight) return;
  const sessionId = state.sessionId;
  if (!sessionId) return;
  _idleReattachInFlight = true; // evita que ESTE poll se re-dispare a si mismo mientras espera el /status
  try {
    const statusResp = await apiFetch(`${API_BASE}/api/session/${encodeURIComponent(sessionId)}/status`);
    const statusData = await statusResp.json();
    // Re-chequeo DESPUES del await: el usuario pudo haber mandado su
    // propio mensaje (o cambiado de sesion) mientras esperabamos esta
    // respuesta de red -- sin esto, la ventana entre el primer chequeo y
    // este punto es exactamente donde se colaba el bug de arriba.
    if (state.sending > 0 || !statusData.busy || sessionId !== state.sessionId) return;
    state.sending += 1;
    try {
      setConnStatus("busy", "Vision sigue pensando en esta sesión…");
      resetToolRow();
      const resp = await apiFetch(`${API_BASE}/api/chat/stream/${sessionId}`);
      await renderIncomingStream(resp, statusData.active_turn_id, sessionId);
      if (connStatusEl.dataset.state === "busy") setConnStatus("online", "Vision en línea");
      touchCurrentSessionInIndex(sessionId);
    } finally {
      state.sending -= 1;
      if (state.sending < 0) state.sending = 0;
    }
  } catch {
    // Sin conexion momentanea, o la sesion ya no existe -- se reintenta
    // solo en el proximo tick, no es critico.
  } finally {
    _idleReattachInFlight = false;
  }
}
setInterval(pollActiveSessionForExternalActivity, 8000);

// =========================================================================
// Spec parada-y-cola-mensajes: STOP (REQ-001/002), cola de mensajes
// (REQ-003/007/008), editar/eliminar mensajes en cualquier estado
// (REQ-004/005/009), sesion huerfana (REQ-006 -- el aviso al cargar ya
// esta arriba en reattachIfBusyOnLoad).
// =========================================================================

// -- REQ-001/002: boton unico Enviar/Detener -------------------------------
// Reemplaza los 2 botones independientes que habia antes. Pedido explicito
// del usuario: ya no se puede encolar un mensaje nuevo mientras un turno
// sigue corriendo -- hay que esperar a que termine (evento 'done'/'error')
// o presionar "Detener" primero. `formEl.addEventListener("submit", ...)`
// (mas abajo) respeta esto: no dispara sendMessage si el boton no esta en
// "idle".
function setSendButtonState(newState) {
  // newState: "idle" | "active" | "stopping"
  sendOrStopBtnEl.dataset.state = newState;
  sendOrStopBtnEl.disabled = newState === "stopping";
  if (newState === "idle") {
    sendOrStopBtnEl.title = "Enviar mensaje";
    sendOrStopLabelEl.textContent = "Enviar";
  } else if (newState === "active") {
    sendOrStopBtnEl.title = "Detener a Vision";
    sendOrStopLabelEl.textContent = "Detener";
  } else {
    sendOrStopBtnEl.title = "Deteniendo… esperando que termine la herramienta en curso";
    sendOrStopLabelEl.textContent = "Deteniendo…";
  }
}

sendOrStopBtnEl.addEventListener("click", async (e) => {
  const currentState = sendOrStopBtnEl.dataset.state;

  if (currentState === "idle") {
    // Comportamiento de "Enviar": dispara el submit normal del form (el
    // boton es type="button" a proposito, ver comentario en index.html).
    e.preventDefault();
    formEl.requestSubmit();
    return;
  }

  if (currentState !== "active") return; // "stopping": deshabilitado, no deberia llegar un click

  setSendButtonState("stopping");
  try {
    const resp = await apiFetch(`${API_BASE}/api/chat/${state.sessionId}/stop`, { method: "POST" });
    // Bug real reportado por el usuario (2026-09-20): "el boton send-btn-
    // label se queda en detener y luego en deteniendo... queda colgado
    // ahi y no se detiene, tengo que refrescar". Causa raiz: el boton
    // vuelve a "idle" SOLO cuando llega el evento SSE 'stopped' (comentario
    // de abajo) -- pero `POST /api/chat/{id}/stop` (backend) devuelve
    // `{"stopped": false, "reason": "no_active_turn"}` SIN emitir ningun
    // evento SSE cuando `session.is_busy` YA es false en el momento en que
    // llega el pedido (turno terminado solo justo antes del click, o
    // condicion de carrera real con el loop autonomo terminando su ultimo
    // turno). Ese evento 'stopped' que nunca iba a llegar dejaba el boton
    // pegado en "Deteniendo…" para siempre. Confirmado en produccion: el
    // usuario reporto ver `{"busy":false,...,"active_turn_id":null}` en
    // /status justo cuando el boton seguia colgado. Fix: si el backend
    // contesta `stopped: false` (nada que detener), no hay ningun evento
    // que esperar -- se resetea el boton ACA mismo en vez de quedar
    // esperando algo que nunca va a pasar.
    const data = resp.ok ? await resp.json().catch(() => ({})) : {};
    if (data.stopped === false) {
      setSendButtonState("idle");
    }
    // Si `stopped: true`, el boton vuelve a "idle" cuando llega el evento
    // 'stopped' (ver handleEvent) -- no aca, porque el backend puede
    // tardar en cerrar limpio la tool en curso (REQ-001: "esperar cierre
    // limpio").
  } catch (err) {
    console.warn("No se pudo pedir STOP:", err);
    setSendButtonState("active"); // reintentable
  }
});

// =========================================================================
// spec modo-plan-loop: Modo Plan/Activo (REQ-001/002/003/004/005) + /loop
// autonomo (REQ-006/007/010) + alarma sonora (REQ-009).
// =========================================================================

// -- REQ-001/005: chip de modo + atajo Tab (con foco en el chat) ----------
function updateModeChip(mode) {
  state.mode = mode;
  modeChipEl.dataset.mode = mode;
  modeChipValEl.textContent = mode === "plan" ? "PLAN" : "ACTIVO";
}

async function setMode(newMode) {
  // REQ-004: no se toca session.messages/todos aca -- solo se manda el
  // toggle al backend (fuente de verdad real, ver POST /api/session/{id}/
  // mode) y se refleja la UI. Optimista: se pinta antes de que responda
  // el fetch, y se revierte si falla (mismo criterio que el resto de los
  // toggles chicos de la UI, ej. saveSelectedModel).
  const previous = state.mode;
  updateModeChip(newMode);
  try {
    const resp = await apiFetch(`${API_BASE}/api/session/${state.sessionId}/mode`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode: newMode }),
    });
    if (!resp.ok) throw await httpErrorFromResponse(resp);
  } catch (err) {
    console.warn("No se pudo cambiar de modo:", err);
    updateModeChip(previous);
    appendMessage("system-note", `No se pudo cambiar de modo. ${friendlyErrorMessage(err)}`, { persist: false });
  }
}

function toggleMode() {
  setMode(state.mode === "plan" ? "active" : "plan");
}

modeChipEl.addEventListener("click", toggleMode);

// REQ-001: Tab alterna de modo SOLO con el foco en #chat-input -- en
// cualquier otro lugar de la pagina, Tab se comporta como navegacion
// estandar de foco (no se intercepta nada fuera de este listener puntual).
inputEl.addEventListener("keydown", (e) => {
  if (e.key !== "Tab") return;
  // No interceptar si el menu "/" esta abierto -- Tab ahi tiene sentido
  // de autocompletar/navegar el menu, no de cambiar de modo global.
  if (!slashMenuEl.classList.contains("hidden")) return;
  e.preventDefault();
  toggleMode();
});

// -- REQ-009: alarma sonora (Web Audio API nativa, sin archivo vendorizado) --
let _loopAlarmCtx = null;
let _loopAlarmIntervalId = null;

// -------------------------------------------------------------------------
// Alarma del loop (REQ-009 + pedido del usuario 2026-09): configurable en
// tipo, frecuencia e intervalo y volumen, persistida en localStorage.
// Default: tipo "beep", intervalo 4000ms (mas frecuente que antes) y volumen 0.2.
// -------------------------------------------------------------------------
const LOOP_ALARM_TYPE_KEY = "vision.loop.alarmType";
const LOOP_ALARM_INTERVAL_KEY = "vision.loop.alarmInterval";
const LOOP_ALARM_VOLUME_KEY = "vision.loop.alarmVolume";

function _loopLs(key, def) {
  try { const v = localStorage.getItem(key); return v === null ? def : v; } catch { return def; }
}
function loopAlarmType() { return _loopLs(LOOP_ALARM_TYPE_KEY, "beep"); }
function loopAlarmIntervalMs() {
  const n = parseInt(_loopLs(LOOP_ALARM_INTERVAL_KEY, "4000"), 10);
  return Number.isFinite(n) && n >= 1000 ? n : 4000;
}
function loopAlarmVolume() {
  const n = parseFloat(_loopLs(LOOP_ALARM_VOLUME_KEY, "0.2"));
  return Number.isFinite(n) ? Math.max(0.05, Math.min(0.8, n)) : 0.2;
}

function _scheduleTone(ctx, freq, startAt, dur, vol) {
  const osc = ctx.createOscillator();
  const gain = ctx.createGain();
  osc.type = "sine";
  osc.frequency.value = freq;
  gain.gain.setValueAtTime(0.001, startAt);
  gain.gain.exponentialRampToValueAtTime(vol, startAt + 0.02);
  gain.gain.exponentialRampToValueAtTime(0.001, startAt + dur);
  osc.connect(gain).connect(ctx.destination);
  osc.start(startAt);
  osc.stop(startAt + dur + 0.05);
}

// Pedido del usuario (2026-09): la alarma "era un simple bip" -- se agrega
// un sonido de SIRENA (alternancia agudo/grave repetida, 3 ciclos) que es
// mucho mas llamativo. Se ofrece en la alarma del loop (REQ-009) y por
// evento en el programador (spec programador-alarmas REQ-004).
function _scheduleSiren(ctx, now, vol) {
  let t = now;
  for (let i = 0; i < 3; i++) {
    _scheduleTone(ctx, 1180, t, 0.18, vol);
    t += 0.18;
    _scheduleTone(ctx, 620, t, 0.18, vol);
    t += 0.18;
  }
}

// Refactor 2026 (anti-spaghetti): un SOLO generador de tonos por sonido,
// compartido por la alarma del loop (REQ-009) y por el sonido por evento del
// programador. Antes el if/else de beep/doble/pulso/sirena estaba duplicado
// en playLoopAlarm y playAlarmSound (dos copias del mismo scheduling).
function scheduleAlarmTone(ctx, now, vol, kind) {
  if (kind === "double") {
    _scheduleTone(ctx, 880, now, 0.25, vol);
    _scheduleTone(ctx, 660, now + 0.28, 0.3, vol);
  } else if (kind === "pulse") {
    _scheduleTone(ctx, 1245, now, 0.2, vol);
    _scheduleTone(ctx, 1245, now + 0.24, 0.18, vol);
  } else if (kind === "sirena") {
    _scheduleSiren(ctx, now, vol);
  } else {
    _scheduleTone(ctx, 880, now, 0.35, vol);
  }
}

// Refactor 2026 (anti-spaghetti): antes el idiom
// `new (window.AudioContext || window.webkitAudioContext)` se repetia 6 veces
// en el archivo. Ahora es UNA unica fabrica.
function createWebAudioContext() {
  return new (window.AudioContext || window.webkitAudioContext)();
}

// Contexto COMPARTIDO y "desbloqueado" de la alarma (REQ-009): una sola
// instancia reutilizada por la alarma del loop y el programador, creada/
// reanudada en el arranque o el primer gesto (ver _unlockLoopAudio).
function getLoopAudioContext() {
  try {
    if (!_loopAlarmCtx) _loopAlarmCtx = createWebAudioContext();
    if (_loopAlarmCtx.state === "suspended") _loopAlarmCtx.resume();
  } catch { /* noop -- sin audio no bloqueamos nada */}
  return _loopAlarmCtx;
}

// Alarma por <audio> (WAV pre-generado) -- reemplaza el AudioContext manual,
// que es lo mas bloqueado por autoplay y nos fallo en el entorno del usuario
// (2026-09, recurrente: ni el boton 'Probar alarma' con gesto real sonaba).
// Un <audio>.play() desbloqueado en un gesto es mas confiable que un oscilador.
const ALARM_WAVS = {
  beep: "/sounds/beep.wav",
  double: "/sounds/double.wav",
  pulse: "/sounds/pulse.wav",
  sirena: "/sounds/sirena.wav",
  success: "/sounds/success.wav",
};
function playAlarmWav(kind) {
  try {
    const a = new Audio(ALARM_WAVS[kind] || ALARM_WAVS.beep);
    a.volume = (function () { try { return loopAlarmVolume(); } catch { return 0.2; } })();
    a.play().catch((err) => console.warn("audio.play() bloqueado puntualmente:", err && err.message));
  } catch (err) {
    console.warn("No se pudo reproducir la alarma:", err);
  }
}

// Pedido del usuario (2026-09-13): cuando el loop TERMINA TODO (la tarea
// principal y sus sub-tareas quedaron completed y no queda nada pendiente),
// suena una SEÑAL DE EXITO distinta del beep de riesgo -- un arpegio ascendente
// (C5-E5-G5-C6) para que el usuario sepa que ya puede dejarla y volver aunque
// esté lejos de la PC. Reutiliza el motor Web Audio y el volumen configurado.
function playSuccessAlarm() {
  playAlarmWav("success");
}

function playLoopAlarm() {
  playAlarmWav(loopAlarmType());
}

function startLoopAlarm() {
  if (_loopAlarmIntervalId !== null) return; // ya sonando
  playLoopAlarm();
  _loopAlarmIntervalId = window.setInterval(playLoopAlarm, loopAlarmIntervalMs());
}

function stopLoopAlarm() {
  if (_loopAlarmIntervalId !== null) {
    window.clearInterval(_loopAlarmIntervalId);
    _loopAlarmIntervalId = null;
  }
}

// Bug real reportado por el usuario (2026-09): la alarma del loop NO sonaba
// cuando el loop pausaba con una `question` en autonomo. Causa raiz: los
// navegadores bloquean AudioContext por autoplay si NO hubo gesto de usuario
// previo, y `_loopAlarmCtx` se creaba recien al momento de sonar (sin
// gesto) -> quedaba `suspended` y `resume()` no podia desbloquearlo. Fix:
// crear/reanudar el contexto en el PRIMER gesto (click/tecla) unica vez,
// para que ya este "running" cuando el loop pause y la alarma suene.
function _unlockLoopAudio() {
  try {
    const ctx = getLoopAudioContext();
    if (!ctx || ctx.state === "suspended") return;
    document.removeEventListener("pointerdown", _unlockLoopAudio);
    document.removeEventListener("keydown", _unlockLoopAudio);
  } catch { /* noop -- sin audio no bloqueamos ninguna otra funcionalidad */ }
}
document.addEventListener("pointerdown", _unlockLoopAudio);
document.addEventListener("keydown", _unlockLoopAudio);


// -- REQ-009: banner "el loop necesita tu ayuda" ---------------------------
function showLoopBanner() {
  loopBannerEl.classList.remove("hidden");
  startLoopAlarm();
}

function hideLoopBanner() {
  loopBannerEl.classList.add("hidden");
  stopLoopAlarm();
}

loopBannerStopEl.addEventListener("click", async () => {
  try {
    await apiFetch(`${API_BASE}/api/chat/${state.sessionId}/stop`, { method: "POST" });
  } catch (err) {
    console.warn("No se pudo detener el loop:", err);
  }
  hideLoopBanner();
});

// REQ-009: apenas el usuario responde (envia cualquier mensaje normal,
// sea clickeando una opcion de `question` o texto libre), se oculta la
// alarma/banner de inmediato -- el backend recien confirma la reanudacion
// del loop cuando termine este turno, pero la UI no debe seguir sonando
// mientras la respuesta ya esta en camino.
const _originalSendMessageForLoop = sendMessage;
sendMessage = async function (text, opts) {
  if (!loopBannerEl.classList.contains("hidden")) hideLoopBanner();
  return _originalSendMessageForLoop(text, opts);
};

// -- REQ-006/007/010: comando /loop -----------------------------------------
async function startLoop(auto = false) {
  try {
    // REQ-011 (spec modo-plan-loop): `auto` = modo auto del loop (Vision
    // decide las preguntas con "(Recomendada)"; pausa + alarma si hay
    // "(Riesgo)" o no hay recomendada). Se manda en el body.
    const resp = await apiFetch(`${API_BASE}/api/session/${state.sessionId}/loop`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ auto: !!auto }),
    });
    if (!resp.ok) {
      const data = await resp.json().catch(() => ({}));
      appendMessage("error", `No se pudo iniciar el loop. ${httpError(resp.status, data).message}`);
      return;
    }
    _loopRunning = true;
    _loopAutoPref = !!auto; // sincroniza la preferencia con lo iniciado
    if (quickPanelLooprunToggleEl) syncQuickPanelLoopState();
    // Si Vision ya estaba ocupada, `/api/session/{id}/loop` solo activa
    // el flag -- el proximo turno sintetico llega por el stream SSE que
    // YA esta corriendo (activateSession/sendMessage), no hace falta
    // re-engancharse aca. Si NO estaba ocupada, el backend arranca el
    // primer turno sintetico en background: nos re-enganchamos para
    // verlo en vivo (mismo camino que "sesion sigue pensando" en
    // activateSession).
    //
    // BUG real corregido (deteccion QA 2026-09-14, 2 rondas): el
    // comentario de arriba ya describia esta logica, pero el codigo NUNCA
    // la aplicaba -- se re-enganchaba SIEMPRE, sin importar si esta MISMA
    // pestaña ya tenia un `renderIncomingStream` activo leyendo el turno
    // en curso (`state.sending > 0`). Como `session.subscribe()` hace
    // fan-out a CADA conexion por separado, una segunda conexion abierta a
    // mitad de un turno recibe los eventos desde ESE punto en adelante
    // (nunca un replay de lo que ya paso) y arma su PROPIA burbuja desde
    // cero -- resultado: la respuesta se veia duplicada en pantalla, con
    // la segunda copia mostrando texto incompleto (le faltaba todo lo que
    // la primera conexion ya habia consumido) y sin clasificacion (el
    // evento "response-type" tambien ya habia pasado). Reportado por el
    // usuario con captura real.
    //
    // Primer intento (saltear la reconexion directo si `state.sending>0`)
    // evitaba el duplicado pero introducia un agujero peor: la conexion
    // original de `sendMessage` esta acotada a SU turno
    // (`wait_for_turn_id`, ver `_drain_session_queue` en el backend) y
    // cierra sola apenas ese turno particular termina -- si el loop
    // encadena OTRO turno sintetico despues (`_maybe_continue_loop`, en el
    // `finally` del turno original), nadie en esta pestaña quedaba
    // escuchando: esa continuacion nunca se renderizaba NI se guardaba en
    // el `chatHistory` local, se perdia por completo del lado del
    // usuario aunque el backend la haya ejecutado bien. Fix real: ESPERAR
    // (polling corto) a que la lectura en curso termine SOLA -- en ese
    // momento su conexion ya cerro, cero riesgo de duplicado -- y recien
    // ahi consultar si el loop sigue ocupado (encadeno mas turnos) para
    // reconectarse recien entonces, nunca en paralelo.
    if (state.sending > 0) {
      while (state.sending > 0) {
        await new Promise((r) => setTimeout(r, 200));
      }
    }
    const loopStatusResp = await apiFetch(`${API_BASE}/api/session/${state.sessionId}/status`);
    const loopStatusData = await loopStatusResp.json();
    if (loopStatusData.busy) {
      setConnStatus("busy", "Vision está trabajando…");
      const streamResp = await apiFetch(`${API_BASE}/api/chat/stream/${state.sessionId}`);
      await renderIncomingStream(streamResp);
      if (connStatusEl.dataset.state === "busy") setConnStatus("online", "Vision en línea");
    }
  } catch (err) {
    appendMessage("error", `No se pudo iniciar el loop. ${friendlyErrorMessage(err)}`, { errorKind: isOfflineError(err) ? "offline" : "other" });
  }
}

// -------------------------------------------------------------------------
// REQ-003/007/008: cola de mensajes de usuario. Cada mensaje en cola se
// trackea localmente (Map messageId -> { wrap, position, text }) para
// poder actualizar su badge/posicion en vivo con los eventos 'queued' y
// reordenar/editar/eliminar sin volver a pedirle nada al servidor salvo
// la accion puntual.
// -------------------------------------------------------------------------
const queuedTurns = new Map(); // messageId -> { wrapEl, position }
const QUEUE_WARNING_THRESHOLD = 5;
let queueWarningShown = false;

function buildUserTurnControls(messageId, rawText) {
  const controls = document.createElement("div");
  controls.className = "user-turn-controls";
  if (!messageId) return controls; // mensajes viejos sin id (pre-feature) no tienen controles

  // Pedido del usuario (2026-09): el boton copiar del turno de usuario debe
  // estar en el MISMO `user-turn-controls` que editar/eliminar (no flotar
  // dentro de la burbuja, que era el comportamiento previo). Usa el mismo
  // estilo que los demas botones del contenedor (.user-turn-btn).
  if (typeof rawText === "string" && rawText) {
    const copyBtn = document.createElement("button");
    copyBtn.type = "button";
    copyBtn.className = "user-turn-btn";
    copyBtn.title = "Copiar este mensaje";
    copyBtn.textContent = "📋";
    copyBtn.onclick = () => copyToClipboard(rawText, copyBtn, "📋");
    controls.prepend(copyBtn);
  }

  const editBtn = document.createElement("button");
  editBtn.type = "button";
  editBtn.className = "user-turn-btn";
  editBtn.title = "Editar este mensaje";
  editBtn.textContent = "✎";
  editBtn.onclick = () => startEditingUserMessage(messageId);

  const deleteBtn = document.createElement("button");
  deleteBtn.type = "button";
  deleteBtn.className = "user-turn-btn danger";
  deleteBtn.title = "Eliminar este mensaje";
  deleteBtn.textContent = "🗑";
  deleteBtn.onclick = () => deleteUserMessage(messageId);

  controls.append(editBtn, deleteBtn);

  // Reordenar (solo tiene sentido visual si el mensaje esta en cola --
  // se agregan/quitan botones subir/bajar dinamicamente en
  // renderQueueBadge, no aca, porque recien ahi se sabe si esta en cola).
  return controls;
}

function findUserTurnWrap(messageId) {
  return messagesEl.querySelector(`.user-turn-wrap[data-message-id="${CSS.escape(messageId)}"]`);
}

function renderQueueBadge(messageId, position) {
  const wrap = findUserTurnWrap(messageId);
  if (!wrap) return;

  let badge = wrap.querySelector(".user-turn-badge");
  if (!badge) {
    badge = document.createElement("span");
    badge.className = "user-turn-badge";
    wrap.insertBefore(badge, wrap.firstChild);
  }
  badge.innerHTML = "";
  const label = document.createElement("span");
  label.textContent = "En cola · posición ";
  const posSpan = document.createElement("span");
  posSpan.className = "queue-pos";
  posSpan.textContent = String(position + 1);
  badge.append(label, posSpan);

  // Botones subir/bajar -- solo mientras esta en cola.
  const controls = wrap.querySelector(".user-turn-controls");
  if (controls && !controls.querySelector(".queue-move")) {
    const upBtn = document.createElement("button");
    upBtn.type = "button";
    upBtn.className = "user-turn-btn queue-move";
    upBtn.title = "Subir en la cola";
    upBtn.textContent = "↑";
    upBtn.onclick = () => reorderQueuedMessage(messageId, -1);

    const downBtn = document.createElement("button");
    downBtn.type = "button";
    downBtn.className = "user-turn-btn queue-move";
    downBtn.title = "Bajar en la cola";
    downBtn.textContent = "↓";
    downBtn.onclick = () => reorderQueuedMessage(messageId, 1);

    controls.prepend(downBtn);
    controls.prepend(upBtn);
  }

  queuedTurns.set(messageId, { position });
  maybeShowQueueWarning();
}

function removeQueueBadge(messageId) {
  const wrap = findUserTurnWrap(messageId);
  if (wrap) {
    const badge = wrap.querySelector(".user-turn-badge");
    if (badge) badge.remove();
    wrap.querySelectorAll(".queue-move").forEach((btn) => btn.remove());
  }
  queuedTurns.delete(messageId);
}

function maybeShowQueueWarning() {
  if (queuedTurns.size >= QUEUE_WARNING_THRESHOLD && !queueWarningShown) {
    queueWarningShown = true;
    const div = document.createElement("div");
    div.className = "queue-warning";
    div.textContent = `Tenés ${queuedTurns.size} mensajes esperando`;
    messagesEl.appendChild(div);
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }
  if (queuedTurns.size === 0) queueWarningShown = false;
}

async function reorderQueuedMessage(messageId, delta) {
  const info = queuedTurns.get(messageId);
  if (!info) return;
  const newPosition = Math.max(0, info.position + delta);
  try {
    await apiFetch(`${API_BASE}/api/chat/${state.sessionId}/queue/reorder`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message_id: messageId, new_position: newPosition }),
    });
    // El backend no emite un evento de "reordenado" -- se refleja
    // localmente de forma optimista, es suficiente para este uso.
    renderQueueBadge(messageId, newPosition);
  } catch (err) {
    console.warn("No se pudo reordenar:", err);
  }
}

// -------------------------------------------------------------------------
// REQ-004/005/009: editar/eliminar CUALQUIER mensaje de usuario (en curso,
// en cola, o historial viejo) -- el backend distingue el caso solo con el
// messageId, el frontend no necesita saber en cual de los 3 casos esta.
// -------------------------------------------------------------------------
function startEditingUserMessage(messageId) {
  const wrap = findUserTurnWrap(messageId);
  if (!wrap) return;
  const bubble = wrap.querySelector(".msg.user");
  if (!bubble || bubble.classList.contains("editing")) return;

  // BUG PREEXISTENTE corregido (ver appendMessage/.msg-text): el texto
  // editable es SOLO el contenido de .msg-text, no toda la burbuja (que
  // tambien contiene controles de copiar/eliminar, timestamp, y ahora
  // miniaturas de imagen -- ninguno de esos debe entrar al textarea de
  // edicion ni desaparecer al terminar de editar).
  const textEl = bubble.querySelector(".msg-text");
  const originalText = textEl ? textEl.textContent : bubble.textContent;
  bubble.classList.add("editing");
  if (textEl) textEl.replaceChildren();
  else bubble.textContent = "";

  const textarea = document.createElement("textarea");
  textarea.className = "user-edit-textarea";
  textarea.value = originalText;
  textarea.rows = Math.min(6, Math.max(2, originalText.split("\n").length));

  const actions = document.createElement("div");
  actions.className = "user-edit-actions";
  const saveBtn = document.createElement("button");
  saveBtn.type = "button";
  saveBtn.className = "acc-action-btn";
  saveBtn.textContent = "Guardar";
  const cancelBtn = document.createElement("button");
  cancelBtn.type = "button";
  cancelBtn.className = "acc-action-btn secondary";
  cancelBtn.textContent = "Cancelar";
  actions.append(cancelBtn, saveBtn);

  (textEl || bubble).append(textarea, actions);
  textarea.focus();
  textarea.setSelectionRange(textarea.value.length, textarea.value.length);

  const finishEditing = (newText) => {
    bubble.classList.remove("editing");
    if (textEl) {
      textEl.replaceChildren();
      textEl.textContent = newText;
    } else {
      bubble.innerHTML = "";
      bubble.textContent = newText;
    }
  };

  cancelBtn.onclick = () => finishEditing(originalText);
  saveBtn.onclick = async () => {
    const newText = textarea.value.trim();
    if (!newText || newText === originalText) {
      finishEditing(originalText);
      return;
    }
    finishEditing(newText);
    await submitMessageEdit(messageId, newText);
  };
}

async function submitMessageEdit(messageId, newText) {
  try {
    const resp = await apiFetch(`${API_BASE}/api/chat/${state.sessionId}/message/${encodeURIComponent(messageId)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: newText }),
    });
    const data = await resp.json();

    // Actualiza chatHistory en memoria para que persista el texto nuevo.
    const entry = chatHistory.find((m) => m.messageId === messageId);
    if (entry) entry.text = newText;
    persistChatHistory();

    if (data.case === "active_turn_edited") {
      // El backend va a emitir 'message-edited' + continuar el streaming
      // en el turno YA ABIERTO (esta misma conexion de sendMessage sigue
      // escuchando) -- no hace falta hacer nada mas aca.
      removeQueueBadge(messageId);
    } else if (data.case === "queued_turn_edited") {
      // Sigue en cola, solo se actualizo el texto -- badge de posicion
      // sin cambios.
    } else if (data.case === "history_truncated_and_relaunched") {
      // Se trunco todo lo posterior a este mensaje y se relanzo un turno
      // nuevo -- hay que reflejar visualmente que los mensajes despues de
      // este ya no son validos, y escuchar el nuevo turno.
      markMessagesAfterAsStale(messageId);
      await attachToRelaunchedTurn(messageId);
    }
  } catch (err) {
    console.warn("No se pudo editar el mensaje:", err);
  }
}

async function deleteUserMessage(messageId) {
  // Pedido del usuario (2026-09): borrar una PREGUNTA mia debe eliminar la
  // pregunta Y sus respuestas (todo el turno). Antes esto no quitaba nada de
  // pantalla (quedaba atado al backend y solo console.warn). Fix: optimista --
  // al tocar el borrador se confirma y se remueve el .turn-container completo
  // (burbuja user + tool-calls + respuesta) de la vista, se poda el historial
  // local, y el DELETE al backend queda best-effort.
  const confirmed = await showConfirmModal({
    title: "Eliminar mensaje",
    message: "Se eliminará tu pregunta y sus respuestas. Esta acción no se puede deshacer.",
    acceptLabel: "Eliminar",
  });
  if (!confirmed) return;

  const wrap = findUserTurnWrap(messageId);
  const container = wrap ? wrap.closest(".turn-container") : null;
  if (container) container.remove();

  // Poda local: la pregunta + su respuesta (y tools) hasta el siguiente user.
  const idx = chatHistory.findIndex((m) => m.messageId === messageId);
  if (idx !== -1) {
    let endIdx = idx + 1;
    while (endIdx < chatHistory.length && chatHistory[endIdx].role !== "user") endIdx += 1;
    chatHistory.splice(idx, endIdx - idx);
    persistChatHistory();
  }

  try {
    const resp = await apiFetch(`${API_BASE}/api/chat/${state.sessionId}/message/${encodeURIComponent(messageId)}`, {
      method: "DELETE",
    });
    if (!resp.ok) {
      const d = await resp.json().catch(() => ({}));
      console.warn("Mensaje borrado localmente; backend:", resp.status, d.detail || "");
    }
  } catch (err) {
    console.warn("Mensaje borrado localmente; backend:", err);
  }
}

/**
 * eliminar-respuestas REQ-001/002/003: borra UNICAMENTE la respuesta del
 * asistente que sigue al turno `userTurnId` -- a diferencia de
 * deleteUserMessage/markMessagesAfterAsStale (que TRUNCAN todo lo
 * posterior), aca se remueve solo ese bloque puntual de chatHistory y del
 * DOM, dejando intacto el resto de la conversacion.
 * `assistantMsgEl`: el propio .msg.assistant en el DOM (ya localizado por
 * el caller via wrap.closest(".msg")) -- se remueve directo sin
 * reconstruir todo #messages, mas simple y mas rapido que el patron de
 * "reconstruir desde chatHistory" que usa el truncado.
 */
/**
 * Boton "recargar respuesta" (reset ↻): re-genera la respuesta del
 * asistente del turno `userTurnId` re-ejecutando el MISMO prompt que lo
 * origino. Para no duplicar la burbuja de usuario en el DOM, se quita el
 * contenedor completo de ese turno (burbuja user + tool-calls + respuesta)
 * y se vuelve a mandar el texto como un turno nuevo. No se puede usar
 * mientras otro turno esta corriendo (mismo criterio del boton Enviar).
 */
async function regenerateResponse(userTurnId, assistantMsgEl) {
  if (state.sending > 0) return;
  const userMsg = chatHistory.find((m) => m.messageId === userTurnId);
  if (!userMsg) return;

  const turnWrap = assistantMsgEl ? assistantMsgEl.closest(".turn-container") : null;
  if (turnWrap) turnWrap.remove();
  else if (assistantMsgEl) assistantMsgEl.remove();

  // Se quitan de chatHistory la burbuja de USUARIO de ese turno y la de su
  // respuesta del asistente (solo esas dos), para que la re-ejecucion no
  // duplique contenido en el historial local.
  const userIdx = chatHistory.findIndex((m) => m.messageId === userTurnId);
  if (userIdx !== -1) {
    let assistantIdx = -1;
    for (let i = userIdx + 1; i < chatHistory.length; i++) {
      if (chatHistory[i].role === "user") break;
      if (chatHistory[i].role === "assistant") { assistantIdx = i; break; }
    }
    if (assistantIdx !== -1) chatHistory.splice(assistantIdx, 1);
    chatHistory.splice(userIdx, 1);
    persistChatHistory();
  }

  await sendMessage(userMsg.text, {
    messageId: crypto.randomUUID(),
    images: userMsg.images,
  });
}

async function revalidateErrorMessage(msgEl, rawText, reloadBtn) {
  // Pedido del usuario (2026-09): "recargar" un mensaje de error revalida si
  // el problema persiste. Para el caso comun (error de conexion) se verifica
  // la disponibilidad del backend con un ping liviano a /api/status. Si
  // responde -> se marca en verde "Solucionado: <mensaje>"; si no -> se
  // mantiene el error (y se avisa que todavia no se resolvio).
  if (reloadBtn) { reloadBtn.textContent = "…"; reloadBtn.classList.add("copied"); }
  let alive = false;
  try {
    const resp = await apiFetch(`${API_BASE}/api/status`, { method: "GET" });
    alive = resp.ok;
  } catch { alive = false; }
  if (reloadBtn) { reloadBtn.textContent = "↻"; reloadBtn.classList.remove("copied"); }
  if (!msgEl) return;
  const textEl = msgEl.querySelector(".msg-text");
  const done = document.createElement("span");
  done.className = "msg-text";
  // Errores entendibles (2026-09-24): antes CUALQUIER error pasaba a
  // "✓ Solucionado" con solo que el backend respondiera -- falso para un
  // rechazo del modelo o un límite de uso, que no se arreglan por eso. Solo
  // un corte de conexión se da por resuelto; al resto se le dice cómo
  // reintentar, sin prometer nada.
  const connectionError = msgEl.dataset.errorKind === "offline";
  if (alive && connectionError) {
    msgEl.classList.remove("msg-error");
    msgEl.classList.add("msg-solved");
    done.textContent = "✓ Vision volvió a responder. Ya podés seguir.";
  } else if (alive) {
    msgEl.classList.remove("msg-solved");
    done.textContent = `${rawText}\nVision está funcionando: volvé a enviar tu mensaje para reintentar.`;
  } else {
    msgEl.classList.add("msg-error");
    msgEl.classList.remove("msg-solved");
    done.textContent = `${rawText}\nTodavía no hay conexión con Vision. Esperá unos segundos y probá de nuevo con ↻.`;
  }
  if (textEl) { textEl.replaceWith(done); } else { msgEl.insertBefore(done, msgEl.firstChild); }
}

async function deleteAssistantResponse(userTurnId, assistantMsgEl) {
  const confirmed = await showConfirmModal({
    title: "Eliminar respuesta",
    message: "Se eliminará esta respuesta. Esta acción no se puede deshacer.",
    acceptLabel: "Eliminar",
  });
  if (!confirmed) return;

  // Pedido del usuario (2026-09): al confirmar, la respuesta debe desaparecer
  // de PANTALLA aunque el backend flaquee. Antes el borrado visual quedaba
  // atado a que el DELETE devolviera ok -- si el backend respondia no-ok (ej.
  // en sesiones reconstruidas) o apiFetch lanzaba, NO se quitaba nada de la
  // vista y el usuario veía "confirmo y no hace nada". Fix: se quita la burbuja
  // del DOM y se poda el chatHistory/localStorage de inmediato (el usuario ya
  // confirmó), y el DELETE al backend queda BEST-EFFORT para mantener la DB
  // consistente cuando sea posible.
  if (assistantMsgEl) assistantMsgEl.remove();

  const userIdx = chatHistory.findIndex((m) => m.messageId === userTurnId);
  if (userIdx !== -1) {
    for (let i = userIdx + 1; i < chatHistory.length; i++) {
      if (chatHistory[i].role === "user") break; // empieza el siguiente turno
      if (chatHistory[i].role === "assistant") { chatHistory.splice(i, 1); break; }
    }
    persistChatHistory();
  }

  try {
    const resp = await apiFetch(
      `${API_BASE}/api/chat/${state.sessionId}/response/${encodeURIComponent(userTurnId)}`,
      { method: "DELETE" }
    );
    if (!resp.ok) {
      const data = await resp.json().catch(() => ({}));
      console.warn("Respuesta borrada localmente; backend:", resp.status, data.detail || "");
    }
  } catch (err) {
    console.warn("Respuesta borrada localmente; error de backend:", err);
  }
}

// REQ-009: tras truncar el historial desde `messageId` en adelante, marca
// visualmente (o remueve) todos los .user-turn-wrap/.msg posteriores en
// el DOM -- el chatHistory local tambien se recorta para que persista
// coherente con lo que el backend ahora tiene.
function markMessagesAfterAsStale(messageId, { removeThis = false } = {}) {
  const idx = chatHistory.findIndex((m) => m.messageId === messageId);
  if (idx === -1) return;

  const cutIdx = removeThis ? idx : idx + 1;
  chatHistory.length = cutIdx;
  persistChatHistory();

  // Reconstruye el DOM de mensajes desde chatHistory (mas simple y
  // confiable que intentar recortar nodos sueltos que pueden incluir
  // tool-call-rows intercaladas).
  clearMessagesDom();
  for (const entry of chatHistory) {
    appendMessage(entry.role, entry.text, { persist: false, messageId: entry.messageId, time: entry.time, responseType: entry.responseType || null, technical: entry.technical || null, errorKind: entry.errorKind || null });
  }
}

// Tras truncar+relanzar (REQ-009), el turno nuevo ya esta corriendo en
// background del lado del servidor -- nos enganchamos a su stream igual
// que la reconexion de "sesion sigue pensando" (activateSession).
// `messageId`: el backend reusa el mismo messageId del mensaje editado
// como id del turno relanzado (ver edit_message_endpoint case 3 en
// app/main.py) -- se pasa aca para que la respuesta quede pegada debajo
// de ESE mensaje (su turnContainer ya existe, reconstruido por
// markMessagesAfterAsStale antes de llamar a esta funcion).
async function attachToRelaunchedTurn(messageId) {
  try {
    setConnStatus("busy", "Vision está trabajando…");
    resetToolRow();
    const resp = await apiFetch(`${API_BASE}/api/chat/stream/${state.sessionId}`);
    await renderIncomingStream(resp, messageId);
  } finally {
    if (connStatusEl.dataset.state === "busy") setConnStatus("online", "Vision en línea");
  }
}
