"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

class Element {
  constructor(dataset = {}) {
    this.dataset = dataset;
    this.textContent = "";
    this.innerHTML = "";
    this.hidden = false;
    this.disabled = false;
    this.className = "";
    this.open = false;
    this.active = false;
    this.classList = {
      contains: (name) => name === "active" && this.active,
      toggle: (name, force) => { if (name === "active") this.active = Boolean(force); },
    };
  }
  showModal() { this.open = true; }
  close() { this.open = false; }
}

const views = ["comprar", "admin", "observabilidad"].map((name) => Object.assign(new Element(), { id: `view-${name}` }));
views[0].active = true;
const nav = ["comprar", "admin", "observabilidad"].map((name) => new Element({ view: name }));
const authTabs = ["login", "register"].map((name) => new Element({ authMode: name }));
const elements = new Map();
const document = {
  getElementById(id) {
    if (!elements.has(id)) elements.set(id, new Element());
    return elements.get(id);
  },
  querySelector(selector) {
    return selector === '[data-auth-mode="register"]' ? authTabs[1] : null;
  },
  querySelectorAll(selector) {
    if (selector === ".view") return views;
    if (selector === ".nav-link") return nav;
    if (selector === "[data-auth-mode]") return authTabs;
    return [];
  },
  addEventListener() {},
};

const context = vm.createContext({
  document,
  window: { scrollTo() {} },
  CONFIG: { USUARIOS_URL: "users", EVENTOS_URL: "events", COMPRAS_LB_URL: "purchases", COMPRAS_R1_URL: "r1", COMPRAS_R2_URL: "r2" },
  setInterval() {},
  fetchJSON: async () => ({ ok: true, status: 200, data: [] }),
  performance: { now: () => 0 },
  Headers,
  AbortController,
  URL,
  setTimeout,
  clearTimeout,
  navigator: {},
  Date,
  Promise,
  Number,
  Array,
  String,
  crypto: { randomUUID: (() => { let sequence = 0; return () => `synthetic-${++sequence}`; })() },
});
const source = fs.readFileSync("frontend/app.js", "utf8");
vm.runInContext(source, context, { filename: "app.js" });
const purchaseKeyA = context.purchaseIdempotencyKey(10, { evento_id: 3, cantidad: 2 });
const purchaseKeyB = context.purchaseIdempotencyKey(10, { evento_id: 3, cantidad: 2 });
assert.equal(purchaseKeyA.key, purchaseKeyB.key, "un reintento conserva la clave para el mismo usuario y contenido");
assert.notEqual(context.purchaseIdempotencyKey(11, { evento_id: 3, cantidad: 2 }).key, purchaseKeyA.key, "otra cuenta usa otra clave");
assert.notEqual(context.purchaseIdempotencyKey(10, { evento_id: 3, cantidad: 1 }).key, purchaseKeyA.key, "otro contenido usa otra clave");
const appFetchJSON = context.fetchJSON;
context.fetchJSON = async () => ({ ok: true, status: 200, data: [] });

context.setAuthenticated({ access_token: "synthetic", user: { id: 10, nombre: "Comprador", role: "comprador" } });
const result = document.getElementById("purchase-result");
const button = document.getElementById("purchase-submit");
result.textContent = "Compra confirmada";
result.dataset.outcome = "confirmed";
button.disabled = true;
context.setAuthenticated({ access_token: "rotated", user: { id: 10, nombre: "Comprador", role: "comprador" } });
assert.equal(result.textContent, "Compra confirmada", "la confirmación persiste durante la misma sesión");

context.setAuthenticated({ access_token: "other", user: { id: 11, nombre: "Otra cuenta", role: "comprador" } });
assert.equal(result.textContent, "", "cambiar de cuenta borra el mensaje de compra");
assert.equal(result.dataset.outcome, undefined);
result.textContent = "Compra confirmada";
result.dataset.outcome = "confirmed";
button.disabled = true;
context.clearAuthenticated();
assert.equal(result.textContent, "", "cerrar sesión borra el mensaje de compra");
assert.equal(result.dataset.outcome, undefined);
assert.equal(button.disabled, false);

context.setAuthenticated({ access_token: "buyer", user: { id: 12, nombre: "Comprador", role: "comprador" } });
context.setView("admin");
assert.equal(views[0].active, true, "un comprador permanece en el portal de compra");
assert.equal(document.getElementById("auth-dialog").open, true);
assert.equal(authTabs[1].hidden, true, "el acceso administrativo no ofrece registro público");
context.setView("observabilidad");
assert.equal(views[2].active, false, "un comprador no abre observabilidad");

