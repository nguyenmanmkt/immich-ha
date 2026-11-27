#!/usr/bin/env python3
"""Install the dashboard upgrade feature; does NOT upgrade Immich itself."""
import argparse
import datetime as dt
import json
from pathlib import Path
import subprocess
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import deployment as m
from ssh_nodes import connect,settings
from local_env import load_env
from project_config import load_project

PREFLIGHT = '''
import json,sys,subprocess
from pathlib import Path
sys.path.insert(0,'/usr/local/lib/immich-ha')
from pg_stream import configuration,status
c=json.loads(Path('/etc/immich-ha/config.json').read_text());assert c['node']==INPUT['node'] and c.get('runtime_version',1)>=2
assert Path('/var/lib/immich-ha/session-v2').is_file()
pg=status();cfg=configuration();assert cfg
assert (Path('/var/lib/immich-ha/runtime/mode').read_text().strip()=='active')==(INPUT['owner']==c['node'])
assert pg['inRecovery']==(INPUT['owner']!=c['node'])
print(json.dumps({'node':c['node'],'peerIp':cfg['peer_ip'],'appVersion':json.loads(Path('/opt/immich/app/package.json').read_text())['version'],'systemId':pg['systemId']}))
'''


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--via',choices=['primary','backup'],default='primary',help='First SSH node; reach its peer through the existing Tailscale transport')
    parser.add_argument('--deploy',action='store_true',help='Install modules/Worker UI; no Immich version changes')
    args=parser.parse_args();state=m.control()
    assert state['ready'] and state['standbyEligible'] and not state['pending'],'Both nodes must be healthy'
    assert not state.get('operation') or state['operation']['status'] in ['succeeded','failed']
    assert not (state.get('immichUpgrade') or {}).get('applying'),'Finish application upgrade first'
    first,gateway=m.connection(args.via);clients={args.via:first}
    other='backup' if args.via=='primary' else 'primary'
    changed=False
    try:
        info=m.remote(first,PREFLIGHT,{'node':args.via,'owner':state['owner']})
        options=settings(other,load_env(),load_project());options['hostname']=info['peerIp']
        options['sock']=first.get_transport().open_channel('direct-tcpip',(options['hostname'],options['port']),('127.0.0.1',0),timeout=15)
        clients[other]=connect(options)
        peer=m.remote(clients[other],PREFLIGHT,{'node':other,'owner':state['owner']})
        assert info['systemId']==peer['systemId'] and info['appVersion']==peer['appVersion']
        print(json.dumps({'checked':[info,peer],'mode':'deploy feature only' if args.deploy else 'read-only preflight'}),flush=True)
        if not args.deploy:return
        stamp=dt.datetime.now().strftime('%Y%m%d-%H%M%S')
        (m.ROOT/'generated'/('before-immich-upgrade-feature-'+stamp+'.json')).write_text(json.dumps(state,indent=2),encoding='utf-8')
        p=state.get('preference') or {}
        if p.get('node'):m.control('set-preference',target=p['node'],enabled=False)
        m.control('set-recovery',enabled=False);m.control('set-auto',enabled=False);changed=True
        for node in [n for n in ['primary','backup'] if n!=state['owner']]+[state['owner']]:
            c=clients[node]
            m.remote(c,'''import json,shutil
from pathlib import Path
save=Path('/etc/immich-ha')/('upgrade-feature-'+INPUT['stamp']);save.mkdir(mode=0o700)
for name in ['ha_runtime.py','pg_stream.py','quorum_lock.py','quorum_authority.py','immich_upgrade.py','build-immich-release.sh','ha_agent.py']:
 p=Path('/usr/local/lib/immich-ha')/name
 if p.exists():shutil.copy2(p,save/name)
shutil.copy2('/usr/local/sbin/immich-ha-agent',save/'agent-exec')
print(json.dumps({'saved':str(save)}))
''',{'stamp':stamp})
            for name in ['ha_runtime.py','pg_stream.py','quorum_lock.py','quorum_authority.py','immich_upgrade.py','build-immich-release.sh']:
                m.upload_module(c,m.ROOT/'node'/name,'/usr/local/lib/immich-ha/'+name,0o700 if name.endswith('.sh') else 0o600)
            m.upload_module(c,m.ROOT/'node/agent.py','/usr/local/lib/immich-ha/ha_agent.py')
            m.upload_module(c,m.ROOT/'node/agent.py','/usr/local/sbin/immich-ha-agent',0o700)
            print(node,m.remote(c,'''import subprocess,py_compile,json,sys
for name in ['ha_runtime.py','pg_stream.py','quorum_lock.py','quorum_authority.py','immich_upgrade.py','ha_agent.py']:py_compile.compile('/usr/local/lib/immich-ha/'+name,doraise=True)
sys.path.insert(0,'/usr/local/lib/immich-ha')
from pg_stream import install_network_guard
install_network_guard()
subprocess.run(['bash','-n','/usr/local/lib/immich-ha/build-immich-release.sh'],check=True,capture_output=True)
subprocess.run(['systemctl','restart','immich-ha-agent.service'],check=True,capture_output=True,timeout=30)
print(json.dumps({'agentReloaded':True}))
'''),flush=True)
        # New Worker accepts capability metrics; old Worker safely ignored them.
        subprocess.run([sys.executable,str(m.ROOT/'deploy.py'),'--update'],cwd=m.ROOT,check=True)
        # Cloudflare may defer an active Durable Object's code update for up to
        # five minutes; the old controller safely ignores new metrics meanwhile.
        deadline=time.monotonic()+420
        while time.monotonic()<deadline:
            live=m.control();assert live['owner']==state['owner'] and live['epoch']==state['epoch']
            if live.get('immichUpgradeProtocol',0)>=2 and live['ready'] and live['standbyEligible'] and all(a.get('metrics',{}).get('immichUpgrade',{}).get('protocol')==1 for a in live['agents'].values()):break
            time.sleep(5)
        else:raise RuntimeError('Capability/health not confirmed; automation remains OFF')
        m.control('set-recovery',enabled=state.get('recovery',{}).get('enabled',False))
        if p.get('node'):m.control('set-preference',target=p['node'],enabled=p.get('enabled',False))
        m.control('set-auto',enabled=state.get('autoRequested',state['auto']))
        changed=False
        print('IMMICH UPGRADE DASHBOARD READY. Immich itself is unchanged; choose Prepare in the dashboard.',flush=True)
    finally:
        for c in clients.values():c.close()
        if gateway:gateway.close()
        if changed:print('Rollout incomplete: automation remains OFF; inspect saved state and health before restoring settings.',file=sys.stderr)


if __name__=='__main__':main()
