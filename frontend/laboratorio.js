"use strict";

// Laboratorio: organiza, observa y documenta. Ninguna función de este archivo actúa
// sobre máquinas, contenedores, redes o bases; los experimentos son manuales.
const LAB_CATEGORIES = {procesos: "Fallas de procesos", comunicacion: "Fallas de comunicación", bases_datos: "Fallas de bases de datos", fisicas: "Fallas físicas", comerciales: "Fallas comerciales"};
const LAB_RISKS = {bajo: "Bajo", medio: "Medio", alto: "Alto"};
const LAB_MEASURE_KINDS = {objetivo: "Objetivo", medicion: "Medición real", medicion_declarada: "Declarada por el operador", estimacion: "Estimación", proyeccion: "Proyección", desconocido: "Dato desconocido"};
const LAB_RECOVERY_STAGES = [["falla", "Falla detectada"], ["aislamiento", "Aislamiento verificado"], ["replica", "Réplica evaluada"], ["promocion", "Promoción manual"], ["conexiones", "Conexiones actualizadas"], ["servicios", "Servicios recuperados"], ["integridad", "Integridad verificada"]];
const LAB_DOMAIN_OPERATIONS = {usuarios: "Registro e inicio de sesión", eventos: "Catálogo, reservas y administración de eventos", compras: "Pedidos, pagos simulados, entradas y validación"};
const LAB_FAILURE_CAPTURES = new Map();
const LAB_DECLARED_STAGES = new Set();

function labOptionsHtml(scenarios) {
  const groups = Object.entries(LAB_CATEGORIES).map(([key, label]) => {
    const rows = scenarios.filter(item => item.categoria === key);
    return rows.length ? `<optgroup label="${adminText(label)}">${rows.map(item => `<option value="${adminText(item.id)}">${adminText(item.id)} · ${adminText(item.nombre)}</option>`).join("")}</optgroup>` : "";
  }).join("");
  const others = scenarios.filter(item => !LAB_CATEGORIES[item.categoria]).map(item => `<option value="${adminText(item.id)}">${adminText(item.id)} · ${adminText(item.nombre)}</option>`).join("");
  return groups + others;
}

function labScenarioExtras(scenario) {
  const initial = ADMIN_INITIAL_CAPTURES.get(scenario.id), failure = LAB_FAILURE_CAPTURES.get(scenario.id);
  const profile = CONFIG.DEPLOYMENT_PROFILE || "No informado";
  const stages = [
    ["1. Estado inicial", initial ? `Registrado: ${adminDate(initial.fecha)}` : "Pendiente de captura"],
    ["2. Procedimiento", "Se realiza manualmente fuera de esta interfaz, según la acción descrita"],
    ["3. Observación posterior", failure ? `Registrada: ${adminDate(failure.fecha)}` : "Pendiente de captura"],
    ["4. Conclusión", "Se guarda con la captura final y sus métricas"],
  ];
  return `${adminFacts([["Categoría", LAB_CATEGORIES[scenario.categoria] || "Sin categoría"], ["Perfiles compatibles", adminList(scenario.perfiles).join(" · ") || "No indicado"], ["Perfil de este despliegue", profile], ["Riesgo del experimento", LAB_RISKS[scenario.riesgo] || "No indicado"], ["Dependencias afectadas", adminList(scenario.dependencias).join(" · ") || "No indicadas"], ["Criterios de recuperación", scenario.recuperacion || "No indicados"], ["Métricas aplicables", adminList(scenario.metricas).join(" · ") || "No indicadas"]])}<h4>Flujo de demostración</h4><ol class="lab-stages">${stages.map(([label, state]) => `<li><strong>${adminText(label)}</strong><small>${adminText(state)}</small></li>`).join("")}</ol><p class="field-help">Las capturas son lecturas tomadas en momentos distintos: no forman un estado global atómico.</p>`;
}

function labMeasures(data) {
  const text = name => String(data.get(name) || "").trim();
  const count = name => { const value = text(name); return value === "" || !Number.isFinite(Number(value)) ? null : Number(value); };
  const moment = name => { const value = text(name), parsed = new Date(value); return value && Number.isFinite(parsed.getTime()) ? parsed.toISOString() : null; };
  return {inicio_falla: moment("inicio_falla"), restauracion: moment("restauracion"), operacion: text("operacion"), intentadas: count("intentadas"), correctas: count("correctas"), fallidas: count("fallidas"), rto_objetivo_segundos: count("rto_objetivo_segundos"), rpo_objetivo: text("rpo_objetivo"), ultima_transaccion_reconocida: text("ultima_transaccion_reconocida"), ultima_transaccion_recuperada: text("ultima_transaccion_recuperada"), perdida_observada: text("perdida_observada")};
}

