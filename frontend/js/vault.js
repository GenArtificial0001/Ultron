// spec gestor-conexiones Fase B (REQ-004/004b/005/006/007/007b/008/009/011): pantalla del gestor de conexiones en Configuración > Conexiones, al estilo de las capturas
// de FlowCraft que pasó el usuario (2026-10-08): un menú «Conectores» por tipo, una ventana «Credenciales de <tipo>» con una TARJETA por conexión (nombre, estado,
// datos no secretos, fechas, Probar / Editar / Eliminar) y «+ Agregar» con el formulario en el lugar.
// Todo habla con `/api/vault/*`, que responde SOLO a esta página en esta PC; ver o cambiar una contraseña guardada pide Windows Hello (el servidor lo exige: 423).
// Módulo aparte (no dentro de app.js, que ya pasa las 11 000 líneas): app.js le pasa sus utilidades con `initVault(...)`.
// Seguridad de la pantalla: todo texto que viene del servidor se pinta con textContent (nunca innerHTML) y el valor de una contraseña revelada vive solo en su
// <input>; al cerrar la ventana se borra y se bloquea el acceso (DELETE /api/vault/unlock).

// Misma regla de nombre que el servidor (connections.valid_name): letras o números separados por espacios o guiones, sin separadores en las puntas, 1 a 60.
const NAME_RE = /^[\p{L}\p{N}]+(?:[ -]+[\p{L}\p{N}]+)*$/u;
const NAME_MAX = 60;

// Conectores del menú. `kind: "connection"` = tipos con nombre (varias por tipo); `kind: "general"` = una sección de la configuración general.
const CONNECTORS = [
  { id: "oracle", kind: "connection", title: "Oracle", icon: "db", hint: "Bases Oracle (por ejemplo, Datamart). Podés cargar varias, cada una con su nombre." },
  { id: "sharepoint", kind: "connection", title: "SharePoint", icon: "site", hint: "Un sitio de SharePoint con la app de Azure que tiene acceso. Cada sitio lleva sus propias credenciales." },
  { id: "sap", kind: "connection", title: "SAP", icon: "db", hint: "Servicios de SAP (Fiori / OData). Cada conexión con su usuario." },
  { id: "confluence", kind: "connection", title: "Confluence", icon: "doc", hint: "Tu correo y tu token PERSONAL de Confluence. Vision solo lee, nunca escribe." },
  { id: "m365", kind: "m365", title: "Outlook y Teams", icon: "site", hint: "Tu correo, tu agenda y tus chats de Teams, con tu cuenta de la empresa. Se inicia sesión una sola vez con el botón; Vision nunca lo hace por su cuenta." },
  { id: "office", kind: "office", title: "Office (Excel y Word)", icon: "doc", hint: "Vision lee y escribe en el Excel que ya tenés abierto, y convierte documentos de Word, Excel y PowerPoint. Nunca abre Excel ni Word por su cuenta ni guarda tus libros: eso lo decidís vos." },
  { id: "bigquery", kind: "general", section: "bigquery", title: "BigQuery", icon: "db", hint: "Importá el archivo de clave de la cuenta de servicio (.json): Vision lo guarda cifrado y completa el proyecto solo. Cuando venza, importá el nuevo encima." },
  { id: "graph", kind: "general", section: "graph", title: "Microsoft Graph", icon: "site", hint: "Graph usa las credenciales de una de tus SharePoint: elegí cuál." },
  { id: "proveedor", kind: "general", section: "proveedor", title: "El proveedor de IA", icon: "api", hint: "Credenciales de la app del proveedor de IA. Vacío = se inicia sesión con tu usuario." },
  { id: "voz", kind: "general", section: "voz", title: "Voz (ElevenLabs y Fish)", icon: "api", hint: "Claves de los motores de voz en la nube. La voz se elige en Configuración > Audio (con «Elegir de mi cuenta…»)." },
  { id: "red", kind: "general", section: "red", title: "Red y proxy", icon: "net", hint: "Proxy de la empresa. Lo que ya está definido en Windows manda y no se pisa." },
  { id: "general", kind: "general", section: "general", title: "General", icon: "gear", hint: "Puerto, modelo y carpetas de Vision." },
];

let _ctx = null; // utilidades de app.js
let _general = null; // configuracion general (sin secretos): la fila de BigQuery del resumen la necesita
let _bqLastTest = null; // null = sin probar en esta pantalla; true/false = resultado de la ultima prueba de BigQuery
let _status = null; // GET /api/vault/status
let _m365 = null; // GET /api/m365/status: {state, connected, message, enabled}; null = todavia no se consulto (o el servidor no respondio)
let _office = null; // GET /api/office/status: {enabled, excel: {state, message}, word: {state, message}}; null = todavia no se consulto (o el servidor no respondio)
let _m365Timer = null; // consulta cada pocos segundos mientras se espera que la persona termine de entrar en el navegador
let _connections = []; // GET /api/vault/connections
let _rootEl = null;
let _modalEl = null;
let _lastImportReport = null; // el informe de la importacion queda a la vista despues de redibujar (si no, desaparecia al instante)

export function initVault(ctx) {
  _ctx = ctx;
  _rootEl = document.getElementById(ctx.rootId);
  if (!_rootEl) return;
  refreshVault();
}

function api(path, options = {}) {
  return _ctx.apiFetchOk(`${_ctx.API_BASE}/api/vault${path}`, options);
}

function jsonBody(method, body) {
  return { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
}

function el(tag, { className = "", text = "", attrs = {} } = {}, children = []) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text) node.textContent = text;
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  for (const child of children) if (child) node.appendChild(child);
  return node;
}

function nameProblem(name) {
  if (!name.trim()) return "Poné un nombre a la conexión antes de guardarla.";
  if (name.length > NAME_MAX || name !== name.trim() || !NAME_RE.test(name)) {
    return "Solo letras, números, espacios y guion (hasta 60), sin espacios al principio ni al final.";
  }
  return "";
}

export async function refreshVault() {
  if (!_rootEl) return;
  try {
    const [statusResp, connResp, generalResp] = await Promise.all([api("/status"), api("/connections"), api("/general")]);
    _status = await statusResp.json();
    _connections = (await connResp.json()).connections || [];
    _general = await generalResp.json();
  } catch (err) {
    _rootEl.replaceChildren(el("p", { className: "settings-hint error", text: _ctx.friendlyErrorMessage(err) }));
    return;
  }
  await loadM365Status();
  await loadOfficeStatus();
  renderRoot();
}

function renderRoot() {
  const head = el("div", { className: "vault-head" }, [
    el("h3", { className: "settings-subheading", text: "Gestor de conexiones" }),
    el("p", { className: "settings-hint", text: "Todo se guarda cifrado en Vision: solo tu usuario de Windows, en esta PC, puede leerlo." }),
  ]);
  const parts = [head];
  const legacy = _status.legacy || {};
  if (!_status.active && (legacy.env || legacy.connections_json || legacy.env_enc || legacy.bq_key_file)) {
    // REQ-003: hasta importar no se puede cargar nada (el servidor responde 409): activar la base con una sola conexión haría desaparecer las del .env
    parts.push(renderImportBanner(legacy));
    _rootEl.replaceChildren(...parts);
    return;
  }
  if (_lastImportReport) {
    parts.push(
      el("div", { className: "vault-banner vault-banner-done", attrs: { id: "vault-import-done" } }, [
        el("p", { text: "Importación terminada. Esto es lo que pasó a Vision (los archivos viejos no se tocaron):" }),
        renderImportReport(_lastImportReport),
      ])
    );
  }
  if (_status.active && (legacy.env || legacy.connections_json || legacy.env_enc || legacy.bq_key_file)) parts.push(renderDeleteLegacyBanner(legacy));
  parts.push(renderMenu());
  parts.push(renderSummary());
  _rootEl.replaceChildren(...parts);
}

