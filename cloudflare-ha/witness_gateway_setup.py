"""Deployment of the witness reverse proxy, separate from grant issuance."""
import ipaddress
from node.witness_router import validate


def configuration(plan,ports):
    value={'version':1,'clusterId':plan['clusterId'],
        'endpoints':[f'https://{plan["members"][n]["tailscaleIp"]}:2379' for n in ('primary','backup','witness')],
        'nodes':{n:{'ip':plan['members'][n]['tailscaleIp'],'port':ports[n]} for n in ('primary','backup')}}
    return validate(value)


def render(template,config,port):
    validate(config)
    if type(port) is not int or not 1024<=port<=65535 or port in (2283,2379,2380,5432,5433,8080,9081):raise ValueError('Invalid witness listener port')
    if template.count('__PORT__')!=2:raise ValueError('Unexpected template')
    for node in ('primary','backup'):
        placeholder='__'+node.upper()+'_TARGET__'
        if template.count(placeholder)!=1:raise ValueError('Unexpected target map')
        v=config['nodes'][node];template=template.replace(placeholder,v['ip']+':'+str(v['port']))
    return template.replace('__PORT__',str(port))


PREFLIGHT=r'''
import hashlib,json,os,shutil,subprocess,socket
from pathlib import Path
identity=json.loads(Path('/etc/immich-ha-quorum/installation.json').read_text())
if identity['node']!='witness' or identity['clusterId']!=INPUT['clusterId'] or not identity['grantIntegrationEnabled']:raise RuntimeError('Wrong enforced witness')
marker=Path('/etc/immich-ha/witness-gateway.json');conf=Path('/etc/nginx/conf.d/immich-ha-witness.conf')
if marker.exists()!=conf.exists():raise RuntimeError('Unmanaged or partial witness gateway retained')
if marker.exists():
 value=json.loads(marker.read_text())
 if value['clusterId']!=identity['clusterId'] or value['port']!=INPUT['port'] or value['sha256']!=hashlib.sha256(conf.read_bytes()).hexdigest():raise RuntimeError('Existing witness gateway differs; retained')
elif subprocess.check_output(['ss','-H','-ltn','sport = :'+str(INPUT['port'])],text=True).strip():raise RuntimeError('Witness listener port occupied')
if shutil.which('nginx') and subprocess.run(['nginx','-t'],capture_output=True,timeout=15).returncode:raise RuntimeError('Existing Nginx config invalid')
tail=json.loads(subprocess.check_output(['tailscale','status','--json'],text=True,timeout=15))
addresses=json.loads(subprocess.check_output(['ip','-j','-4','addr','show','dev','eth0'],text=True))
print(json.dumps({'hostname':socket.gethostname(),'tailscale':tail['Self']['TailscaleIPs'][0],
 'dnsName':tail['Self']['DNSName'].rstrip('.'),'nginxInstalled':bool(shutil.which('nginx')),
 'lan':[a['local'] for i in addresses for a in i['addr_info'] if a.get('scope')=='global'],
 'freeBytes':shutil.disk_usage('/').free,'quorumService':subprocess.check_output(['systemctl','is-active','immich-ha-quorum.service'],text=True).strip()}))
'''

TRUST_SCHEME=r'''
import hashlib,json,os,subprocess,sys
from pathlib import Path
sys.path.insert(0,INPUT['directory'])
from local_gateway import render
marker=Path('/etc/immich-ha/local-gateway.json');value=json.loads(marker.read_text())
conf=Path('/etc/nginx/conf.d/immich-ha-local.conf')
if value['node']!=INPUT['node'] or value['clusterId']!=INPUT['clusterId'] or value['sha256']!=hashlib.sha256(conf.read_bytes()).hexdigest():raise RuntimeError('Gateway identity/hash changed; retained')
cfg=json.loads(Path('/etc/immich-ha/config.json').read_text())
previous=conf.read_bytes();before_marker=marker.read_bytes()
trusted=sorted(set(value.get('trustedGatewayIps',[])+[INPUT['witnessIp']]))
text=render((Path(INPUT['directory'])/'immich-ha-local.nginx.conf').read_text(),value['port'],cfg['origin_token'],trusted)
temporary=conf.with_suffix('.stage');temporary.write_text(text);os.chmod(temporary,0o600);os.replace(temporary,conf)
if subprocess.run(['nginx','-t'],capture_output=True,timeout=15).returncode:
 conf.write_bytes(previous);os.chmod(conf,0o600)
 raise RuntimeError('New scheme map invalid; old config restored')
save=Path(INPUT['directory']);(save/'local-conf.before').write_bytes(previous);os.chmod(save/'local-conf.before',0o600)
(save/'local-marker.before').write_bytes(before_marker);os.chmod(save/'local-marker.before',0o600)
value.update(trustedGatewayIps=trusted,sha256=hashlib.sha256(conf.read_bytes()).hexdigest())
temporary=marker.with_suffix('.stage');temporary.write_text(json.dumps(value,indent=2)+'\n');os.chmod(temporary,0o600);os.replace(temporary,marker)
subprocess.run(['systemctl','reload','nginx.service'],check=True,capture_output=True,timeout=30)
print(json.dumps({'trustedWitnessScheme':True}))
'''

