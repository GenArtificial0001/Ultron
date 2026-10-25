// Vision - frontend vanilla JS (sin build step, consistente con el resto
// del workspace). Habla con el backend FastAPI (app/main.py) via
// POST /api/chat, que responde un stream Server-Sent Events (SSE) real
// sobre el body del POST -- se parsea a mano con el Streams API del
// navegador (fetch + ReadableStream), ya que EventSource nativo del
// browser NO soporta POST con body.

import { renderMarkdown, toPlainText } from "./markdown.js";
import { initVoiceEye, destroyVoiceEye } from "./voice-eye.js";
import { initSplashScreen } from "./splash-screen.js";
import { isTtsEnabled, setTtsEnabled, isTechReportEnabled, setTechReportEnabled, getTechReportLevel, setTechReportLevel, speak, cancelSpeech, speakTestPhrase, pickBestSpanishMaleVoice, getAvailableSpanishVoices, setSavedVoiceURI, playActivationSound, isVoiceMuted, setVoiceMuted, onSpeechEnd, isSpeaking, configureVoiceBackend, applyVoiceConfig, playVoiceSample, TEST_PHRASE } from "./tts.js";
import { typesetMath } from "./math.js";
import { initVault, refreshVault } from "./vault.js";
import { initMicControl, refreshAudioPanel, startWithMicCheck, maybeWatchNoSignal, pendingVoiceMode } from "./mic-control.js";
import {
  initMeetingSource,
  requestMeetingSource,
  buildMeetingAudio,
  refreshMeetingSourceSettings,
  meetingSourceLabel,
} from "./meeting-source.js";
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

// -------------------------------------------------------------------------
// spec informe-tecnico-hablado: todas las frases del informe van por la misma
// cola con un tope de 3 esperando (REQ-004: en orden, sin atrasarse), y si
// pasan ~15 s sin ninguna, se dice en que tarea sigue (REQ-011: nunca callado).
// -------------------------------------------------------------------------
// REQ-004/012: tope de frases del informe esperando su turno. En Detallado se cuentan muchas mas
// cosas por acorde, y con solo 3 se perderia el recorrido; las mas viejas se descartan igual.
const TECH_REPORT_PENDING_LIMIT = 3;
const TECH_REPORT_PENDING_LIMIT_DETAILED = 6;
const TECH_REPORT_KEEPALIVE_MS = 15_000;
let _techKeepAliveTimer = null;
let _techKeepAliveSessionId = null;
let _techLastSpokenAt = 0;
let _techCurrentTask = "";

function speakTechReport(text, { lead = false } = {}) {
  if (!text) return;
  _techLastSpokenAt = Date.now();
  const limit = getTechReportLevel() === "detailed" ? TECH_REPORT_PENDING_LIMIT_DETAILED : TECH_REPORT_PENDING_LIMIT;
  speak(text, { limitKey: "tech-report", limit, lead });
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

// spec voz-fish-audio REQ-001/003: tts.js pide el audio de cada frase al
// backend con este mismo fetch (lleva el token de acceso) y los avisos de
// "esta voz fallo, sigo con otra" van al chat como nota del sistema.
configureVoiceBackend({
  fetchFn: apiFetch,
  apiBase: API_BASE,
  onNotice: (text) => appendMessage("system-note", text, { persist: false }),
});

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
  "No hay conexión con ULTRON. Puede que se haya cerrado o se esté reiniciando: esperá unos segundos y volvé a intentar. Si no vuelve, abrí ULTRON de nuevo.";
const UNEXPECTED_ERROR_MESSAGE =
  "Ocurrió un problema inesperado. Volvé a intentar; si se repite, cerrá y volvé a abrir ULTRON.";

const HTTP_STATUS_MESSAGES = {
  400: "ULTRON no pudo procesar el pedido: falta un dato o alguno no es válido. Revisalo y volvé a intentar.",
  401: "Este navegador no tiene la clave de acceso de ULTRON, o no es la correcta. Cargala en Configuración > Red, en «Token de acceso».",
  403: "No tenés permiso para hacer esto. Si lo necesitás, avisá a soporte.",
  404: "No se encontró lo que buscabas: puede que se haya borrado o movido. Recargá la página y volvé a intentar.",
  408: "La operación tardó demasiado. Volvé a intentar.",
  409: "ULTRON está ocupada con otra tarea en esta conversación. Esperá a que termine y volvé a intentar.",
  413: "Lo que intentaste enviar es demasiado grande. Probá con un archivo o una imagen más chica.",
  422: "Falta un dato o alguno no tiene el formato esperado. Revisalo y volvé a intentar.",
  429: "Hay demasiados pedidos seguidos. Esperá unos segundos y volvé a intentar.",
  500: "ULTRON tuvo un problema interno. Volvé a intentar; si se repite, cerrá y volvé a abrir ULTRON.",
  502: "Un servicio externo (la inteligencia artificial o una base de datos) no respondió bien. Volvé a intentar en unos minutos.",
  503: "ULTRON o un servicio externo no está disponible en este momento. Volvé a intentar en unos minutos.",
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
    return "ULTRON devolvió una respuesta que no se pudo leer. Volvé a intentar; si se repite, cerrá y volvé a abrir ULTRON.";
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
    return "ULTRON no tiene permiso para usar el micrófono. Habilitalo desde el candado de la barra de direcciones y volvé a intentar.";
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

// -------------------------------------------------------------------------
// REQ-016 (spec fix-detener-y-progreso-visible): preferencias del navegador (tema, «Leer en voz alta», auto-envío, micrófono,
// alarma del loop...). Con el almacenamiento del navegador lleno o bloqueado, `setItem` tira (QuotaExceededError) y antes cada
// lugar lo tragaba en silencio: la preferencia no se guardaba y, en las que se releen de localStorage cada vez («Leer en voz
// alta», auto-envío, idioma), ni siquiera regía en esta carga («Leer en voz alta» quedaba tildado y no leía; medido en la spec,
// sección D). Ahora pasan todas por acá: si no se puede guardar, el valor vale EN MEMORIA hasta recargar y se avisa UNA vez en
// pantalla. `tts.js` (módulo sin dependencias de app.js) hace lo mismo con su propio respaldo y avisa con el mismo evento.
// -------------------------------------------------------------------------
const PREF_NOT_SAVED_EVENT = "vision:preference-not-saved";
const PREF_NOT_SAVED_TEXT =
  "No se pudo guardar tu preferencia en este navegador (su espacio de guardado está lleno o bloqueado). El cambio vale hasta que recargues la página.";
const _unsavedPrefs = new Map();

function readPreference(key) {
  if (_unsavedPrefs.has(key)) return _unsavedPrefs.get(key);
  try {
    return localStorage.getItem(key);
  } catch {
    return null; // localStorage inaccesible: cada preferencia cae a su valor por defecto
  }
}

function savePreference(key, value) {
  const text = String(value);
  try {
    localStorage.setItem(key, text);
    _unsavedPrefs.delete(key);
    return true;
  } catch {
    _unsavedPrefs.set(key, text);
    window.dispatchEvent(new CustomEvent(PREF_NOT_SAVED_EVENT, { detail: { key } }));
    return false;
  }
}

function forgetPreference(key) {
  _unsavedPrefs.delete(key);
  try {
    localStorage.removeItem(key);
  } catch {
    // noop -- sin localStorage no habia nada guardado que borrar
  }
}

// Una sola vez por carga aunque fallen varias (cada arrastre del panel Navegador, por ejemplo, vuelve a intentar guardar).
// Se busca el aviso recien al necesitarlo: la primera falla puede llegar mientras el modulo todavia se esta evaluando.
let _prefNoticeShown = false;
window.addEventListener(PREF_NOT_SAVED_EVENT, () => {
  const noticeEl = document.getElementById("pref-save-notice");
  if (_prefNoticeShown || !noticeEl) return;
  _prefNoticeShown = true;
  document.getElementById("pref-save-notice-text").textContent = PREF_NOT_SAVED_TEXT;
  noticeEl.classList.remove("hidden");
});
document.getElementById("pref-save-notice-close").addEventListener("click", () => {
  document.getElementById("pref-save-notice").classList.add("hidden");
});

// REQ-016 (D2 del usuario: «el chat se lee siempre de SQLite»): la conversacion vive en SQLite (`<carpeta>/session/vision.db`) y
// el navegador ya NO guarda una copia de las sesiones CON carpeta. Antes copiaba TODAS, enteras y sin limite (cada sesion que se
// abria), y el medidor «Caché local» sumaba esas copias contra 300 000 caracteres (el 5,7 % de la cuota real del navegador)
// diciendo ser «de esta conversación»; «Liberar caché» borraba solo la abierta. Queda una copia CHICA solo para las sesiones SIN
// carpeta: sus mensajes NO estan en SQLite (viven en la memoria del backend, spec carpeta-por-sesion REQ-008 y
// `persistence/service.py:_db_path_for_session`, que devuelve None) y sin copia un F5 las dejaria vacias.
const SESSION_COPY_PREFIX = "vision.session.";
const LOCAL_COPY_MAX_MESSAGES = 50;
const LOCAL_COPY_MAX_CHARS = 200_000;
const LOCAL_COPY_ROLES = new Set(["user", "assistant", "system-note", "error"]);
// sessionId -> true (tiene carpeta) / false (no tiene): sale de /status (`persistence_available`), de asignarle una carpeta y de
// la lista de sesiones del backend. Mientras no se sabe, no se escribe ninguna copia.
const _sessionHasFolder = new Map();

// Escapa a una clave PASADA explicitamente, no a la sesion activa en vivo.
// Necesario para persistir el resultado de un turno en la sesion que LO
// ORIGINO aunque el usuario ya este viendo otra (bug real reportado: al
// cambiar de sesion a mitad de un turno, la respuesta se escribia en la
// sesion equivocada porque el bloque de completado leia state.sessionId
// en vivo -- ver el bloque de completado de renderIncomingStream).
function cacheStorageKeyForSession(sessionId) {
  return `${SESSION_COPY_PREFIX}${sessionId}`;
}

const messagesEl = document.getElementById("messages");
const todoListEl = document.getElementById("todo-list");
const toolLogEl = document.getElementById("tool-log");
const questionPanelEl = document.getElementById("question-panel");
// REQ-018 (spec fix-detener-y-progreso-visible): la pregunta (tool `question`) que cada sesion dejo esperando respuesta. El panel es uno
// solo para toda la pagina: la pregunta se ve y se contesta SOLO en su sesion; si llega con el usuario en otra, queda aca hasta que
// vuelva, y la lista de sesiones la marca («❓ te hizo una pregunta», D5). Vive en la memoria de esta pestaña; con F5 la de la sesion que
// se abre se recupera de SQLite (`pending_question` de GET /api/sessions/{id}/messages, ver rememberQuestionFromHistory).
// `_questionSessionsSeen`: sesiones de las que esta pestaña ya sabe que paso con su pregunta (llego en vivo, se contesto, se descarto):
// para esas manda lo de aca, no lo de SQLite.
const _pendingQuestions = new Map(); // sessionId -> { question, options }
const _questionSessionsSeen = new Set();
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
//   - Dentro de una MISMA operacion el % nunca retrocede (`Math.max`).
//   - Al superar el presupuesto, se pasa a modo "sobrepasado" (sigue
//     trabajando, mas de lo esperado) en vez de fingir precision.
//
// spec fix-detener-y-progreso-visible (reporte del usuario 2026-10-11: «se
// quedo en este proceso y no avanzó o si estaba avanzando me dio la idea de
// que estaba congelado», captura «Ejecutando comando… 91%» quieta; el turno
// avanzaba, la pantalla no lo decia):
//   - REQ-001: la tarjeta muestra SIEMPRE cuanto lleva la operacion actual
//     («lleva 12 s», «lleva 1 min 05 s»), en todos los modos (tambien sin
//     historial y con % real), con un latido; pasado lo estimado dice
//     «… · sigue trabajando · lleva X» sin % y sin el relleno clavado.
//   - REQ-002: cada operacion nueva arranca de 0 % (antes el % se
//     arrastraba entre operaciones: una vez en 91 %, todo lo que seguia
//     arrancaba en 91 % fijo); el resultado de una herramienta cierra su
//     operacion, y un cierre pendiente nunca borra la operacion siguiente.
//   - REQ-003: «Escribiendo <archivo>… N KB» mientras el modelo escribe los
//     argumentos de una herramienta (`tool-writing`).
//   - REQ-004: «Salida en vivo» del bash (`tool-output`), como TEXTO.
// -------------------------------------------------------------------------

// Cada cuanto se repinta la barra. 100ms es suficientemente fluido para el
// ojo y despreciable para el CPU; mas rapido no se percibe.
const PROGRESS_TICK_MS = 100;

// Cuanto se puede pasar del presupuesto estimado antes de admitir que la
// estimacion fallo y pasar a modo "sobrepasado".
// REQ-001 (spec fix-detener-y-progreso-visible): antes era 2x, y entre 1x y
// 2x la curva mostraba un % que ya se sabia inventado (81-91 %) y despues el
// numero desaparecia con el relleno clavado en 91 %. Ahora, apenas se pasa
// lo estimado, la tarjeta lo dice: «sigue trabajando · lleva X», sin %.
const OVERRUN_FACTOR = 1;

// Techo del avance estimado: la barra se acerca asintoticamente a esto y
// solo llega a 100% cuando el turno REALMENTE termina.
const PROGRESS_CEILING = 92;

// Umbral por debajo del cual NO se muestra barra (hallazgo empirico de la
// validacion, ver spec §6): tools locales como `read`/`grep`/`todowrite`
// promedian 0-1.3ms de historial real. Mostrar una barra para algo que
// dura menos que un frame produce un parpadeo que se lee como un glitch,
// no como progreso. Solo se aplica cuando HAY historial: sin historial no
// se sabe si va a ser rapida, y ahi si corresponde el modo indeterminado.
// REQ-001/004 (spec fix-detener-y-progreso-visible): una operacion "rapida"
// que NO termina en este plazo (p. ej. un bash largo despues de muchos
// comandos cortos: la mediana queda chica) igual se muestra, ya como
// «sigue trabajando», con su tiempo real y su salida en vivo.
const MIN_VISIBLE_BUDGET_MS = 250;

// REQ-002: cuanto se ve el 100 % de una operacion terminada antes de quitar la tarjeta.
const PROGRESS_DONE_LINGER_MS = 400;

// mode: "estimate" (curva con %) | "indeterminate" (sin historial / escribiendo argumentos) |
// "real" (% real de un PDF) | "overrun" (se paso de lo estimado) | "done".
const progressState = {
  timer: null,
  startedAt: 0,
  budgetMs: 0,
  pct: 0,
  mode: "idle",
  opId: null,
  resetTimer: null,
  pending: null,
  pendingTimer: null,
};

// REQ-003: cuando empezo a escribir los argumentos cada tool-call (por id), para que el tiempo no vuelva a 0 si la tarjeta alterna.
const _writingStartedAt = new Map();

// REQ-004: si el usuario pliega «Salida en vivo», queda plegada para las proximas operaciones (solo en memoria de esta pestaña).
let _liveOutputOpen = true;

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
  // REQ-002 (spec fix-detener-y-progreso-visible, revision F4): la tarjeta TERMINADA del turno anterior vive en el lugar de OTRO mensaje
  // (el siguiente de la cola arranco antes de los 400 ms de su cierre diferido, que la operacion nueva cancela): se quita aca. Antes
  // quedaba pegada para siempre («… · tardó X 100%») debajo de una respuesta ya terminada.
  if (activeProgressBar && progressState.mode === "done") activeProgressBar.root.remove();
  const root = document.createElement("div");
  root.className = "command-progress hidden";
  root.dataset.state = "idle";
  root.setAttribute("role", "status");
  root.setAttribute("aria-live", "polite");

  const head = document.createElement("div");
  head.className = "progress-head";
  // REQ-001: latido -- un punto que pulsa mientras la operacion sigue viva (quieto con animaciones reducidas: el tiempo de al lado
  // sigue cambiando igual, porque lo mueve el timer de JS, no la animacion).
  const beat = document.createElement("span");
  beat.className = "progress-beat";
  beat.setAttribute("aria-hidden", "true");
  const label = document.createElement("span");
  label.className = "progress-label";
  const elapsed = document.createElement("span");
  elapsed.className = "progress-elapsed";
  // La tarjeta es una region `aria-live`: un reloj que cambia cada segundo no se anuncia (seria un lector de pantalla hablando sin parar).
  elapsed.setAttribute("aria-live", "off");
  const pct = document.createElement("span");
  pct.className = "progress-pct";
  head.append(beat, label, elapsed, pct);

  const track = document.createElement("div");
  track.className = "progress-track";
  const fill = document.createElement("div");
  fill.className = "progress-fill";
  track.appendChild(fill);

  root.append(head, track);
  target.appendChild(root);

  activeProgressBar = { root, labelEl: label, elapsedEl: elapsed, pctEl: pct, fillEl: fill, liveEl: null, livePreEl: null };
  return activeProgressBar;
}

/** «12 s», «1 min 05 s», «1 h 02 min» (REQ-001). */
function formatElapsed(ms) {
  const total = Math.max(0, Math.floor(ms / 1000));
  if (total < 60) return `${total} s`;
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = String(total % 60).padStart(2, "0");
  if (h > 0) return `${h} h ${String(m).padStart(2, "0")} min`;
  return `${m} min ${s} s`;
}

function cancelPendingProgress() {
  clearTimeout(progressState.pendingTimer);
  progressState.pendingTimer = null;
  progressState.pending = null;
}

/**
 * REQ-002 (spec fix-detener-y-progreso-visible): arranca una operacion NUEVA en la tarjeta. Siempre de 0 %, con su propio reloj, y
 * cancelando cualquier cierre pendiente de la anterior (sin esto, el `finishProgress` del resultado de una herramienta borraba a los
 * 400 ms la tarjeta de la operacion que arrancaba enseguida).
 */
function beginProgressOp({ opId = null, label, budgetMs = 0, mode, startedAt = null }, mountEl) {
  cancelPendingProgress();
  clearTimeout(progressState.resetTimer);
  progressState.resetTimer = null;
  const bar = getOrCreateProgressBar(mountEl);
  bar.labelEl.textContent = label || "Trabajando…";
  bar.root.classList.remove("hidden");
  removeLiveOutput(bar);
  progressState.opId = opId;
  progressState.mode = mode;
  progressState.pct = 0;
  progressState.startedAt = startedAt ?? performance.now();
  progressState.budgetMs = budgetMs > 0 ? budgetMs : 4000;
  bar.root.dataset.state = mode === "indeterminate" ? "indeterminate" : "running";
  bar.pctEl.textContent = "";
  bar.fillEl.style.width = mode === "indeterminate" ? "" : "0%";
  if (!progressState.timer) {
    progressState.timer = setInterval(tickProgress, PROGRESS_TICK_MS);
  }
  tickProgress();
}

function startProgress({ label, budgetMs, hasHistory, opId = null }, mountEl) {
  const startedAt = performance.now();
  // Operacion conocida y sub-perceptual: no se toca la barra todavia. Si ya hay
  // una visible de una operacion anterior, se la deja seguir en vez de pisarla
  // con una que va a durar 1ms. Pero si NO termina en el plazo (REQ-001/004),
  // se muestra con su tiempo real (ya pasada de lo estimado).
  if (hasHistory && budgetMs < MIN_VISIBLE_BUDGET_MS) {
    cancelPendingProgress();
    progressState.pending = { opId, label, budgetMs, mode: "estimate", startedAt, mountEl };
    progressState.pendingTimer = setTimeout(adoptPendingProgress, MIN_VISIBLE_BUDGET_MS);
    return;
  }
  // REQ-004: sin ninguna medicion previa de esta operacion no hay base
  // para inventar un %. Se muestra animacion indeterminada + el texto
  // contextual, que es informacion honesta y util igual.
  beginProgressOp({ opId, label, budgetMs, mode: hasHistory ? "estimate" : "indeterminate", startedAt }, mountEl);
}

function adoptPendingProgress() {
  const pending = progressState.pending;
  progressState.pendingTimer = null;
  if (!pending) return;
  // Si mientras tanto se cambio de sesion, el slot de ese turno ya no esta en pantalla: no se dibuja nada huerfano.
  if (pending.mountEl && !pending.mountEl.isConnected) {
    progressState.pending = null;
    return;
  }
  beginProgressOp(pending, pending.mountEl);
}

function tickProgress() {
  if (!activeProgressBar) return;
  const bar = activeProgressBar;
  const elapsed = performance.now() - progressState.startedAt;
  const mode = progressState.mode;
  if (mode === "done") return;

  if (mode === "estimate" && elapsed / progressState.budgetMs >= OVERRUN_FACTOR) {
    // REQ-003 (barra-progreso-adaptativa) + REQ-001 (fix-detener-y-progreso-visible): tarda mas de lo estimado. No se miente con un
    // % que ya se sabe malo: sin numero, el relleno pasa a un barrido (nunca clavado en 91 %) y el texto dice que sigue trabajando y
    // cuanto lleva. El registro de esta duracion real (que el backend hace igual) hara que la proxima estimacion sea mas generosa.
    progressState.mode = "overrun";
    bar.root.dataset.state = "overrun";
    bar.pctEl.textContent = "";
    bar.fillEl.style.width = "";
  }

  // REQ-001: el tiempo transcurrido de la operacion actual, en TODOS los modos.
  const overrun = progressState.mode === "overrun";
  bar.elapsedEl.textContent = `${overrun ? " · sigue trabajando" : ""} · lleva ${formatElapsed(elapsed)}`;
  if (progressState.mode !== "estimate") return;

  // Curva asintotica: 1 - e^(-k·t). Avanza rapido al principio (donde el
  // usuario mas necesita feedback de que algo pasa) y desacelera cerca
  // del techo, sin llegar nunca solo al 100%.
  const ratio = elapsed / progressState.budgetMs;
  const target = PROGRESS_CEILING * (1 - Math.exp(-2.2 * ratio));
  progressState.pct = Math.max(progressState.pct, target);

  const shown = Math.round(progressState.pct);
  bar.fillEl.style.width = `${shown}%`;
  bar.pctEl.textContent = `${shown}%`;
}

/**
 * REQ-008 b (spec adjuntar-archivos-chat): un `progress-estimate` con `pct` trae un avance REAL
 * (pagina k de N al leer un PDF), no un presupuesto de tiempo. Se fija ese valor, se detiene la
 * curva asintotica (que solo adivina) y nunca retrocede dentro de la misma operacion. Sin barra
 * montada, se crea una. El reloj de la operacion (REQ-001) sigue corriendo.
 */
function setRealProgress({ label, pct }, mountEl) {
  if (!activeProgressBar || progressState.mode !== "real") {
    // REQ-002 (fix-detener-y-progreso-visible): primera señal REAL de esta operacion: arranca de 0 %, sin heredar el % de la anterior
    // (p. ej. el «Pensando…» que seguia en pantalla porque la lectura del PDF suele estimarse rapida y no tenia tarjeta propia). El
    // reloj es el de la herramienta: el de su estimacion pendiente, o el de la operacion en curso si ya era ella.
    const pending = progressState.pending;
    const alive = activeProgressBar && progressState.mode !== "done" && progressState.mode !== "idle";
    beginProgressOp(
      {
        opId: pending ? pending.opId : alive ? progressState.opId : null,
        label,
        mode: "real",
        startedAt: pending ? pending.startedAt : alive ? progressState.startedAt : null,
      },
      mountEl
    );
  }
  const bar = getOrCreateProgressBar(mountEl);
  bar.labelEl.textContent = label || "Trabajando…";
  bar.root.classList.remove("hidden");
  progressState.pct = Math.max(progressState.pct, Math.min(100, Math.max(0, pct)));
  const shown = Math.round(progressState.pct);
  bar.root.dataset.state = "running";
  bar.fillEl.style.width = `${shown}%`;
  bar.pctEl.textContent = `${shown}%`;
}

/**
 * REQ-003 (spec fix-detener-y-progreso-visible): el modelo esta escribiendo los argumentos de una herramienta (caso real: un `write`
 * de 15 KB tardo ~69 s sin ninguna señal). «Escribiendo <archivo>… N KB» si se sabe el archivo de un write/edit; si no, «Preparando
 * <herramienta>… N KB». Sin % (no se sabe cuanto falta), con el tiempo que lleva escribiendo.
 */
function writingLabel({ name, chars, path }) {
  const kb = chars >= 1024 ? ` ${Math.round(chars / 1024)} KB` : "";
  if (path && (name === "write" || name === "edit")) return `Escribiendo ${path}…${kb}`;
  return `Preparando ${name || "herramienta"}…${kb}`;
}

function showWritingProgress(data, mountEl) {
  const opId = `writing:${data.id}`;
  const label = writingLabel(data);
  if (activeProgressBar && progressState.opId === opId && progressState.mode !== "done") {
    activeProgressBar.labelEl.textContent = label; // misma operacion: solo crece el tamaño, el reloj sigue
    return;
  }
  if (!_writingStartedAt.has(data.id)) _writingStartedAt.set(data.id, performance.now());
  beginProgressOp({ opId, label, mode: "indeterminate", startedAt: _writingStartedAt.get(data.id) }, mountEl);
}

function removeLiveOutput(bar) {
  if (!bar || !bar.liveEl) return;
  bar.liveEl.remove();
  bar.liveEl = null;
  bar.livePreEl = null;
}

/**
 * REQ-004 (spec fix-detener-y-progreso-visible): ultimas lineas de la salida del bash que esta corriendo, plegable. SIEMPRE con
 * textContent: es salida de un comando cualquiera y puede traer HTML. Solo para la operacion de ESE id (una salida atrasada de un
 * comando que ya termino no se pega a la operacion siguiente).
 */
function showLiveOutput({ id, tail }) {
  if (progressState.pending && progressState.pending.opId === id) adoptPendingProgress(); // hay salida real: se muestra ya
  const bar = activeProgressBar;
  if (!bar || progressState.opId !== id || progressState.mode === "done") return;
  if (!bar.liveEl) {
    const details = document.createElement("details");
    details.className = "progress-live";
    details.setAttribute("aria-live", "off"); // idem el reloj: la salida cambia cada segundo
    details.open = _liveOutputOpen;
    details.addEventListener("toggle", () => {
      _liveOutputOpen = details.open;
    });
    const summary = document.createElement("summary");
    summary.textContent = "Salida en vivo";
    const pre = document.createElement("pre");
    details.append(summary, pre);
    bar.root.appendChild(details);
    bar.liveEl = details;
    bar.livePreEl = pre;
  }
  bar.livePreEl.textContent = typeof tail === "string" ? tail : "";
  bar.livePreEl.scrollTop = bar.livePreEl.scrollHeight;
}

function stopProgressTimer() {
  if (progressState.timer) {
    clearInterval(progressState.timer);
    progressState.timer = null;
  }
}

function finishProgress() {
  stopProgressTimer();
  cancelPendingProgress();
  // Cierre visual: se completa a 100% y recien ahi se oculta, para que el
  // usuario vea que termino en vez de que la barra desaparezca a mitad.
  const bar = activeProgressBar;
  if (bar && progressState.mode !== "indeterminate" && progressState.mode !== "idle") {
    bar.root.dataset.state = "done";
    bar.fillEl.style.width = "100%";
    bar.pctEl.textContent = "100%";
  }
  if (bar && progressState.mode !== "idle" && progressState.mode !== "done") {
    // REQ-001: termino -- ni «sigue trabajando» ni un reloj que ya no corre al lado del 100 %.
    bar.elapsedEl.textContent = ` · tardó ${formatElapsed(performance.now() - progressState.startedAt)}`;
  }
  progressState.mode = "done";
  clearTimeout(progressState.resetTimer);
  // REQ-002: el cierre diferido queda atado a ESTA tarjeta: si mientras tanto arranco otra operacion (o se cambio de sesion), no la borra.
  progressState.resetTimer = setTimeout(() => {
    progressState.resetTimer = null;
    if (activeProgressBar === bar && progressState.mode === "done") resetProgress();
  }, PROGRESS_DONE_LINGER_MS);
}

function resetProgress() {
  stopProgressTimer();
  cancelPendingProgress();
  clearTimeout(progressState.resetTimer);
  progressState.resetTimer = null;
  // A diferencia de resetToolRow (que solo limpia la referencia y deja
  // los badges "done" visibles como historial del turno), la barra de
  // progreso SI se remueve del DOM al terminar -- es un indicador
  // transitorio de actividad, no un registro de lo que se hizo.
  if (activeProgressBar) {
    activeProgressBar.root.remove();
    activeProgressBar = null;
  }
  progressState.pct = 0;
  progressState.mode = "idle";
  progressState.opId = null;
  _writingStartedAt.clear();
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
  if (footer) footer.textContent = company || "ULTRON";
  document.title = company ? `ULTRON · ${company}` : "ULTRON · Asistente Inteligente";
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
    // REQ-009 (spec portar-desde-origen): la primera vez, ULTRON avisa que trajo los datos de Vision-Personal (el backend lo manda una sola vez).
    if (data.data_migration && data.data_migration.message) {
      appendMessage("system-note", data.data_migration.message, { persist: false });
    }
    setConnStatus("online", "ULTRON en línea");
  } catch (err) {
    setConnStatus("error", "Sin conexión al backend");
  }
}

// spec estado-conexion-en-vivo (pedido del usuario 2026-10-19: «ni bien se cierre la consola tendria que mostrarse en rojo»): el indicador se
// consultaba UNA sola vez al cargar y quedaba verde para siempre. Ahora un latido liviano a /api/status cada 3 s lo mantiene fiel:
// 2 fallos seguidos (~6 s) = rojo con la causa probable en palabras; 1 acierto = vuelve (y se reconsultan modelo y version).
// No consulta con la pestaña oculta; nunca hay dos latidos a la vez; cada uno vence a los 2,5 s. El estado «trabajando» no se pisa a verde.
const CONN_BEAT_MS = 3000;
const CONN_BEAT_TIMEOUT_MS = 2500;
const CONN_BEAT_FAILS_TO_RED = 2;
const CONN_LOST_TEXT = "Sin conexión con ULTRON. ¿Cerraste la ventana del servidor?";
let connBeatInFlight = false;
let connBeatFails = 0;

async function connHeartbeat() {
  if (connBeatInFlight || document.hidden) return;
  connBeatInFlight = true;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), CONN_BEAT_TIMEOUT_MS);
  let alive = false;
  try {
    const resp = await apiFetch(`${API_BASE}/api/status`, { signal: controller.signal, cache: "no-store" });
    alive = resp.ok;
  } catch {
    alive = false; // servidor cerrado, red caida o plazo vencido: es justamente lo que el latido viene a detectar
  } finally {
    clearTimeout(timer);
    connBeatInFlight = false;
  }
  if (alive) {
    connBeatFails = 0;
    // Solo el rojo vuelve por este camino: «conectando», «en linea» y «trabajando» ya son correctos y el turno decide cuando termina.
    if (connStatusEl.dataset.state === "error") checkBackendStatus();
    return;
  }
  connBeatFails += 1;
  if (connBeatFails >= CONN_BEAT_FAILS_TO_RED && connStatusTextEl.textContent !== CONN_LOST_TEXT) {
    setConnStatus("error", CONN_LOST_TEXT); // tambien estando «trabajando»: si el servidor murio, ya no esta trabajando
  }
}

const connBeatTimer = setInterval(connHeartbeat, CONN_BEAT_MS);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) connHeartbeat(); // al volver a la pestaña se muestra la verdad al instante
});
window.addEventListener("pagehide", () => clearInterval(connBeatTimer));

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
  return readPreference(MODEL_STORAGE_KEY) || "";
}

