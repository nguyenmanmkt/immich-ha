import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import * as controller from "../src/controller.mjs";
import * as notifications from "../src/notifications.mjs";

const webhook = 'https://hooks.slack.com/services/TTEST00/BTEST00/not_a_real_webhook_token_1234';
function harness(fetcher) {
  const values = new Map(); let alarm = null;
  const ctx = { storage: {
    async get(key) { return structuredClone(values.get(key)); },
    async put(key, value) { if (typeof key === 'string') values.set(key, structuredClone(value));
      else for (const [k,v] of Object.entries(key)) values.set(k, structuredClone(v)); },
    async getAlarm() { return alarm; }, async setAlarm(at) { alarm=at; },
  }, blockConcurrencyWhile(callback) { const p=callback(); this.latest=p; return p; } };
  const sandbox = { ...controller, ...notifications, DurableObject: class {},
    structuredClone, Response, URL, Date, crypto, fetch: fetcher };
  const source = fs.readFileSync(new URL('../src/worker.mjs',import.meta.url),'utf8')
    .replace(/^import .*;\r?$/gm,'').replace('export class HAController','class HAController')
    .replace('export default {','const worker = {') + '\nglobalThis.Controller=HAController;';
  vm.runInNewContext(source,sandbox);
  const instance = new sandbox.Controller(ctx,{ NODE_METADATA:'{}' });
  async function call(path,input={}) { return instance.fetch(new Request('https://internal'+path,{method:'POST',body:JSON.stringify(input)})); }
  return { instance, ctx, values, call, get alarm() { return alarm; } };
}
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
