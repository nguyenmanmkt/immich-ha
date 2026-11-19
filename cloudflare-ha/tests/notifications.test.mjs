import test from "node:test";
import assert from "node:assert/strict";
import { initialNotifications, publicNotifications, validateWebhook, configureNotifications, queueTest,
  observeNotifications, slackPayload, postSlack, completeNotification } from "../src/notifications.mjs";

const webhook = "https://hooks.slack.com/services/TTEST00/BTEST00/not_a_real_webhook_token_1234";
const at = 1_000_000;
function fixture() {
  const n = initialNotifications(); configureNotifications(n, { enabled: true, webhook });
  const s = { owner: "primary", epoch: 3, ready: true, leaseUntil: at + 240000, serverTime: at,
    primaryHealthy: true, backupHealthy: true, standbyEligible: true,
    agents: { primary: { at, metrics: { appMode: "active" } }, backup: { at, metrics: { appMode: "standby" } } } };
  observeNotifications(n, s, at);
  return { n, s };
}
test("webhook validation rejects redirects, credentials, arbitrary hosts and secret-bearing query strings", () => {
  assert.equal(validateWebhook(webhook), webhook);
  for (const url of [webhook.replace('https:', 'http:'), webhook.replace('hooks.slack.com','hooks.slack.com.evil.test'),
    webhook.replace('hooks.slack.com','127.0.0.1'), webhook+'?token=private', webhook+'#private',
    webhook.replace('hooks.slack.com','user@hooks.slack.com'), webhook.replace('hooks.slack.com','hooks.slack.com:9443')]) {
    assert.throws(() => validateWebhook(url), (e) => !e.message.includes('private') && !e.message.includes('services/'));
  }
});
test("configuration stays private and initial state does not send historical notifications", () => {
  const { n } = fixture();
  assert.equal(n.queue.length, 0);
  assert.deepEqual(Object.keys(publicNotifications(n)).sort(), ['configured','enabled','failedCount','lastError','lastSentAt','pending'].sort());
  assert.equal(JSON.stringify(publicNotifications(n)).includes('not_a_real'), false);
});
test("ownership changes notify immediately once, including the reason and both node roles", () => {
  const { n, s } = fixture();
  Object.assign(s, { owner: 'backup', epoch: 4, ready: false, standbyEligible: false, primaryHealthy: false,
    lastTakeover: { reason: 'automatic' } });
  observeNotifications(n, s, at + 1); observeNotifications(n, s, at + 2);
  assert.equal(n.queue.length, 1); assert.equal(n.queue[0].kind, 'role-change');
  const payload = slackPayload(n.queue[0], {primary:{label:'Proxmox3'},backup:{label:'Asus PVE'}});
  assert.match(payload.text, /Proxmox3 → Asus PVE/); assert.match(payload.text, /Tự chuyển khi có sự cố/);
  assert.equal(payload.text.includes(webhook), false);
  s.ready = true; observeNotifications(n, s, at + 3);
  assert.equal(n.queue.length, 2);
});
test("brief standby readiness flaps do not alert, sustained changes do", () => {
  const { n, s } = fixture(); s.standbyEligible = false;
  observeNotifications(n, s, at + 1);
  s.standbyEligible = true; observeNotifications(n, s, at + 30000);
  assert.equal(n.queue.length, 0);
  s.standbyEligible = false; observeNotifications(n, s, at + 31000);
  observeNotifications(n, s, at + 91000);
  assert.equal(n.queue.length, 1);
});
test("queue survives serialization and repeated status reads do not create duplicates", () => {
  const { n, s } = fixture(); queueTest(n, s, at);
  const restored = JSON.parse(JSON.stringify(n));
  observeNotifications(restored, s, at + 1);
  assert.equal(restored.queue.length, 1); assert.throws(() => queueTest(restored, s, at + 2));
});
test("saving a blank URL retains the secret; disabling/removing clears pending delivery", () => {
  const { n, s } = fixture(); queueTest(n, s, at);
  configureNotifications(n, { enabled: false, webhook: '' });
  assert.equal(n.webhook, webhook); assert.equal(n.queue.length, 0);
  assert.throws(() => queueTest(n, s, at));
  configureNotifications(n, { enabled: false, remove: true }); assert.equal(n.webhook, null);
  assert.throws(() => configureNotifications(n, { enabled: true }));
});
test("delivery uses JSON, refuses redirects and recognizes Slack success", async () => {
  const result = await postSlack(webhook, { text: 'test' }, async (url, init) => {
    assert.equal(url, webhook); assert.equal(init.redirect, 'manual'); assert.equal(init.method, 'POST');
    assert.deepEqual(JSON.parse(init.body), { text: 'test' });
    return new Response('ok');
  });
  assert.deepEqual(result, { ok: true });
  const redirect = await postSlack(webhook, {}, async () => new Response('', {status:302,headers:{Location:'https://evil.test'}}));
  assert.equal(redirect.retry, false);
});
test("rate limits and network failures retry without leaking the URL", async () => {
  const limited = await postSlack(webhook, {}, async () => new Response('', {status:429,headers:{'Retry-After':'120'}}));
  assert.equal(limited.retryAfter, 120000); assert.equal(limited.retry, true);
  const failed = await postSlack(webhook, {}, async () => {throw new Error(webhook);});
  assert.equal(failed.retry, true); assert.equal(JSON.stringify(failed).includes(webhook), false);
});
test("bounded retries respect Retry-After; permanent errors stop retrying", () => {
  const { n, s } = fixture(); queueTest(n, s, at);
  let e = n.queue[0]; e.attempts = 1;
  completeNotification(n, e.id, { ok:false,retry:true,error:'Slack HTTP 429',retryAfter:120000 }, at);
  assert.equal(e.nextAttemptAt, at+120000);
  e.attempts=8; completeNotification(n,e.id,{ok:false,retry:true,error:'timeout'},at);
  assert.equal(n.queue.length, 0); assert.equal(n.failedCount, 1);
  queueTest(n,s,at); e=n.queue[0]; e.attempts=1;
  completeNotification(n,e.id,{ok:false,retry:false,error:'Slack HTTP 404'},at);
  assert.equal(n.queue.length, 0);
});
test("successful delivery stores confirmation and clears the previous error", () => {
  const { n, s } = fixture(); queueTest(n,s,at); n.lastError='old error';
  completeNotification(n,n.queue[0].id,{ok:true},at+10);
  assert.equal(n.lastSentAt, at+10); assert.equal(n.lastError,null); assert.equal(n.queue.length,0);
});
