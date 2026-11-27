#!/usr/bin/env python3
"""Read-only quorum routing oracle. Never grants, promotes or writes etcd."""
import base64
from http.server import BaseHTTPRequestHandler
import ipaddress
import json
import os
from pathlib import Path
import re
import socketserver
import ssl
import stat
import threading
import time
import urllib.parse
import urllib.request
import uuid

SOCKET=Path('/run/immich-ha-router/route.sock')


def validate(config):
    if config.get('version')!=1 or str(uuid.UUID(config['clusterId']))!=config['clusterId']:raise ValueError('Invalid router identity')
    if set(config.get('nodes',{}))!={'primary','backup'}:raise ValueError('Two node identities required')
    for value in config['nodes'].values():
        if ipaddress.ip_address(value['ip']) not in ipaddress.ip_network('100.64.0.0/10') or type(value['port']) is not int or not 1024<=value['port']<=65535 or value['port'] in (2283,2379,2380,5432,5433,8080,9081):raise ValueError('Expected a gated Tailscale target')
    if len({v['ip'] for v in config['nodes'].values()})!=2:raise ValueError('Distinct node addresses required')
    endpoints=config.get('endpoints',[])
    if len(endpoints)!=3 or len(set(endpoints))!=3:raise ValueError('Three quorum endpoints required')
    for endpoint in endpoints:
        u=urllib.parse.urlsplit(endpoint)
        if u.scheme!='https' or u.port!=2379 or u.username or u.password or u.path not in ('','/') or u.query or u.fragment or ipaddress.ip_address(u.hostname) not in ipaddress.ip_network('100.64.0.0/10'):raise ValueError('Invalid quorum endpoint')
    return config


def b64(value):return base64.b64encode(value.encode()).decode()


def read_payload(cluster):
    prefix='/immich-ha/'+cluster+'/control/admission/'
    return {'compare':[],'success':[{'request_range':{'key':b64(prefix+name),'serializable':False}}
                                   for name in ('generation','active')],'failure':[]}


def select_route(config,result):
    responses=result.get('responses',[])
    if len(responses)!=2:raise RuntimeError('Incomplete quorum snapshot')
    rows=[]
    for response in responses:
        kvs=response.get('response_range',{}).get('kvs',[])
        if len(kvs)!=1:raise RuntimeError('No live authoritative record')
        rows.append(kvs[0])
    previous,current=[json.loads(base64.b64decode(v['value'])) for v in rows]
    if previous!=current or current.get('released') or current.get('localBlocked') or not re.fullmatch(r'-?[0-9]+',str(rows[1].get('lease','0'))) or int(rows[1].get('lease','0'))==0:
        raise RuntimeError('Authority is not live or is fenced')
    owner=current.get('owner');epoch=current.get('epoch')
    if owner not in config['nodes'] or type(epoch) is not int or epoch<1 or current.get('authority') not in ('cloudflare','fallback','handoff') or not re.fullmatch(r'[a-zA-Z0-9_-]{16,100}',current.get('session','')):
        raise RuntimeError('Invalid authority record')
    return {'owner':owner,'epoch':epoch}


class Reader:
    def __init__(self,config,credentials):
        self.config=validate(config)
        context=ssl.create_default_context(cafile=credentials/'ca.crt')
        context.load_cert_chain(credentials/'client.crt',credentials/'client.key')
        self.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPSHandler(context=context))
        self.body=json.dumps(read_payload(config['clusterId'])).encode()
        self.endpoints=list(config['endpoints'])

    def read(self):
        for endpoint in list(self.endpoints):
            try:
                request=urllib.request.Request(endpoint.rstrip('/')+'/v3/kv/txn',data=self.body,
                    headers={'Content-Type':'application/json','User-Agent':'ImmichHA-WitnessRouter/1'})
                with self.opener.open(request,timeout=2) as response:
                    body=response.read(65537)
                    if len(body)>65536:raise RuntimeError('Oversize quorum response')
                    value=json.loads(body)
                    if 'error' in value:raise RuntimeError('Quorum unavailable')
            except Exception:continue
            # Prefer a working endpoint after a transport failure. Invalid or
            # fenced authority is a closed route, never a reason to hunt for a
            # different record on another member.
            self.endpoints.remove(endpoint);self.endpoints.insert(0,endpoint)
            return select_route(self.config,value)
        raise RuntimeError('No confirmed route')


def boot_time():return time.clock_gettime(time.CLOCK_BOOTTIME)


class Resolver:
    def __init__(self,reader,clock=boot_time):
        self.reader=reader;self.clock=clock;self.lock=threading.Lock();self.until=0;self.value=None

    def resolve(self):
        with self.lock:
            if self.value and self.clock()<self.until:return dict(self.value)
            self.value=None;self.until=0
            started=self.clock()
            value=self.reader.read()
            self.value=dict(value);self.until=started+0.5
            return dict(value)


def handler(resolver):
    class RouteHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path!='/route':self.send_error(404);return
            try:value=resolver.resolve()
            except Exception:
                self.send_response(403)
            else:
                self.send_response(200)
                self.send_header('X-Immich-HA-Node',value['owner'])
                self.send_header('X-Immich-HA-Epoch',str(value['epoch']))
            self.send_header('Cache-Control','no-store');self.send_header('Content-Length','0');self.end_headers()

        def log_message(self,*_):pass
        def address_string(self):return 'local'
    return RouteHandler


class Server(socketserver.ThreadingMixIn,getattr(socketserver,'UnixStreamServer',socketserver.TCPServer)):
    daemon_threads=True
    request_queue_size=64

    def __init__(self,*args):
        self.slots=threading.BoundedSemaphore(64)
        super().__init__(*args)

    def process_request(self,request,client_address):
        if not self.slots.acquire(blocking=False):
            request.sendall(b'HTTP/1.0 503 Service Unavailable\r\nContent-Length: 0\r\n\r\n')
            self.shutdown_request(request);return
        try:super().process_request(request,client_address)
        except BaseException:self.slots.release();raise

    def process_request_thread(self,*args):
        try:super().process_request_thread(*args)
        finally:self.slots.release()


def main():
    if not hasattr(socketserver,'UnixStreamServer'):raise RuntimeError('Witness router requires Linux Unix sockets')
    credentials=Path(os.environ['CREDENTIALS_DIRECTORY'])
    config=validate(json.loads((credentials/'config.json').read_text()))
    resolver=Resolver(Reader(config,credentials))
    if SOCKET.exists():
        info=SOCKET.lstat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid!=os.getuid():raise RuntimeError('Unexpected socket path retained')
        SOCKET.unlink()
    with Server(str(SOCKET),handler(resolver)) as server:
        os.chmod(SOCKET,0o660)
        server.serve_forever()


if __name__=='__main__':main()
