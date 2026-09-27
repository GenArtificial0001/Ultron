// REQ-001/002 (spec copiar-formulas-unicode, 2026-09-26): convierte formulas
// LaTeX de una respuesta del asistente a caracteres Unicode reales (α, √, ²,
// ₂, ×, etc.) para que se puedan copiar y pegar FUERA de Vision (WhatsApp,
// email, otro chat) sin que se vea el LaTeX crudo con backslashes. Modulo
// standalone (mismo criterio de math.js: sin acoplarse a app.js), pensado
// SOLO para el texto que se COPIA -- el renderizado visual en pantalla
// (KaTeX via math.js/typesetMath) no se toca para nada.
//
// Rediseño 2026-09-27 (Claude Code, fórmula integral de Cauchy copiada a
// WhatsApp por el usuario). La primera version pasaba la formula ENTERA por
// `unicodeit.replace()` y parchaba con expresiones regulares lo que la
// libreria no sabia hacer (fracciones, quimica, exponentes con griegas). Cada
// parche arreglaba un caso y dejaba otros rotos: dentro de \frac quedaban
// `\pi` y `z_0` crudos, `^{n+1}` se aplanaba a `^n+1` (cambiaba el
// significado), y unicodeit reconoce comandos por PREFIJO (`\operatorname` ->
// "øperatorname", `\lim` -> "łim"). Ahora un lector propio recorre el LaTeX
// respetando llaves anidadas y decide la estructura (fracciones, raices,
// exponentes/subindices, texto, quimica); unicodeit queda SOLO como tabla de
// simbolos, consultada con un comando completo por vez. El corpus de
// `tests/test_e2e_formulas_corpus.py` fija el resultado esperado de cada
// construccion.
//
// Libreria usada: unicodeit (vendorizada, frontend/vendor/unicodeit/, ver
// LICENSE.txt ahi para el detalle del bundle armado a mano -- unicodeit no
// publica un build standalone para navegador).

const UNICODEIT_JS_SRC = "./vendor/unicodeit/unicodeit.min.js";

let _loadPromise = null;

function loadUnicodeit() {
  if (_loadPromise) return _loadPromise;
  _loadPromise = new Promise((resolve, reject) => {
    if (window.unicodeit) {
      resolve();
      return;
    }
    const script = document.createElement("script");
    script.src = UNICODEIT_JS_SRC;
    script.onload = resolve;
    script.onerror = reject;
    document.head.appendChild(script);
  });
  return _loadPromise;
}

// Mismos delimitadores que usa KATEX_DELIMITERS en math.js (orden importa:
// "$$" antes que "$") -- se busca dar la MISMA interpretacion de que es o no
// una formula que el renderizado visual, para que "lo que ves es lo que se
// copia" (REQ-001). Un "$" suelto de precio/moneda sin cierre no matchea
// (requiere un segundo "$" en la misma linea), mismo riesgo ya aceptado por
// el renderizado visual existente.
const LATEX_SEGMENT_RE = /\$\$([\s\S]+?)\$\$|\$([^$\n]+?)\$|\\\(([\s\S]+?)\\\)|\\\[([\s\S]+?)\\\]/g;

