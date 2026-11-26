// Durable, serialized planned operations. Commands are fixed actions, never shell text.
import { upgradeKind, upgradeLocked, upgradePlan, validateUpgradeResult, finishUpgrade } from './immich-upgrade.mjs';
export const NODE_IDS = ["primary", "backup"];
export const otherNode = (node) => node === "primary" ? "backup" : "primary";
const terminal = new Set(["succeeded", "failed"]);

export function upgradeOperations(state) {
  state.operation ??= null;
  state.history ??= [];
  state.audit ??= [];
  return state;
}

export function busy(state) {
  return !!state.operation && !terminal.has(state.operation.status);
}

function event(job, now, message, level = "info", node = null) {
  job.events.push({ at: now, message, level, node });
  if (job.events.length > 100) job.events.shift();
  job.updatedAt = now;
}

export function audit(state, now, actor, action, details = "") {
  upgradeOperations(state);
  state.audit.push({ at: now, actor: String(actor).slice(0, 150), action, details: String(details).slice(0, 300) });
  state.audit = state.audit.slice(-100);
}

function prepareSteps(source, target) {
  return [
    { node: target, action: "archive", label: "Giữ bản khôi phục trên máy đích" },
    { node: target, action: "receive", label: "Chuẩn bị chiều nhận dữ liệu" },
    { node: source, action: "send", label: "Thiết lập nguồn đồng bộ chính xác" },
    { node: source, action: "dump", label: "Tạo bản sao database và danh mục tệp" },
    { node: target, action: "sync-restore", snapshotStep: 3, label: "Đồng bộ và khôi phục trên máy đích" },
    { node: target, action: "validate", label: "Kiểm tra Immich máy đích" },
  ];
}

