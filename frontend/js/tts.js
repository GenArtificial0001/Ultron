// REQ-007/008/009/010 (spec narracion-agente-efimera, "Interaccion por voz"):
// TTS de las respuestas de Jarvis y narracion hablada de progreso via Web
// Speech API (`SpeechSynthesisUtterance`) -- nativa del navegador, sin
// costo ni tramite, procesamiento 100% local. Opcional (spec voz-fish-audio):
// motores del backend (Fish Audio, ElevenLabs, clonacion local), apagados de
// fabrica; los de la nube solo con el consentimiento del usuario.
//
// Modulo standalone (sin dependencias de app.js) para poder testear/
// reusar la seleccion de voz desde la pestaña de Configuracion > Audio
// (REQ-009/010) sin acoplarse al resto del chat.

const TTS_ENABLED_KEY = "vision.tts.enabled";
const TTS_VOICE_KEY = "vision.tts.voiceURI";

// REQ-016 (spec fix-detener-y-progreso-visible): con el navegador lleno o bloqueado `setItem` tira y antes se tragaba en silencio:
// «Leer en voz alta» quedaba tildado y NO leia (se relee de localStorage en cada respuesta). Ahora el valor que no se pudo guardar
// vale en memoria hasta recargar, y se avisa con el mismo evento que usa app.js para sus preferencias (un aviso por carga, lo
// muestra app.js): este modulo sigue sin depender de app.js.
const PREF_NOT_SAVED_EVENT = "vision:preference-not-saved";
const _unsavedPrefs = new Map();

function readPref(key) {
  if (_unsavedPrefs.has(key)) return _unsavedPrefs.get(key);
  try {
    return localStorage.getItem(key);
  } catch {
    return null; // localStorage inaccesible: cada preferencia cae a su valor por defecto
  }
}

function writePref(key, value) {
  try {
    localStorage.setItem(key, value);
    _unsavedPrefs.delete(key);
  } catch {
    _unsavedPrefs.set(key, value);
    window.dispatchEvent(new CustomEvent(PREF_NOT_SAVED_EVENT, { detail: { key } }));
  }
}

// -------------------------------------------------------------------------
// Preferencias persistidas (localStorage, mismo patron que THEME_KEY en
// app.js) -- TTS arranca DESACTIVADO por defecto: es una feature nueva que
// habla en voz alta sin que el usuario lo haya pedido todavia, activarla
// silenciosamente por default seria una sorpresa indeseada la primera vez
// que alguien abre Vision despues de este cambio.
// -------------------------------------------------------------------------
export function isTtsEnabled() {
  return readPref(TTS_ENABLED_KEY) === "true";
}

// spec informe-tecnico-hablado REQ-001: "Hablar informe tecnico", PRENDIDO de
// fabrica (decision del usuario). Solo suena si "Leer en voz alta" tambien
// esta activo (que sigue apagado de fabrica).
const TECH_REPORT_KEY = "vision.tts.techReport";

export function isTechReportEnabled() {
  return readPref(TECH_REPORT_KEY) !== "false";
}

export function setTechReportEnabled(enabled) {
  writePref(TECH_REPORT_KEY, enabled ? "true" : "false"); // sin localStorage: vale hasta recargar (REQ-016)
}

// REQ-012 (informe-tecnico-hablado, reapertura 2): nivel del informe. "detailed" de fabrica (decision
// delegada del usuario, revisable): suma estructura de pagina, estilos y funciones. "summary" es el
// informe de siempre. Cualquier otro valor guardado cuenta como "detailed" solo si es exactamente ese.
const TECH_LEVEL_KEY = "vision.tts.techReportLevel";

export function getTechReportLevel() {
  return readPref(TECH_LEVEL_KEY) === "summary" ? "summary" : "detailed";
}

export function setTechReportLevel(level) {
  writePref(TECH_LEVEL_KEY, level === "summary" ? "summary" : "detailed"); // sin localStorage: vale hasta recargar (REQ-016)
}

export function setTtsEnabled(enabled) {
  // Sin localStorage la preferencia no persiste entre recargas, pero sigue aplicada en esta sesion (REQ-016).
  writePref(TTS_ENABLED_KEY, enabled ? "true" : "false");
  if (!enabled) cancelSpeech(); // apagar TTS a mitad de una frase la corta al instante, no la deja terminar
}

function getSavedVoiceURI() {
  return readPref(TTS_VOICE_KEY) || "";
}

export function setSavedVoiceURI(voiceURI) {
  writePref(TTS_VOICE_KEY, voiceURI || ""); // sin localStorage: vale hasta recargar (REQ-016)
}

