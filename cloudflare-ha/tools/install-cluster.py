#!/usr/bin/env python3
"""Run init on A / join on B. Every node installs locally; no SSH is used."""
import argparse
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import urllib.parse
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'node'))
from cluster_bootstrap import atomic,inspect,run
from cluster_service import exchange
from project_config import load_project,hostname
from bootstrap_cloudflare import API,preflight,provision,attach_domains


def bundle_digest(root=ROOT):
    paths=sorted([*(root/'node').glob('*.py'),*(root/'node').glob('*.sh'),root/'node/app-run-v2',root/'node/standby-run',
        *(root/'node').glob('*.conf'),root/'streaming_setup.py',root/'tools/streaming-cutover.py',root/'tools/sync-native-backups.py',
        *root.glob('*.py'),root/'requirements-local.txt',root/'tools/install-witness-local.py',root/'tools/quorum-enforce.py',root/'tools/quorum-fallback.py'])
    digest=hashlib.sha256()
    for path in paths:
        digest.update(str(path.relative_to(root)).replace('\\','/').encode()+b'\0')
        digest.update(path.read_bytes().replace(b'\r\n',b'\n')+b'\0')
    return digest.hexdigest()


def ask(label,default='',secret=False):
    value=(getpass.getpass if secret else input)(label+(' ['+default+']' if default and not secret else '')+': ').strip() or default
    if any(c in value for c in ('\n','\r','\x00')):raise ValueError('Invalid input characters')
    return value


def local_network(mode=None):
    mode=mode or ask('Kết nối dữ liệu: lan hoặc tailscale','lan').lower()
    if mode not in ('lan','tailscale'):raise ValueError('Choose lan or tailscale')
    address=ask('IP '+('LAN' if mode=='lan' else 'Tailscale')+' của chính LXC này')
    return {'networkMode':mode,'ip':address}


def require_fresh():
    if sys.platform!='linux' or os.geteuid()!=0:raise RuntimeError('Run as root inside an existing Immich LXC')
    if Path('/etc/immich-ha/config.json').exists() or Path('/etc/postgresql/16/ha').exists():raise RuntimeError('Existing HA retained; do not initialize it again')
    if not Path('/opt/immich/.env').is_file():raise RuntimeError('Native Immich must already be installed')


def install_service(local):
    path=ROOT/'.install/node.json'
    if path.exists() and json.loads(path.read_text())!=local:raise RuntimeError('Existing join identity retained')
    atomic(path,json.dumps(local)+'\n')
    python=str(Path(sys.executable).absolute())
    # systemd quoting; reject control characters, avoid a shell or secrets in argv.
    def quoted(value):return '"'+value.replace('\\','\\\\').replace('"','\\"').replace('%','%%')+'"'
    unit='''[Unit]
Description=Immich HA outbound init/join bootstrap
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
User=root
UMask=0077
Environment=DEBIAN_FRONTEND=noninteractive
ExecStart='''+quoted(python)+' '+quoted(str(ROOT/'node/cluster_service.py'))+'''
Restart=on-failure
RestartSec=10
[Install]
WantedBy=multi-user.target
'''
    atomic('/etc/systemd/system/immich-ha-bootstrap.service',unit,0o644)
    run(['systemctl','daemon-reload']);run(['systemctl','enable','--now','immich-ha-bootstrap.service'])


