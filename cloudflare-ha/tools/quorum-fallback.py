#!/usr/bin/env python3
"""Checkpoint 3c: enable same-quorum fallback and nonce-based CF reconciliation."""
import argparse
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import time
import uuid

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import deployment as d
import quorum_setup as q
from local_env import load_env

spec=importlib.util.spec_from_file_location('quorum_enforce',Path(__file__).with_name('quorum-enforce.py'))
enforce=importlib.util.module_from_spec(spec);spec.loader.exec_module(enforce)

ENABLE=q.APP_MARKER_ACCESS+r'''
import json,os,subprocess
from pathlib import Path
p=Path('/etc/immich-ha/quorum-authority.json');value=json.loads(p.read_text())
if value['node']!=INPUT['node'] or value['clusterId']!=INPUT['clusterId'] or value['mode']!='enforced':raise RuntimeError('Wrong enforced identity')
if Path('/etc/immich-ha/quorum-required').read_text().strip()!=value['clusterId']:raise RuntimeError('Mandatory marker missing')
ensure_app_marker_access()
value['fallbackEnabled']=True
temporary=p.with_suffix('.fallback-stage');temporary.write_text(json.dumps(value,indent=2)+'\n');os.chmod(temporary,0o600)
with temporary.open('rb') as stream:os.fsync(stream.fileno())
os.replace(temporary,p)
descriptor=os.open(p.parent,os.O_RDONLY|os.O_DIRECTORY)
try:os.fsync(descriptor)
finally:os.close(descriptor)
subprocess.run(['systemctl','restart','immich-ha-agent.service'],check=True,capture_output=True,timeout=30)
print(json.dumps({'fallbackEnabled':True,'node':INPUT['node']}))
'''

