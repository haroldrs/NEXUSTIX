"use strict";

const SERVICE_DEFS = [
  { id: "usuarios", label: "Usuarios", url: CONFIG.USUARIOS_URL, detail: "Validación de compradores" },
  { id: "eventos", label: "Eventos", url: CONFIG.EVENTOS_URL, detail: "Catálogo y reservas de stock" },
  { id: "compras1", label: "Compras · R1", url: CONFIG.COMPRAS_R1_URL, detail: "Instancia de Compras" },
  { id: "compras2", label: "Compras · R2", url: CONFIG.COMPRAS_R2_URL, detail: "Instancia de Compras" },
  { id: "compras_lb", label: "Balanceador", url: CONFIG.COMPRAS_LB_URL, detail: "Entrada de Compras" },
];

function escapeHTML(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
}

async function fetchJSON(url, options = {}, timeoutMs = 5000, retried = false) {
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const headers = new Headers(options.headers || {});
    const requestUserId = CURRENT_USER?.id;
    const requestToken = ACCESS_TOKEN && !headers.has("Authorization") ? ACCESS_TOKEN : null;
    if (requestToken) headers.set("Authorization", `Bearer ${requestToken}`);
    const response = await fetch(url, { ...options, headers, signal: controller.signal });
    let data = null;
    try { data = await response.json(); } catch (_) { /* respuesta sin JSON */ }
    if (response.status === 401 && ACCESS_TOKEN && !retried && !url.includes("/auth/")) {
      if (requestToken && requestToken !== ACCESS_TOKEN) {
        if (requestUserId === CURRENT_USER?.id) return fetchJSON(url, options, timeoutMs, true);
        return { ok: false, status: response.status, data };
      }
      const refreshed = await renewAccessToken(timeoutMs);
      if (refreshed.ok && refreshed.data?.access_token && refreshed.data?.user) {
        return fetchJSON(url, options, timeoutMs, true);
      }
      if (refreshed.status === 401) {
        clearAuthenticated();
        requestLoginAfterExpiry();
      } else {
        showRenewalUnavailable();
      }
      return { ok: false, status: response.status, data, error: refreshed.error || "No se pudo renovar la sesión." };
    }
    return { ok: response.ok, status: response.status, data };
  } catch (error) {
    return { ok: false, status: 0, error: error.name === "AbortError" ? "La solicitud agotó el tiempo de espera." : "No se pudo conectar con el servicio." };
  } finally {
    clearTimeout(timeoutId);
  }
}

let ACCESS_TOKEN = null;
let CURRENT_USER = null;
let REFRESH_PROMISE = null;
const PENDING_PURCHASE_KEYS = new Map();
const PURCHASE_TIMEOUT_MS = 30000;

function purchaseIdempotencyKey(userId, payload) {
  const fingerprint = `${userId}:${JSON.stringify(payload)}`;
  if (!PENDING_PURCHASE_KEYS.has(fingerprint)) {
    const key = globalThis.crypto?.randomUUID?.() || Array.from(globalThis.crypto.getRandomValues(new Uint8Array(16)), (byte) => byte.toString(16).padStart(2, "0")).join("");
    PENDING_PURCHASE_KEYS.set(fingerprint, key);
  }
  return { fingerprint, key: PENDING_PURCHASE_KEYS.get(fingerprint) };
}

async function renewAccessToken(timeoutMs = 5000) {
  if (!REFRESH_PROMISE) {
    const performRefresh = async () => {
      const result = await fetchJSON(`${CONFIG.USUARIOS_URL}/auth/refresh`, {
        method: "POST", credentials: "include", headers: { "X-CSRF-Protection": "1" },
      }, timeoutMs, true);
      if (result.ok && result.data?.access_token && result.data?.user) setAuthenticated(result.data);
      return result;
    };
    const refresh = typeof navigator !== "undefined" && navigator.locks?.request
      ? navigator.locks.request("nexustix-auth-refresh", performRefresh)
      : performRefresh();
    REFRESH_PROMISE = refresh;
  }
  const pending = REFRESH_PROMISE;
  try {
    return await pending;
  } finally {
    if (REFRESH_PROMISE === pending) REFRESH_PROMISE = null;
  }
}

