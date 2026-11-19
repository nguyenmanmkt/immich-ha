import test from "node:test";
import assert from "node:assert/strict";
import { initialState, heartbeat, recordProbe, admin, route, advance, LIMITS } from "../src/controller.mjs";

const sessions = { primary: "primary_session_123456", backup: "backup_session_123456" };
const payload = (node, extra = {}) => ({ session: sessions[node], localHealthy: true,
  enforcing: true, canPromote: node === "backup", ready: true, epoch: 1, ...extra });
function setup(now = 100_000) {
  const s = initialState();
  for (const node of ["primary", "backup"]) {
    heartbeat(s, node, payload(node), now);
    recordProbe(s, node, true, now);
  }
  heartbeat(s, "primary", payload("primary"), now + 1);
  return s;
}

test("defaults to manual and no public route before readiness", () => {
  const s = initialState();
  assert.equal(s.auto, false);
  assert.equal(route(s, 1), null);
});
test("diagnostic probe objects preserve failures rather than treating objects as true", () => {
  const s = setup();
  recordProbe(s, "primary", { ok: false, status: 403 }, 100_002);
  assert.equal(s.probes.primary.ok, false);
  assert.equal(s.probes.primary.status, 403);
  assert.equal(route(s, 100_002), null);
});
test("routes primary only after matching session acknowledges its grant", () => {
  const s = setup();
  assert.deepEqual(route(s, 100_002), { node: "primary", epoch: 1 });
});
test("first heartbeat does not count as activation acknowledgement", () => {
  const s = initialState();
  heartbeat(s, "primary", payload("primary"), 100);
  recordProbe(s, "primary", true, 100);
  assert.equal(route(s, 101), null);
});
test("fresh replacement process cannot overlap an existing lease", () => {
  const s = setup();
  const answer = heartbeat(s, "primary", payload("primary", { session: "replacement_session_1234" }), 100_010);
  assert.equal(answer.role, "fenced");
  assert.equal(s.session, sessions.primary);
});
test("expired lease closes public routing", () => {
  const s = setup();
  assert.equal(route(s, s.leaseUntil), null);
});
test("automatic enable requires both enforced nodes and valid probes", () => {
  const s = setup();
  s.heartbeats.backup.enforcing = false;
  assert.throws(() => admin(s, { action: "set-auto", enabled: true }, 100_003));
});
test("manual takeover waits until the old grant plus margin expires", () => {
  const s = setup();
  const deadline = s.leaseUntil + LIMITS.fenceMs;
  admin(s, { action: "promote-backup" }, 100_003);
  assert.equal(s.owner, "primary");
  assert.equal(route(s, 100_004), null);
  assert.equal(heartbeat(s, "primary", payload("primary"), 100_005).role, "fenced");
  recordProbe(s, "backup", true, deadline - 1);
  heartbeat(s, "backup", payload("backup"), deadline - 1);
  assert.equal(s.owner, "primary");
  const granted = heartbeat(s, "backup", payload("backup"), deadline);
  assert.equal(granted.role, "active");
  assert.equal(s.owner, "backup");
  assert.equal(s.epoch, 2);
  assert.equal(route(s, deadline), null);
  heartbeat(s, "backup", payload("backup", { epoch: 2 }), deadline + 1);
  assert.deepEqual(route(s, deadline + 1), { node: "backup", epoch: 2 });
});
test("primary return never causes automatic failback", () => {
  const s = setup();
  s.owner = "backup"; s.epoch = 2; s.session = sessions.backup;
  assert.equal(heartbeat(s, "primary", payload("primary"), 100_010).role, "standby");
  assert.equal(s.owner, "backup");
  assert.throws(() => admin(s, { action: "promote-primary" }, 100_010));
});
test("manual mode never promotes merely because primary disappears", () => {
  const s = setup();
  const now = 100_001 + LIMITS.failureMs * 3;
  recordProbe(s, "backup", true, now);
  heartbeat(s, "backup", payload("backup"), now);
  advance(s, now + LIMITS.failureMs);
  assert.equal(s.owner, "primary");
});
test("auto promotion requires sustained failure, not one failed probe", () => {
  const s = setup();
  admin(s, { action: "set-auto", enabled: true }, 100_002);
  recordProbe(s, "primary", false, 100_003);
  assert.equal(s.pending, null);
  recordProbe(s, "primary", true, 100_004);
  assert.equal(s.failureSince, null);
});
test("no automatic promotion to a stale or unprepared backup", () => {
  const s = setup();
  s.auto = true;
  s.failureSince = 1;
  advance(s, 1_000_000);
  assert.equal(s.owner, "primary");
  assert.equal(s.pending, null);
});
test("healthy backup eventually takes over after sustained primary failure", () => {
  const s = setup();
  admin(s, { action: "set-auto", enabled: true }, 100_002);
  recordProbe(s, "primary", false, 100_003);
  const now = 100_003 + LIMITS.failureMs;
  recordProbe(s, "backup", true, now);
  const answer = heartbeat(s, "backup", payload("backup"), now);
  assert.equal(answer.role, "active");
  assert.equal(s.owner, "backup");
  assert.equal(s.lastTakeover.reason, "automatic");
  assert.equal(s.lastTakeover.detectedAt, 100_003);
  assert.equal(s.lastTakeover.grantedAt, now);
  assert.equal(s.lastTakeover.readyAt, null);
  heartbeat(s, "backup", payload("backup", { epoch: 2 }), now + 1);
  assert.equal(s.lastTakeover.readyAt, now + 1);
});

test('dashboard observability survives heartbeat sanitization without unrelated secrets', () => {
  const s = setup();
  const immich = { state: 'ok', photos: 13979, videos: 1596, checkedAt: 100002 };
  const tailscale = { state: 'direct', latencyMs: 2, checkedAt: 100002 };
  heartbeat(s, 'primary', payload('primary', { protocolVersion: 2, metrics: { immich, tailscale, token: 'private' } }), 100002);
  assert.deepEqual(s.heartbeats.primary.metrics.immich, immich);
  assert.deepEqual(s.heartbeats.primary.metrics.tailscale, tailscale);
  assert.equal(s.heartbeats.primary.metrics.token, undefined);
});
