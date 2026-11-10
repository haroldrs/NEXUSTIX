"use strict";

let ORDER_CART = [];
let CHECKOUT_ORDERS = CONFIG.CHECKOUT_ORDERS === true;
let CHECKOUT_READY = false;
let CHECKOUT_BUSY = false;
let SEATS = [];
let ZONES = [];
let SELECTED_SEATS = new Set();
let SELECTED_ZONE = "";
let INVENTORY_EVENT = 0;
let SEATS_READY = false;
let SEATS_REQUEST = 0;
let ORDER_LIST_REQUEST = 0;
let REVIEW = null;
let PENDING_REVIEW = null;
const INTENT_KEYS = new Map();
const ORDER_ACTIONS = new Set();

function setCheckoutStep(step) {
  document.querySelectorAll(".checkout-steps li").forEach((item, index) => {
    item.classList.toggle("active", index + 1 === step);
    if (index + 1 === step) item.setAttribute("aria-current", "step");
    else item.removeAttribute("aria-current");
  });
}

async function detectCheckout() {
  if (typeof CONFIG.CHECKOUT_ORDERS === "boolean") {
    CHECKOUT_ORDERS = CONFIG.CHECKOUT_ORDERS; CHECKOUT_READY = true;
  } else {
    const response = await fetchJSON(`${CONFIG.COMPRAS_LB_URL}/checkout/capacidades`);
    CHECKOUT_READY = response.ok && response.data?.pedidos === true || response.status === 404;
    CHECKOUT_ORDERS = response.ok && response.data?.pedidos === true;
  }
  document.getElementById("order-options").hidden = !CHECKOUT_ORDERS;
  document.getElementById("checkout-panel").hidden = CHECKOUT_READY && !CHECKOUT_ORDERS;
  document.getElementById("checkout-reconnect").hidden = CHECKOUT_READY;
  document.getElementById("checkout-mode-note").textContent = !CHECKOUT_READY ? "No se pudo consultar el modo de compra. Reintenta la conexión." : CHECKOUT_ORDERS ? "Primero reservas; después completas el pago simulado en Mis entradas." : "Compra directa. Este despliegue usa el flujo compatible sin pagos ni QR de pedidos.";
  updatePurchaseSummary();
  if (SELECTED_EVENT) loadSeats();
  if (CURRENT_USER) refreshOrders();
}

function selectedItem() {
  return {evento_id: Number(document.getElementById("purchase-event").value),
    cantidad: Number(document.getElementById("purchase-quantity").value),
    asientos: [...SELECTED_SEATS].sort(), ...(SELECTED_ZONE ? {zona: SELECTED_ZONE} : {})};
}
function groupMode() { return CHECKOUT_ORDERS && document.getElementById("purchase-mode").value === "grupal"; }
function checkoutItems() { return groupMode() ? ORDER_CART.map(({evento_id, cantidad, asientos, zona}) => ({evento_id, cantidad, asientos, ...(zona ? {zona} : {})})) : [selectedItem()]; }
function admissionType() { return SELECTED_EVENT?.distribucion?.tipo || (SEATS.length ? "asientos" : "general"); }
function zoneColor(name, index = 0) {
  const color = SELECTED_EVENT?.distribucion?.zonas?.find(zone => zone.nombre === name)?.color;
  return /^#[0-9a-f]{6}$/i.test(color || "") ? color : ["#537c68", "#aa7540", "#647bad", "#9b648e"][Math.abs(index) % 4];
}
function itemLocation(item) { return item.zona ? `Zona: ${item.zona}` : item.asientos?.length ? `Asientos: ${item.asientos.join(", ")}` : "Admisión general"; }
function estimatedMoney(value) { return Number.isInteger(value) && value >= 0 ? money(value) : "Por confirmar"; }
function selectionQuote(item, snapshot = null) {
  const current = Number(SELECTED_EVENT?.id) === item.evento_id && SEATS_READY;
  const event = snapshot?.event || (current ? SELECTED_EVENT : EVENTS.find(row => Number(row.id) === item.evento_id));
  const inventory = snapshot?.inventory || (current ? item.zona ? ZONES : SEATS : []);
  const lines = item.asientos?.length ? item.asientos.map(code => {
    const seat = inventory.find(row => row.codigo === code);
    return {label: `${code}${seat?.zona ? ` · ${seat.zona}` : ""}`, quantity: 1, price: seat?.precio_centimos};
  }) : [{label: item.zona ? `Zona ${item.zona}` : "Admisión general", quantity: item.cantidad,
    price: item.zona ? inventory.find(zone => zone.nombre === item.zona)?.precio_centimos : event?.precio_centimos}];
  const total = lines.every(line => Number.isInteger(line.price) && line.price >= 0 && Number.isInteger(line.quantity) && line.quantity >= 0)
    ? lines.reduce((sum, line) => sum + line.price * line.quantity, 0) : null;
  return {name: event?.nombre || `Evento #${item.evento_id}`, date: event?.fecha, venue: event?.lugar, lines, total};
}
function selectionSummary(item, quote = item.quote || selectionQuote(item)) {
  return `<article class="checkout-selection"><h3 class="checkout-selection-heading">${escapeHTML(quote.name)}</h3><p class="checkout-selection-meta">${escapeHTML(eventDate(quote.date))} · ${escapeHTML(quote.venue || "Lugar por confirmar")}</p><p>${escapeHTML(itemLocation(item))}</p><ul class="checkout-price-lines">${quote.lines.map(line => `<li class="checkout-price-line"><span>${escapeHTML(line.label)}<small>${line.quantity} × ${estimatedMoney(line.price)}</small></span><span>${estimatedMoney(Number.isInteger(line.price) ? line.quantity * line.price : null)}</span></li>`).join("")}</ul><div class="checkout-subtotal"><span>Subtotal · ${item.cantidad} entrada(s)</span><strong>${estimatedMoney(quote.total)}</strong></div></article>`;
}
function itemTotal(item) {
  return item.quote ? item.quote.total : selectionQuote(item).total;
}
function selectionError(item = selectedItem()) {
  if (!SELECTED_EVENT || !CHECKOUT_READY || !SEATS_READY) return "Espera a que se confirme la disponibilidad.";
  if (eventState(SELECTED_EVENT) !== "disponible") return "Este evento no admite nuevas compras.";
  if (!Number.isInteger(item.cantidad) || item.cantidad < 1 || item.cantidad > (CHECKOUT_ORDERS ? 100 : 1000)) return "Indica una cantidad válida de entradas.";
  if (item.cantidad > Number(SELECTED_EVENT.stock)) return "La cantidad supera la disponibilidad consultada.";
  if (admissionType() !== "general" && !CHECKOUT_ORDERS) return "Las zonas y los asientos requieren el checkout de pedidos.";
  if (admissionType() === "asientos" && (item.asientos.length !== item.cantidad || item.asientos.some(code => !SEATS.find(seat => seat.codigo === code)?.disponible))) return "Selecciona un asiento disponible por entrada.";
  if (admissionType() === "zonas") {
    const zone = ZONES.find(zone => zone.nombre === item.zona);
    if (!zone) return "Selecciona una zona.";
    if (item.cantidad > zone.disponibles) return "La cantidad supera las entradas disponibles en esta zona.";
  }
  return "";
}

