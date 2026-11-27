"""Packaged local actions for optional witness enrollment. Never uses SSH.

Controller data selects a fixed action and topology; never code, shell or paths.
CA signing keys remain on A. Only each member's leaf keys are distributed.
"""
import hashlib
import copy
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import platform
import secrets
import shutil
import subprocess
import time
import urllib.error
import urllib.request
import uuid

from cluster_bootstrap import APP,atomic,payload,run

NODES=('primary','backup','witness')


def module(name):
    # Filenames are constants selected by this package, never controller input.
    if name not in ('quorum-enforce','quorum-fallback'):raise ValueError('Unknown local module')
    spec=importlib.util.spec_from_file_location('local_'+name.replace('-','_'),APP/'tools'/f'{name}.py')
    value=importlib.util.module_from_spec(spec);spec.loader.exec_module(value);return value


def inspect_host(local,witness=False):
    from quorum_lock import private_address
    ip=str(private_address(local['ip'],local['networkMode']))
    if os.geteuid()!=0 or platform.machine()!='x86_64':raise RuntimeError('Root Debian amd64 LXC required')
    release=Path('/etc/os-release').read_text()
    if '\nID=debian\n' not in '\n'+release or not any(x in release for x in ('VERSION_ID="13"','VERSION_ID=13\n')):raise RuntimeError('Debian 13 required')
    addresses=json.loads(run(['ip','-j','-4','addr','show']))
    if not any(a.get('local')==ip for i in addresses for a in i.get('addr_info',[])):raise RuntimeError('Selected private address is not local')
    if local['networkMode']=='tailscale' and run(['tailscale','ip','-4'])!=ip:raise RuntimeError('Tailnet address mismatch')
    existing=any(Path(p).exists() for p in ('/etc/immich-ha-quorum','/var/lib/immich-ha-quorum','/etc/immich-ha/quorum-required','/etc/immich-ha/quorum-authority.json'))
    listeners=run(['ss','-H','-lnt'])
    busy=any(line.split()[3].endswith(':'+str(port)) for line in listeners.splitlines() if len(line.split())>=4 for port in (2379,2380,8090))
    memory=int(next(x.split()[1] for x in Path('/proc/meminfo').read_text().splitlines() if x.startswith('MemTotal:')))*1024
    result={'verified':True,'ip':ip,'machineId':Path('/etc/machine-id').read_text().strip(),'existingQuorum':existing,
        'busy':busy,'freeBytes':shutil.disk_usage('/').free,'memoryBytes':memory}
    if existing or busy or result['freeBytes']<1024**3 or memory<768*1024**2:raise RuntimeError('Quorum preflight failed; existing installation/ports/data retained')
    if witness:
        if Path('/opt/immich/.env').exists() or Path('/etc/immich-ha/config.json').exists():raise RuntimeError('Witness must not be an Immich node')
    else:
        from pg_stream import status
        pg=status();result.update({key:pg.get(key) for key in ('systemId','inRecovery','receiverConnected')})
    return result


def config(args):
    from quorum_stage import member_ip
    return dict(version=1,mode='enforced',fallbackEnabled=False,networkMode=args['networkMode'],clusterId=args['plan']['clusterId'],
        node=args['node'] if args['node']!='witness' else 'primary',
        endpoints=[f'https://{member_ip(args["plan"]["members"][n])}:2379' for n in NODES],
        ca='/etc/immich-ha-quorum/client-ca.crt',cert='/etc/immich-ha-quorum/probe.crt',key='/etc/immich-ha-quorum/probe.key')


def wait(check,seconds=180):
    deadline=time.monotonic()+seconds
    while True:
        try:return check()
        except (RuntimeError,OSError,ValueError,KeyError):
            if time.monotonic()>=deadline:raise RuntimeError('Verification timed out; retained checkpoint, no automatic rollback') from None
            time.sleep(3)


