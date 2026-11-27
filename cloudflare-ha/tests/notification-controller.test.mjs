import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import * as controller from "../src/controller.mjs";
import * as notifications from "../src/notifications.mjs";
import * as funnelHealth from '../src/funnel-health.mjs';
import * as funnelControl from '../src/funnel-control.mjs';
import * as enrollment from '../src/enrollment.mjs';
import * as witnessEnrollment from '../src/witness-enrollment.mjs';

const webhook = 'https://hooks.slack.com/services/TTEST00/BTEST00/not_a_real_webhook_token_1234';
function harness(fetcher, env={}) {
  const values = new Map(); let alarm = null;
  const ctx = { storage: {
    async get(key) { return structuredClone(values.get(key)); },
    async put(key, value) { if (typeof key === 'string') values.set(key, structuredClone(value));
      else for (const [k,v] of Object.entries(key)) values.set(k, structuredClone(v)); },
    async getAlarm() { return alarm; }, async setAlarm(at) { alarm=at; },
  }, blockConcurrencyWhile(callback) { const p=callback(); this.latest=p; return p; } };
  const sandbox = { ...controller, ...notifications, ...funnelHealth, ...funnelControl, ...enrollment, ...witnessEnrollment,
    witnessSteps:witnessEnrollment.steps,witnessHealthy:witnessEnrollment.healthy,
    setupCommand:enrollment.command,setupResult:enrollment.acceptResult,setupDigest:enrollment.digest,DurableObject: class {},
    structuredClone, Response, URL, Date, crypto, fetch: fetcher };
  const source = fs.readFileSync(new URL('../src/worker.mjs',import.meta.url),'utf8')
    .replace(/^import .*;\r?$/gm,'').replace('export class HAController','class HAController')
    .replace('export default {','const worker = {') + '\nglobalThis.Controller=HAController;';
  vm.runInNewContext(source,sandbox);
  const instance = new sandbox.Controller(ctx,{ NODE_METADATA:'{}', ...env });
  async function call(path,input={}) { return instance.fetch(new Request('https://internal'+path,{method:'POST',body:JSON.stringify(input)})); }
  return { instance, ctx, values, call, get alarm() { return alarm; } };
}
test('Funnel observations persist separately and cannot change owner, epoch or lease', async () => {
  const url='https://witness.example.ts.net';
  const h=harness(undefined,{FUNNEL_URL:url});await h.ctx.latest;
  const before=await (await h.call('/status')).json();
  assert.equal(before.funnel.state,'unknown');
  const reading={url,checkedAt:Date.now(),ok:true,node:'primary',epoch:1,latencyMs:80,status:200};
  await h.call('/probe',{primary:false,backup:false,funnel:reading});
  const after=await (await h.call('/status')).json();
  assert.equal(after.funnel.state,'healthy');
  for (const key of ['owner','epoch','leaseUntil']) assert.equal(after[key],before[key]);
  assert.equal(h.values.get('funnelProbe').node,'primary');
  assert.equal(h.values.get('state').funnel,undefined);
  assert.equal((await (await h.call('/node-info',{node:'primary'})).json()).funnel,undefined);
});

test('new enrollment locks manual HA changes and never returns its secrets in dashboard status', async()=>{
  const config=n=>({node:n,agent_token:n+'_agent_example_12345678901234567890',origin_token:n+'_origin_example_12345678901234567890'});
  const env={PRIMARY_AGENT_TOKEN:config('primary').agent_token,BACKUP_AGENT_TOKEN:config('backup').agent_token,
    PRIMARY_ORIGIN_TOKEN:config('primary').origin_token,BACKUP_ORIGIN_TOKEN:config('backup').origin_token};
  const h=harness(undefined,env);await h.ctx.latest;
  const input={id:crypto.randomUUID(),bundle:'a'.repeat(64),joinHash:'b'.repeat(64),networkMode:'lan',
    appPassword:'new_app_secret_example_12345678901234567890',replicationPassword:'repl_secret_example_12345678901234567890',
    nodes:Object.fromEntries(['primary','backup'].map(n=>[n,{config:config(n),token:n+'_setup_example_12345678901234567890',tunnelToken:n+'_tunnel'}]))};
  assert.equal((await h.call('/setup-init',input)).status,200);
  const view=await (await h.call('/status')).json();
  assert.equal(view.setup.status,'waiting-node');assert.equal(view.auto,false);
  assert.equal(JSON.stringify(view).includes(input.appPassword),false);
  assert.equal(JSON.stringify(view).includes(input.nodes.backup.token),false);
  assert.equal((await h.call('/admin',{action:'set-auto',enabled:true})).status,409);
  assert.equal((await h.call('/setup-node',{node:'backup',token:input.nodes.backup.token})).status,401);
});