// -------------------------------------------------------------------------
// Seleccion de voz: el usuario pidio explicitamente "español latino y voz
// de hombre si es posible" (ver Decisiones registradas en spec.md) --
// heuristica de mejor esfuerzo sobre `SpeechSynthesisVoice.name`/`lang`,
// ya que la Web Speech API no expone genero como campo propio (varia por
// SO/navegador que nombres usa cada voz).
// -------------------------------------------------------------------------
const MALE_NAME_HINTS = [
  "male", "hombre", "diego", "jorge", "carlos", "juan", "pablo", "raul",
  "miguel", "andres", "ricardo", "fernando", "esteban", "sergio",
];
const FEMALE_NAME_HINTS = [
  "female", "mujer", "paulina", "monica", "sabina", "maria", "lucia",
  "laura", "sofia", "helena", "camila", "valentina",
];

function scoreVoiceForSpanishMale(voice) {
  const lang = (voice.lang || "").toLowerCase();
  const name = (voice.name || "").toLowerCase();
  let score = 0;

  if (lang.startsWith("es-")) score += 10;
  else if (lang.startsWith("es")) score += 5;
  else return -1; // no es español -- descartada por completo, sin importar el resto

  // Variantes latinoamericanas explicitas por encima de es-ES (pedido
  // "español latino"), pero cualquier es-* sigue siendo mejor que nada.
  if (/es-(mx|ar|co|cl|pe|ve|uy|py|bo|ec|gt|cr|pa|do|hn|ni|sv|us)/.test(lang)) score += 5;

  if (MALE_NAME_HINTS.some((hint) => name.includes(hint))) score += 8;
  if (FEMALE_NAME_HINTS.some((hint) => name.includes(hint))) score -= 8;

  // Motores locales (no remotos) responden mas rapido y no dependen de
  // conexion -- pequeño empujon adicional en el desempate.
  if (voice.localService) score += 2;

  return score;
}

export function getAvailableSpanishVoices() {
  if (!("speechSynthesis" in window)) return [];
  return window.speechSynthesis
    .getVoices()
    .filter((v) => (v.lang || "").toLowerCase().startsWith("es"));
}

export function pickBestSpanishMaleVoice() {
  const voices = window.speechSynthesis ? window.speechSynthesis.getVoices() : [];
  if (voices.length === 0) return null;

  const savedURI = getSavedVoiceURI();
  if (savedURI) {
    const saved = voices.find((v) => v.voiceURI === savedURI);
    if (saved) return saved; // eleccion explicita del usuario en Configuracion > Audio siempre gana
  }

  let best = null;
  let bestScore = -1;
  for (const voice of voices) {
    const score = scoreVoiceForSpanishMale(voice);
    if (score > bestScore) {
      bestScore = score;
      best = voice;
    }
  }
  return bestScore >= 0 ? best : null; // -1 significa "ninguna voz en español disponible"
}

// -------------------------------------------------------------------------
// Cola de habla: SpeechSynthesis reproduce una utterance a la vez -- si
// llegan varias narraciones de progreso mientras la anterior sigue
// sonando (ej. tool-calls rapidas en cadena), se encolan en vez de
// pisarse/solaparse. `speak()` es la unica entrada publica de esta cola.
// -------------------------------------------------------------------------
let _speechQueue = []; // items { text, onStart, limitKey }
let _speaking = false;
// Voz que esta sonando AHORA. Bug real (2026-09-27, spec fix-boton-play-stop):
// cancel() de la Web Speech API avisa el corte de la voz vieja DESPUES, con un
// evento asincrono ("interrupted"/"canceled"). Si para entonces ya habia
// arrancado otra voz (el usuario toco 🔊 de nuevo), ese aviso viejo la daba
// por terminada: soltaba el boton (🔊 con el audio sonando) y el proximo toque
// arrancaba la lectura OTRA VEZ desde el principio en vez de detenerla. Los
// eventos de una voz que ya no es la actual se ignoran.
let _currentUtter = null;

// REQ-002 (spec fix-tts-respuestas-silenciosas, 2026-09-27): watchdog. Si el
// onend/onerror de una utterance NUNCA llega (bug conocido de Chrome/Edge:
// la utterance muere al cambiar el dispositivo de audio del sistema, ej.
// conectar un Bluetooth), _speaking quedaria trabado en true para siempre y
// todas las respuestas siguientes se descartarian en _pump() sin sonar. Este
// timer forzar fin() pasado un margen amplio segun el largo del texto.
let _speakWatchdogTimer = null;
const SPEAK_WATCHDOG_MIN_MS = 5000; // colchon fijo (arranque lento del motor, primer utterance)
const SPEAK_WATCHDOG_PER_CHAR_MS = 120; // ~120ms por caracter, generoso (una voz real suele ser mas rapida)