UNIT='''[Unit]
Description=Immich read-only quorum routing oracle
After=network-online.target tailscaled.service immich-ha-quorum.service
Wants=network-online.target
[Service]
Type=simple
User=immich-router
Group=www-data
RuntimeDirectory=immich-ha-router
RuntimeDirectoryMode=0750
LoadCredential=config.json:/etc/immich-ha/witness-router.json
LoadCredential=ca.crt:/etc/immich-ha-quorum/client-ca.crt
LoadCredential=client.crt:/etc/immich-ha-quorum/probe.crt
LoadCredential=client.key:/etc/immich-ha-quorum/probe.key
ExecStart=/usr/bin/python3 /usr/local/lib/immich-ha-router/witness_router.py
Restart=on-failure
RestartSec=2
UMask=0007
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/run/immich-ha-router
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
CapabilityBoundingSet=
MemoryMax=96M
[Install]
WantedBy=multi-user.target
'''

INSTALL=r'''
import hashlib,json,os,pwd,py_compile,shutil,subprocess
from pathlib import Path
fresh=not bool(shutil.which('nginx'))
if fresh:
 env={**os.environ,'DEBIAN_FRONTEND':'noninteractive'}
 for args in (['apt-get','update'],['apt-get','install','-y','nginx']):
  result=subprocess.run(args,env=env,capture_output=True,text=True,timeout=240)
  if result.returncode:raise RuntimeError('Nginx package installation failed; inspect apt logs')
 default=Path('/etc/nginx/sites-enabled/default')
 if default.is_symlink() and str(default.resolve())=='/etc/nginx/sites-available/default':default.unlink()
try:user=pwd.getpwnam('immich-router')
except KeyError:
 subprocess.run(['useradd','--system','--no-create-home','--home-dir','/nonexistent','--shell','/usr/sbin/nologin','immich-router'],check=True,capture_output=True)
 user=pwd.getpwnam('immich-router')
if user.pw_uid==0:raise RuntimeError('Router must use a dedicated unprivileged identity')
def atomic(path,content,mode):
 path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
 temporary=path.with_suffix(path.suffix+'.stage');temporary.write_bytes(content);os.chmod(temporary,mode);os.replace(temporary,path)
directory=Path(INPUT['directory']);save=directory/'before';save.mkdir(mode=0o700)
Path('/usr/local/lib/immich-ha-router').mkdir(mode=0o755,exist_ok=True)
targets={'/usr/local/lib/immich-ha-router/witness_router.py':('witness_router.py',0o644),
 '/etc/systemd/system/immich-ha-router.service':('router.service',0o644),
 '/etc/immich-ha/witness-router.json':('router.json',0o600),
 '/etc/nginx/conf.d/immich-ha-witness.conf':('witness.nginx.conf',0o644)}
for target,(source,mode) in targets.items():
 path=Path(target)
 if path.exists():shutil.copy2(path,save/path.name)
 content=(directory/source).read_bytes().replace(b'\r\n',b'\n')
 if source.endswith('.py'):py_compile.compile(str(directory/source),doraise=True)
 atomic(path,content,mode)
if subprocess.run(['nginx','-t'],capture_output=True,timeout=15).returncode:raise RuntimeError('Witness Nginx configuration rejected; inspect staged files')
conf=Path('/etc/nginx/conf.d/immich-ha-witness.conf')
atomic('/etc/immich-ha/witness-gateway.json',json.dumps(dict(version=1,clusterId=INPUT['clusterId'],port=INPUT['port'],sha256=hashlib.sha256(conf.read_bytes()).hexdigest())).encode(),0o600)
subprocess.run(['systemctl','daemon-reload'],check=True,capture_output=True,timeout=15)
subprocess.run(['systemctl','enable','--now','immich-ha-router.service'],check=True,capture_output=True,timeout=30)
subprocess.run(['systemctl','restart','immich-ha-router.service'],check=True,capture_output=True,timeout=30)
subprocess.run(['systemctl','enable','--now','nginx.service'],check=True,capture_output=True,timeout=30)
subprocess.run(['systemctl','reload','nginx.service'],check=True,capture_output=True,timeout=30)
print(json.dumps({'witnessGatewayInstalled':True,'quorumUnchanged':True}))
'''
