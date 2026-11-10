"use strict";

let EVENTS = [];
let SELECTED_EVENT = null;
let CATALOG_LIMIT = 12;
let CATALOG_REQUEST = 0;
let DETAIL_REQUEST = 0;

function money(cents) { return `S/ ${(Number(cents || 0) / 100).toFixed(2)}`; }
function eventStartingPrice(event) { return event.precio_desde_centimos ?? event.precio_centimos; }
function eventName(id) { return EVENTS.find(event => Number(event.id) === Number(id))?.nombre || `Evento #${id}`; }
function eventState(event) {
  if (!event) return "desconocido";
  if (event.cancelado_en || event.activo === false) return "cancelado";
  if (event.fecha && new Date(event.fecha).getTime() <= Date.now()) return "finalizado";
  return Number(event.stock) > 0 ? "disponible" : "agotado";
}
function eventDate(value) {
  return value ? new Date(value).toLocaleString("es-PE", {dateStyle: "medium", timeStyle: "short"}) : "Fecha por confirmar";
}
function safeEventImage(value) {
  if (!value) return "";
  try { const url = new URL(value, location.href); return ["https:", "http:"].includes(url.protocol) && !url.username && !url.password ? url.href : ""; }
  catch (_) { return ""; }
}
function eventVisual(event, index = 0) {
  const url = safeEventImage(event.imagen_url);
  return `<div class="event-visual visual-${index % 4}">${url ? `<img src="${escapeHTML(url)}" alt="${escapeHTML(event.nombre)}" loading="lazy" referrerpolicy="no-referrer">` : '<span class="visual-symbol" aria-hidden="true">✳</span>'}<span class="visual-index">${escapeHTML(event.categoria || "EN VIVO")}</span><span class="visual-brand">NEXUSTIX / LIVE</span></div>`;
}

async function loadEvents() {
  const request = ++CATALOG_REQUEST;
  const detailRequest = DETAIL_REQUEST;
  const status = document.getElementById("catalog-status");
  document.getElementById("event-grid").setAttribute("aria-busy", "true");
  showNotice(status, "Actualizando eventos…");
  const response = await fetchJSON(`${CONFIG.EVENTOS_URL}/eventos`);
  if (request !== CATALOG_REQUEST) return;
  document.getElementById("event-grid").setAttribute("aria-busy", "false");
  if (!response.ok || !Array.isArray(response.data)) {
    showNotice(status, EVENTS.length ? "La información de los eventos puede estar desactualizada. No podemos confirmar la disponibilidad hasta que el servicio responda." : "El catálogo no está disponible por ahora. Usa Actualizar catálogo para reintentar.", EVENTS.length ? "warning" : "error");
    if (!EVENTS.length) document.getElementById("event-grid").innerHTML = '<div class="empty-state">El catálogo no está disponible por ahora.</div>';
    if (SELECTED_EVENT && detailRequest === DETAIL_REQUEST) { SELECTED_EVENT = null; updatePurchaseSummary(); }
    return;
  }
  EVENTS = response.data;
  const category = document.getElementById("event-category");
  const selected = category.value;
  const categories = [...new Set(EVENTS.map(event => event.categoria).filter(Boolean))].sort();
  category.innerHTML = '<option value="">Todas</option>' + categories.map(value => `<option value="${escapeHTML(value)}">${escapeHTML(value)}</option>`).join("");
  category.value = categories.includes(selected) ? selected : "";
  category.disabled = categories.length === 0;
  if (SELECTED_EVENT && detailRequest === DETAIL_REQUEST) {
    SELECTED_EVENT = EVENTS.find(event => Number(event.id) === Number(SELECTED_EVENT.id)) || null;
    renderDetail(); updatePurchaseSummary();
  }
  renderCatalog();
}

function renderCatalog() {
  const query = document.getElementById("event-search").value.trim().toLocaleLowerCase("es");
  const filter = document.getElementById("event-filter").value;
  const category = document.getElementById("event-category").value;
  const since = document.getElementById("event-date").value;
  const sort = document.getElementById("event-sort").value;
  const visible = EVENTS.filter(event => (!query || event.nombre.toLocaleLowerCase("es").includes(query))
    && (filter === "all" || eventState(event) === filter) && (!category || event.categoria === category)
    && (!since || (event.fecha && new Date(event.fecha) >= new Date(`${since}T00:00:00`))));
  visible.sort((a, b) => {
    if (sort === "precio-asc") return eventStartingPrice(a) - eventStartingPrice(b) || a.id - b.id;
    if (sort === "precio-desc") return eventStartingPrice(b) - eventStartingPrice(a) || a.id - b.id;
    return (a.fecha ? new Date(a.fecha).getTime() : Infinity) - (b.fecha ? new Date(b.fecha).getTime() : Infinity) || a.id - b.id;
  });
  showNotice(document.getElementById("catalog-status"), `${visible.length} evento(s) · mostrando ${Math.min(visible.length, CATALOG_LIMIT)}. Disponibilidad sujeta a confirmación.`, "");
  const grid = document.getElementById("event-grid");
  grid.innerHTML = visible.length ? visible.slice(0, CATALOG_LIMIT).map((event, index) => {
    const state = eventState(event);
    return `<article class="event-card">${eventVisual(event, index)}<div class="event-card-body"><div class="event-kicker"><span>${escapeHTML(event.categoria || "EVENTO")}</span><span class="stock-state ${state === "disponible" ? "available" : "sold-out"}">${escapeHTML(state)}</span></div><h3>${escapeHTML(event.nombre)}</h3><p class="event-unavailable">${escapeHTML(eventDate(event.fecha))}<br>${escapeHTML(event.lugar || "Lugar por confirmar")}</p><div class="event-card-bottom"><span class="event-price">Desde<strong>${money(eventStartingPrice(event))}</strong></span><a class="text-button" href="#evento/${Number(event.id)}">Ver evento →</a></div></div></article>`;
  }).join("") : '<div class="empty-state">No hay eventos con estos filtros. Prueba con otro nombre o fecha.</div>';
  document.getElementById("events-more").hidden = visible.length <= CATALOG_LIMIT;
}