def pin_command(command,local):
    import quorum_stage as stage
    args=command['args'];plan=args['plan'];stage.validate_plan(plan)
    node=local['node']
    if node not in NODES or args['node']!=node or args['setupId']!=plan['clusterId'] or args['networkMode']!=local['networkMode'] or plan['networkMode']!=local['networkMode'] or stage.member_ip(plan['members'][node])!=local['ip']:
        raise RuntimeError('Witness command identity mismatch')
    identity={'plan':plan,'owner':args['owner'],'epoch':args['epoch']}
    if args['owner'] not in ('primary','backup') or type(args['epoch']) is not int or args['epoch']<1:raise RuntimeError('Invalid pinned authority')
    path=APP/'.install/witness.json'
    if path.exists():
        if json.loads(path.read_text())!=identity:raise RuntimeError('Existing witness identity retained')
    elif command['action']=='witness-inspect':atomic(path,json.dumps(identity)+'\n')
    else:raise RuntimeError('Inspect and pin witness identity first')
    return args


def directory(action):
    path=APP/'.install/witness-actions'/action
    path.mkdir(parents=True,mode=0o700,exist_ok=True)
    return path


def execute(command,local):
    args=pin_command(command,local);action=command['action'];node=args['node'];plan=args['plan']
    import quorum_stage as stage
    if action=='witness-inspect':
        result=inspect_host(local,node=='witness')
        if node!='witness' and result['inRecovery']!=(node!=args['owner']):raise RuntimeError('Unexpected PostgreSQL role')
        return result
    if action=='witness-pki':
        if node!='primary':raise RuntimeError('Only init node A signs the new cluster')
        pki=stage.certificates(plan,APP/'.install/witness-pki')
        bundles={}
        for member in NODES:
            bundles[member]={name:(pki/name).read_text() for name in ('peer-ca.crt','client-ca.crt')}
            bundles[member].update({f'{kind}.{ext}':(pki/f'{member}-{kind}.{ext}').read_text() for kind in ('peer','server','probe') for ext in ('crt','key')})
        return {'verified':True,'certificates':bundles}
    if action=='witness-install':
        files=args['certificates'];expected={'peer-ca.crt','client-ca.crt',*[f'{k}.{e}' for k in ('peer','server','probe') for e in ('crt','key')]}
        if set(files)!=expected:raise RuntimeError('Invalid certificate package')
        pki=directory('member-pki')
        # package() reads node-prefixed leaves; no CA private keys are accepted.
        for name,value in files.items():
            if not isinstance(value,str) or len(value)>10000:raise RuntimeError('Invalid certificate content')
            atomic(pki/(name if '-ca.' in name else node+'-'+name),value)
        binaries=stage.binaries(APP/'.install/quorum-binaries')
        content,data=stage.package(plan,node,pki,binaries)
        staging=directory('install')
        for name,value in content.items():
            path=staging/name;path.write_bytes(value);path.chmod(0o600)
        payload(stage.PREPARE,{**data,'stage':str(staging)})
        return {'verified':True}
    if action=='witness-start':
        payload(stage.START,{'clusterId':plan['clusterId']});return {'verified':True}
    if action=='witness-check':
        endpoints=config(args)['endpoints']
        def check():
            result=payload(stage.CHECK,dict(clusterId=plan['clusterId'],etcdctl=f'/opt/immich-ha-quorum/v{stage.VERSION}/etcdctl',endpoints=endpoints,localEndpoint=f'https://{local["ip"]}:2379'))
            stage.validate_check(plan,result)
            from quorum_lock import ShadowLock,Etcd
            cfg={**config(args),'mode':'shadow'};cfg.pop('fallbackEnabled')
            lock=ShadowLock(Etcd(cfg),plan['clusterId'],'installer-'+uuid.uuid4().hex)
            grant=lock.claim('cloudflare','primary',1,'installer_check_session_12345',5)
            if not grant.get('admitted'):raise RuntimeError('Linearizable lease check failed')
            lock.client.call('/v3/lease/revoke',{'ID':lock.read()[1]['lease']})
            return {'verified':True}
        return wait(check)
    if action=='witness-enforce':
        enforce=module('quorum-enforce')
        Path('/usr/local/lib/immich-ha').mkdir(mode=0o700,parents=True,exist_ok=True)
        # All inputs/paths come from the local package. Install no remote code.
        staging=directory('runtime');sources={}
        names=['quorum_lock.py','quorum_authority.py']
        if node!='witness':names+=['ha_runtime.py','pg_stream.py','immich_upgrade.py']
        for name in names:
            shutil.copyfile(APP/'node'/name,staging/name);sources['/usr/local/lib/immich-ha/'+name]=name
        if node!='witness':
            for destination,name in [('/usr/local/lib/immich-ha/ha_agent.py','agent.py'),('/usr/local/sbin/immich-ha-agent','agent.py'),('/usr/local/sbin/immich-ha-app-run','app-run-v2')]:
                shutil.copyfile(APP/'node'/name,staging/name);sources[destination]=name
        payload(enforce.INSTALL,dict(stageId=uuid.uuid4().hex,files=sources,directory=str(staging)))
        payload(enforce.ENABLE,dict(clusterId=plan['clusterId'],node=node,config=config(args)))
        return {'verified':True}
    if action=='witness-grant-check':
        def check():
            result=payload(module('quorum-enforce').CHECK,dict(node=node,config=config(args)))
            if not result['required'] or not result['activeLease'] or result['owner']!=args['owner'] or result['epoch']!=args['epoch'] or node==args['owner'] and not result.get('freshLocalGrant'):raise RuntimeError('No matching admitted grant')
            if node==args['owner']:
                from quorum_authority import ControlLock,Etcd,validate_enforced_config
                lock=ControlLock(Etcd(config(args),validate_enforced_config),plan['clusterId'])
                peer='backup' if node=='primary' else 'primary'
                for authority in ('cloudflare','fallback'):
                    denied=lock.claim(authority,peer,args['epoch']+1,'installer_exclusion_session_12345',5)
                    if denied.get('admitted') or denied.get('reason') not in ('lease-held','fallback-blocked'):raise RuntimeError('Competing authority was not excluded')
            return {'verified':True}
        return wait(check)
    if action=='witness-fallback':
        if node=='witness':raise RuntimeError('Witness does not promote Immich')
        payload(module('quorum-fallback').ENABLE,dict(node=node,clusterId=plan['clusterId']));return {'verified':True}
    if action=='witness-fallback-check':
        def check():
            value=payload(module('quorum-fallback').CHECK,{'config':config(args)})
            if value['owner']!=args['owner'] or value['epoch']!=args['epoch'] or value['authority']!='cloudflare' or not value.get('policy'):raise RuntimeError('Fallback policy not observed')
            return {'verified':True}
        return wait(check)
    if action=='witness-gateway':return install_gateway(args)
    if action=='witness-router':return install_router(args)
    if action=='witness-gateway-check':return check_gateways(args)
    raise RuntimeError('Unsupported witness action')


