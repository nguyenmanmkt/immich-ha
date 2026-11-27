#!/usr/bin/env python3
"""Checkpoint 3b: enforce shared admission, without autonomous fallback or Funnel."""
import argparse
import json
from pathlib import Path
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import deployment as d
import quorum_setup as q
import quorum_stage as stage
from local_env import load_env

PREFLIGHT = r'''
import json,subprocess,sys
from pathlib import Path
marker=json.loads(Path('/etc/immich-ha-quorum/installation.json').read_text())
if marker['clusterId']!=INPUT['clusterId'] or marker['node']!=INPUT['node'] or marker['grantIntegrationEnabled']:raise RuntimeError('Expected isolated quorum identity')
subprocess.run(['systemctl','is-active','--quiet','immich-ha-quorum.service'],check=True)
result={'node':INPUT['node'],'quorumHealthy':True}
if INPUT['node']!='witness':
 sys.path.insert(0,'/usr/local/lib/immich-ha')
 from pg_stream import configuration,status
 cfg=json.loads(Path('/etc/immich-ha/config.json').read_text());pg=status()
 if cfg['node']!=INPUT['node'] or cfg.get('enforce') is not True or not configuration():raise RuntimeError('Expected enforcing streaming runtime')
 if Path('/etc/immich-ha/quorum-required').exists() or Path('/etc/immich-ha/quorum-authority.json').exists():raise RuntimeError('Existing enforcement retained; use --check before any repair')
 mode=Path('/var/lib/immich-ha/runtime/mode').read_text().strip()
 if mode!=('active' if INPUT['owner']==INPUT['node'] else 'standby') or pg['inRecovery']!=(INPUT['owner']!=INPUT['node']):raise RuntimeError('Unexpected app/database role')
 if INPUT['owner']!=INPUT['node'] and not pg['receiverConnected']:raise RuntimeError('Replica is not receiving WAL')
 result.update(systemId=pg['systemId'],sessionPresent=Path('/var/lib/immich-ha/session-v2').is_file())
print(json.dumps(result))
'''

INSTALL = r'''
import json,os,py_compile,shutil,subprocess
from pathlib import Path
save=Path('/etc/immich-ha/quorum-enforcement-backups')/INPUT['stageId']
save.mkdir(parents=True,mode=0o700,exist_ok=False)
for destination,source in INPUT['files'].items():
 p=Path(destination)
 if p.exists():shutil.copy2(p,save/p.name)
 temporary=p.with_name(p.name+'.quorum-stage')
 temporary.write_bytes(Path(INPUT['directory'],source).read_bytes().replace(b'\r\n',b'\n'))
 os.chmod(temporary,0o750 if p.name=='immich-ha-app-run' else 0o700 if p.name=='immich-ha-agent' else 0o600)
 if p.name=='immich-ha-app-run':
  import pwd
  os.chown(temporary,0,pwd.getpwnam('immich').pw_gid)
  subprocess.run(['bash','-n',str(temporary)],check=True,capture_output=True)
 else:py_compile.compile(str(temporary),doraise=True)
 os.replace(temporary,p)
print(json.dumps({'saved':True,'filesInstalled':len(INPUT['files'])}))
'''

ENABLE = q.APP_MARKER_ACCESS + r'''
import json,os,subprocess
from pathlib import Path
def atomic(p,value,mode=0o600):
 p=Path(p);temporary=p.with_suffix(p.suffix+'.enforce-stage');temporary.write_text(value);os.chmod(temporary,mode)
 with temporary.open('rb') as stream:os.fsync(stream.fileno())
 os.replace(temporary,p)
 descriptor=os.open(p.parent,os.O_RDONLY|os.O_DIRECTORY)
 try:os.fsync(descriptor)
 finally:os.close(descriptor)
marker=Path('/etc/immich-ha-quorum/installation.json');value=json.loads(marker.read_text())
if value['clusterId']!=INPUT['clusterId'] or value['node']!=INPUT['node']:raise RuntimeError('Wrong quorum identity')
value['grantIntegrationEnabled']=True;value['enforcementProtocol']=1
atomic(marker,json.dumps(value,indent=2)+'\n')
if INPUT['node']!='witness':
 existing=Path('/etc/immich-ha/quorum-authority.json')
 if existing.exists() and json.loads(existing.read_text())!=INPUT['config']:raise RuntimeError('Existing enforcement config differs; retained')
 atomic('/etc/immich-ha/quorum-authority.json',json.dumps(INPUT['config'],indent=2)+'\n')
 atomic('/etc/immich-ha/quorum-required',INPUT['clusterId']+'\n',0o644)
 ensure_app_marker_access()
 subprocess.run(['systemctl','restart','immich-ha-agent.service'],check=True,timeout=30,capture_output=True)
print(json.dumps({'node':INPUT['node'],'enforcementRequired':True}))
'''

