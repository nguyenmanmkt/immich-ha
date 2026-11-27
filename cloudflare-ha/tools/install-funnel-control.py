#!/usr/bin/env python3
"""Install only the common witness Funnel control; preserve grants and node Funnels."""
import argparse
import json
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import urllib.parse
import uuid

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import deployment as d
import quorum_setup as q
from local_env import load_env
from project_config import load_project

INSTALL=r"""
import json,os,py_compile,subprocess
from pathlib import Path
base=Path('/usr/local/lib/immich-ha');base.mkdir(mode=0o700,exist_ok=True)
for name in ('witness_funnel.py','funnel_setup.py'):
 p=base/name;tmp=p.with_suffix('.install-stage');tmp.write_bytes(Path(INPUT['directory'],name).read_bytes().replace(b'\r\n',b'\n'));tmp.chmod(0o600)
 py_compile.compile(str(tmp),doraise=True);os.replace(tmp,p)
config=Path('/etc/immich-ha/witness-funnel-control.json')
if config.exists():
 old=json.loads(config.read_text())
 if old!=INPUT['config']:raise RuntimeError('Existing witness control differs; retained')
else:
 config.parent.mkdir(mode=0o700,exist_ok=True)
 tmp=config.with_suffix('.install-stage');tmp.write_text(json.dumps(INPUT['config'])+'\n');tmp.chmod(0o600);os.replace(tmp,config)
unit='''[Unit]
Description=Apply common Immich witness Funnel setting
After=network-online.target tailscaled.service
[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /usr/local/lib/immich-ha/witness_funnel.py
User=root
Group=root
UMask=0077
TimeoutStartSec=100
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
'''
timer='''[Unit]
Description=Check common Immich Funnel setting
[Timer]
OnBootSec=15s
OnUnitInactiveSec=15s
AccuracySec=1s
Unit=immich-ha-funnel-control.service
[Install]
WantedBy=timers.target
'''
Path('/etc/systemd/system/immich-ha-funnel-control.service').write_text(unit)
Path('/etc/systemd/system/immich-ha-funnel-control.timer').write_text(timer)
subprocess.run(['systemctl','daemon-reload'],check=True,capture_output=True)
subprocess.run(['systemctl','enable','--now','immich-ha-funnel-control.timer'],check=True,capture_output=True)
subprocess.run(['systemctl','start','immich-ha-funnel-control.service'],check=True,capture_output=True,timeout=100)
print(json.dumps({'controlInstalled':True}))
"""


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--deploy',action='store_true');args=parser.parse_args()
    if not args.deploy:print('Use --deploy after verifying the common witness Funnel.');return
    plan=json.loads(q.PLAN.read_text());common=plan.get('witnessFunnel',{})
    if not common.get('enabled') or not plan.get('funnelEnabled'):raise RuntimeError('Verified common and node Funnels required')
    state=d.control()
    if not state['ready'] or not state['standbyEligible'] or state.get('pending') or (state.get('operation') or {}).get('status') not in (None,'succeeded','failed'):raise RuntimeError('Healthy idle HA required; nothing changed')
    parsed=urllib.parse.urlsplit(common['url']);port=parsed.port or 443
    cfg={'controller':json.loads((d.ROOT/'generated/primary/config.json').read_text())['controller_url'],
         'dnsName':parsed.hostname,'port':port,'gatewayPort':plan['witnessGateway']['port']}
    with q.connections(load_env()['SSH_COORDINATOR_HOST']) as (clients,_):
        d.remote(clients['witness'],r'''
import json,subprocess
from pathlib import Path
marker=json.loads(Path('/etc/immich-ha/funnel-gateway.json').read_text())
assert marker['node']=='witness' and marker['clusterId']==INPUT['clusterId']
settings=json.loads(subprocess.check_output(['tailscale','serve','status','--json'],text=True,timeout=15))
address=INPUT['address'];assert settings['AllowFunnel'][address] is True
assert settings['Web'][address]['Handlers']=={'/':{'Proxy':INPUT['target']}}
print(json.dumps({'enabled':True}))
''',{'clusterId':plan['clusterId'],'address':parsed.netloc if parsed.port else parsed.hostname+':443','target':f"http://127.0.0.1:{cfg['gatewayPort']}"})
        stamp=uuid.uuid4().hex
        project_path=d.ROOT/'project.json';project=load_project()
        project['witnessTailscaleIp']=plan['members']['witness']['tailscaleIp']
        for node in ('primary','backup'):
            project['nodes'][node]['funnelUrl']=plan['funnel']['urls'][node]
            project['nodes'][node]['tailscaleIp']=plan['members'][node]['tailscaleIp']
        validation=d.ROOT/'generated/funnel-control-project.json';validation.write_text(json.dumps(project,ensure_ascii=False,indent=2)+'\n',encoding='utf-8');load_project(validation)
        secret_path=d.ROOT/'generated/worker-secrets.json';values=json.loads(secret_path.read_text())
        shutil.copy2(project_path,d.ROOT/f'generated/project-before-funnel-control-{stamp}.json')
        shutil.copy2(secret_path,d.ROOT/f'generated/secrets-before-funnel-control-{stamp}.json')
        values.setdefault('FUNNEL_CONTROL_TOKEN',secrets.token_urlsafe(48));cfg['token']=values['FUNNEL_CONTROL_TOKEN']
        project_path.write_text(validation.read_text(encoding='utf-8'),encoding='utf-8');secret_path.write_text(json.dumps(values,indent=2)+'\n')
        subprocess.run([sys.executable,str(d.ROOT/'deploy.py'),'--update'],check=True)
        directory='/root/immich-funnel-control-'+stamp
        d.remote(clients['witness'],"import json;from pathlib import Path;Path(INPUT['directory']).mkdir(mode=0o700);print(json.dumps({'created':True}))",{'directory':directory})
        with clients['witness'].open_sftp() as sftp:
            for name,source in [('witness_funnel.py',d.ROOT/'node/witness_funnel.py'),('funnel_setup.py',d.ROOT/'funnel_setup.py')]:
                sftp.put(str(source),directory+'/'+name);sftp.chmod(directory+'/'+name,0o600)
        print(json.dumps(d.remote(clients['witness'],INSTALL,{'directory':directory,'config':cfg},timeout=120)))


if __name__=='__main__':main()
