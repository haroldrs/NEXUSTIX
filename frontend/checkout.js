"use strict";

let ORDER_CART = [];
let CHECKOUT_ORDERS = CONFIG.CHECKOUT_ORDERS === true;
let CHECKOUT_READY = false;
let CHECKOUT_BUSY = false;
let SEATS = [];
let SEATS_READY = false;
let SEATS_REQUEST = 0;
let ORDER_LIST_REQUEST = 0;
let REVIEW = null;
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
    asientos: [...document.querySelectorAll("#seat-options input:checked")].map(input => input.value).sort()};
}
function groupMode() { return CHECKOUT_ORDERS && document.getElementById("purchase-mode").value === "grupal"; }
function checkoutItems() { return groupMode() ? ORDER_CART.map(({evento_id, cantidad, asientos}) => ({evento_id, cantidad, asientos})) : [selectedItem()]; }
function itemTotal(item) {
  if (item.estimate !== undefined) return item.estimate;
  if (item.asientos.length) return item.asientos.reduce((sum, code) => sum + (SEATS.find(seat => seat.codigo === code)?.precio_centimos || 0), 0);
  return Number(SELECTED_EVENT?.precio_centimos || 0) * item.cantidad;
}
function selectionError(item = selectedItem()) {
  if (!SELECTED_EVENT || !CHECKOUT_READY || !SEATS_READY) return "Espera a que se confirme la disponibilidad.";
  if (eventState(SELECTED_EVENT) !== "disponible") return "Este evento no admite nuevas compras.";
  if (!Number.isInteger(item.cantidad) || item.cantidad < 1 || item.cantidad > (CHECKOUT_ORDERS ? 100 : 1000)) return "Indica una cantidad válida de entradas.";
  if (item.cantidad > Number(SELECTED_EVENT.stock)) return "La cantidad supera la disponibilidad consultada.";
  if (SEATS.length && !CHECKOUT_ORDERS) return "Este evento numerado requiere el checkout de pedidos.";
  if (SEATS.length && item.asientos.length !== item.cantidad) return "Selecciona un asiento disponible por entrada.";
  return "";
}

async function loadSeats() {
  const request = ++SEATS_REQUEST;
  const id = Number(document.getElementById("purchase-event").value);
  const previous = selectedItem().asientos;
  SEATS_READY = false; SEATS = [];
  const target = document.getElementById("seat-options");
  target.textContent = "Consultando tipos de entrada…";
  updatePurchaseSummary();
  if (!id) return;
  const response = await fetchJSON(`${CONFIG.EVENTOS_URL}/eventos/${id}/asientos`);
  if (request !== SEATS_REQUEST || id !== Number(document.getElementById("purchase-event").value)) return;
  if (!response.ok || !Array.isArray(response.data)) {
    target.textContent = "No se pudo consultar el inventario. Actualiza el evento antes de comprar.";
    updatePurchaseSummary(); return;
  }
  SEATS = response.data; SEATS_READY = true;
  if (!SEATS.length) target.innerHTML = `<p>Admisión general · ${money(SELECTED_EVENT?.precio_centimos)} por entrada.</p>`;
  else {
    const zones = [...new Set(SEATS.map(seat => seat.zona))];
    target.innerHTML = zones.map(zone => `<fieldset class="seat-zone"><legend>${escapeHTML(zone)}</legend><div class="seat-grid">${SEATS.filter(seat => seat.zona === zone).map(seat => `<label class="seat-option ${seat.disponible ? "" : "seat-taken"}"><input type="checkbox" value="${escapeHTML(seat.codigo)}" ${seat.disponible && CHECKOUT_ORDERS ? "" : "disabled"} ${seat.disponible && previous.includes(seat.codigo) ? "checked" : ""}><span>${escapeHTML(seat.codigo)}<small>${money(seat.precio_centimos)}</small></span></label>`).join("")}</div></fieldset>`).join("");
    target.querySelectorAll("input").forEach(input => input.addEventListener("change", () => { clearPurchaseStatus(); updatePurchaseSummary(); }));
  }
  updatePurchaseSummary();
}

