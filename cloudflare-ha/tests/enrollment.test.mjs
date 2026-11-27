import test from 'node:test';
import assert from 'node:assert/strict';
import { initialState } from '../src/controller.mjs';
import { createSetup, enroll, digest, command, acceptResult, publicSetup, nodeAuthorized,SETUP_STEPS } from '../src/enrollment.mjs';

async function fixture(mode='lan') {
  const input={id:crypto.randomUUID(),bundle:'a'.repeat(64),joinHash:await digest('join_code_example_12345678901234567890'),networkMode:mode,
    appPassword:'app_password_example_12345678901234567890',replicationPassword:'repl_password_example_12345678901234567890',
    nodes:Object.fromEntries(['primary','backup'].map(n=>[n,{token:n+'_bootstrap_token_example_12345678901234567890',tunnelToken:n+'_tunnel_token',config:{node:n,controller_url:'https://test.example.workers.dev'}}]))};
  return createSetup(input,1000,initialState());
}

test('join is bundle-bound, expires and can only be claimed by one joining node',async()=>{
  const s=await fixture();const request={code:'join_code_example_12345678901234567890',claim:'device_claim_example_12345678901234567890',bundle:s.bundle};
  const joined=await enroll(s,request,1100);
  assert.equal(joined.node,'backup');assert.equal(joined.networkMode,'lan');
  assert.deepEqual(await enroll(s,request,1200),joined,'same joining device can recover a lost response');
  await assert.rejects(enroll(s,{...request,claim:'different_device_example_12345678901234567890'},1300));
  await assert.rejects(enroll(s,{...request,bundle:'b'.repeat(64)},1300));
  await assert.rejects(enroll(s,request,s.expiresAt+1));
  assert.equal(nodeAuthorized(s,'backup',joined.token),true);
  assert.equal(nodeAuthorized(s,'primary',joined.token),false);
  const view=JSON.stringify(publicSetup(s));
  for(const secret of [s.appPassword,s.replicationPassword,joined.token,s.nodes.backup.tunnelToken])assert.equal(view.includes(secret),false);
});

test('preflight rejects populated backup and invalid peer addresses before installing either node',async()=>{
  const s=await fixture();s.nodes.backup.claim='claimed';
  const inspect={ip:'192.168.1.10',replicationIp:'192.168.1.10',machineId:'machine-a',mediaMounted:true,rootFree:10*1024**3,
    mediaFree:100*1024**3,mediaBytes:10,mediaFiles:2,assets:2,version:'3.4.2',postgres:'16.16',extensions:['vchord:0.5']};
  acceptResult(s,'primary',{id:command(s,'primary').id,status:'succeeded',result:inspect},1001);
  assert.equal(command(s,'primary'),null);
  const id=command(s,'backup').id;
  assert.throws(()=>acceptResult(s,'backup',{id,status:'succeeded',result:{...inspect,ip:'192.168.1.20',replicationIp:'192.168.1.20',machineId:'machine-b',assets:1}},1002));
  assert.equal(s.index,1);
  assert.throws(()=>acceptResult(s,'backup',{id,status:'succeeded',result:{...inspect,ip:'8.8.8.8',replicationIp:'8.8.8.8',machineId:'machine-b',assets:0,mediaFiles:0}},1002));
  acceptResult(s,'backup',{id,status:'succeeded',result:{...inspect,ip:'192.168.1.20',replicationIp:'192.168.1.20',machineId:'machine-b',assets:0,mediaFiles:0}},1002);
  assert.equal(command(s,'primary').action,'base');
});

test('cannot initialize an existing coordinator or request shell execution',async()=>{
  const s=await fixture();
  const state=initialState();state.epoch=2;
  assert.throws(()=>createSetup(s,2000,state));
  assert.equal(Object.hasOwn(command(s,'primary'),'shell'),false);
  assert.equal(command(s,'backup'),null);
});

test('LAN setup executes the complete fixed plan without Tailscale metadata or SSH tasks',async()=>{
  const s=await fixture();s.nodes.backup.claim='claimed';
  const inspection=n=>({ip:n==='primary'?'192.168.1.10':'192.168.1.20',replicationIp:n==='primary'?'192.168.1.10':'192.168.1.20',
    machineId:n,mediaMounted:true,rootFree:10*1024**3,mediaFree:100*1024**3,mediaBytes:0,mediaFiles:0,assets:0,version:'3.4.2',postgres:'16.16',extensions:[]});
  const media={digest:'f'.repeat(64),files:0,bytes:0};
  while(s.index<SETUP_STEPS.length) {
    const node=SETUP_STEPS[s.index][0];const cmd=command(s,node);
    const replies={inspect:inspection(node),base:{deviceId:Array(8).fill(node==='primary'?'AAAAAAA':'BBBBBBB').join('-')},pair:{paired:true},
      sync:{synced:true,media},'sync-final':{synced:true,media},runtime:{installed:true},enforce:{enforcing:true},
      stage:{staged:true,postgres:{inRecovery:node==='backup'}},copy:{copied:true},'stage-check':{inRecovery:true,receiverConnected:true,pendingBytes:0},
      save:{saved:'/etc/immich-ha/streaming-rollback/example'},'freeze-copy':{copied:true,lsn:'0/ABC',systemId:'123',media},
      'verify-final':{verified:true},enable:{enabled:true,systemId:'123'},'native-backups':{configured:true},start:{started:true}};
    if(cmd.action==='stage') {assert.equal(cmd.args.config.network_mode,'lan');assert.ok(cmd.args.config.peer_ip.startsWith('192.168.1.'));}
    assert.ok(!/ssh|shell/.test(cmd.action));
    acceptResult(s,node,{id:cmd.id,status:'succeeded',result:replies[cmd.action]},2000+s.index);
  }
  assert.equal(s.index,SETUP_STEPS.length);assert.equal(command(s,'primary'),null);
  assert.notEqual(s.status,'completed','authority/readiness must still be acknowledged by the live agents');
});
