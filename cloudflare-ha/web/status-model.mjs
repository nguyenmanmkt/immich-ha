export const freshAgent = (s, node) => !!s.agents?.[node] && s.serverTime >= s.agents[node].at && s.serverTime - s.agents[node].at <= 180000;
export const nodeHealthy = (s, node) => freshAgent(s, node) && s[node + "Healthy"] === true;
export const serving = (s) => s.ready && !s.pending && !s.operation?.maintenance && s.leaseUntil > s.serverTime && nodeHealthy(s, s.owner);
export const nodeName = (s, node) => s.nodes?.[node]?.label || (node === "primary" ? "Máy A" : "Máy B");

export function maintenanceView(s, node) {
  const record = s.nodeMaintenance?.[node] || {}, h = s.agents?.[node], m = h?.metrics;
  const enabled = record.enabled === true;
  const paused = ["immich-library", "immich-database"].every((id) => m?.syncthing?.folders?.[id]?.paused === true);
  const confirmed = enabled && freshAgent(s, node) && m?.appMode === "maintenance" && !h.localHealthy && !m.runtimeBusy && paused;
  const locked = s.pending || s.operation?.maintenance || s.operation && !["succeeded", "failed"].includes(s.operation.status);
  return { enabled, confirmed, revalidating: !enabled && record.awaitingValidation === true,
    canChange: !locked && (enabled || node !== s.owner && serving(s) && m?.maintenanceControl === true),
    label: enabled ? !freshAgent(s, node) ? "Bảo trì chưa xác nhận · mất kết nối" : confirmed ? "Đã vào chế độ bảo trì" : "Chờ xác nhận bảo trì" : record.awaitingValidation ? "Đang kiểm tra lại sau bảo trì" : null };
}

export function preferenceLabel(s) {
  const p = s.preference;
  if (!p?.node) return "Chưa chọn máy ưu tiên. Máy đang phục vụ: " + nodeName(s, s.owner);
  const prefix = `Ưu tiên: ${nodeName(s, p.node)} · Đang phục vụ: ${nodeName(s, s.owner)}. `;
  const remaining = Math.max(0, Math.ceil((Math.max((p.stableSince || s.serverTime) + (p.stableMs || 120000), p.nextAttemptAt || 0) - s.serverTime) / 1000));
  const messages = { disabled: "Tự trở về đang tắt.", "on-preferred": "Đang phục vụ trên máy ưu tiên.",
    maintenance: "Máy ưu tiên đang bảo trì; tự trở về tạm ngưng.",
    "waiting-ready": "Chờ máy ưu tiên khôi phục thành dự phòng sẵn sàng.", "waiting-sync": "Chờ đồng bộ ảnh và database đầy đủ.",
    "waiting-operation": "Chờ thao tác hiện tại hoàn tất.", "waiting-source": "Chờ máy đang phục vụ ổn định.",
    "waiting-stable": `Sẽ tự bàn giao về sau khoảng ${remaining} giây ổn định.`,
    "retry-wait": `Sẽ thử lại sau khoảng ${remaining} giây. ${p.lastError || ""}`,
    returning: "Đang tự đồng bộ và bàn giao về máy ưu tiên.", blocked: "Chưa thể tự trở về: " + (p.lastError || "cần kiểm tra") };
  return prefix + (messages[p.status] || "Đang kiểm tra điều kiện tự trở về.");
}

