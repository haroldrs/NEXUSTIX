"use strict";

// Coordinación distribuida: directorio con latidos, elección Bully, relojes de
// Cristian/Berkeley y relojes lógicos. Los datos vienen de /admin/coordinacion
// de la instancia de Compras que atienda; las órdenes de caída simulada se
// reenvían a la instancia indicada.
const COORD_API = () => `${CONFIG.COMPRAS_LB_URL}/admin/coordinacion`;
const COORD_STATES = {viva: "Viva", sospechosa: "Sospechosa", caida: "Caída", sin_registro: "Sin registro", silenciada: "Caída simulada"};
let COORD_TIMER = null;
let COORD_SELECTED = [];
let COORD_EVENTS = [];

function stopCoordinationPolling() {
  clearInterval(COORD_TIMER);
  COORD_TIMER = null;
}

function coordVectorText(vector) {
  return Object.entries(vector || {}).sort(([a], [b]) => a.localeCompare(b)).map(([key, value]) => `${key}:${value}`).join(" · ");
}

// Misma regla que shared/coordination.py: compare_vectors.
function coordCompare(a, b) {
  const keys = new Set([...Object.keys(a || {}), ...Object.keys(b || {})]);
  let less = false, more = false;
  keys.forEach(key => { const x = a?.[key] || 0, y = b?.[key] || 0; if (x < y) less = true; if (x > y) more = true; });
  if (!less && !more) return "igual";
  if (less && !more) return "antes";
  if (more && !less) return "despues";
  return "concurrente";
}

function coordStateChip(state) {
  const tone = state === "viva" ? "ok" : state === "sospechosa" ? "warn" : "bad";
  return `<span class="coord-state coord-${tone}">${adminText(COORD_STATES[state] || state)}</span>`;
}

function renderCoordination(data) {
  const me = data.instancia || {};
  adminNode("coord-leader").innerHTML = adminFacts([
    ["Coordinador actual", data.coordinador || "Sin coordinador: elección en curso"],
    ["Consultado en", `${me.id} (${me.nodo})`],
    ["¿Esta instancia es la coordinadora?", data.soy_coordinador ? "Sí" : "No"],
    ["Reloj de Lamport de esta instancia", data.reloj?.lamport],
  ]);
  const params = data.parametros || {};
  adminNode("coord-params").innerHTML = adminFacts([["Intervalo de latido (T)", `${params.latido_s} s`], ["Sospecha (T + Δ)", `${params.sospecha_s} s`], ["Caída declarada", `${params.caida_s} s`], ["TTL de resolución de nombres", `${params.ttl_resolucion_s} s`]]);
  const rows = adminList(data.directorio);
  adminNode("coord-directory").innerHTML = rows.length ? `<table class="data-table"><thead><tr><th>Instancia</th><th>Servicio</th><th>Computadora</th><th>Dirección</th><th>Prioridad</th><th>Estado</th><th>Último latido</th><th>Acción</th></tr></thead><tbody>${rows.map(row => `<tr><td>${adminText(row.id)}${row.id === data.coordinador ? " <strong>· coordinador</strong>" : ""}</td><td>${adminText(row.servicio)}</td><td>${adminText(row.nodo)}</td><td>${adminText(row.url || "local")}</td><td>${adminText(row.prioridad)}</td><td>${coordStateChip(row.estado)}</td><td>${row.es_yo ? "esta instancia" : row.ultimo_latido_hace_s == null ? "nunca" : `hace ${adminText(row.ultimo_latido_hace_s)} s`}</td><td>${row.estado === "silenciada" ? `<button type="button" class="button button-outline" data-coord-restore="${adminText(row.id)}">Restaurar</button>` : `<button type="button" class="button button-outline" data-coord-silence="${adminText(row.id)}">Simular caída</button><button type="button" class="button button-outline" data-coord-restore="${adminText(row.id)}">Restaurar</button>`}</td></tr>`).join("")}</tbody></table>` : adminEmpty("Sin instancias declaradas.");
  const cristian = data.cristian || {};
  adminNode("coord-cristian").innerHTML = adminFacts([["Servidor de referencia", cristian.referencia || "Sin coordinador"], ["Desfase estimado", cristian.desfase_ms == null ? "Sin medición" : `${cristian.desfase_ms} ms`], ["Ida y vuelta (RTT)", cristian.rtt_ms == null ? "Sin medición" : `${cristian.rtt_ms} ms`], ["Medido", adminDate(cristian.medido_en)]]);
  const berkeley = data.berkeley || {};
  const participants = adminList(berkeley.participantes);
  adminNode("coord-berkeley").innerHTML = `${adminFacts([["Ajuste de esta instancia", berkeley.ajuste_ms == null ? "Sin ronda" : `${berkeley.ajuste_ms} ms`], ["Última ronda", adminDate(berkeley.ronda)]])}${participants.length ? adminTable(participants, [["Instancia", "instancia"], ["Desfase", row => `${row.desfase_ms} ms`], ["Ajuste enviado", row => `${row.ajuste_ms} ms`]]) : '<p class="field-help">La tabla completa aparece cuando se consulta a la instancia coordinadora.</p>'}`;
  const history = adminList(data.elecciones);
  adminNode("coord-history").innerHTML = history.length ? history.map(item => `<div class="admin-row"><div><strong>${adminText(item.detalle)}</strong><small>${adminDate(item.momento)} · Lamport ${adminText(item.lamport)}</small></div></div>`).join("") : adminEmpty("Sin elecciones registradas todavía.");
}

