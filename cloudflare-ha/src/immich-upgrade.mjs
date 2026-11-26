// Only explicitly reviewed native recipes may be requested by the dashboard.
export const UPGRADE_RECIPES = Object.freeze([
  { version: '3.4.2', fromVersion: '3.4.1', recipe: 'native-3.4.2-r1', schemaChange: false,
    notesUrl: 'https://github.com/immich-app/immich/releases/tag/v3.4.2' },
]);
export const upgradeKind = kind => ['upgrade-stage', 'upgrade-apply', 'upgrade-retry'].includes(kind);
export const upgradeLocked = s => !!s.immichUpgrade?.applying && s.immichUpgrade.phase !== 'completed';

export function upgradePlan(s, input, now, eligible) {
  const source = s.owner, target = source === 'primary' ? 'backup' : 'primary';
  const retry = input.kind === 'upgrade-retry';
  const saved = s.immichUpgrade;
  const version = retry ? saved?.version : input.version;
  const recipe = UPGRADE_RECIPES.find(r => r.version === version);
  if (!recipe) throw new Error('Phiên bản chưa có quy trình nâng cấp được kiểm chứng');
  if (retry && (!upgradeLocked(s) || s.operation?.status !== 'failed' || !s.operation?.upgrade)) throw new Error('Không có nâng cấp lỗi để tiếp tục');
  if (!retry && (!s.ready || !eligible(source, true) || !eligible(target, false))) throw new Error('Cả hai máy phải sẵn sàng trước khi nâng cấp');
  if (retry || input.kind === 'upgrade-apply') {
    if (!saved || saved.source !== source || saved.epoch !== s.epoch || saved.version !== version ||
        (!retry && saved.phase !== 'prepared')) throw new Error('Bản chuẩn bị đã cũ; kiểm tra và chuẩn bị lại');
  } else if (upgradeLocked(s)) throw new Error('Hoàn tất nâng cấp đang tạm dừng trước');
  for (const node of [source, target]) {
    const h = s.heartbeats[node];
    if (!h || now < h.at || now-h.at > 180000 || !h.enforcing || h.metrics?.immichUpgrade?.protocol !== 1 ||
        !Array.isArray(h.metrics.immichUpgrade.recipes) || !h.metrics.immichUpgrade.recipes.includes(recipe.recipe) ||
        !h.metrics.postgresStreaming || (!retry && h.metrics.postgresStreaming.available === false) || s.nodeMaintenance?.[node]?.enabled ||
        !(retry ? [recipe.fromVersion, recipe.version, undefined, null] : [recipe.fromVersion]).includes(h.metrics.appVersion)) {
      throw new Error('Cần agent nâng cấp online, PostgreSQL streaming và phiên bản phù hợp trên cả hai máy');
    }
  }
  const apply = input.kind !== 'upgrade-stage';
  const steps = apply ? [
    {node:target,action:'upgrade-verify',label:'Kiểm tra bản chuẩn bị'},
    {node:source,action:'upgrade-verify',label:'Kiểm tra nguồn active'},
    {node:source,action:'upgrade-freeze',label:'Dừng ghi ứng dụng'},
    {node:source,action:'upgrade-backup',label:'Sao lưu database cuối'},
    {node:target,action:'upgrade-copy-backup',snapshotStep:3,label:'Giữ backup trên máy còn lại'},
    {node:target,action:'upgrade-install',label:'Cài bản dự phòng'},
    {node:source,action:'upgrade-install',label:'Cài bản active'},
    {node:source,action:'upgrade-start',label:'Khởi động bản mới'},
    {action:'upgrade-wait',label:'Chờ active sẵn sàng'},
    {node:source,action:'wal-barrier',label:'Chốt WAL và thư viện'},
    {node:target,action:'sync-wal',snapshotStep:9,label:'Xác nhận WAL và ảnh'},
    {node:target,action:'validate',label:'Kiểm tra dự phòng'},
    {node:target,action:'upgrade-check',label:'Xác nhận phiên bản dự phòng'},
    {node:source,action:'upgrade-check',label:'Xác nhận phiên bản active'},
  ] : [
    {node:source,action:'upgrade-preflight',label:'Kiểm tra active'},
    {node:target,action:'upgrade-preflight',label:'Kiểm tra dự phòng'},
    {node:target,action:'upgrade-build',label:'Chuẩn bị bản dự phòng'},
    {node:source,action:'upgrade-build',label:'Chuẩn bị bản active'},
  ];
  const systemId=saved?.systemId || s.heartbeats[source]?.metrics?.postgresStreaming?.systemId;
  if (apply && !/^\d+$/.test(systemId || '')) throw new Error('Thiếu định danh database trước nâng cấp');
  return {steps, target, upgrade: apply ? {...structuredClone(saved),systemId} : {version, fromVersion:recipe.fromVersion,
    recipe:recipe.recipe,source,epoch:s.epoch,receipts:{},phase:'preparing',applying:false}};
}

export function validateUpgradeResult(job, command, result) {
  if (!command.action.startsWith('upgrade-')) return;
  if (result.version !== job.upgrade.version || result.recipe !== job.upgrade.recipe) throw new Error('Kết quả sai recipe hoặc phiên bản');
  if (command.action === 'upgrade-preflight' && (result.checked !== true || !/^\d+$/.test(result.systemId || '') ||
      (command.index === 1 && result.systemId !== job.results[0]?.systemId))) throw new Error('Hai PostgreSQL không cùng dòng dữ liệu');
  if (command.action === 'upgrade-verify' && result.checked !== true) throw new Error('Bản chuẩn bị chưa được xác nhận');
  if (command.action === 'upgrade-build' && (!/^[a-f0-9]{64}$/.test(result.sha256 || '') || result.stageId !== job.upgrade.stageId)) throw new Error('Bản chuẩn bị thiếu checksum');
  if (command.action === 'upgrade-freeze' && result.appStopped !== true) throw new Error('Chưa xác nhận nguồn dừng ghi');
  if (command.action === 'upgrade-backup' && (!/^[a-f0-9]{64}$/.test(result.sha256 || '') || result.verified !== true ||
      !/^[a-f0-9]{64}$/.test(result.replicaSha256 || '') || !/^immich-db-backup-[A-Za-z0-9.-]+\.sql\.gz$/.test(result.replicaName || ''))) throw new Error('Backup nâng cấp chưa được xác minh');
  if (command.action === 'upgrade-copy-backup' && (result.copied !== true || result.sha256 !== job.results[3]?.replicaSha256)) throw new Error('Máy dự phòng chưa có backup đã xác minh');
  if (command.action === 'upgrade-install' && result.installed !== true) throw new Error('Chưa xác nhận cài phiên bản mới');
  if (command.action === 'upgrade-start' && result.started !== true) throw new Error('Chưa khởi động phiên bản mới');
  if (command.action === 'upgrade-check' && result.checked !== true) throw new Error('Kiểm tra phiên bản thất bại');
}

export function finishUpgrade(s, job) {
  if (!job.upgrade) return;
  if (job.kind === 'upgrade-stage') {
    const receipts = {};
    job.steps.forEach((step, i) => { if (step.action === 'upgrade-build') receipts[step.node] = job.results[i]; });
    s.immichUpgrade = {...job.upgrade,systemId:job.results[0].systemId,receipts,phase:'prepared',applying:false};
  } else s.immichUpgrade = {...job.upgrade,phase:'completed',applying:false};
}