export function startOperation(state, input, now, id, eligible) {
  upgradeOperations(state);
  if (input.requestId && [state.operation, ...state.history].some((job) => job && job.requestId === input.requestId && job.actor === input.actor)) return state.operation;
  if (busy(state) || state.pending) throw new Error("Đang có một thao tác khác. Hãy chờ thao tác đó hoàn tất.");
  const upgrading = upgradeKind(input.kind);
  if (upgradeLocked(state) && input.kind !== 'upgrade-retry') throw new Error('Cần tiếp tục nâng cấp đang tạm dừng; không bàn giao hoặc mở lại code cũ');
  if (!["prepare", "switch", "resume"].includes(input.kind) && !upgrading) throw new Error("Thao tác không hợp lệ");
  const source = state.owner;
  const plan = upgrading ? upgradePlan(state, input, now, eligible) : null;
  const target = plan?.target || input.target;
  if (input.kind !== "resume" && (!NODE_IDS.includes(target) || target === source)) {
    throw new Error("Máy đích phải khác máy đang active");
  }
  if (input.kind !== "resume" && state.nodeMaintenance?.[target]?.enabled) throw new Error("Máy đích đang bảo trì; kết thúc bảo trì trước khi đồng bộ hoặc bàn giao");
  if (input.kind === "resume") {
    const failed = state.operation;
    if (!failed || failed.status !== "failed" || !failed.maintenance ||
        failed.source !== source || failed.sourceEpoch !== state.epoch) {
      throw new Error("Không có nguồn đang tạm dừng đủ điều kiện khôi phục");
    }
  }
  const required = input.kind === "resume" ? [source] : [source, target];
  for (const node of required) {
    const h = state.heartbeats[node];
    if (!h || now - h.at > 180_000 || h.protocolVersion < 2 || !h.enforcing || !h.metrics?.runtimeV2) {
      throw new Error("Cần backend v2 đang online và enforcement trên cả hai máy");
    }
  }
  if (input.kind !== "resume" && input.kind !== 'upgrade-retry' && !eligible(source, true)) {
    throw new Error("Máy active chưa sẵn sàng để bắt đầu đồng bộ có kiểm soát");
  }
  if (input.kind !== "resume" && !upgrading) {
    const a = state.heartbeats[source].metrics;
    const b = state.heartbeats[target].metrics;
    if (!!a.postgresStreaming !== !!b.postgresStreaming) {
      throw new Error("Hai máy đang khác chế độ PostgreSQL; hoàn tất nâng cấp trước khi bàn giao");
    }
    if (a.postgresStreaming && (a.postgresStreaming.handoverVersion < 2 || b.postgresStreaming?.handoverVersion < 2 ||
        !a.postgresStreaming.handoverVersion || !b.postgresStreaming?.handoverVersion)) {
      throw new Error("Cần cập nhật bàn giao WAL trên cả hai máy trước khi tiếp tục");
    }
    if (!a.appVersion || a.appVersion !== b.appVersion || !a.postgresVersion ||
        a.postgresVersion.split(".")[0] !== b.postgresVersion?.split(".")[0]) {
      throw new Error("Phiên bản Immich hoặc PostgreSQL hai máy chưa tương thích");
    }
  }
  if (state.operation) state.history.unshift(structuredClone(state.operation));
  state.history = state.history.slice(0, 12);
  const steps = plan?.steps || (input.kind === "resume" ? [
    { node: source, action: "resume", label: "Khôi phục phục vụ trên nguồn hiện tại" },
    { action: "wait-owner", label: "Chờ xác nhận phục vụ" },
  ] : prepareSteps(source, target));
  if (input.kind === "switch") steps.push(
    { node: source, action: "freeze-dump", label: "Tạm dừng ghi và chốt dữ liệu cuối" },
    { node: source, action: "wait-peer", snapshotStep: 6, label: "Chờ máy đích nhận đầy đủ bản cuối" },
    { node: target, action: "sync-restore", snapshotStep: 6, final: true, label: "Nạp database cuối lên máy đích" },
    { node: target, action: "validate", label: "Xác nhận máy đích sẵn sàng" },
    { action: "transfer", label: "Bàn giao quyền phục vụ" },
    { action: "wait-owner", label: "Chờ máy active mới phục vụ" },
    { node: source, action: "archive", label: "Giữ bản khôi phục của máy cũ" },
    { node: source, action: "receive", label: "Đưa máy cũ về chiều nhận" },
    { node: target, action: "send", label: "Đồng bộ ngược từ máy active mới" },
    { node: target, action: "dump", label: "Sao lưu database của máy active mới" },
    { node: source, action: "sync-restore", snapshotStep: 15, label: "Khôi phục máy cũ thành dự phòng" },
    { node: source, action: "validate", label: "Kiểm tra máy dự phòng mới" },
  );
  if (state.heartbeats[source]?.metrics?.postgresStreaming) {
    for (const step of steps) {
      if (step.action === "dump") { step.action = "wal-barrier"; step.label = "Chốt mốc WAL và danh mục file"; }
      if (step.action === "freeze-dump") { step.action = "freeze-wal"; step.label = "Dừng ghi và xác nhận WAL cuối"; }
      if (step.action === "sync-restore") { step.action = "sync-wal"; step.label = step.final ? "Xác nhận WAL cuối và thư viện" : "Đồng bộ WAL và thư viện"; }
      if (step.action === "validate") step.label = "Kiểm tra PostgreSQL dự phòng";
    }
  }
  const job = {
    id, kind: input.kind, source, target: input.kind === "resume" ? otherNode(source) : target,
    sourceEpoch: state.epoch, status: "running", maintenance: input.kind === "resume",
    requestId: input.requestId || null,
    previousAuto: state.auto, createdAt: now, updatedAt: now, actor: input.actor || "operator",
    index: 0, steps, results: {}, events: [], command: null,
    autoRecovery: input.autoRecovery === true,
    autoFailback: input.autoFailback === true,
    streaming: !!state.heartbeats[source]?.metrics?.postgresStreaming,
    handoverVersion: state.heartbeats[source]?.metrics?.postgresStreaming ? 2 : 1,
    fastHandover: input.kind === 'switch' && [source,target].every(node => state.heartbeats[node]?.metrics?.postgresStreaming?.fastHandoverVersion === 1),
  };
  if (plan) {
    job.upgrade = {...plan.upgrade,stageId:plan.upgrade.stageId || id};
    if (input.kind !== 'upgrade-stage') {
      state.immichUpgrade = {...job.upgrade,phase:'applying',applying:true};
      job.maintenance = input.kind === 'upgrade-retry';
    }
  }
  state.autoRequested ??= state.auto;
  state.auto = false;
  state.operation = job;
  event(job, now, "Đã bắt đầu. Tự chuyển máy được tạm ngưng trong thao tác này.");
  audit(state, now, job.actor, input.kind, `${source} → ${job.target}`);
  return job;
}