function showRenewalUnavailable() {
  const status = document.getElementById("auth-status");
  if (CURRENT_USER && status) {
    status.textContent = `${CURRENT_USER.nombre} · ${CURRENT_USER.role} · renovación pendiente`;
    status.title = "No se pudo renovar la sesión por un problema temporal. Se volverá a intentar en la próxima petición.";
  }
}

function setAuthenticated(payload) {
  const previousUser = CURRENT_USER;
  const nextUser = payload.user;
  if (previousUser?.id !== nextUser?.id || previousUser?.role !== nextUser?.role) clearPurchaseStatus();
  ACCESS_TOKEN = payload.access_token;
  CURRENT_USER = nextUser;
  const status = document.getElementById("auth-status");
  status.textContent = `${CURRENT_USER.nombre} · ${CURRENT_USER.role}`;
  status.title = "";
  document.getElementById("auth-open").hidden = true;
  document.getElementById("auth-logout").hidden = false;
  setRegistrationAvailable(false);
  if (CURRENT_USER.role !== "administrador") clearAdminData();
  loadMyPurchases();
}

function clearPurchaseStatus() {
  const result = document.getElementById("purchase-result");
  if (result) {
    result.textContent = "";
    result.className = "result-box";
    delete result.dataset.outcome;
  }
  const button = document.getElementById("purchase-submit");
  if (button) button.disabled = false;
}

function setRegistrationAvailable(available) {
  const registerTab = document.querySelector('[data-auth-mode="register"]');
  if (registerTab) registerTab.hidden = !available;
}

function openAuthDialog(mode = "login", allowRegistration = !CURRENT_USER, message = "") {
  const dialog = document.getElementById("auth-dialog");
  setRegistrationAvailable(allowRegistration);
  selectAuthMode(mode);
  if (message) showNotice(document.getElementById("auth-result"), message, "warning");
  if (!dialog.open) dialog.showModal();
}

function selectAuthMode(mode) {
  const register = mode === "register";
  document.getElementById("auth-dialog").dataset.mode = register ? "register" : "login";
  document.getElementById("auth-title").textContent = register ? "Crea tu cuenta" : "Inicia sesión";
  document.getElementById("auth-name-label").hidden = !register;
  document.getElementById("auth-name").hidden = !register;
  document.getElementById("auth-name").required = register;
  document.getElementById("auth-password").autocomplete = register ? "new-password" : "current-password";
  document.querySelectorAll("[data-auth-mode]").forEach((tab) => tab.classList.toggle("active", tab.dataset.authMode === mode));
}

function requestLoginAfterExpiry() {
  openAuthDialog("login", false, "La sesión expiró. Inicia sesión de nuevo para continuar.");
}

function clearAdminData() {
  document.getElementById("admin-events").textContent = "Inicia sesión como administrador para consultar estos datos.";
  document.getElementById("admin-sales").textContent = "Información administrativa restringida.";
  document.getElementById("admin-users").textContent = "Información administrativa restringida.";
  document.getElementById("audit-result").textContent = "Auditoría restringida a administradores.";
  document.getElementById("pending-status").textContent = "Información restringida a administradores.";
  document.getElementById("journal-list").textContent = "Journal restringido a administradores.";
  document.getElementById("journal-count").textContent = "restringido";
}

function clearAuthenticated() {
  ACCESS_TOKEN = null;
  CURRENT_USER = null;
  document.getElementById("auth-status").textContent = "Sesión no iniciada";
  document.getElementById("auth-open").hidden = false;
  document.getElementById("auth-logout").hidden = true;
  setRegistrationAvailable(true);
  document.getElementById("my-purchases").textContent = "Inicia sesión para consultar tu historial.";
  clearPurchaseStatus();
  clearAdminData();
  if (document.getElementById("view-admin").classList.contains("active") || document.getElementById("view-observabilidad").classList.contains("active")) {
    document.querySelectorAll(".view").forEach((view) => view.classList.toggle("active", view.id === "view-comprar"));
    document.querySelectorAll(".nav-link").forEach((button) => button.classList.toggle("active", button.dataset.view === "comprar"));
  }
}

