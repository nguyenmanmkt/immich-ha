#!/usr/bin/env python3
"""Step 2: install/check isolated quorum. No Immich service/grant changes."""
import argparse
import concurrent.futures
import json
from pathlib import Path
import sys
import time
import uuid
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import deployment as d
import quorum_setup as q
import quorum_stage as stage
from local_env import load_env


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    mode=parser.add_mutually_exclusive_group()
    mode.add_argument('--install',action='store_true')
    mode.add_argument('--check',action='store_true',help='Check installed quorum without uploading/restarting services')
    args=parser.parse_args()
    plan=json.loads(q.PLAN.read_text(encoding='utf-8'));stage.validate_plan(plan)
    if not args.install and not args.check:
        print('Prepared topology found. Use --install to stage isolated etcd; grant integration remains OFF.');return
    before=d.control()
    if not before['ready'] or not before['standbyEligible'] or before.get('pending') or before.get('operation',{}).get('status') not in (None,'failed','succeeded'):
        raise RuntimeError('Live Immich HA must be idle and ready before staging')
    executables=pki=None
    if args.install:
        print('Downloading/verifying pinned etcd '+stage.VERSION+' archive...',flush=True)
        executables=stage.binaries(q.PLAN.parent/'quorum-binaries'/stage.VERSION)
        pki=stage.certificates(plan,q.PLAN.parent/'quorum-pki')
    report={'version':stage.VERSION,'clusterId':plan['clusterId'],'prepared':{},'checks':{},'grantIntegrationEnabled':False,'funnelEnabled':False}
    with q.connections(load_env()['SSH_COORDINATOR_HOST']) as (clients,first):
        inventory={'primary':first}
        for node in ('backup','witness'):inventory[node]=d.remote(clients[node],q.INSPECT)
        for node in q.NODES:
            info=inventory[node]
            if info['tailscaleIp']!=plan['members'][node]['tailscaleIp'] or info['node']!=(None if node=='witness' else node):raise RuntimeError('Live node identity differs from saved plan')
            if node=='witness' and info['hostname']!='immich-ha-witness':raise RuntimeError('Wrong witness')
            if info['os']!='debian' or info['osVersion']!='13' or not info['tun']:raise RuntimeError('Unexpected node OS/TUN')
        def prepare(node):
            content,data=stage.package(plan,node,pki,executables)
            directory='/root/immich-ha-quorum-stage-'+uuid.uuid4().hex
            d.remote(clients[node],"from pathlib import Path;import json;Path(INPUT['directory']).mkdir(mode=0o700);print(json.dumps({'created':True}))",{'directory':directory})
            with clients[node].open_sftp() as sftp:
                for name,value in content.items():
                    with sftp.open(directory+'/'+name,'wb') as output:
                        sftp.chmod(directory+'/'+name,0o600);output.set_pipelined(True);output.write(value)
            result=d.remote(clients[node],stage.PREPARE,{**data,'stage':directory},timeout=180)
            print(node+': binaries/certificates/service prepared; app untouched',flush=True)
            return result
        if args.install:
            with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
                futures={node:pool.submit(prepare,node) for node in q.NODES}
                for node,future in futures.items():report['prepared'][node]=future.result()
            # All members have matching immutable identities before any starts.
            with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
                futures={node:pool.submit(d.remote,clients[node],stage.START,{'clusterId':plan['clusterId']}) for node in q.NODES}
                for node,future in futures.items():future.result();print(node+': isolated quorum service started',flush=True)
        endpoints=[f'https://{plan["members"][n]["tailscaleIp"]}:2379' for n in q.NODES]
        for node in q.NODES:
            data={'clusterId':plan['clusterId'],'etcdctl':f'/opt/immich-ha-quorum/v{stage.VERSION}/etcdctl',
                  'endpoints':endpoints,'localEndpoint':f'https://{plan["members"][node]["tailscaleIp"]}:2379'}
            deadline=time.monotonic()+90
            while True:
                try:check=d.remote(clients[node],stage.CHECK,data,timeout=90);break
                except RuntimeError:
                    if time.monotonic()>deadline:raise
                    time.sleep(3)
            stage.validate_check(plan,check)
            report['checks'][node]=check
            print(node+': three-member quorum healthy; unauthenticated client rejected',flush=True)
        checkdata={'etcdctl':f'/opt/immich-ha-quorum/v{stage.VERSION}/etcdctl','endpoints':endpoints,
                   'key':'/immich-ha/'+plan['clusterId']+'/checks/'+uuid.uuid4().hex,'value':uuid.uuid4().hex}
        try:
            d.remote(clients['witness'],stage.SMOKE,{**checkdata,'action':'put'})
            for node in q.NODES:d.remote(clients[node],stage.SMOKE,{**checkdata,'action':'get'})
            report['readWriteVerified']=True
        finally:d.remote(clients['witness'],stage.SMOKE,{**checkdata,'action':'delete'})
    after=d.control()
    report['liveHA']={key:after[key] for key in ('owner','epoch','ready','standbyEligible','auto')}
    (q.PLAN.parent/'quorum-stage-report.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    if any(after[key]!=before[key] for key in ('owner','epoch','auto')) or not after['ready'] or not after['standbyEligible']:raise RuntimeError('Quorum staged, but live HA changed; review before continuing')
    plan['stage']='isolated'
    temporary=q.PLAN.with_suffix('.tmp')
    temporary.write_text(json.dumps(plan,indent=2)+'\n',encoding='utf-8');temporary.replace(q.PLAN)
    print('STEP 2 OK: isolated quorum only; Cloudflare/Immich grants unchanged; Funnel OFF.')


if __name__=='__main__':main()
