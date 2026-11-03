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
    this.title = "";
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

let cookie = "";
let refreshCalls = 0;
const paths = new Map();
async function browserFetch(url, options = {}) {
  const parsed = new URL(url);
  const headers = new Headers(options.headers || {});
  if (parsed.pathname === "/auth/refresh") {
    refreshCalls += 1;
    if (cookie) headers.set("Cookie", `nexustix_refresh=${cookie}`);
    headers.set("Origin", process.env.NEXUSTIX_STAGING_FRONTEND_ORIGIN || "http://localhost:18080");
  }
  if (parsed.pathname === "/auth/logout") {
    if (cookie) headers.set("Cookie", `nexustix_refresh=${cookie}`);
    headers.set("Origin", process.env.NEXUSTIX_STAGING_FRONTEND_ORIGIN || "http://localhost:18080");
  }
  if (parsed.pathname.endsWith("/compras/mias")) paths.set(parsed.pathname + parsed.search, (paths.get(parsed.pathname + parsed.search) || 0) + 1);
  const response = await fetch(url, { ...options, headers });
  if (parsed.pathname === "/auth/refresh") {
    const setCookies = response.headers.getSetCookie?.() || [];
    const refreshCookie = setCookies.find((value) => value.startsWith("nexustix_refresh="));
    if (refreshCookie) {
      const value = refreshCookie.split(";", 1)[0].slice("nexustix_refresh=".length);
      cookie = /max-age=0/i.test(refreshCookie) ? "" : value;
    }
  }
  return response;
}

async function main() {
  let input = "";
  for await (const chunk of process.stdin) input += chunk;
  const spec = JSON.parse(input);
  cookie = spec.refresh_cookie || "";
  const elements = new Map();
  const document = {
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, new Element());
      return elements.get(id);
    },
    querySelector(selector) { return selector === '[data-auth-mode="register"]' ? new Element({ authMode: "register" }) : null; },
    querySelectorAll(selector) {
      if (selector === "[data-auth-mode]") return [new Element({ authMode: "login" }), new Element({ authMode: "register" })];
      if (selector === ".view") return ["comprar", "admin", "observabilidad"].map((name) => Object.assign(new Element(), { id: `view-${name}` }));
      if (selector === ".nav-link") return ["comprar", "admin", "observabilidad"].map((name) => new Element({ view: name }));
      return [];
    },
    addEventListener() {},
  };
  const context = vm.createContext({
    CONFIG: spec.config,
    document,
    window: { scrollTo() {} },
    fetch: browserFetch,
    Headers,
    AbortController,
    URL,
    setTimeout,
    clearTimeout,
    Promise,
    Number,
    Array,
    String,
    Date,
    performance: { now: () => 0 },
    navigator: typeof navigator === "undefined" ? {} : navigator,
  });
  vm.runInContext(fs.readFileSync("frontend/app.js", "utf8"), context, { filename: "app.js" });
  vm.runInContext(`ACCESS_TOKEN = ${JSON.stringify(spec.access_token)}; CURRENT_USER = ${JSON.stringify(spec.user)};`, context);

  if (spec.mode === "restore") {
    await context.restoreSession();
    assert.ok(vm.runInContext("ACCESS_TOKEN", context), "restaurar sesión debe crear un access token en memoria");
    assert.equal(vm.runInContext("CURRENT_USER.role", context), spec.user.role);
    assert.equal(refreshCalls, 1);
    assert.ok(cookie && cookie !== spec.refresh_cookie, "restaurar sesión rota la cookie refresh");
    process.stdout.write("real staging frontend restore: HttpOnly refresh restored identity and rotated the cookie\n");
    return;
  }

  if (spec.mode === "renew") {
    const url = `${spec.config.COMPRAS_LB_URL}/compras/mias?renewal_probe=1`;
    const results = await Promise.all([context.fetchJSON(url), context.fetchJSON(url)]);
    assert.deepEqual(results.map((result) => result.status), [200, 200]);
    assert.equal(refreshCalls, 1, "las peticiones concurrentes deben compartir refresh");
    assert.equal(paths.get("/compras/mias?renewal_probe=1"), 4, "cada solicitud protegida se envía inicialmente y se reintenta una sola vez");
    assert.ok(cookie, "el servidor debe rotar y devolver una cookie refresh");
    process.stdout.write("real staging frontend refresh: concurrent requests retried once with a single refresh\n");
    return;
  }

  const result = await context.fetchJSON(`${spec.config.COMPRAS_LB_URL}/compras/mias?renewal_probe=1`);
  assert.equal(result.status, 401);
  assert.equal(refreshCalls, 1, "refresh revocado no debe crear un bucle");
  assert.equal(vm.runInContext("ACCESS_TOKEN", context), null);
  assert.equal(document.getElementById("auth-dialog").open, true, "la interfaz solicita autenticación nuevamente");
  process.stdout.write("real staging frontend refresh: revoked session prompted login without a retry loop\n");
}

main().catch((error) => {
  console.error(error.message);
  process.exitCode = 1;
});
