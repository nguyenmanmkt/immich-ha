// Bootstrap uses fixed local actions, not SSH, shell text or arbitrary Python.
export const SETUP_STEPS = [
  ['primary','inspect','Kiểm tra node A'],['backup','inspect','Kiểm tra node B'],
  ['primary','base','Cài agent A'],['backup','base','Cài agent B'],
  ['primary','pair','Ghép Syncthing A'],['backup','pair','Ghép Syncthing B'],
  ['primary','sync','Đồng bộ ảnh A'],['backup','sync','Đồng bộ ảnh B'],
  ['primary','runtime','Cài runtime A'],['backup','runtime','Cài runtime B'],
  ['primary','enforce','Bật gate A'],['backup','enforce','Bật gate B'],
  ['primary','stage','Tạo PostgreSQL HA nguồn'],['primary','copy','Sao chép database ban đầu'],
  ['backup','stage','Tạo PostgreSQL replica'],['backup','stage-check','Kiểm tra replica'],
  ['backup','save','Giữ cấu hình B'],['primary','save','Giữ cấu hình A'],
  ['primary','freeze-copy','Tạm dừng ghi và chốt database'],
  ['primary','sync-final','Chốt đồng bộ ảnh A'],['backup','verify-final','Xác nhận WAL và ảnh B'],
  ['backup','enable','Bật streaming B'],['primary','enable','Bật streaming A'],
  ['primary','native-backups','Đồng bộ SQL backup A'],['backup','native-backups','Đồng bộ SQL backup B'],
  ['backup','start','Khởi động agent B'],['primary','start','Khởi động agent A'],
];
const ids=['primary','backup'];
const text=(v,max=256)=>typeof v==='string' && v.length>0 && v.length<=max;
function validIP(value,mode) {
  if(typeof value!=='string' || !/^\d{1,3}(\.\d{1,3}){3}$/.test(value))return false;
  const p=value.split('.').map(Number);if(p.some(x=>x>255))return false;
  return mode==='tailscale'?p[0]===100 && p[1]>=64 && p[1]<=127:
    p[0]===10 || p[0]===172 && p[1]>=16 && p[1]<=31 || p[0]===192 && p[1]===168;
}
export async function digest(value) {
  return [...new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(value)))].map(x=>x.toString(16).padStart(2,'0')).join('');
}
export function safeEqual(a,b) {
  if (typeof a!=='string' || typeof b!=='string' || a.length!==b.length) return false;
  let difference=0;for(let i=0;i<a.length;i++)difference|=a.charCodeAt(i)^b.charCodeAt(i);return difference===0;
}
export function createSetup(input,now,state) {
  if (state.epoch!==1 || state.owner!=='primary' || Object.keys(state.heartbeats).length || state.operation || state.pending) throw new Error('New unused deployment required');
  if (!/^[a-f0-9]{64}$/.test(input.bundle || '') || !/^[a-f0-9]{64}$/.test(input.joinHash || '') || !/^[a-f0-9]{8}(-[a-f0-9]{4}){3}-[a-f0-9]{12}$/.test(input.id || '')) throw new Error('Invalid installer identity');
  for (const node of ids) if (!text(input.nodes?.[node]?.token,100) || input.nodes[node].token.length<32 || input.nodes[node].config?.node!==node || !text(input.nodes[node].tunnelToken,8192)) throw new Error('Invalid node enrollment');
  if (![input.appPassword,input.replicationPassword].every(v=>text(v,128) && v.length>=32)) throw new Error('Missing bootstrap database credentials');
  state.auto=false;state.autoRequested=false;state.recovery={enabled:false,status:'disabled'};
  if(!['lan','tailscale'].includes(input.networkMode))throw new Error('Choose LAN or Tailscale');
  return {id:input.id,bundle:input.bundle,networkMode:input.networkMode,joinHash:input.joinHash,expiresAt:now+30*60000,
    nodes:structuredClone(input.nodes),appPassword:input.appPassword,replicationPassword:input.replicationPassword,
    index:0,status:'waiting-node',results:{},createdAt:now,attempt:0};
}
export async function enroll(setup,input,now) {
  if (!setup || setup.status==='completed' || setup.expiresAt<now || input.bundle!==setup.bundle ||
      !text(input.code,128) || !safeEqual(await digest(input.code),setup.joinHash) || !text(input.claim,128) || input.claim.length<32) throw new Error('Join code invalid, expired or installer version differs');
  const node=setup.nodes.backup;const claim=await digest(input.claim);
  if (node.claim && !safeEqual(node.claim,claim)) throw new Error('Join code already used by another node');
  node.claim=claim;node.joinedAt??=now;
  return {id:setup.id,bundle:setup.bundle,node:'backup',token:node.token,controller:node.config.controller_url,networkMode:setup.networkMode};
}
export function nodeAuthorized(setup,node,token) {return !!setup && ids.includes(node) && safeEqual(setup.nodes[node].token,token) && (node==='primary' || !!setup.nodes[node].claim);}
export function command(setup,node) {
  if (!setup || setup.status==='completed' || setup.status==='failed') return null;
  const step=SETUP_STEPS[setup.index];if (!step || step[0]!==node || (node==='backup' && !setup.nodes.backup.claim))return null;
  const peer=node==='primary'?'backup':'primary';const own=setup.nodes[node];const results=setup.results;
  const inspect=results[node+'-inspect'];const other=results[peer+'-inspect'];
  const args={node,setupId:setup.id,epoch:1,networkMode:setup.networkMode};
  if(step[1]==='base')Object.assign(args,{config:{...own.config,ip_address:inspect.ip},tunnelToken:own.tunnelToken});
  if(['pair','sync','sync-final','verify-final'].includes(step[1]))Object.assign(args,{peerIp:other.replicationIp,peerDeviceId:results[peer+'-base']?.deviceId});
  if(step[1]==='stage')Object.assign(args,{config:{enabled:false,node,peer,network_mode:setup.networkMode,peer_ip:other.replicationIp,local_ip:inspect.replicationIp,
    port:5433,database:'immich_stream',source:node==='primary',app_superuser:results['primary-inspect'].superuser,
    app_password:setup.appPassword,replication_password:setup.replicationPassword,role_password:setup.replicationPassword}});
  if(step[1]==='verify-final')args.barrier=results['primary-freeze-copy'];
  return {id:setup.id+'-'+setup.index+'-'+setup.attempt,action:step[1],args};
}
export function acceptResult(setup,node,receipt,now) {
  const expected=command(setup,node);
  if (!expected || receipt.id!==expected.id)return;
  if(receipt.status==='failed') {setup.status='failed';setup.error=String(receipt.error || 'Node setup failed').slice(0,300);return;}
  if(receipt.status!=='succeeded' || !receipt.result || typeof receipt.result!=='object')throw new Error('Invalid setup receipt');
  const value=receipt.result;
  const checks={base:()=>/^[A-Z2-7]{7}(-[A-Z2-7]{7}){7}$/.test(value.deviceId || ''),pair:()=>value.paired===true,
    sync:()=>value.synced===true,'sync-final':()=>value.synced===true,runtime:()=>value.installed===true,enforce:()=>value.enforcing===true,
    stage:()=>value.staged===true && value.postgres?.inRecovery===(node==='backup'),copy:()=>value.copied===true,
    'stage-check':()=>value.inRecovery===true && value.receiverConnected===true && value.pendingBytes===0,
    save:()=>text(value.saved,512),'freeze-copy':()=>value.copied===true && /^[0-9A-F]+\/[0-9A-F]+$/.test(value.lsn || '') && /^[a-f0-9]{64}$/.test(value.media?.digest || '') && /^\d+$/.test(value.systemId || ''),
    'verify-final':()=>value.verified===true,enable:()=>value.enabled===true && value.systemId===setup.results['primary-freeze-copy']?.systemId,'native-backups':()=>value.configured===true,start:()=>value.started===true};
  if(checks[expected.action] && !checks[expected.action]())throw new Error('Setup step did not produce the required verification receipt');
  if(expected.action==='inspect') {
    if(value.existingHa || !value.mediaMounted || !validIP(value.replicationIp,setup.networkMode) || value.ip!==value.replicationIp || !/^16\./.test(value.postgres || '') || !Number.isFinite(value.rootFree) || value.rootFree<3*1024**3)throw new Error('Node preflight failed');
    if(node==='backup') {
      const source=setup.results['primary-inspect'];
      if(value.assets!==0 || value.mediaFiles!==0 || value.version!==source.version || value.postgres!==source.postgres || JSON.stringify(value.extensions)!==JSON.stringify(source.extensions) || value.replicationIp===source.replicationIp || value.machineId===source.machineId || value.mediaFree<source.mediaBytes+2*1024**3)throw new Error('Backup must be empty, compatible, distinct and have sufficient storage');
    }
  }
  setup.results[node+'-'+expected.action]=value;setup.index++;setup.updatedAt=now;setup.status='running';setup.error=null;
}
export function publicSetup(setup) {
  if(!setup)return null;
  return {id:setup.id,status:setup.status,index:setup.index,total:SETUP_STEPS.length,expiresAt:setup.expiresAt,controller:setup.nodes.primary.config.controller_url,
    label:SETUP_STEPS[setup.index]?.[2] || (setup.status==='completed'?'HA sẵn sàng':'Chờ xác nhận hai node'),
    error:setup.error || null,joined:!!setup.nodes.backup.claim,
    nodes:Object.fromEntries(ids.map(n=>[n,{ip:setup.results[n+'-inspect']?.ip,tailscaleIp:setup.results[n+'-inspect']?.tailscaleIp}]))};
}