function saveSelectedModel(modelId) {
  state.selectedModel = modelId;
  state.activeModel = "";
  state.activeModelJumped = false;
  renderModelChip();
  savePreference(MODEL_STORAGE_KEY, modelId); // REQ-016: si no se guarda, rige igual (state) y se avisa una vez
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
// Historial en pantalla. `chatHistory` = los mensajes ya pintados de la sesion ACTIVA (rol + texto + id + hora). REQ-016 (spec
// fix-detener-y-progreso-visible): se llena SIEMPRE desde SQLite -- al abrir la pagina o con F5 (restoreActiveSessionOnLoad) y al
// cambiar de sesion (activateSession), los dos por `fetchSessionHistory` --, y la copia en el navegador solo existe para las
// sesiones sin carpeta (ver SESSION_COPY_PREFIX arriba). Antes F5 leia SOLO esa copia y cambiar de sesion leia SQLite: dos
// fuentes para lo mismo (spec, seccion D).
// -------------------------------------------------------------------------
const chatHistory = [];

function sessionHasFolder(sessionId) {
  if (_sessionHasFolder.has(sessionId)) return _sessionHasFolder.get(sessionId);
  // La lista de sesiones del backend (refreshSessionsFromBackend) deja la carpeta en el indice liviano: si esta, la tiene.
  return loadSessionsIndex()[sessionId]?.projectPath ? true : undefined;
}

function rememberSessionFolder(sessionId, hasFolder) {
  if (typeof hasFolder !== "boolean") return;
  _sessionHasFolder.set(sessionId, hasFolder);
  // Recien ahora se sabe si a esta sesion le corresponde copia: si es la abierta y no tiene carpeta, se guarda la suya.
  if (!hasFolder && sessionId === state.sessionId) persistChatHistory();
}

// Lee la copia del navegador de UNA sesion (no necesariamente la activa: el bloque de completado de renderIncomingStream la usa
// para la sesion que ORIGINO el turno aunque el usuario ya este mirando otra). Una copia rota o con otra forma se ignora: antes
// un `{...}` en vez de una lista rompia la pagina entera al cargar (`chatHistory` no era iterable).
function readLocalCopy(sessionId) {
  try {
    return parseLocalCopy(localStorage.getItem(cacheStorageKeyForSession(sessionId)));
  } catch {
    return []; // localStorage inaccesible
  }
}

function parseLocalCopy(raw) {
  if (!raw) return [];
  let data;
  try {
    data = JSON.parse(raw);
  } catch {
    return []; // copia rota: se ignora
  }
  if (!Array.isArray(data)) return [];
  return data.filter((e) => e && typeof e === "object" && LOCAL_COPY_ROLES.has(e.role) && typeof e.text === "string");
}

function removeLocalCopy(sessionId) {
  try {
    localStorage.removeItem(cacheStorageKeyForSession(sessionId));
  } catch {
    // noop -- sin localStorage no hay copia que borrar
  }
}

// La copia acotada: los ultimos LOCAL_COPY_MAX_MESSAGES mensajes y, si aun asi pasan de LOCAL_COPY_MAX_CHARS, se sueltan los mas
// viejos. Un unico mensaje mas grande que el tope se guarda recortado (su principio).
function boundedLocalCopy(history) {
  const tail = history.slice(-LOCAL_COPY_MAX_MESSAGES);
  const sizes = tail.map((entry) => JSON.stringify(entry).length + 1); // +1: la coma entre elementos
  let total = sizes.reduce((sum, n) => sum + n, 1); // +1: los corchetes (menos la coma que sobra)
  let start = 0;
  while (total > LOCAL_COPY_MAX_CHARS && start < tail.length - 1) total -= sizes[start++];
  const kept = tail.slice(start);
  let json = JSON.stringify(kept);
  if (json.length > LOCAL_COPY_MAX_CHARS && kept.length === 1) {
    const only = kept[0];
    let cut = LOCAL_COPY_MAX_CHARS;
    do {
      cut = Math.floor(cut * 0.8);
      json = JSON.stringify([{ ...only, text: `${String(only.text).slice(0, cut)}…` }]);
    } while (json.length > LOCAL_COPY_MAX_CHARS);
  }
  return json;
}

function persistChatHistory() {
  persistHistoryForSession(state.sessionId, chatHistory);
}

// Guarda la copia acotada de UNA sesion puntual (no necesariamente la activa) -- centro del fix del bug de cambiar de sesion a
// mitad de turno. REQ-016: con carpeta (o sin saberlo todavia) no se copia nada, el chat esta en SQLite. Una copia que quedo de
// antes de asignarle carpeta se conserva hasta que SQLite devuelva mensajes (fetchSessionHistory la borra ahi): asi un F5 justo
// despues de asignarla no deja el chat vacio.
function persistHistoryForSession(sessionId, history) {
  if (sessionHasFolder(sessionId) !== false) return;
  if (history.length === 0) {
    removeLocalCopy(sessionId);
    return;
  }
  try {
    localStorage.setItem(cacheStorageKeyForSession(sessionId), boundedLocalCopy(history));
  } catch (err) {
    // Navegador lleno o bloqueado: el chat sigue andando (el backend tiene la conversacion en memoria); lo unico que se pierde es
    // que un F5 de esta sesion SIN carpeta la muestre -- eso ya lo avisa el banner «sin carpeta de proyecto».
    console.warn("No se pudo guardar la copia local de esta conversación:", err);
  }
}

// SQLite le da a cada respuesta del asistente su PROPIO id, pero en pantalla (en vivo, ver closeCurrentAssistantTurn, y en la
// copia vieja) la respuesta lleva el id del mensaje de USUARIO que la origino: con ese id ↻ (volver a generar) y 🗑 de la
// respuesta encuentran su turno. Sin esto, tras cambiar de sesion -- y desde REQ-016 tambien tras F5 -- ↻ buscaba el turno por
// el id de la respuesta y tomaba la propia respuesta como si fuera la pregunta.
function withTurnIds(messages) {
  let turnId = null;
  return messages.map((m) => {
    if (m.role === "user") {
      turnId = m.messageId || null;
      return m;
    }
    return m.role === "assistant" && turnId ? { ...m, messageId: turnId } : m;
  });
}

// REQ-016: EL camino para traer el chat de una sesion -- lo usan F5/abrir la pagina y cambiar de sesion. SQLite es la autoridad
// (`GET /api/sessions/{id}/messages`); la copia del navegador es solo la red de seguridad cuando SQLite no tiene nada (sesion sin
// carpeta, que vive en memoria) o Vision no responde. Cuando SQLite SI trae mensajes, la copia sobra y se borra.
async function fetchSessionHistory(sessionId) {
  try {
    const resp = await apiFetch(`${API_BASE}/api/sessions/${encodeURIComponent(sessionId)}/messages`);
    const data = resp.ok ? await resp.json() : null;
    const messages = Array.isArray(data?.messages) ? data.messages : [];
    if (data) rememberQuestionFromHistory(sessionId, data.pending_question); // REQ-018: la pregunta que quedo sin contestar (p. ej. tras F5)
    if (messages.length > 0) {
      _sessionHasFolder.set(sessionId, true); // solo una sesion con carpeta tiene mensajes en SQLite
      removeLocalCopy(sessionId);
      return withTurnIds(messages);
    }
  } catch {
    // Sin conexion con Vision: se cae a la copia de abajo (si la hay), sin romper la pantalla.
  }
  return readLocalCopy(sessionId);
}

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
  return readPreference(TTS_LOOP_KEY) === "true";
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
function appendMessage(role, text, { persist = true, messageId = null, mountEl = null, time = null, images = null, attachments = null, responseType = null, technical = null, errorKind = null } = {}) {
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

  // REQ-004/010 (spec adjuntar-archivos-chat): chips de los archivos adjuntos, tambien
  // al reconstruir el historial tras recargar (estado no encontrado / eliminado).
  if (role === "user" && attachments && attachments.length > 0) {
    div.appendChild(buildMessageAttachmentsRow(attachments));
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
      chatHistory.push({ role, text, messageId: messageId || undefined, time: msgTime || undefined, attachments: attachments && attachments.length > 0 ? attachments : undefined });
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
      chatHistory.push({ role, text, messageId: messageId || undefined, time: msgTime || undefined, attachments: attachments && attachments.length > 0 ? attachments : undefined });
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
// spec conexiones-con-nombre Fase B (REQ-006): el backend emite `confluence-tree` (arbol, resultados de busqueda o pagina abierta) y el chat lo pinta como un
// bloque seleccionable. Click en una pagina = pedirle a Vision que la abra (mismo camino que las opciones de una pregunta). Cada nodo lleva «Abrir en Confluence».
// TODO el texto viene del wiki y es dato no confiable: textContent siempre, y los links solo si son https (un `javascript:` en una URL no se vuelve un link).
function _safeHttpUrl(url) {
  try {
    const parsed = new URL(url, window.location.href);
    return parsed.protocol === "https:" || parsed.protocol === "http:" ? parsed.href : "";
  } catch {
    return "";
  }
}

function _buildCfNode(node, depth) {
  const li = document.createElement("li");
  li.className = "cf-chat-node";
  const row = document.createElement("div");
  row.className = "cf-chat-row";
  row.style.paddingLeft = `${depth * 14}px`;

  const hasChildren = Array.isArray(node.children) && node.children.length > 0;
  const toggle = document.createElement("button");
  toggle.type = "button";
  toggle.className = "cf-chat-toggle";
  toggle.textContent = hasChildren ? "−" : "·";
  toggle.disabled = !hasChildren;
  toggle.setAttribute("aria-label", hasChildren ? "Contraer o expandir" : "Sin subpáginas");

  const title = document.createElement("button");
  title.type = "button";
  title.className = "cf-chat-title";
  title.textContent = node.title || "(sin título)";
  title.title = node.subtitle ? `Espacio ${node.subtitle}: pedirle a ULTRON que abra esta página` : "Pedirle a ULTRON que abra esta página";
  title.addEventListener("click", () => {
    if (state.sending > 0) return; // Vision esta ocupada: el clic no encola nada a escondidas
    sendMessage(`Abrí la página de Confluence «${node.title}» (id ${node.id}) y mostrame su contenido.`);
  });

  row.append(toggle, title);
  if (node.subtitle) {
    const sub = document.createElement("span");
    sub.className = "cf-chat-sub";
    sub.textContent = node.subtitle;
    row.appendChild(sub);
  }
  const href = _safeHttpUrl(node.open_url || "");
  if (href) {
    const open = document.createElement("a");
    open.className = "cf-chat-open";
    open.href = href;
    open.target = "_blank";
    open.rel = "noopener noreferrer";
    open.textContent = "Abrir en Confluence";
    row.appendChild(open);
  }
  li.appendChild(row);

  if (hasChildren) {
    const sub = document.createElement("ul");
    sub.className = "cf-chat-children";
    node.children.forEach((child) => sub.appendChild(_buildCfNode(child, depth + 1)));
    li.appendChild(sub);
    toggle.addEventListener("click", () => {
      const collapsed = sub.classList.toggle("hidden");
      toggle.textContent = collapsed ? "+" : "−";
    });
  }
  return li;
}

function renderConfluenceTree(data) {
  const wrap = document.createElement("div");
  wrap.className = "msg assistant cf-chat-block";
  const head = document.createElement("div");
  head.className = "cf-chat-head";
  head.textContent = data.kind === "search" ? `🔎 ${data.title || "Resultados"}` : `🌐 ${data.title || "Confluence"}`;
  const headHref = _safeHttpUrl(data.open_url || "");
  if (headHref) {
    const open = document.createElement("a");
    open.className = "cf-chat-open";
    open.href = headHref;
    open.target = "_blank";
    open.rel = "noopener noreferrer";
    open.textContent = "Abrir en Confluence";
    head.appendChild(open);
  }
  const list = document.createElement("ul");
  list.className = "cf-chat-list";
  (data.nodes || []).forEach((node) => list.appendChild(_buildCfNode(node, 0)));
  wrap.append(head, list);
  messagesEl.appendChild(wrap);
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

// REQ-018 (spec fix-detener-y-progreso-visible): `questionSessionId` = la sesion que pregunto. Solo se llama con esa sesion activa.
function renderQuestion(question, options, autoAnswered, questionSessionId = state.sessionId) {
  questionPanelEl.classList.remove("hidden");
  questionPanelEl.classList.toggle("auto-answered", !!autoAnswered);
  questionPanelEl.innerHTML = "";
  questionPanelEl.dataset.sessionId = questionSessionId;

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
        // REQ-018: la respuesta va SIEMPRE a la sesion que pregunto. Fuera de ella el panel esta oculto, pero conserva sus botones: si
        // alguno se disparara igual (teclado, un script), no se manda nada. Antes `sendMessage` la mandaba a la sesion ACTIVA.
        if (state.sessionId !== questionSessionId) return;
        resolvePendingQuestion(questionSessionId);
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

// REQ-018 (spec fix-detener-y-progreso-visible): evento `question` de un turno de la sesion `sessionId`. Una pregunta real queda
// pendiente de SU sesion: se dibuja si es la activa; si no, solo se marca en la lista y aparece al volver. Una que el loop autonomo
// contesto solo (`auto_answered`) se sigue mostrando como siempre, pero solo en su sesion, y no deja nada pendiente.
function handleQuestionEvent(data, sessionId) {
  _questionSessionsSeen.add(sessionId);
  if (data.auto_answered) {
    if (_pendingQuestions.delete(sessionId)) renderSessionsPanel(); // el turno ya siguio: una pregunta anterior quedo vieja
    if (sessionId === state.sessionId) renderQuestion(data.question, data.options, data.auto_answered, sessionId);
    return;
  }
  _pendingQuestions.set(sessionId, { question: data.question, options: data.options });
  if (sessionId === state.sessionId) renderQuestion(data.question, data.options, null, sessionId);
  else renderSessionsPanel(); // la marca «❓ te hizo una pregunta» en la lista
}

// REQ-018: la sesion ya contesto (una opcion u otro mensaje cualquiera) o el usuario descarto la pregunta: no queda nada pendiente.
function resolvePendingQuestion(sessionId) {
  _questionSessionsSeen.add(sessionId);
  if (_pendingQuestions.delete(sessionId)) renderSessionsPanel();
}

// REQ-018: al activar una sesion (o al abrir la pagina), su pregunta pendiente vuelve a aparecer.
function showPendingQuestionFor(sessionId) {
  const pending = _pendingQuestions.get(sessionId);
  if (pending && sessionId === state.sessionId) renderQuestion(pending.question, pending.options, null, sessionId);
}

// REQ-018: la pregunta sin contestar que dice SQLite (GET /api/sessions/{id}/messages). Solo cuenta si esta pestaña no sabe nada mas
// nuevo de esa sesion (tipico: despues de F5); lo que llego en vivo, se contesto o se descarto aca manda.
function rememberQuestionFromHistory(sessionId, pending) {
  if (_questionSessionsSeen.has(sessionId)) return;
  if (pending && typeof pending.question === "string" && Array.isArray(pending.options)) {
    _pendingQuestions.set(sessionId, { question: pending.question, options: pending.options });
  } else {
    _pendingQuestions.delete(sessionId); // lo unico que habia era un dato anterior de SQLite, y SQLite ya no la tiene pendiente
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
  // REQ-014 b (spec fix-detener-y-progreso-visible): descarta lo que este turno alcanzo a escribir, sin guardarlo ni leerlo en voz alta
  // (el usuario borro su mensaje). A diferencia de `resetAssistant` (edicion en curso) no toca la fila de herramientas global: el turno
  // borrado puede ser de una sesion en segundo plano.
  const discardAssistant = () => {
    if (_renderDebounceTimer) {
      clearTimeout(_renderDebounceTimer);
      _renderDebounceTimer = null;
    }
    if (assistantDiv) assistantDiv.remove();
    assistantDiv = null;
    assistantText = "";
    pendingResponseType = null;
    toolBadges.clear();
  };

  // Revision F6 (spec fix-detener-y-progreso-visible): el turno de este stream termino en 'stopped' (el usuario apreto Detener). El
  // handler de 'stopped' corta la voz; sin esta marca, la red de seguridad del final (abajo, al cerrarse el stream) cerraba la burbuja a
  // medias y la LEIA en voz alta, justo despues de que el usuario la freno.
  let stoppedByUser = false;

  // REQ-014 b: si el usuario borro el mensaje de este turno mientras terminaba, "done" llama antes a `discardAssistant` (ver
  // handleEvent) y aca ya no queda nada que cerrar.
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
      // Revision F6: la respuesta cortada por Detener queda en pantalla y guardada, pero no se lee.
      if (!stoppedByUser) {
        const spokenMsg = assistantDiv; // spec fix-boton-play-stop: su 🔊 pasa a ⏹ mientras se lee
        speak(toPlainText(assistantText), { onStart: () => showMessageSpeaking(spokenMsg) });
      }
    } else {
      // REQ-016: solo hace algo si esa sesion no tiene carpeta (con carpeta, su respuesta ya esta en SQLite).
      const originHistory = readLocalCopy(turnSessionId);
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
    stoppedByUser = false; // revision F6: un turno siguiente de esta misma conexion (el loop) se lee como siempre
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
        // REQ-002 (spec aviso-proceso-terminado): la respuesta del turno tal como llego (el aviso de Windows muestra su comienzo).
        assistantText: () => assistantText,
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
            // REQ-018 (spec fix-detener-y-progreso-visible): sin el lugar de su turno en pantalla (`responseSlot` null: reenganche de un
            // turno cuyo mensaje no esta pintado, o un turno del loop), la burbuja caia al final del chat ACTIVO. Si el turno es de otra
            // sesion se arma fuera de la pantalla: el texto se sigue juntando para guardarlo en SU copia, pero nunca se ve en esta.
            const mountEl = responseSlot || (turnSessionId === state.sessionId ? null : document.createElement("div"));
            assistantDiv = appendMessage("assistant", "", { persist: false, mountEl, time: assistantTime });
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
        discardAssistant, // REQ-014 b
        markStopped: () => {
          stoppedByUser = true; // revision F6: ver closeCurrentAssistantTurn
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
// (Desde REQ-002 de fix-detener-y-progreso-visible el backend emite «Pensando
// el próximo paso… (paso N)» en cada paso; los puntitos quedan como red de
// seguridad hasta que llega ese evento.)
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

// PQ-003 / BL-012 (opcion (a), ADR-018): si el MODELO no empieza a responder en 30 s, se avisa en pantalla y se ofrece esperar o cambiar
// de modelo. Solo informa: no salta de modelo ni corta el pedido. Medido en perf.log: 7 turnos esperaron mas de 2 min (peor, 733 s) sin
// ninguna senal de que la demora era del modelo. Se arma en el click de Enviar y de nuevo tras cada resultado de tool (el tramo siguiente
// tambien es del modelo), y se desarma apenas el modelo produce algo (texto, una tool-call, o -- REQ-005 de fix-detener-y-progreso-visible
// -- el primer aviso `tool-writing` de que esta escribiendo los argumentos de una herramienta) o el turno termina.
const SLOW_MODEL_NOTICE_MS = 30_000;
let _slowModelTimer = null;
let _slowModelNoticeEl = null;

function armSlowModelNotice() {
  cancelSlowModelNotice();
  _slowModelTimer = setTimeout(() => {
    _slowModelTimer = null;
    if (state.sending <= 0 || _slowModelNoticeEl) return;
    const wrap = document.createElement("div");
    wrap.className = "msg assistant";
    const note = document.createElement("div");
    note.className = "msg-slow-model-notice";
    note.setAttribute("role", "status");
    // REQ-005 (spec fix-detener-y-progreso-visible): no dice «sin responder» -- en el caso real el modelo SI estaba trabajando (escribia
    // un archivo de 15 KB como argumento de una herramienta). Solo dice lo que se sabe: que tarda mas de lo normal.
    note.textContent = "⏳ El modelo está tardando más de lo normal (lleva más de 30 segundos). Podés esperar, o cambiar de modelo desde el selector de modelos y volver a enviar. No cambio nada por mi cuenta.";
    wrap.appendChild(note);
    messagesEl.appendChild(wrap);
    _slowModelNoticeEl = wrap;
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }, SLOW_MODEL_NOTICE_MS);
}

function cancelSlowModelNotice() {
  clearTimeout(_slowModelTimer);
  _slowModelTimer = null;
  if (!_slowModelNoticeEl) return;
  _slowModelNoticeEl.remove();
  _slowModelNoticeEl = null;
}

function cancelThinkingTip() {
  clearTimeout(_thinkingTipTimer);
  _thinkingTipTimer = null;
  if (!_thinkingTipEl) return;
  _thinkingTipEl.remove();
  _thinkingTipEl = null;
}

async function sendMessage(text, { messageId = null, images = null, voiceConfirm = false, attachments = null, sentFiles = null } = {}) {
  // spec contexto-multimodal REQ-004: un mensaje con SOLO imagenes (sin
  // texto) es valido -- el chequeo original solo miraba el texto.
  // spec adjuntar-archivos-chat REQ-003: tambien es valido con SOLO archivos.
  if (!text.trim() && (!images || images.length === 0) && (!attachments || attachments.length === 0)) return;
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
  // REQ-018 (spec fix-detener-y-progreso-visible): cualquier mensaje de esta sesion (una opcion o texto libre) contesta su pregunta
  // pendiente: la marca de la lista se va y no vuelve a aparecer al regresar.
  resolvePendingQuestion(state.sessionId);
  if (!wasAlreadyBusy) setConnStatus("busy", "ULTRON está trabajando…");

  const msgId = messageId || crypto.randomUUID();
  appendMessage("user", text, { messageId: msgId, images, attachments: sentFiles });
  if (!wasAlreadyBusy) resetToolRow();
  // REQ-014 f: el boton pasa a «Detener» ya mismo (abajo), antes del primer evento: si se aprieta ahora, es para ESTE mensaje.
  if (!wasAlreadyBusy) noteWatchedTurn(state.sessionId, msgId);

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
    armSlowModelNotice();
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
        // REQ-003 (spec adjuntar-archivos-chat): solo la REFERENCIA; el aviso de
        // rutas para el agente lo arma el backend.
        attachments: attachments && attachments.length > 0 ? attachments : undefined,
        // REQ-006/009 (spec asistente-contextual-voz): marca este turno
        // como originado en el modo Cerebro -- activa la confirmacion
        // hablada del lado del backend antes de ejecutar tools peligrosas
        // (bash/write/edit). false (default) = chat de texto normal, sin
        // cambio de comportamiento.
        voice_confirm: voiceConfirm || undefined,
        // spec informe-tecnico-hablado: avisa al backend que se narre (solo si hay voz Y el interruptor).
        narrate: (isTtsEnabled() && isTechReportEnabled()) || undefined,
        // REQ-012: nivel del informe ('summary' | 'detailed'); el backend trata lo desconocido como 'summary'.
        narrate_level: isTtsEnabled() && isTechReportEnabled() ? getTechReportLevel() : undefined,
      }),
    });
    await renderIncomingStream(resp, msgId, turnSessionId);
  } catch (err) {
    hideOptimisticThinking();
    cancelThinkingTip();
    cancelSlowModelNotice();
    // Solo un corte de red real es "sin conexión": un bug de la interfaz
    // (ej. el ReferenceError de 2026-09-20) no debe disfrazarse de eso.
    const offline = isOfflineError(err);
    appendMessage("error", friendlyErrorMessage(err), {
      technical: technicalErrorLine(err),
      errorKind: offline ? "offline" : "other",
    });
    if (offline) setConnStatus("error", "Sin conexión con ULTRON");
  } finally {
    state.sending -= 1;
    if (state.sending <= 0) {
      state.sending = 0;
      hideOptimisticThinking(); // red de seguridad: nunca debe sobrevivir al fin del turno
      cancelThinkingTip(); // idem -- el tip tampoco debe sobrevivir al turno
      cancelSlowModelNotice(); // ni el aviso de modelo lento
      if (connStatusEl.dataset.state === "busy") setConnStatus("online", "ULTRON en línea");
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

// REQ-002 (navegador-integrado-ventana): claves de eventos browser-page-* ya
// atendidos, para que recibirlos dos veces no reabra el panel.
const _browserPageEventsSeen = new Set();

// REQ-014 f (spec fix-detener-y-progreso-visible): el turno que el usuario esta VIENDO responder en la sesion activa. Cada evento de un
// turno trae su `turn_id` (backend: `_drain_session_queue`); Detener lo manda y el backend frena solo ESE turno. Antes, un Detener en los
// ultimos milisegundos de un turno frenaba al SIGUIENTE mensaje de la cola, que acababa de arrancar. Sin id conocido, Detener va sin id
// (como siempre: frena lo que este corriendo).
let _watchedTurn = { sessionId: null, turnId: null };

function noteWatchedTurn(sessionId, turnId) {
  if (sessionId && turnId) _watchedTurn = { sessionId, turnId };
}

function watchedTurnIdFor(sessionId) {
  return _watchedTurn.sessionId === sessionId ? _watchedTurn.turnId : null;
}

// REQ-014 b: mensajes que el usuario BORRO en esta pagina. Si su turno ya habia arrancado, el backend lo frena y lo descarta (memoria y
// SQLite); lo que todavia llegue de ese turno (texto a medias, o un 'done' que se cruzo con el borrado) no se guarda en la copia local
// ni se lee en voz alta: el mensaje ya no existe.
const _deletedTurnIds = new Set();

// =========================================================================
// Spec aviso-proceso-terminado (REQ-001..007, decisiones D1-D4 del usuario 2026-10-13). Pedido: «una notificacion abajo a la derecha
// cuando terminó un proceso solicitado y tenga todas las tareas en completed». D1 = SOLO la notificacion de Windows: la API
// `Notification` del navegador, que Windows muestra abajo a la derecha este o no mirando Vision (minimizado, en otra pestaña o en otra
// aplicacion). D2 = el disparador es el mismo momento del `done` (la barra llega al 100 % y Vision empieza a hablar). D3 = suena el
// sonido de notificacion de Windows: Vision no pasa `silent` ni suena nada propio (REQ-004; se cambia en Windows).
// =========================================================================
// REQ-006: interruptor «Avisarme cuando termine» (Configuración > Audio), prendido de fabrica. Por readPreference/savePreference (REQ-016
// de fix-detener-y-progreso-visible): sin almacenamiento, el cambio vale igual hasta recargar.
const NOTIFY_ON_FINISH_KEY = "vision.notifyOnFinish";
// REQ-003: marca por turno ya avisado (`vision.notified.<turnId>` = hora del aviso), compartida por todas las pestañas del navegador. Las
// marcas viejas se borran solas: un turno ya terminado no vuelve a llegar despues de un dia.
const NOTIFIED_MARK_PREFIX = "vision.notified.";
const NOTIFIED_MARK_MAX_AGE_MS = 24 * 60 * 60 * 1000;
// REQ-003: dos pestañas mirando la misma sesion reciben el MISMO `done` casi a la vez, y la marca de localStorage puede tardar en verse
// de una pestaña a la otra. El candado del navegador (Web Locks, atomico entre pestañas) decide cual avisa; se retiene unos segundos
// para que la otra lo encuentre tomado, y para entonces ya ve la marca.
const NOTIFY_LOCK_HOLD_MS = 4000;
// REQ-007: el permiso negado se explica UNA vez (tambien entre recargas); si despues se concede, vuelve a valer para una negativa futura.
const NOTIFY_DENIED_NOTICE_KEY = "vision.notify.deniedNoticeShown";
// REQ-002: «recortado a unas 3 lineas» de la notificacion de Windows.
const NOTIFY_SUMMARY_MAX_CHARS = 180;
const NOTIFY_ICON_URL = new URL("./assets/favicon-180.png", window.location.href).href;
const NOTIFY_HOW_TO_ALLOW =
  "Para habilitarlas en Edge: tocá el candado de la barra de direcciones > Permisos del sitio > Notificaciones > Permitir, y recargá la página.";
const NOTIFY_DENIED_TEXT =
  `ULTRON no puede avisarte en Windows cuando termina: el navegador tiene bloqueadas sus notificaciones. ${NOTIFY_HOW_TO_ALLOW} ` +
  "Si no querés el aviso, apagá «Avisarme cuando termine» en Configuración > Audio.";
const NOTIFY_FAILED_TEXT =
  "Windows no mostró el aviso de que ULTRON terminó: el navegador no lo permitió. Podés revisarlo en Configuración > Audio > «Avisarme cuando termine».";
// REQ-007: lo que dice Configuración debajo del interruptor segun el permiso del navegador.
const NOTIFY_PERMISSION_STATUS = {
  granted: "El navegador ya dio permiso: Windows muestra el aviso abajo a la derecha.",
  default: "La primera vez el navegador te va a pedir permiso para mostrar notificaciones: elegí «Permitir».",
  denied: `El navegador tiene bloqueadas las notificaciones de ULTRON. ${NOTIFY_HOW_TO_ALLOW}`,
  unsupported: "Este navegador no puede mostrar avisos de Windows. ULTRON funciona igual, sin el aviso.",
};

// REQ-001: sessionId -> { turnId, todos }: la ULTIMA lista de tareas que llego de un turno de esa sesion. Se recuerda aunque la sesion no
// sea la activa (el panel de tareas pinta solo la activa, REQ-018 de fix-detener-y-progreso-visible): un turno de una sesion de fondo o
// del loop tambien avisa. Solo cuenta la lista de ESE turno: un «gracias» despues de un plan terminado no repite el aviso.
const _turnTodosBySession = new Map();
const _notifiedTurns = new Set(); // REQ-003: turnos ya avisados (o descartados) en esta pestaña
let _notifyPermissionAskedOnSend = false; // REQ-007: el primer clic de Enviar pide el permiso, no cada envio
let _notifyFailureNoticeShown = false;

function isNotifyOnFinishEnabled() {
  const raw = readPreference(NOTIFY_ON_FINISH_KEY);
  return raw === null ? true : raw === "true"; // REQ-006: prendido de fabrica
}

function setNotifyOnFinishEnabled(enabled) {
  savePreference(NOTIFY_ON_FINISH_KEY, String(Boolean(enabled)));
}

// REQ-007: "granted" | "default" | "denied" | "unsupported" (navegador sin la API: nada se rompe, solo no hay aviso).
function notifyPermission() {
  try {
    return typeof window.Notification === "function" ? String(window.Notification.permission || "default") : "unsupported";
  } catch {
    return "unsupported";
  }
}

function rememberTurnTodos(sessionId, turnId, todos) {
  if (!sessionId) return;
  _turnTodosBySession.set(sessionId, { turnId: turnId || null, todos: Array.isArray(todos) ? todos : [] });
}

// REQ-001: Detener, un error o un mensaje borrado cierran el turno sin aviso: su lista ya no puede disparar nada.
function forgetTurnTodos(sessionId) {
  _turnTodosBySession.delete(sessionId);
}

// REQ-001: N (>= 1) si TODAS las tareas (y sus sub-tareas) estan `completed`; 0 si no. Una cancelada o pendiente no es «todas completed».
function allTasksCompletedCount(todos) {
  if (!Array.isArray(todos) || todos.length === 0) return 0;
  const completed = (t) => Boolean(t) && t.status === "completed";
  const all = todos.every((t) => completed(t) && (Array.isArray(t.subtasks) ? t.subtasks : []).every(completed));
  return all ? todos.length : 0;
}

// REQ-002: arriba el nombre de la sesion, como figura en la lista (el de la primera pregunta si todavia no tiene uno guardado).
function sessionNameForNotice(sessionId) {
  const saved = loadSessionsIndex()[sessionId]?.name;
  const firstUser = sessionId === state.sessionId ? chatHistory.find((m) => m.role === "user" && m.text) : null;
  const name = String(saved || (firstUser ? firstUser.text.slice(0, 40) : "") || "").replace(/\s+/g, " ").trim();
  return name || "ULTRON";
}

function clockForNotice(date) {
  return `${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`;
}

// REQ-002/005: el comienzo de la respuesta en texto PLANO (sin markdown ni etiquetas HTML: la notificacion nunca interpreta HTML, pero
// `**`, `#` o `<b>` sueltos se leerian tal cual), en una sola linea y cortado en una palabra con «…».
function plainSummaryForNotice(text) {
  const plain = toPlainText(String(text || ""))
    .replace(/<!--[\s\S]*?-->/g, " ")
    .replace(/<\/?[a-zA-Z][^<>]*>/g, " ")
    .replace(/\s+/g, " ")
    .replace(/\s+([.,;:!?])/g, "$1")
    .trim();
  if (plain.length <= NOTIFY_SUMMARY_MAX_CHARS) return plain;
  let cut = plain.slice(0, NOTIFY_SUMMARY_MAX_CHARS);
  const lastSpace = cut.lastIndexOf(" ");
  if (lastSpace > NOTIFY_SUMMARY_MAX_CHARS * 0.6) cut = cut.slice(0, lastSpace);
  return `${cut.replace(/[\s.,;:!?¡¿(—–-]+$/u, "")}…`;
}

function finishedNoticeBody(answerText, total) {
  const tasks = `${total} de ${total} ${total === 1 ? "tarea" : "tareas"}`;
  const summary = plainSummaryForNotice(answerText);
  return summary ? `${summary}\n${tasks}` : tasks;
}

function pruneNotifiedMarks() {
  try {
    const now = Date.now();
    const keys = [];
    for (let i = 0; i < localStorage.length; i += 1) {
      const key = localStorage.key(i);
      if (key && key.startsWith(NOTIFIED_MARK_PREFIX)) keys.push(key);
    }
    for (const key of keys) {
      const at = Number(localStorage.getItem(key));
      if (!Number.isFinite(at) || now - at > NOTIFIED_MARK_MAX_AGE_MS) localStorage.removeItem(key);
    }
  } catch {
    // noop -- sin localStorage no hay marcas que limpiar
  }
}

// REQ-003: `show` corre una sola vez por turno entre TODAS las pestañas: el candado decide quien avisa en el mismo instante y la marca de
// localStorage cubre a una pestaña que llegue despues (o a esta misma tras F5). Sin candado ni almacenamiento, cada pestaña decide sola y
// el `tag` (el id del turno) igual hace que Windows reemplace el aviso repetido en vez de sumar otro.
function claimTurnNotice(turnKey, show) {
  if (_notifiedTurns.has(turnKey)) return;
  _notifiedTurns.add(turnKey);
  const markKey = NOTIFIED_MARK_PREFIX + turnKey;
  const markIfFirst = () => {
    try {
      if (localStorage.getItem(markKey) !== null) return false; // otra pestaña (o esta antes de recargar) ya aviso este turno
      pruneNotifiedMarks();
      localStorage.setItem(markKey, String(Date.now()));
    } catch {
      // noop -- sin localStorage cada pestaña decide sola (ver arriba)
    }
    return true;
  };
  const locks = navigator.locks;
  if (!locks || typeof locks.request !== "function") {
    if (markIfFirst()) show();
    return;
  }
  locks
    .request(`vision.notify.${turnKey}`, { ifAvailable: true }, async (lock) => {
      if (!lock || !markIfFirst()) return; // otra pestaña lo esta avisando en este mismo momento, o ya lo aviso
      show();
      await new Promise((resolve) => setTimeout(resolve, NOTIFY_LOCK_HOLD_MS));
    })
    .catch(() => {
      if (markIfFirst()) show(); // el candado fallo (no deberia): la marca sola alcanza
    });
}

function showNotifyNotice(text) {
  const noticeEl = document.getElementById("notify-denied-notice");
  if (!noticeEl) return;
  document.getElementById("notify-denied-notice-text").textContent = text;
  noticeEl.classList.remove("hidden");
}

// REQ-007: el navegador nego el permiso: se dice UNA vez, en lenguaje claro y con como habilitarlo.
function warnNotifyDeniedOnce() {
  if (readPreference(NOTIFY_DENIED_NOTICE_KEY) === "true") return;
  savePreference(NOTIFY_DENIED_NOTICE_KEY, "true");
  showNotifyNotice(NOTIFY_DENIED_TEXT);
}

// REQ-002: la notificacion de Windows. Queda hasta que se la toque o se la cierre (`requireInteraction`); tocarla trae a Vision al
// frente, abre ESA sesion y la cierra. El `tag` es el id del turno (REQ-003). Sin `silent`: suena el sonido de Windows (REQ-004).
function showFinishedNotice(sessionId, tag, title, body) {
  let notice;
  try {
    notice = new window.Notification(title, { body, tag, requireInteraction: true, icon: NOTIFY_ICON_URL });
  } catch (err) {
    console.warn("Windows no mostró el aviso de que ULTRON terminó:", err);
    if (!_notifyFailureNoticeShown) {
      _notifyFailureNoticeShown = true;
      showNotifyNotice(NOTIFY_FAILED_TEXT);
    }
    return;
  }
  notice.onclick = () => {
    try {
      window.focus();
    } catch {
      // noop -- si el navegador no deja traer la ventana al frente, igual se abre la sesion
    }
    switchToSession(sessionId);
    notice.close();
  };
}

// REQ-001: se llama en el `done` de cada turno (de la sesion activa, de una de fondo o del loop), con el texto de su respuesta.
function notifyIfAllTasksCompleted(sessionId, turnId, answerText) {
  const record = _turnTodosBySession.get(sessionId);
  _turnTodosBySession.delete(sessionId);
  if (!record || record.turnId !== (turnId || null)) return; // la ultima lista no es de ESTE turno (o el turno no tuvo lista)
  const total = allTasksCompletedCount(record.todos);
  if (total === 0 || !isNotifyOnFinishEnabled()) return;
  const permission = notifyPermission();
  if (permission === "denied") {
    warnNotifyDeniedOnce();
    return;
  }
  if (permission !== "granted") return; // sin decidir: se pide desde un clic del usuario (REQ-007), nunca aca; sin la API, nada
  forgetPreference(NOTIFY_DENIED_NOTICE_KEY);
  const finishedAt = new Date();
  const title = `${sessionNameForNotice(sessionId)} · ${clockForNotice(finishedAt)}`;
  const body = finishedNoticeBody(answerText, total);
  const turnKey = turnId || `${sessionId}:${finishedAt.getTime()}`;
  claimTurnNotice(turnKey, () => showFinishedNotice(sessionId, turnKey, title, body));
}

// REQ-007: el estado del permiso, debajo del interruptor de Configuración.
function refreshNotifyPermissionStatus() {
  const statusEl = document.getElementById("cfg-notify-finish-permission");
  const permission = notifyPermission();
  if (permission === "granted") forgetPreference(NOTIFY_DENIED_NOTICE_KEY);
  if (statusEl) statusEl.textContent = NOTIFY_PERMISSION_STATUS[permission] || "";
}

// REQ-007: el permiso se pide SOLO desde un clic del usuario (el navegador lo exige y asi no aparece un cartel de la nada).
function requestNotifyPermission() {
  if (notifyPermission() !== "default") return;
  let answer;
  try {
    answer = window.Notification.requestPermission();
  } catch {
    return; // el navegador no deja pedirlo: queda sin aviso, sin romper nada
  }
  if (!answer || typeof answer.then !== "function") return;
  answer.then(
    (result) => {
      refreshNotifyPermissionStatus();
      if (result === "denied") warnNotifyDeniedOnce();
    },
    () => refreshNotifyPermissionStatus()
  );
}

// REQ-007: el primer clic de Enviar (con el interruptor prendido y el permiso sin decidir) pide el permiso; los siguientes ya no.
function askNotifyPermissionOnFirstSend() {
  if (_notifyPermissionAskedOnSend || !isNotifyOnFinishEnabled() || notifyPermission() !== "default") return;
  _notifyPermissionAskedOnSend = true;
  requestNotifyPermission();
}

pruneNotifiedMarks();
document.getElementById("notify-denied-notice-close").addEventListener("click", () => {
  document.getElementById("notify-denied-notice").classList.add("hidden");
});

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
  // REQ-014 f: el turno de este evento pasa a ser el que el usuario esta viendo (Detener lo nombra).
  if (event.turn_id && ctx.turnSessionId === state.sessionId) noteWatchedTurn(ctx.turnSessionId, event.turn_id);
  // REQ-014 d: 'dequeued' (un mensaje salio de la cola) tampoco es actividad del modelo, como 'queued'.
  if (!["queued", "dequeued", "stopped", "done", "error"].includes(event.type)) {
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
      // REQ-018 (spec fix-detener-y-progreso-visible): el texto se sigue juntando aunque el turno sea de otra sesion (se guarda en SU
      // copia al terminar, ver closeCurrentAssistantTurn), pero se escribe en la burbuja de SU turno, nunca en la pantalla de la
      // sesion activa (ver appendAssistantText). El aviso de modelo lento es de la sesion activa: solo lo cancela un turno suyo.
      if (ctx.turnSessionId === state.sessionId) cancelSlowModelNotice(); // el modelo ya empezo a responder
      ctx.appendAssistantText(event.data.text);
      break;

    case "response-type":
      if (ctx.setResponseType) ctx.setResponseType(event.data.type || event.data.response_type);
      break;

    case "tool-call": {
      // REQ-018: la insignia de la herramienta es solo visual; sin el turno en pantalla (`responseSlot` null) caia al final del chat
      // de la sesion ACTIVA. De otra sesion no se dibuja (su resultado se ve al volver, desde SQLite).
      if (ctx.turnSessionId !== state.sessionId) break;
      cancelSlowModelNotice(); // el modelo respondio (con una herramienta)
      const badge = appendToolBadge(event.data.name, event.data.arguments, ctx.responseSlot);
      ctx.toolBadges.set(event.data.id, badge);
      break;
    }

    // REQ-003/005 (spec fix-detener-y-progreso-visible): el modelo esta ESCRIBIENDO los argumentos de una herramienta (el `tool-call`
    // recien llega cuando termina). Caso real: un `write` de 15 KB tardo ~69 s y la pantalla decia «sin responder». Solo para la
    // sesion activa, como el resto de lo que pinta la tarjeta de progreso.
    case "tool-writing":
      if (ctx.turnSessionId !== state.sessionId) break;
      cancelSlowModelNotice(); // el modelo SI esta respondiendo: escribe una herramienta
      showWritingProgress(event.data, ctx.responseSlot);
      break;

    // REQ-004 (spec fix-detener-y-progreso-visible): ultimas lineas de salida del bash en curso (evento fuera de banda de la tool).
    case "tool-output":
      if (ctx.turnSessionId !== state.sessionId) break;
      showLiveOutput(event.data);
      break;

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
      // REQ-018: el registro de herramientas (y su contador) del panel lateral es de la sesion activa.
      if (ctx.turnSessionId === state.sessionId) logTool(event.data.name, event.data.result);
      // Investigacion de rendimiento 2026-09: apenas termina una tool, se
      // re-muestra el indicador optimista para no dejar en silencio el
      // tramo siguiente (puede ser el tramo mas largo del turno, ver
      // comentario de showOptimisticThinking). Desde fix-detener-y-progreso-
      // visible el backend ademas avisa «Pensando el próximo paso… (paso N)»
      // en cada paso (llm/loop.py), que reemplaza a los puntitos apenas llega.
      if (ctx.turnSessionId === state.sessionId) {
        // REQ-002 (spec fix-detener-y-progreso-visible): la herramienta termino, asi que su operacion se cierra (antes la tarjeta
        // seguia diciendo «Ejecutando comando…» de un comando que ya habia terminado mientras el modelo pensaba el paso siguiente).
        finishProgress();
        showOptimisticThinking();
        armSlowModelNotice(); // lo que sigue es otra espera al modelo
      }
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
      // REQ-001 (spec aviso-proceso-terminado): la lista se recuerda por sesion y por turno ANTES de ese filtro, sin pintarla: el `done`
      // de una sesion de fondo (o del loop) tambien avisa si todas sus tareas quedaron completed.
      rememberTurnTodos(ctx.turnSessionId, event.turn_id, event.data.todos);
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
      if (isTechReportEnabled()) speakTechReport(event.data.text, { lead: Boolean(event.data.lead) });
      ensureTechKeepAlive();
      break;

    // REQ-001/003/004/005 (spec dashboard-graficos): el backend ya arma
    // labels/values completos (sin el truncado a 4000 chars del SSE,
    // resuelto server-side via SQLite) -- el frontend solo dibuja.
    case "chart-ready":
      // REQ-018 (spec fix-detener-y-progreso-visible): el grafico de una sesion no se abre encima de otra (antes abria el modal en la
      // sesion que el usuario estuviera mirando).
      if (ctx.turnSessionId !== state.sessionId) break;
      renderChart(event.data);
      break;

    // spec conexiones-con-nombre Fase B: arbol / resultados / pagina de Confluence como bloque seleccionable en el chat.
    case "confluence-tree":
      if (ctx.turnSessionId !== state.sessionId) break;
      renderConfluenceTree(event.data);
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
      // REQ-018 (spec fix-detener-y-progreso-visible), reporte del usuario 2026-10-13: «salte a otra sesion ... y me hizo una pregunta
      // referida a la sesion de la cual venia». Esto dibujaba el panel global sin mirar la sesion del evento, y la opcion elegida se
      // mandaba a la sesion ACTIVA (la respuesta de A terminaba en B). Ahora la pregunta queda pendiente de SU sesion.
      handleQuestionEvent(event.data, ctx.turnSessionId);
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
      // REQ-008 b: con `pct` es avance REAL (parseo de un PDF), no un presupuesto de tiempo.
      if (typeof event.data.pct === "number") {
        setRealProgress({ label: event.data.label, pct: event.data.pct }, ctx.responseSlot);
        break;
      }
      startProgress(
        {
          label: event.data.label,
          budgetMs: event.data.budget_ms,
          hasHistory: event.data.has_history,
          // REQ-004 (fix-detener-y-progreso-visible): la salida en vivo se pega solo a la operacion de ese mismo id.
          opId: event.data.id ?? null,
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
      // spec informe-tecnico-hablado REQ-001/003/004: con "Hablar informe tecnico" prendido, en
      // vez de la etiqueta corta se dice la frase que arma el backend con lo que Vision
      // ejecuta (`speech`), solo para las acciones (`speech` null = leer/buscar/pensar:
      // silencio). Las frases viejas que no llegaron a sonar se reemplazan por la mas nueva.
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
      forgetTurnTodos(ctx.turnSessionId); // REQ-001 (spec aviso-proceso-terminado): un turno que termina en error no avisa
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

case "done": {
      stopTechKeepAlive(ctx.turnSessionId); // spec informe-tecnico-hablado REQ-011
      // Bug real (2026-09-20, modo Loop): `closeCurrentAssistantTurn()`
      // debe correr SIEMPRE aca, sin importar la sesion activa -- ya tiene
      // su propio manejo interno (`stillOnOriginSession`) para decidir
      // DONDE persistir. Si esto quedara detras del guard de sesion de
      // abajo, un loop corriendo en background (sesion no activa)
      // acumularia TODOS sus turnos sinteticos en un solo assistantText
      // sin resetear entre ellos -- el mismo bug de desorden de turnos,
      // pero para sesiones que ni siquiera se estan mirando.
      // REQ-014 b (spec fix-detener-y-progreso-visible): si el usuario borro este mensaje mientras terminaba, lo que alcanzo a
      // escribir se descarta (no se guarda ni se lee en voz alta) y el cierre de abajo ya no tiene nada que cerrar.
      const deletedTurn = Boolean(event.turn_id && _deletedTurnIds.has(event.turn_id));
      if (deletedTurn) ctx.discardAssistant();
      // REQ-001/002 (spec aviso-proceso-terminado): el texto de la respuesta se toma ANTES de cerrar el turno (el cierre lo vacia para el
      // turno siguiente de la misma conexion, p. ej. el loop).
      const finishedAnswer = ctx.assistantText ? ctx.assistantText() : "";
      ctx.closeCurrentAssistantTurn();
      // REQ-001 (spec aviso-proceso-terminado): en el mismo momento del fin (D2), sea de la sesion activa, de una de fondo o del loop:
      // si la lista de tareas de ESTE turno quedo toda completed, aviso de Windows. Un mensaje que el usuario borro no avisa.
      if (deletedTurn) forgetTurnTodos(ctx.turnSessionId);
      else notifyIfAllTasksCompleted(ctx.turnSessionId, event.turn_id, finishedAnswer);
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
      // REQ-017 (spec fix-detener-y-progreso-visible): el resumen «Contexto» se pone al dia al terminar CADA turno (tambien los
      // de la cola y los reenganchados), con el acordeon abierto o cerrado.
      refreshContextUsage();
      break;
    }

    // --- REQ-001/003/004: eventos nuevos de la spec parada-y-cola-mensajes ---
    case "queued":
      renderQueueBadge(event.data.message_id, event.data.position);
      break;

    // REQ-014 d (spec fix-detener-y-progreso-visible): un mensaje salio de la cola -- arranco ('started') o el usuario lo borro
    // ('deleted'). Su cartel «En cola» se va y los que siguen esperando se renumeran con la cola que manda el backend. Antes este
    // evento nunca se emitia y el cartel quedaba pegado aunque el mensaje ya se estuviera respondiendo. Llega a todas las conexiones
    // de la sesion: es idempotente (y de otra sesion solo limpia la lista local: su cartel no esta en pantalla).
    case "dequeued": {
      removeQueueBadge(event.data.message_id);
      if (Array.isArray(event.data.queue)) {
        event.data.queue.forEach((id, position) => {
          if (queuedTurns.has(id)) renderQueueBadge(id, position);
        });
      }
      maybeShowQueueWarning();
      if (event.data.reason === "started" && ctx.turnSessionId === state.sessionId) {
        noteWatchedTurn(ctx.turnSessionId, event.data.message_id); // REQ-014 f: ese es el que ahora responde
      }
      break;
    }

    case "stopped": {
      stopTechKeepAlive(ctx.turnSessionId); // spec informe-tecnico-hablado REQ-011
      forgetTurnTodos(ctx.turnSessionId); // REQ-001 (spec aviso-proceso-terminado): Detener no avisa aunque las tareas esten completed
      // REQ-014 b: el turno se freno porque el usuario BORRO su mensaje (ya habia arrancado): lo que alcanzo a escribir se descarta
      // (el backend tambien lo quita de memoria y de SQLite) y no queda un «Turno detenido» suelto en el chat de un mensaje que ya no esta.
      const deletedByUser = Boolean(event.data && event.data.reason === "message_deleted");
      if (deletedByUser) ctx.discardAssistant();
      // Revision F6 (spec fix-detener-y-progreso-visible): lo que alcanzo a escribir se cierra al terminar el stream, SIN leerlo en voz alta.
      if (ctx.markStopped) ctx.markStopped();
      // Mismo bug/fix que "progress-estimate"/"error" arriba: si el turno
      // detenido es de otra sesion (no la activa), no corresponde tocar
      // NADA de la UI que el usuario esta mirando -- ni el mensaje, ni el
      // boton Enviar/Detener, ni los indicadores optimistas, ni la barra.
      if (ctx.turnSessionId !== state.sessionId) break;
      if (!deletedByUser) appendMessage("system-note", "⏹ Turno detenido por el usuario.", { persist: false });
      setSendButtonState("idle");
      // BUG REAL detectado por el usuario (2026-09-16, screenshot): faltaban
      // estas dos lineas -- el indicador optimista (puntitos) y el tip de
      // 15s (ver showOptimisticThinking/scheduleThinkingTip) podian quedar
      // pegados en pantalla despues de "Turno detenido por el usuario.",
      // como si el turno siguiera en curso.
      hideOptimisticThinking();
      cancelThinkingTip();
      cancelSlowModelNotice();
      // El turno se corto: la barra se oculta SIN completarse a 100%
      // (completarla sugeriria que la operacion termino bien, que es
      // justo lo contrario de lo que paso).
      resetProgress();
      // REQ-007/008: si Jarvis estaba narrando algo por voz (respuesta o
      // progreso), se corta al instante -- seguir hablando despues de que
      // el usuario ya presiono "Detener" seria confuso/contradictorio.
      cancelSpeech();
      break;
    }

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
      // REQ-018: el banner (y su alarma, que se apaga con el banner) es del loop de ESA sesion: en otra decia «respondé la pregunta de
      // abajo» sin ninguna pregunta, y su «Detener loop» frenaba la sesion activa. Al volver, activateSession lo muestra segun el estado
      // real (loop_paused_for_question) y la lista ya marca la sesion que pregunto.
      if (ctx.turnSessionId !== state.sessionId) break;
      showLoopBanner();
      break;

    // spec modo-plan-loop REQ-007: el loop se detuvo (exito, STOP, o
    // limite de 30 turnos) -- se apaga cualquier alarma que estuviera
    // sonando (caso max_turns tambien pausa+alarma, pero via el evento
    // 'loop-paused' de arriba, no este) y se avisa el motivo si no fue
    // un simple exito silencioso.
case "loop-stopped":
      // Pedido del usuario (2026-09-13): si el loop termino TODAS las tareas
      // (reason "completed"), suena la señal de EXITO para saberlo estando
      // lejos de la PC. REQ-018: es solo un sonido (no pinta nada ni deja estado), asi que suena aunque el loop sea de otra sesion.
      if (event.data.reason === "completed") playSuccessAlarm();
      // REQ-018 (spec fix-detener-y-progreso-visible): el banner, el interruptor «Loop» y el aviso son de la sesion activa; el loop de
      // otra sesion no los toca (al volver, activateSession los pone segun el estado real de esa sesion).
      if (ctx.turnSessionId !== state.sessionId) break;
      hideLoopBanner();
      _loopRunning = false; // sincroniza el toggle "Loop" del panel de accesos rapidos
      if (quickPanelLooprunToggleEl) syncQuickPanelLoopState();
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
      // REQ-018: el modal edita la sesion ACTIVA: con el pedido de otra sesion, el usuario le asignaba la carpeta a la que no era. En
      // la sesion que lo pidio, el banner «Sin carpeta de proyecto» sigue ofreciendo asignarla.
      if (ctx.turnSessionId !== state.sessionId) break;
      openSessionEditModal();
      break;

    // spec gestion-contexto-conversacion: solo la compactacion completa
    // genera un aviso; el pruning incremental permanece silencioso.
    case "system-note":
      // REQ-018: el aviso (compactacion, «lleva N pasos», promesa vacia) es del turno de su sesion; en otra aparecia como si fuera de esa.
      if (ctx.turnSessionId !== state.sessionId) break;
      appendMessage("system-note", event.data.text, { persist: false });
      break;

    // REQ-001/002 (spec navegador-integrado-ventana): el backend ya decidio
    // -- a nivel de TURNO completo, ver `_browser_index_snapshot` en
    // llm/loop.py -- que este turno creo la pagina de entrada de la carpeta
    // de vista previa (no existia antes, existe ahora), sin importar que
    // tool la escribio. Bug real corregido (QA-034, "el panel se abre solo
    // despues de generar un dashboard, cuando luego se hace una pregunta"):
    // antes se re-chequeaba la huella del FILESYSTEM en cada 'done' contra
    // una variable en memoria (`_browserSeenFingerprint`) que se reiniciaba
    // en null con cada recarga de pestaña, asi que la primera pregunta de
    // CUALQUIER tipo despues de recargar "descubria" una huella que en
    // realidad ya era vieja. Ahora la apertura depende 100% de este evento
    // puntual del turno, no de comparar estado persistente contra memoria
    // volatil -- se abre como MUCHO una vez por pagina nueva, y solo si el
    // panel esta cerrado (si el usuario lo tiene abierto, el polling de
    // huella ya se encarga del refresh, ver pollQuickBrowserFingerprint).
    case "browser-page-created": {
      if (ctx.turnSessionId !== state.sessionId) break;
      // Idempotente por message_id: recibir el mismo evento dos veces no
      // reabre un panel que el usuario ya cerró.
      const createdKey = `${state.sessionId}:${event.data.message_id || event.data.file}`;
      if (_browserPageEventsSeen.has(createdKey)) break;
      _browserPageEventsSeen.add(createdKey);
      if (quickBrowserPanelEl.classList.contains("hidden")) openQuickBrowserPanel();
      break;
    }

    // REQ-007: una pagina que YA existia se actualizo -- nunca reabre el
    // panel solo (esa decision es 100% del usuario, REQ-001). Si esta
    // cerrado, se avisa con una nota discreta y un boton para abrirlo; si
    // esta abierto, no hace falta nada (el polling de huella ya refresca
    // el iframe en vivo).
    case "browser-page-updated": {
      if (ctx.turnSessionId !== state.sessionId) break;
      const updatedKey = `${state.sessionId}:upd:${event.data.message_id || event.data.file}`;
      if (_browserPageEventsSeen.has(updatedKey)) break;
      _browserPageEventsSeen.add(updatedKey);
      if (quickBrowserPanelEl.classList.contains("hidden")) {
        const note = appendMessage("system-note", "Se actualizó la vista previa. ", { persist: false });
        const openBtn = document.createElement("button");
        openBtn.type = "button";
        openBtn.className = "msg-inline-action";
        openBtn.textContent = "Abrir";
        openBtn.addEventListener("click", () => openQuickBrowserPanel());
        note?.querySelector(".msg-text")?.appendChild(openBtn);
      }
      break;
    }

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

// (spec adjuntar-archivos-chat) paste / soltar / boton de adjuntar viven ahora en el
// bloque "Adjuntar ARCHIVOS" de abajo: cubren imagenes Y cualquier otro archivo.

// =========================================================================
// Adjuntar ARCHIVOS (spec adjuntar-archivos-chat, REQ-001/002/004/007/008/010).
//
// Extiende el flujo de imagenes de arriba sin tocarlo: las IMAGENES siguen
// yendo en base64 por el body (contexto-multimodal); todo lo demas (Excel, CSV,
// PDF, Word, texto, comprimidos...) se SUBE a `attachment/` de la carpeta de la
// sesion y al enviar viaja solo la referencia (`attachments: [{path,...}]`). El
// aviso de rutas para el agente lo arma el backend (REQ-003), nunca este codigo.
//
// Estados de un chip pendiente: uploading -> ready | error (REQ-004 a).
// =========================================================================
const pendingFilesRowEl = document.getElementById("pending-files-row");
const attachDropOverlayEl = document.getElementById("attach-drop-overlay");

const MAX_FILES_PER_MESSAGE = 10; // REQ-001 g (el backend tambien lo aplica)
const MB_BYTES = 1024 * 1024;
const BIG_TOKENS_WARNING = 100000; // REQ-008 c

let pendingFiles = []; // [{ id, name, size, state, pct, error, meta, inplace, xhr }]
let _attachThresholds = { warn_mb: 100, inplace_mb: 500 };

function formatFileSize(bytes) {
  if (!Number.isFinite(bytes) || bytes < 0) return "";
  const units = ["B", "KB", "MB", "GB"];
  let size = bytes;
  let i = 0;
  while (size >= 1024 && i < units.length - 1) {
    size /= 1024;
    i += 1;
  }
  const text = i === 0 ? String(Math.round(size)) : size.toFixed(1).replace(".", ",");
  return `${text} ${units[i]}`;
}

function formatTokens(n) {
  if (!Number.isFinite(n) || n <= 0) return "";
  return n >= 1000 ? `≈ ${Math.round(n / 1000)} k tokens` : `≈ ${n} tokens`;
}

// Icono por familia de archivo (REQ-004 b) -- mismo mapa que el backend (`kind_for`).
const FILE_KIND_ICON = { excel: "📊", csv: "🧾", pdf: "📕", word: "📘", text: "📄", archive: "🗜️", file: "📎" };
const FILE_KIND_BY_EXT = {
  excel: [".xlsx", ".xlsm", ".xls", ".xlsb"],
  csv: [".csv", ".tsv", ".parquet"],
  pdf: [".pdf"],
  word: [".docx", ".doc"],
  text: [".txt", ".md", ".log", ".json", ".yaml", ".yml", ".xml", ".html", ".htm", ".py", ".js", ".ts", ".sql", ".css", ".ini", ".cfg", ".env"],
  archive: [".zip", ".7z", ".rar", ".gz", ".tar", ".tgz"],
};
function fileKindFor(name) {
  const dot = name.lastIndexOf(".");
  const ext = dot >= 0 ? name.slice(dot).toLowerCase() : "";
  for (const [kind, exts] of Object.entries(FILE_KIND_BY_EXT)) if (exts.includes(ext)) return kind;
  return "file";
}
const SECRET_EXTS = [".env", ".pem", ".key"];
function looksSecret(name) {
  const lower = name.toLowerCase();
  return lower === ".env" || SECRET_EXTS.some((e) => lower.endsWith(e));
}

async function loadAttachThresholds() {
  try {
    const resp = await apiFetch(`${API_BASE}/api/config/attachments`);
    if (resp.ok) _attachThresholds = await resp.json();
  } catch {
    // noop -- se usan los defaults (100/500 MB).
  }
}

// ---- estado de la sesion: hace falta carpeta antes de subir ----------------
async function ensureFolderForAttachments() {
  // Reusa el mismo chequeo del modal bloqueante de "Nueva sesion".
  return await validateProjectFolderOrPrompt();
}

function attachNotice(message) {
  // Mismo aviso ambar que el de imagenes (no error: es un "atencion, pasa esto").
  showImageBlockedNotice(message);
}

// ---- chips ----------------------------------------------------------------
function renderPendingFiles() {
  pendingFilesRowEl.innerHTML = "";
  if (pendingFiles.length === 0) {
    pendingFilesRowEl.classList.add("hidden");
    return;
  }
  pendingFilesRowEl.classList.remove("hidden");
  for (const f of pendingFiles) pendingFilesRowEl.appendChild(buildFileChip(f, { pending: true }));
}

/**
 * Chip de un archivo (REQ-004). `pending`: chip del compositor (con boton de
 * quitar y estados de subida); si no, chip de una burbuja ya enviada.
 */
function buildFileChip(f, { pending = false } = {}) {
  const chip = document.createElement("div");
  chip.className = "file-chip";
  chip.dataset.state = f.state || "ready";
  const kind = f.kind || fileKindFor(f.name || "");
  chip.dataset.kind = kind;

  const icon = document.createElement("span");
  icon.className = "file-chip-icon";
  icon.textContent = FILE_KIND_ICON[kind] || "📎";

  const body = document.createElement("div");
  body.className = "file-chip-body";
  const name = document.createElement("span");
  name.className = "file-chip-name";
  name.textContent = f.name || "archivo";
  name.title = f.path || f.name || "";
  const meta = document.createElement("span");
  meta.className = "file-chip-meta";
  body.append(name, meta);

  const bits = [];
  if (Number.isFinite(f.size)) bits.push(formatFileSize(f.size));
  if (f.state === "uploading") {
    // REQ-008 a: % REAL de subida (XHR), nunca simulado.
    bits.push(f.pct != null ? `Subiendo… ${f.pct} %` : "Subiendo…");
  } else if (f.state === "parsing") {
    bits.push(f.pct != null ? `Leyendo… ${f.pct} %` : "Leyendo…");
  } else if (f.state === "error") {
    bits.push(f.error || "No se pudo subir");
  } else if (f.state === "deleted") {
    bits.push("archivo eliminado");
  } else if (f.state === "missing") {
    bits.push("archivo no encontrado");
  } else {
    if (f.inplace) bits.push("se lee en su lugar");
    const tok = formatTokens(f.token_estimate);
    if (tok) bits.push(tok);
  }
  meta.textContent = bits.filter(Boolean).join(" · ");
  if (f.state === "ready" && f.token_estimate > BIG_TOKENS_WARNING) {
    chip.classList.add("file-chip-warn");
    meta.title = "Es un archivo enorme: el agente lo va a leer por partes.";
  }
  if (looksSecret(f.name || "")) {
    const warn = document.createElement("span");
    warn.className = "file-chip-secret";
    warn.textContent = "puede contener secretos";
    warn.title = "Los archivos .env, .pem y .key suelen tener credenciales.";
    body.appendChild(warn);
  }

  if (f.state === "uploading" || f.state === "parsing") {
    const bar = document.createElement("div");
    bar.className = "file-chip-bar";
    const fill = document.createElement("div");
    fill.className = "file-chip-bar-fill";
    fill.style.width = `${f.pct ?? 0}%`;
    if (f.pct == null) bar.classList.add("indeterminate");
    bar.appendChild(fill);
    body.appendChild(bar);
  }

  chip.append(icon, body);

  if (pending) {
    const rm = document.createElement("button");
    rm.type = "button";
    rm.className = "file-chip-remove";
    rm.title = f.state === "uploading" ? "Cancelar subida" : "Quitar archivo";
    rm.setAttribute("aria-label", rm.title);
    rm.textContent = "✕";
    rm.onclick = () => removePendingFile(f.id);
    chip.appendChild(rm);
  } else if (f.state === "ready" || f.state === "missing") {
    // REQ-004 d: click abre el archivo con la app de Windows (nunca un ejecutable).
    chip.classList.add("file-chip-openable");
    chip.tabIndex = 0;
    chip.setAttribute("role", "button");
    chip.title = "Abrir con la aplicación de Windows";
    const open = () => openAttachment(f, chip);
    chip.addEventListener("click", open);
    chip.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        open();
      }
    });
  }
  return chip;
}

async function openAttachment(f, chip) {
  try {
    const resp = await apiFetchOk(`${API_BASE}/api/sessions/${encodeURIComponent(state.sessionId)}/attachments/open`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: f.path }),
    });
    const data = await resp.json();
    if (data.revealed) attachNotice("Por seguridad no se ejecutan programas: te lo mostré en el Explorador de archivos.");
  } catch (err) {
    // REQ-004 c: si el archivo ya no esta, el chip lo dice en vez de fallar mudo.
    chip.dataset.state = "missing";
    const meta = chip.querySelector(".file-chip-meta");
    if (meta) meta.textContent = "archivo no encontrado";
    attachNotice(friendlyErrorMessage(err));
  }
}

function removePendingFile(id) {
  const f = pendingFiles.find((p) => p.id === id);
  if (!f) return;
  if (f.xhr) f.xhr.abort(); // cancela una subida en curso
  pendingFiles = pendingFiles.filter((p) => p.id !== id);
  renderPendingFiles();
  // Un archivo ya subido que nadie envio se borra del disco (REQ-002 a);
  // uno "leido en su lugar" nunca se borra (no es una copia nuestra).
  if (f.state === "ready" && !f.inplace && f.meta?.name) {
    apiFetch(`${API_BASE}/api/sessions/${encodeURIComponent(state.sessionId)}/attachments/${encodeURIComponent(f.meta.name)}`, {
      method: "DELETE",
    }).catch(() => {});
  }
}

// ---- subida con % real (XHR: fetch no expone progreso de subida) ------------
// EXCEPCION documentada a la regla "solo fetch" de AGENTS.md: es la unica forma
// del navegador de conocer el avance real de una subida (REQ-008 a).
function uploadAttachment(entry, file) {
  return new Promise((resolve) => {
    const xhr = new XMLHttpRequest();
    entry.xhr = xhr;
    const url = `${API_BASE}/api/sessions/${encodeURIComponent(state.sessionId)}/attachments?name=${encodeURIComponent(file.name)}`;
    xhr.open("POST", url);
    const token = typeof getAccessToken === "function" ? getAccessToken() : "";
    if (token) xhr.setRequestHeader("X-Jarvis-Token", token);
    xhr.upload.onprogress = (e) => {
      if (!e.lengthComputable) return;
      entry.pct = Math.min(100, Math.round((e.loaded / e.total) * 100));
      renderPendingFiles();
    };
    xhr.onload = () => {
      entry.xhr = null;
      if (xhr.status >= 200 && xhr.status < 300) {
        try {
          entry.meta = JSON.parse(xhr.responseText);
        } catch {
          entry.state = "error";
          entry.error = "La respuesta de ULTRON no se entendió. Probá de nuevo.";
          resolve(false);
          return;
        }
        entry.state = "ready";
        entry.path = entry.meta.path;
        entry.size = entry.meta.size;
        entry.kind = entry.meta.kind;
        entry.token_estimate = entry.meta.token_estimate;
        entry.name = entry.meta.name; // el backend puede haber desambiguado "(1)"
        resolve(true);
      } else {
        entry.state = "error";
        try {
          entry.error = JSON.parse(xhr.responseText).detail || "No se pudo subir el archivo.";
        } catch {
          entry.error = "No se pudo subir el archivo.";
        }
        resolve(false);
      }
    };
    xhr.onerror = () => {
      entry.xhr = null;
      entry.state = "error";
      entry.error = "Se cortó la conexión con ULTRON. Probá de nuevo.";
      resolve(false);
    };
    xhr.onabort = () => {
      entry.xhr = null;
      resolve(false);
    };
    xhr.send(file);
  });
}

/**
 * Punto de entrada UNICO para boton, soltar y pegar (REQ-001). Separa las
 * imagenes (flujo base64 existente) del resto (subida a `attachment/`).
 */
async function addAttachmentFiles(fileList) {
  const files = Array.from(fileList || []).filter((f) => f && f.name !== undefined);
  if (files.length === 0) return;

  // REQ-001 g: tope de 10 por mensaje (imagenes + archivos juntos).
  const room = MAX_FILES_PER_MESSAGE - pendingFiles.length - pendingImages.length;
  if (room <= 0) {
    attachNotice(`Ya hay ${MAX_FILES_PER_MESSAGE} adjuntos: quitá alguno antes de agregar más.`);
    return;
  }
  const accepted = files.slice(0, room);
  if (accepted.length < files.length) {
    attachNotice(`Solo se pueden adjuntar ${MAX_FILES_PER_MESSAGE} archivos por mensaje: se agregaron ${accepted.length} de ${files.length}.`);
  }

  // REQ-001 h (lote mixto): imagenes -> flujo existente; el resto -> subida. Una
  // imagen que el navegador no puede decodificar (HEIC, corrupta) pasa a archivo.
  const images = accepted.filter((f) => f.type.startsWith("image/"));
  const others = accepted.filter((f) => !f.type.startsWith("image/"));
  if (images.length > 0) {
    const before = pendingImages.length;
    const rejected = await addImageFilesReportingRejects(images);
    if (rejected.length > 0) others.push(...rejected);
    if (pendingImages.length === before && rejected.length === 0) return; // modelo sin vision: ya aviso
  }
  if (others.length === 0) return;

  if (!(await ensureFolderForAttachments())) return; // sin carpeta no hay donde guardar (REQ-002 f)

  await loadAttachThresholds();
  for (const file of others) {
    const sizeMb = file.size / MB_BYTES;
    if (sizeMb > _attachThresholds.inplace_mb) {
      // REQ-007 b: demasiado grande para copiar -- se ofrece leerlo en su lugar.
      attachNotice(
        `«${file.name}» pesa ${formatFileSize(file.size)} y no se copia a la sesión. Usá «Buscar…» (Ctrl+Shift+F) para leerlo en su lugar.`
      );
      continue;
    }
    if (sizeMb > _attachThresholds.warn_mb) {
      const ok = await showConfirmModal({
        title: "Archivo grande",
        message: `«${file.name}» pesa ${formatFileSize(file.size)}. Copiarlo a la carpeta de la sesión ocupa ese espacio en el disco. ¿Querés continuar?`,
        acceptLabel: "Copiar igual",
      });
      if (!ok) continue;
    }
    const entry = { id: crypto.randomUUID(), name: file.name, size: file.size, kind: fileKindFor(file.name), state: "uploading", pct: 0, meta: null, inplace: false, xhr: null };
    pendingFiles.push(entry);
    renderPendingFiles();
    uploadAttachment(entry, file).then(() => renderPendingFiles());
  }
}

// Como `addImageFiles`, pero devuelve las imagenes que NO se pudieron procesar para
// que pasen a subirse como archivo (REQ-001 h).
async function addImageFilesReportingRejects(files) {
  const supported = await activeModelSupportsImages();
  if (!supported) {
    showImageBlockedNotice(
      "Este modelo no soporta imágenes — cambiá a gpt-5-mini/gemini/claude-sonnet-5 para usar esta función."
    );
    return [];
  }
  const rejected = [];
  for (const file of files) {
    try {
      const dataUri = await compressImageFile(file);
      pendingImages.push({ dataUri, id: crypto.randomUUID() });
    } catch {
      rejected.push(file);
    }
  }
  renderPendingImages();
  return rejected;
}

// ---- leer en su lugar: "Buscar…" con el Explorador nativo (REQ-007 c) -------
async function attachFileInPlace() {
  if (!(await ensureFolderForAttachments())) return;
  try {
    const resp = await apiFetchOk(`${API_BASE}/api/fs/native-picker?mode=any-file`);
    const { path } = await resp.json();
    if (!path) return; // el usuario cancelo
    await registerInPlace(path);
  } catch (err) {
    attachNotice(friendlyErrorMessage(err));
  }
}

async function registerInPlace(path) {
  if (pendingFiles.length + pendingImages.length >= MAX_FILES_PER_MESSAGE) {
    attachNotice(`Ya hay ${MAX_FILES_PER_MESSAGE} adjuntos: quitá alguno antes de agregar más.`);
    return;
  }
  try {
    const resp = await apiFetchOk(`${API_BASE}/api/sessions/${encodeURIComponent(state.sessionId)}/attachments/register-path`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path }),
    });
    const meta = await resp.json();
    pendingFiles.push({
      id: crypto.randomUUID(),
      name: meta.name,
      size: meta.size,
      kind: meta.kind,
      path: meta.path,
      token_estimate: meta.token_estimate,
      state: "ready",
      inplace: true,
      meta,
      xhr: null,
    });
    renderPendingFiles();
  } catch (err) {
    attachNotice(friendlyErrorMessage(err));
  }
}

