"""Shared inventory, private topology and SSH transport for the quorum layer.

Importing this module does not issue grants, install services or read config.
"""
import contextlib
import ipaddress
import json
from pathlib import Path
import uuid

import deployment as d
from local_env import load_env
from project_config import load_project
from ssh_nodes import connect, settings

NODES = ('primary', 'backup', 'witness')
PLAN = d.ROOT / 'generated/quorum-topology.json'

# Executed as root by the installers, never by importing this module.
APP_MARKER_ACCESS = r'''
def ensure_app_marker_access():
    import os,pwd,stat,subprocess
    from pathlib import Path
    base=Path('/etc/immich-ha');marker=base/'quorum-required'
    user=pwd.getpwnam('immich');groups=set(os.getgrouplist('immich',user.pw_gid))
    for path in (base,marker):
        if path.is_symlink() or path.stat().st_uid!=0:raise RuntimeError('Unsafe quorum marker ownership')
    def allowed(path,bit):
        value=path.stat();shift=6 if value.st_uid==user.pw_uid else 3 if value.st_gid in groups else 0
        return bool((stat.S_IMODE(value.st_mode)>>shift)&bit)
    def audit(directory):
        for path in directory.iterdir():
            if path.is_symlink():raise RuntimeError('Audit quorum configuration symlink before granting traversal')
            if path.is_dir():
                if allowed(path,1):audit(path)
            elif path!=marker and allowed(path,4):raise RuntimeError('Private quorum file would become readable')
    audit(base)
    os.chown(base,0,user.pw_gid);os.chmod(base,0o710)
    # The app can read the public cluster marker, but cannot list config files.
    subprocess.run(['runuser','-u','immich','--','test','-r',str(marker)],check=True,capture_output=True)
'''

INSPECT = r'''
import json,os,shutil,socket,subprocess,time
from pathlib import Path
def command(args):
    p=subprocess.run(args,capture_output=True,text=True,timeout=15)
    return p.stdout.strip() if p.returncode==0 else None
config=Path('/etc/immich-ha/config.json')
identity=json.loads(config.read_text()).get('node') if config.is_file() else None
tail=command(['tailscale','ip','-4'])
release=dict(line.split('=',1) for line in Path('/etc/os-release').read_text().splitlines() if '=' in line)
mem=dict(line.split(':',1) for line in Path('/proc/meminfo').read_text().splitlines() if ':' in line)
listeners=command(['ss','-H','-lnt']) or ''
busy=[]
for port in (2379,2380):
    if any(line.split()[3].endswith(':'+str(port)) for line in listeners.splitlines() if len(line.split())>=4):busy.append(port)
peer=None
if identity:
    import sys
    sys.path.insert(0,'/usr/local/lib/immich-ha')
    from pg_stream import configuration,status
    cfg=configuration();value=status()
    peer=cfg.get('peer_ip') if cfg else None
    pg={'inRecovery':value.get('inRecovery'),'systemId':value.get('systemId'),'receiverConnected':value.get('receiverConnected')}
else:pg=None
print(json.dumps({'hostname':socket.gethostname(),'uid':os.getuid(),'node':identity,
 'os':release.get('ID','').strip('"'),'osVersion':release.get('VERSION_ID','').strip('"'),
 'tailscaleIp':tail,'tun':Path('/dev/net/tun').is_char_device(),
 'memoryBytes':int(mem['MemTotal'].split()[0])*1024,'rootFreeBytes':shutil.disk_usage('/').free,
 'quorumPortsBusy':busy,'peerIp':peer,'postgres':pg,'checkedAt':int(time.time()*1000),
 'etcdInstalled':shutil.which('etcd') is not None,
 'agent':command(['systemctl','is-active','immich-ha-agent.service']) if identity else None}))
'''

PING = r'''
import json,subprocess
result={}
for node,ip in INPUT['peers'].items():
    p=subprocess.run(['tailscale','ping','--c=1','--until-direct=false','--timeout=5s',ip],capture_output=True,text=True,timeout=10)
    result[node]={'reachable':p.returncode==0,'summary':(p.stdout or p.stderr).strip()[-400:]}
print(json.dumps(result))
'''


