"use strict";

// Vistas de solo lectura sobre la captura /admin/operaciones/estado. Ningún dato
// de este archivo se obtiene por SQL desde el navegador ni controla PostgreSQL.
const PG_DOMAINS = ["usuarios", "eventos", "compras"];
const PG_DELAY_BYTES = 16 * 1024 * 1024;
const PG_DELAY_SECONDS = 60;
const PG_STALE_SECONDS = 90;
const PG_UNDETERMINED = "No determinable";
const PG_NOT_AVAILABLE = "No disponible";
const SNAPSHOT_MODES = {
  independiente: {
    compras: "Sin pedido registrado", eventos: "Reserva registrada", canal: "Ninguno registrado",
    resumen: "Compras se captura antes de enviar y Eventos después de recibir. La reserva aparece sin una solicitud que la origine: es un corte inconsistente.",
  },
  coordinada: {
    compras: "Reserva solicitada", eventos: "Sin reserva todavía", canal: "Solicitud de reserva en tránsito",
    resumen: "Compras ya envió y Eventos aún no recibe. Registrar el mensaje en tránsito hace que la captura sea un corte consistente.",
  },
};

function pgStale(snapshot) {
  const age = insightAge(snapshot.capturado_en);
  return age === null || age > PG_STALE_SECONDS;
}

function pgBytes(value) {
  const number = Number(value);
  if (value === null || value === undefined || !Number.isFinite(number)) return PG_UNDETERMINED;
  if (number < 1024) return `${number} bytes`;
  return number < 1048576 ? `${(number / 1024).toFixed(1)} KiB` : `${(number / 1048576).toFixed(1)} MiB`;
}

function pgSeconds(value) {
  const number = Number(value);
  return value === null || value === undefined || !Number.isFinite(number) ? PG_UNDETERMINED : `${number.toFixed(3)} s`;
}

function pgModels(snapshot) {
  const components = adminList(snapshot.componentes);
  const byId = new Map(components.map(component => [component.id, component]));
  const assignments = adminList(snapshot.infraestructura?.bases);
  const names = assignments.length ? assignments.map(row => row.base) : PG_DOMAINS;
  const fallback = insightIsLocal() ? "Esta computadora" : "Nodo no declarado";
  return names.map(name => {
    const assignment = assignments.find(row => row.base === name) || null;
    const sources = adminList(snapshot.persistencia).filter(row => byId.get(row.componente)?.servicio === name || String(row.componente).startsWith(name));
    const available = sources.filter(row => row.disponible === true);
    const latest = available.reduce((best, row) => !best || String(row.comprobado_en || "") > String(best.comprobado_en || "") ? row : best, null);
    const roles = [...new Set(available.map(row => row.rol).filter(Boolean))];
    const channels = adminList(latest?.replicas);
    const channelFor = application => channels.find(row => row.application_name === application) || {};
    const standbys = assignment
      ? adminList(assignment.standbys).map(row => ({...channelFor(row.application_name), ...row}))
      : channels.map(row => ({...row, nodo: null, canal_observado: true, estado_canal: row.state}));
    return {name, assignment, sources, available, latest, roles, standbys, node: assignment?.primario_configurado || byId.get(`${name}_db`)?.nodo || fallback};
  });
}

function pgRole(model) {
  if (model.roles.length !== 1) return {key: "unknown", label: model.roles.length ? "Desconocida: se observaron roles distintos" : "Desconocida"};
  return model.roles[0] === "primario" ? {key: "primary", label: "Primaria"} : {key: "standby", label: "Standby"};
}

function pgChannel(row, stale) {
  if (stale) return {key: "old", label: "Datos de observación desactualizados"};
  if (row.canal_observado && row.estado_canal === "streaming") {
    const bytes = Number(row.lag_bytes), seconds = Number(row.replay_lag_seconds);
    const delayed = (row.lag_bytes != null && bytes > PG_DELAY_BYTES) || (row.replay_lag_seconds != null && seconds > PG_DELAY_SECONDS);
    return delayed ? {key: "warn", label: "Réplica retrasada"} : {key: "ok", label: "Replicación observada activa"};
  }
  if (row.canal_observado) return {key: "warn", label: `Canal observado sin streaming (${row.estado_canal || "sin estado"})`};
  return row.estado_observacion === "no_disponible" ? {key: "bad", label: "Conexión interrumpida"} : {key: "unknown", label: "Estado no observado"};
}

