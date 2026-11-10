"use strict";

// Vista de solo lectura. El servidor nunca entrega valores secretos a este panel.
const SECURITY_SOURCES = {archivo: "Archivo de secreto", entorno: "Variable de entorno", no_configurado: "No configurado"};

function renderSecurityCommunications() {
  const https = location.protocol === "https:";
  const portal = CONFIG.PUBLIC_PORTAL_URL;
  const scheme = /^https:/i.test(String(portal || "")) ? "HTTPS configurado" : portal ? "HTTP" : "No configurado";
  adminNode("security-communications").innerHTML = `${adminFacts([["Este navegador", https ? "Conexión HTTPS" : "Conexión HTTP"], ["Portal configurado", scheme], ["Contexto seguro (cámara)", window.isSecureContext ? "Sí" : "No: usa el ingreso manual del código"]])}<div class="table-scroll">${adminTable([
    {canal: "Navegador – gateway", estado: https ? "HTTPS en esta conexión" : "Sin HTTPS en esta conexión"},
    {canal: "Entre microservicios", estado: "No verificable desde el panel; sin TLS en las plantillas actuales"},
    {canal: "Aplicaciones – PostgreSQL", estado: "No verificable desde el panel"},
    {canal: "Replicación WAL", estado: "No verificable desde el panel"},
    {canal: "Coordinación", estado: "No instalada"},
  ], [["Canal", "canal"], ["Estado", "estado"]])}</div><p class="field-help">HTTPS en el acceso público no significa cifrado de extremo a extremo. PostgreSQL y la coordinación no deben exponerse a Internet.</p>`;
}

async function loadSecurityCredentials() {
  const target = adminNode("security-credentials");
  const result = await adminRequest(`${ADMIN_API()}/seguridad`);
  if (result.stale) return;
  if (!result.ok || !result.data) { target.innerHTML = adminEmpty(adminError(result, "Estado de credenciales no disponible.")); return; }
  const data = result.data;
  const warnings = [...adminList(data.secretos).filter(row => !row.presente).map(row => `${row.nombre}: no disponible en esta instancia.`), ...adminList(data.secretos).filter(row => row.fuente === "entorno").map(row => `${row.nombre}: se recomienda un archivo de secreto.`), ...(data.origen_comodin ? ["CORS admite cualquier origen."] : [])];
  target.innerHTML = `<div class="table-scroll">${adminTable(adminList(data.secretos), [["Credencial", "nombre"], ["Origen", row => SECURITY_SOURCES[row.fuente] || row.fuente], ["Disponible", row => row.presente ? "Sí" : "No"]])}</div>${adminFacts([["Orígenes permitidos", data.origenes_permitidos], ["Esquema del portal en el servidor", data.esquema_portal || "No configurado"]])}${warnings.length ? `<ul class="pg-steps">${warnings.map(text => `<li>${adminText(text)}</li>`).join("")}</ul>` : ""}<p class="field-help">${adminText(data.alcance)} Los valores nunca se muestran.</p>`;
}

async function loadSecurityAudit() {
  const sources = [["Compras", `${ADMIN_API()}/auditoria`], ["Eventos", `${CONFIG.EVENTOS_URL}/auditoria/catalogo`], ["Usuarios (memoria)", `${CONFIG.USUARIOS_URL}/auditoria/roles`]];
  const results = await Promise.all(sources.map(([, url]) => adminRequest(url)));
  if (results.some(result => result.stale)) return;
  const rows = results.flatMap((result, index) => adminList(result.data?.registros).map(row => ({...row, servicio: sources[index][0]})));
  rows.sort((a, b) => (Date.parse(b.momento) || 0) - (Date.parse(a.momento) || 0));
  adminNode("security-audit").innerHTML = adminTable(rows.slice(0, 150), [["Fecha", row => adminDate(row.momento)], ["Acción", "accion"], ["Resultado", row => row.resultado || "Registrado"], ["Recurso", row => row.recurso || "No informado"], ["Responsable", row => row.actor || "No informado"], ["Rol", row => row.rol || "No informado"], ["Operación", row => row.operacion_id || "—"], ["Motivo", row => row.motivo || "—"], ["Servicio", "servicio"]], "No hay acciones registradas.");
  const unavailable = sources.filter((_, index) => !results[index].ok).map(([name]) => name);
  adminNode("security-audit-note").textContent = `${unavailable.length ? `Sin respuesta de: ${unavailable.join(", ")}. ` : ""}Los cambios de rol se guardan en la memoria y los logs de cada instancia de Usuarios; no sobreviven en este historial a un reinicio.`;
}

async function loadSecurityPanel() {
  adminNode("security-role").textContent = `Rol de esta sesión: ${CURRENT_USER?.role || "sin sesión"}.`;
  renderSecurityCommunications();
  await Promise.all([loadSecurityCredentials(), loadSecurityAudit()]);
}

function clearSecurityPanel() {
  ["security-role", "security-communications", "security-credentials", "security-audit", "security-audit-note"].forEach(id => { const node = adminNode(id); if (node) node.textContent = ""; });
}

document.addEventListener("DOMContentLoaded", () => {
  const button = adminNode("security-refresh");
  button.addEventListener("click", () => adminBusy(button, loadSecurityPanel));
});
