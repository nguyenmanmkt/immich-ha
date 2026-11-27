import test from 'node:test';
import assert from 'node:assert/strict';
import {digest} from '../src/enrollment.mjs';
import {createWitness,joinWitness,witnessAuthorized,witnessCommand,witnessResult,publicWitness,extensionStable,steps,privateIP} from '../src/witness-enrollment.mjs';

const code='test_witness_code_12345678901234567890';
async function fixture(mode='lan',owner='primary') {
  const ips=mode==='lan'?['192.168.1.10','192.168.1.20','192.168.1.30']:['100.64.0.1','100.64.0.2','100.64.0.3'];
  const base={status:'completed',bundle:'a'.repeat(64),networkMode:mode,
    nodes:{primary:{token:'primary_token_12345678901234567890'},backup:{token:'backup_token_12345678901234567890',claim:'claimed'}},
    results:Object.fromEntries(['primary','backup'].map((n,i)=>[n+'-inspect',{replicationIp:ips[i]}]))};
  const view={ready:true,standbyEligible:true,owner,epoch:9,auto:true,recovery:{enabled:true},preference:{node:owner,enabled:true}};
  const input={id:crypto.randomUUID(),bundle:base.bundle,joinHash:await digest(code),token:'witness_token_12345678901234567890'};
  return {base,view,input,e:createWitness(input,base,view,1000),ips};
}
async function join(f,extra={}) {return joinWitness(f.e,{code,bundle:f.base.bundle,ip:f.ips[2],claim:'witness_claim_12345678901234567890',...extra},'https://test.example.workers.dev',1100);}

test('witness is optional, requires completed healthy init/join and cannot replace an existing quorum',async()=>{
  const f=await fixture();assert.equal(witnessCommand(null,'primary'),null);
  for(const view of [{...f.view,ready:false},{...f.view,pending:{}},{...f.view,quorumControl:{clusterId:crypto.randomUUID()}}])assert.throws(()=>createWitness(f.input,f.base,view,1000));
  assert.throws(()=>createWitness(f.input,{...f.base,status:'running'},f.view,1000));
});
test('LAN/Tailscale validation rejects public, loopback, IPv6 and ambiguous address text',()=>{
  for(const mode of ['lan','tailscale','other'])for(const ip of ['127.0.0.1','0.0.0.0','8.8.8.8','::1','192.168.001.1','999.1.1.1'])assert.equal(privateIP(ip,mode),false);
  assert.equal(privateIP('192.168.1.1','lan'),true);assert.equal(privateIP('100.64.0.1','lan'),false);
  assert.equal(privateIP('100.127.255.1','tailscale'),true);assert.equal(privateIP('192.168.1.1','tailscale'),false);
});
test('witness code is expiring, bundle-bound and permanently binds claim and address',async()=>{
  const f=await fixture();
  await assert.rejects(join(f,{ip:f.ips[0]}));await assert.rejects(join(f,{bundle:'b'.repeat(64)}));
  const first=await join(f);assert.deepEqual(await join(f),first);
  await assert.rejects(join(f,{claim:'another_claim_12345678901234567890'}));
  await assert.rejects(join(f,{ip:'192.168.1.40'}));
  await assert.rejects(joinWitness(f.e,{code,bundle:f.base.bundle,ip:f.ips[2],claim:'witness_claim_12345678901234567890'},first.controller,f.e.expiresAt+1));
  assert.equal(witnessAuthorized(f.e,f.base,'witness',first.token),true);
  assert.equal(witnessAuthorized(f.e,f.base,'primary',first.token),false);
  assert.equal(witnessAuthorized(f.e,f.base,'backup',f.base.nodes.primary.token),false);
});
for(const mode of ['lan','tailscale'])for(const owner of ['primary','backup'])test(`${mode}: fixed witness plan preserves ${owner} as active and gates fallback after grant checks`,async()=>{
  const f=await fixture(mode,owner);await join(f);
  const order=steps(f.e);assert.ok(order.findIndex(x=>x[1]==='policy')>order.findLastIndex(x=>x[1]==='grant-check'));
  const enforced=order.filter(x=>x[1]==='enforce').map(x=>x[0]);assert.equal(enforced.at(-1),owner);
  while(f.e.index<order.length) {
    const [node,action]=order[f.e.index];
    if(node==='controller'){f.e.index++;continue;}
    const command=witnessCommand(f.e,node);assert.equal(command.args.epoch,9);assert.equal(command.args.owner,owner);
    assert.equal(witnessCommand(f.e,node==='witness'?'backup':'witness'),null);
    let result={verified:true};
    if(action==='inspect')result={...result,ip:f.e.addresses[node],machineId:'machine-'+node,existingQuorum:false,busy:false,freeBytes:3*1024**3,memoryBytes:1024**3,systemId:'12345',inRecovery:node!==owner,receiverConnected:true};
    if(action==='pki')result.certificates=Object.fromEntries(['primary','backup','witness'].map(n=>[n,Object.fromEntries(['client-ca.crt','peer-ca.crt','peer.crt','peer.key','server.crt','server.key','probe.crt','probe.key'].map(k=>[k,n+'-'+k]))]));
    if(action==='install') {
      assert.equal(Object.values(command.args.certificates).every(v=>v.startsWith(node+'-')),true);
      assert.equal(JSON.stringify(command).includes('-ca.key'),false);
    }
    witnessResult(f.e,node,{id:command.id,status:'succeeded',result},1200);
  }
  const view=JSON.stringify(publicWitness(f.e));
  for(const secret of [code,f.input.token,f.input.joinHash,'primary-peer.key','witness-server.key'])assert.equal(view.includes(secret),false);
});
test('uncertain failure or authority change retains stage and stops further commands',async()=>{
  const f=await fixture();await join(f);const c=witnessCommand(f.e,'primary');
  witnessResult(f.e,'primary',{id:'stale-id',status:'succeeded',result:{verified:true}},1200);assert.equal(f.e.index,0);
  witnessResult(f.e,'primary',{id:c.id,status:'failed',error:'interrupted'},1201);
  assert.equal(witnessCommand(f.e,'primary'),null);assert.equal(f.e.status,'failed');
  const other=await fixture();await join(other);extensionStable(other.e,{...other.view,epoch:10});
  assert.equal(other.e.status,'failed');assert.equal(other.e.index,0);
});