function intentFingerprint(payload) { return `nexustix-intent:${CHECKOUT_ORDERS ? "order" : "legacy"}:${CURRENT_USER?.id}:${JSON.stringify(payload)}`; }
function readIntent(payload, create = false) {
  const fingerprint = intentFingerprint(payload);
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
  const error = group ? (items.length ? items.some(item => { const event = EVENTS.find(e => Number(e.id) === item.evento_id); return event && eventState(event) !== "disponible"; }) ? "Hay un evento del grupo que ya no está disponible. Retíralo para continuar." : "" : "Añade una selección al pedido grupal.") : selectionError();
  const quantity = items.reduce((sum, item) => sum + (Number.isInteger(item.cantidad) ? item.cantidad : 0), 0);
  const total = items.reduce((sum, item) => sum + itemTotal(item), 0);
  summary.innerHTML = `<div><span>${quantity} entrada(s)${group ? ` · ${items.length} selección(es)` : ""}</span><strong>${money(total)}</strong></div><small>Total estimado. El precio reservado lo confirma el servicio al procesar.</small>${error ? `<p class="selection-warning">${escapeHTML(error)}</p>` : ""}`;
  const complete = Boolean(CURRENT_USER && readIntent(purchasePayload()).record?.completed);
  document.getElementById("purchase-submit").disabled = CHECKOUT_BUSY || !CHECKOUT_READY || Boolean(error) || complete || quantity > (CHECKOUT_ORDERS ? 200 : 1000);
  document.getElementById("purchase-submit").textContent = CHECKOUT_BUSY ? "Procesando…" : complete ? "Solicitud ya completada" : "Revisar compra →";
  document.getElementById("purchase-new").hidden = !complete;
  document.getElementById("add-cart").disabled = CHECKOUT_BUSY || Boolean(selectionError());
  document.getElementById("purchase-quantity").disabled = CHECKOUT_BUSY;
  document.getElementById("purchase-mode").disabled = CHECKOUT_BUSY;
  document.getElementById("checkout-reconnect").disabled = CHECKOUT_BUSY;
  document.querySelectorAll("#seat-options input").forEach(input => {
    input.disabled = CHECKOUT_BUSY || !CHECKOUT_ORDERS || !SEATS.find(seat => seat.codigo === input.value)?.disponible;
  });
}

function renderCart() {
  document.getElementById("cart-summary").innerHTML = ORDER_CART.length ? ORDER_CART.map((item, index) => `<div class="cart-line"><span>${escapeHTML(eventName(item.evento_id))}<small>${item.cantidad} entrada(s) · ${money(item.estimate)}</small></span><button class="text-button" type="button" data-remove-cart="${index}" aria-label="Retirar selección">Quitar</button></div>`).join("") : '<p class="field-help">El grupo está vacío. Puedes añadir este evento y continuar explorando.</p>';
  document.querySelectorAll("[data-remove-cart]").forEach(button => button.onclick = () => { if (!CHECKOUT_BUSY) { ORDER_CART.splice(Number(button.dataset.removeCart), 1); renderCart(); updatePurchaseSummary(); } });
}

function addToCart() {
  const item = selectedItem();
  const error = selectionError(item);
  if (error) { showNotice(document.getElementById("purchase-result"), error, "warning"); return; }
  if (ORDER_CART.length >= 20 || ORDER_CART.reduce((sum, row) => sum + row.cantidad, 0) + item.cantidad > 200) { showNotice(document.getElementById("purchase-result"), "El grupo admite hasta 20 selecciones y 200 entradas.", "warning"); return; }
  if (ORDER_CART.some(row => row.evento_id === item.evento_id && row.asientos.some(seat => item.asientos.includes(seat)))) { showNotice(document.getElementById("purchase-result"), "El grupo ya contiene uno de esos asientos.", "warning"); return; }
  ORDER_CART.push({...item, estimate: itemTotal(item)}); renderCart(); updatePurchaseSummary();
}

function submitPurchase(event) {
  event.preventDefault();
  if (CHECKOUT_BUSY || document.getElementById("purchase-submit").disabled) return;
  if (!CURRENT_USER) { openAuthDialog("login", true, "Inicia sesión para confirmar la selección que acabas de preparar."); return; }
  const payload = purchasePayload();
  const {record} = readIntent(payload);
  if (record?.completed) return;
  REVIEW = {payload: JSON.parse(JSON.stringify(payload)), orders: CHECKOUT_ORDERS, revision: SESSION_REVISION};
  document.getElementById("review-content").innerHTML = checkoutItems().map(item => `<p><strong>${escapeHTML(eventName(item.evento_id))}</strong><br>${item.cantidad} entrada(s)${item.asientos.length ? ` · ${escapeHTML(item.asientos.join(", "))}` : " · Admisión general"}</p>`).join("") + `<p>${CHECKOUT_ORDERS ? "Se reservará el pedido y podrás revisar el total definitivo antes del pago simulado." : "Se registrará la compra al confirmar."}</p>`;
  document.getElementById("review-confirm").disabled = false;
  setCheckoutStep(2);
  document.getElementById("purchase-review").showModal();
}