function renderSeatMap(target) {
  const layout = SELECTED_EVENT?.distribucion || {};
  const zones = [...new Set(SEATS.map(seat => seat.zona))];
  const byCode = new Map(SEATS.map(seat => [seat.codigo, seat]));
  const rows = new Map();
  if (layout.filas?.length && layout.columnas) {
    layout.filas.forEach(row => rows.set(row, Array.from({length: layout.columnas}, (_, i) => byCode.get(`${row}${(layout.inicio || 1) + i}`) || {codigo: `${row}${(layout.inicio || 1) + i}`, zona: "", disponible: false, estado: "no_disponible"})));
  } else {
    SEATS.slice().sort((a, b) => a.codigo.localeCompare(b.codigo, undefined, {numeric: true})).forEach(seat => {
      const row = /^(.*?)[0-9]+$/.exec(seat.codigo)?.[1] || seat.zona;
      if (!rows.has(row)) rows.set(row, []);
      rows.get(row).push(seat);
    });
  }
  const names = {disponible: "Disponible", reservado: "Reservado", ocupado: "Ocupado", no_disponible: "No disponible"};
  target.innerHTML = `<div class="venue-stage">ESCENARIO / PANTALLA</div><div class="venue-legend">${zones.map((zone, i) => `<span><i style="background:${zoneColor(zone, i)}"></i>${escapeHTML(zone)}</span>`).join("")}</div><div class="seat-state-legend"><span>□ Disponible</span><span>✓ Seleccionado</span><span>▧ Reservado / ocupado</span><span>× No disponible</span></div><div class="venue-scroll" tabindex="0" role="region" aria-label="Plano de asientos, desplazamiento horizontal"><div class="venue-rows">${[...rows].map(([row, seats]) => `<div class="venue-row"><span class="venue-row-label">${escapeHTML(row)}</span>${seats.map((seat, index) => {
    const state = seat.disponible ? "disponible" : names[seat.estado] ? seat.estado : "reservado";
    const selected = SELECTED_SEATS.has(seat.codigo);
    return `<button type="button" class="venue-seat seat-${state}" data-seat="${escapeHTML(seat.codigo)}" style="--zone-color:${zoneColor(seat.zona, zones.indexOf(seat.zona))}" aria-pressed="${selected}" aria-label="${escapeHTML(`${seat.codigo}, ${seat.zona}, ${money(seat.precio_centimos || 0)}, ${names[state]}`)}" title="${escapeHTML(`${seat.zona} · ${money(seat.precio_centimos || 0)} · ${names[state]}`)}" ${seat.disponible && CHECKOUT_ORDERS ? "" : "disabled"}>${escapeHTML(seat.codigo)}<span aria-hidden="true">${selected ? "✓" : state === "disponible" ? "" : state === "no_disponible" ? "×" : "▧"}</span></button>${layout.pasillos?.includes(index + 1) ? '<span class="venue-aisle" aria-hidden="true"></span>' : ""}`;
  }).join("")}</div>`).join("")}</div></div><p class="field-help">Pulsa una butaca para elegirla. Desliza el plano si no cabe en pantalla.</p>`;
  target.querySelectorAll("[data-seat]").forEach(button => button.addEventListener("click", () => {
    const code = button.dataset.seat;
    if (CHECKOUT_BUSY || !SEATS_READY || !SEATS.find(seat => seat.codigo === code)?.disponible) return;
    if (SELECTED_SEATS.has(code)) SELECTED_SEATS.delete(code);
    else if (SELECTED_SEATS.size < 100) SELECTED_SEATS.add(code);
    else { showNotice(document.getElementById("purchase-result"), "Máximo 100 asientos por selección.", "warning"); return; }
    button.setAttribute("aria-pressed", String(SELECTED_SEATS.has(code)));
    button.querySelector("span").textContent = SELECTED_SEATS.has(code) ? "✓" : "";
    document.getElementById("purchase-quantity").value = String(SELECTED_SEATS.size);
    clearPurchaseStatus(); updatePurchaseSummary();
  }));
}

function renderZones(target) {
  target.innerHTML = `<div class="venue-stage">ESCENARIO</div><div class="zone-cards">${ZONES.map((zone, index) => `<article class="zone-card" style="--zone-color:${/^#[0-9a-f]{6}$/i.test(zone.color) ? zone.color : zoneColor(zone.nombre, index)}"><button type="button" data-zone="${index}" aria-pressed="${SELECTED_ZONE === zone.nombre}" ${zone.disponibles && CHECKOUT_ORDERS ? "" : "disabled"}><strong>${escapeHTML(zone.nombre)}</strong><span>${money(zone.precio_centimos)} por entrada</span><small>${zone.disponibles ? `${zone.disponibles} disponibles` : "Agotada"}</small></button><label>Cantidad en ${escapeHTML(zone.nombre)}<input type="number" min="1" max="${Math.min(100, zone.disponibles)}" value="${SELECTED_ZONE === zone.nombre ? selectedItem().cantidad : 1}" data-zone-quantity="${index}" ${zone.disponibles && CHECKOUT_ORDERS ? "" : "disabled"}></label><output data-zone-subtotal="${index}"></output></article>`).join("")}</div><p class="field-help">Elige un sector y su cantidad. Estas entradas no tienen butaca numerada.</p>`;
  target.querySelectorAll("[data-zone]").forEach(button => button.addEventListener("click", () => {
    if (CHECKOUT_BUSY || !SEATS_READY) return;
    const index = Number(button.dataset.zone); SELECTED_ZONE = ZONES[index].nombre;
    document.getElementById("purchase-quantity").value = target.querySelector(`[data-zone-quantity="${index}"]`).value;
    clearPurchaseStatus(); updatePurchaseSummary();
  }));
  target.querySelectorAll("[data-zone-quantity]").forEach(input => input.addEventListener("input", () => {
    if (CHECKOUT_BUSY || !SEATS_READY) return;
    SELECTED_ZONE = ZONES[Number(input.dataset.zoneQuantity)].nombre;
    document.getElementById("purchase-quantity").value = input.value;
    clearPurchaseStatus(); updatePurchaseSummary();
  }));
}

async function loadSeats() {
  const request = ++SEATS_REQUEST;
  const id = Number(document.getElementById("purchase-event").value);
  if (INVENTORY_EVENT !== id) { SELECTED_SEATS.clear(); SELECTED_ZONE = ""; INVENTORY_EVENT = id; }
  const event = SELECTED_EVENT;
  SEATS_READY = false; SEATS = []; ZONES = [];
  const target = document.getElementById("seat-options");
  target.textContent = "Consultando tipos de entrada…";
  updatePurchaseSummary();
  if (!id || Number(event?.id) !== id) return;
  const zoned = SELECTED_EVENT?.distribucion?.tipo === "zonas";
  const response = await fetchJSON(`${CONFIG.EVENTOS_URL}/eventos/${id}/${zoned ? "zonas" : "asientos"}`);
  if (request !== SEATS_REQUEST || id !== Number(document.getElementById("purchase-event").value) || Number(SELECTED_EVENT?.id) !== id) return;
  if (!response.ok || !Array.isArray(response.data)) {
    target.textContent = "No se pudo consultar el inventario. Actualiza el evento antes de comprar.";
    updatePurchaseSummary(); return;
  }
  applyAvailability({id, event, inventory: response.data, zoned});
}

