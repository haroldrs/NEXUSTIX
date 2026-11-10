"use strict";

// Vistas didácticas sobre las capturas administrativas existentes. Solo leen:
// lo que no tiene evidencia se muestra como "No identificado" o "No disponible".
const INSIGHT_STALE_SECONDS = 90;
const INSIGHT_LOCAL_NODE = "__esta_computadora__";
const INSIGHT_UNDECLARED_NODE = "__no_declarado__";
const INSIGHT_ORDER = {frontend: 0, gateway: 1, api: 2, primary: 3, standby: 4, other: 5};
const INSIGHT_KIND_LABELS = {frontend: "Frontend web", gateway: "Gateway / balanceador", api: "Microservicio", primary: "Base primaria", standby: "Base standby", other: "Componente"};
const INSIGHT_NOT_IDENTIFIED = "No identificado";
const INSIGHT_NOT_AVAILABLE = "No disponible";
let INSIGHT_SELECTED = null;
let INSIGHT_GROUPS = [];
let INSIGHT_EDGES = [];
let INSIGHT_RESIZE = null;
let INSIGHT_SNAPSHOT_PENDING = false;
let INSIGHT_OUTBOX_REQUEST = 0;
let INSIGHT_GENERATION = 0;

const ADMIN_SECTION_HELP = {"summary": ["Una mirada rápida a ventas, eventos y estado del sistema.", "Ábrela primero para saber si algo requiere atención."], "academic": ["Organiza el proyecto según las tres unidades del curso.", "Elige una unidad y usa \"Abrir\" para ir a la pantalla de cada tema."], "events": ["Crear, editar y cancelar eventos, con zonas o asientos.", "Completa el formulario; lo que guardes aparece en el catálogo."], "sales": ["Compras, pedidos y entradas emitidas por los clientes.", "Filtra por ID de evento. Los códigos QR nunca se muestran aquí."], "refunds": ["Reembolsos y cancelaciones de eventos en curso.", "Consulta su avance; el sistema reintenta los pendientes."], "users": ["Lista de cuentas y asignación del rol organizador.", "Indica el ID del usuario para darle ese rol."], "cluster": ["Estado de computadoras, servicios y bases, y qué pasaría si algo falla.", "En una sola PC verás un resumen local; las tarjetas de varias PC se ocultan."], "services": ["Cada API y si respondió a su comprobación de salud.", "Pulsa \"Consultar estado\" para actualizar."], "databases": ["Rol de cada PostgreSQL, replicación, checkpoints y recuperación.", "En una sola PC cada base aparece sin standby: es lo esperado."], "consistency": ["Qué garantiza cada parte del sistema y por qué varias lecturas no forman una foto global.", "Sección explicativa, con datos reales de la última consulta."], "security": ["Permisos, credenciales (sin valores), comunicaciones y auditoría.", "Pulsa \"Actualizar\" tras validar entradas o realizar acciones administrativas."], "observability": ["El recorrido de una compra, las instancias y el inventario.", "Pega el ID completo de un pedido (Mis entradas → Referencia del pedido) en \"Seguir una compra\"."], "lab": ["Registrar experimentos de fallos: antes, después y conclusión.", "Elige un escenario, captura el estado, haz la prueba manualmente fuera del panel y guarda el reporte."], "incidents": ["Fallos detectados entre capturas guardadas.", "Se llena solo al capturar estados en el Laboratorio."], "evidence": ["Reportes guardados del Laboratorio, para consultar y exportar.", "Estará vacío hasta que guardes el primero."]};

function insightIsLocal() {
  return CONFIG.DEPLOYMENT_PROFILE === "local" || (!CONFIG.DEPLOYMENT_PROFILE && ["localhost", "127.0.0.1"].includes(location.hostname));
}

// Las réplicas sin destino configurado no existen en este despliegue: no se muestran.
function pruneUndeployed(snapshot) {
  const absent = new Set(adminList(snapshot.componentes).filter(row => row.detalle === "Destino no configurado").map(row => row.id));
  snapshot.componentes = adminList(snapshot.componentes).filter(row => !absent.has(row.id));
  snapshot.persistencia = adminList(snapshot.persistencia).filter(row => !absent.has(row.componente));
  snapshot.metricas = adminList(snapshot.metricas).filter(row => !absent.has(row.componente));
  return snapshot;
}

function installSectionHelp() {
  document.querySelectorAll("[data-admin-panel]").forEach(panel => {
    const help = ADMIN_SECTION_HELP[panel.dataset.adminPanel];
    if (!help) return;
    const box = document.createElement("div");
    box.className = "section-help";
    box.innerHTML = `<p><strong>Para qué sirve:</strong> ${adminText(help[0])}</p><p><strong>Cómo usarla:</strong> ${adminText(help[1])}</p><p class="section-help-note">"Desconocido" no es un error: indica que todavía no hay datos para afirmarlo. "No declarado" significa que este despliegue no lo configura.</p>`;
    const heading = panel.querySelector(".section-heading");
    if (heading) heading.after(box); else panel.prepend(box);
  });
}