function finish(state, now, eligible) {
  const job = state.operation;
  job.status = "succeeded";
  job.maintenance = false;
  job.command = null;
  job.finishedAt = now;
  finishUpgrade(state, job);
  state.auto = (state.autoRequested ?? job.previousAuto) && eligible(otherNode(state.owner), false);
  event(job, now, "Hoàn tất. Vai trò và chiều đồng bộ đã được xác nhận.", "success");
}

export function failOperation(state, now, message) {
  const job = state.operation;
  if (!job || terminal.has(job.status)) return;
  job.status = "failed";
  job.error = String(message).slice(0, 1000);
  job.finishedAt = now;
  state.auto = false;
  // The first two apply commands only verify staging. If either fails, no
  // freeze/install has been issued: allow a new preparation, not an unsafe
  // resume/downgrade path. Once freeze is issued, the durable lock remains.
  if (job.upgrade && job.kind !== 'upgrade-stage' && job.index < 2 && !job.maintenance) {
    state.immichUpgrade = {...job.upgrade,phase:'stale',applying:false};
  }
  if (state.pending?.jobId === job.id && state.owner === job.source && state.epoch === job.sourceEpoch) state.pending = null;
  // Never reopen the old source automatically after a failed frozen handover.
  event(job, now, job.error, "error");
}

export function advanceOperation(state, now, eligible) {
  upgradeOperations(state);
  const job = state.operation;
  if (!job || terminal.has(job.status)) return;
  if (now - job.updatedAt > 45 * 60_000) {
    failOperation(state, now, "Quá thời gian chờ phản hồi. Nguồn đang phục vụ không bị đổi tự động.");
    return;
  }
  for (;;) {
    const step = job.steps[job.index];
    if (!step) { finish(state, now, eligible); return; }
    if (step.node) {
      if (!job.command) {
        if (["freeze-dump", "freeze-wal", "upgrade-freeze"].includes(step.action)) job.maintenance = true;
        job.status = "running";
        job.command = {
          id: `${job.id}-${job.index}`, jobId: job.id, index: job.index, node: step.node,
          action: step.action, createdAt: now, source: state.owner, epoch: state.epoch,
          peer: otherNode(step.node),
          peerDeviceId: state.heartbeats[otherNode(step.node)]?.metrics?.syncthing?.deviceId || null,
          snapshot: step.snapshotStep === undefined ? null : job.results[step.snapshotStep],
          final: step.final === true,
          autoRecovery: job.autoRecovery === true,
          ...(job.upgrade ? {upgrade:job.upgrade} : {}),
          ...(job.upgrade && job.kind !== 'upgrade-stage' ? {upgradeBackup:job.results[3] || null} : {}),
          ...(job.upgrade && job.kind !== 'upgrade-stage' ? {upgradeBackupCopy:job.results[4] || null} : {}),
        };
        job.progress = null;
        event(job, now, step.label, "info", step.node);
      }
      return;
    }
    if (step.action === 'upgrade-wait') {
      if (state.owner !== job.source || state.epoch !== job.sourceEpoch) { failOperation(state, now, 'Quyền active thay đổi khi nâng cấp'); return; }
      if (state.ready && eligible(job.source, true) && state.heartbeats[job.source]?.metrics?.appVersion === job.upgrade.version) {
        job.index++;job.command=null;continue;
      }
      job.status='waiting-ready';return;
    }
    if (step.action === "transfer") {
      if (state.owner === job.target && state.epoch > job.sourceEpoch) {
        job.index++; job.command = null; continue;
      }
      if (state.owner !== job.source || state.epoch !== job.sourceEpoch) {
        failOperation(state, now, "Quyền phục vụ đã thay đổi ngoài thao tác này"); return;
      }
      if (!state.pending) {
        const frozen = job.results[6];
        if (!frozen?.stopped || frozen.epoch !== state.epoch) {
          failOperation(state, now, "Chưa có xác nhận dừng ghi từ nguồn"); return;
        }
        if (job.handoverVersion === 2 && (!frozen.peerAcknowledged || !job.results[8]?.manifestVerified)) {
          failOperation(state, now, "Chưa xác nhận WAL cuối và file trên máy đích"); return;
        }
        state.pending = { target: job.target, requestedAt: now, jobId: job.id, reason: job.autoFailback ? "automatic-return" : "planned" };
        state.leaseUntil = Math.min(state.leaseUntil, now);
        state.ready = false;
        event(job, now, job.fastHandover ? "Nguồn đã dừng; đang xác nhận điều kiện bàn giao nhanh." : "Nguồn đã dừng ghi; đang chờ khoảng đệm bàn giao quyền.");
      }
      job.status = "waiting-lease";
      return;
    }
    if (step.action === "wait-owner") {
      const expected = job.kind === "resume" ? job.source : job.target;
      if (state.owner === expected && state.ready && eligible(expected, true)) {
        job.maintenance = false;
        job.index++; job.command = null; continue;
      }
      job.status = "waiting-ready";
      return;
    }
  }
}

