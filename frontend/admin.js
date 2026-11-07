"use strict";

// The server checks the administrator role independently on every operation.
// A session generation also prevents a late response repopulating a logged-out UI.
let ADMIN_GENERATION = 0;
let ADMIN_SECTION = "summary";
let ADMIN_INITIALIZED = false;
let ADMIN_EVENTS = [];
let ADMIN_SCENARIOS = [];
let ADMIN_SNAPSHOT = null;
let ADMIN_SNAPSHOT_REQUEST = 0;
let ADMIN_REPORT = null;
const ADMIN_INITIAL_CAPTURES = new Map();
const ADMIN_PAGES = new Map();
const ADMIN_PAGE_REQUESTS = new Map();
const ADMIN_REFRESHES = new Set();
const ADMIN_API = () => `${CONFIG.COMPRAS_LB_URL}/admin/operaciones`;
const adminAllowed = () => CURRENT_USER?.role === "administrador";
const adminNode = id => document.getElementById(id);
const adminText = value => escapeHTML(value ?? "No disponible");
const adminList = value => Array.isArray(value) ? value : [];
const adminDate = value => value && Number.isFinite(Date.parse(value)) ? new Date(value).toLocaleString("es-PE") : "No registrado";
const adminMoney = value => Number.isFinite(Number(value)) && value !== null && value !== undefined ? `S/ ${(Number(value) / 100).toFixed(2)}` : "No disponible";

function adminIdentity() { return `${ADMIN_GENERATION}:${CURRENT_USER?.id}:${CURRENT_USER?.role}`; }
async function adminRequest(url, options = {}, timeout = 20000) {
  if (!adminAllowed()) return {ok: false, stale: true};
  const identity = adminIdentity();
  const result = await fetchJSON(url, options, timeout);
  return identity === adminIdentity() && adminAllowed() ? result : {ok: false, stale: true};
}
function adminError(result, fallback = "No se pudo consultar esta información.") {
  return result.error || errorMessage(result.data, `${fallback}${result.status ? ` (HTTP ${result.status})` : ""}`);
}
function adminNotice(id, text, kind = "") { showNotice(adminNode(id), text, kind); }
function adminEmpty(text) { return `<p class="empty-state">${adminText(text)}</p>`; }
function adminFacts(rows) { return `<dl class="admin-facts">${rows.map(([label, value]) => `<dt>${adminText(label)}</dt><dd>${adminText(value)}</dd>`).join("")}</dl>`; }
function adminState(value) { return `<span class="admin-result-state">${adminText(value)}</span>`; }
function adminTable(rows, columns, empty = "No hay registros.") {
  if (!rows.length) return adminEmpty(empty);
  return `<table class="data-table"><thead><tr>${columns.map(([title]) => `<th scope="col">${adminText(title)}</th>`).join("")}</tr></thead><tbody>${rows.map(row => `<tr>${columns.map(([, value]) => `<td>${adminText(typeof value === "function" ? value(row) : row[value])}</td>`).join("")}</tr>`).join("")}</tbody></table>`;
}
async function adminBusy(button, operation) {
  if (button?.disabled || !adminAllowed()) return;
  if (button) button.disabled = true;
  try { await operation(); }
  catch (_) { adminNode("admin-global-notice").hidden = false; adminNotice("admin-global-notice", "La operación no pudo completarse. Consulta el estado antes de reintentar.", "error"); }
  finally { if (button) button.disabled = false; }
}

function clearAdminWorkspace() {
  ADMIN_GENERATION += 1;
  ADMIN_EVENTS = []; ADMIN_SCENARIOS = []; ADMIN_SNAPSHOT = null; ADMIN_REPORT = null;
  ADMIN_INITIAL_CAPTURES.clear(); ADMIN_PAGES.clear(); ADMIN_REFRESHES.clear();
  ADMIN_PAGE_REQUESTS.clear(); ADMIN_SNAPSHOT_REQUEST++;
  document.querySelectorAll("#view-admin [data-admin-panel]").forEach(panel => { panel.hidden = panel.dataset.adminPanel !== "summary"; });
  ["admin-events", "admin-sales", "admin-users", "admin-summary", "admin-orders", "admin-tickets", "admin-refunds", "admin-cancellations", "service-grid", "infrastructure-map", "database-cards", "replica-metrics", "audit-result", "pending-status", "journal-list", "trace-result", "lab-status", "lab-evidence", "lab-scenario-detail", "incident-list", "evidence-list", "distributed-health", "admin-limitations", "evidence-report", "node-infrastructure", "node-database-assignments"].forEach(id => {
    if (adminNode(id)) adminNode(id).textContent = "Información restringida a administradores.";
  });
  ["event-result", "access-result", "role-result", "lab-report-status", "evidence-import-status", "admin-global-notice", "incident-scope", "service-check-time", "last-refresh"].forEach(id => { if (adminNode(id)) adminNode(id).textContent = ""; });
  ["event-editor", "access-form", "role-form", "trace-form", "lab-report-form", "evidence-import-form"].forEach(id => adminNode(id)?.reset());
  if (adminNode("event-editor")) resetEventEditor();
  if (adminNode("lab-scenario")) adminNode("lab-scenario").innerHTML = '<option value="">Sin consultar</option>';
  if (adminNode("evidence-report")) adminNode("evidence-report").hidden = true;
  if (adminNode("lab-initial-status")) adminNode("lab-initial-status").textContent = "Sin captura inicial en esta sesión.";
  if (adminNode("journal-count")) adminNode("journal-count").textContent = "—";
  document.body.classList.remove("admin-print");
}

function openAdminSection(section = "summary") {
  if (!adminAllowed()) { clearAdminWorkspace(); return; }
  if (!document.querySelector(`[data-admin-panel="${CSS.escape(section)}"]`)) section = "summary";
  ADMIN_SECTION = section;
  if (CURRENT_VIEW === "admin") history.replaceState(null, "", `#admin/${section}`);
  document.querySelectorAll("[data-admin-section]").forEach(button => {
    const active = button.dataset.adminSection === section;
    button.classList.toggle("active", active);
    if (active) button.setAttribute("aria-current", "page"); else button.removeAttribute("aria-current");
  });
  document.querySelectorAll("[data-admin-panel]").forEach(panel => { panel.hidden = panel.dataset.adminPanel !== section; });
  adminNode("admin-session-note").textContent = "Sesión administradora · acceso verificado por cada servicio";
  const loaders = {summary: loadAdminSummary, events: loadAdminEvents, sales: loadAdminSales, refunds: loadAdminRefunds, users: loadAdminUsers, services: refreshAdminSnapshot, databases: refreshAdminSnapshot, observability: refreshObservability, lab: refreshLab, incidents: loadAdminIncidents, evidence: loadAdminEvidence};
  loaders[section]?.();
}

