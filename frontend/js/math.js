// Renderizado de notacion matematica/quimica/estadistica/griega en el
// chat -- pedido explicito del usuario: "todo debe ser totalmente
// legible para el ojo humano incluyendo simbolos matematicos, quimicos,
// estadisticos, griegos, y demas todo debe ser exacto". Causa del bug
// original: el renderer de markdown propio de Vision (markdown.js) no
// interpreta LaTeX -- una respuesta con `$\text{Rendimiento} =
// \frac{m_{obtenida}}{m_{teorica}} \times 100$` se mostraba literal.
//
// ROLLBACK 2026-09-07 (pedido explicito del usuario): se uso MathJax v3
// primero, pero el usuario pidio pasar a la alternativa MAS LIVIANA que
// se le habia sugerido en su momento -- KaTeX (la misma libreria que usa
// Khan Academy, mucho mas rapida/chica que MathJax). Vendorizado sin CDN
// (ver frontend/vendor/katex/*.LICENSE.txt): katex.min.js + katex.min.css
// + fuentes .woff2 (KaTeX SI necesita fuentes web, a diferencia de la
// salida SVG de MathJax) + auto-render.min.js (extension oficial que
// escanea el DOM buscando los delimitadores, mismo rol que
// MathJax.typesetPromise) + mhchem.min.js (extension de quimica, \ce{},
// igual que con MathJax -- NINGUNA de las 2 opciones trae quimica gratis
// en el core).

const KATEX_CSS_HREF = "./vendor/katex/katex.min.css";
const KATEX_JS_SRC = "./vendor/katex/katex.min.js";
const KATEX_AUTORENDER_SRC = "./vendor/katex/auto-render.min.js";
const KATEX_MHCHEM_SRC = "./vendor/katex/mhchem.min.js";

// Delimitadores estandar que ya usan los LLMs (Claude/GPT/Gemini) al
// responder con notacion matematica -- mismo criterio que se uso con
// MathJax, para no tener que cambiar el prompt del backend. Orden
// importante: "$$" debe evaluarse ANTES que "$" (si no, "$$" quedaria
// atrapado como 2 formulas vacias "$" + "$").
const KATEX_DELIMITERS = [
  { left: "$$", right: "$$", display: true },
  { left: "$", right: "$", display: false },
  { left: "\\(", right: "\\)", display: false },
  { left: "\\[", right: "\\]", display: true },
];

// Bug real reportado por el usuario: "las formulas y calculos se siguen
// viendo raras" -- letras acentuadas (o, a, e, i, u, n con tilde) dentro
// de \text{...} se veian partidas (ej. "teórica" se dibujaba como
// "te" + "o" + un acento flotando aparte en otra linea). Causa raiz
// confirmada leyendo el propio katex.min.js vendorizado: KaTeX v0.18.7 NO
// tiene una tabla de nombres para letras latinas acentuadas (a diferencia
// de \'{o}, que si esta soportado nativamente, ver support_table oficial)
// -- al encontrar el caracter Unicode compuesto "\u00f3" dentro de \text{},
// lo trata como un comando de acentuacion matematica suelto en vez de
// como texto literal.
//
// Fix elegido (de 3 alternativas consultadas al usuario) tras confirmar
// un requisito explicito: "que pueda copiar y pegar a otro chat sin
// problemas de formato". Este fix SOLO transforma la copia de texto que
// se le pasa a KaTeX para renderizar visualmente -- el `rawText` que usan
// los botones de copiar (ver buildMessageCopyControls en app.js) nunca
// pasa por esta funcion, sigue siendo el LaTeX original tal cual lo
// escribio el LLM. Se usa el callback oficial `preProcess` de la propia
// extension auto-render de KaTeX (ver katex.org/docs/autorender.html),
// pensado exactamente para este caso de uso.
//
// Mapa de acentos latinos comunes en espanol -> su comando LaTeX nativo
// (todos confirmados soportados dentro de \text{} en la support table
// oficial de KaTeX: \'{a}, \'{e}, \'{i}, \'{o}, \'{u}, \~{n}).
const ACCENT_TO_LATEX = {
  "\u00e1": "\\'{a}", "\u00c1": "\\'{A}",
  "\u00e9": "\\'{e}", "\u00c9": "\\'{E}",
  "\u00ed": "\\'{i}", "\u00cd": "\\'{I}",
  "\u00f3": "\\'{o}", "\u00d3": "\\'{O}",
  "\u00fa": "\\'{u}", "\u00da": "\\'{U}",
  "\u00f1": "\\~{n}", "\u00d1": "\\~{N}",
  "\u00fc": "\\\"{u}", "\u00dc": "\\\"{U}",
};
const ACCENT_RE = new RegExp(`[${Object.keys(ACCENT_TO_LATEX).join("")}]`, "g");

