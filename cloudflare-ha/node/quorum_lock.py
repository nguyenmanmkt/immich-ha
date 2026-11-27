"""Shadow-only lease admission against etcd. Never authorizes an Immich writer.

All keys are confined to /immich-ha/<cluster>/shadow/. Native TTL leases and
compare-and-swap model the common lock before enforcement is introduced.
"""
import base64
import ipaddress
import json
import math
from pathlib import Path,PurePosixPath
import re
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

CONFIG=Path('/etc/immich-ha/quorum-shadow.json')


def validate_config(config):
    if config.get('version')!=1 or config.get('mode')!='shadow':raise ValueError('Only shadow mode is implemented')
    if str(uuid.UUID(config['clusterId']))!=config['clusterId']:raise ValueError('Invalid quorum identity')
    if config.get('node') not in ('primary','backup'):raise ValueError('Only Immich agents observe grants')
    endpoints=config.get('endpoints',[])
    if len(endpoints)!=3 or len(set(endpoints))!=3:raise ValueError('Three distinct endpoints required')
    for endpoint in endpoints:
        url=urllib.parse.urlsplit(endpoint)
        if url.scheme!='https' or url.port!=2379 or url.username or url.password or url.path not in ('','/') or url.query or url.fragment:
            raise ValueError('Invalid etcd endpoint')
        if ipaddress.ip_address(url.hostname) not in ipaddress.ip_network('100.64.0.0/10'):raise ValueError('Expected Tailscale endpoints')
    for name in ('ca','cert','key'):
        if not PurePosixPath(config[name]).is_absolute():raise ValueError('Absolute TLS paths required')
    return config


def b64(value):return base64.b64encode(value.encode()).decode()


class Etcd:
    def __init__(self,config,validator=validate_config):
        self.config=validator(config)
        self.context=ssl.create_default_context(cafile=config['ca'])
        self.context.load_cert_chain(config['cert'],config['key'])

    def call(self,path,payload):
        if path not in ('/v3/kv/txn','/v3/lease/grant','/v3/lease/revoke'):raise ValueError('Unsupported quorum operation')
        body=json.dumps(payload).encode()
        for endpoint in self.config['endpoints']:
            request=urllib.request.Request(endpoint.rstrip('/')+path,data=body,
                headers={'Content-Type':'application/json','User-Agent':'ImmichHA-QuorumShadow/1'})
            try:
                with urllib.request.urlopen(request,context=self.context,timeout=2) as response:
                    raw=response.read(256*1024+1)
                    if len(raw)>256*1024:raise RuntimeError('Quorum response too large')
                    value=json.loads(raw)
                    if 'error' in value:raise RuntimeError('Quorum API rejected request')
                    return value
            except (urllib.error.URLError,TimeoutError,ConnectionError):continue
        raise RuntimeError('Quorum shadow unavailable')