CHECK=r'''
import json,sys
from pathlib import Path
sys.path.insert(0,'/usr/local/lib/immich-ha')
from quorum_authority import ControlLock,Etcd,validate_enforced_config
lock=ControlLock(Etcd(INPUT['config'],validate_enforced_config),INPUT['config']['clusterId'])
generation,active=lock.read();v=active['value'];g=generation['value'] or {}
print(json.dumps({'owner':v.get('owner') if v else None,'epoch':v.get('epoch') if v else None,
 'authority':v.get('authority') if v else None,'policy':g.get('policy'),
 'localBlocked':g.get('localBlocked',False),'released':g.get('released',False)}))
'''


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    mode=parser.add_mutually_exclusive_group()
    mode.add_argument('--deploy',action='store_true');mode.add_argument('--check',action='store_true')
    args=parser.parse_args();plan=json.loads(q.PLAN.read_text())
    if not plan.get('grantIntegrationEnabled') or plan.get('stage') not in ('enforced','fallback-enabled'):raise RuntimeError('Finish and verify checkpoint 3b first')
    if args.check:
        live=d.control()
        with q.connections(load_env()['SSH_COORDINATOR_HOST']) as (clients,_):
            observed=d.remote(clients['witness'],CHECK,{'config':enforce.config(plan,'witness')})
        print(json.dumps({'quorum':observed,'cloudflare':live.get('quorumControl'),
            'agents':{n:a.get('metrics',{}).get('quorumAuthority') for n,a in live['agents'].items()}},indent=2));return
    if not args.deploy:print('Use --deploy to enable fallback. Does not install Funnel or test an outage.');return
    before=d.control()
    if not enforce.idle(before) or (before.get('quorumControl') or {}).get('mode','cloudflare')!='cloudflare':raise RuntimeError('Both nodes must be idle and healthy on Cloudflare')
    record=dict(owner=before['owner'],epoch=before['epoch'],auto=before.get('autoRequested',before['auto']),
                recovery=before['recovery']['enabled'],preference=before.get('preference'))
    stage_id=uuid.uuid4().hex
    report_path=q.PLAN.parent/('quorum-fallback-rollout-'+stage_id+'.json')
    report={'stageId':stage_id,'before':record,'completed':False}
    report_path.write_text(json.dumps(report,indent=2)+'\n')
    with q.connections(load_env()['SSH_COORDINATOR_HOST']) as (clients,_):
        checks={n:d.remote(clients[n],enforce.CHECK,dict(node=n,config=enforce.config(plan,n))) for n in q.NODES}
        if not all(c['required'] and c['owner']==record['owner'] and c['epoch']==record['epoch'] for c in checks.values()) or not checks[record['owner']].get('freshLocalGrant'):raise RuntimeError('Existing quorum grant not healthy')
        if checks['primary']['systemId']!=checks['backup']['systemId']:raise RuntimeError('Database lineage mismatch')
        d.control('set-auto',enabled=False);d.control('set-recovery',enabled=False)
        pref=record['preference'] or {}
        if pref.get('node'):d.control('set-preference',target=pref['node'],enabled=False)
        for node in q.NODES:
            enforce.unchanged(record)
            files={'/usr/local/lib/immich-ha/'+n:d.ROOT/'node'/n for n in ('quorum_lock.py','quorum_authority.py')}
            if node!='witness':
                files.update({'/usr/local/lib/immich-ha/'+n:d.ROOT/'node'/n for n in ('ha_runtime.py','pg_stream.py','immich_upgrade.py')})
                files['/usr/local/lib/immich-ha/ha_agent.py']=d.ROOT/'node/agent.py'
                files['/usr/local/sbin/immich-ha-agent']=d.ROOT/'node/agent.py'
                files['/usr/local/sbin/immich-ha-app-run']=d.ROOT/'node/app-run-v2'
            directory='/root/immich-ha-fallback-'+stage_id
            d.remote(clients[node],"from pathlib import Path;import json;Path(INPUT['directory']).mkdir(mode=0o700);print(json.dumps({'created':True}))",dict(directory=directory))
            sources={}
            with clients[node].open_sftp() as sftp:
                for i,(dest,local) in enumerate(files.items()):
                    sources[dest]=str(i);sftp.put(str(local),directory+'/'+str(i));sftp.chmod(directory+'/'+str(i),0o600)
            d.remote(clients[node],enforce.INSTALL,dict(stageId=stage_id,directory=directory,files=sources))
        # First run new code with the old, enforced configuration. CF policy
        # must never be enabled while an old agent lacks its policy parser.
        order=[n for n in ('primary','backup') if n!=record['owner']]+[record['owner']]
        for node in order:
            d.remote(clients[node],"import json,subprocess;subprocess.run(['systemctl','restart','immich-ha-agent.service'],check=True,timeout=30);print(json.dumps({'reloaded':True}))")
        subprocess.run([sys.executable,str(d.ROOT/'deploy.py'),'--update'],cwd=d.ROOT,check=True)
        until=time.monotonic()+420
        while True:
            live=enforce.unchanged(record)
            if live.get('quorumFallbackProtocol')==2 and live['ready']:break
            if time.monotonic()>=until:raise RuntimeError('CF protocol not confirmed; automation remains OFF')
            time.sleep(5)
        d.control('set-quorum-policy',clusterId=plan['clusterId'],enabled=True)
        plan.update(fallbackEnabled=True,stage='fallback-rollout')
        q.PLAN.write_text(json.dumps(plan,indent=2)+'\n')
        for node in order:
            enforce.unchanged(record)
            d.remote(clients[node],ENABLE,dict(node=node,clusterId=plan['clusterId']))
            print(node+': fallback enabled; app/database not restarted',flush=True)
        until=time.monotonic()+180
        while True:
            live=enforce.unchanged(record)
            if live['ready'] and live['standbyEligible'] and all((live['agents'][n].get('metrics',{}).get('quorumAuthority') or {}).get('fallbackEnabled') is True for n in ('primary','backup')):break
            if time.monotonic()>=until:raise RuntimeError('Agent fallback capabilities not confirmed; automation remains OFF')
            time.sleep(5)
        d.control('set-recovery',enabled=record['recovery'])
        if pref.get('node'):d.control('set-preference',target=pref['node'],enabled=pref.get('enabled',False))
        d.control('set-auto',enabled=record['auto'])
        until=time.monotonic()+150
        while True:
            observed=d.remote(clients['witness'],CHECK,{'config':enforce.config(plan,'witness')})
            if observed['owner']==record['owner'] and observed['epoch']==record['epoch'] and observed['authority']=='cloudflare' and observed.get('policy',{}).get('auto')==record['auto']:break
            if time.monotonic()>=until:raise RuntimeError('Restored policy not yet observed by quorum; inspect --check')
            time.sleep(5)
        report['quorum']=observed
    plan['stage']='fallback-enabled';q.PLAN.write_text(json.dumps(plan,indent=2)+'\n')
    report['completed']=True;report_path.write_text(json.dumps(report,indent=2)+'\n')
    print('CHECKPOINT 3c ENABLED. CF remains coordinator. No outage, failover or Funnel test has been performed by this installer.')


if __name__=='__main__':main()
