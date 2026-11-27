"""New-install Cloudflare provisioning. No API/config access at import time."""
import json
import urllib.error
import urllib.parse
import urllib.request


class API:
    def __init__(self,token):self.token=token
    def __call__(self,path,method='GET',body=None,missing=False):
        request=urllib.request.Request('https://api.cloudflare.com/client/v4'+path,
            data=None if body is None else json.dumps(body).encode(),method=method,
            headers={'Authorization':'Bearer '+self.token,'Content-Type':'application/json','User-Agent':'ImmichHA-Installer/1.0'})
        try:
            with urllib.request.urlopen(request,timeout=60) as response:value=json.load(response)
        except urllib.error.HTTPError as error:
            if error.code==404 and missing:return None
            # Response bodies may contain a submitted credential: never print them.
            raise RuntimeError(f'Cloudflare {method} {path.split("?")[0]}: HTTP {error.code}; check token scope/permissions') from None
        if not value.get('success'):raise RuntimeError('Cloudflare API rejected operation; no response secrets logged')
        return value.get('result')


def hosts(project):
    return [project['publicHost'],project['adminHost']]+[urllib.parse.urlsplit(project['origins'][n]).hostname for n in ('primary','backup')]


def preflight(api,project,account,zone):
    api('/user/tokens/verify')
    info=api('/zones/'+zone)
    if info['account']['id']!=account or info.get('status')!='active':raise RuntimeError('Zone must be active in the selected account')
    for host in hosts(project):
        if not host.endswith('.'+info['name']):raise RuntimeError('All hostnames must belong to the selected zone')
        if api('/zones/'+zone+'/dns_records?'+urllib.parse.urlencode({'name':host})):
            raise RuntimeError('Hostname already has DNS records; choose unused install hostnames')
    prefix='/accounts/'+account
    if api(prefix+'/workers/scripts/'+project['workerName']+'/settings',missing=True) is not None:
        raise RuntimeError('Worker already exists; choose a new name, never overwrite another deployment')
    # Read all potentially matching Access apps; do not rewrite existing policies.
    apps=[]
    for page in range(1,101):
        batch=api(prefix+'/access/apps?per_page=100&page='+str(page)) or [];apps.extend(batch)
        if len(batch)<100:break
    else:raise RuntimeError('Too many Access apps to verify safely')
    if any(a.get('domain','').split('/')[0] in hosts(project) for a in apps):
        raise RuntimeError('An Access app already owns an install hostname')
    for node in ('primary','backup'):
        matches=api(prefix+'/cfd_tunnel?'+urllib.parse.urlencode({'name':project['workerName']+'-'+node,'is_deleted':'false'})) or []
        if matches:raise RuntimeError('Tunnel name already exists; choose a new worker name')
    if not (api(prefix+'/workers/subdomain') or {}).get('subdomain'):
        raise RuntimeError('Activate the account workers.dev subdomain in Cloudflare once before installing')
    return info


def provision(api,project,account,zone,state,save):
    """Persist each created resource immediately; never adopt an unknown resource."""
    prefix='/accounts/'+account;owned=state.setdefault('cloudflare',{})
    def resource(key,path,body):
        if key not in owned:
            owned[key]=api(path,'POST',body);save()
        return owned[key]
    if 'organization' not in owned:
        value=api(prefix+'/access/organizations',missing=True)
        owned['organization']=value or api(prefix+'/access/organizations','POST',{'name':project['workerName']})
        save()
    providers=api(prefix+'/access/identity_providers') or []
    otp=next((x for x in providers if x.get('type')=='onetimepin'),None)
    if not otp:otp=resource('otp',prefix+'/access/identity_providers',{'name':'Immich HA email login','type':'onetimepin','config':{}})
    service=resource('service',prefix+'/access/service_tokens',{'name':project['workerName']+'-origins','duration':'forever'})
    def app(key,host,decision,include):
        body={'name':project['workerName']+'-'+key,'domain':host,'type':'self_hosted','session_duration':'24h'}
        if key=='admin':body['allowed_idps']=[otp['id']]
        value=resource('app-'+key,prefix+'/access/apps',body)
        resource('policy-'+key,prefix+'/access/apps/'+value['id']+'/policies',
            {'name':'Managed by Immich HA installer','decision':decision,'include':include,'precedence':1})
        return value
    admin=app('admin',project['adminHost'],'allow',[{'email':{'email':e}} for e in project['access']['emails']])
    app('public',project['publicHost'],'bypass',[{'everyone':{}}])
    tokens={}
    for node in ('primary','backup'):
        origin=urllib.parse.urlsplit(project['origins'][node]).hostname
        app(node,origin,'non_identity',[{'service_token':{'token_id':service['id']}}])
        tunnel=resource('tunnel-'+node,prefix+'/cfd_tunnel',{'name':project['workerName']+'-'+node,'config_src':'cloudflare'})
        key='ingress-'+node
        if key not in owned:
            api(prefix+'/cfd_tunnel/'+tunnel['id']+'/configurations','PUT',{'config':{'ingress':[
                {'hostname':origin,'service':'http://127.0.0.1:8080'},{'service':'http_status:404'}]}})
            owned[key]=True;save()
        resource('dns-'+node,'/zones/'+zone+'/dns_records',{'type':'CNAME','proxied':True,'name':origin,'content':tunnel['id']+'.cfargotunnel.com'})
        if 'token' not in tunnel:
            tunnel['token']=api(prefix+'/cfd_tunnel/'+tunnel['id']+'/token');save()
        tokens[node]=tunnel['token']
    project['access'].update(teamDomain=owned['organization']['auth_domain'],audience=admin['aud'])
    return tokens,{'ORIGIN_ACCESS_CLIENT_ID':service['client_id'],'ORIGIN_ACCESS_CLIENT_SECRET':service['client_secret']}


def attach_domains(api,project,account,zone,state,save):
    for name in ('publicHost','adminHost'):
        key='domain-'+name
        if not state['cloudflare'].get(key):
            api('/accounts/'+account+'/workers/domains','PUT',{'hostname':project[name],'service':project['workerName'],'environment':'production','zone_id':zone,'zone_name':state['zoneName']})
            state['cloudflare'][key]=True;save()
