"""Conservative, per-node Funnel setup for the existing grant-gated gateway."""
import copy
import http.client
import ipaddress
import json
import socket
import ssl
import urllib.parse
import urllib.request


def validate_existing(config,dns_name,port=443,gateway_port=8090):
    if port not in (443,8443,10000):raise ValueError('Unsupported Funnel port')
    if not isinstance(dns_name,str) or not dns_name.endswith('.ts.net') or any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789.-' for c in dns_name):raise ValueError('Expected a Tailscale DNS name')
    config=config or {}
    if not isinstance(config,dict):raise ValueError('Invalid Serve configuration')
    address=f'{dns_name}:{port}'
    tcp=config.get('TCP') or {}
    web=config.get('Web') or {}
    allowed=config.get('AllowFunnel') or {}
    route=(web.get(address) or {}).get('Handlers') or {}
    target=f'http://127.0.0.1:{gateway_port}'
    if str(port) in tcp and tcp[str(port)]!={'HTTPS':True}:raise ValueError('Funnel port already has another listener')
    if str(port) in tcp and not route:raise ValueError('Existing listener has no matching managed root route')
    if any(key!=address and key.endswith(':'+str(port)) for key in web):raise ValueError('Another hostname shares this Serve port; retained')
    if route and route!={'/':{'Proxy':target}}:raise ValueError('Existing root routes retained; choose a separate Funnel port')
    if address in allowed and allowed[address] not in (False,True):raise ValueError('Invalid Funnel setting')
    # Background/foreground sessions can own listeners outside the base map.
    if config.get('Foreground'):raise ValueError('Existing foreground Serve session retained')
    return target


def validate_after(before,after,dns_name,port=443,gateway_port=8090):
    validate_existing(after,dns_name,port,gateway_port)
    address=f'{dns_name}:{port}'
    if (after.get('TCP') or {}).get(str(port))!={'HTTPS':True} or (after.get('Web') or {}).get(address,{}).get('Handlers')!={'/':{'Proxy':f'http://127.0.0.1:{gateway_port}'}} or (after.get('AllowFunnel') or {}).get(address) is not True:
        raise ValueError('Expected Funnel route not confirmed')
    old=copy.deepcopy(before or {});new=copy.deepcopy(after)
    for value in (old,new):
        for key,entry in (('TCP',str(port)),('Web',address),('AllowFunnel',address)):
            if isinstance(value.get(key),dict):
                value[key].pop(entry,None)
                if not value[key]:value.pop(key)
    if old!=new:raise ValueError('Unrelated Serve configuration changed; inspect snapshots')


PREFLIGHT=r'''
import hashlib,json,subprocess,urllib.request,urllib.error
from pathlib import Path
c=json.loads(Path('/etc/immich-ha/config.json').read_text())
marker=json.loads(Path('/etc/immich-ha/local-gateway.json').read_text())
if c['node']!=INPUT['node'] or marker['node']!=c['node'] or marker['clusterId']!=INPUT['clusterId']:raise RuntimeError('Wrong gateway identity')
if marker['sha256']!=hashlib.sha256(Path('/etc/nginx/conf.d/immich-ha-local.conf').read_bytes()).hexdigest():raise RuntimeError('Gateway configuration changed; retained')
tail=json.loads(subprocess.check_output(['tailscale','status','--json'],text=True,timeout=15))
if tail.get('BackendState')!='Running':raise RuntimeError('Tailscale is not running')
serve=json.loads(subprocess.check_output(['tailscale','serve','status','--json'],text=True,timeout=15)) or {}
dns=tail['Self']['DNSName'].rstrip('.')
help_text=subprocess.check_output(['tailscale','funnel','--help'],text=True,stderr=subprocess.STDOUT,timeout=10)
print(json.dumps({'dnsName':dns,'version':subprocess.check_output(['tailscale','version'],text=True).splitlines()[0],
 'gatewayPort':marker['port'],'serve':serve,'supportsBackground':'--bg' in help_text,'supportsYes':'--yes' in help_text,
 'capabilities':list((tail['Self'].get('CapMap') or {}).keys())}))
'''

