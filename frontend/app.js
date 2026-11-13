"use strict";

let ACCESS_TOKEN = null;
let CURRENT_USER = null;
let REFRESH_PROMISE = null;
let SESSION_REVISION = 0;
let CURRENT_VIEW = "comprar";
let PENDING_VIEW = null;
const PURCHASE_TIMEOUT_MS = 30000;

function escapeHTML(value) {
  return String(value ?? "").replace(/[&<>"']/g, char => ({"&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"})[char]);
}

function showNotice(element, message, kind = "") {
  if (!element) return;
  element.className = `notice${kind ? ` notice-${kind}` : ""}`;
  element.textContent = message;
}

function errorMessage(data, fallback) {
  const detail = data?.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) return detail.map(item => item.msg).filter(Boolean).join(" · ") || fallback;
  if (detail && typeof detail === "object") return detail.mensaje || detail.detail || (detail.recuperacion_requerida ? "La operación sigue pendiente de recuperación. Consulta su estado o reintenta la misma solicitud." : fallback);
  return fallback;
}

// Modo degradado: un fallo de una petición ya realizada solo provoca verificar /health.
// Nunca reintenta la operación comercial ni decide por una sola respuesta.
const SERVICE_DOWN = new Set();
const SERVICE_CHECKED = new Map();
let SERVICE_RECHECK = null;
const SERVICE_MESSAGES = {
  usuarios: "El inicio de sesión y el registro no están disponibles temporalmente. Si ya tenías una sesión, puede seguir funcionando mientras esté vigente.",
  eventos: "La información del evento puede estar desactualizada y no podemos confirmar la disponibilidad de este asiento. Las reservas están suspendidas por ahora.",
  compras: "El servicio de compras no está disponible temporalmente y la validación de entradas está suspendida. Tu pedido podría haberse registrado: consulta su estado antes de repetirlo.",
};

function serviceTargets() {
  return [["usuarios", CONFIG.USUARIOS_URL], ["eventos", CONFIG.EVENTOS_URL], ["compras", CONFIG.COMPRAS_LB_URL]].filter(([, base]) => base);
}

function noteServiceResponse(url, status) {
  if (status !== 0 && status < 502) return;
  const target = serviceTargets().find(([, base]) => url.startsWith(base));
  if (target) verifyService(target[0]);
}

async function verifyService(service, force = false) {
  if (!force && Date.now() - (SERVICE_CHECKED.get(service) || 0) < 20000) return;
  SERVICE_CHECKED.set(service, Date.now());
  const base = serviceTargets().find(([name]) => name === service)?.[1];
  if (!base) return;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 4000);
  let healthy = false;
  try {
    const response = await fetch(routeThroughGateway(`${base.replace(/\/$/, "")}/health`), {signal: controller.signal});
    healthy = response.ok && (await response.json())?.status === "ok";
  } catch (_) { /* Sin respuesta verificable. */ } finally { clearTimeout(timer); }
  const wasDown = SERVICE_DOWN.has(service);
  if (healthy) SERVICE_DOWN.delete(service); else SERVICE_DOWN.add(service);
  renderServiceStatus();
  if (healthy && wasDown) serviceRecovered(service);
}

function serviceRecovered(service) {
  // Actualiza la vista; los pedidos con resultado desconocido nunca se reenvían solos.
  if (service === "eventos") loadEvents().then(() => { if (SELECTED_EVENT) loadSeats(); });
  if (service === "compras") { if (!CHECKOUT_READY) detectCheckout(); if (CURRENT_VIEW === "mis-entradas") { refreshOrders(); loadMyPurchases(); } }
}

function serviceBlockMessage() {
  if (SERVICE_DOWN.has("eventos")) return "No podemos confirmar la disponibilidad de este asiento. Las reservas están suspendidas por ahora.";
  return SERVICE_DOWN.has("compras") ? "El servicio de compras no está disponible temporalmente." : "";
}

function renderServiceStatus() {
  const box = document.getElementById("service-status");
  clearTimeout(SERVICE_RECHECK);
  box.hidden = SERVICE_DOWN.size === 0;
  const lines = [...SERVICE_DOWN].map(service => SERVICE_MESSAGES[service]);
  if (SERVICE_DOWN.size === serviceTargets().length) lines.push(gatewayCandidates().length > 1 ? "Estamos buscando otra computadora del sistema que pueda atenderte." : "Si esta dirección deja de responder por completo, la página no podrá avisarte: entra por otra dirección del portal indicada por la organización.");
  document.getElementById("service-status-list").replaceChildren(...lines.map(text => Object.assign(document.createElement("li"), {textContent: text})));
  if (typeof updatePurchaseSummary === "function") updatePurchaseSummary();
  if (SERVICE_DOWN.size && !document.hidden) SERVICE_RECHECK = setTimeout(recheckDownServices, 60000);
}

