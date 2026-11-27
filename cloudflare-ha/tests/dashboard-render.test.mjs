// Render smoke checks without a browser; selectors must match the shipped HTML.
import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import { readFileSync } from 'node:fs';
import * as model from '../web/status-model.mjs';

class Element {
  constructor(tag = 'div', text = '') {
    this.tagName = tag; this.textContent = text; this.children = []; this.dataset = {};
    this.style = { setProperty() {} }; this.events = {}; this.className = '';
    this.classList = { add: (s) => { this.className += ' ' + s; }, toggle() {} };
  }
  append(...items) { this.children.push(...items); for (const x of items) x.parentElement = this; }
  replaceChildren(...items) { this.children = []; this.append(...items); }
  setAttribute(k, v) { this[k] = v; }
  addEventListener(k, f) { this.events[k] = f; }
  showModal() { this.open = true; }
  focus() {}
}
function setup(state) {
  const html = readFileSync(new URL('../web/index.html', import.meta.url), 'utf8');
  const elements = new Map([...html.matchAll(/id="([^"]+)"/g)].map((m) => ['#' + m[1], new Element()]));
  elements.get('#account-label').parentElement = new Element();
  const controls = [new Element(), new Element(), new Element()];
  controls.forEach((c, i) => { c.dataset.view = ['overview', 'backups', 'history'][i]; });
  const document = {
    hidden: false,
    querySelector: (s) => { assert.ok(elements.has(s), `Missing HTML selector ${s}`); return elements.get(s); },
    querySelectorAll: (s) => {
      if (s === '[data-view]') return controls;
      const key = { '[data-prepare-node]': 'prepareNode', '[data-maintenance-node]': 'maintenanceNode', '[data-switch-node]': 'switchNode', '[data-resume-node]': 'resumeNode', '[data-node-settings]': 'nodeSettings' }[s];
      if (!key) return [];
      const found = new Set();
      function collect(x) { if (x.dataset[key]) found.add(x); for (const c of x.children) collect(c); }
      for (const x of elements.values()) collect(x);
      return [...found];
    },
    createElement: (tag) => new Element(tag), createElementNS: (_, tag) => new Element(tag),
    createTextNode: (s) => new Element('text', s), addEventListener() {},
  };
  const context = vm.createContext({ document, ...model, __state: state, URL, Intl, Date, console,
    setTimeout() {}, clearTimeout() {}, crypto: { randomUUID: () => 'preview-id' } });
  const js = readFileSync(new URL('../web/admin.js', import.meta.url), 'utf8')
    .replace(/^import .*;\r?\n/m, '').replace(/load\(\);\s*$/, '');
  vm.runInContext(js + '\nstate = __state; render();', context);
  return { elements, context };
}
function fixture() {
  const now = Date.now();
  const s = { serverTime: now, owner: 'primary', ready: true, leaseUntil: now + 200000,
    primaryHealthy: true, backupHealthy: true, standbyNode: 'backup', standbyEligible: true,
    auto: true, nodes: { primary: { label: 'Proxmox3', containerId: 113 }, backup: { label: 'Asus PVE', containerId: 111 } },
    agents: {}, recovery: { enabled: true, status: 'ready', source: 'primary', target: 'backup' },
    preference: { node: 'primary', enabled: true, status: 'on-preferred' }, actorEmail: 'operator@example.com' };
  for (const n of ['primary', 'backup']) s.agents[n] = { at: now - 1000, enforcing: true, protocolVersion: 2, localHealthy: true,
    metrics: { runtimeV2: true, maintenanceControl: true, appMode: n === 'primary' ? 'active' : 'standby',
      disk: { total: 1000 * 1024**3, used: 100 * 1024**3, free: 900 * 1024**3 },
      syncthing: { folders: { 'immich-library': { paused: false, needItems: 0 }, 'immich-database': { paused: false } } } } };
  return s;
}
function find(root, predicate) {
  if (predicate(root)) return root;
  for (const c of root.children) { const found = find(c, predicate); if (found) return found; }
}

test('Funnel monitor exposes real route and HTTP latency and marks old readings stale', () => {
  const s=fixture();
  assert.equal(setup(s).elements.get('#funnel-panel').hidden,true);
  s.funnel={url:'https://witness.example.ts.net',checkedAt:s.serverTime,state:'healthy',ok:true,node:'primary',epoch:15,latencyMs:125};
  let elements=setup(s).elements;
  assert.equal(elements.get('#funnel-panel').hidden,false);
  assert.equal(elements.get('#funnel-status').textContent,'Hoạt động');
  assert.equal(elements.get('#funnel-route').textContent,'Proxmox3 · 125 ms');
  s.funnel.checkedAt-=181000;
  elements=setup(s).elements;
  assert.equal(elements.get('#funnel-status').textContent,'Kết quả cũ');
  assert.ok(!elements.get('#funnel-status').className.includes('green'));
});

test('LAN streaming renders a LAN connection tile and setup progress stays separate from HA operations',()=>{
  const s=fixture();for(const n of ['primary','backup']){s.agents[n].metrics.postgresStreaming={networkMode:'lan',available:true};s.agents[n].metrics.syncthing.peerConnected=true;}
  s.setup={status:'running',index:4,total:27,label:'Ghép Syncthing A',joined:true};
  const {elements}=setup(s);
  assert.equal(elements.get('#setup-panel').hidden,false);
  assert.equal(elements.get('#setup-label').textContent,'Ghép Syncthing A');
  assert.ok(find(elements.get('#servers'),e=>e.textContent==='Kết nối LAN'));
});

test('Funnel settings list common and node addresses and require witness confirmation for off state',()=>{
  const s=fixture();s.funnel={url:'https://witness.example.ts.net',tailscaleIp:'100.64.0.3',checkedAt:s.serverTime,state:'healthy',ok:true,node:'primary',control:{enabled:false,confirmed:false,reportedAt:s.serverTime}};
  s.nodes.primary.funnelUrl='https://a.example.ts.net';s.nodes.primary.tailscaleIp='100.64.0.1';
  s.nodes.backup.funnelUrl='https://b.example.ts.net';s.nodes.backup.tailscaleIp='100.64.0.2';
  let elements=setup(s).elements;
  assert.equal(elements.get('#funnel-toggle').checked,false);
  assert.equal(elements.get('#funnel-status').textContent,'Đang tắt…');
  assert.equal(elements.get('#funnel-addresses').children.length,3);
  assert.ok(find(elements.get('#funnel-addresses'),e=>e.href==='https://b.example.ts.net/'));
  assert.ok(find(elements.get('#funnel-addresses'),e=>e.textContent==='100.64.0.3'));
  elements.get('#funnel-settings-body').hidden=true;
  elements.get('#funnel-settings-button').events.click();
  assert.equal(elements.get('#funnel-settings-body').hidden,false);
  s.funnel.control.confirmed=true;
  elements=setup(s).elements;
  assert.equal(elements.get('#funnel-status').textContent,'Đã tắt');
});

test('Funnel replaces the failover tile and return remains a separate switch', () => {
  const s=fixture();s.preference.enabled=false;
  const { elements }=setup(s);
  assert.equal(elements.has('#auto-toggle'),false);
  assert.equal(elements.has('#auto-state'),false);
  assert.equal(elements.get('#return-toggle').checked,false);
  const html=readFileSync(new URL('../web/index.html',import.meta.url),'utf8');
  assert.ok(html.includes('<strong>Tự trở về máy ưu tiên</strong>'));
  assert.ok(html.indexOf('id="funnel-panel"') > html.indexOf('class="mushroom-controls quick-controls"'));
});

test('route mismatch describes observations without claiming a transfer is underway', () => {
  const s=fixture();s.funnel={url:'https://witness.example.ts.net',checkedAt:s.serverTime,state:'route-mismatch',ok:true,node:'backup',epoch:14};
  assert.equal(setup(s).elements.get('#funnel-status').textContent,'Chưa khớp active');
});

test('one bottom progress card prioritizes live takeover over completed jobs', () => {
  const html=readFileSync(new URL('../web/index.html',import.meta.url),'utf8');
  assert.equal([...html.matchAll(/id="transition-panel"/g)].length,1);
  assert.ok(html.indexOf('id="progress-panel"') > html.indexOf('id="servers"'));
  assert.ok(html.indexOf('id="transition-panel"') > html.indexOf('id="progress-panel"'));
  for (const phase of ['confirming','activating']) {
    const s=fixture();s.ready=false;s.primaryHealthy=false;s.agents.primary.localHealthy=false;
    s.failureSince=s.serverTime-50000;s.failureConfirmAt=s.serverTime+108000;
    if(phase==='activating')s.lastTakeover={source:'backup',target:'primary',reason:'automatic',grantedAt:s.serverTime-1000,readyAt:null};
    const {elements}=setup(s);
    assert.equal(elements.get('#transition-panel').hidden,false);
    assert.equal(elements.get('#job-panel').hidden,true);
    assert.equal(elements.get('#system-summary').hidden,true);
    assert.equal(elements.get('#connection-banner').hidden,true);
    assert.match(elements.get('#transition-path').textContent,/TIẾP QUẢN/);
  }
  const {elements}=setup(fixture());
  assert.equal(elements.get('#transition-panel').hidden,true);
  assert.equal(elements.get('#job-panel').hidden,false);
});

test('running failback stays visible across readiness flaps and late takeover acknowledgements', () => {
  const s=fixture();
  s.lastTakeover={source:'backup',target:'primary',reason:'automatic',grantedAt:s.serverTime-60000,readyAt:s.serverTime-50000};
  s.operation={id:'stable-job',kind:'switch',autoFailback:true,status:'running',source:'primary',target:'backup',createdAt:s.serverTime-30000,index:0,steps:[{action:'sync-wal',final:true,label:'Kiểm tra WAL cuối'}]};
  for (const ready of [true,false,true,false]) {
    s.ready=ready;s.primaryHealthy=ready;
    const {elements}=setup(s);
    assert.equal(elements.get('#transition-panel').hidden,true);
    assert.equal(elements.get('#job-panel').hidden,false);
    assert.match(elements.get('#job-title').textContent,/Tự trở về máy ưu tiên/);
    assert.ok(find(elements.get('#job-detail'),x=>x.textContent==='WAL cuối'));
  }
  s.operation.status='succeeded';s.operation.finishedAt=s.serverTime-1000;
  s.lastTakeover.readyAt=s.serverTime;s.ready=false;
  const {elements}=setup(s);
  assert.equal(elements.get('#job-panel').hidden,false);
  assert.equal(elements.get('#transition-panel').hidden,true);
});

test('streaming tile uses WAL status rather than an old dump restore timestamp', () => {
  const s=fixture();
  s.agents.primary.metrics.postgresStreaming={inRecovery:false};
  s.agents.backup.metrics.postgresStreaming={inRecovery:true,receiverConnected:true,pendingBytes:0};
  let cards=setup(s).elements.get('#servers').children;
  assert.ok(find(cards[0],x=>x.textContent==='Nguồn dữ liệu'));
  assert.ok(find(cards[1],x=>x.textContent==='Đang nhận WAL'));
  assert.equal(find(cards[1],x=>x.textContent==='Database đã nạp'),undefined);
  s.agents.backup.metrics.postgresStreaming.receiverConnected=false;
  cards=setup(s).elements.get('#servers').children;
  assert.ok(find(cards[1],x=>x.textContent==='Mất kết nối nguồn'));
});
test('dashboard renders real selectors, compact cards and protected maintenance controls', () => {
  const { elements, context } = setup(fixture());
  assert.equal(elements.get('#system-summary').hidden, true);
  assert.equal(find(elements.get('#servers').children[0], (x) => x.className.includes('server-state')).textContent, 'Đang phục vụ');
  assert.equal(elements.get('#recovery-label').hidden, true);
  const cards = elements.get('#servers').children;
  assert.equal(cards.length, 2);
  assert.equal(find(cards[0], (x) => !!x.dataset.switchNode), undefined);
  const transfer = find(cards[1], (x) => x.dataset.switchNode === 'backup');
  assert.equal(transfer.disabled, false);
  assert.equal(transfer.parentElement.parentElement.tagName, 'details');
  assert.equal(transfer.parentElement.parentElement.open, false);
  const prepare = find(cards[1], (x) => x.dataset.prepareNode === 'backup');
  assert.equal(prepare.parentElement, transfer.parentElement);
  assert.equal(find(cards[0], (x) => !!x.dataset.prepareNode), undefined);
  assert.equal(find(cards[1], (x) => x.dataset.prepareNode === 'backup').disabled, false);
  assert.equal(find(cards[0], (x) => x.dataset.maintenanceNode === 'primary').disabled, true);
  assert.equal(find(cards[1], (x) => x.dataset.maintenanceNode === 'backup').disabled, false);
  assert.equal(find(cards[0], (x) => x.tagName === 'details').open, false);
  assert.equal(find(cards[0], (x) => x.className.includes('disk-ring')).children[0].textContent, '10%');
  vm.runInContext("confirmAction('set-maintenance','backup')", context);
  assert.equal(elements.get('#confirm-dialog').open, true);
  assert.equal(elements.get('#confirm-submit').disabled, true);
});
test('pending and confirmed maintenance have distinct titles and cancellation controls', () => {
  const s = fixture(); s.standbyEligible = false; s.nodeMaintenance = { backup: { enabled: true } };
  let result = setup(s);
  assert.equal(result.elements.get('#system-summary').hidden, true);
  assert.equal(find(result.elements.get('#servers').children[1], (x) => x.className.includes('server-state')).textContent, 'Chờ xác nhận bảo trì');
  assert.equal(find(find(result.elements.get('#servers').children[1], (x) => x.dataset.maintenanceNode === 'backup'), (x) => x.className === 'button-caption').textContent, 'Hủy yêu cầu bảo trì');
  Object.assign(s.agents.backup, { localHealthy: false }); s.backupHealthy = false;
  s.agents.backup.metrics.appMode = 'maintenance';
  for (const f of Object.values(s.agents.backup.metrics.syncthing.folders)) f.paused = true;
  result = setup(s);
  assert.equal(result.elements.get('#system-summary').hidden, true);
  assert.equal(find(result.elements.get('#servers').children[1], (x) => x.className.includes('server-state')).textContent, 'Đã vào chế độ bảo trì');
  assert.equal(find(find(result.elements.get('#servers').children[1], (x) => x.dataset.maintenanceNode === 'backup'), (x) => x.className === 'button-caption').textContent, 'Kết thúc bảo trì');
});
test('stale serving heartbeat cannot show a green system summary', () => {
  const s = fixture(); s.agents.primary.at -= 300000;
  const { elements } = setup(s);
  assert.equal(elements.get('#system-summary').hidden, false);
  assert.equal(elements.get('#system-title').textContent, 'Phục vụ đang gián đoạn');
  assert.match(elements.get('#system-summary').className, /error/);
});
test('steps stay visible with icons for completed, failed and running work', () => {
  for (const status of ['succeeded', 'failed', 'running']) {
    const s = fixture(); s.operation = { kind: 'prepare', source: 'primary', target: 'backup', status, index: 1,
      steps: [{ label: 'Đồng bộ', node: 'backup' }, { label: 'Kiểm tra', node: 'backup' }] };
    const { elements } = setup(s);
    const details = find(elements.get('#job-detail'), (x) => x.tagName === 'details');
    assert.equal(details, undefined);
    const timeline = find(elements.get('#job-detail'), (x) => x.className === 'timeline');
    assert.equal(timeline.children.length, 2);
    assert.ok(find(timeline.children[0], (x) => x.className === 'step-marker'));
    assert.equal(elements.get('#job-route').textContent, 'Proxmox3 → Asus PVE');
  }
});

test('manual refresh follows the standby role and is disabled during maintenance', () => {
  const s = fixture(); s.owner = 'backup';
  let cards = setup(s).elements.get('#servers').children;
  assert.equal(find(cards[1], (x) => !!x.dataset.prepareNode), undefined);
  assert.equal(find(cards[0], (x) => x.dataset.prepareNode === 'primary').disabled, false);
  s.nodeMaintenance = { primary: { enabled: true } };
  cards = setup(s).elements.get('#servers').children;
  assert.equal(find(cards[0], (x) => x.dataset.prepareNode === 'primary').disabled, true);
});

test('Immich counts and live Tailscale probes render without treating stale probes as connected', () => {
  const s = fixture(), m = s.agents.primary.metrics;
  m.immich = { state: 'ok', photos: 13979, videos: 1596, checkedAt: s.serverTime };
  m.tailscale = { state: 'direct', latencyMs: 1.25, checkedAt: s.serverTime };
  let card = setup(s).elements.get('#servers').children[0];
  assert.ok(find(card, (x) => x.textContent === '13.979 ảnh · 1.596 video'));
  assert.ok(find(card, (x) => x.textContent === 'Trực tiếp · 1,3 ms'));
  m.tailscale.checkedAt -= 300000;
  card = setup(s).elements.get('#servers').children[0];
  assert.equal(find(card, (x) => x.textContent === 'Trực tiếp · 1,3 ms'), undefined);
  assert.ok(find(card, (x) => x.textContent === 'Chưa xác nhận'));
});

test('CPU and RAM tiles appear on both physical node identities', () => {
  const s = fixture();
  for (const n of ['primary', 'backup']) {
    s.agents[n].metrics.cpu = { cores: n === 'primary' ? 6 : 4, percent: 12.5 };
    s.agents[n].metrics.memory = { used: 2 * 1024**3, total: 6 * 1024**3 };
  }
  const cards = setup(s).elements.get('#servers').children;
  assert.ok(find(cards[0], (x) => x.textContent === '12,5% · 6 core'));
  assert.ok(find(cards[1], (x) => x.textContent === '12,5% · 4 core'));
  for (const card of cards) assert.ok(find(card, (x) => x.textContent === '2 GiB / 6 GiB'));
});

test('native backup page distinguishes absent, present and stale unseen copies', () => {
  const s = fixture(), file = { name: 'immich-db-backup-20261118T020000-v3.4.1-pg16.16.sql.gz', bytes: 1000, modifiedAt: s.serverTime };
  s.agents.primary.metrics.builtinBackups = { state: 'ok', checkedAt: s.serverTime, count: 1, bytes: 1000, files: [file] };
  s.agents.backup.metrics.builtinBackups = { state: 'ok', checkedAt: s.serverTime, count: 0, bytes: 0, files: [] };
  let page = setup(s).elements.get('#backup-list');
  assert.ok(find(page, (x) => x.textContent === 'Có file'));
  assert.ok(find(page, (x) => x.textContent === 'Chưa có'));
  s.agents.backup.at -= 300000;
  page = setup(s).elements.get('#backup-list');
  assert.equal(find(page, (x) => x.textContent === 'Chưa có'), undefined);
  assert.ok(find(page, (x) => x.textContent === 'Chưa xác nhận'));
});

test('completed upgrades show current versions without historical success banners or unusable controls',()=>{
  const s=fixture();s.upgradeRecipes=[{version:'3.4.2',fromVersion:'3.4.1',recipe:'native-3.4.2-r1'}];
  s.immichUpgrade={phase:'completed',version:'3.4.2',source:'primary',epoch:1};
  s.operation={kind:'upgrade-apply',upgrade:{},status:'succeeded',index:0,steps:[]};
  for(const n of ['primary','backup'])s.agents[n].metrics.appVersion='3.4.2';
  const {elements}=setup(s);
  for(const id of ['upgrade-phase','upgrade-progress','upgrade-options','upgrade-buttons'])assert.equal(elements.get('#'+id).hidden,true,id);
  assert.equal(elements.get('#upgrade-hint').textContent,'Chưa có bản nâng cấp khả dụng.');
  assert.ok(find(elements.get('#upgrade-nodes'),x=>x.textContent==='3.4.2'));
  s.agents.backup.at-=300000;
  assert.match(setup(s).elements.get('#upgrade-hint').textContent,/Chưa xác nhận/);
});

test('upgrade failures retain visible recovery controls even after both code versions change',()=>{
  const s=fixture();s.upgradeRecipes=[{version:'3.4.2',fromVersion:'3.4.1',recipe:'native-3.4.2-r1'}];
  s.immichUpgrade={phase:'applying',applying:true,version:'3.4.2'};
  s.operation={kind:'upgrade-apply',upgrade:{},status:'failed',error:'Khởi động thất bại',index:0,steps:[]};
  for(const n of ['primary','backup'])s.agents[n].metrics.appVersion='3.4.2';
  const {elements}=setup(s);
  assert.equal(elements.get('#upgrade-retry').hidden,false);
  assert.equal(elements.get('#upgrade-progress').hidden,false);
  assert.match(elements.get('#upgrade-progress').textContent,/Khởi động thất bại/);
  assert.equal(elements.get('#upgrade-phase').textContent,'CẦN XỬ LÝ');
});

test('backup filters distinguish fresh copies on both nodes from records needing confirmation',()=>{
  const s=fixture(),file={name:'immich-db-backup.sql.gz',bytes:100,modifiedAt:s.serverTime};
  for(const n of ['primary','backup'])s.agents[n].metrics.builtinBackups={state:'ok',checkedAt:s.serverTime,count:1,bytes:100,files:[file]};
  const {elements,context}=setup(s);elements.get('#backup-filter').value='both';vm.runInContext('renderBackups()',context);
  assert.equal(elements.get('#backup-count').textContent,'1');
  s.agents.backup.at-=300000;vm.runInContext('renderBackups()',context);
  assert.equal(elements.get('#backup-count').textContent,'0');
  elements.get('#backup-filter').value='attention';vm.runInContext('renderBackups()',context);
  assert.equal(elements.get('#backup-count').textContent,'1');
  assert.ok(find(elements.get('#backup-list'),x=>x.textContent==='Có · số liệu cũ'));
});

test('history groups dates and filters friendly server names and upgrade actions',()=>{
  const s=fixture();s.audit=[{at:s.serverTime,action:'upgrade-apply',actor:'operator@example.com',details:'primary → backup'},
    {at:s.serverTime-86400000,action:'set-auto',actor:'system',details:'enabled'}];
  const {elements,context}=setup(s);
  assert.equal(elements.get('#history-count').textContent,'2 sự kiện');
  assert.ok(find(elements.get('#history-list'),x=>x.className==='history-day'));
  elements.get('#history-search').value='Asus';vm.runInContext('renderHistory()',context);
  assert.equal(elements.get('#history-count').textContent,'1 sự kiện');
  assert.ok(find(elements.get('#history-list'),x=>x.textContent==='Proxmox3 → Asus PVE'));
  elements.get('#history-filter').value='settings';vm.runInContext('renderHistory()',context);
  assert.equal(elements.get('#history-count').textContent,'0 sự kiện');
});
