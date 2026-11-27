// Pure state machine. Only the Durable Object may persist/mutate this state.
import { upgradeOperations, busy, startOperation, advanceOperation, operationCommand,
  operationResult, progressOperation, audit, otherNode, canFastTransfer } from "./operations.mjs";
import { recoveryState, reconcileStandby } from "./recovery.mjs";
import { preferenceState, setPreference, reconcilePreference, RETURN_STABLE_MS } from "./failback.mjs";
import { maintenanceFor, excludedNode, setNodeMaintenance } from "./node-maintenance.mjs";
import { UPGRADE_RECIPES, upgradeLocked } from './immich-upgrade.mjs';
import { configureQuorum, observeQuorum, quorumPaused, quorumReply } from './quorum-control.mjs';
export const LIMITS = Object.freeze({
  leaseMs: 240_000,
  fenceMs: 30_000,
  failureMs: 300_000,
  heartbeatMs: 180_000,
  probeMs: 180_000,
});

export function initialState() {
  return {
    version: 2, owner: "primary", epoch: 1, auto: false,
    leaseUntil: 0, session: null, ready: false,
    heartbeats: {}, probes: {}, failureSince: null, pending: null,
    operation: null, history: [], audit: [],
  };
}

function fresh(record, now, maxAge) {
  return !!record && now >= record.at && now - record.at <= maxAge;
}

export function healthy(s, node, now) {
  const h = s.heartbeats[node];
  const p = s.probes[node];
  return fresh(h, now, LIMITS.heartbeatMs) && h.localHealthy &&
    fresh(p, now, LIMITS.probeMs) && p.ok;
}

export function eligible(s, node, now) {
  if (excludedNode(s, node)) return false;
  const h = s.heartbeats[node];
  if (!healthy(s, node, now) || !h.enforcing || !h.canPromote) return false;
  return h.protocolVersion < 2 || h.metrics?.seed?.sourceEpoch === s.epoch;
}
export const backupEligible = (s, now) => eligible(s, "backup", now);
const eligibility = (s, now) => (node, serving) => serving ? healthy(s, node, now) : eligible(s, node, now);

export function advance(s, now) {
  upgradeOperations(s);
  if (quorumPaused(s)) return s;
  advanceOperation(s, now, eligibility(s, now));
  if (!busy(s) && !s.operation?.maintenance && !s.pending) {
    if (healthy(s, s.owner, now)) s.failureSince = null;
    else if (s.failureSince === null) s.failureSince = now;
    if (s.auto && s.failureSince !== null &&
        now - s.failureSince >= LIMITS.failureMs && eligible(s, otherNode(s.owner), now)) {
      s.pending = { target: otherNode(s.owner), requestedAt: now, reason: "automatic" };
      s.ready = false;
    }
  }
  // Unknown failures wait for expiry. Planned transfers may use verified fencing.
  const fastTransfer = canFastTransfer(s, now);
  if (s.pending && (fastTransfer || now >= s.leaseUntil + LIMITS.fenceMs) && eligible(s, s.pending.target, now)) {
    const old = s.owner;
    const request = s.pending;
    s.owner = s.pending.target;
    s.epoch++;
    s.session = null;
    s.leaseUntil = 0;
    s.ready = false;
    s.lastTakeover = { source: old, target: s.owner, epoch: s.epoch,
      reason: request.reason || (request.jobId ? "planned" : "manual"),
      detectedAt: request.reason === "automatic" ? s.failureSince : null,
      requestedAt: request.requestedAt, grantedAt: now, readyAt: null };
    s.lastTakeover.fastHandover = fastTransfer;
    s.pending = null;
    s.failureSince = null;
    audit(s, now, "coordinator", "owner-changed", `${old} → ${s.owner}`);
    if (fastTransfer) audit(s, now, "coordinator", "fast-handover", "Source durably fenced; target WAL and files verified");
  }
  advanceOperation(s, now, eligibility(s, now));
  markTakeoverReady(s, now);
  if (!upgradeLocked(s)) {
    reconcilePreference(s, now, (node) => healthy(s, node, now), (node) => eligible(s, node, now));
    reconcileStandby(s, now, (node) => healthy(s, node, now), (node) => eligible(s, node, now));
  }
  advanceOperation(s, now, eligibility(s, now));
  return s;
}

function markTakeoverReady(s, now) {
  const change = s.lastTakeover;
  if (change && change.target === s.owner && change.epoch === s.epoch && !change.readyAt &&
      s.ready && !s.pending && !s.operation?.maintenance && healthy(s, s.owner, now)) change.readyAt = now;
}