async function restoreSession() {
  const result = await renewAccessToken();
  if (!result.ok && result.status === 401 && result.data?.detail && result.data.detail !== "Sesión de actualización no disponible") {
    requestLoginAfterExpiry();
  }
}

async function loadMyPurchases() {
  const target = document.getElementById("my-purchases");
  if (!CURRENT_USER) return;
  const { ok, data, error, status } = await fetchJSON(`${CONFIG.COMPRAS_LB_URL}/compras/mias`);
  if (!ok || !Array.isArray(data)) { target.textContent = error || `Historial no disponible (HTTP ${status}).`; return; }
  target.innerHTML = data.length ? data.slice().reverse().map((purchase) => `<div class="admin-row"><div><strong>Compra #${escapeHTML(purchase.id)} · Evento #${escapeHTML(purchase.evento_id)}</strong><small>${escapeHTML(purchase.cantidad)} entrada(s) · ${escapeHTML(purchase.estado)}</small></div></div>`).join("") : "Aún no tienes compras.";
}

async function submitAuth(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const mode = document.getElementById("auth-dialog").dataset.mode || "login";
  const payload = { email: form.elements.email.value.trim(), password: form.elements.password.value };
  if (mode === "register") payload.nombre = form.elements.nombre.value.trim();
  const { ok, data, error, status } = await fetchJSON(`${CONFIG.USUARIOS_URL}/auth/${mode === "register" ? "register" : "login"}`, {
    method: "POST", credentials: "include", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
  });
  const result = document.getElementById("auth-result");
  if (!ok || !data?.access_token || !data?.user) {
    showNotice(result, error || errorMessage(data, `No se pudo autenticar (HTTP ${status}).`), "error");
    return;
  }
  setAuthenticated(data);
  form.reset();
  document.getElementById("auth-dialog").close();
  showNotice(document.getElementById("purchase-result"), `Sesión iniciada como ${data.user.nombre}.`, "success");
}

async function logout() {
  if (REFRESH_PROMISE) {
    try { await REFRESH_PROMISE; } catch (_) { /* Se intenta revocar igualmente la cookie disponible. */ }
  }
  const response = await fetchJSON(`${CONFIG.USUARIOS_URL}/auth/logout`, {
    method: "POST", credentials: "include", headers: { "X-CSRF-Protection": "1" },
  });
  if (response.ok) clearAuthenticated();
  else showNotice(document.getElementById("purchase-result"), response.error || "No se pudo revocar la sesión. Inténtalo de nuevo.", "error");
}

function setView(name) {
  if ((name === "admin" && !["administrador", "organizador"].includes(CURRENT_USER?.role)) || (name === "observabilidad" && CURRENT_USER?.role !== "administrador")) {
    const message = CURRENT_USER
      ? "Esta sección requiere una cuenta administradora. Inicia sesión con una cuenta autorizada; el registro público crea cuentas compradoras."
      : "Esta sección requiere una cuenta administradora existente. El registro público crea cuentas compradoras.";
    openAuthDialog("login", false, message);
    return;
  }
  document.querySelectorAll(".view").forEach((view) => view.classList.toggle("active", view.id === `view-${name}`));
  document.querySelectorAll(".nav-link").forEach((button) => button.classList.toggle("active", button.dataset.view === name));
  window.scrollTo({ top: 0, behavior: "smooth" });
  if (name === "admin") { loadAdminEvents(); loadAdminSales(); loadAdminUsers(); }
  if (name === "observabilidad") refreshObservability();
}

function showNotice(element, message, kind = "") {
  if (!element) return;
  element.className = `notice${kind ? ` notice-${kind}` : ""}`;
  element.textContent = message;
}

