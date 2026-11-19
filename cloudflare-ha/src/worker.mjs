import { DurableObject } from "cloudflare:workers";
import { initialState, heartbeat, recordProbe, admin, route, status, receiveResult } from "./controller.mjs";
import { authenticateAdmin, requireSameOrigin } from "./admin-auth.mjs";
import { ADMIN_HTML, ADMIN_CSS, ADMIN_JS, ADMIN_STATUS_JS } from "./admin-ui.mjs";
import { originAccessHeaders } from "./origin-access.mjs";
import { websocketResponse } from "./websocket-proxy.mjs";
import { initialNotifications, publicNotifications, configureNotifications, queueTest, observeNotifications, slackPayload, postSlack, completeNotification } from "./notifications.mjs";

const json = (value, code = 200) => Response.json(value, {
  status: code, headers: { "Cache-Control": "no-store" },
});

function authorized(request, secret) {
  if (typeof secret !== "string" || secret.length < 32) return false;
  const value = request.headers.get("Authorization") || "";
  const expected = `Bearer ${secret}`;
  if (value.length !== expected.length) return false;
  let difference = 0;
  for (let i = 0; i < value.length; i++) difference |= value.charCodeAt(i) ^ expected.charCodeAt(i);
  return difference === 0;
}

function originFor(env, node) {
  const origin = new URL(node === "primary" ? env.PRIMARY_ORIGIN : env.BACKUP_ORIGIN);
  if (origin.protocol !== "https:" || origin.hostname === env.PUBLIC_HOST ||
      origin.username || origin.password || origin.pathname !== "/" || origin.search) {
    throw new Error("Invalid origin configuration");
  }
  return origin;
}

const originToken = (env, node) => node === "primary" ? env.PRIMARY_ORIGIN_TOKEN : env.BACKUP_ORIGIN_TOKEN;
// Permanent storage identifier from the first deployment. Keep it when changing
// hostnames: renaming this identifier would open a different coordinator state.
const CONTROLLER_STORAGE_ID = "immich-cybersma";
const controller = (env) => env.CONTROLLER.get(env.CONTROLLER.idFromName(CONTROLLER_STORAGE_ID));
const call = (env, path, body) => controller(env).fetch(`https://controller${path}`, {
  method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
});

function nodeIdentity(request, env) {
  return authorized(request, env.PRIMARY_AGENT_TOKEN) ? "primary" :
    authorized(request, env.BACKUP_AGENT_TOKEN) ? "backup" : null;
}

function uiAsset(body, type) {
  return new Response(body, { headers: {
    "Content-Type": type, "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
    "Referrer-Policy": "same-origin", "X-Frame-Options": "DENY",
  } });
}

async function uiStatus(env, actor) {
  const response = await call(env, "/status", {});
  if (!response.ok) return response;
  const value = await response.json();
  let nodes;
  try { nodes = JSON.parse(env.NODE_METADATA || "{}"); } catch { nodes = {}; }
  return json({ ...value, nodes, publicUrl: `https://${env.PUBLIC_HOST}`, actorEmail: actor.email });
}

