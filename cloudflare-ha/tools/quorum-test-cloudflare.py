#!/usr/bin/env python3
"""Bounded live test: suppress only agent heartbeats, renew fallback, reconcile CF."""
import argparse
import importlib.util
import json
from pathlib import Path
import sys
import time
import uuid

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import deployment as d
import quorum_setup as q
from local_env import load_env

spec=importlib.util.spec_from_file_location('fallback_checkpoint',Path(__file__).with_name('quorum-fallback.py'))
fallback=importlib.util.module_from_spec(spec);spec.loader.exec_module(fallback)

SAMPLE=r'''
import json,sys,subprocess,urllib.request
from pathlib import Path
sys.path.insert(0,'/usr/local/lib/immich-ha')
from pg_stream import DATA,status
import ha_agent
cfg=json.loads(Path('/etc/immich-ha/quorum-authority.json').read_text())
if cfg['node']!=INPUT['node'] or cfg['clusterId']!=INPUT['clusterId'] or not cfg['fallbackEnabled']:raise RuntimeError('Wrong fallback identity')
pg=status();app=int(subprocess.check_output(['systemctl','show','immich-ha-app.service','--property=MainPID','--value'],text=True).strip())
healthy=False
if app:
 with urllib.request.urlopen('http://127.0.0.1:2283/api/server/ping',timeout=3) as response:healthy=json.load(response).get('res')=='pong'
print(json.dumps({'appPid':app,'pgPid':int((DATA/'postmaster.pid').read_text().splitlines()[0]),
 'inRecovery':pg['inRecovery'],'receiverConnected':pg['receiverConnected'],'appHealthy':healthy,
 'heartbeatBlocked':getattr(ha_agent,'heartbeat_test_blocked',lambda:False)()}))
'''

INSTALL=r'''
import json,os,py_compile,shutil,subprocess
from pathlib import Path
save=Path('/etc/immich-ha/heartbeat-test-backups')/INPUT['testId'];save.mkdir(parents=True,mode=0o700,exist_ok=False)
source=Path(INPUT['source']).read_bytes().replace(b'\r\n',b'\n')
targets=[Path('/usr/local/sbin/immich-ha-agent'),Path('/usr/local/lib/immich-ha/ha_agent.py')]
changed=any(p.read_bytes()!=source for p in targets)
if changed:
 for p in targets:
  shutil.copy2(p,save/p.name)
  tmp=p.with_name(p.name+'.heartbeat-test-stage');tmp.write_bytes(source);os.chmod(tmp,0o700 if p.name=='immich-ha-agent' else 0o600)
  py_compile.compile(str(tmp),doraise=True);os.replace(tmp,p)
 subprocess.run(['systemctl','restart','immich-ha-agent.service'],check=True,capture_output=True,timeout=30)
print(json.dumps({'agentReloaded':changed}))
'''

ARM=r'''
import json,os,subprocess,time
from pathlib import Path
p=Path('/run/immich-ha-heartbeat-outage.json')
if p.exists():raise RuntimeError('Existing test marker retained; inspect it before another test')
directory=Path('/etc/immich-ha/heartbeat-test-backups')/INPUT['testId']
cleanup=directory/'restore-heartbeat.py'
cleanup.write_text("import json\nfrom pathlib import Path\np=Path('/run/immich-ha-heartbeat-outage.json')\nif p.exists() and json.loads(p.read_text()).get('testId')=="+repr(INPUT['testId'])+":\n p.unlink()\n")
os.chmod(cleanup,0o600)
# A local timer is armed BEFORE injecting the failure. The marker itself also
# expires even if systemd's cleanup cannot run or the management PC disappears.
subprocess.run(['systemd-run','--quiet','--unit='+INPUT['unit'],'--on-active=180s','/usr/bin/python3',str(cleanup)],check=True,capture_output=True,timeout=20)
now=time.clock_gettime(time.CLOCK_BOOTTIME)
value=dict(version=1,testId=INPUT['testId'],bootId=Path('/proc/sys/kernel/random/boot_id').read_text().strip(),startedBoot=now,untilBoot=now+180)
temporary=p.with_suffix('.stage');temporary.write_text(json.dumps(value));os.chmod(temporary,0o600);os.replace(temporary,p)
print(json.dumps({'blockedForAtMostSeconds':180,'restoreTimerArmed':True}))
'''