// ---- adjuntos que viajan con el mensaje ------------------------------------
function collectReadyAttachments() {
  return pendingFiles
    .filter((f) => f.state === "ready" && f.meta)
    .map((f) => ({ path: f.meta.path, name: f.meta.name, size: f.meta.size, mime: f.meta.mime, inplace: !!f.inplace }));
}

function hasUploadsInProgress() {
  return pendingFiles.some((f) => f.state === "uploading");
}

// ---- soltar: TODO `.chat-main`, con guard a nivel `document` (REQ-001 b/f) ---
// Sin el guard, soltar un archivo fuera de la zona hace que el navegador lo
// ABRA, reemplazando la pagina y perdiendo la conversacion en curso.
function _dragHasFiles(e) {
  return Array.from(e.dataTransfer?.types || []).includes("Files");
}
["dragover", "drop"].forEach((evt) => {
  document.addEventListener(evt, (e) => {
    if (_dragHasFiles(e)) e.preventDefault();
  });
});
let _dragDepth = 0;
const _chatMainDropZone = document.querySelector(".chat-main");
if (_chatMainDropZone) {
  _chatMainDropZone.addEventListener("dragenter", (e) => {
    if (!_dragHasFiles(e)) return;
    _dragDepth += 1;
    attachDropOverlayEl.classList.remove("hidden");
  });
  _chatMainDropZone.addEventListener("dragleave", (e) => {
    if (!_dragHasFiles(e)) return;
    _dragDepth = Math.max(0, _dragDepth - 1);
    if (_dragDepth === 0) attachDropOverlayEl.classList.add("hidden");
  });
  _chatMainDropZone.addEventListener("drop", (e) => {
    _dragDepth = 0;
    attachDropOverlayEl.classList.add("hidden");
    const files = e.dataTransfer?.files;
    if (files && files.length > 0) addAttachmentFiles(files);
  });
}

// ---- pegar: archivos del portapapeles (REQ-001 d) --------------------------
inputEl.addEventListener("paste", (e) => {
  const files = e.clipboardData?.files;
  if (files && files.length > 0) {
    e.preventDefault(); // nunca pegar el binario como texto
    addAttachmentFiles(files);
  }
});

// ---- boton de adjuntar -----------------------------------------------------
attachImageBtnEl.addEventListener("click", () => imageFileInputEl.click());
imageFileInputEl.addEventListener("change", () => {
  if (imageFileInputEl.files.length > 0) addAttachmentFiles(imageFileInputEl.files);
  imageFileInputEl.value = ""; // permite volver a elegir el mismo archivo dos veces seguidas
});
// Ctrl+Shift+F: adjuntar "en su lugar" (archivo enorme) desde el teclado.
document.addEventListener("keydown", (e) => {
  if (e.ctrlKey && e.shiftKey && e.key.toLowerCase() === "f" && document.activeElement === inputEl) {
    e.preventDefault();
    attachFileInPlace();
  }
});

// ---- Configuracion > Conexiones: umbrales (REQ-007 a) ------------------------
const connAttachWarnEl = document.getElementById("conn-attach-warn-mb");
const connAttachInplaceEl = document.getElementById("conn-attach-inplace-mb");
const connAttachSaveEl = document.getElementById("conn-attach-save");
const connAttachStatusEl = document.getElementById("conn-attach-status");

async function _loadAttachConfig() {
  connAttachStatusEl.textContent = "";
  try {
    const resp = await apiFetch(`${API_BASE}/api/config/attachments`);
    if (!resp.ok) throw await httpErrorFromResponse(resp);
    const data = await resp.json();
    connAttachWarnEl.value = data.warn_mb;
    connAttachInplaceEl.value = data.inplace_mb;
  } catch (err) {
    connAttachStatusEl.textContent = `No se pudieron cargar los límites. ${friendlyErrorMessage(err)}`;
    connAttachStatusEl.className = "settings-status error";
  }
}
connAttachSaveEl.addEventListener("click", async () => {
  connAttachStatusEl.textContent = "Guardando…";
  connAttachStatusEl.className = "settings-status";
  try {
    await apiFetchOk(`${API_BASE}/api/config/attachments`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        warn_mb: connAttachWarnEl.value.trim() !== "" ? Number(connAttachWarnEl.value) : null,
        inplace_mb: connAttachInplaceEl.value.trim() !== "" ? Number(connAttachInplaceEl.value) : null,
      }),
    });
    connAttachStatusEl.textContent = "Guardado ✓";
    connAttachStatusEl.className = "settings-status ok";
    loadAttachThresholds();
  } catch (err) {
    connAttachStatusEl.textContent = friendlyErrorMessage(err);
    connAttachStatusEl.className = "settings-status error";
  }
});
document.querySelector('.settings-tab[data-tab="connections"]')?.addEventListener("click", _loadAttachConfig);

// ---- Configuracion > Conexiones: imagenes dentro de PDF y Word (spec leer-imagenes-pdf-word REQ-007) -------
const connImgMinPxEl = document.getElementById("conn-img-min-px");
const connImgAskCountEl = document.getElementById("conn-img-ask-count");
const connImgAskPctEl = document.getElementById("conn-img-ask-pct");
const connImgSaveEl = document.getElementById("conn-img-save");
const connImgStatusEl = document.getElementById("conn-img-status");

async function _loadDocImagesConfig() {
  connImgStatusEl.textContent = "";
  try {
    const resp = await apiFetch(`${API_BASE}/api/config/document-images`);
    if (!resp.ok) throw await httpErrorFromResponse(resp);
    const data = await resp.json();
    connImgMinPxEl.value = data.min_px;
    connImgAskCountEl.value = data.ask_count;
    connImgAskPctEl.value = data.ask_pct;
  } catch (err) {
    connImgStatusEl.textContent = `No se pudieron cargar estos límites. ${friendlyErrorMessage(err)}`;
    connImgStatusEl.className = "settings-status error";
  }
}
connImgSaveEl.addEventListener("click", async () => {
  connImgStatusEl.textContent = "Guardando…";
  connImgStatusEl.className = "settings-status";
  const numberOrNull = (el) => (el.value.trim() !== "" ? Number(el.value) : null); // vacio = conservar el valor actual
  try {
    await apiFetchOk(`${API_BASE}/api/config/document-images`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        min_px: numberOrNull(connImgMinPxEl),
        ask_count: numberOrNull(connImgAskCountEl),
        ask_pct: numberOrNull(connImgAskPctEl),
      }),
    });
    connImgStatusEl.textContent = "Guardado ✓";
    connImgStatusEl.className = "settings-status ok";
  } catch (err) {
    connImgStatusEl.textContent = friendlyErrorMessage(err);
    connImgStatusEl.className = "settings-status error";
  }
});
document.querySelector('.settings-tab[data-tab="connections"]')?.addEventListener("click", _loadDocImagesConfig);

// ---- burbuja ya enviada / reconstruida: chips de archivo (REQ-004 / REQ-010) -----
function buildMessageAttachmentsRow(attachments) {
  const row = document.createElement("div");
  row.className = "msg-files-row";
  for (const a of attachments) {
    const state = a.deleted ? "deleted" : a.exists === false ? "missing" : "ready";
    row.appendChild(buildFileChip({ ...a, state }));
  }
  return row;
}


