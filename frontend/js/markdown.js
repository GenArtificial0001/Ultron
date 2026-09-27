// Renderizador de Markdown propio, vanilla JS, sin dependencias externas
// (consistente con el resto del workspace: sin CDN, sin build step). Cubre
// el subconjunto que efectivamente usan los LLMs en respuestas de chat
// (headers, negrita/cursiva, listas, bloques de codigo con lenguaje,
// codigo inline, tablas, citas, links, hr) -- no es un parser CommonMark
// completo, es deliberadamente acotado y legible.
//
// Seguridad (anti-XSS): TODO el texto de entrada se escapa a entidades HTML
// ANTES de aplicar cualquier regla de markdown. Los unicos tags que este
// modulo genera son los que el codigo arma explicitamente mas abajo -- el
// contenido del usuario/LLM nunca puede inyectar un tag propio, porque ya
// fue escapado. Los links solo se generan si el esquema es http(s) o un
// path relativo; cualquier otro esquema (ej. `javascript:`) se descarta.

const ESCAPE_MAP = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
function escapeHtml(text) {
  return String(text ?? "").replace(/[&<>"']/g, (ch) => ESCAPE_MAP[ch]);
}

function isSafeUrl(url) {
  const trimmed = (url || "").trim();
  if (/^(https?:)?\/\//i.test(trimmed)) return true; // http(s):// o //host
  if (/^[./#]/.test(trimmed)) return true; // relativo o ancla
  return false;
}

// REQ-001/002/006 (spec fix-latex-corrompido-por-markdown, 2026-09-26): BUG
// REAL reportado por el usuario con evidencia -- una formula LaTeX con un
// subindice tipo "_s" (ej. \text{Cobertura}_s, T_s, z_0 -- notacion
// extremadamente comun en matematica/fisica/quimica) se mostraba CRUDA en
// el chat en vez de renderizada por KaTeX. Causa raiz: `renderInline()` mas
// abajo interpreta "_x_" como *cursiva* de markdown -- corre sobre el texto
// COMPLETO antes de que KaTeX (typesetMath, llamado despues desde app.js)
// tenga oportunidad de ver los delimitadores. Un guion bajo de subindice
// dentro de "$...$"/"$$...$$" podia ser tomado como apertura/cierre de
// cursiva, comiendose el "_" y partiendo el HTML en fragmentos -- para
// cuando KaTeX escaneaba el DOM, la formula ya estaba rota.
//
// Mismos delimitadores que usa KATEX_DELIMITERS en math.js y
// LATEX_SEGMENT_RE en latex-to-unicode.js (duplicado a proposito -- modulos
// standalone, mismo criterio de independencia que el resto del proyecto):
// "$$" antes que "$" (si no, "$$" quedaria atrapado como 2 formulas vacias).
const LATEX_SEGMENT_RE = /\$\$[\s\S]+?\$\$|\$[^$\n]+?\$|\\\([\s\S]+?\\\)|\\\[[\s\S]+?\\\]/g;

// Extrae cada segmento LaTeX delimitado a un placeholder de un solo caracter
// nulo + indice (mismo patron que ya usan los bloques de codigo mas abajo,
// `\u0000BLOCKn\u0000`) -- ningun regex de renderInline/toPlainText matchea
// contra "\u0000" ni digitos, asi que el placeholder atraviesa TODO el
// resto del pipeline (negrita, cursiva, listas, tablas) sin que nada lo
// toque, hasta que se restaura tal cual al final.
function extractMathPlaceholders(text) {
  const segments = [];
  const out = text.replace(LATEX_SEGMENT_RE, (full) => {
    const idx = segments.length;
    segments.push(full);
    return `\u0000MATH${idx}\u0000`;
  });
  return { text: out, segments };
}

function restoreMathPlaceholders(text, segments, transform) {
  return text.replace(/\u0000MATH(\d+)\u0000/g, (_, i) => transform(segments[Number(i)]));
}

// -- Inline: negrita, cursiva, tachado, codigo, links -----------------------
function renderInline(text) {
  // El texto que llega aqui YA esta escapado (ver renderMarkdown), asi que
  // es seguro insertar tags alrededor sin volver a escapar.
  let out = text;

  // Codigo inline `...` primero (para que su contenido no procese ** o _)
  const codeSpans = [];
  out = out.replace(/`([^`\n]+)`/g, (_, code) => {
    codeSpans.push(code);
    return `\u0000CODE${codeSpans.length - 1}\u0000`;
  });

  // Links [texto](url) e imagenes ![alt](url) -- imagenes se muestran como
  // link de texto (no se embeben <img> remotas por defecto, evita leaks de
  // referrer/tracking en un contexto corporativo).
  out = out.replace(/!?\[([^\]]*)\]\(([^)\s]+)(?:\s+"[^"]*")?\)/g, (match, label, url) => {
    if (!isSafeUrl(url)) return escapeHtml(match);
    return `<a href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer">${label || escapeHtml(url)}</a>`;
  });

  // Negrita+cursiva ***x***, negrita **x**/__x__, cursiva *x*/_x_, tachado ~~x~~
  out = out.replace(/\*\*\*([^*]+)\*\*\*/g, "<strong><em>$1</em></strong>");
  out = out.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  out = out.replace(/__([^_]+)__/g, "<strong>$1</strong>");
  out = out.replace(/~~([^~]+)~~/g, "<del>$1</del>");
  out = out.replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, "$1<em>$2</em>");
  out = out.replace(/(^|[^_\w])_([^_\n]+)_(?!_)/g, "$1<em>$2</em>");

  // Restaura codigo inline
  out = out.replace(/\u0000CODE(\d+)\u0000/g, (_, i) => `<code>${codeSpans[Number(i)]}</code>`);

  return out;
}

function renderTableIfPresent(lines, startIdx) {
  // Detecta una tabla GFM: linea de headers | separador (---|---) | filas
  if (startIdx + 1 >= lines.length) return null;
  const headerLine = lines[startIdx];
  const sepLine = lines[startIdx + 1];
  if (!headerLine.includes("|") || !/^[\s|:-]+$/.test(sepLine) || !sepLine.includes("-")) return null;

  const splitRow = (line) =>
    line.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());

  const headers = splitRow(headerLine);
  const rows = [];
  let i = startIdx + 2;
  while (i < lines.length && lines[i].includes("|") && lines[i].trim() !== "") {
    rows.push(splitRow(lines[i]));
    i++;
  }

  const thead = `<thead><tr>${headers.map((h) => `<th>${renderInline(h)}</th>`).join("")}</tr></thead>`;
  const tbody = `<tbody>${rows.map((r) => `<tr>${r.map((c) => `<td>${renderInline(c)}</td>`).join("")}</tr>`).join("")}</tbody>`;
  return { html: `<table class="md-table">${thead}${tbody}</table>`, nextIdx: i };
}

