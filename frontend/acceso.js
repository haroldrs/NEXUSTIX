"use strict";

// Control de acceso. El backend es la única autoridad: aquí solo se lee el código
// y se presenta la respuesta de POST /entradas/validar.
const ACCESS_CODE_PATTERN = /^[A-Za-z0-9_-]{20,80}$/;
const ACCESS_ICONS = {allowed: "✓", denied: "✕", unknown: "?"};
let ACCESS_PORTAL_ORIGINS = new Set([location.origin]);
let ACCESS_BUSY = false;
let ACCESS_RUN = 0;
let ACCESS_STREAM = null;
let ACCESS_TIMER = null;
const accessNode = id => document.getElementById(id);

// Admite el código histórico o el enlace del QR nuevo. El enlace nunca se abre:
// solo se extrae el código, y cualquier otra forma de URL se descarta.
function accessCodeFrom(raw) {
  const text = String(raw ?? "").trim();
  if (ACCESS_CODE_PATTERN.test(text)) return text;
  let url;
  try { url = new URL(text); } catch (_) { return null; }
  if (!["http:", "https:"].includes(url.protocol) || url.username || url.password || url.search || url.pathname !== "/" || !ACCESS_PORTAL_ORIGINS.has(url.origin)) return null;
  const match = /^#entrada\/([A-Za-z0-9_-]{20,80})$/.exec(url.hash);
  return match ? match[1] : null;
}

function accessHint(text) { accessNode("access-scan-status").textContent = text; }

function accessControls(scanning) {
  accessNode("access-camera").hidden = !scanning;
  accessNode("access-start").disabled = scanning || ACCESS_BUSY;
  accessNode("access-stop").disabled = !scanning;
  accessNode("access-submit").disabled = ACCESS_BUSY;
}

function showAccessVerdict(kind, title, lines = []) {
  const box = document.createElement("div");
  box.className = "access-verdict"; box.dataset.kind = kind;
  const icon = document.createElement("span"); icon.setAttribute("aria-hidden", "true"); icon.textContent = ACCESS_ICONS[kind];
  const heading = document.createElement("strong"); heading.textContent = title;
  box.append(icon, heading);
  lines.filter(Boolean).forEach(line => { const row = document.createElement("p"); row.textContent = line; box.appendChild(row); });
  const target = accessNode("access-result");
  target.replaceChildren(box);
  target.scrollIntoView({block: "nearest"});
}

function stopAccessScanner() {
  ACCESS_RUN++;
  clearTimeout(ACCESS_TIMER);
  ACCESS_STREAM?.getTracks().forEach(track => track.stop());
  ACCESS_STREAM = null;
  const video = accessNode("access-video");
  video.pause(); video.srcObject = null;
  accessControls(false);
}

function loadLocalQrDecoder() {
  if (window.jsQR) return Promise.resolve();
  return new Promise((resolve, reject) => {
    const script = document.createElement("script");
    script.src = "vendor/jsQR.js";
    script.onload = resolve;
    script.onerror = () => { script.remove(); reject(new Error("decoder")); };
    document.head.appendChild(script);
  });
}

// Toda la decodificación ocurre en este navegador; ningún fotograma sale de él.
async function createQrReader() {
  if ("BarcodeDetector" in window) {
    try {
      if ((await window.BarcodeDetector.getSupportedFormats()).includes("qr_code")) {
        const detector = new window.BarcodeDetector({formats: ["qr_code"]});
        return async video => (await detector.detect(video))[0]?.rawValue || null;
      }
    } catch (_) { /* Se utiliza el decodificador local. */ }
  }
  await loadLocalQrDecoder();
  const canvas = document.createElement("canvas");
  const context = canvas.getContext("2d", {willReadFrequently: true});
  return async video => {
    if (!video.videoWidth) return null;
    const scale = Math.min(1, 640 / video.videoWidth);
    canvas.width = Math.round(video.videoWidth * scale);
    canvas.height = Math.round(video.videoHeight * scale);
    context.drawImage(video, 0, 0, canvas.width, canvas.height);
    const image = context.getImageData(0, 0, canvas.width, canvas.height);
    return window.jsQR(image.data, image.width, image.height, {inversionAttempts: "dontInvert"})?.data || null;
  };
}

function scanFrames(run, read) {
  const video = accessNode("access-video");
  const tick = async () => {
    if (run !== ACCESS_RUN) return;
    let raw = null;
    try { if (video.readyState >= 2) raw = await read(video); } catch (_) { /* Se reintenta con el siguiente fotograma. */ }
    if (run !== ACCESS_RUN) return;
    if (raw) {
      const code = accessCodeFrom(raw);
      if (code) { stopAccessScanner(); validateAccess(code); return; }
      accessHint("Se leyó un QR que no es una entrada NEXUSTIX. No se envió al servidor.");
    }
    ACCESS_TIMER = setTimeout(tick, 180);
  };
  tick();
}

