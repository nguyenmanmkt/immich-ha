const NODES = ["primary", "backup"];
const DEBOUNCE_MS = 60_000;
export function initialNotifications() {
  return { enabled: false, webhook: null, revision: 0, baseline: null, candidate: null,
    queue: [], lastSentAt: null, lastError: null, failedCount: 0 };
}
export function publicNotifications(n) {
  return { enabled: n.enabled, configured: !!n.webhook, pending: n.queue.length,
    lastSentAt: n.lastSentAt, lastError: n.lastError, failedCount: n.failedCount };
}
export function validateWebhook(value) {
  if (typeof value !== "string" || value.length > 512) throw new Error("Webhook Slack không hợp lệ");
  let url;
  try { url = new URL(value.trim()); } catch { throw new Error("Webhook Slack không hợp lệ"); }
  if (url.protocol !== "https:" || url.hostname !== "hooks.slack.com" || url.port || url.username || url.password || url.search || url.hash ||
      !/^\/services\/T[A-Za-z0-9]+\/B[A-Za-z0-9]+\/[A-Za-z0-9_-]{16,}$/.test(url.pathname)) {
    throw new Error("Cần URL HTTPS Incoming Webhook của hooks.slack.com");
  }
  return url.href;
}
export function configureNotifications(n, input) {
  if (typeof input.enabled !== "boolean") throw new Error("Trạng thái alert không hợp lệ");
  const webhook = input.remove === true ? null : input.webhook?.trim() ? validateWebhook(input.webhook) : n.webhook;
  if (input.enabled && !webhook) throw new Error("Nhập webhook trước khi bật alert");
  Object.assign(n, { enabled: input.enabled, webhook, revision: n.revision + 1,
    baseline: null, candidate: null, queue: [], lastError: null });
}
function snapshot(s) {
  const states = {};
  for (const node of NODES) {
    const h = s.agents[node], m = h?.metrics;
    const fresh = !!h && s.serverTime >= h.at && s.serverTime - h.at <= 180_000;
    if (s.nodeMaintenance?.[node]?.enabled) states[node] = "maintenance";
    else if (!fresh) states[node] = "offline";
    else if (node === s.owner) states[node] = s.operation?.maintenance || s.pending ? "handover" :
      s.ready && s.leaseUntil > s.serverTime && s[node + "Healthy"] ? "serving" : !s.ready ? "starting" : "offline";
    else states[node] = s.standbyEligible ? "standby-ready" : m?.runtimeBusy || ["receiving", "fenced"].includes(m?.appMode) ? "syncing" : "standby-not-ready";
  }
  return { owner: s.owner, epoch: s.epoch, states };
}
function enqueue(n, event, now) {
  if (n.queue.length >= 50) { n.failedCount++; n.lastError = "Hàng đợi Slack đầy; kiểm tra webhook"; return; }
  n.queue.push({ ...event, id: crypto.randomUUID(), at: now, attempts: 0, nextAttemptAt: now });
}
export function queueTest(n, s, now) {
  if (!n.enabled || !n.webhook) throw new Error("Lưu và bật webhook trước khi gửi thử");
  if (n.queue.some((e) => e.kind === "test")) throw new Error("Đang có tin thử chờ gửi");
  enqueue(n, { kind: "test", ...snapshot(s) }, now);
}
export function observeNotifications(n, s, now) {
  if (!n.enabled || !n.webhook) return;
  const next = snapshot(s);
  if (!n.baseline) { n.baseline = next; return; }
  const roleChanged = next.owner !== n.baseline.owner || next.epoch !== n.baseline.epoch;
  const servingReady = next.states[next.owner] === "serving" && n.baseline.states[next.owner] !== "serving";
  const key = JSON.stringify(next);
  if (key === JSON.stringify(n.baseline)) { n.candidate = null; return; }
  if (!roleChanged && !servingReady) {
    if (n.candidate?.key !== key) { n.candidate = { key, since: now }; return; }
    if (now - n.candidate.since < DEBOUNCE_MS) return;
  }
  enqueue(n, { kind: roleChanged ? "role-change" : "state-change", ...next,
    previousOwner: n.baseline.owner, reason: s.lastTakeover?.reason || "unknown" }, now);
  n.baseline = next; n.candidate = null;
}
const escape = (text) => String(text).replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;");
export function slackPayload(event, nodes = {}) {
  const label = (node) => escape(nodes[node]?.label || (node === "primary" ? "Máy A" : "Máy B"));
  const states = { maintenance: "Đang bảo trì (không tiếp quản)", serving: "Đang phục vụ (active)", starting: "Đang kích hoạt", offline: "Mất kết nối / không phản hồi",
    "standby-ready": "Dự phòng sẵn sàng", "standby-not-ready": "Dự phòng chưa sẵn sàng", syncing: "Đang đồng bộ thành dự phòng", handover: "Đang chốt ghi / bàn giao" };
  const titles = { "role-change": "Đổi quyền phục vụ", "state-change": "Thay đổi trạng thái hai máy", test: "Kiểm tra kết nối Slack" };
  const reasons = { automatic: "Tự chuyển khi có sự cố", "automatic-return": "Tự trở về máy ưu tiên", planned: "Bàn giao chủ động", manual: "Chuyển thủ công" };
  const lines = [`Immich HA · ${titles[event.kind]}`];
  if (event.kind === "role-change") lines.push(`${label(event.previousOwner)} → ${label(event.owner)}`, reasons[event.reason] || "Thay đổi quyền phục vụ");
  for (const node of NODES) lines.push(`${label(node)}: ${states[event.states[node]]}`);
  lines.push(`Epoch ${event.epoch} · ${new Intl.DateTimeFormat("vi-VN", { timeZone: "Asia/Ho_Chi_Minh", dateStyle: "short", timeStyle: "medium" }).format(new Date(event.at))}`, `Sự kiện: ${event.id}`);
  return { text: lines.join("\n"), unfurl_links: false, unfurl_media: false };
}
export async function postSlack(webhook, payload, fetcher = fetch) {
  try {
    const response = await fetcher(validateWebhook(webhook), { method: "POST", redirect: "manual",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload), signal: AbortSignal.timeout(8000) });
    if (response.status === 200 && (await response.text()).trim() === "ok") return { ok: true };
    const retry = response.status === 429 || response.status >= 500;
    const seconds = Number(response.headers.get("Retry-After"));
    return { ok: false, retry, error: `Slack HTTP ${response.status}`, retryAfter: Number.isFinite(seconds) && seconds > 0 ? Math.min(3600_000, seconds * 1000) : 0 };
  } catch { return { ok: false, retry: true, error: "Không kết nối được Slack hoặc hết thời gian chờ", retryAfter: 0 }; }
}
export function completeNotification(n, id, result, now) {
  const event = n.queue.find((e) => e.id === id);
  if (!event) return;
  if (result.ok) { n.lastSentAt = now; n.lastError = null; n.queue = n.queue.filter((e) => e.id !== id); }
  else {
    n.lastError = result.error;
    if (!result.retry || event.attempts >= 8) { n.failedCount++; n.queue = n.queue.filter((e) => e.id !== id); }
    else event.nextAttemptAt = now + Math.max(result.retryAfter || 0, Math.min(900_000, 30_000 * 2 ** (event.attempts - 1)));
  }
}