// -------------------------------------------------------------------------
// Super/subindices Unicode. No existen para todos los caracteres (ej. no hay
// "π" en superindice ni "C" en subindice): si alguno falta, se usa la forma
// legible "^(…)"/"_(…)" en vez de dejar un caracter roto.
// -------------------------------------------------------------------------
const SUPERSCRIPT = {
  0: "⁰", 1: "¹", 2: "²", 3: "³", 4: "⁴", 5: "⁵", 6: "⁶", 7: "⁷", 8: "⁸", 9: "⁹",
  "+": "⁺", "-": "⁻", "−": "⁻", "=": "⁼", "(": "⁽", ")": "⁾",
  a: "ᵃ", b: "ᵇ", c: "ᶜ", d: "ᵈ", e: "ᵉ", f: "ᶠ", g: "ᵍ", h: "ʰ", i: "ⁱ", j: "ʲ", k: "ᵏ", l: "ˡ", m: "ᵐ",
  n: "ⁿ", o: "ᵒ", p: "ᵖ", r: "ʳ", s: "ˢ", t: "ᵗ", u: "ᵘ", v: "ᵛ", w: "ʷ", x: "ˣ", y: "ʸ", z: "ᶻ",
  A: "ᴬ", B: "ᴮ", D: "ᴰ", E: "ᴱ", G: "ᴳ", H: "ᴴ", I: "ᴵ", J: "ᴶ", K: "ᴷ", L: "ᴸ", M: "ᴹ", N: "ᴺ",
  O: "ᴼ", P: "ᴾ", R: "ᴿ", T: "ᵀ", U: "ᵁ", V: "ⱽ", W: "ᵂ",
  // Sin "θ": su superindice (U+1DBF) no esta en muchas fuentes de celulares.
  "β": "ᵝ", "γ": "ᵞ", "δ": "ᵟ", "φ": "ᵠ", "χ": "ᵡ",
};
const SUBSCRIPT = {
  0: "₀", 1: "₁", 2: "₂", 3: "₃", 4: "₄", 5: "₅", 6: "₆", 7: "₇", 8: "₈", 9: "₉",
  "+": "₊", "-": "₋", "−": "₋", "=": "₌", "(": "₍", ")": "₎",
  a: "ₐ", e: "ₑ", h: "ₕ", i: "ᵢ", j: "ⱼ", k: "ₖ", l: "ₗ", m: "ₘ", n: "ₙ", o: "ₒ", p: "ₚ",
  r: "ᵣ", s: "ₛ", t: "ₜ", u: "ᵤ", v: "ᵥ", x: "ₓ",
  "β": "ᵦ", "γ": "ᵧ", "ρ": "ᵨ", "φ": "ᵩ", "χ": "ᵪ",
};
const SCRIPT_CHARS = new Set([...Object.values(SUPERSCRIPT), ...Object.values(SUBSCRIPT)]);

// Un exponente o subindice que YA trae uno adentro (e^{-x^2}, \int_{x_0})
// no se aplana: "e⁻ˣ²" o "ₓ₀" pierden el anidamiento y se pueden leer mal.
// Esos van con parentesis: "e^(−x²)", "∫_(x₀)ˣ".
function mapAll(text, table) {
  let out = "";
  for (const ch of text) {
    const mapped = table[ch];
    if (mapped === undefined) return null;
    out += mapped;
  }
  return out;
}