function pgMode(model) {
  const value = model.assignment?.replicacion_configurada;
  return value === "async" ? "Asíncrona (configurada)" : value === "sync" ? `Síncrona, quórum ${model.assignment.quorum_configurado} (configurada)` : "No declarada";
}

function pgReplicationCard(model, stale) {
  const role = pgRole(model), diagnostic = model.latest, wal = diagnostic?.wal || {};
  const standbys = model.standbys.map(row => {
    const channel = pgChannel(row, stale);
    return `<div class="pg-flow-link"><span class="pg-wal" aria-hidden="true">WAL →</span><div class="pg-box pg-standby"><strong>${adminText(row.nodo || "Nodo no declarado")}</strong><small>Standby configurado · ${adminText(row.application_name || "sin canal")}</small><span class="pg-chip pg-${channel.key}">${adminText(channel.label)}</span></div></div>`;
  }).join("");
  const empty = model.assignment ? "Sin standby configurado para esta base." : "No se observan canales WAL. Esto no demuestra que no exista un standby.";
  const rows = model.standbys.length ? `<div class="table-scroll">${adminTable(model.standbys, [["Standby", row => row.nodo || "Nodo no declarado"], ["Emisor WAL", row => row.state || row.estado_canal || "Sin evidencia"], ["Modo observado", row => row.sync_state || "Sin evidencia"], ["LSN enviado", row => row.sent_lsn || PG_NOT_AVAILABLE], ["LSN escrito", row => row.write_lsn || PG_NOT_AVAILABLE], ["LSN persistido", row => row.flush_lsn || PG_NOT_AVAILABLE], ["LSN aplicado", row => row.replay_lsn || PG_NOT_AVAILABLE], ["Diferencia WAL", row => pgBytes(row.lag_bytes)], ["Retraso de reproducción", row => pgSeconds(row.replay_lag_seconds)]])}</div>` : "";
  const receiver = adminList(diagnostic?.receptor_wal)[0];
  return `<article class="pg-card"><h4>${adminText(model.name)}</h4><div class="pg-flow"><div class="pg-box pg-${role.key}"><strong>${adminText(model.node)}</strong><small>Rol configurado: primaria</small><span class="pg-chip">Rol observado: ${adminText(role.label)}</span></div>${standbys || `<p class="field-help">${adminText(empty)}</p>`}</div>${adminFacts([["pg_is_in_recovery()", typeof diagnostic?.en_recuperacion === "boolean" ? String(diagnostic.en_recuperacion) : PG_NOT_AVAILABLE], ["Consulta", model.available.length ? `Disponible (${model.available.length}/${model.sources.length})` : "No disponible"], ["Modo de replicación", pgMode(model)], ["synchronous_standby_names", diagnostic?.durabilidad?.synchronous_standby_names === "" ? "Vacío" : diagnostic?.durabilidad?.synchronous_standby_names ?? PG_NOT_AVAILABLE], ["LSN actual", wal.actual_lsn || PG_NOT_AVAILABLE], ["LSN persistido", wal.persistido_lsn || PG_NOT_AVAILABLE], ["Receptor WAL", receiver?.status || (diagnostic?.en_recuperacion ? "Sin evidencia" : "No aplica en la primaria consultada")], ["Tiempo desde el último replay", diagnostic?.en_recuperacion && wal.ultimo_replay ? clusterElapsed(wal.ultimo_replay) : PG_UNDETERMINED], ["Última observación válida", diagnostic?.comprobado_en ? `${adminDate(diagnostic.comprobado_en)} · ${clusterElapsed(diagnostic.comprobado_en)}` : "Sin evidencia"]])}${rows}</article>`;
}

function pgLossProjection(snapshot, models) {
  const nodes = adminList(snapshot.infraestructura?.nodos);
  if (nodes.length < 2) return "";
  const rows = nodes.map(node => ({node: node.id, bases: models.filter(model => model.node === node.id).map(model => model.name)}));
  return `<h4>Si una computadora dejara de funcionar</h4><p class="field-help">Proyección según el inventario, no una prueba. No existe promoción automática: cada base afectada queda sin escritor hasta una recuperación manual.</p><div class="table-scroll">${adminTable(rows, [["Computadora", "node"], ["Bases que perderían su escritor", row => row.bases.join(" · ") || "Ninguna"]])}</div>`;
}