function renderImportBanner(legacy) {
  const files = [legacy.env && ".env", legacy.connections_json && "connections.json", legacy.env_enc && ".env.enc", legacy.bq_key_file && "la clave de BigQuery (.json)"].filter(Boolean).join(", ");
  const statusEl = el("div", { className: "vault-import-report" });
  const btn = el("button", { className: "acc-action-btn", text: "Importar a Vision", attrs: { type: "button", id: "vault-import-btn" } });
  btn.addEventListener("click", async () => {
    btn.disabled = true;
    statusEl.replaceChildren(el("span", { className: "settings-status", text: "Importando…" }));
    try {
      _lastImportReport = await (await api("/import", { method: "POST" })).json();
      await refreshVault();
      _ctx.onChanged?.();
    } catch (err) {
      statusEl.replaceChildren(el("span", { className: "settings-status error", text: _ctx.friendlyErrorMessage(err) }));
      btn.disabled = false;
    }
  });
  return el("div", { className: "vault-banner", attrs: { id: "vault-import-banner" } }, [
    el("p", { text: `Tus conexiones todavía salen de ${files}. Importalas una vez y desde ahí se configuran acá (los archivos no se tocan).` }),
    btn,
    statusEl,
  ]);
}

// REQ-011 (Fase C): «ni el .env ni connections.json deben existir». Después de importar, los archivos viejos siguen en la PC con las contraseñas en texto: se
// ofrece borrarlos, siempre con confirmación (el servidor además se niega si la base no está activa).
function renderDeleteLegacyBanner(legacy) {
  const files = [legacy.env && ".env", legacy.connections_json && "connections.json", legacy.env_enc && ".env.enc", legacy.bq_key_file && "la clave de BigQuery (.json)"].filter(Boolean).join(", ");
  const statusEl = el("div", { className: "vault-import-report" });
  const btn = el("button", { className: "acc-action-btn danger", text: "Borrar archivos viejos", attrs: { type: "button", id: "vault-delete-legacy-btn" } });
  btn.addEventListener("click", async () => {
    const confirmed = await _ctx.showConfirmModal({
      title: "Borrar los archivos viejos",
      message: `Se borran ${files}. Vision ya no los usa: todo está guardado cifrado en Vision. Esta acción no se puede deshacer.`,
      acceptLabel: "Borrar",
    });
    if (!confirmed) return;
    btn.disabled = true;
    try {
      const result = await (await api("/legacy-files", { method: "DELETE" })).json();
      _lastImportReport = null;
      await refreshVault();
      if ((result.problems || []).length) {
        _rootEl.prepend(el("p", { className: "settings-hint error", text: result.problems.join(" ") }));
      }
    } catch (err) {
      statusEl.replaceChildren(el("span", { className: "settings-status error", text: _ctx.friendlyErrorMessage(err) }));
      btn.disabled = false;
    }
  });
  return el("div", { className: "vault-banner", attrs: { id: "vault-delete-legacy-banner" } }, [
    el("p", { text: `Tus archivos viejos (${files}) siguen en esta PC, con las contraseñas en texto. Vision ya no los usa: podés borrarlos.` }),
    btn,
    statusEl,
  ]);
}

function renderImportReport(report) {
  const list = el("ul", { className: "vault-report-list" });
  for (const c of report.connections || []) list.appendChild(el("li", { text: `${c.display}: ${c.result}` }));
  if ((report.general || []).length) list.appendChild(el("li", { text: `Configuración general: ${report.general.join(", ")}` }));
  for (const name of report.switched_off || []) list.appendChild(el("li", { text: `${name}: quedó apagada (como en el .env)` }));
  for (const p of report.problems || []) list.appendChild(el("li", { className: "error", text: p }));
  if (!list.children.length) list.appendChild(el("li", { text: "No había nada para importar." }));
  return list;
}

function renderMenu() {
  const menuBtn = el("button", { className: "acc-action-btn vault-menu-btn", text: "Conectores ▾", attrs: { type: "button", id: "vault-menu-btn", "aria-haspopup": "true" } });
  const menu = el("div", { className: "vault-menu hidden", attrs: { id: "vault-menu", role: "menu" } });
  for (const c of CONNECTORS) {
    const item = el("button", { className: "vault-menu-item", text: c.title, attrs: { type: "button", role: "menuitem", "data-connector": c.id } });
    item.addEventListener("click", () => {
      menu.classList.add("hidden");
      openConnectorModal(c.id);
    });
    menu.appendChild(item);
  }
  menuBtn.addEventListener("click", () => menu.classList.toggle("hidden"));
  return el("div", { className: "vault-menu-wrap" }, [menuBtn, menu]);
}

function badgeFor(card) {
  if (card.unreadable) return { text: "Volvé a cargarla", state: "warn" };
  if (card.test_status === "error") return { text: "Error", state: "error" };
  if ((card.missing || []).length) return { text: "Faltan datos", state: "warn" };
  if (card.test_status === "ok") return { text: "Probada", state: "ok" };
  return { text: "Sin probar", state: "idle" };
}

// Pedido del usuario 2026-10-10: «entre los conectores a probar no veo BigQuery». BigQuery no es una conexion con nombre (es una seccion de la
// configuracion general), asi que tiene su propia fila: aparece si hay key o proyecto, con el estado de la key o de la ultima prueba.
function bigQuerySummary() {
  const fields = (_general && _general.fields) || [];
  const field = (key) => fields.find((f) => f.key === key) || {};
  const keySet = Boolean(field("bq_key_json").set);
  const name = field("bigquery_conexion_name").value || field("bq_project_id").value || "";
  if (!keySet && !name) return null;
  let badge = { text: "Clave guardada", state: "idle" };
  if (!keySet) badge = { text: "Sin clave", state: "warn" };
  else if (_bqLastTest === true) badge = { text: "Probada", state: "ok" };
  else if (_bqLastTest === false) badge = { text: "Error", state: "error" };
  return { display: name ? `BigQuery - ${name}` : "BigQuery", badge };
}