function recheckDownServices() {
  [...SERVICE_DOWN].forEach(service => verifyService(service, true));
}

// Failover de entrada: con varios gateways declarados (CONFIG.GATEWAYS), si la
// computadora por la que se entró deja de responder, las solicitudes seguras
// (lecturas o con clave de idempotencia) se reintentan en otro gateway y las
// siguientes ya se dirigen allí. La página sigue funcionando sin recargarla.
// Se guarda solo en memoria: el frontend no usa almacenamiento del navegador.
let ACTIVE_GATEWAY = null;

function gatewayCandidates() {
  return Array.isArray(CONFIG.GATEWAYS) ? CONFIG.GATEWAYS.filter(host => typeof host === "string" && host) : [];
}

function routeThroughGateway(url, host = ACTIVE_GATEWAY) {
  if (!host || host === location.hostname) return url;
  try {
    const target = new URL(url);
    if (target.hostname !== location.hostname) return url;
    target.hostname = host;
    return target.toString();
  } catch (_) { return url; }
}

function safeToRetry(options) {
  const method = (options.method || "GET").toUpperCase();
  return method === "GET" || method === "HEAD" || new Headers(options.headers || {}).has("Idempotency-Key");
}

async function fetchThroughAnotherGateway(url, options, timeoutMs, retried) {
  const current = ACTIVE_GATEWAY || location.hostname;
  for (const host of gatewayCandidates().filter(candidate => candidate !== current)) {
    const previous = ACTIVE_GATEWAY;
    ACTIVE_GATEWAY = host;
    const result = await fetchJSON(url, options, timeoutMs, retried, true);
    if (result.status !== 0) {
      announceGatewaySwitch(current, host);
      return result;
    }
    ACTIVE_GATEWAY = previous;
  }
  return null;
}

function announceGatewaySwitch(from, to) {
  const box = document.getElementById("service-status");
  if (!box) return;
  box.hidden = false;
  document.getElementById("service-status-list").replaceChildren(Object.assign(document.createElement("li"), {
    textContent: `La computadora ${from} dejó de responder. Seguimos atendiéndote desde ${to}; no necesitas hacer nada.`}));
}

async function fetchJSON(url, options = {}, timeoutMs = 5000, retried = false, gatewayRetry = false) {
  const originalUrl = url;
  url = routeThroughGateway(url);
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const headers = new Headers(options.headers || {});
    const requestUserId = CURRENT_USER?.id;
    const requestToken = ACCESS_TOKEN && !headers.has("Authorization") ? ACCESS_TOKEN : null;
    if (requestToken) headers.set("Authorization", `Bearer ${requestToken}`);
    const response = await fetch(url, {...options, headers, signal: controller.signal});
    noteServiceResponse(originalUrl, response.status);
    let data = null;
    try { data = await response.json(); } catch (_) { /* Respuesta sin JSON. */ }
    if (response.status === 401 && ACCESS_TOKEN && !retried && !url.includes("/auth/")) {
      if (requestToken && requestToken !== ACCESS_TOKEN) {
        if (requestUserId === CURRENT_USER?.id) return fetchJSON(url, options, timeoutMs, true);
        return {ok: false, status: response.status, data};
      }
      const refreshed = await renewAccessToken(timeoutMs);
      if (refreshed.ok && requestUserId === CURRENT_USER?.id) return fetchJSON(url, options, timeoutMs, true);
      if (refreshed.status === 401) { clearAuthenticated(); requestLoginAfterExpiry(); }
      else showRenewalUnavailable();
      return {ok: false, status: response.status, data, error: refreshed.error || "No se pudo renovar la sesión."};
    }
    return {ok: response.ok, status: response.status, data};
  } catch (error) {
    if (!gatewayRetry && gatewayCandidates().length > 1 && safeToRetry(options)) {
      const rerouted = await fetchThroughAnotherGateway(originalUrl, options, timeoutMs, retried);
      if (rerouted) return rerouted;
    }
    noteServiceResponse(originalUrl, 0);
    return {ok: false, status: 0, error: error.name === "AbortError" ? "La solicitud agotó el tiempo de espera." : "No se pudo conectar con el servicio."};
  } finally { clearTimeout(timeoutId); }
}

async function renewAccessToken(timeoutMs = 5000) {
  if (!REFRESH_PROMISE) {
    const revision = SESSION_REVISION;
    const performRefresh = async () => {
      const result = await fetchJSON(`${CONFIG.USUARIOS_URL}/auth/refresh`, {
        method: "POST", credentials: "include", headers: {"X-CSRF-Protection": "1"},
      }, timeoutMs, true);
      if (revision !== SESSION_REVISION) return {ok: false, status: 409};
      if (result.ok && result.data?.access_token && result.data?.user) setAuthenticated(result.data);
      return result;
    };
    REFRESH_PROMISE = navigator.locks?.request ? navigator.locks.request("nexustix-auth-refresh", performRefresh) : performRefresh();
  }
  const pending = REFRESH_PROMISE;
  try { return await pending; }
  finally { if (REFRESH_PROMISE === pending) REFRESH_PROMISE = null; }
}

