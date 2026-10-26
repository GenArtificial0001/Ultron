// REQ-001/002/003/004 (spec pantalla-bienvenida-ojo): pantalla de
// bienvenida que se ve EN CADA carga de pagina, antes del chat -- titulo
// "VISION" animado letra por letra + subtitulo de marca + el ojo
// (voice-eye.js, reusado en modo DECORATIVO: useCamera:false, sin pedir
// permisos de camara/microfono aca -- pedirlos en cada carga se sentiria
// invasivo). Se sale de 2 formas equivalentes: tocando el ojo (con sonido
// de click sintetizado, REQ-002) o por timeout automatico de 5s sin
// interaccion (REQ-004) -- ambas disparan la misma transicion de zoom+fade
// hacia el chat (REQ-003).
//
// anime.js se carga como <script> clasico (window.anime, ver index.html,
// mismo patron que Chart.js) -- no se importa como modulo ES aca.

import { initVoiceEye, destroyVoiceEye } from "./voice-eye.js";

const TITLE_TEXT = "ULTRON";
const AUTO_ENTER_TIMEOUT_MS = 5000; // REQ-004
const TRANSITION_DURATION_MS = 1000; // REQ-003: ~0.8-1.2s total confirmado en la spec

/**
 * REQ-002: sonido de click sintetizado con Web Audio API (sin archivo de
 * audio externo -- decision explicita del usuario, cero dependencias
 * nuevas que vendorizar). Un tono corto descendente (mas percusivo que un
 * beep sostenido), consistente con una interfaz tipo "sci-fi/HUD".
 */
function playClickSound() {
  try {
    const AudioCtx = window.AudioContext || window.webkitAudioContext;
    const ctx = new AudioCtx();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.type = "sine";
    osc.frequency.setValueAtTime(880, ctx.currentTime);
    osc.frequency.exponentialRampToValueAtTime(220, ctx.currentTime + 0.12);
    gain.gain.setValueAtTime(0.25, ctx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.15);
    osc.connect(gain);
    gain.connect(ctx.destination);
    osc.start();
    osc.stop(ctx.currentTime + 0.16);
    // Cierra el AudioContext despues de sonar -- no debe quedar corriendo
    // de fondo (mismo criterio de limpieza que destroyVoiceEye con
    // camara/mic real).
    setTimeout(() => ctx.close().catch(() => {}), 300);
  } catch {
    // Web Audio API no disponible/bloqueada -- la transicion sigue sin
    // sonido, no es un error que deba frenar el flujo (REQ-003 no depende
    // del sonido para completarse).
  }
}

/**
 * REQ-001: anima el titulo "ULTRON" letra por letra + la linea decorativa
 * debajo, portado de _mockup_jarvis_animation.html (ya validado
 * visualmente por el usuario para el modo reunion).
 */
function animateTitle(titleEl, underlineEl) {
  titleEl.innerHTML = TITLE_TEXT.split("")
    .map((ch) => `<span class="splash-letter">${ch}</span>`)
    .join("");

  if (!window.anime) return; // anime.js no cargo -- el titulo queda estatico visible, no bloquea nada
  window.anime
    .timeline({ easing: "easeOutExpo" })
    .add({
      targets: titleEl.querySelectorAll(".splash-letter"),
      opacity: [0, 1],
      translateY: [10, 0],
      duration: 650,
      delay: window.anime.stagger(45),
    })
    .add({ targets: underlineEl, width: ["0%", "62%"], duration: 700, easing: "easeOutQuart" }, "-=250");
}

/**
 * REQ-003: zoom del ojo hacia el usuario + fade hasta revelar el chat.
 * Disparado por REQ-002 (click manual) o REQ-004 (timeout) -- mismo
 * comportamiento en ambos casos, `playedClickSound` solo decide si suena
 * el click (no tiene sentido sonar un click si no hubo interaccion real).
 */
function playExitTransition(splashEl, eyeContainerEl) {
  // "Zoom hacia el usuario": escala el contenedor del ojo (no toda la
  // pantalla, para que el titulo/subtitulo no se deformen junto con el
  // ojo) -- combinado con el fade de opacity de .splash-screen completo.
  eyeContainerEl.style.transition = `transform ${TRANSITION_DURATION_MS}ms ease-in`;
  eyeContainerEl.style.transform = "scale(2.4)";
  splashEl.classList.add("splash-fading");

  setTimeout(() => {
    destroyVoiceEye(); // libera el renderer/canvas del ojo decorativo
    splashEl.classList.add("hidden");
  }, TRANSITION_DURATION_MS);
}

/**
 * REQ-001: monta la pantalla de bienvenida completa. Se llama una vez al
 * cargar app.js (ver la llamada a initSplashScreen() al final de ese
 * archivo) -- si el elemento #splash-screen no existe en el DOM (defensivo,
 * no deberia pasar en produccion), no hace nada.
 */
export function initSplashScreen() {
  const splashEl = document.getElementById("splash-screen");
  const eyeContainerEl = document.getElementById("splash-eye-container");
  const titleEl = document.getElementById("splash-title");
  if (!splashEl || !eyeContainerEl || !titleEl) return;

  const underlineEl = document.createElement("div");
  underlineEl.className = "splash-title-underline";
  underlineEl.id = "splash-title-underline";
  titleEl.insertAdjacentElement("afterend", underlineEl);

  animateTitle(titleEl, underlineEl);

  // REQ-001: useCamera:false -- modo decorativo, NUNCA pide permisos de
  // camara/microfono en esta pantalla (a diferencia del modo reunion,
  // REQ-011 de narracion-agente-efimera, que si los necesita).
  initVoiceEye(eyeContainerEl, { useCamera: false });

  let exited = false;
  function exitOnce(playedClickSound) {
    if (exited) return; // evita doble transicion si click y timeout casi coinciden (regla de negocio de la spec)
    exited = true;
    clearTimeout(autoEnterTimer);
    if (playedClickSound) playClickSound();
    playExitTransition(splashEl, eyeContainerEl);
  }

  splashEl.addEventListener("click", () => exitOnce(true), { once: true });

  // REQ-004: timeout automatico de 5s sin interaccion.
  const autoEnterTimer = setTimeout(() => exitOnce(false), AUTO_ENTER_TIMEOUT_MS);
}
