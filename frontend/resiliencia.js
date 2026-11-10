"use strict";

// Tolerancia a fallos sobre las capturas existentes: no añade sondeos ni controla
// balanceadores o bases. Una operación solo se marca disponible con evidencia.
const RES_SLOW_MS = 800;
const RES_STALE_SECONDS = 90;
const RES_DOMAINS = ["usuarios", "eventos", "compras"];
const RES_LOCAL_NODE_LIMIT = "Una sola computadora: varios contenedores toleran la caída de un proceso, pero apagar la máquina detiene toda la aplicación.";
const RES_OPERATIONS = [
  {label: "Consultar catálogo", needs: [["eventos", "lectura"]]},
  {label: "Registrar usuario", needs: [["usuarios", "escritura"]]},
  {label: "Iniciar sesión", needs: [["usuarios", "escritura"]]},
  {label: "Reservar asiento o zona", needs: [["compras", "escritura"], ["eventos", "escritura"]]},
  {label: "Crear pedido", needs: [["compras", "escritura"], ["eventos", "escritura"]]},
  {label: "Completar pago simulado", needs: [["compras", "escritura"], ["eventos", "escritura"]]},
  {label: "Consultar entradas", needs: [["compras", "lectura"]]},
  {label: "Validar entrada", needs: [["compras", "escritura"], ["eventos", "lectura"]]},
  {label: "Administrar eventos", needs: [["eventos", "escritura"]]},
];
const RES_OPERATION_STATES = {disponible: "✓ Disponible", bloqueado: "✕ Bloqueado", desconocido: "? Desconocido"};
let RES_PREVIOUS_CAPTURE = null;
let RES_LAST_CAPTURE = null;
let RES_INCIDENTS = null;
let RES_INCIDENTS_AT = 0;

function resDomainOf(component) {
  return RES_DOMAINS.includes(component.servicio) ? component.servicio : RES_DOMAINS.find(domain => String(component.id).startsWith(domain)) || null;
}

function resApis(domain, source) {
  return source.components.filter(component => component.tipo === "api" && resDomainOf(component) === domain);
}

function resState(component, source) {
  const age = insightAge(component.comprobado_en ?? source.captured);
  if (source.stale || age === null || age > RES_STALE_SECONDS) return {key: "old", label: "Observación desactualizada"};
  if (component.estado === "saludable") return {key: "ok", label: "Disponible"};
  if (component.estado === "degradado") return {key: "warn", label: "Degradado"};
  return component.sonda_realizada === true && component.http_status == null ? {key: "bad", label: "Sin respuesta"} : {key: "unknown", label: "Desconocido"};
}

function resFailure(component, source) {
  const state = resState(component, source);
  if (state.key === "old") return "Estado desconocido";
  if (state.key === "ok") return Number(component.latencia_ms) > RES_SLOW_MS ? `Temporización (más de ${RES_SLOW_MS} ms)` : "Ninguno observado";
  if (state.key === "bad") return "Omisión: sin respuesta dentro del límite de la sonda";
  if (component.base_datos === "no_disponible") return "Dependencia: base de datos";
  if (component.tipo === "postgresql") return component.estado === "degradado" ? "Disponibilidad funcional" : "Estado desconocido";
  if (component.tipo === "gateway" || component.tipo === "ruta") return state.key === "warn" ? "Dependencia: ruta o backend" : "Estado desconocido";
  return "Estado desconocido";
}

function resLevels(component) {
  if (component.tipo !== "api") return "No aplica";
  const process = component.http_status != null ? "sí" : component.sonda_realizada === true ? "sin respuesta" : "sin comprobar";
  const service = component.estado === "saludable" ? "sí" : component.http_status >= 500 ? "no" : "sin evidencia";
  const dependencies = component.base_datos === "disponible" ? "sí" : component.base_datos === "no_disponible" ? "no" : "sin evidencia";
  return `Proceso: ${process} · Servicio: ${service} · Dependencias: ${dependencies}`;
}