/**
 * REQ-001 (spec copiar-mensajes): version "texto plano" de un mensaje --
 * quita el markup de markdown (headers, negrita/cursiva, código inline,
 * marcadores de lista, blockquote) para que copiar una respuesta no
 * arrastre `**`/backticks/`- ` sueltos. Heurística simple con regex (no
 * un parser completo): suficiente para el subconjunto de markdown que
 * `renderMarkdown` ya soporta arriba.
 * @param {string} raw
 * @returns {string} texto plano legible
 */
export function toPlainText(raw) {
  if (!raw) return "";
  let text = String(raw);

  // Bloques de código: se conserva el contenido, se quitan las comillas
  // triples y la etiqueta de lenguaje.
  text = text.replace(/```(\w*)\n?([\s\S]*?)```/g, (_, _lang, code) => code.replace(/\n$/, ""));

  // REQ-003 (spec fix-latex-corrompido-por-markdown): mismo mecanismo de
  // extraccion/restauracion que renderMarkdown -- sin esto, una formula con
  // subindice ($T_s$) perdia el "_" al pasar por la regla de cursiva de
  // abajo (`_([^_\n]+)_` -> "$1"), dejando "$Ts$" en el texto copiado/leido
  // por voz en vez de "$T_s$". Sin escapado HTML aca (toPlainText no genera
  // HTML) -- se restaura el LaTeX tal cual.
  const mathExtraction = extractMathPlaceholders(text);
  text = mathExtraction.text;

  text = text
    .replace(/^#{1,6}\s+/gm, "")            // headers
    .replace(/^>\s?/gm, "")                  // blockquote
    .replace(/^[-*+]\s+/gm, "• ")            // lista desordenada -> viñeta simple
    .replace(/^\d+[.)]\s+/gm, (m) => m)      // lista ordenada: se deja el numero tal cual
    .replace(/\*\*\*([^*]+)\*\*\*/g, "$1")
    .replace(/\*\*([^*]+)\*\*/g, "$1")
    .replace(/__([^_]+)__/g, "$1")
    .replace(/~~([^~]+)~~/g, "$1")
    .replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, "$1$2")
    .replace(/(^|[^_\w])_([^_\n]+)_(?!_)/g, "$1$2")
    .replace(/`([^`\n]+)`/g, "$1")           // código inline
    .replace(/!?\[([^\]]*)\]\(([^)\s]+)(?:\s+"[^"]*")?\)/g, "$1"); // links -> solo el texto

  text = restoreMathPlaceholders(text, mathExtraction.segments, (s) => s);
  return text;
}

/**
 * Convierte texto markdown (ya en crudo, SIN escapar) a HTML seguro.
 * @param {string} raw
 * @returns {string} HTML listo para asignar a innerHTML
 */
export function renderMarkdown(raw) {
  if (!raw) return "";

  // 1) Extrae bloques de codigo ```lang\n...\n``` primero y los reemplaza
  //    por placeholders, para que su contenido nunca se procese como
  //    markdown ni se re-escape dos veces.
  const codeBlocks = [];
  let text = String(raw).replace(/```(\w*)\n?([\s\S]*?)```/g, (_, lang, code) => {
    codeBlocks.push({ lang: lang.trim(), code: code.replace(/\n$/, "") });
    return `\n\u0000BLOCK${codeBlocks.length - 1}\u0000\n`;
  });

  // 1.5) REQ-001/002/005/006: extrae formulas LaTeX delimitadas ANTES de
  // escapar/procesar nada mas -- corre DESPUES de la extraccion de bloques
  // de codigo de arriba (un "$x_1$" de ejemplo dentro de un bloque ya quedo
  // reemplazado por su placeholder \u0000BLOCKn\u0000, asi que este paso
  // nunca lo ve -- REQ-005 satisfecho sin logica extra). Guarda el LaTeX
  // crudo tal cual (sin escapar todavia) para restaurarlo mas abajo.
  const mathExtraction = extractMathPlaceholders(text);
  text = mathExtraction.text;

  // 2) Escapa TODO el resto del texto antes de aplicar cualquier regla.
  text = escapeHtml(text);
  // El escape de arriba tambien afecto los placeholders (no tienen
  // caracteres especiales, asi que quedan intactos: \u0000BLOCKn\u0000 y
  // \u0000MATHn\u0000).

  const lines = text.split("\n");
  const htmlParts = [];
  let paragraphBuf = [];
  let listBuf = null; // { type: 'ul'|'ol', items: [] }

  const flushParagraph = () => {
    if (paragraphBuf.length === 0) return;
    htmlParts.push(`<p>${renderInline(paragraphBuf.join(" "))}</p>`);
    paragraphBuf = [];
  };
  const flushList = () => {
    if (!listBuf) return;
    const tag = listBuf.type;
    htmlParts.push(`<${tag} class="md-list">${listBuf.items.map((it) => `<li>${renderInline(it)}</li>`).join("")}</${tag}>`);
    listBuf = null;
  };

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const trimmed = line.trim();

    // Placeholder de bloque de codigo
    const blockMatch = trimmed.match(/^\u0000BLOCK(\d+)\u0000$/);
    if (blockMatch) {
      flushParagraph();
      flushList();
      const { lang, code } = codeBlocks[Number(blockMatch[1])];
      const langLabel = lang ? `<span class="md-code-lang">${escapeHtml(lang)}</span>` : "";
      // REQ-003 (spec copiar-mensajes): `data-raw-code` guarda el codigo
      // SIN escapar HTML (decodificado) para que el boton de copiar del
      // bloque no tenga que revertir el escape del innerHTML a mano.
      const rawAttr = escapeHtml(code).replace(/\n/g, "&#10;");
      htmlParts.push(
        `<div class="md-codeblock" data-raw-code="${rawAttr}">${langLabel}<pre><code>${code}</code></pre></div>`
      );
      continue;
    }

    if (trimmed === "") {
      flushParagraph();
      flushList();
      continue;
    }

    // Headers
    const headerMatch = trimmed.match(/^(#{1,6})\s+(.*)$/);
    if (headerMatch) {
      flushParagraph();
      flushList();
      const level = headerMatch[1].length;
      htmlParts.push(`<h${level} class="md-h">${renderInline(headerMatch[2])}</h${level}>`);
      continue;
    }

    // Regla horizontal
    if (/^(-{3,}|\*{3,}|_{3,})$/.test(trimmed)) {
      flushParagraph();
      flushList();
      htmlParts.push('<hr class="md-hr">');
      continue;
    }

    // Cita
    if (/^&gt;\s?/.test(trimmed)) {
      flushParagraph();
      flushList();
      htmlParts.push(`<blockquote class="md-quote">${renderInline(trimmed.replace(/^&gt;\s?/, ""))}</blockquote>`);
      continue;
    }

    // Tabla GFM
    const table = renderTableIfPresent(lines, i);
    if (table) {
      flushParagraph();
      flushList();
      htmlParts.push(table.html);
      i = table.nextIdx - 1;
      continue;
    }

    // Lista desordenada
    const ulMatch = trimmed.match(/^[-*+]\s+(.*)$/);
    if (ulMatch) {
      flushParagraph();
      if (!listBuf || listBuf.type !== "ul") {
        flushList();
        listBuf = { type: "ul", items: [] };
      }
      listBuf.items.push(ulMatch[1]);
      continue;
    }

    // Lista ordenada
    const olMatch = trimmed.match(/^\d+[.)]\s+(.*)$/);
    if (olMatch) {
      flushParagraph();
      if (!listBuf || listBuf.type !== "ol") {
        flushList();
        listBuf = { type: "ol", items: [] };
      }
      listBuf.items.push(olMatch[1]);
      continue;
    }

    // Parrafo normal (acumula lineas seguidas en el mismo <p>)
    flushList();
    paragraphBuf.push(trimmed);
  }

  flushParagraph();
  flushList();

  // REQ-001/002/006: restaura cada formula LaTeX tal cual estaba, recien
  // ACA -- ya paso por renderInline/listas/tablas sin que nada la toque
  // (el placeholder \u0000MATHn\u0000 es opaco a todos esos regex). Se
  // escapa a entidades HTML igual que el resto del texto (REQ de
  // seguridad: no se debilita el modelo anti-XSS existente) -- KaTeX
  // interpreta bien su sintaxis aunque los caracteres especiales de HTML
  // esten escapados como entidades (el navegador los decodifica antes de
  // que el texto le llegue a auto-render.min.js via el DOM).
  const html = htmlParts.join("\n");
  return restoreMathPlaceholders(html, mathExtraction.segments, escapeHtml);
}
