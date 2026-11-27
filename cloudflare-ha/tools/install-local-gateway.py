#!/usr/bin/env python3
"""Checkpoint 4a: a grant-gated LAN/Tailscale endpoint; does not enable Funnel."""
import argparse
import json
from pathlib import Path
import sys
import time
import uuid

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import deployment as d
import quorum_setup as q
import local_gateway as g
from local_env import load_env


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    mode=parser.add_mutually_exclusive_group();mode.add_argument('--deploy',action='store_true');mode.add_argument('--check',action='store_true')
    parser.add_argument('--port',type=int,default=8090)
    args=parser.parse_args();g.validate_port(args.port)
    plan=json.loads(q.PLAN.read_text())
    if plan.get('stage')!='fallback-enabled':raise RuntimeError('Complete checkpoint 3c first')
    state=d.control();owner=state['owner'];epoch=state['epoch']
    if not state['ready'] or not state['standbyEligible'] or state.get('pending') or (state.get('operation') or {}).get('status') not in (None,'succeeded','failed') or (state.get('quorumControl') or {}).get('mode')!='cloudflare':raise RuntimeError('Both nodes must be idle and healthy on CF before this rollout')
    with q.connections(load_env()['SSH_COORDINATOR_HOST']) as (clients,_):
        inventory={n:d.remote(clients[n],g.PREFLIGHT,dict(node=n,clusterId=plan['clusterId'],port=args.port)) for n in ('primary','backup')}
        for n,v in inventory.items():
            if v['inRecovery']!=(n!=owner):raise RuntimeError('Unexpected database role')
        urls={n:{'tailscale':f'http://{v["tailscale"]}:{args.port}',
                 'lan':[f'http://{ip}:{args.port}' for ip in v['lan']]} for n,v in inventory.items()}
        if not args.deploy and not args.check:
            print(json.dumps({'mode':'read-only preflight','urls':urls,'funnelEnabled':bool(plan.get('funnelEnabled'))},indent=2));return
        if args.deploy:
            saved=dict(auto=state.get('autoRequested',state['auto']),recovery=state['recovery']['enabled'],preference=state.get('preference'))
            stage_id=uuid.uuid4().hex
            report_path=q.PLAN.parent/('local-gateway-rollout-'+stage_id+'.json')
            report=dict(stageId=stage_id,owner=owner,epoch=epoch,options=saved,urls=urls,before=inventory,completed=False)
            report_path.write_text(json.dumps(report,indent=2)+'\n')
            d.control('set-auto',enabled=False);d.control('set-recovery',enabled=False)
            pref=saved['preference'] or {}
            if pref.get('node'):d.control('set-preference',target=pref['node'],enabled=False)
            for n in [n for n in inventory if n!=owner]+[owner]:
                live=d.control()
                if live['owner']!=owner or live['epoch']!=epoch or live.get('pending'):raise RuntimeError('Authority changed; automation remains OFF')
                directory='/root/immich-ha-local-gateway-'+stage_id
                d.remote(clients[n],"from pathlib import Path;import json;Path(INPUT['directory']).mkdir(mode=0o700);print(json.dumps({'created':True}))",dict(directory=directory))
                with clients[n].open_sftp() as sftp:
                    for source,name in [(d.ROOT/'node/agent.py','agent.py'),(d.ROOT/'local_gateway.py','local_gateway.py'),(d.ROOT/'node/immich-ha-local.nginx.conf','immich-ha-local.nginx.conf')]:
                        sftp.put(str(source),directory+'/'+name);sftp.chmod(directory+'/'+name,0o600)
                d.remote(clients[n],g.INSTALL,dict(directory=directory,node=n,clusterId=plan['clusterId'],port=args.port,stageId=stage_id))
                print(n+': local gateway installed; app/database not restarted',flush=True)
            until=time.monotonic()+150
            while True:
                live=d.control()
                if live['owner']!=owner or live['epoch']!=epoch:raise RuntimeError('Authority changed; automation remains OFF')
                if live['ready'] and live['standbyEligible']:break
                if time.monotonic()>until:raise RuntimeError('Agent readiness not confirmed; automation remains OFF')
                time.sleep(5)
        checks={}
        for n in inventory:
            checks[n]=d.remote(clients['witness'],g.CHECK,{'url':urls[n]['tailscale']})
            expected=200 if n==owner else 503
            if checks[n]['ping']['status']!=expected or checks[n]['spoof']['status']!=expected or checks[n]['internal']['status']!=404 or checks[n]['control']['status']!=404:
                raise RuntimeError('Private gateway/standby/header isolation check failed on '+n)
            if n==owner and json.loads(checks[n]['ping']['body']).get('res')!='pong':raise RuntimeError('Active gateway is not serving Immich')
        after={n:d.remote(clients[n],g.PREFLIGHT,dict(node=n,clusterId=plan['clusterId'],port=args.port)) for n in inventory}
        for n in inventory:
            if any(inventory[n][key]!=after[n][key] for key in ('appPid','pgPid','inRecovery')):raise RuntimeError('Unexpected application/database restart')
        if args.deploy:
            d.control('set-recovery',enabled=saved['recovery'])
            if pref.get('node'):d.control('set-preference',target=pref['node'],enabled=pref.get('enabled',False))
            d.control('set-auto',enabled=saved['auto'])
            report.update(checks=checks,after=after,completed=True)
            report_path.write_text(json.dumps(report,indent=2)+'\n')
            plan['localGateway']={'enabled':True,'port':args.port};q.PLAN.write_text(json.dumps(plan,indent=2)+'\n')
        print(json.dumps({'gateway':'ready','active':owner,'epoch':epoch,'urls':urls,
            'checks':{n:checks[n]['ping']['status'] for n in checks},'funnelEnabled':bool(plan.get('funnelEnabled'))},indent=2))


if __name__=='__main__':main()