function renderSummary() {
  const wrap = el("div", { className: "vault-summary" });
  wrap.id = "vault-summary"; // asignado asi (y no por attrs) para que `scripts/qa_battery.py` lo reconozca como id creado en runtime: `refreshVaultSummaryOnly` lo busca con getElementById
  const bq = bigQuerySummary();
  if (bq) {
    const row = el("button", { className: "vault-summary-row", attrs: { type: "button", "data-id": "bigquery" } }, [
      el("span", { className: "vault-summary-name", text: bq.display }),
      el("span", { className: "vault-badge", text: bq.badge.text, attrs: { "data-state": bq.badge.state } }),
    ]);
    row.addEventListener("click", () => openConnectorModal("bigquery"));
    wrap.appendChild(row);
  }
  const m365 = m365Summary();
  if (m365) {
    const row = el("button", { className: "vault-summary-row", attrs: { type: "button", "data-id": "m365" } }, [
      el("span", { className: "vault-summary-name", text: _m365.display || "Outlook y Teams - Principal" }),
      el("span", { className: "vault-badge", text: m365.text, attrs: { "data-state": m365.state } }),
    ]);
    row.addEventListener("click", () => openConnectorModal("m365"));
    wrap.appendChild(row);
  }
  const office = officeSummary();
  if (office) {
    const row = el("button", { className: "vault-summary-row", attrs: { type: "button", "data-id": "office" } }, [
      el("span", { className: "vault-summary-name", text: _office.display || "Office - Principal" }),
      el("span", { className: "vault-badge", text: office.text, attrs: { "data-state": office.state } }),
    ]);
    row.addEventListener("click", () => openConnectorModal("office"));
    wrap.appendChild(row);
  }
  if (!_connections.length && !bq && !m365 && !office) {
    wrap.appendChild(el("p", { className: "settings-hint", text: "Todavía no hay conexiones cargadas en Vision. Elegí un conector para empezar." }));
    return wrap;
  }
  for (const card of _connections) {
    const badge = badgeFor(card);
    const row = el("button", { className: "vault-summary-row", attrs: { type: "button", "data-id": card.id } }, [
      el("span", { className: "vault-summary-name", text: card.display }),
      el("span", { className: "vault-badge", text: badge.text, attrs: { "data-state": badge.state } }),
    ]);
    row.addEventListener("click", () => openConnectorModal(card.type));
    wrap.appendChild(row);
  }
  return wrap;
}

// ---------------------------------------------------------------------------------------------------------------------------------------------------------
// Ventana «Credenciales de <tipo>»
// ---------------------------------------------------------------------------------------------------------------------------------------------------------
// `notice`: aviso para la ventana YA redibujada (revision 2026-10-09: redibujar despues de guardar borraba «Guardado.» en el acto).
function openConnectorModal(connectorId, { notice = "" } = {}) {
  const connector = CONNECTORS.find((c) => c.id === connectorId);
  if (!connector) return;
  closeModal({ lock: false }); // volver a dibujar la ventana (despues de guardar) no bloquea: solo el cierre real
  const body = el("div", { className: "vault-modal-body" });
  const closeBtn = el("button", { className: "vault-modal-close", text: "×", attrs: { type: "button", "aria-label": "Cerrar" } });
  closeBtn.addEventListener("click", () => closeModal());
  const dialog = el("div", { className: "vault-modal", attrs: { role: "dialog", "aria-modal": "true", "aria-label": `Credenciales de ${connector.title}`, id: "vault-modal" } }, [
    el("div", { className: "vault-modal-header" }, [el("h3", { text: `Credenciales de ${connector.title}` }), closeBtn]),
    el("p", { className: "settings-hint", text: connector.hint }),
    body,
  ]);
  _modalEl = el("div", { className: "vault-modal-overlay", attrs: { id: "vault-modal-overlay" } }, [dialog]);
  _modalEl.addEventListener("click", (e) => {
    if (e.target === _modalEl) closeModal();
  });
  document.body.appendChild(_modalEl);
  document.addEventListener("keydown", onModalKeydown);
  if (connector.kind === "connection") renderConnectionType(body, connector);
  else if (connector.kind === "m365") renderM365(body, notice);
  else if (connector.kind === "office") renderOffice(body, notice);
  else renderGeneralSection(body, connector, notice);
}

function onModalKeydown(e) {
  if (e.key === "Escape") closeModal();
}

async function closeModal({ lock = true } = {}) {
  if (!_modalEl) return;
  stopM365Polling();
  _modalEl.remove(); // los <input> con contraseñas reveladas se van con la ventana
  _modalEl = null;
  document.removeEventListener("keydown", onModalKeydown);
  if (!lock) return;
  try {
    await api("/unlock", { method: "DELETE" }); // REQ-007b: al cerrar la pantalla, se bloquea ya
  } catch {
    /* noop: el desbloqueo vence solo a los 5 minutos igual */
  }
}

function typeSchema(typeId) {
  return ((_status && _status.types) || []).find((t) => t.type === typeId) || { fields: [] };
}

function renderConnectionType(body, connector) {
  const cards = _connections.filter((c) => c.type === connector.id);
  const list = el("div", { className: "vault-cards" });
  if (!cards.length) list.appendChild(el("p", { className: "settings-hint", text: `Todavía no hay conexiones de ${connector.title}.` }));
  for (const card of cards) list.appendChild(renderCard(card, connector, body));
  const addBtn = el("button", { className: "acc-action-btn vault-add-btn", text: `+ Agregar credencial de ${connector.title}`, attrs: { type: "button", id: "vault-add-btn" } });
  addBtn.addEventListener("click", () => {
    addBtn.classList.add("hidden");
    body.appendChild(renderConnectionForm(connector, null, () => openConnectorModal(connector.id)));
  });
  body.replaceChildren(list, addBtn);
}

function fmtDate(epoch) {
  if (!epoch) return "nunca";
  return new Date(epoch * 1000).toLocaleString("es-AR", { dateStyle: "short", timeStyle: "short" });
}

