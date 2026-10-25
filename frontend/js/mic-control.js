// spec control-microfono-windows: ver, activar y ajustar el volumen del microfono de Windows, y avisar ANTES de escuchar si esta silenciado.
// Por que existe (caso real, 2026-10-15): Windows deja el microfono predeterminado SILENCIADO al iniciar y el navegador lo abre igual, sin error: solo trae silencio. Vision no escuchaba y
// no decia nada. El estado vive en Windows, asi que se lee y se cambia por `/api/audio/*` (solo esta PC y esta pagina; ver backend/app/audio_control.py).
// Modulo aparte (no dentro de app.js, que supera las 14 000 lineas): app.js le pasa sus utilidades con `initMicControl(...)` y lo llama en 3 puntos: la pestana Audio, el arranque MANUAL de
// cada modo de voz (reunion, Cerebro, microfono del chat) y `openMicStream` (para vigilar la senal). Las reconexiones automaticas del plug and play NO pasan por aca (REQ-010).
// Reglas: NUNCA se cambia el audio por iniciativa propia (REQ-008): solo lo que la persona toca (Activar o el deslizador). Todo texto del servidor se pinta con textContent.

const NO_SIGNAL_MS = 5000; // REQ-009: sin senal durante este tiempo y sin poder confirmar el silencio -> aviso
const NO_SIGNAL_PEAK = 0.0001; // un microfono silenciado entrega ceros exactos; el ruido de fondo de uno real queda muy por encima
const NO_SIGNAL_ARM_MS = 10_000; // el pedido de vigilancia vence si el modo no llega a abrir el microfono

let _ctx = null; // utilidades de app.js
let _panelMics = []; // microfonos de Windows que muestra la pestana Audio
let _panelBusy = false;
let _checking = false; // hay un aviso o chequeo previo en curso: un 2do clic del mismo boton se ignora
let _pendingKind = null; // modo manual cuyo chequeo previo sigue en curso: para app.js ese modo YA ocupa el microfono (ver pendingVoiceMode)
let _armed = null; // { kind, until }: el proximo openMicStream vigila la senal (REQ-009)
let _modalMeter = null;
let _modalMicId = "";

const $ = (id) => document.getElementById(id);

export function initMicControl(ctx) {
  _ctx = ctx;
  const select = $("cfg-winmic-select");
  const toggle = $("cfg-winmic-toggle");
  const slider = $("cfg-winmic-slider");
  if (select) select.addEventListener("change", () => renderPanel());
  if (toggle) toggle.addEventListener("click", () => panelToggleMute());
  if (slider) {
    slider.addEventListener("input", () => setValueText($("cfg-winmic-value"), Number(slider.value)));
    slider.addEventListener("change", () => panelSetVolume(Number(slider.value)));
  }
  const modalSelect = $("mic-warn-select");
  if (modalSelect) modalSelect.addEventListener("change", () => modalSelectMic(modalSelect.value));
}

// ---------------------------------------------------------------------------------------------------------------------
// Nombres: cruce entre el microfono del navegador (etiqueta) y los de Windows (REQ-006). Nunca se actua sobre uno "parecido":
// con dos del mismo nombre no se elige ninguno por la persona.
// ---------------------------------------------------------------------------------------------------------------------
export function normalizeMicLabel(label) {
  return String(label || "")
    .replace(/^(default|communications|predeterminado|comunicaciones)\s+-\s+/i, "")
    .replace(/\s*\([0-9a-f]{4}:[0-9a-f]{4}\)\s*$/i, "")
    .trim()
    .toLowerCase();
}

// Devuelve { mic, ambiguous }: `mic` si hay EXACTAMENTE uno con ese nombre; `ambiguous` si hay varios (no se elige ninguno).
export function matchWindowsMic(winMics, browserLabel) {
  const wanted = normalizeMicLabel(browserLabel);
  if (!wanted) return { mic: null, ambiguous: false };
  const exact = winMics.filter((m) => normalizeMicLabel(m.name) === wanted);
  if (exact.length === 1) return { mic: exact[0], ambiguous: false };
  if (exact.length > 1) return { mic: null, ambiguous: true };
  return { mic: null, ambiguous: false };
}

// El microfono de Windows que va a usar Vision: el elegido en Configuracion (por nombre) o, sin eleccion, el predeterminado.
function targetMic(winMics) {
  const saved = _ctx.getSavedMic();
  if (!saved.deviceId) return { mic: winMics.find((m) => m.is_default) || null, ambiguous: false };
  return matchWindowsMic(winMics, saved.label);
}