formEl.addEventListener("submit", (e) => {
  e.preventDefault();
  // Pedido explicito del usuario: ya no se puede encolar un mensaje nuevo
  // mientras Vision sigue procesando el turno anterior -- hay que esperar
  // a que termine (vuelve a "idle" via setSendButtonState) o presionar
  // "Detener" primero. Antes esto SI se permitia (ver REQ-003 de la spec
  // parada-y-cola-mensajes, comentario viejo en sendMessage sobre
  // `state.sending` como contador) -- cambio de comportamiento deliberado.
  if (sendOrStopBtnEl.dataset.state !== "idle") return;

  // REQ-002/004: con una subida en curso NO se envia (el agente recibiria una
  // ruta a un archivo incompleto). Un chip con error no bloquea: se descarta.
  if (hasUploadsInProgress()) {
    attachNotice("Esperá a que termine de subirse el archivo antes de enviar.");
    return;
  }
  const text = inputEl.value;
  const images = pendingImages.map((p) => p.dataUri);
  const attachments = collectReadyAttachments();
  const sentFiles = pendingFiles.filter((f) => f.state === "ready" && f.meta).map((f) => ({ ...f.meta, state: "ready" }));
  // REQ-007 (spec aviso-proceso-terminado): el primer clic de Enviar (o Enter) pide el permiso de notificaciones si todavia no se decidio:
  // el navegador lo exige desde un gesto del usuario, y este submit corre dentro de ese gesto.
  if (text.trim() || images.length > 0 || attachments.length > 0) askNotifyPermissionOnFirstSend();
  inputEl.value = "";
  pendingImages = [];
  pendingFiles = [];
  renderPendingImages();
  renderPendingFiles();
  // spec compositor-expandible REQ-004: NO se llama toggleComposerExpanded
  // aca -- el estado expandido persiste entre mensajes a proposito. Si
  // esta en auto-grow normal, se recalcula la altura (vuelve a chico
  // porque el texto ya se vacio arriba).
  autoGrowComposer();
  sendMessage(text, {
    images: images.length > 0 ? images : null,
    attachments: attachments.length > 0 ? attachments : null,
    sentFiles,
  });
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

// Los mensajes previos de esta sesion (F5, reabrir la pestaña) ya NO se pintan aca desde la copia del navegador: REQ-016 los trae
// de SQLite en restoreActiveSessionOnLoad (bloque de inicializacion final), que los ubica ANTES de la nota de abajo.

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
// spec aviso-contexto-largo REQ-008/009: chip de contexto siempre visible en la barra superior.
const contextChipEl = document.getElementById("context-chip");
const contextChipValEl = document.getElementById("context-chip-val");
const CONTEXT_WARN_PCT = 75; // el aviso textual sale al llegar aca...
const CONTEXT_REARM_PCT = 60; // ...y se rearma recien al bajar de aca (compactar o /clear), no con una oscilacion chica
const _contextWarnedSessions = new Set(); // sesiones a las que YA se les aviso (una vez por sesion, mientras no baje de CONTEXT_REARM_PCT)


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
// separado del historial (que vive en SQLite; REQ-016: en el navegador solo
// queda la copia chica de las sesiones sin carpeta, ver SESSION_COPY_PREFIX
// mas arriba). Permite listar/click sin cargar todo el historial de cada sesion.
// -------------------------------------------------------------------------
const SESSIONS_INDEX_KEY = "vision.sessionsIndex";

function loadSessionsIndex() {
  try {
    const index = JSON.parse(localStorage.getItem(SESSIONS_INDEX_KEY) || "{}");
    // Un indice con otra forma (`null`, una lista) se trata como vacio en vez de romper a quien lo lea.
    return index && typeof index === "object" && !Array.isArray(index) ? index : {};
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

    li.append(nameSpan);
    // REQ-018 (spec fix-detener-y-progreso-visible, D5 = «Marca en la lista»): la sesion que le hizo una pregunta al usuario y espera
    // su respuesta lo dice en la lista (texto real: lo lee tambien un lector de pantalla). En la sesion donde esta el usuario no va:
    // ahi la pregunta ya se ve en su panel. Se va al contestarla (o al mandar otro mensaje en esa sesion).
    if (id !== state.sessionId && _pendingQuestions.has(id)) {
      const questionMark = document.createElement("span");
      questionMark.className = "session-question-mark";
      questionMark.textContent = "❓ te hizo una pregunta";
      li.append(questionMark);
      li.title = "Ir a esta sesión: te hizo una pregunta";
    }
    li.append(dateSpan, renameBtn, deleteBtn);
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
// REQ-002 (spec fix-busqueda-limpia): numero de la ultima busqueda pedida. Una respuesta que llega cuando ya se borro o cambio la palabra
// trae un numero viejo y se descarta: antes volvia a pintar los resultados de una palabra que el usuario ya habia borrado.
let searchSeq = 0;

// REQ-001 (spec fix-busqueda-limpia): borrar la busqueda deja la lista de resultados VACIA (no solo oculta) y vuelve la lista de sesiones.
function resetSessionSearchResults() {
  searchSeq += 1; // cualquier busqueda en viaje queda vieja
  clearTimeout(searchDebounceTimer);
  sessionSearchResultsEl.innerHTML = "";
  sessionSearchResultsEl.classList.add("hidden");
  sessionListEl.classList.remove("hidden");
  clearSearchHighlights();
}
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
    // BUG real (reportado 2026-09-29, "no me aparece la barra"): antes se llamaba SIN
    // `session_id`. Desde que ya no existe una carpeta global, el backend resuelve la base
    // a partir de la carpeta de ESA sesion; sin el id respondia `persistence_available:
    // false` y `results: []`, y la busqueda caia al fallback de localStorage, que solo ve las
    // sesiones de este navegador. Con el id de la sesion actual busca en su carpeta.
    const resp = await apiFetch(
      `${API_BASE}/api/sessions/search?q=${encodeURIComponent(term)}&session_id=${encodeURIComponent(state.sessionId)}`
    );
    const data = await resp.json();
    if (data.persistence_available) return data.results;
  } catch {
    // sin backend disponible -- cae al fallback de localStorage abajo
  }

  // REQ-006: sin persistencia SQLite, se recorre el texto de la copia del
  // navegador de cada sesion indexada (spec fix-detener-y-progreso-visible
  // REQ-016: solo las sesiones sin carpeta tienen copia, y acotada; ver
  // readLocalCopy).
  const index = loadSessionsIndex();
  const lowerTerm = term.toLowerCase();
  const results = [];
  for (const [id, info] of Object.entries(index)) {
    const history = readLocalCopy(id);
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
    // BUG real: antes se esperaba un tiempo FIJO de 200 ms, pero `activateSession` hace
    // varias consultas al backend antes de pintar los mensajes; si la conversacion no
    // estaba lista no habia coincidencias y la barra nunca aparecia. Ahora se espera a que
    // la activacion TERMINE de verdad.
    await activateSession(sessionId);
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
  // Pedido del usuario (2026-09-29): "1/10, 2/10... 10/10 y vuelve a 1/10".
  searchNavCountEl.textContent = `${activeIndex + 1}/${matches.length}`;
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

// Enter en el buscador pasa a la coincidencia siguiente y Shift+Enter a la anterior (da la
// vuelta: despues de la ultima vuelve a la primera). Solo si ya hay coincidencias resaltadas
// de ESE mismo termino; si no, Enter no hace nada (el debounce ya esta buscando).
sessionSearchInputEl.addEventListener("keydown", (e) => {
  if (e.key !== "Enter") return;
  const { matches, term } = searchState;
  if (matches.length === 0 || term !== sessionSearchInputEl.value.trim()) return;
  e.preventDefault();
  focusSearchMatch(searchState.activeIndex + (e.shiftKey ? -1 : 1));
});

searchNavPrevEl.addEventListener("click", () => focusSearchMatch(searchState.activeIndex - 1));
searchNavNextEl.addEventListener("click", () => focusSearchMatch(searchState.activeIndex + 1));
searchNavCloseEl.addEventListener("click", () => {
  sessionSearchInputEl.value = "";
  resetSessionSearchResults();
});

sessionSearchInputEl.addEventListener("input", () => {
  clearTimeout(searchDebounceTimer);
  const term = sessionSearchInputEl.value.trim();

  if (!term) {
    resetSessionSearchResults();
    return;
  }

  // REQ-001: debounce de 300ms -- no se dispara una consulta por cada tecla.
  const seq = ++searchSeq;
  searchDebounceTimer = setTimeout(async () => {
    const results = await searchAllSessionsGlobal(term);
    // REQ-002 (spec fix-busqueda-limpia): mientras viajaba la respuesta, el usuario borro o cambio la palabra: no se pinta.
    if (seq !== searchSeq || sessionSearchInputEl.value.trim() !== term) return;
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

// spec indice-resiliente-y-borrado-seguro REQ-012: para una sesión SIN carpeta registrada el backend responde, como «referencia
// visual», la carpeta de Vision (`%LOCALAPPDATA%\Vision`); eso se veía como «perdió la ruta de mi carpeta». El chip dice la verdad.
const NO_FOLDER_CHIP_TEXT = "Sin carpeta registrada";
function paintSessionPathFromStatus(statusData) {
  if (statusData.persistence_available === false) updateMetaSessionPath(NO_FOLDER_CHIP_TEXT);
  else if (statusData.project_path) updateMetaSessionPath(statusData.project_path);
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
// spec sesion-publica-privada REQ-008: interruptor «Sesion publica» (apagado por defecto) y su aviso.
const sessionEditPublicEl = document.getElementById("session-edit-public");
const sessionEditPublicWarningEl = document.getElementById("session-edit-public-warning");
let _sessionPublicAtOpen = false; // lo que el servidor decia al abrir el modal: para saber si la persona lo cambio

function syncSessionPublicWarning() {
  sessionEditPublicWarningEl.classList.toggle("hidden", !sessionEditPublicEl.checked);
}
sessionEditPublicEl.addEventListener("change", syncSessionPublicWarning);

// spec sesion-publica-privada REQ-008: al abrir el modal el interruptor refleja el estado REAL de la sesion (privada si no tiene carpeta o si no se
// puede leer: el valor por defecto). Es de mejor esfuerzo: si falla, queda apagado y el resto del modal funciona igual.
async function loadSessionVisibility(sessionId) {
  _sessionPublicAtOpen = false;
  sessionEditPublicEl.checked = false;
  sessionEditPublicEl.dataset.loaded = "false";
  let touched = false; // si la persona toca el interruptor antes de que llegue la respuesta, su eleccion manda
  const onTouch = () => {
    touched = true;
  };
  sessionEditPublicEl.addEventListener("change", onTouch, { once: true });
  syncSessionPublicWarning();
  try {
    const resp = await apiFetch(`${API_BASE}/api/sessions/${encodeURIComponent(sessionId)}/visibility`);
    if (!resp.ok) return;
    const data = await resp.json();
    if (sessionId !== state.sessionId) return; // respuesta tardia de otra sesion: no se pinta sobre la actual
    _sessionPublicAtOpen = Boolean(data.public);
    if (!touched) {
      sessionEditPublicEl.checked = _sessionPublicAtOpen;
      syncSessionPublicWarning();
    }
  } catch {
    // Sin respuesta: se queda en privada.
  } finally {
    sessionEditPublicEl.removeEventListener("change", onTouch);
    if (sessionId === state.sessionId) sessionEditPublicEl.dataset.loaded = "true";
  }
}

function openSessionEditModal() {
  sessionEditNameEl.value = metaSessionNameEl.textContent;
  sessionEditIdEl.value = state.sessionId;
  sessionEditPathEl.value = metaSessionPathEl.textContent === NO_FOLDER_CHIP_TEXT ? "" : metaSessionPathEl.textContent;
  sessionEditStatusEl.textContent = "";
  sessionEditOverlayEl.classList.remove("hidden");
  sessionEditNameEl.focus();
  loadSessionVisibility(state.sessionId);
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

/**
 * spec cambio-carpeta-historial: antes de CAMBIAR la carpeta de una sesion que ya tenia otra, pregunta si se copia su historial.
 * Devuelve `true` (copiar), `false` (dejarlo donde esta, o no hace falta preguntar) o `null` (la persona cerro la pregunta: no cambiar
 * la carpeta). De mejor esfuerzo: si la consulta falla, devuelve `false` y el guardado sigue como siempre.
 */
async function askAboutHistoryOnFolderChange(sessionId, newPath) {
  let preview = null;
  try {
    const resp = await apiFetch(
      `${API_BASE}/api/sessions/${encodeURIComponent(sessionId)}/folder-change-preview?path=${encodeURIComponent(newPath)}`
    );
    if (resp.ok) preview = await resp.json();
  } catch {
    // Sin respuesta: se sigue como siempre, sin preguntar.
  }
  // `"none"` = no hacia falta preguntar (no es lo mismo que contestar «dejarlo», que es `false`): solo en ese caso se consulta si la CARPETA tiene
  // conversaciones anteriores (spec importar-historial-de-carpeta REQ-012: nunca dos avisos seguidos).
  if (!preview || !preview.applicable) return "none";
  const n = preview.message_count;
  let message =
    `Esta sesión tiene ${n} mensaje${n === 1 ? "" : "s"} guardado${n === 1 ? "" : "s"} en ${preview.old_path}. ` +
    `Si la pasás a ${preview.new_path}, ¿querés copiar el historial a la carpeta nueva? ` +
    `La copia no borra nada de la carpeta anterior.`;
  if (preview.attachments > 0) {
    message +=
      ` Los ${preview.attachments} archivo${preview.attachments === 1 ? "" : "s"} adjunto${preview.attachments === 1 ? "" : "s"} no se copian: ` +
      `sus mensajes sí, y en la carpeta nueva se verán como «archivo no encontrado».`;
  }
  message += ` Si lo dejás donde está, después de reiniciar ULTRON esta sesión no recordará lo anterior.`;
  const answer = await showConfirmModal({
    title: "Cambiar la carpeta de la sesión",
    message,
    acceptLabel: "Copiar el historial",
    cancelLabel: "Dejarlo donde está",
    dismissValue: null,
  });
  return answer; // true = copiar, false = dejarlo, null = cerrada con la ✕
}

// spec importar-historial-de-carpeta: «dd/mm hh:mm» de una conversacion anterior, para que la lista sea reconocible a simple vista.
function formatFolderHistoryDate(epochSeconds) {
  if (!epochSeconds) return "";
  const d = new Date(epochSeconds * 1000);
  const dd = String(d.getDate()).padStart(2, "0");
  const mm = String(d.getMonth() + 1).padStart(2, "0");
  const hh = String(d.getHours()).padStart(2, "0");
  const mi = String(d.getMinutes()).padStart(2, "0");
  return `${dd}/${mm} ${hh}:${mi}`;
}

/**
 * spec importar-historial-de-carpeta REQ-010/011/012: la carpeta elegida YA tiene conversaciones anteriores del usuario. Muestra la lista y
 * devuelve la decision: `{ action: "import", sessionId }`, `{ action: "open", sessionId, registered }`, `{ action: "keep" }` (seguir con esta sesion
 * tal cual) o `{ action: "dismiss" }` (la ✕ o el clic fuera: NO cambiar nada). Si no hay nada que ofrecer, o la consulta falla, devuelve `{ action: "keep" }`
 * sin mostrar nada (mejor esfuerzo: la carpeta se asigna como siempre).
 */
async function askAboutFolderHistory(sessionId, folderPath) {
  let info = null;
  try {
    const resp = await apiFetch(
      `${API_BASE}/api/sessions/${encodeURIComponent(sessionId)}/folder-sessions?path=${encodeURIComponent(folderPath)}`
    );
    if (resp.ok) info = await resp.json();
  } catch {
    // Sin respuesta: se sigue como siempre, sin preguntar.
  }
  if (!info || !info.applicable || !Array.isArray(info.sessions) || info.sessions.length === 0) return { action: "keep" };

  const overlayEl = document.getElementById("folder-history-overlay");
  const introEl = document.getElementById("folder-history-intro");
  const listEl = document.getElementById("folder-history-list");
  const noteEl = document.getElementById("folder-history-note");
  const keepBtnEl = document.getElementById("folder-history-keep");
  const closeBtnEl = document.getElementById("folder-history-close");
  const n = info.sessions.length;
  // spec sesion-publica-privada REQ-009: las conversaciones de OTRAS personas que ahora estan publicas se rotulan; sin ninguna, el aviso es el de siempre.
  const foreignPublic = info.sessions.filter((s) => s.public && s.own === false);
  const allForeign = foreignPublic.length === n;
  introEl.textContent =
    `La carpeta ${folderPath} ya tiene ${n} conversación${n === 1 ? "" : "es"} anterior${n === 1 ? "" : "es"}` +
    `${allForeign ? " pública" + (n === 1 ? "" : "s") + " de otras personas" : foreignPublic.length ? " (algunas públicas de otras personas)" : " tuya" + (n === 1 ? "" : "s")}. ` +
    `¿Querés traer una a esta sesión o abrirla directamente?`;
  noteEl.textContent = info.can_import
    ? "Importar COPIA el historial: la conversación anterior queda como estaba."
    : "Esta sesión ya tiene mensajes, así que no se puede importar encima (se mezclarían). Podés abrir la conversación anterior.";
  if (foreignPublic.length) {
    noteEl.textContent += " De las conversaciones públicas de otras personas no se copian los resultados de Outlook ni de Teams (verás «contenido privado omitido»).";
  }
  listEl.replaceChildren();

  return new Promise((resolve) => {
    const finish = (decision) => {
      overlayEl.classList.add("hidden");
      keepBtnEl.onclick = null;
      closeBtnEl.onclick = null;
      overlayEl.onclick = null;
      listEl.replaceChildren();
      resolve(decision);
    };
    for (const s of info.sessions) {
      const li = document.createElement("li");
      li.className = "folder-history-item";
      li.dataset.sessionId = s.id;
      const infoEl = document.createElement("div");
      infoEl.className = "folder-history-info";
      const titleEl = document.createElement("span");
      titleEl.className = "folder-history-title";
      titleEl.textContent = s.title;
      if (s.public) {
        const badgeEl = document.createElement("span");
        badgeEl.className = "folder-history-badge";
        badgeEl.textContent = s.own === false ? "pública · de otra persona" : "pública";
        titleEl.appendChild(badgeEl);
      }
      const metaEl = document.createElement("span");
      metaEl.className = "folder-history-meta";
      metaEl.textContent = `${s.message_count} mensaje${s.message_count === 1 ? "" : "s"} · ${formatFolderHistoryDate(s.updated_at)}`;
      infoEl.append(titleEl, metaEl);
      const actionsEl = document.createElement("div");
      actionsEl.className = "folder-history-actions";
      if (info.can_import) {
        const importBtn = document.createElement("button");
        importBtn.type = "button";
        importBtn.className = "acc-action-btn folder-history-import";
        importBtn.textContent = "Importar a esta sesión";
        importBtn.onclick = () => finish({ action: "import", sessionId: s.id });
        actionsEl.appendChild(importBtn);
      }
      const openBtn = document.createElement("button");
      openBtn.type = "button";
      openBtn.className = "acc-action-btn secondary folder-history-open";
      openBtn.textContent = "Abrir esa sesión";
      openBtn.onclick = () => finish({ action: "open", sessionId: s.id, registered: Boolean(s.registered) });
      actionsEl.appendChild(openBtn);
      li.append(infoEl, actionsEl);
      listEl.appendChild(li);
    }
    keepBtnEl.onclick = () => finish({ action: "keep" });
    closeBtnEl.onclick = () => finish({ action: "dismiss" });
    overlayEl.onclick = (e) => {
      if (e.target === overlayEl) finish({ action: "dismiss" });
    };
    overlayEl.classList.remove("hidden");
  });
}

/**
 * spec importar-historial-de-carpeta REQ-011: «Abrir esa sesion». Si la conversacion anterior no estaba registrada en el indice, se registra con su
 * carpeta (sin tocar su historial ni su titulo) y se abre. La sesion actual, vacia, no se toca.
 */
async function openPreviousSessionFromFolder(previousId, registered, folderPath) {
  if (!registered) await assignSessionProjectPath(previousId, folderPath, "");
  await activateSession(previousId);
}

async function assignSessionProjectPath(sessionId, path, title, copyHistory = false, importFromSessionId = "", isPublic = false) {
  const body = { path, title: title || "", copy_history: copyHistory };
  // spec sesion-publica-privada REQ-001: el campo solo viaja cuando se pide PUBLICA (sin el, la sesion queda privada, como siempre).
  if (isPublic) body.public = true;
  // spec importar-historial-de-carpeta REQ-004: el campo solo viaja cuando se eligio importar (sin el, el pedido es el de siempre).
  if (importFromSessionId) body.import_from_session_id = importFromSessionId;
  const resp = await apiFetch(`${API_BASE}/api/sessions/${sessionId}/project-path`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}));
    throw httpError(resp.status, err);
  }
  return resp.json().catch(() => ({}));
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
      // spec cambio-carpeta-historial REQ-007/008/009: si la sesion ya tenia otra carpeta con historial, se PREGUNTA que hacer con el.
      const copyAnswer = await askAboutHistoryOnFolderChange(state.sessionId, newPath);
      if (copyAnswer === null) {
        // La ✕ o el clic fuera: la persona se arrepintio, la carpeta no cambia (el nombre, si lo cambio, ya quedo guardado arriba).
        sessionEditStatusEl.textContent = "";
        return;
      }
      // spec importar-historial-de-carpeta REQ-010/011/012: si NO se hizo la pregunta de arriba, se consulta si la CARPETA ya tiene conversaciones
      // anteriores del usuario (caso real: sesion nueva + carpeta de una sesion vieja) y se ofrece importarlas o abrirlas.
      let importFrom = "";
      if (copyAnswer === "none") {
        const decision = await askAboutFolderHistory(state.sessionId, newPath);
        if (decision.action === "dismiss") {
          sessionEditStatusEl.textContent = "";
          return;
        }
        if (decision.action === "open") {
          await openPreviousSessionFromFolder(decision.sessionId, decision.registered, newPath);
          closeSessionEditModal();
          return;
        }
        if (decision.action === "import") importFrom = decision.sessionId;
      }
      const result = await assignSessionProjectPath(
        state.sessionId,
        newPath,
        newName || metaSessionNameEl.textContent,
        copyAnswer === true,
        importFrom,
        sessionEditPublicEl.checked
      );
      _sessionPublicAtOpen = sessionEditPublicEl.checked; // ya quedo guardado junto con la carpeta
      updateMetaSessionPath(newPath);
      if (importFrom) {
        if (result.import_failed) {
          // Nunca un «Guardado ✓» mentiroso: la carpeta quedo asignada, pero el historial NO se trajo.
          sessionEditStatusEl.textContent =
            "La carpeta se guardó, pero no pude importar la conversación anterior. Podés intentarlo de nuevo o abrirla desde la lista de sesiones.";
          sessionEditStatusEl.className = "settings-status error";
          return;
        }
        // Repinta el chat desde SQLite: la sesion ahora tiene todo el historial importado.
        await activateSession(state.sessionId);
      }
      updatePersistenceBanner(true);
      rememberSessionFolder(state.sessionId, true); // REQ-016: desde aca su chat va a SQLite y deja de copiarse
      // BUG real detectado: faltaba refrescar el footer ("Sin carpeta
      // asignada a esta sesión") tras asignar carpeta desde este modal --
      // solo se actualizaba metaSessionPathEl (el chip de arriba), el
      // footer de abajo dependia unicamente de activateSession()/status.
      refreshProjectFooter();
    }
    // spec sesion-publica-privada REQ-008: si solo cambio el interruptor (la carpeta no se reasigno arriba), se guarda por su propio endpoint.
    // Con carpeta nueva el campo `public` ya viajo con ella. Una sesion sin carpeta no tiene donde guardarlo: se avisa en vez de callar.
    if (sessionEditPublicEl.checked !== _sessionPublicAtOpen) {
      if (!metaSessionPathEl.textContent || metaSessionPathEl.textContent === NO_FOLDER_CHIP_TEXT) {
        sessionEditStatusEl.textContent = "Primero elegí la carpeta de la sesión: sin carpeta no puedo guardar si es pública o privada.";
        sessionEditStatusEl.className = "settings-status error";
        return;
      }
      const visibilityResp = await apiFetch(`${API_BASE}/api/sessions/${encodeURIComponent(state.sessionId)}/visibility`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ public: sessionEditPublicEl.checked }),
      });
      if (!visibilityResp.ok) throw await httpErrorFromResponse(visibilityResp);
      _sessionPublicAtOpen = sessionEditPublicEl.checked;
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
  // spec cambio-carpeta-historial REQ-010: texto del boton secundario y lo que devuelve la ✕ (y el clic fuera). Defaults = el
  // comportamiento de siempre («Cancelar» y `false`), asi ningun llamador existente cambia. Con `dismissValue: null` la ✕ se puede
  // distinguir de «no» (que devuelve `false`).
  cancelLabel = "Cancelar",
  dismissValue = false,
} = {}) {
  return new Promise((resolve) => {
    confirmTitleEl.textContent = title;
    confirmMessageEl.textContent = message;
    confirmAcceptBtnEl.textContent = acceptLabel;
    confirmCancelBtnEl.textContent = cancelLabel;
    confirmCancelBtnEl.classList.toggle("hidden", hideCancel);
    confirmOverlayEl.classList.remove("hidden");

    const cleanup = (result) => {
      confirmOverlayEl.classList.add("hidden");
      confirmCancelBtnEl.classList.remove("hidden");  // restaura para el proximo uso normal
      confirmCancelBtnEl.textContent = "Cancelar"; // y su texto de siempre
      confirmAcceptBtnEl.onclick = null;
      confirmCancelBtnEl.onclick = null;
      confirmCloseEl.onclick = null;
      resolve(result);
    };

    confirmAcceptBtnEl.onclick = () => cleanup(true);
    confirmCancelBtnEl.onclick = () => cleanup(false);
    confirmCloseEl.onclick = () => cleanup(dismissValue);
  });
}
confirmOverlayEl.addEventListener("click", (e) => {
  // Clic fuera = lo mismo que la ✕ (respeta `dismissValue`); sin `dismissValue` es el «Cancelar» de siempre.
  if (e.target === confirmOverlayEl) confirmCloseEl.click();
});

// Elimina una sesion por completo: backend (cancela turno en curso si
// hubiera + borra estado en memoria + borra su carpeta de vista previa),
// historial persistido en localStorage, y su entrada en el indice de
// sesiones. Si la sesion eliminada era la activa, activa una nueva vacia
// para no dejar la UI en un estado inconsistente.
// spec aviso-eliminar-sesion-usuarios REQ-006/007: la frase que cuenta que otras conversaciones hay en la carpeta. Vacia si no hay nada que decir.
// Con tope de 5 nombres por grupo y «y N más». `others` puede faltar (backend viejo): entonces no agrega nada.
const OTHERS_SHOWN = 5;
function describeOthersInFolder(others) {
  if (!others || typeof others !== "object") return "";
  const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;
  const list = (items, label) => {
    const shown = items.slice(0, OTHERS_SHOWN).map(label).join(", ");
    return items.length > OTHERS_SHOWN ? `${shown} y ${items.length - OTHERS_SHOWN} más` : shown;
  };
  let out = "";
  const mine = Array.isArray(others.mine) ? others.mine : [];
  if (mine.length > 0) {
    out += ` En esa carpeta también tenés: ${list(mine, (s) => `«${s.title}» (${plural(s.message_count, "mensaje", "mensajes")})`)}.`;
  }
  const pub = Array.isArray(others.public) ? others.public : [];
  if (pub.length > 0) {
    out +=
      ` Conversaciones públicas de otras personas: ` +
      `${list(pub, (s) => `«${s.title}» de ${s.published_by} (${plural(s.message_count, "mensaje", "mensajes")})`)}.`;
  }
  const priv = Array.isArray(others.private_by_user) ? others.private_by_user : [];
  if (priv.length > 0) {
    out += ` Hay conversaciones privadas de otras personas (no se ven sus nombres): ${list(priv, (g) => `${g.user} (${g.count})`)}.`;
  }
  return out;
}

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
  // spec indice-resiliente-y-borrado-seguro REQ-011: se dice QUE se va a perder y DONDE, y si el mismo identificador existe en otra
  // carpeta (borrar la sesión no la elimina). Solo texto: ningún paso ni botón nuevo.
  if (impact.message_count > 0) {
    message +=
      ` Tiene ${impact.message_count} mensaje${impact.message_count === 1 ? "" : "s"} guardado${impact.message_count === 1 ? "" : "s"}` +
      (impact.project_path ? ` en ${impact.project_path}` : "") +
      `. Los archivos de esa carpeta no se tocan.`;
  }
  for (const copy of Array.isArray(impact.copies_elsewhere) ? impact.copies_elsewhere : []) {
    message +=
      ` Hay otra copia de esta conversación con el mismo identificador en ${copy.project_path}` +
      ` (${copy.message_count} mensaje${copy.message_count === 1 ? "" : "s"}); borrar la sesión no la elimina.`;
  }
  // spec aviso-eliminar-sesion-usuarios REQ-006: quien mas usa esa carpeta, solo para saber. Mis sesiones con nombre, las publicas con su autor y las
  // privadas de otras personas SOLO por usuario y cantidad (nunca titulos: Vision aisla por usuario). Solo texto: ningun paso ni boton nuevo.
  message += describeOthersInFolder(impact.others_in_folder);
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

  // REQ-009 g (spec adjuntar-archivos-chat): si la sesion tiene adjuntos, un SEGUNDO paso
  // pregunta si tambien se borran del disco. El default es CONSERVARLOS: la carpeta puede ser
  // del usuario (no de Vision) y borrar archivos es irreversible. Se hace ANTES de eliminar la
  // sesion, porque despues ya no hay a que ligar la limpieza.
  try {
    const sumResp = await apiFetch(`${API_BASE}/api/sessions/${encodeURIComponent(sessionId)}/attachments/summary`);
    const summary = sumResp.ok ? await sumResp.json() : { has_attachments: false };
    if (summary.has_attachments) {
      const alsoDelete = await showConfirmModal({
        title: "Archivos adjuntos",
        message:
          "Esta sesión tiene archivos e imágenes adjuntos. ¿Querés borrar también los archivos de la carpeta " +
          "«attachment» del disco? Si elegís conservarlos, quedan ahí y solo se elimina la conversación.",
        acceptLabel: "Borrarlos también",
      });
      if (alsoDelete) {
        try {
          const prev = await (await apiFetchOk(`${API_BASE}/api/sessions/${encodeURIComponent(sessionId)}/attachments/cleanup-preview`)).json();
          const ids = (prev.rows || []).filter((r) => r.deletable).map((r) => r.id);
          if (ids.length > 0) {
            await apiFetchOk(`${API_BASE}/api/sessions/${encodeURIComponent(sessionId)}/attachments/cleanup`, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ delete: ids }),
            });
          }
        } catch (err) {
          // Una falla de limpieza no debe impedir eliminar la sesion, pero tampoco ser muda.
          appendMessage("error", `No se pudieron borrar los adjuntos del disco. ${friendlyErrorMessage(err)}`, { persist: false });
        }
      }
    }
  } catch {
    // Sin backend no hay adjuntos que consultar: se sigue con el borrado normal.
  }

  try {
    await apiFetch(`${API_BASE}/api/session/${sessionId}`, { method: "DELETE" });
  } catch (err) {
    console.warn("No se pudo eliminar la sesión en el backend:", err);
    // Se continua igual con la limpieza local -- mejor eliminarla del
    // lado del usuario aunque el backend no haya respondido, que dejarla
    // fantasma en la lista sin ninguna forma de sacarla.
  }

  removeLocalCopy(sessionId); // REQ-016: la copia chica que tenga (sesion sin carpeta)
  _sessionHasFolder.delete(sessionId);

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
//
// REQ-016/REQ-018 (spec fix-detener-y-progreso-visible, revision F2/F3): cada activacion de una sesion suma 1. Lo que espera algo de la
// red para pintar (la carga de la pagina, una activacion anterior) compara este numero antes y despues de cada espera: si cambio, el
// usuario ya fue a otra sesion -- o fue y volvio, y esa activacion nueva ya pinto todo -- y lo suyo no se dibuja. Antes solo se comparaba
// el id de la sesion: el turno de A se enganchaba (y se pintaba) en B, y ir y volver a A durante la carga pintaba su historial dos veces.
let _sessionActivations = 0;

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
  // Revision F2/F3: si mientras esta activacion espera a Vision el usuario activa otra sesion, esta deja de pintar (ver _sessionActivations).
  const activation = ++_sessionActivations;
  const superseded = () => activation !== _sessionActivations;
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
  // REQ-016 (spec fix-detener-y-progreso-visible): el mismo camino que usa F5 al abrir la pagina (fetchSessionHistory).
  const history = await fetchSessionHistory(sessionId);
  if (superseded()) return; // revision F2/F3: el usuario ya esta en otra sesion; pintar esto (o guardarlo como su copia) seria cruzarlas
  chatHistory.push(...history);
  persistChatHistory(); // solo escribe si la sesion no tiene carpeta (la copia chica, acotada)
  for (const entry of chatHistory) {
    appendMessage(entry.role, entry.text, { persist: false, messageId: entry.messageId, time: entry.time, responseType: entry.responseType || null, technical: entry.technical || null, errorKind: entry.errorKind || null, attachments: entry.attachments || null });
  }
  // REQ-018 (spec fix-detener-y-progreso-visible): la pregunta que esta sesion dejo esperando (llego estando el usuario en otra, o el
  // usuario se fue con ella en pantalla) aparece al volver. clearMessagesDom ya oculto la de la sesion anterior.
  showPendingQuestionFor(sessionId);

  // BUG real corregido (deteccion QA 2026-09-14): el panel "Tareas" solo se
  // actualizaba via eventos SSE "todo" en vivo -- al activar/volver a una
  // sesion no se re-consultaba su plan real, asi que si esa sesion procesaba
  // en background mientras el usuario estaba en OTRA (donde el evento se
  // ignoraba, ver el guard nuevo en handleEvent), al volver el panel
  // quedaba vacio/desactualizado aunque el backend SI tenia el plan guardado
  // (`GET /api/todos/{id}`, ya existente). Se re-sincroniza siempre al
  // activar cualquier sesion, sea que este ocupada o no.
  let todos = [];
  try {
    const todosResp = await apiFetch(`${API_BASE}/api/todos/${sessionId}`);
    const todosData = todosResp.ok ? await todosResp.json() : null;
    todos = todosData?.todos || [];
  } catch {
    todos = [];
  }
  if (superseded()) return;
  renderTodos(todos);

  // spec proyecto-por-sesion REQ-006: el Session ID se muestra INSTANTANEO
  // (no depende de red) -- la ruta de carpeta si necesita el fetch de
  // status de abajo, porque se resuelve del lado del backend.
  updateMetaSessionId(sessionId);

  // REQ-002 (spec gestion-sesiones): renderSessionsPanel() ya actualiza
  // el chip con el nombre real (updateMetaSessionName) -- no hace falta
  // pisarlo aca con las siglas del UUID.
  renderSessionsPanel();
  // REQ-017 (spec fix-detener-y-progreso-visible): el % de ESTA sesion, aunque el acordeon «Contexto» este cerrado (antes solo
  // se refrescaba abierto y el resumen quedaba en «—» o con el % de la sesion anterior).
  refreshContextUsage();

  // Si esta sesion sigue procesando un turno en background (ver fix del
  // bug: crear/cambiar de sesion ya no lo cancela), nos re-enganchamos al
  // stream SSE en curso en vez de dejar al usuario sin ver nada hasta que
  // "misteriosamente" aparezca la respuesta completa la proxima vez que
  // entre a esta sesion.
  try {
    const statusResp = await apiFetch(`${API_BASE}/api/session/${sessionId}/status`);
    const statusData = await statusResp.json();
    if (superseded()) return; // revision F2: el banner, el chip, el loop y el turno en curso de ESTA sesion no van en otra
    // REQ-006 (spec persistencia-sqlite-sesiones): el status ya trae
    // `persistence_available` -- se reusa esta misma consulta (que ya se
    // hace al activar cualquier sesion) en vez de agregar un fetch extra.
    updatePersistenceBanner(statusData.persistence_available);
    rememberSessionFolder(sessionId, statusData.persistence_available); // REQ-016: decide si le corresponde copia chica
    // spec proyecto-por-sesion REQ-006: idem para `project_path` -- ya
    // resuelto del lado del backend (carpeta propia, default, o raiz de
    // Vision), no se recalcula nada aca.
    paintSessionPathFromStatus(statusData);
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
        setConnStatus("busy", "ULTRON sigue pensando en esta sesión…");
        resetToolRow();
        const resp = await apiFetch(`${API_BASE}/api/chat/stream/${sessionId}`);
        // Revision F2: el stream es de ESTA sesion aunque el usuario cambie mientras llega (sin el tercer argumento quedaba de la activa).
        await renderIncomingStream(resp, statusData.active_turn_id, sessionId);
        if (connStatusEl.dataset.state === "busy") setConnStatus("online", "ULTRON en línea");
        touchCurrentSessionInIndex(sessionId);
      } finally {
        state.sending -= 1;
        if (state.sending < 0) state.sending = 0;
      }
        settleSendButtonAfterAttach(sessionId);
    }
  } catch {
    // Si falla la reconexion no es critico -- el usuario puede reintentar
    // simplemente reabriendo el acordeon de Sesion.
  }
}

// -------------------------------------------------------------------------
// REQ-016 (spec fix-detener-y-progreso-visible): al abrir la pagina (o con F5) el chat de la sesion activa se lee de SQLite por el
// MISMO camino que activateSession (`fetchSessionHistory`), no de la copia del navegador. Antes F5 pintaba SOLO esa copia: despues
// de «Liberar caché» la sesion se veia vacia aunque SQLite tenia todo, con el navegador lleno faltaba la ultima respuesta, y una
// copia con otra forma rompia la pagina entera. Es asincrono (espera al backend): lo que se haya pintado mientras tanto (la nota
// «Sesión iniciada», un mensaje que el usuario ya mando) queda DESPUES del historial, como antes.
// -------------------------------------------------------------------------
let _historyRestoreDone = false;

async function restoreActiveSessionOnLoad() {
  const loadSessionId = state.sessionId;
  const loadActivation = _sessionActivations;
  try {
    const history = await fetchSessionHistory(loadSessionId);
    // El usuario ya cambio de sesion mientras tanto: activateSession pinta la otra, esto ya no corresponde. Revision F3: tambien si fue
    // a otra y VOLVIO a esta (el id coincide, pero activateSession ya la pinto entera: insertarla otra vez la duplicaba).
    if (_sessionActivations !== loadActivation || state.sessionId !== loadSessionId || history.length === 0) return;
    const firstShownEl = Array.from(messagesEl.children).find((el) => !el.classList.contains("messages-watermark")) || null;
    const countBefore = messagesEl.children.length;
    for (const entry of history) {
      appendMessage(entry.role, entry.text, { persist: false, messageId: entry.messageId, time: entry.time, responseType: entry.responseType || null, technical: entry.technical || null, errorKind: entry.errorKind || null, attachments: entry.attachments || null });
    }
    if (firstShownEl) {
      for (const el of Array.from(messagesEl.children).slice(countBefore)) messagesEl.insertBefore(el, firstShownEl);
    }
    chatHistory.splice(0, 0, ...history);
    persistChatHistory(); // solo la sesion sin carpeta (copia chica): asi queda acotada desde la primera carga
    messagesEl.scrollTop = messagesEl.scrollHeight;
    showPendingQuestionFor(loadSessionId); // REQ-018: con F5, la pregunta sin contestar de esta sesion vuelve (dato de SQLite)
  } catch (err) {
    console.warn("No se pudo restaurar la conversación al abrir la página:", err);
  } finally {
    _historyRestoreDone = true;
  }
}

// REQ-016: migracion de UNA sola vez (marca LOCAL_COPY_MIGRATION_KEY) de lo que el navegador guardaba de mas:
//  - las copias `vision.session.<id>` de las sesiones CON carpeta cuyo chat YA esta en SQLite (el backend las lista en
//    /api/sessions/all con su `message_count`). REQ-016 (spec fix-detener-y-progreso-visible, revision F1): una sesion con carpeta de
//    la que SQLite no tiene ningun mensaje (charlo sin carpeta con la version anterior y despues la recibio) conserva su copia,
//    acotada como las de las sesiones sin carpeta: era lo unico que quedaba de esa charla (la misma regla que fetchSessionHistory);
//  - las copias huerfanas: sesion sin carpeta que tampoco esta en el indice de sesiones (nadie la puede abrir desde la lista);
//  - las claves `vision.pomodoro.*` de la funcion Pomodoro, eliminada (spec configuracion-limpieza-credenciales-servidor REQ-101).
// Las copias de sesiones sin carpeta que siguen en la lista quedan, acotadas como las nuevas. NUNCA toca preferencias, el indice
// de sesiones, la sesion abierta ni el token. Sin respuesta del backend no se sabe que sesiones tienen carpeta: no se borra nada y
// se reintenta en la proxima carga.
const LOCAL_COPY_MIGRATION_KEY = "vision.migrations.chatCopiesV1";
const REMOVED_FEATURE_PREFIXES = ["vision.pomodoro."];