async function confirmPurchase() {
  const review = REVIEW;
  if (!review || CHECKOUT_BUSY || review.revision !== SESSION_REVISION || !CURRENT_USER) return;
  let intention;
  try { intention = readIntent(review.payload, true); }
  catch (_) { showNotice(document.getElementById("purchase-result"), "No se pudo preparar una clave segura de solicitud. No se envió la compra.", "error"); document.getElementById("purchase-review").close(); return; }
  CHECKOUT_BUSY = true;
  setCheckoutStep(3);
  document.getElementById("review-confirm").disabled = true;
  document.getElementById("purchase-review").close();
  REVIEW = null;
  const {fingerprint, record} = intention;
  updatePurchaseSummary();
  const target = document.getElementById("purchase-result");
  showNotice(target, "Procesando tu solicitud…");
  const response = await fetchJSON(`${CONFIG.COMPRAS_LB_URL}/${review.orders ? "ordenes" : "compras"}`, {
    method: "POST", headers: {"Content-Type": "application/json", "Idempotency-Key": record.key}, body: JSON.stringify(review.payload),
  }, PURCHASE_TIMEOUT_MS);
  CHECKOUT_BUSY = false;
  if (review.revision !== SESSION_REVISION) { updatePurchaseSummary(); return; }
  if (!response.ok) {
    const unknown = response.status === 0 || response.status >= 500;
    showNotice(target, unknown ? `${response.error || "El servicio no pudo confirmar el resultado."} Consulta Mis entradas o reintenta: conservamos la misma clave de solicitud.` : errorMessage(response.data, `La solicitud fue rechazada (HTTP ${response.status}).`), unknown ? "warning" : "error");
    target.dataset.outcome = unknown ? "unknown" : "rejected";
  } else {
    saveIntent(fingerprint, {...record, completed: true});
    const result = response.data;
    if (review.orders) {
      const reserved = result.estado === "reservada";
      showNotice(target, reserved ? `Pedido reservado por ${money(result.total_centimos)}. Completa el pago simulado en Mis entradas antes de ${eventDate(result.expira_en)}.` : `Pedido ${result.estado}. Consulta sus detalles en Mis entradas.`, reserved ? "success" : "warning");
    } else {
      const pending = result.confirmacion_reserva === "pendiente" || result.recuperacion_requerida;
      showNotice(target, pending ? "Compra registrada; la confirmación del inventario sigue pendiente de recuperación." : `Compra #${result.id} confirmada · ${result.cantidad} entrada(s).`, pending ? "warning" : "success");
    }
    target.dataset.outcome = "confirmed";
  }
  await Promise.all([refreshOrders(), loadMyPurchases(), loadEvents()]);
  if (SELECTED_EVENT) await loadSeats();
  updatePurchaseSummary();
}

