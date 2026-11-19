import test from "node:test";
import assert from "node:assert/strict";
import { initialState, heartbeat, recordProbe, admin, receiveResult, route, advance, LIMITS } from "../src/controller.mjs";

function harness(owner = "primary", epoch = 1, streaming = false, fast = false) {
  const s = initialState();
  Object.assign(s, { owner, epoch });
  let now = 100_000;
  function tick() {
    for (const node of ["primary", "backup"]) {
      recordProbe(s, node, true, now);
      heartbeat(s, node, { session: node + "_session_12345678", epoch: s.epoch, ready: true,
        localHealthy: true, enforcing: true, canPromote: true, protocolVersion: 2,
        metrics: { runtimeV2: true, appVersion: "3.4.1", postgresVersion: "16.16",
          ...(streaming ? { postgresStreaming: { mode: "streaming", handoverVersion: 2, fastHandoverVersion:fast?1:0, inRecovery: node !== s.owner } } : {}),
          seed: { sourceEpoch: s.epoch }, syncthing: { deviceId: node + "-device" } } }, now);
    }
    now++;
  }
  tick(); tick();
  function start(action = "switch") {
    admin(s, { action, target: owner === "primary" ? "backup" : "primary", actor: "admin@example.net",
      id: "12345678-1234-1234-1234-123456789abc", requestId: "request-1" }, now++);
  }
  function ack(extra = {}) {
    const c = s.operation.command;
    assert.ok(c);
    const result = ["wal-barrier", "freeze-wal"].includes(c.action) ? {
        kind:"wal-manifest-v1",commandId:c.id,jobId:c.jobId,source:c.source,epoch:c.epoch,
        manifestName:`wal-${c.id}.manifest.json.gz`,manifestSha256:"b".repeat(64),walLsn:"0/ABC",systemId:"123",timeline:5,
        files:10,bytes:1000,stopped:c.action==="freeze-wal",peerAcknowledged:c.action==="freeze-wal",
        ...(fast?{fenceProtocol:1,appStopped:true,postgresStopped:true,originFenced:true,frozenDurable:true}:{})
      } : c.action === "sync-wal" ? {manifestVerified:true,systemId:"123",timeline:5,replayLsn:"0/ABC",filesVerified:10}
      : ["dump", "freeze-dump"].includes(c.action)
      ? { name: "immich-20261117-120000.dump", sha256: "a".repeat(64), manifestSha256: "b".repeat(64),
        manifestName: "immich-20261117-120000.dump.manifest.json.gz", source: c.source, epoch: c.epoch,
        stopped: c.action === "freeze-dump", ...(streaming ? { walLsn: "0/ABC", systemId: "123" } : {}) }
      : { healthy: true, seedEpoch:c.epoch };
    const payload = { jobId: c.jobId, commandId: c.id, index: c.index, status: "succeeded", result, ...extra };
    receiveResult(s, c.node, payload, now++);
    return { node: c.node, payload };
  }
  return { s, tick, start, ack, get now() { return now; }, set now(value) { now = value; } };
}

function fastFixture(owner='primary') {
  const h=harness(owner,7,true,true);h.start();
  while(h.s.operation.command)h.ack();
  const source=h.s.heartbeats[owner];source.at=h.now;source.localHealthy=false;source.metrics.appMode='frozen';
  return h;
}

for(const owner of ['primary','backup'])test(`verified ${owner} source can hand over before the 30 second margin`,()=>{
  const h=fastFixture(owner);
  assert.ok(h.now<h.s.leaseUntil+LIMITS.fenceMs);
  advance(h.s,h.now);
  assert.notEqual(h.s.owner,owner);assert.equal(h.s.epoch,8);
  assert.equal(h.s.lastTakeover.fastHandover,true);
  assert.equal(route(h.s,h.now),null); // New app must acknowledge readiness first.
  h.tick();h.tick();
  while(h.s.operation.command)h.ack();
  assert.equal(h.s.operation.status,'succeeded');
  assert.notEqual(h.s.owner,owner);
});