function takeoverSummary(s) {
  if (s.lastTakeover) return s.lastTakeover;
  // Old deployments recorded only the ownership change. Do not invent the
  // failure-detection/startup timestamps or infer automatic vs manual cause.
  const event = [...(s.audit || [])].reverse().find((item) => item.action === "owner-changed");
  const match = event?.details?.match(/^(primary|backup) → (primary|backup)$/);
  return match && match[2] === s.owner ? { source: match[1], target: match[2], epoch: s.epoch,
    reason: "recorded", detectedAt: null, requestedAt: null, grantedAt: event.at, readyAt: null } : null;
}

export function recordProbe(s, node, ok, now) {
  s.probes[node] = typeof ok === "object" && ok !== null
    ? { at: now, ok: ok.ok === true, status: ok.status ?? null, error: ok.error ?? null }
    : { at: now, ok: !!ok };
  advance(s, now);
}

export function heartbeat(s, node, input, now) {
  if (!["primary", "backup"].includes(node)) throw new Error("Invalid node");
  if (typeof input.session !== "string" || !/^[a-zA-Z0-9_-]{16,100}$/.test(input.session)) {
    throw new Error("Invalid agent session");
  }
  s.heartbeats[node] = {
    at: now, localHealthy: input.localHealthy === true,
    enforcing: input.enforcing === true, canPromote: input.canPromote === true,
    protocolVersion: Number.isInteger(input.protocolVersion) ? input.protocolVersion : 1,
    metrics: input.protocolVersion >= 2 ? cleanMetrics(input.metrics) : null,
  };
  if (input.progress) progressOperation(s, node, input.progress, now);
  observeQuorum(s, node, input, now);
  if (quorumPaused(s) && s.quorumControl.mode !== 'recovering') {
    return reply(s,node,{role:'quorum-wait',owner:s.owner,epoch:s.epoch,leaseMs:0});
  }
  advance(s, now);
  if (s.owner !== node) {
    if (maintenanceFor(s, node).enabled) return reply(s, node, { role: "maintenance", owner: s.owner, epoch: s.epoch, leaseMs: 0 });
    return reply(s, node, { role: "standby", owner: s.owner, epoch: s.epoch, leaseMs: 0 });
  }
  if (s.pending) return reply(s, node, { role: "fenced", owner: s.owner, epoch: s.epoch, leaseMs: 0 });
  if (s.session && s.session !== input.session && now < s.leaseUntil + LIMITS.fenceMs) {
    s.ready = false;
    return reply(s, node, { role: "fenced", owner: s.owner, epoch: s.epoch, leaseMs: 0 });
  }
  const sameSession = s.session === input.session;
  s.ready = sameSession && input.epoch === s.epoch && input.ready === true;
  s.session = input.session;
  s.leaseUntil = now + LIMITS.leaseMs;
  advanceOperation(s, now, eligibility(s, now));
  markTakeoverReady(s, now);
  return reply(s, node, { role: "active", owner: s.owner, epoch: s.epoch, leaseMs: LIMITS.leaseMs });
}

function reply(s, node, grant) {
  const quorum = quorumReply(s,node);
  return { ...grant, quorum, auto: s.auto, command: quorumPaused(s) ? null : operationCommand(s, node),
    operation: busy(s) ? { id: s.operation.id, status: s.operation.status } : null,
    nextPollSeconds: quorumPaused(s) || busy(s) || grant.role === "maintenance" && s.heartbeats[node]?.metrics?.appMode !== "maintenance" || (grant.role === "active" && !s.ready) ? 5 : 60 };
}

function cleanMetrics(input) {
  if (!input || typeof input !== "object") return null;
  // Only data used by the dashboard/job engine; never accept arbitrary config/secrets.
  const output = {};
  for (const key of ["runtimeV2", "appMode", "appVersion", "postgresVersion", "disk", "databaseDisk",
    "memory", "cpu", "backup", "builtinBackups", "seed", "syncthing", "immich", "tailscale", "lastError", "runtimeBusy", "mediaMounted", "maintenanceControl", "postgresStreaming", "immichUpgrade", "quorumShadow", "quorumAuthority"]) {
    if (input[key] !== undefined) output[key] = input[key];
  }
  if (JSON.stringify(output).length > 12_000) throw new Error("Metrics payload too large");
  return output;
}