def install_gateway(args):
    from local_gateway import render
    node=args['node'];cfg=json.loads(Path('/etc/immich-ha/config.json').read_text())
    if cfg['node']!=node:raise RuntimeError('Wrong Immich node')
    conf=Path('/etc/nginx/conf.d/immich-ha-local.conf');marker=Path('/etc/immich-ha/local-gateway.json')
    if conf.exists() or marker.exists():raise RuntimeError('Existing local gateway retained')
    trusted=args['plan']['members']['witness']['ip']
    text=render((APP/'node/immich-ha-local.nginx.conf').read_text(),8090,cfg['origin_token'],[trusted],args['networkMode'])
    atomic(conf,text)
    if subprocess.run(['nginx','-t'],capture_output=True).returncode:conf.unlink();raise RuntimeError('Gateway config rejected; prior config retained')
    atomic(marker,json.dumps(dict(version=1,node=node,clusterId=args['plan']['clusterId'],port=8090,networkMode=args['networkMode'],trustedGatewayIps=[trusted],sha256=hashlib.sha256(conf.read_bytes()).hexdigest()))+'\n')
    run(['systemctl','reload','nginx.service']);return {'verified':True}


def install_router(args):
    import witness_gateway_setup as w
    if args['node']!='witness':raise RuntimeError('Router only on witness')
    for path in ('/etc/immich-ha/witness-router.json','/etc/immich-ha/witness-gateway.json','/etc/nginx/conf.d/immich-ha-witness.conf'):
        if Path(path).exists():raise RuntimeError('Existing witness gateway retained')
    plan=args['plan'];cfg={'version':1,'networkMode':args['networkMode'],'clusterId':plan['clusterId'],'endpoints':config(args)['endpoints'],
        'nodes':{n:{'ip':plan['members'][n]['ip'],'port':8090} for n in ('primary','backup')}}
    staging=directory('router')
    contents={'witness_router.py':(APP/'node/witness_router.py').read_text(),'router.json':json.dumps(cfg),
        'router.service':w.UNIT.replace(' tailscaled.service','') if args['networkMode']=='lan' else w.UNIT,
        'witness.nginx.conf':w.render((APP/'node/immich-ha-witness.nginx.conf').read_text(),cfg,8090)}
    for name,text in contents.items():atomic(staging/name,text)
    payload(w.INSTALL,dict(directory=str(staging),clusterId=plan['clusterId'],port=8090));return {'verified':True}