function insightSource() {
  const clusterNodes = adminList(ADMIN_CLUSTER?.cluster?.nodos);
  if (clusterNodes.length) {
    return {inventory: true, nodes: clusterNodes, components: adminList(ADMIN_CLUSTER.componentes), metrics: adminList(ADMIN_CLUSTER.metricas),
      bases: adminList(ADMIN_CLUSTER.cluster.bases), captured: ADMIN_CLUSTER.capturado_en, stale: ADMIN_CLUSTER_STALE,
      profile: ADMIN_CLUSTER.infraestructura?.perfil || CONFIG.DEPLOYMENT_PROFILE, deployment: ADMIN_CLUSTER.infraestructura?.despliegue || CONFIG.DEPLOYMENT_ID};
  }
  if (!ADMIN_SNAPSHOT) return null;
  const infrastructure = ADMIN_SNAPSHOT.infraestructura;
  const nodes = infrastructure?.modo === "inventario_nodos" ? adminList(infrastructure.nodos) : [];
  const age = insightAge(ADMIN_SNAPSHOT.capturado_en);
  return {inventory: nodes.length > 0, nodes, components: adminList(ADMIN_SNAPSHOT.componentes), metrics: adminList(ADMIN_SNAPSHOT.metricas),
    bases: adminList(infrastructure?.bases), captured: ADMIN_SNAPSHOT.capturado_en, stale: age === null || age > INSIGHT_STALE_SECONDS,
    profile: infrastructure?.perfil || CONFIG.DEPLOYMENT_PROFILE || (insightIsLocal() ? "local" : undefined), deployment: infrastructure?.despliegue || CONFIG.DEPLOYMENT_ID};
}

function insightAge(value) {
  const moment = Date.parse(value);
  return Number.isFinite(moment) ? Math.max(0, (Date.now() - moment) / 1000) : null;
}

function insightEvidence(component, source) {
  const raw = String(component.estado || "desconocido");
  const healthy = ["saludable", "activo", "operativo", "healthy", "ok"].includes(raw);
  const failed = ["no_disponible", "caido", "unavailable", "degradado", "degraded"].includes(raw);
  if (!healthy && !failed && component.sonda_realizada !== true && component.http_status == null) {
    return {key: "configurado", label: "Configurado", icon: "○", note: "Aparece en la configuración; esta captura no lo comprobó directamente."};
  }
  if (healthy || failed) {
    const moment = component.comprobado_en ?? source.captured;
    const age = insightAge(moment);
    if (source.stale || age === null || age > INSIGHT_STALE_SECONDS) {
      return {key: "desconocido", label: "Desconocido", icon: "?", note: `Última evidencia: ${healthy ? "respondía" : "con fallo"} · ${clusterElapsed(moment)}. No se considera estado actual.`};
    }
  }
  if (healthy) return {key: "observado", label: "Observado", icon: "✓", note: "Existe evidencia reciente de respuesta."};
  if (failed) return {key: "no_disponible", label: "No disponible", icon: "✕", note: `Una consulta válida informó: ${raw}. No prueba que la computadora esté apagada.`};
  return {key: "desconocido", label: "Desconocido", icon: "?", note: "No hay evidencia suficiente para determinar su estado."};
}

function insightKind(component) {
  const type = String(component.tipo || "");
  if (type === "postgresql") return "primary";
  if (type.includes("replica") || type.includes("standby") || /_sb_/.test(String(component.id))) return "standby";
  return ["frontend", "gateway", "api"].includes(type) ? type : "other";
}

function insightInstance(component) {
  return component.identidad_observada?.instancia || component.instancia || INSIGHT_NOT_IDENTIFIED;
}

function insightGroups(source) {
  const fallback = insightIsLocal() ? INSIGHT_LOCAL_NODE : INSIGHT_UNDECLARED_NODE;
  const groups = new Map(source.nodes.map(node => [node.id, {id: node.id, label: node.id, node, items: []}]));
  for (const component of source.components.filter(row => row.tipo !== "ruta")) {
    const key = component.nodo || fallback;
    if (!groups.has(key)) groups.set(key, {id: key, label: key === INSIGHT_LOCAL_NODE ? "Esta computadora" : key === INSIGHT_UNDECLARED_NODE ? "Nodo no declarado" : key, node: null, items: []});
    groups.get(key).items.push({id: component.id, component, kind: insightKind(component)});
  }
  for (const group of groups.values()) group.items.sort((a, b) => INSIGHT_ORDER[a.kind] - INSIGHT_ORDER[b.kind]);
  return [...groups.values()];
}

function insightEdges(source, groups) {
  const items = groups.flatMap(group => group.items.map(item => ({...item, groupId: group.id})));
  const known = new Map(items.map(item => [item.id, item]));
  const components = new Map(source.components.map(row => [row.id, row]));
  const edges = new Map();
  const add = (from, to, kind) => { if (from !== to && known.has(from) && known.has(to)) edges.set(`${from}|${to}|${kind}`, {from, to, kind}); };
  for (const item of items) {
    const component = item.component;
    if (item.kind === "standby") { adminList(component.dependencias).forEach(dependency => add(dependency, item.id, "wal")); continue; }
    for (const dependency of adminList(component.dependencias)) {
      const target = components.get(dependency);
      if (target?.tipo === "ruta") adminList(target.dependencias).forEach(next => add(item.id, next, "http"));
      else add(item.id, dependency, known.get(dependency)?.kind === "primary" ? "sql" : "http");
    }
    for (const service of adminList(component.dependencias_servicio)) items.filter(other => other.component.servicio === service && other.kind === "api").forEach(other => add(item.id, other.id, "http"));
  }
  if (source.inventory) {
    for (const route of source.components.filter(row => row.tipo === "ruta")) {
      const gateway = items.find(item => item.kind === "gateway" && item.component.nodo === route.nodo);
      if (gateway) adminList(route.dependencias).forEach(dependency => add(gateway.id, dependency, "http"));
    }
    for (const gateway of items.filter(item => item.kind === "gateway")) {
      const frontend = items.find(item => item.kind === "frontend" && item.component.nodo === gateway.component.nodo);
      if (frontend) add(gateway.id, frontend.id, "http");
    }
    for (const base of source.bases) {
      const primary = `${base.base}_db`;
      for (const standby of adminList(base.standbys)) {
        const target = items.find(item => item.kind === "standby" && item.component.nodo === standby.nodo && String(item.id).startsWith(`${base.base}_`));
        if (target) add(primary, target.id, "wal");
      }
    }
  }
  return [...edges.values()];
}

