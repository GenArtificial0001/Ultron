// spec audio-sistema-modo-reunion: de DONDE escucha el Modo reunión -- «solo lo que hablo» (micrófono, lo de siempre), «solo lo que escucho» (el audio de la PC) o «lo que escucho y lo que hablo» (los dos mezclados).
// Por que existe (caso real, 2026-09-11): en una reunión de Teams con auriculares, Vision solo oía al usuario: lo que dicen los demás sale por la PC, no llega al micrófono.
// Como (REQ-003/004): el audio de la PC se pide al navegador con `getDisplayMedia` (loopback DIGITAL, no el micrófono recapturando el parlante), se descarta la pista de VIDEO en el acto y, con «ambos»,
// el micrófono y ese audio se mezclan en UN solo flujo con Web Audio. El resto del Modo reunión no cambia: fragmentos de ~4 s a /api/transcribe, texto en memoria, resumen al «Guardar y salir». No se guarda audio.
// Módulo aparte (app.js supera las 14 000 lineas): app.js le pasa sus utilidades con `initMeetingSource(...)` y lo llama en 3 puntos: el clic de Record, startMeetingMode y la salida.
// Regla del navegador: `getDisplayMedia` solo se puede pedir DENTRO de un gesto reciente del usuario, y SIEMPRE muestra su selector (no se puede saltear): por eso se llama en el mismo clic de «Empezar».
// Todo texto se pinta con textContent.

const SOURCE_KEY = "vision.meeting.source"; // fuente POR DEFECTO de la reunion (la ventana la preselecciona)
const ASK_KEY = "vision.meeting.ask"; // "0" = no preguntar: Record arranca directo con la fuente por defecto
const VALID_SOURCES = ["mic", "system", "both"];
const AUDIO_LIVE_CHECK_MS = 250; // si la pista de audio muere nada mas pedirla, no era un audio compartido de verdad

const LABELS = { mic: "Micrófono", system: "Audio de la PC", both: "Audio de la PC + micrófono" };

const MESSAGES = {
  unsupported: "Este navegador no permite compartir el audio de la PC. Usá Chrome o Edge, o elegí «Solo lo que hablo».",
  denied: "No se compartió nada: cancelaste el selector o el navegador no lo permitió. Probá de nuevo.",
  "no-audio": "No se compartió el audio. Elegí «Pantalla completa» y tildá «Compartir también el audio del sistema».",
  ended: "El audio de la PC se cortó apenas empezó. Probá de nuevo eligiendo «Pantalla completa».",
  failed: "No se pudo capturar el audio de la PC. Probá de nuevo.",
};

const HINT_SYSTEM =
  "Al empezar, el navegador te va a pedir qué compartir: elegí «Pantalla completa» y tildá «Compartir también el audio del sistema». Vision no graba ni guarda la pantalla ni el audio: solo lo transcribe para el resumen. Las voces de los demás también se transcriben.";

let _ctx = null; // { readPreference, savePreference } de app.js
let _open = false; // hay una ventana abierta o una captura en curso: un 2do pedido se ignora

const $ = (id) => document.getElementById(id);

class MeetingSourceError extends Error {
  constructor(kind) {
    super(kind);
    this.kind = kind;
  }
}

function messageFor(err) {
  return MESSAGES[err instanceof MeetingSourceError ? err.kind : "failed"] || MESSAGES.failed;
}

export function initMeetingSource(ctx) {
  _ctx = ctx;
  const select = $("cfg-meeting-source");
  const ask = $("cfg-meeting-ask");
  if (select) select.addEventListener("change", () => setMeetingSource(select.value));
  if (ask) ask.addEventListener("change", () => setAskMeetingSource(ask.checked));
  refreshMeetingSourceSettings();
}

// ---------------------------------------------------------------------------------------------------------------------
// Preferencias (REQ-011)
// ---------------------------------------------------------------------------------------------------------------------
export function getMeetingSource() {
  const value = _ctx ? _ctx.readPreference(SOURCE_KEY) : null;
  return VALID_SOURCES.includes(value) ? value : "mic";
}

export function setMeetingSource(value) {
  if (_ctx && VALID_SOURCES.includes(value)) _ctx.savePreference(SOURCE_KEY, value);
}

export function shouldAskMeetingSource() {
  return !_ctx || _ctx.readPreference(ASK_KEY) !== "0";
}

export function setAskMeetingSource(ask) {
  if (_ctx) _ctx.savePreference(ASK_KEY, ask ? "1" : "0");
}

// Pone en Configuración > Audio lo que hay guardado (se llama al abrir la pestaña: la ventana de arranque también puede cambiarlo).
export function refreshMeetingSourceSettings() {
  const select = $("cfg-meeting-source");
  const ask = $("cfg-meeting-ask");
  if (select) select.value = getMeetingSource();
  if (ask) ask.checked = shouldAskMeetingSource();
}

