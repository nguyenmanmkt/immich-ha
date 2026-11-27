#!/usr/bin/env python3
"""Step 1: inspect three LXCs and save a private plan; no remote changes."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import deployment as d
import quorum_setup as q
from local_env import load_env


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--witness-host',default=None)
    args=parser.parse_args()
    host=args.witness_host or load_env().get('SSH_COORDINATOR_HOST')
    if not host:raise ValueError('Set SSH_COORDINATOR_HOST in .env')
    before=d.control()
    if not before['ready'] or not before['standbyEligible']:
        raise RuntimeError('Both serving node and standby must be ready before preparing quorum')
    if before.get('pending') or before.get('operation',{}).get('status') not in (None,'failed','succeeded'):
        raise RuntimeError('Wait for the live HA operation to finish')
    inventory={};links={}
    with q.connections(host) as (clients,first):
        inventory['primary']=first
        for node in ('backup','witness'):inventory[node]=d.remote(clients[node],q.INSPECT)
        for node in q.NODES:
            peers={other:inventory[other]['tailscaleIp'] for other in q.NODES if other!=node}
            links[node]=d.remote(clients[node],q.PING,{'peers':peers})
            print(json.dumps({'node':node,**inventory[node],'links':links[node]},ensure_ascii=False),flush=True)
    plan,owner=q.prepare_plan(inventory,links)
    after=d.control()
    if after['owner']!=before['owner'] or after['epoch']!=before['epoch'] or owner!=after['owner'] or not after['ready'] or not after['standbyEligible']:
        raise RuntimeError('HA changed during inspection; plan saved but not ready for installation')
    report={'inventory':inventory,'links':links,'cloudflareOwner':after['owner'],'epoch':after['epoch'],
            'ready':after['ready'],'standbyEligible':after['standbyEligible'],'auto':after['auto']}
    (q.PLAN.parent/'quorum-preflight.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print('STEP 1 OK: private topology saved; no packages, services, grants or Funnel changed.')


if __name__=='__main__':main()