function insightLegend() {
  return `<div class="topo-legend" aria-label="Leyenda del diagrama"><span><i class="topo-swatch node"></i>Computadora física</span><span><i class="topo-swatch container"></i>Contenedor con su servicio e instancia</span><span><i class="topo-swatch primary"></i>Base primaria</span><span><i class="topo-swatch standby"></i>Base standby</span><span><i class="topo-sample-line"></i>HTTP</span><span><i class="topo-sample-line sql"></i>SQL</span><span><i class="topo-sample-line wal"></i>Replicación WAL</span><span>○ Configurado</span><span>✓ Observado</span><span>✕ No disponible</span><span>? Desconocido</span></div>`;
}

function renderInsightTopology() {
  const target = adminNode("insight-topology"), note = adminNode("insight-topology-note");
  const source = insightSource();
  if (!source) {
    INSIGHT_GROUPS = []; INSIGHT_EDGES = [];
    target.innerHTML = adminEmpty("Sin captura disponible: no se puede representar la arquitectura ni determinar su estado.");
    adminNode("insight-topology-detail").textContent = "";
    note.textContent = "Estado desconocido: todavía no hay una respuesta del diagnóstico.";
    return;
  }
  const focused = document.activeElement?.dataset?.insightId;
  INSIGHT_GROUPS = insightGroups(source);
  INSIGHT_EDGES = insightEdges(source, INSIGHT_GROUPS);
  const physical = INSIGHT_GROUPS.filter(group => group.id !== INSIGHT_UNDECLARED_NODE);
  note.textContent = `${source.inventory ? `${source.nodes.length} computadora(s) declarada(s) en el inventario` : physical.length ? "Una computadora, según el perfil local del portal" : "Sin inventario multinodo: la ubicación física no está declarada"}. Captura: ${adminDate(source.captured)} · ${clusterElapsed(source.captured)}${source.stale ? ". Captura desactualizada: los estados son desconocidos" : ""}. Selecciona un elemento para ver su detalle.`;
  const known = INSIGHT_SELECTED && INSIGHT_GROUPS.some(group => `n:${group.id}` === INSIGHT_SELECTED || group.items.some(item => `c:${item.id}` === INSIGHT_SELECTED));
  if (!known) INSIGHT_SELECTED = null;
  target.innerHTML = `${insightLegend()}<div class="topo-board" id="insight-board"><svg class="topo-lines" aria-hidden="true"></svg><div class="topo-nodes">${INSIGHT_GROUPS.map(group => `<section class="topo-node${INSIGHT_SELECTED === `n:${group.id}` ? " is-selected" : ""}" aria-label="Computadora ${adminText(group.label)}"><button type="button" class="topo-node-head" data-insight-id="${adminText(`n:${group.id}`)}" aria-pressed="${INSIGHT_SELECTED === `n:${group.id}`}"><strong>${adminText(group.label)}</strong><small>${group.id === INSIGHT_UNDECLARED_NODE ? "Ubicación no declarada" : "Computadora física"}</small></button><div class="topo-items">${group.items.map(item => {
    const evidence = insightEvidence(item.component, source);
    return `<button type="button" class="topo-item kind-${item.kind}${INSIGHT_SELECTED === `c:${item.id}` ? " is-selected" : ""}" data-insight-id="${adminText(`c:${item.id}`)}" aria-pressed="${INSIGHT_SELECTED === `c:${item.id}`}"><span class="topo-kind">${INSIGHT_KIND_LABELS[item.kind]}</span><strong>${adminText(item.component.nombre || item.id)}</strong><span class="topo-inst">Instancia: ${adminText(insightInstance(item.component))}</span><span class="topo-ev ev-${evidence.key}"><span aria-hidden="true">${evidence.icon}</span>${evidence.label}</span></button>`;
  }).join("") || adminEmpty("Sin componentes en la captura.")}</div></section>`).join("")}</div></div>`;
  target.querySelectorAll("[data-insight-id]").forEach(button => button.addEventListener("click", () => {
    INSIGHT_SELECTED = INSIGHT_SELECTED === button.dataset.insightId ? null : button.dataset.insightId;
    renderInsightTopology();
  }));
  if (focused) target.querySelector(`[data-insight-id="${CSS.escape(focused)}"]`)?.focus({preventScroll: true});
  renderInsightDetail(source);
  drawInsightLines();
  INSIGHT_RESIZE?.disconnect();
  if (window.ResizeObserver) {
    INSIGHT_RESIZE = new ResizeObserver(drawInsightLines);
    INSIGHT_RESIZE.observe(adminNode("insight-board"));
  }
}

function drawInsightLines() {
  const board = adminNode("insight-board"), svg = board?.querySelector("svg");
  if (!svg) return;
  const box = board.getBoundingClientRect();
  if (!box.width) return;
  svg.setAttribute("viewBox", `0 0 ${box.width} ${box.height}`);
  const rect = id => board.querySelector(`[data-insight-id="${CSS.escape(`c:${id}`)}"]`)?.getBoundingClientRect();
  const selected = INSIGHT_SELECTED;
  const inSelection = id => selected === `c:${id}` || INSIGHT_GROUPS.some(group => selected === `n:${group.id}` && group.items.some(item => item.id === id));
  svg.innerHTML = INSIGHT_EDGES.map(edge => {
    const from = rect(edge.from), to = rect(edge.to);
    if (!from || !to) return "";
    const ay = from.top - box.top + from.height / 2, by = to.top - box.top + to.height / 2;
    const leftToRight = from.left + from.width / 2 <= to.left + to.width / 2;
    const sameColumn = Math.abs(from.left - to.left) < 40;
    const ax = (sameColumn || leftToRight ? from.right : from.left) - box.left;
    const bx = (sameColumn || !leftToRight ? to.right : to.left) - box.left;
    const bend = sameColumn ? 34 : Math.max(26, Math.abs(bx - ax) / 2);
    const c1 = ax + (sameColumn || leftToRight ? bend : -bend), c2 = bx + (sameColumn || !leftToRight ? bend : -bend);
    const emphasis = selected ? (inSelection(edge.from) || inSelection(edge.to) ? " is-active" : " is-dim") : "";
    return `<path class="topo-line line-${edge.kind}${emphasis}" d="M${ax} ${ay} C${c1} ${ay},${c2} ${by},${bx} ${by}"/>`;
  }).join("");
}

