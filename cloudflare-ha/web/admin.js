"use strict";
import { freshAgent, nodeHealthy, serving, transitionView, recoveryLabel, preferenceLabel, maintenanceView } from "/assets/status-model.mjs";
const $ = (selector) => document.querySelector(selector);
const nodes = ["primary", "backup"];
let state = null;
let target = null;
let displayedOwner = null;
let preferredNode = null;
let returnEnabled = false;
let preferenceDirty = false;
let alertsDirty = false;
let timer = null;
let loading = false;
let submitting = false;
let upgradeVersion = null;
let dialogAction = null;
let toastTimer = null;
const expandedNodes = new Set();
const labels = { "maintenance-start": "Bắt đầu bảo trì", "maintenance-end": "Kết thúc bảo trì", "set-preference": "Lưu máy chính ưu tiên", prepare: "Chuẩn bị & đồng bộ", switch: "Đổi máy chính / dự phòng", resume: "Khôi phục phục vụ nguồn", "set-auto": "Thay đổi tự chuyển máy", "set-recovery": "Thay đổi tự duy trì dự phòng", "auto-restored": "Bật lại tự chuyển máy", "owner-changed": "Đổi máy chính" };
Object.assign(labels,{'upgrade-stage':'Chuẩn bị phiên bản Immich','upgrade-apply':'Nâng cấp Immich','upgrade-retry':'Tiếp tục nâng cấp Immich'});
const terminal = (job) => !job || ["succeeded", "failed"].includes(job.status);
const time = (value) => value ? new Intl.DateTimeFormat("vi-VN", { timeZone: "Asia/Ho_Chi_Minh", day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" }).format(new Date(value)) : "Chưa có";
const bytes = (value) => Number.isFinite(value) ? (value / 1024 ** 3).toLocaleString("vi-VN", { maximumFractionDigits: 1 }) + " GiB" : "—";
const el = (tag, className, text) => { const node = document.createElement(tag); if (className) node.className = className; if (text !== undefined) node.textContent = text; return node; };
const name = (node) => state?.nodes?.[node]?.label || (node === "primary" ? "Máy A" : "Máy B");
const isFresh = (node) => !!state && freshAgent(state, node);
const isHealthy = (node) => !!state && nodeHealthy(state, node);
const capable = (node) => isFresh(node) && state.agents[node].enforcing && state.agents[node].protocolVersion >= 2 && state.agents[node].metrics?.runtimeV2;

function toast(message) {
  clearTimeout(toastTimer); $("#toast").textContent = message; $("#toast").hidden = false;
  toastTimer = setTimeout(() => { $("#toast").hidden = true; }, 6500);
}

async function api(path, payload) {
  const response = await fetch(path, {
    credentials: "same-origin", cache: "no-store",
    method: payload ? "POST" : "GET",
    headers: payload ? { "Content-Type": "application/json", "X-Immich-HA-UI": "1" } : {},
    body: payload ? JSON.stringify(payload) : undefined,
  });
  if (!response.headers.get("content-type")?.includes("application/json")) {
    throw new Error("Không đọc được trạng thái. Hãy đăng nhập lại qua Cloudflare Access.");
  }
  const value = await response.json();
  if (!response.ok) throw new Error(value.error || "Không hoàn tất được yêu cầu");
  return value;
}

function showNotice(message, variant = "info") {
  const banner = $("#connection-banner"); banner.textContent = message;
  banner.className = "notice " + variant; banner.hidden = !message;
}

function metric(label, value) {
  const box = el("div"); box.append(el("span", "meta-label", label), el("span", "meta-value", value)); return box;
}

// Small, local SVGs: no font or icon service is required.
function icon(kind) {
  const paths = {
    // Official Immich mark: https://github.com/immich-app/immich/blob/main/design/immich-logo.svg
    immich: "<path fill=\"#FA2921\" d=\"M375.48,267.63c38.64,34.21,69.78,70.87,89.82,105.42c34.42-61.56,57.42-134.71,57.71-181.3 c0-0.33,0-0.63,0-0.91c0-68.94-68.77-95.77-128.01-95.77s-128.01,26.83-128.01,95.77c0,0.94,0,2.2,0,3.72 C300.01,209.24,339.15,235.47,375.48,267.63z\"/><path fill=\"#ED79B5\" d=\"M164.7,455.63c24.15-26.87,61.2-55.99,103.01-80.61c44.48-26.18,88.97-44.47,128.02-52.84 c-47.91-51.76-110.37-96.24-154.6-110.91c-0.31-0.1-0.6-0.19-0.86-0.28c-65.57-21.3-112.34,35.81-130.64,92.15 c-18.3,56.34-14.04,130.04,51.53,151.34C162.05,454.77,163.25,455.16,164.7,455.63z\"/><path fill=\"#FFB400\" d=\"M681.07,302.19c-18.3-56.34-65.07-113.45-130.64-92.15c-0.9,0.29-2.1,0.68-3.54,1.15 c-3.75,35.93-16.6,81.27-35.96,125.76c-20.59,47.32-45.84,88.27-72.51,118c69.18,13.72,145.86,12.98,190.26-1.14 c0.31-0.1,0.6-0.2,0.86-0.28C695.11,432.22,699.37,358.52,681.07,302.19z\"/><path fill=\"#1E83F7\" d=\"M336.54,510.71c-11.15-50.39-14.8-98.36-10.7-138.08c-64.03,29.57-125.63,75.23-153.26,112.76 c-0.19,0.26-0.37,0.51-0.53,0.73c-40.52,55.78-0.66,117.91,47.27,152.72c47.92,34.82,119.33,53.54,159.86-2.24 c0.56-0.76,1.3-1.78,2.19-3.01C363.28,602.32,347.02,558.08,336.54,510.71z\"/><path fill=\"#18C249\" d=\"M617.57,482.52c-35.33,7.54-82.42,9.33-130.72,4.66c-51.37-4.96-98.11-16.32-134.63-32.5 c8.33,70.03,32.73,142.73,59.88,180.6c0.19,0.26,0.37,0.51,0.53,0.73c40.52,55.78,111.93,37.06,159.86,2.24 c47.92-34.82,87.79-96.95,47.27-152.72C619.2,484.77,618.46,483.75,617.57,482.52z\"/>",
    cpu: '<rect x="6" y="6" width="12" height="12" rx="2"/><path d="M9 1v5m6-5v5M9 18v5m6-5v5M1 9h5m-5 6h5m12-6h5m-5 6h5M10 10h4v4h-4z"/>',
    memory: '<rect x="2" y="6" width="20" height="12" rx="2"/><path d="M6 10v4m4-4v4m4-4v4m4-4v4M6 18v3m4-3v3m4-3v3m4-3v3"/>',
    image: '<rect x="3" y="3" width="18" height="18" rx="3"/><circle cx="8" cy="8" r="1.5"/><path d="m3 17 5-5 4 4 4-7 5 8"/>',
    network: '<circle cx="5" cy="6" r="2"/><circle cx="19" cy="6" r="2"/><circle cx="12" cy="19" r="2"/><path d="M7 6h10M6 8l5 9m7-9-5 9"/>',
    server: '<rect x="3" y="3" width="18" height="7" rx="2"/><rect x="3" y="14" width="18" height="7" rx="2"/><path d="M7 6.5h.01M7 17.5h.01M11 6.5h6M11 17.5h6"/>',
    shield: '<path d="M12 3 4 6v6c0 5 8 9 8 9s8-4 8-9V6l-8-3Z"/><path d="m8 12 3 3 5-6"/>',
    sync: '<path d="M20 7a8 8 0 0 0-14-1L3 9m0-5v5h5M4 17a8 8 0 0 0 14 1l3-3m0 5v-5h-5"/>',
    database: '<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v14c0 4 16 4 16 0V5M4 12c0 4 16 4 16 0"/>',
    check: '<circle cx="12" cy="12" r="9"/><path d="m8 12 3 3 5-6"/>',
    alert: '<path d="m12 3 10 18H2L12 3ZM12 9v5m0 3h.01"/>',
    archive: '<path d="M4 8h16v13H4zM3 3h18v5H3zM9 12h6"/>',
    download: '<path d="M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5"/>',
    upload: '<path d="M12 16V4m-5 5 5-5 5 5M4 17v4h16v-4"/>',
    pause: '<path d="M8 5v14M16 5v14"/>',
    handover: '<path d="M3 7h17m-4-4 4 4-4 4M21 17H4m4-4-4 4 4 4"/>',
    clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    play: '<path d="m9 5 11 7-11 7V5Z"/>',
    pulse: '<path d="M2 12h5l3-8 4 16 3-8h5"/>',
    tool: '<path d="m14 6 4 4-9 9a3 3 0 0 1-4-4l9-9ZM14 6V3l4 1 3 3-1 4h-3"/>'
  };
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 24 24'); svg.setAttribute('fill', 'none');
  svg.setAttribute('stroke', 'currentColor'); svg.setAttribute('stroke-width', '1.7');
  svg.setAttribute('stroke-linecap', 'round'); svg.setAttribute('stroke-linejoin', 'round');
  svg.setAttribute('aria-hidden', 'true'); svg.classList.add('svg-icon');
  if (kind === 'immich') { svg.setAttribute('viewBox', '0 0 792 792'); svg.setAttribute('stroke', 'none'); }
  svg.innerHTML = paths[kind] || paths.server; return svg;
}
function keyMetric(kind, label, value, warning = false) {
  const tile = el('div', 'metric-tile' + (warning ? ' warning' : ''));
  const title = el('div', 'tile-title'); title.append(icon(kind), el('span', '', label));
  tile.append(title, el('strong', '', value)); return tile;
}
function renderServer(node) {
  const info = state.nodes?.[node] || {}, agent = state.agents?.[node], metrics = agent?.metrics || {};
  const owner = state.owner === node, fresh = isFresh(node), health = isHealthy(node);
  const maintenance = maintenanceView(state, node);
  const tone = !fresh ? 'unavailable' : maintenance.enabled ? 'maintenance' : owner && serving(state) ? 'active' : state.standbyEligible ? 'standby' : 'unavailable';
  const card = el('article', 'server-card ' + tone);
  const head = el('div', 'server-head'), heading = el('div', 'server-heading'), title = el('div', 'server-title');
  title.append(el('h2', 'server-name', name(node)));
  const symbol = el('div', 'server-symbol'); symbol.append(icon('server')); heading.append(symbol, title);
  const badge = maintenance.label || (!fresh ? 'Mất liên lạc' : owner ? serving(state) ? 'Đang phục vụ' : 'Chờ phục vụ' : state.standbyEligible ? 'Dự phòng sẵn sàng' : 'Chưa sẵn sàng');
  const statusRow = el('div', 'server-status-row');
  statusRow.append(el('span', 'server-state ' + (maintenance.enabled ? 'recovery' : !fresh ? 'offline' : owner && serving(state) ? 'active' : 'standby'), badge));
  title.append(statusRow);
  head.append(heading);
  const resources = el('div', 'header-resources');
  const cpu = metrics.cpu, ram = metrics.memory;
  const cpuText = Number.isFinite(cpu?.percent) ? cpu.percent.toLocaleString('vi-VN', { maximumFractionDigits: 1 }) + '%' : 'Đang đo';
  const cpuChip = keyMetric('cpu', 'CPU', cpu ? cpuText + (Number.isFinite(cpu.cores) ? ` · ${cpu.cores.toLocaleString('vi-VN')} core` : '') : 'Chưa có dữ liệu');
  cpuChip.classList.add('resource-cpu');
  if (Number.isFinite(ram?.used) && Number.isFinite(ram?.total)) {
    const ramChip = keyMetric('memory', 'RAM', `${bytes(ram.used)} / ${bytes(ram.total)}`);
    ramChip.classList.add('resource-ram'); resources.append(ramChip);
  }
  resources.append(cpuChip);
  if (!fresh) { resources.classList.add('stale'); resources.title = 'Số liệu cũ · mất heartbeat'; }
  statusRow.append(resources);
  const headActions = el('div', 'server-head-actions');
  if (state.preference?.node === node) {
    const star = el('span', 'preferred-chip preferred-star', '★');
    star.title = 'Máy ưu tiên'; star.setAttribute('aria-label', 'Máy ưu tiên');
    headActions.append(star);
  }
  const gear = el('button', 'settings-gear', '⚙'); gear.type = 'button';
  gear.setAttribute('aria-label', 'Cài đặt ' + name(node));
  gear.setAttribute('aria-expanded', String(expandedNodes.has(node)));
  headActions.append(gear); head.append(headActions);
  const disk = metrics.disk || {};
  const pct = disk.total > 0 ? Math.min(100, Math.round(disk.used / disk.total * 100)) : null;
  const severity = pct >= 90 ? 'critical' : pct >= 80 ? 'warning' : '';
  const storage = el('div', 'storage-overview'), ring = el('div', 'disk-ring ' + severity);
  ring.style.setProperty('--percent', (pct || 0) + '%');
  ring.append(el('span', '', pct === null ? '—' : pct + '%')); ring.setAttribute('aria-label', pct === null ? 'Chưa có số liệu dung lượng' : `Đã dùng ${pct}% dung lượng`);
  const storageCopy = el('div', 'storage-copy'), value = el('div', 'storage-value');
  value.append(el('strong', '', bytes(disk.used)), document.createTextNode(' / ' + bytes(disk.total)));
  storageCopy.append(el('div', 'storage-heading', fresh ? 'Dung lượng thư viện' : 'Dung lượng · số liệu cũ'), value,
    el('div', 'storage-note ' + severity, `Còn ${bytes(disk.free)}${severity === 'critical' ? ' · Sắp đầy' : ''}`));
  storage.append(ring, storageCopy);
  const library = metrics.syncthing?.folders?.['immich-library'];
  let syncText = !fresh ? 'Chưa xác nhận' : !library ? 'Chờ số liệu' : library.paused ? 'Đang tạm dừng' : library.error ? 'Có lỗi đồng bộ' : library.needItems > 0 ? `Còn ${library.needItems.toLocaleString('vi-VN')} mục` : 'Đã đồng bộ';
  if (maintenance.enabled) syncText = maintenance.confirmed ? 'Đã dừng để bảo trì' : 'Chờ tạm dừng';
  if (fresh && library && !library.paused && !library.error && metrics.syncthing?.peerConnected === false) syncText = 'Mất kết nối';
  const counts = metrics.immich;
  const countsOk = counts?.state === 'ok' && Number.isFinite(counts.photos) && Number.isFinite(counts.videos);
  const countsStale = !fresh || !counts?.checkedAt || state.serverTime - counts.checkedAt > 180000;
  const countText = countsOk ? `${counts.photos.toLocaleString('vi-VN')} ảnh · ${counts.videos.toLocaleString('vi-VN')} video` : 'Chưa có dữ liệu';
  const tail = metrics.tailscale;
  const tailFresh = fresh && tail?.checkedAt && state.serverTime - tail.checkedAt <= 180000;
  const tailStates = { direct: 'Trực tiếp', relay: 'Qua relay', offline: 'Máy kia offline', unreachable: 'Không phản hồi', stopped: 'Chưa bật', unavailable: 'Chưa có dữ liệu', unknown: 'Chưa xác định' };
  const tailText = !tailFresh ? 'Chưa xác nhận' : (tailStates[tail.state] || 'Chưa xác định') + (['direct', 'relay'].includes(tail.state) && Number.isFinite(tail.latencyMs) ? ` · ${tail.latencyMs.toLocaleString('vi-VN', {maximumFractionDigits: 1})} ms` : '');
  const keyMetrics = el('div', 'server-key-metrics');
  const libraryCounts = el('div', 'library-counts' + (countsOk && countsStale ? ' stale' : ''));
  const countsSymbol = el('div', 'library-counts-symbol'); countsSymbol.append(icon('immich'));
  const countsCopy = el('div', 'library-counts-copy');
  countsCopy.append(el('div', 'library-counts-heading', 'Immich' + (countsOk && countsStale ? ' · số liệu cũ' : !owner ? ' · DB dự phòng' : '')), el('strong', '', countText));
  libraryCounts.append(countsSymbol, countsCopy); storage.append(libraryCounts);
  const stream = metrics.postgresStreaming;
  const streamOk = fresh && stream?.available !== false && (owner ? stream?.inRecovery === false : stream?.receiverConnected && stream?.pendingBytes === 0);
  const dbLabel = stream ? 'PostgreSQL streaming' : owner ? 'Sao lưu database' : 'Database đã nạp';
  const dbText = stream ? !fresh ? 'Chưa xác nhận' : stream.available === false ? 'Chưa kết nối' : owner ? 'Nguồn dữ liệu' : stream.receiverConnected ? stream.pendingBytes > 0 ? `Đang áp dụng · ${bytes(stream.pendingBytes)}` : 'Đang nhận WAL' : 'Mất kết nối nguồn' : time(owner ? metrics.backup?.lastDumpAt : metrics.seed?.appliedAt);
  keyMetrics.append(keyMetric('sync', 'Syncthing', syncText, !fresh || !library || library.paused || !!library.error || metrics.syncthing?.peerConnected === false),
    keyMetric('database', dbLabel, dbText, stream && !streamOk));
  keyMetrics.append(keyMetric('network', 'Tailscale', tailText, tailFresh && !['direct', 'relay'].includes(tail.state)));
  keyMetrics.append(keyMetric('pulse', 'Heartbeat', time(agent?.at), !fresh));
  const details = el('details', 'node-details'); details.open = expandedNodes.has(node);
  details.addEventListener('toggle', () => { if (details.open) expandedNodes.add(node); else expandedNodes.delete(node); });
  details.dataset.nodeSettings = node;
  const summary = el('summary'); summary.hidden = true; details.append(summary);
  gear.addEventListener('click', () => {
    details.open = !details.open;
    if (details.open) expandedNodes.add(node); else expandedNodes.delete(node);
    gear.setAttribute('aria-expanded', String(details.open));
  });
  details.addEventListener('toggle', () => gear.setAttribute('aria-expanded', String(details.open)));
  const meta = el('div', 'server-meta');
  meta.append(metric('Container', 'LXC ' + (info.containerId || '—')), metric('Địa chỉ', info.ip || '—'),
    metric('Immich', metrics.appVersion || '—'), metric('PostgreSQL', metrics.postgresVersion || '—'),
    metric('Dịch vụ Immich', maintenance.confirmed ? 'Đã dừng để bảo trì' : !fresh ? 'Chưa xác nhận' : health ? 'Đang chạy' : 'Không phản hồi'));
  if (metrics.syncthing?.peerAddress) meta.append(metric('Kết nối Syncthing', metrics.syncthing.peerAddress));
  if (fresh && Number.isFinite(metrics.syncthing?.receiveBps)) {
    const rate = (bps) => bps >= 1024 ** 2 ? (bps / 1024 ** 2).toLocaleString('vi-VN', {maximumFractionDigits: 1}) + ' MiB/s' : (bps / 1024).toLocaleString('vi-VN', {maximumFractionDigits: 1}) + ' KiB/s';
    meta.append(metric('Syncthing nhận / gửi', rate(metrics.syncthing.receiveBps) + ' / ' + rate(metrics.syncthing.sendBps)));
  }
  if (tail?.peerIp) meta.append(metric('Tailscale máy kia', tail.peerIp));
  if (tail?.checkedAt) meta.append(metric('Đo Tailscale', time(tail.checkedAt)));
  if (counts?.checkedAt) meta.append(metric('Thống kê Immich', time(counts.checkedAt)));
  const button = el('button', 'metric-tile maintenance-tile' + (maintenance.enabled ? ' warning' : ''));
  button.type = 'button'; button.dataset.maintenanceNode = node; button.disabled = submitting || !maintenance.canChange;
  button.addEventListener('click', () => confirmAction('set-maintenance', node));
  const actionLabel = maintenance.enabled ? maintenance.confirmed ? 'Kết thúc bảo trì' : 'Hủy yêu cầu bảo trì' : 'Bảo trì máy';
  const maintenanceTitle = el('span', 'tile-title'); maintenanceTitle.append(icon('tool'), el('span', '', 'Bảo trì'));
  button.append(maintenanceTitle, el('strong', 'button-caption', actionLabel));
  button.title = owner ? 'Bàn giao sang máy còn lại trước khi bảo trì.' : maintenance.enabled ? maintenance.confirmed ? 'Dịch vụ và đồng bộ đã dừng.' : 'Chưa xác nhận dừng dịch vụ. Chờ trước khi nâng cấp.' : 'Tạm dừng dịch vụ và đồng bộ trên máy này.';
  button.setAttribute('aria-label', actionLabel + ' · ' + name(node));
  const actions = el('div', 'server-key-metrics node-settings-actions');
  actions.append(button);
  if (!owner) {
    const transfer = el('button', 'metric-tile maintenance-tile');
    const transferTitle = el('span', 'tile-title'); transferTitle.append(icon('server'), el('span', '', 'Vai trò'));
    transfer.append(transferTitle, el('strong', '', 'Chuyển active'));
    transfer.setAttribute('aria-label', 'Chuyển active · ' + name(node));
    transfer.type = 'button'; transfer.dataset.switchNode = node; transfer.disabled = true;
    transfer.addEventListener('click', () => { target = node; confirmAction('switch'); });
    actions.append(transfer);
  } else {
    const resume = el('button', 'button danger node-resume', 'Khôi phục phục vụ');
    resume.type = 'button'; resume.dataset.resumeNode = node; resume.hidden = true;
    resume.addEventListener('click', () => confirmAction('resume')); details.append(resume);
  }
  details.append(actions);
  details.append(meta);
  card.append(head, storage, keyMetrics, details);
  if (!owner) {
    const prepare = el('button', 'metric-tile maintenance-tile');
    const prepareTitle = el('span', 'tile-title'); prepareTitle.append(icon('sync'), el('span', '', 'Đồng bộ lại'));
    prepare.append(prepareTitle, el('strong', '', 'Dự phòng'));
    prepare.type = 'button'; prepare.dataset.prepareNode = node; prepare.disabled = true;
    prepare.setAttribute('aria-label', 'Đồng bộ lại dự phòng · ' + name(node));
    prepare.addEventListener('click', () => confirmAction('prepare'));
    actions.append(prepare);
  }
  return card;
}

function renderControls() {
  const running = !terminal(state.operation);
  const upgradeLocked = state.immichUpgrade?.applying && state.immichUpgrade.phase !== 'completed';
  const possible = !upgradeLocked && nodes.every(capable) && isHealthy(state.owner) && !running && !state.pending && !submitting && !state.nodeMaintenance?.[nodes.find((node) => node !== state.owner)]?.enabled;
  document.querySelectorAll("[data-maintenance-node]").forEach((button) => { button.disabled = upgradeLocked || submitting || !maintenanceView(state, button.dataset.maintenanceNode).canChange; });
  document.querySelectorAll('[data-prepare-node]').forEach((button) => {
    button.disabled = !possible || button.dataset.prepareNode === state.owner;
  });
  document.querySelectorAll('[data-switch-node]').forEach((button) => { button.disabled = !possible || button.dataset.switchNode === state.owner; });
  $("#auto-toggle").checked = state.auto;
  $("#auto-toggle").disabled = upgradeLocked || running || submitting || !!state.pending || (!state.auto && !state.standbyEligible);
  $("#recovery-toggle").checked = state.recovery?.enabled === true;
  $("#recovery-toggle").disabled = upgradeLocked || running || submitting || !state.recovery;
  $("#preferred-node").disabled = upgradeLocked || running || submitting || !!state.pending || !!state.operation?.maintenance;
  $("#return-toggle").disabled = $("#preferred-node").disabled || preferenceDirty;
  $("#save-preference").disabled = $("#preferred-node").disabled || !preferenceDirty;
  $("#alerts-enable").disabled = submitting || alertsDirty || !state.alerts?.configured;
  $("#slack-webhook").disabled = submitting;
  $("#save-alerts").disabled = submitting || !alertsDirty;
  $("#test-alerts").disabled = submitting || !state.alerts?.enabled || !state.alerts?.configured || state.alerts.pending > 0;
  $("#delete-alerts").disabled = submitting || !state.alerts?.configured;
  const op = state.operation;
  const canResume = !upgradeLocked && op?.status === "failed" && op.maintenance && op.source === state.owner && op.sourceEpoch === state.epoch && !state.pending && capable(state.owner);
  document.querySelectorAll('[data-resume-node]').forEach((button) => { button.hidden = !canResume || button.dataset.resumeNode !== state.owner; button.disabled = submitting; });
  renderUpgrades();
}

function renderUpgrades() {
  const recipes = state.upgradeRecipes || [];
  upgradeVersion ||= recipes[0]?.version;
  const recipe = recipes.find(r => r.version === upgradeVersion);
  const select = $('#upgrade-version');select.replaceChildren();
  for (const r of recipes) { const option=el('option','',r.version);option.value=r.version;select.append(option); }
  select.value=upgradeVersion || '';
  $('#upgrade-notes').hidden=!recipe;
  if (recipe) $('#upgrade-notes').href=recipe.notesUrl;
  const saved=state.immichUpgrade, job=state.operation;
  const running=!terminal(job), locked=saved?.applying && saved.phase!=='completed';
  const online=nodes.every(n=>capable(n) && state.agents[n].metrics?.immichUpgrade?.protocol===1 &&
    (!recipe || state.agents[n].metrics.immichUpgrade.recipes?.includes(recipe.recipe)));
  const ready=online && recipe && state.ready && state.standbyEligible && !state.pending && !running && !submitting;
  const sameBase=recipe && nodes.every(n=>state.agents?.[n]?.metrics?.appVersion===recipe.fromVersion);
  const prepared=saved?.phase==='prepared' && saved.source===state.owner && saved.epoch===state.epoch && saved.version===upgradeVersion;
  select.disabled=running || locked || submitting;
  $('#upgrade-stage').disabled=!ready || !sameBase || locked;
  $('#upgrade-apply').disabled=!ready || !sameBase || !prepared || locked;
  $('#upgrade-retry').hidden=!(locked && job?.status==='failed');
  $('#upgrade-retry').disabled=!online || running || submitting;
  const phases={prepared:'ĐÃ CHUẨN BỊ',applying:'ĐANG NÂNG CẤP',completed:'ĐÃ NÂNG CẤP',stale:'CẦN CHUẨN BỊ LẠI'};
  $('#upgrade-phase').textContent=locked && job?.status==='failed' ? 'CẦN XỬ LÝ' : phases[saved?.phase] || (running && job.upgrade ? 'ĐANG CHUẨN BỊ' : 'CHƯA CHUẨN BỊ');
  $('#upgrade-nodes').replaceChildren();
  for (const n of nodes) {
    const card=el('div','upgrade-node');card.append(icon('server'));
    const text=el('div','',name(n));text.append(el('strong','',state.agents?.[n]?.metrics?.appVersion || 'Chưa xác nhận'));
    card.append(text);$('#upgrade-nodes').append(card);
  }
  const current=recipe && nodes.every(n=>state.agents?.[n]?.metrics?.appVersion===recipe.version);
  $('#upgrade-hint').textContent=locked ? 'Đang bảo vệ phiên nâng cấp. Nếu có lỗi, tiếp tục sửa bản mới; không tự hạ code hoặc phục hồi database.' :
    current ? `Cả hai máy đang chạy Immich ${recipe.version}.` :
    !online ? 'Chờ cả hai agent hỗ trợ nâng cấp và kết nối đầy đủ.' : prepared ? 'Bản mới đã chuẩn bị trên cả hai máy. Bước áp dụng sẽ sao lưu database và tạm ngưng phục vụ.' :
    !sameBase ? 'Hai máy phải cùng phiên bản đầu vào của recipe. Chưa hỗ trợ hạ phiên bản hoặc nâng cấp ngoài danh sách.' :
    'Chuẩn bị riêng code mới trong staging, giữ app hiện tại phục vụ. Chỉ áp dụng sau lần xác nhận tiếp theo.';
  const progress=$('#upgrade-progress');progress.hidden=!job?.upgrade;
  if (job?.upgrade) progress.textContent=job.error || job.progress?.message || (job.status==='succeeded' ? 'Hoàn tất · '+(job.kind==='upgrade-stage'?'đã chuẩn bị, chưa nâng phiên bản':'đã xác nhận cả hai máy') : job.steps[job.index]?.label || 'Đang xử lý');
}

function renderJob() {
  const job = state.operation;
  const detail = $("#job-detail");
  if (!job) return;
  $("#job-title").textContent = (terminal(job) ? job.status === 'succeeded' ? "Đã hoàn tất · " : "Thao tác gần nhất · " : "") + (job.autoFailback ? "Tự trở về máy ưu tiên" : job.autoRecovery ? "Tự chuẩn bị máy dự phòng" : labels[job.kind] || "Thao tác điều phối");
  const statusText = { running: "ĐANG CHẠY", "waiting-lease": "CHỜ BÀN GIAO", "waiting-ready": "CHỜ SẴN SÀNG", succeeded: "HOÀN TẤT", failed: "CẦN XỬ LÝ" };
  $("#job-status").textContent = statusText[job.status] || job.status;
  $("#job-status").className = "small-badge " + (job.status === "failed" ? "red" : job.status === "succeeded" ? "green" : "amber");
  detail.replaceChildren();
  $('#job-route').textContent = `${name(job.source)} → ${name(job.target)}`;
  if (job.upgrade) $('#job-route').textContent=`${job.upgrade.fromVersion} → ${job.upgrade.version} · Cả hai máy`;
  const progressMessage = job.progress?.message || (job.maintenance ? 'Tạm dừng phục vụ · đang chốt dữ liệu' : '');
  if (progressMessage && !terminal(job)) detail.append(el("div", "job-progress", progressMessage));
  if (job.error) detail.append(el("div", "notice error", job.error));
  const list = el("ol", "timeline");
  list.setAttribute('aria-label', 'Tiến trình từ trái sang phải');
  const shortNames = {
    archive: 'Lưu bản cũ', receive: 'Nhận dữ liệu', send: 'Chọn nguồn', dump: 'Sao lưu DB',
    'sync-restore': 'Đồng bộ DB', validate: 'Kiểm tra', 'freeze-dump': 'Chốt dữ liệu',
    'wal-barrier': 'Chốt WAL', 'sync-wal': 'WAL & ảnh', 'freeze-wal': 'Dừng ghi',
    'wait-peer': 'Chờ tệp cuối', transfer: 'Bàn giao', 'wait-owner': 'Mở phục vụ', resume: 'Mở lại',
    'upgrade-preflight':'Kiểm tra','upgrade-build':'Build bản mới','upgrade-verify':'Xác minh',
    'upgrade-freeze':'Dừng ghi','upgrade-backup':'Backup cuối','upgrade-install':'Cài code',
    'upgrade-copy-backup':'Chép backup',
    'upgrade-start':'Mở bản mới','upgrade-wait':'Chờ active','upgrade-check':'Xác nhận'
  };
  const stepIcons = { archive: 'archive', receive: 'download', send: 'upload', dump: 'database',
    'sync-restore': 'sync', validate: 'check', 'freeze-dump': 'pause', 'wait-peer': 'clock',
    'wal-barrier': 'database', 'sync-wal': 'sync', 'freeze-wal': 'pause',
    transfer: 'handover', 'wait-owner': 'play', resume: 'play' };
  job.steps.forEach((step, index) => {
    const completed = index < job.index || job.status === "succeeded";
    const li = el("li", completed ? "done" : index === job.index ? job.status === "failed" ? "failed" : "current" : "");
    const marker = el("span", "step-marker"); marker.append(icon(index === job.index && job.status === "failed" ? "alert" : stepIcons[step.action] || "server"));
    li.append(marker, el("span", "step-name", step.final ? step.action === 'sync-wal' ? 'WAL cuối' : "Nạp DB cuối" : shortNames[step.action] || step.label));
    const description = step.node ? name(step.node) : "Bộ điều phối";
    const phase = completed ? 'Hoàn tất' : index === job.index ? job.status === 'failed' ? 'Lỗi' : 'Đang chạy' : 'Chờ';
    li.title = `${index + 1}. ${step.label} · ${description} · ${phase}`;
    li.setAttribute('aria-label', li.title);
    if (index === job.index && !terminal(job)) li.setAttribute('aria-current', 'step');
    list.append(li);
  });
  detail.append(list);
  if (!terminal(job)) list.scrollLeft = Math.max(0, job.index * 66 - (list.clientWidth || 660) / 2);
}

function renderBackups() {
  const summary = $('#backup-summary'); summary.replaceChildren();
  const inventory = {}, files = new Map();
  const fileBytes = (n) => Number.isFinite(n) ? (n / 1024 ** 2).toLocaleString('vi-VN', { maximumFractionDigits: 1 }) + ' MiB' : '—';
  for (const node of nodes) {
    const data = state.agents?.[node]?.metrics?.builtinBackups;
    inventory[node] = data;
    const current = isFresh(node) && data?.state === 'ok' && state.serverTime - data.checkedAt <= 180000;
    const card = el('article', 'panel backup-summary-card'), symbol = el('div', 'server-symbol'); symbol.append(icon('archive'));
    const copy = el('div'); copy.append(el('h2', '', name(node)), el('strong', '', data?.state === 'ok' ? `${data.count} bản · ${fileBytes(data.bytes)}` : 'Chưa có dữ liệu'), el('small', '', current ? 'Cập nhật ' + time(data.checkedAt) : 'Chưa xác nhận · số liệu có thể cũ'));
    card.append(symbol, copy); summary.append(card);
    for (const file of data?.files || []) {
      if (!files.has(file.name)) files.set(file.name, {});
      files.get(file.name)[node] = file;
    }
  }
  const box = $('#backup-list'); box.replaceChildren();
  const table = el('table', 'backup-table'), head = el('thead'), headRow = el('tr');
  for (const label of ['File backup', 'Ngày tạo', 'Dung lượng', ...nodes.map(name)]) headRow.append(el('th', '', label));
  head.append(headRow); table.append(head);
  const body = el('tbody');
  const rows = [...files.entries()].sort((a,b) => Math.max(...Object.values(b[1]).map(f => f.modifiedAt)) - Math.max(...Object.values(a[1]).map(f => f.modifiedAt)));
  for (const [filename, copies] of rows) {
    const file = copies[state.owner] || Object.values(copies)[0], row = el('tr');
    const label = el('td', 'backup-filename', filename); label.title = filename;
    row.append(label, el('td', '', time(file.modifiedAt)), el('td', '', fileBytes(file.bytes)));
    for (const node of nodes) {
      const cell = el('td');
      const known = inventory[node]?.state === 'ok' && isFresh(node) && state.serverTime - inventory[node].checkedAt <= 180000;
      const badge = el('span', 'small-badge ' + (copies[node] ? 'green' : known ? 'amber' : ''), copies[node] ? known ? 'Có file' : 'Có · số liệu cũ' : known ? inventory[node].truncated ? 'Ngoài danh sách' : 'Chưa có' : 'Chưa xác nhận');
      cell.append(badge); row.append(cell);
    }
    body.append(row);
  }
  table.append(body);
  if (rows.length) box.append(table);
  else box.append(el('div', 'history-empty', 'Chưa ghi nhận backup mặc định Immich.'));
  if (nodes.some(n => inventory[n]?.truncated)) box.append(el('p', 'hint', 'Hiển thị tối đa 30 bản mới nhất mỗi máy.'));
  if (state.publicUrl) { $('#manage-backups').href = new URL('/admin/maintenance', state.publicUrl).href; $('#manage-backups').hidden = false; }
}

function renderHistory() {
  const box = $("#history-list"); box.replaceChildren();
  const events = [...(state.audit || [])].reverse();
  if (!events.length) { box.append(el("div", "history-empty", "Chưa có thao tác được ghi nhận.")); return; }
  for (const item of events) {
    const row = el("div", "history-row");
    const details = el("div"); details.append(el("strong", "", labels[item.action] || item.action), el("p", "", item.details || ""));
    row.append(el("time", "", time(item.at)), details, el("small", "", item.actor)); box.append(row);
  }
}

function renderTransition() {
  const view = transitionView(state);
  const panel = $("#transition-panel");
  const job = state.operation;
  const activeJob = job && !terminal(job);
  // A job owns the card until terminal; health changes cannot revive an older takeover.
  const newIncident = ['confirming', 'waiting-lease'].includes(view?.phase);
  const takeoverAt = state.lastTakeover?.grantedAt || state.lastTakeover?.requestedAt || 0;
  const jobAt = job?.createdAt || job?.finishedAt || 0;
  const visible = !activeJob && !!view && (newIncident || !job || takeoverAt > jobAt);
  panel.hidden = !visible;
  $('#job-panel').hidden = visible;
  if (!view) return;
  $("#transition-title").textContent = view.title;
  $("#transition-path").textContent = "TIẾP QUẢN · " + name(view.source) + " → " + name(view.target);
  $("#transition-badge").textContent = view.badge;
  $("#transition-badge").className = "small-badge " + view.tone;
  $("#transition-summary").textContent = view.summary;
  const steps = $("#transition-steps"); steps.replaceChildren();
  for (const step of view.steps) {
    const item = el("li", step.state, step.label);
    if (step.at || step.detail) item.append(el("small", "", step.at ? time(step.at) : step.detail));
    steps.append(item);
  }
  $("#transition-recovery").textContent = view.recovery || "";
  $("#transition-recovery").hidden = !view.recovery;
}

function render() {
  $("#servers").replaceChildren(...nodes.map(renderServer));
  if (!target || displayedOwner !== state.owner) target = nodes.find((node) => node !== state.owner);
  displayedOwner = state.owner;
  if (!preferenceDirty) { preferredNode = state.preference?.node || state.owner; returnEnabled = state.preference?.enabled === true; }
  $("#preferred-node").replaceChildren(...nodes.map((node) => { const option = el("option", "", name(node)); option.value = node; return option; }));
  $("#preferred-node").value = preferredNode;
  $("#return-toggle").checked = returnEnabled;
  $("#preference-label").textContent = preferenceLabel(state);
  $("#preference-summary").textContent = state.preference?.node ? name(state.preference.node) : "Chưa chọn máy ưu tiên";
  const alerts = state.alerts || {};
  if (!alertsDirty) $("#alerts-enable").checked = alerts.enabled === true;
  $("#slack-webhook").placeholder = alerts.configured ? "Đã lưu webhook · để trống để giữ nguyên" : "https://hooks.slack.com/services/…";
  $("#alerts-summary").textContent = !alerts.configured ? "Chưa cấu hình" : alerts.lastError ? "Cần kiểm tra" : "Webhook đã lưu";
  $("#alerts-label").textContent = alerts.lastError ? alerts.lastError : alerts.pending > 0 ? `Đang chờ gửi ${alerts.pending} thông báo.` : alerts.lastSentAt ? "Gửi thành công gần nhất: " + time(alerts.lastSentAt) : alerts.configured ? "Webhook đã lưu. Bấm Gửi thử để kiểm tra channel." : "Bạn nhập webhook rồi bật alert và lưu.";
  $("#account-label").textContent = state.actorEmail || "Quản trị viên";
  const standby = nodes.find((node) => node !== state.owner);
  const maintenance = maintenanceView(state, standby);
  const healthy = serving(state);
  const transition = transitionView(state);
  const liveTransition = transition && transition.phase !== 'serving';
  const title = !healthy ? 'Phục vụ đang gián đoạn' : maintenance.enabled ? maintenance.confirmed ? 'Dự phòng đang bảo trì' : 'Chờ xác nhận bảo trì' : state.standbyEligible ? 'Hệ thống sẵn sàng' : 'Dự phòng chưa sẵn sàng';
  $('#system-summary').hidden = healthy || !!liveTransition || !!state.operation?.maintenance || !!(state.operation && !terminal(state.operation));
  $('#system-title').textContent = title;
  $('#system-summary').className = 'panel system-summary' + (!healthy ? ' error' : !state.standbyEligible ? ' warning' : '');
  $('#summary-symbol').replaceChildren(icon(!healthy ? 'alert' : maintenance.enabled ? 'tool' : state.standbyEligible ? 'shield' : 'sync'));
  $('#recovery-label').hidden = healthy;
  $('#recovery-label').textContent = maintenance.enabled ? maintenance.confirmed ? 'Dịch vụ và đồng bộ trên máy dự phòng đã dừng.' : 'Chưa xác nhận dừng dịch vụ trên máy dự phòng.' : recoveryLabel(state);
  $('#account-label').parentElement.title = state.actorEmail || 'Quản trị viên';
  const ownerFailed = ["confirming", "waiting-lease"].includes(transition?.phase);
  $("#serving-label").textContent = state.operation?.maintenance ? "Đang chốt dữ liệu · tạm dừng phục vụ" : ownerFailed ? "Đã phát hiện sự cố: " + name(state.owner) : serving(state) ? "Đang phục vụ qua " + name(state.owner) : "Đang chờ " + name(state.owner) + " sẵn sàng phục vụ";
  $("#live-dot").className = "live-dot" + (serving(state) ? " healthy" : "");
  $("#footer-domain").textContent = state.publicUrl ? new URL(state.publicUrl).hostname : "Immich Control";
  if (state.publicUrl) { $("#open-immich").href = state.publicUrl; $("#open-immich").hidden = false; }
  $("#preview-badge").hidden = !state.preview;
  if (state.preview) showNotice("Bản xem trước giao diện · Số liệu mô phỏng. Các nút không điều khiển server thật.");
  else showNotice("");
  renderTransition(); renderControls(); renderJob(); renderBackups(); renderHistory();
}

async function load() {
  if (loading || document.hidden) return;
  clearTimeout(timer); loading = true; $("#refresh").disabled = true;
  try { state = await api("/api/status"); render(); }
  catch (error) { showNotice(error.message, "error"); $("#live-dot").className = "live-dot"; }
  finally {
    loading = false; $("#refresh").disabled = false;
    const transition = state && transitionView(state);
    const maintenancePending = state && nodes.some((node) => state.nodeMaintenance?.[node]?.enabled && isFresh(node) && !maintenanceView(state, node).confirmed);
    timer = setTimeout(load, state && (maintenancePending || !terminal(state.operation) || state.alerts?.pending > 0 && !state.alerts.lastError || transition && transition.phase !== "serving") ? 5000 : 60000);
  }
}

function confirmAction(kind, requestedNode = null) {
  if (kind.startsWith('upgrade-')) {
    const version=kind==='upgrade-retry'?state.immichUpgrade?.version:upgradeVersion;
    if (!version) return;
    dialogAction={kind,version,expected:version};
    $('#confirm-title').textContent=kind==='upgrade-stage'?'Chuẩn bị phiên bản '+version:kind==='upgrade-retry'?'Tiếp tục nâng cấp '+version:'Nâng cấp cả hai lên '+version;
    $('#confirm-description').textContent=kind==='upgrade-stage'?'Build trong staging trên hai máy; chưa thay code đang chạy. Tự chuyển máy tạm dừng trong job chuẩn bị.':'Sẽ tạm ngưng phục vụ, sao lưu database, cài cùng phiên bản trên cả hai máy và chỉ chạy migration trên active. Có lỗi sẽ giữ khóa bàn giao; không tự downgrade hoặc restore database.';
    $('#confirm-summary').textContent=`Active giữ nguyên: ${name(state.owner)} · Phiên bản ${version}`;
    $('#confirm-label').textContent=`Nhập “${version}” để xác nhận`;
    $('#confirm-input').value='';$('#confirm-submit').disabled=true;$('#confirm-dialog').showModal();$('#confirm-input').focus();return;
  }
  if (!state || submitting) return;
  const destination = kind === "set-maintenance" ? requestedNode : kind === "set-preference" ? preferredNode : kind === "resume" ? state.owner : kind === "prepare" ? nodes.find((node) => node !== state.owner) : target;
  dialogAction = { kind, target: destination, enabled: returnEnabled, expected: name(destination).trim().toLowerCase() };
  if (kind === "set-maintenance") dialogAction.enabled = !state.nodeMaintenance?.[destination]?.enabled;
  $("#confirm-title").textContent = kind === "set-preference" ? "Lưu máy chính ưu tiên" : labels[kind];
  $("#confirm-description").textContent = kind === "switch" ? "Máy đã chọn sẽ giữ vai trò máy chính; máy hiện tại thành dự phòng. Hệ thống đồng bộ trước, rồi tạm dừng ghi để bàn giao. Vai trò mới được lưu và giữ sau khi khởi động lại." : kind === "prepare" ? "Máy chính tiếp tục phục vụ. Máy dự phòng được giữ bản khôi phục trước khi đồng bộ và nạp database từ nguồn." : "Chỉ khôi phục nguồn khi chưa có máy khác tiếp quản. Hệ thống sẽ kiểm tra quyền hiện tại trước khi mở lại.";
  $("#confirm-summary").textContent = kind === "resume" ? "Mở lại: " + name(state.owner) : kind === "switch" ? `Chính: ${name(destination)} · Dự phòng: ${name(state.owner)}` : name(state.owner) + " → " + name(destination);
  if (kind === "set-preference") {
    $("#confirm-description").textContent = "Lưu máy ưu tiên độc lập với máy đang phục vụ. Nếu bật tự trở về, hệ thống có thể tự bắt đầu bàn giao sau khi máy ưu tiên đồng bộ đầy đủ và ổn định 2 phút.";
    $("#confirm-summary").textContent = `Ưu tiên: ${name(destination)} · Tự trở về: ${returnEnabled ? "Bật" : "Tắt"}`;
  }
  if (kind === "switch" && state.preference?.enabled && destination !== state.preference.node) {
    $("#confirm-description").textContent += ` Tự trở về ${name(state.preference.node)} đang bật; tắt tùy chọn này nếu muốn giữ máy đã chọn phục vụ lâu dài.`;
  }
  if (kind === "set-maintenance") {
    $("#confirm-title").textContent = dialogAction.enabled ? "Bắt đầu bảo trì" : "Kết thúc bảo trì";
    $("#confirm-description").textContent = dialogAction.enabled ? "Máy này sẽ tạm dừng Immich, ML và đồng bộ; không tự nhận active hoặc tự trở về máy ưu tiên. Chờ dashboard xác nhận dịch vụ đã dừng trước khi nâng cấp." : "Hệ thống sẽ tự đồng bộ và kiểm tra lại máy này. Chỉ khi sẵn sàng mới được nhận active hoặc tự trở về theo cấu hình ưu tiên đã lưu.";
    $("#confirm-summary").textContent = name(destination);
    if (!dialogAction.enabled && state.recovery?.enabled === false) $("#confirm-description").textContent = "Máy được gỡ chế độ bảo trì nhưng vẫn chưa được tiếp quản. Tự duy trì dự phòng đang tắt; bật lại hoặc dùng Đồng bộ lại dự phòng để kiểm tra máy.";
  }
  $("#confirm-label").textContent = "Nhập “" + name(destination) + "” để xác nhận";
  $("#confirm-input").value = ""; $("#confirm-submit").disabled = true;
  $("#confirm-dialog").showModal(); $("#confirm-input").focus();
}

async function sendAction(payload) {
  submitting = true; renderControls();
  try {
    const result = await api("/api/action", { ...payload, requestId: crypto.randomUUID() });
    if (payload.action === "set-preference") preferenceDirty = false;
    if (payload.action === "set-alerts") { alertsDirty = false; $("#slack-webhook").value = ""; }
    state = result; render(); toast(payload.action === "test-alerts" ? "Đã xếp tin thử vào hàng đợi Slack." : payload.action === "set-alerts" ? "Đã lưu cấu hình Slack." : "Đã ghi nhận yêu cầu. Theo dõi tiến trình trên dashboard.");
  } catch (error) { toast(error.message); }
  finally { submitting = false; if (state) renderControls(); clearTimeout(timer); timer = setTimeout(load, 1500); }
}

$("#confirm-input").addEventListener("input", () => { $("#confirm-submit").disabled = !dialogAction || $("#confirm-input").value.trim().toLowerCase() !== dialogAction.expected; });
$("#confirm-form").addEventListener("submit", (event) => {
  if (event.submitter?.value !== "confirm") return;
  if (!dialogAction || $("#confirm-input").value.trim().toLowerCase() !== dialogAction.expected) { event.preventDefault(); return; }
  sendAction({ action: dialogAction.kind, target: dialogAction.target, ...(dialogAction.version ? {version:dialogAction.version} : {}), ...(["set-preference", "set-maintenance"].includes(dialogAction.kind) ? { enabled: dialogAction.enabled } : {}) });
});
$("#auto-toggle").addEventListener("change", (event) => { const enabled = event.target.checked; event.target.checked = state.auto; sendAction({ action: "set-auto", enabled }); });
$("#recovery-toggle").addEventListener("change", (event) => { const enabled = event.target.checked; event.target.checked = state.recovery.enabled; sendAction({ action: "set-recovery", enabled }); });
$("#refresh").addEventListener("click", load);
$('#upgrade-version').addEventListener('change', event=>{upgradeVersion=event.target.value;renderUpgrades();});
for (const kind of ['stage','apply','retry']) $('#upgrade-'+kind).addEventListener('click',()=>confirmAction('upgrade-'+kind));
$("#preferred-node").addEventListener("change", (event) => { preferredNode = event.target.value; preferenceDirty = true; renderControls(); });
$("#return-toggle").addEventListener("change", (event) => {
  const enabled = event.target.checked; event.target.checked = state.preference?.enabled === true;
  sendAction({ action: 'set-preference', target: state.preference?.node || state.owner, enabled });
});
$("#save-preference").addEventListener("click", () => confirmAction("set-preference"));
$("#slack-webhook").addEventListener("input", () => { alertsDirty = true; renderControls(); });
$("#alerts-enable").addEventListener("change", (event) => {
  const enabled = event.target.checked; event.target.checked = state.alerts?.enabled === true;
  sendAction({ action: 'set-alerts', enabled });
});
$("#save-alerts").addEventListener("click", () => sendAction({ action: "set-alerts", enabled: $("#alerts-enable").checked, webhook: $("#slack-webhook").value.trim() }));
$("#test-alerts").addEventListener("click", () => sendAction({ action: "test-alerts" }));
$("#delete-alerts").addEventListener("click", () => sendAction({ action: "set-alerts", enabled: false, remove: true }));
for (const id of ['priority', 'slack']) {
  $('#' + id + '-settings-button').addEventListener('click', () => {
    const body = $('#' + id + '-settings-body'); body.hidden = !body.hidden;
    $('#' + id + '-settings-button').setAttribute('aria-expanded', String(!body.hidden));
  });
}
document.querySelectorAll("[data-view]").forEach((button) => button.addEventListener("click", () => {
  document.querySelectorAll("[data-view]").forEach((item) => item.classList.toggle("selected", item === button));
  const view = button.dataset.view;
  $('#history-view').hidden = view !== 'history';
  $('#backups-view').hidden = view !== 'backups';
  $('#upgrades-view').hidden = view !== 'upgrades';
  $('#overview-view').hidden = view !== 'overview';
}));
document.addEventListener("visibilitychange", () => { if (document.hidden) clearTimeout(timer); else load(); });
load();
