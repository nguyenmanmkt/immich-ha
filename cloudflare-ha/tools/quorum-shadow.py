#!/usr/bin/env python3
"""Checkpoint 3a: shadow lease tests/observer. Does not enable live fencing."""
import argparse
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

INSTALL=r'''
import hashlib,json,os,py_compile,shutil,subprocess
from pathlib import Path
marker=json.loads(Path('/etc/immich-ha-quorum/installation.json').read_text())
if marker['clusterId']!=INPUT['clusterId'] or marker['node']!=INPUT['node'] or marker['grantIntegrationEnabled']:raise RuntimeError('Wrong isolated quorum identity')
if INPUT['node']!='witness':
 cfg=json.loads(Path('/etc/immich-ha/config.json').read_text())
 if cfg['node']!=INPUT['node']:raise RuntimeError('Wrong Immich identity')
save=Path('/etc/immich-ha/quorum-shadow-backups')/INPUT['stageId']
save.mkdir(parents=True,mode=0o700,exist_ok=False)
lib=Path('/usr/local/lib/immich-ha');lib.mkdir(mode=0o700,exist_ok=True)
for name in INPUT['files']:
 target=Path(name)
 if target.exists():shutil.copy2(target,save/target.name)
 temporary=target.with_name(target.name+'.shadow-stage')
 temporary.write_bytes(Path(INPUT['directory'],INPUT['files'][name]).read_bytes());os.chmod(temporary,0o600 if name.endswith(('.py','.json')) else 0o755)
 if name.endswith('.py') or name=='/usr/local/sbin/immich-ha-agent':py_compile.compile(str(temporary),doraise=True)
 os.replace(temporary,target)
print(json.dumps({'saved':True,'installedShadow':True,'liveGrantIntegration':False}))
'''

TEST=r'''
import json,sys,time,re
from pathlib import Path
sys.path.insert(0,'/usr/local/lib/immich-ha')
from quorum_lock import Etcd,ShadowLock,b64
marker=json.loads(Path('/etc/immich-ha-quorum/installation.json').read_text())
if marker['clusterId']!=INPUT['config']['clusterId'] or marker['grantIntegrationEnabled']:raise RuntimeError('Only isolated quorum may be tested')
if not re.fullmatch('test-[a-f0-9]{32}',INPUT['scope']):raise RuntimeError('Only disposable shadow scopes allowed')
lock=ShadowLock(Etcd(INPUT['config']),INPUT['config']['clusterId'],INPUT['scope'])
action=INPUT['action']
if action=='claim':result=lock.claim(INPUT['authority'],INPUT['owner'],INPUT['epoch'],INPUT['session'],5)
elif action=='read':
 rows=lock.read();result={'generation':rows[0]['value'],'active':rows[1]['value']}
elif action=='expired':
 until=time.monotonic()+20
 while lock.read()[1]['value'] is not None:
  if time.monotonic()>until:raise RuntimeError('Test lease did not expire within bound')
  time.sleep(1)
 result={'expired':True}
elif action=='cleanup':
 rows=lock.read()
 result=lock.client.call('/v3/kv/txn',{'compare':[{'target':'MOD','result':'EQUAL','key':b64(key),'mod_revision':str(row['revision'])} for key,row in zip(lock.keys,rows)],
 'success':[{'request_delete_range':{'key':b64(key)}} for key in lock.keys],'failure':[]})
 if not result.get('succeeded'):raise RuntimeError('Test keys changed before cleanup')
else:raise RuntimeError('Unsupported test action')
print(json.dumps(result))
'''

OBSERVE=r'''
import json,sys
from pathlib import Path
sys.path.insert(0,'/usr/local/lib/immich-ha')
from quorum_lock import Etcd,ShadowLock
lock=ShadowLock(Etcd(INPUT['config']),INPUT['config']['clusterId'])
generation,active=lock.read()
value=active['value']
print(json.dumps({'owner':value.get('owner') if value else None,'epoch':value.get('epoch') if value else None,'authority':value.get('authority') if value else None,'mode':'shadow'}))
'''