function insightLabels(source) {
  return new Map(source.components.map(component => [component.id, component.nombre || component.id]));
}

function renderInsightDetail(source) {
  const target = adminNode("insight-topology-detail");
  if (!INSIGHT_SELECTED) { target.innerHTML = '<p class="field-help">Pulsa una computadora o un servicio para ver su detalle y resaltar sus conexiones.</p>'; return; }
  const id = INSIGHT_SELECTED.slice(2);
  if (INSIGHT_SELECTED.startsWith("n:")) {
    const group = INSIGHT_GROUPS.find(row => row.id === id);
    const observed = group.items.map(item => item.component).filter(component => insightEvidence(component, source).key === "observado" && component.comprobado_en).map(component => component.comprobado_en).sort().pop();
    const lastValid = group.node?.ultima_respuesta || observed;
    target.innerHTML = `<h4>Computadora ${adminText(group.label)}</h4>${adminFacts([["Identificador", group.label], ["Perfil del despliegue", source.profile || "Desconocido"], ["Servicios asignados", group.items.map(item => item.component.nombre || item.id).join(" · ") || "Ninguno"], ["Instancias configuradas", group.items.filter(item => item.kind === "api").length], ["Última observación válida", lastValid ? `${adminDate(lastValid)} · ${clusterElapsed(lastValid)}` : "Sin evidencia"]])}<div class="table-scroll">${adminTable(group.items, [["Componente", item => item.component.nombre || item.id], ["Instancia", item => insightInstance(item.component)], ["Estado", item => insightEvidence(item.component, source).label], ["Alcance", item => insightEvidence(item.component, source).note]])}</div>`;
    return;
  }
  const item = INSIGHT_GROUPS.flatMap(group => group.items.map(row => ({...row, group}))).find(row => row.id === id);
  const component = item.component, evidence = insightEvidence(component, source), labels = insightLabels(source);
  const dependencies = [...adminList(component.dependencias).map(dependency => labels.get(dependency) || dependency), ...adminList(component.dependencias_servicio)];
  const database = item.kind === "primary" || item.kind === "standby";
  target.innerHTML = `<h4>${adminText(component.nombre || id)}</h4>${adminFacts([["Tipo", INSIGHT_KIND_LABELS[item.kind]], ["Nodo", item.group.label], ["Contenedor", "Declarado por el despliegue Compose; identificador no disponible"], ["Dependencias", dependencies.join(" · ") || "Ninguna declarada"], [database ? "Identidad observada" : "Instancia observada", insightInstance(component)], ["Estado", evidence.label], ["Última comprobación", component.comprobado_en ? `${adminDate(component.comprobado_en)} · ${clusterElapsed(component.comprobado_en)}` : "Sin evidencia"]])}<p class="field-help">${adminText(evidence.note)} ${adminText(component.detalle || "")}</p>${resImpactHtml(component, source)}`;
}

function renderInsightProcesses() {
  const target = adminNode("insight-processes"), source = insightSource();
  if (!source) { target.innerHTML = adminEmpty("Sin captura disponible."); return; }
  const metrics = new Map(source.metrics.map(row => [row.componente, row]));
  const rows = source.components.filter(component => component.tipo !== "ruta");
  const measure = (component, key, suffix = "") => { const value = metrics.get(component.id)?.[key]; return value === null || value === undefined ? INSIGHT_NOT_AVAILABLE : `${value}${suffix}`; };
  const labels = insightLabels(source);
  target.innerHTML = adminTable(rows, [["Servicio", row => row.nombre || row.id], ["Instancia", insightInstance], ["Nodo", row => row.nodo || (insightIsLocal() ? "Esta computadora" : INSIGHT_NOT_IDENTIFIED)], ["Contenedor", () => INSIGHT_NOT_AVAILABLE], ["Estado", row => insightEvidence(row, source).label], ["Dependencias", row => adminList(row.dependencias).map(id => labels.get(id) || id).join(" · ") || "Ninguna declarada"], ["Solicitudes", row => measure(row, "solicitudes")], ["Errores HTTP", row => measure(row, "errores_http")], ["Latencia media", row => measure(row, "latencia_media_ms", " ms")], ["Métricas desde", row => { const since = metrics.get(row.id)?.desde; return since ? adminDate(since) : INSIGHT_NOT_AVAILABLE; }], ["PID · CPU · RAM", () => INSIGHT_NOT_AVAILABLE]], "La captura no incluye componentes.");
}

function renderInsightInstances() {
  const target = adminNode("insight-instances"), source = insightSource();
  if (!source) { target.innerHTML = adminEmpty("Sin captura disponible."); return; }
  const rows = source.components.filter(component => component.tipo === "api" || component.tipo === "ruta" || component.tipo === "gateway");
  target.innerHTML = adminTable(rows, [["Componente", row => row.nombre || row.id], ["Atendió", row => row.solicitud_observada?.instancia || insightInstance(row)], ["Nodo", row => row.identidad_observada?.node_id || row.nodo || INSIGHT_NOT_IDENTIFIED], ["Despliegue", row => row.identidad_observada?.deployment_id || INSIGHT_NOT_IDENTIFIED], ["Versión", row => row.identidad_observada?.release || INSIGHT_NOT_IDENTIFIED], ["Comprobado", row => adminDate(row.comprobado_en)], ["Consulta", row => row.http_status ? `HTTP ${row.http_status}` : "Sin respuesta"]], "La captura no incluye instancias.");
}