def key_settings(node, env, project):
    """A common explicit key may be used, without borrowing another node's password."""
    values=dict(env)
    prefix='SSH_'+node.upper()+'_'
    if not values.get(prefix+'KEY_PATH'):
        values[prefix+'KEY_PATH']=values.get('SSH_KEY_PATH','')
        if not values.get(prefix+'KEY_PASSPHRASE'):
            values[prefix+'KEY_PASSPHRASE']=values.get('SSH_KEY_PASSPHRASE','')
    return settings(node,values,project)


@contextlib.contextmanager
def connections(witness_host):
    env=load_env();project=load_project()
    first,gateway=d.connection('primary');opened=[first]
    if gateway:opened.insert(0,gateway)
    try:
        info=d.remote(first,INSPECT)
        peer=info.get('peerIp')
        if not peer:raise RuntimeError('Primary streaming peer is not configured')
        options=key_settings('backup',env,project);options['hostname']=peer
        options['sock']=first.get_transport().open_channel('direct-tcpip',(peer,options['port']),('127.0.0.1',0),timeout=15)
        second=connect(options);opened.append(second)
        values={**env,'SSH_COORDINATOR_HOST':witness_host}
        options=key_settings('coordinator',values,{'nodes':{'coordinator':{'ip':witness_host}}})
        try:third=connect(options)
        except Exception:
            options['sock']=first.get_transport().open_channel('direct-tcpip',(witness_host,options['port']),('127.0.0.1',0),timeout=15)
            third=connect(options)
        opened.append(third)
        yield {'primary':first,'backup':second,'witness':third},info
    finally:
        for client in reversed(opened):client.close()


def validate_inventory(inventory, links):
    if set(inventory)!=set(NODES):raise ValueError('Exactly three node identities are required')
    ips=set()
    for node in NODES:
        value=inventory[node]
        if value['uid']!=0 or value['os']!='debian' or value['osVersion']!='13':
            raise ValueError('Expected root SSH into Debian 13 on '+node)
        if value['node']!=(None if node=='witness' else node):raise ValueError('Wrong LXC identity: '+node)
        if node=='witness' and value['hostname']!='immich-ha-witness':raise ValueError('Wrong witness hostname')
        ip=ipaddress.IPv4Address(value['tailscaleIp'])
        if ip not in ipaddress.ip_network('100.64.0.0/10') or str(ip) in ips:raise ValueError('Distinct Tailscale IPv4 addresses required')
        ips.add(str(ip))
        if not value['tun']:raise ValueError('Missing TUN on '+node)
        if value['memoryBytes']<768*1024**2 or value['rootFreeBytes']<1024**3:raise ValueError('Insufficient witness/quorum resources on '+node)
        if value['quorumPortsBusy']:raise ValueError('Existing service occupies quorum ports on '+node)
        if node!='witness' and value['agent']!='active':raise ValueError('HA agent is not active on '+node)
        if set(links.get(node,{}))!=set(NODES)-{node} or any(not result['reachable'] for result in links[node].values()):
            raise ValueError('Tailscale cannot reach all peers from '+node)
    source=[n for n in ('primary','backup') if inventory[n]['postgres']['inRecovery'] is False]
    replica=[n for n in ('primary','backup') if inventory[n]['postgres']['inRecovery'] is True]
    if len(source)!=1 or len(replica)!=1:raise ValueError('Expected exactly one writable PostgreSQL primary')
    if inventory[source[0]]['postgres']['systemId']!=inventory[replica[0]]['postgres']['systemId'] or not inventory[replica[0]]['postgres']['receiverConnected']:
        raise ValueError('PostgreSQL replica is not following the same data lineage')
    return source[0]


def prepare_plan(inventory, links, path=PLAN):
    owner=validate_inventory(inventory,links)
    topology={n:{'tailscaleIp':inventory[n]['tailscaleIp'],'clientPort':2379,'peerPort':2380} for n in NODES}
    if path.exists():
        plan=json.loads(path.read_text(encoding='utf-8'))
        if plan.get('members')!=topology or plan.get('version')!=1:raise ValueError('Saved topology differs; retained existing cluster identity')
        return plan,owner
    plan={'version':1,'clusterId':str(uuid.uuid4()),'stage':'planned','engine':'etcd',
          'members':topology,'requiredVotes':2,'cloudflareRemainsPrimary':True,
          'grantIntegrationEnabled':False,'funnelEnabled':False}
    path.parent.mkdir(parents=True,exist_ok=True)
    # Never overwrite/restage an existing cluster or its identity.
    with path.open('x',encoding='utf-8') as f:json.dump(plan,f,indent=2);f.write('\n')
    return plan,owner
