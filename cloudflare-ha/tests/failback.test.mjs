import test from "node:test";
import assert from "node:assert/strict";
import { initialState, advance, admin, status, receiveResult, heartbeat, LIMITS } from "../src/controller.mjs";
import { failOperation } from "../src/operations.mjs";
import { RETURN_STABLE_MS } from "../src/failback.mjs";
import { preferenceLabel } from "../web/status-model.mjs";

const now = 1_000_000;
function fixture(preferred = "backup") {
  const s = initialState();
  s.owner = preferred === "backup" ? "primary" : "backup";
  Object.assign(s, { epoch: 5, ready: true, auto: true, leaseUntil: now + 240_000 });
  refresh(s, now);
  admin(s, { action: "set-preference", target: preferred, enabled: true }, now);
  return s;
}
function refresh(s, at) {
  for (const node of ["primary", "backup"]) {
    s.probes[node] = { at, ok: true };
    s.heartbeats[node] = { at, localHealthy: true, enforcing: true, canPromote: true, protocolVersion: 2,
      metrics: { runtimeV2: true, runtimeBusy: false, mediaMounted: true, appVersion: "3.4.1", postgresVersion: "16.16",
        disk: { free: 4 * 1024 ** 3 }, databaseDisk: { free: 4 * 1024 ** 3 },
        seed: { source: s.owner, sourceEpoch: s.epoch },
        syncthing: { folders: Object.fromEntries(["immich-library", "immich-database"].map(id => [id, { paused: false, state: "idle", needItems: 0, error: "" }])) } } };
  }
  s.leaseUntil = at + 240_000;
}
for (const preferred of ["primary", "backup"]) {
  test(`returns to preferred ${preferred} only through a complete synchronized handover`, () => {
    const s = fixture(preferred), owner = s.owner;
    assert.equal(s.operation, null);
    refresh(s, now + RETURN_STABLE_MS); advance(s, now + RETURN_STABLE_MS);
    assert.equal(s.operation.kind, "switch"); assert.equal(s.operation.autoFailback, true);
    assert.equal(s.operation.target, preferred); assert.equal(s.operation.steps.length, 18);
    assert.equal(s.operation.steps[6].action, "freeze-dump");
    assert.equal(s.operation.steps[8].action, "sync-restore");
    assert.equal(s.operation.steps[10].action, "transfer");
    assert.equal(s.owner, owner); assert.equal(s.epoch, 5);
    assert.equal(status(s, now + RETURN_STABLE_MS).preference.node, preferred);
  });
}
test("existing installations do not start returning until an explicit preference is enabled", () => {
  const s = fixture(); admin(s, { action: "set-preference", target: "backup", enabled: false }, now);
  refresh(s, now + RETURN_STABLE_MS); advance(s, now + RETURN_STABLE_MS);
  assert.equal(s.operation, null); assert.equal(s.preference.status, "disabled");
});
test("an hour offline resets the stability window; readiness with the old epoch is rejected", () => {
  const s = fixture(); s.heartbeats.backup.metrics.seed.sourceEpoch = 4;
  advance(s, now + 1); assert.equal(s.preference.stableSince, null);
  refresh(s, now + 3_600_000); advance(s, now + 3_600_000);
  assert.equal(s.operation, null); assert.equal(s.preference.stableSince, now + 3_600_000);
});
test("busy refresh, failed mount, low space, paused sync and missing files prevent return", () => {
  for (const fault of ["busy", "mount", "disk", "scratch", "paused", "files", "scan"]) {
    const s = fixture(), m = s.heartbeats.backup.metrics;
    if (fault === "busy") m.runtimeBusy = true;
    if (fault === "mount") m.mediaMounted = false;
    if (fault === "disk") m.disk.free = 1;
    if (fault === "scratch") m.disk.free = 1.3 * 1024 ** 3;
    if (fault === "paused") m.syncthing.folders["immich-library"].paused = true;
    if (fault === "files") m.syncthing.folders["immich-library"].needItems = 1;
    if (fault === "scan") m.syncthing.folders["immich-library"].state = "scanning";
    advance(s, now + RETURN_STABLE_MS);
    assert.equal(s.operation, null, fault);
  }
});
test("already serving preferred machine remains active without creating a handover", () => {
  const s = fixture(); admin(s, { action: "set-preference", target: s.owner, enabled: true }, now);
  advance(s, now + RETURN_STABLE_MS);
  assert.equal(s.operation, null); assert.equal(s.preference.status, "on-preferred");
});

