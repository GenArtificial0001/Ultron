// REQ-007/008/009/010 (spec narracion-agente-efimera, "Interaccion por voz"):
// TTS de las respuestas de Jarvis y narracion hablada de progreso via Web
// Speech API (`SpeechSynthesisUtterance`) -- nativa del navegador, sin
// costo ni tramite, procesamiento 100% local (nunca sale de la maquina,
// ver categoria 7 de la spec). Migracion futura opcional a un servicio de
// voz corporativo (fuera de alcance de este modulo).
//
// Modulo standalone (sin dependencias de app.js) para poder testear/
// reusar la seleccion de voz desde la pestaña de Configuracion > Audio
// (REQ-009/010) sin acoplarse al resto del chat.

const TTS_ENABLED_KEY = "vision.tts.enabled";
const TTS_VOICE_KEY = "vision.tts.voiceURI";

// -------------------------------------------------------------------------
// Preferencias persistidas (localStorage, mismo patron que THEME_KEY en
// app.js) -- TTS arranca DESACTIVADO por defecto: es una feature nueva que
// habla en voz alta sin que el usuario lo haya pedido todavia, activarla
// silenciosamente por default seria una sorpresa indeseada la primera vez
// que alguien abre Vision despues de este cambio.
// -------------------------------------------------------------------------
export function isTtsEnabled() {
  try {
    return localStorage.getItem(TTS_ENABLED_KEY) === "true";
  } catch {
    return false;
  }
}

export function setTtsEnabled(enabled) {
  try {
    localStorage.setItem(TTS_ENABLED_KEY, enabled ? "true" : "false");
  } catch {
    // noop -- sin localStorage la preferencia no persiste entre recargas, pero sigue aplicada en esta sesion
  }
  if (!enabled) cancelSpeech(); // apagar TTS a mitad de una frase la corta al instante, no la deja terminar
}

function getSavedVoiceURI() {
  try {
    return localStorage.getItem(TTS_VOICE_KEY) || "";
  } catch {
    return "";
  }
}

export function setSavedVoiceURI(voiceURI) {
  try {
    localStorage.setItem(TTS_VOICE_KEY, voiceURI || "");
  } catch {
    // noop
  }
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
let _speechQueue = []; // items { text, onStart }
let _speaking = false;
// Voz que esta sonando AHORA. Bug real (2026-09-27, spec fix-boton-play-stop):
// cancel() de la Web Speech API avisa el corte de la voz vieja DESPUES, con un
// evento asincrono ("interrupted"/"canceled"). Si para entonces ya habia
// arrancado otra voz (el usuario toco 🔊 de nuevo), ese aviso viejo la daba
// por terminada: soltaba el boton (🔊 con el audio sonando) y el proximo toque
// arrancaba la lectura OTRA VEZ desde el principio en vez de detenerla. Los
// eventos de una voz que ya no es la actual se ignoran.
let _currentUtter = null;

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

// Reproduce un texto desde la cola serial (una frase a la vez, sin pisarse)
// con la Web Speech API -- el unico motor de voz de Vision desde que se
// retiro Kokoro (2026-09-12, ver CHANGELOG 0.9.2): no vale la pena la
// latencia de un TTS neural local frente a la voz nativa instantanea.
function _pump() {
  if (_speaking || _speechQueue.length === 0) return;
  if (_voiceMuted || !isTtsEnabled()) {
    _speechQueue = [];
    return;
  }

  const item = _speechQueue.shift();
  if (!("speechSynthesis" in window)) {
    // Sin soporte del navegador -- seguir la cola (nada mas que hacer).
    _pump();
    return;
  }

  const utter = new SpeechSynthesisUtterance(item.text);
  const voice = pickBestSpanishMaleVoice();
  if (voice) {
    utter.voice = voice;
    utter.lang = voice.lang;
  } else {
    utter.lang = "es-419"; // fallback: español latinoamericano generico si no hay ninguna voz es-* instalada
  }

  _speaking = true;
  _currentUtter = utter;
  const finish = () => {
    if (_currentUtter !== utter) return; // aviso tardio de una voz ya cancelada
    _currentUtter = null;
    _speaking = false;
    _notifySpeechEnd();
    _pump();
  };
  utter.onend = finish;
  // Voz interrumpida/no disponible a mitad de camino -- no bloquea el resto
  // de la cola (ej. si el usuario desactivo TTS a mitad de frase).
  utter.onerror = finish;
  if (item.onStart) {
    try { item.onStart(); } catch { /* un listener que falla no corta la voz */ }
  }

  window.speechSynthesis.speak(utter);
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
export function speak(text, { onStart } = {}) {
  if (!isTtsEnabled() || !("speechSynthesis" in window)) return;
  const trimmed = (text || "").trim();
  if (!trimmed) return;
  _speechQueue.push({ text: trimmed, onStart });
  _pump();
}

// Corta cualquier habla en curso y vacia la cola -- usado al apagar TTS
// (setTtsEnabled(false)), al detener un turno (Stop) y por el mute.
export function cancelSpeech() {
  _speechQueue = [];
  _speaking = false;
  _currentUtter = null; // lo que avise despues la voz cortada ya no cuenta
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
export function speakTestPhrase() {
  const phrase = "Hola, soy Vision. Así es como voy a sonar durante tus reuniones y respuestas.";
  if (!("speechSynthesis" in window)) return;
  window.speechSynthesis.cancel();
  const utter = new SpeechSynthesisUtterance(phrase);
  const voice = pickBestSpanishMaleVoice();
  if (voice) {
    utter.voice = voice;
    utter.lang = voice.lang;
  } else {
    utter.lang = "es-419";
  }
  window.speechSynthesis.speak(utter);
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