// `plain` ya convertido (sin LaTeX). Los espacios no significan nada dentro de
// un exponente/subindice de LaTeX, asi que se descartan.
function scriptOut(marker, plain) {
  const s = plain.replaceAll(FRAC_END, "").replace(/\s+/g, "");
  if (!s) return "";
  if (marker === "^") {
    if (s === "∘" || s === "°") return "°"; // 25^\circ
    if (s === "*" || s === "∗") return "*"; // Q^*
    if (/^[′']+$/.test(s)) return s.replace(/'/g, "′");
  }
  const mapped = mapAll(s, marker === "^" ? SUPERSCRIPT : SUBSCRIPT);
  if (mapped !== null) return mapped;
  if ([...s].length === 1) return marker + s; // ∮_C, σ_d, e^π
  // Subindice que es una palabra (m_obtenida): formato acordado con el
  // usuario en REQ-003, sufijo sin parentesis.
  if (marker === "_" && /^\p{L}+$/u.test(s)) return marker + s;
  return `${marker}(${s})`;
}

// -------------------------------------------------------------------------
// Lector de LaTeX: comandos (\alpha, \frac, \, ...), grupos {…} con llaves
// anidadas, espacios y caracteres sueltos.
// -------------------------------------------------------------------------
function closingBrace(s, open) {
  let depth = 0;
  for (let j = open; j < s.length; j++) {
    if (s[j] === "\\") {
      j++;
      continue;
    }
    if (s[j] === "{") depth++;
    else if (s[j] === "}" && --depth === 0) return j;
  }
  return s.length; // llave sin cerrar: el grupo llega hasta el final
}

function tokenize(s) {
  const toks = [];
  let i = 0;
  while (i < s.length) {
    const ch = s[i];
    if (ch === "\\") {
      const m = /^\\([A-Za-z]+\*?|[^A-Za-z])/.exec(s.slice(i));
      if (!m) {
        i++;
        continue;
      }
      toks.push({ t: "cmd", v: m[1] });
      i += m[0].length;
    } else if (ch === "{") {
      const end = closingBrace(s, i);
      toks.push({ t: "group", v: s.slice(i + 1, end) });
      i = end + 1;
    } else if (ch === "}") {
      i++; // llave suelta sin abrir: se ignora
    } else if (/\s/.test(ch)) {
      let j = i;
      while (j < s.length && /\s/.test(s[j])) j++;
      toks.push({ t: "space", newline: s.slice(i, j).includes("\n") });
      i = j;
    } else {
      toks.push({ t: "char", v: ch });
      i++;
    }
  }
  return toks;
}

function rawOf(tok) {
  if (!tok) return "";
  if (tok.t === "cmd") return `\\${tok.v}`;
  if (tok.t === "group") return `{${tok.v}}`;
  if (tok.t === "space") return " ";
  return tok.v;
}

// -------------------------------------------------------------------------
// Tablas de comandos
// -------------------------------------------------------------------------
// Nombres de funcion: van como texto ("sin", "lim"), nunca a unicodeit (que
// los confunde por prefijo: \lim -> "łim").
const FUNCTION_NAMES = new Set(
  ("sin cos tan cot sec csc arcsin arccos arctan sinh cosh tanh coth log ln lg exp lim limsup liminf " +
    "max min sup inf det dim ker arg deg gcd lcm Pr hom").split(" ")
);
// Operadores con limites (∫₀¹, ∑ᵢ₌₁ⁿ, lim_(x→0)): ver emitWithLimits.
const BIG_OPERATORS = new Set(
  "int iint iiint oint oiint sum prod coprod bigcup bigcap bigoplus bigotimes bigsqcup".split(" ")
);
const LIMIT_FUNCTIONS = new Set("lim limsup liminf max min sup inf".split(" "));
// Simbolos "de letra": LaTeX no deja espacio entre ellos y lo que sigue
// (2\pi i se ve "2πi", \Delta x se ve "Δx"), asi que el espacio que los
// separa en el codigo fuente no se copia.
const LETTER_LIKE = new Set(
  ("alpha beta gamma delta epsilon varepsilon zeta eta theta vartheta iota kappa lambda mu nu xi omicron " +
    "pi varpi rho varrho sigma varsigma tau upsilon phi varphi chi psi omega Gamma Delta Theta Lambda Xi Pi " +
    "Sigma Upsilon Phi Psi Omega partial nabla hbar ell forall exists nexists neg").split(" ")
);
const TEXT_COMMANDS = new Set(
  ("text textrm textit textbf textsf texttt textnormal mathrm mathit mathsf mathtt mathnormal " +
    "operatorname operatorname* mbox hbox").split(" ")
);
const STYLE_COMMANDS = new Set("mathbf boldsymbol bm pmb".split(" "));
const FONT_COMMANDS = new Set("mathbb mathcal mathfrak mathscr".split(" "));
const ACCENTS = {
  bar: "\u0304", overline: "\u0305", hat: "\u0302", widehat: "\u0302", tilde: "\u0303", widetilde: "\u0303",
  vec: "\u20d7", overrightarrow: "\u20d7", dot: "\u0307", ddot: "\u0308", check: "\u030c", breve: "\u0306",
  acute: "\u0301", grave: "\u0300", underline: "\u0332",
};
const SIZING = new Set(
  ("left right middle big Big bigg Bigg bigl bigr Bigl Bigr biggl biggr Biggl Biggr displaystyle textstyle " +
    "scriptstyle limits nolimits nonumber notag").split(" ")
);
const SPACES = new Set([",", ":", ";", ">", " ", "quad", "qquad", "enspace", "thinspace", "medspace", "thickspace"]);
const ESCAPED = new Set(["%", "$", "&", "#", "_", "{", "}"]);
const FRACTIONS = new Set(["frac", "dfrac", "tfrac", "cfrac"]);
const BINOMIALS = new Set(["binom", "dbinom", "tbinom"]);

// Simbolos que unicodeit no trae (o trae con otra forma).
const SYMBOL_OVERRIDES = {
  dots: "…", ldots: "…", cdots: "⋯", vdots: "⋮", ddots: "⋱", langle: "⟨", rangle: "⟩",
  lvert: "|", rvert: "|", lVert: "‖", rVert: "‖", colon: ":", cdotp: "·",
};

const _symbolCache = new Map();

// unicodeit como tabla de simbolos: un comando completo por vez, y se
// descarta un resultado que conserve letras del nombre (senal de que tomo
// solo un prefijo, como \lim -> "łim").
function symbolOf(latexCommand, fallback) {
  if (_symbolCache.has(latexCommand)) return _symbolCache.get(latexCommand);
  let out = fallback;
  if (window.unicodeit) {
    const r = window.unicodeit.replace(latexCommand);
    if (r && r !== latexCommand && !/[A-Za-z\\]/.test(r)) out = r;
  }
  _symbolCache.set(latexCommand, out);
  return out;
}

// -------------------------------------------------------------------------
// Fracciones y raices: "a/b" y "√x", con parentesis solo donde hacen falta
// para no cambiar el significado.
// -------------------------------------------------------------------------
function topLevel(s) {
  let prev;
  let out = s;
  do {
    prev = out;
    out = out.replace(/\([^()]*\)|\[[^[\]]*\]/g, "");
  } while (out !== prev);
  return out;
}

function countAtoms(s) {
  const base = [...s]
    .filter((ch) => !SCRIPT_CHARS.has(ch))
    .join("")
    .replace(/[_^][\p{L}\p{N}]+/gu, "") // subindice/exponente escrito "_d", "^π"
    .replace(/[_^]/g, "")
    .replace(/([√∛∜])[\p{L}\p{N}]/gu, "$1"); // √n es UN factor
  return (base.match(/[\p{L}\p{N}√∛∜∑∏∫∮∂∇]/gu) || []).length;
}

// Numerador: con operadores o espacios arriba del todo -> "(…)". Denominador:
// ademas, con mas de un factor ("2a", "σ√(2π)") -- "1/2a" se leeria (1/2)·a.
function needsParens(s, isDenominator) {
  const top = topLevel(s);
  if (/[\s+\-−±∓=<>≤≥≠≈⋅×÷/]/.test(top)) return true;
  if (!isDenominator || /^\d+([.,]\d+)?$/.test(top)) return false;
  if (/^[d∂][\p{L}]$/u.test(top)) return false; // dy/dx, ∂f/∂x
  return countAtoms(top) > 1;
}

function fracOut(num, den) {
  const n = needsParens(num, false) ? `(${num})` : num;
  const d = needsParens(den, true) ? `(${den})` : den;
  return `${n}/${d}`;
}

function rootOut(index, arg) {
  const atom = /^(\d+([.,]\d+)?|\p{L})$/u.test(arg);
  const body = atom ? arg : `(${arg})`;
  if (!index) return `√${body}`;
  if (index === "3") return `∛${body}`;
  if (index === "4") return `∜${body}`;
  const sup = mapAll(index, SUPERSCRIPT);
  return `${sup ?? `(${index})`}√${body}`;
}

// Una sola letra con barra (x̄, z̄) lleva macron; varias, raya continua.
function accentOut(name, arg) {
  let mark = ACCENTS[name];
  if (name === "bar" || name === "overline") mark = [...arg].length === 1 ? ACCENTS.bar : ACCENTS.overline;
  return [...arg].map((ch) => (/\s/.test(ch) ? ch : ch + mark)).join("");
}

// -------------------------------------------------------------------------
// Quimica \ce{...} (extension mhchem de KaTeX): subindices de atomos, cargas
// en superindice y flechas de reaccion.
// -------------------------------------------------------------------------
function chemSpecies(tok) {
  if (tok === "+" || tok === "-") return tok;
  const m = /^(\d*)(.*?)(\^\{?[0-9]*[+-]\}?|(?<=[A-Za-z0-9)\]])[+-])?$/.exec(tok);
  if (!m) return tok;
  const [, coef, body, charge] = m;
  const atoms = body.replace(/([A-Za-z)\]])(\d+)/g, (_, a, d) => a + mapAll(d, SUBSCRIPT));
  const chargeOut = charge ? mapAll(charge.replace(/[\^{}]/g, ""), SUPERSCRIPT) ?? charge : "";
  return coef + atoms + chargeOut;
}

