"use strict";

// Organiza por unidad lo que ya existe. Sin reporte registrado, nada se marca como demostrado.
const ACADEMIC_STATES = {implementado: "Implementado", configurado: "Configurado", observado: "Observado", demostrado: "Demostrado", propuesto: "Propuesto", pendiente: "Pendiente de verificar"};
const ACADEMIC_UNITS = {
  "Unidad I": {title: "Procesos y comunicación", topics: [
    {name: "Arquitectura distribuida", text: "Mapa de computadoras, servicios y conexiones según el inventario.", concept: "Sistema distribuido", feature: "Mapa interactivo", state: "implementado", section: "cluster"},
    {name: "Identificación de procesos", text: "Instancia, nodo y métricas de cada proceso que respondió.", concept: "Proceso", feature: "Procesos, contenedores y servicios", state: "pendiente", section: "cluster", observe: "instances"},
    {name: "Microservicios", text: "Usuarios, Eventos y Compras con bases separadas.", concept: "Descomposición en servicios", feature: "Microservicios y salud", state: "implementado", section: "services", observe: "instances"},
    {name: "Contenedores", text: "Cada servicio se despliega con Docker Compose.", concept: "Aislamiento", feature: "Compose e inventario", state: "configurado", section: "cluster"},
    {name: "Comunicación HTTP y protocolos", text: "Las APIs se comunican por HTTP con JSON.", concept: "Comunicación entre procesos", feature: "Comunicación síncrona y asíncrona", state: "implementado", section: "observability"},
    {name: "Red distribuida", text: "Direcciones y puertos por inventario, sin IP fijas en el código.", concept: "Diseño de red", feature: "Acceso al sistema", state: "configurado", section: "cluster"},
    {name: "Balanceo", text: "HAProxy reparte entre instancias y retira las que no responden.", concept: "Balanceo y redundancia", feature: "Balanceo de APIs", state: "implementado", section: "cluster", scenarios: ["B", "C", "D"]},
    {name: "Trazabilidad de compras", text: "Recorrido de un pedido a partir de registros durables.", concept: "Correlación", feature: "Seguir una compra o pedido", state: "pendiente", section: "observability"},
    {name: "Identificación de instancias", text: "Muestras reales de qué instancia responde.", concept: "Replicación de procesos", feature: "Instancias que responden", state: "pendiente", section: "observability", observe: "instances"},
    {name: "Síncrona y asíncrona", text: "Reserva con respuesta esperada frente a registros del outbox.", concept: "Modelos de comunicación", feature: "Comunicación síncrona y asíncrona", state: "pendiente", section: "observability"},
  ]},
  "Unidad II": {title: "Consistencia y tolerancia a fallos", topics: [
    {name: "Servicio de nombres", text: "DNS de Docker e inventario; no hay un servicio propio.", concept: "Nombrado", feature: "Inventario y resolutores", state: "configurado", section: "cluster"},
    {name: "Sincronización y relojes", text: "Hora física, orden local y reloj lógico por separado.", concept: "Relojes lógicos", feature: "Eventos y relojes", state: "pendiente", section: "observability"},
    {name: "Concurrencia", text: "Comparación de operaciones sobre un mismo recurso.", concept: "Condición de carrera", feature: "Operaciones concurrentes", state: "pendiente", section: "observability", scenarios: ["A"]},
    {name: "Exclusión mutua", text: "Bloqueo FOR UPDATE de la fila del evento al reservar.", concept: "Exclusión mutua", feature: "Reserva en Eventos", state: "implementado", section: "consistency", scenarios: ["A"]},
    {name: "Consistencia de inventario", text: "Inicial = disponible + reservadas + confirmadas.", concept: "Invariantes", feature: "Inventario por evento", state: "pendiente", section: "observability", scenarios: ["A"]},
    {name: "Saga", text: "Pasos y compensaciones entre Compras y Eventos.", concept: "Transacción distribuida sin 2PC", feature: "Saga del pedido", state: "implementado", section: "observability", scenarios: ["E", "F", "P"]},
    {name: "Idempotencia", text: "Una clave durable evita pedidos duplicados.", concept: "Reintento seguro", feature: "Idempotencia en el recorrido", state: "implementado", section: "observability", scenarios: ["F"]},
    {name: "Réplicas PostgreSQL", text: "Primaria y standbys según inventario y observación.", concept: "Replicación", feature: "Replicación por base", state: "configurado", section: "databases", observe: "replication", scenarios: ["H", "M"]},
    {name: "WAL", text: "LSN y diferencia en bytes con la réplica.", concept: "Registro anticipado", feature: "Métricas de replicación", state: "implementado", section: "databases", observe: "wal", scenarios: ["J"]},
    {name: "Checkpoints", text: "Puntos internos de recuperación; no son respaldos.", concept: "Recuperación local", feature: "Checkpoints y WAL", state: "implementado", section: "databases", observe: "checkpoints", scenarios: ["J"]},
    {name: "Detección de fallos", text: "Sondeos con clasificación por evidencia.", concept: "Detección", feature: "Tolerancia a fallos", state: "pendiente", section: "cluster"},
    {name: "Recuperación", text: "Pedidos por trabajo durable; bases por procedimiento manual.", concept: "Recuperación", feature: "Recuperación de pedidos y guía manual", state: "implementado", section: "lab", scenarios: ["G", "N", "P"]},
    {name: "Modo degradado", text: "Cada operación depende de sus servicios observados.", concept: "Funcionamiento degradado", feature: "Funcionamiento degradado", state: "pendiente", section: "cluster"},
    {name: "Caída de computadoras", text: "Proyección por nodo y comparación con experimentos.", concept: "Fallos físicos", feature: "Si una computadora deja de funcionar", state: "pendiente", section: "cluster", scenarios: ["I", "O"]},
    {name: "Métricas y experimentos", text: "RTO, RPO, latencia y disponibilidad con su tipo de dato.", concept: "Evaluación", feature: "Laboratorio de Resiliencia", state: "pendiente", section: "lab"},
    {name: "Consenso y quórum", text: "Solo explicación y plantilla inactiva: no hay coordinador instalado.", concept: "Consenso", feature: "Preparación para alta disponibilidad", state: "propuesto", section: "cluster"},
  ]},
  "Unidad III": {title: "Seguridad e implantación", topics: [
    {name: "Autenticación", text: "Argon2id, JWT RS256 y refresh en cookie HttpOnly.", concept: "Identificación", feature: "Inicio de sesión", state: "implementado", section: "security"},
    {name: "Autorización y roles", text: "Comprador, organizador y administrador, aplicados en el backend.", concept: "Control de acceso", feature: "Permisos", state: "implementado", section: "security"},
    {name: "Protección de QR", text: "El código es una credencial de un solo uso.", concept: "Protección de credenciales", feature: "Entradas QR", state: "pendiente", section: "security"},
    {name: "Gestión de secretos", text: "Archivos de secreto fuera del repositorio.", concept: "Secretos", feature: "Credenciales", state: "implementado", section: "security"},
    {name: "Comunicaciones", text: "HTTPS opcional; el tráfico interno no está cifrado.", concept: "Protección en tránsito", feature: "Comunicaciones", state: "configurado", section: "security"},
    {name: "Modelos de implantación", text: "Local, LAN y externo opcional.", concept: "Implantación", feature: "Implantación", state: "configurado", section: "security"},
    {name: "Auditoría de acciones", text: "Acciones sensibles sin datos secretos.", concept: "Trazabilidad", feature: "Auditoría", state: "pendiente", section: "security"},
    {name: "Pruebas de seguridad", text: "Autenticación probada; permisos y QR por comprobar.", concept: "Verificación", feature: "docs/PREPARACION_EXPOSICION.md", state: "pendiente", section: "evidence"},
    {name: "Caso de estudio", text: "Venta distribuida con concurrencia y tolerancia a fallos.", concept: "Integración", feature: "docs/PREPARACION_EXPOSICION.md", state: "propuesto", section: "evidence"},
  ]},
};
let ACADEMIC_TAB = "Inicio";
let ACADEMIC_REPORTS = null;
let ACADEMIC_SCENARIOS = [];