test('optional witness endpoints preserve admission state, scope credentials and hide PKI from status',async()=>{
  const h=harness();await h.ctx.latest;
  const s=h.instance.state;
  // Stub the view's readiness with normal controller fixtures from a completed
  // setup: creation guard itself is covered in witness-enrollment tests.
  const id=crypto.randomUUID();
  h.instance.setup={status:'completed',results:{},nodes:{primary:{token:'a'.repeat(48),config:{controller_url:'https://test.example.workers.dev'}},backup:{token:'b'.repeat(48),claim:'claimed'}}};
  h.instance.witness={id,clusterId:id,status:'running',owner:s.owner,epoch:s.epoch,index:0,attempt:0,claim:'claimed',token:'w'.repeat(48),
    networkMode:'lan',addresses:{primary:'192.168.1.1',backup:'192.168.1.2',witness:'192.168.1.3'},results:{},saved:{auto:false,recovery:false}};
  const wrong=await h.call('/witness-node',{node:'primary',token:'w'.repeat(48)});assert.equal(wrong.status,401);
  const w=await (await h.call('/witness-node',{node:'witness',token:'w'.repeat(48)})).json();assert.equal(w.command,null);
  const a=await (await h.call('/witness-node',{node:'primary',token:'a'.repeat(48)})).json();assert.equal(a.command.action,'witness-inspect');
  const before=await (await h.call('/status')).json();assert.equal(before.witness.joined,true);
  assert.equal(JSON.stringify(before).includes('w'.repeat(48)),false);
  assert.equal((await h.call('/admin',{action:'switch',target:'backup'})).status,409);
  assert.equal((await h.call('/admin',{action:'set-auto',enabled:true})).status,409);
  const after=await (await h.call('/status')).json();
  assert.equal(after.owner,before.owner);assert.equal(after.epoch,before.epoch);
  h.instance.witness.status='completed';
  assert.equal((await h.call('/witness-node',{node:'witness',token:'w'.repeat(48),funnel:{url:'http://127.0.0.1',enabled:true}})).status,409);
  const report=await h.call('/witness-node',{node:'witness',token:'w'.repeat(48),funnel:{url:'https://witness.example.ts.net',enabled:true,tailscaleIp:'100.64.0.3'}});assert.equal(report.status,200);
  const published=await (await h.call('/status')).json();assert.equal(published.funnel.url,'https://witness.example.ts.net');
  assert.equal((await h.call('/admin',{action:'set-funnel',enabled:false})).status,200);
  const desired=await (await h.call('/witness-node',{node:'witness',token:'w'.repeat(48)})).json();assert.equal(desired.funnel.enabled,false);
  assert.equal(h.instance.state.owner,before.owner);assert.equal(h.instance.state.epoch,before.epoch);
});

test('HTTPS witness workflow completes without SSH and restores automation only after verified local stages',async()=>{
  const h=harness();await h.ctx.latest;
  const base={status:'completed',bundle:'a'.repeat(64),networkMode:'lan',index:27,results:{},nodes:{}};
  const now=Date.now();
  for(const [i,n] of ['primary','backup'].entries()) {
    base.nodes[n]={token:n+'_token_12345678901234567890',config:{controller_url:'https://test.example.workers.dev'},claim:'joined'};
    base.results[n+'-inspect']={replicationIp:'192.168.1.'+(i+1)};
    controller.heartbeat(h.instance.state,n,{session:n+'_session_12345678',epoch:1,ready:true,localHealthy:true,enforcing:true,canPromote:true},now);
    controller.recordProbe(h.instance.state,n,true,now);
  }
  controller.heartbeat(h.instance.state,'primary',{session:'primary_session_12345678',epoch:1,ready:true,localHealthy:true,enforcing:true,canPromote:false},now+1);
  controller.admin(h.instance.state,{action:'set-auto',enabled:true},now+2);
  h.instance.setup=base;
  const code='new_witness_join_code_12345678901234567890';const token='witness_token_12345678901234567890';
  const init=await h.call('/witness-init',{id:crypto.randomUUID(),bundle:base.bundle,joinHash:await enrollment.digest(code),token});assert.equal(init.status,200);
  assert.equal(h.instance.state.auto,false);
  assert.equal((await h.call('/witness-join',{code,claim:'new_device_claim_12345678901234567890',bundle:base.bundle,ip:'192.168.1.3'})).status,200);
  for(let i=0;i<40 && h.instance.witness.status!=='completed';i++) {
    const e=h.instance.witness;const [node,action]=witnessEnrollment.steps(e)[e.index];
    assert.notEqual(node,'controller','controller policy step is persisted automatically');
    const auth=node==='witness'?token:base.nodes[node].token;
    const response=await (await h.call('/witness-node',{node,token:auth})).json();const c=response.command;assert.ok(c);
    let result={verified:true};
    if(action==='inspect')result={...result,ip:e.addresses[node],machineId:'machine-'+node,existingQuorum:false,busy:false,freeBytes:3*1024**3,memoryBytes:1024**3,systemId:'12345',inRecovery:node!=='primary',receiverConnected:true};
    if(action==='pki')result.certificates=Object.fromEntries(['primary','backup','witness'].map(n=>[n,Object.fromEntries(['client-ca.crt','peer-ca.crt','peer.crt','peer.key','server.crt','server.key','probe.crt','probe.key'].map(k=>[k,n+'-'+k]))]));
    if(action==='fallback')h.instance.state.heartbeats[node].metrics={quorumAuthority:{fallbackEnabled:true}};
    const ack=await h.call('/witness-node',{node,token:auth,receipt:{id:c.id,status:'succeeded',result}});assert.equal(ack.status,200);
    if(h.instance.witness.status!=='completed')assert.equal(h.instance.state.auto,false);
  }
  assert.equal(h.instance.witness.status,'completed');assert.equal(h.instance.state.auto,true);
  assert.equal(h.instance.state.owner,'primary');assert.equal(h.instance.state.epoch,1);
  assert.equal(h.instance.state.quorumControl.clusterId,h.instance.witness.clusterId);
  assert.equal((await h.call('/witness-init',{})).status,409,'never restage an existing witness');
});