function chemOut(raw) {
  return raw
    .replace(/<=>/g, " ⇌ ")
    .replace(/<->/g, " ↔ ")
    .replace(/->/g, " → ")
    .replace(/<-/g, " ← ")
    .split(/\s+/)
    .filter(Boolean)
    .map(chemSpecies)
    .join(" ");
}

// -------------------------------------------------------------------------
// Conversion
// -------------------------------------------------------------------------
const FRAC_END = "\uE010"; // marca: "aca termino una fraccion" (ver separateFractions)

// -------------------------------------------------------------------------
// Limites de operadores (formato elegido por el usuario el 2026-09-27): SIEMPRE
// entre parentesis con flechas, "∫(↓0 ↑2π)", "∑(↓i=1 ↑n)", "lim(↓x→0)". Antes
// iban como super/subindice ("∫₀^(2π)", que se leia como "0 elevado a 2π"); los
// limites en renglones aparte se veian bien en WhatsApp pero partian el
// mensaje en saltos de linea. Un solo formato para todos, facil de reconocer.
// -------------------------------------------------------------------------
function limitsOut(lower, upper) {
  const clean = (v) => v.replaceAll(FRAC_END, "").replace(/\s+/g, "");
  const parts = [];
  if (lower !== null) parts.push(`↓${clean(lower)}`);
  if (upper !== null) parts.push(`↑${clean(upper)}`);
  return `(${parts.join(" ")})`;
}

