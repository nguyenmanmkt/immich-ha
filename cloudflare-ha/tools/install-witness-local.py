#!/usr/bin/env python3
"""Optional witness and Funnel setup, executed locally; never opens SSH."""
import argparse
import getpass
import importlib.util
import json
import os
from pathlib import Path
import secrets
import sys
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'node'))
from cluster_bootstrap import atomic,run
from cluster_service import exchange


def installer():
    spec=importlib.util.spec_from_file_location('cluster_cli',ROOT/'tools/install-cluster.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=('witness-init','witness-join','funnel-enable'))
    args=p.parse_args()
    if sys.platform!='linux' or os.geteuid()!=0:raise RuntimeError('Run as root inside the selected LXC')
    path=ROOT/'.install/node.json'
    if args.action=='witness-init':
        local=json.loads(path.read_text())
        if local['node']!='primary':raise RuntimeError('Run witness-init on the original init node A, regardless of its current role')
        cli=installer();code=secrets.token_urlsafe(32)
        token=json.loads((ROOT/'generated/worker-secrets.json').read_text())['ADMIN_TOKEN']
        value={'id':str(uuid.uuid4()),'bundle':cli.bundle_digest(),'joinHash':__import__('hashlib').sha256(code.encode()).hexdigest(),'token':secrets.token_urlsafe(48)}
        exchange(local['controller'],'/__ha/witness-init',value,token)
        run(['systemctl','restart','immich-ha-bootstrap.service'])
        print('Optional witness requested. Automation is paused until installation is verified.')
        print('Controller URL: '+local['controller']);print('Witness join code (30 minutes): '+code)
    elif args.action=='witness-join':
        if path.exists() or Path('/etc/immich-ha-quorum').exists() or Path('/opt/immich/.env').exists():raise RuntimeError('Witness must be a separate fresh LXC; existing identities retained')
        cli=installer();controller=cli.ask('Controller URL from A').rstrip('/')
        from urllib.parse import urlsplit
        u=urlsplit(controller)
        if u.scheme!='https' or not u.hostname or not u.hostname.endswith('.workers.dev') or u.path or u.query or u.fragment or u.username or u.password or u.port:raise ValueError('Use the HTTPS workers.dev controller URL')
        code=cli.ask('Witness join code',secret=True);network=cli.local_network()
        from witness_bootstrap import inspect_host
        inspect_host(network,True)
        claim_path=ROOT/'.install/witness-claim'
        claim=claim_path.read_text().strip() if claim_path.exists() else secrets.token_urlsafe(48)
        atomic(claim_path,claim)
        value=exchange(controller,'/__ha/witness-join',dict(code=code,claim=claim,bundle=cli.bundle_digest(),ip=network['ip']))
        if value['networkMode']!=network['networkMode']:raise RuntimeError('Use the same private network mode as A/B')
        cli.install_service(value)
        print('Witness joined. Follow the optional extension on the dashboard; Funnel is still off.')
    else:
        from witness_bootstrap import enable_funnel
        local=json.loads(path.read_text())
        response=exchange(local['controller'],'/__ha/witness-node',{'node':local['node'],'token':local['token']})
        if response.get('extension',{}).get('status')!='completed':raise RuntimeError('Finish and verify witness/gateway installation first')
        report=enable_funnel(local)
        exchange(local['controller'],'/__ha/witness-node',{'node':local['node'],'token':local['token'],'funnel':report})
        print('Verified public Funnel: '+report['url'])


if __name__=='__main__':
    try:main()
    except Exception:
        import traceback
        atomic(ROOT/'.install/witness-local-error.log',traceback.format_exc())
        print('STOP: local extension setup did not complete; keep private files and inspect node journal/checkpoints. No cluster reset was performed.',file=sys.stderr)
        print('Details (root only): '+str(ROOT/'.install/witness-local-error.log'),file=sys.stderr)
        raise SystemExit(1)