function renderCard(card, connector, body) {
  const badge = badgeFor(card);
  const schema = typeSchema(card.type);
  const lines = el("div", { className: "vault-card-lines" });
  for (const f of schema.fields) {
    if (f.structured) continue;
    if (f.secret) {
      lines.appendChild(el("div", { className: "vault-card-line", text: `${f.label}: ${card.secrets[f.key] ? "•••••• (guardada)" : "sin cargar"}` }));
    } else if (card.fields[f.key]) {
      lines.appendChild(el("div", { className: "vault-card-line", text: `${f.label}: ${card.fields[f.key]}` }));
    }
  }
  if (card.info && card.info.sections) {
    lines.appendChild(el("div", { className: "vault-card-line", text: `Secciones: ${card.info.sections.map((s) => s.name).join(", ")}` }));
  }
  lines.appendChild(el("div", { className: "vault-card-meta", text: `Creada: ${fmtDate(card.created_at)} · Última prueba: ${fmtDate(card.tested_at)}${card.default ? " · Predeterminada" : ""}${card.enabled ? "" : " · Apagada"}` }));
  if (card.unreadable) lines.appendChild(el("div", { className: "vault-card-line warn", text: "Se guardó en otra PC u otro usuario de Windows: tocá Editar y volvé a cargar sus datos." }));

  const resultEl = el("div", { className: "vault-card-result" });
  const testBtn = el("button", { className: "acc-action-btn secondary", text: "Probar", attrs: { type: "button", "data-action": "test" } });
  testBtn.addEventListener("click", async () => {
    testBtn.disabled = true;
    resultEl.textContent = "Probando…";
    try {
      const result = await (await api(`/connections/${encodeURIComponent(card.id)}/test`, { method: "POST" })).json();
      resultEl.textContent = result.ok ? "Conectó bien." : result.detail || "No se pudo conectar.";
      resultEl.dataset.state = result.ok ? "ok" : "error";
      await refreshVault();
    } catch (err) {
      resultEl.textContent = _ctx.friendlyErrorMessage(err);
      resultEl.dataset.state = "error";
    } finally {
      testBtn.disabled = false;
    }
  });
  const editBtn = el("button", { className: "acc-action-btn secondary", text: "Editar", attrs: { type: "button", "data-action": "edit" } });
  const deleteBtn = el("button", { className: "acc-action-btn danger", text: "Eliminar", attrs: { type: "button", "data-action": "delete" } });
  const cardEl = el("div", { className: "vault-card", attrs: { "data-id": card.id } }, [
    el("div", { className: "vault-card-top" }, [
      el("span", { className: "vault-card-name", text: card.name }),
      el("span", { className: "vault-badge", text: badge.text, attrs: { "data-state": badge.state } }),
    ]),
    lines,
    el("div", { className: "vault-card-actions" }, [testBtn, editBtn, deleteBtn]),
    resultEl,
  ]);
  editBtn.addEventListener("click", () => {
    cardEl.replaceWith(renderConnectionForm(connector, card, () => openConnectorModal(connector.id)));
  });
  deleteBtn.addEventListener("click", async () => {
    // REQ-009: borrar pide confirmación
    const confirmed = await _ctx.showConfirmModal({
      title: `Eliminar «${card.display}»`,
      message: "Se borran sus datos guardados en Vision. Si la necesitás de nuevo, vas a tener que volver a cargarla.",
      acceptLabel: "Eliminar",
    });
    if (!confirmed) return;
    try {
      await api(`/connections/${encodeURIComponent(card.id)}`, { method: "DELETE" });
      await refreshVault();
      _ctx.onChanged?.();
      openConnectorModal(connector.id);
    } catch (err) {
      resultEl.textContent = _ctx.friendlyErrorMessage(err);
      resultEl.dataset.state = "error";
    }
  });
  return cardEl;
}

// ---------------------------------------------------------------------------------------------------------------------------------------------------------
// Desbloqueo (Windows Hello) y ojo
// ---------------------------------------------------------------------------------------------------------------------------------------------------------
async function ensureUnlocked(noticeEl) {
  try {
    const state = await (await api("/unlock")).json();
    if (state.unlocked_until && state.unlocked_until * 1000 > Date.now()) return true;
  } catch {
    /* noop: si no se pudo consultar, se intenta desbloquear igual */
  }
  noticeEl.textContent = "Se abrió la ventana de Windows Hello: confirmá que sos vos.";
  noticeEl.dataset.state = "";
  try {
    await api("/unlock", { method: "POST" });
    noticeEl.textContent = "";
    return true;
  } catch (err) {
    noticeEl.textContent = _ctx.friendlyErrorMessage(err);
    noticeEl.dataset.state = "error";
    return false;
  }
}

// Campo de contraseña con ojo. `revealPath` = ruta del secreto guardado (null si no hay nada guardado todavía).
function secretInput({ id, label, stored, revealPath, noticeEl }) {
  const input = el("input", { attrs: { type: "password", id, autocomplete: "new-password", spellcheck: "false" } });
  input.placeholder = stored ? "guardada — tocá el ojo para verla, o escribí una nueva" : "";
  input.dataset.touched = "false";
  input.addEventListener("input", () => {
    input.dataset.touched = "true";
  });
  const eye = el("button", { className: "vault-eye", text: "👁", attrs: { type: "button", "aria-label": `Mostrar ${label}`, "aria-pressed": "false" } });
  eye.addEventListener("click", async () => {
    if (input.type === "text") {
      input.type = "password";
      eye.setAttribute("aria-pressed", "false");
      return;
    }
    // REQ-007: lo escrito se muestra sin más; lo GUARDADO, solo después de Windows Hello
    if (stored && revealPath && input.dataset.touched !== "true" && !input.value) {
      if (!(await ensureUnlocked(noticeEl))) return;
      try {
        const data = await (await api(revealPath)).json();
        input.value = data.value || "";
      } catch (err) {
        noticeEl.textContent = _ctx.friendlyErrorMessage(err);
        noticeEl.dataset.state = "error";
        return;
      }
    }
    input.type = "text";
    eye.setAttribute("aria-pressed", "true");
  });
  return el("div", { className: "vault-secret" }, [input, eye]);
}

function fieldRow(labelText, required, control, forId) {
  return el("div", { className: "settings-field" }, [el("label", { text: required ? `${labelText} *` : labelText, attrs: { for: forId } }), control]);
}