test('Funnel setting and witness acknowledgement stay separate from Immich authority', async()=>{
  const h=harness(undefined,{FUNNEL_URL:'https://witness.example.ts.net',FUNNEL_CONTROL_TOKEN:'dedicated_test_token_12345678901234567890'});await h.ctx.latest;
  const before=await (await h.call('/status')).json();
  const pending=await (await h.call('/admin',{action:'set-funnel',enabled:false})).json();
  assert.equal(pending.funnel.control.enabled,false);
  assert.equal(pending.funnel.control.confirmed,false);
  const applied=await (await h.call('/funnel-agent',{enabled:false,revision:1})).json();
  assert.equal(applied.confirmed,true);
  const after=await (await h.call('/status')).json();
  for (const key of ['owner','epoch','leaseUntil','auto']) assert.equal(after[key],before[key]);
  assert.equal(h.values.get('funnelControl').enabled,false);
  assert.equal(h.values.get('state').funnelControl,undefined);
});

test('controller persists webhook privately; status and node-info never return the secret', async () => {
  const h=harness(); await h.ctx.latest;
  const saved=await (await h.call('/admin',{action:'set-alerts',enabled:true,webhook})).json();
  assert.equal(saved.alerts.configured,true); assert.equal(JSON.stringify(saved).includes(webhook),false);
  assert.equal(h.values.get('notifications').webhook,webhook);
  const node=await (await h.call('/node-info',{node:'primary'})).json();
  assert.equal(JSON.stringify(node).includes(webhook),false);
  assert.equal(node.alerts,undefined);
  assert.equal(h.alarm,null,'no idle alarm polling without queued work');
});
test('Slack delivery does not block coordinator requests and success is persisted', async () => {
  let resolve;
  const pending=new Promise(r=>{resolve=r;});
  const h=harness(async()=>pending); await h.ctx.latest;
  await h.call('/admin',{action:'set-alerts',enabled:true,webhook});
  await h.call('/admin',{action:'test-alerts'});
  assert.ok(h.alarm);
  const task=h.instance.alarm();
  // Allow the alarm claim/storage awaits to finish before inspecting the pending fetch.
  await new Promise(r=>setTimeout(r,10));
  const result=await (await h.call('/status')).json();
  assert.equal(result.owner,'primary'); assert.equal(result.alerts.pending,1);
  resolve(new Response('ok')); await task;
  assert.equal(h.values.get('notifications').queue.length,0);
  assert.ok(h.values.get('notifications').lastSentAt);
});
test('reconfiguring while a Slack send is in progress cannot overwrite new settings', async () => {
  let resolve; const pending=new Promise(r=>{resolve=r;});
  const h=harness(async()=>pending); await h.ctx.latest;
  await h.call('/admin',{action:'set-alerts',enabled:true,webhook});
  await h.call('/admin',{action:'test-alerts'});
  const task=h.instance.alarm(); await new Promise(r=>setTimeout(r,10));
  await h.call('/admin',{action:'set-alerts',enabled:false,remove:true});
  resolve(new Response('ok')); await task;
  const n=h.values.get('notifications');
  assert.equal(n.webhook,null); assert.equal(n.enabled,false); assert.equal(n.queue.length,0);
});