async function migrateLocalCopiesOnce() {
  try {
    if (localStorage.getItem(LOCAL_COPY_MIGRATION_KEY)) return;
  } catch {
    return; // sin localStorage no hay copias que ordenar
  }
  let sqliteMessageCounts; // id de sesion con carpeta -> cuantos mensajes tiene en SQLite
  try {
    const resp = await apiFetch(`${API_BASE}/api/sessions/all`);
    if (!resp.ok) return;
    sqliteMessageCounts = new Map(((await resp.json()).sessions || []).map((s) => [s.id, Number(s.message_count) || 0]));
  } catch {
    return;
  }
  const index = loadSessionsIndex();
  let keys;
  try {
    keys = Object.keys(localStorage);
  } catch {
    return;
  }
  for (const key of keys) {
    try {
      if (REMOVED_FEATURE_PREFIXES.some((prefix) => key.startsWith(prefix))) {
        localStorage.removeItem(key);
        continue;
      }
      if (!key.startsWith(SESSION_COPY_PREFIX)) continue;
      const sessionId = key.slice(SESSION_COPY_PREFIX.length);
      const sqliteCount = sqliteMessageCounts.get(sessionId);
      if (sqliteCount !== undefined) _sessionHasFolder.set(sessionId, true);
      if (sqliteCount > 0) {
        localStorage.removeItem(key); // su chat esta en SQLite
      } else if (sqliteCount === undefined && !Object.hasOwn(index, sessionId) && sessionId !== state.sessionId) {
        localStorage.removeItem(key);
      } else {
        const history = parseLocalCopy(localStorage.getItem(key));
        if (history.length > 0) localStorage.setItem(key, boundedLocalCopy(history));
        else localStorage.removeItem(key);
      }
    } catch (err) {
      console.warn("No se pudo ordenar una copia local de conversación:", key, err);
    }
  }
  try {
    localStorage.setItem(LOCAL_COPY_MIGRATION_KEY, new Date().toISOString());
  } catch {
    // noop -- sin la marca, la migracion se repite en la proxima carga (es idempotente)
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
// REQ-017 (spec fix-detener-y-progreso-visible): se llama al cargar la pagina, al cambiar de sesion y al terminar cada turno, con
// el acordeon abierto o cerrado -- antes solo con el acordeon abierto, y el resumen cerrado quedaba en «—» mientras el medidor
// «Caché local» (ya quitado) mostraba 100 %: el usuario leia ese 100 % como el contexto del modelo (que estaba en 15 %).
// spec aviso-contexto-largo REQ-008: el chip de la barra superior. `level` lo calcula el backend (ok | warn | high | critical); sin dato queda
// en «—» y `unknown` (nunca con el % de otra sesion).
function paintContextChip(data) {
  if (!data) {
    contextChipEl.dataset.state = "unknown";
    contextChipValEl.textContent = "—";
    contextChipEl.title = "Contexto: no se pudo calcular";
    return;
  }
  contextChipEl.dataset.state = data.level || "ok";
  contextChipValEl.textContent = `${data.pct_used || 0}%`;
  contextChipEl.title =
    `Contexto: ${data.tokens.toLocaleString("es-AR")} / ${data.context_window.toLocaleString("es-AR")} tokens (estimado). ` +
    "Si se acerca al 100 %, usá /compact para resumirlo o /clear para borrar la conversación.";
}

// spec aviso-contexto-largo REQ-009: UNA nota por sesion al llegar a CONTEXT_WARN_PCT; se rearma solo al bajar de CONTEXT_REARM_PCT.
function maybeWarnLongContext(sessionId, pct) {
  if (pct < CONTEXT_REARM_PCT) {
    _contextWarnedSessions.delete(sessionId);
    return;
  }
  if (pct < CONTEXT_WARN_PCT || _contextWarnedSessions.has(sessionId)) return;
  _contextWarnedSessions.add(sessionId);
  appendMessage(
    "system-note",
    `Esta conversación está llegando al límite de lo que el modelo puede leer (${pct}% estimado). ` +
      "Escribí /compact para resumirla y seguir, o /clear para borrarla toda. Si no, pronto el modelo va a rechazarla por larga.",
    { persist: false }
  );
}

async function refreshContextUsage() {
  const sessionId = state.sessionId;
  try {
    // spec aviso-contexto-largo REQ-007: se mide contra la ventana del modelo ELEGIDO en el selector (nada en modo Automatico).
    const model = selectedModelForRequest();
    const query = model ? `?model=${encodeURIComponent(model)}` : "";
    const resp = await apiFetch(`${API_BASE}/api/context/${sessionId}${query}`);
    if (!resp.ok) throw await httpErrorFromResponse(resp);
    const data = await resp.json();
    if (sessionId !== state.sessionId) return; // respuesta tardia de la sesion anterior: no se pinta sobre la nueva
    const pct = data.pct_used || 0;
    contextSummaryEl.textContent = `${pct}%`;
    contextFillEl.style.width = `${pct}%`;
    contextFillEl.classList.toggle("warn", pct >= 60 && pct < 85);
    contextFillEl.classList.toggle("critical", pct >= 85);
    contextDetailEl.textContent = `${data.tokens.toLocaleString("es-AR")} / ${data.context_window.toLocaleString("es-AR")} tokens (estimado)`;
    paintContextChip(data);
    maybeWarnLongContext(sessionId, pct);
  } catch {
    if (sessionId !== state.sessionId) return;
    contextDetailEl.textContent = "No se pudo calcular el contexto.";
    // REQ-017 (spec fix-detener-y-progreso-visible, revision F5): sin dato de ESTA sesion, el resumen (lo unico que se ve con el
    // acordeon cerrado) y la barra no se quedan con el % de la sesion anterior.
    contextSummaryEl.textContent = "—";
    contextFillEl.style.width = "0%";
    contextFillEl.classList.remove("warn", "critical");
    paintContextChip(null);
  }
}

// -------------------------------------------------------------------------
// Conectores: estado online/error de Oracle/BigQuery/SharePoint/SAP/Graph
// (reemplazo funcional del panel "LSP" de OpenCode -- ver connectors_status.py)
// -------------------------------------------------------------------------
const CONNECTOR_LABELS = {
  oracle: "Oracle", bigquery: "BigQuery", sharepoint: "SharePoint", sap: "SAP", graph: "Graph",
};

// spec interruptores-conexiones-skills, decisión del usuario 2026-10-08: el panel lateral y Configuración > Conexiones muestran la MISMA prueba, y se vuelve a
// probar SOLO con el botón «Actualizar estado» (o la primera vez que hace falta). Antes cada lugar hacía su propia prueba en momentos distintos: el panel lateral
// quedaba con una vieja (ej. BigQuery en rojo porque su primera prueba venció) mientras Conexiones mostraba otra nueva en verde; y apagar un interruptor
// re-probaba todo y mandaba la fila al final de la lista. `_connSnapshot` = { connectors (estado de la última prueba), switches (interruptores), error }.
let _connSnapshot = null;
let _connCheckInFlight = null;

function connectorDisplayName(c) {
  // spec conexiones-con-nombre REQ-002: «Tipo - nombre» (ej. «SharePoint - Sitio Logistico»); BigQuery no tiene nombre y sigue como siempre
  return c.display || CONNECTOR_LABELS[c.name] || c.name;
}

// spec conexiones-office-m365-visibles: Outlook y Teams y Office NO son conexiones del gestor (sin campos ni credenciales: tienen su propio estado y su propio interruptor en el
// servidor), pero la persona las espera ver junto al resto, con un nombre que diga que se puede hacer. Se SUMAN a la foto como filas extra; los endpoints de conexiones no cambian.
function m365Capability(s) {
  const enabled = s.enabled !== false;
  if (!enabled) return { enabled, status: "off", detail: "Desactivada" };
  if (s.state === "connected") return { enabled, status: "online", detail: "Conectado" };
  // «waiting» (ambar fijo) y NO «checking»: `checking` es el «Verificando…» de la carga y la pantalla lo usa para saber que todavia no termino de probar
  if (s.state === "waiting") return { enabled, status: "waiting", detail: "Esperando que termines de entrar en el navegador" };
  if (s.state === "error") return { enabled, status: "error", detail: s.message || "No se pudo iniciar sesión. Probá de nuevo desde Conexiones." };
  return { enabled, status: "error", detail: "Sin sesión: iniciala en Conexiones > Conectores > Outlook y Teams" };
}

// Office SE PUEDE usar con Excel cerrado (solo falta abrir el libro): marcarlo en rojo seria mentir.
function officeCapability(o) {
  const enabled = o.enabled !== false;
  if (!enabled) return { enabled, status: "off", detail: "Desactivada" };
  const excel = o.excel || {};
  const word = o.word || {};
  const wordNote = word.state === "missing" && word.message ? ` ${word.message}` : "";
  if (excel.state === "running" || excel.state === "closed") return { enabled, status: "online", detail: `${excel.message || ""}${wordNote}`.trim() };
  if (excel.state === "busy") return { enabled, status: "waiting", detail: excel.message || "Excel está ocupado." };
  return { enabled, status: "error", detail: excel.message || word.message || "No pude saber si Office está disponible." };
}

const CAPABILITIES = [
  { key: "m365", display: "Outlook y Teams - Principal", statusPath: "/api/m365/status", switchPath: "/api/m365/switch", build: m365Capability },
  { key: "office", display: "Office - Principal", statusPath: "/api/office/status", switchPath: "/api/office/switch", build: officeCapability },
];

// Si una consulta falla, esa fila simplemente no aparece: la lista de siempre se ve completa (nunca se rompe por esto).
async function fetchCapabilities() {
  const out = [];
  await Promise.all(
    CAPABILITIES.map(async (cap) => {
      try {
        const resp = await apiFetchOk(`${API_BASE}${cap.statusPath}`);
        const data = await resp.json();
        // spec conexiones-office-m365-visibles REQ-009: el nombre lo pone la PERSONA (el servidor lo manda en `display`); `cap.display` es solo el respaldo si no llegara
        out.push({ ...cap.build(data), key: cap.key, name: cap.key, display: typeof data.display === "string" && data.display ? data.display : cap.display, switchPath: cap.switchPath });
      } catch { /* sin respuesta: sin fila */ }
    }),
  );
  return out;
}

// La persona renombro una de estas conexiones desde su ventana (vault.js avisa con un evento): se relee su estado y se repintan las dos listas SIN recargar ni re-probar las demas.
window.addEventListener("vision:capability-name-changed", () => {
  refreshCapabilities().catch(() => {}); // sin respuesta: queda la ultima foto
});

// Un interruptor de estas filas cambio en el servidor: se vuelve a leer SOLO su estado (no se re-prueban las demas conexiones) y se repintan las dos listas.
async function refreshCapabilities() {
  if (!_connSnapshot) return;
  _connSnapshot = { ..._connSnapshot, capabilities: await fetchCapabilities() };
  renderConnStatusList();
  renderConnectorsSidebar();
}

// La ÚNICA prueba real de las conexiones: pide el estado y los interruptores y repinta los dos lugares a la vez.
async function checkConnectionsNow() {
  if (_connCheckInFlight) return _connCheckInFlight;
  connectorsListEl.innerHTML = '<li><span class="connector-dot checking"></span>Verificando…</li>';
  const statusListEl = document.getElementById("conn-status-list");
  if (statusListEl) statusListEl.innerHTML = '<div class="conn-status-row"><span class="connector-dot checking"></span>Verificando…</div>';
  _connCheckInFlight = (async () => {
    try {
      const resp = await apiFetchOk(`${API_BASE}/api/connectors/status`);
      const connectors = (await resp.json()).connectors || [];
      let switches = [];
      try {
        switches = await fetchConnectionSwitches();
      } catch { /* sin la lista de interruptores se muestra el estado como antes */ }
      const capabilities = await fetchCapabilities();
      _connSnapshot = { connectors, switches, capabilities, error: null };
    } catch (err) {
      _connSnapshot = { connectors: [], switches: [], capabilities: [], error: err };
    } finally {
      _connCheckInFlight = null;
    }
    renderConnectorsSidebar();
    renderConnStatusList();
    if (!_connSnapshot.error) updateConnectionsSummary(_connSnapshot.switches);
  })();
  return _connCheckInFlight;
}

// Un interruptor cambió (se guardó en el servidor): se anota en la foto SIN volver a probar, así Conexiones dibuja el interruptor donde quedó.
function applySwitchesToConnSnapshot(switches) {
  if (!_connSnapshot) return;
  _connSnapshot = { ..._connSnapshot, switches, pending: true };
  renderConnStatusList();
}

// Orden estable: alfabético por nombre visible (el mismo de la lista de interruptores). Apagar o prender nunca mueve una fila.
function sortByConnectionName(items, nameOf) {
  return [...items].sort((a, b) => nameOf(a).localeCompare(nameOf(b), "es", { sensitivity: "base" }));
}

function renderConnectorsSidebar() {
  const snap = _connSnapshot;
  if (!snap) return;
  if (snap.error) {
    connectorsSummaryEl.textContent = "sin datos";
    renderListError(connectorsListEl, `No se pudo consultar el estado de las conexiones. ${friendlyErrorMessage(snap.error)}`, "");
    return;
  }
  const connectors = sortByConnectionName(snap.connectors, connectorDisplayName);
  const onlineCount = connectors.filter((c) => c.status === "online").length;
  connectorsSummaryEl.textContent = `${onlineCount}/${connectors.length}`; // «X/Y» = conexiones de DATOS: las capacidades de abajo no lo cambian
  connectorsListEl.innerHTML = "";
  const visible = sortByConnectionName([...connectors, ...(snap.capabilities || []).filter((cap) => cap.enabled)], connectorDisplayName);
  visible.forEach((c) => {
    const li = document.createElement("li");
    const dot = document.createElement("span");
    dot.className = `connector-dot ${c.status}`;
    const label = document.createElement("span");
    label.textContent = connectorDisplayName(c);
    li.append(dot, label);
    li.title = c.detail || c.status;
    connectorsListEl.appendChild(li);
  });
}

// Abrir el acordeón del panel lateral muestra la última prueba; solo si todavía no hubo ninguna, la hace.
async function refreshConnectorsStatus() {
  if (_connSnapshot) renderConnectorsSidebar();
  else await checkConnectionsNow();
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

// -- Spec fix-conflicto-modos-voz-simultaneos (REQ-001..005, opcion A) ----
// Bug real reportado por el usuario (2026-09-26): con Modo Cerebro activo,
// tocar "Hablar" lo dejaba sordo y ni Escape + reactivar lo recuperaba hasta
// recargar la pagina. Los 3 modos de voz abren el microfono cada uno por su
// cuenta, y la cancelacion de eco de Chromium es UNA sola por microfono
// fisico: un segundo stream con eco cancelado (Chrome lo aplica por defecto)
// le "roba" el procesamiento al primero, que queda recibiendo silencio sin
// error ni evento `ended`. Como "Hablar" es manual, quedaba grabando para
// siempre y cada reactivacion de Cerebro sufria lo mismo.
// Fix acordado con el usuario: UN modo de voz a la vez (bloquear el nuevo,
// nunca apagar solo el que ya escucha). Se chequea en el click de cada boton,
// ANTES de abrir el microfono, y por fuera de cada modo: no toca el VAD de
// Cerebro, la cadena de fragmentos de Reunion ni la limpieza al desenchufar.
const VOICE_MODE_LABELS = { chat: "el micrófono del chat", brain: "Modo Cerebro", meeting: "Modo reunión" };

function activeVoiceMode() {
  // voiceMicStarting: "Hablar" ya pidio el microfono pero todavia no marco
  // `listening` -- un click a otro modo en ese instante tambien se bloquea.
  if (voiceMicStarting || voiceMicBtnEl.dataset.listening === "true") return "chat";
  if (brainModeActive) return "brain";
  if (meetingModeActive) return "meeting";
  // spec control-microfono-windows: el aviso previo (leer si Windows lo silencio) tarda un instante ANTES de que el modo marque que esta activo;
  // en ese tramo el modo pedido primero ya ocupa el microfono (mismo criterio que voiceMicStarting).
  return pendingVoiceMode() || (meetingStartPending ? "meeting" : null);
}

// REQ-004: que modo esta escuchando y como apagarlo, sin jerga tecnica.
function voiceModeBusyMessage(active, requested) {
  const wanted = VOICE_MODE_LABELS[requested];
  // spec control-microfono-windows: Cerebro o Record todavia estan en su chequeo previo del microfono: aun no escuchan (va ANTES de los dos avisos de abajo).
  if ((active === "brain" && !brainModeActive) || (active === "meeting" && !meetingModeActive)) {
    return `${VOICE_MODE_LABELS[active]} se está preparando. Esperá un momento antes de usar ${wanted}.`;
  }
  if (active === "brain") return `Modo Cerebro está escuchando. Apagalo con Escape si querés usar ${wanted}.`;
  if (active === "meeting") return `Modo reunión está escuchando. Terminá la reunión (tocá Record o Escape) si querés usar ${wanted}.`;
  // Revision 2026-10-09: mientras "Hablar" todavia espera el microfono (p. ej. el permiso del navegador) NO graba:
  // "esta grabando, tocalo para terminar" seria falso y ese toque se ignora (voiceMicStarting).
  if (voiceMicBtnEl.dataset.listening !== "true") {
    return `El micrófono del chat se está encendiendo. Esperá a que empiece a grabar y tocalo de nuevo para terminar si querés usar ${wanted}.`;
  }
  return `El micrófono del chat está grabando. Tocalo de nuevo para terminar si querés usar ${wanted}.`;
}

// REQ-001/002/003: true si el modo pedido puede arrancar. Si otro modo esta
// escuchando, avisa y devuelve false SIN tocar ningun estado (REQ-005: no
// queda ninguna bandera trabada; apagado el otro modo, este arranca normal).
function canStartVoiceMode(requested) {
  const active = activeVoiceMode();
  if (!active || active === requested) return true;
  appendMessage("system-note", voiceModeBusyMessage(active, requested), { persist: false });
  return false;
}

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
    text = `${modeLabel}: perdí la conexión con ULTRON y no puedo pasar el audio a texto. ${OFFLINE_ERROR_MESSAGE}`;
  } else if (resp) {
    text = `${modeLabel}: ${(await httpErrorFromResponse(resp)).message}`;
  }
  appendMessage("system-note", text, { persist: false });
}

voiceMicBtnEl.addEventListener("click", () => {
  const listening = voiceMicBtnEl.dataset.listening === "true";
  if (listening) stopVoiceMicRecording();
  // spec control-microfono-windows REQ-005: aviso ANTES de grabar si el microfono esta silenciado en Windows. Solo el clic manual: el plug and play no pasa por aca.
  else if (canStartVoiceMode("chat")) startWithMicCheck("chat", startVoiceMicRecording); // fix-conflicto-modos-voz-simultaneos REQ-001
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
  fetchConnectionSwitches().then(updateConnectionsSummary).catch(() => { /* el resumen es decorativo: sin el, el boton sigue andando */ });
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
  savePreference(TTS_LOOP_KEY, _loopEnabled ? "true" : "false"); // REQ-016: si no se guarda, se avisa una vez
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
  return readPreference(LOOP_AUTO_PREF_KEY) === "true";
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
  savePreference(LOOP_AUTO_PREF_KEY, _loopAutoPref ? "true" : "false"); // REQ-016: si no se guarda, se avisa una vez
  syncQuickPanelLoopState();
});

// REQ-005: submenu in-place de skills locales -- reemplaza el CONTENIDO
// del mismo menu (nunca un modal aparte), lista solo skills LOCALES del
// proyecto de la sesion actual (getLocalSkillsList() ya existente, mismo
// endpoint que usa el menu "/"). Estado vacio explicito si el proyecto no
// tiene ninguna definida -- nunca una lista en blanco sin contexto ni un
// fallback silencioso a skills globales.
// spec interruptores-conexiones-skills (REQ-006/008/009): interruptores de conexiones y skills. El estado vive en el servidor (`~/.vision/vision.db`, global): la pantalla
// solo lo muestra y lo cambia, y SIEMPRE repinta con lo que devolvio el servidor (nunca asume que el cambio se guardo).
async function fetchConnectionSwitches() {
  const resp = await apiFetchOk(`${API_BASE}/api/switches/connections`);
  return (await resp.json()).connections || [];
}

async function postConnectionSwitch(key, enabled) {
  const resp = await apiFetchOk(`${API_BASE}/api/switches/connections`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ key, enabled }),
  });
  return (await resp.json()).connections || [];
}

// Interruptor accesible (role=switch). `onToggle(nextValue)` devuelve una promesa: si falla, el interruptor vuelve a su estado real y se avisa en pantalla.
function buildSwitch({ checked, disabled = false, label, onToggle }) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "sw-toggle";
  btn.setAttribute("role", "switch");
  btn.setAttribute("aria-label", label);
  btn.setAttribute("aria-checked", String(checked));
  btn.dataset.state = checked ? "on" : "off";
  btn.disabled = disabled;
  btn.innerHTML = '<span class="sw-toggle-knob"></span>';
  btn.addEventListener("click", async (event) => {
    // Precaucion, NO un arreglo de un bug actual: hoy cada interruptor es hermano (no hijo) del elemento que se puede clicar, asi que sin esta linea nada cambia (se probo
    // quitandola: el panel rapido sigue abierto y la skill no se ejecuta). Se deja por si un interruptor se mete algun dia DENTRO de un <li> expandible o de un boton.
    event.stopPropagation();
    if (btn.disabled) return;
    const next = btn.getAttribute("aria-checked") !== "true";
    btn.disabled = true;
    try {
      await onToggle(next);
    } catch (err) {
      alert(friendlyErrorMessage(err));
    } finally {
      btn.disabled = disabled;
    }
  });
  return btn;
}

function setSwitchVisual(btn, checked) {
  btn.setAttribute("aria-checked", String(checked));
  btn.dataset.state = checked ? "on" : "off";
}

const quickPanelConnectionsBtnEl = document.getElementById("quick-panel-connections-btn");
const quickPanelConnectionsViewEl = document.getElementById("quick-panel-connections-view");
const quickPanelConnectionsBackEl = document.getElementById("quick-panel-connections-back");
const quickPanelConnectionsListEl = document.getElementById("quick-panel-connections-list");
const quickPanelConnectionsSummaryEl = document.getElementById("quick-panel-connections-summary");

// El resumen del boton («3/5») se ve con el menu cerrado: cuantas conexiones hay activas sobre cuantas estan configuradas.
function updateConnectionsSummary(list) {
  if (!quickPanelConnectionsSummaryEl) return;
  quickPanelConnectionsSummaryEl.textContent = list.length ? `${list.filter((c) => c.enabled).length}/${list.length}` : "";
}

function renderConnectionSwitchRows(container, list) {
  container.innerHTML = "";
  if (!list.length) {
    const empty = document.createElement("div");
    empty.className = "quick-panel-skills-empty";
    empty.textContent = "No hay conexiones configuradas";
    container.appendChild(empty);
    return;
  }
  list.forEach((conn) => {
    const row = document.createElement("div");
    row.className = "sw-row";
    row.dataset.state = conn.enabled ? "on" : "off";
    const name = document.createElement("span");
    name.className = "sw-row-name";
    name.textContent = conn.display; // texto del servidor: textContent, nunca innerHTML
    const note = document.createElement("span");
    note.className = "sw-row-note";
    note.textContent = conn.env_disabled ? "desactivada en el .env" : conn.enabled ? "activa" : "desactivada";
    const toggle = buildSwitch({
      checked: conn.enabled,
      disabled: conn.env_disabled && !conn.enabled, // lo que el .env apaga no se puede prender desde aca
      label: `${conn.enabled ? "Desactivar" : "Activar"} ${conn.display}`,
      onToggle: async (next) => {
        const fresh = await postConnectionSwitch(conn.key, next);
        updateConnectionsSummary(fresh);
        renderConnectionSwitchRows(container, fresh);
        // Decisión del usuario (2026-10-08): el interruptor se GUARDA al instante, pero el panel lateral y Configuración > Conexiones no se vuelven a probar ni se
        // reordenan solos: «primero configuremos todo y luego presiono el botón Actualizar estado». Solo se sincroniza la posición del interruptor en Conexiones.
        applySwitchesToConnSnapshot(fresh);
      },
    });
    row.append(name, note, toggle);
    container.appendChild(row);
  });
}

async function showQuickPanelConnectionsView() {
  quickPanelMainViewEl.classList.add("hidden");
  quickPanelConnectionsViewEl.classList.remove("hidden");
  quickPanelConnectionsListEl.innerHTML = '<div class="quick-panel-skills-empty">Cargando…</div>';
  try {
    const list = await fetchConnectionSwitches();
    updateConnectionsSummary(list);
    renderConnectionSwitchRows(quickPanelConnectionsListEl, list);
  } catch (err) {
    renderListError(quickPanelConnectionsListEl, friendlyErrorMessage(err));
  }
}

quickPanelConnectionsBtnEl.addEventListener("click", () => showQuickPanelConnectionsView());
quickPanelConnectionsBackEl.addEventListener("click", () => showQuickPanelMainView());

function showQuickPanelMainView() {
  quickPanelSkillsViewEl.classList.add("hidden");
  // REQ-002 (spec quick-panel-labels-comandos): el submenu de Comandos es
  // una vista hermana de Skills -- volver al menu principal oculta ambas.
  quickPanelCommandsViewEl?.classList.add("hidden");
  quickPanelConnectionsViewEl?.classList.add("hidden");
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

  // spec interruptores-conexiones-skills REQ-009: estado individual de cada skill (global y local). Se pide una vez al abrir la vista.
  const skillState = new Map();
  try {
    const sResp = await apiFetch(`${API_BASE}/api/switches/skills?session_id=${encodeURIComponent(state.sessionId)}`);
    if (sResp.ok) (await sResp.json()).skills.forEach((s) => skillState.set(`${s.scope}:${s.name}`, s.enabled));
  } catch { /* sin el estado individual todas se muestran activas, como antes */ }

  const makeBtn = (name, scope) => {
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
    const row = document.createElement("div");
    row.className = "sw-row sw-row-skill";
    const enabled = skillState.get(`${scope}:${name}`) !== false; // sin dato = activa, como antes
    row.dataset.state = enabled ? "on" : "off";
    const toggle = buildSwitch({
      checked: enabled,
      label: `${enabled ? "Desactivar" : "Activar"} la skill ${name}`,
      onToggle: async (next) => {
        const query = scope === "local" ? `?session_id=${encodeURIComponent(state.sessionId)}` : "";
        const resp = await apiFetchOk(`${API_BASE}/api/switches/skills${query}`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name, enabled: next }),
        });
        const fresh = (await resp.json()).skills.find((s) => s.name === name && s.scope === scope);
        const now = fresh ? fresh.enabled : next; // lo que dice el SERVIDOR, no lo que se pidio
        skillState.set(`${scope}:${name}`, now);
        setSwitchVisual(toggle, now);
        row.dataset.state = now ? "on" : "off";
      },
    });
    row.append(it, toggle);
    return row;
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
    skills.forEach((s) => gList.appendChild(makeBtn(s.name, "global")));
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
    local.forEach((s) => lList.appendChild(makeBtn(s.name, "local")));
  }
}


quickPanelSkillsBtnEl.addEventListener("click", () => showQuickPanelSkillsView());
quickPanelSkillsBackEl.addEventListener("click", () => showQuickPanelMainView());

// -------------------------------------------------------------------------
// Submenu in-place de Comandos (spec quick-panel-labels-comandos).
// REQ-003: dos secciones ("Comandos del sistema" / "Comandos propios"),
// mismo endpoint que el menu "/" (getCommandsList). REQ-004: el click
// reusa renderCommandModal() -- el MISMO camino que el menu "/", sin
// duplicar la logica por `kind`. REQ-005: boton "Importar desde carpeta"
// que abre el Explorador nativo de Windows y copia los .md a commands/.
// -------------------------------------------------------------------------
const quickPanelCommandsBtnEl = document.getElementById("quick-panel-commands-btn");
const quickPanelCommandsViewEl = document.getElementById("quick-panel-commands-view");
const quickPanelCommandsBackEl = document.getElementById("quick-panel-commands-back");
const quickPanelCommandsListEl = document.getElementById("quick-panel-commands-list");

function _quickPanelSection(title) {
  const sec = document.createElement("div");
  sec.className = "quick-panel-skills-section";
  const head = document.createElement("div");
  head.className = "quick-panel-skills-section-title";
  const label = document.createElement("span");
  label.textContent = title;
  head.appendChild(label);
  sec.appendChild(head);
  const list = document.createElement("div");
  list.className = "quick-panel-skills-list";
  sec.appendChild(list);
  return { sec, head, list };
}

function _quickPanelCommandBtn(command) {
  const it = document.createElement("button");
  it.type = "button";
  it.className = "quick-panel-skill-item";
  const icon = command.kind === "custom" ? "✎" : "⚡";
  it.textContent = `${icon} /${command.name}`;
  it.title = command.description || (command.kind === "custom" ? "Comando propio" : "");
  it.onclick = () => {
    closeQuickPanelMenu();
    renderCommandModal(command); // REQ-004: mismo camino que el menu "/"
  };
  return it;
}

async function showQuickPanelCommandsView() {
  quickPanelMainViewEl.classList.add("hidden");
  quickPanelSkillsViewEl.classList.add("hidden");
  quickPanelCommandsViewEl.classList.remove("hidden");
  quickPanelCommandsListEl.innerHTML = "";

  const data = await getCommandsList();

  const sys = _quickPanelSection("Comandos del sistema");
  (data.system || []).forEach((c) => sys.list.appendChild(_quickPanelCommandBtn({ ...c, isCustom: false })));
  quickPanelCommandsListEl.appendChild(sys.sec);

  const own = _quickPanelSection("Comandos propios");
  const importBtn = document.createElement("button");
  importBtn.type = "button";
  importBtn.className = "msg-copy-btn";
  importBtn.textContent = "Importar…";
  importBtn.title = "Importar comandos desde una carpeta con archivos <nombre>.md";
  importBtn.onclick = (e) => {
    e.stopPropagation();
    importCommandsFromFolder();
  };
  own.head.appendChild(importBtn);
  const custom = data.custom || [];
  if (!custom.length) {
    const empty = document.createElement("div");
    empty.className = "quick-panel-skills-empty";
    empty.textContent = "Sin comandos propios en esta sesión";
    own.list.appendChild(empty);
  } else {
    custom.forEach((c) => own.list.appendChild(_quickPanelCommandBtn({ ...c, isCustom: true, kind: "custom" })));
  }
  quickPanelCommandsListEl.appendChild(own.sec);
}

/**
 * REQ-005: importa comandos desde una carpeta local. Flujo en 2 pasos:
 * el backend nunca sobrescribe en silencio -- devuelve `conflicts`, se
 * pide confirmacion y se reintenta con `overwrite`. Resultado resumido
 * como nota de sistema en el chat (una falla nunca es muda).
 */
async function importCommandsFromFolder() {
  const ok = await validateProjectFolderOrPrompt();
  if (!ok) return;
  // QA-031: antes se llamaba sin `statusEl` -- el catch de
  // openNativeFolderPicker SOLO escribe en statusEl, asi que si el
  // Explorador nativo fallaba (500, ya hay otro abierto, tkinter roto) el
  // click en "Importar…" no mostraba absolutamente nada ("una falla no
  // debe ser muda", regla del proyecto). No hay un statusEl visible en
  // este submenu (a diferencia del modal de sesion/skills), asi que se
  // pasa un objeto minimo que reusa esa misma rama de error para avisar
  // en el chat con friendlyErrorMessage, igual que el resto de las
  // acciones de este panel.
  const statusProxy = {
    _text: "",
    set textContent(v) {
      this._text = v;
      if (v) appendMessage("error", `No se pudo importar comandos. ${v}`, { persist: false });
    },
    get textContent() {
      return this._text;
    },
    className: "",
  };
  openNativeFolderPicker(
    "",
    async (sourcePath) => {
      const post = (overwrite) =>
        apiFetchOk(`${API_BASE}/api/commands/import-path`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ session_id: state.sessionId, source_path: sourcePath, overwrite }),
        }).then((r) => r.json());
      try {
        let result = await post([]);
        let imported = [...result.imported];
        if (result.conflicts.length) {
          const confirmOverwrite = window.confirm(
            `Ya existen estos comandos en la sesión:\n\n/${result.conflicts.join("\n/")}\n\n¿Querés reemplazarlos por los de la carpeta elegida?`
          );
          if (confirmOverwrite) {
            const second = await post(result.conflicts);
            imported = imported.concat(second.imported);
            result = { ...second, reserved: result.reserved, invalid: result.invalid };
          }
        }
        // REQ-005 d: resumen completo -- nombra tambien lo conservado sin
        // confirmar (antes solo se mencionaban importados/reservados/invalidos).
        const parts = [];
        parts.push(imported.length ? `Comandos importados: /${imported.join(", /")}.` : "No se importó ningún comando nuevo.");
        if (result.conflicts?.length) parts.push(`Se conservaron sin cambios (no confirmado): /${result.conflicts.join(", /")}.`);
        if (result.reserved?.length) parts.push(`Se omitieron por ser del sistema: /${result.reserved.join(", /")}.`);
        if (result.invalid?.length) parts.push(`Se omitieron por estar vacíos, ilegibles, con nombre inválido o demasiado grandes: ${result.invalid.join(", ")}.`);
        appendMessage("system-note", parts.join(" "), { persist: false });
        _commandsCache = null;
        if (!quickPanelCommandsViewEl.classList.contains("hidden")) showQuickPanelCommandsView();
      } catch (err) {
        appendMessage("error", `No se pudieron importar los comandos. ${friendlyErrorMessage(err)}`, { persist: false });
      }
    },
    statusProxy
  );
}

quickPanelCommandsBtnEl.addEventListener("click", () => showQuickPanelCommandsView());
quickPanelCommandsBackEl.addEventListener("click", () => showQuickPanelMainView());

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
  restoreBrowserGeometry(); // REQ-005: posicion/tamano guardados (ajustados a la ventana actual)
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
// Auto-revelado del panel "Navegador Integrado" (REQ-001/002, spec
// navegador-integrado-ventana). QA-034 CERRADO: la apertura automatica ya
// NO se decide reconsultando la huella del filesystem en cada 'done' (eso
// comparaba contra `_browserSeenFingerprint`, una variable en memoria que
// se reiniciaba en null con cada recarga de pestaña -- la primera pregunta
// de CUALQUIER tipo despues de recargar "descubria" una huella vieja como
// si fuera nueva). Ahora la apertura depende 100% del evento puntual
// 'browser-page-created' que el backend emite SOLO cuando el TURNO
// completo creo la pagina de entrada (ver casos "browser-page-created"/
// "browser-page-updated" en el switch de handleEvent, mas arriba). Siempre
// se muestra la URL copiable + boton ↗ para abrir en una pestana del
// navegador externo.
// -------------------------------------------------------------------------
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

// -------------------------------------------------------------------------
// REQ-003/004/005 (spec navegador-integrado-ventana): mover, redimensionar y
// recordar posicion/tamano del panel. Se usa Pointer Events + setPointerCapture
// (el arrastre sigue aunque el mouse salga del header) y el iframe recibe
// pointer-events:none via las clases .dragging/.resizing (sin eso el iframe se
// "come" el mouse y el gesto se corta). Maximizado no se mueve/redimensiona;
// minimizado (solo la barra) si se mueve pero no se redimensiona.
// -------------------------------------------------------------------------
const quickBrowserResetBtnEl = document.getElementById("quick-browser-reset-btn");
const quickBrowserIsolateBtnEl = document.getElementById("quick-browser-isolate-btn");
const quickBrowserResizeHandleEl = document.getElementById("quick-browser-resize-handle");
const BROWSER_GEOMETRY_KEY = "vision.browserPanel.geometry";
const BROWSER_ISOLATED_KEY = "vision.browserPanel.isolated";
const BROWSER_MIN_W = 320;
const BROWSER_MIN_H = 200;
const BROWSER_WINDOW_MARGIN = 16; // maximo = ventana menos 16 px por lado
const BROWSER_HEADER_VISIBLE = 48; // el header siempre asoma al menos 48 px

// Ajusta {left, top, width, height} a los limites de la ventana actual.
function clampBrowserGeometry(g) {
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  const maxW = Math.max(BROWSER_MIN_W, vw - BROWSER_WINDOW_MARGIN * 2);
  const maxH = Math.max(BROWSER_MIN_H, vh - BROWSER_WINDOW_MARGIN * 2);
  const width = Math.round(Math.min(Math.max(g.width, BROWSER_MIN_W), maxW));
  const height = Math.round(Math.min(Math.max(g.height, BROWSER_MIN_H), maxH));
  // Al menos 48 px del header dentro de la ventana, en ambos ejes.
  const left = Math.round(Math.min(Math.max(g.left, BROWSER_HEADER_VISIBLE - width), vw - BROWSER_HEADER_VISIBLE));
  const top = Math.round(Math.min(Math.max(g.top, 0), vh - BROWSER_HEADER_VISIBLE));
  return { left, top, width, height };
}

function applyBrowserGeometry(g) {
  const c = clampBrowserGeometry(g);
  const s = quickBrowserPanelEl.style;
  s.left = `${c.left}px`;
  s.top = `${c.top}px`;
  s.width = `${c.width}px`;
  s.height = `${c.height}px`;
  s.bottom = "auto";
  return c;
}

function currentBrowserGeometry() {
  const r = quickBrowserPanelEl.getBoundingClientRect();
  return { left: r.left, top: r.top, width: r.width, height: r.height };
}

function saveBrowserGeometry(g) {
  // REQ-016 (spec fix-detener-y-progreso-visible): sin localStorage el panel recuerda la posicion solo hasta recargar (y se avisa una vez).
  savePreference(BROWSER_GEOMETRY_KEY, JSON.stringify(g));
}

// Lee la geometria guardada; JSON corrupto o incompleto => null (default, sin error visible).
function loadBrowserGeometry() {
  try {
    const raw = readPreference(BROWSER_GEOMETRY_KEY);
    if (!raw) return null;
    const g = JSON.parse(raw);
    for (const k of ["left", "top", "width", "height"]) {
      if (!g || typeof g[k] !== "number" || !Number.isFinite(g[k])) return null;
    }
    return g;
  } catch {
    return null;
  }
}

function resetBrowserGeometry() {
  const s = quickBrowserPanelEl.style;
  s.left = s.top = s.width = s.height = s.bottom = "";
  forgetPreference(BROWSER_GEOMETRY_KEY);
}

// Al abrir: aplica la geometria guardada (ajustada a la ventana de hoy). Los
// estados maximizado/minimizado NO se guardan: siempre abre normal.
function restoreBrowserGeometry() {
  const g = loadBrowserGeometry();
  if (g) applyBrowserGeometry(g);
}

// Un gesto de arrastre/resize; `onMove` recibe el delta desde el inicio.
function startBrowserGesture(e, className, onMove) {
  e.preventDefault();
  const startX = e.clientX;
  const startY = e.clientY;
  const start = currentBrowserGeometry();
  const target = e.currentTarget;
  target.setPointerCapture(e.pointerId);
  quickBrowserPanelEl.classList.add(className);
  const move = (ev) => onMove(start, ev.clientX - startX, ev.clientY - startY);
  const end = (ev) => {
    target.removeEventListener("pointermove", move);
    target.removeEventListener("pointerup", end);
    target.removeEventListener("pointercancel", end);
    try {
      target.releasePointerCapture(ev.pointerId);
    } catch {
      // noop -- la captura ya se habia liberado.
    }
    quickBrowserPanelEl.classList.remove(className);
    saveBrowserGeometry(currentBrowserGeometry());
  };
  target.addEventListener("pointermove", move);
  target.addEventListener("pointerup", end);
  target.addEventListener("pointercancel", end);
}

const quickBrowserHeaderEl = quickBrowserPanelEl.querySelector(".quick-browser-header");
quickBrowserHeaderEl.addEventListener("pointerdown", (e) => {
  if (e.button !== 0) return;
  // Los botones del header (y la URL copiable) NO inician un arrastre.
  if (e.target.closest(".quick-browser-cntrl-btn, .quick-browser-url")) return;
  if (quickBrowserPanelEl.classList.contains("maximized")) return;
  startBrowserGesture(e, "dragging", (start, dx, dy) => {
    applyBrowserGeometry({ ...start, left: start.left + dx, top: start.top + dy });
  });
});

quickBrowserResizeHandleEl.addEventListener("pointerdown", (e) => {
  if (e.button !== 0) return;
  if (quickBrowserPanelEl.classList.contains("maximized") || quickBrowserPanelEl.classList.contains("minimized")) return;
  startBrowserGesture(e, "resizing", (start, dx, dy) => {
    applyBrowserGeometry({ ...start, width: start.width + dx, height: start.height + dy });
  });
});

// Reajusta tambien cuando cambia el tamano de la ventana del navegador.
window.addEventListener("resize", () => {
  if (quickBrowserPanelEl.classList.contains("hidden")) return;
  if (quickBrowserPanelEl.classList.contains("maximized")) return;
  if (quickBrowserPanelEl.style.left) applyBrowserGeometry(currentBrowserGeometry());
});

