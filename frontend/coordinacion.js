"use strict";

// Muestra preparación y limitaciones. No promueve bases, no cambia rutas y no sondea
// direcciones desde el navegador: todo procede de la captura del servidor.
const HA_REQUIREMENT_STATES = {cumplido: "✓ Cumplido según la declaración", pendiente: "✕ Pendiente", no_configurado: "– No configurado", no_verificable: "? No verificable"};
const HA_PROFILES = {local: "Local", distribuido: "Distribuido", alta_disponibilidad: "Alta disponibilidad preparada"};
const HA_ACCESS_MODES = {manual: "Direcciones alternativas manuales", external_dns: "DNS externo", external_proxy: "Proxy o balanceador externo"};
const HA_ACCESS_DEPENDENCIES = {
  manual: "Ninguna externa. La persona debe cambiar de dirección si cae el gateway que usa.",
  external_dns: "Servicio DNS externo. El cambio de destino depende de caché, TTL y detección: no es instantáneo.",
  external_proxy: "Proxy externo, que a su vez debe ser redundante para no convertirse en un punto único de fallo.",
};
const HA_SCENARIOS = [
  {name: "A · Falla de API", current: "HAProxy retira la instancia cuando no supera sus comprobaciones.", projection: "Otra instancia saludable puede atender las solicitudes nuevas.", recovery: "La instancia vuelve y supera sus comprobaciones.", affected: "Solicitudes en vuelo hacia esa instancia.", guarantees: "No se reenvían escrituras no idempotentes.", limits: "Requiere otra réplica con sus dependencias disponibles."},
  {name: "B · Falla de PostgreSQL primaria", current: "Las operaciones que necesitan ese escritor se bloquean.", projection: "La base queda sin escritor hasta una recuperación manual.", recovery: "Aislar la primaria anterior y promover una standby elegible por el procedimiento manual.", affected: "Las escrituras del servicio afectado y los flujos que dependen de él.", guarantees: "Una standby no acepta escrituras mientras siga en recuperación.", limits: "Sin promoción automática. La replicación asíncrona puede perder commits."},
  {name: "C · Falla de una computadora física", current: "Se pierden las aplicaciones y bases alojadas en ella.", projection: "El resultado depende de qué servicios y primarias sobreviven según el inventario.", recovery: "Procedimiento manual por cada base que perdió su primaria.", affected: "Consulta la proyección por computadora del Centro de Control.", guarantees: "Los estados durables se conservan en las bases que sobreviven.", limits: "Una falta de respuesta no demuestra apagado."},
  {name: "D · Partición de red", current: "Cada grupo conserva solo lo que puede alcanzar; no hay elección automática.", projection: "Nodos vivos pero incomunicados, con servicios parcialmente disponibles.", recovery: "Restablecer la red y verificar roles antes de reanudar.", affected: "Operaciones que cruzan la partición.", guarantees: "Sin promoción automática no surgen dos escritoras por esta causa.", limits: "Una promoción manual sin aislamiento efectivo sí podría producir split-brain."},
  {name: "E · Pérdida de quórum", current: "No aplica: no hay coordinador de consenso instalado.", projection: "Con coordinación real, sin mayoría no podría autorizarse una nueva elección.", recovery: "Recuperar la mayoría de votantes.", affected: "Las elecciones de líder, no las lecturas de la primaria vigente.", guarantees: "Una minoría no puede autorizar un líder.", limits: "El número de nodos encendidos no indica que una elección sea segura."},
  {name: "F · Pérdida del gateway de entrada", current: "Otro gateway puede seguir funcionando.", projection: "La persona necesita una ruta accesible para llegar a él.", recovery: "Entrar por una dirección alternativa o por un acceso estable.", affected: "Quienes usaban esa dirección.", guarantees: "Las operaciones con resultado desconocido se consultan antes de repetirse.", limits: "Sin acceso estable no hay redirección automática."},
  {name: "G · Recuperación del nodo antiguo", current: "El arranque de una antigua primaria aislada está bloqueado por sus marcadores.", projection: "Puede contener una historia WAL divergente.", recovery: "Reconstruirlo como standby con una generación nueva.", affected: "La redundancia hasta que termine la reincorporación.", guarantees: "No vuelve como primaria sin reconciliación.", limits: "Encenderlo no significa que esté reincorporado."},
];

