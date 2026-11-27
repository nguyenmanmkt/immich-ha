import test from 'node:test';
import assert from 'node:assert/strict';
import { funnelUrl, probeFunnel, funnelStatus } from '../src/funnel-health.mjs';

const url = 'https://witness.example.ts.net';
const reply = (body='{"res":"pong"}', node='backup', epoch='16') => new Response(body, {
  headers: { 'X-Immich-HA-Node': node, 'X-Immich-HA-Epoch': epoch },
});

test('Funnel monitoring permits only configured public HTTPS Tailscale URLs', () => {
  assert.equal(funnelUrl(''), null);
  assert.equal(funnelUrl(url + '/'), url);
  assert.equal(funnelUrl(url + ':8443'), url + ':8443');
  for (const invalid of ['http://witness.example.ts.net','https://127.0.0.1','https://photos.example.org',url+'/path',url+'?token=x',url+':8090','https://user:password@witness.example.ts.net']) {
    assert.throws(() => funnelUrl(invalid));
  }
});

test('public Funnel ping verifies application and route identity without forwarding credentials', async () => {
  let args;
  const reading = await probeFunnel(url, async (...input) => { args=input; return reply(); }, () => 1000);
  assert.ok(args[0].startsWith(url+'/api/server/ping?_immich_ha_probe='));
  assert.equal(args[1].redirect,'manual');
  assert.deepEqual(Object.keys(args[1].headers),['User-Agent','Cache-Control']);
  assert.equal(args[1].headers['Cache-Control'],'no-cache, no-store');
  assert.equal(reading.ok,true);
  assert.equal(reading.node,'backup');assert.equal(reading.epoch,16);
  assert.equal(funnelStatus(url,reading,1000,'backup',16).state,'healthy');
  assert.equal(funnelStatus(url,reading,1000,'primary',17).state,'route-mismatch');
  assert.equal(funnelStatus(url,reading,181001,'backup',16).state,'stale');
  assert.equal(funnelStatus(url,reading,999,'backup',16).state,'stale');
  assert.equal(funnelStatus('https://new.example.ts.net',reading,1000,'backup',16).state,'unknown');
});

test('redirects, oversized responses, wrong JSON and missing identity never report healthy', async () => {
  for (const response of [new Response('',{status:302,headers:{Location:'https://login.example.org'}}),
    reply('not JSON'),reply('{"res":"other"}'),reply('x'.repeat(1025)),reply(undefined,'unknown'),reply(undefined,'backup','0')]) {
    const reading=await probeFunnel(url,async()=>response,()=>1000);
    assert.equal(reading.ok,false);
    assert.equal(funnelStatus(url,reading,1000,'backup',16).state,'unavailable');
  }
});

test('timeout is diagnostic only and disabled monitor performs no network requests', async () => {
  const reading=await probeFunnel(url,async()=>{throw new DOMException('timeout','TimeoutError');},()=>1000);
  assert.equal(reading.ok,false);assert.equal(reading.error,'Kiểm tra quá 5 giây');
  assert.equal(await probeFunnel('',()=>{throw new Error('must not fetch');}),null);
  assert.equal(funnelStatus('',reading,1000,'backup',16),null);
});

test('successive probes use different URLs rather than reuse a cached owner response', async () => {
  const requests=[];
  const fetcher=async input=>{requests.push(input);return reply();};
  await probeFunnel(url,fetcher);await probeFunnel(url,fetcher);
  assert.notEqual(requests[0],requests[1]);
});
