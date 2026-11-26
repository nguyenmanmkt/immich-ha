import test from 'node:test';
import assert from 'node:assert/strict';
import { initialState, heartbeat, recordProbe, admin, receiveResult, route, advance } from '../src/controller.mjs';
let serial=0;
const id=()=>`12345678-1234-1234-1234-${String(++serial).padStart(12,'0')}`;
function harness(owner='primary') {
 const s=initialState();s.owner=owner;s.auto=true;let now=100000;
 function tick(versions={}) {
  for(const n of ['primary','backup']) {
   recordProbe(s,n,true,now);
   heartbeat(s,n,{session:n+'_session_12345678',epoch:s.epoch,ready:true,localHealthy:true,enforcing:true,canPromote:n!==s.owner,protocolVersion:2,
    metrics:{runtimeV2:true,appVersion:versions[n]||s.heartbeats[n]?.metrics.appVersion||'3.4.1',postgresVersion:'16.16',
     immichUpgrade:{protocol:1,recipes:['native-3.4.2-r1']},postgresStreaming:{handoverVersion:2,inRecovery:n!==s.owner},seed:{sourceEpoch:s.epoch}}},now);
  }now++;
 }
 tick();tick();
 function start(action,extra={}) {admin(s,{action,version:'3.4.2',id:id(),actor:'admin',...extra},now++);}
 function ack(extra={}) {
  const c=s.operation.command;assert.ok(c);
  let result={version:'3.4.2',recipe:'native-3.4.2-r1',checked:true,systemId:'123',stageId:s.operation.upgrade.stageId,sha256:'a'.repeat(64),appStopped:true,verified:true,installed:true,started:true,copied:true,replicaName:'immich-db-backup-20261123T010000-preupgrade.sql.gz',replicaSha256:'a'.repeat(64)};
  if(c.action==='wal-barrier')result={kind:'wal-manifest-v1',commandId:c.id,jobId:c.jobId,source:c.source,epoch:c.epoch,manifestName:`wal-${c.id}.manifest.json.gz`,manifestSha256:'b'.repeat(64),walLsn:'0/ABC',systemId:'123',timeline:5,files:10,bytes:1000,stopped:false,peerAcknowledged:false};
  if(c.action==='sync-wal')result={manifestVerified:true,systemId:'123',timeline:5,replayLsn:'0/ABC',filesVerified:10};
  receiveResult(s,c.node,{jobId:c.jobId,commandId:c.id,index:c.index,status:'succeeded',result:{...result,...extra}},now++);
 }
 function prepared() {start('upgrade-stage');while(s.operation.status==='running')ack();assert.equal(s.immichUpgrade.phase,'prepared');}
 return {s,start,ack,tick,prepared,now:()=>now++};
}
test('unsupported versions and missing capability cannot start upgrades',()=>{
 const h=harness();assert.throws(()=>h.start('upgrade-stage',{version:'3.5.0'}),/kiểm chứng/);
 delete h.s.heartbeats.backup.metrics.immichUpgrade;
 assert.throws(()=>h.start('upgrade-stage'),/agent/);
});
test('stage only builds; old app remains routable, same owner, separate confirmation required',()=>{
 const h=harness('backup');h.start('upgrade-stage');
 assert.equal(h.s.operation.maintenance,false);assert.ok(route(h.s,h.now()));
 assert.deepEqual(h.s.operation.steps.map(x=>x.action),['upgrade-preflight','upgrade-preflight','upgrade-build','upgrade-build']);
 while(h.s.operation.status==='running')h.ack();
 assert.equal(h.s.owner,'backup');assert.equal(h.s.immichUpgrade.phase,'prepared');assert.equal(h.s.auto,true);
 assert.equal(Object.keys(h.s.immichUpgrade.receipts).length,2);
});
test('different database lineage rejects preparation',()=>{
 const h=harness();h.start('upgrade-stage');h.ack();assert.throws(()=>h.ack({systemId:'456'}),/dòng dữ liệu/);
});
test('both agents must advertise the specific reviewed recipe, not just protocol support',()=>{
 const h=harness();h.s.heartbeats.backup.metrics.immichUpgrade.recipes=['some-other-recipe'];
 assert.throws(()=>h.start('upgrade-stage'),/agent/);
});
test('apply requires prepared receipts bound to current source and epoch',()=>{
 const h=harness();assert.throws(()=>h.start('upgrade-apply'),/chuẩn bị/);
 h.prepared();h.s.epoch++;h.tick();assert.throws(()=>h.start('upgrade-apply'),/đã cũ/);
});
test('apply freezes and backs up before installing standby then active; never transfers ownership',()=>{
 const h=harness();h.prepared();h.start('upgrade-apply');
 const actions=h.s.operation.steps.map(x=>x.action);
 assert.ok(actions.indexOf('upgrade-backup')<actions.indexOf('upgrade-install'));
 assert.equal(h.s.operation.steps.find(x=>x.action==='upgrade-install').node,'backup');
 assert.equal(h.s.operation.steps.find(x=>x.action==='upgrade-start').node,'primary');
 assert.ok(!actions.includes('transfer'));
 h.ack();h.ack();assert.equal(h.s.operation.maintenance,true);assert.equal(route(h.s,h.now()),null);
 for(const action of ['switch','resume','set-auto','set-preference','set-maintenance'])assert.throws(()=>h.start(action,{target:'backup',enabled:true}),/Nâng cấp/);
});
test('failed upgrade stays protected through heartbeat/recovery and retries same immutable stage',()=>{
 const h=harness();h.prepared();h.start('upgrade-apply');h.ack();h.ack();
 const stage=h.s.operation.upgrade.stageId,oldId=h.s.operation.id,c=h.s.operation.command;
 receiveResult(h.s,c.node,{jobId:c.jobId,commandId:c.id,index:c.index,status:'failed',error:'disk full'},h.now());
 h.s.ready=false;h.s.heartbeats.primary.localHealthy=false;
 assert.doesNotThrow(()=>advance(h.s,h.now()));assert.equal(h.s.operation.status,'failed');
 assert.equal(h.s.auto,false);assert.equal(h.s.immichUpgrade.applying,true);
 h.start('upgrade-retry');assert.notEqual(h.s.operation.id,oldId);assert.equal(h.s.operation.upgrade.stageId,stage);
 assert.equal(h.s.operation.maintenance,true);
});
test('failed initial verification permits fresh preparation without freezing or changing code',()=>{
 const h=harness();h.prepared();h.start('upgrade-apply');const c=h.s.operation.command;
 receiveResult(h.s,c.node,{jobId:c.jobId,commandId:c.id,index:c.index,status:'failed',error:'staging missing'},h.now());
 assert.equal(h.s.immichUpgrade.phase,'stale');assert.equal(h.s.immichUpgrade.applying,false);assert.equal(h.s.operation.maintenance,false);
 assert.ok(route(h.s,h.now()));h.start('upgrade-stage');assert.equal(h.s.operation.status,'running');
});
test('completion waits for new active version, WAL validation and both checks',()=>{
 const h=harness();h.prepared();h.start('upgrade-apply');
 while(h.s.operation.command)h.ack();
 assert.equal(h.s.operation.steps[h.s.operation.index].action,'upgrade-wait');
 assert.equal(h.s.immichUpgrade.applying,true);assert.equal(route(h.s,h.now()),null);
 h.tick({primary:'3.4.2',backup:'3.4.2'});
 while(h.s.operation.command)h.ack();
 assert.equal(h.s.operation.status,'succeeded');assert.equal(h.s.immichUpgrade.phase,'completed');assert.equal(h.s.owner,'primary');assert.ok(route(h.s,h.now()));
});
test('retry after reboot may lack SQL health or app directory, but must retain saved database identity',()=>{
 const h=harness();h.prepared();h.start('upgrade-apply');h.ack();h.ack();const c=h.s.operation.command;
 receiveResult(h.s,c.node,{jobId:c.jobId,commandId:c.id,index:c.index,status:'failed',error:'reboot'},h.now());
 h.s.ready=false;
 for(const n of ['primary','backup']){h.s.heartbeats[n].metrics.postgresStreaming.available=false;delete h.s.heartbeats[n].metrics.appVersion;}
 h.start('upgrade-retry');assert.equal(h.s.operation.upgrade.systemId,'123');assert.equal(h.s.operation.command.action,'upgrade-verify');
});
test('invalid build and backup receipts cannot advance the operation',()=>{
 const h=harness();h.start('upgrade-stage');h.ack();h.ack();
 assert.throws(()=>h.ack({sha256:'bad'}),/checksum/);h.ack();h.ack();
 h.start('upgrade-apply');h.ack();h.ack();h.ack();
 assert.throws(()=>h.ack({verified:false}),/Backup/);
});