async function sampleInsightInstances(button) {
  await adminBusy(button, async () => {
    const generation = INSIGHT_GENERATION;
    const targets = [["Usuarios", CONFIG.USUARIOS_URL], ["Eventos", CONFIG.EVENTOS_URL], ["Compras", CONFIG.COMPRAS_LB_URL]].filter(([, url]) => url);
    const samples = [];
    for (const [service, base] of targets) {
      for (let round = 1; round <= 6; round++) {
        const result = await adminRequest(`${base.replace(/\/$/, "")}/diagnostico/instancia`, {}, 4000);
        if (result.stale || generation !== INSIGHT_GENERATION) return;
        samples.push({service, round, status: result.status, data: result.ok && result.data ? result.data : {}});
      }
    }
    const summary = targets.map(([service]) => {
      const counts = new Map();
      samples.filter(row => row.service === service).forEach(row => { const key = row.data.instancia || INSIGHT_NOT_IDENTIFIED; counts.set(key, (counts.get(key) || 0) + 1); });
      return `${service}: ${[...counts].map(([instance, total]) => `${instance} ×${total}`).join(", ")}`;
    }).join(" · ");
    adminNode("insight-samples").innerHTML = `<p class="notice">${adminText(summary)}</p>${adminTable(samples, [["Servicio", "service"], ["Muestra", "round"], ["Instancia", row => row.data.instancia || INSIGHT_NOT_IDENTIFIED], ["Nodo", row => row.data.node_id || INSIGHT_NOT_IDENTIFIED], ["Despliegue", row => row.data.deployment_id || INSIGHT_NOT_IDENTIFIED], ["Versión", row => row.data.release || INSIGHT_NOT_IDENTIFIED], ["Respuesta", row => row.data.comprobado_en ? adminDate(row.data.comprobado_en) : "Sin respuesta"], ["Consulta", row => row.status ? `HTTP ${row.status}` : "Sin conexión"]])}`;
  });
}

function insightNodeFor(entry, source) {
  if (!source) return INSIGHT_NOT_IDENTIFIED;
  if (entry.componente) {
    const node = source.components.find(component => component.id === entry.componente)?.nodo;
    if (node) return node;
  }
  if (!entry.instancia) return INSIGHT_NOT_IDENTIFIED;
  const nodes = new Set(source.components.filter(component => insightInstance(component) === entry.instancia && component.nodo).map(component => component.nodo));
  return nodes.size === 1 ? [...nodes][0] : INSIGHT_NOT_IDENTIFIED;
}

async function renderInsightTrace(trace) {
  const correlations = new Set(adminList(trace.correlaciones));
  const captured = insightAge(ADMIN_SNAPSHOT?.capturado_en);
  if (correlations.size && (captured === null || captured > 30)) await refreshAdminSnapshot();
  const source = insightSource();
  const services = {ordenes: "Compras", outbox: "Compras", pagos_simulados: "Compras", entradas: "Compras", compras: "Compras", purchase_idempotency: "Compras", journal_compras: "Compras", "reservas_stock (Eventos)": "Eventos"};
  const http = (source?.metrics || []).flatMap(row => adminList(row.recientes).filter(entry => correlations.has(entry.correlation_id)).map(entry => ({
    momento: entry.momento, etapa: `${entry.metodo} ${entry.ruta}`, fuente: "HTTP en memoria", estado: `HTTP ${entry.status}`, instancia: row.instancia || null,
    orden_local: `Proceso ${row.instancia || INSIGHT_NOT_IDENTIFIED}`, relacion: "Comparte correlación HTTP con la operación; no fija el orden respecto a otros servicios",
    servicio: row.servicio, componente: row.componente, duracion_ms: entry.latencia_ms, detalle: "Petición registrada por la instancia que la atendió; la marca temporal corresponde a su finalización."})));
  const steps = [...adminList(trace.eventos), ...http].sort((a, b) => {
    const left = Date.parse(a.momento), right = Date.parse(b.momento);
    return (Number.isFinite(left) ? left : Infinity) - (Number.isFinite(right) ? right : Infinity);
  });
  let previous = null;
  const rendered = steps.map(entry => {
    const moment = Date.parse(entry.momento);
    const interval = Number.isFinite(moment) && previous !== null ? `+${Math.max(0, moment - previous)} ms desde el registro anterior` : "No calculable";
    if (Number.isFinite(moment)) previous = moment;
    return `<article class="admin-timeline-entry trace-step"><time>${adminText(entry.momento ? adminDate(entry.momento) : "Sin marca temporal registrada")}</time><h3>${adminText(entry.etapa)}<span class="trace-source">${adminText(entry.fuente)}</span></h3><p>${adminText(entry.detalle || entry.estado)}</p>${adminFacts([["Servicio", entry.servicio || services[entry.fuente] || INSIGHT_NOT_IDENTIFIED], ["Instancia", entry.instancia || INSIGHT_NOT_IDENTIFIED], ["Nodo físico", insightNodeFor(entry, source)], ["Orden local", entry.orden_local || "No registrado"], ["Reloj lógico", entry.reloj_logico ?? "No registrado"], ["Relación causal", entry.relacion || "Relación causal no determinada"], ["Resultado", entry.estado], ["Duración observada", entry.duracion_ms !== undefined ? `${entry.duracion_ms} ms` : INSIGHT_NOT_AVAILABLE], ["Intervalo físico", interval]])}</article>`;
  }).join("");
  const operations = adminList(trace.operaciones);
  adminNode("trace-result").innerHTML = `<div class="trace-head"><strong>Operación ${adminText(trace.identificador)}</strong>${adminState(trace.estado)}</div><p class="field-help">Operaciones: ${adminText(operations.join(" · ") || "Sin identificadores de operación")}. Correlaciones HTTP: ${correlations.size}. Peticiones HTTP encontradas en memoria: ${http.length}; las instancias solo conservan las 80 más recientes.</p>${renderInsightSaga(trace.saga)}${renderInsightIdempotency(trace.idempotencia)}<h4>Eventos y relojes</h4><dl class="concept-grid"><div><dt>Hora física</dt><dd>Momento según el reloj del sistema que lo registró.</dd></div><div><dt>Orden local</dt><dd>Secuencia observada dentro de un proceso o registro.</dd></div><div><dt>Reloj lógico</dt><dd>Contador por proceso que solo se ajusta al recibir un valor propagado. Se reinicia con él.</dd></div><div><dt>Relación causal</dt><dd>Solo existe si puede acreditarse por información propagada entre eventos.</dd></div></dl><p class="field-help">Se listan por hora física para facilitar la lectura. Entre computadoras o procesos distintos, ese orden no demuestra el orden real de ejecución.</p>${rendered || adminEmpty("No hay pasos registrados.")}<p class="notice">Hitos sin evidencia: ${adminText(adminList(trace.faltantes).join(" · ") || "Ninguno informado")}.</p><p class="field-help">${adminText(adminList(trace.limitaciones).join(" · "))}</p>`;
}