function labFinalFields(scenario, data) {
  const failure = LAB_FAILURE_CAPTURES.get(scenario);
  return {...(failure ? {referencia_falla: failure.id} : {}), mediciones: labMeasures(data), pasos_recuperacion: [...LAB_DECLARED_STAGES], ...(data.get("nodo_objetivo") ? {nodo_objetivo: String(data.get("nodo_objetivo"))} : {})};
}

async function captureLabFailure(button) {
  await adminBusy(button, async () => {
    const scenario = adminNode("lab-scenario").value, initial = ADMIN_INITIAL_CAPTURES.get(scenario);
    if (!initial) { adminNotice("lab-report-status", "Captura primero el estado inicial de este escenario.", "warning"); return; }
    const result = await adminRequest(`${ADMIN_API()}/evidencias`, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({escenario: scenario, fase: "observacion", referencia_inicial: initial.id, acciones: "Captura posterior a la falla; sin acciones de infraestructura desde la interfaz.", observado: "Consulta de los componentes tras el procedimiento manual.", conclusion: "Pendiente de la captura final."})}, 45000);
    if (result.stale) return;
    if (!result.ok) { adminNotice("lab-report-status", `No se guardó la captura: ${adminError(result)} Puedes exportar la última consulta desde "Última captura técnica"; al importarla quedará marcada como no verificada.`, "error"); return; }
    LAB_FAILURE_CAPTURES.set(scenario, result.data);
    renderLabScenario();
    adminNotice("lab-report-status", "Observación posterior guardada.", "success");
  });
}

function labReportHtml(report) {
  if (!report.metricas && !report.comparacion) return "";
  const comparison = adminList(report.comparacion);
  const names = value => adminList(value).join(" · ") || "Ninguno";
  const projection = report.proyeccion_vs_observado;
  const declared = report.mediciones_operador || {};
  const origin = adminFacts([["Unidad académica", report.unidad_academica || "Unidad II"], ["Observado por el sistema", "Capturas de estado, incidentes, latencias y replicación"], ["Declarado manualmente", `Acciones, conclusión${declared.operacion ? ", operación probada" : ""}${declared.inicio_falla ? ", marcas de tiempo" : ""}${declared.perdida_observada ? ", pérdida de datos" : ""}`], ["Resultado", report.resultado === "sin_evaluar" ? "Sin evaluar: lo determina quien ejecutó el experimento" : report.resultado || "Sin evaluar"]]);
  return `<h3>Datos del experimento</h3>${origin}<h3>Comparación antes y después</h3><div class="table-scroll">${adminTable(comparison, [["Etapa", "fase"], ["Fecha", row => adminDate(row.fecha)], ["Disponibles", row => adminList(row.saludables).length], ["Degradados", row => names(row.degradados)], ["Sin respuesta", row => names(row.sin_respuesta)], ["Desconocidos", row => adminList(row.desconocidos).length], ["Operaciones pendientes", row => row.pendientes?.disponible ? `Outbox: ${row.pendientes.outbox_pendiente} · Pedidos: ${JSON.stringify(row.pendientes.ordenes || {})}` : "No disponible"]])}</div><h3>Métricas</h3><div class="table-scroll">${adminTable(adminList(report.metricas), [["Grupo", "grupo"], ["Métrica", "metrica"], ["Valor", "valor"], ["Tipo de dato", row => LAB_MEASURE_KINDS[row.tipo] || row.tipo], ["Nota", row => row.nota || "—"]])}</div>${projection ? `<h3>Proyección frente a resultado observado</h3>${adminFacts([["Computadora", projection.nodo], ["Proyección: componentes afectados", names(projection.previstos)], ["Observado: sin respuesta o degradados", names(projection.observados)], ["Previstos sin confirmar", names(projection.previstos_no_observados)], ["Observados no previstos", names(projection.observados_no_previstos)]])}<p class="field-help">La proyección procede del inventario; el resultado, de las capturas.</p>` : ""}${report.experimento ? adminFacts([["Perfil", report.experimento.perfil || "No informado"], ["Nodos involucrados", names(report.experimento.nodos)], ["Pasos declarados por el operador", names(report.pasos_recuperacion)], ["Incidentes registrados", report.experimento.incidentes ?? "No disponible"]]) : ""}`;
}