context.setAuthenticated({ access_token: "admin", user: { id: 1, nombre: "Admin QA", role: "administrador" } });
assert.equal(document.getElementById("auth-status").textContent, "Admin QA · administrador");
assert.equal(authTabs[1].hidden, true, "una sesión autenticada no ofrece registro");
context.setView("admin");
assert.equal(views[1].active, true, "el administrador autenticado puede abrir administración");

(async () => {
  context.fetchJSON = appFetchJSON;
  let refreshCount = 0;
  const resourceCalls = new Map();
  context.fetch = async (url, options) => {
    if (url.endsWith("/auth/refresh")) {
      refreshCount += 1;
      await new Promise((resolve) => setTimeout(resolve, 20));
      return { ok: true, status: 200, json: async () => ({ access_token: "fresh-token", user: { id: 1, nombre: "Admin QA", role: "administrador" } }) };
    }
    const path = new URL(url).pathname;
    resourceCalls.set(path, (resourceCalls.get(path) || 0) + 1);
    if (options.headers.get("Authorization") === "Bearer expired-token") {
      return { ok: false, status: 401, json: async () => ({ detail: "Token inválido o expirado" }) };
    }
    return { ok: true, status: 200, json: async () => [] };
  };
  context.setAuthenticated({ access_token: "expired-token", user: { id: 1, nombre: "Admin QA", role: "administrador" } });
  const results = await Promise.all([
    context.fetchJSON("http://staging.test/protected-a"),
    context.fetchJSON("http://staging.test/protected-b"),
  ]);
  assert.deepEqual(results.map((item) => item.status), [200, 200], "las dos peticiones se reintentan tras renovar");
  assert.equal(refreshCount, 1, "peticiones simultáneas comparten una sola renovación");
  assert.equal(resourceCalls.get("/protected-a"), 2, "la primera petición protegida se reintenta una vez");
  assert.equal(resourceCalls.get("/protected-b"), 2, "la segunda petición protegida se reintenta una vez");

  refreshCount = 0;
  context.fetch = async (url) => {
    if (url.endsWith("/auth/refresh")) {
      refreshCount += 1;
      return { ok: false, status: 401, json: async () => ({ detail: "Sesión inválida o expirada" }) };
    }
    return { ok: false, status: 401, json: async () => ({ detail: "Token inválido o expirado" }) };
  };
  context.setAuthenticated({ access_token: "revoked-access", user: { id: 1, nombre: "Admin QA", role: "administrador" } });
  const failed = await context.fetchJSON("http://staging.test/protected-failure");
  assert.equal(failed.status, 401);
  assert.equal(refreshCount, 1, "un refresh revocado no entra en un bucle");
  assert.equal(vm.runInContext("ACCESS_TOKEN", context), null, "refresh revocado limpia el access token");
  assert.equal(document.getElementById("auth-dialog").open, true, "refresh revocado solicita autenticación");

  document.getElementById("auth-dialog").close();
  refreshCount = 0;
  context.fetch = async (url) => {
    if (url.endsWith("/auth/refresh")) {
      refreshCount += 1;
      return { ok: false, status: 503, json: async () => ({ detail: "Temporalmente no disponible" }) };
    }
    return { ok: false, status: 401, json: async () => ({ detail: "Token inválido o expirado" }) };
  };
  context.setAuthenticated({ access_token: "temporary-outage-token", user: { id: 1, nombre: "Admin QA", role: "administrador" } });
  const temporaryFailure = await context.fetchJSON("http://staging.test/protected-temporary-failure");
  assert.equal(temporaryFailure.status, 401);
  assert.equal(refreshCount, 1);
  assert.equal(vm.runInContext("ACCESS_TOKEN", context), "temporary-outage-token", "un fallo temporal no invalida la sesión local");
  assert.equal(document.getElementById("auth-dialog").open, false, "un fallo temporal no solicita un login falso");
  assert.match(document.getElementById("auth-status").textContent, /renovación pendiente/);

  let logoutRequested = false;
  context.fetch = async (url) => {
    if (url.endsWith("/auth/logout")) {
      logoutRequested = true;
      return { ok: true, status: 200, json: async () => ({ status: "sesion_cerrada" }) };
    }
    return { ok: true, status: 200, json: async () => [] };
  };
  let releaseRefresh;
  context.__refreshGate = new Promise((resolve) => { releaseRefresh = resolve; });
  vm.runInContext("REFRESH_PROMISE = globalThis.__refreshGate", context);
  const pendingLogout = context.logout();
  await Promise.resolve();
  assert.equal(logoutRequested, false, "logout espera a la renovación en curso");
  releaseRefresh({ ok: true, status: 200, data: {} });
  await pendingLogout;
  assert.equal(logoutRequested, true, "logout revoca después de que termine el refresh pendiente");
  console.log("frontend staging auth state: session, retry, single-flight, expiry, transient failure and logout checks passed");
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