function renderCoordinationEvents(data) {
  COORD_EVENTS = adminList(data.eventos).slice(-80).reverse();
  COORD_SELECTED = COORD_SELECTED.filter(id => COORD_EVENTS.some(row => row.id === id));
  adminNode("coord-events").innerHTML = COORD_EVENTS.length ? `<p class="field-help">Instancias consultadas: ${adminText(adminList(data.consultadas).join(" · "))}</p><table class="data-table"><thead><tr><th>Comparar</th><th>Instancia</th><th>Evento</th><th>Lamport</th><th>Vector</th><th>Hora física</th></tr></thead><tbody>${COORD_EVENTS.map(row => `<tr><td><input type="checkbox" data-coord-event="${adminText(row.id)}" ${COORD_SELECTED.includes(row.id) ? "checked" : ""} aria-label="Comparar evento"></td><td>${adminText(row.instancia)}</td><td>${adminText(row.evento)}</td><td>${adminText(row.lamport)}</td><td><code>${adminText(coordVectorText(row.vector))}</code></td><td>${adminDate(row.fisico)}</td></tr>`).join("")}</tbody></table>` : adminEmpty("Aún no hay mensajes entre servicios. Realiza una compra y vuelve a consultar.");
  renderCoordinationComparison();
}

function renderCoordinationComparison() {
  const box = adminNode("coord-compare");
  if (COORD_SELECTED.length !== 2) { box.hidden = true; return; }
  const [a, b] = COORD_SELECTED.map(id => COORD_EVENTS.find(row => row.id === id));
  const relation = coordCompare(a.vector, b.vector);
  const text = {antes: `"${a.evento}" (${a.instancia}) ocurrió ANTES y pudo causar "${b.evento}" (${b.instancia}).`, despues: `"${b.evento}" (${b.instancia}) ocurrió ANTES y pudo causar "${a.evento}" (${a.instancia}).`, concurrente: `Los eventos son CONCURRENTES: ninguno pudo influir en el otro, aunque sus relojes de Lamport (${a.lamport} y ${b.lamport}) sugieran un orden.`, igual: "Es el mismo estado causal."}[relation];
  box.hidden = false;
  box.className = `notice ${relation === "concurrente" ? "notice-warning" : "notice-success"}`;
  box.textContent = text;
}

async function refreshCoordination() {
  if (!adminAllowed()) return;
  const [state, events] = await Promise.all([adminRequest(COORD_API()), adminRequest(`${COORD_API()}/eventos`)]);
  if (state.stale) return;
  const notice = adminNode("coord-notice");
  if (!state.ok) {
    notice.hidden = false;
    notice.className = "notice notice-error";
    notice.textContent = adminError(state, "No se pudo consultar la coordinación.");
    return;
  }
  notice.hidden = true;
  renderCoordination(state.data);
  if (events.ok) renderCoordinationEvents(events.data);
}

async function coordAction(button, path, message) {
  await adminBusy(button, async () => {
    const result = await adminRequest(`${COORD_API()}${path}`, {method: "POST"});
    const notice = adminNode("coord-notice");
    notice.hidden = false;
    notice.className = `notice ${result.ok ? "notice-success" : "notice-error"}`;
    notice.textContent = result.ok ? message : adminError(result, "La orden no se completó.");
    await refreshCoordination();
  });
}

function loadCoordinationPanel() {
  refreshCoordination();
  stopCoordinationPolling();
  COORD_TIMER = setInterval(() => { if (!document.hidden && ADMIN_SECTION === "coordination") refreshCoordination(); }, 5000);
}

document.addEventListener("DOMContentLoaded", () => {
  const panel = document.querySelector('[data-admin-panel="coordination"]');
  if (!panel) return;
  adminNode("coord-refresh").addEventListener("click", refreshCoordination);
  adminNode("coord-election").addEventListener("click", event => coordAction(event.currentTarget, "/eleccion", "Elección convocada. El historial muestra los mensajes."));
  adminNode("coord-sync").addEventListener("click", event => coordAction(event.currentTarget, "/sincronizar", "Ronda de Cristian y Berkeley ejecutada."));
  panel.addEventListener("click", event => {
    const silence = event.target.closest("[data-coord-silence]");
    const restore = event.target.closest("[data-coord-restore]");
    if (silence) coordAction(silence, `/simular-caida?instancia=${encodeURIComponent(silence.dataset.coordSilence)}`, `${silence.dataset.coordSilence} deja de latir. En unos segundos aparecerá como sospechosa y, si era coordinadora, habrá una nueva elección.`);
    if (restore) coordAction(restore, `/restaurar?instancia=${encodeURIComponent(restore.dataset.coordRestore)}`, `${restore.dataset.coordRestore} vuelve a latir y convoca una elección.`);
  });
  panel.addEventListener("change", event => {
    const box = event.target.closest("[data-coord-event]");
    if (!box) return;
    COORD_SELECTED = box.checked ? [...COORD_SELECTED, box.dataset.coordEvent].slice(-2) : COORD_SELECTED.filter(id => id !== box.dataset.coordEvent);
    panel.querySelectorAll("[data-coord-event]").forEach(input => { input.checked = COORD_SELECTED.includes(input.dataset.coordEvent); });
    renderCoordinationComparison();
  });
  adminNode("coord-resolve-form").addEventListener("submit", async event => {
    event.preventDefault();
    const service = adminNode("coord-resolve-service").value;
    const result = await fetchJSON(`${CONFIG.COMPRAS_LB_URL}/coordinacion/resolver/${encodeURIComponent(service)}`);
    adminNode("coord-resolve-result").innerHTML = result.ok ? `${adminTable(adminList(result.data.direcciones), [["Instancia", "instancia"], ["Computadora", "nodo"], ["Dirección", row => row.url || "local"]], "Ninguna instancia viva de ese servicio.")}<p class="field-help">Respuesta válida en caché durante ${adminText(result.data.ttl_s)} s.</p>` : adminEmpty(adminError(result, "No se pudo resolver."));
  });
});