function renderLabDiagnosis() {
  const target = adminNode("lab-diagnosis");
  const snapshot = ADMIN_SNAPSHOT;
  if (!snapshot) { target.innerHTML = adminEmpty("Sin captura: el estado de las primarias es desconocido."); return; }
  const stale = pgStale(snapshot);
  const rows = pgModels(snapshot).map(model => ({
    base: model.name, configured: model.node,
    observed: stale ? "Desactualizado" : model.assignment?.primario_observado?.nodo || (pgRole(model).key === "primary" ? "Rol primario observado; nodo no identificado" : "Sin evidencia"),
    declared: adminList(model.assignment?.standbys).map(row => row.nodo).join(" · ") || "Ninguna declarada",
    channels: model.standbys.filter(row => row.canal_observado).map(row => `${row.nodo || row.application_name}: ${row.estado_canal}`).join(" · ") || "Sin canal observado",
    wal: model.latest?.wal?.actual_lsn || PG_NOT_AVAILABLE,
    time: model.latest?.comprobado_en ? adminDate(model.latest.comprobado_en) : "Sin evidencia",
    operations: LAB_DOMAIN_OPERATIONS[model.name] || "No identificadas",
  }));
  target.innerHTML = adminTable(rows, [["Base", "base"], ["Primaria configurada", "configured"], ["Primaria observada", "observed"], ["Standbys declaradas", "declared"], ["Standbys observadas", "channels"], ["LSN primaria", "wal"], ["Última observación", "time"], ["Operaciones afectadas", "operations"]]);
}

function labStageObservation(stage, source) {
  if (!source || source.stale) return false;
  const databases = source.components.filter(row => row.tipo === "postgresql");
  if (stage === "falla") return databases.some(row => row.estado !== "saludable") || source.components.some(row => row.tipo === "api" && resState(row, source).key === "bad");
  if (stage === "promocion") return adminList(ADMIN_SNAPSHOT?.infraestructura?.bases).some(row => row.primario_observado?.nodo && row.primario_observado.nodo !== row.primario_configurado);
  if (stage === "servicios") return resOperations(source).every(row => row.state === "disponible");
  return false;
}

function renderLabRecovery() {
  const source = insightSource();
  adminNode("lab-recovery-stages").innerHTML = LAB_RECOVERY_STAGES.map(([key, label]) => {
    const observed = labStageObservation(key, source), declared = LAB_DECLARED_STAGES.has(key);
    const state = observed ? "Confirmada mediante observación" : declared ? "Declarada manualmente por el operador" : ["falla", "servicios"].includes(key) ? "Pendiente" : "Documentada";
    return `<li class="${observed ? "is-observed" : declared ? "is-declared" : ""}"><strong>${adminText(label)}</strong><small>${adminText(state)}</small><label><input type="checkbox" data-lab-stage="${key}" ${declared ? "checked" : ""}> Declarar</label></li>`;
  }).join("");
}

function labProjections() {
  return adminList(ADMIN_CLUSTER?.infraestructura?.proyecciones || ADMIN_SNAPSHOT?.infraestructura?.proyecciones);
}

function labSyncNodeOptions(projections) {
  const select = adminNode("lab-node-select"), experiment = adminNode("lab-target-node");
  const selected = select.value, chosen = experiment.value;
  const option = row => `<option value="${adminText(`${row.tipo}:${row.objetivo}`)}">${adminText(row.objetivo)}</option>`;
  const nodes = projections.filter(row => row.tipo === "nodo"), domains = projections.filter(row => row.tipo === "dominio_fallo");
  select.innerHTML = `${nodes.length ? `<optgroup label="Computadoras">${nodes.map(option).join("")}</optgroup>` : ""}${domains.length ? `<optgroup label="Dominios físicos declarados">${domains.map(option).join("")}</optgroup>` : ""}`;
  if ([...select.options].some(item => item.value === selected)) select.value = selected;
  experiment.innerHTML = `<option value="">Sin computadora objetivo</option>${nodes.map(row => `<option value="${adminText(row.objetivo)}">${adminText(row.objetivo)}</option>`).join("")}`;
  if ([...experiment.options].some(item => item.value === chosen)) experiment.value = chosen;
}