const SAGA_STATES = {pendiente: ["Pendiente", "○"], completado: ["Completado", "✓"], fallido: ["Fallido", "✕"], compensado: ["Compensado", "↺"], recuperacion_requerida: ["Recuperación requerida", "!"], desconocido: ["Estado desconocido", "?"]};

function insightSagaStep(step) {
  const key = Object.hasOwn(SAGA_STATES, step.estado) ? step.estado : "desconocido";
  const origin = step.origen === "trabajo_recuperacion" ? " · trabajo de recuperación" : step.origen === "solicitud_http" ? " · solicitud HTTP" : "";
  return `<li class="saga-step saga-${key}"><span class="saga-icon" aria-hidden="true">${SAGA_STATES[key][1]}</span><div><strong>${adminText(step.paso)}</strong><small>${SAGA_STATES[key][0]}${step.momento ? ` · ${adminText(adminDate(step.momento))}` : ""}${origin}</small><p>${adminText(step.evidencia)}</p></div></li>`;
}

function renderInsightSaga(saga) {
  const steps = adminList(saga?.pasos), compensation = adminList(saga?.compensaciones);
  if (!steps.length && !compensation.length) return `<h4>Saga</h4>${adminEmpty("No hay registros suficientes para reconstruir la Saga.")}`;
  return `<h4>Saga ${saga.tipo === "compra_directa" ? "de la compra directa" : "del pedido"}</h4>${saga.incierto ? '<p class="notice notice-warning">Hay pasos sin resultado confirmado. Estado pendiente de verificación: no significa que hayan fallado.</p>' : ""}<ol class="saga-track">${steps.map(insightSagaStep).join("")}</ol>${compensation.length ? `<h4>Compensación</h4><ol class="saga-track">${compensation.map(insightSagaStep).join("")}</ol>` : ""}<dl class="concept-grid"><div><dt>Rollback local</dt><dd>Reversión de una transacción dentro de una base PostgreSQL.</dd></div><div><dt>Compensación Saga</dt><dd>Nueva operación de negocio que revierte efectos ya confirmados en otro servicio. Una Saga no es una transacción ACID global.</dd></div></dl>`;
}

function renderInsightIdempotency(rows) {
  return `<h4>Idempotencia</h4><div class="table-scroll">${adminTable(adminList(rows), [["Relación", "referencia"], ["Clave (final)", row => row.clave || INSIGHT_NOT_AVAILABLE], ["Estado persistido", "estado"], ["Respuesta guardada", row => row.respuesta_http ? `HTTP ${row.respuesta_http}` : "Sin respuesta persistida"], ["Registrado", row => adminDate(row.creado_en)], ["Intentos", row => row.intentos ?? "No registrado"]], "No hay evidencia idempotente asociada.")}</div>`;
}