function applyAvailability({id, event, inventory, zoned}, announce = true) {
  if (id !== Number(document.getElementById("purchase-event").value)) return;
  const previous = [...SELECTED_SEATS];
  const target = document.getElementById("seat-options");
  SELECTED_EVENT = event;
  SEATS = zoned ? [] : inventory; ZONES = zoned ? inventory : [];
  SEATS_READY = true;
  SELECTED_SEATS = new Set(previous.filter(code => SEATS.find(seat => seat.codigo === code)?.disponible));
  const lost = previous.filter(code => !SELECTED_SEATS.has(code));
  if (!zoned) SELECTED_ZONE = "";
  if (zoned && SELECTED_ZONE && !ZONES.some(zone => zone.nombre === SELECTED_ZONE && zone.disponibles > 0)) {
    SELECTED_ZONE = "";
    if (announce) showNotice(document.getElementById("purchase-result"), "La zona seleccionada se agotó. Elige otra zona.", "warning");
  } else if (zoned && SELECTED_ZONE && selectedItem().cantidad > ZONES.find(zone => zone.nombre === SELECTED_ZONE).disponibles && announce) {
    showNotice(document.getElementById("purchase-result"), `La zona ${SELECTED_ZONE} ya no tiene suficientes entradas. Ajusta la cantidad.`, "warning");
  }
  if (admissionType() === "asientos") { document.getElementById("purchase-quantity").value = String(SELECTED_SEATS.size); renderSeatMap(target); }
  else if (zoned) renderZones(target);
  else target.innerHTML = `<p>Admisión general · ${money(SELECTED_EVENT?.precio_centimos)} por entrada.</p>`;
  if (lost.length && announce) showNotice(document.getElementById("purchase-result"), `Estos asientos dejaron de estar disponibles: ${lost.join(", ")}. Selecciona otros.`, "warning");
  updatePurchaseSummary();
}

function intentFingerprint(payload, orders = CHECKOUT_ORDERS) { return `nexustix-intent:${orders ? "order" : "legacy"}:${CURRENT_USER?.id}:${JSON.stringify(payload)}`; }
function readIntent(payload, create = false, orders = CHECKOUT_ORDERS) {
  const fingerprint = intentFingerprint(payload, orders);
  if (!INTENT_KEYS.has(fingerprint)) {
    try { const stored = JSON.parse(sessionStorage.getItem(fingerprint)); if (stored?.key) INTENT_KEYS.set(fingerprint, stored); } catch (_) { /* Storage may be unavailable. */ }
  }
  if (!INTENT_KEYS.has(fingerprint) && create) {
    const key = crypto.randomUUID?.() || [...crypto.getRandomValues(new Uint8Array(16))].map(value => value.toString(16).padStart(2, "0")).join("");
    INTENT_KEYS.set(fingerprint, {key, completed: false});
    saveIntent(fingerprint, INTENT_KEYS.get(fingerprint));
  }
  return {fingerprint, record: INTENT_KEYS.get(fingerprint)};
}
function saveIntent(fingerprint, record) {
  INTENT_KEYS.set(fingerprint, record);
  try { sessionStorage.setItem(fingerprint, JSON.stringify(record)); } catch (_) { /* In-memory key still protects this page. */ }
}
function rememberPending(review, fingerprint, record, orderId) {
  PENDING_REVIEW = {...review, orderId: orderId || review.orderId || null};
  saveIntent(fingerprint, {...record, completed: false, pending: true, payload: review.payload,
    orders: review.orders, orderId: PENDING_REVIEW.orderId});
}
function restorePendingPurchase() {
  if (!CURRENT_USER || PENDING_REVIEW) return;
  const prefixes = ["order", "legacy"].map(mode => `nexustix-intent:${mode}:${CURRENT_USER.id}:`);
  try {
    for (let index = 0; index < sessionStorage.length; index++) {
      const key = sessionStorage.key(index);
      if (!prefixes.some(prefix => key?.startsWith(prefix))) continue;
      try { INTENT_KEYS.set(key, JSON.parse(sessionStorage.getItem(key))); } catch (_) { /* Ignore an incomplete stored intent. */ }
    }
  } catch (_) { /* The current page can still keep its intent in memory. */ }
  for (const [fingerprint, record] of INTENT_KEYS) {
    if (prefixes.some(prefix => fingerprint.startsWith(prefix)) && record?.pending && record.key && record.payload && typeof record.orders === "boolean") {
      PENDING_REVIEW = {payload: record.payload, orders: record.orders, orderId: record.orderId,
        revision: SESSION_REVISION};
      break;
    }
  }
}
function orderFeedback(order) {
  const states = {
    reservando: ["Tu pedido está siendo procesado; la reserva aún no está confirmada.", true, "warning"],
    reservada: [`Reserva confirmada · pedido pendiente de pago. Total confirmado: ${estimatedMoney(order.total_centimos)}. Reserva hasta ${eventDate(order.expira_en)}.`, false, "success"],
    pagada: ["Pago simulado aprobado. La confirmación del pedido sigue en proceso.", true, "warning"],
    emitiendo: ["Pago simulado aprobado. El servidor está emitiendo las entradas.", true, "warning"],
    completada: ["Pedido completado. Tus entradas están disponibles en Mis entradas.", false, "success"],
    cancelando: [order.motivo === "pago_rechazado" ? "El pago simulado fue rechazado. Se está liberando la reserva y no se emitieron entradas." : "Cancelación en proceso; la recuperación del inventario sigue pendiente.", true, "warning"],
    cancelada: [order.motivo === "reserva_expirada" ? "Reserva vencida y cancelada por el servidor." : order.motivo === "pago_rechazado" ? "El pago simulado fue rechazado. El pedido se canceló y no se emitieron entradas." : order.motivo === "evento_cancelado" ? "El evento fue cancelado y este pedido quedó cancelado." : "Pedido cancelado por el servidor.", false, "warning"],
    reembolsando: ["El reembolso y la compensación siguen en proceso; hay una recuperación pendiente en el servidor.", true, "warning"],
    reembolsada: ["Pedido reembolsado por el servidor.", false, "success"],
  };
  const [label, pending, tone] = states[order.estado] || [`Estado comunicado por el servidor: ${order.estado || "no disponible"}. Consulta el pedido.`, true, "warning"];
  const conflict = order.conflicto_disponibilidad;
  const conflictText = conflict ? `Conflicto de disponibilidad: ${typeof conflict.mensaje === "string" ? conflict.mensaje : "La disponibilidad cambió. Revisa tu selección."} ` : order.motivo === "inventario_no_disponible" ? "La disponibilidad cambió. Revisa tu selección. " : "";
  return {text: conflictText + label, pending, tone: conflictText ? "warning" : tone,
    outcome: pending ? "pending" : order.estado === "reservada" ? "reserved" : order.estado === "cancelada" ? "rejected" : "confirmed"};
}
const ORDER_STATE_LABELS = {reservando: "Reservando", reservada: "Pendiente de pago", pagada: "Pago aprobado", emitiendo: "Emitiendo entradas", completada: "Completado", cancelando: "Cancelando", cancelada: "Cancelado", reembolsando: "Reembolsando", reembolsada: "Reembolsado"};
const PAYMENT_LABELS = {pendiente: "Pendiente", aprobado: "Aprobado", rechazado: "Rechazado", reembolsado: "Reembolsado", reembolsado_parcial: "Reembolso parcial"};
function orderProgress(order) {
  const paid = ["aprobado", "reembolsado", "reembolsado_parcial"].includes(order.pago);
  const rejected = order.motivo === "pago_rechazado";
  const stops = {cancelando: rejected ? "Pago rechazado · cancelación en proceso" : "Cancelación en proceso", cancelada: rejected ? "Pago rechazado" : order.motivo === "reserva_expirada" ? "Reserva vencida" : "Cancelado", reembolsando: "Reembolso en proceso", reembolsada: "Reembolsado"};
  const reached = {reservando: 1, reservada: 2, pagada: 3, emitiendo: 3, completada: 4}[order.estado];
  if (!reached && !stops[order.estado]) return "";
  const icons = {done: "✓", current: "…", todo: "○", stop: "!"};
  const words = {done: "completado", current: "en curso", todo: "pendiente", stop: "pedido interrumpido o en compensación"};
  const step = (label, kind) => `<li class="step-${kind}"${kind === "current" ? ' aria-current="step"' : ""}><span class="step-icon" aria-hidden="true">${icons[kind]}</span>${escapeHTML(label)}<span class="ticket-sr"> (${words[kind]})</span></li>`;
  const labels = ["Pedido creado", "Reserva", "Pago aprobado", "Entrada emitida"];
  // En cancelaciones solo se afirma lo que los datos reales permiten comprobar.
  const steps = reached ? labels.map((label, index) => step(label, index < reached ? "done" : index === reached ? "current" : "todo"))
    : [step(labels[0], "done"), ...(paid ? [step(labels[2], "done")] : []), step(stops[order.estado], "stop")];
  return `<ol class="order-progress" aria-label="Progreso del pedido">${steps.join("")}</ol>`;
}