function renderDetail() {
  const target = document.getElementById("event-detail");
  if (!SELECTED_EVENT) { target.innerHTML = '<h1 id="detail-title">Evento no disponible</h1><p>Regresa al catálogo o actualiza la página para consultar otra vez.</p>'; return; }
  const event = SELECTED_EVENT;
  const state = eventState(event);
  target.innerHTML = `${eventVisual(event, Number(event.id) % 4)}<div class="detail-copy"><p class="eyebrow">${escapeHTML(event.categoria || "EN VIVO")}</p><h1 id="detail-title" tabindex="-1">${escapeHTML(event.nombre)}</h1><p>${escapeHTML(event.descripcion || "El organizador todavía no ha publicado una descripción.")}</p><dl class="detail-facts"><div><dt>Fecha</dt><dd>${escapeHTML(eventDate(event.fecha))}</dd></div><div><dt>Lugar</dt><dd>${escapeHTML(event.lugar || "Por confirmar")}</dd></div><div><dt>Estado</dt><dd>${escapeHTML(state)}${state === "disponible" ? ` · ${Number(event.stock)} entradas` : ""}</dd></div><div><dt>Entradas desde</dt><dd>${money(eventStartingPrice(event))}</dd></div></dl>${event.motivo_cancelacion ? `<p class="notice notice-warning">Cancelación: ${escapeHTML(event.motivo_cancelacion)}</p>` : ""}<p class="detail-pricing-note">Elige cantidad, zona o butacas según la distribución del evento. El servidor confirma el precio al reservar.</p></div>`;
}

async function showEventDetail(id, updateHash = true) {
  if (CHECKOUT_BUSY) { showNotice(document.getElementById("global-notice"), "Espera el resultado de la solicitud en curso antes de cambiar de evento.", "warning"); return; }
  const request = ++DETAIL_REQUEST;
  ++SEATS_REQUEST;
  SEATS_READY = false;
  const changed = INVENTORY_EVENT !== id;
  SELECTED_EVENT = null;
  document.getElementById("purchase-event").value = String(id);
  if (changed) { document.getElementById("purchase-quantity").value = "1"; clearPurchaseStatus(); }
  setView("evento", false);
  if (updateHash) history.pushState(null, "", `#evento/${id}`);
  showNotice(document.getElementById("detail-status"), "Consultando disponibilidad y precios…");
  document.getElementById("event-detail").innerHTML = '<h1 id="detail-title">Cargando evento…</h1>';
  document.getElementById("seat-options").textContent = "Actualizando disponibilidad…";
  updatePurchaseSummary();
  const response = await fetchJSON(`${CONFIG.EVENTOS_URL}/eventos/${id}`);
  if (request !== DETAIL_REQUEST || id !== Number(document.getElementById("purchase-event").value) || CHECKOUT_BUSY) return;
  if (!response.ok || Number(response.data?.id) !== id) {
    showNotice(document.getElementById("detail-status"), response.error || errorMessage(response.data, "No se pudo consultar este evento."), "error");
    renderDetail(); return;
  }
  SELECTED_EVENT = response.data;
  renderDetail();
  showNotice(document.getElementById("detail-status"), eventState(SELECTED_EVENT) === "disponible" ? "Selecciona tus entradas. Podrás revisar el pedido antes de confirmar." : "Este evento no admite nuevas compras.", eventState(SELECTED_EVENT) === "disponible" ? "" : "warning");
  await loadSeats();
  if (request === DETAIL_REQUEST) document.getElementById("detail-title")?.focus({preventScroll: true});
}

function routeLocation() {
  const path = location.hash.replace(/^#/, "");
  const detail = /^evento\/(\d+)$/.exec(path);
  if (detail) { showEventDetail(Number(detail[1]), false); return; }
  if (path === "entrada" || path.startsWith("entrada/")) { setView("entrada", false); showTicketPresentation(path.slice(8)); return; }
  setView(path || "comprar", false);
}

function initializeCatalog() {
  document.getElementById("refresh-events").addEventListener("click", loadEvents);
  ["event-search", "event-filter", "event-category", "event-date", "event-sort"].forEach(id => {
    document.getElementById(id).addEventListener(id === "event-search" ? "input" : "change", () => { CATALOG_LIMIT = 12; renderCatalog(); });
  });
  document.getElementById("events-more").addEventListener("click", () => { CATALOG_LIMIT += 12; renderCatalog(); });
}