async function orderAction(id, action, outcome) {
  if (ORDER_ACTIONS.has(id) || !CURRENT_USER) return;
  if (!window.confirm(action === "pagar" ? `¿Confirmas el pago simulado ${outcome} de este pedido?` : "¿Confirmas cancelar o reembolsar este pedido? El backend verificará las entradas y pagos.")) return;
  const revision = SESSION_REVISION;
  ORDER_ACTIONS.add(id);
  document.querySelectorAll("[data-order]").forEach(button => { if (button.dataset.order === id) button.disabled = true; });
  const options = {method: "POST", headers: {"Content-Type": "application/json"}};
  if (outcome) options.body = JSON.stringify({resultado: outcome});
  const response = await fetchJSON(`${CONFIG.COMPRAS_LB_URL}/ordenes/${encodeURIComponent(id)}/${action}`, options, 30000);
  ORDER_ACTIONS.delete(id);
  if (revision !== SESSION_REVISION) return;
  showNotice(document.getElementById("order-notice"), response.ok ? `Pedido ${response.data.estado} · Pago ${response.data.pago}` : errorMessage(response.data, "El resultado puede seguir pendiente. Actualiza el pedido antes de reintentar."), response.ok ? "success" : "warning");
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
  document.getElementById("orders-list").innerHTML = orders.ok && Array.isArray(orders.data) ? orders.data.map(order => {
    const id = escapeHTML(order.id);
    return `<article class="workspace-card order-card"><div class="order-heading"><strong>Pedido ${id.slice(0, 8)}</strong><span class="pill">${escapeHTML(order.estado)}</span></div><p>Pago ${escapeHTML(order.pago)} · <strong>${money(order.total_centimos)}</strong></p>${order.reembolsado_centimos ? `<p>Reembolso acumulado: ${money(order.reembolsado_centimos)}</p>` : ""}<ul>${(order.items || []).map(item => `<li>${escapeHTML(eventName(item.evento_id))} · ${item.cantidad} entrada(s)${item.cancelado ? " · Cancelada / compensación solicitada" : ""}</li>`).join("")}</ul>${order.estado === "reservada" ? `<p>Reserva hasta ${escapeHTML(eventDate(order.expira_en))}</p><button class="button button-accent" data-order="${id}" data-action="pagar" data-outcome="aprobado">Pagar (simulado)</button><button class="text-button" data-order="${id}" data-action="pagar" data-outcome="rechazado">Simular rechazo del pago</button>` : ""}${["reservada", "reservando", "completada", "reembolsando", "cancelando"].includes(order.estado) ? `<button class="button button-outline" data-order="${id}" data-action="cancelar">${order.estado === "completada" ? "Solicitar reembolso" : ["reembolsando", "cancelando"].includes(order.estado) ? "Reintentar compensación" : "Cancelar pedido"}</button>` : ""}<details><summary>Referencia del pedido</summary><code>${id}</code>${order.motivo ? `<p>${escapeHTML(order.motivo)}</p>` : ""}</details></article>`;
  }).join("") || '<p class="empty-state">Aún no tienes pedidos. Explora el catálogo para elegir tu próxima experiencia.</p>' : '<p class="notice notice-error">No se pudieron consultar tus pedidos. Pulsa Actualizar para reintentar.</p>';
  document.querySelectorAll("[data-order]").forEach(button => { button.disabled = ORDER_ACTIONS.has(button.dataset.order); button.onclick = () => orderAction(button.dataset.order, button.dataset.action, button.dataset.outcome); });
  const ticketTarget = document.getElementById("tickets-list");
  ticketTarget.replaceChildren();
  if (tickets.ok && Array.isArray(tickets.data)) {
    for (const ticket of tickets.data) {
      const card = document.createElement("article"); card.className = "workspace-card ticket-card";
      card.innerHTML = `<h3>${escapeHTML(eventName(ticket.evento_id))}</h3><p>${escapeHTML(ticket.asiento || "Admisión general")} · <strong>${escapeHTML(ticket.estado)}</strong></p>`;
      // SVG is displayed as an image, never inserted as executable markup.
      if (ticket.estado === "emitida" && ticket.qr_svg) {
        const img = document.createElement("img"); img.alt = "Código QR de acceso"; img.width = 180; img.height = 180;
        img.src = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(ticket.qr_svg)}`; card.appendChild(img);
        const details = document.createElement("details"); const title = document.createElement("summary"); title.textContent = "Mostrar código de acceso";
        const code = document.createElement("code"); code.textContent = ticket.qr; details.append(title, code); card.appendChild(details);
      } else { const note = document.createElement("p"); note.className = "muted"; note.textContent = "Esta entrada no está habilitada para un nuevo acceso."; card.appendChild(note); }
      ticketTarget.appendChild(card);
    }
    if (!tickets.data.length) ticketTarget.textContent = "Las entradas aparecerán después de completar el pago.";
  } else ticketTarget.textContent = "No se pudieron consultar las entradas.";
  document.getElementById("notifications-list").textContent = messages.ok && Array.isArray(messages.data) ? messages.data.map(message => message.mensaje).join(" · ") || "Sin notificaciones." : "Notificaciones no disponibles.";
}

function checkoutIdentityChanged(previous, next) {
  REVIEW = null;
  document.getElementById("purchase-review")?.close();
  if (previous && previous.id !== next?.id) ORDER_CART = [];
  ["orders-list", "tickets-list", "notifications-list", "order-notice"].forEach(id => { document.getElementById(id).textContent = ""; });
  renderCart(); updatePurchaseSummary(); refreshOrders();
}

function initializeCheckout() {
  document.getElementById("purchase-form").addEventListener("submit", submitPurchase);
  document.getElementById("purchase-quantity").addEventListener("input", () => { clearPurchaseStatus(); updatePurchaseSummary(); });
  document.getElementById("purchase-mode").addEventListener("change", () => { clearPurchaseStatus(); updatePurchaseSummary(); });
  document.getElementById("add-cart").addEventListener("click", addToCart);
  document.getElementById("review-confirm").addEventListener("click", confirmPurchase);
  document.getElementById("purchase-review").addEventListener("close", () => { REVIEW = null; if (!CHECKOUT_BUSY && !document.getElementById("purchase-result").dataset.outcome) setCheckoutStep(1); });
  document.getElementById("refresh-orders").addEventListener("click", () => { refreshOrders(); loadMyPurchases(); if (!CURRENT_USER) openAuthDialog(); });
  document.getElementById("checkout-reconnect").addEventListener("click", async () => { await detectCheckout(); if (SELECTED_EVENT) loadSeats(); });
  document.getElementById("purchase-new").addEventListener("click", () => {
    const {fingerprint} = readIntent(purchasePayload()); INTENT_KEYS.delete(fingerprint);
    try { sessionStorage.removeItem(fingerprint); } catch (_) { /* No persistent storage. */ }
    clearPurchaseStatus(); updatePurchaseSummary();
  });
  renderCart();
}