const TICKET_STATES = {
  emitida: {label: "Emitida", icon: "✓", tone: "ready", note: "Lista para ingresar. Presenta este código al personal del evento."},
  usada: {label: "Utilizada", icon: "●", tone: "used", note: "Esta entrada ya registró su ingreso y no admite otro acceso."},
  anulada: {label: "Anulada", icon: "✕", tone: "void", note: "Esta entrada fue anulada y no permite ingresar."},
};
const TICKET_UNAVAILABLE = {label: "No disponible para acceso", icon: "!", tone: "void", note: "Esta entrada no está habilitada para un nuevo acceso."};
function ticketCard(ticket, presenting = false) {
  const state = Object.hasOwn(TICKET_STATES, ticket.estado) ? TICKET_STATES[ticket.estado] : TICKET_UNAVAILABLE;
  const event = EVENTS.find(row => Number(row.id) === Number(ticket.evento_id));
  const name = eventName(ticket.evento_id);
  const fact = (label, value) => value ? `<div><dt>${label}</dt><dd>${escapeHTML(value)}</dd></div>` : "";
  const card = document.createElement("article");
  card.className = "ticket"; card.dataset.tone = state.tone; card.setAttribute("aria-label", `Entrada para ${name}`);
  card.innerHTML = `<header class="ticket-top"><span class="ticket-brand">NEXUSTIX</span><span class="ticket-state"><span aria-hidden="true">${state.icon}</span>${state.label}</span></header><div class="ticket-body"><h3>${escapeHTML(name)}</h3><dl class="ticket-facts">${fact("Fecha y hora", event ? eventDate(event.fecha) : "No disponible")}${fact("Lugar", event?.lugar)}${fact("Tipo", ticket.zona ? "Zona" : ticket.asiento ? "Asiento numerado" : "Admisión general")}${fact("Zona", ticket.zona)}${fact("Asiento", ticket.asiento)}${fact("Precio", ticket.precio_centimos !== undefined ? money(ticket.precio_centimos) : "")}</dl></div><div class="ticket-tear" aria-hidden="true"></div>`;
  const access = document.createElement("div"); access.className = "ticket-access";
  // SVG is displayed as an image, never inserted as executable markup.
  if (ticket.estado === "emitida" && ticket.qr_svg) {
    const img = document.createElement("img"); img.className = "ticket-qr"; img.alt = `Código QR de acceso para ${name}`; img.width = 220; img.height = 220;
    img.src = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(ticket.qr_svg)}`; access.appendChild(img);
  }
  const note = document.createElement("p"); note.className = "ticket-note";
  note.textContent = ticket.estado === "usada" && ticket.usado_en ? `${state.note} Ingreso: ${new Date(ticket.usado_en).toLocaleString("es-PE")}.` : state.note;
  access.appendChild(note);
  if (ticket.estado === "emitida" && ticket.qr_svg) {
    if (ticket.qr_url && /^https?:/.test(ticket.qr_url) && !presenting) {
      const link = document.createElement("a"); link.className = "text-button ticket-link"; link.href = ticket.qr_url; link.rel = "noreferrer"; link.textContent = "Abrir presentación de la entrada"; access.appendChild(link);
    } else if (!ticket.qr_url) {
      const local = document.createElement("p"); local.className = "ticket-note"; local.textContent = "Este QR se valida en el ingreso. Para abrir la entrada desde otro dispositivo, el despliegue necesita una dirección accesible configurada."; access.appendChild(local);
    }
    const details = document.createElement("details"); const title = document.createElement("summary"); title.textContent = "Mostrar código de acceso";
    const code = document.createElement("code"); code.textContent = ticket.qr; details.append(title, code); access.appendChild(details);
  }
  card.appendChild(access);
  const foot = document.createElement("footer"); foot.className = "ticket-foot";
  foot.innerHTML = `<span>${ticket.referencia ? `Ref. ${escapeHTML(ticket.referencia)}` : ""}</span><span>Validación definitiva en el ingreso</span>`;
  card.appendChild(foot);
  return card;
}

let TICKET_PRESENTATION = null;
let TICKET_PRESENTATION_REQUEST = 0;
// El código nunca sale del navegador: se compara con las entradas de la propia cuenta.
async function showTicketPresentation(code = TICKET_PRESENTATION) {
  TICKET_PRESENTATION = code;
  const target = document.getElementById("ticket-presentation");
  const notice = (text, kind) => { target.innerHTML = `<p class="notice notice-${kind}">${escapeHTML(text)}</p>`; };
  const request = ++TICKET_PRESENTATION_REQUEST, revision = SESSION_REVISION;
  if (!/^[A-Za-z0-9_-]{20,80}$/.test(code || "")) { notice("Este enlace no corresponde a una entrada válida.", "error"); return; }
  if (!CURRENT_USER) {
    notice("Inicia sesión con la cuenta que compró esta entrada para ver sus datos y el código QR.", "warning");
    const button = document.createElement("button"); button.className = "button button-accent"; button.type = "button"; button.textContent = "Ingresar"; button.addEventListener("click", () => openAuthDialog()); target.appendChild(button);
    return;
  }
  notice("Consultando tu entrada…", "warning");
  const result = await fetchJSON(`${CONFIG.COMPRAS_LB_URL}/entradas/mias`, {}, 15000);
  if (request !== TICKET_PRESENTATION_REQUEST || revision !== SESSION_REVISION) return;
  if (!result.ok || !Array.isArray(result.data)) { notice("El servicio no pudo confirmar esta entrada. Recarga la página para reintentar.", "warning"); return; }
  const ticket = result.data.find(row => row.qr === code);
  if (ticket) target.replaceChildren(ticketCard(ticket, true));
  else notice("Esta cuenta no tiene una entrada asociada a este enlace. Ingresa con la cuenta que la compró.", "warning");
}

function purchasePayload() {
  const items = checkoutItems();
  return CHECKOUT_ORDERS ? {items} : {evento_id: items[0]?.evento_id, cantidad: items[0]?.cantidad};
}

function updatePurchaseSummary() {
  const summary = document.getElementById("purchase-summary"); if (!summary) return;
  document.getElementById("purchase-quantity").max = CHECKOUT_ORDERS ? "100" : "1000";
  const group = groupMode();
  document.getElementById("group-options").hidden = !group;
  const items = group ? ORDER_CART : [selectedItem()];
  const error = serviceBlockMessage() || (group ? (items.length ? items.some(item => { const event = EVENTS.find(e => Number(e.id) === item.evento_id); return event && eventState(event) !== "disponible"; }) ? "Hay un evento del grupo que ya no está disponible. Retíralo para continuar." : "" : "Añade una selección al pedido grupal.") : selectionError());
  const quantity = items.reduce((sum, item) => sum + (Number.isInteger(item.cantidad) ? item.cantidad : 0), 0);
  const total = items.every(item => Number.isInteger(itemTotal(item))) ? items.reduce((sum, item) => sum + itemTotal(item), 0) : null;
  summary.setAttribute("aria-busy", String(CHECKOUT_BUSY));
  document.getElementById("purchase-form").setAttribute("aria-busy", String(CHECKOUT_BUSY));
  summary.innerHTML = `${items.map(item => selectionSummary(item)).join("")}<div class="checkout-estimate-total"><span>Total estimado · ${quantity} entrada(s)</span><strong>${estimatedMoney(total)}</strong></div><small>El servidor confirma disponibilidad y precio al reservar.</small>${CHECKOUT_BUSY ? '<p class="checkout-pending" role="status">Tu solicitud está siendo procesada…</p>' : PENDING_REVIEW ? '<p class="selection-warning">No pudimos confirmar el resultado. Consulta el pedido pendiente antes de enviar otra solicitud.</p>' : ""}${error ? `<p class="selection-warning">${escapeHTML(error)}</p>` : ""}`;
  const complete = Boolean(CURRENT_USER && readIntent(purchasePayload()).record?.completed);
  document.getElementById("purchase-submit").disabled = CHECKOUT_BUSY || Boolean(PENDING_REVIEW) || !CHECKOUT_READY || Boolean(error) || complete || quantity > (CHECKOUT_ORDERS ? 200 : 1000);
  document.getElementById("purchase-submit").textContent = CHECKOUT_BUSY ? "Procesando…" : complete ? "Solicitud ya completada" : "Revisar compra →";
  document.getElementById("purchase-new").hidden = !complete || Boolean(PENDING_REVIEW);
  document.getElementById("purchase-new").disabled = CHECKOUT_BUSY;
  document.getElementById("add-cart").disabled = CHECKOUT_BUSY || Boolean(selectionError());
  document.getElementById("purchase-quantity").disabled = CHECKOUT_BUSY;
  document.getElementById("purchase-quantity").readOnly = admissionType() === "asientos";
  document.querySelector('label[for="purchase-quantity"]').hidden = admissionType() === "zonas";
  document.querySelector(".quantity-wrap").hidden = admissionType() === "zonas";
  document.getElementById("purchase-mode").disabled = CHECKOUT_BUSY;
  document.getElementById("checkout-reconnect").disabled = CHECKOUT_BUSY;
  document.getElementById("refresh-availability").disabled = CHECKOUT_BUSY;
  document.getElementById("purchase-retry").hidden = !PENDING_REVIEW;
  document.getElementById("purchase-retry").disabled = CHECKOUT_BUSY;
  document.getElementById("purchase-retry").textContent = PENDING_REVIEW?.orderId ? "Consultar pedido pendiente" : "Consultar o reintentar la misma solicitud";
  document.querySelectorAll("[data-remove-cart]").forEach(button => { button.disabled = CHECKOUT_BUSY; });
  document.querySelectorAll("[data-order]").forEach(button => { button.disabled = CHECKOUT_BUSY || ORDER_ACTIONS.has(button.dataset.order); });
  document.querySelectorAll("#seat-options [data-seat]").forEach(button => {
    button.disabled = CHECKOUT_BUSY || !SEATS_READY || !CHECKOUT_ORDERS || !SEATS.find(seat => seat.codigo === button.dataset.seat)?.disponible;
  });
  document.querySelectorAll("#seat-options [data-zone]").forEach(button => {
    const index = Number(button.dataset.zone), zone = ZONES[index];
    const selected = SELECTED_ZONE === zone.nombre;
    button.disabled = CHECKOUT_BUSY || !SEATS_READY || !CHECKOUT_ORDERS || zone.disponibles < 1;
    button.setAttribute("aria-pressed", String(selected));
    button.closest(".zone-card").classList.toggle("is-selected", selected);
    const input = document.querySelector(`[data-zone-quantity="${index}"]`);
    input.disabled = button.disabled || !selected;
    document.querySelector(`[data-zone-subtotal="${index}"]`).textContent = selected ? `Subtotal: ${money(zone.precio_centimos * (Number(input.value) || 0))}` : "";
  });
}

function renderCart() {
  document.getElementById("cart-summary").innerHTML = ORDER_CART.length ? ORDER_CART.map((item, index) => `<div class="cart-line"><span>${escapeHTML(item.quote?.name || eventName(item.evento_id))}<small>${escapeHTML(itemLocation(item))}</small><small>${item.cantidad} entrada(s) · ${estimatedMoney(itemTotal(item))}</small></span><button class="text-button" type="button" data-remove-cart="${index}" aria-label="Retirar selección">Quitar</button></div>`).join("") : '<p class="field-help">El grupo está vacío. Puedes añadir este evento y continuar explorando.</p>';
  document.querySelectorAll("[data-remove-cart]").forEach(button => button.onclick = () => { if (!CHECKOUT_BUSY) { ORDER_CART.splice(Number(button.dataset.removeCart), 1); renderCart(); updatePurchaseSummary(); } });
}

function addToCart() {
  const item = selectedItem();
  const error = selectionError(item);
  if (error) { showNotice(document.getElementById("purchase-result"), error, "warning"); return; }
  if (ORDER_CART.length >= 20 || ORDER_CART.reduce((sum, row) => sum + row.cantidad, 0) + item.cantidad > 200) { showNotice(document.getElementById("purchase-result"), "El grupo admite hasta 20 selecciones y 200 entradas.", "warning"); return; }
  if (ORDER_CART.some(row => row.evento_id === item.evento_id && row.asientos.some(seat => item.asientos.includes(seat)))) { showNotice(document.getElementById("purchase-result"), "El grupo ya contiene uno de esos asientos.", "warning"); return; }
  const sameEvent = ORDER_CART.filter(row => row.evento_id === item.evento_id);
  const zone = item.zona && ZONES.find(row => row.nombre === item.zona);
  if (sameEvent.reduce((sum, row) => sum + row.cantidad, item.cantidad) > SELECTED_EVENT.stock ||
      (zone && sameEvent.filter(row => row.zona === item.zona).reduce((sum, row) => sum + row.cantidad, item.cantidad) > zone.disponibles)) {
    showNotice(document.getElementById("purchase-result"), "La cantidad acumulada del grupo supera la disponibilidad del evento o zona.", "warning"); return;
  }
  ORDER_CART.push({...item, quote: selectionQuote(item)}); renderCart(); updatePurchaseSummary();
}

function submitPurchase(event) {
  event.preventDefault();
  if (CHECKOUT_BUSY || document.getElementById("purchase-submit").disabled) return;
  if (!CURRENT_USER) { openAuthDialog("login", true, "Inicia sesión para confirmar la selección que acabas de preparar."); return; }
  const payload = purchasePayload();
  const {record} = readIntent(payload);
  if (record?.completed) return;
  const items = groupMode() ? ORDER_CART : checkoutItems();
  REVIEW = {payload: JSON.parse(JSON.stringify(payload)), orders: CHECKOUT_ORDERS, revision: SESSION_REVISION,
    quotes: items.map(item => item.quote || selectionQuote(item))};
  renderPurchaseReview(REVIEW);
  document.getElementById("review-confirm").disabled = false;
  setCheckoutStep(2);
  document.getElementById("purchase-review").showModal();
}

function renderPurchaseReview(review, notice = "") {
  const items = review.orders ? review.payload.items : [{...review.payload, asientos: []}];
  const quotes = review.quotes || items.map(item => selectionQuote(item));
  const total = quotes.every(quote => Number.isInteger(quote.total)) ? quotes.reduce((sum, quote) => sum + quote.total, 0) : null;
  document.getElementById("review-content").innerHTML = `${notice ? `<p class="notice notice-warning" role="status">${escapeHTML(notice)}</p>` : ""}${items.map((item, index) => selectionSummary(item, quotes[index])).join("")}<div class="checkout-estimate-total"><span>Total estimado</span><strong>${estimatedMoney(total)}</strong></div><p>${review.orders ? "El total definitivo se mostrará en el pedido reservado antes del pago simulado." : "El servidor validará la compra al confirmar."}</p><p class="field-help">La consulta de disponibilidad no garantiza la reserva. Puedes volver a editar tu selección.</p>`;
}

async function availabilityError(items) {
  const ids = [...new Set(items.map(item => item.evento_id))];
  const results = await Promise.all(ids.map(async id => {
    const event = await fetchJSON(`${CONFIG.EVENTOS_URL}/eventos/${id}`);
    if (!event.ok || Number(event.data?.id) !== id) return {id, error: `No se pudo consultar el evento #${id}. No se ha enviado la reserva.`};
    const zoned = event.data.distribucion?.tipo === "zonas";
    const inventory = await fetchJSON(`${CONFIG.EVENTOS_URL}/eventos/${id}/${zoned ? "zonas" : "asientos"}`);
    if (!inventory.ok || !Array.isArray(inventory.data)) return {id, error: `No se pudo actualizar el inventario del evento #${id}.`};
    return {id, event: event.data, inventory: inventory.data, zoned};
  }));
  const snapshots = results.filter(result => !result.error);
  const finish = error => ({error, snapshots, quotes: items.map(item => {
    const snapshot = snapshots.find(result => result.id === item.evento_id);
    return snapshot ? selectionQuote(item, snapshot) : null;
  })});
  for (const result of results) {
    if (result.error) return finish(result.error);
    if (eventState(result.event) !== "disponible") return finish(`El evento ${result.event.nombre} ya no admite nuevas reservas. Revisa tu selección.`);
    const selected = items.filter(item => item.evento_id === result.id);
    if (selected.reduce((sum, item) => sum + item.cantidad, 0) > result.event.stock) return finish(`El evento ${result.event.nombre} ya no tiene suficientes entradas. Ajusta la selección.`);
    const used = new Set(), quantities = new Map();
    for (const item of selected) {
      if (result.zoned) {
        const zone = result.inventory.find(zone => zone.nombre === item.zona);
        const count = (quantities.get(item.zona) || 0) + item.cantidad;
        quantities.set(item.zona, count);
        if (!zone || count > zone.disponibles || item.asientos?.length) return finish(`La zona ${item.zona || "seleccionada"} ya no tiene suficientes entradas. Elige otra zona o cantidad.`);
      } else if (result.inventory.length || result.event.distribucion?.tipo === "asientos") {
        if (item.zona || item.asientos?.length !== item.cantidad) return finish("Selecciona un asiento por entrada.");
        for (const code of item.asientos) {
          if (used.has(code) || !result.inventory.find(seat => seat.codigo === code)?.disponible) return finish(`El asiento ${code} ya no está disponible o está repetido en el grupo. Selecciona otro.`);
          used.add(code);
        }
      } else if (item.zona || item.asientos?.length) return finish("El evento es de admisión general. Revisa la selección.");
    }
  }
  return finish("");
}

