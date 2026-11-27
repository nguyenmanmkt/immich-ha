// Agent tokens authenticate the reporting machine. Admission itself remains in
// the private mTLS quorum; a nonce round trip reconciles its current writer.
const nodes = ['primary', 'backup'];
const token = /^[a-zA-Z0-9_-]{16,100}$/;
const uuid = /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/;

export function quorumPaused(s) {
  return s.quorumControl?.enabled && s.quorumControl.mode !== 'cloudflare';
}

export function configureQuorum(s, input, now) {
  if (!uuid.test(input.clusterId || '') || typeof input.enabled !== 'boolean') throw new Error('Invalid quorum policy');
  if (s.quorumControl && s.quorumControl.clusterId !== input.clusterId) throw new Error('Quorum identity is immutable');
  if (s.quorumControl?.enabled && !input.enabled) throw new Error('Disabling quorum requires a separate fenced drain');
  s.quorumControl ??= { clusterId: input.clusterId, sequence: 0, mode: 'cloudflare' };
  s.quorumControl.enabled = input.enabled;
  s.quorumControl.configuredAt = now;
}

export function observeQuorum(s, node, input, now) {
  const q = s.quorumControl, r = input.quorumReport;
  if (!q?.enabled || !r || r.clusterId !== q.clusterId || !nodes.includes(r.owner) ||
      !Number.isSafeInteger(r.epoch) || r.epoch < 1 || !token.test(r.session || '') || r.live !== true ||
      !['cloudflare', 'fallback', 'handoff'].includes(r.authority)) return;
  if (r.authority === 'cloudflare') {
    if (q.mode === 'recovering' && node === s.owner && r.owner === node && r.epoch === s.epoch && r.session === s.session && r.localGrant === true && r.cfAck === q.challenge?.nonce && r.term === q.challenge?.term) {
      q.mode = 'cloudflare';q.reconciledAt = now;q.completedTerm = r.term;
    }
    return;
  }
  if (!/^[a-f0-9]{32}$/.test(r.term || '') || (r.term === q.completedTerm && r.epoch <= s.epoch)) return;
  // A peer can pause stale commands, but only the live writer's authenticated
  // agent can complete the challenge and have CF adopt that writer.
  q.mode = q.mode === 'recovering' ? q.mode : 'fallback';
  s.ready = false;
  if (node !== r.owner || r.session !== input.session || r.localGrant !== true) return;
  let c = q.challenge;
  if (!c || c.term !== r.term || c.owner !== r.owner || c.session !== r.session || c.targetEpoch < r.epoch) {
    c = q.challenge = { nonce: crypto.randomUUID().replaceAll('-', ''), owner: r.owner, session: r.session,term:r.term,
      targetEpoch: Math.max(r.epoch, s.epoch + (s.owner !== r.owner || s.session !== r.session || s.epoch > r.epoch ? 1 : 0)) };
  }
  if (r.authority !== 'handoff' || r.handoffNonce !== c.nonce || r.epoch !== c.targetEpoch) return;
  // Idempotent after a lost response. Never advance preference/recovery until
  // the same writer acknowledges a normal CF admission in a later heartbeat.
  if (s.owner !== r.owner || s.epoch !== r.epoch || s.session !== r.session || q.mode !== 'recovering') {
    if (s.operation && !['failed', 'succeeded'].includes(s.operation.status)) {
      s.operation.status = 'failed';s.operation.error = 'Điều phối dự phòng đã nhận quyền; tác vụ cũ dừng an toàn';
      s.operation.maintenance = false;s.operation.command = null;
    }
    s.pending = null;s.failureSince = null;
    s.owner = r.owner;s.epoch = r.epoch;s.session = r.session;s.ready = false;
    s.leaseUntil = now + 240_000;
    q.mode = 'recovering';q.adoptedAt = now;
    s.audit ??= [];s.audit.push({at:now, actor:'quorum', action:'quorum-reconciled', details:`${r.owner} · epoch ${r.epoch}`});
    s.audit = s.audit.slice(-100);
  }
}

export function quorumReply(s, node) {
  const q = s.quorumControl;
  if (!q?.enabled) return null;
  q.sequence = (q.sequence || 0) + 1;
  const challenge = q.challenge?.owner === node ? q.challenge : null;
  return { clusterId:q.clusterId, protocol:2, mode:q.mode, sequence:q.sequence,
    auto:s.autoRequested ?? s.auto,
    blocked:!!s.pending || !!s.operation?.maintenance || !!s.immichUpgrade?.applying ||
      !!s.operation && !['failed','succeeded'].includes(s.operation.status),
    excluded:Object.fromEntries(nodes.map(n=>[n,!!s.nodeMaintenance?.[n]?.enabled])),
    challenge:quorumPaused(s) ? challenge : null,
    ack:q.mode === 'recovering' && challenge ? challenge.nonce : null };
}