RESTORE=r'''
import json,subprocess
from pathlib import Path
p=Path('/run/immich-ha-heartbeat-outage.json')
if p.exists():
 if json.loads(p.read_text()).get('testId')!=INPUT['testId']:raise RuntimeError('Refusing to remove another test marker')
 p.unlink()
if p.exists():raise RuntimeError('Heartbeat marker still exists; keep automatic cleanup armed')
subprocess.run(['systemctl','stop',INPUT['unit']+'.timer'],check=False,capture_output=True,timeout=20)
print(json.dumps({'heartbeatRestored':True}))
'''

OBSERVE=r'''
import json,sys
from pathlib import Path
sys.path.insert(0,'/usr/local/lib/immich-ha')
from quorum_authority import ControlLock,Etcd,validate_enforced_config
lock=ControlLock(Etcd(INPUT['config'],validate_enforced_config),INPUT['config']['clusterId'])
generation,active=lock.read();v=active['value'] or {}
print(json.dumps({'owner':v.get('owner'),'epoch':v.get('epoch'),'authority':v.get('authority'),
 'revision':active['revision'],'claim':v.get('claim')}))
'''


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--run',action='store_true');args=parser.parse_args()
    if not args.run:print('Use --run for a live, root-only heartbeat fault capped at 180 seconds, with autonomous cleanup.');return
    plan=json.loads(q.PLAN.read_text())
    if plan.get('stage')!='fallback-enabled' or not plan.get('fallbackEnabled'):raise RuntimeError('Verify checkpoint 3c first')
    before=d.control()
    if not fallback.enforce.idle(before) or (before.get('quorumControl') or {}).get('mode')!='cloudflare':raise RuntimeError('Both nodes must be ready and idle under CF')
    expected={k:before[k] for k in ('owner','epoch')}
    test_id=str(uuid.uuid4());unit='immich-ha-heartbeat-test-'+test_id[:8]
    report={'testId':test_id,'before':expected,'passed':False,'fault':'real agents suppress heartbeat POST only; ingress and other requests unchanged'}
    report_path=q.PLAN.parent/('quorum-cloudflare-test-'+test_id+'.json')
    report_path.write_text(json.dumps(report,indent=2)+'\n')
    with q.connections(load_env()['SSH_COORDINATOR_HOST']) as (clients,_):
        def sample(node):return d.remote(clients[node],SAMPLE,dict(node=node,clusterId=plan['clusterId']))
        baseline={n:sample(n) for n in ('primary','backup')};report['baseline']=baseline
        report_path.write_text(json.dumps(report,indent=2)+'\n')
        for n in baseline:
            if baseline[n]['inRecovery']!=(n!=before['owner']) or baseline[n]['heartbeatBlocked']:raise RuntimeError('Unexpected database role or existing injection')
        if not baseline[before['owner']]['appHealthy']:raise RuntimeError('Serving app is not healthy')
        # Install the bounded test hook while normal heartbeats still work, then
        # wait for the replacement agent to acknowledge a healthy normal grant.
        for n in [n for n in baseline if n!=before['owner']]+[before['owner']]:
            remote='/root/immich-ha-heartbeat-test-'+test_id+'.py'
            with clients[n].open_sftp() as sftp:
                sftp.put(str(d.ROOT/'node/agent.py'),remote);sftp.chmod(remote,0o600)
            d.remote(clients[n],INSTALL,dict(testId=test_id,source=remote))
        until=time.monotonic()+150
        while True:
            live=d.control()
            if any(live[k]!=expected[k] for k in expected):raise RuntimeError('Authority changed before fault; no injection performed')
            if fallback.enforce.idle(live):break
            if time.monotonic()>until:raise RuntimeError('Agents not ready; no injection performed')
            time.sleep(5)
        armed=[]
        try:
            for n in ('primary','backup'):
                # Include even a partially failed ARM in unconditional cleanup.
                armed.append(n)
                d.remote(clients[n],ARM,dict(testId=test_id,unit=unit))
                report['armed']=list(armed);report_path.write_text(json.dumps(report,indent=2)+'\n')
            print('HEARTBEATS BLOCKED: both agents; 180-second local expiry and cleanup timers armed.',flush=True)
            for n in baseline:
                if not sample(n)['heartbeatBlocked']:raise RuntimeError('Heartbeat fault not active on '+n)
            observed=[];until=time.monotonic()+110
            while True:
                value=d.remote(clients['witness'],OBSERVE,dict(config=fallback.enforce.config(plan,'witness')))
                if any(value[k]!=expected[k] for k in expected):raise RuntimeError('Unexpected writer change in heartbeat-only test')
                if value['authority']=='fallback':
                    if not observed or value['claim']!=observed[-1]['claim']:
                        observed.append(value)
                        print('FALLBACK '+str(len(observed))+': same owner/epoch; native lease '+('acquired' if len(observed)==1 else 'renewed')+'.',flush=True)
                    if len(observed)>=2:break
                if time.monotonic()>until:raise RuntimeError('Fallback continuation/renewal was not observed within bounded fault')
                time.sleep(5)
            during={n:sample(n) for n in baseline}
            for n in baseline:
                if any(during[n][k]!=baseline[n][k] for k in ('appPid','pgPid','inRecovery')):raise RuntimeError('Unexpected app/database restart or promotion')
            if not during[before['owner']]['appHealthy']:raise RuntimeError('Serving app unhealthy during fallback')
            report.update(fallback=observed,during=during)
            report_path.write_text(json.dumps(report,indent=2)+'\n')
            print('FALLBACK VERIFIED: application and PostgreSQL PIDs unchanged; restoring heartbeat now.',flush=True)
        finally:
            errors=[]
            for n in armed:
                try:d.remote(clients[n],RESTORE,dict(testId=test_id,unit=unit))
                except Exception:errors.append(n)
            if errors:raise RuntimeError('Could not confirm explicit restoration on '+','.join(errors)+'; local expiry/timers remain armed')
            report['heartbeatsRestored']=True;report_path.write_text(json.dumps(report,indent=2)+'\n')
            print('HEARTBEATS RESTORED: waiting for nonce handback to Cloudflare.',flush=True)
        until=time.monotonic()+120
        while True:
            live=d.control();value=d.remote(clients['witness'],OBSERVE,dict(config=fallback.enforce.config(plan,'witness')))
            if any(value[k]!=expected[k] for k in expected):raise RuntimeError('Unexpected owner/epoch after restoration')
            if value['authority']=='cloudflare' and (live.get('quorumControl') or {}).get('mode')=='cloudflare' and live['ready'] and live['standbyEligible']:break
            if time.monotonic()>until:raise RuntimeError('Heartbeat restored but CF handback not confirmed; retain quorum, inspect agents')
            time.sleep(5)
        after={n:sample(n) for n in baseline}
        for n in baseline:
            if any(after[n][k]!=baseline[n][k] for k in ('appPid','pgPid','inRecovery')) or after[n]['heartbeatBlocked']:raise RuntimeError('Post-test continuity check failed')
        for key in ('auto','autoRequested'):
            if live.get(key)!=before.get(key):raise RuntimeError('Automation setting changed during test')
        if live['recovery']['enabled']!=before['recovery']['enabled'] or any((live.get('preference') or {}).get(k)!=(before.get('preference') or {}).get(k) for k in ('node','enabled')):raise RuntimeError('Recovery/preference changed during test')
        report.update(passed=True,after=after,quorum=value,cloudflare=live.get('quorumControl'))
        report_path.write_text(json.dumps(report,indent=2)+'\n')
        print('PASS: fallback acquired and renewed; CF reconciled; owner/epoch/app/PG unchanged; heartbeat markers removed.',flush=True)


if __name__=='__main__':main()