async function adminPaged(id, path, columns, empty) {
  const target = adminNode(id), offset = ADMIN_PAGES.get(id) || 0;
  const request = (ADMIN_PAGE_REQUESTS.get(id) || 0) + 1;
  ADMIN_PAGE_REQUESTS.set(id, request);
  const separator = path.includes("?") ? "&" : "?";
  target.textContent = "Consultando…";
  const result = await adminRequest(`${CONFIG.COMPRAS_LB_URL}${path}${separator}limit=50&offset=${offset}`);
  if (result.stale || ADMIN_PAGE_REQUESTS.get(id) !== request) return;
  if (!result.ok) { target.textContent = adminError(result); return; }
  const rows = adminList(result.data?.items), total = Number(result.data?.total ?? rows.length);
  target.innerHTML = adminTable(rows, columns, empty) + `<div class="admin-row-actions"><span class="field-help">${total ? offset + 1 : 0}–${Math.min(offset + rows.length, total)} de ${adminText(total)}</span><button class="button button-outline" data-page="previous" ${offset === 0 ? "disabled" : ""}>Anterior</button><button class="button button-outline" data-page="next" ${offset + rows.length >= total ? "disabled" : ""}>Siguiente</button></div>`;
  target.querySelectorAll("[data-page]").forEach(button => button.addEventListener("click", () => {
    ADMIN_PAGES.set(id, Math.max(0, offset + (button.dataset.page === "next" ? 50 : -50)));
    adminPaged(id, path, columns, empty);
  }));
}

async function loadAdminSummary() {
  if (!adminAllowed()) return;
  const identity = adminIdentity();
  adminNode("admin-summary").textContent = "Consultando cifras reales…";
  const [snapshot, sales, refunds, cancellations] = await Promise.all([
    refreshAdminSnapshot(), adminRequest(`${CONFIG.COMPRAS_LB_URL}/admin/ventas?limit=1`),
    adminRequest(`${CONFIG.COMPRAS_LB_URL}/admin/reembolsos?limit=1`), adminRequest(`${CONFIG.COMPRAS_LB_URL}/admin/cancelaciones?limit=1`),
  ]);
  if (identity !== adminIdentity() || !adminAllowed()) return;
  const components = adminList(snapshot?.componentes);
  const healthy = components.filter(component => ["saludable", "healthy", "ok"].includes(component.estado)).length;
  const stats = [["Componentes saludables", snapshot ? `${healthy}/${components.length}` : "No disponible", "Última comprobación HTTP"], ["Compras registradas", sales.ok ? sales.data.total : "No disponible", "Incluye estados del historial"], ["Reembolsos", refunds.ok ? refunds.data.total : "No disponible", "Registros de compensación"], ["Cancelaciones", cancellations.ok ? cancellations.data.total : "No disponible", "Solicitudes persistentes"]];
  adminNode("admin-summary").innerHTML = stats.map(([label, value, note]) => `<div class="admin-stat"><span>${adminText(label)}</span><strong>${adminText(value)}</strong><small>${adminText(note)}</small></div>`).join("");
}

async function loadAdminEvents() {
  if (!adminAllowed()) return;
  const target = adminNode("admin-events"); target.textContent = "Consultando catálogo…";
  const result = await adminRequest(`${CONFIG.EVENTOS_URL}/eventos`);
  if (result.stale) return;
  if (!result.ok || !Array.isArray(result.data)) { target.textContent = adminError(result); return; }
  ADMIN_EVENTS = result.data;
  target.innerHTML = ADMIN_EVENTS.map(event => `<div class="admin-row"><div><strong>${adminText(event.nombre)} · #${adminText(event.id)}</strong><small>${adminText(event.stock)} disponibles · ${adminMoney(event.precio_centimos)} · ${event.activo ? "Ventas habilitadas" : "Cancelado"}</small><small>${adminText(adminDate(event.fecha))} · ${adminText(event.lugar || "Lugar por confirmar")}</small></div><div class="admin-row-actions"><button class="button button-outline" data-edit-event="${adminText(event.id)}">Editar</button><button class="button button-outline" data-cancel-event="${adminText(event.id)}">${event.activo ? "Cancelar evento" : "Ver compensaciones"}</button>${event.activo ? `<button class="button button-outline" data-delete-event="${adminText(event.id)}">Eliminar sin referencias</button>` : ""}</div></div>`).join("") || adminEmpty("No hay eventos registrados.");
  target.querySelectorAll("[data-edit-event]").forEach(button => button.addEventListener("click", () => editAdminEvent(Number(button.dataset.editEvent))));
  target.querySelectorAll("[data-cancel-event]").forEach(button => button.addEventListener("click", () => adminBusy(button, () => cancelEvent(Number(button.dataset.cancelEvent)))));
  target.querySelectorAll("[data-delete-event]").forEach(button => button.addEventListener("click", () => adminBusy(button, () => deleteAdminEvent(Number(button.dataset.deleteEvent)))));
}