quickBrowserResetBtnEl.addEventListener("click", () => resetBrowserGeometry());

// -------------------------------------------------------------------------
// REQ-006 (decision del usuario 2026-09-28): aislamiento A PEDIDO. Por
// defecto el iframe comparte origen con Vision (los dashboards pueden hablar
// con su origen de datos); con el toggle se recarga con sandbox SIN
// allow-same-origin => origen opaco (sin localStorage ni API de Vision).
// -------------------------------------------------------------------------
const BROWSER_SANDBOX = "allow-scripts allow-forms allow-popups allow-modals";

function isBrowserIsolated() {
  return readPreference(BROWSER_ISOLATED_KEY) === "true";
}

function applyBrowserIsolation({ reload = true } = {}) {
  const isolated = isBrowserIsolated();
  if (isolated) quickBrowserIframeEl.setAttribute("sandbox", BROWSER_SANDBOX);
  else quickBrowserIframeEl.removeAttribute("sandbox");
  quickBrowserIsolateBtnEl.setAttribute("aria-pressed", String(isolated));
  // El atributo sandbox solo rige en la PROXIMA navegacion: se recarga el iframe.
  if (reload && !quickBrowserPanelEl.classList.contains("hidden")) {
    quickBrowserIframeEl.src = `${API_BASE}/browser/${encodeURIComponent(state.sessionId)}/?t=${Date.now()}`;
  }
}

quickBrowserIsolateBtnEl.addEventListener("click", () => {
  savePreference(BROWSER_ISOLATED_KEY, String(!isBrowserIsolated())); // REQ-016: si no se guarda, rige igual hasta recargar
  applyBrowserIsolation();
});
applyBrowserIsolation({ reload: false });

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
// spec audio-sistema-modo-reunion: de donde escucha ESTA reunion ("mic" = lo de siempre | "system" | "both") y el audio armado con la PC (null con "mic").
let meetingSource = "mic";
let meetingAudio = null;
let meetingSharedStream = null; // el flujo de getDisplayMedia pedido en el clic, hasta que startMeetingMode lo toma
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
  const activatorDetected = /\b(jarvis|ultr[oó]n)\b/i.test(fragmentText); // REQ-010 (portar-desde-origen): «Ultron» o «Jarvis»
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
    // vuelvo a decir Vision". `speechSynthesis.speaking` es el chequeo
    // nativo del navegador para saber si hay una utterance sonando ahora.
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
    console.warn("[ULTRON] click en el ojo ignorado: meetingModeActive es false");
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
        "ULTRON no pudo responder ahora (¿hay otro turno en curso?)",
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

