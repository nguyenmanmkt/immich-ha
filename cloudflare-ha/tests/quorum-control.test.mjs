import test from 'node:test';
import assert from 'node:assert/strict';
import {initialState,heartbeat,admin,status,advance} from '../src/controller.mjs';

const clusterId='11111111-2222-4333-8444-555555555555';
const session='persistent_session_12345',term='a'.repeat(32);
function state(){const s=initialState();s.owner='backup';s.epoch=14;s.session=session;
  admin(s,{action:'set-quorum-policy',clusterId,enabled:true},100);return s;}
function payload(report,extra={}){return {session,epoch:report.epoch,localHealthy:true,enforcing:true,protocolVersion:2,
  quorumReport:{clusterId,live:true,localGrant:true,session,term,...report},...extra};}
function report(extra={}){return {owner:'backup',epoch:14,authority:'fallback',...extra};}

test('CF challenges fallback then adopts only the live writer with its committed nonce',()=>{
  const s=state();
  let answer=heartbeat(s,'backup',payload(report()),101);
  assert.equal(answer.role,'quorum-wait');assert.equal(s.quorumControl.mode,'fallback');
  const c=answer.quorum.challenge;assert.equal(c.targetEpoch,14);
  answer=heartbeat(s,'backup',payload(report({authority:'handoff',handoffNonce:'incorrect'})),102);
  assert.equal(answer.role,'quorum-wait');
  answer=heartbeat(s,'backup',payload(report({authority:'handoff',handoffNonce:c.nonce})),103);
  assert.equal(answer.role,'active');assert.equal(answer.quorum.ack,c.nonce);
  assert.equal(s.owner,'backup');assert.equal(s.epoch,14);assert.equal(s.quorumControl.mode,'recovering');
  heartbeat(s,'backup',payload(report({authority:'cloudflare'})),104);
  assert.equal(s.quorumControl.mode,'recovering'); // A stale CF report is insufficient.
  heartbeat(s,'backup',payload(report({authority:'cloudflare',cfAck:c.nonce}),{ready:true}),105);
  assert.equal(s.quorumControl.mode,'cloudflare');
  heartbeat(s,'primary',payload(report({authority:'handoff',handoffNonce:c.nonce,localGrant:false})),106);
  assert.equal(s.quorumControl.mode,'cloudflare'); // Delayed peer report cannot re-open reconciliation.
});

test('only the owner token can complete reconciliation; stale cluster/nonce are ignored',()=>{
  const s=state();
  let a=heartbeat(s,'primary',payload(report()),101);
  assert.equal(a.quorum.challenge,null);assert.equal(s.quorumControl.mode,'fallback');
  assert.equal(s.owner,'backup');
  a=heartbeat(s,'backup',payload(report({clusterId:'different'})),102);
  assert.equal(a.role,'quorum-wait');
  assert.equal(s.quorumControl.challenge,undefined);
});

test('a promoted fallback writer is adopted without reverting to the old active',()=>{
  const s=state();s.owner='primary';s.epoch=14;s.session='old_primary_session_123';
  let a=heartbeat(s,'backup',payload(report({epoch:15})),101);
  const c=a.quorum.challenge;assert.equal(c.targetEpoch,15);
  a=heartbeat(s,'backup',payload(report({epoch:15,authority:'handoff',handoffNonce:c.nonce})),102);
  assert.equal(s.owner,'backup');assert.equal(s.epoch,15);assert.equal(a.role,'active');
});

test('unadmitted newer CF epochs are bridged upward on the same actual writer',()=>{
  const s=state();s.owner='primary';s.epoch=20;
  const a=heartbeat(s,'backup',payload(report()),101);
  assert.equal(a.quorum.challenge.targetEpoch,21);
  assert.equal(s.owner,'primary'); // No adoption until quorum nonce is committed.
});

test('jobs and automatic changes are paused while reconciliation is outstanding',()=>{
  const s=state();s.auto=true;s.failureSince=1;
  const a=heartbeat(s,'backup',payload(report()),1000000);
  assert.equal(a.command,null);
  assert.throws(()=>admin(s,{action:'set-auto',enabled:false},1000001),/quorum/);
  advance(s,2000000);assert.equal(s.owner,'backup');assert.equal(s.pending,null);
  assert.equal(status(s,2000000).quorumFallbackProtocol,2);
});

test('policy sequencing is persistent and topology identity cannot be changed',()=>{
  const s=state();
  const a=heartbeat(s,'backup',{session,epoch:14,enforcing:true},101);
  const b=heartbeat(s,'primary',{session:'peer_session_12345678',epoch:14,enforcing:true},102);
  assert.ok(b.quorum.sequence>a.quorum.sequence);
  assert.throws(()=>admin(s,{action:'set-quorum-policy',clusterId:'22222222-2222-4333-8444-555555555555',enabled:true},103),/immutable/);
  assert.throws(()=>admin(s,{action:'set-quorum-policy',clusterId,enabled:false},103),/drain/);
});