export function meetingSourceLabel(source) {
  return LABELS[source] || "";
}

export function displayAudioSupported() {
  return typeof navigator.mediaDevices?.getDisplayMedia === "function";
}

// ---------------------------------------------------------------------------------------------------------------------
// Captura del audio de la PC (REQ-003, REQ-005)
// ---------------------------------------------------------------------------------------------------------------------
async function acquireSystemAudio() {
  if (!displayAudioSupported()) throw new MeetingSourceError("unsupported");
  let shared;
  try {
    // `video: true` es requisito de la API aunque no se use. `suppressLocalAudioPlayback: false` = la persona SIGUE oyendo la reunion por sus parlantes/auriculares mientras Vision la graba.
    // Bug real (2026-10-20): con `true` (como estaba hasta la 0.35.0) el navegador silenciaba ese audio para quien escuchaba y lo devolvia al detener. La propia voz de Vision no vuelve a entrar
    // porque un fragmento en el que Vision hablo no se transcribe (REQ-006), asi que no hace falta silenciar nada.
    shared = await navigator.mediaDevices.getDisplayMedia({ video: true, audio: { suppressLocalAudioPlayback: false }, systemAudio: "include" });
  } catch (err) {
    throw new MeetingSourceError(err && (err.name === "NotAllowedError" || err.name === "AbortError") ? "denied" : "failed");
  }
  shared.getVideoTracks().forEach((track) => track.stop()); // privacidad: Vision nunca guarda ni manda imagen de la pantalla
  const audio = shared.getAudioTracks();
  if (!audio.length) {
    shared.getTracks().forEach((track) => track.stop());
    throw new MeetingSourceError("no-audio");
  }
  await new Promise((resolve) => setTimeout(resolve, AUDIO_LIVE_CHECK_MS));
  if (!audio.some((track) => track.readyState === "live")) {
    shared.getTracks().forEach((track) => track.stop());
    throw new MeetingSourceError("ended");
  }
  return shared;
}

// ---------------------------------------------------------------------------------------------------------------------
// Ventana de elección (REQ-001). Devuelve { source, systemStream } o null si la persona canceló.
// ---------------------------------------------------------------------------------------------------------------------
function showWindow({ error = "" } = {}) {
  return new Promise((resolve) => {
    _open = true;
    const overlay = $("meeting-source-overlay");
    const startBtn = $("meeting-source-start");
    const errorEl = $("meeting-source-error");
    const hintEl = $("meeting-source-hint");
    const noAskEl = $("meeting-source-noask");
    const radios = [...overlay.querySelectorAll('input[name="meeting-source"]')];
    const supported = displayAudioSupported();
    const preferred = getMeetingSource();
    let closed = false;
    let busy = false;

    for (const radio of radios) {
      const label = radio.closest("label");
      const blocked = radio.value !== "mic" && !supported;
      radio.disabled = blocked;
      if (blocked) label.setAttribute("aria-disabled", "true");
      else label.removeAttribute("aria-disabled");
      radio.checked = radio.value === (supported ? preferred : "mic");
    }
    noAskEl.checked = false;
    errorEl.textContent = error || (supported ? "" : MESSAGES.unsupported);
    startBtn.disabled = false;

    const selected = () => radios.find((radio) => radio.checked)?.value || "mic";
    const updateHint = () => {
      hintEl.textContent = selected() === "mic" ? "" : HINT_SYSTEM;
    };
    updateHint();

    const onKey = (event) => {
      if (event.key === "Escape") finish(null);
    };
    const finish = (result) => {
      if (closed) return;
      closed = true;
      overlay.classList.add("hidden");
      document.removeEventListener("keydown", onKey);
      overlay.onclick = null;
      startBtn.onclick = null;
      $("meeting-source-cancel").onclick = null;
      $("meeting-source-close").onclick = null;
      radios.forEach((radio) => (radio.onchange = null));
      _open = false;
      resolve(result);
    };
    // «No volver a preguntar» fija ESTA elección como la de siempre; sin tildarla, la elección vale solo para esta reunión (la por defecto sigue siendo la de Configuración).
    const rememberIfAsked = (source) => {
      if (!noAskEl.checked) return;
      setMeetingSource(source);
      setAskMeetingSource(false);
    };

    radios.forEach((radio) => (radio.onchange = updateHint));
    document.addEventListener("keydown", onKey);
    overlay.onclick = (event) => {
      if (event.target === overlay) finish(null);
    };
    $("meeting-source-cancel").onclick = () => finish(null);
    $("meeting-source-close").onclick = () => finish(null);
    startBtn.onclick = () => {
      if (busy) return;
      const source = selected();
      if (source === "mic") {
        rememberIfAsked(source);
        finish({ source, systemStream: null });
        return;
      }
      busy = true;
      startBtn.disabled = true;
      errorEl.textContent = "";
      // Se pide AQUI, dentro del clic: el navegador solo deja abrir su selector de pantalla con un gesto reciente.
      acquireSystemAudio().then(
        (systemStream) => {
          if (closed) {
            systemStream.getTracks().forEach((track) => track.stop()); // la persona cerró la ventana mientras elegía: no dejar nada abierto
            return;
          }
          rememberIfAsked(source);
          finish({ source, systemStream });
        },
        (err) => {
          busy = false;
          if (!closed) {
            startBtn.disabled = false;
            errorEl.textContent = messageFor(err);
          }
        },
      );
    };
    overlay.classList.remove("hidden");
  });
}

