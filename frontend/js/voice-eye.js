// REQ-011 (spec narracion-agente-efimera): "el ojo" con vision por camara
// del Modo reunion -- nucleo esferico GRANDE con brillo de borde tipo
// fresnel ("humo" con domain warping, ver makeFresnelMaterial), un IRIS
// chico (disco negro liso con un solo brillo especular, SIN textura
// fibrosa -- 3er rediseno, pedido explicito del usuario: "mejor como lo
// teniamos antes", ver historial completo en _mockup_jarvis_animation.html)
// que se mueve de verdad dentro de ese nucleo, halo pulsante tipo "Siri",
// ecualizador circular de barras estereo, dirigido por 2 senales en
// paralelo:
//   1. Deteccion de MOVIMIENTO por diferencia de frames de la camara (NO
//      reconocimiento facial/MediaPipe -- decision explicita del usuario,
//      validada en el prototipo _mockup_jarvis_animation.html: la tecnica
//      simple ya alcanza para que el ojo "gire a mirar a quien se mueve").
//   2. Deteccion de "onset" de audio (RMS vs. linea de base adaptativa):
//      un salto brusco de energia fuerza un giro RAPIDO hacia la ultima
//      zona de movimiento detectada (simula "mirar a quien acaba de
//      hablar" -- con un solo microfono no hay direccion real de sonido,
//      se aproxima cruzando audio+video, simplificacion aceptada).
// Si se deniegan permisos de camara/microfono (o el navegador los
// bloquea), cae a un modo "escaneo" simulado -- el ojo sigue vivo/animado
// en vez de quedarse congelado (REQ-011, ultimo criterio de aceptacion).
//
// Seguridad/privacidad (REQ-011 + categoria 7 de la spec): el video NUNCA
// se persiste, ni siquiera temporalmente -- se analiza cuadro a cuadro en
// un canvas en memoria (48x36px) y se descarta al instante. Sin aviso de
// consentimiento adicional mas alla del propio indicador visual del ojo
// (decision explicita del usuario).
//
// Este modulo se MONTA/DESMONTA explicitamente (initVoiceEye/destroyVoiceEye)
// -- a diferencia del prototipo original (_mockup_jarvis_animation.html),
// que arrancaba solo al cargar la pagina, aca debe activarse recien cuando
// el usuario toca el boton "Record" (REQ-003) y detenerse por completo al
// salir del modo reunion (liberar camara/microfono, parar el loop de
// render) -- no puede quedar corriendo de fondo el resto del tiempo.

import * as THREE from "../vendor/three.module.min.js";

let _renderer = null;
let _animationFrameId = null;
let _mediaStream = null;
let _audioCtx = null;
let _resizeHandler = null;
let _container = null;

/**
 * Monta el ojo dentro de `container` (un elemento DOM, tipicamente el
 * overlay de pantalla completa del modo reunion). Devuelve una promesa que
 * resuelve cuando el render loop ya arranco (no espera a que la camara/mic
 * esten listos -- eso pasa en paralelo y el ojo ya se ve animado en modo
 * "escaneo" simulado desde el primer frame).
 *
 * @param {HTMLElement} container
 * @param {{useCamera?: boolean}} [options] - REQ-001 (spec pantalla-
 *   bienvenida-ojo): `useCamera:false` evita pedir permisos de camara/mic
 *   por completo -- necesario para el splash screen, que se ve en CADA
 *   carga de pagina y NO debe pedir permisos ahi (se sentiria invasivo).
 *   Default `true` para no romper el modo reunion (REQ-011 de
 *   narracion-agente-efimera), que SI necesita camara/mic real.
 */