CHECK = r'''
import json,sys,time,subprocess
from pathlib import Path
sys.path.insert(0,'/usr/local/lib/immich-ha')
from quorum_authority import ControlLock,Etcd,validate_enforced_config
marker=json.loads(Path('/etc/immich-ha-quorum/installation.json').read_text())
if marker['clusterId']!=INPUT['config']['clusterId']:raise RuntimeError('Wrong cluster')
lock=ControlLock(Etcd(INPUT['config'],validate_enforced_config),marker['clusterId'])
generation,active=lock.read();record=active['value']
result={'node':INPUT['node'],'required':marker['grantIntegrationEnabled'],
 'owner':record['owner'] if record else None,'epoch':record['epoch'] if record else None,
 'authority':record['authority'] if record else None,'activeLease':active['lease']!='0'}
if INPUT['node']!='witness':
 from pg_stream import status
 cfg=json.loads(Path('/etc/immich-ha/config.json').read_text())
 required=Path('/etc/immich-ha/quorum-required').read_text().strip()
 if required!=marker['clusterId'] or cfg['node']!=INPUT['node']:raise RuntimeError('Mandatory identity differs')
 pg=status();result['inRecovery']=pg['inRecovery'];result['receiverConnected']=pg['receiverConnected'];result['systemId']=pg['systemId']
 result['mode']=Path('/var/lib/immich-ha/runtime/mode').read_text().strip()
 if record and record['owner']==INPUT['node']:
  proof=json.loads(Path('/run/immich-ha-quorum-grant.json').read_text())
  result['freshLocalGrant']=proof['epoch']==record['epoch'] and proof['clusterId']==required and proof['bootId']==Path('/proc/sys/kernel/random/boot_id').read_text().strip() and proof['deadline']>time.clock_gettime(time.CLOCK_BOOTTIME)+30
  result['sessionMatches']=Path('/var/lib/immich-ha/session-v2').read_text().strip()==record['session']
 if INPUT.get('testExclusion'):
  if not record or record['owner']!=INPUT['owner'] or record['epoch']!=INPUT['epoch'] or not result.get('freshLocalGrant'):raise RuntimeError('Fresh active proof required before exclusion test')
  other='backup' if record['owner']=='primary' else 'primary'
  for authority in ('cloudflare','fallback'):
   answer=lock.claim(authority,other,record['epoch']+1,'exclusion_test_session_12345',5)
   if answer.get('admitted') or answer.get('reason')!='lease-held':raise RuntimeError('Competing authority exclusion failed')
  result['oppositeAuthoritiesRejected']=True
print(json.dumps(result))
'''


def config(plan, node):
    return dict(version=1, mode='enforced', fallbackEnabled=False, clusterId=plan['clusterId'],
                node=node if node != 'witness' else 'primary',
                endpoints=[f'https://{plan["members"][n]["tailscaleIp"]}:2379' for n in q.NODES],
                ca='/etc/immich-ha-quorum/client-ca.crt', cert='/etc/immich-ha-quorum/probe.crt',
                key='/etc/immich-ha-quorum/probe.key')


def idle(state):
    return state['ready'] and state['standbyEligible'] and not state.get('pending') and (state.get('operation') or {}).get('status') in (None, 'succeeded', 'failed') and not (state.get('immichUpgrade') or {}).get('applying')