function resetEventEditor() {
  const form = adminNode("event-editor"); form.reset(); form.elements.evento_id.value = "";
  form.elements.capacidad.disabled = false; form.elements.asientos.disabled = false;
  adminNode("event-editor-title").textContent = "Crear evento";
}
function editAdminEvent(id) {
  const item = ADMIN_EVENTS.find(event => Number(event.id) === id); if (!item) return;
  const form = adminNode("event-editor"); form.reset();
  for (const key of ["nombre", "descripcion", "lugar", "categoria", "imagen_url"]) form.elements[key].value = item[key] || "";
  form.elements.evento_id.value = id; form.elements.capacidad.value = item.stock_inicial;
  form.elements.precio.value = (Number(item.precio_centimos) / 100).toFixed(2);
  if (item.fecha) {
    const date = new Date(item.fecha);
    if (Number.isFinite(date.getTime())) form.elements.fecha.value = new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
  }
  form.elements.capacidad.disabled = true; form.elements.asientos.disabled = true;
  adminNode("event-editor-title").textContent = `Editar evento #${id}`;
  form.scrollIntoView({behavior: "smooth", block: "center"}); form.elements.nombre.focus({preventScroll: true});
}
async function submitAdminEvent(event) {
  event.preventDefault(); const form = event.currentTarget;
  await adminBusy(form.querySelector('[type="submit"]'), async () => {
    const data = new FormData(form), id = Number(data.get("evento_id"));
    const date = new Date(data.get("fecha"));
    const price = Math.round(Number(data.get("precio")) * 100);
    if (!Number.isFinite(date.getTime()) || !Number.isInteger(price) || price < 0 || price > 1000000) { adminNotice("event-result", "Revisa la fecha y el precio.", "error"); return; }
    const payload = {nombre: String(data.get("nombre")).trim(), descripcion: String(data.get("descripcion") || ""), lugar: String(data.get("lugar") || ""), fecha: date.toISOString(), precio_centimos: price, categoria: String(data.get("categoria") || ""), imagen_url: String(data.get("imagen_url") || "")};
    if (id) {
      const original = ADMIN_EVENTS.find(item => Number(item.id) === id);
      if (!original) { adminNotice("event-result", "Actualiza el catálogo y selecciona de nuevo el evento.", "warning"); return; }
      payload.activo = original.activo;
    } else {
      payload.capacidad = Number(data.get("capacidad"));
      payload.asientos = String(data.get("asientos") || "").trim().split("\n").filter(Boolean).map(line => {
        const [codigo, zona, precio, ...extra] = line.split(",").map(value => value.trim());
        return {codigo, zona, precio_centimos: extra.length || !precio ? NaN : Math.round(Number(precio) * 100)};
      });
      if (payload.asientos.some(seat => !seat.codigo || !seat.zona || !Number.isInteger(seat.precio_centimos) || seat.precio_centimos < 0)) { adminNotice("event-result", "Revisa los asientos: código,zona,precio. Cada fila debe contener tres valores válidos.", "error"); return; }
    }
    const result = await adminRequest(`${CONFIG.EVENTOS_URL}/eventos${id ? `/${id}` : ""}`, {method: id ? "PUT" : "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload)});
    if (result.stale) return;
    adminNotice("event-result", result.ok ? `Evento #${result.data.id} guardado.` : adminError(result, "No se pudo guardar el evento."), result.ok ? "success" : "error");
    if (result.ok) { resetEventEditor(); await Promise.all([loadAdminEvents(), loadEvents()]); }
  });
}
async function deleteAdminEvent(id) {
  if (!confirm(`¿Eliminar el evento #${id}? El servidor solo lo permitirá si no hay ventas ni referencias que deban conservarse.`)) return;
  const result = await adminRequest(`${CONFIG.EVENTOS_URL}/eventos/${id}`, {method: "DELETE"});
  if (result.stale) return;
  adminNode("admin-global-notice").hidden = false;
  adminNotice("admin-global-notice", result.ok ? `Evento #${id} eliminado.` : adminError(result), result.ok ? "success" : "error");
  if (result.ok) await Promise.all([loadAdminEvents(), loadEvents()]);
}
async function cancelEvent(id, retryReason = null) {
  const item = ADMIN_EVENTS.find(event => Number(event.id) === id);
  if (item && !item.activo && !retryReason) { openAdminSection("refunds"); return; }
  const reason = retryReason || prompt("Motivo de cancelación (se conserva en el historial):");
  if (!reason?.trim()) return;
  if (!retryReason && !confirm(`¿Cancelar el evento #${id}? Se cerrarán las ventas y se procesarán las compensaciones. El historial se conserva.`)) return;
  const result = await adminRequest(`${CONFIG.COMPRAS_LB_URL}/eventos/${id}/cancelar?motivo=${encodeURIComponent(reason.trim())}`, {method: "POST"}, 30000);
  if (result.stale) return;
  adminNode("admin-global-notice").hidden = false;
  adminNotice("admin-global-notice", result.ok ? `Cancelación del evento #${id}: ${result.data.estado}. Consulta el progreso en Reembolsos.` : `${adminError(result)} Consulta el progreso antes de reintentar; el cierre puede haberse registrado.`, result.ok ? "success" : "warning");
  await Promise.all([loadAdminEvents(), loadEvents(), loadAdminRefunds()]);
}

async function loadAdminSales() {
  if (!adminAllowed()) return;
  const eventId = Number(adminNode("admin-sales-event").value);
  const query = Number.isInteger(eventId) && eventId > 0 ? `?evento_id=${eventId}` : "";
  await Promise.all([
    adminPaged("admin-sales", `/admin/ventas${query}`, [["Compra", "id"], ["Evento", "evento_id"], ["Cantidad", "cantidad"], ["Estado", "estado"], ["Instancia", "instancia"], ["Pedido", "orden_id"], ["Fecha", row => adminDate(row.creado_en)]], "No hay compras para este filtro."),
    adminPaged("admin-orders", `/admin/ordenes${query}`, [["Pedido", "id"], ["Estado", "estado"], ["Pago simulado", "pago"], ["Total", row => adminMoney(row.total_centimos)], ["Reembolsado", row => adminMoney(row.reembolsado_centimos)], ["Fecha", row => adminDate(row.creado_en)]], "No hay pedidos para este filtro."),
    adminPaged("admin-tickets", `/admin/entradas${query}`, [["Pedido", "orden_id"], ["Compra", "compra_id"], ["Evento", "evento_id"], ["Asiento", row => row.asiento || "General"], ["Estado", "estado"], ["Uso", row => adminDate(row.usado_en)]], "No hay entradas para este filtro."),
  ]);
}
async function loadAdminRefunds() {
  if (!adminAllowed()) return;
  const identity = adminIdentity();
  const result = await adminRequest(`${CONFIG.COMPRAS_LB_URL}/admin/cancelaciones?limit=100&offset=0`);
  if (result.stale || identity !== adminIdentity()) return;
  const target = adminNode("admin-cancellations");
  if (!result.ok) target.textContent = adminError(result);
  else {
    const rows = adminList(result.data?.items);
    target.innerHTML = rows.map(item => `<article class="admin-row"><div><strong>Evento #${adminText(item.evento_id)} · ${adminState(item.estado)}</strong><p>${adminText(item.motivo)}</p><small>Solicitada: ${adminText(adminDate(item.creado_en))} · Responsable: ${adminText(item.solicitada_por)}</small><small>Cierre de ventas ${item.ventas_cerradas ? "confirmado" : "pendiente de confirmar"} · ${adminText(item.legacy_pendientes ?? 0)} operaciones legacy por conciliar</small><small>${adminText(item.completadas)} completadas · ${adminText(item.pendientes)} pendientes · ${adminText(item.errores)} con error / ${adminText(item.total_ordenes)} operaciones</small><progress max="${Math.max(1, Number(item.total_ordenes) || 1)}" value="${Math.max(0, Number(item.completadas) || 0)}" aria-label="Compensaciones completadas"></progress><small>Actualizado: ${adminText(adminDate(item.actualizado_en))}</small>${item.ultimo_error ? `<p class="notice notice-warning">${adminText(item.ultimo_error)}</p>` : ""}</div>${item.estado !== "completado" ? `<button class="button button-outline" data-retry-cancellation="${adminText(item.evento_id)}">Solicitar reintento</button>` : ""}</article>`).join("") || adminEmpty("No hay cancelaciones registradas.");
    if (result.data.total > rows.length) target.insertAdjacentHTML("beforeend", `<p class="field-help">Se muestran las primeras ${rows.length} de ${adminText(result.data.total)} cancelaciones.</p>`);
    target.querySelectorAll("[data-retry-cancellation]").forEach(button => button.addEventListener("click", () => {
      const item = rows.find(row => String(row.evento_id) === button.dataset.retryCancellation);
      adminBusy(button, () => cancelEvent(Number(item.evento_id), item.motivo));
    }));
  }
  await adminPaged("admin-refunds", "/admin/reembolsos", [["Evento", "evento_id"], ["Pedido", "orden_id"], ["Tipo", "tipo"], ["Estado", "estado"], ["Monto", row => ["requiere_conciliacion", "verificacion_pendiente"].includes(row.tipo) ? "Por verificar" : adminMoney(row.total_centimos)], ["Intentos", "intentos"], ["Error recuperable", row => row.ultimo_error || "—"], ["Actualizado", row => adminDate(row.actualizado_en)]], "No hay reembolsos registrados.");
}
async function loadAdminUsers() {
  if (!adminAllowed()) return;
  const result = await adminRequest(`${CONFIG.USUARIOS_URL}/usuarios`);
  if (result.stale) return;
  adminNode("admin-users").innerHTML = result.ok && Array.isArray(result.data) ? adminTable(result.data, [["ID", "id"], ["Nombre", "nombre"], ["Rol", "role"]], "No hay usuarios registrados.") : adminEmpty(adminError(result));
}

function renderAdminComponents(components, compact = false) {
  return adminList(components).map(component => {
    const states = {saludable: "healthy", healthy: "healthy", ok: "healthy", degradado: "degraded", degraded: "degraded", caido: "unavailable", unavailable: "unavailable"};
    const state = states[component.estado] || "unknown";
    const traffic = component.solicitud_observada;
    return `<article class="service-card state-${state}"><div class="service-card-head"><span class="state-indicator"></span><strong>${adminText(component.nombre || component.id)}</strong>${adminState(component.estado || "desconocido")}</div><p>${adminText(component.tipo || "Componente")} · ${adminText(component.nodo || "Nodo no informado")}</p><small>Instancia: ${adminText(component.instancia || "No informada")}</small><small>Comprobado: ${adminText(adminDate(component.comprobado_en))} · ${component.latencia_ms === null || component.latencia_ms === undefined ? "Latencia no disponible" : `${adminText(component.latencia_ms)} ms`}</small>${traffic ? `<p>Lectura por esta ruta: ${adminText(traffic.estado)}</p><small>Atendió: ${adminText(traffic.instancia || "Instancia no informada")} · Backend: ${adminText(traffic.backend || "No informado")}</small>` : ""}${compact ? "" : `<small>Base de datos: ${adminText(typeof component.base_datos === "object" ? JSON.stringify(component.base_datos) : component.base_datos || "No informada")}</small><small>${adminText(component.detalle || "Sin diagnóstico adicional")}</small>`}</article>`;
  }).join("") || adminEmpty("No se recibieron componentes configurados.");
}
function renderNodeInfrastructure(infrastructure) {
  const container = adminNode("node-infrastructure"), databases = adminNode("node-database-assignments");
  adminNode("legacy-lan-warning").hidden = Boolean(infrastructure);
  databases.innerHTML = "";
  if (infrastructure?.modo !== "inventario_nodos") {
    container.innerHTML = adminEmpty(infrastructure ? "El inventario configurado no pudo verificarse. No se infieren nodos ni garantías de disponibilidad." : "Este entorno usa la topología anterior. El inventario configurable se habilita con despliegue-nodos; sus entornos actuales no cambian.");
    return;
  }
  const list = value => adminList(value).join(" · ") || "Ninguno";
  const access = points => adminList(points).map(point => {
    let href = "";
    try {
      const parsed = new URL(point.url);
      if (["http:", "https:"].includes(parsed.protocol) && !parsed.username && !parsed.password) href = parsed.href;
    } catch (_) { /* No convertir destinos inválidos en enlaces. */ }
    const label = `${point.node} · ${point.preferred ? "preferido" : "alternativo"}`;
    return href ? `<a href="${adminText(href)}" target="_blank" rel="noopener noreferrer">${adminText(label)}: ${adminText(href)}</a>` : adminText(label);
  }).join("<br>") || "Sin gateway restante configurado";
  const nodeRows = adminList(infrastructure.nodos);
  const projections = adminList(infrastructure.proyecciones);
  container.innerHTML = `<article class="workspace-card"><h3>Inventario: ${adminText(infrastructure.despliegue)}</h3>
    <p>Perfil declarado: ${adminText(infrastructure.perfil)}. HA física no verificada; promoción de PostgreSQL manual y coordinada.</p>
    <p class="field-help">${adminText(infrastructure.alcance)}</p>
    <div class="table-scroll">${adminTable(nodeRows, [["Nodo / dirección", n => `${n.id} · ${n.address}`], ["Plataforma / dominio físico declarado", n => `${n.platform} · ${n.failure_domain}`], ["Servicios", n => list(n.roles)], ["Bases asignadas", n => adminList(n.bases).map(b => `${b.base}: ${b.rol_configurado}`).join(" · ") || "Ninguna"], ["Observación HTTP", n => `${n.estado_observacion} (${n.respuestas_http}/${n.sondas})`], ["Presupuesto declarado", n => `${n.resources?.cpus} CPU · ${n.resources?.memory_mb} MiB`], ["Límites de contenedores", n => `${n.reserved_resources?.cpus ?? "—"} CPU · ${n.reserved_resources?.memory_mb ?? "—"} MiB`]])}</div>
    <p class="field-help">No se mide el estado físico ni la carga del host. Un nodo dedicado a PostgreSQL puede no tener sonda HTTP directa.</p>
    <h4>Acceso redundante explícito</h4><p>${access(infrastructure.accesos)}</p><p class="field-help">${adminText(infrastructure.cambio_acceso)}</p>
    <h4>Dependencias críticas y puntos únicos de fallo</h4><div class="table-scroll">${adminTable(adminList(infrastructure.dependencias_criticas), [["Componente", "componente"], ["Nodos", row => list(row.nodos)], ["Dependencia", "motivo"]])}</div></article>
    <article class="workspace-card"><h3>Si se pierde un nodo o dominio físico</h3><p class="field-help">Proyección estática del inventario, no una simulación ni un fallo ejecutado. Las instancias restantes necesitan red, almacenamiento y capacidad suficientes; las saludables se observaron antes de esa pérdida hipotética.</p>
    ${projections.map(projection => `<details class="node-projection"><summary>${adminText(projection.tipo === "nodo" ? "Nodo" : "Dominio físico declarado")} ${adminText(projection.objetivo)}</summary>
      <p>Excluidos: ${adminText(list(projection.nodos_excluidos))}</p>
      <div class="table-scroll">${adminTable(adminList(projection.servicios), [["Servicio", "servicio"], ["Instancias restantes", row => list(row.instancias_configuradas_restantes)], ["Saludables observadas", row => list(row.observadas_saludables_ahora)], ["Continuidad", "estado_proyectado"], ["Dependencias pendientes", row => list(row.dependencias_pendientes)]])}</div>
      <div class="table-scroll">${adminTable(adminList(projection.bases), [["Base", "base"], ["Primario restante", row => row.primario_restante || "Ninguno"], ["Standbys restantes", row => list(row.standbys_restantes)], ["Escrituras", "estado_escrituras"], ["Límite", "riesgo_datos"]])}</div>
      <p>Accesos restantes:<br>${access(projection.accesos_restantes)}</p></details>`).join("")}</article>`;
  databases.innerHTML = `<article class="workspace-card"><h3>Asignación PostgreSQL del inventario</h3><div class="table-scroll">${adminTable(adminList(infrastructure.bases), [["Base", "base"], ["Primario declarado", "primario_configurado"], ["Política WAL", row => `${row.replicacion_configurada} · quórum ${row.quorum_configurado}`], ["Roles observados por API", row => list(row.roles_observados)], ["Standbys / canal observado", row => adminList(row.standbys).map(replica => `${replica.nodo}: ${replica.estado_canal || "sin verificar"} (${replica.sync_state || "sin estado síncrono"})`).join(" · ") || "Ninguno"]])}</div><p class="field-help">La ubicación del primario es declarada; el diagnóstico API no acredita en qué máquina está conectado. Un canal WAL no certifica salud física, ausencia de pérdida ni fencing.</p></article>`;
}
async function refreshAdminSnapshot() {
  if (!adminAllowed()) return null;
  const request = ++ADMIN_SNAPSHOT_REQUEST;
  const result = await adminRequest(`${ADMIN_API()}/estado`, {}, 45000);
  if (result.stale || request !== ADMIN_SNAPSHOT_REQUEST) return null;
  if (!result.ok || !result.data) {
    ADMIN_SNAPSHOT = null;
    for (const id of ["service-grid", "database-cards", "replica-metrics", "lab-status", "infrastructure-map", "node-infrastructure", "node-database-assignments"]) adminNode(id).innerHTML = adminEmpty(adminError(result, "El estado operativo no está disponible."));
    adminNode("admin-limitations").textContent = "No se pudo consultar el estado actual. No es posible afirmar disponibilidad.";
    adminNode("service-check-time").textContent = `Consulta sin respuesta: ${new Date().toLocaleString("es-PE")}`;
    adminNode("last-refresh").textContent = "Última consulta fallida; las métricas no están disponibles.";
    adminNode("distributed-health").textContent = "Sin respuesta actual del diagnóstico.";
    adminNode("lab-evidence").textContent = "Sin respuesta actual del diagnóstico.";
    return null;
  }
  ADMIN_SNAPSHOT = result.data;
  const snapshot = result.data, components = adminList(snapshot.componentes);
  renderNodeInfrastructure(snapshot.infraestructura);
  adminNode("service-grid").innerHTML = renderAdminComponents(components);
  adminNode("lab-status").innerHTML = renderAdminComponents(components, true);
  const labels = new Map(components.map(component => [component.id, component.nombre || component.id]));
  adminNode("infrastructure-map").innerHTML = components.filter(component => adminList(component.dependencias).length).map(component => `<div class="infrastructure-edge"><strong>${adminText(component.nombre || component.id)}</strong><br>↓ ${adminList(component.dependencias).map(dependency => adminText(labels.get(dependency) || dependency)).join(" · ")}</div>`).join("");
  adminNode("service-check-time").textContent = `Captura del servidor: ${adminDate(snapshot.capturado_en)}. Los estados corresponden a esa consulta.`;
  adminNode("last-refresh").textContent = `Última consulta: ${adminDate(snapshot.capturado_en)}`;
  adminNode("admin-limitations").textContent = adminList(snapshot.limitaciones).join(" · ") || "El servidor no informó limitaciones adicionales. Consulta la sección de PostgreSQL para conocer el alcance de la redundancia.";
  adminNode("lab-evidence").textContent = JSON.stringify(snapshot, null, 2);
  renderAdminDatabases(snapshot.persistencia);
  renderAdminMetrics(snapshot.metricas);
  const pending = snapshot.pendientes;
  adminNotice("pending-status", pending?.disponible ? `Pedidos: ${JSON.stringify(pending.ordenes || {})} · Outbox pendiente: ${pending.outbox_pendiente} · Reembolsos: ${JSON.stringify(pending.reembolsos || {})}` : "No se pudieron consultar las operaciones pendientes.", pending?.disponible ? "" : "warning");
  return snapshot;
}
function renderAdminDatabases(databases) {
  const rows = adminList(databases);
  adminNode("database-cards").innerHTML = rows.map(row => {
    const db = row.diagnostico || row.datos || row;
    const replicas = adminList(db.replicas);
    return `<article class="workspace-card"><h3>${adminText(row.componente || row.nombre || "PostgreSQL")}</h3>${adminState(db.disponible === true ? "Disponible" : "Sin disponibilidad comprobada")}${adminFacts([["Rol", db.rol || "Desconocido"], ["En recuperación", typeof db.en_recuperacion === "boolean" ? (db.en_recuperacion ? "Sí" : "No") : db.recuperacion ?? "No informado"], ["Conexiones", typeof db.conexiones === "object" ? JSON.stringify(db.conexiones) : db.conexiones ?? "No informado"], ["WAL / LSN", db.wal_lsn || db.lsn_actual || db.wal?.actual_lsn || "Consultar detalle"], ["Réplicas observadas", replicas.length], ["Failover / promoción", db.ultimo_failover || "Sin evidencia registrada"]])}<p class="field-help">${adminText(db.alcance || db.motivo || "Diagnóstico del nodo conectado.")}</p>${replicas.length ? adminTable(replicas, [["Réplica", "application_name"], ["Estado", "state"], ["Sincronización", "sync_state"], ["Retraso (bytes)", "lag_bytes"]]) : '<p class="field-help">No se observan réplicas activas en esta consulta. No acredita redundancia funcional.</p>'}<details><summary>WAL, checkpoints y recuperación</summary><pre>${adminText(JSON.stringify(db, null, 2))}</pre></details></article>`;
  }).join("") || adminEmpty("No hay diagnósticos PostgreSQL disponibles.");
  adminNode("distributed-health").textContent = JSON.stringify(rows, null, 2);
}
function renderAdminMetrics(metrics) {
  adminNode("replica-metrics").innerHTML = adminList(metrics).map(row => {
    const metric = row.metricas || row.datos || row;
    const latency = metric.latencia_media_ms ?? metric.latencia_promedio_ms;
    return `<article class="metric-card"><strong>${adminText(row.componente || row.instancia || "Instancia")}</strong>${adminFacts([["Solicitudes", metric.solicitudes ?? metric.peticiones_totales ?? metric.solicitudes_totales], ["Exitosas", metric.peticiones_exitosas ?? metric.exitosas], ["Errores HTTP", metric.errores_http], ["Latencia media", latency === undefined ? undefined : `${latency} ms`], ["Reintentos", metric.reintentos ?? "Sin contador registrado"], ["Desde", adminDate(metric.desde)]])}${row.error ? `<small>${adminText(row.error)}</small>` : ""}<details><summary>Métricas disponibles</summary><pre>${adminText(JSON.stringify(metric, null, 2))}</pre></details></article>`;
  }).join("") || adminEmpty("El servidor no recibió métricas de instancias configuradas.");
}
async function refreshObservability() { await refreshAdminSnapshot(); }
async function loadAudit() {
  adminNotice("audit-result", "Consultando consistencia…");
  const result = await adminRequest(`${CONFIG.COMPRAS_LB_URL}/auditoria/consistencia`, {}, 15000);
  if (result.stale) return;
  if (!result.ok) { adminNotice("audit-result", adminError(result), "error"); adminNotice("pending-status", "No se pudo consultar el estado de reservas.", "warning"); return; }
  const data = result.data, complete = data.auditoria_completa !== false;
  const consistent = data.todo_consistente === true && data.balance_global_diferencia === 0 && complete;
  adminNotice("audit-result", `${consistent ? "Consistencia comprobada en esta consulta" : complete ? "Revisar resultado de consistencia" : "Consulta parcial; consistencia global no comprobada"}. ${data.veredicto || ""} Diferencia global: ${data.balance_global_diferencia ?? "No disponible"}.`, consistent ? "success" : "warning");
  adminNotice("pending-status", `Reservas pendientes: ${data.totales?.reservas_pendientes ?? "No disponible"}.`, data.totales?.reservas_pendientes === 0 ? "success" : "warning");
}
async function loadJournal() {
  const result = await adminRequest(`${CONFIG.COMPRAS_LB_URL}/compras/journal`);
  if (result.stale) return;
  if (!result.ok) { adminNode("journal-list").textContent = adminError(result); adminNode("journal-count").textContent = "Sin consulta"; return; }
  const rows = adminList(result.data?.entradas);
  adminNode("journal-count").textContent = `${result.data?.total_entradas ?? rows.length} registros`;
  adminNode("journal-list").innerHTML = rows.slice().reverse().map(row => `<div class="journal-entry"><span class="journal-type">${adminText(row.tipo)}</span><div><strong>${adminText(row.mensaje)}</strong><small>${adminText(adminDate(row.timestamp))} · Instancia ${adminText(row.instancia)} · T=${adminText(row.reloj_t)}</small></div></div>`).join("") || adminEmpty("El journal consultado no contiene registros.");
}
async function submitAdminTrace(event) {
  event.preventDefault(); const form = event.currentTarget;
  await adminBusy(form.querySelector("button"), async () => {
    const identifier = String(new FormData(form).get("identificador")).trim();
    const result = await adminRequest(`${ADMIN_API()}/trazas/${encodeURIComponent(identifier)}`);
    if (result.stale) return;
    if (!result.ok) { adminNode("trace-result").textContent = adminError(result); return; }
    const trace = result.data;
    adminNode("trace-result").innerHTML = `<p>Operación ${adminText(trace.identificador)} · ${adminState(trace.estado)}</p>` + adminList(trace.eventos).map(entry => `<article class="admin-timeline-entry"><time>${adminText(adminDate(entry.momento))}</time><h3>${adminText(entry.etapa)}</h3><p>${adminText(entry.detalle || entry.estado)}</p><small>${adminText(entry.fuente)} · Instancia ${adminText(entry.instancia || "No registrada")}</small></article>`).join("") + `<p class="notice">Hitos sin evidencia: ${adminText(adminList(trace.faltantes).join(" · ") || "Ninguno informado")}.</p><p class="field-help">${adminText(adminList(trace.limitaciones).join(" · "))}</p>`;
  });
}

async function refreshLab() {
  const identity = adminIdentity();
  const [result] = await Promise.all([adminRequest(`${ADMIN_API()}/escenarios`), refreshAdminSnapshot()]);
  if (result.stale || identity !== adminIdentity()) return;
  if (!result.ok) { adminNode("lab-scenario-detail").textContent = adminError(result); return; }
  ADMIN_SCENARIOS = adminList(result.data?.escenarios);
  const selected = adminNode("lab-scenario").value;
  adminNode("lab-scenario").innerHTML = ADMIN_SCENARIOS.map(scenario => `<option value="${adminText(scenario.id)}">${adminText(scenario.id)} · ${adminText(scenario.nombre)}</option>`).join("");
  if (ADMIN_SCENARIOS.some(scenario => scenario.id === selected)) adminNode("lab-scenario").value = selected;
  renderLabScenario();
}
function renderLabScenario() {
  const selected = adminNode("lab-scenario").value, scenario = ADMIN_SCENARIOS.find(item => item.id === selected);
  if (!scenario) { adminNode("lab-scenario-detail").textContent = "No se recibió información del escenario."; return; }
  const value = input => Array.isArray(input) ? input.join(" · ") : input;
  adminNode("lab-scenario-detail").innerHTML = `<p class="eyebrow">ESCENARIO ${adminText(scenario.id)}</p><h3>${adminText(scenario.nombre)}</h3><p>${adminText(scenario.objetivo)}</p><div class="scenario-concepts">${adminList(scenario.conceptos).map(concept => `<span>${adminText(concept)}</span>`).join("")}</div>${adminFacts([["Componentes", value(scenario.componentes)], ["Requisitos", value(scenario.requisitos)], ["Acción manual", value(scenario.accion)], ["Resultado esperado", value(scenario.esperado)], ["Limitaciones", value(scenario.limitaciones)]])}`;
  const initial = ADMIN_INITIAL_CAPTURES.get(selected);
  adminNode("lab-initial-status").textContent = initial ? `Captura inicial: ${adminDate(initial.fecha)} · ${initial.id}` : "Sin captura inicial para este escenario en esta sesión.";
}
async function captureLabInitial() {
  const scenario = adminNode("lab-scenario").value; if (!scenario) return;
  const result = await adminRequest(`${ADMIN_API()}/evidencias`, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({escenario: scenario, fase: "inicial", acciones: "Captura de estado inicial; sin ejecutar acciones de infraestructura desde la interfaz.", observado: "Consulta inicial de los componentes configurados.", conclusion: "Pendiente de observación final."})}, 45000);
  if (result.stale) return;
  if (!result.ok) { adminNotice("lab-report-status", adminError(result), "error"); return; }
  ADMIN_INITIAL_CAPTURES.set(scenario, result.data); renderLabScenario();
  adminNode("lab-evidence").textContent = JSON.stringify(result.data.evidencias, null, 2);
  adminNotice("lab-report-status", "Captura inicial guardada. Realiza el procedimiento manual autorizado y registra lo observado.", "success");
}
async function submitLabReport(event) {
  event.preventDefault(); const form = event.currentTarget;
  await adminBusy(form.querySelector('[type="submit"]'), async () => {
    const scenario = adminNode("lab-scenario").value, initial = ADMIN_INITIAL_CAPTURES.get(scenario);
    if (!initial) { adminNotice("lab-report-status", "Captura primero el estado inicial de este escenario.", "warning"); return; }
    const data = new FormData(form);
    const result = await adminRequest(`${ADMIN_API()}/evidencias`, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({escenario: scenario, fase: "final", referencia_inicial: initial.id, acciones: String(data.get("acciones")), observado: String(data.get("observado")), conclusion: String(data.get("conclusion"))})}, 45000);
    if (result.stale) return;
    if (!result.ok) { adminNotice("lab-report-status", adminError(result), "error"); return; }
    ADMIN_REPORT = result.data;
    adminNode("lab-evidence").textContent = JSON.stringify(result.data.evidencias, null, 2);
    adminNotice("lab-report-status", `Reporte ${result.data.id} guardado. Disponible en Evidencias de demostración. El tiempo entre capturas es una ventana de observación, no un RTO certificado.`, "success");
    ADMIN_INITIAL_CAPTURES.delete(scenario); renderLabScenario();
  });
}

