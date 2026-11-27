import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import * as controller from "../src/controller.mjs";
import * as notifications from "../src/notifications.mjs";
import * as funnelHealth from '../src/funnel-health.mjs';
import * as funnelControl from '../src/funnel-control.mjs';

const webhook = 'https://hooks.slack.com/services/TTEST00/BTEST00/not_a_real_webhook_token_1234';
function harness(fetcher, env={}) {
  const values = new Map(); let alarm = null;
  const ctx = { storage: {
    async get(key) { return structuredClone(values.get(key)); },
    async put(key, value) { if (typeof key === 'string') values.set(key, structuredClone(value));
      else for (const [k,v] of Object.entries(key)) values.set(k, structuredClone(v)); },
    async getAlarm() { return alarm; }, async setAlarm(at) { alarm=at; },
  }, blockConcurrencyWhile(callback) { const p=callback(); this.latest=p; return p; } };
  const sandbox = { ...controller, ...notifications, ...funnelHealth, ...funnelControl, DurableObject: class {},
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