async function fetchMics() {
  const resp = await _ctx.apiFetchOk(`${_ctx.apiBase}/api/audio/mics`);
  const data = await resp.json();
  return Array.isArray(data.mics) ? data.mics : [];
}

async function postMic(path, micId, body) {
  const resp = await _ctx.apiFetchOk(`${_ctx.apiBase}/api/audio/mics/${path}?mic_id=${encodeURIComponent(micId)}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return (await resp.json()).mic;
}

function stateText(mic) {
  if (mic.muted === true) return "Silenciado en Windows";
  if (mic.muted === false) return "Activo";
  return "Windows no informa el estado de este micrófono";
}

function setValueText(el, value) {
  if (el) el.textContent = `${Math.round(value)} %`;
}

function fillSelect(selectEl, mics, preferredId) {
  selectEl.textContent = "";
  for (const mic of mics) {
    const opt = document.createElement("option");
    opt.value = mic.id;
    opt.textContent = mic.is_default ? `${mic.name} (predeterminado de Windows)` : mic.name;
    selectEl.appendChild(opt);
  }
  if (preferredId && mics.some((m) => m.id === preferredId)) selectEl.value = preferredId;
}

// ---------------------------------------------------------------------------------------------------------------------
// Pestana Configuracion > Audio (REQ-001..004)
// ---------------------------------------------------------------------------------------------------------------------
function selectedPanelMic() {
  const id = $("cfg-winmic-select")?.value;
  return _panelMics.find((m) => m.id === id) || null;
}

function renderPanel() {
  const mic = selectedPanelMic();
  const stateEl = $("cfg-winmic-state");
  const toggle = $("cfg-winmic-toggle");
  const slider = $("cfg-winmic-slider");
  if (!mic) return;
  stateEl.textContent = stateText(mic);
  stateEl.dataset.state = mic.muted === true ? "muted" : mic.muted === false ? "on" : "unknown";
  const unknown = mic.muted === null;
  toggle.disabled = unknown;
  slider.disabled = unknown;
  toggle.textContent = mic.muted === true ? "Activar micrófono" : "Silenciar micrófono";
  if (mic.volume !== null) {
    slider.value = String(mic.volume);
    setValueText($("cfg-winmic-value"), mic.volume);
  }
}

export async function refreshAudioPanel() {
  const block = $("cfg-winmic-block");
  const note = $("cfg-winmic-note");
  if (!block || !_ctx || _panelBusy) return;
  _panelBusy = true;
  try {
    const mics = await fetchMics();
    _panelMics = mics;
    if (!mics.length) {
      block.dataset.state = "empty";
      note.textContent = "Windows no tiene ningún micrófono conectado.";
      return;
    }
    const { mic, ambiguous } = targetMic(mics);
    fillSelect($("cfg-winmic-select"), mics, mic ? mic.id : $("cfg-winmic-select").value);
    block.dataset.state = "ready";
    note.textContent = ambiguous
      ? "Hay varios micrófonos con el mismo nombre: elegí cuál querés controlar en esta lista."
      : "";
    renderPanel();
  } catch (err) {
    block.dataset.state = "error";
    note.textContent = _ctx.friendlyErrorMessage(err);
  } finally {
    _panelBusy = false;
  }
}

async function applyPanelChange(call) {
  const note = $("cfg-winmic-note");
  const mic = selectedPanelMic();
  if (!mic) return;
  try {
    const updated = await call(mic);
    _panelMics = _panelMics.map((m) => (m.id === updated.id ? updated : m));
    note.textContent = "";
    renderPanel();
  } catch (err) {
    note.textContent = _ctx.friendlyErrorMessage(err);
    await refreshAudioPanel(); // vuelve a mostrar lo que Windows dice de verdad
  }
}

function panelToggleMute() {
  return applyPanelChange((mic) => postMic("mute", mic.id, { muted: mic.muted !== true }));
}

function panelSetVolume(value) {
  return applyPanelChange((mic) => postMic("volume", mic.id, { volume: Math.round(value) }));
}

// ---------------------------------------------------------------------------------------------------------------------
// Medidor de nivel (la barra verde) para el aviso: mismo dibujo que el de Configuracion > Audio.
// ---------------------------------------------------------------------------------------------------------------------
function createMeter(fillEl) {
  let stream = null;
  let audioCtx = null;
  let rafId = null;
  let generation = 0;
  const stop = () => {
    generation += 1;
    if (rafId) cancelAnimationFrame(rafId);
    rafId = null;
    if (stream) stream.getTracks().forEach((t) => t.stop());
    stream = null;
    if (audioCtx) audioCtx.close().catch(() => {});
    audioCtx = null;
    fillEl.style.width = "0%";
  };
  const start = async () => {
    stop();
    const mine = generation;
    try {
      const s = await _ctx.openMicStream();
      if (mine !== generation) {
        s.getTracks().forEach((t) => t.stop());
        return;
      }
      stream = s;
      const Ctx = window.AudioContext || window.webkitAudioContext;
      audioCtx = new Ctx();
      const analyser = audioCtx.createAnalyser();
      analyser.fftSize = 512;
      audioCtx.createMediaStreamSource(s).connect(analyser);
      const data = new Uint8Array(analyser.fftSize);
      const poll = () => {
        analyser.getByteTimeDomainData(data);
        let sum = 0;
        for (let i = 0; i < data.length; i += 1) {
          const n = (data[i] - 128) / 128;
          sum += n * n;
        }
        fillEl.style.width = `${Math.min(100, Math.round(Math.sqrt(sum / data.length) * 300))}%`;
        rafId = requestAnimationFrame(poll);
      };
      poll();
    } catch {
      // sin microfono no hay barra: el resto del aviso sigue sirviendo (el motivo ya lo da el mensaje)
    }
  };
  return { start, stop };
}

// ---------------------------------------------------------------------------------------------------------------------
// Aviso (modal) antes de escuchar (REQ-005, REQ-006, REQ-009)
// ---------------------------------------------------------------------------------------------------------------------
const KIND_LABEL = { meeting: "el Modo reunión", brain: "el Modo Cerebro", chat: "el micrófono del chat" };

function modalMicById(id) {
  return _panelMics.find((m) => m.id === id) || null;
}

function renderModalMic() {
  const mic = modalMicById(_modalMicId);
  const status = $("mic-warn-status");
  const slider = $("mic-warn-slider");
  const activate = $("mic-warn-activate");
  if (!mic) return;
  status.textContent = stateText(mic);
  status.dataset.state = mic.muted === true ? "muted" : mic.muted === false ? "on" : "unknown";
  activate.disabled = mic.muted === null;
  activate.textContent = mic.muted === true ? "Activar micrófono" : "Silenciar micrófono";
  slider.disabled = mic.muted === null;
  if (mic.volume !== null) {
    slider.value = String(mic.volume);
    setValueText($("mic-warn-value"), mic.volume);
  }
}

function modalSelectMic(id) {
  _modalMicId = id;
  renderModalMic();
  _modalMeter?.start(); // la barra verde sigue al microfono elegido en Configuracion; el aviso no cambia cual usa Vision (D1)
}

async function modalApply(call) {
  const mic = modalMicById(_modalMicId);
  if (!mic) return;
  try {
    const updated = await call(mic);
    _panelMics = _panelMics.map((m) => (m.id === updated.id ? updated : m));
    $("mic-warn-error").textContent = "";
    renderModalMic();
  } catch (err) {
    $("mic-warn-error").textContent = _ctx.friendlyErrorMessage(err);
  }
}

// Muestra el aviso y resuelve con "continue" (siguio, activando o no) o "cancel" (cerro con la X: el modo NO arranca).
function showMicWarning({ kind, reason, winMics, mic }) {
  return new Promise((resolve) => {
    const overlay = $("mic-warn-overlay");
    const label = KIND_LABEL[kind] || "la escucha";
    _panelMics = winMics;
    $("mic-warn-title").textContent = reason === "no-signal" ? "No llega señal de audio" : "Tu micrófono está silenciado en Windows";
    $("mic-warn-message").textContent =
      reason === "no-signal"
        ? `Pasaron unos segundos y ${label} no recibe sonido. Puede ser el micrófono equivocado o uno silenciado. Elegí cuál usar y activalo.`
        : `${label[0].toUpperCase()}${label.slice(1)} no te va a escuchar: «${mic ? mic.name : "tu micrófono"}» está silenciado en Windows. Activalo acá o elegí otro de la lista.`;
    fillSelect($("mic-warn-select"), winMics, mic ? mic.id : "");
    _modalMicId = $("mic-warn-select").value;
    $("mic-warn-error").textContent = "";
    renderModalMic();
    overlay.classList.remove("hidden");
    _modalMeter = createMeter($("mic-warn-meter-fill"));
    _modalMeter.start();

    const finish = (result) => {
      overlay.classList.add("hidden");
      _modalMeter?.stop();
      _modalMeter = null;
      $("mic-warn-activate").onclick = null;
      $("mic-warn-continue").onclick = null;
      $("mic-warn-close").onclick = null;
      $("mic-warn-slider").onchange = null;
      $("mic-warn-slider").oninput = null;
      resolve(result);
    };
    $("mic-warn-activate").onclick = () => {
      const current = modalMicById(_modalMicId);
      if (current) modalApply((m) => postMic("mute", m.id, { muted: m.muted !== true }));
    };
    $("mic-warn-slider").oninput = () => setValueText($("mic-warn-value"), Number($("mic-warn-slider").value));
    $("mic-warn-slider").onchange = () => modalApply((m) => postMic("volume", m.id, { volume: Math.round(Number($("mic-warn-slider").value)) }));
    $("mic-warn-continue").onclick = () => finish("continue");
    $("mic-warn-close").onclick = () => finish("cancel");
  });
}

// Antes de arrancar un modo de voz A MANO. Devuelve "continue" (arrancar), "cancel" (la persona cerro el aviso) o "busy" (ya hay un chequeo en curso:
// un doble clic no abre dos). Nunca bloquea por una falla: si Windows no responde, el modo arranca como siempre (y rige REQ-009).
export async function ensureMicReady(kind) {
  if (!_ctx) return "continue";
  if (_checking) return "busy";
  _checking = true;
  _pendingKind = kind;
  try {
    let winMics;
    try {
      winMics = await fetchMics();
    } catch {
      armNoSignalWatch(kind); // no se pudo leer Windows: el modo arranca y se vigila la senal (REQ-009)
      return "continue";
    }
    const { mic } = targetMic(winMics);
    if (mic && mic.muted === true) {
      return await showMicWarning({ kind, reason: "muted", winMics, mic });
    }
    if (!mic || mic.muted === null) armNoSignalWatch(kind); // no se puede confirmar el silencio (REQ-009)
    return "continue";
  } finally {
    _checking = false;
    _pendingKind = null;
  }
}

// Modo manual ("chat" | "brain" | "meeting") que esta en su chequeo previo, o null. Mientras dura, otro modo NO puede arrancar encima (fix-conflicto-modos-voz-simultaneos):
// sin esto, el clic a otro modo en ese instante veria el microfono libre, porque el modo recien marca que esta activo DESPUES del chequeo.
export function pendingVoiceMode() {
  return _pendingKind;
}

// Envuelve el arranque manual de un modo: `start` solo corre si la persona no cancelo el aviso.
export function startWithMicCheck(kind, start) {
  return ensureMicReady(kind).then((result) => {
    if (result === "continue") start();
    return result;
  });
}

// ---------------------------------------------------------------------------------------------------------------------
// REQ-009: sin senal a los 5 s cuando no se pudo confirmar el silencio
// ---------------------------------------------------------------------------------------------------------------------
function armNoSignalWatch(kind) {
  _armed = { kind, until: Date.now() + NO_SIGNAL_ARM_MS };
}

// Lo llama `openMicStream` con cada stream que abre; solo actua si un arranque manual lo pidio.
export function maybeWatchNoSignal(stream) {
  if (!_ctx || !_armed || Date.now() > _armed.until) {
    _armed = null;
    return;
  }
  const { kind } = _armed;
  _armed = null;
  const Ctx = window.AudioContext || window.webkitAudioContext;
  if (!Ctx) return;
  let audioCtx;
  try {
    audioCtx = new Ctx();
    const analyser = audioCtx.createAnalyser();
    analyser.fftSize = 1024;
    audioCtx.createMediaStreamSource(stream).connect(analyser);
    const samples = new Float32Array(analyser.fftSize);
    let peak = 0;
    const started = Date.now();
    const timer = setInterval(async () => {
      analyser.getFloatTimeDomainData(samples);
      for (let i = 0; i < samples.length; i += 1) peak = Math.max(peak, Math.abs(samples[i]));
      const live = stream.getAudioTracks().some((t) => t.readyState === "live");
      const done = peak > NO_SIGNAL_PEAK || !live || Date.now() - started >= NO_SIGNAL_MS;
      if (!done) return;
      clearInterval(timer);
      audioCtx.close().catch(() => {});
      if (peak > NO_SIGNAL_PEAK || !live || _checking) return; // hubo senal, o el micro se fue (eso lo maneja el plug and play)
      _checking = true;
      try {
        let winMics = [];
        try {
          winMics = await fetchMics();
        } catch {
          winMics = [];
        }
        if (winMics.length) await showMicWarning({ kind, reason: "no-signal", winMics, mic: targetMic(winMics).mic });
      } finally {
        _checking = false;
      }
    }, 100);
  } catch {
    audioCtx?.close().catch(() => {});
  }
}

export function stopAudioPanel() {
  _panelBusy = false;
}
