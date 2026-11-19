import test from "node:test";
import assert from "node:assert/strict";
import { initialState, advance, admin } from "../src/controller.mjs";
import { failOperation } from "../src/operations.mjs";
import { recoveryLabel } from "../web/status-model.mjs";

const start = 1_000_000;
function fixture(owner = "primary") {
  const s = initialState();
  Object.assign(s, { owner, epoch: 3, auto: true, ready: true, leaseUntil: start + 240_000 });
  refresh(s, start);
  return s;
}
function refresh(s, now) {
  for (const node of ["primary", "backup"]) {
    const owner = node === s.owner;
    s.probes[node] = { at: now, ok: owner };
    s.heartbeats[node] = { at: now, localHealthy: owner, enforcing: true, canPromote: false, protocolVersion: 2,
      metrics: { runtimeV2: true, appMode: owner ? "active" : "fenced", runtimeBusy: false, mediaMounted: true,
        appVersion: "3.4.1", postgresVersion: "16.16", disk: { free: 2 * 1024 ** 3 }, databaseDisk: { free: 6 * 1024 ** 3 } } };
  }
  s.leaseUntil = now + 240_000;
}

for (const owner of ["primary", "backup"]) {
  test(`automatically prepares the other machine while ${owner} stays active`, () => {
    const s = fixture(owner);
    advance(s, start);
    assert.equal(s.operation, null);
    advance(s, start + 60_000);
    assert.equal(s.operation.kind, "prepare");
    assert.equal(s.operation.source, owner);
    assert.notEqual(s.operation.target, owner);
    assert.equal(s.operation.autoRecovery, true);
    assert.equal(s.operation.actor, "standby-reconciler");
    assert.equal(s.operation.steps.length, 6);
    assert.equal(s.operation.steps.some((step) => ["transfer", "freeze-dump"].includes(step.action)), false);
    assert.equal(s.owner, owner); assert.equal(s.epoch, 3);
  });
}

test("automatic retries back off and retain the previous failed job", () => {
  const s = fixture(); advance(s, start); advance(s, start + 60_000);
  const first = s.operation.id;
  failOperation(s, start + 61_000, "temporary network failure");
  advance(s, start + 61_000);
  assert.equal(s.recovery.nextAttemptAt, start + 121_000);
  advance(s, start + 120_999); assert.equal(s.operation.id, first);
  refresh(s, start + 121_000); advance(s, start + 121_000);
  assert.notEqual(s.operation.id, first); assert.equal(s.recovery.attempts, 2);
  assert.equal(s.history[0].id, first); assert.equal(s.owner, "primary");
});

test("background snapshot refresh is allowed to finish without reseeding", () => {
  const s = fixture(); s.heartbeats.backup.metrics.runtimeBusy = true;
  advance(s, start); advance(s, start + 120_000);
  assert.equal(s.operation, null); assert.equal(s.recovery.status, "waiting-refresh");
});

test("manual switchover is never replaced by automatic recovery", () => {
  const s = fixture();
  admin(s, { action: "switch", target: "backup", id: "manual-switch" }, start);
  advance(s, start + 60_000);
  assert.equal(s.operation.id, "manual-switch");
  assert.equal(s.recovery.status, "waiting-operation");
});

test("low disk space blocks automated archives and repeated restores", () => {
  const s = fixture(); s.heartbeats.backup.metrics.disk.free = 100;
  advance(s, start); advance(s, start + 60_000);
  assert.equal(s.operation, null); assert.equal(s.recovery.status, "blocked");
});

test("source outage, stale standby or missing media mount cannot start a reseed", () => {
  for (const fault of ["source", "stale", "mount"]) {
    const s = fixture();
    if (fault === "source") s.probes.primary.ok = false;
    if (fault === "stale") s.heartbeats.backup.at = start - 180_001;
    if (fault === "mount") s.heartbeats.backup.metrics.mediaMounted = false;
    advance(s, start); advance(s, start + 60_000);
    assert.equal(s.operation, null, fault);
    assert.equal(s.owner, "primary", fault);
  }
});

test("automatic standby maintenance can be disabled independently of failover", () => {
  const s = fixture(); admin(s, { action: "set-recovery", enabled: false }, start);
  advance(s, start + 60_000);
  assert.equal(s.operation, null); assert.equal(s.auto, true); assert.equal(s.recovery.status, "disabled");
});

test("operator failover preference is restored only after standby is ready", () => {
  for (const desired of [true, false]) {
    const s = fixture(); s.auto = false; s.autoRequested = desired;
    s.probes.backup.ok = true; s.heartbeats.backup.localHealthy = true; s.heartbeats.backup.canPromote = true;
    s.heartbeats.backup.metrics.seed = { source: "primary", sourceEpoch: 3 };
    advance(s, start); assert.equal(s.auto, desired); assert.equal(s.recovery.status, "ready");
  }
});

test("failed frozen handover remains closed instead of being overwritten", () => {
  const s = fixture();
  s.operation = { id: "failed-frozen", status: "failed", maintenance: true };
  advance(s, start); advance(s, start + 60_000);
  assert.equal(s.operation.id, "failed-frozen"); assert.equal(s.recovery.status, "waiting-operation");
});

test("dashboard describes automatic preparation and retry without implying ownership changed", () => {
  const s = { serverTime: start, owner: "backup", nodes: { primary: { label: "Proxmox3" }, backup: { label: "Asus PVE" } },
    recovery: { source: "backup", target: "primary", status: "preparing" } };
  assert.match(recoveryLabel(s), /Proxmox3.*Asus PVE/);
  s.recovery.status = "retry-wait"; s.recovery.nextAttemptAt = start + 60_000;
  assert.match(recoveryLabel(s), /60 giây/);
  s.recovery.status = "ready";
  assert.match(recoveryLabel(s), /Proxmox3 đã sẵn sàng dự phòng cho Asus PVE/);
});