async function startMeetingMode({ source = "mic", systemStream = null } = {}) {
  meetingModeActive = true;
  meetingSource = source;
  meetingFullTranscript = "";
  voiceRecordBtnEl.dataset.recording = "true";
  // REQ-002 (spec asistente-contextual-voz): se muestra el widget
  // flotante chico (ya no la pantalla completa) -- el "ojo" arranca en su
  // posicion por defecto (esquina inferior derecha, ver CSS) salvo que el
  // usuario ya lo haya arrastrado antes en esta misma sesion de pagina.
  voiceEyeWidgetEl.classList.remove("hidden");
  await initVoiceEye(voiceEyeContainerEl);
  setMeetingSourceBadge(); // REQ-010: el ojo dice de donde escucha

  if (source !== "mic") {
    await startMeetingWithSystemAudio(source, systemStream);
    return;
  }

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
        ? "Modo reunión: ULTRON no tiene permiso para usar el micrófono. Habilitalo desde el candado de la barra de direcciones; la reunión queda abierta."
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

// spec audio-sistema-modo-reunion REQ-010: el widget del ojo muestra de donde escucha la reunion.
function setMeetingSourceBadge() {
  const badgeEl = document.getElementById("voice-eye-source");
  if (badgeEl) badgeEl.textContent = meetingModeActive ? meetingSourceLabel(meetingSource) : "";
}

// spec audio-sistema-modo-reunion REQ-003/004: arranca la reunion con el audio de la PC ("system") o con el audio de la PC mezclado con el microfono ("both").
// Solo se llega aca con una fuente distinta de "mic": el camino de siempre (solo microfono) no pasa por esta funcion.
async function startMeetingWithSystemAudio(source, systemStream) {
  let micStream = null;
  if (source === "both") {
    try {
      micStream = await openMicStream(); // QA-007: el microfono elegido en Configuracion; sin microfono la reunion sigue con el audio de la PC
    } catch (err) {
      appendMessage(
        "system-note",
        isMicPermissionError(err)
          ? "Modo reunión: ULTRON no tiene permiso para usar el micrófono; escucha solo el audio de la PC. Habilitalo desde el candado de la barra de direcciones."
          : "Modo reunión: no se pudo acceder al micrófono; escucha solo el audio de la PC y suma tu voz apenas conectes uno.",
        { persist: false },
      );
    }
  }
  // El usuario pudo haber salido mientras el navegador pedia el microfono: no dejar nada abierto.
  if (!meetingModeActive) {
    micStream?.getTracks().forEach((track) => track.stop());
    systemStream?.getTracks().forEach((track) => track.stop());
    return;
  }
  meetingAudio = await buildMeetingAudio({ source, systemStream, micStream });
  meetingStream = meetingAudio.stream;
  // REQ-009: si la persona toca «Dejar de compartir» del navegador, la reunion SIGUE abierta y se avisa una sola vez.
  meetingAudio.systemTracks().forEach((track) => track.addEventListener("ended", onMeetingSystemAudioEnded, { once: true }));
  if (meetingAudio.micStream()) watchMicTrackEnd(meetingAudio.micStream(), reacquireMeetingMic);
  noticeMicFallbackOnce();
  startSpeechGuard();
  recordNextMeetingFragment();
}

// REQ-009: se corto el audio de la PC a mitad de la reunion. Con "both" sigue por el microfono; con "system" queda abierta esperando (se puede terminar con Escape).
function onMeetingSystemAudioEnded() {
  if (!meetingModeActive || !meetingAudio) return;
  if (meetingSource === "both" && meetingAudio.micStream()) {
    appendMessage("system-note", "Modo reunión: se dejó de compartir el audio de la PC. Sigue escuchando solo tu micrófono.", { persist: false });
  } else {
    appendMessage(
      "system-note",
      "Modo reunión: se dejó de compartir el audio de la PC y no hay otra fuente. La reunión sigue abierta: terminala con Escape y elegí «Guardar y salir» para el resumen.",
      { persist: false },
    );
  }
}

// spec audio-sistema-modo-reunion REQ-006: con el audio de la PC, la propia voz de Vision (TTS) puede volver a entrar y, si dice «Vision», dispararse a si misma en bucle.
// Los fragmentos que se solapan con la voz de Vision no se transcriben. Con "mic" (cancelacion de eco del microfono) no se descarta nada: comportamiento de siempre.
let meetingVisionSpokeInFragment = false;
let meetingSpeechGuardTimer = null;

function startSpeechGuard() {
  stopSpeechGuard();
  if (meetingSource === "mic") return;
  meetingSpeechGuardTimer = setInterval(() => {
    if (isSpeaking()) meetingVisionSpokeInFragment = true;
  }, 250);
}

function stopSpeechGuard() {
  clearInterval(meetingSpeechGuardTimer);
  meetingSpeechGuardTimer = null;
  meetingVisionSpokeInFragment = false;
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
  meetingVisionSpokeInFragment = meetingSource !== "mic" && isSpeaking(); // REQ-006: arranca el fragmento ya con Vision hablando?
  meetingRecorder = new MediaRecorder(meetingStream);
  meetingRecorder.ondataavailable = (e) => chunks.push(e.data);
  meetingRecorder.onstop = () => {
    const blob = new Blob(chunks, { type: "audio/webm" });
    // REQ-006: con el audio de la PC, un fragmento que se solapo con la voz de Vision NO se transcribe (evita el bucle de que Vision se escuche a si misma).
    if (meetingSource !== "mic" && meetingVisionSpokeInFragment) {
      meetingVisionSpokeInFragment = false;
      if (meetingModeActive) recordNextMeetingFragment();
      return;
    }
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
  // spec audio-sistema-modo-reunion: con "solo lo que escucho" no se usa el microfono: nada que reabrir. Con "ambos" el flujo mezclado NO cambia; solo se cambia el microfono de adentro.
  if (meetingSource === "system") return;
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
    if (meetingSource === "both" && meetingAudio) {
      // El flujo mezclado es el MISMO: el grabador sigue sobre el, sin cortar el fragmento en curso ni perder el audio de la PC.
      meetingAudio.swapMic(stream);
      watchMicTrackEnd(stream, reacquireMeetingMic);
      noticeMicFallbackOnce();
      appendMessage("system-note", `Modo reunión: escuchando por «${micLabel(stream)}» y el audio de la PC.`, { persist: false });
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
  // spec audio-sistema-modo-reunion REQ-008: tambien el microfono de adentro de la mezcla, el audio de la PC y el contexto de audio.
  if (meetingAudio) {
    meetingAudio.stop();
    meetingAudio = null;
  }
  meetingSource = "mic";
  stopSpeechGuard();
  setMeetingSourceBadge();
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

// spec audio-sistema-modo-reunion REQ-001/013/014: elegir la fuente y recien despues el aviso de microfono silenciado de la 0.30.0 (solo si la fuente usa el microfono).
let meetingStartPending = false; // mientras dura la ventana de fuente, otro modo de voz no puede arrancar encima (REQ-014)

async function startMeetingFromClick() {
  if (meetingStartPending) return;
  meetingStartPending = true;
  try {
    const choice = await requestMeetingSource();
    if (!choice) return; // canceló: no arranca nada
    if (!canStartVoiceMode("meeting")) {
      choice.systemStream?.getTracks().forEach((track) => track.stop()); // otro modo tomo el microfono mientras elegia
      return;
    }
    if (choice.source === "system") {
      startMeetingMode(choice); // REQ-013: no usa el microfono, asi que no se revisa si esta silenciado
    } else {
      const result = await startWithMicCheck("meeting", () => startMeetingMode(choice));
      if (result !== "continue") choice.systemStream?.getTracks().forEach((track) => track.stop()); // cerró el aviso: no dejar el audio de la PC abierto
    }
  } finally {
    meetingStartPending = false;
  }
}

voiceRecordBtnEl.addEventListener("click", () => {
  if (!meetingModeActive) {
    // spec audio-sistema-modo-reunion REQ-001: primero se elige de donde escucha (el audio de la PC se pide AQUI, dentro del clic). Con «Solo lo que hablo» sigue el camino de siempre.
    if (canStartVoiceMode("meeting")) startMeetingFromClick();
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
// spec cerebro-espacio-turnos: Espacio con el modo ESCUCHANDO cierra la frase en curso y la envia cuando su transcripcion termina; Espacio PROCESANDO deja el modo en escucha (verde) hasta el proximo envio.
let brainSendWhenReady = false;
let brainManualListen = false;
// spec cerebro-hablar-con-espacio-y-audio-agrupado: modo «Mantener Espacio para hablar». `brainPttHeld` = la barra esta apretada y se esta grabando; `brainPttIgnoreKeyup` = la pulsacion que ENCENDIO el modo (su
// soltada no envia nada); `brainPttHintIfEmpty` = al soltar sin haber dicho nada se avisa en el cartel; `brainPttBegin` = el `beginUtterance` de startBrainMode (vive en su cierre) para grabar desde el teclado.
let brainPttHeld = false;
let brainPttIgnoreKeyup = false;
let brainPttHintIfEmpty = false;
let brainPttBegin = null;
// Incidente real 2026-10-03 (v0.23.1): con Whisper devolviendo 500 y reintentando, un fragmento tardaba ~1 minuto y, como el autoenvio
// esperaba a que NO hubiera ninguna transcripcion en vuelo, el texto ya transcripto no se enviaba (el modo quedaba amarillo sin
// contestar). Se guarda cuando empezo cada una para dejar de esperar a las que llevan demasiado.
const brainTranscriptionsInFlight = new Set();
const BRAIN_TRANSCRIPTION_WAIT_MS = 10_000;
let brainAudioCtx = null;
let brainAnalyser = null;
let brainVadRafId = null;
let brainSpeechActive = false; // true mientras el VAD detecta energia por encima del piso de silencio
// spec fix-cerebro-escucha-y-microfono REQ-003: ultima vez que Vision hablo. Antes era local de startBrainMode y arrancaba en 0 en cada
// encendido o reinicio: si el Cerebro arrancaba en un hueco entre dos partes de lo que Vision dice, no habia enfriamiento y grababa en el
// primer frame (medido en un e2e: a los 26 ms, con Vision volviendo a hablar a los 200 ms). Cada fin de voz -- tambien el corte por
// espacio o ⏹ -- lo actualiza.
let brainLastSpeakingAt = 0;
onSpeechEnd(() => {
  brainLastSpeakingAt = performance.now();
});
let brainSilenceStartedAt = 0; // timestamp (performance.now()) de cuando empezo el silencio actual
// spec autoenvio-cerebro-sin-voz REQ-001: ultima vez que el detector OYO voz real (performance.now()). Los N segundos del
// autoenvio se cuentan desde aca, no desde que termino la transcripcion de la frase anterior.
let brainLastVoiceAt = 0;

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
// spec cerebro-hablar-con-espacio-y-audio-agrupado REQ-009: el umbral deja de ser unico. 4 niveles con palabras; «normal» ES el de siempre (0,012), asi que quien no toque nada no nota ningun cambio.
const BRAIN_SENSITIVITY_KEY = "vision.brain.sensitivity";
const BRAIN_SENSITIVITY_LEVELS = { very_noisy: 0.05, noisy: 0.028, normal: BRAIN_SILENCE_THRESHOLD, quiet: 0.007 };
const BRAIN_SENSITIVITY_DEFAULT = "normal";
function getBrainSensitivity() {
  const raw = readPreference(BRAIN_SENSITIVITY_KEY);
  // Un valor desconocido o corrupto (otro build, edicion a mano) vale «normal»: nunca un umbral absurdo que deje al modo sordo o loco.
  return Object.prototype.hasOwnProperty.call(BRAIN_SENSITIVITY_LEVELS, raw) ? raw : BRAIN_SENSITIVITY_DEFAULT;
}
function brainSilenceThreshold() {
  return BRAIN_SENSITIVITY_LEVELS[getBrainSensitivity()]; // se lee en cada cuadro del detector: el cambio se aplica EN VIVO, sin reiniciar el modo
}
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
  renderBrainBanner(state);
}

// spec cerebro-cartel-estado: el cartel dice COMO APAGAR el modo (SOLO con Esc: tocar el icono con el modo activo NO lo apaga, decision del usuario) y en que estado esta. Se pinta desde el MISMO `state` que el boton (una sola fuente de verdad:
// Escape o un error de microfono terminan en `stopBrainMode()` -> `setBrainState("idle")` y lo ocultan sin codigo aparte). Dos colores, como se pidio: verde = escucha,
// ambar = procesa. El hueco en que el BOTON es rojo (activo y en silencio, o Vision hablando) sigue siendo «escuchando» para el cartel: rojo asustaria y ocultarlo dejaria sin aviso justo cuando
// mas hace falta saber como apagarlo. Texto fijo (nada del servidor), con `<kbd>` armado por DOM, nunca innerHTML.
const brainBannerEl = document.getElementById("brain-banner");
const brainBannerTextEl = document.getElementById("brain-banner-text");
function renderBrainBanner(state) {
  if (!brainBannerEl) return; // compatibilidad con un index.html viejo en cache: sin cartel, el modo sigue andando
  if (state === "idle") {
    brainBannerEl.classList.add("hidden");
    brainBannerEl.dataset.state = "idle";
    return;
  }
  // Para el cartel «hablando» (Vision responde por voz) manda `isSpeaking()` y NO el color del boton (que ahi es rojo): es lo mismo que mira Espacio para decidir si interrumpe.
  // spec cerebro-hablar-con-espacio-y-audio-agrupado REQ-008: en modo «Mantener Espacio» el estado sale del TECLADO (apretada = escuchando) y entre pulsaciones es «paused» (gris, sordo). Prioridad: hablando > apretada > procesando > pausa.
  const push = isBrainPushMode();
  const shown = isSpeaking() ? "speaking"
    : push ? (brainPttHeld ? "listening" : state === "processing" ? "processing" : "paused")
    : state === "processing" ? "processing" : "listening";
  brainBannerEl.classList.remove("hidden");
  const hint = performance.now() < brainBannerHintUntil ? BRAIN_BANNER_HINT : "";
  const mode = push ? "push" : "auto";
  if (brainBannerEl.dataset.state === shown && brainBannerTextEl.dataset.hint === hint && brainBannerTextEl.dataset.mode === mode && brainBannerTextEl.childNodes.length) return; // sin repintar cada 250 ms: un lector de pantalla no se re-anuncia
  brainBannerEl.dataset.state = shown;
  brainBannerTextEl.dataset.hint = hint;
  brainBannerTextEl.dataset.mode = mode;
  if (push) {
    const [pushLead, verb, action] = BRAIN_BANNER_PUSH[shown];
    brainBannerTextEl.replaceChildren(
      document.createTextNode(`${hint || pushLead} ${verb} `),
      (() => { const key = document.createElement("kbd"); key.textContent = "Espacio"; return key; })(),
      document.createTextNode(`${action} · `),
      (() => { const key = document.createElement("kbd"); key.textContent = "Esc"; return key; })(),
      document.createTextNode(" para apagarlo."),
    );
    return;
  }
  const kbd = (label) => {
    const key = document.createElement("kbd");
    key.textContent = label;
    return key;
  };
  const lead = hint || BRAIN_BANNER_LEAD[shown];
  brainBannerTextEl.replaceChildren(
    document.createTextNode(`${lead} `),
    kbd("Espacio"),
    document.createTextNode(`${BRAIN_BANNER_SPACE[shown]} · `),
    kbd("Esc"),
    document.createTextNode(" para apagarlo."),
  );
}
// Que hace cada tecla en cada estado (spec cerebro-espacio-turnos). Texto fijo del front.
const BRAIN_BANNER_LEAD = {
  listening: "Modo Cerebro: te estoy escuchando.",
  processing: "Modo Cerebro: procesando…",
  speaking: "Modo Cerebro: ULTRON está respondiendo.",
};
const BRAIN_BANNER_SPACE = {
  listening: " para enviar lo que dijiste",
  processing: " para seguir hablando",
  speaking: " para interrumpir y hablar",
};
const BRAIN_BANNER_HINT = "Todavía no dijiste nada.";
// Modo «Mantener Espacio para hablar»: [frase, verbo antes de la tecla, lo que hace la tecla]. Cuatro estados; «paused» (gris) = sordo entre pulsaciones.
const BRAIN_BANNER_PUSH = {
  paused: ["Modo Cerebro: en pausa, no te escucho.", "Mantené", " para hablar"],
  listening: ["Modo Cerebro: te estoy escuchando.", "Soltá", " para enviar lo que dijiste"],
  processing: ["Modo Cerebro: procesando…", "Mantené", " para seguir hablando"],
  speaking: ["Modo Cerebro: ULTRON está respondiendo.", "Mantené", " para interrumpir y hablar"],
};
let brainBannerHintUntil = 0; // aviso breve (2 s) cuando se presiona Espacio sin haber dicho nada: no envia un mensaje vacio

// REQ-013: refleja en vivo, cada 250ms, en que estado visual cae el modo
// -- prioridad: procesando (esperando Whisper/LLM) > escuchando activo
// (VAD detecto voz ahora mismo) > resto (rojo: en silencio esperando, o
// TTS hablando la respuesta).
function tickBrainState() {
  if (!brainModeActive) return;
  trackBrainSilence(); // spec fix-cerebro-escucha-y-microfono: cuanto lleva el microfono entregando ceros exactos
  // spec fix-cerebro-escucha-y-microfono REQ-006: tambien con un turno escrito a mano o de un agente (state.sending), no solo los del Cerebro.
  if (isBrainPushMode()) {
    // spec cerebro-hablar-con-espacio-y-audio-agrupado: el estado sale del teclado, no del detector. Apretada = escuchando (verde); soltada y enviada = procesando (ambar); el resto = en pausa (el boton se ve rojo, como «activo y callado»).
    if (brainPttHeld) setBrainState("listening");
    else if (brainSending || state.sending > 0 || brainTranscriptionsPending > 0) setBrainState("processing");
    else setBrainState("idle-active");
    return;
  }
  if (brainManualListen && isSpeaking()) brainManualListen = false; // Vision empezo a responder: la escucha manual terminó
  if (brainManualListen) {
    setBrainState("listening"); // verde -- la persona presiono Espacio mientras se procesaba: el modo vuelve a escucharla (spec cerebro-espacio-turnos)
  } else if (brainSending || state.sending > 0 || brainTranscriptionsPending > 0) {
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

// spec autoenvio-cerebro-sin-voz REQ-003: el autoenvio solo puede disparar con el usuario en silencio. Hay voz si el detector la oye
// ahora, si hay una frase grabandose (`brainRecorder`, incluye la confirmacion de silencio de 600 ms) o si hay una transcripcion en
// vuelo (su texto todavia no esta en `brainPendingText`, asi que enviar ya seria enviar cortado).
// Una transcripcion en vuelo hace esperar al envio SOLO durante BRAIN_TRANSCRIPTION_WAIT_MS: pasado ese plazo se envia lo que ya
// esta transcripto y el fragmento que llegue tarde se agrega al mensaje siguiente (no se pierde).
function brainHasFreshTranscription() {
  const now = performance.now();
  for (const startedAt of brainTranscriptionsInFlight) {
    if (now - startedAt < BRAIN_TRANSCRIPTION_WAIT_MS) return true;
  }
  return false;
}

function brainUserMayStillBeTalking() {
  return brainSpeechActive || brainRecorder !== null || brainHasFreshTranscription();
}

// REQ-001/003/004: UNICO punto que arma el autoenvio. Se llama al terminar CUALQUIER frase (con texto, vacia, descartada por
// energia, alucinacion o error de red) y al terminar un turno de Vision; asi, si la voz cancelo la cuenta y esa segunda frase no
// aporto texto, el texto anterior no queda varado. Cuenta N segundos desde la ULTIMA voz real (`brainLastVoiceAt`).
function armBrainAutoSendIfIdle() {
  if (isBrainPushMode()) return; // spec cerebro-hablar-con-espacio-y-audio-agrupado: con «Mantener Espacio» solo se envia al soltar (o al terminar el turno en curso); nunca por silencio
  if (!brainModeActive || !isBrainAutosendEnabled() || !brainPendingText.trim()) return;
  if (brainUserMayStillBeTalking() || brainSending) return; // se vuelve a llamar cuando cambie ese estado
  const waitMs = getBrainAutosendPauseMs() - (performance.now() - brainLastVoiceAt);
  clearTimeout(brainAutoSendTimer);
  brainAutoSendTimer = setTimeout(() => {
    // Se vuelve a mirar al vencer: puede haber empezado otra frase entre que se armo la cuenta y ahora.
    if (brainUserMayStillBeTalking()) return;
    sendBrainIntervention();
  }, Math.max(0, waitMs));
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
  brainManualListen = false; // spec cerebro-espacio-turnos: al salir un mensaje, el modo pasa a «procesando»
  brainSending = true;
  try {
    await sendMessage(text, { voiceConfirm: true });
  } finally {
    brainSending = false;
    // Barge-in/turno hablado mientras el anterior estaba en curso: queda
    // acumulado arriba y se despacha ahora, sin perderlo ni solapar dos
    // streams de la misma conversacion.
    if (brainModeActive && brainPendingText.trim()) {
      // Con autoenvio, respeta la voz (REQ-004). Apagado: texto en cola de un turno anterior, como siempre.
      if (isBrainAutosendEnabled() && !isBrainPushMode()) armBrainAutoSendIfIdle(); // en modo «Mantener Espacio» ya soltaste: sale ahora
      else scheduleBrainAutoSend(0);
    }
  }
}

// Se llama apenas el VAD confirma que el usuario dejo de hablar (o se
// llego al limite duro de duracion) -- YA con el audio de esa "frase"
// completo, arranca la transcripcion de inmediato (sin esperar ningun
// timer adicional de fragmento fijo, a diferencia de Record).
async function processBrainUtterance(blob) {
  // REQ-004: sea cual sea el final de la frase (texto, vacio, descartada, error), se re-evalua el autoenvio.
  try {
    await transcribeBrainUtterance(blob);
  } finally {
    armBrainAutoSendIfIdle();
    flushBrainSendRequest(); // spec cerebro-espacio-turnos: la persona pidio enviar con Espacio mientras esta frase se transcribia
  }
}

// spec cerebro-espacio-turnos: Espacio con el modo escuchando. Cierra la frase en curso (el texto recien existe cuando Whisper termina) y envia TODO lo acumulado como UN mensaje cuando no queda ninguna
// transcripcion en vuelo. Sin nada dicho no envia (nunca un mensaje vacio) y avisa en el cartel. Pasado el plazo de siempre (`BRAIN_TRANSCRIPTION_WAIT_MS`) se envia lo ya transcripto y lo tardio va al siguiente.
function flushBrainSendRequest() {
  if (!brainSendWhenReady || !brainModeActive) return;
  if (brainRecorder !== null || brainTranscriptionsPending > 0) return; // todavia hay una frase grabandose o transcribiendose
  brainSendWhenReady = false;
  if (brainPttHintIfEmpty) {
    // spec cerebro-hablar-con-espacio-y-audio-agrupado: soltar sin haber dicho nada no envia nada y lo avisa (2 s), igual que Espacio sin nada dicho.
    brainPttHintIfEmpty = false;
    if (!brainPendingText.trim()) {
      brainBannerHintUntil = performance.now() + 2000;
      renderBrainBanner(brainBtnEl.dataset.state);
      setTimeout(() => renderBrainBanner(brainBtnEl.dataset.state), 2100);
    }
  }
  sendBrainIntervention();
}

function requestBrainSend() {
  brainManualListen = false;
  if (brainRecorder && brainRecorder.state !== "inactive") {
    brainSendWhenReady = true;
    brainRecorder.stop(); // misma accion que endUtterance(); su onstop transcribe y, al terminar, flushBrainSendRequest() envia
  } else if (brainTranscriptionsPending > 0) {
    brainSendWhenReady = true;
  } else if (brainPendingText.trim()) {
    clearTimeout(brainAutoSendTimer);
    sendBrainIntervention();
    return;
  } else {
    brainBannerHintUntil = performance.now() + 2000; // nada dicho: no se envia, se avisa (2 s)
    renderBrainBanner(brainBtnEl.dataset.state);
    setTimeout(() => renderBrainBanner(brainBtnEl.dataset.state), 2100);
    return;
  }
  // Si Whisper se cuelga, no se queda esperando para siempre: pasado el plazo se envia lo que ya esta transcripto.
  setTimeout(() => {
    if (!brainSendWhenReady) return;
    brainSendWhenReady = false;
    sendBrainIntervention();
  }, BRAIN_TRANSCRIPTION_WAIT_MS + 100);
}

async function transcribeBrainUtterance(blob) {
  brainTranscriptionsPending += 1;
  const startedAt = performance.now();
  brainTranscriptionsInFlight.add(startedAt);
  // Si esta transcripcion se cuelga, nada mas volveria a evaluar el envio: se agenda al cumplirse el plazo.
  setTimeout(armBrainAutoSendIfIdle, BRAIN_TRANSCRIPTION_WAIT_MS + 50);
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
    brainTranscriptionsInFlight.delete(startedAt);
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
    // El envio lo decide `armBrainAutoSendIfIdle` (la llama processBrainUtterance al terminar): cuenta N segundos desde la ultima
    // voz y no dispara mientras el usuario siga hablando. Antes se armaba aca con el reloj del fin de la frase anterior.
    return;
  }

  // REQ-005: auto-envio desactivado -- se necesita decir "Vision"
  // (case-insensitive) para forzar el envio de lo dicho hasta el momento.
  if (/\bultr[oó]n\b/i.test(fragmentText)) { // REQ-010 (portar-desde-origen): ULTRON se llama «Ultron»
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
    brainMicOpening += 1; // spec fix-cerebro-escucha-y-microfono: la verificacion de salud no reabre encima de un pedido en vuelo
    let stream;
    try {
      stream = await openMicStream({ echoCancellation: true, noiseSuppression: true, autoGainControl: true });
    } finally {
      brainMicOpening -= 1;
    }
    // Escape pudo apagar el modo mientras el navegador pedia el microfono:
    // no dejarlo abierto (y escuchando) sin ningun modo que lo use.
    if (!brainModeActive) {
      stream.getTracks().forEach((track) => track.stop());
      return;
    }
    brainStream = stream;
    brainMicLastError = null;
    brainMicMissingNoticeShown = false;
    brainZeroSince = 0;
  } catch (err) {
    brainMicLastError = err;
    // Sin microfono real -- el modo queda "activo" visualmente (el icono
    // ya cambio de estado) pero sin audio real no hay nada que transcribir.
    // QA-008: ya no en silencio -- se avisa, y `refreshActiveMicStreams`
    // lo vuelve a arrancar solo en cuanto se conecte un microfono.
    if (notifyMicError) {
      appendMessage(
        "system-note",
        isMicPermissionError(err)
          ? "Modo Cerebro: ULTRON no tiene permiso para usar el micrófono. Habilitalo desde el candado de la barra de direcciones; el modo sigue encendido."
          : "Modo Cerebro: no se pudo acceder al micrófono. Sigue encendido y empieza a escuchar apenas conectes uno.",
        { persist: false },
      );
    }
    return;
  }
  noticeMicFallbackOnce();
  watchMicTrackEnd(brainStream, restartBrainModeForMic);
  // spec fix-cerebro-escucha-y-microfono REQ-004: al terminar un silencio de la pista (palanca del auricular, tecla de microfono) se
  // verifica enseguida si la captura revivio; si sigue entregando ceros exactos, se reabre.
  brainStream.getAudioTracks().forEach((track) => {
    track.addEventListener("unmute", () => setTimeout(() => brainMicHealthCheck({ force: true }), 1500));
  });

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
  // brainLastSpeakingAt vive a nivel de modulo (spec fix-cerebro-escucha-y-microfono REQ-003).

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
      processBrainUtterance(blob);
    };
    brainRecorder.start();
    utteranceStartedAt = performance.now();
  }

  function endUtterance() {
    if (brainRecorder && brainRecorder.state !== "inactive") {
      brainRecorder.stop();
    }
  }
  brainPttBegin = beginUtterance; // spec cerebro-hablar-con-espacio-y-audio-agrupado: el teclado graba con la MISMA funcion que el detector

  function vadTick() {
    if (!brainModeActive || !brainAnalyser) return;
    // spec cerebro-hablar-con-espacio-y-audio-agrupado REQ-002: con «Mantener Espacio» el detector queda FUERA: no arranca ni corta frases (lo hace el teclado), asi que el ruido ambiente no importa. Solo dibuja las ondas mientras se habla.
    if (isBrainPushMode()) {
      brainSpeechActive = false;
      let pushLevel = 0;
      if (brainPttHeld) {
        brainAnalyser.getByteTimeDomainData(vadData);
        let pushSum = 0;
        for (let i = 0; i < vadData.length; i++) {
          const sample = (vadData[i] - 128) / 128;
          pushSum += sample * sample;
        }
        pushLevel = Math.sqrt(pushSum / vadData.length);
      }
      updateBrainWaveform(pushLevel);
      brainVadRafId = requestAnimationFrame(vadTick);
      return;
    }
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
    // spec voz-fish-audio REQ-010: con un motor de voz del backend el audio sale por
    // Web Audio de esta pestaña; se pausa el microfono igual que con la voz del navegador.
    const nowSpeaking = isSpeaking();
    if (nowSpeaking) brainLastSpeakingAt = performance.now();
    if (nowSpeaking || performance.now() - brainLastSpeakingAt < BRAIN_TTS_COOLDOWN_MS) {
      // spec fix-cerebro-escucha-y-microfono REQ-002: una frase que el usuario venia diciendo se CIERRA apenas Vision empieza a hablar
      // (se manda lo suyo, como antes -- decision D1: lo dicho mientras procesa se guarda) en vez de seguir grabando la voz de Vision,
      // que la cancelacion de eco no resta.
      if (nowSpeaking && brainRecorder) endUtterance();
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
    const hasVoice = rms >= brainSilenceThreshold(); // REQ-009: el umbral sale de la sensibilidad elegida (de fabrica, 0,012 como siempre)

    if (hasVoice) {
      brainSpeechActive = true;
      // spec autoenvio-cerebro-sin-voz REQ-001/002 (D1 aprobada): la voz que vuelve cancela la cuenta del autoenvio.
      brainLastVoiceAt = now;
      clearTimeout(brainAutoSendTimer);
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
  brainSendWhenReady = false;
  brainManualListen = false;
  brainPttHeld = false; // spec cerebro-hablar-con-espacio-y-audio-agrupado: Esc con la barra apretada apaga todo y su keyup ya no hace nada
  brainPttHintIfEmpty = false;
  brainPttBegin = null;
  brainTranscriptionsPending = 0;
  brainTranscriptionsInFlight.clear();
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

async function restartBrainModeForMic({ quiet = false } = {}) {
  if (!brainModeActive || brainMicRestarting) return;
  brainMicRestarting = true;
  const wasMissing = brainMicMissingNoticeShown; // venia sin microfono: al volver se avisa aunque el reintento sea silencioso
  try {
    stopBrainMode();
    await startBrainMode({ notifyMicError: false });
    if (!brainModeActive) return; // Escape durante el reinicio
    if (!brainStream) {
      // spec fix-cerebro-escucha-y-microfono REQ-007: los reintentos no llenan el chat de avisos (una nota por episodio).
      if (brainMicMissingNoticeShown) return;
      brainMicMissingNoticeShown = true;
      appendMessage(
        "system-note",
        "Modo Cerebro: se desconectó el micrófono y no hay otro conectado. Sigue encendido y vuelve a escuchar apenas conectes uno.",
        { persist: false },
      );
      return;
    }
    if (!quiet || wasMissing) {
      appendMessage("system-note", `Modo Cerebro: escuchando por «${micLabel(brainStream)}».`, { persist: false });
    }
  } finally {
    brainMicRestarting = false;
  }
}

// spec fix-cerebro-escucha-y-microfono REQ-004/005/007 (reporte del usuario 2026-10-10, auricular Dell WL5024 con microfono de palanca: al
// subirla se silencia; tras unos minutos asi, al bajarla Modo Cerebro quedaba en rojo y no volvia a escuchar, ni con espacio). El microfono
// puede volver SIN un `devicechange` (palanca, Bluetooth que se re-engancha, driver dormido) y nada reintentaba. Una verificacion liviana
// cada 3 s -- y el espacio, y el fin del silencio de la pista -- lo cura. Con un microfono SANO no hace nada (REQ-007 del atajo de espacio).
let brainMicOpening = 0; // getUserMedia del Cerebro en vuelo (incluye el cartel de permiso): no reabrir encima
let brainMicLastError = null; // ultimo error al abrir: con el permiso denegado no se insiste solo (no repetir el cartel del navegador)
let brainMicMissingNoticeShown = false; // la nota «no hay otro conectado» sale una vez por episodio
let brainZeroSince = 0; // desde cuando el microfono entrega CEROS exactos (captura muerta o silenciada por el auricular); 0 = entrega audio
let brainLastQuietRestartAt = 0;
let _brainHealthBuf = null;
const BRAIN_MIC_HEALTH_MS = 3000;
// Un microfono de verdad siempre tiene piso de ruido, aunque la sala este en silencio: CEROS exactos sostenidos = la captura no entrega nada.
const BRAIN_DEAD_SILENCE_MS = 8000;
// Con el espacio o al bajar la palanca alcanza menos, pero nunca un instante: recien abierto, el analizador entrega ceros unos milisegundos.
const BRAIN_FORCE_SILENCE_MS = 1500;
const BRAIN_QUIET_RESTART_MIN_MS = 15000; // con la palanca arriba un rato largo: un reintento silencioso cada 15 s como mucho

function brainMicIsDead() {
  return !hasLiveAudioTrack(brainStream) || !brainAudioCtx || brainAudioCtx.state === "closed";
}

// true si el analizador entrega exactamente cero AHORA (lectura en punto flotante: el ruido de una sala silenciosa no es cero exacto).
function brainMicIsDigitallySilent() {
  if (!brainAnalyser) return false;
  if (!_brainHealthBuf || _brainHealthBuf.length !== brainAnalyser.fftSize) _brainHealthBuf = new Float32Array(brainAnalyser.fftSize);
  brainAnalyser.getFloatTimeDomainData(_brainHealthBuf);
  return _brainHealthBuf.every((v) => v === 0);
}

// Se mide cada 250 ms (tickBrainState): desde cuando el microfono entrega CEROS exactos sin interrupcion.
function trackBrainSilence() {
  if (!brainAnalyser) return;
  if (!brainMicIsDigitallySilent()) brainZeroSince = 0;
  else if (!brainZeroSince) brainZeroSince = performance.now();
}

// `force`: lo pide el usuario (espacio) o el fin de un silencio de la pista -- reabre si el microfono esta muerto o lleva un rato en ceros.
function brainMicHealthCheck({ force = false } = {}) {
  if (!brainModeActive || brainMicRestarting || brainMicOpening > 0) return;
  if (brainMicIsDead()) {
    if (!force && isMicPermissionError(brainMicLastError)) return;
    restartBrainModeForMic({ quiet: true });
    return;
  }
  if (brainAudioCtx.state === "suspended") brainAudioCtx.resume().catch(() => {}); // reanudar no toca el hardware
  const now = performance.now();
  const zerosFor = brainZeroSince ? now - brainZeroSince : 0;
  const idle = !brainSending && !brainPendingText && brainTranscriptionsPending === 0 && !brainRecorder; // nada que perder al reabrir
  const longDeadSilence = zerosFor >= BRAIN_DEAD_SILENCE_MS && now - brainLastQuietRestartAt >= BRAIN_QUIET_RESTART_MIN_MS;
  if ((force && zerosFor >= BRAIN_FORCE_SILENCE_MS) || (idle && longDeadSilence)) {
    brainLastQuietRestartAt = now;
    restartBrainModeForMic({ quiet: true });
  }
}
setInterval(() => brainMicHealthCheck(), BRAIN_MIC_HEALTH_MS);

// REQ-004: click activa/desactiva -- sin modal de confirmacion (a
// diferencia de Record, el modo Cerebro no genera un resumen aparte al
// salir: cada intervencion ya quedo persistida como turno de chat real en
// el momento en que se disparo, no hay nada que "perder" al apagar).
brainBtnEl.addEventListener("click", () => {
  // Pedido explicito posterior a REQ-007: una vez activo, SOLO Escape lo
  // apaga (mismo patron de una conversacion continua en Copilot/ChatGPT/
  // Claude/Gemini). Un click accidental sobre el icono activo no corta el
  // microfono ni pierde una intervencion que se estaba transcribiendo.
  // spec control-microfono-windows REQ-005: aviso antes de empezar si el microfono esta silenciado en Windows (solo el clic manual, no la reconexion del plug and play).
  if (!brainModeActive && canStartVoiceMode("brain")) startWithMicCheck("brain", () => startBrainMode()); // fix-conflicto-modos-voz-simultaneos REQ-002/003
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
// spec cerebro-espacio-turnos (pedido del usuario 2026-10-19): ademas de lo de arriba, Espacio es el boton de turnos del Cerebro:
//   apagado -> ENCIENDE | escuchando -> ENVIA lo dicho | procesando -> vuelve a ESCUCHAR | Vision hablando -> INTERRUMPE (sin cambios) | Esc apaga siempre.
// Con el modo APAGADO Espacio NO puede robarse la barra mientras se escribe: no enciende con el foco en un campo de texto, con una ventana modal abierta, con Ctrl/Alt/Meta/Shift ni repetido.
function spaceMayTurnTheBrainOn(e) {
  if (e.repeat || e.ctrlKey || e.altKey || e.metaKey || e.shiftKey) return false;
  const el = document.activeElement;
  if (el && (el.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName))) return false;
  if (document.querySelector(".dashboard-overlay:not(.hidden)")) return false; // cualquier ventana modal abierta
  return true;
}

// spec cerebro-hablar-con-espacio-y-audio-agrupado: «Mantener Espacio para hablar». keydown (la primera, no la repeticion) = graba YA; keyup = cierra y envia. Entre pulsaciones esta sordo (el detector de voz esta fuera, ver vadTick).
function pushKeydown(e) {
  if (!brainModeActive) {
    // Apagado: enciende (mismas guardas que el modo automatico). Esa pulsacion SOLO enciende: su soltada no envia nada.
    if (!spaceMayTurnTheBrainOn(e)) return;
    e.preventDefault();
    brainPttIgnoreKeyup = true;
    if (canStartVoiceMode("brain")) startWithMicCheck("brain", () => startBrainMode());
    return;
  }
  e.preventDefault(); // evita el scroll de pagina / activar un boton enfocado con espacio
  if (e.repeat || brainPttHeld) return; // mantener apretada no reinicia ni reenvia
  brainPttIgnoreKeyup = false;
  if (isSpeaking()) cancelSpeech(); // interrumpe a Vision, pero NO descarta lo pendiente (aca cada soltada ya envia)
  brainPttHeld = true;
  brainManualListen = false;
  if (!brainRecorder && brainPttBegin) brainPttBegin(); // graba con la misma funcion que usa el detector
}

function pushRelease() {
  if (!brainPttHeld) return;
  brainPttHeld = false;
  // Hay algo que cerrar/enviar (frase grabandose, transcripcion en curso o texto en cola): el aviso «nada dicho» lo da flushBrainSendRequest al terminar. Sin nada, lo da requestBrainSend de inmediato.
  brainPttHintIfEmpty = Boolean((brainRecorder && brainRecorder.state !== "inactive") || brainTranscriptionsPending > 0 || brainPendingText.trim());
  requestBrainSend();
}

document.addEventListener("keyup", (e) => {
  if (e.code !== "Space" && e.key !== " ") return;
  if (!isBrainPushMode()) return;
  if (brainPttIgnoreKeyup) {
    brainPttIgnoreKeyup = false; // la soltada de la pulsacion que encendio el modo
    return;
  }
  if (brainModeActive) pushRelease();
});
// Alt+Tab o pestaña oculta con la barra apretada: el navegador NUNCA manda el keyup. Se trata como soltar para no dejar el microfono grabando para siempre.
window.addEventListener("blur", () => { if (brainModeActive && brainPttHeld) pushRelease(); });
document.addEventListener("visibilitychange", () => { if (document.hidden && brainModeActive && brainPttHeld) pushRelease(); });

document.addEventListener("keydown", (e) => {
  if (e.code !== "Space" && e.key !== " ") return;
  if (isBrainPushMode()) { // REQ-007: la persona eligio hablar con Espacio, asi que NO depende del interruptor de «interrumpir»
    pushKeydown(e);
    return;
  }
  if (!isBrainSpaceInterruptEnabled()) return;
  if (!brainModeActive) {
    // Apagado: enciende por el MISMO camino que el boton (un solo modo de voz a la vez + aviso de microfono silenciado).
    if (!spaceMayTurnTheBrainOn(e)) return;
    e.preventDefault();
    if (canStartVoiceMode("brain")) startWithMicCheck("brain", () => startBrainMode());
    return;
  }
  e.preventDefault(); // evita el scroll de pagina / activar un boton enfocado con espacio
  if (isSpeaking()) { // spec voz-fish-audio REQ-010: tambien corta el audio de los motores de voz
    cancelSpeech();
    // REQ-008: reset liviano de la ronda en curso -- NO toca brainStream/VAD. (Vision hablando: IGUAL QUE ANTES, sin cambios.)
    clearTimeout(brainAutoSendTimer);
    brainPendingText = "";
    // spec fix-cerebro-escucha-y-microfono REQ-005 (enmienda de REQ-007, aprobada 2026-10-10): con el Cerebro SORDO -- sin pista viva, o
    // entregando ceros exactos (palanca del auricular, driver dormido) -- el espacio reabre el microfono; con uno sano sigue sin tocar nada.
    brainMicHealthCheck({ force: true });
    return;
  }
  if (e.repeat) return; // mantener la barra apretada no repite las acciones NUEVAS (enviar / volver a escuchar); el camino de arriba queda como siempre
  // spec fix-cerebro-escucha-y-microfono REQ-005: Espacio revisa la salud del microfono y reabre uno sordo, pero SOLO cuando no hay nada que perder: reabrirlo
  // descarta lo dicho y todavia no enviado. Con texto pendiente, una frase grabandose o una transcripcion en curso se envia primero (el microfono sordo se cura solo
  // en la verificacion de cada 3 s). Si lo reabrio, el modo se esta re-armando y no hay frase que cerrar ni enviar.
  if (!brainPendingText.trim() && !brainRecorder && brainTranscriptionsPending === 0 && !brainSending) {
    brainMicHealthCheck({ force: true });
    if (brainMicRestarting) return;
  }
  const processing = brainSending || state.sending > 0 || brainTranscriptionsPending > 0;
  if (processing && !brainManualListen && !brainRecorder && !brainSpeechActive) {
    // Procesando: el modo vuelve a escuchar (el microfono nunca se cerro) y lo que se diga sale como mensaje nuevo al terminar el turno; el turno en curso NO se cancela.
    brainManualListen = true;
    return;
  }
  requestBrainSend(); // escuchando: cierra la frase y envia lo dicho
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
// spec gestor-conexiones (pedido del usuario 2026-10-09: «ya con esto podemos borrar la solapa Credenciales porque no cumpliria
// ninguna funcion»): con la configuracion ya en Vision, el servidor no lee ni guarda el .env.enc y esa pestaña no sirve: se oculta.
// Antes de importar sigue, porque ahi es donde se cargan las claves de voz y de BigQuery sin editar el .env.
async function syncCredentialsTab() {
  const tabEl = document.querySelector('.settings-tab[data-tab="credentials"]');
  if (!tabEl) return;
  let active;
  try {
    active = Boolean((await (await apiFetchOk(`${API_BASE}/api/vault/status`)).json()).active);
  } catch {
    return; // sin respuesta se deja como esta
  }
  tabEl.style.display = active ? "none" : "";
  if (active && tabEl.classList.contains("active")) switchSettingsTab("connections");
}

function openSettings(tab) {
  settingsOverlayEl.classList.remove("hidden");
  if (tab) switchSettingsTab(tab);
  syncCredentialsTab();
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
  if (tabName === "audio") {
    startMicMeter();
    refreshAudioPanel(); // spec control-microfono-windows REQ-001: estado de los micrófonos de Windows (solo lectura al abrir)
    refreshMeetingSourceSettings(); // spec audio-sistema-modo-reunion REQ-011: lo guardado (la ventana de arranque tambien lo cambia)
  } else stopMicMeter();
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

// REQ-016 (spec fix-detener-y-progreso-visible): `persist: false` al cargar la pagina -- reaplicar el tema guardado no es un
// cambio del usuario, y con el navegador lleno no debe avisar «no se pudo guardar tu preferencia» sin que haya tocado nada.
function applyTheme(theme, { persist = true } = {}) {
  const resolved = VALID_THEMES.includes(theme) ? theme : "light";
  if (resolved === "dark") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = resolved;
  // Sin localStorage el tema no persiste entre recargas, pero sigue aplicado en esta sesion (y se avisa una vez).
  if (persist) savePreference(THEME_KEY, resolved);
}

function getSavedTheme() {
  // Pedido explicito del usuario (2026-09-08): "azul" (claro) pasa a
  // ser el tema default -- mismo criterio que el script inline de
  // index.html (fuente de verdad duplicada ahi solo para evitar el flash
  // de tema oscuro antes de que este modulo cargue). Si el usuario ya
  // eligio "dark" o "dark-blue" explicitamente alguna vez, esa
  // preferencia se respeta.
  const saved = readPreference(THEME_KEY);
  return VALID_THEMES.includes(saved) ? saved : "light";
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
applyTheme(getSavedTheme(), { persist: false });

// -------------------------------------------------------------------------
// Tab: Audio (REQ-009/010, spec narracion-agente-efimera) -- activar/
// desactivar TTS (REQ-007/008), elegir voz en español, probar la voz
// configurada, elegir microfono, y medidor de nivel EN VIVO que solo
// captura mientras esta pestaña/modal esta abierto (patron de Meetily, ver
// Decisiones registradas en spec.md -- evita gastar CPU/bateria de fondo).
// -------------------------------------------------------------------------
const cfgTtsEnabledEl = document.getElementById("cfg-tts-enabled");
const cfgTtsTechReportEl = document.getElementById("cfg-tts-tech-report"); // spec informe-tecnico-hablado
const cfgTtsTechLevelEl = document.getElementById("cfg-tts-tech-level"); // REQ-012
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
const cfgBrainListenModeEl = document.getElementById("cfg-brain-listen-mode");
const cfgBrainSensitivityEl = document.getElementById("cfg-brain-sensitivity");
const cfgBrainSensitivityFieldEl = document.getElementById("cfg-brain-sensitivity-field");
const cfgBrainSensitivityNoteEl = document.getElementById("cfg-brain-sensitivity-note");
const cfgMicMeterMarkEl = document.getElementById("cfg-mic-meter-mark");
// REQ-006 (spec aviso-proceso-terminado): interruptor «Avisarme cuando termine».
const cfgNotifyFinishEl = document.getElementById("cfg-notify-finish");

// Bug real reportado por el usuario: hablando en español, Whisper a veces
// transcribia en INGLES -- sin forzar el idioma, Whisper hace auto-
// deteccion por cada clip de audio, y con fragmentos cortos/ruido puede
// "adivinar mal" (comportamiento conocido de Whisper). Se persiste la
// preferencia igual que THEME_KEY -- "" (vacio) = auto-detectar (default
// explicito pedido por el usuario), o un codigo ISO-639-1 que fuerza el
// idioma en el backend (ver TranscribeRequest.language en main.py).
const STT_LANGUAGE_KEY = "vision.stt.language";

function getSavedSttLanguage() {
  return readPreference(STT_LANGUAGE_KEY) || "";
}

function setSavedSttLanguage(lang) {
  // REQ-016: sin localStorage la preferencia no persiste entre recargas, pero sigue aplicada en esta sesion (y se avisa una vez)
  savePreference(STT_LANGUAGE_KEY, lang || "");
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
  "El micrófono elegido no está conectado. ULTRON usa el predeterminado y vuelve al elegido apenas lo conectes.";
// true cuando el micrófono guardado no está conectado y se abrió el
// predeterminado en su lugar -- lo lee el aviso "una sola vez" de abajo.
let _micUsingFallback = false;
let _micFallbackNoticeShown = false;

function getSavedMic() {
  return {
    deviceId: readPreference(MIC_DEVICE_KEY) || "",
    label: readPreference(MIC_LABEL_KEY) || "",
  };
}

function setSavedMic(deviceId, label) {
  // REQ-016: sin localStorage la elección no persiste entre recargas, pero sigue aplicada en esta sesión (y se avisa una vez)
  savePreference(MIC_DEVICE_KEY, deviceId || "");
  savePreference(MIC_LABEL_KEY, deviceId ? label || "" : "");
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
      maybeWatchNoSignal(stream); // spec control-microfono-windows REQ-009: no hace nada salvo tras un arranque manual
      return stream;
    } catch (err) {
      if (err?.name !== "OverconstrainedError" && err?.name !== "NotFoundError") throw err;
    }
  }
  const hasExtra = Object.keys(extraAudio).length > 0;
  const stream = await navigator.mediaDevices.getUserMedia({ audio: hasExtra ? extraAudio : true });
  _micUsingFallback = deviceId !== ""; // null (no conectado) o un id que no se pudo abrir
  maybeWatchNoSignal(stream); // spec control-microfono-windows REQ-009: no hace nada salvo tras un arranque manual
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
    "El micrófono elegido en Configuración > Audio no está conectado: ULTRON escucha por el predeterminado hasta que lo vuelvas a conectar.",
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
  if (isAudioSettingsPaneOpen()) {
    startMicMeter();
    refreshAudioPanel(); // spec control-microfono-windows: el panel de Windows sigue al plug and play
  }
  refreshActiveMicStreams();
}

if (navigator.mediaDevices?.addEventListener) {
  navigator.mediaDevices.addEventListener("devicechange", handleMicDeviceChange);
}

// spec control-microfono-windows: el modulo recibe las utilidades de app.js (mismo patron que vault.js). No abre nada por su cuenta.
initMicControl({ apiBase: API_BASE, apiFetchOk, friendlyErrorMessage, getSavedMic, openMicStream });
// spec audio-sistema-modo-reunion: la fuente de la reunion (ventana de arranque y Configuracion > Audio). Tampoco abre nada por su cuenta.
initMeetingSource({ readPreference, savePreference });

// REQ-002 (spec fix-deteccion-microfono-bluetooth, 2026-09-27): `devicechange`
// de Chromium NO es confiable para Bluetooth (a veces no dispara al emparejar).
// El emparejamiento Bluetooth suele hacerse desde Configuración de Windows con
// la pestaña de Vision en segundo plano, así que al volver el foco a Vision se
// re-enumera -- un micrófono conectado "afuera" aparece igual, sin recargar.
// Es aditivo y barato: si no cambió nada, populateMicOptions deja la lista igual.
if (typeof window !== "undefined" && window.addEventListener) {
  let _lastMicFocusRefreshAt = 0;
  window.addEventListener("focus", () => {
    const now = Date.now();
    if (now - _lastMicFocusRefreshAt < 1000) return; // evita ráfagas de focus
    _lastMicFocusRefreshAt = now;
    handleMicDeviceChange();
  });
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
  const raw = readPreference(BRAIN_AUTOSEND_ENABLED_KEY);
  return raw === null ? true : raw === "true"; // default: activado
}

function setBrainAutosendEnabled(enabled) {
  // REQ-016: sin localStorage la preferencia no persiste entre recargas, pero sigue aplicada en esta sesion (y se avisa una vez)
  savePreference(BRAIN_AUTOSEND_ENABLED_KEY, String(enabled));
}

// spec autoenvio-cerebro-sin-voz REQ-005: si el navegador no deja guardar, lo escrito rige en memoria hasta recargar
// (antes la pausa seguia siendo la guardada antes, o la de 2 s, aunque el aviso dijera «vale hasta recargar»).
let brainAutosendPauseUnsavedSeconds = null;

function getBrainAutosendPauseMs() {
  if (brainAutosendPauseUnsavedSeconds !== null) return brainAutosendPauseUnsavedSeconds * 1000;
  try {
    const raw = parseInt(localStorage.getItem(BRAIN_AUTOSEND_PAUSE_KEY), 10);
    if (Number.isFinite(raw) && raw >= 1 && raw <= 10) return raw * 1000;
    return BRAIN_AUTOSEND_PAUSE_DEFAULT_MS;
  } catch {
    return BRAIN_AUTOSEND_PAUSE_DEFAULT_MS;
  }
}

// spec autoenvio-cerebro-sin-voz REQ-005: valida el valor escrito y devuelve que se guardo y por que.
// Fuera de rango -> el limite mas cercano (0 -> 1, 15 -> 10); vacio o no numerico -> el valor por defecto (2 s).
function normalizeBrainAutosendPause(raw) {
  const text = String(raw ?? "").trim();
  const parsed = Number(text);
  if (text === "" || !Number.isFinite(parsed)) return { seconds: BRAIN_AUTOSEND_PAUSE_DEFAULT_MS / 1000, corrected: true };
  const rounded = Math.round(parsed);
  const seconds = Math.max(1, Math.min(10, rounded));
  return { seconds, corrected: seconds !== parsed };
}

// Devuelve los segundos guardados (compatibilidad con quien ya lo usaba). `persisted` en la 2da propiedad via setBrainAutosendPauseResult.
function setBrainAutosendPauseSeconds(seconds) {
  return setBrainAutosendPauseResult(seconds).seconds;
}

function setBrainAutosendPauseResult(raw) {
  const { seconds, corrected } = normalizeBrainAutosendPause(raw);
  // Sin localStorage la preferencia no persiste entre recargas, pero sigue aplicada en esta sesion (y se avisa una vez, REQ-016
  // de fix-detener-y-progreso-visible, ademas del texto de estado de este campo).
  const persisted = savePreference(BRAIN_AUTOSEND_PAUSE_KEY, String(seconds));
  brainAutosendPauseUnsavedSeconds = persisted ? null : seconds;
  return { seconds, corrected, persisted };
}

// REQ-004 (spec atajo-espacio-modo-cerebro): preferencia del atajo de
// espacio -- mismo patron de persistencia y default (activado) que
// BRAIN_AUTOSEND_ENABLED_KEY de arriba.
// spec cerebro-hablar-con-espacio-y-audio-agrupado REQ-001/012: como escucha el Modo Cerebro. «auto» (de fabrica) = lo de siempre; «push» = Mantener Espacio para hablar.
const BRAIN_LISTEN_MODE_KEY = "vision.brain.listenMode";
function getBrainListenMode() {
  return readPreference(BRAIN_LISTEN_MODE_KEY) === "push" ? "push" : "auto"; // cualquier otro valor guardado = automatico
}
function isBrainPushMode() {
  return getBrainListenMode() === "push";
}
const BRAIN_SPACE_INTERRUPT_KEY = "vision.brain.spaceInterruptEnabled";

function isBrainSpaceInterruptEnabled() {
  const raw = readPreference(BRAIN_SPACE_INTERRUPT_KEY);
  return raw === null ? true : raw === "true"; // default: activado
}

function setBrainSpaceInterruptEnabled(enabled) {
  // REQ-016: sin localStorage la preferencia no persiste entre recargas, pero sigue aplicada en esta sesion (y se avisa una vez)
  savePreference(BRAIN_SPACE_INTERRUPT_KEY, String(enabled));
}

// spec cerebro-hablar-con-espacio-y-audio-agrupado REQ-010/012: la marca del medidor cae donde cae el umbral (umbral x 300 %, la escala del propio medidor); la sensibilidad solo vale en modo Automatico.
function syncBrainListenSettings() {
  if (cfgMicMeterMarkEl) cfgMicMeterMarkEl.style.left = `${(brainSilenceThreshold() * 300).toFixed(1)}%`;
  const push = isBrainPushMode();
  if (cfgBrainSensitivityEl) cfgBrainSensitivityEl.disabled = push;
  if (cfgBrainSensitivityFieldEl) cfgBrainSensitivityFieldEl.classList.toggle("settings-field-disabled", push);
  if (cfgBrainSensitivityNoteEl) cfgBrainSensitivityNoteEl.textContent = push ? "Solo vale con el modo Automático: con «Mantener Espacio para hablar» el ruido no se tiene en cuenta." : "";
}
if (cfgBrainListenModeEl) {
  cfgBrainListenModeEl.addEventListener("change", () => {
    savePreference(BRAIN_LISTEN_MODE_KEY, cfgBrainListenModeEl.value === "push" ? "push" : "auto");
    brainPttHeld = false; // cambiar de modo con la barra apretada no deja nada grabando
    syncBrainListenSettings();
    if (brainModeActive) renderBrainBanner(brainBtnEl.dataset.state);
  });
}
if (cfgBrainSensitivityEl) {
  cfgBrainSensitivityEl.addEventListener("change", () => {
    savePreference(BRAIN_SENSITIVITY_KEY, cfgBrainSensitivityEl.value); // el detector lee el umbral en cada cuadro: se aplica en vivo
    syncBrainListenSettings();
  });
}

if (cfgBrainAutosendEnabledEl) {
  cfgBrainAutosendEnabledEl.addEventListener("change", () => {
    setBrainAutosendEnabled(cfgBrainAutosendEnabledEl.checked);
    if (cfgBrainAutosendPauseFieldEl) {
      cfgBrainAutosendPauseFieldEl.classList.toggle("settings-field-disabled", !cfgBrainAutosendEnabledEl.checked);
    }
  });
}
// REQ-005: se guarda al escribir (con un pequeno retraso, para no guardar "1" mientras se tipea "10") y tambien en "change".
// El texto de estado dice que se guardo; si el valor era invalido, a cual se corrigio. Mientras se tipea NO se reescribe el campo:
// la correccion del numero se refleja recien en "change".
const cfgBrainAutosendPauseStatusEl = document.getElementById("cfg-brain-autosend-pause-status");
let _brainPauseSaveTimer = null;
function saveBrainAutosendPauseFromField({ rewriteField }) {
  const typed = cfgBrainAutosendPauseEl.value;
  const { seconds, corrected, persisted } = setBrainAutosendPauseResult(typed);
  if (rewriteField) cfgBrainAutosendPauseEl.value = seconds;
  if (cfgBrainAutosendPauseStatusEl) {
    let text = `Guardado: ${seconds} s`;
    if (corrected) text = `${typed.trim() === "" ? "Vacío" : `“${typed}”`} no es válido; se guardó ${seconds} s`;
    if (!persisted) {
      // REQ-005: sin «Guardado» (seria falso); el valor rige igual hasta recargar (getBrainAutosendPauseMs).
      const unsaved = `No se pudo guardar; vale solo hasta recargar (${seconds} s)`;
      text = corrected ? `${typed.trim() === "" ? "Vacío" : `“${typed}”`} no es válido. ${unsaved}` : unsaved;
    }
    cfgBrainAutosendPauseStatusEl.textContent = text;
  }
}
if (cfgBrainAutosendPauseEl) {
  cfgBrainAutosendPauseEl.addEventListener("input", () => {
    clearTimeout(_brainPauseSaveTimer);
    _brainPauseSaveTimer = setTimeout(() => saveBrainAutosendPauseFromField({ rewriteField: false }), 400);
  });
  cfgBrainAutosendPauseEl.addEventListener("change", () => {
    clearTimeout(_brainPauseSaveTimer);
    saveBrainAutosendPauseFromField({ rewriteField: true });
  });
}
if (cfgBrainSpaceInterruptEnabledEl) {
  cfgBrainSpaceInterruptEnabledEl.addEventListener("change", () => {
    setBrainSpaceInterruptEnabled(cfgBrainSpaceInterruptEnabledEl.checked);
  });
}
// REQ-006/007 (spec aviso-proceso-terminado): prenderlo es un clic del usuario, asi que es el momento de pedir el permiso si el navegador
// todavia no lo decidio; apagarlo no pide nada.
if (cfgNotifyFinishEl) {
  cfgNotifyFinishEl.addEventListener("change", () => {
    setNotifyOnFinishEnabled(cfgNotifyFinishEl.checked);
    if (cfgNotifyFinishEl.checked) requestNotifyPermission();
    refreshNotifyPermissionStatus();
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
  if (cfgTtsTechLevelEl) cfgTtsTechLevelEl.value = getTechReportLevel();
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
  // spec cerebro-hablar-con-espacio-y-audio-agrupado REQ-009/012: como escucha y con que sensibilidad.
  if (cfgBrainListenModeEl) cfgBrainListenModeEl.value = getBrainListenMode();
  if (cfgBrainSensitivityEl) cfgBrainSensitivityEl.value = getBrainSensitivity();
  syncBrainListenSettings();
  // REQ-006/007 (spec aviso-proceso-terminado): el interruptor y el estado del permiso del navegador.
  if (cfgNotifyFinishEl) cfgNotifyFinishEl.checked = isNotifyOnFinishEnabled();
  refreshNotifyPermissionStatus();
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
if (cfgTtsTechLevelEl) {
  cfgTtsTechLevelEl.addEventListener("change", () => setTechReportLevel(cfgTtsTechLevelEl.value));
}
if (cfgTtsVoiceSelectEl) {
  cfgTtsVoiceSelectEl.addEventListener("change", () => setSavedVoiceURI(cfgTtsVoiceSelectEl.value));
}
if (cfgLoopAlarmTypeEl) {
  cfgLoopAlarmTypeEl.addEventListener("change", () => {
    savePreference(LOOP_ALARM_TYPE_KEY, cfgLoopAlarmTypeEl.value); // REQ-016: si no se guarda, rige hasta recargar
  });
}
if (cfgLoopAlarmIntervalEl) {
  cfgLoopAlarmIntervalEl.addEventListener("change", () => {
    savePreference(LOOP_ALARM_INTERVAL_KEY, cfgLoopAlarmIntervalEl.value);
  });
}
if (cfgLoopAlarmVolumeEl && cfgLoopAlarmVolumeValEl) {
  cfgLoopAlarmVolumeEl.addEventListener("input", () => {
    cfgLoopAlarmVolumeValEl.textContent = Math.round(parseFloat(cfgLoopAlarmVolumeEl.value) * 100) + "%";
    savePreference(LOOP_ALARM_VOLUME_KEY, cfgLoopAlarmVolumeEl.value);
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
        `Para hablar con ${engine.label}, ULTRON le envía el texto que lee en voz alta: las respuestas, ` +
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
        "En una PC sin placa de video NVIDIA, tu voz clonada tarda varios segundos por frase: ULTRON va a " +
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
      "instalación no cambie nada de lo que ULTRON ya usa: si cambiaría algo, no instalo nada y te explico " +
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
    if (isAudioSettingsPaneOpen()) {
      startMicMeter();
      refreshAudioPanel(); // spec control-microfono-windows: el panel de Windows pasa al micrófono elegido
    }
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
const connFileNoteEl = document.getElementById("conn-file-note");
const connCfToggleEl = document.getElementById("conn-cf-toggle");
const connCfListEl = document.getElementById("conn-cf-list");

const MB_IN_BYTES = 1024 * 1024;

// Dibuja Configuración > Conexiones desde la ÚLTIMA prueba (`_connSnapshot`, ver checkConnectionsNow): no prueba nada por su cuenta.
// spec interruptores-conexiones-skills REQ-006: el estado solo trae lo ACTIVO (lo apagado no se prueba); lo apagado se lista igual, con su interruptor, desde la
// lista de interruptores. Una fila por conexión, en orden alfabético estable.
function renderConnStatusList() {
  const snap = _connSnapshot;
  if (!snap || !connStatusListEl) return;
  if (snap.error) {
    connStatusListEl.innerHTML = "";
    connStatusStatusEl.textContent = friendlyErrorMessage(snap.error);
    connStatusStatusEl.className = "settings-status error";
    return;
  }
  const statusByName = new Map(snap.connectors.map((c) => [connectorDisplayName(c), c]));
  const rows = snap.switches.map((sw) => ({
    sw,
    c: statusByName.get(sw.display) || { name: sw.type, display: sw.display, status: "off", detail: sw.env_disabled ? "Desactivada en el .env" : "Desactivada" },
  }));
  const withSwitch = new Set(snap.switches.map((s) => s.display));
  snap.connectors.filter((c) => !withSwitch.has(connectorDisplayName(c))).forEach((c) => rows.push({ sw: null, c }));
  (snap.capabilities || []).forEach((cap) => rows.push({ sw: null, c: cap, cap }));
  connStatusListEl.innerHTML = "";
  sortByConnectionName(rows, (r) => connectorDisplayName(r.c)).forEach(({ sw, c, cap }) => {
    const row = document.createElement("div");
    row.className = "conn-status-row";
    const dot = document.createElement("span");
    dot.className = `connector-dot ${c.status}`;
    const label = document.createElement("span");
    label.className = "conn-status-label";
    label.textContent = connectorDisplayName(c);
    const detail = document.createElement("span");
    detail.className = "conn-status-detail";
    detail.textContent = cap ? cap.detail : (c.status === "online" ? "Conectado" : (c.detail || "Sin conexión")); // todo texto del servidor va como textContent
    if (c.technical) detail.title = `Detalle técnico: ${c.technical}`;
    row.append(dot, label, detail);
    if (cap) {
      // Outlook y Teams / Office: su interruptor es el del servidor (el mismo de la ventana del gestor); al cambiarlo se relee su estado y se repintan las dos listas
      row.dataset.enabled = String(cap.enabled);
      row.appendChild(
        buildSwitch({
          checked: cap.enabled,
          label: `${cap.enabled ? "Desactivar" : "Activar"} ${cap.display}`,
          onToggle: async (next) => {
            await apiFetchOk(`${API_BASE}${cap.switchPath}`, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ enabled: next }),
            });
            await refreshCapabilities();
          },
        }),
      );
    }
    if (sw) {
      row.dataset.enabled = String(sw.enabled);
      // El interruptor quedó distinto de lo que se probó: se avisa en la fila, sin volver a probar ni moverla (se aplica con «Actualizar estado»).
      const probedOn = statusByName.has(sw.display);
      if (sw.enabled !== probedOn && !sw.env_disabled) {
        row.dataset.pending = "true";
        detail.textContent = sw.enabled ? "Activada: tocá «Actualizar estado»" : "Desactivada: tocá «Actualizar estado»";
      }
      const toggle = buildSwitch({
        checked: sw.enabled,
        disabled: sw.env_disabled && !sw.enabled,
        label: `${sw.enabled ? "Desactivar" : "Activar"} ${sw.display}`,
        onToggle: async (next) => {
          const fresh = await postConnectionSwitch(sw.key, next);
          updateConnectionsSummary(fresh);
          applySwitchesToConnSnapshot(fresh); // se guarda y se ve en el interruptor; el estado y el panel lateral esperan a «Actualizar estado»
          connStatusStatusEl.textContent = "Guardado. Cuando termines, tocá «Actualizar estado» para ver cómo quedan acá y en el panel lateral.";
          connStatusStatusEl.className = "settings-status";
        },
      });
      row.appendChild(toggle);
    }
    connStatusListEl.appendChild(row);
  });
  if (!snap.pending) {
    connStatusStatusEl.textContent = "";
    connStatusStatusEl.className = "settings-status";
  }
}
// «Actualizar estado» es lo único que vuelve a probar: actualiza esta lista y el panel lateral juntos, con la misma prueba.
connStatusRefreshEl.addEventListener("click", () => checkConnectionsNow());

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
    li.addEventListener("click", (event) => {
      // Un nodo hijo vive DENTRO del <li> de su padre: sin este filtro, tocar un hijo tambien disparaba el manejador del padre y lo colapsaba
      // (pasaba en SharePoint: tocar el nombre de una lista contraia el sitio). Solo cuenta el clic que cae en ESTE nodo, no en uno anidado.
      if (event.target instanceof Element && event.target.closest("li") !== li) return;
      onExpand(li);
    });
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

// spec conexiones-con-nombre REQ-010: una rama por cada conexion SharePoint con nombre, cada una con SUS listas (carga perezosa al abrirla).
// El boton original «SharePoint — listas» sigue siendo el contenedor; sus hijos se arman desde GET /api/connections.
let _spConnectionsLoaded = false;
async function _loadSpConnectionBranches() {
  connSpListsListEl.innerHTML = "<li>Buscando conexiones…</li>";
  const resp = await apiFetch(`${API_BASE}/api/connections`);
  if (!resp.ok) throw await httpErrorFromResponse(resp);
  const data = await resp.json();
  const sites = (data.connections || []).filter((c) => c.type === "sharepoint");
  connSpListsListEl.innerHTML = "";
  if (!sites.length) {
    connSpListsListEl.appendChild(_buildConnTreeItem("No hay conexiones de SharePoint configuradas."));
    return;
  }
  sites.forEach((site) => {
    connSpListsListEl.appendChild(
      _buildConnTreeItem(`🗂 ${site.display}`, (li) => _expandSpLists(li, site.name))
    );
  });
}

async function _expandSpLists(siteLi, connectionName) {
  const existing = siteLi.querySelector(".conn-tree-sublist");
  if (existing) {
    existing.classList.toggle("hidden");
    return;
  }
  const sublist = document.createElement("ul");
  sublist.className = "conn-tree-list conn-tree-sublist";
  sublist.innerHTML = "<li>Cargando listas…</li>";
  siteLi.appendChild(sublist);
  try {
    const resp = await apiFetch(`${API_BASE}/api/config/connections/sharepoint/lists?name=${encodeURIComponent(connectionName)}`);
    if (!resp.ok) throw await httpErrorFromResponse(resp);
    const data = await resp.json();
    sublist.innerHTML = "";
    (data.lists || []).forEach((name) => sublist.appendChild(_buildConnTreeItem(`📋 ${name}`)));
    if (!(data.lists || []).length) sublist.appendChild(_buildConnTreeItem("(sin listas)"));
  } catch (err) {
    renderListError(sublist, friendlyErrorMessage(err));
  }
}

connSpListsToggleEl.addEventListener("click", async () => {
  const expanded = connSpListsToggleEl.getAttribute("aria-expanded") === "true";
  connSpListsToggleEl.setAttribute("aria-expanded", String(!expanded));
  connSpListsToggleEl.querySelector(".conn-tree-caret").textContent = expanded ? "▶" : "▼";
  connSpListsListEl.classList.toggle("hidden", expanded);
  if (expanded || _spConnectionsLoaded) return;
  try {
    await _loadSpConnectionBranches();
    _spConnectionsLoaded = true;
  } catch (err) {
    renderListError(connSpListsListEl, friendlyErrorMessage(err));
  }
});

// spec conexiones-con-nombre Fase B (REQ-006/010): una rama por cada conexion de Confluence, con sus SECCIONES (ej. «MP&L Procedures»); cada pagina
// carga sus hijos de forma perezosa al abrirla (un espacio entero trae cientos). Todo texto de Confluence se pinta con textContent: nunca innerHTML.
let _cfConnectionsLoaded = false;

function _cfNodeLabel(prefix, title) {
  return `${prefix} ${title}`;
}

async function _cfExpandChildren(pageLi, pageId, connectionName) {
  const existing = pageLi.querySelector(":scope > .conn-tree-sublist");
  if (existing) {
    existing.classList.toggle("hidden");
    return;
  }
  const sublist = document.createElement("ul");
  sublist.className = "conn-tree-list conn-tree-sublist";
  sublist.innerHTML = "<li>Cargando…</li>";
  pageLi.appendChild(sublist);
  try {
    const resp = await apiFetch(`${API_BASE}/api/confluence/children?id=${encodeURIComponent(pageId)}&connection=${encodeURIComponent(connectionName)}`);
    if (!resp.ok) throw await httpErrorFromResponse(resp);
    const data = await resp.json();
    sublist.innerHTML = "";
    (data.children || []).forEach((child) => {
      sublist.appendChild(
        _buildConnTreeItem(_cfNodeLabel("📄", child.title), (li) => _cfExpandChildren(li, child.id, connectionName))
      );
    });
    if (!(data.children || []).length) sublist.appendChild(_buildConnTreeItem("(sin subpáginas)"));
  } catch (err) {
    renderListError(sublist, friendlyErrorMessage(err));
  }
}

async function _loadCfConnectionBranches() {
  connCfListEl.innerHTML = "<li>Buscando conexiones…</li>";
  const resp = await apiFetch(`${API_BASE}/api/connections`);
  if (!resp.ok) throw await httpErrorFromResponse(resp);
  const data = await resp.json();
  const sites = (data.connections || []).filter((c) => c.type === "confluence");
  connCfListEl.innerHTML = "";
  if (!sites.length) {
    connCfListEl.appendChild(_buildConnTreeItem("No hay conexiones de Confluence configuradas."));
    return;
  }
  sites.forEach((site) => {
    const sections = (site.info && site.info.sections) || [];
    const branch = _buildConnTreeItem(
      `🌐 ${site.display}${site.status === "incomplete" ? " — faltan CONFLUENCE_EMAIL / CONFLUENCE_API_TOKEN en tu .env" : ""}`,
      (li) => {
        const existing = li.querySelector(":scope > .conn-tree-sublist");
        if (existing) {
          existing.classList.toggle("hidden");
          return;
        }
        const sublist = document.createElement("ul");
        sublist.className = "conn-tree-list conn-tree-sublist";
        if (!sections.length) sublist.appendChild(_buildConnTreeItem("Esta conexión no tiene secciones: agregalas en connections.json."));
        sections.forEach((section) => {
          sublist.appendChild(
            _buildConnTreeItem(_cfNodeLabel("📚", section.name), (sectionLi) => _cfExpandChildren(sectionLi, section.page_id, site.name))
          );
        });
        li.appendChild(sublist);
      }
    );
    connCfListEl.appendChild(branch);
  });
}

connCfToggleEl.addEventListener("click", async () => {
  const expanded = connCfToggleEl.getAttribute("aria-expanded") === "true";
  connCfToggleEl.setAttribute("aria-expanded", String(!expanded));
  connCfToggleEl.querySelector(".conn-tree-caret").textContent = expanded ? "▶" : "▼";
  connCfListEl.classList.toggle("hidden", expanded);
  if (expanded || _cfConnectionsLoaded) return;
  try {
    await _loadCfConnectionBranches();
    _cfConnectionsLoaded = true;
  } catch (err) {
    renderListError(connCfListEl, friendlyErrorMessage(err));
  }
});

// spec conexiones-con-nombre REQ-011: connections.json se relee solo; si esta roto, se avisa aca (la ultima lectura valida sigue funcionando).
function connSpToggleIsOpen() {
  return connSpListsToggleEl.getAttribute("aria-expanded") === "true";
}

function connCfToggleIsOpen() {
  return connCfToggleEl.getAttribute("aria-expanded") === "true";
}

async function _refreshConnFileNote() {
  if (!connFileNoteEl) return;
  // Se invalida ANTES de esperar la respuesta: si el usuario abre una rama mientras la relectura de connections.json todavia no llego, no puede ver la lista vieja
  // (con la suite completa, bajo carga, la respuesta tardaba lo suficiente como para que pasara).
  _spConnectionsLoaded = false;
  _cfConnectionsLoaded = false;
  try {
    const resp = await apiFetch(`${API_BASE}/api/connections`);
    const data = resp.ok ? await resp.json() : null;
    if (!data) return;
    // Si una rama se abrio mientras esperabamos esta respuesta, se arma de nuevo con los datos frescos (y vuelve a quedar marcada como cargada).
    if (connSpToggleIsOpen()) _loadSpConnectionBranches().then(() => { _spConnectionsLoaded = true; }).catch(() => {});
    if (connCfToggleIsOpen()) _loadCfConnectionBranches().then(() => { _cfConnectionsLoaded = true; }).catch(() => {});
    connFileNoteEl.textContent = data.error
      ? `⚠ ${data.error}`
      : `Las conexiones se editan en ${data.file}. Se releen solas al guardar el archivo.`;
    connFileNoteEl.className = data.error ? "settings-status error" : "settings-hint";
  } catch {
    // sin red: el estado de abajo ya muestra el error de conexion
  }
}

// spec gestor-conexiones Fase B: el gestor de conexiones (frontend/js/vault.js) va arriba del todo de esta pestaña. Lo que se guarda ahí se ve en la lista de
// estado y en el panel lateral recién con «Actualizar estado» (misma regla que los interruptores, decisión del usuario 2026-10-08).
let _vaultStarted = false;
function onVaultChanged() {
  connStatusStatusEl.textContent = "Guardado. Cuando termines, tocá «Actualizar estado» para ver cómo quedan acá y en el panel lateral.";
  connStatusStatusEl.className = "settings-status";
}

async function loadConnectionsSettingsPane() {
  if (!_vaultStarted) {
    _vaultStarted = true;
    initVault({ rootId: "vault-root", API_BASE, apiFetchOk, friendlyErrorMessage, showConfirmModal, buildSwitch, onChanged: onVaultChanged });
  } else {
    refreshVault();
  }
  // abrir la pestaña muestra la última prueba (la misma del panel lateral); solo si todavía no hubo ninguna, la hace
  const status = _connSnapshot ? Promise.resolve(renderConnStatusList()) : checkConnectionsNow();
  await Promise.all([status, _loadConnThresholds(), _refreshConnFileNote()]);
}

credClearEl.addEventListener("click", async () => {
  const confirmed = await showConfirmModal({
    title: "Quitar todas las credenciales",
    message: "Se van a borrar TODAS las credenciales guardadas (BigQuery y voz). Esta acción no se puede deshacer.",
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

// -- Tab: Red > Servidor (spec jarvis-mobile-pwa; la pestaña se llamaba «Servidor» y desde la spec proxy-autoheal es «Red», con Servidor y Proxy) ----------------
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
  loadProxyPane();
}

// -- Tab: Red > Proxy (spec proxy-autoheal REQ-007) ---------------------------
// A diferencia de «Servidor» (que vive en el localStorage de ESTE navegador), esto cambia por donde sale a internet el BACKEND y se guarda cifrado en la base de Vision:
// por eso habla con `/api/proxy/*`, que responde solo a esta página en esta PC. La lista y el aviso vienen del servidor; todo se pinta con textContent.
const cfgProxyInuseEl = document.getElementById("cfg-proxy-inuse");
const cfgProxySavedEl = document.getElementById("cfg-proxy-saved");
const cfgProxyListEl = document.getElementById("cfg-proxy-list");
const cfgProxyAddressEl = document.getElementById("cfg-proxy-address");
const cfgProxyTestEl = document.getElementById("cfg-proxy-test");
const cfgProxySaveEl = document.getElementById("cfg-proxy-save");
const cfgProxyReconnectEl = document.getElementById("cfg-proxy-reconnect");
const cfgProxyStatusEl = document.getElementById("cfg-proxy-status");

const PROXY_SOURCE_LABELS = {
  guardado: "Guardada",
  predeterminado: "Nombre de la empresa",
  dns: "Que indica la red de la empresa",
  pac: "Que indica Windows",
  directo: "Conexión directa",
  manual: "Escrita acá",
};

function proxyShortName(url) {
  return url ? url.replace(/^https?:\/\//, "") : "Conexión directa";
}

function setProxyStatus(text, kind = "") {
  cfgProxyStatusEl.textContent = text;
  cfgProxyStatusEl.className = kind ? `settings-status ${kind}` : "settings-status";
}

function setProxyBusy(busy) {
  for (const btn of [cfgProxyTestEl, cfgProxySaveEl, cfgProxyReconnectEl]) btn.disabled = busy;
}

function renderProxySnapshot(snap) {
  cfgProxyInuseEl.textContent = snap.in_use ? proxyShortName(snap.in_use) : "Sin proxy (conexión directa o la que elija el sistema)";
  cfgProxySavedEl.textContent = snap.saved ? proxyShortName(snap.saved) : "Ninguna";
  // Distintas A PROPOSITO: Vision salio directo por ahora y conserva lo guardado por si en otra red sí sirve (no es un error)
  if (snap.kept_saved) cfgProxySavedEl.textContent += " (se conserva: ahora no responde y ULTRON usa otra salida)";
  const rows = (snap.candidates || []).map((c) => {
    const row = document.createElement("div");
    row.className = "proxy-row";
    row.dataset.state = c.alive ? "up" : "down";
    const name = document.createElement("span");
    name.className = "proxy-row-name";
    name.textContent = proxyShortName(c.url);
    const source = document.createElement("span");
    source.className = "proxy-row-source";
    source.textContent = PROXY_SOURCE_LABELS[c.source] || "";
    const stateEl = document.createElement("span");
    stateEl.className = "proxy-row-state";
    stateEl.textContent = c.alive ? (c.ms != null ? `responde (${c.ms} ms)` : "responde") : "no responde";
    row.append(name, source, stateEl);
    return row;
  });
  cfgProxyListEl.replaceChildren(...rows);
}

async function proxyCall(path, { post = true, body } = {}) {
  const options = post ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body ?? {}) } : {};
  const resp = await apiFetchOk(`${API_BASE}/api/proxy${path}`, options);
  return resp.json();
}

async function loadProxyPane() {
  try {
    renderProxySnapshot(await proxyCall("/status", { post: false }));
  } catch (err) {
    setProxyStatus(`No se pudo leer el estado de la salida a internet. ${friendlyErrorMessage(err)}`, "error");
  }
}

async function runProxyAction(label, action) {
  setProxyBusy(true);
  setProxyStatus(label);
  try {
    await action();
  } catch (err) {
    setProxyStatus(friendlyErrorMessage(err), "error");
  } finally {
    setProxyBusy(false);
  }
}

cfgProxyReconnectEl.addEventListener("click", () =>
  runProxyAction("Buscando una salida que funcione…", async () => {
    const snap = await proxyCall("/reconnect");
    renderProxySnapshot(snap);
    if (snap.action === "none" || snap.action === "error") setProxyStatus(snap.message, "error");
    else setProxyStatus(snap.message ? `Listo. ${snap.message}` : "Listo. La salida a internet responde.", "ok");
  }),
);

cfgProxyTestEl.addEventListener("click", () =>
  runProxyAction("Probando…", async () => {
    const one = await proxyCall("/test", { body: { address: cfgProxyAddressEl.value.trim() } });
    const what = one.url ? proxyShortName(one.url) : "La conexión directa";
    if (one.alive) setProxyStatus(`${what} responde${one.ms != null ? ` (${one.ms} ms)` : ""}.`, "ok");
    else setProxyStatus(`${what} no responde. Revisá que estés conectado a la VPN de tu empresa y probá de nuevo.`, "error");
  }),
);

cfgProxySaveEl.addEventListener("click", () =>
  runProxyAction("Guardando y reconectando…", async () => {
    const out = await proxyCall("/apply", { body: { address: cfgProxyAddressEl.value.trim() } });
    renderProxySnapshot(out);
    if (!out.applied) {
      setProxyStatus("Esa dirección no responde, así que no se cambió nada. Revisá que estés conectado a la VPN de tu empresa y probá de nuevo.", "error");
    } else if (!out.saved) {
      setProxyStatus("Listo. ULTRON ya usa esa salida, pero no quedó guardada: primero importá tu configuración en Conexiones.", "ok");
    } else {
      setProxyStatus("Listo. ULTRON usa esa salida y la dejó guardada.", "ok");
    }
  }),
);

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

// ---------------------------------------------------------------------------
// /limpiar-adjuntos (spec adjuntar-archivos-chat REQ-009): 1) vista previa de lo
// que se libera con un checkbox "Conservar" por fila; 2) borra del DISCO lo no
// conservado; 3) informa lo liberado REAL (archivos + base de datos compactada).
// ---------------------------------------------------------------------------
async function renderAttachmentsCleanupModal() {
  commandModalBodyEl.innerHTML = "";
  commandModalOverlayEl.classList.remove("hidden"); // sin esto el modal se arma pero nunca se ve
  const status = document.createElement("p");
  status.className = "cleanup-summary";
  status.textContent = "Buscando adjuntos…";
  commandModalBodyEl.appendChild(status);

  let preview;
  try {
    const resp = await apiFetchOk(`${API_BASE}/api/sessions/${encodeURIComponent(state.sessionId)}/attachments/cleanup-preview`);
    preview = await resp.json();
  } catch (err) {
    status.textContent = `No se pudo revisar los adjuntos. ${friendlyErrorMessage(err)}`;
    return;
  }
  const rows = preview.rows || [];
  if (rows.length === 0) {
    status.textContent = "Esta sesión no tiene adjuntos para limpiar.";
    return;
  }

  status.textContent = "Se van a borrar del disco los archivos que NO marques como «Conservar». Los archivos leídos en su lugar nunca se borran: solo se quita la referencia.";
  const list = document.createElement("div");
  list.className = "cleanup-list";
  const keep = new Map();
  const updateTotal = () => {
    let bytes = 0;
    for (const r of rows) if (r.deletable && !keep.get(r.id)) bytes += r.bytes || 0;
    total.textContent = `Se liberarán aproximadamente ${formatFileSize(bytes)} (más lo que se compacte en la base de datos).`;
  };
  for (const r of rows) {
    const row = document.createElement("div");
    row.className = "cleanup-row";
    row.dataset.locked = String(!r.deletable);
    const name = document.createElement("span");
    name.className = "cleanup-row-name";
    name.textContent = r.kind === "images" ? r.name : `${r.name}${r.missing ? " (ya no está en el disco)" : ""}`;
    name.title = r.path || r.name;
    const size = document.createElement("span");
    size.className = "cleanup-row-size";
    size.textContent = r.inplace ? "en su lugar" : formatFileSize(r.bytes || 0);
    row.append(name, size);
    if (r.kind === "orphan") {
      const note = document.createElement("span");
      note.className = "cleanup-row-note";
      note.textContent = "sin usar";
      row.appendChild(note);
    }
    if (!r.deletable) {
      const note = document.createElement("span");
      note.className = "cleanup-row-note";
      note.textContent = "lo usa otra sesión";
      row.appendChild(note);
    } else {
      const label = document.createElement("label");
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.onchange = () => {
        keep.set(r.id, cb.checked);
        updateTotal();
      };
      label.append(cb, document.createTextNode("Conservar"));
      row.appendChild(label);
    }
    list.appendChild(row);
  }
  const total = document.createElement("p");
  total.className = "cleanup-summary";
  commandModalBodyEl.append(list, total);
  updateTotal();

  const actions = document.createElement("div");
  actions.className = "confirm-actions";
  const cancelBtn = document.createElement("button");
  cancelBtn.type = "button";
  cancelBtn.className = "acc-action-btn secondary";
  cancelBtn.textContent = "Cancelar";
  cancelBtn.onclick = closeCommandModal;
  const applyBtn = document.createElement("button");
  applyBtn.type = "button";
  applyBtn.className = "acc-action-btn";
  applyBtn.textContent = "Borrar";
  applyBtn.onclick = async () => {
    const ids = rows.filter((r) => r.deletable && !keep.get(r.id)).map((r) => r.id);
    if (ids.length === 0) {
      closeCommandModal();
      return;
    }
    applyBtn.disabled = true;
    applyBtn.textContent = "Borrando…";
    try {
      const resp = await apiFetchOk(`${API_BASE}/api/sessions/${encodeURIComponent(state.sessionId)}/attachments/cleanup`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ delete: ids }),
      });
      const result = await resp.json();
      closeCommandModal();
      appendMessage("system-note", `Adjuntos limpiados: se liberaron ${formatFileSize(result.freed_bytes)}.`, { persist: false });
    } catch (err) {
      applyBtn.disabled = false;
      applyBtn.textContent = "Borrar";
      total.textContent = `No se pudo borrar. ${friendlyErrorMessage(err)}`;
    }
  };
  actions.append(cancelBtn, applyBtn);
  commandModalBodyEl.appendChild(actions);
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
          // spec aviso-contexto-largo REQ-006: resume con el modelo elegido en el selector (sin cuerpo = el de siempre).
          const compactModel = selectedModelForRequest();
          const resp = await apiFetch(
            `${API_BASE}/api/commands/compact/${state.sessionId}`,
            compactModel
              ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ model: compactModel }) }
              : { method: "POST" }
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
      if (command.action === "update") {
        // spec comando-update-sesiones-publicas REQ-010: resume lo NUEVO de las conversaciones publicas de otras personas (con su CSID) y lista sus
        // archivos. Las notas ya quedaron guardadas en SQLite: se repinta el chat desde ahi para verlas. Una falla se dice con una frase, nunca muda.
        const updateSessionId = state.sessionId;
        appendMessage("system-note", "Buscando novedades en las conversaciones públicas de esta carpeta…", { persist: false });
        try {
          const resp = await apiFetch(`${API_BASE}/api/commands/update/${encodeURIComponent(updateSessionId)}`, { method: "POST" });
          const data = await resp.json().catch(() => ({}));
          if (!resp.ok) throw httpError(resp.status, data);
          if (updateSessionId !== state.sessionId) return; // cambio de sesion mientras tanto: no se pinta sobre la nueva
          if (data.updated > 0) await activateSession(updateSessionId); // trae las notas nuevas desde SQLite
          appendMessage("system-note", data.message || "Listo.", { persist: false });
          refreshContextUsage();
        } catch (err) {
          if (updateSessionId !== state.sessionId) return;
          appendMessage("system-note", `No pude traer las novedades. ${friendlyErrorMessage(err)}`, { persist: false });
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

  if (command.kind === "attachments-cleanup") {
    // spec adjuntar-archivos-chat REQ-009: vista previa con "Conservar" y recien despues borra.
    renderAttachmentsCleanupModal();
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
    document.createTextNode(" Modo auto (ULTRON decide las opciones recomendadas, sin detenerse)")
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
// resetea el DOM, el historial en pantalla (y la copia chica de una sesion sin carpeta) y la cola.
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
    // Reset del estado visible: DOM, historial en pantalla (y su copia chica, si la sesion no tiene carpeta) y cola de turnos.
    clearMessagesDom();
    chatHistory.length = 0;
    removeLocalCopy(state.sessionId);
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
    // REQ-018 (spec fix-detener-y-progreso-visible): omitida por el usuario, no vuelve a aparecer al regresar a la sesion ni la marca.
    if (questionPanelEl.dataset.sessionId === state.sessionId) resolvePendingQuestion(state.sessionId);
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
  refreshContextUsage(); // REQ-017: tambien con el acordeon «Contexto» cerrado (y si el turno termino detenido o con error)
};

renderSessionsPanel();
refreshSessionsFromBackend();
refreshProjectFooter();
refreshModelFooter();

// REQ-016 (spec fix-detener-y-progreso-visible): el chat de la sesion activa sale de SQLite (ver restoreActiveSessionOnLoad); recien
// despues se ordenan, una sola vez, las copias viejas del navegador (la restauracion todavia puede necesitar la copia de una
// sesion sin carpeta como red de seguridad). REQ-017: el % real de «Contexto» desde que abre la pagina, aunque este cerrado.
const _initialHistoryRestore = restoreActiveSessionOnLoad();
(async () => {
  await _initialHistoryRestore;
  await migrateLocalCopiesOnce();
})();
refreshContextUsage();

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
  // REQ-016/REQ-018 (spec fix-detener-y-progreso-visible, revision F2): si el usuario activa otra sesion (o va y vuelve) mientras la
  // carga espera, esto ya no le corresponde: antes se enganchaba al turno de la sesion de la carga como si fuera de la nueva (su texto
  // aparecia en el chat de la otra, se guardaba en SU copia y su boton quedaba en «Detener»). Al volver, activateSession se re-engancha.
  const loadActivation = _sessionActivations;
  const stillOnLoadSession = () => _sessionActivations === loadActivation && state.sessionId === loadSessionId;
  // REQ-016: primero el historial (de SQLite): el turno en curso se engancha debajo de su propio mensaje (turnContainers), y el
  // aviso de «sesión perdida» de abajo mira si hay historial.
  await _initialHistoryRestore;
  if (!stillOnLoadSession()) return;
  try {
    const statusResp = await apiFetch(`${API_BASE}/api/session/${loadSessionId}/status`);
    const statusData = await statusResp.json();
    if (!stillOnLoadSession()) return;
    // spec proyecto-por-sesion REQ-006: rellena ID/ruta al cargar la
    // pagina (F5), no solo al crear/cambiar de sesion.
    updateMetaSessionId(state.sessionId);
    paintSessionPathFromStatus(statusData);
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
    rememberSessionFolder(loadSessionId, statusData.persistence_available); // REQ-016: decide si le corresponde copia chica
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
        setConnStatus("busy", "ULTRON sigue pensando en esta sesión…");
        const resp = await apiFetch(`${API_BASE}/api/chat/stream/${loadSessionId}`);
        // Revision F2: el stream es de la sesion de la carga aunque el usuario cambie mientras llega (sin el tercer argumento quedaba
        // atribuido a la sesion activa de ese momento).
        await renderIncomingStream(resp, statusData.active_turn_id, loadSessionId);
        if (connStatusEl.dataset.state === "busy") setConnStatus("online", "ULTRON en línea");
        touchCurrentSessionInIndex(loadSessionId);
      } finally {
        state.sending -= 1;
        if (state.sending < 0) state.sending = 0;
      }
        settleSendButtonAfterAttach(loadSessionId);
    }
    // REQ-006: si `exists` viene false, esta sesion tiene historial
    // persistido en localStorage que el backend (probablemente reiniciado)
    // ya no reconoce. Se avisa una sola vez, con estilo distinto a un
    // error comun.
    if (statusData.exists === false && chatHistory.length > 0 && stillOnLoadSession()) {
      appendMessage(
        "system-orphan",
        "⚠️ Esta sesión se perdió (el servidor se reinició). Se preserva tu historial visual, pero ULTRON no recuerda el contexto anterior — el próximo mensaje arranca una conversación nueva del lado del servidor.",
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
  // REQ-016: hasta que el historial de la carga (SQLite) este pintado, el reenganche lo hace reattachIfBusyOnLoad.
  if (state.sending > 0 || _idleReattachInFlight || !_historyRestoreDone) return;
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
      setConnStatus("busy", "ULTRON sigue pensando en esta sesión…");
      resetToolRow();
      const resp = await apiFetch(`${API_BASE}/api/chat/stream/${sessionId}`);
      await renderIncomingStream(resp, statusData.active_turn_id, sessionId);
      if (connStatusEl.dataset.state === "busy") setConnStatus("online", "ULTRON en línea");
      touchCurrentSessionInIndex(sessionId);
    } finally {
      state.sending -= 1;
      if (state.sending < 0) state.sending = 0;
    }
    settleSendButtonAfterAttach(sessionId);
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
    sendOrStopBtnEl.title = "Detener a ULTRON";
    sendOrStopLabelEl.textContent = "Detener";
  } else {
    sendOrStopBtnEl.title = "Deteniendo… esperando que termine la herramienta en curso";
    sendOrStopLabelEl.textContent = "Deteniendo…";
  }
}

// Un turno al que el frontend se ENGANCHA (edicion de un mensaje ya respondido, recarga de pagina a mitad de turno, cambio de sesion,
// poll de fondo) no pasa por el `finally` de sendMessage(), que es lo unico que devolvia el boton a "Enviar": quedaba pegado en
// "Detener" hasta recargar, y con el guard de submit el usuario no podia mandar otro mensaje. Se llama al terminar el stream enganchado.
// No toca nada si hay otro envio en curso (state.sending > 0) ni si el stream era de una sesion que ya no es la activa (el boton es global).
function settleSendButtonAfterAttach(attachedSessionId) {
  if (state.sending > 0) return;
  if (attachedSessionId !== state.sessionId) return;
  setSendButtonState("idle");
}

// Detener TAJANTE (pedido del usuario 2026-10-05, «se queda pegado sin detener»): el boton no puede quedarse en «Deteniendo…» esperando UN evento SSE
// que puede no llegar (stream cortado, evento perdido, POST /stop que fallo o no llego al servidor). Mientras el boton diga «Deteniendo…» se
// consulta el estado real de la sesion cada segundo: si ya no esta ocupada, vuelve a «Enviar»; si sigue ocupada, se REINTENTA el Detener. El
// backend ya no espera a las herramientas lentas mas alla de su plazo de gracia (STOP_GRACE_S de llm/loop.py).
// REQ-007 (spec fix-detener-y-progreso-visible): lo que vivio el usuario (2026-10-11) fue un Vision de desarrollo que se reinicio a mitad del
// turno: el proceso viejo seguia con el turno pero ya no atendia conexiones, y el POST de Detener quedo colgado SIN respuesta. Como ese fetch
// no tenia plazo y el vigilante arrancaba recien cuando el POST contestaba, el boton quedo en «Deteniendo…» sin limite y sin explicar nada.
// Ahora el POST tiene plazo (5 s) y cada consulta del vigilante el suyo (3 s); si Vision no contesta se avisa UNA vez en lenguaje claro y se
// sigue consultando y reintentando el Detener cada ~5 s (antes: una sola vez); el boton vuelve a «Enviar» cuando la sesion queda libre o
// Vision vuelve sin el turno (con «Listo…» si se habia avisado), y a los ~30 s sin contacto se sugiere recargar y se suelta el boton.
const STOP_REQUEST_TIMEOUT_MS = 5000;
const STOP_STATUS_TIMEOUT_MS = 3000;
const STOP_WATCH_INTERVAL_MS = 1000;
const STOP_WATCH_RETRY_MS = 5000;
const STOP_UNREACHABLE_RELEASE_MS = 30_000;
// REQ-007: cuando el boton ya volvio por otro camino pero se habia avisado «no responde», se sigue esperando a Vision solo para cerrar ese
// aviso con «Listo…»; con un tope, para no consultar para siempre a un Vision que se cerro.
const STOP_CONFIRM_MAX_MS = 120_000;
const STOP_UNREACHABLE_NOTE = "ULTRON no responde todavía: puede estar reiniciándose. Sigo intentando…";
const STOP_RECOVERED_NOTE = "Listo: ULTRON ya no está trabajando.";
const STOP_RELOAD_NOTE =
  "ULTRON sigue sin responder después de 30 segundos. Si no vuelve en un rato, recargá la página con Ctrl+Shift+R; si tampoco carga, cerrá y volvé a abrir ULTRON.";
// REQ-014 f (spec fix-detener-y-progreso-visible)
const STOP_OTHER_TURN_NOTE =
  "Esa respuesta ya había terminado y ULTRON ya está con el siguiente mensaje. Si querés frenarlo también, apretá Detener de nuevo.";

// REQ-007: el vigilante vigente. Uno solo: un Detener nuevo, un cambio de sesion o el fin del vigilante dejan sin efecto al anterior.
let _stopWatch = null;

// REQ-007: fetch con plazo para lo que no puede quedar colgado (Detener y su vigilante). Un Vision que se esta reiniciando acepta la conexion
// pero no contesta nunca: sin plazo, el `await` espera para siempre. El cuerpo tambien se lee dentro del plazo (puede no llegar nunca).
// Lanza si no hubo respuesta a tiempo o si fallo la red; una respuesta no-ok vuelve con `ok: false` (Vision contesto, aunque sea un error).
async function apiFetchJsonWithin(url, timeoutMs, options = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const resp = await apiFetch(url, { ...options, signal: controller.signal });
    let data = null;
    try {
      data = await resp.json();
    } catch (err) {
      if (err && err.name === "AbortError") throw err;
      // cuerpo vacio o ilegible: alcanza con saber que Vision contesto
    }
    return { ok: resp.ok, data };
  } finally {
    clearTimeout(timer);
  }
}

// REQ-007: "nothing" = Vision contesto que no habia turno que detener; "sent" = contesto (pedido recibido, o un error: el vigilante decide
// mirando el estado); "no-answer" = sin respuesta en el plazo o sin red. En ese ultimo caso el pedido PUDO haber llegado al servidor.
// REQ-014 f (spec fix-detener-y-progreso-visible): `turnId` = el turno que el usuario estaba viendo (null = el que este corriendo, como
// siempre). "other-turn" = ese turno ya habia terminado y Vision esta con OTRO (el siguiente de la cola): no se lo freno, y el proximo
// Detener ya apunta a ese otro.
async function postStopRequest(sessionId, turnId = null) {
  try {
    const options = { method: "POST" };
    if (turnId) {
      options.headers = { "Content-Type": "application/json" };
      options.body = JSON.stringify({ turn_id: turnId });
    }
    const { ok, data: body } = await apiFetchJsonWithin(`${API_BASE}/api/chat/${sessionId}/stop`, STOP_REQUEST_TIMEOUT_MS, options);
    const data = ok && body ? body : {};
    if (data.stopped === false && data.reason === "other_turn_active") {
      noteWatchedTurn(sessionId, data.active_turn_id);
      return "other-turn";
    }
    // `stopped: false` = no habia turno: no va a llegar ningun evento 'stopped' (fix del boton colgado, ver el click de Detener).
    if (data.stopped === false) return "nothing";
    return "sent";
  } catch (err) {
    console.warn("El pedido de Detener no tuvo respuesta:", err);
    return "no-answer";
  }
}

// REQ-007: estado de la sesion para el vigilante, o null si Vision no contesto a tiempo o contesto algo que no sirve para decidir.
async function fetchStopWatchStatus(sessionId) {
  try {
    const { ok, data } = await apiFetchJsonWithin(
      `${API_BASE}/api/session/${encodeURIComponent(sessionId)}/status`,
      STOP_STATUS_TIMEOUT_MS,
    );
    return ok && data && typeof data.busy === "boolean" ? data : null;
  } catch {
    return null; // sin respuesta: el vigilante lo cuenta como «Vision no responde»
  }
}

// REQ-007: un Detener nuevo sobre la MISMA sesion mientras el anterior seguia vigilando (p. ej. el usuario lo vuelve a apretar despues de que
// se solto a los 30 s) hereda lo ya avisado: la nota de «no responde» sale una sola vez y el «Listo…» final sigue cerrandola.
function startStopWatch(sessionId, targetTurnId = null) {
  const prev = _stopWatch && _stopWatch.sessionId === sessionId ? _stopWatch : null;
  const now = Date.now();
  _stopWatch = {
    sessionId,
    startedAt: now,
    lastStopAt: now, // el POST del click cuenta como el primer intento
    stopInFlight: false,
    unreachableSince: null, // desde cuando no se puede hablar con Vision (null = hubo contacto)
    unreachableNoted: prev ? prev.unreachableNoted : false,
    reloadNoted: prev ? prev.reloadNoted : false,
    // REQ-014 f: el turno que el usuario estaba viendo al apretar Detener (va en cada reintento, asi tampoco un reintento frena al
    // siguiente de la cola). null = no se sabia: los reintentos van sin id, como siempre.
    targetTurnId: targetTurnId || (prev ? prev.targetTurnId : null),
    // el turno que se esta deteniendo: el que se apunto, o si no se sabia, el del primer estado «ocupada» que se vio
    turnId: targetTurnId || (prev ? prev.turnId : null),
  };
  return _stopWatch;
}

function endStopWatch(watch) {
  if (_stopWatch === watch) _stopWatch = null;
}

function noteStopUnreachable(watch) {
  if (watch.unreachableNoted) return;
  watch.unreachableNoted = true;
  appendMessage("system-note", STOP_UNREACHABLE_NOTE, { persist: false });
}

// REQ-007: reintento del Detener cada ~STOP_WATCH_RETRY_MS mientras la sesion siga ocupada (o sin respuesta). Sin solapar pedidos: cada uno
// tiene su plazo y el siguiente sale recien cuando el anterior termino (contesto, fallo o vencio).
function retryStopIfDue(watch) {
  if (watch.stopInFlight || Date.now() - watch.lastStopAt < STOP_WATCH_RETRY_MS) return;
  watch.stopInFlight = true;
  watch.lastStopAt = Date.now();
  postStopRequest(watch.sessionId, watch.targetTurnId).finally(() => {
    watch.stopInFlight = false;
  });
}

async function watchStopUntilIdle(watch) {
  const { sessionId } = watch;
  const current = () => _stopWatch === watch && sessionId === state.sessionId;
  let confirmingSince = 0; // cuando el boton dejo de decir «Deteniendo…» (0 = todavia lo dice)
  try {
    while (current()) {
      await new Promise((resolve) => setTimeout(resolve, STOP_WATCH_INTERVAL_MS));
      if (!current()) return;
      if (sendOrStopBtnEl.dataset.state !== "stopping") {
        // El boton ya volvio por otro camino (evento 'stopped'/'done', fin del stream, o se solto a los 30 s). Si nunca se aviso que Vision no
        // respondia no queda nada que hacer; si se aviso, se espera a poder confirmarlo para cerrar ese aviso con «Listo…».
        if (!watch.unreachableNoted) return;
        if (!confirmingSince) confirmingSince = Date.now();
        if (Date.now() - confirmingSince > STOP_CONFIRM_MAX_MS) return;
      }
      const status = await fetchStopWatchStatus(sessionId);
      if (!current()) return;
      // El estado del boton se vuelve a leer DESPUES de la consulta: un evento 'stopped'/'done' pudo llegar mientras tanto.
      const stopping = sendOrStopBtnEl.dataset.state === "stopping";
      if (status === null) {
        if (!stopping) continue; // esperando a Vision solo para el «Listo…»
        if (watch.unreachableSince === null) watch.unreachableSince = Date.now();
        noteStopUnreachable(watch);
        retryStopIfDue(watch); // «Sigo intentando…»: si Vision vuelve con el turno vivo, el pedido llega
        if (Date.now() - watch.unreachableSince >= STOP_UNREACHABLE_RELEASE_MS) {
          // Nunca «Deteniendo…» sin limite. Si esta pagina sigue recibiendo el turno (su stream abierto), el boton vuelve a «Detener» para
          // poder intentarlo de nuevo; si no, a «Enviar». El vigilante sigue esperando a Vision para el «Listo…».
          if (!watch.reloadNoted) {
            watch.reloadNoted = true;
            appendMessage("system-note", STOP_RELOAD_NOTE, { persist: false });
          }
          setSendButtonState(state.sending > 0 ? "active" : "idle");
        }
        continue;
      }
      watch.unreachableSince = null;
      if (!status.busy) {
        // La sesion quedo libre (o Vision volvio de un reinicio sin el turno): fin del Detener.
        if (stopping) setSendButtonState("idle");
        if (watch.unreachableNoted) appendMessage("system-note", STOP_RECOVERED_NOTE, { persist: false });
        return;
      }
      // Ocupada. `active_turn_id` puede venir vacio un instante mientras el turno se cierra: eso no cambia nada.
      const turnId = status.active_turn_id || null;
      if (stopping && watch.turnId === null) watch.turnId = turnId;
      const otherTurn = Boolean(turnId && watch.turnId && turnId !== watch.turnId);
      if (otherTurn || (!stopping && !watch.turnId)) {
        // Ocupada con OTRO turno (el siguiente de la cola, o un mensaje nuevo): ese no se frena. Si el boton seguia esperando, pasa a
        // «Detener» del turno en curso (su stream lo va a seguir actualizando). Si solo se esperaba para el «Listo…» y no se sabe que turno
        // es, tambien termina: Vision volvio y esta trabajando, no hay nada que confirmar.
        if (stopping) setSendButtonState("active");
        return;
      }
      if (stopping) retryStopIfDue(watch); // sigue ocupada con el MISMO turno: se reintenta (no una sola vez)
    }
  } finally {
    endStopWatch(watch);
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

  const sessionId = state.sessionId;
  setSendButtonState("stopping");
  // REQ-014 f: se frena el turno que el usuario esta viendo; si justo termino y arranco el siguiente de la cola, ese no se frena.
  const targetTurnId = watchedTurnIdFor(sessionId);
  const watch = startStopWatch(sessionId, targetTurnId);
  const result = await postStopRequest(sessionId, targetTurnId);
  // Mientras se esperaba la respuesta pudo pasar de todo: otro Detener o un cambio de sesion (manda ese otro vigilante, o ninguno)...
  if (_stopWatch !== watch || sessionId !== state.sessionId) {
    endStopWatch(watch);
    return;
  }
  // ...o el turno termino solo (el evento 'stopped'/'done' o el fin del stream ya devolvieron el boton): no hay nada que avisar. Solo se sigue
  // esperando si un Detener anterior ya habia avisado «no responde», para cerrar ese aviso con «Listo…».
  if (sendOrStopBtnEl.dataset.state !== "stopping") {
    if (watch.unreachableNoted) watchStopUntilIdle(watch);
    else endStopWatch(watch);
    return;
  }
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
  if (result === "nothing") {
    setSendButtonState("idle");
    // REQ-007: si un Detener anterior habia avisado «no responde», Vision ya contesto y no tiene turno: se cierra ese aviso.
    if (watch.unreachableNoted) appendMessage("system-note", STOP_RECOVERED_NOTE, { persist: false });
    endStopWatch(watch);
    return;
  }
  // REQ-014 f: el turno que el usuario miraba ya habia terminado y Vision esta con otro (el siguiente de la cola): ese no se frena. Se
  // avisa, y el boton queda en «Detener» de ese otro turno si esta pagina lo esta recibiendo (el proximo Detener ya apunta a el).
  if (result === "other-turn") {
    appendMessage("system-note", STOP_OTHER_TURN_NOTE, { persist: false });
    setSendButtonState(state.sending > 0 ? "active" : "idle");
    endStopWatch(watch);
    return;
  }
  // REQ-007: sin respuesta (plazo vencido o sin red) el boton NO vuelve a «Detener» como antes: el pedido pudo haber llegado y el turno
  // puede estar cortandose. Se avisa una vez y el vigilante sigue consultando y reintentando.
  if (result === "no-answer") {
    watch.unreachableSince = watch.startedAt;
    noteStopUnreachable(watch);
  }
  // Si `stopped: true`, el boton vuelve a "idle" cuando llega el evento 'stopped' (ver handleEvent) -- el backend puede tardar en cerrar
  // limpio la tool en curso (REQ-001: "esperar cierre limpio") --, pero no se queda esperando SOLO ese evento: ver watchStopUntilIdle.
  watchStopUntilIdle(watch);
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
  const v = readPreference(key);
  return v === null ? def : v;
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
      setConnStatus("busy", "ULTRON está trabajando…");
      const streamResp = await apiFetch(`${API_BASE}/api/chat/stream/${state.sessionId}`);
      await renderIncomingStream(streamResp);
      if (connStatusEl.dataset.state === "busy") setConnStatus("online", "ULTRON en línea");
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
  // REQ-014 b (spec fix-detener-y-progreso-visible): si su turno ya arranco (el modal seguia abierto cuando le toco), el backend lo frena
  // y lo descarta; lo que llegue de ese turno ya no se guarda ni se lee (ver closeCurrentAssistantTurn).
  _deletedTurnIds.add(messageId);

  const wrap = findUserTurnWrap(messageId);
  const container = wrap ? wrap.closest(".turn-container") : null;
  if (container) container.remove();

  // Poda local: la pregunta + su respuesta (y tools) hasta el siguiente user.
  const idx = chatHistory.findIndex((m) => m.messageId === messageId);
  if (idx !== -1) {
    let endIdx = idx + 1;
    while (endIdx < chatHistory.length && chatHistory[endIdx].role !== "user") endIdx += 1;
    // REQ-014 b (spec fix-detener-y-progreso-visible): un mensaje mandado mientras Vision trabajaba queda guardado ANTES de la respuesta
    // del turno que seguia corriendo; si se borra despues de que arranco, esa respuesta (de OTRO turno, con su propio messageId) se
    // conserva. Antes se iba con la poda y, al recargar, faltaba la respuesta anterior.
    for (let i = endIdx - 1; i > idx; i -= 1) {
      const entry = chatHistory[i];
      if (entry.messageId && entry.messageId !== messageId) continue;
      chatHistory.splice(i, 1);
    }
    chatHistory.splice(idx, 1);
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

  // adjuntar-archivos-chat REQ-003 e: volver a generar la respuesta reenvia TAMBIEN los archivos del
  // mensaje (antes solo viajaba el texto y las imagenes, y el agente perdia las rutas). `userMsg.attachments`
  // sale de chatHistory, que los guarda y los recupera tras recargar la pagina.
  const resentFiles = Array.isArray(userMsg.attachments) ? userMsg.attachments.filter((f) => f && f.path) : [];
  await sendMessage(userMsg.text, {
    messageId: crypto.randomUUID(),
    images: userMsg.images,
    attachments: resentFiles.length > 0
      ? resentFiles.map((f) => ({ path: f.path, name: f.name, size: f.size, mime: f.mime, inplace: !!f.inplace }))
      : null,
    sentFiles: resentFiles.length > 0 ? resentFiles : null,
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
    done.textContent = "✓ ULTRON volvió a responder. Ya podés seguir.";
  } else if (alive) {
    msgEl.classList.remove("msg-solved");
    done.textContent = `${rawText}\nULTRON está funcionando: volvé a enviar tu mensaje para reintentar.`;
  } else {
    msgEl.classList.add("msg-error");
    msgEl.classList.remove("msg-solved");
    done.textContent = `${rawText}\nTodavía no hay conexión con ULTRON. Esperá unos segundos y probá de nuevo con ↻.`;
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
    appendMessage(entry.role, entry.text, { persist: false, messageId: entry.messageId, time: entry.time, responseType: entry.responseType || null, technical: entry.technical || null, errorKind: entry.errorKind || null, attachments: entry.attachments || null });
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
  let attachedSessionId = null;
  try {
    setConnStatus("busy", "ULTRON está trabajando…");
    resetToolRow();
    attachedSessionId = state.sessionId;
    // Detener tajante: apenas se relanza el turno el boton tiene que decir «Detener». Antes quedaba en «Enviar» hasta el PRIMER evento del
    // modelo (que puede tardar mucho): en ese lapso Vision estaba trabajando y el usuario no podia frenarlo. Igual que hace sendMessage().
    if (sendOrStopBtnEl.dataset.state === "idle") setSendButtonState("active");
    const resp = await apiFetch(`${API_BASE}/api/chat/stream/${state.sessionId}`);
    await renderIncomingStream(resp, messageId);
  } finally {
    if (connStatusEl.dataset.state === "busy") setConnStatus("online", "ULTRON en línea");
    settleSendButtonAfterAttach(attachedSessionId);
  }
}
