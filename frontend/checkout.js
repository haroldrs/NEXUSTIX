"use strict";
let ORDER_CART = [];
let CHECKOUT_USER = null;

function checkoutNotice(id, message, error = false) {
  const target = document.getElementById(id);
  target.textContent = message;
  target.className = `notice ${error ? "notice-error" : "notice-success"}`;
}

async function loadSeats() {
  const id = Number(document.getElementById("purchase-event").value);
  const target = document.getElementById("seat-options");
  target.textContent = "";
  if (!id || !CONFIG.CHECKOUT_ORDERS) return;
  const result = await fetchJSON(`${CONFIG.EVENTOS_URL}/eventos/${id}/asientos`);
  if (!result.ok) { target.textContent = "No se pudieron consultar los asientos."; return; }
  target.innerHTML = result.data.map(s => `<label class="stock-chip"><input type="checkbox" value="${escapeHTML(s.codigo)}" ${s.disponible ? "" : "disabled"}>${escapeHTML(s.codigo)} · ${escapeHTML(s.zona)} · S/ ${(s.precio_centimos / 100).toFixed(2)}</label>`).join("") || "Admisión general";
}

function selectedItem() {
  return {evento_id: Number(document.getElementById("purchase-event").value),
    cantidad: Number(document.getElementById("purchase-quantity").value),
    asientos: Array.from(document.querySelectorAll("#seat-options input:checked"), s => s.value)};
}

async function submitOrder(event) {
  event.preventDefault();
  if (!CURRENT_USER) { openAuthDialog("login", true); return; }
  const items = ORDER_CART.length ? ORDER_CART : [selectedItem()];
  const payload = {items};
  const fingerprint = `nexustix-order:${CURRENT_USER.id}:${JSON.stringify(payload)}`;
  // Solo la clave de intención; nunca JWT ni datos de pago en almacenamiento.
  const key = sessionStorage.getItem(fingerprint) || crypto.randomUUID();
  sessionStorage.setItem(fingerprint, key);
  const button = document.getElementById("purchase-submit"); button.disabled = true;
  const result = await fetchJSON(`${CONFIG.COMPRAS_LB_URL}/ordenes`, {method: "POST",
    headers: {"Content-Type": "application/json", "Idempotency-Key": key}, body: JSON.stringify(payload)}, 30000);
  button.disabled = false;
  if (result.ok) {
    sessionStorage.removeItem(fingerprint); ORDER_CART = [];
    document.getElementById("cart-summary").textContent = "";
    showNotice(document.getElementById("purchase-result"), `Pedido ${result.data.estado}. Completa el pago simulado en Mis pedidos antes de ${new Date(result.data.expira_en).toLocaleTimeString("es-PE")}.`, "success");
  } else {
    showNotice(document.getElementById("purchase-result"), errorMessage(result.data, "Resultado pendiente. Conservamos la clave: consulta Mis pedidos o reintenta."), "warning");
  }
  await refreshOrders(); await loadEvents();
}

async function orderAction(id, action, outcome) {
  const options = {method: "POST", headers: {"Content-Type": "application/json"}};
  if (outcome) options.body = JSON.stringify({resultado: outcome});
  const result = await fetchJSON(`${CONFIG.COMPRAS_LB_URL}/ordenes/${id}/${action}`, options, 30000);
  checkoutNotice("order-notice", result.ok ? `Pedido ${result.data.estado} · Pago ${result.data.pago}` : errorMessage(result.data, "Resultado pendiente de recuperación; actualiza el pedido."), !result.ok);
  await refreshOrders(); await loadEvents();
}

async function refreshOrders() {
  if (!CONFIG.CHECKOUT_ORDERS) return;
  const owner = CURRENT_USER?.id;
  if (!owner) {
    ["orders-list", "tickets-list", "notifications-list"].forEach(id => { document.getElementById(id).textContent = "Inicia sesión para consultar tus pedidos."; });
    return;
  }
  const [orders, tickets, messages] = await Promise.all(["ordenes/mias", "entradas/mias", "notificaciones"].map(path => fetchJSON(`${CONFIG.COMPRAS_LB_URL}/${path}`, {}, 15000)));
  if (CURRENT_USER?.id !== owner) return;
  document.getElementById("orders-list").innerHTML = orders.ok ? orders.data.map(o => `<article class="workspace-card"><strong>Pedido ${escapeHTML(o.id)}</strong><p>${escapeHTML(o.estado)} · Pago ${escapeHTML(o.pago)} · S/ ${(o.total_centimos/100).toFixed(2)}</p><p>Reserva hasta ${escapeHTML(new Date(o.expira_en).toLocaleString("es-PE"))}</p>${o.estado === "reservada" ? `<button class="button button-accent" data-order="${o.id}" data-action="pagar" data-outcome="aprobado">Pagar (simulación)</button> <button class="button button-outline" data-order="${o.id}" data-action="pagar" data-outcome="rechazado">Simular rechazo</button>` : ""}${["reservada", "reservando", "completada"].includes(o.estado) ? ` <button class="button button-outline" data-order="${o.id}" data-action="cancelar">${o.estado === "completada" ? "Reembolsar" : "Cancelar"}</button>` : ""}</article>`).join("") : "Pedidos no disponibles.";
  document.querySelectorAll("[data-order]").forEach(button => button.addEventListener("click", async () => {
    button.disabled = true; await orderAction(button.dataset.order, button.dataset.action, button.dataset.outcome);
  }));
  document.getElementById("tickets-list").innerHTML = tickets.ok ? tickets.data.map(t => `<article class="workspace-card"><h3>Evento #${escapeHTML(t.evento_id)}</h3><p>${escapeHTML(t.asiento || "Admisión general")} · ${escapeHTML(t.estado)}</p><img alt="Código QR de acceso" width="180" height="180" src="data:image/svg+xml;base64,${btoa(t.qr_svg)}"><details><summary>Código para control de acceso</summary><code>${escapeHTML(t.qr)}</code></details></article>`).join("") : "Entradas no disponibles.";
  document.getElementById("notifications-list").textContent = messages.ok ? messages.data.map(m => m.mensaje).join(" · ") : "Notificaciones no disponibles.";
}