export function operationCommand(state, node) {
  return busy(state) && state.operation.command?.node === node ? state.operation.command : null;
}

export function canFastTransfer(state, now) {
  const job = state.operation, pending = state.pending;
  if (!pending || !job || job.kind !== 'switch' || job.status !== 'waiting-lease' || !job.fastHandover ||
      job.handoverVersion !== 2 || job.steps[job.index]?.action !== 'transfer' ||
      pending.jobId !== job.id || pending.target !== job.target || state.owner !== job.source || state.epoch !== job.sourceEpoch) return false;
  const frozen = job.results[6], received = job.results[8], checked = job.results[9];
  if (!frozen || frozen.kind !== 'wal-manifest-v1' || frozen.commandId !== `${job.id}-6` || frozen.jobId !== job.id ||
      frozen.source !== job.source || frozen.epoch !== state.epoch || frozen.stopped !== true || frozen.peerAcknowledged !== true ||
      frozen.fenceProtocol !== 1 || frozen.appStopped !== true || frozen.postgresStopped !== true ||
      frozen.originFenced !== true || frozen.frozenDurable !== true) return false;
  const source = state.heartbeats[job.source];
  const receiptAt = job.resultTimes?.[6];
  if (!Number.isFinite(receiptAt) || !source || source.at < receiptAt || now < source.at || now-source.at > 15000 ||
      !source.enforcing || source.localHealthy || source.metrics?.appMode !== 'frozen' ||
      source.metrics?.postgresStreaming?.fastHandoverVersion !== 1) return false;
  if (!received?.manifestVerified || received.systemId !== frozen.systemId || received.timeline !== frozen.timeline ||
      received.filesVerified !== frozen.files || checked?.healthy !== true || checked.seedEpoch !== state.epoch) return false;
  const lsn = x => /^[0-9A-F]+\/[0-9A-F]+$/.test(x || '') ? BigInt(`0x${x.split('/')[0]}`)*0x100000000n+BigInt(`0x${x.split('/')[1]}`) : -1n;
  return lsn(frozen.walLsn) >= 0n && lsn(received.replayLsn) >= lsn(frozen.walLsn);
}