async function confirmPurchase() {
  const review = REVIEW;
  if (!review || CHECKOUT_BUSY || review.revision !== SESSION_REVISION || !CURRENT_USER) return;
  CHECKOUT_BUSY = true;
  document.getElementById("review-confirm").disabled = true;
  updatePurchaseSummary();
  // Un reintento conserva su clave: puede tener una reserva ya aceptada cuya
  // respuesta se perdió. No se sustituye por una operación nueva.
  const previousIntent = readIntent(review.payload, false, review.orders).record;
  if (!previousIntent?.pending && !previousIntent?.completed && !review.orderId) {
    const items = review.orders ? review.payload.items : [{...review.payload, asientos: []}];
    const availability = await availabilityError(items);
    if (review.revision !== SESSION_REVISION || REVIEW !== review) { CHECKOUT_BUSY = false; updatePurchaseSummary(); return; }
    const current = availability.snapshots.find(snapshot => snapshot.id === Number(document.getElementById("purchase-event").value));
    if (current) { ++SEATS_REQUEST; applyAvailability(current, false); renderDetail(); }
    else if (items.some(item => item.evento_id === Number(document.getElementById("purchase-event").value))) SEATS_READY = false;
    ORDER_CART = ORDER_CART.map(item => {
      const snapshot = availability.snapshots.find(result => result.id === item.evento_id);
      return snapshot ? {...item, quote: selectionQuote(item, snapshot)} : item;
    });
    renderCart();
    if (availability.error) {
      CHECKOUT_BUSY = false; document.getElementById("purchase-review").close();
      showNotice(document.getElementById("purchase-result"), availability.error, "warning"); updatePurchaseSummary(); return;
    }
    if (JSON.stringify(review.quotes) !== JSON.stringify(availability.quotes)) {
      review.quotes = availability.quotes; CHECKOUT_BUSY = false;
      renderPurchaseReview(review, "Cambió el precio estimado o la información del evento. Revisa el resumen actualizado antes de enviar.");
      document.getElementById("review-confirm").disabled = false; updatePurchaseSummary(); return;
    }
  }
  let intention;
  try { intention = readIntent(review.payload, true, review.orders); }
  catch (_) { CHECKOUT_BUSY = false; updatePurchaseSummary(); showNotice(document.getElementById("purchase-result"), "No se pudo preparar una clave segura de solicitud. No se envió la compra.", "error"); document.getElementById("purchase-review").close(); return; }
  CHECKOUT_BUSY = true;
  setCheckoutStep(3);
  document.getElementById("review-confirm").disabled = true;
  document.getElementById("purchase-review").close();
  REVIEW = null;
  const {fingerprint, record} = intention;
  updatePurchaseSummary();
  const target = document.getElementById("purchase-result");
  showNotice(target, "Procesando tu solicitud…");
  target.dataset.outcome = "pending";
  // Persistir antes de enviar conserva la solicitud si la página se recarga.
  rememberPending(review, fingerprint, record, review.orderId);
  const response = review.orders && review.orderId
    ? await fetchJSON(`${CONFIG.COMPRAS_LB_URL}/ordenes/${encodeURIComponent(review.orderId)}`, {}, PURCHASE_TIMEOUT_MS)
    : await fetchJSON(`${CONFIG.COMPRAS_LB_URL}/${review.orders ? "ordenes" : "compras"}`, {
    method: "POST", headers: {"Content-Type": "application/json", "Idempotency-Key": record.key}, body: JSON.stringify(review.payload),
  }, PURCHASE_TIMEOUT_MS);
  CHECKOUT_BUSY = false;
  if (review.revision !== SESSION_REVISION) { updatePurchaseSummary(); return; }
  const malformed = response.ok && (review.orders ? !response.data?.id || typeof response.data.estado !== "string" : !response.data?.id);
  if (!response.ok || malformed) {
    const unknown = malformed || Boolean(review.orderId) || Boolean(record.pending) || response.status === 0 || response.status === 408 || response.status >= 500 || response.data?.detail?.recuperacion_requerida === true;
    if (unknown) rememberPending(review, fingerprint, record, response.data?.detail?.orden_id);
    else { PENDING_REVIEW = null; saveIntent(fingerprint, {...record, pending: false}); }
    showNotice(target, unknown ? `No pudimos confirmar el resultado; esto no significa que la compra haya fallado. ${PENDING_REVIEW?.orderId ? `Referencia del pedido: ${PENDING_REVIEW.orderId.slice(0, 8)}. ` : ""}Revisa Mis entradas antes de intentar nuevamente: la consulta o reintento conserva los mismos datos y la misma clave${review.orders && review.payload.items?.length > 1 ? ", y el grupo sigue siendo un solo pedido" : ""}.` : response.status === 401 ? "Tu sesión expiró antes de enviar la solicitud. Inicia sesión y vuelve a revisarla; no es un error de compra." : response.status === 403 ? "Tu cuenta no tiene permiso para esta acción." : errorMessage(response.data, `La solicitud fue rechazada (HTTP ${response.status}).`), unknown ? "warning" : "error");
    target.dataset.outcome = unknown ? "unknown" : "rejected";
  } else {
    const result = response.data;
    if (review.orders) {
      const feedback = orderFeedback(result);
      if (feedback.pending) rememberPending(review, fingerprint, record, result.id);
      else { PENDING_REVIEW = null; saveIntent(fingerprint, {...record, completed: true, pending: false}); }
      showNotice(target, feedback.text, feedback.tone);
      target.dataset.outcome = feedback.outcome;
    } else {
      const pending = result.confirmacion_reserva === "pendiente" || result.recuperacion_requerida;
      if (pending) rememberPending(review, fingerprint, record);
      else { PENDING_REVIEW = null; saveIntent(fingerprint, {...record, completed: true, pending: false}); }
      showNotice(target, pending ? "Compra registrada; la confirmación del inventario sigue pendiente de recuperación." : `Compra #${result.id} confirmada · ${result.cantidad} entrada(s).`, pending ? "warning" : "success");
      target.dataset.outcome = pending ? "pending" : "confirmed";
    }
  }
  await Promise.all([refreshOrders(), loadMyPurchases(), loadEvents()]);
  if (SELECTED_EVENT) await loadSeats();
  updatePurchaseSummary();
}