def initialize():
    require_fresh()
    if any((ROOT/name).exists() for name in ('project.json','.env','generated','.install')):
        raise RuntimeError('Installer needs a clean source checkout; existing deployment files retained')
    network=local_network();source=inspect({'node':'primary',**network})
    if source['existingHa'] or source['rootFree']<3*1024**3:raise RuntimeError('Source preflight failed')
    if source['version'] not in ('3.4.1','3.4.2'):raise RuntimeError('This installer currently supports tested native Immich 3.4.1/3.4.2')
    account=ask('Cloudflare Account ID');zone=ask('Cloudflare Zone ID');token=ask('Cloudflare API token',secret=True)
    if not all(re.fullmatch('[a-fA-F0-9]{32}',v) for v in (account,zone)):raise ValueError('Account/Zone IDs must be 32 hexadecimal characters')
    api=API(token);zone_info=api('/zones/'+zone)
    worker=ask('Tên Worker','immich-ha')
    public=hostname(ask('Domain Immich','immich.'+zone_info['name']))
    admin=hostname(ask('Domain dashboard','immich-ha.'+zone_info['name']))
    email=ask('Email được đăng nhập dashboard')
    project={'workerName':worker,'publicHost':public,'adminHost':admin,
      'origins':{'primary':'https://immich-origin-a.'+zone_info['name'],'backup':'https://immich-origin-b.'+zone_info['name']},
      'nodes':{'primary':{'label':'Immich A','ip':network['ip'],'containerId':int(ask('LXC ID node A (metadata)','100'))},
               'backup':{'label':'Immich B','ip':'0.0.0.0','containerId':int(ask('LXC ID node B (metadata)','101'))}},
      'access':{'teamDomain':'','audience':'','emails':[email]}}
    # Validate using an ephemeral file outside existing deployment configuration.
    import tempfile
    with tempfile.TemporaryDirectory() as temp:
        validation=Path(temp)/'project.json';validation.write_text(json.dumps(project));load_project(validation)
    preflight(api,project,account,zone)
    state={'id':str(uuid.uuid4()),'zoneName':zone_info['name']}
    private=ROOT/'.install';private.mkdir(mode=0o700)
    def save():atomic(private/'init.json',json.dumps(state)+'\n')
    save()
    tokens,credentials=provision(api,project,account,zone,state,save)
    atomic(ROOT/'project.json',json.dumps(project,ensure_ascii=False,indent=2)+'\n')
    env={'CLOUDFLARE_ACCOUNT_ID':account,'CLOUDFLARE_API_TOKEN':token,**credentials}
    atomic(ROOT/'.env',''.join(key+"='"+value+"'\n" for key,value in env.items()))
    subprocess.run([sys.executable,str(ROOT/'deploy.py')],cwd=ROOT,check=True)
    attach_domains(api,project,account,zone,state,save)
    cfg={n:json.loads((ROOT/'generated'/n/'config.json').read_text()) for n in ('primary','backup')}
    worker_secrets=json.loads((ROOT/'generated/worker-secrets.json').read_text())
    code=secrets.token_urlsafe(32);bundle=bundle_digest()
    nodes={n:{'token':secrets.token_urlsafe(48),'config':cfg[n],'tunnelToken':tokens[n]} for n in ('primary','backup')}
    payload={'id':state['id'],'bundle':bundle,'networkMode':network['networkMode'],'joinHash':hashlib.sha256(code.encode()).hexdigest(),
      'nodes':nodes,'appPassword':secrets.token_hex(32),'replicationPassword':secrets.token_hex(32)}
    exchange(cfg['primary']['controller_url'],'/__ha/setup-init',payload,worker_secrets['ADMIN_TOKEN'])
    local={'id':state['id'],'node':'primary','controller':cfg['primary']['controller_url'],'token':nodes['primary']['token'],'bundle':bundle,**network}
    install_service(local)
    print('\nNode A đã init. Trên node B, dùng cùng source/commit và chạy: bash install.sh join')
    print('Controller URL: '+local['controller'])
    print('Mã join (30 phút, ghép đúng một node B): '+code)
    print('Dashboard: https://'+admin)


def join():
    require_fresh()
    if (ROOT/'.install/node.json').exists():raise RuntimeError('This node already has an enrollment identity')
    controller=ask('Controller URL do node A cấp').rstrip('/')
    url=urllib.parse.urlsplit(controller)
    if url.scheme!='https' or not url.hostname or not url.hostname.endswith('.workers.dev') or url.path or url.query or url.fragment or url.username or url.password or url.port:
        raise ValueError('Use the HTTPS workers.dev controller URL from node A')
    code=ask('Mã join do node A cấp',secret=True)
    network=local_network();info=inspect({'node':'backup',**network})
    if info['assets'] or info['mediaFiles'] or info['existingHa']:raise RuntimeError('Node B must be empty; its data was retained')
    claim_path=ROOT/'.install/join-claim'
    claim=claim_path.read_text().strip() if claim_path.exists() else secrets.token_urlsafe(48)
    atomic(claim_path,claim)
    value=exchange(controller,'/__ha/setup-join',{'code':code,'claim':claim,'bundle':bundle_digest()})
    if value['networkMode']!=network['networkMode']:raise RuntimeError('Choose the same LAN/Tailscale mode as node A')
    install_service({**value,**network})
    print('Node B đã join. Theo dõi cài đặt trên dashboard; không cần SSH giữa hai node.')


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('action',choices=('init','join'));args=parser.parse_args()
    if args.action=='init':initialize()
    else:join()


if __name__=='__main__':
    try:main()
    except Exception as error:
        print('STOP: '+str(error),file=sys.stderr);raise SystemExit(1)