function resDomainState(domain, source) {
  if (source.stale) return {lectura: "desconocido", escritura: "desconocido", causa: "Observación desactualizada"};
  const apis = resApis(domain, source);
  const inventoried = apis.some(row => row.sonda_realizada === false && row.origen_configuracion === "inventario_nodos");
  const probed = apis.filter(row => row.sonda_realizada !== false);
  const healthy = probed.filter(row => row.estado === "saludable");
  const database = source.components.find(row => row.id === `${domain}_db`);
  if (!healthy.length) {
    const silent = probed.length > 0 && !inventoried && probed.every(row => row.sonda_realizada === true);
    const unconfirmed = probed.some(row => row.http_status == null && row.fallos_consecutivos === 1);
    const state = silent && !unconfirmed ? "bloqueado" : "desconocido";
    return {lectura: state, escritura: state, causa: state === "bloqueado" ? `API ${domain} sin instancias operativas` : `API ${domain} sin evidencia suficiente`};
  }
  if (database?.estado === "saludable") return {lectura: "disponible", escritura: "disponible", causa: null};
  return database?.estado === "degradado"
    ? {lectura: "disponible", escritura: "bloqueado", causa: `Sin escritor confirmado en PostgreSQL ${domain}`}
    : {lectura: "disponible", escritura: "desconocido", causa: `Primaria de ${domain} sin evidencia suficiente`};
}

function resOperations(source) {
  const rank = {disponible: 0, desconocido: 1, bloqueado: 2};
  return RES_OPERATIONS.map(operation => {
    const states = operation.needs.map(([domain, mode]) => ({domain, state: resDomainState(domain, source), value: resDomainState(domain, source)[mode]}));
    const worst = states.reduce((best, row) => rank[row.value] > rank[best.value] ? row : best);
    const causes = [...new Set(states.filter(row => row.value !== "disponible").map(row => row.state.causa).filter(Boolean))];
    return {label: operation.label, state: worst.value, cause: causes.join(" · ") || "Ninguna observada", domains: operation.needs.map(([domain]) => domain)};
  });
}

function resAffected(component, source) {
  const domain = resDomainOf(component);
  if (!domain) return "Acceso a través de este componente";
  return RES_OPERATIONS.filter(operation => operation.needs.some(([name]) => name === domain)).map(operation => operation.label).join(" · ") || "Ninguna";
}

function resImpactHtml(component, source) {
  const domain = resDomainOf(component);
  const dependents = source.components.filter(row => row.id !== component.id && (adminList(row.dependencias).includes(component.id) || adminList(row.dependencias_servicio).includes(component.servicio)));
  const replicas = domain ? resApis(domain, source) : [];
  const database = domain && source.components.find(row => row.id === `${domain}_db`);
  const limit = component.tipo === "api" && database?.estado !== "saludable" ? "Las réplicas de API no sustituyen al escritor PostgreSQL: sin primaria confirmada no se garantizan escrituras." : component.tipo === "postgresql" ? "Un standby no asume la escritura de forma automática; la promoción es manual." : "Sin limitación adicional identificada en esta captura.";
  return `<h4>Impacto si este componente falla</h4><p class="field-help">Proyección a partir del inventario y la última observación; no es un fallo ejecutado.</p>${adminFacts([["Componentes que dependen de él", dependents.map(row => row.nombre || row.id).join(" · ") || "Ninguno declarado"], ["Operaciones comerciales", resAffected(component, source)], ["Réplicas de API del servicio", replicas.map(row => `${row.nombre || row.id}: ${resState(row, source).label}`).join(" · ") || "No aplica"], ["Limitación", limit], ["Tipo de fallo observado", resFailure(component, source)]])}`;
}