def get(url,path):
    request=urllib.request.Request(url+path+('?probe='+uuid.uuid4().hex if '?' not in path else ''),headers={'Cache-Control':'no-store'})
    try:
        with urllib.request.urlopen(request,timeout=10) as response:return response.status,response.read(2048),response.headers
    except urllib.error.HTTPError as error:return error.code,error.read(2048),error.headers


def check_gateways(args):
    def check():
        for n in ('primary','backup'):
            url='http://'+args['plan']['members'][n]['ip']+':8090'
            if get(url,'/api/server/ping')[0]!=(200 if n==args['owner'] else 503):raise RuntimeError('Node gateway role mismatch')
            if get(url,'/__ha/health')[0]!=404:raise RuntimeError('Control path exposed')
        code,body,headers=get('http://127.0.0.1:8090','/api/server/ping')
        if code!=200 or json.loads(body).get('res')!='pong' or headers.get('X-Immich-HA-Node')!=args['owner'] or headers.get('X-Immich-HA-Epoch')!=str(args['epoch']):raise RuntimeError('Common gateway does not match admitted owner')
        if get('http://127.0.0.1:8090','/api/users/me')[0] not in (401,403) or get('http://127.0.0.1:8090','/_immich_witness_route')[0]!=404:raise RuntimeError('Gateway authentication/isolation check failed')
        return {'verified':True}
    return wait(check)