def unchanged(record):
    live = d.control()
    if live['owner'] != record['owner'] or live['epoch'] != record['epoch'] or live.get('pending') or (live.get('operation') or {}).get('status') not in (None, 'succeeded', 'failed'):
        raise RuntimeError('Authority/job changed during rollout; automation remains OFF')
    return live


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--deploy', action='store_true')
    mode.add_argument('--update', action='store_true', help='Update an already healthy enforced checkpoint; never disables enforcement')
    mode.add_argument('--check', action='store_true')
    args = parser.parse_args()
    plan = json.loads(q.PLAN.read_text())
    # Validate immutable topology even on the read-only path after cutover.
    stage.validate_plan({**plan, 'grantIntegrationEnabled': False})
    if args.check:
        with q.connections(load_env()['SSH_COORDINATOR_HOST']) as (clients, _):
            report = {n: d.remote(clients[n], CHECK, dict(config=config(plan, n), node=n)) for n in q.NODES}
        print(json.dumps(report, indent=2));return
    if not (args.deploy or args.update):print('Use --deploy for mandatory admission only. No fallback promoter or Funnel is enabled.');return
    if args.update:
        if plan.get('fallbackEnabled'):raise RuntimeError('Fallback is enabled; use the checkpoint 3c updater, never overwrite its configuration')
        if not plan['grantIntegrationEnabled'] or plan.get('stage')!='enforced':raise RuntimeError('Update requires a healthy completed enforcement rollout; inspect --check')
    elif plan['grantIntegrationEnabled']:raise RuntimeError('Already enabled or rollout incomplete; use --check. Never restage automatically.')
    before = d.control()
    if not idle(before):raise RuntimeError('Both nodes must be idle and ready')
    record = dict(owner=before['owner'], epoch=before['epoch'], auto=before.get('autoRequested', before['auto']),
                  recovery=before['recovery']['enabled'], preference=before.get('preference'))
    stage_id = uuid.uuid4().hex
    report = dict(stageId=stage_id, before=record, fallbackEnabled=False, funnelEnabled=False)
    report_path = q.PLAN.parent / ('quorum-enforcement-rollout-' + stage_id + '.json')
    report_path.write_text(json.dumps(report, indent=2) + '\n')
    with q.connections(load_env()['SSH_COORDINATOR_HOST']) as (clients, _):
        if args.update:
            inventory = {n: d.remote(clients[n], CHECK, dict(config=config(plan,n),node=n)) for n in q.NODES}
            if not all(v['required'] and v['owner']==record['owner'] and v['epoch']==record['epoch'] for v in inventory.values()) or not inventory[record['owner']].get('freshLocalGrant'):raise RuntimeError('Existing enforced grant not healthy')
            for n in ('primary','backup'):
                value=inventory[n]
                if value['inRecovery']!=(n!=record['owner']) or value['mode']!=('active' if n==record['owner'] else 'standby') or (n!=record['owner'] and not value['receiverConnected']):raise RuntimeError('Existing PostgreSQL roles/WAL not healthy')
        else:inventory = {n: d.remote(clients[n], PREFLIGHT, dict(clusterId=plan['clusterId'], node=n, owner=record['owner'])) for n in q.NODES}
        if inventory['primary']['systemId'] != inventory['backup']['systemId']:raise RuntimeError('Different database lineages')
        d.control('set-auto', enabled=False);d.control('set-recovery', enabled=False)
        pref = record['preference'] or {}
        if pref.get('node'):d.control('set-preference', target=pref['node'], enabled=False)
        for node in q.NODES:
            unchanged(record)
            files = {'/usr/local/lib/immich-ha/' + name: d.ROOT / 'node' / name for name in ('quorum_lock.py', 'quorum_authority.py')}
            if node != 'witness':
                files.update({'/usr/local/lib/immich-ha/' + name: d.ROOT / 'node' / name for name in ('ha_runtime.py', 'pg_stream.py', 'immich_upgrade.py')})
                files['/usr/local/lib/immich-ha/ha_agent.py'] = d.ROOT / 'node/agent.py'
                files['/usr/local/sbin/immich-ha-agent'] = d.ROOT / 'node/agent.py'
                files['/usr/local/sbin/immich-ha-app-run'] = d.ROOT / 'node/app-run-v2'
            directory = '/root/immich-ha-enforcement-' + stage_id
            d.remote(clients[node], "from pathlib import Path;import json;Path(INPUT['directory']).mkdir(mode=0o700);print(json.dumps({'created':True}))", dict(directory=directory))
            source = {}
            with clients[node].open_sftp() as sftp:
                for index, (destination, local) in enumerate(files.items()):
                    name = str(index);source[destination] = name
                    sftp.put(str(local), directory + '/' + name);sftp.chmod(directory + '/' + name, 0o600)
            d.remote(clients[node], INSTALL, dict(stageId=stage_id, files=source, directory=directory))
        # Mark the topology before any restart: staging/fault installers must
        # refuse to operate on an enforcement rollout, including a partial one.
        plan.update(grantIntegrationEnabled=True, stage='enforcement-rollout')
        q.PLAN.write_text(json.dumps(plan, indent=2) + '\n')
        for node in ['witness'] + [n for n in ('primary', 'backup') if n != record['owner']] + [record['owner']]:
            unchanged(record)
            d.remote(clients[node], ENABLE, dict(clusterId=plan['clusterId'], node=node, config=config(plan, node)))
            print(node + ': mandatory quorum integration installed', flush=True)
        until = time.monotonic() + 180
        while True:
            live = unchanged(record)
            try:checks = {n: d.remote(clients[n], CHECK, dict(config=config(plan, n), node=n)) for n in q.NODES}
            except RuntimeError:
                if time.monotonic() >= until:raise RuntimeError('Mandatory grant/health not confirmed; automation remains OFF') from None
                time.sleep(5);continue
            if live['ready'] and live['standbyEligible'] and all(v['owner'] == record['owner'] and v['epoch'] == record['epoch'] and v['required'] for v in checks.values()) and checks[record['owner']].get('freshLocalGrant') and checks[record['owner']].get('sessionMatches'):break
            if time.monotonic() >= until:raise RuntimeError('Mandatory grant/health not confirmed; automation remains OFF')
            time.sleep(5)
        report['checks'] = checks
        report['exclusion'] = d.remote(clients[record['owner']], CHECK, dict(config=config(plan, record['owner']), node=record['owner'], owner=record['owner'], epoch=record['epoch'], testExclusion=True))
        d.control('set-recovery', enabled=record['recovery'])
        if pref.get('node'):d.control('set-preference', target=pref['node'], enabled=pref.get('enabled', False))
        d.control('set-auto', enabled=record['auto'])
    plan['stage'] = 'enforced';q.PLAN.write_text(json.dumps(plan, indent=2) + '\n')
    report['completed'] = True;report_path.write_text(json.dumps(report, indent=2) + '\n')
    print('CHECKPOINT 3b OK: mandatory shared admission. Cloudflare remains coordinator; autonomous fallback and Funnel are OFF.')


if __name__ == '__main__':main()