// Punto de entrada del clic de Record. Sin «preguntar al empezar», arranca directo con la fuente por defecto; si esa fuente incluye el audio de la PC y no se pudo capturar,
// abre la ventana mostrando el motivo (para reintentar o elegir otra). Devuelve { source, systemStream } o null (canceló).
export function requestMeetingSource() {
  if (!_ctx || _open) return Promise.resolve(null);
  const saved = getMeetingSource();
  if (shouldAskMeetingSource()) return showWindow();
  if (saved === "mic") return Promise.resolve({ source: "mic", systemStream: null });
  _open = true;
  return acquireSystemAudio().then(
    (systemStream) => {
      _open = false;
      return { source: saved, systemStream };
    },
    (err) => {
      _open = false;
      return showWindow({ error: messageFor(err) });
    },
  );
}

// ---------------------------------------------------------------------------------------------------------------------
// Audio de la reunion con el audio de la PC (REQ-003/004/008/009). Solo para «system» y «both»; con «mic» no se usa (el flujo de siempre).
// ---------------------------------------------------------------------------------------------------------------------
export async function buildMeetingAudio({ source, systemStream, micStream }) {
  const systemTracks = systemStream ? systemStream.getAudioTracks() : [];
  const stopSystem = () => (systemStream ? systemStream.getTracks().forEach((track) => track.stop()) : undefined);

  // «system» y «both» pasan por Web Audio para entregar SIEMPRE un flujo MONO. Bug real (2026-10-21): el audio de la PC llega en estereo (2 canales) y el servicio de transcripcion de la empresa
  // (`audio/transcriptions`, whisper-large) responde `500 'channel_transcript'` con cualquier audio de 2 canales (medido con el servicio real: el microfono, mono, da 200; el audio de la PC y la
  // mezcla, estereo, dan 500; 6 de 6). Con 1 canal transcribe bien. Un destino de 1 canal baja los dos canales a uno promediandolos (`channelInterpretation: "speakers"`), asi no se pierde nada.
  const Ctx = window.AudioContext || window.webkitAudioContext;
  const audioCtx = new Ctx();
  try {
    await audioCtx.resume();
  } catch {
    // noop: si ya esta corriendo, o el navegador lo reanuda solo con el gesto previo
  }
  const destination = audioCtx.createMediaStreamDestination();
  destination.channelCount = 1;
  destination.channelCountMode = "explicit";
  destination.channelInterpretation = "speakers";
  const systemNode = audioCtx.createMediaStreamSource(new MediaStream(systemTracks));
  systemNode.connect(destination);

  if (source === "system") {
    // Solo lo que escucho: no hay microfono que mezclar ni reabrir (mismo contrato que antes), pero el flujo sale mono.
    return {
      stream: destination.stream,
      systemTracks: () => systemTracks,
      micStream: () => null,
      swapMic: () => {},
      stop() {
        systemNode.disconnect();
        stopSystem();
        audioCtx.close().catch(() => {});
      },
    };
  }

  // "both": el microfono y el audio de la PC entran a UN solo flujo (sin diarizacion, decision de la spec: un unico transcript mezclado).
  let mic = null;
  let micNode = null;
  const detachMic = () => {
    if (micNode) micNode.disconnect();
    micNode = null;
    if (mic) mic.getTracks().forEach((track) => track.stop());
    mic = null;
  };
  // Cambiar el microfono (plug and play) NO reinicia nada: el flujo mezclado es el mismo y el grabador sigue sobre el.
  const attachMic = (stream) => {
    detachMic();
    mic = stream;
    micNode = audioCtx.createMediaStreamSource(stream);
    micNode.connect(destination);
  };
  if (micStream) attachMic(micStream);

  return {
    stream: destination.stream,
    systemTracks: () => systemTracks,
    micStream: () => mic,
    swapMic: attachMic,
    stop() {
      detachMic();
      systemNode.disconnect();
      stopSystem();
      audioCtx.close().catch(() => {});
    },
  };
}