document.addEventListener("DOMContentLoaded", () => {
  document.getElementById("checkout-panel").hidden = !CONFIG.CHECKOUT_ORDERS;
  document.getElementById("order-options").hidden = !CONFIG.CHECKOUT_ORDERS;
  document.getElementById("purchase-event").addEventListener("change", loadSeats);
  document.getElementById("refresh-orders").addEventListener("click", refreshOrders);
  document.getElementById("add-cart").addEventListener("click", () => {
    const item = selectedItem(); if (!item.evento_id || item.cantidad < 1) return;
    ORDER_CART.push(item); document.getElementById("cart-summary").textContent = ORDER_CART.map(i => `${i.cantidad} entrada(s) de evento #${i.evento_id}`).join(" + ");
  });
  document.getElementById("event-editor").addEventListener("submit", async e => {
    e.preventDefault(); const f = new FormData(e.target);
    const seats = String(f.get("asientos")).trim().split("\n").filter(Boolean).map(line => {
      const [codigo, zona, precio] = line.split(",").map(s => s.trim()); return {codigo, zona, precio_centimos: Math.round(Number(precio)*100)};
    });
    const id = Number(f.get("evento_id"));
    const metadata = {nombre: f.get("nombre"), lugar: f.get("lugar"), fecha: new Date(f.get("fecha")).toISOString()};
    const payload = id ? {...metadata, activo: true} : {...metadata, capacidad: Number(f.get("capacidad")), precio_centimos: Math.round(Number(f.get("precio"))*100), asientos: seats};
    const result = await fetchJSON(`${CONFIG.EVENTOS_URL}/eventos${id ? `/${id}` : ""}`, {method: id ? "PUT" : "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload)});
    checkoutNotice("event-result", result.ok ? `Evento #${result.data.id} publicado` : errorMessage(result.data, "No se pudo crear el evento"), !result.ok);
    if (result.ok) loadEvents();
  });
  document.getElementById("access-form").addEventListener("submit", async e => {
    e.preventDefault(); const f = new FormData(e.target);
    const result = await fetchJSON(`${CONFIG.COMPRAS_LB_URL}/entradas/validar`, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({qr: f.get("qr"), evento_id: Number(f.get("evento_id"))})});
    checkoutNotice("access-result", result.ok ? "Acceso permitido" : errorMessage(result.data, "No se pudo validar la entrada"), !result.ok);
  });
  document.getElementById("role-form").addEventListener("submit", async e => {
    e.preventDefault(); const id = Number(new FormData(e.target).get("user_id"));
    const result = await fetchJSON(`${CONFIG.USUARIOS_URL}/usuarios/${id}/rol`, {method: "POST", headers: {"Content-Type": "application/json"}, body: '{"role":"organizador"}'});
    checkoutNotice("role-result", result.ok ? "Rol asignado; el usuario debe iniciar sesión de nuevo." : errorMessage(result.data, "No se pudo asignar el rol"), !result.ok);
  });
  document.getElementById("refresh-distributed").addEventListener("click", async () => {
    const [orders, inventory, ...databases] = await Promise.all([
      fetchJSON(`${CONFIG.COMPRAS_LB_URL}/ordenes/estado`), fetchJSON(`${CONFIG.EVENTOS_URL}/inventario/estado`),
      ...[CONFIG.USUARIOS_URL, CONFIG.EVENTOS_URL, CONFIG.COMPRAS_LB_URL].map(url => fetchJSON(`${url}/diagnostico/db`))]);
    document.getElementById("distributed-health").textContent = JSON.stringify({pedidos: orders.data, inventario: inventory.data, bases: databases.map(r => r.data)}, null, 2);
  });
  setInterval(() => {
    if (CHECKOUT_USER !== CURRENT_USER?.id) {
      CHECKOUT_USER = CURRENT_USER?.id; ORDER_CART = [];
      document.getElementById("cart-summary").textContent = "";
      ["order-notice", "access-result", "event-result", "role-result", "distributed-health"].forEach(id => { document.getElementById(id).textContent = ""; });
      refreshOrders();
    }
  }, 500);
});