async function orderAction(id, action, outcome) {
  if (CHECKOUT_BUSY || ORDER_ACTIONS.has(id) || !CURRENT_USER) return;
  if (!window.confirm(action === "pagar" ? `¿Confirmas el pago simulado ${outcome} de este pedido?` : "¿Confirmas cancelar o reembolsar este pedido? El backend verificará las entradas y pagos.")) return;
  const revision = SESSION_REVISION;
  ORDER_ACTIONS.add(id);
  document.querySelectorAll("[data-order]").forEach(button => { if (button.dataset.order === id) button.disabled = true; });
  const options = {method: "POST", headers: {"Content-Type": "application/json"}};
  if (outcome) options.body = JSON.stringify({resultado: outcome});
  const response = await fetchJSON(`${CONFIG.COMPRAS_LB_URL}/ordenes/${encodeURIComponent(id)}/${action}`, options, 30000);
  ORDER_ACTIONS.delete(id);
  if (revision !== SESSION_REVISION) return;
  const feedback = response.ok && response.data?.estado ? orderFeedback(response.data) : null;
  showNotice(document.getElementById("order-notice"), feedback ? feedback.text : errorMessage(response.data, "No pudimos confirmar el resultado. Consulta el pedido antes de intentar nuevamente."), feedback?.tone || "warning");
  await Promise.all([refreshOrders(), loadEvents(), loadMyPurchases()]);
}

