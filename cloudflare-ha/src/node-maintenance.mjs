import { busy, audit, NODE_IDS } from "./operations.mjs";

export function maintenanceFor(s, node) {
  return s.nodeMaintenance?.[node] || { enabled: false, awaitingValidation: false };
}
export const excludedNode = (s, node) => {
  const m = maintenanceFor(s, node);
  return m.enabled || m.awaitingValidation;
};

export function setNodeMaintenance(s, input, now, healthy) {
  const node = input.target;
  if (!NODE_IDS.includes(node) || typeof input.enabled !== "boolean") throw new Error("Chọn máy và trạng thái bảo trì hợp lệ");
  if (busy(s) || s.pending || s.operation?.maintenance) throw new Error("Chờ thao tác hiện tại hoàn tất trước khi đổi chế độ bảo trì");
  if (input.enabled) {
    if (node === s.owner) throw new Error("Bàn giao sang máy còn lại trước khi bảo trì máy đang phục vụ");
    if (!healthy(s.owner) || !s.ready || now >= s.leaseUntil) throw new Error("Máy đang phục vụ phải sẵn sàng trước khi bảo trì dự phòng");
    if (!s.heartbeats[node]?.metrics?.maintenanceControl) throw new Error("Máy chưa sẵn sàng nhận lệnh bảo trì; chờ kết nối cập nhật trạng thái");
  }
  s.nodeMaintenance ??= {};
  const previous = maintenanceFor(s, node);
  if ((previous.enabled === true) === input.enabled) return;
  s.nodeMaintenance[node] = { ...previous, enabled: input.enabled, awaitingValidation: true,
    ...(input.enabled ? { startedAt: now, endedAt: null } : { endedAt: now }),
    actor: String(input.actor || "operator").slice(0, 150) };
  if (s.preference) s.preference.stableSince = null;
  if (s.recovery) { s.recovery.unreadySince = null; s.recovery.nextAttemptAt = null; }
  audit(s, now, input.actor || "operator", input.enabled ? "maintenance-start" : "maintenance-end", node);
}
