#!/usr/bin/env python3
"""Bounded isolated-quorum fault test; restores witness even after a failed check."""
import json
from pathlib import Path
import sys
import time
import uuid
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import deployment as d
import quorum_setup as q
import quorum_stage as stage
from local_env import load_env

WITNESS=r'''
import json,re,socket,subprocess
from pathlib import Path
marker=json.loads(Path('/etc/immich-ha-quorum/installation.json').read_text())
if marker['node']!='witness' or marker['clusterId']!=INPUT['clusterId'] or marker['grantIntegrationEnabled']:raise RuntimeError('Only isolated witness may be stopped')
unit=INPUT['restoreUnit']
if not re.fullmatch('immich-ha-quorum-restore-[a-f0-9]{32}',unit):raise RuntimeError('Invalid restore timer identity')
if INPUT['action']=='stop':
 subprocess.run(['systemd-run','--unit='+unit,'--on-active=120s','/usr/bin/systemctl','start','immich-ha-quorum.service'],check=True,capture_output=True,timeout=20)
 subprocess.run(['systemctl','stop','immich-ha-quorum.service'],check=True,timeout=30)
if INPUT['action'] in ('stop','verify-stopped'):
 active=subprocess.run(['systemctl','is-active','--quiet','immich-ha-quorum.service']).returncode==0
 if active:raise RuntimeError('Witness was not stopped during the fault test')
 try:
  conn=socket.create_connection((INPUT['ip'],2379),timeout=2);conn.close()
 except OSError:pass
 else:raise RuntimeError('Witness still accepts connections')
elif INPUT['action']=='restore':
 subprocess.run(['systemctl','start','immich-ha-quorum.service'],check=True,timeout=30)
 subprocess.run(['systemctl','is-active','--quiet','immich-ha-quorum.service'],check=True)
 subprocess.run(['systemctl','stop',unit+'.timer'],capture_output=True,timeout=15)
else:raise RuntimeError('Unknown test action')
print(json.dumps({'action':INPUT['action'],'ok':True}))
'''


def main():
 plan=json.loads(q.PLAN.read_text());stage.validate_plan(plan)
 before=d.control()
 if not before['ready'] or not before['standbyEligible'] or before.get('pending') or before.get('operation',{}).get('status') not in (None,'succeeded','failed'):raise RuntimeError('Live HA must be idle and ready')
 report={'clusterId':plan['clusterId'],'grantIntegrationEnabled':False}
 endpoints={n:f'https://{plan["members"][n]["tailscaleIp"]}:2379' for n in q.NODES}
 def check(client,node,members=q.NODES):
  data={'clusterId':plan['clusterId'],'etcdctl':f'/opt/immich-ha-quorum/v{stage.VERSION}/etcdctl','endpoints':[endpoints[n] for n in members],'localEndpoint':endpoints[node]}
  until=time.monotonic()+40
  while True:
   try:
    value=d.remote(client,stage.CHECK,data,timeout=65);stage.validate_check(plan,value,members);return value
   except RuntimeError:
    if time.monotonic()>until:raise
    time.sleep(2)
 with q.connections(load_env()['SSH_COORDINATOR_HOST']) as (clients,first):
  for node in q.NODES:check(clients[node],node)
  print('Baseline: all three members healthy.',flush=True)
  control={'clusterId':plan['clusterId'],'restoreUnit':'immich-ha-quorum-restore-'+uuid.uuid4().hex,'ip':plan['members']['witness']['tailscaleIp']}
  smoke={'etcdctl':f'/opt/immich-ha-quorum/v{stage.VERSION}/etcdctl','endpoints':[endpoints[n] for n in ('primary','backup')],
         'key':'/immich-ha/'+plan['clusterId']+'/checks/'+uuid.uuid4().hex,'value':uuid.uuid4().hex}
  try:
   d.remote(clients['witness'],WITNESS,{**control,'action':'stop'})
   print('Witness quorum stopped; automatic restore armed for 120 seconds.',flush=True)
   report['survivors']={node:check(clients[node],node,('primary','backup')) for node in ('primary','backup')}
   d.remote(clients['primary'],stage.SMOKE,{**smoke,'action':'put'})
   for node in ('primary','backup'):d.remote(clients[node],stage.SMOKE,{**smoke,'action':'get'})
   d.remote(clients['witness'],WITNESS,{**control,'action':'verify-stopped'})
   report['readWriteWhileWitnessStopped']=True
   print('PASS: both survivors committed/read the test key while witness remained stopped.',flush=True)
  finally:
   d.remote(clients['witness'],WITNESS,{**control,'action':'restore'})
   print('Witness quorum restarted.',flush=True)
   d.remote(clients['primary'],stage.SMOKE,{**smoke,'action':'delete'})
  report['restored']={node:check(clients[node],node) for node in q.NODES}
 after=d.control();report['liveHA']={k:after[k] for k in ('owner','epoch','ready','standbyEligible','auto')}
 (q.PLAN.parent/'quorum-witness-test.json').write_text(json.dumps(report,indent=2)+'\n')
 if any(before[k]!=after[k] for k in ('owner','epoch','auto')) or not after['ready'] or not after['standbyEligible']:raise RuntimeError('Quorum passed; live HA changed and needs review')
 print('TEST OK: witness restored, all three members healthy; Immich owner/epoch unchanged.')


if __name__=='__main__':main()