async function adminUI(request, env) {
  const url = new URL(request.url);
  if (request.method === "GET" && url.pathname === "/") return uiAsset(ADMIN_HTML, "text/html; charset=utf-8");
  if (request.method === "GET" && url.pathname === "/assets/admin.css") return uiAsset(ADMIN_CSS, "text/css; charset=utf-8");
  if (request.method === "GET" && url.pathname === "/assets/admin.js") return uiAsset(ADMIN_JS, "text/javascript; charset=utf-8");
  if (request.method === "GET" && url.pathname === "/assets/status-model.mjs") return uiAsset(ADMIN_STATUS_JS, "text/javascript; charset=utf-8");
  let actor;
  try { actor = await authenticateAdmin(request, env); }
  catch (error) { return json({ error: error.message }, 401); }
  if (request.method === "GET" && url.pathname === "/api/status") return uiStatus(env, actor);
  if (request.method === "POST" && url.pathname === "/api/action") {
    try {
      requireSameOrigin(request, env);
      const text = await request.text();
      if (text.length > 4096) return json({ error: "Payload too large" }, 413);
      const input = JSON.parse(text);
      if (!["set-auto", "set-recovery", "set-preference", "set-maintenance", "set-alerts", "test-alerts", "prepare", "switch", "resume"].includes(input.action)) throw new Error("Thao tác không hợp lệ");
      const response = await call(env, "/admin", {
        action: input.action, target: input.target, enabled: input.enabled,
        webhook: input.webhook, remove: input.remove === true,
        requestId: typeof input.requestId === "string" ? input.requestId.slice(0, 80) : null,
        id: crypto.randomUUID(), actor: actor.email,
      });
      if (!response.ok) return response;
      return uiStatus(env, actor);
    } catch (error) { return json({ error: error.message }, 400); }
  }
  return json({ error: "Not found" }, 404);
}

async function probeOrigins(env) {
  const readings = {};
  await Promise.all(["primary", "backup"].map(async (node) => {
    try {
      const url = new URL("/__ha/health", originFor(env, node));
      const response = await fetch(url, {
        headers: originAccessHeaders(env, { "X-Immich-HA-Token": originToken(env, node), "User-Agent": "ImmichHA-Health/1.0" }),
        signal: AbortSignal.timeout(5000), redirect: "manual",
        cf: { cacheTtl: 0, cacheEverything: false },
      });
      readings[node] = { ok: response.ok, status: response.status };
      if (response.status >= 300 && response.status < 400) {
        const location = response.headers.get("Location");
        if (location) {
          const target = new URL(location, url);
          // Preserve only the destination host/path, never login tokens or query strings.
          readings[node].error = `redirect to ${target.hostname}${target.pathname}`.slice(0, 240);
        } else {
          readings[node].error = "redirect without Location";
        }
      }
      await response.body?.cancel();
    } catch (error) {
      readings[node] = { ok: false, error: error.name === "TimeoutError" ? "timeout" : "origin fetch failed" };
    }
  }));
  const result = await call(env, "/probe", readings);
  if (!result.ok) throw new Error("Could not record origin probes");
  return result;
}

export class HAController extends DurableObject {
  constructor(ctx, env) {
    super(ctx, env);
    this.ctx = ctx;
    this.env = env;
    this.state = null;
    this.notifications = initialNotifications();
    this.delivering = false;
    ctx.blockConcurrencyWhile(async () => {
      this.state = (await ctx.storage.get("state")) || initialState();
      this.notifications = (await ctx.storage.get("notifications")) || initialNotifications();
    });
  }

