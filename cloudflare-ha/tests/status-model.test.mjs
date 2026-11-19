import test from "node:test";
import assert from "node:assert/strict";
import { transitionView, freshAgent, nodeHealthy, serving } from "../web/status-model.mjs";
import { initialState, status } from "../src/controller.mjs";

function setup() {
  return { owner: "backup", epoch: 2, ready: true, auto: true, serverTime: 1000000,
    leaseUntil: 1100000, primaryHealthy: false, backupHealthy: true,
    agents: { primary: { at: 700000, localHealthy: true }, backup: { at: 999000, localHealthy: true } },
    nodes: { primary: { label: "Proxmox3" }, backup: { label: "Asus PVE" } },
    standbyNode: "primary", standbyEligible: false,
    lastTakeover: { source: "primary", target: "backup", reason: "automatic", detectedAt: 600000, grantedAt: 930000, readyAt: 990000 } };
}

test("offline old primary never appears healthy from a stale healthy heartbeat", () => {
  const s = setup();
  assert.equal(freshAgent(s, "primary"), false);
  assert.equal(nodeHealthy(s, "primary"), false);
  assert.equal(serving(s), true);
  const view = transitionView(s);
  assert.equal(view.phase, "serving");
  assert.match(view.summary, /Asus PVE/);
  assert.match(view.recovery, /chưa sẵn sàng/);
  assert.equal(view.steps.at(-1).state, "waiting");
});

test("fault confirmation shows countdown before ownership changes", () => {
  const s = setup();
  Object.assign(s, { owner: "primary", ready: false, failureSince: 900000, failureConfirmAt: 1200000, lastTakeover: null });
  const view = transitionView(s);
  assert.equal(view.phase, "confirming");
  assert.match(view.summary, /200 giây/);
  assert.equal(view.steps[1].state, "current");
  assert.equal(view.steps[2].state, "waiting");
});

test("new owner warming up is activating, not a newly failed old owner", () => {
  const s = setup();
  s.lastTakeover.readyAt = null;
  s.ready = false; s.backupHealthy = false; s.failureSince = 999000;
  const view = transitionView(s);
  assert.equal(view.phase, "activating");
  assert.match(view.summary, /đang chờ/);
  assert.equal(serving(s), false);
});

test("legacy ownership audit is retained without invented timestamps or cause", () => {
  const s = initialState();
  s.owner = "backup"; s.epoch = 2;
  s.audit = [{ at: 930000, action: "owner-changed", details: "primary → backup" }];
  const view = status(s, 1000000).lastTakeover;
  assert.equal(view.reason, "recorded");
  assert.equal(view.detectedAt, null);
  assert.equal(view.readyAt, null);
  assert.equal(view.grantedAt, 930000);
});

test("manual switchover maintenance is shown by the planned job panel", () => {
  const s = setup(); s.operation = { maintenance: true };
  assert.equal(transitionView(s), null);
  assert.equal(serving(s), false);
});

test("expired public grant never displays as serving even if app is healthy", () => {
  const s = setup(); s.leaseUntil = s.serverTime;
  assert.equal(serving(s), false);
});
