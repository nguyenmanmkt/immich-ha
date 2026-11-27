#!/usr/bin/env python3
"""Explicit TLS compatibility update for an existing isolated quorum; keeps keys."""
import argparse
import json
from pathlib import Path
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import deployment as d
import quorum_setup as q
import quorum_stage as stage
from local_env import load_env


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--apply',action='store_true');args=parser.parse_args()
    plan=json.loads(q.PLAN.read_text());stage.validate_plan(plan)
    if not args.apply:print('Use --apply only for isolated quorum: preserve CA/private keys, add leaf AKI and configure authenticated JSON gateway.');return
    pki=q.PLAN.parent/'quorum-pki';journal=q.PLAN.parent/'quorum-cert-compat-changes.json'
    if journal.exists():changes=json.loads(journal.read_text())
    else:
        changes=stage.add_client_key_identifiers(plan,pki);journal.write_text(json.dumps(changes,indent=2)+'\n')
    endpoints=[f'https://{plan["members"][node]["tailscaleIp"]}:2379' for node in q.NODES]
    with q.connections(load_env()['SSH_COORDINATOR_HOST']) as (clients,first):
        for node,client in clients.items():
            leaves={kind+'.crt':{**changes[node+'-'+kind+'.crt'],'content':(pki/(node+'-'+kind+'.crt')).read_bytes().hex()} for kind in ('server','probe') if node+'-'+kind+'.crt' in changes}
            result=d.remote(client,stage.TLS_COMPAT,{'clusterId':plan['clusterId'],'node':node,'config':stage.configuration(plan,node),'changes':leaves})
            deadline=time.monotonic()+60
            while True:
                try:
                    check=d.remote(client,stage.CHECK,{'clusterId':plan['clusterId'],'etcdctl':f'/opt/immich-ha-quorum/v{stage.VERSION}/etcdctl','endpoints':endpoints,'localEndpoint':f'https://{plan["members"][node]["tailscaleIp"]}:2379'})
                    stage.validate_check(plan,check);break
                except RuntimeError:
                    if time.monotonic()>deadline:raise
                    time.sleep(2)
            print(node+': TLS compatibility confirmed; three members healthy; keys unchanged',flush=True)


if __name__=='__main__':main()