function showRenewalUnavailable() {
  if (CURRENT_USER) document.getElementById("auth-status").textContent = `${CURRENT_USER.nombre} · renovación pendiente`;
}

function setAuthenticated(payload) {
  const previous = CURRENT_USER;
  const changed = previous?.id !== payload.user?.id || previous?.role !== payload.user?.role;
  if (changed) { SESSION_REVISION++; clearAdminData(); clearPurchaseStatus(); }
  ACCESS_TOKEN = payload.access_token;
  CURRENT_USER = payload.user;
  document.getElementById("auth-status").textContent = `${CURRENT_USER.nombre} · ${CURRENT_USER.role}`;
  document.getElementById("auth-open").hidden = true;
  document.getElementById("auth-logout").hidden = false;
  setRegistrationAvailable(false);
  syncAccessNavigation();
  if (changed && typeof checkoutIdentityChanged === "function") checkoutIdentityChanged(previous, CURRENT_USER);
  if (changed) loadMyPurchases();
}

function syncAccessNavigation() {
  document.getElementById("nav-access").hidden = !["administrador", "organizador"].includes(CURRENT_USER?.role);
  document.getElementById("nav-admin").hidden = CURRENT_USER?.role !== "administrador";
}

function clearPurchaseStatus() {
  const result = document.getElementById("purchase-result");
  if (result) { result.textContent = ""; result.className = "result-box"; delete result.dataset.outcome; }
  if (typeof setCheckoutStep === "function") setCheckoutStep(1);
}

function clearAdminData() { if (typeof stopAccessScanner === "function") stopAccessScanner(); if (typeof clearAdminWorkspace === "function") clearAdminWorkspace(); }

function clearAuthenticated() {
  const previous = CURRENT_USER;
  SESSION_REVISION++;
  ACCESS_TOKEN = null;
  CURRENT_USER = null;
  PENDING_VIEW = null;
  document.getElementById("auth-status").textContent = "Sesión no iniciada";
  document.getElementById("auth-open").hidden = false;
  document.getElementById("auth-logout").hidden = true;
  setRegistrationAvailable(true);
  document.getElementById("my-purchases").textContent = "Inicia sesión para consultar tu historial.";
  clearPurchaseStatus(); clearAdminData(); syncAccessNavigation();
  if (typeof checkoutIdentityChanged === "function") checkoutIdentityChanged(previous, null);
  if (["admin", "control-acceso"].includes(CURRENT_VIEW)) setView("comprar");
}

function setRegistrationAvailable(available) {
  const tab = document.querySelector('[data-auth-mode="register"]');
  if (tab) tab.hidden = !available;
}

function openAuthDialog(mode = "login", allowRegistration = !CURRENT_USER, message = "") {
  setRegistrationAvailable(allowRegistration);
  selectAuthMode(mode);
  showNotice(document.getElementById("auth-result"), message, message ? "warning" : "");
  const dialog = document.getElementById("auth-dialog");
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
  document.querySelectorAll("[data-auth-mode]").forEach(tab => tab.classList.toggle("active", tab.dataset.authMode === mode));
}

function requestLoginAfterExpiry() { openAuthDialog("login", false, "La sesión expiró. Inicia sesión de nuevo para continuar."); }

async function restoreSession() {
  const result = await renewAccessToken();
  if (!result.ok && result.status === 401 && result.data?.detail && result.data.detail !== "Sesión de actualización no disponible") requestLoginAfterExpiry();
}

async function submitAuth(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const button = form.querySelector('[type="submit"]');
  if (button.disabled) return;
  const mode = document.getElementById("auth-dialog").dataset.mode || "login";
  const payload = {email: form.elements.email.value.trim(), password: form.elements.password.value};
  if (mode === "register") payload.nombre = form.elements.nombre.value.trim();
  button.disabled = true;
  if (REFRESH_PROMISE) await REFRESH_PROMISE;
  const result = await fetchJSON(`${CONFIG.USUARIOS_URL}/auth/${mode === "register" ? "register" : "login"}`, {
    method: "POST", credentials: "include", headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload),
  });
  button.disabled = false;
  if (!result.ok || !result.data?.access_token || !result.data?.user) {
    showNotice(document.getElementById("auth-result"), result.error || errorMessage(result.data, `No se pudo autenticar (HTTP ${result.status}).`), "error"); return;
  }
  setAuthenticated(result.data); form.reset();
  document.getElementById("auth-dialog").close();
  showNotice(document.getElementById("purchase-result"), "Sesión iniciada. Revisa tu selección para confirmar.", "success");
  if (PENDING_VIEW) { const next = PENDING_VIEW; PENDING_VIEW = null; setView(next); }
}