function renderResOverview(source, operations) {
  const apis = source.components.filter(row => row.tipo === "api");
  const primaries = source.components.filter(row => row.tipo === "postgresql");
  const standbys = source.components.filter(row => insightKind(row) === "standby");
  const open = RES_INCIDENTS === null ? INSIGHT_NOT_AVAILABLE : RES_INCIDENTS.filter(row => !row.recuperado_en).length;
  const responding = source.nodes.filter(node => node.respondio === true || Number(node.respuestas_http) > 0).length;
  const local = !source.nodes.length && insightIsLocal();
  const available = rows => source.stale ? "Desconocido" : `${rows.filter(row => row.estado === "saludable").length} / ${rows.length}`;
  const stats = [["Nodos configurados", source.nodes.length || (local ? "1 (esta PC)" : "No declarados")], ["Nodos con respuesta", source.stale ? "Desconocido" : source.nodes.length ? responding : local ? "1" : "No declarado"], ["APIs disponibles", available(apis)], ["Primarias disponibles", available(primaries)], ["Canales standby observados", source.stale ? "Desconocido" : standbys.filter(row => row.replicacion).length], ["Operaciones afectadas", source.stale ? "Desconocido" : operations.filter(row => row.state !== "disponible").length], ["Incidentes abiertos", open]];
  adminNode("res-overview").innerHTML = stats.map(([label, value]) => `<div class="admin-stat"><span>${adminText(label)}</span><strong>${adminText(value)}</strong></div>`).join("");
}

function renderResMonitor(source) {
  const rows = source.components.filter(row => row.tipo !== "ruta");
  const labels = insightLabels(source);
  adminNode("res-monitor").innerHTML = adminTable(rows, [["Componente", row => row.nombre || row.id], ["Tipo", row => INSIGHT_KIND_LABELS[insightKind(row)]], ["Nodo", row => row.nodo || (insightIsLocal() ? "Esta computadora" : INSIGHT_NOT_IDENTIFIED)], ["Estado", row => resState(row, source).label], ["Niveles de salud", resLevels], ["Dependencias", row => adminList(row.dependencias).map(id => labels.get(id) || id).join(" · ") || "Ninguna declarada"], ["Última respuesta válida", row => row.ultima_respuesta_valida ? adminDate(row.ultima_respuesta_valida) : row.estado === "saludable" ? adminDate(row.comprobado_en) : "Sin evidencia"], ["Antigüedad", row => clusterElapsed(row.comprobado_en ?? source.captured)], ["Tiempo de respuesta", row => row.latencia_ms == null ? INSIGHT_NOT_AVAILABLE : `${row.latencia_ms} ms`], ["Tipo de fallo", row => resFailure(row, source)], ["Fallos consecutivos", row => row.fallos_consecutivos ?? "No registrado"], ["Funciones afectadas", row => resState(row, source).key === "ok" ? "Ninguna observada" : resAffected(row, source)]], "La captura no incluye componentes.");
}