function academicObserved(key) {
  const snapshot = ADMIN_SNAPSHOT;
  if (!snapshot || pgStale(snapshot)) return false;
  const databases = adminList(snapshot.persistencia).filter(row => row.disponible === true);
  if (key === "instances") return adminList(snapshot.componentes).some(row => row.tipo === "api" && row.instancia);
  if (key === "replication") return databases.some(row => adminList(row.replicas).length);
  if (key === "wal") return databases.some(row => row.wal);
  return key === "checkpoints" && databases.some(row => row.ultimo_checkpoint);
}

function academicVerifiedReport(row) {
  return row.origen === "captura" && row.fase === "final";
}

function academicState(topic) {
  if (adminList(topic.scenarios).some(id => adminList(ACADEMIC_REPORTS).some(row => row.escenario === id && academicVerifiedReport(row)))) return "demostrado";
  return topic.observe && academicObserved(topic.observe) ? "observado" : topic.state;
}

function academicTopicCard(topic) {
  const state = academicState(topic);
  return `<article class="academic-topic"><div class="academic-head"><strong>${adminText(topic.name)}</strong><span class="academic-state academic-${state}">${ACADEMIC_STATES[state]}</span></div><p>${adminText(topic.text)}</p><small>Concepto: ${adminText(topic.concept)}</small><small>En NEXUSTIX: ${adminText(topic.feature)}</small><button type="button" class="button button-outline" data-academic-open="${adminText(topic.section)}">Abrir</button></article>`;
}

function academicScenarioRows() {
  const reports = adminList(ACADEMIC_REPORTS);
  return ACADEMIC_SCENARIOS.map(scenario => {
    const rows = reports.filter(row => row.escenario === scenario.id);
    const verified = rows.find(academicVerifiedReport);
    return {id: scenario.id, name: scenario.nombre, status: verified ? "Con reporte registrado" : rows.length ? "Sin resultados verificables" : "Pendiente de ejecución", report: verified || rows[0] || null};
  });
}