async function refreshOrders() {
  const revision = SESSION_REVISION;
  const request = ++ORDER_LIST_REQUEST;
  if (!CURRENT_USER) {
    ["orders-list", "tickets-list", "notifications-list"].forEach(id => { document.getElementById(id).textContent = "Inicia sesión para consultar tus pedidos y entradas."; }); return;
  }
  if (!CHECKOUT_ORDERS) return;
  const [orders, tickets, messages] = await Promise.all(["ordenes/mias", "entradas/mias", "notificaciones"].map(path => fetchJSON(`${CONFIG.COMPRAS_LB_URL}/${path}`, {}, 15000)));
  if (revision !== SESSION_REVISION || request !== ORDER_LIST_REQUEST) return;
  if (PENDING_REVIEW?.orderId && orders.ok && Array.isArray(orders.data)) {
    const order = orders.data.find(row => row.id === PENDING_REVIEW.orderId);
    if (order) {
      const feedback = orderFeedback(order);
      if (!feedback.pending) {
        const intent = readIntent(PENDING_REVIEW.payload, false, PENDING_REVIEW.orders);
        if (intent.record) saveIntent(intent.fingerprint, {...intent.record, completed: true, pending: false});
        PENDING_REVIEW = null;
      }
      const target = document.getElementById("purchase-result");
      showNotice(target, feedback.text, feedback.tone); target.dataset.outcome = feedback.outcome;
      updatePurchaseSummary();
    }
  }
  document.getElementById("orders-list").innerHTML = orders.ok && Array.isArray(orders.data) ? orders.data.map(order => {
    const id = escapeHTML(order.id);
    const feedback = orderFeedback(order);
    return `<article class="workspace-card order-card"><div class="order-heading"><div><strong>Pedido ${id.slice(0, 8)}</strong>${order.creado_en ? `<small>Creado ${escapeHTML(eventDate(order.creado_en))}</small>` : ""}</div><span class="pill">${escapeHTML(ORDER_STATE_LABELS[order.estado] || order.estado)}</span></div>${orderProgress(order)}<p class="checkout-order-status notice notice-${feedback.tone}">${escapeHTML(feedback.text)}</p><p class="order-payment"><span>Pago: ${escapeHTML(PAYMENT_LABELS[order.pago] || order.pago)}</span><strong>${estimatedMoney(order.estado === "reservando" ? null : order.total_centimos)}</strong></p>${order.reembolsado_centimos ? `<p>Reembolso acumulado: ${money(order.reembolsado_centimos)}</p>` : ""}<ul>${(order.items || []).map(item => `<li>${escapeHTML(eventName(item.evento_id))} · ${item.cantidad} entrada(s) · ${escapeHTML(itemLocation(item))} · ${estimatedMoney(item.total_centimos)}${item.cancelado ? " · Cancelada / compensación solicitada" : ""}</li>`).join("")}</ul>${order.estado === "reservada" ? `<p>Reserva hasta ${escapeHTML(eventDate(order.expira_en))}</p><button class="button button-accent" data-order="${id}" data-action="pagar" data-outcome="aprobado">Pagar (simulado)</button><button class="text-button" data-order="${id}" data-action="pagar" data-outcome="rechazado">Simular rechazo del pago</button>` : ""}${["reservada", "reservando", "completada", "reembolsando", "cancelando"].includes(order.estado) ? `<button class="button button-outline" data-order="${id}" data-action="cancelar">${order.estado === "completada" ? "Solicitar reembolso" : ["reembolsando", "cancelando"].includes(order.estado) ? "Reintentar compensación" : "Cancelar pedido"}</button>` : ""}<details><summary>Referencia del pedido</summary><code>${id}</code>${order.motivo ? `<p>${escapeHTML(order.motivo)}</p>` : ""}</details></article>`;
  }).join("") || '<p class="empty-state">Aún no tienes pedidos. Explora el catálogo para elegir tu próxima experiencia.</p>' : '<p class="notice notice-error">No se pudieron consultar tus pedidos. Pulsa Actualizar para reintentar.</p>';
  document.querySelectorAll("[data-order]").forEach(button => { button.disabled = CHECKOUT_BUSY || ORDER_ACTIONS.has(button.dataset.order); button.onclick = () => orderAction(button.dataset.order, button.dataset.action, button.dataset.outcome); });
  const ticketTarget = document.getElementById("tickets-list");
  ticketTarget.replaceChildren();
  if (tickets.ok && Array.isArray(tickets.data)) {
    for (const ticket of tickets.data) ticketTarget.appendChild(ticketCard(ticket));
    if (!tickets.data.length) ticketTarget.innerHTML = '<p class="empty-state">Las entradas aparecerán aquí después de completar el pago.</p>';
  } else ticketTarget.innerHTML = '<p class="notice notice-warning">El servicio no pudo confirmar tus entradas. Pulsa Actualizar para reintentar.</p>';
  document.getElementById("notifications-list").textContent = messages.ok && Array.isArray(messages.data) ? messages.data.map(message => message.mensaje).join(" · ") || "Sin notificaciones." : "Notificaciones no disponibles.";
}