function renderNodeLoss() {
  const target = adminNode("lab-node-loss");
  const source = insightSource(), projections = labProjections();
  labSyncNodeOptions(projections);
  adminNode("lab-node-select").hidden = !projections.length;
  if (!source) { target.innerHTML = adminEmpty("Sin captura disponible."); return; }
  if (!projections.length) { target.innerHTML = `<p class="notice notice-warning">${adminText(insightIsLocal() ? RES_LOCAL_NODE_LIMIT : "Este despliegue no entrega un inventario de nodos: no se puede proyectar la pérdida de una computadora.")}</p>`; return; }
  const projection = projections.find(row => `${row.tipo}:${row.objetivo}` === adminNode("lab-node-select").value) || projections[0];
  const excluded = new Set(adminList(projection.nodos_excluidos));
  const lost = source.components.filter(row => row.tipo !== "ruta" && excluded.has(row.nodo));
  const named = kinds => lost.filter(row => kinds.includes(insightKind(row))).map(row => row.nombre || row.id).join(" · ") || "Ninguno";
  const bases = adminList(projection.bases);
  const writers = bases.filter(row => row.estado_escrituras !== "continuidad_condicionada");
  const operation = needs => needs.every(([domain]) => {
    const service = adminList(projection.servicios).find(row => row.servicio === domain);
    const base = bases.find(row => row.base === domain);
    return adminList(service?.instancias_configuradas_restantes).length && base?.estado_escrituras === "continuidad_condicionada";
  });
  const operations = RES_OPERATIONS.map(row => ({label: row.label, state: operation(row.needs) ? "Condicionada a las dependencias" : "Potencialmente afectada", needs: row.needs.map(([domain]) => domain).join(" · ")}));
  const shared = source.nodes.filter(node => projection.tipo === "nodo" && !excluded.has(node.id) && node.failure_domain && node.failure_domain === source.nodes.find(row => excluded.has(row.id))?.failure_domain).map(node => node.id);
  const remaining = adminList(projection.accesos_restantes);
  const hereLost = source.nodes.some(node => excluded.has(node.id) && node.address === location.hostname);
  target.innerHTML = `<p class="notice notice-warning"><strong>Proyección, no un fallo observado.</strong> ${adminText(projection.alcance || "Calculada desde el inventario.")}</p>${source.nodes.length === 1 ? `<p class="notice notice-error">${adminText(RES_LOCAL_NODE_LIMIT)}</p>` : ""}${shared.length ? `<p class="notice notice-warning">${adminText(shared.join(" · "))} comparte el mismo dominio físico declarado: no son fallos independientes.</p>` : ""}${adminFacts([["APIs alojadas", named(["api"])], ["Frontend y gateway", named(["frontend", "gateway"])], ["Bases primarias", named(["primary"])], ["Standbys", named(["standby"])], ["Bases sin escritor", writers.map(row => `${row.base} (${row.standbys_restantes?.length ? "standby sin promoción automática" : "sin copia restante"})`).join(" · ") || "Ninguna"], ["Bases con primaria restante", bases.filter(row => row.primario_restante).map(row => `${row.base}: ${row.primario_restante}`).join(" · ") || "Ninguna"]])}<h4>Servicios restantes</h4><div class="table-scroll">${adminTable(adminList(projection.servicios), [["Servicio", "servicio"], ["Instancias restantes", row => adminList(row.instancias_configuradas_restantes).join(" · ") || "Ninguna"], ["Saludables en la última captura", row => adminList(row.observadas_saludables_ahora).join(" · ") || "Ninguna"], ["Dependencias pendientes", row => adminList(row.dependencias_pendientes).join(" · ") || "Ninguna"]])}</div><h4>Capacidades comerciales</h4><div class="table-scroll">${adminTable(operations, [["Operación", "label"], ["Proyección", "state"], ["Depende de", "needs"]])}</div><h4>Acceso del navegador</h4>${hereLost ? '<p class="notice notice-warning">Estás conectado a través de esta computadora: si se pierde, esta página no podrá recibir observaciones nuevas.</p>' : ""}<div class="cluster-access-list">${remaining.map(point => `<div>${clusterSafeLink(point.url, `${point.node}: ${point.url}`)}</div>`).join("") || adminEmpty("No queda otro gateway configurado.")}</div><p class="field-help">El cambio de dirección es manual: no hay redirección automática salvo que se haya configurado un acceso estable. Que un componente esté declarado en otra computadora no asegura que siga disponible.</p>`;
}

function renderLabPanels() {
  renderLabDiagnosis(); renderLabRecovery(); renderNodeLoss();
}

function clearLabPanels() {
  LAB_FAILURE_CAPTURES.clear(); LAB_DECLARED_STAGES.clear();
  ["lab-diagnosis", "lab-recovery-stages", "lab-node-loss"].forEach(id => { const node = adminNode(id); if (node) node.textContent = ""; });
}

function initializeLab() {
  const failure = adminNode("lab-capture-failure");
  failure.addEventListener("click", () => captureLabFailure(failure));
  adminNode("lab-node-select").addEventListener("change", renderNodeLoss);
  adminNode("lab-recovery-stages").addEventListener("change", event => {
    const stage = event.target.dataset?.labStage;
    if (!stage) return;
    if (event.target.checked) LAB_DECLARED_STAGES.add(stage); else LAB_DECLARED_STAGES.delete(stage);
    renderLabRecovery();
  });
}