function renderPgReplication() {
  const target = adminNode("pg-replication");
  const snapshot = ADMIN_SNAPSHOT;
  if (!snapshot) { target.innerHTML = adminEmpty("Consulta no disponible: el rol y la replicación de cada base son desconocidos."); return; }
  const models = pgModels(snapshot), stale = pgStale(snapshot);
  target.innerHTML = `${stale ? `<p class="notice notice-warning">Captura desactualizada (${adminText(clusterElapsed(snapshot.capturado_en))}). Los estados no se consideran actuales.</p>` : ""}<div class="pg-grid">${models.map(model => pgReplicationCard(model, stale)).join("")}</div>${pgLossProjection(snapshot, models)}<p class="field-help">Criterio de "retrasada": más de 16 MiB de diferencia WAL o más de 60 s de retraso de reproducción. La diferencia WAL se mide en bytes y no equivale a tiempo. Un canal activo no demuestra que la réplica esté al día, y una réplica en standby no admite escrituras.</p>`;
}

function renderPgRecovery() {
  const target = adminNode("pg-recovery");
  const snapshot = ADMIN_SNAPSHOT;
  if (!snapshot) { target.innerHTML = adminEmpty("Consulta no disponible: no hay evidencia actual de checkpoints ni de archivado."); return; }
  const cards = pgModels(snapshot).map(model => {
    const diagnostic = model.latest || {}, checkpoint = diagnostic.ultimo_checkpoint || {}, counters = diagnostic.checkpoints || {}, archive = diagnostic.archivado || {}, durability = diagnostic.durabilidad || {};
    const archiveOn = durability.archive_mode === "on" || durability.archive_mode === "always";
    const conditions = [
      ["wal_level suficiente", durability.wal_level ? ["replica", "logical"].includes(durability.wal_level) : null],
      ["archive_mode activo", durability.archive_mode ? archiveOn : null],
      ["WAL archivados sin fallos recientes", archive.archived_count == null ? null : Number(archive.archived_count) > 0 && Number(archive.failed_count || 0) === 0],
      ["Respaldo base verificable", null], ["Restauración probada", null],
    ];
    const blocked = conditions.some(([, value]) => value === false);
    const moments = [["Último checkpoint", checkpoint.checkpoint_time], ["Último WAL archivado", archive.last_archived_time], ["Último fallo de archivado", archive.last_failed_time], ["Último respaldo", null]];
    return `<article class="pg-card"><h4>${adminText(model.name)}</h4><ol class="pg-timeline">${moments.map(([label, value]) => `<li><strong>${adminText(label)}</strong><small>${adminText(value ? adminDate(value) : "Sin evidencia")}</small></li>`).join("")}</ol>${adminFacts([["Checkpoints programados", counters.num_timed ?? PG_NOT_AVAILABLE], ["Checkpoints solicitados", counters.num_requested ?? PG_NOT_AVAILABLE], ["Buffers escritos", counters.buffers_written ?? PG_NOT_AVAILABLE], ["LSN de checkpoint", checkpoint.checkpoint_lsn || PG_NOT_AVAILABLE], ["Timeline", checkpoint.timeline_id ?? PG_NOT_AVAILABLE], ["wal_level", durability.wal_level || PG_NOT_AVAILABLE], ["archive_mode", durability.archive_mode || PG_NOT_AVAILABLE], ["WAL archivados", archive.archived_count ?? PG_NOT_AVAILABLE], ["Fallos de archivado", archive.failed_count ?? PG_NOT_AVAILABLE], ["Versión PostgreSQL", diagnostic.version ?? PG_NOT_AVAILABLE], ["Observado", diagnostic.comprobado_en ? adminDate(diagnostic.comprobado_en) : "Sin evidencia"]])}<h5>Condiciones para PITR</h5><ul class="pg-conditions">${conditions.map(([label, value]) => `<li><span aria-hidden="true">${value === true ? "✓" : value === false ? "✕" : "?"}</span>${adminText(label)}: ${value === true ? "observada" : value === false ? "no cumplida" : "sin evidencia"}</li>`).join("")}</ul><p class="notice ${blocked ? "notice-error" : "notice-warning"}">Recuperación a un punto en el tiempo: ${blocked ? "no disponible según la evidencia actual" : "no determinada, falta evidencia de un respaldo base y de una restauración probada"}.</p></article>`;
  }).join("");
  const asynchronous = pgModels(snapshot).some(model => model.assignment?.replicacion_configurada === "async" || !model.assignment);
  target.innerHTML = `<div class="pg-grid">${cards}</div><h4>RPO y RTO</h4><div class="table-scroll">${adminTable([{metric: "RPO", meaning: "Datos que se admite perder o pérdida medida en un experimento", note: asynchronous ? "Con replicación asíncrona no puede afirmarse RPO = 0" : "Depende del nivel síncrono configurado"}, {metric: "RTO", meaning: "Tiempo objetivo de recuperación o tiempo medido en un experimento", note: "Requiere ejecutar un experimento real"}], [["Métrica", "metric"], ["Significado", "meaning"], ["Objetivo", () => "No declarado"], ["Medición", () => "Sin medición"], ["Observación", "note"]])}</div>`;
}