let EVENTS = [];
async function loadEvents() {
  const status = document.getElementById("catalog-status");
  const grid = document.getElementById("event-grid");
  const select = document.getElementById("purchase-event");
  const { ok, data, error, status: code } = await fetchJSON(`${CONFIG.EVENTOS_URL}/eventos`);
  if (!ok || !Array.isArray(data)) {
    showNotice(status, `${error || `Eventos respondió HTTP ${code}.`} El catálogo no está disponible por ahora.`, "error");
    grid.innerHTML = "";
    select.innerHTML = '<option value="">Catálogo no disponible</option>';
    return;
  }
  EVENTS = data;
  showNotice(status, `${data.length} eventos recibidos desde el servicio de Eventos.`, "success");
  if (!data.length) grid.innerHTML = '<div class="empty-state">No hay eventos publicados.</div>';
  else grid.innerHTML = data.map((event, index) => {
    const soldOut = Number(event.stock) <= 0;
    return `<article class="event-card">
      <div class="event-visual visual-${index % 4}"><span class="visual-index">N° ${String(index + 1).padStart(2, "0")}</span><span class="visual-symbol">${["✳", "◈", "✦", "◉"][index % 4]}</span><span class="visual-brand">NEXUSTIX / LIVE</span></div>
      <div class="event-card-body"><div class="event-kicker"><span>EVENTO #${escapeHTML(event.id)}</span><span class="stock-state ${soldOut ? "sold-out" : "available"}">${soldOut ? "Agotado" : `${escapeHTML(event.stock)} disponibles`}</span></div>
      <h3>${escapeHTML(event.nombre)}</h3><p class="event-unavailable">${escapeHTML(event.fecha ? new Date(event.fecha).toLocaleString("es-PE") : "Fecha por confirmar")} · ${escapeHTML(event.lugar || "Lugar por confirmar")}</p>
      <div class="event-card-bottom"><span class="event-price">Precio <strong>S/ ${escapeHTML(((event.precio_centimos || 0) / 100).toFixed(2))}</strong></span><button class="text-button" data-select-event="${escapeHTML(event.id)}" ${soldOut ? "disabled" : ""}>${soldOut ? "Sin disponibilidad" : "Elegir evento →"}</button></div></div>
    </article>`;
  }).join("");
  select.innerHTML = '<option value="">Selecciona un evento</option>' + data.map((event) => `<option value="${escapeHTML(event.id)}" ${Number(event.stock) <= 0 ? "disabled" : ""}>${escapeHTML(event.nombre)} · ${escapeHTML(event.stock)} disponibles</option>`).join("");
  grid.querySelectorAll("[data-select-event]").forEach((button) => button.addEventListener("click", () => {
    select.value = button.dataset.selectEvent;
    if (typeof loadSeats === "function") loadSeats();
    document.getElementById("compra").scrollIntoView({ behavior: "smooth" });
  }));
}

function errorMessage(data, fallback) {
  const detail = data?.detail;
  if (typeof detail === "string") return detail;
  if (detail && typeof detail === "object") return detail.mensaje || detail.detail || fallback;
  return fallback;
}