async function loadAdminIncidents() {
  const result = await adminRequest(`${ADMIN_API()}/incidentes`);
  if (result.stale) return;
  if (!result.ok) { adminNode("incident-list").textContent = adminError(result); return; }
  adminNode("incident-scope").textContent = result.data.alcance || "Incidentes observados entre capturas persistentes; los periodos sin captura no se monitorizan.";
  adminNode("incident-list").innerHTML = adminList(result.data.incidentes).map(item => `<article class="admin-timeline-entry"><time>${adminText(adminDate(item.detectado_en))}</time><h3>${adminText(item.componente)} · ${adminState(item.estado_actual)}</h3>${adminFacts([["Tipo de fallo", item.tipo_fallo || "No determinado"], ["Estado anterior", item.estado_anterior], ["Cambio observado", item.estado_actual], ["Instancia que continuó", item.instancia_continuadora || "Sin evidencia"], ["Recuperación", adminDate(item.recuperado_en)], ["Ventana observada", item.ventana_observada_segundos == null ? "No calculable" : `${item.ventana_observada_segundos} s (muestreo; no RTO)`]])}<p>${adminText(item.limitaciones)}</p>${adminList(item.evidencias).map(id => `<button class="button button-outline" data-open-evidence="${adminText(id)}">Ver evidencia asociada</button>`).join(" ")}</article>`).join("") || adminEmpty("No hay incidentes registrados. Las capturas de evidencia permiten registrar cambios de estado observados.");
  bindEvidenceLinks(adminNode("incident-list"));
}
async function loadAdminEvidence() {
  const result = await adminRequest(`${ADMIN_API()}/evidencias`);
  if (result.stale) return;
  if (!result.ok) { adminNode("evidence-list").textContent = adminError(result); return; }
  adminNode("evidence-list").innerHTML = adminList(result.data.evidencias).map(report => `<div class="admin-row"><div><strong>${adminText(report.nombre || `Escenario ${report.escenario}`)}</strong><small>${adminText(adminDate(report.fecha))} · ${adminText(report.fase || "Observación")} · ${adminText(report.origen)} · ${adminText(report.resultado || "Sin evaluar")}</small></div><button class="button button-outline" data-open-evidence="${adminText(report.id)}">Ver y exportar</button></div>`).join("") || adminEmpty("Todavía no hay evidencias. Captura una demostración desde el Laboratorio o importa un reporte existente.");
  bindEvidenceLinks(adminNode("evidence-list"));
}
function bindEvidenceLinks(target) { target.querySelectorAll("[data-open-evidence]").forEach(button => button.addEventListener("click", () => adminBusy(button, () => openAdminEvidence(button.dataset.openEvidence)))); }
async function openAdminEvidence(id) {
  const result = await adminRequest(`${ADMIN_API()}/evidencias/${encodeURIComponent(id)}`);
  if (result.stale) return;
  if (!result.ok) { adminNode("admin-global-notice").hidden = false; adminNotice("admin-global-notice", adminError(result), "error"); return; }
  ADMIN_REPORT = result.data; openAdminSection("evidence"); renderAdminReport();
}
function renderAdminReport() {
  const report = ADMIN_REPORT; if (!report) return;
  const node = adminNode("evidence-report"); node.hidden = false;
  node.innerHTML = `<p class="eyebrow">REPORTE DE DEMOSTRACIÓN · ${adminText(report.id)}</p><h2>${adminText(report.nombre)}</h2>${adminFacts([["Escenario", report.escenario], ["Fecha", adminDate(report.fecha)], ["Fase", report.fase], ["Origen", report.origen], ["Evaluación", report.resultado || "Sin evaluar"], ["Ventana entre capturas", report.ventana_observacion_segundos === null || report.ventana_observacion_segundos === undefined ? "No disponible" : `${report.ventana_observacion_segundos} s (no equivale a RTO)`]])}<h3>Objetivo y conceptos</h3><p>${adminText(report.objetivo)}</p><p>${adminText(adminList(report.conceptos).join(" · "))}</p><h3>Componentes</h3><p>${adminText(adminList(report.componentes).join(" · "))}</p><h3>Estado inicial</h3>${report.referencia_inicial ? `<button class="button button-outline" data-open-evidence="${adminText(report.referencia_inicial)}">Consultar captura inicial</button>` : '<p>No se ha asociado una captura inicial.</p>'}<h3>Acciones realizadas</h3><p>${adminText(report.acciones)}</p><h3>Resultado esperado</h3><p>${adminText(Array.isArray(report.esperado) ? report.esperado.join(" · ") : report.esperado)}</p><h3>Comportamiento observado</h3><p>${adminText(report.observado)}</p><h3>Limitaciones</h3><p>${adminText(Array.isArray(report.limitaciones) ? report.limitaciones.join(" · ") : report.limitaciones)}</p><h3>Conclusión del autor</h3><p>${adminText(report.conclusion)}</p><p class="notice">El sistema no declara exitoso un escenario automáticamente. Las observaciones del autor deben contrastarse con las capturas técnicas; una evidencia importada no verifica el estado de la infraestructura.</p><h3>Evidencia técnica</h3><pre>${adminText(JSON.stringify(report.evidencias || {}, null, 2))}</pre><div class="admin-row-actions"><button class="button button-outline" id="report-export-json">Exportar JSON</button><button class="button button-outline" id="report-export-csv">Exportar CSV</button><button class="button button-accent" id="report-print">Imprimir reporte</button></div>`;
  bindEvidenceLinks(node);
  if (report.fase === "inicial" && report.origen === "captura") {
    const resume = document.createElement("button");
    resume.type = "button"; resume.className = "button button-outline";
    resume.textContent = "Retomar esta captura en el laboratorio";
    node.querySelector(".admin-row-actions").prepend(resume);
    resume.addEventListener("click", () => adminBusy(resume, async () => {
      const identity = adminIdentity();
      ADMIN_INITIAL_CAPTURES.set(report.escenario, report);
      await refreshLab();
      if (identity !== adminIdentity()) return;
      adminNode("lab-scenario").value = report.escenario;
      openAdminSection("lab"); renderLabScenario();
    }));
  }
  adminNode("report-export-json").addEventListener("click", () => exportAdminReport("json"));
  adminNode("report-export-csv").addEventListener("click", () => exportAdminReport("csv"));
  adminNode("report-print").addEventListener("click", () => { document.body.classList.add("admin-print"); window.print(); });
  node.scrollIntoView({behavior: "smooth", block: "start"});
}
function exportAdminReport(format) {
  if (!adminAllowed() || !ADMIN_REPORT) return;
  // Only the server's typed, sanitized report is exported; never local session state.
  let content = JSON.stringify(ADMIN_REPORT, null, 2), type = "application/json;charset=utf-8";
  if (format === "csv") {
    const cell = value => {
      let text = typeof value === "object" ? JSON.stringify(value) : String(value ?? "");
      if (/^[\s]*[=+@-]/.test(text)) text = `'${text}`;
      return `"${text.replaceAll('"', '""')}"`;
    };
    content = "\uFEFF" + [["campo", "valor"], ...Object.entries(ADMIN_REPORT)].map(row => row.map(cell).join(",")).join("\r\n"); type = "text/csv;charset=utf-8";
  }
  const url = URL.createObjectURL(new Blob([content], {type})), link = document.createElement("a");
  link.href = url; link.download = `nexustix-evidencia-${String(ADMIN_REPORT.id).replace(/[^a-zA-Z0-9_-]/g, "")}.${format}`;
  link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
}
async function importAdminEvidence(event) {
  event.preventDefault(); const form = event.currentTarget;
  await adminBusy(form.querySelector("button"), async () => {
    const file = adminNode("evidence-import-file").files[0];
    if (!file || file.size > 8 * 1024 * 1024) { adminNotice("evidence-import-status", "Selecciona un archivo JSON de hasta 8 MiB.", "error"); return; }
    let payload;
    try { payload = JSON.parse(await file.text()); }
    catch (_) { adminNotice("evidence-import-status", "El archivo no contiene JSON válido.", "error"); return; }
    if (!payload || Array.isArray(payload) || typeof payload !== "object") { adminNotice("evidence-import-status", "El archivo debe contener un reporte exportado.", "error"); return; }
    // No enviar estadísticas ni campos arbitrarios que el servidor descartaría.
    // Mantiene el cuerpo acotado, incluso con exportaciones técnicas grandes.
    const snapshot = payload.evidencias;
    if (!snapshot || !Array.isArray(snapshot.componentes) || snapshot.componentes.length > 512) {
      adminNotice("evidence-import-status", "Falta una captura válida de hasta 512 componentes.", "error"); return;
    }
    const imported = {escenario: payload.escenario, fecha: payload.fecha,
      acciones: payload.acciones || "", observado: payload.observado || "", conclusion: payload.conclusion || "",
      evidencias: {capturado_en: snapshot.capturado_en, componentes: snapshot.componentes.map(component => ({
        id: component.id, estado: component.estado, comprobado_en: component.comprobado_en,
        latencia_ms: component.latencia_ms, instancia: component.instancia, nodo: component.nodo,
      }))}};
    const body = JSON.stringify(imported);
    if (new Blob([body]).size > 250000) { adminNotice("evidence-import-status", "El reporte contiene campos demasiado extensos para importarlo.", "error"); return; }
    const result = await adminRequest(`${ADMIN_API()}/evidencias/importar`, {method: "POST", headers: {"Content-Type": "application/json"}, body});
    if (result.stale) return;
    adminNotice("evidence-import-status", result.ok ? "Evidencia importada y marcada como no verificada." : adminError(result), result.ok ? "success" : "error");
    if (result.ok) { form.reset(); await loadAdminEvidence(); }
  });
}

