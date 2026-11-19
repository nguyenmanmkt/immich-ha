import { busy, startOperation, audit, otherNode } from "./operations.mjs";
import { maintenanceFor } from "./node-maintenance.mjs";

const GRACE = 60_000;
const UNHEALTHY_GRACE = 300_000;
export function recoveryState(s) {
  s.recovery ??= { enabled: true, attempts: 0, nextAttemptAt: null, unreadySince: null,
    activeJobId: null, source: s.owner, epoch: s.epoch, status: "waiting", lastError: null };
  return s.recovery;
}

export function reconcileStandby(s, now, healthy, eligible) {
  const r = recoveryState(s);
  if (r.source !== s.owner || r.epoch !== s.epoch) {
    Object.assign(r, { source: s.owner, epoch: s.epoch, attempts: 0, nextAttemptAt: null,
      unreadySince: null, activeJobId: null, lastError: null });
  }
  if (r.activeJobId && s.operation?.id === r.activeJobId && !busy(s)) {
    if (s.operation.status === "failed") {
      r.lastError = s.operation.error;
      r.nextAttemptAt = now + Math.min(900_000, 60_000 * 2 ** Math.min(4, r.attempts - 1));
    } else {
      r.nextAttemptAt = now + GRACE;
      r.lastError = null;
    }
    r.activeJobId = null;
  }
  if (busy(s)) {
    r.status = s.operation.autoRecovery ? "preparing" : "waiting-operation";
    return;
  }
  if (!r.enabled) { r.status = "disabled"; return; }
  if (s.pending || s.operation?.maintenance) { r.status = "waiting-operation"; return; }
  const target = otherNode(s.owner);
  if (maintenanceFor(s, target).enabled) { r.status = "maintenance"; r.unreadySince = null; return; }
  if (eligible(target)) {
    Object.assign(r, { status: "ready", attempts: 0, unreadySince: null, nextAttemptAt: null, lastError: null });
    if (s.autoRequested === true && !s.auto) {
      s.auto = true;
      audit(s, now, "standby-reconciler", "auto-restored", "Standby verified ready");
    }
    return;
  }
  if (!healthy(s.owner) || !s.ready || s.leaseUntil <= now) { r.status = "waiting-source"; return; }
  const a = s.heartbeats[s.owner], b = s.heartbeats[target];
  for (const agent of [a, b]) {
    if (!agent || now - agent.at > 180_000 || !agent.enforcing || agent.protocolVersion < 2 || !agent.metrics?.runtimeV2) {
      r.status = "waiting-connection"; r.unreadySince = null; return;
    }
  }
  if (b.metrics.runtimeBusy) { r.status = "waiting-refresh"; r.unreadySince = null; return; }
  if (b.metrics.mediaMounted === false) { r.status = "blocked"; r.lastError = "Ổ thư viện máy dự phòng chưa mount"; return; }
  if (!(b.metrics.disk?.free >= 1024 ** 3) || !(b.metrics.databaseDisk?.free >= 1024 ** 3)) {
    r.status = "blocked"; r.lastError = "Máy dự phòng cần ít nhất 1 GiB trống trên ổ ảnh và ổ database"; return;
  }
  if (typeof a.metrics.appVersion !== "string" || a.metrics.appVersion !== b.metrics.appVersion ||
      typeof a.metrics.postgresVersion !== "string" || a.metrics.postgresVersion.split(".")[0] !== b.metrics.postgresVersion?.split(".")[0]) {
    r.status = "blocked"; r.lastError = "Phiên bản Immich hoặc PostgreSQL hai máy chưa tương thích"; return;
  }
  r.unreadySince ??= now;
  const seeded = b.metrics.seed?.source === s.owner && b.metrics.seed?.sourceEpoch === s.epoch && b.metrics.appMode === "standby";
  const due = Math.max(r.unreadySince + (seeded ? UNHEALTHY_GRACE : GRACE), r.nextAttemptAt || 0);
  if (now < due) { r.status = r.lastError ? "retry-wait" : "waiting-stable"; r.nextAttemptAt = due; return; }
  const id = crypto.randomUUID();
  try {
    startOperation(s, { kind: "prepare", target, actor: "standby-reconciler", autoRecovery: true,
      requestId: "auto-recovery-" + id }, now, id, (node, serving) => serving ? healthy(node) : eligible(node));
    r.activeJobId = id; r.attempts++; r.status = "preparing"; r.nextAttemptAt = null;
  } catch (error) {
    r.lastError = String(error.message).slice(0, 500); r.status = "blocked"; r.nextAttemptAt = now + GRACE;
  }
}