async function submitPurchase(event) {
  if (CONFIG.CHECKOUT_ORDERS) return submitOrder(event);
  event.preventDefault();
  const form = event.currentTarget;
  const button = document.getElementById("purchase-submit");
  const result = document.getElementById("purchase-result");
  if (!ACCESS_TOKEN) {
    showNotice(result, "Inicia sesión antes de realizar una compra.", "warning");
    openAuthDialog("login", true);
    return;
  }
  const evento_id = Number(form.elements.evento_id.value);
  const cantidad = Number(form.elements.cantidad.value);
  if (!Number.isInteger(evento_id) || evento_id < 1 || !Number.isInteger(cantidad) || cantidad < 1) {
    showNotice(result, "Revisa el evento y la cantidad solicitada.", "error");
    return;
  }
  const { fingerprint, key } = purchaseIdempotencyKey(CURRENT_USER.id, { evento_id, cantidad });
  button.disabled = true;
  showNotice(result, "Enviando la solicitud a Compras…", "");
  // El balanceador puede reintentar una vez en la otra réplica con la misma
  // clave; el tiempo de espera cubre ese reintento sin cambiar de clave.
  const response = await fetchJSON(`${CONFIG.COMPRAS_LB_URL}/compras`, {
    method: "POST", headers: { "Content-Type": "application/json", "Idempotency-Key": key },
    body: JSON.stringify({ evento_id, cantidad }),
  }, PURCHASE_TIMEOUT_MS);
  button.disabled = false;
  if (response.status === 0) {
    showNotice(result, `${response.error} No podemos confirmar si el servidor alcanzó a registrar la compra. Revisa “Mis compras” antes de volver a intentarlo.`, "warning");
    result.dataset.outcome = "unknown";
    loadMyPurchases();
    loadEvents();
    return;
  }
  if (!response.ok) {
    if (response.status >= 400 && response.status < 500) PENDING_PURCHASE_KEYS.delete(fingerprint);
    showNotice(result, `Compra rechazada (HTTP ${response.status}): ${errorMessage(response.data, "El servicio no pudo completar la solicitud.")}`, "error");
    result.dataset.outcome = "rejected";
    loadEvents();
    return;
  }
  const purchase = response.data || {};
  PENDING_PURCHASE_KEYS.delete(fingerprint);
  if (purchase.confirmacion_reserva === "pendiente" || purchase.recuperacion_requerida) {
    showNotice(result, `Compra registrada, pero la confirmación del inventario está pendiente de recuperación. Operación: ${purchase.operacion_id || "no informada"}.`, "warning");
    result.dataset.outcome = "pending";
  } else {
    showNotice(result, `Compra confirmada · ${purchase.cantidad} entrada(s) · Evento #${purchase.evento_id} · Operación ${purchase.operacion_id || `#${purchase.id}`}.`, "success");
    result.dataset.outcome = "confirmed";
  }
  loadEvents();
  loadMyPurchases();
}

async function loadAdminEvents() {
  const target = document.getElementById("admin-events");
  const { ok, data, error, status } = await fetchJSON(`${CONFIG.EVENTOS_URL}/eventos`);
  if (!ok || !Array.isArray(data)) { target.innerHTML = `<p class="notice notice-error">${escapeHTML(error || `Eventos respondió HTTP ${status}.`)}</p>`; return; }
  target.innerHTML = data.map((event) => `<div class="admin-row"><div><strong>${escapeHTML(event.nombre)}</strong><small>Evento #${escapeHTML(event.id)}</small></div><span class="stock-chip">${escapeHTML(event.stock)} disponibles</span></div>`).join("") || '<p class="muted">No hay eventos.</p>';
}

async function loadAdminSales() {
  const target = document.getElementById("admin-sales");
  const { ok, data, error, status } = await fetchJSON(`${CONFIG.COMPRAS_LB_URL}/compras`);
  if (!ok || !Array.isArray(data)) { target.innerHTML = `<p class="notice notice-error">${escapeHTML(error || `Compras respondió HTTP ${status}.`)}</p>`; return; }
  if (!data.length) { target.innerHTML = '<p class="muted">Aún no hay ventas registradas.</p>'; return; }
  target.innerHTML = `<table class="data-table"><thead><tr><th>Compra</th><th>Usuario</th><th>Evento</th><th>Cant.</th><th>Estado</th></tr></thead><tbody>${data.slice().reverse().map((sale) => `<tr><td>#${escapeHTML(sale.id)}</td><td>#${escapeHTML(sale.usuario_id)}</td><td>#${escapeHTML(sale.evento_id)}</td><td>${escapeHTML(sale.cantidad)}</td><td>${escapeHTML(sale.estado)}</td></tr>`).join("")}</tbody></table>`;
}

async function loadAdminUsers() {
  const target = document.getElementById("admin-users");
  const { ok, data, error, status } = await fetchJSON(`${CONFIG.USUARIOS_URL}/usuarios`);
  if (!ok || !Array.isArray(data)) { target.innerHTML = `<p class="notice notice-error">${escapeHTML(error || `Usuarios respondió HTTP ${status}.`)}</p>`; return; }
  target.innerHTML = data.map((user) => `<div class="admin-row"><div><strong>${escapeHTML(user.nombre)}</strong><small>${escapeHTML(user.email)}</small></div><span class="stock-chip">#${escapeHTML(user.id)}</span></div>`).join("") || '<p class="muted">No hay usuarios.</p>';
}

async function loadServiceHealth(service) {
  const started = performance.now();
  const response = await fetchJSON(`${service.url}/health`, {}, 3000);
  const latency = Math.round(performance.now() - started);
  if (response.status === 0) return { ...service, state: "unavailable", label: "Sin respuesta", latency: null, info: response.error };
  if (!response.data) return { ...service, state: "unknown", label: `HTTP ${response.status}`, latency, info: "Respuesta sin datos de salud legibles." };
  const health = response.data;
  const healthy = response.ok && health.status === "ok";
  const degraded = health.status === "degraded";
  return { ...service, state: healthy ? "healthy" : degraded ? "degraded" : "unavailable", label: healthy ? "Responde · reporta OK" : degraded ? "Degradado" : `HTTP ${response.status}`, latency, info: health.db ? `Base de datos: ${health.db}` : (health.motivo || "El servicio no reporta estado saludable."), instance: health.instancia };
}

async function loadMetrics() {
  const target = document.getElementById("replica-metrics");
  const replicas = SERVICE_DEFS.filter((service) => service.id === "compras1" || service.id === "compras2");
  const values = await Promise.all(replicas.map(async (service) => {
    const { ok, data, error, status } = await fetchJSON(`${service.url}/metricas`, {}, 3000);
    if (!ok || !data) return `<div class="metric-card unavailable"><strong>${escapeHTML(service.label)}</strong><span>Información no disponible</span><small>${escapeHTML(error || `HTTP ${status}`)}</small></div>`;
    return `<div class="metric-card"><strong>${escapeHTML(service.label)}</strong><span>${escapeHTML(data.peticiones_totales ?? "—")} solicitudes · ${escapeHTML(data.errores_http ?? "—")} errores</span><small>Latencia promedio ${escapeHTML(data.latencia_promedio_ms ?? "—")} ms · uptime ${escapeHTML(data.uptime_segundos ?? "—")} s</small></div>`;
  }));
  target.innerHTML = values.join("");
}

async function loadAudit() {
  const target = document.getElementById("audit-result");
  const pending = document.getElementById("pending-status");
  if (CURRENT_USER?.role !== "administrador") {
    showNotice(target, "La auditoría de stock requiere una sesión administradora.", "warning");
    showNotice(pending, "El estado de recuperación solo está disponible para administración.", "warning");
    return;
  }
  showNotice(target, "Consultando auditoría real de stock…");
  const { ok, data, error, status } = await fetchJSON(`${CONFIG.COMPRAS_LB_URL}/auditoria/consistencia`, {}, 8000);
  if (!ok || !data) {
    showNotice(target, `${error || `Auditoría no disponible (HTTP ${status}).`} No se puede afirmar consistencia.`, "error");
    showNotice(pending, "No se pudo consultar el estado de reservas pendientes; recuperación no verificable.", "warning");
    return;
  }
  const count = data.totales?.reservas_pendientes;
  const consistent = data.todo_consistente === true && data.balance_global_diferencia === 0;
  const partial = data.todo_consistente === true && data.auditoria_completa === false;
  const kind = consistent ? "success" : partial ? "warning" : "error";
  showNotice(target, `${consistent ? "Consistente" : partial ? "Consistencia parcial" : "Inconsistencia detectada"} · diferencia global ${data.balance_global_diferencia ?? "no disponible"} · ${data.veredicto || ""}`, kind);
  showNotice(pending, count === 0 ? "Sin reservas pendientes según la auditoría." : Number.isInteger(count) ? `${count} reserva(s) pendiente(s): requieren conciliación o investigación.` : "La auditoría no informó el número de reservas pendientes.", count === 0 ? "success" : "warning");
}

async function loadJournal() {
  const target = document.getElementById("journal-list");
  const count = document.getElementById("journal-count");
  if (CURRENT_USER?.role !== "administrador") {
    target.innerHTML = '<p class="notice notice-warning">El journal requiere una sesión administradora.</p>';
    count.textContent = "restringido";
    return;
  }
  const { ok, data, error, status } = await fetchJSON(`${CONFIG.COMPRAS_LB_URL}/compras/journal`, {}, 5000);
  if (!ok || !data) { target.innerHTML = `<p class="notice notice-warning">Bitácora no disponible: ${escapeHTML(error || `HTTP ${status}`)}</p>`; count.textContent = "sin datos"; return; }
  count.textContent = `${data.total_entradas ?? data.entradas?.length ?? 0} registros`;
  const entries = data.entradas || [];
  target.innerHTML = entries.length ? entries.slice().reverse().map((entry) => `<div class="journal-entry"><span class="journal-type">${escapeHTML(entry.tipo)}</span><div><strong>${escapeHTML(entry.mensaje)}</strong><small>${escapeHTML(entry.timestamp)} · T=${escapeHTML(entry.reloj_t)} · Instancia ${escapeHTML(entry.instancia)}</small></div></div>`).join("") : '<p class="muted">El journal no contiene entradas.</p>';
}

async function refreshObservability() {
  const target = document.getElementById("service-grid");
  target.innerHTML = '<p class="muted">Consultando servicios…</p>';
  const services = await Promise.all(SERVICE_DEFS.map(loadServiceHealth));
  target.innerHTML = services.map((service) => `<article class="service-card state-${service.state}"><div class="service-card-head"><span class="state-indicator"></span><span class="service-label">${escapeHTML(service.label)}</span><span class="state-label">${escapeHTML(service.label === "" ? "" : ({ healthy: "Disponible", degraded: "Degradado", unavailable: "No responde", unknown: "Sin verificar" }[service.state]))}</span></div><p>${escapeHTML(service.detail)}</p><small>${escapeHTML(service.info)}${service.latency === null ? "" : ` · ${service.latency} ms`}</small></article>`).join("");
  document.getElementById("last-refresh").textContent = `Última consulta: ${new Date().toLocaleTimeString("es-PE")}`;
  await Promise.all([loadMetrics(), loadAudit(), loadJournal()]);
}

document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("[data-view]").forEach((button) => button.addEventListener("click", () => setView(button.dataset.view)));
  document.querySelectorAll("[data-scroll-events]").forEach((link) => link.addEventListener("click", (event) => { event.preventDefault(); document.getElementById("eventos").scrollIntoView({ behavior: "smooth" }); }));
  document.getElementById("auth-open").addEventListener("click", () => openAuthDialog("login", true));
  document.getElementById("auth-logout").addEventListener("click", logout);
  document.getElementById("auth-form").addEventListener("submit", submitAuth);
  document.querySelectorAll("[data-auth-mode]").forEach((button) => button.addEventListener("click", () => {
    if (!button.hidden && !CURRENT_USER) selectAuthMode(button.dataset.authMode);
  }));
  selectAuthMode("login");
  document.getElementById("purchase-form").addEventListener("submit", submitPurchase);
  document.getElementById("refresh-events").addEventListener("click", loadEvents);
  document.getElementById("admin-refresh-events").addEventListener("click", loadAdminEvents);
  document.getElementById("admin-refresh-sales").addEventListener("click", loadAdminSales);
  document.getElementById("admin-refresh-users").addEventListener("click", loadAdminUsers);
  document.getElementById("refresh-observability").addEventListener("click", refreshObservability);
  document.getElementById("refresh-audit").addEventListener("click", loadAudit);
  document.getElementById("refresh-journal").addEventListener("click", loadJournal);
  loadEvents();
  restoreSession();
  setInterval(() => { if (document.getElementById("view-observabilidad").classList.contains("active")) refreshObservability(); }, 10000);
});