export function admin(s, input, now) {
  upgradeOperations(s);
  if (input.action === 'set-quorum-policy') {
    if (busy(s) || s.pending || upgradeLocked(s)) throw new Error('Finish current operation first');
    configureQuorum(s,input,now);return;
  }
  if (quorumPaused(s)) throw new Error('Đang đối chiếu quyền điều phối với quorum; chờ máy active xác nhận');
  if (upgradeLocked(s) && !['upgrade-retry','set-alerts','test-alerts'].includes(input.action)) throw new Error('Nâng cấp chưa hoàn tất; chỉ tiếp tục nâng cấp, không đổi vai trò hoặc bật tự động');
  if (input.action === "set-maintenance") {
    setNodeMaintenance(s, input, now, (node) => healthy(s, node, now)); advance(s, now); return;
  }
  if (input.action === "set-preference") {
    setPreference(s, input, now); advance(s, now); return;
  }
  if (input.action === "set-recovery" && typeof input.enabled === "boolean") {
    const r = recoveryState(s); r.enabled = input.enabled;
    if (input.enabled) { r.nextAttemptAt = null; r.unreadySince = null; }
    audit(s, now, input.actor || "operator", "set-recovery", String(input.enabled));
    advance(s, now); return;
  }
  if (["prepare", "switch", "resume", "upgrade-stage", "upgrade-apply", "upgrade-retry"].includes(input.action)) {
    startOperation(s, { ...input, kind: input.action }, now, input.id || crypto.randomUUID(), eligibility(s, now));
    advance(s, now);
    return;
  }
  if (input.action === "set-auto" && typeof input.enabled === "boolean") {
    if (input.enabled && (busy(s) || s.operation?.maintenance)) throw new Error("Đang có thao tác cần hoàn tất");
    if (input.enabled && (!healthy(s, s.owner, now) ||
        !s.heartbeats[s.owner]?.enforcing || !eligible(s, otherNode(s.owner), now))) {
      throw new Error("Both enforcing agents and healthy origin probes are required");
    }
    s.auto = input.enabled;
    s.autoRequested = input.enabled;
    if (!input.enabled && busy(s)) s.operation.previousAuto = false;
    audit(s, now, input.actor || "operator", "set-auto", String(input.enabled));
  } else if (input.action === "promote-backup") {
    if (busy(s) || s.operation?.maintenance) throw new Error("Đang có thao tác cần hoàn tất");
    if (!backupEligible(s, now)) throw new Error("Backup is not ready for promotion");
    if (s.owner === "primary" && !s.pending) {
      s.pending = { target: "backup", requestedAt: now, reason: "manual" };
      s.ready = false;
    }
  } else throw new Error("Unsupported action");
  advance(s, now);
}

export function route(s, now) {
  advance(s, now);
  if (s.operation?.maintenance || s.pending || !s.ready || now >= s.leaseUntil ||
      !healthy(s, s.owner, now)) return null;
  return { node: s.owner, epoch: s.epoch };
}

export function status(s, now) {
  upgradeOperations(s);
  return {
    owner: s.owner, epoch: s.epoch, auto: s.auto, pending: s.pending,
    ready: s.ready, leaseUntil: s.leaseUntil, failureSince: s.failureSince,
    failureConfirmAt: s.failureSince === null ? null : s.failureSince + LIMITS.failureMs,
    primaryHealthy: healthy(s, "primary", now),
    backupHealthy: healthy(s, "backup", now),
    backupEligible: backupEligible(s, now),
    agents: s.heartbeats,
    probes: s.probes,
    serverTime: now, operation: s.operation,
    history: s.history.map(({ results, ...job }) => job), audit: s.audit.slice(-40),
    standbyNode: otherNode(s.owner), standbyEligible: eligible(s, otherNode(s.owner), now),
    lastTakeover: takeoverSummary(s),
    recovery: { ...recoveryState(s), source: s.owner, target: otherNode(s.owner) },
    autoRequested: s.autoRequested ?? s.auto,
    preference: { ...preferenceState(s), stableMs: RETURN_STABLE_MS },
    nodeMaintenance: s.nodeMaintenance || {},
    immichUpgrade: s.immichUpgrade || null, upgradeRecipes: UPGRADE_RECIPES, immichUpgradeProtocol: 2,
    quorumShadowProtocol: 1,
    quorumAuthorityProtocol: 1,
    quorumFallbackProtocol: 2,
    quorumControl: s.quorumControl ? {enabled:s.quorumControl.enabled,clusterId:s.quorumControl.clusterId,mode:s.quorumControl.mode,reconciledAt:s.quorumControl.reconciledAt || null} : null,
  };
}

export function receiveResult(s, node, input, now) {
  const command = s.operation?.command;
  operationResult(s, node, input, now, eligibility(s, now));
  if (input.status === "succeeded" && command?.id === input.commandId && command.jobId === input.jobId && command.index === input.index && command.action === "validate" && command.node === node &&
      command.epoch === s.epoch && command.source === s.owner && s.nodeMaintenance?.[node] && !s.nodeMaintenance[node].enabled) {
    s.nodeMaintenance[node].awaitingValidation = false;
    s.nodeMaintenance[node].validatedAt = now;
  }
  advance(s, now);
}
