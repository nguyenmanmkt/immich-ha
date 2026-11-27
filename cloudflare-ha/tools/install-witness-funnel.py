#!/usr/bin/env python3
"""Expose the validated witness ingress as one persistent, common Funnel URL."""
import argparse
import json
from pathlib import Path
import sys
import time
import uuid

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import deployment as d
import quorum_setup as q
import witness_gateway_setup as w
import local_gateway as g
import funnel_setup as f
from local_env import load_env

STATUS=r'''
import json,subprocess,urllib.request
tail=json.loads(subprocess.check_output(['tailscale','status','--json'],text=True,timeout=15))
if tail.get('BackendState')!='Running':raise RuntimeError('Tailscale is not running')
serve=json.loads(subprocess.check_output(['tailscale','serve','status','--json'],text=True,timeout=15)) or {}
help_text=subprocess.check_output(['tailscale','funnel','--help'],stderr=subprocess.STDOUT,text=True,timeout=10)
request=urllib.request.Request('http://127.0.0.1:'+str(INPUT['gatewayPort'])+'/api/server/ping')
with urllib.request.urlopen(request,timeout=10) as response:
 local=dict(status=response.status,body=json.load(response),node=response.headers.get('X-Immich-HA-Node'),epoch=response.headers.get('X-Immich-HA-Epoch'))
print(json.dumps({'dnsName':tail['Self']['DNSName'].rstrip('.'),'serve':serve,
 'supportsCli':'--bg' in help_text and '--yes' in help_text,'local':local}))
'''


def check_public(dns,port,owner,epoch):
    addresses=f.public_addresses(dns)
    checks={}
    for name,path in [('ping','/api/server/ping'),('private','/api/users/me'),('internal','/_immich_witness_route'),('control','/__ha/health')]:
        checks[name]=f.public_get(dns,addresses[0],path,port)
    ping=checks['ping']
    if ping['status']!=200 or json.loads(ping['body']).get('res')!='pong' or ping.get('node')!=owner or ping.get('epoch')!=str(epoch):raise RuntimeError('Common public route does not match the expected authority')
    if checks['private']['status'] not in (401,403) or checks['internal']['status']!=404 or checks['control']['status']!=404:raise RuntimeError('Public authentication/internal-route check failed')
    return {'addresses':addresses,'checks':checks}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    mode=parser.add_mutually_exclusive_group();mode.add_argument('--deploy',action='store_true');mode.add_argument('--check',action='store_true')
    parser.add_argument('--port',type=int,choices=(443,8443,10000),default=443)
    args=parser.parse_args();plan=json.loads(q.PLAN.read_text())
    if not plan.get('witnessGateway',{}).get('enabled'):raise RuntimeError('Validate the witness LAN/Tailscale gateway first')
    gateway_port=plan['witnessGateway']['port'];state=d.control()
    if not state['ready'] or not state['standbyEligible'] or state.get('pending') or (state.get('operation') or {}).get('status') not in (None,'failed','succeeded'):raise RuntimeError('Idle healthy HA required before this rollout')
    expected={k:state[k] for k in ('owner','epoch')}
    stage_id=uuid.uuid4().hex
    report={'stageId':stage_id,'before':expected,'completed':False}
    report_path=q.PLAN.parent/('witness-funnel-rollout-'+stage_id+'.json')
    with q.connections(load_env()['SSH_COORDINATOR_HOST']) as (clients,_):
        witness=d.remote(clients['witness'],w.PREFLIGHT,dict(clusterId=plan['clusterId'],port=gateway_port))
        status=d.remote(clients['witness'],STATUS,dict(gatewayPort=gateway_port))
        if not status['supportsCli']:raise RuntimeError('Unsupported Funnel CLI; no changes made')
        if status['local']['node']!=expected['owner'] or status['local']['epoch']!=str(expected['epoch']) or status['local']['body'].get('res')!='pong':raise RuntimeError('Witness local route differs from current owner')
        f.validate_existing(status['serve'],status['dnsName'],args.port,gateway_port)
        url='https://'+status['dnsName']+('' if args.port==443 else ':'+str(args.port))
        if not args.deploy and not args.check:
            print(json.dumps({'mode':'read-only','url':url,'existingServe':bool(status['serve']),'route':expected},indent=2));return
        baseline={n:d.remote(clients[n],g.PREFLIGHT,dict(node=n,clusterId=plan['clusterId'],port=plan['localGateway']['port'])) for n in ('primary','backup')}
        if args.deploy:
            report.update(serveBefore=status['serve'],nodePids=baseline)
            report_path.write_text(json.dumps(report,indent=2)+'\n')
            directory='/root/immich-ha-witness-funnel-'+stage_id
            d.remote(clients['witness'],"from pathlib import Path;import json;Path(INPUT['directory']).mkdir(mode=0o700);print(json.dumps({'created':True}))",dict(directory=directory))
            with clients['witness'].open_sftp() as sftp:
                sftp.put(str(d.ROOT/'funnel_setup.py'),directory+'/funnel_setup.py');sftp.chmod(directory+'/funnel_setup.py',0o600)
            result=d.remote(clients['witness'],f.APPLY,dict(directory=directory,before=status['serve'],dnsName=status['dnsName'],port=args.port,
                gatewayPort=gateway_port,stageId=stage_id,node='witness',clusterId=plan['clusterId']),timeout=60)
            report['apply']=result;report_path.write_text(json.dumps(report,indent=2)+'\n')
            if not result['configured']:
                print(json.dumps({'configured':False,'approvalUrls':result['approvalUrls']},indent=2))
                raise RuntimeError('Witness Funnel not configured; inspect snapshots and feature approval')
            f.validate_after(status['serve'],result['after'],status['dnsName'],args.port,gateway_port)
            print('Common witness Funnel configured; checking global Internet ingress.',flush=True)
        else:
            f.validate_after(status['serve'],status['serve'],status['dnsName'],args.port,gateway_port)
        until=time.monotonic()+100
        while True:
            try:
                public=check_public(status['dnsName'],args.port,expected['owner'],expected['epoch']);break
            except Exception:
                if time.monotonic()>=until:raise
                time.sleep(5)
        after={n:d.remote(clients[n],g.PREFLIGHT,dict(node=n,clusterId=plan['clusterId'],port=plan['localGateway']['port'])) for n in baseline}
        if any(after[n][k]!=baseline[n][k] for n in baseline for k in ('appPid','pgPid','inRecovery')):raise RuntimeError('Unexpected application/database restart')
        live=d.control()
        if any(live[k]!=expected[k] for k in expected):raise RuntimeError('Authority changed during verification; inspect live route')
        if live['auto']!=state['auto'] or live['recovery']['enabled']!=state['recovery']['enabled']:raise RuntimeError('Automation choices changed')
        if args.deploy:
            report.update(completed=True,public=public,url=url,after=after)
            report_path.write_text(json.dumps(report,indent=2)+'\n')
            plan['witnessFunnel']={'enabled':True,'port':args.port,'url':url};q.PLAN.write_text(json.dumps(plan,indent=2)+'\n')
        print(json.dumps({'commonFunnel':'verified','url':url,'route':expected,'publicStatus':public['checks']['ping']['status'],
            'privateApiStatus':public['checks']['private']['status'],'directNodeFunnelsRetained':True},indent=2))


if __name__=='__main__':main()