  async fetch(request) {
    return this.ctx.blockConcurrencyWhile(async () => {
      const input = await request.json();
      const path = new URL(request.url).pathname;
      const now = Date.now();
      // Work on a copy: rejected operations must not partially change memory.
      const next = structuredClone(this.state);
      const notifications = structuredClone(this.notifications);
      let output;
      try {
        if (path === "/heartbeat") output = heartbeat(next, input.node, input, now);
        else if (path === "/probe") {
          for (const node of ["primary", "backup"]) recordProbe(next, node, input[node], now);
          output = status(next, now);
        } else if (path === "/admin") {
          if (input.action === "set-alerts") {
            configureNotifications(notifications, input);
          } else if (input.action === "test-alerts") {
            queueTest(notifications, status(next, now), now);
          } else admin(next, input, now);
          output = status(next, now);
        } else if (path === "/job-result") {
          receiveResult(next, input.node, input, now);
          output = { accepted: true };
        } else if (path === "/node-info") {
          output = { apiVersion: 2, owner: next.owner, epoch: next.epoch, auto: next.auto,
            ready: next.ready, pending: next.pending,
            agent: next.heartbeats[input.node] || null,
            nodeMaintenance: next.nodeMaintenance || {},
            operation: next.operation ? { id: next.operation.id, status: next.operation.status, maintenance: next.operation.maintenance } : null };
        } else if (path === "/status") output = status(next, now);
        else if (path === "/route") output = route(next, now);
        else return json({ error: "Not found" }, 404);
      } catch (error) {
        return json({ error: error.message }, 409);
      }
      // Slack delivery is never awaited by routing, heartbeat or role changes.
      try { observeNotifications(notifications, status(next, now), now); }
      catch { notifications.lastError = "Không tạo được alert; điều phối vẫn tiếp tục"; }
      const writes = {};
      if (JSON.stringify(next) !== JSON.stringify(this.state)) writes.state = next;
      if (JSON.stringify(notifications) !== JSON.stringify(this.notifications)) writes.notifications = notifications;
      if (Object.keys(writes).length) await this.ctx.storage.put(writes);
      this.notifications = notifications;
      if (["/status", "/probe", "/admin"].includes(path)) {
        output.alerts = publicNotifications(notifications);
      }
      this.state = next;
      try { await this.scheduleNotifications(); } catch { /* Retry scheduling on the next heartbeat. */ }
      return json(output);
    });
  }

  async scheduleNotifications() {
    const n = this.notifications;
    if (!n.enabled || !n.webhook || !n.queue.length) return;
    const due = Math.max(Date.now() + 1000, Math.min(...n.queue.map((e) => e.nextAttemptAt)));
    const current = await this.ctx.storage.getAlarm();
    if (current === null || current > due) await this.ctx.storage.setAlarm(due);
  }

  async alarm() {
    const delivery = await this.ctx.blockConcurrencyWhile(async () => {
      const n = this.notifications, now = Date.now();
      if (this.delivering || !n.enabled || !n.webhook) return null;
      const event = n.queue.find((e) => e.nextAttemptAt <= now);
      if (!event) { await this.scheduleNotifications(); return null; }
      this.delivering = true;
      event.attempts++; event.nextAttemptAt = now + 15_000;
      await this.ctx.storage.put("notifications", n);
      await this.scheduleNotifications(); // Recover the claimed item after an isolate restart.
      let metadata; try { metadata = JSON.parse(this.env.NODE_METADATA || "{}"); } catch { metadata = {}; }
      return { id: event.id, revision: n.revision, webhook: n.webhook, payload: slackPayload(event, metadata) };
    });
    if (!delivery) return;
    const result = await postSlack(delivery.webhook, delivery.payload, fetch);
    await this.ctx.blockConcurrencyWhile(async () => {
      this.delivering = false;
      if (this.notifications.revision === delivery.revision) {
        completeNotification(this.notifications, delivery.id, result, Date.now());
        await this.ctx.storage.put("notifications", this.notifications);
      }
      await this.scheduleNotifications();
    });
  }
}