async function logout() {
  if (REFRESH_PROMISE) await REFRESH_PROMISE;
  const response = await fetchJSON(`${CONFIG.USUARIOS_URL}/auth/logout`, {method: "POST", credentials: "include", headers: {"X-CSRF-Protection": "1"}});
  if (response.ok) clearAuthenticated();
  else showNotice(document.getElementById("global-notice"), response.error || "No se pudo cerrar la sesión. Inténtalo de nuevo.", "error");
}

function setView(name, updateHash = true) {
  const adminSection = name === "observabilidad" ? "observability" : (name.startsWith("admin/") ? name.split("/")[1] : "summary");
  if (name === "observabilidad" || name.startsWith("admin/")) name = "admin";
  if (name === "admin" && CURRENT_USER?.role !== "administrador") {
    PENDING_VIEW = `admin/${adminSection}`;
    openAuthDialog("login", false, "La administración requiere una cuenta administradora autorizada."); return;
  }
  if (name === "control-acceso" && !["administrador", "organizador"].includes(CURRENT_USER?.role)) {
    PENDING_VIEW = name;
    openAuthDialog("login", false, "El control de acceso requiere una cuenta organizadora o administradora."); return;
  }
  if (!["comprar", "evento", "mis-entradas", "admin", "entrada", "control-acceso"].includes(name)) name = "comprar";
  if (name !== "control-acceso" && typeof stopAccessScanner === "function") stopAccessScanner();
  CURRENT_VIEW = name;
  document.querySelectorAll(".view").forEach(view => view.classList.toggle("active", view.id === `view-${name}`));
  document.querySelectorAll(".nav-link").forEach(button => button.classList.toggle("active", button.dataset.view === (name === "evento" ? "comprar" : name)));
  if (updateHash && !["evento", "entrada"].includes(name)) history.pushState(null, "", `#${name === "admin" ? `admin/${adminSection}` : name}`);
  if (name === "admin") openAdminSection(adminSection);
  if (name === "mis-entradas") { refreshOrders(); loadMyPurchases(); }
  if (name === "control-acceso") prepareAccessView();
  window.scrollTo({top: 0, behavior: "instant"});
}

async function loadMyPurchases() {
  const target = document.getElementById("my-purchases");
  const revision = SESSION_REVISION;
  if (!CURRENT_USER) { target.textContent = "Inicia sesión para consultar tu historial."; return; }
  const result = await fetchJSON(`${CONFIG.COMPRAS_LB_URL}/compras/mias`);
  if (revision !== SESSION_REVISION) return;
  if (!result.ok || !Array.isArray(result.data)) { target.textContent = result.error || `Historial no disponible (HTTP ${result.status}).`; return; }
  target.innerHTML = result.data.length ? result.data.slice(0, 100).map(purchase => `<div class="admin-row"><div><strong>Compra #${escapeHTML(purchase.id)} · ${escapeHTML(eventName(purchase.evento_id))}</strong><small>${escapeHTML(purchase.cantidad)} entrada(s) · ${escapeHTML(purchase.estado)}</small></div></div>`).join("") : "Aún no tienes compras.";
}

document.addEventListener("DOMContentLoaded", async () => {
  document.querySelectorAll("[data-view]").forEach(button => button.addEventListener("click", () => setView(button.dataset.view)));
  document.querySelectorAll("[data-scroll-events]").forEach(link => link.addEventListener("click", event => { event.preventDefault(); setView("comprar"); document.getElementById("eventos").scrollIntoView({behavior: "smooth"}); }));
  document.getElementById("auth-open").addEventListener("click", () => openAuthDialog());
  document.getElementById("auth-logout").addEventListener("click", logout);
  document.getElementById("auth-form").addEventListener("submit", submitAuth);
  document.querySelectorAll("[data-auth-mode]").forEach(button => button.addEventListener("click", () => { if (!button.hidden && !CURRENT_USER) selectAuthMode(button.dataset.authMode); }));
  selectAuthMode("login"); initializeCatalog(); initializeCheckout(); initializeAdmin(); initializeAccess();
  await Promise.all([loadEvents(), restoreSession(), detectCheckout()]);
  routeLocation();
  window.addEventListener("hashchange", routeLocation);
  document.getElementById("service-status-check").addEventListener("click", recheckDownServices);
  document.addEventListener("visibilitychange", () => { if (!document.hidden && SERVICE_DOWN.size) recheckDownServices(); });
});
