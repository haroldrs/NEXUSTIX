"use strict";

// Organiza por unidad lo que ya existe. Sin reporte registrado, nada se marca como demostrado.
const ACADEMIC_STATES = {implementado: "Implementado", configurado: "Configurado", observado: "Observado", demostrado: "Demostrado", propuesto: "Propuesto", pendiente: "Pendiente de verificar"};
const ACADEMIC_UNITS = {
  "Unidad I": {title: "Introducción, procesos y comunicación", topics: [
    {name: "Qué es un sistema distribuido", text: "Tres computadoras autónomas (Fedora y Windows) que cooperan por mensajes y se ven como una sola tienda.", concept: "Autonomía, transparencia, escalabilidad, heterogeneidad", feature: "Centro de Control · computadoras del inventario", state: "implementado", section: "cluster"},
    {name: "Componentes", text: "Nodos, red (Tailscale), procesos, recursos compartidos (bases), protocolos (HTTP/JSON) y middleware (HAProxy, Docker).", concept: "Elementos de un sistema distribuido", feature: "Mapa de la arquitectura", state: "implementado", section: "observability"},
    {name: "Identificación de procesos", text: "PID, estado, hilos, memoria, CPU y cambios de contexto del proceso real de cada servicio en cada PC, leídos de /proc.", concept: "Proceso, PCB, planificador", feature: "Centro de Control · procesos, contenedores y servicios", state: "implementado", section: "cluster", observe: "instances"},
    {name: "Ciclo de vida del proceso", text: "Un servicio espera solicitudes (bloqueado) y pasa a ejecución al recibir una. docker stop envía la señal SIGTERM (fin ordenado); docker kill envía SIGKILL (fin inmediato).", concept: "Estados y señales", feature: "Comandos de la demostración", state: "implementado", section: "observability"},
    {name: "Microservicios", text: "Usuarios, Eventos y Compras: procesos independientes, cada uno con su base.", concept: "Descomposición en servicios", feature: "Microservicios y salud", state: "implementado", section: "services", observe: "instances"},
    {name: "Contenedores", text: "Cada proceso corre aislado en su contenedor, con su propio espacio de PID y de red.", concept: "Aislamiento", feature: "Compose generado por inventario", state: "implementado", section: "cluster"},
    {name: "IPC local", text: "Dentro de cada PC: PostgreSQL usa memoria compartida y semáforos entre sus procesos; la terminal usa tuberías (docker logs | grep); Docker usa señales.", concept: "Memoria compartida, semáforos, tuberías, señales", feature: "PostgreSQL y persistencia", state: "implementado", section: "databases"},
    {name: "Sockets", text: "Toda la comunicación entre PC viaja por sockets TCP: HTTP de las APIs, replicación de PostgreSQL y votación de etcd.", concept: "Socket", feature: "Puertos de cada PC", state: "implementado", section: "cluster"},
    {name: "RPC sobre REST", text: "Compras invoca a Eventos (reservar, confirmar, liberar) como una llamada remota con respuesta.", concept: "Llamada a procedimiento remoto", feature: "Seguir una compra", state: "implementado", section: "observability"},
    {name: "Comunicación síncrona y asíncrona", text: "La reserva espera respuesta (síncrona); los avisos pendientes se guardan en el outbox y se envían después (asíncrona, como una cola).", concept: "Modelos de comunicación", feature: "Comunicación síncrona y asíncrona", state: "implementado", section: "observability"},
    {name: "Comunicación en grupo", text: "Cada instancia envía su latido a todas las demás cada 2 s (difusión a un grupo).", concept: "Multidifusión", feature: "Coordinación distribuida · directorio", state: "implementado", section: "coordination"},
    {name: "Balanceo", text: "HAProxy reparte entre las instancias de las 3 PC y retira las que no responden.", concept: "Balanceo y redundancia", feature: "Balanceo de APIs", state: "implementado", section: "cluster", scenarios: ["B", "C", "D"]},
    {name: "Publicación/suscripción", text: "No se usa un broker (Kafka, RabbitMQ): con tres servicios bastan HTTP y outbox, y un broker sería otro componente que puede fallar.", concept: "Pub/sub", feature: "Propuesta de mejora", state: "propuesto", section: "observability"},
  ]},
  "Unidad II": {title: "Nombres, sincronización, consistencia, replicación y tolerancia a fallas", topics: [
    {name: "Servicio de nombres", text: "Directorio replicado: cada instancia se registra, late y resuelve servicio → direcciones vivas con TTL (caché).", concept: "Nombrado plano con caché", feature: "Coordinación distribuida · directorio", state: "implementado", section: "coordination"},
    {name: "Relojes físicos", text: "Cristian estima el desfase contra el coordinador; Berkeley promedia y envía ajustes.", concept: "Sincronización física", feature: "Coordinación distribuida · Cristian y Berkeley", state: "implementado", section: "coordination"},
    {name: "Relojes lógicos", text: "Lamport y vectorial viajan en cada mensaje de la Saga; se comparan dos eventos para ver causalidad o concurrencia.", concept: "Relojes lógicos", feature: "Coordinación distribuida · relojes lógicos", state: "implementado", section: "coordination"},
    {name: "Elección de líder", text: "Algoritmo del matón (Bully). El anillo no se implementó: con pocas instancias Bully es más simple y rápido.", concept: "Elección", feature: "Coordinación distribuida · coordinador", state: "implementado", section: "coordination"},
    {name: "Exclusión mutua", text: "Centralizada: la base principal bloquea la fila del evento (FOR UPDATE) al reservar. Si cae, otra principal toma su lugar.", concept: "Exclusión mutua", feature: "Operaciones concurrentes", state: "implementado", section: "observability", scenarios: ["A"]},
    {name: "Consenso y quórum", text: "etcd (Raft) decide qué base es principal con mayoría 2 de 3; nunca hay dos principales.", concept: "Consenso", feature: "Centro de Control · alta disponibilidad", state: "implementado", section: "cluster"},
    {name: "Saga", text: "Pasos y compensaciones entre Compras y Eventos, en lugar de 2PC (que bloquea si cae el coordinador).", concept: "Transacción distribuida sin 2PC", feature: "Saga del pedido", state: "implementado", section: "observability", scenarios: ["E", "F", "P"]},
    {name: "Idempotencia", text: "Una clave durable evita pedidos y cobros duplicados al reintentar.", concept: "Reintento seguro", feature: "Idempotencia en el recorrido", state: "implementado", section: "observability", scenarios: ["F"]},
    {name: "Replicación", text: "Pasiva (primario-respaldo): tres copias de cada base, una síncrona y una asíncrona. Los servicios son activos: las tres copias atienden a la vez.", concept: "Replicación pasiva y activa", feature: "PostgreSQL y persistencia", state: "implementado", section: "databases", observe: "replication", scenarios: ["H", "M"]},
    {name: "Modelos de consistencia", text: "Fuerte en la principal y la réplica síncrona; eventual en la asíncrona y en el directorio; orden causal con reloj vectorial. Ante partición elige consistencia (CP).", concept: "Linealizable, causal, eventual, CAP", feature: "Consistencia y snapshots", state: "implementado", section: "consistency"},
    {name: "Invariante del inventario", text: "Inicial = disponibles + reservadas + vendidas, en cada evento.", concept: "Invariantes", feature: "Inventario por evento", state: "implementado", section: "consistency", scenarios: ["A"]},
    {name: "WAL y checkpoints", text: "Registro anticipado, LSN, retraso de réplica y puntos de recuperación.", concept: "Recuperación hacia atrás", feature: "Checkpoints y WAL", state: "implementado", section: "databases", observe: "wal", scenarios: ["J"]},
    {name: "Detección de fallos", text: "Latidos con plazo T + Δ, sondeos del balanceador cada 3 s y llave de líder con TTL de 30 s.", concept: "Detección", feature: "Coordinación distribuida · latidos", state: "implementado", section: "coordination"},
    {name: "Recuperación automática", text: "Patroni promueve la réplica síncrona (~30 s, RPO 0); pg_rewind reincorpora la antigua principal; los pedidos pendientes se retoman solos (hacia adelante).", concept: "Failover y recuperación", feature: "Centro de Control e Historial de incidentes", state: "implementado", section: "cluster", scenarios: ["G", "I", "N", "O"]},
    {name: "Tipos de falla", text: "Se toleran fallas por caída y de temporización. Las bizantinas requieren 3k+1 nodos y firma de mensajes: quedan como límite.", concept: "Crash, temporización, valor, bizantina", feature: "Laboratorio de Resiliencia", state: "implementado", section: "lab"},
    {name: "Métricas y experimentos", text: "RTO, RPO, latencia y disponibilidad medidos en las caídas de PC.", concept: "Evaluación", feature: "Laboratorio de Resiliencia", state: "implementado", section: "lab"},
  ]},
  "Unidad III": {title: "Seguridad, modelos de implantación y casos de estudio", topics: [
    {name: "Tríada CIA", text: "Confidencialidad (Argon2id, secretos), integridad (JWT y QR firmados, invariante) y disponibilidad (3 PC, failover).", concept: "Confidencialidad, integridad, disponibilidad", feature: "Seguridad e implantación", state: "implementado", section: "security"},
    {name: "Autenticación", text: "Contraseña con Argon2id y token JWT RS256 de 10 min; la contraseña viaja solo al iniciar sesión, luego se usa el token (idea similar a los tickets de Kerberos).", concept: "Autenticación distribuida", feature: "Inicio de sesión", state: "implementado", section: "security"},
    {name: "MFA", text: "Verificación en dos pasos con código TOTP de Google o Microsoft Authenticator: algo que sabes y algo que tienes.", concept: "Autenticación multifactor", feature: "Usuarios y roles · Mi verificación en dos pasos", state: "implementado", section: "users"},
    {name: "Fuerza bruta y reenvío", text: "Bloqueo temporal tras 5 intentos fallidos (compartido por las 3 PC); códigos MFA, QR y claves de idempotencia de un solo uso.", concept: "Ataques de fuga, alteración y reenvío", feature: "Usuarios y roles", state: "implementado", section: "users"},
    {name: "Control de acceso (RBAC)", text: "Comprador, organizador y administrador, comprobados en el backend.", concept: "RBAC y privilegio mínimo", feature: "Usuarios y roles", state: "implementado", section: "users"},
    {name: "Protección en tránsito", text: "Entre PC el tráfico va cifrado y autenticado por Tailscale (WireGuard); dentro de cada PC, entre contenedores, va sin cifrar. HTTPS en el navegador queda como mejora.", concept: "TLS y mTLS", feature: "Seguridad e implantación · comunicaciones", state: "configurado", section: "security"},
    {name: "Defensa de la red", text: "Firewall de Windows limitado a Tailscale y solo los puertos necesarios. No hay IDS: el Historial de incidentes alerta caídas, no ataques.", concept: "Firewall e IDS", feature: "Historial de incidentes", state: "configurado", section: "incidents"},
    {name: "Gestión de secretos", text: "Claves en archivos fuera del código; el algoritmo es público y el secreto está en la clave.", concept: "Secretos", feature: "Credenciales", state: "implementado", section: "security"},
    {name: "Modelos de arquitectura", text: "Cliente-servidor, multinivel (presentación, negocio, datos) y microservicios; híbrido: PC iguales pero una base principal por dominio.", concept: "Arquitecturas distribuidas", feature: "Mapa de la arquitectura", state: "implementado", section: "observability"},
    {name: "Modelos de computación e implantación", text: "Computación en clúster, implantación local (on-premises) en las PC del laboratorio unidas por red privada. Despliegue en nube preparado, no usado.", concept: "Clúster, on-premises, nube", feature: "Centro de Control", state: "implementado", section: "cluster"},
    {name: "Auditoría", text: "Accesos denegados, cambios de rol, MFA y bloqueos quedan registrados sin datos secretos.", concept: "Trazabilidad", feature: "Seguridad e implantación · auditoría", state: "implementado", section: "security"},
    {name: "Caso de estudio", text: "Venta distribuida de entradas con concurrencia, caída de PC y seguridad; comparación con GFS, Dynamo, Spanner, Kafka y Netflix.", concept: "Integración", feature: "Pestaña Casos de estudio", state: "implementado", section: "evidence"},
  ]},
};
const ACADEMIC_CASES = [
  ["Google File System", "Archivos en fragmentos replicados 3 veces; un maestro coordina.", "Cada base tiene 3 copias en PC distintas; Patroni + etcd hacen de coordinador, pero sin un maestro único: se elige por votación."],
  ["Amazon Dynamo", "Prioriza disponibilidad (AP): siempre acepta escrituras y reconcilia después.", "NEXUSTIX prioriza consistencia (CP): vender un asiento dos veces es peor que esperar 30 s."],
  ["Google Spanner", "Transacciones globales con relojes muy precisos (TrueTime) y consenso Paxos.", "Relojes con Cristian/Berkeley y orden con Lamport/vectorial; consenso Raft en etcd; Saga en vez de transacción global."],
  ["Apache Kafka", "Publicación/suscripción: los servicios se desacoplan mediante un registro de eventos.", "El outbox cumple el papel de cola durable; un broker queda como mejora si crecen los servicios."],
  ["Netflix", "Aislamiento de fallos, reintentos, circuit breaker y Chaos Monkey (fallas provocadas).", "Compras tiene circuit breaker y reintentos idempotentes hacia Eventos; el modo degradado aísla fallas; el Laboratorio de Resiliencia provoca fallas controladas."],
];
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
  return `<div class="table-scroll"><table class="data-table"><thead><tr><th scope="col">Escenario</th><th scope="col">Nombre</th><th scope="col">Estado</th><th scope="col">Reporte</th></tr></thead><tbody>${rows.map(row => `<tr><td>${adminText(row.id)}</td><td>${adminText(row.name)}</td><td>${adminText(row.status)}</td><td>${row.report ? `<button type="button" class="button button-outline" data-open-evidence="${adminText(row.report.id)}">Ver</button>` : "—"}</td></tr>`).join("")}</tbody></table></div><p class="field-help">Un reporte registrado indica que existe una prueba capturada; su resultado lo evalúa quien la ejecutó. Resultados medidos: docs/RESULTADOS_ALTA_DISPONIBILIDAD.md.</p>`;
}