function academicEvidenceHtml() {
  if (ACADEMIC_REPORTS === null) return adminEmpty("No se pudieron consultar los experimentos registrados.");
  const rows = academicScenarioRows();
  return `<div class="table-scroll"><table class="data-table"><thead><tr><th scope="col">Escenario</th><th scope="col">Nombre</th><th scope="col">Estado</th><th scope="col">Reporte</th></tr></thead><tbody>${rows.map(row => `<tr><td>${adminText(row.id)}</td><td>${adminText(row.name)}</td><td>${adminText(row.status)}</td><td>${row.report ? `<button type="button" class="button button-outline" data-open-evidence="${adminText(row.report.id)}">Ver</button>` : "—"}</td></tr>`).join("")}</tbody></table></div><p class="field-help">Un reporte registrado indica que existe una prueba capturada; su resultado lo evalúa quien la ejecutó. Información faltante y lista de evidencias: docs/MATRIZ_CUMPLIMIENTO_SISOPE2.md y docs/PREPARACION_EXPOSICION.md. Línea base anterior a las últimas mejoras: docs/RESULTADOS_DISTRIBUIDO.md.</p>`;
}

function academicHomeHtml() {
  const units = Object.entries(ACADEMIC_UNITS).map(([name, unit]) => {
    const states = unit.topics.map(academicState);
    const count = key => states.filter(state => state === key).length;
    return `<article class="academic-topic"><strong>${adminText(name)} · ${adminText(unit.title)}</strong><small>${unit.topics.length} temas · ${count("demostrado")} demostrados · ${count("observado")} observados · ${count("pendiente")} por verificar</small><button type="button" class="button button-outline" data-academic-tab="${adminText(name)}">Abrir unidad</button></article>`;
  }).join("");
  const rows = academicScenarioRows();
  const done = rows.filter(row => row.status === "Con reporte registrado").length;
  return `<div class="academic-grid">${units}</div><h4>Demostraciones</h4><div class="snap-modes"><button type="button" class="button button-outline" data-academic-open="cluster">Centro de Control</button><button type="button" class="button button-outline" data-academic-open="observability">Observabilidad</button><button type="button" class="button button-outline" data-academic-open="lab">Laboratorio de Resiliencia</button></div><p class="field-help">Evidencias: ${ACADEMIC_REPORTS === null ? "no disponibles" : `${done} de ${rows.length} escenarios con reporte registrado`}. Implementado significa que existe código; Demostrado exige una prueba registrada.</p>`;
}

function renderAcademicPanel() {
  const tabs = ["Inicio", ...Object.keys(ACADEMIC_UNITS), "Evidencias"];
  adminNode("academic-tabs").innerHTML = tabs.map(tab => `<button type="button" class="button button-outline" data-academic-tab="${adminText(tab)}" aria-pressed="${tab === ACADEMIC_TAB}">${adminText(tab)}</button>`).join("");
  const unit = ACADEMIC_UNITS[ACADEMIC_TAB];
  adminNode("academic-content").innerHTML = unit ? `<h3>${adminText(ACADEMIC_TAB)} · ${adminText(unit.title)}</h3><div class="academic-grid">${unit.topics.map(academicTopicCard).join("")}</div>` : ACADEMIC_TAB === "Evidencias" ? academicEvidenceHtml() : academicHomeHtml();
  document.querySelectorAll("#view-admin [data-admin-panel='academic'] [data-academic-tab]").forEach(button => button.addEventListener("click", () => { ACADEMIC_TAB = button.dataset.academicTab; renderAcademicPanel(); }));
  document.querySelectorAll("#view-admin [data-admin-panel='academic'] [data-academic-open]").forEach(button => button.addEventListener("click", () => openAdminSection(button.dataset.academicOpen)));
  bindEvidenceLinks(adminNode("academic-content"));
}

async function loadAcademicPanel() {
  const [reports, scenarios] = await Promise.all([adminRequest(`${ADMIN_API()}/evidencias?limit=200`), adminRequest(`${ADMIN_API()}/escenarios`)]);
  if (reports.stale || scenarios.stale) return;
  ACADEMIC_REPORTS = reports.ok ? adminList(reports.data?.evidencias) : null;
  ACADEMIC_SCENARIOS = scenarios.ok ? adminList(scenarios.data?.escenarios) : [];
  renderAcademicPanel();
  if (!ADMIN_SNAPSHOT) { await refreshAdminSnapshot(); if (adminAllowed()) renderAcademicPanel(); }
}

function clearAcademicPanel() {
  ACADEMIC_REPORTS = null; ACADEMIC_SCENARIOS = []; ACADEMIC_TAB = "Inicio";
  ["academic-tabs", "academic-content"].forEach(id => { const node = adminNode(id); if (node) node.textContent = ""; });
}