class ShadowLock:
    def __init__(self,client,cluster,scope='admission'):
        if str(uuid.UUID(cluster))!=cluster or not re.fullmatch('[a-z0-9-]{1,80}',scope):raise ValueError('Invalid shadow namespace')
        self.client=client
        self.prefix='/immich-ha/'+cluster+'/shadow/'+scope+'/'
        self.keys=[self.prefix+'generation',self.prefix+'active']

    def read(self):
        result=self.client.call('/v3/kv/txn',{'compare':[],
            'success':[{'request_range':{'key':b64(key),'serializable':False}} for key in self.keys], 'failure':[]})
        responses=result.get('responses',[])
        if len(responses)!=2:raise RuntimeError('Incomplete quorum snapshot')
        rows=[]
        for item in responses:
            values=item['response_range'].get('kvs',[])
            if not values:rows.append({'revision':0,'value':None,'lease':'0'});continue
            if len(values)!=1:raise RuntimeError('Unexpected shadow key count')
            row=values[0]
            rows.append({'revision':int(row['mod_revision']),'value':json.loads(base64.b64decode(row['value'])), 'lease':str(row.get('lease','0'))})
        return rows

    def claim(self,authority,owner,epoch,session,ttl):
        if authority not in ('cloudflare','fallback') or owner not in ('primary','backup') or type(epoch) is not int or epoch<1:
            raise ValueError('Invalid proposed authority')
        if not re.fullmatch('[a-zA-Z0-9_-]{16,100}',session) or type(ttl) is not int or not 5<=ttl<=300:raise ValueError('Invalid proposed session/TTL')
        proposal={'version':1,'authority':authority,'owner':owner,'epoch':epoch,'session':session,'claim':uuid.uuid4().hex}
        for attempt in range(3):
            rows=self.read();previous=rows[0]['value'];active=rows[1]['value']
            if previous and (epoch<previous['epoch'] or epoch==previous['epoch'] and any(proposal[k]!=previous[k] for k in ('authority','owner','session'))):
                return {'admitted':False,'reason':'stale-generation','owner':previous['owner'],'epoch':previous['epoch']}
            if active and any(proposal[k]!=active[k] for k in ('authority','owner','epoch','session')):
                return {'admitted':False,'reason':'lease-held','owner':active['owner'],'epoch':active['epoch']}
            lease=self.client.call('/v3/lease/grant',{'TTL':ttl})
            lease_id=str(lease.get('ID','0'))
            if not re.fullmatch('-?[0-9]{1,20}',lease_id) or int(lease_id)==0 or int(lease.get('TTL',0))<ttl:raise RuntimeError('Invalid etcd lease')
            compares=[{'target':'MOD','result':'EQUAL','key':b64(key),'mod_revision':str(row['revision'])} for key,row in zip(self.keys,rows)]
            encoded=b64(json.dumps(proposal,separators=(',',':')))
            writes=[{'request_put':{'key':b64(self.keys[0]),'value':encoded}},
                    {'request_put':{'key':b64(self.keys[1]),'value':encoded,'lease':lease_id}}]
            try:
                result=self.client.call('/v3/kv/txn',{'compare':compares,'success':writes,'failure':[]})
                # A response may have been lost after commit and replayed as a
                # failed CAS. Confirm the exact unique proposal, never guess.
                confirmed=result.get('succeeded') is True
                if not confirmed:
                    current=self.read()
                    confirmed=all(row['value']==proposal for row in current) and current[1]['lease']==lease_id
                if confirmed:
                    old_lease=rows[1]['lease']
                    if old_lease!='0' and old_lease!=lease_id:
                        try:self.client.call('/v3/lease/revoke',{'ID':old_lease})
                        except RuntimeError:pass
                    return {'admitted':True,'owner':owner,'epoch':epoch,'authority':authority,'ttl':int(lease['TTL'])}
            except BaseException:
                # Uncertain commit: do not revoke a potentially live proposal.
                # Its TTL is bounded. No caller may treat this as admission.
                raise
            self.client.call('/v3/lease/revoke',{'ID':lease_id})
        return {'admitted':False,'reason':'concurrent-update'}


class ShadowMonitor:
    def __init__(self,config):
        self.config=validate_config(config)
        self.lock=ShadowLock(Etcd(config),config['clusterId'])
        self.condition=threading.Condition();self.pending=None
        self.result={'mode':'shadow','state':'waiting','checkedAt':0}
        threading.Thread(target=self.run,name='quorum-shadow',daemon=True).start()

    def submit(self,view,session):
        with self.condition:
            self.pending=(dict(view),session);self.condition.notify()

    def telemetry(self):
        with self.condition:return dict(self.result)

    def run(self):
        while True:
            with self.condition:
                self.condition.wait_for(lambda:self.pending is not None)
                view,session=self.pending;self.pending=None
            try:
                remaining=view.get('deadline',0)-time.monotonic()
                if view.get('role')=='active' and view.get('owner')==self.config['node'] and 0<remaining<=240:
                    result=self.lock.claim('cloudflare',self.config['node'],view['epoch'],session,min(300,math.ceil(remaining)+30))
                    value={'state':'matched' if result['admitted'] else 'contended',
                        'owner':result.get('owner'),'epoch':result.get('epoch'),'reason':result.get('reason')}
                else:
                    generation,active=self.lock.read();record=active['value']
                    value={'state':'observing' if record else 'waiting',
                        'owner':record.get('owner') if record else None,'epoch':record.get('epoch') if record else None}
            except Exception:value={'state':'unavailable'}
            with self.condition:self.result={'mode':'shadow',**value,'checkedAt':int(time.time()*1000)}