async function loadInsightInventory() {
  if (!adminAllowed()) return;
  const target = adminNode("insight-inventory");
  const generation = INSIGHT_GENERATION;
  target.textContent = "Consultando inventario…";
  const result = await adminRequest(`${CONFIG.EVENTOS_URL}/inventario/estado`, {}, 15000);
  if (result.stale || generation !== INSIGHT_GENERATION) return;
  if (!result.ok || !Array.isArray(result.data?.eventos)) { target.innerHTML = `<p class="notice notice-warning">Consulta no disponible: no se puede afirmar el estado del inventario. ${adminText(adminError(result, ""))}</p>`; return; }
  const states = {consistente: ["✓ Consistente", "ok"], inconsistente: ["✕ Inconsistente", "bad"], no_verificable: ["? No verificable", "unknown"]};
  const verdict = key => { const [label, tone] = states[key] || states.no_verificable; return `<span class="inv-verdict inv-${tone}">${label}</span>`; };
  const bar = (free, held, sold) => {
    const total = free + held + sold;
    return total > 0 ? `<div class="inv-bar" aria-hidden="true"><span class="inv-free" style="width:${free / total * 100}%"></span><span class="inv-held" style="width:${held / total * 100}%"></span><span class="inv-sold" style="width:${sold / total * 100}%"></span></div>` : "";
  };
  const cards = result.data.eventos.map(row => `<article class="inv-card"><div class="inv-head"><strong>${adminText(row.nombre || `Evento #${row.evento_id}`)}</strong>${verdict(row.verificacion)}</div>${bar(Number(row.disponible) || 0, Number(row.reservadas) || 0, Number(row.vendidas) || 0)}${adminFacts([["Capacidad inicial", row.inicial ?? "No registrada"], ["Disponibles", row.disponible], ["Reservadas", row.reservadas], ["Confirmadas", row.vendidas]])}${adminList(row.zonas).length ? `<div class="table-scroll">${adminTable(adminList(row.zonas), [["Zona", "zona"], ["Capacidad", "capacidad"], ["Disponibles", "disponibles"], ["Reservadas", "reservadas"], ["Confirmadas", "confirmadas"], ["Resultado", zone => (states[zone.verificacion] || states.no_verificable)[0]]])}</div>` : ""}</article>`).join("");
  target.innerHTML = `<p class="field-help">Observado: ${adminText(adminDate(result.data.observado_en))}. Leyenda: verde disponibles, ámbar reservadas, gris confirmadas.</p><div class="inv-grid">${cards || adminEmpty("No hay eventos registrados.")}</div>`;
}

async function loadInsightRecovery() {
  if (!adminAllowed()) return;
  const target = adminNode("insight-recovery");
  const generation = INSIGHT_GENERATION;
  const result = await adminRequest(`${ADMIN_API()}/recuperacion`);
  if (result.stale || generation !== INSIGHT_GENERATION) return;
  if (!result.ok || !result.data?.disponible) { target.innerHTML = adminEmpty(result.data?.nota || adminError(result, "Recuperación no consultable.")); return; }
  const modes = {adelante: "Recuperación hacia adelante", compensacion: "Compensación", por_determinar: "Por determinar"};
  const traceButton = id => `<button type="button" class="button button-outline" data-insight-trace="${adminText(id)}">Ver recorrido</button>`;
  const pending = adminList(result.data.pendientes), recovered = adminList(result.data.recuperadas);
  target.innerHTML = `<h4>Operaciones incompletas</h4>${pending.map(row => `<article class="recovery-row">${adminFacts([["Operación", row.operaciones?.[0] || INSIGHT_NOT_IDENTIFIED], ["Pedido asociado", row.orden_id || "Compra directa"], ["Último estado conocido", row.ultimo_estado], ["Reserva remota conocida", row.reserva_remota], ["Paso pendiente", row.paso_pendiente], ["Modalidad", modes[row.modalidad] || row.modalidad], ["Intervención", row.intervencion], ["Última actualización", adminDate(row.actualizado_en)]])}${traceButton(row.referencia)}</article>`).join("") || adminEmpty("No hay operaciones incompletas registradas en esta consulta.")}<h4>Recuperaciones observadas</h4>${recovered.map(row => `<article class="recovery-row"><p><strong>${adminText(modes[row.modalidad] || row.modalidad)}</strong>: ${adminText(adminList(row.secuencia).join(" → "))}</p>${adminFacts([["Pedido", row.orden_id], ["Estado anterior", row.estado_anterior || "Sin registro"], ["Acción observada", row.accion_observada], ["Estado posterior", row.estado_posterior], ["Última actualización", adminDate(row.actualizado_en)]])}${traceButton(row.orden_id)}</article>`).join("") || adminEmpty("No hay transiciones de recuperación en los registros revisados.")}<p class="field-help">${adminText(result.data.nota)}</p>`;
  target.querySelectorAll("[data-insight-trace]").forEach(button => button.addEventListener("click", () => {
    const form = adminNode("trace-form");
    form.elements.identificador.value = button.dataset.insightTrace;
    form.requestSubmit();
    form.scrollIntoView({block: "start", behavior: "smooth"});
  }));
}

function renderInsightSync() {
  const target = adminNode("insight-sync"), source = insightSource();
  const metrics = source?.metrics || [];
  const callers = new Map(metrics.filter(row => row.servicio === "compras").flatMap(row => adminList(row.recientes).map(entry => [entry.correlation_id, {...entry, instancia: row.instancia}])));
  const rows = metrics.filter(row => row.servicio === "eventos").flatMap(row => adminList(row.recientes).filter(entry => entry.metodo === "POST" && /reserv|confirmar|devolver|liberar/i.test(entry.ruta)).map(entry => ({...entry, receptor: row.instancia}))).sort((a, b) => Date.parse(b.momento) - Date.parse(a.momento)).slice(0, 5);
  if (!rows.length) {
    target.innerHTML = '<p class="comm-example"><strong>Ejemplo ilustrativo, no es una medición:</strong> Compras envía POST a Eventos para reservar y espera su respuesta antes de avanzar el pedido. Todavía no hay llamadas de reserva en la memoria de las instancias consultadas.</p>';
    return;
  }
  target.innerHTML = `<div class="table-scroll">${adminTable(rows, [["Emisor", row => { const caller = callers.get(row.correlation_id); return caller ? `Compras · ${caller.instancia}` : `Compras · ${INSIGHT_NOT_IDENTIFIED}`; }], ["Receptor", row => `Eventos · ${row.receptor || INSIGHT_NOT_IDENTIFIED}`], ["Operación", row => `${row.metodo} ${row.ruta}`], ["Solicitud", row => adminDate(new Date(Date.parse(row.momento) - row.latencia_ms).toISOString())], ["Respuesta", row => adminDate(row.momento)], ["Tiempo en Eventos", row => `${row.latencia_ms} ms`], ["Estado", row => `HTTP ${row.status}`]])}</div><p class="field-help">Medido por la instancia receptora. Compras depende de esta respuesta para continuar; el emisor solo se identifica si su registro comparte correlación.</p>`;
}

async function loadInsightOutbox() {
  if (!adminAllowed()) return;
  const request = ++INSIGHT_OUTBOX_REQUEST;
  const result = await adminRequest(`${ADMIN_API()}/outbox?limit=10`);
  if (result.stale || request !== INSIGHT_OUTBOX_REQUEST) return;
  const target = adminNode("insight-async");
  if (!result.ok || !result.data?.disponible) { target.innerHTML = adminEmpty(result.data?.nota || adminError(result, "Outbox no consultable.")); return; }
  const states = {pendiente: "Registrado · procesamiento pendiente", notificacion_interna_registrada: "Notificación interna registrada"};
  target.innerHTML = `<p class="notice">Pendientes de procesar: ${adminText(result.data.pendientes)}</p><div class="table-scroll">${adminTable(adminList(result.data.registros), [["Evento", "tipo"], ["Generó", row => `${row.productor} · ${row.instancia || INSIGHT_NOT_IDENTIFIED}`], ["Registrado", row => adminDate(row.registrado_en)], ["Procesado", row => row.procesado_en ? adminDate(row.procesado_en) : "Sin evidencia"], ["Estado conocido", row => states[row.estado] || row.estado]], "No hay eventos registrados en el outbox.")}</div><p class="field-help">${adminText(result.data.nota)}</p>`;
}

async function submitInsightConcurrency(event) {
  event.preventDefault();
  const form = event.currentTarget;
  await adminBusy(form.querySelector("button"), async () => {
    const eventId = Number(new FormData(form).get("evento_id"));
    const result = await adminRequest(`${ADMIN_API()}/concurrencia${eventId > 0 ? `?evento_id=${eventId}` : ""}`);
    if (result.stale) return;
    const target = adminNode("insight-concurrency");
    if (!result.ok || !result.data?.disponible) { target.innerHTML = adminEmpty(result.data?.nota || adminError(result, "Concurrencia no consultable.")); return; }
    const results = {conflicto_disponibilidad: "Conflicto de disponibilidad", reserva_aceptada: "Reserva aceptada", sin_resultado: "Sin resultado registrado", cancelada_o_compensada: "Cancelada o compensada"};
    const evidence = {conflicto_con_contraparte: "Conflicto registrado con una reserva aceptada", conflicto_sin_contraparte: "Conflicto registrado; contraparte no encontrada", mismo_recurso_sin_conflicto: "Mismo recurso, sin conflicto registrado"};
    const groups = adminList(result.data.grupos);
    target.innerHTML = `<p class="field-help">Pedidos analizados: ${adminText(result.data.pedidos_analizados)} de un máximo de ${adminText(result.data.limite)}. Comparaciones encontradas: ${adminText(result.data.grupos_totales)}.</p>${groups.map(group => `<section class="concurrency-group"><h4>${adminText(eventName(group.evento_id))} · ${adminText(group.tipo_recurso)}: ${adminText(group.recurso)}</h4><p>${adminState(evidence[group.evidencia] || group.evidencia)} <small>${adminText(group.total_operaciones)} operación(es)</small></p><div class="table-scroll">${adminTable(adminList(group.operaciones), [["Operación", row => row.operacion_id || INSIGHT_NOT_IDENTIFIED], ["Creación persistida", row => adminDate(row.inicio_registrado)], ["Resultado", row => results[row.resultado] || row.resultado], ["Conflicto", row => row.conflicto || "Sin conflicto registrado"], ["Reserva", row => row.estado_reserva || "Sin evidencia"], ["Estado final", "estado_final"]])}</div></section>`).join("") || adminEmpty("Faltan evidencias: los pedidos analizados no contienen operaciones comparables sobre un mismo recurso.")}<p class="field-help">${adminText(adminList(result.data.limitaciones).join(" · "))}</p>`;
  });
}

function renderInsightsFromSnapshot() {
  renderInsightTopology(); renderInsightProcesses(); renderInsightInstances(); renderInsightSync(); renderPersistencePanels(); renderResiliencePanels(); renderLabPanels();
}

async function insightsAfterCluster() {
  if (!adminAllowed() || !clusterVisible()) return;
  renderInsightTopology(); renderInsightProcesses(); renderResiliencePanels(); renderNodeLoss(); renderHaPanels(); refreshResIncidents();
  // Sin inventario multinodo, el Observador no entrega componentes: se usa la captura operativa.
  const captureAge = insightAge(ADMIN_SNAPSHOT?.capturado_en);
  if (!adminList(ADMIN_CLUSTER?.cluster?.nodos).length && (captureAge === null || captureAge > 60) && !INSIGHT_SNAPSHOT_PENDING) {
    INSIGHT_SNAPSHOT_PENDING = true;
    try { await refreshAdminSnapshot(); } finally { INSIGHT_SNAPSHOT_PENDING = false; }
  }
}

function clearInsights() {
  INSIGHT_GENERATION += 1;
  clearPersistencePanels();
  clearResiliencePanels();
  clearLabPanels();
  clearHaPanels();
  clearSecurityPanel();
  clearAcademicPanel();
  INSIGHT_SELECTED = null; INSIGHT_GROUPS = []; INSIGHT_EDGES = [];
  INSIGHT_RESIZE?.disconnect(); INSIGHT_RESIZE = null;
  ["insight-topology", "insight-topology-detail", "insight-processes", "insight-instances", "insight-samples", "insight-sync", "insight-async", "insight-concurrency", "insight-inventory", "insight-recovery"].forEach(id => { const node = adminNode(id); if (node) node.textContent = ""; });
}

function initializeInsights() {
  installSectionHelp();
  initializePersistence();
  initializeLab();
  renderHaScenarios();
  const refresh = adminNode("insight-topology-refresh");
  refresh.addEventListener("click", () => adminBusy(refresh, async () => {
    if (adminList(ADMIN_CLUSTER?.cluster?.nodos).length) await refreshAdminCluster();
    else await refreshAdminSnapshot();
  }));
  const sample = adminNode("insight-sample");
  sample.addEventListener("click", () => sampleInsightInstances(sample));
  const outbox = adminNode("insight-outbox-refresh");
  outbox.addEventListener("click", () => adminBusy(outbox, async () => { await refreshObservability(); }));
  adminNode("insight-concurrency-form").addEventListener("submit", submitInsightConcurrency);
  const inventory = adminNode("insight-inventory-refresh");
  inventory.addEventListener("click", () => adminBusy(inventory, loadInsightInventory));
  const recovery = adminNode("insight-recovery-refresh");
  recovery.addEventListener("click", () => adminBusy(recovery, loadInsightRecovery));
}