export function recoveryLabel(s) {
  const r = s.recovery;
  if (!r) return "Đang kiểm tra máy dự phòng…";
  const target = nodeName(s, r.target), source = nodeName(s, r.source);
  const seconds = Math.max(0, Math.ceil(((r.nextAttemptAt || 0) - s.serverTime) / 1000));
  switch (r.status) {
    case "maintenance": return `${target} đang bảo trì · tạm ngưng tự chuẩn bị dự phòng`;
    case "ready": return `${target} đã sẵn sàng dự phòng cho ${source}`;
    case "preparing": return `Đang tự chuẩn bị ${target} · nhận dữ liệu từ ${source}`;
    case "waiting-stable": return `Sẽ tự chuẩn bị ${target} sau khoảng ${seconds} giây`;
    case "retry-wait": return `Sẽ thử lại chuẩn bị ${target} sau khoảng ${seconds} giây · ${r.lastError || "Lỗi tạm thời"}`;
    case "waiting-refresh": return `${target} đang cập nhật database từ bản sao mới`;
    case "waiting-connection": return `Chờ cả hai máy kết nối để tự chuẩn bị ${target}`;
    case "waiting-source": return `Chờ ${source} phục vụ ổn định trước khi chuẩn bị dự phòng`;
    case "waiting-operation": return "Chờ thao tác hiện tại hoàn tất để duy trì máy dự phòng";
    case "blocked": return `Chưa thể chuẩn bị ${target}: ${r.lastError || "cần kiểm tra"}`;
    case "disabled": return "Tự duy trì dự phòng đang tắt";
    default: return `Đang kiểm tra máy dự phòng ${target}`;
  }
}

export function transitionView(s) {
  if (s.operation?.maintenance) return null;
  const change = s.lastTakeover;
  const activating = change?.reason !== "recorded" && change?.target === s.owner && !change.readyAt && !serving(s);
  const outage = !nodeHealthy(s, s.owner) && s.failureSince != null && !activating;
  if (outage || (s.pending && !s.pending.jobId)) {
    const remaining = s.failureConfirmAt ? Math.max(0, Math.ceil((s.failureConfirmAt - s.serverTime) / 1000)) : null;
    const granted = !!s.pending;
    return { phase: granted ? "waiting-lease" : "confirming", source: s.owner,
      target: s.pending?.target || (s.owner === "primary" ? "backup" : "primary"),
      title: "Đã phát hiện sự cố", badge: s.auto ? "ĐANG XỬ LÝ" : "TỰ CHUYỂN ĐANG TẮT", tone: "amber",
      summary: `${nodeName(s, s.owner)} không phản hồi. ` + (!s.auto ? "Chưa tự chuyển máy." : remaining > 0 ? `Chờ xác nhận sự cố: còn khoảng ${remaining} giây.` : "Đang chờ điều kiện tiếp quản."),
      steps: [
        { label: "Phát hiện sự cố", state: "done", at: s.failureSince },
        { label: "Xác nhận sự cố", state: granted ? "done" : "current", detail: remaining > 0 ? `Còn khoảng ${remaining} giây` : undefined },
        { label: "Bàn giao quyền", state: granted ? "current" : "waiting" },
        { label: "Khởi động Immich", state: "waiting" },
        { label: "Phục vụ ứng dụng", state: "waiting" },
      ] };
  }
  if (!change || change.target !== s.owner) return null;
  const live = serving(s);
  const sourceReady = s.standbyNode === change.source && s.standbyEligible;
  return { phase: live ? "serving" : "activating", source: change.source, target: change.target,
    title: change.reason === "automatic" ? "Tự động tiếp quản" : change.reason === "planned" ? "Bàn giao có kế hoạch" : "Chuyển máy phục vụ",
    badge: live ? "ĐANG PHỤC VỤ" : "ĐANG KÍCH HOẠT", tone: live ? "green" : "amber",
    summary: live ? `${nodeName(s, change.target)} đã nhận vai trò active và đang phục vụ qua domain Immich.` : `${nodeName(s, change.target)} đã nhận quyền; đang chờ Immich và cổng truy cập sẵn sàng.`,
    recovery: sourceReady ? `${nodeName(s, change.source)} đã sẵn sàng dự phòng.` : `${nodeName(s, change.source)} chưa sẵn sàng dự phòng. Đồng bộ ngược chưa được xác nhận.`,
    steps: [
      ...(change.reason === "automatic" ? [{ label: "Phát hiện sự cố", state: "done", at: change.detectedAt }] : []),
      { label: "Đã bàn giao quyền", state: "done", at: change.grantedAt },
      { label: "Immich sẵn sàng", state: live ? "done" : "current", at: change.readyAt },
      { label: "Đang phục vụ ứng dụng", state: live ? "done" : "waiting" },
      { label: "Máy cũ sẵn sàng dự phòng", state: sourceReady ? "done" : "waiting" },
    ] };
}