// ---------------------------------------------------------------------------------------------------------------------------------------------------------
// Formulario de una conexión con nombre (alta o edición)
// ---------------------------------------------------------------------------------------------------------------------------------------------------------
function renderConnectionForm(connector, card, onDone) {
  const schema = typeSchema(connector.id);
  const noticeEl = el("div", { className: "vault-notice", attrs: { role: "status" } });
  const nameInput = el("input", { attrs: { type: "text", id: "vault-field-name", maxlength: String(NAME_MAX), autocomplete: "off" } });
  nameInput.value = card ? card.name : "";
  const nameHint = el("div", { className: "settings-hint error vault-name-hint" });
  const saveBtn = el("button", { className: "acc-action-btn", text: "Guardar", attrs: { type: "button", id: "vault-save-btn" } });
  const cancelBtn = el("button", { className: "acc-action-btn secondary", text: "Cancelar", attrs: { type: "button" } });
  // Pedido del usuario 2026-10-10: «todos deberian tener un boton de probar antes de guardar».
  const testBtn = el("button", { className: "acc-action-btn secondary", text: "Probar", attrs: { type: "button", id: "vault-form-test-btn" } });
  const testResultEl = el("div", { className: "vault-card-result", attrs: { id: "vault-form-test-result", role: "status" } });

  // REQ-004b: sin nombre válido no se puede guardar (el servidor también lo rechaza)
  const syncName = () => {
    const problem = nameProblem(nameInput.value);
    nameHint.textContent = problem;
    saveBtn.disabled = Boolean(problem);
  };
  nameInput.addEventListener("input", syncName);

  const controls = new Map();
  const rows = [fieldRow("Nombre", true, nameInput, "vault-field-name"), nameHint];
  for (const f of schema.fields) {
    if (f.structured) continue; // las secciones de Confluence se cargan por importación (fuera de esta pantalla por ahora)
    const id = `vault-field-${f.key}`;
    if (f.secret) {
      const stored = Boolean(card && card.secrets[f.key]);
      const wrap = secretInput({ id, label: f.label, stored, revealPath: card ? `/connections/${encodeURIComponent(card.id)}/secret/${f.key}` : null, noticeEl });
      controls.set(f.key, { input: wrap.querySelector("input"), secret: true, stored });
      rows.push(fieldRow(f.label, f.required, wrap, id));
    } else {
      const input = el("input", { attrs: { type: "text", id, autocomplete: "off", spellcheck: "false" } });
      input.value = card ? card.fields[f.key] || "" : "";
      controls.set(f.key, { input, secret: false, initial: input.value });
      rows.push(fieldRow(f.label, f.required, input, id));
    }
  }

  // Lo que el usuario escribio: las contraseñas solo si las toco (al editar, las que no toco salen de la guardada, en el servidor).
  const typedFields = () => {
    const fields = {};
    for (const [key, c] of controls) {
      if (c.secret) {
        if (c.input.dataset.touched === "true") fields[key] = c.input.value;
      } else {
        fields[key] = c.input.value.trim();
      }
    }
    return fields;
  };
  testBtn.addEventListener("click", async () => {
    testBtn.disabled = true;
    testResultEl.textContent = "Probando…";
    testResultEl.dataset.state = "";
    try {
      const body = { type: connector.id, name: nameInput.value, fields: typedFields() };
      if (card) body.conn_id = card.id;
      const result = await (await api("/connections/test-draft", jsonBody("POST", body))).json();
      testResultEl.textContent = result.ok ? "Conectó bien (todavía no se guardó)." : result.detail || "No se pudo conectar.";
      testResultEl.dataset.state = result.ok ? "ok" : "error";
    } catch (err) {
      testResultEl.textContent = _ctx.friendlyErrorMessage(err);
      testResultEl.dataset.state = "error";
    } finally {
      testBtn.disabled = false;
    }
  });

  saveBtn.addEventListener("click", async () => {
    const fields = {};
    let changesStoredSecret = false;
    for (const [key, c] of controls) {
      if (c.secret) {
        if (c.input.dataset.touched === "true") {
          fields[key] = c.input.value;
          if (c.stored) changesStoredSecret = true;
        }
      } else if (!card || c.input.value !== c.initial) {
        fields[key] = c.input.value.trim();
      }
    }
    saveBtn.disabled = true;
    try {
      if (changesStoredSecret && !(await ensureUnlocked(noticeEl))) return;
      if (card) {
        const changes = { fields };
        if (nameInput.value !== card.name) changes.name = nameInput.value;
        await api(`/connections/${encodeURIComponent(card.id)}`, jsonBody("PUT", changes));
      } else {
        await api("/connections", jsonBody("POST", { type: connector.id, name: nameInput.value, fields }));
      }
      await refreshVault();
      _ctx.onChanged?.();
      onDone();
    } catch (err) {
      noticeEl.textContent = _ctx.friendlyErrorMessage(err);
      noticeEl.dataset.state = "error";
    } finally {
      syncName();
    }
  });
  cancelBtn.addEventListener("click", onDone);
  syncName();
  return el("div", { className: "vault-form", attrs: { id: "vault-form" } }, [
    el("h4", { text: card ? `Editar «${card.display}»` : `Nueva credencial de ${connector.title}` }),
    ...rows,
    noticeEl,
    testResultEl,
    el("div", { className: "settings-actions" }, [testBtn, saveBtn, cancelBtn]),
  ]);
}

// ---------------------------------------------------------------------------------------------------------------------------------------------------------
// Secciones de la configuración general (BigQuery, Graph, el proveedor de IA, Voz, Red, General)
// ---------------------------------------------------------------------------------------------------------------------------------------------------------
// ---------------------------------------------------------------------------------------------------------------------------------------------------------
// Outlook y Teams (spec office-outlook-teams-mcp REQ-M09/M11/M14). Decision del usuario 2026-10-17: el inicio de sesion se hace DESDE ACA con un boton; Vision no muestra avisos ni abre
// el navegador por su cuenta. Habla con `/api/m365/*` (solo esta PC y esta pagina). Nunca se pinta el token: el servidor ni siquiera lo manda.
// ---------------------------------------------------------------------------------------------------------------------------------------------------------
function m365Api(path, options = {}) {
  return _ctx.apiFetchOk(`${_ctx.API_BASE}/api/m365${path}`, options);
}

async function loadM365Status() {
  try {
    _m365 = await (await m365Api("/status")).json();
  } catch {
    _m365 = null; // sin respuesta: la fila no aparece y la ventana lo dice; la pantalla del resto de las conexiones sigue andando
  }
}

function m365Summary() {
  if (!_m365) return null;
  if (_m365.enabled === false) return { text: "Apagada", state: "idle" };
  if (_m365.state === "connected") return { text: "Conectado", state: "ok" };
  if (_m365.state === "waiting") return { text: "Esperando…", state: "warn" };
  if (_m365.state === "error") return { text: "Error", state: "error" };
  return { text: "Sin sesión", state: "idle" };
}

function stopM365Polling() {
  if (_m365Timer) clearInterval(_m365Timer);
  _m365Timer = null;
}

// Nombre de «Outlook y Teams» y «Office» elegido por la PERSONA (spec conexiones-office-m365-visibles REQ-008/009/010). Correccion del usuario 2026-10-19: «yo soy el que debe asignar
// el nombre de esa conexion y poder editarla, como todas las demas». Misma regla que el formulario del gestor (`nameProblem`, que replica `connections.valid_name`): sin nombre valido no se
// puede guardar. Al guardar se avisa con un evento para que app.js repinte el panel lateral y «Estado de las conexiones» sin recargar (vault.js no conoce esas listas).
const CAPABILITY_NAME_EVENT = "vision:capability-name-changed";

function capabilityNameBlock({ prefix, path, current, onSaved }) {
  const input = el("input", { attrs: { type: "text", id: `${prefix}-name-input`, maxlength: String(NAME_MAX), autocomplete: "off", spellcheck: "false" } });
  input.value = current;
  const hint = el("div", { className: "settings-hint error vault-name-hint", attrs: { id: `${prefix}-name-hint` } });
  const save = el("button", { className: "acc-action-btn secondary", text: "Guardar nombre", attrs: { type: "button", id: `${prefix}-name-save` } });
  // Clase propia (`vault-name-notice`) y NO `vault-notice`: la ventana ya tiene UN aviso con esa clase (el del inicio de sesion, el de los botones) y varios tests y el propio codigo lo buscan
  // como «el primero»; un segundo `.vault-notice` antes que el suyo les tapaba el aviso (lo mostro el e2e de «una falla al iniciar sesion»). Se ve igual: comparte el estilo abajo.
  const notice = el("div", { className: "vault-name-notice", attrs: { role: "status", id: `${prefix}-name-notice` } });
  const sync = () => {
    const problem = nameProblem(input.value);
    hint.textContent = problem;
    save.disabled = Boolean(problem) || input.value === current;
  };
  input.addEventListener("input", sync);
  sync();
  save.addEventListener("click", async () => {
    if (nameProblem(input.value)) return;
    save.disabled = true;
    try {
      const out = await (await _ctx.apiFetchOk(`${_ctx.API_BASE}${path}`, jsonBody("POST", { name: input.value }))).json();
      window.dispatchEvent(new CustomEvent(CAPABILITY_NAME_EVENT, { detail: { prefix, display: out.display } }));
      await onSaved(`Listo: ahora se llama «${out.display}».`);
    } catch (err) {
      notice.textContent = _ctx.friendlyErrorMessage(err); // frase ya escrita para una persona (el 422 trae la regla)
      notice.dataset.state = "error";
      sync();
    }
  });
  return el("div", { className: "settings-field vault-name-field" }, [
    el("label", { text: "Nombre", attrs: { for: `${prefix}-name-input` } }),
    input,
    hint,
    el("div", { className: "vault-card-actions" }, [save]),
    notice,
  ]);
}

