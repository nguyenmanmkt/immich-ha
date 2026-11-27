#!/usr/bin/env python3
"""Checkpoint 4b: persistent Funnel per node, targeting only the gated gateway."""
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
import funnel_setup as f
from local_env import load_env


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    mode=parser.add_mutually_exclusive_group();mode.add_argument('--deploy',action='store_true');mode.add_argument('--check',action='store_true')
    parser.add_argument('--port',type=int,choices=(443,8443,10000),default=443)
    args=parser.parse_args();plan=json.loads(q.PLAN.read_text())
    if plan.get('stage')!='fallback-enabled' or not plan.get('localGateway',{}).get('enabled'):raise RuntimeError('Complete local gateway checkpoint first')
    state=d.control();expected={k:state[k] for k in ('owner','epoch')}
    if not state['ready'] or not state['standbyEligible'] or state.get('pending') or (state.get('operation') or {}).get('status') not in (None,'succeeded','failed'):raise RuntimeError('Two idle healthy nodes required')
    stage_id=uuid.uuid4().hex
    report={'stageId':stage_id,'before':expected,'port':args.port,'completed':False,'nodes':{}}
    report_path=q.PLAN.parent/('funnel-rollout-'+stage_id+'.json')
    with q.connections(load_env()['SSH_COORDINATOR_HOST']) as (clients,_):
        inventory={n:d.remote(clients[n],f.PREFLIGHT,dict(node=n,clusterId=plan['clusterId'])) for n in ('primary','backup')}
        baseline={n:d.remote(clients[n],g.PREFLIGHT,dict(node=n,clusterId=plan['clusterId'],port=inventory[n]['gatewayPort'])) for n in inventory}
        for n,value in inventory.items():
            f.validate_existing(value['serve'],value['dnsName'],args.port,value['gatewayPort'])
            if not value['supportsBackground'] or not value['supportsYes']:raise RuntimeError('Unsupported Funnel CLI; no settings changed')
        if not args.deploy and not args.check:
            print(json.dumps({'mode':'read-only','nodes':{n:{'dnsName':v['dnsName'],'gatewayPort':v['gatewayPort'],'existingServe':bool(v['serve'])} for n,v in inventory.items()}},indent=2));return
        if args.deploy:
            report['snapshots']=inventory;report_path.write_text(json.dumps(report,indent=2)+'\n')
            # Standby first: validate its public denial before exposing active.
            order=[n for n in inventory if n!=state['owner']]+[state['owner']]
            for n in order:
                live=d.control()
                if any(live[k]!=expected[k] for k in expected):raise RuntimeError('Authority changed; stop this rollout')
                directory='/root/immich-ha-funnel-'+stage_id
                d.remote(clients[n],"from pathlib import Path;import json;Path(INPUT['directory']).mkdir(mode=0o700);print(json.dumps({'created':True}))",dict(directory=directory))
                with clients[n].open_sftp() as sftp:
                    sftp.put(str(d.ROOT/'funnel_setup.py'),directory+'/funnel_setup.py');sftp.chmod(directory+'/funnel_setup.py',0o600)
                v=inventory[n]
                result=d.remote(clients[n],f.APPLY,dict(directory=directory,before=v['serve'],node=n,clusterId=plan['clusterId'],stageId=stage_id,
                    dnsName=v['dnsName'],port=args.port,gatewayPort=v['gatewayPort']),timeout=60)
                report['nodes'][n]=result;report_path.write_text(json.dumps(report,indent=2)+'\n')
                if not result['configured']:
                    print(json.dumps({'node':n,'configured':False,'approvalUrls':result['approvalUrls'],'otherSettingsRetained':True},indent=2))
                    raise RuntimeError('Funnel not configured. Complete the Tailscale feature approval if shown; inspect private snapshots before retrying.')
                f.validate_after(v['serve'],result['after'],v['dnsName'],args.port,v['gatewayPort'])
                print(n+': persistent Funnel configured to gated gateway',flush=True)
        urls={}
        for n,v in inventory.items():
            status=d.remote(clients[n],f.PREFLIGHT,dict(node=n,clusterId=plan['clusterId']))
            f.validate_after(v['serve'],status['serve'],v['dnsName'],args.port,v['gatewayPort'])
            until=time.monotonic()+100
            while True:
                try:
                    addresses=f.public_addresses(v['dnsName']);checks={}
                    for label,path in [('ping','/api/server/ping'),('privateApi','/api/users/me'),('internal','/_immich_ha_local_authorize'),('control','/__ha/health')]:
                        checks[label]=f.public_get(v['dnsName'],addresses[0],path,args.port)
                    expected_code=200 if n==state['owner'] else 503
                    if checks['ping']['status']!=expected_code:raise RuntimeError('Public ingress not ready or node role differs')
                    if checks['internal']['status']!=404 or checks['control']['status']!=404:raise RuntimeError('Internal endpoints exposed')
                    if n==state['owner'] and (json.loads(checks['ping']['body']).get('res')!='pong' or checks['privateApi']['status'] not in (401,403)):raise RuntimeError('Immich authentication check failed')
                    break
                except Exception:
                    if time.monotonic()>=until:raise
                    time.sleep(5)
            urls[n]='https://'+v['dnsName']+('' if args.port==443 else ':'+str(args.port))
            report.setdefault('publicChecks',{})[n]={'addresses':addresses,'checks':checks}
            print(n+': public ingress '+str(checks['ping']['status'])+' verified via global IP (not MagicDNS)',flush=True)
        after={n:d.remote(clients[n],g.PREFLIGHT,dict(node=n,clusterId=plan['clusterId'],port=inventory[n]['gatewayPort'])) for n in inventory}
        for n in inventory:
            if any(after[n][k]!=baseline[n][k] for k in ('appPid','pgPid','inRecovery')):raise RuntimeError('Unexpected application/database restart')
    live=d.control()
    if any(live[k]!=expected[k] for k in expected):raise RuntimeError('Authority changed during verification')
    report.update(completed=True,urls=urls)
    if args.deploy:
        report_path.write_text(json.dumps(report,indent=2)+'\n')
        plan['funnelEnabled']=True;plan['funnel']={'port':args.port,'urls':urls};q.PLAN.write_text(json.dumps(plan,indent=2)+'\n')
    print(json.dumps({'funnel':'verified','active':state['owner'],'epoch':state['epoch'],'urls':urls},indent=2))


if __name__=='__main__':main()