function haCoordinationChips(coordinator) {
  const chips = [];
  if (coordinator.estado === "no_configurado") chips.push(["Coordinación no configurada", false]);
  if (coordinator.mayoria_requerida) chips.push([`Quórum declarado: mayoría de ${coordinator.mayoria_requerida} sobre ${adminList(coordinator.votantes_declarados).length} votantes`, false]);
  chips.push([coordinator.quorum_observado == null ? "Quórum observado: desconocido" : `Quórum observado: ${coordinator.quorum_observado}`, false]);
  chips.push(["Promoción no autorizada", true], ["Recuperación manual requerida", true]);
  return `<div class="ha-chips">${chips.map(([text, block]) => `<span class="${block ? "is-block" : ""}">${adminText(text)}</span>`).join("")}</div>`;
}

function renderHaStatus(cluster, coordinator, source) {
  const bases = adminList(cluster?.bases);
  const profile = source?.profile || CONFIG.DEPLOYMENT_PROFILE;
  const nodes = adminList(cluster?.nodos);
  const domains = [...new Set(nodes.map(node => node.failure_domain).filter(Boolean))];
  const facts = [["Perfil de despliegue", HA_PROFILES[profile] || "No informado"], ["Computadoras", nodes.map(node => node.id).join(" · ") || "Sin inventario multinodo"], ["Dominios físicos", domains.join(" · ") || "No declarados"], ["Replicación existente", bases.length ? `${bases.filter(row => adminList(row.standbys).length).length} de ${bases.length} bases con standby` : "No determinable"], ["Recuperación manual", "Documentada en RECUPERACION.md"], ["Coordinador", coordinator.mensaje || (coordinator.estado === "preparado_inactivo" ? "Plan declarado, inactivo" : coordinator.estado === "observado" ? "Patroni observado" : "Estado desconocido")], ["Votantes declarados", adminList(coordinator.votantes_declarados).map(row => `${row.nodo} (${row.dominio})`).join(" · ") || "Ninguno"], ["Mayoría tras perder un dominio", coordinator.conserva_mayoria_tras_perder_dominio == null ? "No configurado" : coordinator.conserva_mayoria_tras_perder_dominio ? "Se conservaría, según la declaración" : "No se conservaría"], ["Recuperación automática", "No disponible"], ["Alta disponibilidad operativa", "No observada"]];
  adminNode("ha-status").innerHTML = `${haCoordinationChips(coordinator)}${adminFacts(facts)}${adminList(coordinator.motivos).length ? `<h4>Por qué no se autoriza una promoción</h4><ul class="pg-steps">${adminList(coordinator.motivos).map(reason => `<li>${adminText(reason)}</li>`).join("")}</ul>` : ""}${nodes.length === 1 ? `<p class="notice notice-warning">${adminText(RES_LOCAL_NODE_LIMIT)}</p>` : ""}`;
}