function renderConsistencyData() {
  const target = adminNode("consistency-live");
  const snapshot = ADMIN_SNAPSHOT;
  if (!snapshot) { target.innerHTML = adminEmpty("Consulta no disponible."); return; }
  const inventory = snapshot.inventario || {}, events = adminList(inventory.eventos);
  const total = key => events.reduce((sum, row) => sum + (Number(row[key]) || 0), 0);
  const verdict = !inventory.verificable ? "Consulta no disponible" : inventory.consistente === true ? "Consistente en esta consulta" : "Revisar: no se confirmó la consistencia";
  const comparison = pgModels(snapshot).flatMap(model => model.standbys.length
    ? model.standbys.map(row => ({base: model.name, primary: model.latest?.wal?.actual_lsn || PG_NOT_AVAILABLE, standby: row.nodo || "Nodo no declarado", replay: row.replay_lsn || PG_NOT_AVAILABLE, lag: pgBytes(row.lag_bytes)}))
    : [{base: model.name, primary: model.latest?.wal?.actual_lsn || PG_NOT_AVAILABLE, standby: "Sin standby observado", replay: PG_NOT_AVAILABLE, lag: PG_UNDETERMINED}]);
  const moments = adminList(snapshot.persistencia).map(row => Date.parse(row.comprobado_en)).filter(Number.isFinite);
  const spread = moments.length > 1 ? `${Math.max(...moments) - Math.min(...moments)} ms` : PG_UNDETERMINED;
  target.innerHTML = `<h4>Datos observados</h4>${adminFacts([["Capacidad total", events.length ? total("inicial") : PG_NOT_AVAILABLE], ["Disponible", events.length ? total("disponible") : PG_NOT_AVAILABLE], ["Reservado", events.length ? total("reservadas") : PG_NOT_AVAILABLE], ["Confirmado", events.length ? total("vendidas") : PG_NOT_AVAILABLE], ["Auditoría de inventario", verdict], ["Observado", adminDate(snapshot.capturado_en)]])}<h4>Primaria y standby, según la última captura</h4><div class="table-scroll">${adminTable(comparison, [["Base", "base"], ["LSN actual en primaria", "primary"], ["Standby", "standby"], ["LSN aplicado", "replay"], ["Diferencia WAL", "lag"]])}</div><p class="field-help">Mientras el LSN aplicado sea anterior al de la primaria, una lectura en ese standby puede devolver un estado más antiguo.</p>`;
  adminNode("snapshot-observed").innerHTML = `<h4>Observación real de esta captura</h4>${adminFacts([["Diagnósticos de bases leídos", moments.length], ["Separación entre la primera y la última lectura", spread], ["Captura", adminDate(snapshot.capturado_en)]])}<p class="field-help">Son lecturas independientes tomadas en momentos distintos: no forman un snapshot global. Este dato no procede del ejemplo didáctico.</p>`;
}

function selectSnapshotMode(mode) {
  const data = SNAPSHOT_MODES[mode];
  adminNode("snapshot-diagram").dataset.mode = mode;
  document.querySelectorAll("[data-snapshot-mode]").forEach(button => button.setAttribute("aria-pressed", String(button.dataset.snapshotMode === mode)));
  adminNode("snapshot-compras").textContent = data.compras;
  adminNode("snapshot-eventos").textContent = data.eventos;
  adminNode("snapshot-canal").textContent = data.canal;
  adminNode("snapshot-summary").textContent = data.resumen;
}

function renderPersistencePanels() {
  renderPgReplication(); renderPgRecovery(); renderConsistencyData();
}

function clearPersistencePanels() {
  ["pg-replication", "pg-recovery", "consistency-live", "snapshot-observed"].forEach(id => { const node = adminNode(id); if (node) node.textContent = ""; });
}

function initializePersistence() {
  document.querySelectorAll("[data-snapshot-mode]").forEach(button => button.addEventListener("click", () => selectSnapshotMode(button.dataset.snapshotMode)));
  selectSnapshotMode("independiente");
}