export default {
  async fetch(request, env) {
    try {
      const url = new URL(request.url);
      if (env.ADMIN_HOST && url.hostname === env.ADMIN_HOST) return adminUI(request, env);
      if (url.pathname === "/__ha/heartbeat" && request.method === "POST") {
        const node = nodeIdentity(request, env);
        if (!node) return json({ error: "Unauthorized" }, 401);
        const text = await request.text();
        if (text.length > 18000) return json({ error: "Payload too large" }, 413);
        return call(env, "/heartbeat", { ...JSON.parse(text), node });
      }
      if (url.pathname === "/__ha/node-info" && request.method === "GET") {
        const node = nodeIdentity(request, env);
        if (!node) return json({ error: "Unauthorized" }, 401);
        return call(env, "/node-info", { node });
      }
      if (url.pathname === "/__ha/job-result" && request.method === "POST") {
        const node = nodeIdentity(request, env);
        if (!node) return json({ error: "Unauthorized" }, 401);
        const text = await request.text();
        if (text.length > 18000) return json({ error: "Payload too large" }, 413);
        return call(env, "/job-result", { ...JSON.parse(text), node });
      }
      if (url.pathname === "/__ha/status" && request.method === "GET") {
        if (!authorized(request, env.ADMIN_TOKEN)) return json({ error: "Unauthorized" }, 401);
        return call(env, "/status", {});
      }
      if (url.pathname === "/__ha/probe" && request.method === "POST") {
        if (!authorized(request, env.ADMIN_TOKEN)) return json({ error: "Unauthorized" }, 401);
        return probeOrigins(env);
      }
      if (url.pathname === "/__ha/admin" && request.method === "POST") {
        if (!authorized(request, env.ADMIN_TOKEN)) return json({ error: "Unauthorized" }, 401);
        const text = await request.text();
        if (text.length > 8192) return json({ error: "Payload too large" }, 413);
        return call(env, "/admin", JSON.parse(text));
      }
      if (url.pathname.startsWith("/__ha/")) return json({ error: "Not found" }, 404);
      // workers.dev is reserved for the coordinator until production cutover.
      if (url.hostname !== env.PUBLIC_HOST) return json({ service: "immich-ha-control" });

      const selection = await call(env, "/route", {});
      if (!selection.ok) return json({ error: "Coordinator unavailable" }, 503);
      const selected = await selection.json();
      if (!selected) return new Response("Immich is preparing the serving node. Please retry shortly.", {
        status: 503, headers: { "Retry-After": "15", "Cache-Control": "no-store" },
      });
      const origin = originFor(env, selected.node);
      const token = originToken(env, selected.node);
      if (!token || token.length < 32) return json({ error: "Origin not configured" }, 503);
      const upstream = new URL(origin);
      upstream.pathname = url.pathname;
      upstream.search = url.search;
      const forwarded = new Request(upstream, request);
      const accessHeaders = originAccessHeaders(env, forwarded.headers);
      forwarded.headers.delete("CF-Access-Client-Id");
      forwarded.headers.delete("CF-Access-Client-Secret");
      forwarded.headers.delete("Cf-Access-Jwt-Assertion");
      for (const name of ["CF-Access-Client-Id", "CF-Access-Client-Secret"]) {
        if (accessHeaders.has(name)) forwarded.headers.set(name, accessHeaders.get(name));
      }
      forwarded.headers.set("Host", origin.host);
      forwarded.headers.set("X-Immich-HA-Token", token);
      forwarded.headers.set("X-Immich-HA-Epoch", String(selected.epoch));
      forwarded.headers.set("X-Forwarded-Host", env.PUBLIC_HOST);
      forwarded.headers.set("X-Forwarded-Proto", "https");
      // Stream directly; never buffer uploads/videos or retry a write on another node.
      const websocket = request.headers.get("Upgrade")?.toLowerCase() === "websocket";
      const response = await fetch(forwarded, websocket ? { redirect: "manual" } :
        { redirect: "manual", cf: { cacheTtl: 0, cacheEverything: false } });
      if (response.status === 101) return websocketResponse(response, selected.node, selected.epoch);
      const result = new Response(response.body, response);
      result.headers.set("Cache-Control", "no-store");
      result.headers.set("X-Immich-HA-Node", selected.node);
      result.headers.set("X-Immich-HA-Epoch", String(selected.epoch));
      const location = result.headers.get("Location");
      if (location) {
        const target = new URL(location, upstream);
        if (target.origin === origin.origin) {
          target.protocol = "https:";
          target.host = env.PUBLIC_HOST;
          result.headers.set("Location", target.toString());
        }
      }
      return result;
    } catch {
      return json({ error: "Immich gateway unavailable" }, 503);
    }
  },

  async scheduled(_event, env, ctx) {
    ctx.waitUntil(probeOrigins(env));
  },
};