async function renderM365(body, notice = "") {
  stopM365Polling();
  await loadM365Status();
  if (!_m365) {
    body.replaceChildren(el("p", { className: "settings-hint error", text: "No pude consultar el estado de Outlook y Teams. Revisá que Vision esté andando y volvé a abrir esta ventana." }));
    return;
  }
  const noticeEl = el("div", { className: "vault-notice", attrs: { role: "status" } });
  if (notice) noticeEl.textContent = notice;
  const summary = m365Summary();
  const statusRow = el("div", { className: "vault-card", attrs: { id: "m365-card" } }, [
    el("div", { className: "vault-card-head" }, [
      el("strong", { text: _m365.display || "Outlook y Teams - Principal" }),
      el("span", { className: "vault-badge", text: summary.text, attrs: { "data-state": summary.state, id: "m365-badge" } }),
    ]),
  ]);
  if (_m365.state === "waiting") {
    statusRow.appendChild(el("p", { className: "settings-hint", text: "Esperando que termines de entrar con tu cuenta de la empresa en la ventana del navegador que se abrió. Podés dejar esta ventana abierta." }));
  } else if (_m365.state === "connected") {
    statusRow.appendChild(el("p", { className: "settings-hint", text: "Ya iniciaste sesión. Vision puede leer tu correo, tu agenda y tus chats de Teams cuando se lo pidas." }));
  } else if (_m365.state === "error" && _m365.message) {
    noticeEl.textContent = _m365.message; // frase ya escrita para una persona (nunca el error crudo)
    noticeEl.dataset.state = "error";
  } else {
    statusRow.appendChild(el("p", { className: "settings-hint", text: "Todavía no iniciaste sesión. Tocá el botón y entrá con tu cuenta de la empresa en la ventana que se abre (hace falta estar en la red de la empresa)." }));
  }

  const actions = el("div", { className: "vault-card-actions" });
  if (_m365.state === "connected") {
    const out = el("button", { className: "acc-action-btn secondary", text: "Cerrar sesión", attrs: { type: "button", id: "m365-logout-btn" } });
    out.addEventListener("click", () => m365Act(out, noticeEl, body, "/logout", "Cerraste la sesión de Outlook y Teams."));
    actions.appendChild(out);
  } else if (_m365.state !== "waiting") {
    const inBtn = el("button", { className: "acc-action-btn", text: "Iniciar sesión", attrs: { type: "button", id: "m365-login-btn" } });
    inBtn.addEventListener("click", () => m365Act(inBtn, noticeEl, body, "/login", ""));
    actions.appendChild(inBtn);
  }

  // Interruptor: apagar NO cierra la sesion (volver a prenderlo no obliga a entrar de nuevo).
  const enabled = _m365.enabled !== false;
  const toggle = _ctx.buildSwitch
    ? _ctx.buildSwitch({
        checked: enabled,
        label: `${enabled ? "Desactivar" : "Activar"} ${_m365.display || "Outlook y Teams"}`,
        onToggle: async (next) => {
          await m365Api("/switch", jsonBody("POST", { enabled: next }));
          await renderM365(body, next ? "Outlook y Teams quedó activado." : "Outlook y Teams quedó desactivado: Vision no lo usa, pero tu sesión sigue guardada.");
          await refreshVault();
        },
      })
    : null;
  if (toggle) toggle.id = "m365-switch";
  const switchRow = el("div", { className: "sw-row" }, [
    el("span", { className: "sw-row-name", text: "Usar Outlook y Teams" }),
    el("span", { className: "sw-row-note", text: enabled ? "activa" : "desactivada" }),
    toggle,
  ]);

  const nameBlock = capabilityNameBlock({
    prefix: "m365",
    path: "/api/m365/name",
    current: _m365.name || "Principal",
    onSaved: async (msg) => {
      await renderM365(body, msg);
      await refreshVaultSummaryOnly();
    },
  });
  body.replaceChildren(statusRow, nameBlock, actions, switchRow, noticeEl);
  if (_m365.state === "waiting") startM365Polling(body);
}

// Toca un boton (iniciar o cerrar sesion): lo deshabilita mientras tanto y repinta con lo que dice el servidor.
async function m365Act(button, noticeEl, body, path, doneNotice) {
  button.disabled = true;
  try {
    await m365Api(path, { method: "POST" });
    await renderM365(body, doneNotice);
    await refreshVaultSummaryOnly();
  } catch (err) {
    button.disabled = false;
    noticeEl.textContent = _ctx.friendlyErrorMessage(err);
    noticeEl.dataset.state = "error";
  }
}

// Mientras la persona termina de entrar en el navegador, se consulta cada 2 s hasta que deje de estar «esperando»; si cierra la ventana, se corta.
function startM365Polling(body) {
  stopM365Polling();
  _m365Timer = setInterval(async () => {
    if (!_modalEl || !_modalEl.contains(body)) return stopM365Polling();
    await loadM365Status();
    if (_m365 && _m365.state !== "waiting") {
      stopM365Polling();
      await renderM365(body, _m365.state === "connected" ? "Listo: iniciaste sesión en Outlook y Teams." : "");
      await refreshVaultSummaryOnly();
    }
  }, 2000);
}

// Repinta solo el resumen (la fila de la conexion), sin volver a pedir todo ni cerrar la ventana abierta.
async function refreshVaultSummaryOnly() {
  const old = document.getElementById("vault-summary");
  if (!old) return;
  old.replaceWith(renderSummary());
}

// ---------------------------------------------------------------------------------------------------------------------
// Office (Excel y Word) -- spec office-com-automatizacion REQ-009 y REQ-015. Muestra si Excel esta abierto y si Word esta instalado (el servidor mira sin abrir ni lanzar nada) y el
// interruptor que saca las herramientas de Office del modelo. Habla con `/api/office/*` (solo esta PC y esta pagina). Todo texto del servidor se pinta con textContent.
// ---------------------------------------------------------------------------------------------------------------------
function officeApi(path, options = {}) {
  return _ctx.apiFetchOk(`${_ctx.API_BASE}/api/office${path}`, options);
}

async function loadOfficeStatus() {
  try {
    _office = await (await officeApi("/status")).json();
  } catch {
    _office = null; // sin respuesta: la fila no aparece y la ventana lo dice; el resto de las conexiones sigue andando
  }
}

function officeSummary() {
  if (!_office) return null;
  if (_office.enabled === false) return { text: "Apagado", state: "idle" };
  return { text: "Activo", state: "ok" };
}

// Un estado -> la insignia de esa aplicacion (el texto de la frase lo escribe el servidor).
const OFFICE_BADGES = {
  running: { text: "Abierto", state: "ok" },
  installed: { text: "Instalado", state: "ok" },
  closed: { text: "Cerrado", state: "warn" },
  busy: { text: "Ocupado", state: "warn" },
  missing: { text: "No disponible", state: "error" },
  unknown: { text: "Sin saber", state: "warn" },
  off: { text: "Apagado", state: "idle" },
};