function renderResDetection(source) {
  if (source.captured !== RES_LAST_CAPTURE) { RES_PREVIOUS_CAPTURE = RES_LAST_CAPTURE; RES_LAST_CAPTURE = source.captured; }
  const spacing = RES_PREVIOUS_CAPTURE ? `${Math.round((Date.parse(RES_LAST_CAPTURE) - Date.parse(RES_PREVIOUS_CAPTURE)) / 1000)} s` : "Se calcula tras la siguiente captura";
  adminNode("res-probe").innerHTML = adminFacts([["Mecanismo", "Sondeo HTTP desde un observador; no existe heartbeat explícito entre nodos"], ["Intervalo del panel", source.inventory ? "30 s con la sección visible; caché del observador de 25 s" : "Cada 60 s con la sección visible"], ["Timeout de la sonda", "1 s de conexión y 1 s de lectura, máximo 4 s por respuesta"], ["Umbral de temporización", `${RES_SLOW_MS} ms`], ["Tiempo entre observaciones", spacing], ["Última observación", `${adminDate(source.captured)} · ${clusterElapsed(source.captured)}`]]);
  const interesting = /^(servicio_|nodo_|standby_)/;
  const events = [...ADMIN_CLUSTER_EVENTS.values()].filter(event => interesting.test(String(event.tipo || ""))).sort((a, b) => (Date.parse(a.fecha) || 0) - (Date.parse(b.fecha) || 0)).slice(-12);
  adminNode("res-timeline").innerHTML = events.length ? `<ol class="res-timeline">${events.map(event => `<li><strong>${adminText(event.componente || event.nodo || "Componente")}</strong><span>${adminText(clusterStateLabel(event.estado_anterior))} → ${adminText(clusterStateLabel(event.estado_actual))}</span><small>Observado: ${adminText(adminDate(event.fecha))}</small></li>`).join("")}</ol>` : adminEmpty("No hay transiciones registradas por este observador en esta sesión. Información insuficiente para trazar una secuencia.");
  const recovered = [...ADMIN_CLUSTER_EVENTS.values()].filter(event => ["servicio_recuperado", "nodo_vuelve_a_responder", "standby_recuperado"].includes(event.tipo)).sort((a, b) => (Date.parse(b.fecha) || 0) - (Date.parse(a.fecha) || 0)).slice(0, 5);
  const operations = resOperations(source);
  adminNode("res-recovery").innerHTML = `${adminTable(recovered, [["Componente recuperado", row => row.componente || row.nodo || INSIGHT_NOT_IDENTIFIED], ["Observado", row => adminDate(row.fecha)]], "No hay recuperaciones observadas en esta sesión.")}${adminFacts([["Operaciones disponibles ahora", operations.filter(row => row.state === "disponible").map(row => row.label).join(" · ") || "Ninguna confirmada"], ["Operaciones todavía pendientes", operations.filter(row => row.state !== "disponible").map(row => row.label).join(" · ") || "Ninguna"]])}<p class="field-help">Recuperar una instancia no significa recuperar el sistema. Las ventanas entre capturas no son un RTO medido.</p>`;
}

function renderResBalancing(source) {
  const rows = RES_DOMAINS.map(domain => {
    const apis = resApis(domain, source);
    const route = source.components.find(row => (row.tipo === "ruta" && resDomainOf(row) === domain) || (domain === "compras" && row.tipo === "gateway" && !source.inventory));
    const state = resDomainState(domain, source);
    return {domain, configured: apis.length, responding: apis.filter(row => row.http_status != null).length, unavailable: apis.filter(row => row.estado !== "saludable").length,
      served: route?.solicitud_observada?.instancia || INSIGHT_NOT_IDENTIFIED, backend: route?.solicitud_observada?.backend || INSIGHT_NOT_IDENTIFIED, continuity: state.escritura === "disponible" ? "Escrituras sin bloqueo observado" : state.causa || "Sin evidencia"};
  });
  const nodes = source.nodes.length;
  const limit = nodes > 1 ? "Con varias computadoras, si una pierde sus primarias la otra no gana capacidad de escritura por tener standbys: la recuperación es manual. Si se apaga el equipo del gateway, hay que entrar por otra dirección configurada." : RES_LOCAL_NODE_LIMIT;
  const policy = source.inventory ? "HAProxy según la plantilla de despliegue-nodos: comprobación /health cada 3 s, retira con 2 fallos y reincorpora con 2 aciertos. retries 0, sin redispatch." : "Depende del despliegue: este panel no recibe la configuración del balanceador. En el Compose local, Nginx hace failover pasivo y solo reenvía POST /compras con la misma clave idempotente.";
  adminNode("res-balancing").innerHTML = `<div class="table-scroll">${adminTable(rows, [["Servicio", "domain"], ["Configuradas", "configured"], ["Responden", row => source.stale ? "Desconocido" : row.responding], ["No disponibles", row => source.stale ? "Desconocido" : row.unavailable], ["Atendió la lectura", "served"], ["Backend informado", "backend"], ["Continuidad", "continuity"]])}</div><p class="field-help">${adminText(policy)}</p><p class="field-help">Estado inferido de las respuestas de cada API: HAProxy no expone su estado de backends a este panel. "Backend informado" es la cabecera real de una lectura de prueba.</p><p class="notice notice-warning">${adminText(limit)}</p>`;
}