test("dashboard distinguishes the saved preference from the temporary serving machine", () => {
  const s = fixture();
  const view = status(s, now);
  view.nodes = { primary: { label: "Proxmox3" }, backup: { label: "Asus PVE" } };
  assert.match(preferenceLabel(view), /Ưu tiên: Asus PVE · Đang phục vụ: Proxmox3/);
});
test("failure after freeze is never retried or reopened automatically", () => {
  const s = fixture(); refresh(s, now + RETURN_STABLE_MS); advance(s, now + RETURN_STABLE_MS);
  s.operation.maintenance = true;
  failOperation(s, now + RETURN_STABLE_MS + 1, "final snapshot failed");
  advance(s, now + RETURN_STABLE_MS + 2);
  assert.equal(s.preference.status, "blocked"); assert.equal(s.operation.status, "failed");
  assert.equal(s.auto, false); assert.equal(s.owner, "primary");
});
test("failed pre-freeze return has a bounded retry delay and preserves manual job exclusion", () => {
  const s = fixture(); refresh(s, now + RETURN_STABLE_MS); advance(s, now + RETURN_STABLE_MS);
  const id = s.operation.id;
  failOperation(s, now + RETURN_STABLE_MS + 1, "network timeout"); advance(s, now + RETURN_STABLE_MS + 2);
  assert.ok(s.preference.nextAttemptAt >= now + RETURN_STABLE_MS + 300_000);
  assert.equal(s.operation.id, id);
  assert.throws(() => admin(s, { action: "set-preference", target: "not-a-node", enabled: true }, now));
});

test("automatic return transfers only after final snapshot and fencing, then restores the old serving node", () => {
  const s = fixture(); let at = now + RETURN_STABLE_MS;
  refresh(s, at); advance(s, at++);
  function ack() {
    const c = s.operation.command; assert.ok(c);
    const result = ["dump", "freeze-dump"].includes(c.action) ? {
      name: "immich-20261117-150000.dump", sha256: "a".repeat(64), manifestSha256: "b".repeat(64),
      manifestName: "immich-20261117-150000.dump.manifest.json.gz", source: c.source, epoch: c.epoch,
      stopped: c.action === "freeze-dump" } : { healthy: true };
    receiveResult(s, c.node, { jobId: c.jobId, commandId: c.id, index: c.index, status: "succeeded", result }, at++);
  }
  while (s.operation.command) ack();
  assert.equal(s.owner, "primary"); assert.equal(s.pending.reason, "automatic-return");
  assert.equal(s.operation.results[6].stopped, true);
  const due = s.leaseUntil + LIMITS.fenceMs;
  advance(s, due - 1); assert.equal(s.owner, "primary");
  at = due; advance(s, at++); assert.equal(s.owner, "backup");
  assert.equal(s.lastTakeover.reason, "automatic-return"); assert.equal(s.ready, false);
  refresh(s, at);
  for (let i = 0; i < 2; i++) heartbeat(s, "backup", {
    ...s.heartbeats.backup, session: "backup_test_session_123456", epoch: s.epoch, ready: true,
  }, at++);
  while (s.operation.command) ack();
  advance(s, at);
  assert.equal(s.operation.status, "succeeded"); assert.equal(s.preference.node, "backup");
  assert.equal(s.preference.status, "on-preferred"); assert.equal(s.auto, true);
  assert.equal(s.operation.steps[16].action, "sync-restore");
  const persisted = JSON.parse(JSON.stringify(s));
  assert.equal(status(persisted, at).preference.node, "backup");
});