function officeAppRow(name, item) {
  const badge = OFFICE_BADGES[item.state] || OFFICE_BADGES.unknown;
  return el("div", { className: "vault-card" }, [
    el("div", { className: "vault-card-head" }, [
      el("strong", { text: name }),
      el("span", { className: "vault-badge", text: badge.text, attrs: { "data-state": badge.state } }),
    ]),
    el("p", { className: "settings-hint", text: item.message }),
  ]);
}

async function renderOffice(body, notice = "") {
  await loadOfficeStatus();
  if (!_office) {
    body.replaceChildren(el("p", { className: "settings-hint error", text: "No pude consultar el estado de Office. Revisá que Vision esté andando y volvé a abrir esta ventana." }));
    return;
  }
  const noticeEl = el("div", { className: "vault-notice", attrs: { role: "status" } });
  if (notice) noticeEl.textContent = notice;
  const summary = officeSummary();
  const head = el("div", { className: "vault-card", attrs: { id: "office-card" } }, [
    el("div", { className: "vault-card-head" }, [
      el("strong", { text: _office.display || "Office - Principal" }),
      el("span", { className: "vault-badge", text: summary.text, attrs: { "data-state": summary.state, id: "office-badge" } }),
    ]),
  ]);

  const enabled = _office.enabled !== false;
  const toggle = _ctx.buildSwitch
    ? _ctx.buildSwitch({
        checked: enabled,
        label: `${enabled ? "Desactivar" : "Activar"} ${_office.display || "Office"}`,
        onToggle: async (next) => {
          await officeApi("/switch", jsonBody("POST", { enabled: next }));
          await renderOffice(body, next ? "Office quedó activado." : "Office quedó desactivado: Vision no lo usa y no toca ningún libro ni documento.");
          await refreshVaultSummaryOnly();
        },
      })
    : null;
  if (toggle) toggle.id = "office-switch";
  const switchRow = el("div", { className: "sw-row" }, [
    el("span", { className: "sw-row-name", text: "Usar Office" }),
    el("span", { className: "sw-row-note", text: enabled ? "activo" : "desactivado" }),
    toggle,
  ]);

  const refresh = el("button", { className: "acc-action-btn secondary", text: "Actualizar estado", attrs: { type: "button", id: "office-refresh-btn" } });
  refresh.addEventListener("click", async () => {
    refresh.disabled = true;
    await renderOffice(body, "");
    await refreshVaultSummaryOnly();
  });
  const actions = el("div", { className: "vault-card-actions" }, [refresh]);

  const nameBlock = capabilityNameBlock({
    prefix: "office",
    path: "/api/office/name",
    current: _office.name || "Principal",
    onSaved: async (msg) => {
      await renderOffice(body, msg);
      await refreshVaultSummaryOnly();
    },
  });
  body.replaceChildren(head, nameBlock, officeAppRow("Excel", _office.excel), officeAppRow("Word", _office.word), switchRow, actions, noticeEl);
}

async function renderGeneralSection(body, connector, notice = "") {
  body.replaceChildren(el("p", { className: "settings-hint", text: "Cargando…" }));
  let data;
  try {
    data = await (await api("/general")).json();
  } catch (err) {
    body.replaceChildren(el("p", { className: "settings-hint error", text: _ctx.friendlyErrorMessage(err) }));
    return;
  }
  // Pedido del usuario 2026-10-09: el ID de voz se elige en Configuración > Audio (gana sobre este, que queda solo como respaldo del .env importado).
  const HIDDEN_FIELDS = new Set(["bq_key_json", "graph_source", "fish_voice_id", "elevenlabs_voice_id"]);
  const fields = (data.fields || []).filter((f) => f.section === connector.section && !HIDDEN_FIELDS.has(f.key));
  const noticeEl = el("div", { className: "vault-notice", attrs: { role: "status" } });
  if (notice) {
    noticeEl.textContent = notice;
    noticeEl.dataset.state = "ok";
  }
  const controls = new Map();
  const rows = [];
  const keyState = { imported: false }; // la key se guarda al importarla: «Guardar» no tiene que decir «No hay cambios» como si no la hubiera tomado
  if (connector.id === "bigquery") rows.push(renderBigQueryKey(data, noticeEl, keyState));
  if (connector.id === "graph") {
    const select = el("select", { attrs: { id: "vault-field-graph_source" } });
    const current = (data.fields.find((f) => f.key === "graph_source") || {}).value || "";
    select.appendChild(el("option", { text: "La SharePoint predeterminada", attrs: { value: "" } }));
    for (const name of data.graph_sources || []) {
      const opt = el("option", { text: name, attrs: { value: name } });
      if (name === current) opt.selected = true;
      select.appendChild(opt);
    }
    controls.set("graph_source", { input: select, secret: false, initial: current });
    rows.push(fieldRow("SharePoint de la que toma las credenciales", false, select, "vault-field-graph_source"));
  }
  for (const f of fields) {
    const id = `vault-field-${f.key}`;
    if (f.env_locked) {
      rows.push(fieldRow(f.label, false, el("div", { className: "settings-hint", text: "Lo fija una variable de entorno de Windows: se usa esa." }), id));
      continue;
    }
    if (f.secret) {
      const wrap = secretInput({ id, label: f.label, stored: f.set, revealPath: f.set ? `/general/secret/${f.key}` : null, noticeEl });
      controls.set(f.key, { input: wrap.querySelector("input"), secret: true, stored: f.set });
      rows.push(fieldRow(f.label, false, wrap, id));
    } else {
      const input = el("input", { attrs: { type: f.kind === "int" ? "number" : "text", id, autocomplete: "off", spellcheck: "false" } });
      input.value = f.value || "";
      controls.set(f.key, { input, secret: false, initial: input.value });
      rows.push(fieldRow(f.label, false, input, id));
    }
  }
  const saveBtn = el("button", { className: "acc-action-btn", text: "Guardar", attrs: { type: "button", id: "vault-save-btn" } });
  // Pedido del usuario 2026-10-10: «Probar» antes de guardar. En BigQuery: el proyecto y la ubicación escritos + la key guardada.
  const actions = [saveBtn];
  const sectionTestEl = el("div", { className: "vault-card-result", attrs: { id: "vault-section-test-result", role: "status" } });
  if (connector.id === "bigquery") {
    const testBtn = el("button", { className: "acc-action-btn secondary", text: "Probar", attrs: { type: "button", id: "vault-section-test-btn" } });
    testBtn.addEventListener("click", async () => {
      testBtn.disabled = true;
      const values = {};
      for (const key of ["bq_project_id", "bq_location"]) {
        const c = controls.get(key);
        if (c && !c.secret) values[key] = c.input.value;
      }
      await runBigQueryTest(sectionTestEl, values);
      testBtn.disabled = false;
    });
    actions.unshift(testBtn);
  }
  saveBtn.addEventListener("click", async () => {
    const values = {};
    let changesStoredSecret = false;
    for (const [key, c] of controls) {
      if (c.secret) {
        if (c.input.dataset.touched === "true") {
          values[key] = c.input.value;
          if (c.stored) changesStoredSecret = true;
        }
      } else if (c.input.value !== c.initial) {
        values[key] = c.input.value;
      }
    }
    if (!Object.keys(values).length) {
      if (keyState.imported) {
        noticeEl.textContent = "La clave ya quedó guardada al importarla. No hay otros cambios para guardar.";
        noticeEl.dataset.state = "ok";
        return;
      }
      noticeEl.textContent = "No hay cambios para guardar.";
      return;
    }
    saveBtn.disabled = true;
    try {
      if (changesStoredSecret && !(await ensureUnlocked(noticeEl))) return;
      await api("/general", jsonBody("PUT", { values }));
      _ctx.onChanged?.();
      openConnectorModal(connector.id, { notice: "Guardado." }); // la ventana redibujada lo muestra
    } catch (err) {
      noticeEl.textContent = _ctx.friendlyErrorMessage(err);
      noticeEl.dataset.state = "error";
    } finally {
      saveBtn.disabled = false;
    }
  });
  body.replaceChildren(...rows, noticeEl, sectionTestEl, el("div", { className: "settings-actions" }, actions));
}