function _clearSpeakWatchdog() {
  if (_speakWatchdogTimer) {
    clearTimeout(_speakWatchdogTimer);
    _speakWatchdogTimer = null;
  }
}

// REQ-001 (spec fix-tts-respuestas-silenciosas): auto-recuperacion de la cola
// trabada. El sintoma real reportado por el usuario fue: "Probar voz" (que
// llama directo a speechSynthesis.speak() y NUNCA toca _speaking) suena, pero
// las respuestas (que pasan por _pump) no. Causa: _speaking quedo true por una
// utterance que murio sin onend. Si el motor nativo NO esta hablando ni tiene
// nada pendiente, no puede haber una voz legítima en curso: la cola esta
// trabada y se libera.
function _recoverStuckQueueIfNeeded() {
  if (!("speechSynthesis" in window)) return;
  // spec voz-fish-audio: con motores del backend, `_speaking` es true mientras suena
  // AUDIO propio (Web Audio) y el motor nativo no habla -- eso NO es una cola trabada.
  // Sin este chequeo se la liberaria en falso y dos frases se pisarian.
  if (_usesAudioEngines()) return;
  if (_speaking && !window.speechSynthesis.speaking && !window.speechSynthesis.pending) {
    _speaking = false;
    _currentUtter = null;
    _clearSpeakWatchdog();
  }
}

// -------------------------------------------------------------------------
// Mute de voz (spec panel-flotante-control-rapido REQ-002, evolucionado a
// TOGGLE por pedido del usuario 2026-09-12): a diferencia de isTtsEnabled()
// (preferencia PERSISTENTE de Configuracion > Audio), este mute es un
// estado de SESION en memoria -- no persiste entre recargas. Cuando esta
// activo: corta cualquier voz en curso (cancel) y bloquea todos los
// speak() futuros, hasta que se desactive. Nunca toca la preferencia
// permanente; es independiente del toggle de auto-envio.
// -------------------------------------------------------------------------
let _voiceMuted = false;

export function isVoiceMuted() {
  return _voiceMuted;
}

export function setVoiceMuted(muted) {
  _voiceMuted = muted;
  if (muted) cancelSpeech(); // activar el mute a mitad de una frase la corta al instante
}

// -------------------------------------------------------------------------
// spec voz-fish-audio: motores de voz del backend (Fish Audio, ElevenLabs,
// clonacion local) en la cadena que arma el usuario en Configuracion > Audio.
// Con la cadena vacia (como viene de fabrica, REQ-002) todo sigue igual que
// antes: voz del navegador. app.js conecta este modulo con el backend
// (`configureVoiceBackend`) porque aca no hay acceso a su fetch con token.
// -------------------------------------------------------------------------
let _voiceBackend = null; // { fetchFn, apiBase, onNotice }
let _voiceConfig = { chain: [], effect: false };
// REQ-003: cada aviso ("se quedo sin credito, sigo con...") se muestra una
// sola vez; se vuelve a mostrar recien despues de guardar la configuracion.
const _shownNotices = new Set();

export function configureVoiceBackend({ fetchFn, apiBase, onNotice }) {
  _voiceBackend = { fetchFn, apiBase, onNotice };
  refreshVoiceConfig();
}

export async function refreshVoiceConfig() {
  if (!_voiceBackend) return;
  try {
    const resp = await _voiceBackend.fetchFn(`${_voiceBackend.apiBase}/api/voice/config`);
    if (resp.ok) applyVoiceConfig(await resp.json());
  } catch {
    // Sin backend: queda la voz del navegador (nunca mudo).
  }
}

export function applyVoiceConfig(cfg) {
  _voiceConfig = { chain: Array.isArray(cfg?.chain) ? cfg.chain : [], effect: Boolean(cfg?.effect) };
  _shownNotices.clear();
}

function _usesAudioEngines() {
  return Boolean(_voiceBackend) && _voiceConfig.chain.length > 0;
}

// REQ-010: "esta hablando Vision?" para el boton ⏹, el barge-in y la pausa del
// microfono del Modo Cerebro. Antes se miraba `speechSynthesis.speaking`, que
// no ve el audio de los motores del backend.
export function isSpeaking() {
  return _speaking || Boolean(window.speechSynthesis && window.speechSynthesis.speaking);
}

// REQ-004: la respuesta se parte en frases y cada una se pide por separado:
// la primera suena apenas llega mientras se genera la siguiente. El primer
// tramo es corto (empieza a hablar antes) pero no minimo: un "Hola." suelto
// dura medio segundo y dejaria un silencio mientras se genera el segundo.
const FIRST_CHUNK_MIN = 60;
const FIRST_CHUNK_MAX = 160;
const CHUNK_MAX = 240;

