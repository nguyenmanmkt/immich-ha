import { busy, startOperation, audit, NODE_IDS } from "./operations.mjs";
import { maintenanceFor } from "./node-maintenance.mjs";

export const RETURN_STABLE_MS = 120_000;
export function preferenceState(s) {
  s.preference ??= { node: null, enabled: false, status: "disabled", stableSince: null,
    nextAttemptAt: null, activeJobId: null, attempts: 0, lastError: null };
  return s.preference;
}

export function setPreference(s, input, now) {
  if (!NODE_IDS.includes(input.target) || typeof input.enabled !== "boolean") throw new Error("Chọn máy ưu tiên và trạng thái tự trở về hợp lệ");
  if (busy(s) || s.pending || s.operation?.maintenance) throw new Error("Chờ thao tác đang chạy hoàn tất trước khi đổi máy ưu tiên");
  Object.assign(preferenceState(s), { node: input.target, enabled: input.enabled, stableSince: null,
    nextAttemptAt: null, activeJobId: null, attempts: 0, lastError: null });
  audit(s, now, input.actor || "operator", "set-preference", `${input.target}; autoReturn=${input.enabled}`);
}

export function reconcilePreference(s, now, healthy, eligible) {
  const p = preferenceState(s);
  if (p.activeJobId) {
    const job = [s.operation, ...s.history].find((j) => j?.id === p.activeJobId);
    if (job && ["failed", "succeeded"].includes(job.status)) {
      p.activeJobId = null;
      p.stableSince = null;
      if (job.status === "failed") {
        p.lastError = job.error;
        p.nextAttemptAt = now + Math.min(900_000, 300_000 * 2 ** Math.min(2, p.attempts - 1));
      } else { p.lastError = null; p.nextAttemptAt = null; p.attempts = 0; }
    }
  }
  if (!p.enabled || !NODE_IDS.includes(p.node)) { p.status = "disabled"; return; }
  if (maintenanceFor(s, p.node).enabled) { p.status = "maintenance"; p.stableSince = null; return; }
  if (busy(s) || s.pending) { p.status = s.operation?.autoFailback ? "returning" : "waiting-operation"; p.stableSince = null; return; }
  if (s.operation?.maintenance) { p.status = "blocked"; p.lastError = s.operation.error || "Bàn giao đang dừng ở bước chốt ghi; cần xử lý trước khi tiếp tục"; return; }
  if (s.owner === p.node) {
    Object.assign(p, { status: "on-preferred", stableSince: null, nextAttemptAt: null, attempts: 0, lastError: null }); return;
  }
  if (!healthy(s.owner) || !s.ready || s.leaseUntil <= now) { p.status = "waiting-source"; p.stableSince = null; return; }
  const target = s.heartbeats[p.node];
  if (!eligible(p.node) || target?.metrics?.seed?.source !== s.owner || target.metrics.seed.sourceEpoch !== s.epoch) {
    p.status = "waiting-ready"; p.stableSince = null; return;
  }
  // A return creates archives and multiple final dumps. Reserve their space
  // before freezing writes, rather than discovering a full disk mid-handover.
  const dumpEstimates = [s.owner, p.node].map((node) => {
    const b = s.heartbeats[node]?.metrics?.backup;
    return b?.count > 0 && Number.isFinite(b.bytes) ? b.bytes / b.count : 0;
  });
  const mediaRequired = 1024 ** 3 + Math.max(512 * 1024 ** 2, 4 * Math.max(...dumpEstimates));
  for (const node of [s.owner, p.node]) {
    const h = s.heartbeats[node], m = h?.metrics;
    if (!h || now < h.at || now - h.at > 180_000 || !h.enforcing || h.protocolVersion < 2 || !m?.runtimeV2 || m.runtimeBusy) {
      p.status = "waiting-ready"; p.stableSince = null; return;
    }
    if (m.mediaMounted !== true || !(m.disk?.free >= mediaRequired) || !(m.databaseDisk?.free >= 1024 ** 3)) {
      p.status = "blocked"; p.lastError = `Cần mount ổ ảnh, tối thiểu ${(mediaRequired / 1024 ** 3).toFixed(1)} GiB trống trên ổ ảnh và 1 GiB trên ổ database của cả hai máy`; p.stableSince = null; return;
    }
    for (const id of ["immich-library", "immich-database"]) {
      const folder = m.syncthing?.folders?.[id];
      if (!folder || folder.paused || folder.error || folder.needItems !== 0 || folder.state !== "idle") {
        p.status = "waiting-sync"; p.stableSince = null; return;
      }
    }
  }
  p.stableSince ??= now;
  const due = Math.max(p.stableSince + RETURN_STABLE_MS, p.nextAttemptAt || 0);
  if (now < due) { p.status = p.lastError ? "retry-wait" : "waiting-stable"; return; }
  try {
    const id = crypto.randomUUID();
    startOperation(s, { kind: "switch", target: p.node, actor: "preferred-node-reconciler",
      autoFailback: true, requestId: "auto-return-" + id }, now, id, (node, serving) => serving ? healthy(node) : eligible(node));
    p.activeJobId = id; p.attempts++; p.status = "returning"; p.nextAttemptAt = null;
  } catch (error) {
    p.status = "blocked"; p.lastError = String(error.message).slice(0, 500);
    p.nextAttemptAt = now + 300_000; p.stableSince = null;
  }
}