// Prueba BigQuery sin guardar nada y deja el resultado en `resultEl` (verde/rojo). Lo usan «Probar» y la importación de la key.
async function runBigQueryTest(resultEl, values = {}) {
  resultEl.textContent = "Probando la conexión con BigQuery…";
  resultEl.dataset.state = "";
  try {
    const result = await (await api("/bigquery/test", jsonBody("POST", { values }))).json();
    resultEl.textContent = result.ok ? "Conectó bien con BigQuery." : result.detail || "No se pudo conectar con BigQuery.";
    resultEl.dataset.state = result.ok ? "ok" : "error";
    _bqLastTest = Boolean(result.ok);
  } catch (err) {
    resultEl.textContent = _ctx.friendlyErrorMessage(err);
    resultEl.dataset.state = "error";
    _bqLastTest = false;
  }
  if (_status) renderRoot(); // la fila de BigQuery del resumen (fuera de esta ventana) muestra el resultado
}

// REQ-008: importar la key de BigQuery arrastrando, eligiendo el archivo o pegándola. Se valida en el servidor y vuelve solo proyecto y cuenta.
function renderBigQueryKey(data, noticeEl, keyState = { imported: false }) {
  const keyField = data.fields.find((f) => f.key === "bq_key_json") || {};
  const project = (data.fields.find((f) => f.key === "bq_project_id") || {}).value || "";
  const fileInput = el("input", { attrs: { type: "file", accept: ".json,application/json", id: "vault-bq-file", class: "hidden" } });
  const pickBtn = el("button", { className: "acc-action-btn", text: keyField.set ? "Importar una clave nueva (.json)" : "Importar archivo de clave (.json)", attrs: { type: "button", id: "vault-bq-pick" } });
  const paste = el("textarea", { attrs: { id: "vault-bq-paste", rows: "4", placeholder: '...o pegá acá el contenido: {"type": "service_account", ...}', spellcheck: "false" } });
  const pasteBtn = el("button", { className: "acc-action-btn secondary", text: "Usar lo pegado", attrs: { type: "button", id: "vault-bq-paste-btn" } });
  const drop = el("div", { className: "vault-dropzone", text: "Soltá acá el archivo .json de la cuenta de servicio", attrs: { id: "vault-bq-drop" } });
  const statusEl = el("p", { className: "settings-hint", attrs: { id: "vault-bq-status" }, text: keyField.set ? `Hay una clave guardada${project ? ` para el proyecto ${project}` : ""}.` : "Todavía no hay una clave guardada." });
  // Pedido del usuario 2026-10-10: «cuando se importa el archivo correctamente deberia pintarse en verde el cuadro de importacion ... sino me quedo
  // sin saber». El resultado se muestra EN el recuadro (antes solo abajo de todo, lejos de donde se suelta el archivo) y despues se prueba solo.
  const keyResultEl = el("p", { className: "vault-bq-result", attrs: { id: "vault-bq-result", role: "status" } });
  const keyTestEl = el("div", { className: "vault-card-result", attrs: { id: "vault-bq-test-result", role: "status" } });
  let box = null;
  const showKey = (state, text) => {
    if (box) box.dataset.state = state;
    keyResultEl.textContent = text;
  };

  const send = async (text) => {
    showKey("", "Importando la clave…");
    keyTestEl.textContent = "";
    noticeEl.textContent = "";
    noticeEl.dataset.state = "";
    try {
      if (keyField.set) {
        showKey("", "Ya hay una clave guardada: para reemplazarla, confirmá que sos vos en la ventana de Windows Hello.");
        if (!(await ensureUnlocked(noticeEl))) {
          showKey("error", `No se importó: ${noticeEl.textContent || "no se confirmó que seas vos"}`);
          return; // reemplazar una key guardada exige desbloqueo
        }
      }
      const info = await (await api("/bigquery-key", jsonBody("POST", { key_json: text }))).json();
      showKey("ok", `✓ Clave importada y guardada: cuenta ${info.client_email} · proyecto ${info.project_id}${info.project_set ? " (se completó solo)" : ""}.`);
      paste.value = "";
      // se actualiza en el lugar (sin volver a dibujar la ventana, que borraría este aviso)
      keyField.set = true;
      statusEl.textContent = `Hay una clave guardada para el proyecto ${info.project_id}.`;
      pickBtn.textContent = "Importar una clave nueva (.json)";
      const projectInput = _modalEl && _modalEl.querySelector("#vault-field-bq_project_id");
      if (info.project_set && projectInput) projectInput.value = info.project_id;
      keyState.imported = true;
      if (_general) {
        const keyGeneral = (_general.fields || []).find((f) => f.key === "bq_key_json");
        if (keyGeneral) keyGeneral.set = true; // la fila del resumen pasa a «Clave guardada» sin esperar a recargar
      }
      _ctx.onChanged?.();
      await runBigQueryTest(keyTestEl); // «tomarlo y registrarlo al mismo momento»: se prueba sola apenas se guarda
    } catch (err) {
      showKey("error", `No se importó: ${_ctx.friendlyErrorMessage(err)}`);
    }
  };
  const readFile = async (file) => {
    if (!file) return;
    if (file.size > 100_000) {
      showKey("error", "No se importó: ese archivo es demasiado grande para ser una clave de cuenta de servicio.");
      return;
    }
    await send(await file.text());
  };
  pickBtn.addEventListener("click", () => fileInput.click());
  fileInput.addEventListener("change", async () => {
    const file = fileInput.files[0];
    fileInput.value = ""; // sin esto, elegir el MISMO archivo otra vez (p. ej. despues de cancelar Windows Hello) no hacia nada
    await readFile(file);
  });
  pasteBtn.addEventListener("click", () => send(paste.value));
  drop.addEventListener("dragover", (e) => {
    e.preventDefault();
    drop.dataset.state = "over";
  });
  drop.addEventListener("dragleave", () => {
    drop.dataset.state = "";
  });
  drop.addEventListener("drop", (e) => {
    e.preventDefault();
    drop.dataset.state = "";
    readFile(e.dataTransfer.files[0]);
  });
  box = el("div", { className: "vault-bq-key", attrs: { id: "vault-bq-box" } }, [
    el("h4", { text: "Cuenta de servicio" }),
    statusEl,
    drop,
    el("div", { className: "settings-actions" }, [pickBtn, fileInput]),
    paste,
    el("div", { className: "settings-actions" }, [pasteBtn]),
    keyResultEl,
    keyTestEl,
  ]);
  return box;
}