async function startAccessScanner() {
  if (ACCESS_STREAM || ACCESS_BUSY || !accessNode("access-stop").disabled) return;
  if (!accessNode("access-event").value) { accessHint("Selecciona primero el evento."); return; }
  if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
    accessHint("La cámara del navegador requiere HTTPS o localhost. Esta dirección no lo permite: introduce el código manualmente.");
    return;
  }
  const run = ++ACCESS_RUN;
  accessControls(true);
  accessNode("access-result").replaceChildren();
  accessHint("Solicitando acceso a la cámara…");
  try {
    const read = await createQrReader();
    if (run !== ACCESS_RUN) return;
    const stream = await navigator.mediaDevices.getUserMedia({audio: false, video: {facingMode: {ideal: "environment"}}});
    if (run !== ACCESS_RUN) { stream.getTracks().forEach(track => track.stop()); return; }
    ACCESS_STREAM = stream;
    const video = accessNode("access-video");
    video.srcObject = stream;
    await video.play();
    if (run !== ACCESS_RUN) return;
    accessHint("Apunta la cámara al código QR de la entrada.");
    scanFrames(run, read);
  } catch (error) {
    if (run !== ACCESS_RUN) return;
    stopAccessScanner();
    accessHint(error.name === "NotAllowedError" ? "No se concedió permiso para usar la cámara. Introduce el código manualmente."
      : error.name === "NotFoundError" ? "No se encontró una cámara en este dispositivo. Introduce el código manualmente."
      : "No se pudo iniciar el escáner. Introduce el código manualmente.");
  }
}

function presentAccessResult(result, eventLabel) {
  const detail = typeof result.data?.detail === "string" ? result.data.detail : null;
  if (result.ok && result.data?.acceso === "permitido") {
    const place = [result.data.zona && `Zona: ${result.data.zona}`, result.data.asiento && `Asiento: ${result.data.asiento}`].filter(Boolean).join(" · ");
    showAccessVerdict("allowed", "ACCESO PERMITIDO", [result.data.evento_nombre || eventLabel, place, "Ingreso autorizado. La entrada quedó registrada como utilizada."]);
  } else if (detail && [400, 401, 403, 404, 409, 422].includes(result.status)) {
    showAccessVerdict("denied", "ACCESO RECHAZADO", [detail]);
  } else {
    // Puede que el servidor haya procesado la solicitud aunque la respuesta se perdiera.
    showAccessVerdict("unknown", "ESTADO DESCONOCIDO", ["El servidor no confirmó el resultado. No autorices el ingreso todavía.", "Vuelve a validar cuando el servicio responda. Si indica que ya fue utilizada, revisa el registro de Ventas y entradas."]);
  }
}

async function validateAccess(raw) {
  if (ACCESS_BUSY) return;
  const select = accessNode("access-event");
  const eventId = Number(select.value);
  if (!eventId) { accessHint("Selecciona primero el evento."); return; }
  const code = accessCodeFrom(raw);
  if (!code) { showAccessVerdict("denied", "CÓDIGO NO VÁLIDO", ["El texto no corresponde a una entrada NEXUSTIX y no se envió al servidor."]); return; }
  stopAccessScanner();
  const revision = SESSION_REVISION;
  const eventLabel = select.selectedOptions[0]?.textContent || "";
  ACCESS_BUSY = true;
  accessControls(false);
  accessNode("access-result").replaceChildren();
  accessHint("Validando con el servidor…");
  try {
    const result = await fetchJSON(`${CONFIG.COMPRAS_LB_URL}/entradas/validar`, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({qr: code, evento_id: eventId})}, 20000);
    if (revision !== SESSION_REVISION) return;
    accessHint("");
    presentAccessResult(result, eventLabel);
    if (result.ok) accessNode("access-code").value = "";
  } finally {
    ACCESS_BUSY = false;
    accessControls(false);
  }
}

async function loadAccessOrigins() {
  const origins = new Set([location.origin]);
  try { if (CONFIG.PUBLIC_PORTAL_URL) origins.add(new URL(CONFIG.PUBLIC_PORTAL_URL).origin); } catch (_) { /* Se ignora una URL pública inválida. */ }
  const result = await fetchJSON(`${CONFIG.COMPRAS_LB_URL}/checkout/capacidades`);
  try { if (result.ok && result.data?.portal_publico) origins.add(new URL(result.data.portal_publico).origin); } catch (_) { /* Sin dato verificable no se amplía la lista. */ }
  ACCESS_PORTAL_ORIGINS = origins;
}

async function prepareAccessView() {
  loadAccessOrigins();
  if (!EVENTS.length) await loadEvents();
  const select = accessNode("access-event");
  const previous = select.value;
  select.replaceChildren(new Option("Selecciona un evento", ""), ...EVENTS.map(event => new Option(`${event.nombre} · ${eventDate(event.fecha)}`, String(event.id))));
  select.value = EVENTS.some(event => String(event.id) === previous) ? previous : "";
  accessHint(!EVENTS.length ? "No se pudieron cargar los eventos. Actualiza la página para reintentar."
    : !window.isSecureContext ? "La cámara requiere HTTPS o localhost. En esta dirección usa el ingreso manual del código." : "");
}

function initializeAccess() {
  accessNode("access-start").addEventListener("click", startAccessScanner);
  accessNode("access-stop").addEventListener("click", () => { stopAccessScanner(); accessHint("Escáner detenido."); });
  accessNode("access-form").addEventListener("submit", event => { event.preventDefault(); validateAccess(accessNode("access-code").value); });
  window.addEventListener("pagehide", stopAccessScanner);
  document.addEventListener("visibilitychange", () => { if (document.hidden) stopAccessScanner(); });
}