function checkoutIdentityChanged(previous, next) {
  REVIEW = null;
  PENDING_REVIEW = null;
  restorePendingPurchase();
  document.getElementById("purchase-review")?.close();
  if (previous && previous.id !== next?.id) ORDER_CART = [];
  ["orders-list", "tickets-list", "notifications-list", "order-notice"].forEach(id => { document.getElementById(id).textContent = ""; });
  renderCart(); updatePurchaseSummary(); refreshOrders();
  if (CURRENT_VIEW === "entrada") showTicketPresentation();
}

function initializeCheckout() {
  document.getElementById("purchase-form").addEventListener("submit", submitPurchase);
  document.getElementById("purchase-quantity").addEventListener("input", () => { clearPurchaseStatus(); updatePurchaseSummary(); });
  document.getElementById("purchase-mode").addEventListener("change", () => { clearPurchaseStatus(); updatePurchaseSummary(); });
  document.getElementById("add-cart").addEventListener("click", addToCart);
  document.getElementById("review-confirm").addEventListener("click", confirmPurchase);
  document.getElementById("purchase-retry").addEventListener("click", () => {
    if (!PENDING_REVIEW || CHECKOUT_BUSY || PENDING_REVIEW.revision !== SESSION_REVISION) return;
    REVIEW = PENDING_REVIEW;
    document.getElementById("review-content").textContent = PENDING_REVIEW.orderId ? `Consultaremos el estado del pedido ${PENDING_REVIEW.orderId}. Su selección enviada se conserva.` : "Se consultará o reintentará la solicitud ya enviada, conservando su selección y referencia original.";
    document.getElementById("review-confirm").disabled = false;
    document.getElementById("purchase-review").showModal();
  });
  document.getElementById("purchase-review").addEventListener("close", () => { REVIEW = null; if (!CHECKOUT_BUSY && !document.getElementById("purchase-result").dataset.outcome) setCheckoutStep(1); });
  document.getElementById("refresh-orders").addEventListener("click", async event => {
    const button = event.currentTarget;
    if (button.disabled) return;
    button.disabled = true;
    if (!CURRENT_USER) openAuthDialog();
    try { await Promise.all([refreshOrders(), loadMyPurchases()]); } finally { button.disabled = false; }
  });
  document.getElementById("checkout-reconnect").addEventListener("click", detectCheckout);
  document.getElementById("refresh-availability").addEventListener("click", () => {
    const id = Number(document.getElementById("purchase-event").value);
    if (!CHECKOUT_BUSY && id > 0) showEventDetail(id, false);
  });
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && !CHECKOUT_BUSY && !REVIEW && document.getElementById("view-evento").classList.contains("active") && SELECTED_EVENT) showEventDetail(Number(SELECTED_EVENT.id), false);
  });
  window.addEventListener("pageshow", event => {
    if (event.persisted && !CHECKOUT_BUSY && !REVIEW && CURRENT_VIEW === "evento" && SELECTED_EVENT) showEventDetail(Number(SELECTED_EVENT.id), false);
  });
  document.getElementById("purchase-new").addEventListener("click", () => {
    if (CHECKOUT_BUSY || PENDING_REVIEW) return;
    const {fingerprint} = readIntent(purchasePayload()); INTENT_KEYS.delete(fingerprint);
    try { sessionStorage.removeItem(fingerprint); } catch (_) { /* No persistent storage. */ }
    // La compra anterior ya está en Mis entradas: la nueva empieza sin su selección.
    ORDER_CART = []; SELECTED_SEATS.clear(); SELECTED_ZONE = "";
    document.getElementById("purchase-mode").value = "individual";
    document.getElementById("purchase-quantity").value = "1";
    clearPurchaseStatus(); renderCart(); updatePurchaseSummary();
    if (SELECTED_EVENT) showEventDetail(Number(SELECTED_EVENT.id), false);
  });
  restorePendingPurchase(); renderCart(); updatePurchaseSummary();
}