export async function initVoiceEye(container, options = {}) {
  // Privacidad/rendimiento (pedido del usuario 2026-09: desactivar TODO lo
  // relacionado con camara): el ojo 3D se ve igual, pero NUNCA se pide
  // permiso de camara ni hay seguimiento por video. Se fuerza false
  // (previamente el default activaba la vision por camara si el caller no
  // pasaba useCamera:false). setupVision() + pollVideo quedan inalcanzables
  // (gated por `if (useCamera)`), listos para reactivar si se pidiera.
  const useCamera = false;
  if (_renderer) destroyVoiceEye(); // por si quedo algo de un montaje anterior sin desmontar
  _container = container;

  // ---------------------------------------------------------------------
  // ENVOLVENTE DE VOZ -- si hay microfono real se usa su RMS; si no, cae a
  // un generador simulado de rafagas/silencios. Ambos escriben la misma
  // variable `envelope`, asi todo lo audio-reactivo es indiferente a la
  // fuente real.
  // ---------------------------------------------------------------------
  let envelope = 0.12;
  let envelopeTarget = 0.12;
  let nextTargetAt = 0;
  let micLevel = null; // null = sin microfono real todavia

  function updateSimulatedEnvelope(t) {
    if (t > nextTargetAt) {
      const speaking = Math.random() < 0.6;
      envelopeTarget = speaking ? 0.35 + Math.random() * 0.55 : 0.04 + Math.random() * 0.1;
      nextTargetAt = t + 0.35 + Math.random() * 0.85;
    }
    envelope += (envelopeTarget - envelope) * 0.08;
  }

  // Ruido organico barato (suma de senos con frecuencias irracionales) --
  // usado para que los anillos "hierban" en vez de ser circulos perfectos,
  // y tambien para decorrelacionar los canales L/R del ecualizador estereo.
  function turbulence(index, seed, t) {
    const x = index * 0.13 + seed * 37.1;
    return (
      Math.sin(x * 1.0 + t * 0.5) * 0.45 +
      Math.sin(x * 2.13 - t * 0.8) * 0.28 +
      Math.sin(x * 4.7 + t * 1.3) * 0.17 +
      Math.sin(x * 9.3 - t * 2.0) * 0.1
    );
  }

  let micLevelL = null, micLevelR = null;
  function updateStereoLevels(t) {
    const noiseL = 0.6 + 0.4 * Math.abs(turbulence(3, 1.7, t * 0.6));
    const noiseR = 0.6 + 0.4 * Math.abs(turbulence(3, 9.4, t * 0.6));
    const levelL = micLevelL !== null ? micLevelL : envelope * noiseL;
    const levelR = micLevelR !== null ? micLevelR : envelope * noiseR;
    return { levelL, levelR };
  }

  // ---------------------------------------------------------------------
  // ESTADO DE LA MIRADA -- a donde "mira" el ojo (-1..1 en x/y), suavizado
  // salvo durante una ventana de "snap" (giro rapido al detectar un sonido).
  // ---------------------------------------------------------------------
  const eyeTarget = { x: 0, y: 0 };
  const eyeSmoothed = { x: 0, y: 0 };
  let snapUntil = -Infinity;
  let lastMotionAt = -Infinity;
  // "Busqueda" idle tipo ojo-robot (pedido del usuario 2026-09: "un simple
  // movimiento, que se mueva hacia varios lados como un ojo robot buscando
  // algo"). 2do intento -- el primero (mirada UNICA a un punto random cada
  // varios segundos) resulto poco visible/muy sutil. Ahora es una
  // secuencia FIJA y simple (izq/centro/der/centro/arriba/centro/abajo/
  // centro, en ese orden, sin random) con snaps rapidos entre puntos --
  // mecanico y facil de leer como "barrido", no organico.
  const SEARCH_POINTS = [
    { x: 0.6, y: 0 }, { x: 0, y: 0 },
    { x: -0.6, y: 0 }, { x: 0, y: 0 },
    { x: 0, y: 0.5 }, { x: 0, y: 0 },
    { x: 0, y: -0.5 }, { x: 0, y: 0 },
  ];
  let searchIndex = 0;
  let nextSearchAt = 1.6;

  function containerSize() {
    const w = container.clientWidth || window.innerWidth || 1;
    const h = container.clientHeight || window.innerHeight || 1;
    return { w, h };
  }

  const scene = new THREE.Scene();
  const size0 = containerSize();
  const camera = new THREE.PerspectiveCamera(45, size0.w / size0.h, 0.1, 100);
  camera.position.set(0, 0.1, 11.2);

  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, preserveDrawingBuffer: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.setSize(size0.w, size0.h);
  container.appendChild(renderer.domElement);
  _renderer = renderer;

  // Canvases 2D superpuestos para el "bloom del pobre" (blur+screen via CSS)
  function makeBloomCanvas(cls) {
    const c = document.createElement("canvas");
    c.className = cls;
    c.style.position = "absolute";
    c.style.inset = "0";
    c.style.width = "100%";
    c.style.height = "100%";
    c.style.pointerEvents = "none";
    container.appendChild(c);
    return c;
  }
  const bloomTight = makeBloomCanvas("voice-eye-bloom-tight");
  const bloomWide = makeBloomCanvas("voice-eye-bloom-wide");
  bloomTight.style.mixBlendMode = "screen";
  bloomTight.style.filter = "blur(6px) saturate(1.3)";
  bloomTight.style.opacity = "0.5";
  bloomWide.style.mixBlendMode = "screen";
  bloomWide.style.filter = "blur(26px) saturate(1.15)";
  bloomWide.style.opacity = "0.32";
  const bloomTightCtx = bloomTight.getContext("2d");
  const bloomWideCtx = bloomWide.getContext("2d");

  function resizeBloomCanvases() {
    const { w, h } = containerSize();
    [bloomTight, bloomWide].forEach((c) => { c.width = w; c.height = h; });
  }
  resizeBloomCanvases();

  // Pedido explicito del usuario: "cuando cambiemos a Claro Azul, el
  // ojo debe ser el azul brillante de ese tema" -- antes RED/RED_BRIGHT
  // eran hex fijo (0xd5001c/0xff1f3d), asi que el ojo se quedaba rojo
  // aunque el resto de la UI ya estuviera en tema azul. Se leen
  // --accent/--accent-2 REALES del CSS activo (getComputedStyle sobre
  // <html>, que es donde vive [data-theme="light"]) en vez de duplicar
  // los valores hex a mano en 2 lugares -- asi el ojo SIEMPRE refleja el
  // tema con el que se activo el modo reunion, sin mantenimiento extra
  // cuando cambien los tokens de tema en styles.css.
  const themeStyles = getComputedStyle(document.documentElement);
  const RED = new THREE.Color(themeStyles.getPropertyValue("--accent").trim() || "#d5001c");
  const RED_BRIGHT = new THREE.Color(themeStyles.getPropertyValue("--accent-2").trim() || "#ff1f3d");
  // Extremos del degrade del ecualizador circular -- oscuro (--accent-soft,
  // ej. #3A0A10 en tema oscuro / #E3E3FF en azul claro) a brillante
  // (variante clara del acento, derivada de RED_BRIGHT en vez de otro hex
  // fijo mas).
  const eqColorDim = new THREE.Color(themeStyles.getPropertyValue("--accent-soft").trim() || "#3a0a10");
  const eqColorBright = RED_BRIGHT.clone().lerp(new THREE.Color(0xffffff), 0.35);

  const coreGroup = new THREE.Group();
  scene.add(coreGroup);

  // Pedido explicito del usuario (2do rediseno del ojo, portado de la v4
  // actualizada de _mockup_jarvis_animation.html): el aro/circulo rojo
  // (CORE_R) tiene que ser GRANDE -- se habia achicado a 0.78 por error
  // en un intento previo, la correccion real era al reves: nucleo grande,
  // iris chico adentro (ver irisScale mas abajo en animate()).
  const CORE_R = 1.05;
  coreGroup.add(new THREE.Mesh(new THREE.SphereGeometry(CORE_R, 48, 48), new THREE.MeshBasicMaterial({ color: 0x0a0305 })));

  // Shader de fresnel (brillo de borde) -- crea el "limbo" rojo del
  // eclipse. Historial de esta capa (documentado por el usuario en el
  // prototipo): firme/solida -> nebulosa con ruido -> "holografico +
  // electrico" (lineas de escaneo + chispas, RECHAZADO explicitamente,
  // "no era tan asi, mas humo") -> version actual: UNA sola capa de humo
  // con "domain warping" (tecnica clasica de shaders procedurales de
  // humo/fuego: se distorsiona la coordenada de un 2do octavo de ruido con
  // la salida del 1ro) -- da volutas curvas y suaves en vez de una grilla
  // de interferencia o filamentos afilados. Transiciones ablandadas con
  // smoothstep (nada de bordes duros ni blancos calientes), toda la paleta
  // sigue derivada de RED/RED_BRIGHT (tema activo), nunca hex fijo.
  function makeFresnelMaterial(color, power, intensity, side) {
    return new THREE.ShaderMaterial({
      uniforms: { glowColor: { value: color }, power: { value: power }, intensity: { value: intensity }, time: { value: 0 } },
      vertexShader: `
        varying vec3 vNormal;
        varying vec3 vViewDir;
        void main() {
          vNormal = normalize(normalMatrix * normal);
          vec4 mvPosition = modelViewMatrix * vec4(position, 1.0);
          vViewDir = normalize(-mvPosition.xyz);
          gl_Position = projectionMatrix * mvPosition;
        }
      `,
      fragmentShader: `
        varying vec3 vNormal;
        varying vec3 vViewDir;
        uniform vec3 glowColor;
        uniform float power;
        uniform float intensity;
        uniform float time;

        // Humo: dos octavas de ruido barato (sumas de senos), la segunda
        // desplazada ("warped") por la salida de la primera -- eso es lo
        // que convierte una grilla de ondas en volutas que se curvan y
        // fluyen como humo real, en vez de un patron de interferencia.
        float smoke(vec3 n) {
          float t = time * 0.35; // deriva lenta y perezosa, como humo real
          float warp = sin(n.x * 2.6 + t) * cos(n.y * 2.2 - t * 0.8)
                     + sin((n.y + n.z) * 2.0 + t * 0.6) * 0.6;

          float wx = n.x * 4.5 + warp * 1.4 + t * 1.3;
          float wy = n.y * 4.0 - warp * 1.1 - t * 1.0;
          float wz = n.z * 4.8 + warp * 1.2 + t * 0.9;
          float detail = sin(wx) * sin(wy + wz) * 0.5 + sin(wx + wy) * 0.3;

          float value = 0.5 + 0.5 * (warp * 0.55 + detail * 0.45);
          return smoothstep(0.18, 0.85, value); // transiciones suaves, sin bordes duros
        }

        void main() {
          vec3 n = normalize(vNormal);
          float rim = 1.0 - max(dot(n, normalize(vViewDir)), 0.0);
          float pattern = 0.5 + 0.75 * smoke(n);
          float glow = pow(rim, power) * intensity * pattern;
          gl_FragColor = vec4(glowColor, glow);
        }
      `,
      transparent: true,
      blending: THREE.AdditiveBlending,
      depthWrite: false,
      side,
    });
  }
  const rimMat = makeFresnelMaterial(RED_BRIGHT, 2.4, 1.5, THREE.FrontSide);
  coreGroup.add(new THREE.Mesh(new THREE.SphereGeometry(CORE_R * 1.04, 48, 48), rimMat));
  const atmoMat = makeFresnelMaterial(RED, 3.6, 0.9, THREE.BackSide);
  coreGroup.add(new THREE.Mesh(new THREE.SphereGeometry(CORE_R * 1.5, 48, 48), atmoMat));

  function makeRadialTexture(size, stops) {
    const c = document.createElement("canvas");
    c.width = c.height = size;
    const ctx = c.getContext("2d");
    const g = ctx.createRadialGradient(size / 2, size / 2, 0, size / 2, size / 2, size / 2);
    stops.forEach(([offset, color]) => g.addColorStop(offset, color));
    ctx.fillStyle = g;
    ctx.fillRect(0, 0, size, size);
    return new THREE.CanvasTexture(c);
  }

  // coreGlowBright/coreGlowMid derivados del acento del tema (mismo
  // criterio que RED/RED_BRIGHT) -- antes rgba(255,190,195,...)/
  // rgba(213,0,28,...) fijos, siempre rojo.
  const coreGlowBright = RED_BRIGHT.clone().lerp(new THREE.Color(0xffffff), 0.2);
  const coreGlow = new THREE.Sprite(new THREE.SpriteMaterial({
    map: makeRadialTexture(256, [
      [0, `rgba(${Math.round(coreGlowBright.r*255)},${Math.round(coreGlowBright.g*255)},${Math.round(coreGlowBright.b*255)},.55)`],
      [0.5, `rgba(${Math.round(RED.r*255)},${Math.round(RED.g*255)},${Math.round(RED.b*255)},.22)`],
      [1, `rgba(${Math.round(RED.r*255)},${Math.round(RED.g*255)},${Math.round(RED.b*255)},0)`],
    ]),
    transparent: true, opacity: 0.3, blending: THREE.AdditiveBlending, depthWrite: false,
  }));
  // 2do achique (pedido del usuario 2026-09, con capturas en mano: seguia
  // "lavando" el iris a un mauve/rosa palido en vez de leerse oscuro). De
  // 3.2 a 1.35 no habia alcanzado -- el sprite seguia siendo mas GRANDE que
  // el iris (que llega a ~0.74*CORE_R como mucho, ver irisScale en
  // animate()), asi que sobraba glow bien por fuera del disco. Ahora
  // CORE_R*0.95 (apenas mas grande que el iris al maximo) + opacidad y
  // colores mas tenues -- deja solo un poco de rojo filtrandose justo en
  // el borde, como pedia la referencia original, sin taparle el negro.
  coreGlow.scale.set(CORE_R * 0.95, CORE_R * 0.95, 1);
  coreGroup.add(coreGlow);

  // -------- IRIS -- el "ojo" propiamente dicho. 3er rediseno (pedido
  // explicito del usuario, portado de la v4 actualizada del prototipo):
  // historial completo documentado ahi -- puntito plano -> "el iris ES
  // todo el circulo oscuro del medio" -> textura ORGANICA con fibras (lo
  // que este archivo tenia ANTES de este cambio) -> se probo una variante
  // ROBOTICA (rechazada, "prefiero el natural") -> el usuario penso mejor
  // y pidio volver a ALGO MAS SIMPLE que lo organico-fibroso: un disco
  // negro liso (SIN textura fibrosa visible) con un solo brillo especular,
  // tratando todo ese circulo negro como "el iris" que se mueve entero al
  // seguir el movimiento/audio. Nada de fibras/criptas/colarette -- solo
  // un degrade sutil + 1 glint. Sin colores del acento (--accent) en la
  // textura misma: el disco es negro puro en cualquier tema, el COLOR
  // (rojo/azul) sigue viniendo del nucleo/rim/halo detras (que
  // ya son conscientes del tema, ver RED/RED_BRIGHT arriba) -- no hace
  // falta derivar tonos del acento aca.
  function makeIrisTexture(size, reflectionImg) {
    const c = document.createElement("canvas");
    c.width = c.height = size;
    const ctx = c.getContext("2d");
    const cx = size / 2, cy = size / 2, R = size / 2;
    const hlX = cx + R * 0.18, hlY = cy - R * 0.2;

    ctx.save();
    ctx.beginPath();
    ctx.arc(cx, cy, R, 0, Math.PI * 2);
    ctx.clip();

    // Disco base -- 3ra pasada, todavia mas oscuro (pedido del usuario
    // 2026-09, 2 veces seguidas: "mas oscuro" / "mas oscuro todavia").
    const base = ctx.createRadialGradient(cx, cy, 0, cx, cy, R);
    base.addColorStop(0, "#040102");
    base.addColorStop(0.55, "#020101");
    base.addColorStop(0.85, "#010001");
    base.addColorStop(1, "#000000");
    ctx.fillStyle = base;
    ctx.fillRect(0, 0, size, size);

    // Sombreado ESFERICO (pedido del usuario 2026-09: "debe ser mas
    // esfera"): un 2do degrade, centrado en el MISMO punto que el brillo
    // (arriba-izq, como si viniera de ahi la luz) en vez de concentrico con
    // el disco -- asi el lado opuesto queda mas oscuro y el conjunto lee
    // como una bocha con volumen, no un circulo plano con un brillo
    // pegado encima.
    const sphereShade = ctx.createRadialGradient(hlX, hlY, R * 0.1, hlX, hlY, R * 1.35);
    sphereShade.addColorStop(0, "rgba(0,0,0,0)");
    sphereShade.addColorStop(0.55, "rgba(0,0,0,0)");
    sphereShade.addColorStop(1, "rgba(0,0,0,0.55)");
    ctx.fillStyle = sphereShade;
    ctx.fillRect(0, 0, size, size);

    // Anillos concentricos MUY sutiles ("como el obturador de una camara").
    // Solo 2 anillos finos -- insinuan el diafragma sin caer en textura
    // fibrosa/organica ni patron robotico, ambos rechazados antes.
    [0.42, 0.68].forEach((rr) => {
      ctx.beginPath();
      ctx.arc(cx, cy, R * rr, 0, Math.PI * 2);
      ctx.strokeStyle = "rgba(0,0,0,0.35)";
      ctx.lineWidth = size * 0.006;
      ctx.stroke();
    });

    // Reflejo del logo DENTRO del brillo (pedido explicito del usuario: "el
    // puntito es un reflejo, en el reflejo se ve el logo pequeño") -- se dibuja ANTES del glow blanco de abajo, clipeado
    // al mismo circulo del catchlight y bien tenue, para que el brillo
    // blanco se mezcle por encima y quede como un glint real (no una
    // calcomania plana). 2do ajuste (pedido del usuario, con captura en
    // mano: a tamaño real del ojo -- unos pocos px en pantalla -- el logo
    // de la 1ra pasada (R*0.1 de radio) era imperceptible sin hacer zoom
    // 5x. Agrandado a R*0.17 para que se note a simple vista sin dejar de
    // leerse como un reflejo (sigue con opacidad parcial, no un sticker).
    const HL_R = 0.17;
    if (reflectionImg) {
      ctx.save();
      ctx.beginPath();
      ctx.arc(hlX, hlY, R * HL_R, 0, Math.PI * 2);
      ctx.clip();
      ctx.globalAlpha = 0.55;
      const logoW = R * HL_R * 1.7;
      const logoH = logoW * (reflectionImg.height / reflectionImg.width || 0.5);
      ctx.drawImage(reflectionImg, hlX - logoW / 2, hlY - logoH / 2, logoW, logoH);
      ctx.restore();
    }

    // Brillo especular (catchlight) -- el glint real del reflejo, agrandado
    // en la misma proporcion que el logo (ver HL_R arriba) para que ambos
    // sigan quedando concentricos.
    const hl = ctx.createRadialGradient(hlX, hlY, 0, hlX, hlY, R * HL_R);
    hl.addColorStop(0, "rgba(255,255,255,0.55)");
    hl.addColorStop(1, "rgba(255,255,255,0)");
    ctx.fillStyle = hl;
    ctx.beginPath();
    ctx.arc(hlX, hlY, R * HL_R, 0, Math.PI * 2);
    ctx.fill();

    ctx.restore();
    return new THREE.CanvasTexture(c);
  }

  // Precarga del logo de Vision para el reflejo dentro del catchlight (ver
  // makeIrisTexture arriba) -- si falla (offline, 404) se sigue sin el
  // reflejo, nunca bloquea el arranque del ojo.
  const logoImg = await new Promise((resolve) => {
    const img = new Image();
    img.onload = () => resolve(img);
    img.onerror = () => resolve(null);
    img.src = "/assets/logo.png";
  });

  const irisMat = new THREE.SpriteMaterial({
    map: makeIrisTexture(512, logoImg),
    transparent: true, depthWrite: false, depthTest: false,
  });
  const irisSprite = new THREE.Sprite(irisMat);
  irisSprite.scale.set(1.0, 1.0, 1);
  irisSprite.position.z = CORE_R * 1.0;
  irisSprite.renderOrder = 10;
  coreGroup.add(irisSprite);

  // Parpado -- barra solida que barre verticalmente de vez en cuando
  const eyelidTexture = (() => {
    const c = document.createElement("canvas");
    c.width = c.height = 8;
    const ctx = c.getContext("2d");
    ctx.fillStyle = "#050203";
    ctx.fillRect(0, 0, 8, 8);
    return new THREE.CanvasTexture(c);
  })();
  const eyelidMat = new THREE.SpriteMaterial({ map: eyelidTexture, transparent: true, opacity: 0, depthWrite: false, depthTest: false });
  const eyelidSprite = new THREE.Sprite(eyelidMat);
  eyelidSprite.scale.set(CORE_R * 2.3, 0.02, 1);
  eyelidSprite.position.z = CORE_R * 1.05;
  eyelidSprite.renderOrder = 12;
  coreGroup.add(eyelidSprite);

  let blinkTimeoutId = null;
  function scheduleBlink() {
    const delay = 2800 + Math.random() * 4200;
    blinkTimeoutId = setTimeout(() => {
      if (window.anime) {
        window.anime({ targets: eyelidSprite.scale, y: [0.02, CORE_R * 2.4, 0.02], duration: 260, easing: "easeInOutQuad" });
        window.anime({ targets: eyelidMat, opacity: [0, 1, 0], duration: 260, easing: "easeInOutQuad" });
      }
      scheduleBlink();
    }, delay);
  }
  scheduleBlink();

  const haloRing = new THREE.Mesh(
    new THREE.TorusGeometry(2.05, 0.05, 16, 128),
    new THREE.MeshBasicMaterial({ color: RED_BRIGHT, transparent: true, opacity: 0.75, blending: THREE.AdditiveBlending })
  );
  haloRing.rotation.x = 0.08;
  scene.add(haloRing);

  // Ecualizador circular estereo -- mitad izquierda = canal L, mitad derecha = canal R
  const EQ_BAR_COUNT = 56, EQ_SEGMENTS = 6, EQ_INNER_R = 2.35, EQ_SEG_LEN = 0.13, EQ_SEG_GAP = 0.045;
  const eqSegGeo = new THREE.BoxGeometry(0.05, EQ_SEG_LEN, 0.05);
  const eqBars = [];
  for (let i = 0; i < EQ_BAR_COUNT; i++) {
    const angle = (i / EQ_BAR_COUNT) * Math.PI * 2;
    const isLeft = Math.cos(angle) < 0;
    const segments = [];
    for (let s = 0; s < EQ_SEGMENTS; s++) {
      const r = EQ_INNER_R + s * (EQ_SEG_LEN + EQ_SEG_GAP);
      const colorT = s / (EQ_SEGMENTS - 1);
      // eqColorDim/eqColorBright: mismo criterio que RED/RED_BRIGHT arriba
      // -- leidos del tema activo, no hex fijo -- para que el ecualizador
      // tambien pase a azul en tema azul.
      const color = eqColorDim.clone().lerp(eqColorBright, colorT);
      const mat = new THREE.MeshBasicMaterial({ color, transparent: true, opacity: 0.08, blending: THREE.AdditiveBlending, depthWrite: false });
      const seg = new THREE.Mesh(eqSegGeo, mat);
      seg.position.set(Math.cos(angle) * r, Math.sin(angle) * r, 0);
      seg.rotation.z = angle - Math.PI / 2;
      scene.add(seg);
      segments.push(seg);
    }
    eqBars.push({ index: i, isLeft, segments });
  }
  function updateEqualizerRing(t, levelL, levelR) {
    eqBars.forEach((bar) => {
      const bandNoise = Math.abs(
        Math.sin(bar.index * 0.4 + t * 2.6) * 0.5 +
        Math.sin(bar.index * 1.15 - t * 1.7) * 0.32 +
        Math.sin(t * 6 + bar.index) * 0.18
      );
      const channelLevel = bar.isLeft ? levelL : levelR;
      const level = Math.max(0.03, Math.min(1, channelLevel * (0.55 + 0.65 * bandNoise)));
      const litCount = level * EQ_SEGMENTS;
      bar.segments.forEach((seg, s) => {
        seg.material.opacity = 0.06 + Math.max(0, Math.min(1, litCount - s)) * 0.85;
      });
    });
  }

  const clock = new THREE.Clock();
  let coreKick = 1;

  function animate() {
    _animationFrameId = requestAnimationFrame(animate);
    const t = clock.getElapsedTime();

    updateSimulatedEnvelope(t);
    if (micLevel !== null) envelope = micLevel;
    const env = envelope;
    const { levelL, levelR } = updateStereoLevels(t);

    // Sin movimiento detectado por un rato (o en modo simulado, donde
    // nunca lo hay) -- el ojo se asienta mirando al CENTRO (a quien lo
    // esta viendo), con un microtemblor sutil para que no se sienta
    // "muerto". Antes vagaba en un patron amplio tipo Lissajous -- el
    // usuario pidio explicitamente que parezca que te mira, no que ande
    // mirando para cualquier lado sin motivo.
    if (t - lastMotionAt > 3.2) {
      if (t > nextSearchAt) {
        searchIndex = (searchIndex + 1) % SEARCH_POINTS.length;
        nextSearchAt = t + 1.4;
        snapUntil = t + 0.2; // snap rapido y mecanico entre puntos
      }
      const p = SEARCH_POINTS[searchIndex];
      // microtemblor chico ENCIMA del punto fijo, para que no quede
      // congelado del todo en cada parada (ver comentario del array arriba).
      eyeTarget.x = p.x + Math.sin(t * 0.8) * 0.03;
      eyeTarget.y = p.y + Math.cos(t * 0.6) * 0.025;
    }
    const stereoBias = Math.max(-1, Math.min(1, (levelR - levelL) * 1.4));
    const lerpFactor = t < snapUntil ? 0.38 : 0.09;
    eyeSmoothed.x += (eyeTarget.x - eyeSmoothed.x) * lerpFactor;
    eyeSmoothed.y += (eyeTarget.y - eyeSmoothed.y) * lerpFactor;
    // Correccion final del usuario (tajante, portada del prototipo): el
    // aro/circulo rojo tiene que ser grande (CORE_R=1.05, ver arriba) y la
    // "pelota" negra del medio (el iris) tiene que ser MUCHO MAS CHICA en
    // relacion a ese rojo -- no "casi todo el circulo" como se penso en el
    // 2do rediseno. Proporcion real medida sobre la captura de referencia
    // del usuario: el disco negro es ~0.5x el diametro del circulo rojo,
    // dejando un anillo de rojo bien visible alrededor.
    const desiredOffsetX = (eyeSmoothed.x + stereoBias * 0.3) * (CORE_R * 0.55);
    const desiredOffsetY = eyeSmoothed.y * (CORE_R * 0.55);
    const irisScale = CORE_R * (0.62 + env * 0.12);
    irisSprite.scale.setScalar(irisScale);
    const irisRadius = irisScale / 2;
    const safeReach = Math.max(0, CORE_R * 0.82 - irisRadius);
    const desiredLen = Math.hypot(desiredOffsetX, desiredOffsetY);
    const clampScale = desiredLen > safeReach && desiredLen > 0 ? safeReach / desiredLen : 1;
    irisSprite.position.x = desiredOffsetX * clampScale;
    irisSprite.position.y = desiredOffsetY * clampScale;

    coreGroup.scale.setScalar((1 + env * 0.22) * coreKick);
    coreKick += (1 - coreKick) * 0.08;
    rimMat.uniforms.intensity.value = 1.1 + env * 1.9;
    rimMat.uniforms.power.value = 2.7 - env * 1.1;
    rimMat.uniforms.time.value = t;
    atmoMat.uniforms.intensity.value = 0.6 + env * 1.1;
    atmoMat.uniforms.time.value = t * 0.7; // desfasada del rim para que no "respiren" identicas
    coreGlow.material.opacity = 0.4 + env * 0.5;
    haloRing.material.opacity = 0.45 + env * 0.5;
    haloRing.scale.setScalar(1 + env * 0.05);
    haloRing.rotation.z += 0.0015;
    updateEqualizerRing(t, levelL, levelR);

    camera.lookAt(0, 0.35, 0);
    renderer.render(scene, camera);

    const w = bloomTight.width, h = bloomTight.height;
    const srcW = renderer.domElement.width, srcH = renderer.domElement.height;
    if (w > 0 && h > 0 && srcW > 0 && srcH > 0) {
      bloomTightCtx.clearRect(0, 0, w, h);
      bloomTightCtx.drawImage(renderer.domElement, 0, 0, w, h);
      bloomWideCtx.clearRect(0, 0, w, h);
      bloomWideCtx.drawImage(renderer.domElement, 0, 0, w, h);
    }
    bloomWide.style.opacity = String(0.24 + env * 0.28);
  }
  animate();

  _resizeHandler = () => {
    const { w, h } = containerSize();
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
    renderer.setSize(w, h);
    resizeBloomCanvases();
  };
  window.addEventListener("resize", _resizeHandler);

  // -------- VISION POR CAMARA (REQ-011) --------
  const videoEl = document.createElement("video");
  videoEl.muted = true;
  videoEl.playsInline = true;
  videoEl.autoplay = true;

  const ANALYSIS_W = 48, ANALYSIS_H = 36;
  const analysisCanvas = document.createElement("canvas");
  analysisCanvas.width = ANALYSIS_W;
  analysisCanvas.height = ANALYSIS_H;
  const analysisCtx = analysisCanvas.getContext("2d", { willReadFrequently: true });
  let prevFrame = null;

  function detectMotion() {
    analysisCtx.drawImage(videoEl, 0, 0, ANALYSIS_W, ANALYSIS_H);
    const frame = analysisCtx.getImageData(0, 0, ANALYSIS_W, ANALYSIS_H).data;
    if (!prevFrame) { prevFrame = frame; return null; }
    let sumX = 0, sumY = 0, sumW = 0;
    for (let y = 0; y < ANALYSIS_H; y++) {
      for (let x = 0; x < ANALYSIS_W; x++) {
        const i = (y * ANALYSIS_W + x) * 4;
        const lum = frame[i] + frame[i + 1] + frame[i + 2];
        const prevLum = prevFrame[i] + prevFrame[i + 1] + prevFrame[i + 2];
        const diff = Math.abs(lum - prevLum);
        if (diff > 55) { sumX += x * diff; sumY += y * diff; sumW += diff; }
      }
    }
    prevFrame = frame;
    if (sumW < 1200) return null;
    return { x: sumX / sumW / ANALYSIS_W, y: sumY / sumW / ANALYSIS_H };
  }

  let lastOnsetAt = -10;
  let visionFrameId1 = null, visionFrameId2 = null;

  async function setupVision() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) return;
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ video: { width: 160, height: 120 }, audio: true });
      _mediaStream = stream;
      videoEl.srcObject = stream;
      await videoEl.play();

      const AudioCtx = window.AudioContext || window.webkitAudioContext;
      const audioCtx = new AudioCtx();
      _audioCtx = audioCtx;
      const source = audioCtx.createMediaStreamSource(stream);
      const analyser = audioCtx.createAnalyser();
      analyser.fftSize = 512;
      source.connect(analyser);
      const dataArray = new Uint8Array(analyser.fftSize);
      let micBaseline = 0.02;
      micLevel = 0.1;

      (function pollAudio() {
        visionFrameId1 = requestAnimationFrame(pollAudio);
        analyser.getByteTimeDomainData(dataArray);
        let sumSq = 0;
        for (let i = 0; i < dataArray.length; i++) {
          const v = (dataArray[i] - 128) / 128;
          sumSq += v * v;
        }
        const rms = Math.sqrt(sumSq / dataArray.length);
        micLevel += (Math.min(1, rms * 3) - micLevel) * 0.15;
        micBaseline += (rms - micBaseline) * 0.01;

        const t = clock.getElapsedTime();
        if (rms > micBaseline * 2.2 + 0.02 && t - lastOnsetAt > 1.3) {
          lastOnsetAt = t;
          snapUntil = t + 0.4;
          coreKick = 1.3;
        }
      })();

      let frameCounter = 0;
      (function pollVideo() {
        visionFrameId2 = requestAnimationFrame(pollVideo);
        frameCounter++;
        if (frameCounter % 4 !== 0) return;
        const m = detectMotion();
        if (m) {
          eyeTarget.x = -(m.x * 2 - 1);
          eyeTarget.y = -(m.y * 2 - 1);
          lastMotionAt = clock.getElapsedTime();
        }
      })();
    } catch (err) {
      // Permiso denegado o navegador sin soporte -- cae a modo "escaneo"
      // simulado (ya activo por defecto, ver updateSimulatedEnvelope y el
      // movimiento de escaneo dentro de animate()). No bloquea el resto
      // del modo reunion (REQ-011, ultimo criterio de aceptacion).
    }
  }
  // REQ-001 (spec pantalla-bienvenida-ojo): si useCamera=false, ni
  // siquiera se INTENTA pedir permisos -- el ojo arranca directo en modo
  // "escaneo" simulado (mismo fallback visual de siempre), sin mostrar
  // ningun prompt de camara/microfono al usuario.
  if (useCamera) setupVision();

  // Guarda referencias para poder limpiar todo en destroyVoiceEye()
  _renderer._voiceEyeCleanup = () => {
    if (blinkTimeoutId) clearTimeout(blinkTimeoutId);
    if (visionFrameId1) cancelAnimationFrame(visionFrameId1);
    if (visionFrameId2) cancelAnimationFrame(visionFrameId2);
  };
}

/**
 * Desmonta el ojo por completo: para el render loop, libera camara/
 * microfono (fundamental por privacidad -- REQ-011/categoria 7: nunca debe
 * quedar la camara prendida despues de salir del modo reunion), y limpia
 * el DOM del contenedor.
 */
export function destroyVoiceEye() {
  if (_animationFrameId) cancelAnimationFrame(_animationFrameId);
  _animationFrameId = null;

  if (_renderer && _renderer._voiceEyeCleanup) _renderer._voiceEyeCleanup();

  if (_mediaStream) {
    _mediaStream.getTracks().forEach((track) => track.stop());
    _mediaStream = null;
  }
  if (_audioCtx) {
    _audioCtx.close().catch(() => {});
    _audioCtx = null;
  }
  if (_resizeHandler) {
    window.removeEventListener("resize", _resizeHandler);
    _resizeHandler = null;
  }
  if (_renderer) {
    _renderer.dispose();
    _renderer = null;
  }
  if (_container) {
    _container.innerHTML = "";
    _container = null;
  }
}