test('missing, stale or mismatched fence proof keeps the normal margin',()=>{
  for(const fault of ['no-capability','no-proof','app-running','pg-running','gate-open','not-durable','stale-heartbeat','old-heartbeat','healthy-source','old-epoch','bad-manifest','bad-timeline','behind-wal','unchecked-target']) {
    const h=fastFixture(),s=h.s,f=s.operation.results[6],source=s.heartbeats.primary;
    if(fault==='no-capability')s.operation.fastHandover=false;
    if(fault==='no-proof')delete f.fenceProtocol;
    if(fault==='app-running')f.appStopped=false;
    if(fault==='pg-running')f.postgresStopped=false;
    if(fault==='gate-open')f.originFenced=false;
    if(fault==='not-durable')f.frozenDurable=false;
    if(fault==='stale-heartbeat'){h.now+=16000;}
    if(fault==='old-heartbeat')source.at=s.operation.resultTimes[6]-1;
    if(fault==='healthy-source')source.localHealthy=true;
    if(fault==='old-epoch')f.epoch=6;
    if(fault==='bad-manifest')s.operation.results[8].manifestVerified=false;
    if(fault==='bad-timeline')s.operation.results[8].timeline=4;
    if(fault==='behind-wal')s.operation.results[8].replayLsn='0/ABB';
    if(fault==='unchecked-target')s.operation.results[9].healthy=false;
    advance(s,h.now);assert.equal(s.owner,'primary',fault);
    advance(s,s.leaseUntil+LIMITS.fenceMs-1);assert.equal(s.owner,'primary',fault);
  }
});

test('fast-capable runtimes never bypass expiry for an unplanned/manual takeover',()=>{
  const h=harness('primary',7,true,true);h.s.auto=true;
  admin(h.s,{action:'promote-backup'},h.now);
  advance(h.s,h.now);assert.equal(h.s.owner,'primary');
  advance(h.s,h.s.leaseUntil+LIMITS.fenceMs-1);assert.equal(h.s.owner,'primary');
});

for (const original of ["primary", "backup"]) for (const streaming of [false, true]) {
  test(`planned ${streaming ? "streaming" : "dump"} handover from ${original} waits for fencing, then reseeds the old owner`, () => {
    const h = harness(original, original === "primary" ? 1 : 3, streaming);
    const initialEpoch = h.s.epoch;
    h.s.auto = true;
    h.start();
    assert.equal(h.s.auto, false);
    const actions = [];
    while (h.s.operation.command) {
      actions.push(h.s.operation.command.action);
      h.ack();
    }
    assert.deepEqual(actions, streaming ? ["archive","receive","send","wal-barrier","sync-wal","validate","freeze-wal","wait-peer","sync-wal","validate"] : ["archive", "receive", "send", "dump", "sync-restore", "validate", "freeze-dump", "wait-peer", "sync-restore", "validate"]);
    assert.equal(h.s.owner, original);
    assert.equal(route(h.s, h.now), null);
    assert.ok(h.s.pending);
    const deadline = h.s.leaseUntil + LIMITS.fenceMs;
    advance(h.s, deadline - 1);
    assert.equal(h.s.owner, original);
    h.now = deadline;
    h.tick(); h.tick();
    assert.notEqual(h.s.owner, original);
    assert.equal(h.s.epoch, initialEpoch + 1);
    assert.ok(route(h.s, h.now));
    const reverse = [];
    while (h.s.operation.command) {
      reverse.push([h.s.operation.command.node, h.s.operation.command.action]);
      h.ack();
    }
    assert.equal(h.s.operation.status, "succeeded");
    assert.deepEqual(reverse.map((x) => x[1]), streaming ? ["archive","receive","send","wal-barrier","sync-wal","validate"] : ["archive", "receive", "send", "dump", "sync-restore", "validate"]);
    assert.equal(reverse[0][0], original);
    assert.equal(reverse[2][0], h.s.owner);
    assert.equal(h.s.auto, true);
  });
}