function convertLatex(latex) {
  const toks = tokenize(latex);
  let out = "";
  let i = 0;

  const skipSpaces = () => {
    while (toks[i] && toks[i].t === "space") i++;
  };
  // Argumento de un comando: {grupo}, o un solo token (\frac12, \sqrt2).
  const takeArg = () => {
    skipSpaces();
    const tok = toks[i++];
    if (!tok) return "";
    return tok.t === "group" ? tok.v : rawOf(tok);
  };
  const takeOptional = () => {
    if (!toks[i] || toks[i].t !== "char" || toks[i].v !== "[") return null;
    let raw = "";
    let j = i + 1;
    while (toks[j] && !(toks[j].t === "char" && toks[j].v === "]")) raw += rawOf(toks[j++]);
    i = j + 1;
    return convertLatex(raw);
  };
  const nextIsLetterLike = () => {
    const tok = toks[i];
    return Boolean(tok) && ((tok.t === "char" && /[\p{L}\p{N}]/u.test(tok.v)) || (tok.t === "cmd" && LETTER_LIKE.has(tok.v)));
  };
  const spaceBefore = () => {
    if (/[\p{L}\p{N})\]′]$/u.test(out)) out += " ";
  };
  // Limites de un operador: _{…} y ^{…} en cualquier orden, con \limits
  // o \nolimits de por medio.
  const takeLimits = () => {
    const limits = { lower: null, upper: null };
    for (;;) {
      let j = i;
      while (toks[j] && (toks[j].t === "space" || (toks[j].t === "cmd" && /^(no)?limits$/.test(toks[j].v)))) j++;
      const tk = toks[j];
      if (!tk || tk.t !== "char" || (tk.v !== "_" && tk.v !== "^")) return limits;
      i = j + 1;
      limits[tk.v === "_" ? "lower" : "upper"] = convertLatex(takeArg());
    }
  };
  // Formato de limites elegido por el usuario: ver limitsOut. Lo que sigue
  // queda separado por un espacio: "∑(↓i=1 ↑n) (xᵢ − x̄)²".
  const emitWithLimits = (op, { lower, upper }) => {
    out += op;
    if (lower === null && upper === null) return;
    out += limitsOut(lower, upper);
    const next = toks[i];
    if (next && next.t !== "space" && !(next.t === "char" && /[)\]},.;:=]/.test(next.v))) out += " ";
  };

  while (i < toks.length) {
    const tok = toks[i++];
    if (tok.t === "space") {
      out += " ";
      continue;
    }
    if (tok.t === "group") {
      out += convertLatex(tok.v);
      continue;
    }
    if (tok.t === "char") {
      if (tok.v === "^" || tok.v === "_") out += scriptOut(tok.v, convertLatex(takeArg()));
      else if (tok.v === "-") out += "−";
      else if (tok.v === "'") out += "′";
      else if (tok.v === "&") continue; // alineacion de \begin{aligned}
      else if (tok.v === "~") out += " ";
      else out += tok.v;
      continue;
    }

    const name = tok.v;
    if (FRACTIONS.has(name)) {
      const num = convertLatex(takeArg());
      const den = convertLatex(takeArg());
      spaceBefore();
      out += fracOut(num, den) + FRAC_END;
    } else if (name === "sqrt") {
      const index = takeOptional();
      out += rootOut(index, convertLatex(takeArg()));
    } else if (BINOMIALS.has(name)) {
      const n = convertLatex(takeArg());
      const k = convertLatex(takeArg());
      out += `C(${n}, ${k})`;
    } else if (TEXT_COMMANDS.has(name)) {
      out += takeArg().replace(/\\([%$&#_{}])/g, "$1");
    } else if (STYLE_COMMANDS.has(name)) {
      out += convertLatex(takeArg());
    } else if (FONT_COMMANDS.has(name)) {
      const arg = takeArg();
      out += symbolOf(`\\${name}{${arg}}`, convertLatex(arg));
    } else if (name in ACCENTS) {
      out += accentOut(name, convertLatex(takeArg()));
    } else if (name === "ce") {
      out += chemOut(takeArg());
    } else if (name === "pmod") {
      out += ` (mod ${convertLatex(takeArg())})`;
    } else if (name === "bmod" || name === "mod") {
      out += " mod ";
    } else if (name === "begin" || name === "end") {
      takeArg(); // \begin{aligned} ... \end{aligned}: solo el contenido
    } else if (name === "\\") {
      out += "\n";
    } else if (SIZING.has(name)) {
      if (name === "right") out = out.replace(/ +$/, ""); // "F(x) \right|" -> "F(x)|"
      if (toks[i] && toks[i].t === "char" && toks[i].v === ".") i++; // \left.
    } else if (SPACES.has(name)) {
      out += " ";
    } else if (name === "!") {
      continue;
    } else if (ESCAPED.has(name)) {
      out += name;
    } else if (name === "|") {
      out += "‖";
    } else if (BIG_OPERATORS.has(name)) {
      const op = symbolOf(`\\${name}`, name);
      const limits = takeLimits();
      if (limits.lower === null && limits.upper === null) out += op;
      else emitWithLimits(op, limits);
    } else if (LIMIT_FUNCTIONS.has(name)) {
      spaceBefore();
      const limits = takeLimits();
      emitWithLimits(name, limits);
      if (limits.lower === null && limits.upper === null && nextIsLetterLike()) out += " ";
    } else if (FUNCTION_NAMES.has(name)) {
      spaceBefore();
      out += name;
      if (nextIsLetterLike()) out += " "; // \sin\theta -> "sin θ"
    } else if (name in SYMBOL_OVERRIDES) {
      out += SYMBOL_OVERRIDES[name];
    } else {
      out += symbolOf(`\\${name}`, name);
      // 2\pi i -> "2πi": el espacio fuente despues de un simbolo de letra no se ve.
      if (LETTER_LIKE.has(name) && toks[i] && toks[i].t === "space" && !toks[i].newline) {
        const save = i;
        i++;
        if (!nextIsLetterLike()) i = save;
      }
    }
  }
  return out;
}

// Una fraccion pegada a lo que sigue ("\frac{1}{n}\sum") se separa con un
// espacio: "1/n ∑…", nunca "1/n∑…".
function separateFractions(s) {
  return s.replace(new RegExp(`${FRAC_END}(?=[^\\s)\\]},.;:=+−<>|!′/^_${FRAC_END}])`, "gu"), " ").replaceAll(FRAC_END, "");
}

// Convierte el contenido de UNA formula delimitada (sin los signos $/\(\) que
// la envuelven). REQ-007: cualquier fallo puntual devuelve el texto tal cual
// estaba -- nunca lanza, nunca rompe la copia del resto del mensaje.
function convertSingleFormula(latex, display) {
  try {
    let out = separateFractions(convertLatex(latex));
    out = out.replace(/[ \t]{2,}/g, " ").replace(/ *\n */g, "\n");
    return display ? out.trim() : out;
  } catch {
    return latex;
  }
}

// Protege bloques de codigo ```...``` de la conversion -- un "$" o "\("
// dentro de un ejemplo de codigo no es una formula matematica. Mismo truco
// de split-por-grupo-capturado que usa toPlainText en markdown.js.
function convertOutsideCodeBlocks(text, transform) {
  const parts = text.split(/(```[\s\S]*?```)/);
  return parts.map((part, i) => (i % 2 === 1 ? part : transform(part))).join("");
}

/**
 * REQ-001/002/003/004/005/006/007: convierte toda formula LaTeX delimitada
 * ($...$, $$...$$, \(...\), \[...\]) dentro de `text` a Unicode legible,
 * dejando el resto del texto intacto. Carga `unicodeit` de forma perezosa
 * (igual que loadKatex() en math.js) -- si no esta disponible o falla la
 * carga, devuelve `text` sin cambios (REQ-007, mejor esfuerzo).
 * @param {string} text
 * @returns {Promise<string>}
 */
export async function convertLatexFormulasToUnicode(text) {
  if (!text) return text;
  if (!/\$|\\\(|\\\[/.test(text)) return text; // REQ-006: sin formulas, no hay nada que hacer
  try {
    await loadUnicodeit();
  } catch {
    return text; // sin unicodeit disponible -- se copia tal cual (REQ-007)
  }
  return convertOutsideCodeBlocks(text, (segment) =>
    segment.replace(LATEX_SEGMENT_RE, (full, dd, d, paren, bracket) => {
      const inner = dd ?? d ?? paren ?? bracket;
      if (inner === undefined) return full;
      return convertSingleFormula(inner, dd !== undefined || bracket !== undefined);
    })
  );
}