function _splitLong(sentence, max) {
  const parts = [];
  let rest = sentence;
  while (rest.length > max) {
    let cut = rest.lastIndexOf(", ", max);
    if (cut < max / 3) cut = rest.lastIndexOf(" ", max);
    if (cut < max / 3) cut = max;
    parts.push(rest.slice(0, cut + 1).trim());
    rest = rest.slice(cut + 1).trim();
  }
  if (rest) parts.push(rest);
  return parts;
}

export function splitForSpeech(text) {
  const clean = (text || "").replace(/\s+/g, " ").trim();
  if (!clean) return [];
  const sentences = (clean.match(/[^.!?…]+(?:[.!?…]+["'»)\]]*|$)/g) || [clean])
    .map((s) => s.trim())
    .filter(Boolean);
  const pieces = sentences.flatMap((s) => (s.length > CHUNK_MAX ? _splitLong(s, CHUNK_MAX) : [s]));
  if (pieces.length && pieces[0].length > FIRST_CHUNK_MAX) {
    pieces.splice(0, 1, ..._splitLong(pieces[0], FIRST_CHUNK_MAX));
  }
  const chunks = [];
  let current = "";
  for (const piece of pieces) {
    const max = chunks.length === 0 ? FIRST_CHUNK_MAX : CHUNK_MAX;
    if (current && current.length + 1 + piece.length > max) {
      chunks.push(current);
      current = piece;
    } else {
      current = current ? `${current} ${piece}` : piece;
    }
    if (chunks.length === 0 && current.length >= FIRST_CHUNK_MIN) {
      chunks.push(current);
      current = "";
    }
  }
  if (current) chunks.push(current);
  return chunks;
}

// Cada cancelacion sube `_playToken`: lo que llegue despues de una frase ya
// cancelada (una respuesta del backend, el fin de un audio) se ignora.
let _playToken = 0;
const _inflight = new Set(); // AbortControllers de los pedidos en curso
let _currentSource = null;
let _playbackCtx = null;
let _effectInput = null;

function _getPlaybackCtx() {
  if (!_playbackCtx || _playbackCtx.state === "closed") {
    const AudioCtx = window.AudioContext || window.webkitAudioContext;
    _playbackCtx = new AudioCtx();
    _effectInput = null;
  }
  return _playbackCtx;
}

// Si el navegador todavia no dejo arrancar el audio (politica de autoplay),
// un AudioContext suspendido nunca avanza y la frase quedaria "sonando" muda
// para siempre: en ese caso se usa la voz del navegador.
async function _ensureRunning(ctx) {
  if (ctx.state === "running") return true;
  try {
    await Promise.race([ctx.resume(), new Promise((resolve) => setTimeout(resolve, 500))]);
  } catch {
    // noop
  }
  return ctx.state === "running";
}

// REQ-008: efecto "IA" estilo Jarvis -- filtro de graves, un poco de presencia,
// eco corto (convolucion con un impulso sintetico) y un leve toque metalico
// (delay de pocos ms con realimentacion). Se arma una vez por contexto.
function _makeImpulse(ctx, seconds, decay) {
  const length = Math.floor(ctx.sampleRate * seconds);
  const impulse = ctx.createBuffer(2, length, ctx.sampleRate);
  for (let channel = 0; channel < 2; channel++) {
    const data = impulse.getChannelData(channel);
    for (let i = 0; i < length; i++) data[i] = (Math.random() * 2 - 1) * Math.pow(1 - i / length, decay);
  }
  return impulse;
}

function _outputNode(ctx) {
  if (!_voiceConfig.effect) return ctx.destination;
  if (_effectInput) return _effectInput;
  const input = ctx.createGain();
  const highpass = ctx.createBiquadFilter();
  highpass.type = "highpass";
  highpass.frequency.value = 110;
  const presence = ctx.createBiquadFilter();
  presence.type = "peaking";
  presence.frequency.value = 3200;
  presence.Q.value = 0.9;
  presence.gain.value = 4;
  const dry = ctx.createGain();
  dry.gain.value = 0.85;
  const reverb = ctx.createConvolver();
  reverb.buffer = _makeImpulse(ctx, 0.35, 2.8);
  const reverbWet = ctx.createGain();
  reverbWet.gain.value = 0.22;
  const metal = ctx.createDelay(0.05);
  metal.delayTime.value = 0.012;
  const metalFeedback = ctx.createGain();
  metalFeedback.gain.value = 0.25;
  const metalWet = ctx.createGain();
  metalWet.gain.value = 0.15;
  input.connect(highpass);
  highpass.connect(presence);
  presence.connect(dry);
  dry.connect(ctx.destination);
  presence.connect(reverb);
  reverb.connect(reverbWet);
  reverbWet.connect(ctx.destination);
  presence.connect(metal);
  metal.connect(metalFeedback);
  metalFeedback.connect(metal);
  metal.connect(metalWet);
  metalWet.connect(ctx.destination);
  _effectInput = input;
  return input;
}

function _base64ToArrayBuffer(b64) {
  const binary = atob(b64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes.buffer;
}

function _noticeOnce(notice) {
  if (!notice || !notice.message || _shownNotices.has(notice.code)) return;
  _shownNotices.add(notice.code);
  // `_voiceBackend` puede ser null (sin motores del backend: solo voz del navegador): sin canal de avisos, no hay a quien avisar.
  try { _voiceBackend?.onNotice?.(notice.message); } catch { /* un aviso que falla no corta la voz */ }
}

// Pide el audio de UNA frase al backend. `{ buffer: null }` = hablarla con la
// voz del navegador (ningun motor pudo, error de red, audio ilegible).
async function _fetchChunk(text, token, link = null) {
  const controller = new AbortController();
  _inflight.add(controller);
  try {
    const body = link ? { text, engine: link.engine, model: link.model } : { text };
    const resp = await _voiceBackend.fetchFn(`${_voiceBackend.apiBase}/api/voice/speak`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: controller.signal,
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) return { buffer: null, error: data.detail || null };
    if (token !== _playToken) return { buffer: null };
    if (!link) _noticeOnce(data.notice);
    if (!data.audio) return { buffer: null, error: data.notice?.message || null };
    const buffer = await _getPlaybackCtx().decodeAudioData(_base64ToArrayBuffer(data.audio));
    return { buffer };
  } catch {
    return { buffer: null };
  } finally {
    _inflight.delete(controller);
  }
}

function _playBuffer(buffer, token) {
  return new Promise((resolve) => {
    if (token !== _playToken) { resolve(); return; }
    const ctx = _getPlaybackCtx();
    const source = ctx.createBufferSource();
    source.buffer = buffer;
    source.connect(_outputNode(ctx));
    source.onended = () => {
      if (_currentSource === source) _currentSource = null;
      resolve();
    };
    _currentSource = source;
    source.start();
  });
}

// Aviso de voz del navegador que NO arranca. Las voces "de Google" (y cualquier voz remota del
// navegador) bajan el audio de internet: sin conexion (ej. PC detras de una VPN) aceptan `speak()` pero
// nunca empiezan, y el usuario solo oye silencio sin saber por que (Sabina y Raul son locales y andan).
// Si no hubo `onstart` pasados unos segundos, se le explica UNA vez la causa probable y la salida.
const BROWSER_VOICE_START_TIMEOUT_MS = 4000;
const BROWSER_VOICE_SILENT_NOTICE = {
  code: "browser-voice-silent",
  message:
    "La voz del navegador no empezó a hablar. Si es una voz «de Google», necesita conexión a internet y " +
    "puede no funcionar con tu red o la VPN. Elegí una voz instalada en Windows (por ejemplo Sabina o Raúl) " +
    "en Configuración > Audio.",
};

// Devuelve una funcion `cancel()` para apagar el vigia (la voz arranco, termino o se cancelo).
function _watchBrowserVoiceStart(utter) {
  let started = false;
  const previous = utter.onstart;
  utter.onstart = (event) => {
    started = true;
    if (typeof previous === "function") previous.call(utter, event);
  };
  const timer = setTimeout(() => {
    if (started || _currentUtter !== utter) return; // arranco, o ya la reemplazo/cancelo otra
    _noticeOnce(BROWSER_VOICE_SILENT_NOTICE);
  }, BROWSER_VOICE_START_TIMEOUT_MS);
  return () => clearTimeout(timer);
}

function _makeBrowserUtterance(text) {
  const utter = new SpeechSynthesisUtterance(text);
  const voice = pickBestSpanishMaleVoice();
  if (voice) {
    utter.voice = voice;
    utter.lang = voice.lang;
  } else {
    utter.lang = "es-419"; // fallback: español latinoamericano generico si no hay ninguna voz es-* instalada
  }
  return utter;
}

// REQ-003: el ultimo eslabon de la cadena, dentro del mismo flujo de frases.
function _speakBrowserText(text, token) {
  return new Promise((resolve) => {
    if (token !== _playToken || !("speechSynthesis" in window)) { resolve(); return; }
    const utter = _makeBrowserUtterance(text);
    _currentUtter = utter;
    let finished = false;
    let watchdog = null;
    const done = () => {
      if (finished) return;
      finished = true;
      clearTimeout(watchdog);
      if (_currentUtter === utter) _currentUtter = null;
      resolve();
    };
    utter.onend = done;
    utter.onerror = done;
    const stopStartWatch = _watchBrowserVoiceStart(utter);
    const doneAndUnwatch = () => { stopStartWatch(); done(); };
    utter.onend = doneAndUnwatch;
    utter.onerror = doneAndUnwatch;
    window.speechSynthesis.speak(utter);
    // Vigilante (mismo criterio que en `_pump`, spec fix-tts-respuestas-silenciosas): algunas voces
    // del navegador -- en particular las de Google -- NUNCA disparan onend/onerror. Sin este
    // tope la promesa no se resolvia, `_speaking` quedaba en true para siempre y el Modo Cerebro no
    // volvia a abrir el microfono ("me escucha la primera vez y despues no"). Margen generoso
    // segun el largo del texto para no cortar una frase larga que si esta sonando.
    watchdog = setTimeout(() => {
      if (finished || _currentUtter !== utter) return;
      try { window.speechSynthesis.cancel(); } catch { /* noop */ }
      done();
    }, SPEAK_WATCHDOG_MIN_MS + text.length * SPEAK_WATCHDOG_PER_CHAR_MS);
  });
}

// Lee `item.text` frase por frase con los motores del backend, pidiendo la
// siguiente mientras suena la actual (REQ-004). Si una frase no tiene audio,
// esa y el resto van con la voz del navegador (REQ-003: nunca mudo).
async function _playWithEngines(item, token) {
  const chunks = splitForSpeech(item.text);
  if (chunks.length === 0) return;
  let started = false;
  const markStarted = () => {
    if (started) return;
    started = true;
    if (item.onStart) {
      try { item.onStart(); } catch { /* un listener que falla no corta la voz */ }
    }
  };
  const ctxReady = _ensureRunning(_getPlaybackCtx());
  let next = _fetchChunk(chunks[0], token);
  for (let i = 0; i < chunks.length; i++) {
    const result = await next;
    if (token !== _playToken) return;
    next = i + 1 < chunks.length ? _fetchChunk(chunks[i + 1], token) : null;
    if (!result.buffer || !(await ctxReady)) {
      for (const controller of _inflight) controller.abort();
      markStarted();
      await _speakBrowserText(chunks.slice(i).join(" "), token);
      return;
    }
    markStarted();
    await _playBuffer(result.buffer, token);
    if (token !== _playToken) return;
  }
}

// Prueba de un eslabon puntual desde Configuracion > Audio (REQ-005/006/007):
// sin salto a otro motor, para escuchar exactamente ese. Devuelve
// `{ ok, message }` para mostrar en la pantalla.
export async function playVoiceSample(link, text) {
  if (!_voiceBackend) return { ok: false, message: "Vision todavía no terminó de cargar." };
  cancelSpeech();
  const token = _playToken;
  _speaking = true;
  try {
    const result = await _fetchChunk(text, token, link);
    if (!result.buffer) return { ok: false, message: result.error || "Esa voz no respondió." };
    if (!(await _ensureRunning(_getPlaybackCtx()))) {
      return { ok: false, message: "El navegador no dejó reproducir audio: tocá la página y volvé a probar." };
    }
    await _playBuffer(result.buffer, token);
    return { ok: true, message: "" };
  } finally {
    if (token === _playToken) {
      _speaking = false;
      _notifySpeechEnd();
    }
  }
}

// Reproduce un texto desde la cola serial (una frase a la vez, sin pisarse)
// con la Web Speech API o, si el usuario armo una cadena de motores en
// Configuracion > Audio (spec voz-fish-audio), con esos motores del backend.
function _pump() {
  _recoverStuckQueueIfNeeded(); // REQ-001: libera la cola si quedo trabada por una utterance muerta sin onend
  if (_speaking || _speechQueue.length === 0) return;
  if (_voiceMuted || !isTtsEnabled()) {
    _speechQueue = [];
    return;
  }

  const item = _speechQueue.shift();
  // spec voz-fish-audio REQ-001: con motores en la cadena, la frase va por el
  // backend (con la voz del navegador como respaldo dentro del mismo flujo).
  if (_usesAudioEngines()) {
    _speaking = true;
    const token = _playToken;
    _playWithEngines(item, token).finally(() => {
      if (token !== _playToken) return; // cancelada: cancelSpeech ya aviso el fin
      _speaking = false;
      _notifySpeechEnd();
      _pump();
    });
    return;
  }
  if (!("speechSynthesis" in window)) {
    // Sin soporte del navegador -- seguir la cola (nada mas que hacer).
    _pump();
    return;
  }

  const utter = _makeBrowserUtterance(item.text);

  _speaking = true;
  _currentUtter = utter;
  const finish = () => {
    if (_currentUtter !== utter) return; // aviso tardio de una voz ya cancelada
    _clearSpeakWatchdog(); // REQ-002: el fin llego solo -- no hace falta el watchdog
    _currentUtter = null;
    _speaking = false;
    _notifySpeechEnd();
    _pump();
  };
  const stopStartWatch = _watchBrowserVoiceStart(utter);
  const finishAndUnwatch = () => { stopStartWatch(); finish(); };
  utter.onend = finishAndUnwatch;
  // Voz interrumpida/no disponible a mitad de camino -- no bloquea el resto
  // de la cola (ej. si el usuario desactivo TTS a mitad de frase).
  utter.onerror = finishAndUnwatch;
  if (item.onStart) {
    try { item.onStart(); } catch { /* un listener que falla no corta la voz */ }
  }

  window.speechSynthesis.speak(utter);

  // REQ-002 (spec fix-tts-respuestas-silenciosas): watchdog. Si onend/onerror
  // nunca llega, fuerza el fin para no trabar la cola para siempre. El margen
  // es generoso para no cortar narraciones largas legitimas.
  _clearSpeakWatchdog();
  _speakWatchdogTimer = setTimeout(() => {
    if (_currentUtter !== utter) return; // ya la reemplazo otra voz
    try { window.speechSynthesis.cancel(); } catch { /* noop */ }
    finish();
  }, SPEAK_WATCHDOG_MIN_MS + item.text.length * SPEAK_WATCHDOG_PER_CHAR_MS);
}

// -------------------------------------------------------------------------
// Notificacion de fin de habla (spec reproducir-respuesta-rangos REQ-007):
// app.js necesita saber cuando una reproduccion termina SOLA (o se cancela)
// para resetear el boton 🔊 de play/stop a su estado "libre" sin tener que
// adivinar con timers. Es un mondejo de callbacks sencillo: app.js registra
// uno al arrancar.
// -------------------------------------------------------------------------
let _speechEndCallbacks = [];

export function onSpeechEnd(cb) {
  _speechEndCallbacks.push(cb);
}

function _notifySpeechEnd() {
  for (const cb of _speechEndCallbacks) {
    try { cb(); } catch { /* un listener que falla no rompe a los demas */ }
  }
}

// Encola `text` para hablarse -- noop silencioso si TTS esta apagado o el
// navegador no soporta la API (nunca rompe el flujo normal del chat).
// `onStart` (opcional): se llama cuando ESTE texto empieza a sonar (puede
// esperar en la cola detras de otros) -- app.js lo usa para poner en ⏹ el
// boton del mensaje que se esta leyendo.
// `limitKey` + `limit` (spec informe-tecnico-hablado REQ-004, "en orden, sin
// atrasarse"): si ya hay `limit` frases con esa clave esperando su turno, se
// descartan las mas viejas. La que esta sonando termina, y nada que no tenga
// esa clave (la lectura de la respuesta) se toca.
export function speak(text, { onStart, limitKey, limit = 0, lead = false } = {}) {
  if (!isTtsEnabled()) return;
  if (!("speechSynthesis" in window) && !_usesAudioEngines()) return;
  const trimmed = (text || "").trim();
  if (!trimmed) return;
  if (limitKey && limit > 0) {
    let waiting = _speechQueue.filter((item) => item.limitKey === limitKey).length;
    _speechQueue = _speechQueue.filter((item) => {
      if (item.limitKey !== limitKey || waiting < limit) return true;
      if (item.lead) return true; // REQ-015: la presentacion del diseno nunca se descarta
      waiting -= 1; // descarta la mas vieja de las que esperan
      return false;
    });
  }
  _speechQueue.push({ text: trimmed, onStart, limitKey, lead });
  _pump();
}

// Corta cualquier habla en curso y vacia la cola -- usado al apagar TTS
// (setTtsEnabled(false)), al detener un turno (Stop) y por el mute.
export function cancelSpeech() {
  _speechQueue = [];
  _speaking = false;
  _currentUtter = null; // lo que avise despues la voz cortada ya no cuenta
  _clearSpeakWatchdog(); // REQ-002: no dejar un watchdog pendiente de una voz ya cancelada
  // spec voz-fish-audio REQ-010: corta tambien el audio de los motores del
  // backend (al instante) y los pedidos de frases que todavia no llegaron.
  _playToken += 1;
  for (const controller of _inflight) controller.abort();
  _inflight.clear();
  if (_currentSource) {
    const source = _currentSource;
    _currentSource = null;
    try { source.stop(); } catch { /* ya habia terminado */ }
  }
  if ("speechSynthesis" in window) window.speechSynthesis.cancel();
  // Pedido del usuario (2026-09): cualquier CANCELACION (incluso el toggle
  // "Silenciar voz" de accesos rapidos) debe soltar el estado del boton de
  // la bocinita 🔊 -- si NO se notifica, la reproduccion seguia "en silencio"
  // (cancel() de Web Speech no dispara onend de forma fiable) y el boton
  // quedaba atascado en "playing", bloqueando reproducir otra respuesta.
  // Notificar aqui resetea el boton activo via onSpeechEnd de app.js.
  _notifySpeechEnd();
}

// Prueba de voz (REQ-010): frase de muestra fija con la voz configurada,
// para confirmar que se escucha bien antes de una reunion real -- se
// habla SIEMPRE (sin chequear isTtsEnabled()) porque es una accion directa
// del usuario tocando el boton, no una narracion automatica de fondo.
export const TEST_PHRASE = "Hola, soy Vision. Así es como voy a sonar durante tus reuniones y respuestas.";

export function speakTestPhrase() {
  // spec voz-fish-audio: con motores en la cadena, se prueba la cadena
  // completa (el primero que responda, igual que en una respuesta real).
  if (_usesAudioEngines()) {
    cancelSpeech();
    const token = _playToken;
    _speaking = true;
    _playWithEngines({ text: TEST_PHRASE }, token).finally(() => {
      if (token !== _playToken) return;
      _speaking = false;
      _notifySpeechEnd();
    });
    return;
  }
  if (!("speechSynthesis" in window)) return;
  window.speechSynthesis.cancel();
  window.speechSynthesis.speak(_makeBrowserUtterance(TEST_PHRASE));
}

// -------------------------------------------------------------------------
// Sonido activador estilo Alexa/Google Assistant (pedido explicito del
// usuario): 2 tonos cortos ascendentes ("beep-boop") que suenan apenas el
// gate del Modo reunion detecta la mencion de "Vision"/"Jarvis" -- feedback
// AUDITIVO inmediato (complementa el resaltado visual ya existente,
// `.fragment-activator`), sin esperar a que el LLM termine de responder
// (eso puede tardar varios segundos). Sintetizado con Web Audio API en vez
// de un archivo de audio -- 0 dependencias externas/CDN a vendorizar,
// consistente con el resto del proyecto.
// -------------------------------------------------------------------------
let _activationAudioCtx = null;

function _getActivationAudioCtx() {
  if (!_activationAudioCtx || _activationAudioCtx.state === "closed") {
    const AudioCtx = window.AudioContext || window.webkitAudioContext;
    _activationAudioCtx = new AudioCtx();
  }
  return _activationAudioCtx;
}

// Reproduce un unico tono senoidal corto con fade-out (evita el "click"
// audible de cortar la onda de golpe) -- bloque reusado 2 veces para el
// patron ascendente de 2 notas.
function _playTone(ctx, freqHz, startTime, durationSec) {
  const osc = ctx.createOscillator();
  const gain = ctx.createGain();
  osc.type = "sine";
  osc.frequency.value = freqHz;
  gain.gain.setValueAtTime(0.15, startTime); // volumen moderado, no un beep agresivo
  gain.gain.exponentialRampToValueAtTime(0.001, startTime + durationSec);
  osc.connect(gain);
  gain.connect(ctx.destination);
  osc.start(startTime);
  osc.stop(startTime + durationSec);
}

export function playActivationSound() {
  try {
    const ctx = _getActivationAudioCtx();
    // Bug real investigado (narracion-agente-efimera, 9na sesion): el
    // usuario reporto que el click manual en el ojo "no funciono... no
    // sonó el beep, no pasó nada". Una hipotesis que NO se habia cubierto
    // antes: Chrome puede crear un AudioContext en estado "suspended" si
    // las heuristicas de autoplay policy no reconocen el evento como
    // interaccion "reciente" (ej. si el click llega mientras el hilo
    // principal esta ocupado, o en navegadores mas estrictos) -- sin
    // resume() explicito, osc.start() se encola pero NUNCA suena, sin
    // ningun error (el try/catch de aca abajo no lo detecta porque no
    // lanza excepcion). Se fuerza resume() cada vez, de forma defensiva.
    if (ctx.state === "suspended") {
      ctx.resume().catch(() => {});
    }
    const now = ctx.currentTime;
    // Patron ascendente de 2 notas (do -> mi, ~una tercera mayor) -- mismo
    // tipo de patron corto/reconocible que usan Alexa/Google Assistant
    // para confirmar "te escuché" sin ser invasivo.
    _playTone(ctx, 523.25, now, 0.12); // C5
    _playTone(ctx, 659.25, now + 0.1, 0.15); // E5
  } catch {
    // Web Audio no disponible/bloqueado por el navegador -- no es fatal,
    // el resaltado visual sigue funcionando igual sin el sonido.
  }
}
