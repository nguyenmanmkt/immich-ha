#!/usr/bin/env python3
"""Install a common LAN/Tailscale witness ingress; does not enable its Funnel."""
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
import witness_gateway_setup as w
from local_env import load_env

CHECK=r'''
import json,urllib.request,urllib.error
def get(path):
 try:
  with urllib.request.urlopen(INPUT['url']+path,timeout=12) as r:return {'status':r.status,'body':r.read(2048).decode(errors='replace'),'node':r.headers.get('X-Immich-HA-Node'),'epoch':r.headers.get('X-Immich-HA-Epoch')}
 except urllib.error.HTTPError as r:return {'status':r.code,'body':r.read(2048).decode(errors='replace')}
print(json.dumps({'ping':get('/api/server/ping'),'private':get('/api/users/me'),'internal':get('/_immich_witness_route'),'control':get('/__ha/health')}))
'''


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    mode=parser.add_mutually_exclusive_group();mode.add_argument('--deploy',action='store_true');mode.add_argument('--check',action='store_true')
    parser.add_argument('--port',type=int,default=8090)
    args=parser.parse_args();g.validate_port(args.port)
    plan=json.loads(q.PLAN.read_text());state=d.control()
    if plan.get('stage')!='fallback-enabled' or not plan.get('localGateway',{}).get('enabled'):raise RuntimeError('Complete node gateways first')
    if not state['ready'] or not state['standbyEligible'] or state.get('pending') or (state.get('operation') or {}).get('status') not in (None,'succeeded','failed'):raise RuntimeError('Two idle healthy Immich nodes required')
    expected={k:state[k] for k in ('owner','epoch')}
    with q.connections(load_env()['SSH_COORDINATOR_HOST']) as (clients,_):
        witness=d.remote(clients['witness'],w.PREFLIGHT,dict(clusterId=plan['clusterId'],port=args.port))
        if witness['tailscale']!=plan['members']['witness']['tailscaleIp'] or witness['freeBytes']<512*1024**2:raise RuntimeError('Unexpected witness identity or insufficient space')
        baseline={n:d.remote(clients[n],g.PREFLIGHT,dict(node=n,clusterId=plan['clusterId'],port=plan['localGateway']['port'])) for n in ('primary','backup')}
        ports={n:plan['localGateway']['port'] for n in baseline}
        cfg=w.configuration(plan,ports)
        urls={'tailscale':f'http://{witness["tailscale"]}:{args.port}',
              'lan':[f'http://{ip}:{args.port}' for ip in witness['lan']]}
        if not args.deploy and not args.check:print(json.dumps({'mode':'read-only preflight','witness':witness,'urls':urls},indent=2));return
        if args.deploy:
            stage_id=uuid.uuid4().hex;report_path=q.PLAN.parent/('witness-gateway-rollout-'+stage_id+'.json')
            report={'stageId':stage_id,'before':expected,'nodePids':baseline,'completed':False,'urls':urls}
            report_path.write_text(json.dumps(report,indent=2)+'\n')
            # Trust HTTPS forwarding from this exact witness IP only. These are
            # Nginx reloads; no agent, app or PostgreSQL restart is required.
            for n in baseline:
                directory='/root/immich-witness-trust-'+stage_id
                d.remote(clients[n],"from pathlib import Path;import json;Path(INPUT['directory']).mkdir(mode=0o700);print(json.dumps({'created':True}))",dict(directory=directory))
                with clients[n].open_sftp() as sftp:
                    for source,name in [(d.ROOT/'local_gateway.py','local_gateway.py'),(d.ROOT/'node/immich-ha-local.nginx.conf','immich-ha-local.nginx.conf')]:
                        sftp.put(str(source),directory+'/'+name);sftp.chmod(directory+'/'+name,0o600)
                d.remote(clients[n],w.TRUST_SCHEME,dict(directory=directory,node=n,clusterId=plan['clusterId'],witnessIp=witness['tailscale']))
            directory='/root/immich-witness-gateway-'+stage_id
            d.remote(clients['witness'],"from pathlib import Path;import json;Path(INPUT['directory']).mkdir(mode=0o700);print(json.dumps({'created':True}))",dict(directory=directory))
            files={'witness_router.py':(d.ROOT/'node/witness_router.py').read_bytes(),
                   'router.service':w.UNIT.encode(),'router.json':json.dumps(cfg,indent=2).encode(),
                   'witness.nginx.conf':w.render((d.ROOT/'node/immich-ha-witness.nginx.conf').read_text(),cfg,args.port).encode()}
            with clients['witness'].open_sftp() as sftp:
                for name,data in files.items():
                    with sftp.open(directory+'/'+name,'wb') as output:sftp.chmod(directory+'/'+name,0o600);output.write(data)
            d.remote(clients['witness'],w.INSTALL,dict(directory=directory,clusterId=plan['clusterId'],port=args.port),timeout=300)
            print('Witness router installed; Immich nodes and quorum not restarted.',flush=True)
        until=time.monotonic()+45
        while True:
            checks=d.remote(clients['backup'],CHECK,{'url':urls['tailscale']})
            if checks['ping']['status']==200:break
            if time.monotonic()>until:raise RuntimeError('Witness gateway not ready; inspect selector/Nginx logs')
            time.sleep(2)
        ping=checks['ping']
        if json.loads(ping['body']).get('res')!='pong' or ping.get('node')!=expected['owner'] or ping.get('epoch')!=str(expected['epoch']):raise RuntimeError('Route is not the expected authoritative owner')
        if checks['private']['status'] not in (401,403) or checks['internal']['status']!=404 or checks['control']['status']!=404:raise RuntimeError('Authentication/internal endpoint check failed')
        after={n:d.remote(clients[n],g.PREFLIGHT,dict(node=n,clusterId=plan['clusterId'],port=ports[n])) for n in baseline}
        if any(after[n][k]!=baseline[n][k] for n in baseline for k in ('appPid','pgPid','inRecovery')):raise RuntimeError('Unexpected Immich/database restart')
        live=d.control()
        if any(live[k]!=expected[k] for k in expected) or live['auto']!=state['auto'] or live['recovery']['enabled']!=state['recovery']['enabled']:raise RuntimeError('Control state changed; inspect rollout')
        if args.deploy:
            report.update(completed=True,checks=checks,after=after);report_path.write_text(json.dumps(report,indent=2)+'\n')
            plan['witnessGateway']={'enabled':True,'port':args.port,'urls':urls};q.PLAN.write_text(json.dumps(plan,indent=2)+'\n')
        print(json.dumps({'witnessGateway':'ready','route':expected,'urls':urls,'commonFunnelEnabled':bool(plan.get('witnessFunnel',{}).get('enabled'))},indent=2))


if __name__=='__main__':main()