function renderHaAccess(cluster, source) {
  const access = cluster?.acceso || {};
  const points = adminList(access.alternativas);
  const mode = access.modo || "manual";
  const gatewayState = node => {
    const gateway = source?.components.find(row => row.tipo === "gateway" && row.nodo === node);
    return gateway && source ? resState(gateway, source).label : "Sin observación";
  };
  const rows = points.map(point => {
    let parsed = null;
    try { parsed = new URL(point.url); } catch (_) { /* Una dirección inválida se muestra sin marcarla como entrada activa. */ }
    return {node: point.node, url: point.url, role: point.preferred ? "Principal configurada" : "Alternativa configurada", state: gatewayState(point.node), used: parsed?.hostname === location.hostname ? "Sí" : "No"};
  });
  const primary = access.url_estable || points.find(point => point.preferred)?.url || "Sin dirección declarada";
  const limit = points.length > 1 && !access.url_estable ? "Dos gateways no equivalen a una entrada altamente disponible: si cae el que usas, esta pestaña no se redirige sola y hay que cambiar la dirección manualmente." : points.length === 1 ? "Un solo gateway declarado: sin alternativa si cae." : "El acceso estable requiere comprobarse experimentalmente.";
  adminNode("ha-access").innerHTML = `${adminFacts([["Modo de entrada", HA_ACCESS_MODES[mode] || mode], ["Dirección principal", primary], ["Esquema", access.url_estable ? (access.esquema === "https" ? "HTTPS declarado" : "HTTP") : "Según el acceso de cada gateway"], ["Acceso estable verificado", access.verificado === true ? "Sí" : "No verificado"], ["Dependencias externas", HA_ACCESS_DEPENDENCIES[mode] || "No informadas"], ["Dominios de esa infraestructura", adminList(access.dominios_infraestructura).join(" · ") || "No aplica"]])}<div class="table-scroll">${adminTable(rows, [["Gateway", "node"], ["Dirección", "url"], ["Configuración", "role"], ["Estado observado", "state"], ["Este navegador entró por aquí", "used"]], "Sin gateways declarados en este observador.")}</div><p class="notice notice-warning">${adminText(limit)}</p><p class="field-help">La URL pública del navegador, las URLs internas, la replicación PostgreSQL y la coordinación son direcciones separadas. Una dirección declarada no significa disponible.</p>`;
}

function renderHaPanels() {
  if (!adminAllowed()) return;
  const cluster = ADMIN_CLUSTER?.cluster, source = insightSource();
  if (!cluster) {
    adminNode("ha-status").innerHTML = '<p class="notice notice-warning">Estado de coordinación desconocido: sin respuesta del observador.</p>';
    ["ha-requirements", "ha-bases", "ha-access"].forEach(id => { adminNode(id).textContent = ""; });
    return;
  }
  const coordinator = cluster.coordinador || {};
  renderHaStatus(cluster, coordinator, source);
  adminNode("ha-requirements").innerHTML = adminTable(adminList(coordinator.requisitos), [["Requisito", "requisito"], ["Estado", row => HA_REQUIREMENT_STATES[row.estado] || row.estado], ["Detalle", "detalle"]], "Sin inventario multinodo: no se evalúan requisitos de alta disponibilidad.");
  adminNode("ha-bases").innerHTML = adminTable(adminList(cluster.bases), [["Base", "base"], ["Primaria configurada", "primario_configurado"], ["Primaria observada", row => row.primario_observado?.nodo || "Sin evidencia"], ["Standbys", row => adminList(row.standbys).map(item => item.nodo).join(" · ") || "Ninguna"], ["Escritor", row => row.escritura?.estado || "No comprobado"], ["Si se pierde la primaria", () => "Recuperación manual requerida"]], "Sin bases en el inventario.");
  renderHaAccess(cluster, source);
}

function renderHaScenarios() {
  adminNode("ha-scenarios").innerHTML = HA_SCENARIOS.map(row => `<details class="ha-scenario"><summary>${adminText(row.name)}</summary>${adminFacts([["Comportamiento actual", row.current], ["Proyección", row.projection], ["Requisitos de recuperación", row.recovery], ["Funciones afectadas", row.affected], ["Garantías disponibles", row.guarantees], ["Limitaciones", row.limits]])}</details>`).join("");
}

function clearHaPanels() {
  ["ha-status", "ha-requirements", "ha-bases", "ha-access"].forEach(id => { const node = adminNode(id); if (node) node.textContent = ""; });
}
