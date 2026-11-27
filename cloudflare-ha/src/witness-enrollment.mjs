// Optional extension for an init/join installation. No executable payloads.
import { digest, safeEqual, nodeAuthorized } from './enrollment.mjs';
const nodes=['primary','backup','witness'];
const uuid=/^[a-f0-9]{8}(-[a-f0-9]{4}){3}-[a-f0-9]{12}$/;
export function privateIP(ip,mode) {
  if(typeof ip!=='string' || !/^\d{1,3}(\.\d{1,3}){3}$/.test(ip))return false;
  const a=ip.split('.').map(Number);if(a.some(x=>x>255) || a.join('.')!==ip)return false;
  return mode==='tailscale'?a[0]===100 && a[1]>=64 && a[1]<=127:mode==='lan' && (a[0]===10 || a[0]===172 && a[1]>=16 && a[1]<=31 || a[0]===192 && a[1]===168);
}
export function healthy(view) {
  return view.ready && view.standbyEligible && !view.pending && [undefined,null,'succeeded','failed'].includes(view.operation?.status) && !view.immichUpgrade?.applying;
}
export function steps(e) {
  const peer=e.owner==='primary'?'backup':'primary';
  return [...nodes.map(n=>[n,'inspect']),['primary','pki'],...nodes.map(n=>[n,'install']),...nodes.map(n=>[n,'start']),
    ...nodes.map(n=>[n,'check']),['witness','enforce'],[peer,'enforce'],[e.owner,'enforce'],
    ...nodes.map(n=>[n,'grant-check']),['controller','policy'],[peer,'fallback'],[e.owner,'fallback'],['witness','fallback-check'],
    ['primary','gateway'],['backup','gateway'],['witness','router'],['witness','gateway-check']];
}
export function createWitness(input,base,view,now) {
  if(!base || base.status!=='completed' || !healthy(view) || view.quorumControl?.clusterId)throw new Error('Completed healthy two-node init/join installation without quorum required');
  if(!uuid.test(input.id || '') || !/^[a-f0-9]{64}$/.test(input.joinHash || '') || input.bundle!==base.bundle || typeof input.token!=='string' || input.token.length<32 || input.token.length>100)throw new Error('Invalid witness enrollment identity');
  const addresses=Object.fromEntries(['primary','backup'].map(n=>[n,base.results[n+'-inspect'].replicationIp]));
  if(!Object.values(addresses).every(ip=>privateIP(ip,base.networkMode)))throw new Error('Invalid base network');
  return {id:input.id,clusterId:input.id,bundle:base.bundle,networkMode:base.networkMode,joinHash:input.joinHash,token:input.token,
    expiresAt:now+30*60000,status:'waiting-witness',index:0,attempt:0,addresses,owner:view.owner,epoch:view.epoch,
    saved:{auto:view.autoRequested ?? view.auto,recovery:view.recovery.enabled,preference:view.preference},results:{}};
}
export async function joinWitness(e,input,controller,now) {
  if(!e || e.status==='completed' || now>e.expiresAt || input.bundle!==e.bundle || !safeEqual(await digest(input.code || ''),e.joinHash) || !privateIP(input.ip,e.networkMode) || Object.values(e.addresses).filter((_,i)=>i<2).includes(input.ip) || typeof input.claim!=='string' || input.claim.length<32 || input.claim.length>128)throw new Error('Invalid, expired or incompatible witness join');
  const claim=await digest(input.claim);
  if(e.claim && (!safeEqual(e.claim,claim) || e.addresses.witness!==input.ip))throw new Error('Witness already claimed by another device');
  e.claim=claim;e.addresses.witness=input.ip;
  if(e.status==='waiting-witness')e.status='running';
  return {id:e.id,node:'witness',bundle:e.bundle,controller,token:e.token,networkMode:e.networkMode,ip:input.ip};
}
export function witnessAuthorized(e,base,node,token) {
  return node==='witness'?!!e?.claim && safeEqual(e.token,token):nodeAuthorized(base,node,token);
}
export function witnessPlan(e) {
  return {version:1,engine:'etcd',networkMode:e.networkMode,clusterId:e.clusterId,grantIntegrationEnabled:false,funnelEnabled:false,
    members:Object.fromEntries(nodes.map(n=>[n,{ip:e.addresses[n],clientPort:2379,peerPort:2380}]))};
}
export function extensionStable(e,view) {
  if(e && e.status!=='completed' && (e.owner!==view.owner || e.epoch!==view.epoch || view.pending || ![undefined,null,'succeeded','failed'].includes(view.operation?.status))) {
    e.status='failed';e.error='Authority/job changed during witness installation; retained all files and disabled automation';
  }
}
export function witnessCommand(e,node) {
  if(!e || e.status!=='running' || !e.claim)return null;
  const step=steps(e)[e.index];if(!step || step[0]!==node)return null;
  const args={node,setupId:e.id,networkMode:e.networkMode,plan:witnessPlan(e),owner:e.owner,epoch:e.epoch};
  if(step[1]==='install')args.certificates=e.results['primary-pki'].certificates[node];
  return {id:e.id+'-'+e.index+'-'+e.attempt,action:'witness-'+step[1],args};
}
export function witnessResult(e,node,r,now) {
  const expected=witnessCommand(e,node);if(!expected || r.id!==expected.id)return;
  if(r.status==='failed'){e.status='failed';e.error=String(r.error || 'Local witness step failed').slice(0,200);return;}
  const value=r.result;
  if(r.status!=='succeeded' || value?.verified!==true)throw new Error('Missing local verification receipt');
  const action=steps(e)[e.index][1];
  if(action==='inspect') {
    if(value.ip!==e.addresses[node] || !value.machineId || value.existingQuorum || value.busy || value.freeBytes<1024**3 || value.memoryBytes<768*1024**2)throw new Error('Witness/quorum preflight failed');
    if(nodes.some(n=>e.results[n+'-inspect']?.machineId===value.machineId))throw new Error('Distinct machines required');
    if(node!=='witness' && (value.systemId!==e.results['primary-inspect']?.systemId && node!=='primary' || value.inRecovery!==(node!==e.owner) || node!==e.owner && !value.receiverConnected))throw new Error('Streaming roles/lineage not ready');
  }
  if(action==='pki') {
    if(Object.keys(value.certificates || {}).sort().join()!==[...nodes].sort().join())throw new Error('Missing node certificates');
    const names=['client-ca.crt','peer-ca.crt','peer.crt','peer.key','server.crt','server.key','probe.crt','probe.key'].sort().join();
    for(const n of nodes)if(Object.keys(value.certificates[n]).sort().join()!==names || Object.values(value.certificates[n]).some(v=>typeof v!=='string' || v.length>10000))throw new Error('Invalid scoped certificate bundle');
  }
  // Only the PKI step needs private outputs; other receipts store bounded facts.
  e.results[node+'-'+action]=action==='pki'?value:action==='inspect'?{...value}: {verified:true};
  e.index++;e.updatedAt=now;
}
export function publicWitness(e) {
  if(!e)return null;
  return {id:e.id,status:e.status,index:e.index,total:steps(e).length,step:steps(e)[e.index]?.[1] || 'completed',
    error:e.error || null,joined:!!e.claim,networkMode:e.networkMode,addresses:e.addresses,expiresAt:e.expiresAt,funnelIps:e.funnelIps || {}};
}
