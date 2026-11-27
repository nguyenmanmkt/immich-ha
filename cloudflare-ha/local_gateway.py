"""Private-network gateway rendering and guarded deployment payloads."""
import re


def validate_port(port):
    if type(port) is not int or not 1024 <= port <= 65535 or port in (2283,2379,2380,5432,5433,8080,9081):
        raise ValueError('Choose an unused separate gateway port')
    return port


def render(template,port,token,trusted_gateway_ips=(),network_mode='tailscale'):
    validate_port(port)
    if not isinstance(token,str) or not re.fullmatch(r'[A-Za-z0-9_-]{32,128}',token):raise ValueError('Invalid origin credential format')
    if template.count('__PORT__')!=2 or template.count('__ORIGIN_TOKEN__')!=1:raise ValueError('Unexpected gateway template')
    import ipaddress
    trusted=[]
    for ip in trusted_gateway_ips:
        address=ipaddress.IPv4Address(ip)
        ranges=('100.64.0.0/10',) if network_mode=='tailscale' else ('10.0.0.0/8','172.16.0.0/12','192.168.0.0/16') if network_mode=='lan' else ()
        if str(address)!=ip or not any(address in ipaddress.ip_network(n) for n in ranges):raise ValueError('Trusted gateway must use the selected private network')
        trusted.append('    "'+ip+':https" https;')
    anchor='    "::1:https" https;'
    if trusted:
        if template.count(anchor)!=1:raise ValueError('Unexpected forwarded scheme map')
        template=template.replace(anchor,anchor+'\n'+'\n'.join(sorted(set(trusted))))
    return template.replace('__PORT__',str(port)).replace('__ORIGIN_TOKEN__',token)


PREFLIGHT=r'''
import hashlib,json,subprocess,sys
from pathlib import Path
sys.path.insert(0,'/usr/local/lib/immich-ha')
from pg_stream import status,DATA
c=json.loads(Path('/etc/immich-ha/config.json').read_text())
q=json.loads(Path('/etc/immich-ha/quorum-authority.json').read_text())
if c['node']!=INPUT['node'] or q['clusterId']!=INPUT['clusterId'] or not c.get('enforce') or not q.get('fallbackEnabled'):raise RuntimeError('Expected enforced fallback node')
path=Path('/etc/nginx/conf.d/immich-ha-local.conf');marker=Path('/etc/immich-ha/local-gateway.json')
if path.exists()!=marker.exists():raise RuntimeError('Unmanaged/partial gateway retained; inspect before continuing')
if marker.exists():
 value=json.loads(marker.read_text())
 if value['node']!=c['node'] or value['clusterId']!=q['clusterId'] or value['port']!=INPUT['port'] or value['sha256']!=hashlib.sha256(path.read_bytes()).hexdigest():raise RuntimeError('Existing gateway differs; retained')
else:
 listeners=subprocess.check_output(['ss','-H','-ltn','sport = :'+str(INPUT['port'])],text=True)
 if listeners.strip():raise RuntimeError('Gateway port occupied')
result=subprocess.run(['nginx','-t'],capture_output=True,timeout=15)
if result.returncode:raise RuntimeError('Existing Nginx configuration invalid')
ips=json.loads(subprocess.check_output(['ip','-j','-4','addr','show','dev','eth0'],text=True))
lan=[a['local'] for i in ips for a in i['addr_info'] if a.get('scope')=='global']
tail=subprocess.check_output(['tailscale','ip','-4'],text=True).strip()
pg=status()
print(json.dumps({'lan':lan,'tailscale':tail,'inRecovery':pg['inRecovery'],'pgPid':int((DATA/'postmaster.pid').read_text().splitlines()[0]),
 'appPid':int(subprocess.check_output(['systemctl','show','immich-ha-app.service','--property=MainPID','--value'],text=True))}))
'''

INSTALL=r'''
import hashlib,json,os,py_compile,shutil,subprocess,sys
from pathlib import Path
stage=Path(INPUT['directory']);sys.path.insert(0,str(stage))
from local_gateway import render
cfg=json.loads(Path('/etc/immich-ha/config.json').read_text())
if cfg['node']!=INPUT['node']:raise RuntimeError('Wrong node identity')
save=Path('/etc/immich-ha/local-gateway-backups')/INPUT['stageId'];save.mkdir(parents=True,mode=0o700)
source=(stage/'agent.py').read_bytes().replace(b'\r\n',b'\n')
for path in (Path('/usr/local/sbin/immich-ha-agent'),Path('/usr/local/lib/immich-ha/ha_agent.py')):
 shutil.copy2(path,save/path.name)
 temporary=path.with_name(path.name+'.local-gateway-stage');temporary.write_bytes(source)
 os.chmod(temporary,0o700 if path.name=='immich-ha-agent' else 0o600)
 py_compile.compile(str(temporary),doraise=True);os.replace(temporary,path)
subprocess.run(['systemctl','restart','immich-ha-agent.service'],check=True,capture_output=True,timeout=30)
conf=Path('/etc/nginx/conf.d/immich-ha-local.conf')
previous=conf.read_bytes() if conf.exists() else None
if previous is not None:shutil.copy2(conf,save/conf.name)
marker=Path('/etc/immich-ha/local-gateway.json')
trusted=json.loads(marker.read_text()).get('trustedGatewayIps',[]) if marker.exists() else []
text=render((stage/'immich-ha-local.nginx.conf').read_text(),INPUT['port'],cfg['origin_token'],trusted)
temporary=conf.with_suffix('.stage');temporary.write_text(text);os.chmod(temporary,0o600);os.replace(temporary,conf)
check=subprocess.run(['nginx','-t'],capture_output=True,timeout=15)
if check.returncode:
 if previous is None:conf.unlink()
 else:conf.write_bytes(previous);os.chmod(conf,0o600)
 raise RuntimeError('New Nginx config rejected; previous config restored; app/PG unchanged')
value=dict(version=1,node=cfg['node'],clusterId=INPUT['clusterId'],port=INPUT['port'],trustedGatewayIps=trusted,sha256=hashlib.sha256(conf.read_bytes()).hexdigest())
temporary=marker.with_suffix('.stage');temporary.write_text(json.dumps(value,indent=2)+'\n');os.chmod(temporary,0o600);os.replace(temporary,marker)
subprocess.run(['systemctl','reload','nginx.service'],check=True,capture_output=True,timeout=30)
print(json.dumps({'gatewayInstalled':True,'port':INPUT['port']}))
'''

CHECK=r'''
import json,urllib.request,urllib.error
def get(path,headers=None):
 request=urllib.request.Request(INPUT['url']+path,headers=headers or {})
 try:
  with urllib.request.urlopen(request,timeout=5) as response:return {'status':response.status,'body':response.read(2048).decode(errors='replace')}
 except urllib.error.HTTPError as error:return {'status':error.code,'body':error.read(2048).decode(errors='replace')}
print(json.dumps({'ping':get('/api/server/ping'),'internal':get('/_immich_ha_local_authorize'),
 'control':get('/__ha/health'),'spoof':get('/api/server/ping',{'X-Immich-HA-Epoch':'999999','X-Immich-HA-Token':'forged'})}))
'''
