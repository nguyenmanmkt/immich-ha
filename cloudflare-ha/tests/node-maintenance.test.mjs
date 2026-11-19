import test from "node:test";
import assert from "node:assert/strict";
import { initialState, admin, advance, eligible, heartbeat, receiveResult, LIMITS } from "../src/controller.mjs";
import { maintenanceView } from "../web/status-model.mjs";

const now=1_000_000;
function fixture(owner='primary') {
  const s=initialState(); Object.assign(s,{owner,epoch:5,ready:true,auto:true,leaseUntil:now+240000});
  for (const node of ['primary','backup']) {
    s.probes[node]={at:now,ok:true};
    s.heartbeats[node]={at:now,localHealthy:true,enforcing:true,canPromote:true,protocolVersion:2,
      metrics:{runtimeV2:true,maintenanceControl:true,appMode:node===owner?'active':'standby',appVersion:'3.4.1',postgresVersion:'16.16',seed:{source:owner,sourceEpoch:5},disk:{free:4*1024**3},databaseDisk:{free:4*1024**3}}};
  }
  return s;
}
for (const owner of ['primary','backup']) test(`maintenance excludes the other machine while ${owner} keeps serving`,()=>{
  const s=fixture(owner), target=owner==='primary'?'backup':'primary';
  admin(s,{action:'set-preference',target,enabled:true},now);
  admin(s,{action:'set-maintenance',target,enabled:true},now);
  assert.equal(s.owner,owner); assert.equal(s.auto,true); assert.equal(eligible(s,target,now),false);
  assert.equal(s.preference.status,'maintenance'); assert.equal(s.recovery.status,'maintenance');
  const grant=heartbeat(s,target,{session:'maintenance_session_123456',epoch:5,ready:false,
    ...s.heartbeats[target]},now+1);
  assert.equal(grant.role,'maintenance'); assert.equal(grant.leaseMs,0); assert.equal(grant.command,null);
  assert.throws(()=>admin(s,{action:'switch',target},now+2),/bảo trì/);
  const future=now+LIMITS.failureMs+1000;
  s.probes[owner]={at:future,ok:false};s.probes[target]={at:future,ok:true};s.heartbeats[target].at=future;
  advance(s,future);advance(s,future+LIMITS.failureMs);
  assert.equal(s.owner,owner); assert.equal(s.pending,null);
});
test('cannot maintain the serving node or interrupt an ongoing operation',()=>{
  const s=fixture();
  assert.throws(()=>admin(s,{action:'set-maintenance',target:'primary',enabled:true},now),/Bàn giao/);
  admin(s,{action:'prepare',target:'backup'},now);
  assert.throws(()=>admin(s,{action:'set-maintenance',target:'backup',enabled:true},now),/hoàn tất/);
});
test('ending maintenance still excludes the old seed until a real validation completes',()=>{
  const s=fixture();
  admin(s,{action:'set-maintenance',target:'backup',enabled:true},now);
  admin(s,{action:'set-maintenance',target:'backup',enabled:false},now+1);
  assert.equal(eligible(s,'backup',now+1),false);
  assert.equal(s.nodeMaintenance.backup.awaitingValidation,true);
  admin(s,{action:'prepare',target:'backup'},now+2);
  let t=now+3,first;
  function ack() {
    const c=s.operation.command;
    const result=c.action==='dump'?{name:'immich-20261117-150000.dump',sha256:'a'.repeat(64),manifestSha256:'b'.repeat(64),
      manifestName:'immich-20261117-150000.dump.manifest.json.gz',source:c.source,epoch:c.epoch}:{healthy:true};
    const payload={jobId:c.jobId,commandId:c.id,index:c.index,status:'succeeded',result};
    receiveResult(s,c.node,payload,t++);return {node:c.node,payload};
  }
  first=ack();while(s.operation.command.action!=='validate')ack();
  receiveResult(s,first.node,first.payload,t++);
  assert.equal(s.nodeMaintenance.backup.awaitingValidation,true,'duplicate acknowledgement must not clear the validation barrier');
  ack();assert.equal(s.nodeMaintenance.backup.awaitingValidation,false);
  assert.equal(eligible(s,'backup',t),true);
});
test('old backends cannot appear to have accepted maintenance',()=>{
  const s=fixture();delete s.heartbeats.backup.metrics.maintenanceControl;
  assert.throws(()=>admin(s,{action:'set-maintenance',target:'backup',enabled:true},now),/Chờ|chờ/);
});
test('UI only confirms maintenance after services and both sync folders are stopped',()=>{
  const s={...fixture(),serverTime:now,primaryHealthy:true,backupHealthy:false,nodeMaintenance:{backup:{enabled:true}}};
  assert.equal(maintenanceView(s,'backup').confirmed,false);
  const h=s.heartbeats.backup;
  s.agents=s.heartbeats;h.localHealthy=false;h.metrics.appMode='maintenance';
  assert.equal(maintenanceView(s,'backup').confirmed,false);
  h.metrics.syncthing={folders:{'immich-library':{paused:true},'immich-database':{paused:true}}};
  assert.equal(maintenanceView(s,'backup').confirmed,true);
  assert.equal(maintenanceView(s,'primary').canChange,false);
});