function exportObservedSnapshot() {
  const scenario = adminNode("lab-scenario").value;
  if (!adminAllowed() || !ADMIN_SNAPSHOT || !scenario) {
    adminNotice("lab-report-status", "Consulta el estado y selecciona un escenario antes de exportar.", "warning"); return;
  }
  const report = {formato: "nexustix-evidencia-v1", escenario: scenario,
    fecha: ADMIN_SNAPSHOT.capturado_en, origen: "consulta_no_persistida", resultado: "sin_evaluar",
    acciones: "", observado: "Consulta HTTP real; no guardada en la base de datos.", conclusion: "",
    evidencias: ADMIN_SNAPSHOT};
  const url = URL.createObjectURL(new Blob([JSON.stringify(report, null, 2)], {type: "application/json;charset=utf-8"}));
  const link = document.createElement("a"); link.href = url; link.download = `nexustix-observacion-${scenario}.json`;
  link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  adminNotice("lab-report-status", `Consulta del ${adminDate(report.fecha)} exportada sin escribir en la base. Su importación posterior se marcará como no verificada.`);
}

function initializeAdmin() {
  if (ADMIN_INITIALIZED) return; ADMIN_INITIALIZED = true;
  document.querySelectorAll("[data-admin-section]").forEach(button => button.addEventListener("click", () => openAdminSection(button.dataset.adminSection)));
  const refreshes = {"admin-refresh-summary": loadAdminSummary, "admin-refresh-events": loadAdminEvents, "admin-refresh-sales": loadAdminSales, "admin-refresh-refunds": loadAdminRefunds, "admin-refresh-users": loadAdminUsers, "admin-refresh-services": refreshAdminSnapshot, "refresh-distributed": refreshAdminSnapshot, "refresh-observability": refreshObservability, "refresh-audit": loadAudit, "refresh-journal": loadJournal, "lab-refresh": refreshLab, "lab-capture-initial": captureLabInitial, "admin-refresh-incidents": loadAdminIncidents, "admin-refresh-evidence": loadAdminEvidence};
  Object.entries(refreshes).forEach(([id, handler]) => adminNode(id).addEventListener("click", event => adminBusy(event.currentTarget, handler)));
  adminNode("event-editor-reset").addEventListener("click", resetEventEditor);
  adminNode("event-editor").addEventListener("submit", submitAdminEvent);
  adminNode("trace-form").addEventListener("submit", submitAdminTrace);
  adminNode("lab-report-form").addEventListener("submit", submitLabReport);
  adminNode("lab-scenario").addEventListener("change", renderLabScenario);
  adminNode("evidence-import-form").addEventListener("submit", importAdminEvidence);
  adminNode("lab-export-observation").addEventListener("click", exportObservedSnapshot);
  adminNode("admin-sales-event").addEventListener("change", () => { ["admin-sales", "admin-orders", "admin-tickets"].forEach(id => ADMIN_PAGES.delete(id)); loadAdminSales(); });
  adminNode("access-form").addEventListener("submit", event => {
    event.preventDefault(); const form = event.currentTarget;
    adminBusy(form.querySelector("button"), async () => {
      const data = new FormData(form);
      const result = await adminRequest(`${CONFIG.COMPRAS_LB_URL}/entradas/validar`, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({qr: data.get("qr"), evento_id: Number(data.get("evento_id"))})});
      if (result.stale) return;
      adminNotice("access-result", result.ok ? "Acceso permitido. Entrada marcada como utilizada." : adminError(result, "No se pudo validar la entrada."), result.ok ? "success" : "error");
      if (result.ok) form.elements.qr.value = "";
    });
  });
  adminNode("role-form").addEventListener("submit", event => {
    event.preventDefault(); const form = event.currentTarget;
    adminBusy(form.querySelector("button"), async () => {
      const id = Number(new FormData(form).get("user_id"));
      if (!confirm(`¿Asignar el rol organizador al usuario #${id}?`)) return;
      const result = await adminRequest(`${CONFIG.USUARIOS_URL}/usuarios/${id}/rol`, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({role: "organizador"})});
      if (result.stale) return;
      adminNotice("role-result", result.ok ? "Rol asignado. El usuario debe iniciar sesión de nuevo." : adminError(result), result.ok ? "success" : "error");
      if (result.ok) await loadAdminUsers();
    });
  });
  window.addEventListener("afterprint", () => document.body.classList.remove("admin-print"));
}