export function operationResult(state, node, input, now, eligible) {
  upgradeOperations(state);
  const job = state.operation;
  if (!job || job.id !== input.jobId) throw new Error("Thao tác không còn hiện hành");
  if (job.results[input.index] && job.steps[input.index]?.node === node && input.commandId === `${job.id}-${input.index}`) {
    return; // Idempotent acknowledgement after a lost HTTP response.
  }
  const command = job.command;
  if (!busy(state) || !command || command.node !== node || command.id !== input.commandId || command.index !== input.index) {
    throw new Error("Kết quả không khớp lệnh đang chờ");
  }
  if (input.status !== "succeeded") {
    failOperation(state, now, input.error || "Backend không hoàn tất được thao tác"); return;
  }
  const result = input.result || {};
  validateUpgradeResult(job, command, result);
  if (["wal-barrier", "freeze-wal"].includes(command.action)) {
    if (result.kind !== "wal-manifest-v1" || result.commandId !== command.id || result.jobId !== command.jobId ||
        result.source !== command.source || result.epoch !== command.epoch ||
        result.manifestName !== `wal-${command.id}.manifest.json.gz` || !/^[a-f0-9]{64}$/.test(result.manifestSha256 || "") ||
        !/^[0-9A-F]+\/[0-9A-F]+$/.test(result.walLsn || "") || !/^\d+$/.test(result.systemId || "") ||
        !Number.isSafeInteger(result.timeline) || result.timeline < 1 ||
        !Number.isSafeInteger(result.files) || result.files < 0 || !Number.isSafeInteger(result.bytes) || result.bytes < 0 ||
        (command.action === "freeze-wal" && (result.stopped !== true || result.peerAcknowledged !== true))) {
      throw new Error("Mốc WAL hoặc danh mục file bàn giao không hợp lệ");
    }
  }
  if (command.action === "sync-wal") {
    const ticket = command.snapshot;
    const lsn = (x) => /^[0-9A-F]+\/[0-9A-F]+$/.test(x || "") ? BigInt(`0x${x.split('/')[0]}`) * 0x100000000n + BigInt(`0x${x.split('/')[1]}`) : -1n;
    if (!ticket || result.manifestVerified !== true || result.systemId !== ticket.systemId ||
        result.timeline !== ticket.timeline || result.filesVerified !== ticket.files || lsn(result.replayLsn) < lsn(ticket.walLsn)) {
      throw new Error("Máy đích chưa xác nhận đủ WAL và file bàn giao");
    }
  }
  if (["dump", "freeze-dump"].includes(command.action) &&
      (!/^immich-\d{8}-\d{6}\.dump$/.test(result.name || "") || !/^[a-f0-9]{64}$/.test(result.sha256 || "") ||
       !/^[a-f0-9]{64}$/.test(result.manifestSha256 || "") ||
       result.manifestName !== result.name + ".manifest.json.gz" ||
       result.source !== command.source || result.epoch !== command.epoch ||
       (command.action === "freeze-dump" && result.stopped !== true))) {
    throw new Error("Thông tin bản sao không hợp lệ");
  }
  if (job.streaming && ["dump", "freeze-dump"].includes(command.action) &&
      (!/^[0-9A-F]+\/[0-9A-F]+$/.test(result.walLsn || "") || !/^\d+$/.test(result.systemId || ""))) {
    throw new Error("Thiếu xác nhận WAL của PostgreSQL nguồn");
  }
  job.results[job.index] = result;
  job.resultTimes ??= {};
  job.resultTimes[job.index] = now;
  event(job, now, "Hoàn tất: " + job.steps[job.index].label, "success", node);
  job.index++; job.command = null;
  advanceOperation(state, now, eligible);
}

export function progressOperation(state, node, input, now) {
  const job = state.operation;
  if (busy(state) && job.command?.node === node && job.command.id === input.commandId) {
    job.updatedAt = now;
    job.progress = { node, at: now, message: String(input.message || "").slice(0, 500) };
  }
}