def config(plan,node):
    return {'version':1,'mode':'shadow','clusterId':plan['clusterId'],'node':node if node!='witness' else 'primary',
        'endpoints':[f'https://{plan["members"][n]["tailscaleIp"]}:2379' for n in q.NODES],
        'ca':'/etc/immich-ha-quorum/client-ca.crt','cert':'/etc/immich-ha-quorum/probe.crt','key':'/etc/immich-ha-quorum/probe.key'}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    mode=parser.add_mutually_exclusive_group()
    mode.add_argument('--deploy',action='store_true')
    mode.add_argument('--check',action='store_true')
    args=parser.parse_args()
    plan=json.loads(q.PLAN.read_text());stage.validate_plan(plan)
    if args.check:
        state=d.control()
        with q.connections(load_env()['SSH_COORDINATOR_HOST']) as (clients,first):observed=d.remote(clients['witness'],OBSERVE,{'config':config(plan,'witness')})
        print(json.dumps({'mode':'shadow','liveGrantIntegration':False,'observed':observed,
            'agents':{node:state['agents'].get(node,{}).get('metrics',{}).get('quorumShadow') for node in ('primary','backup')}},indent=2));return
    if not args.deploy:print('Use --deploy for checkpoint 3a: shadow namespace only; live grants stay on Cloudflare.');return
    before=d.control()
    if not before['ready'] or not before['standbyEligible'] or before.get('pending') or before.get('operation',{}).get('status') not in (None,'succeeded','failed'):raise RuntimeError('Live HA must be idle and ready')
    stage_id=uuid.uuid4().hex;report={'stageId':stage_id,'mode':'shadow','liveGrantIntegration':False,'tests':{}}
    record={'auto':before.get('autoRequested',before['auto']),'recovery':before['recovery']['enabled'],'owner':before['owner'],'epoch':before['epoch']}
    (q.PLAN.parent/('quorum-shadow-rollout-'+stage_id+'.json')).write_text(json.dumps(record,indent=2)+'\n')
    with q.connections(load_env()['SSH_COORDINATOR_HOST']) as (clients,first):
        # Pause only HA automation during agent restart; preserve saved choices.
        d.control('set-auto',enabled=False);d.control('set-recovery',enabled=False)
        for node in q.NODES:
            files={'/usr/local/lib/immich-ha/quorum_lock.py':(d.ROOT/'node/quorum_lock.py').read_bytes()}
            if node!='witness':files['/usr/local/sbin/immich-ha-agent']=(d.ROOT/'node/agent.py').read_bytes()
            directory='/root/immich-ha-shadow-stage-'+stage_id
            d.remote(clients[node],"from pathlib import Path;import json;Path(INPUT['directory']).mkdir(mode=0o700);print(json.dumps({'created':True}))",{'directory':directory})
            source={}
            with clients[node].open_sftp() as sftp:
                for index,(name,content) in enumerate(files.items()):
                    filename=str(index);source[name]=filename
                    with sftp.open(directory+'/'+filename,'wb') as output:sftp.chmod(directory+'/'+filename,0o600);output.write(content)
            d.remote(clients[node],INSTALL,{'clusterId':plan['clusterId'],'node':node,'stageId':stage_id,'directory':directory,'files':source})
        # Test cross-host CAS/TTL before enabling the observer on either agent.
        scope='test-'+uuid.uuid4().hex
        def test(node,**extra):return d.remote(clients[node],TEST,{'config':config(plan,node),'scope':scope,**extra},timeout=40)
        try:
            first=test('primary',action='claim',authority='cloudflare',owner='primary',epoch=100,session='cf_test_session_12345')
            blocked=test('backup',action='claim',authority='fallback',owner='backup',epoch=101,session='fallback_session_12345')
            if not first.get('admitted') or blocked.get('admitted') or blocked.get('reason')!='lease-held':raise RuntimeError('Live shadow exclusion test failed')
            test('witness',action='expired')
            fallback=test('backup',action='claim',authority='fallback',owner='backup',epoch=101,session='fallback_session_12345')
            stale=test('primary',action='claim',authority='cloudflare',owner='primary',epoch=100,session='cf_test_session_12345')
            if not fallback.get('admitted') or stale.get('admitted') or stale.get('reason')!='stale-generation':raise RuntimeError('Live stale-generation test failed')
            report['tests']={'liveLeaseExcludedFallback':True,'expiredLeaseAdmittedNextGeneration':True,'staleCloudflareRejected':True}
            print('Live shadow CAS/TTL tests passed across the three members.',flush=True)
        finally:test('primary',action='cleanup')
        for node in ('primary','backup'):
            d.remote(clients[node],"import json,os;from pathlib import Path;p=Path('/etc/immich-ha/quorum-shadow.json');value=INPUT['config'];\nif p.exists() and json.loads(p.read_text())!=value:raise RuntimeError('Existing shadow config differs; retained')\ntmp=p.with_suffix('.json.tmp');tmp.write_text(json.dumps(value,indent=2)+'\\n');os.chmod(tmp,0o600);os.replace(tmp,p);print(json.dumps({'shadowConfigured':True}))",{'config':config(plan,node)})
        # Standby first, source second; sessions, app and PostgreSQL stay intact.
        for node in [n for n in ('primary','backup') if n!=record['owner']]+[record['owner']]:
            d.remote(clients[node],"import subprocess,json;subprocess.run(['systemctl','restart','immich-ha-agent.service'],check=True,timeout=30);print(json.dumps({'agentReloaded':True}))")
            print(node+': shadow observer running; grant path unchanged',flush=True)
        until=time.monotonic()+120
        while True:
            live=d.control()
            if live['owner']!=record['owner'] or live['epoch']!=record['epoch']:raise RuntimeError('Live authority changed; automation remains OFF')
            observed=d.remote(clients['witness'],OBSERVE,{'config':config(plan,'witness')})
            if live['ready'] and live['standbyEligible'] and observed['owner']==record['owner'] and observed['epoch']==record['epoch']:break
            if time.monotonic()>until:raise RuntimeError('Shadow observation not confirmed; automation remains OFF')
            time.sleep(5)
        d.control('set-recovery',enabled=record['recovery']);d.control('set-auto',enabled=record['auto'])
        report['observed']=observed
    after=d.control();report['liveHA']={k:after[k] for k in ('owner','epoch','ready','standbyEligible','auto')}
    (q.PLAN.parent/'quorum-shadow-report.json').write_text(json.dumps(report,indent=2)+'\n')
    print('CHECKPOINT 3a OK: observer only. Shared-lock enforcement and fallback promotion are NOT enabled.')


if __name__=='__main__':main()
