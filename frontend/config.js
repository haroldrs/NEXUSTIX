// Endpoints usados por el navegador. Para despliegues en varios equipos,
// config.js puede definir window.NEXUSTIX_CONFIG antes de este archivo.
// Ejemplo: window.NEXUSTIX_CONFIG = { EVENTOS_URL: "https://eventos.example" };
const CONFIG = Object.freeze({
  USUARIOS_URL: "http://localhost:8001",
  EVENTOS_URL: "http://localhost:8002",
  COMPRAS_R1_URL: "http://localhost:8003",
  COMPRAS_R2_URL: "http://localhost:8004",
  COMPRAS_LB_URL: "http://localhost:8005",
  ...(window.NEXUSTIX_CONFIG || {}),
});