function academicCasesHtml() {
  return `<h3>Casos de estudio · comparación con sistemas reales</h3><div class="table-scroll"><table class="data-table"><thead><tr><th scope="col">Sistema</th><th scope="col">Qué hace</th><th scope="col">En NEXUSTIX</th></tr></thead><tbody>${ACADEMIC_CASES.map(row => `<tr>${row.map(cell => `<td>${adminText(cell)}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;
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
  const tabs = ["Inicio", ...Object.keys(ACADEMIC_UNITS), "Casos de estudio", "Evidencias"];
  adminNode("academic-tabs").innerHTML = tabs.map(tab => `<button type="button" class="button button-outline" data-academic-tab="${adminText(tab)}" aria-pressed="${tab === ACADEMIC_TAB}">${adminText(tab)}</button>`).join("");
  const unit = ACADEMIC_UNITS[ACADEMIC_TAB];
  adminNode("academic-content").innerHTML = unit ? `<h3>${adminText(ACADEMIC_TAB)} · ${adminText(unit.title)}</h3><div class="academic-grid">${unit.topics.map(academicTopicCard).join("")}</div>` : ACADEMIC_TAB === "Evidencias" ? academicEvidenceHtml() : ACADEMIC_TAB === "Casos de estudio" ? academicCasesHtml() : academicHomeHtml();
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
