// Endpoints usados por el navegador. Para despliegues en varios equipos,
// se puede definir window.NEXUSTIX_CONFIG antes de este archivo.
// Ejemplo: window.NEXUSTIX_CONFIG = { EVENTOS_URL: "https://eventos.example" };
const CONFIG = Object.freeze({
  USUARIOS_URL: "http://localhost:8001",
  EVENTOS_URL: "http://localhost:8002",
  COMPRAS_R1_URL: "http://localhost:8003",
  COMPRAS_R2_URL: "http://localhost:8004",
  COMPRAS_LB_URL: "http://localhost:8005",
  PUBLIC_PORTAL_URL: ["http:", "https:"].includes(window.location.protocol) ? window.location.origin : null,
  DEPLOYMENT_PROFILE: window.NEXUSTIX_CONFIG ? null : "local",
  DEPLOYMENT_ID: null,
  INSTALLATION_ID: null,
  RELEASE: null,
  NODE_COUNT: null,
  ...(window.NEXUSTIX_CONFIG || {}),
});
// Mismo objeto público para consumidores nuevos y el alias CONFIG existente.
window.NEXUSTIX_CONFIG = CONFIG;