def enable_funnel(local):
    import funnel_setup as f
    state=json.loads((APP/'.install/witness.json').read_text());node=local['node'];cluster=state['plan']['clusterId']
    marker=Path('/etc/immich-ha/witness-gateway.json' if node=='witness' else '/etc/immich-ha/local-gateway.json')
    value=json.loads(marker.read_text())
    conf=Path('/etc/nginx/conf.d/immich-ha-witness.conf' if node=='witness' else '/etc/nginx/conf.d/immich-ha-local.conf')
    if value['clusterId']!=cluster or value['port']!=8090 or value['sha256']!=hashlib.sha256(conf.read_bytes()).hexdigest():raise RuntimeError('Gateway identity/hash differs')
    tail=json.loads(run(['tailscale','status','--json']))
    if tail.get('BackendState')!='Running':raise RuntimeError('Log in to Tailscale before enabling Funnel')
    dns=tail['Self']['DNSName'].rstrip('.')
    before=json.loads(run(['tailscale','serve','status','--json'])) or {}
    f.validate_existing(before,dns,443,8090)
    result=payload(f.APPLY,dict(directory=str(APP),before=before,dnsName=dns,port=443,gatewayPort=8090,node=node,clusterId=cluster,stageId=uuid.uuid4().hex))
    if not result['configured']:
        for url in result['approvalUrls']:print('Approve Funnel in your browser: '+url,flush=True)
        raise RuntimeError('Funnel approval/configuration not complete; existing routes retained')
    def check():
        ip=f.public_addresses(dns)[0]
        ping=f.public_get(dns,ip,'/api/server/ping?probe='+uuid.uuid4().hex)
        local_code,body,headers=get('http://127.0.0.1:8090','/api/server/ping')
        if local_code==200:
            if ping['status']!=200 or json.loads(ping['body']).get('res')!='pong':raise RuntimeError('Public Funnel ping failed')
            if node=='witness' and (ping['node']!=headers.get('X-Immich-HA-Node') or ping['epoch']!=headers.get('X-Immich-HA-Epoch')):raise RuntimeError('Funnel owner differs')
            if f.public_get(dns,ip,'/api/users/me')['status'] not in (401,403):raise RuntimeError('Funnel authentication failed')
        elif node=='witness' or local_code!=503 or ping['status']!=503:raise RuntimeError('Standby gateway must stay closed')
        for path in ('/__ha/health','/_immich_witness_route','/_immich_ha_local_authorize'):
            if f.public_get(dns,ip,path)['status']!=404:raise RuntimeError('Private endpoint exposed')
        return True
    wait(check,120)
    tail_ip=next((ip for ip in tail['Self'].get('TailscaleIPs',[]) if ':' not in ip),None)
    return {'url':result['url'],'enabled':True,'tailscaleIp':tail_ip}


def reconcile_funnel(desired):
    # Only witness polls a common control setting. No role/lease operation here.
    path=Path('/etc/immich-ha/funnel-gateway.json')
    if not path.exists():return None
    import funnel_setup as f
    value=json.loads(path.read_text())
    if value['node']!='witness':raise RuntimeError('Wrong common Funnel identity')
    dns=value['dnsName'];port=value['port'];gateway=value['gatewayPort'];enabled=None
    try:
        before=json.loads(run(['tailscale','serve','status','--json'])) or {}
        f.validate_existing(before,dns,port,gateway)
        enabled=(before.get('AllowFunnel') or {}).get(dns+':'+str(port)) is True
        if enabled!=desired['enabled']:
            # Target only this listener, never reset unrelated Serve/Funnel routes.
            if desired['enabled']:run(['tailscale','funnel','--bg','--yes','--https='+str(port),f'http://127.0.0.1:{gateway}'],40)
            else:run(['tailscale','funnel','--https='+str(port),'off'],40)
            after=json.loads(run(['tailscale','serve','status','--json'])) or {}
            def unrelated(settings):
                settings=copy.deepcopy(settings)
                for key,entry in (('TCP',str(port)),('Web',dns+':'+str(port)),('AllowFunnel',dns+':'+str(port))):
                    if isinstance(settings.get(key),dict):
                        settings[key].pop(entry,None)
                        if not settings[key]:settings.pop(key)
                return settings
            if unrelated(before)!=unrelated(after):raise RuntimeError('Unrelated Serve settings changed')
            f.validate_existing(after,dns,port,gateway)
            enabled=(after.get('AllowFunnel') or {}).get(dns+':'+str(port)) is True
        if enabled!=desired['enabled']:raise RuntimeError('Listener setting not confirmed')
        return {'enabled':enabled,'revision':desired['revision']}
    except (OSError,RuntimeError,ValueError):return {'enabled':enabled,'revision':desired['revision'],'error':'Common listener not confirmed; inspect witness'}