test("mixed database modes cannot start a handover", () => {
  const h = harness();
  h.s.heartbeats.primary.metrics.postgresStreaming = {mode:"streaming"};
  assert.throws(() => h.start(), /khác chế độ PostgreSQL/);
});

test("streaming receipt cannot advance without a valid WAL barrier", () => {
  const h = harness("primary",7,true);h.start("prepare");
  while(h.s.operation.command.action !== "wal-barrier")h.ack();
  assert.throws(() => h.ack({result:{name:"immich-20261117-120000.dump",sha256:"a".repeat(64),manifestSha256:"b".repeat(64),
    manifestName:"immich-20261117-120000.dump.manifest.json.gz",source:"primary",epoch:7}}), /WAL/);
  assert.equal(h.s.operation.command.action,"wal-barrier");
});

test("WAL handover requires upgraded runtimes on both physical nodes", () => {
  const h=harness('primary',7,true);delete h.s.heartbeats.backup.metrics.postgresStreaming.handoverVersion;
  assert.throws(()=>h.start(),/cả hai máy/);
});

test("wrong WAL lineage, missing files and replay behind barrier cannot advance", () => {
  for (const fault of [{systemId:'999'},{timeline:4},{manifestVerified:false},{filesVerified:9},{replayLsn:'0/ABB'}]) {
    const h=harness('primary',7,true);h.start('prepare');
    while(h.s.operation.command.action!=='sync-wal')h.ack();
    assert.throws(()=>h.ack({result:{manifestVerified:true,systemId:'123',timeline:5,replayLsn:'0/ABC',filesVerified:10,...fault}}));
    assert.equal(h.s.operation.command.action,'sync-wal');
  }
});

test("WAL freeze failure stays closed without restoring an older database", () => {
  const h=harness('backup',9,true);h.start();
  while(h.s.operation.command.action!=='freeze-wal')h.ack();
  h.ack({status:'failed',error:'Peer did not acknowledge final WAL'});
  assert.equal(h.s.operation.maintenance,true);assert.equal(h.s.operation.status,'failed');
  assert.equal(h.s.owner,'backup');assert.equal(h.s.auto,false);assert.equal(route(h.s,h.now),null);
});

test("duplicate acknowledgements do not execute or skip another step", () => {
  const h = harness(); h.start("prepare");
  const receipt = h.ack();
  const index = h.s.operation.index;
  receiveResult(h.s, receipt.node, receipt.payload, h.now);
  assert.equal(h.s.operation.index, index);
  assert.throws(() => receiveResult(h.s, "primary", receipt.payload, h.now));
});

test("failure after freezing stays closed; explicit resume is required", () => {
  const h = harness(); h.start();
  while (h.s.operation.command.action !== "freeze-dump") h.ack();
  h.ack({ status: "failed", error: "Disk full" });
  assert.equal(h.s.operation.status, "failed");
  assert.equal(h.s.operation.maintenance, true);
  assert.equal(route(h.s, h.now), null);
  assert.equal(h.s.auto, false);
  admin(h.s, { action: "resume", id: "22345678-1234-1234-1234-123456789abc" }, h.now);
  assert.equal(h.s.operation.command.action, "resume");
  h.ack(); h.tick();
  assert.equal(h.s.operation.status, "succeeded");
  assert.equal(h.s.owner, "primary");
});

test("snapshot receipts from another source/epoch cannot advance the operation", () => {
  const h = harness(); h.start("prepare");
  while (h.s.operation.command.action !== "dump") h.ack();
  assert.throws(() => h.ack({ result: { name: "immich-20261117-120000.dump", sha256: "a".repeat(64),
    manifestSha256: "b".repeat(64), manifestName: "immich-20261117-120000.dump.manifest.json.gz", source: "backup", epoch: 0 } }));
  assert.equal(h.s.operation.command.action, "dump");
});

test("healthy current backup owner does not accumulate a primary-failure timer", () => {
  const h = harness("backup", 2);
  h.s.probes.primary.ok = false;
  advance(h.s, h.now);
  assert.equal(h.s.failureSince, null);
});
