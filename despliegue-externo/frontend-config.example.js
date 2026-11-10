// PLANTILLA de configuración pública del portal. Nunca incluir contraseñas, claves ni tokens.
const CONFIG = Object.freeze({
  USUARIOS_URL: "https://usuarios.CAMBIAR_DOMINIO",
  EVENTOS_URL: "https://eventos.CAMBIAR_DOMINIO",
  COMPRAS_R1_URL: "https://compras.CAMBIAR_DOMINIO",
  COMPRAS_R2_URL: "https://compras.CAMBIAR_DOMINIO",
  COMPRAS_LB_URL: "https://compras.CAMBIAR_DOMINIO",
  PUBLIC_PORTAL_URL: "https://CAMBIAR_DOMINIO_PORTAL",
  DEPLOYMENT_PROFILE: "externo",
  DEPLOYMENT_ID: "CAMBIAR_NOMBRE_DESPLIEGUE",
  INSTALLATION_ID: null,
  RELEASE: null,
  NODE_COUNT: 1,
});
window.NEXUSTIX_CONFIG = CONFIG;