// Reemplaza acentos latinos SOLO dentro del contenido de \text{...}/
// \textrm{...}/\mathrm{...} (donde el bug ocurre) -- fuera de esos
// comandos una letra acentuada suelta ya la maneja bien KaTeX (aparece
// como variable/simbolo normal, no como texto). Regex no-greedy con
// balanceo simple de UN nivel de llaves (suficiente para el caso real:
// el LLM no anida \text dentro de \text).
function normalizeAccentedTextForKatex(math) {
  return math.replace(/\\(?:text|textrm|mathrm)\{([^{}]*)\}/g, (full, inner) => {
    if (!ACCENT_RE.test(inner)) return full; // sin acentos -- no tocar, evita trabajo de mas
    const fixedInner = inner.replace(ACCENT_RE, (ch) => ACCENT_TO_LATEX[ch]);
    return full.replace(inner, fixedInner);
  });
}

let _katexLoadPromise = null;

function loadScript(src) {
  return new Promise((resolve, reject) => {
    const script = document.createElement("script");
    script.src = src;
    script.onload = resolve;
    script.onerror = reject;
    document.head.appendChild(script);
  });
}

function ensureKatexCssLoaded() {
  if (document.querySelector(`link[href="${KATEX_CSS_HREF}"]`)) return;
  const link = document.createElement("link");
  link.rel = "stylesheet";
  link.href = KATEX_CSS_HREF;
  document.head.appendChild(link);
}

// Carga perezosa (solo la 1ra vez que se necesita, no al arrancar la
// app): katex.min.js primero, DESPUES auto-render.min.js Y mhchem.min.js
// en paralelo (mhchem solo necesita que `katex` global ya exista, no
// depende de auto-render). El CSS se inyecta en paralelo tambien -- no
// bloquea el renderizado JS.
function loadKatex() {
  if (_katexLoadPromise) return _katexLoadPromise;
  ensureKatexCssLoaded();
  _katexLoadPromise = loadScript(KATEX_JS_SRC).then(() =>
    Promise.all([loadScript(KATEX_AUTORENDER_SRC), loadScript(KATEX_MHCHEM_SRC)])
  );
  return _katexLoadPromise;
}

// Debounce global (no por elemento): durante el streaming de una
// respuesta, `renderMarkdown()` se re-ejecuta en CADA delta -- llamar a
// KaTeX en cada uno seria innecesario y causaria parpadeo visible. Se
// agrupan varias llamadas seguidas en una sola, disparada recien cuando
// el texto deja de cambiar por ~250ms.
let _typesetDebounceTimer = null;
const TYPESET_DEBOUNCE_MS = 250;

/**
 * Tipsetea las formulas LaTeX dentro de `container` (un elemento del DOM
 * ya con `innerHTML` insertado por renderMarkdown). Falla en silencio si
 * KaTeX no pudo cargar -- el resto del chat sigue funcionando normal,
 * solo el LaTeX queda sin interpretar.
 * @param {HTMLElement} container
 * @param {boolean} immediate si true, tipsetea ya mismo sin debounce (usar
 *   para el mensaje ya completo/final, no para deltas de streaming).
 */
export function typesetMath(container, { immediate = false } = {}) {
  if (!container) return;

  const run = () => {
    loadKatex()
      .then(() => {
        window.renderMathInElement(container, {
          delimiters: KATEX_DELIMITERS,
          throwOnError: false, // una formula con un error de sintaxis se muestra en rojo, no rompe el resto del mensaje
          ignoredTags: ["script", "noscript", "style", "textarea", "pre", "code"],
          preProcess: normalizeAccentedTextForKatex,
        });
      })
      .catch(() => {
        // KaTeX no disponible -- el LaTeX queda como texto crudo, pero no rompe nada del resto del chat.
      });
  };

  if (immediate) {
    clearTimeout(_typesetDebounceTimer);
    run();
    return;
  }

  clearTimeout(_typesetDebounceTimer);
  _typesetDebounceTimer = setTimeout(run, TYPESET_DEBOUNCE_MS);
}