APPLY=r'''
import json,os,re,subprocess,sys
from pathlib import Path
sys.path.insert(0,INPUT['directory'])
from funnel_setup import validate_existing,validate_after
before=json.loads(subprocess.check_output(['tailscale','serve','status','--json'],text=True,timeout=15)) or {}
if before!=INPUT['before']:raise RuntimeError('Serve configuration changed since preflight; retained')
target=validate_existing(before,INPUT['dnsName'],INPUT['port'],INPUT['gatewayPort'])
save=Path('/etc/immich-ha/funnel-backups')/INPUT['stageId'];save.mkdir(parents=True,mode=0o700,exist_ok=False)
p=save/'serve-before.json';p.write_text(json.dumps(before,indent=2)+'\n');os.chmod(p,0o600)
try:
 result=subprocess.run(['tailscale','funnel','--bg','--yes','--https='+str(INPUT['port']),target],stdin=subprocess.DEVNULL,capture_output=True,text=True,timeout=25)
 output=result.stdout+'\n'+result.stderr
 code=result.returncode
except subprocess.TimeoutExpired as error:
 output=(error.stdout or b'').decode(errors='replace') if isinstance(error.stdout,bytes) else (error.stdout or '')
 output+='\n'+((error.stderr or b'').decode(errors='replace') if isinstance(error.stderr,bytes) else (error.stderr or ''))
 code=None
p=save/'command-output.txt';p.write_text(output);os.chmod(p,0o600)
after=json.loads(subprocess.check_output(['tailscale','serve','status','--json'],text=True,timeout=15)) or {}
p=save/'serve-after.json';p.write_text(json.dumps(after,indent=2)+'\n');os.chmod(p,0o600)
configured=False
try:validate_after(before,after,INPUT['dnsName'],INPUT['port'],INPUT['gatewayPort']);configured=True
except ValueError:pass
urls=re.findall(r'https://(?:login|controlplane)\.tailscale\.com/[^\s<>\x1b]+',output)
if configured:
 marker=Path('/etc/immich-ha/funnel-gateway.json')
 value=dict(version=1,node=INPUT['node'],clusterId=INPUT['clusterId'],dnsName=INPUT['dnsName'],port=INPUT['port'],gatewayPort=INPUT['gatewayPort'])
 marker.write_text(json.dumps(value,indent=2)+'\n');os.chmod(marker,0o600)
print(json.dumps({'configured':configured,'commandCode':code,'approvalUrls':urls,'after':after,
 'url':'https://'+INPUT['dnsName']+('' if INPUT['port']==443 else ':'+str(INPUT['port']))}))
'''


def public_addresses(host):
    query=urllib.parse.urlencode({'name':host,'type':'A'})
    with urllib.request.urlopen('https://dns.google/resolve?'+query,timeout=15) as response:answer=json.load(response)
    ips=[r['data'] for r in answer.get('Answer',[]) if r.get('type')==1]
    if not ips or any(not ipaddress.ip_address(ip).is_global for ip in ips):raise RuntimeError('Public Funnel DNS has not resolved to global addresses yet')
    return ips


def public_get(host,ip,path,port=443,headers=None):
    """Connect to a public address explicitly, avoiding tailnet MagicDNS routing."""
    connection=http.client.HTTPSConnection(host,port,timeout=15,context=ssl.create_default_context())
    raw=socket.create_connection((ip,port),timeout=15)
    try:
        connection.sock=connection._context.wrap_socket(raw,server_hostname=host)
        connection.request('GET',path,headers=headers or {})
        response=connection.getresponse()
        return {'status':response.status,'body':response.read(2048).decode(errors='replace'),
                'node':response.getheader('X-Immich-HA-Node'),'epoch':response.getheader('X-Immich-HA-Epoch')}
    finally:connection.close();raw.close()