function renderResOperations(source, operations) {
  adminNode("res-operations").innerHTML = `<div class="res-ops">${operations.map(row => `<article class="res-op res-${row.state}"><strong>${adminText(row.label)}</strong><span>${RES_OPERATION_STATES[row.state]}</span><small>Dependencia afectada: ${adminText(row.cause)}</small><small>Última observación: ${adminText(adminDate(source.captured))}</small></article>`).join("")}</div><p class="field-help">Cada operación se deriva de sus dependencias observadas. Si es desconocida, el backend sigue aplicando sus reglas. Una comprobación no ejecuta compras, reservas ni escrituras.</p>`;
}

async function refreshResIncidents() {
  if (Date.now() - RES_INCIDENTS_AT < 60000) return;
  RES_INCIDENTS_AT = Date.now();
  const generation = INSIGHT_GENERATION;
  const result = await adminRequest(`${ADMIN_API()}/incidentes?limit=50`);
  if (result.stale || generation !== INSIGHT_GENERATION) return;
  RES_INCIDENTS = result.ok ? adminList(result.data?.incidentes) : null;
  renderResiliencePanels();
}

// Sin inventario multinodo el observador no aporta datos: el resumen usa la captura local
// y se ocultan las tarjetas que solo tienen sentido con varias computadoras.
function renderLocalCluster(source) {
  ["cluster-deployment-identity", "cluster-nodes", "cluster-node-detail", "cluster-topology", "cluster-announcement-form", "cluster-history"].forEach(id => { const card = adminNode(id)?.closest(".workspace-card"); if (card) card.hidden = !source.inventory; });
  const pair = adminNode("cluster-coordination")?.closest(".admin-two-columns");
  if (pair) pair.hidden = !source.inventory;
  if (source.inventory) return;
  const count = rows => `${rows.filter(row => row.estado === "saludable").length} / ${rows.length}`;
  const apis = source.components.filter(row => row.tipo === "api"), bases = source.components.filter(row => row.tipo === "postgresql"), gateways = source.components.filter(row => row.tipo === "gateway");
  const every = [...apis, ...bases, ...gateways];
  const state = source.stale ? "Desconocido" : every.length && every.every(row => row.estado === "saludable") ? "Operativo" : "Con incidencias";
  adminNode("cluster-summary").innerHTML = [["Estado global", state, "Última consulta de esta instalación"], ["Modalidad", insightIsLocal() ? "Una PC" : "Sin inventario", "No hay inventario multinodo"], ["APIs que responden", count(apis), "Instancias desplegadas"], ["Bases disponibles", count(bases), "Vistas a través de las APIs"], ["Balanceador", count(gateways), "Comprobación de salud"]].map(([label, value, note]) => `<div class="admin-stat"><span>${adminText(label)}</span><strong>${adminText(value)}</strong><small>${adminText(note)}</small></div>`).join("");
}

function renderResiliencePanels() {
  const source = insightSource();
  const ids = ["res-monitor", "res-probe", "res-timeline", "res-recovery", "res-balancing", "res-operations"];
  if (!source) {
    adminNode("res-overview").textContent = "";
    ids.forEach(id => { adminNode(id).innerHTML = adminEmpty("Sin datos actuales del observador: el estado es desconocido."); });
    return;
  }
  renderLocalCluster(source);
  const operations = resOperations(source);
  renderResOverview(source, operations);
  renderResOperations(source, operations);
  renderResMonitor(source);
  renderResDetection(source);
  renderResBalancing(source);
}

function clearResiliencePanels() {
  RES_PREVIOUS_CAPTURE = null; RES_LAST_CAPTURE = null; RES_INCIDENTS = null; RES_INCIDENTS_AT = 0;
  ["res-overview", "res-monitor", "res-probe", "res-timeline", "res-recovery", "res-balancing", "res-operations"].forEach(id => { const node = adminNode(id); if (node) node.textContent = ""; });
}
