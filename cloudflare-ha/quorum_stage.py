"""Stage an isolated, mutually authenticated etcd cluster. No Immich grants."""
import datetime as dt
import hashlib
import ipaddress
import json
from pathlib import Path
import tarfile
import urllib.request
import uuid

from quorum_setup import NODES

VERSION='3.6.17'
ARCHIVE_SHA256='5c6365e03d0c81f605458f3f9a115f6daaebca08d1fb42d3ab0f1a38fd30909c'
ARCHIVE_URL=f'https://github.com/etcd-io/etcd/releases/download/v{VERSION}/etcd-v{VERSION}-linux-amd64.tar.gz'
BASE='/etc/immich-ha-quorum'
DATA='/var/lib/immich-ha-quorum'
UNIT='immich-ha-quorum.service'


def member_ip(member):
    return member.get('ip') or member['tailscaleIp']


def validate_plan(plan):
    if plan.get('version')!=1 or plan.get('engine')!='etcd' or set(plan.get('members',{}))!=set(NODES):raise ValueError('Invalid quorum topology')
    if str(uuid.UUID(plan['clusterId']))!=plan['clusterId']:raise ValueError('Invalid cluster identity')
    if plan.get('grantIntegrationEnabled') is not False or plan.get('funnelEnabled') is not False:raise ValueError('Installation only supports isolated staging')
    ips=[]
    for member in plan['members'].values():
        from node.quorum_lock import private_address
        ip=private_address(member_ip(member),plan.get('networkMode','tailscale'))
        if member['clientPort']!=2379 or member['peerPort']!=2380:raise ValueError('Expected private quorum endpoints')
        ips.append(ip)
    if len(set(ips))!=3:raise ValueError('Duplicate member addresses')


def validate_check(plan,check,healthy_nodes=NODES):
    statuses=check.get('statuses',[]);health=check.get('health',[]);members=check.get('members',{}).get('members',[])
    endpoints={f'https://{member_ip(member)}:2379' for member in plan['members'].values()}
    expected={f'https://{member_ip(plan["members"][node])}:2379' for node in healthy_nodes}
    if len(expected)<2 or not set(healthy_nodes).issubset(NODES):raise ValueError('A majority of named endpoints is required')
    if len(members)!=3 or {m.get('name') for m in members}!=set(NODES) or any(m.get('isLearner') for m in members):raise RuntimeError('Expected three named voting members')
    if {url for member in members for url in member.get('clientURLs',[])}!=endpoints:raise RuntimeError('Unexpected quorum member endpoints')
    if len(statuses)!=len(expected) or len(health)!=len(expected) or {h.get('endpoint') for h in health}!=expected or any(h.get('health') is not True for h in health):raise RuntimeError('All expected endpoints must be healthy')
    if {item.get('Endpoint') for item in statuses}!=expected or any(item['Status'].get('errors') for item in statuses):raise RuntimeError('Endpoint status is missing or contains errors')
    clusters={item['Status']['header']['cluster_id'] for item in statuses};leaders={item['Status']['leader'] for item in statuses}
    if len(clusters)!=1 or len(leaders)!=1 or not next(iter(leaders)) or next(iter(leaders)) not in {m['ID'] for m in members}:raise RuntimeError('Members disagree on cluster/leader')
    if check.get('unauthenticatedRejected') is not True:raise RuntimeError('TLS client authentication not confirmed')


def digest(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as stream:
        while block:=stream.read(1024*1024):h.update(block)
    return h.hexdigest()


def binaries(directory):
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    archive=directory/f'etcd-v{VERSION}-linux-amd64.tar.gz'
    if not archive.exists():
        part=archive.with_suffix('.partial')
        try:
            request=urllib.request.Request(ARCHIVE_URL,headers={'User-Agent':'ImmichHA-Quorum/1'})
            with urllib.request.urlopen(request,timeout=60) as response,part.open('wb') as output:
                total=0
                while block:=response.read(1024*1024):
                    total+=len(block)
                    if total>64*1024**2:raise RuntimeError('Oversized etcd archive')
                    output.write(block)
            if digest(part)!=ARCHIVE_SHA256:raise RuntimeError('Official etcd archive checksum mismatch')
            part.replace(archive)
        finally:part.unlink(missing_ok=True)
    if digest(archive)!=ARCHIVE_SHA256:raise RuntimeError('Cached etcd archive checksum mismatch')
    result={}
    with tarfile.open(archive) as package:
        for name in ('etcd','etcdctl','etcdutl'):
            member=package.getmember(f'etcd-v{VERSION}-linux-amd64/{name}')
            if not member.isfile():raise RuntimeError('Unexpected binary archive member')
            content=package.extractfile(member).read()
            path=directory/name
            if path.exists() and path.read_bytes()!=content:raise RuntimeError('Retained local binary differs from pinned archive')
            if not path.exists():path.write_bytes(content)
            result[name]=path
    return result


def certificates(plan,directory):
    """Separate peer/client CAs. CA private keys stay on the management machine."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes,serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID,ExtendedKeyUsageOID
    directory=Path(directory);manifest=directory/'identity.json'
    expected={'clusterId':plan['clusterId'],'members':plan['members']}
    names=['peer-ca.crt','peer-ca.key','client-ca.crt','client-ca.key']
    for node in NODES:
        names.extend(f'{node}-{kind}.{ext}' for kind in ('peer','server','probe') for ext in ('crt','key'))
    if manifest.exists():
        saved=json.loads(manifest.read_text())
        if saved.get('identity')!=expected or set(saved.get('hashes',{}))!=set(names):raise RuntimeError('PKI identity differs; no automatic rotation')
        for name in names:
            if not (directory/name).is_file() or digest(directory/name)!=saved['hashes'][name]:raise RuntimeError('Incomplete or altered saved quorum PKI')
            if name.endswith('.crt') and x509.load_pem_x509_certificate((directory/name).read_bytes()).not_valid_after_utc<=dt.datetime.now(dt.timezone.utc):raise RuntimeError('Quorum certificate expired; explicit renewal required')
        return directory
    if directory.exists() and any(directory.iterdir()):raise RuntimeError('Partial PKI retained; inspect before creating a new identity')
    directory.mkdir(parents=True,exist_ok=True)
    now=dt.datetime.now(dt.timezone.utc)
    def write(name,data):
        with (directory/name).open('xb') as stream:stream.write(data)
        (directory/name).chmod(0o600)
    def make_key():return ec.generate_private_key(ec.SECP256R1())
    def private(key):return key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption())
    cas={}
    for kind in ('peer','client'):
        key=make_key();subject=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'Immich HA '+kind+' CA '+plan['clusterId'])])
        certificate=(x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now-dt.timedelta(minutes=10)).not_valid_after(now+dt.timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True,path_length=0),critical=True)
            .add_extension(x509.KeyUsage(False,False,False,False,False,True,True,False,False),critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()),critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(key.public_key()),critical=False).sign(key,hashes.SHA256()))
        write(kind+'-ca.key',private(key));write(kind+'-ca.crt',certificate.public_bytes(serialization.Encoding.PEM));cas[kind]=(key,certificate)
    for node in NODES:
        for kind in ('peer','server','probe'):
            ca_key,ca=cas['peer' if kind=='peer' else 'client'];key=make_key()
            usage=[ExtendedKeyUsageOID.CLIENT_AUTH] if kind=='probe' else [ExtendedKeyUsageOID.SERVER_AUTH]
            if kind=='peer':usage.append(ExtendedKeyUsageOID.CLIENT_AUTH)
            builder=(x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'immich-ha-'+kind+'-'+node)]))
                .issuer_name(ca.subject).public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now-dt.timedelta(minutes=10)).not_valid_after(now+dt.timedelta(days=730))
                .add_extension(x509.BasicConstraints(ca=False,path_length=None),critical=True)
                .add_extension(x509.KeyUsage(True,False,False,False,False,False,False,False,False),critical=True)
                .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()),critical=False)
                .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),critical=False)
                .add_extension(x509.ExtendedKeyUsage(usage),critical=True))
            if kind!='probe':builder=builder.add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(member_ip(plan['members'][node])))]),critical=False)
            certificate=builder.sign(ca_key,hashes.SHA256())
            write(f'{node}-{kind}.key',private(key));write(f'{node}-{kind}.crt',certificate.public_bytes(serialization.Encoding.PEM))
    identity={'identity':expected,'hashes':{name:digest(directory/name) for name in names}}
    with manifest.open('x',encoding='utf-8') as stream:json.dump(identity,stream,indent=2)
    return directory


def configuration(plan,node):
    member=plan['members'][node];ip=str(ipaddress.IPv4Address(member_ip(member)))
    def tls(kind,ca):
        value={'cert-file':f'{BASE}/{kind}.crt','key-file':f'{BASE}/{kind}.key',
                'trusted-ca-file':f'{BASE}/{ca}-ca.crt','client-cert-auth':True}
        # The JSON gateway dials the local gRPC listener as a TLS client.
        # Keep the server leaf serverAuth-only; use the existing client leaf.
        if kind=='server':value.update({'client-cert-file':f'{BASE}/probe.crt','client-key-file':f'{BASE}/probe.key'})
        return value
    return {'name':node,'data-dir':DATA+'/data',
        'listen-client-urls':f'https://{ip}:{member["clientPort"]}',
        'advertise-client-urls':f'https://{ip}:{member["clientPort"]}',
        'listen-peer-urls':f'https://{ip}:{member["peerPort"]}',
        'initial-advertise-peer-urls':f'https://{ip}:{member["peerPort"]}',
        'initial-cluster':','.join(f'{n}=https://{member_ip(plan["members"][n])}:{plan["members"][n]["peerPort"]}' for n in NODES),
        'initial-cluster-token':'immich-ha-'+plan['clusterId'],'initial-cluster-state':'new',
        'heartbeat-interval':200,'election-timeout':2000,'quota-backend-bytes':128*1024**2,
        'auto-compaction-mode':'periodic','auto-compaction-retention':'1h','max-wals':3,'max-snapshots':3,
        'client-transport-security':tls('server','client'),'peer-transport-security':tls('peer','peer'),
        'logger':'zap','log-level':'info','log-outputs':['stderr']}


def add_client_key_identifiers(plan,directory):
    """Explicit compatibility repair: reissue leaf certs with existing keys/CA.

    Does not rotate identities, extend validity or touch peer certificates.
    Returns old/new fingerprints so the installer can safely update its record.
    """
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes,serialization
    directory=Path(directory);certificates(plan,directory)
    ca=x509.load_pem_x509_certificate((directory/'client-ca.crt').read_bytes())
    ca_key=serialization.load_pem_private_key((directory/'client-ca.key').read_bytes(),password=None)
    pub=lambda key:key.public_bytes(serialization.Encoding.DER,serialization.PublicFormat.SubjectPublicKeyInfo)
    if pub(ca_key.public_key())!=pub(ca.public_key()):raise RuntimeError('Existing CA key does not match certificate')
    changes={};content={}
    for node in NODES:
        for kind in ('server','probe'):
            name=f'{node}-{kind}.crt';old=x509.load_pem_x509_certificate((directory/name).read_bytes())
            try:old.extensions.get_extension_for_class(x509.AuthorityKeyIdentifier);continue
            except x509.ExtensionNotFound:pass
            key=serialization.load_pem_private_key((directory/f'{node}-{kind}.key').read_bytes(),password=None)
            if pub(key.public_key())!=pub(old.public_key()) or old.issuer!=ca.subject:raise RuntimeError('Existing leaf identity does not match')
            builder=(x509.CertificateBuilder().subject_name(old.subject).issuer_name(old.issuer).public_key(old.public_key())
                .serial_number(x509.random_serial_number()).not_valid_before(old.not_valid_before_utc).not_valid_after(old.not_valid_after_utc))
            for extension in old.extensions:builder=builder.add_extension(extension.value,extension.critical)
            try:old.extensions.get_extension_for_class(x509.SubjectKeyIdentifier)
            except x509.ExtensionNotFound:builder=builder.add_extension(x509.SubjectKeyIdentifier.from_public_key(old.public_key()),False)
            builder=builder.add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca.public_key()),False)
            content[name]=builder.sign(ca_key,hashes.SHA256()).public_bytes(serialization.Encoding.PEM)
            changes[name]={'old':digest(directory/name),'new':hashlib.sha256(content[name]).hexdigest()}
    if not changes:return {}
    save=directory.parent/('quorum-cert-compat-'+uuid.uuid4().hex);save.mkdir(mode=0o700)
    (save/'identity.json').write_bytes((directory/'identity.json').read_bytes())
    for name,value in content.items():
        (save/name).write_bytes((directory/name).read_bytes())
        temporary=(directory/name).with_suffix('.crt.tmp');temporary.write_bytes(value);temporary.chmod(0o600);temporary.replace(directory/name)
    manifest=json.loads((directory/'identity.json').read_text())
    for name,value in changes.items():manifest['hashes'][name]=value['new']
    temporary=directory/'identity.json.tmp';temporary.write_text(json.dumps(manifest,indent=2));temporary.replace(directory/'identity.json')
    return changes


def service(network_mode='tailscale'):
    if network_mode not in ('lan','tailscale'):raise ValueError('Invalid network mode')
    dependencies='network-online.target'+(' tailscaled.service' if network_mode=='tailscale' else '')
    return f'''[Unit]
Description=Immich HA quorum (isolated staging)
Wants={dependencies}
After={dependencies}
StartLimitIntervalSec=0

[Service]
Type=simple
User=immich-quorum
Group=immich-quorum
ExecStart=/opt/immich-ha-quorum/v{VERSION}/etcd --config-file={BASE}/etcd.json
Restart=on-failure
RestartSec=5
TimeoutStopSec=30
UMask=0077
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
ReadWritePaths={DATA}
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
MemoryMax=256M
CPUQuota=50%
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
'''


def package(plan,node,pki,executables):
    validate_plan(plan)
    if node not in NODES:raise ValueError('Unknown member')
    content={name:path.read_bytes() for name,path in executables.items()}
    if set(content)!={'etcd','etcdctl','etcdutl'}:raise ValueError('Unexpected binary set')
    paths={f'/opt/immich-ha-quorum/v{VERSION}/{name}':name for name in content}
    for name in ('peer-ca.crt','client-ca.crt'):
        content[name]=(pki/name).read_bytes();paths[f'{BASE}/{name}']=name
    for kind in ('peer','server','probe'):
        for suffix in ('crt','key'):
            name=kind+'.'+suffix;content[name]=(pki/f'{node}-{name}').read_bytes();paths[f'{BASE}/{name}']=name
    content['etcd.json']=(json.dumps(configuration(plan,node),indent=2)+'\n').encode();paths[BASE+'/etcd.json']='etcd.json'
    content[UNIT]=service(plan.get('networkMode','tailscale')).encode();paths['/etc/systemd/system/'+UNIT]=UNIT
    hashes={target:hashlib.sha256(content[source]).hexdigest() for target,source in paths.items()}
    manifest={'clusterId':plan['clusterId'],'node':node,'version':VERSION,'grantIntegrationEnabled':False,'funnelEnabled':False,'hashes':hashes}
    data={'manifest':manifest,'clusterId':plan['clusterId'],'node':node,'ip':member_ip(plan['members'][node]),
          'networkMode':plan.get('networkMode','tailscale'),'version':VERSION,'binaryDir':f'/opt/immich-ha-quorum/v{VERSION}','files':paths,'installedHashes':hashes}
    return content,data


PREPARE = r'''
import hashlib,json,os,pwd,shutil,subprocess,platform
from pathlib import Path
os.umask(0o077)
stage=Path(INPUT['stage']);base=Path('/etc/immich-ha-quorum');data=Path('/var/lib/immich-ha-quorum')
marker=base/'installation.json';unit=Path('/etc/systemd/system/immich-ha-quorum.service')
if os.getuid()!=0 or platform.machine()!='x86_64':raise RuntimeError('Expected root on amd64 LXC')
def digest(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def verify():
    for name,sha in INPUT['installedHashes'].items():
        if not Path(name).is_file() or digest(Path(name))!=sha:raise RuntimeError('Existing quorum file differs; no automatic overwrite')
if marker.exists():
    if json.loads(marker.read_text())!=INPUT['manifest']:raise RuntimeError('Existing cluster identity differs; no restage')
    verify();os.chmod(base,0o750);subprocess.run(['systemctl','daemon-reload'],check=True,timeout=30);print(json.dumps({'prepared':True,'reused':True}));raise SystemExit(0)
if base.exists() or data.exists() or unit.exists():raise RuntimeError('Unowned or partial quorum installation retained; inspect before resuming')
listeners=subprocess.check_output(['ss','-H','-lnt'],text=True)
if any(line.split()[3].endswith((':'+str(p))) for p in (2379,2380) for line in listeners.splitlines() if len(line.split())>=4):raise RuntimeError('Quorum port already in use')
if shutil.disk_usage('/').free<1024**3:raise RuntimeError('Insufficient root disk space')
if INPUT.get('networkMode','tailscale')=='tailscale':
 if subprocess.check_output(['tailscale','ip','-4'],text=True).strip()!=INPUT['ip']:raise RuntimeError('Wrong Tailscale identity')
else:
 addresses=json.loads(subprocess.check_output(['ip','-j','-4','addr','show'],text=True))
 if not any(a.get('local')==INPUT['ip'] for i in addresses for a in i.get('addr_info',[])):raise RuntimeError('Wrong LAN identity')
try:pwd.getpwnam('immich-quorum')
except KeyError:subprocess.run(['useradd','--system','--home-dir',str(data),'--shell','/usr/sbin/nologin','immich-quorum'],check=True)
account=pwd.getpwnam('immich-quorum');base.mkdir(mode=0o750);os.chmod(base,0o750);os.chown(base,0,account.pw_gid)
data.mkdir(mode=0o700);os.chown(data,account.pw_uid,account.pw_gid)
binary=Path(INPUT['binaryDir']);binary.mkdir(parents=True,exist_ok=False)
os.chmod(binary.parent,0o755);os.chmod(binary,0o755)
for name,source in INPUT['files'].items():
    target=Path(name);target.parent.mkdir(parents=True,exist_ok=True)
    shutil.copyfile(stage/source,target)
    if str(target).startswith(str(binary)+'/'):os.chmod(target,0o755)
    elif target==unit:os.chmod(target,0o644)
    else:os.chmod(target,0o640);os.chown(target,0,account.pw_gid)
verify()
with marker.open('x') as output:json.dump(INPUT['manifest'],output,indent=2);output.flush();os.fsync(output.fileno())
result=subprocess.run([str(binary/'etcd'),'--version'],capture_output=True,text=True,check=True,timeout=10)
if 'etcd Version: '+INPUT['version'] not in result.stdout:raise RuntimeError('Wrong etcd binary version')
subprocess.run(['systemctl','daemon-reload'],check=True,timeout=30)
print(json.dumps({'prepared':True,'reused':False,'version':INPUT['version']}))
'''

START = r'''
import json,subprocess
from pathlib import Path
marker=json.loads(Path('/etc/immich-ha-quorum/installation.json').read_text())
if marker['clusterId']!=INPUT['clusterId'] or marker['grantIntegrationEnabled'] or marker['funnelEnabled']:raise RuntimeError('Wrong isolated staging identity')
subprocess.run(['systemctl','enable','--now','immich-ha-quorum.service'],check=True,timeout=30)
print(json.dumps({'started':True}))
'''

CHECK = r'''
import json,subprocess,ssl,urllib.request,urllib.error
from pathlib import Path
marker=json.loads(Path('/etc/immich-ha-quorum/installation.json').read_text())
if marker['clusterId']!=INPUT['clusterId'] or marker['grantIntegrationEnabled']:raise RuntimeError('Wrong isolated quorum identity')
base='/etc/immich-ha-quorum/'
common=[INPUT['etcdctl'],'--endpoints='+','.join(INPUT['endpoints']),'--cacert='+base+'client-ca.crt','--cert='+base+'probe.crt','--key='+base+'probe.key','--dial-timeout=5s','--command-timeout=15s']
def call(args):
    p=subprocess.run(common+args,capture_output=True,text=True,timeout=60)
    if p.returncode:raise RuntimeError('Quorum check failed: '+p.stderr[-600:])
    return p.stdout
statuses=json.loads(call(['endpoint','status','--write-out=json']))
health=json.loads(call(['endpoint','health','--write-out=json']))
members=json.loads(call(['member','list','--write-out=json']))
context=ssl.create_default_context(cafile=base+'client-ca.crt')
try:
    with urllib.request.urlopen(INPUT['localEndpoint']+'/health',context=context,timeout=5) as response:
        if response.status<400:raise RuntimeError('Unauthenticated request unexpectedly accepted')
except (ssl.SSLError,urllib.error.URLError,ConnectionError):pass
resources=subprocess.check_output(['systemctl','show','immich-ha-quorum.service','-p','MemoryCurrent','-p','ActiveState'],text=True)
print(json.dumps({'statuses':statuses,'health':health,'members':members,'unauthenticatedRejected':True,'resources':resources}))
'''

SMOKE = r'''
import json,re,subprocess,time
from pathlib import Path
marker=json.loads(Path('/etc/immich-ha-quorum/installation.json').read_text())
prefix='/immich-ha/'+marker['clusterId']+'/checks/'
key=INPUT['key']
if not key.startswith(prefix) or not re.fullmatch(r'[a-f0-9]{32}',key[len(prefix):]):raise RuntimeError('Only disposable quorum check keys are allowed')
base='/etc/immich-ha-quorum/'
common=[INPUT['etcdctl'],'--cacert='+base+'client-ca.crt','--cert='+base+'probe.crt','--key='+base+'probe.key','--dial-timeout=5s','--command-timeout=10s']
def run(endpoint,args):
    p=subprocess.run(common+['--endpoints='+endpoint]+args,capture_output=True,text=True,timeout=20)
    if p.returncode:raise RuntimeError('Disposable quorum read/write check failed')
    return p.stdout.strip()
if INPUT['action']=='put':result=run(INPUT['endpoints'][0],['put',key,INPUT['value']])=='OK'
elif INPUT['action']=='get':result=all(run(endpoint,['get',key,'--print-value-only'])==INPUT['value'] for endpoint in INPUT['endpoints'])
elif INPUT['action']=='delete':result=run(INPUT['endpoints'][0],['del',key]) in ('0','1')
else:raise RuntimeError('Unknown check action')
if not result:raise RuntimeError('Disposable quorum check did not match')
print(json.dumps({'action':INPUT['action'],'ok':result}))
'''

TLS_COMPAT = r'''
import hashlib,json,os,pwd,subprocess
from pathlib import Path
base=Path('/etc/immich-ha-quorum');marker=base/'installation.json';path=base/'etcd.json'
value=json.loads(marker.read_text());old=json.loads(path.read_text());new=INPUT['config']
if value['clusterId']!=INPUT['clusterId'] or value['node']!=INPUT['node'] or value['grantIntegrationEnabled']:raise RuntimeError('Only isolated quorum may update TLS compatibility')
legacy=json.loads(json.dumps(new));legacy['client-transport-security'].pop('client-cert-file');legacy['client-transport-security'].pop('client-key-file')
if old not in (legacy,new) or hashlib.sha256(path.read_bytes()).hexdigest()!=value['hashes'][str(path)]:raise RuntimeError('Unexpected existing quorum configuration')
changed=old!=new
for name,record in INPUT['changes'].items():
 if name not in ('server.crt','probe.crt'):raise RuntimeError('Only client-facing leaf certificates may change')
 target=base/name;actual=hashlib.sha256(target.read_bytes()).hexdigest()
 if actual not in (record['old'],record['new']) or value['hashes'][str(target)] not in (record['old'],record['new']):raise RuntimeError('Installed certificate differs from migration record')
 content=bytes.fromhex(record['content'])
 if hashlib.sha256(content).hexdigest()!=record['new']:raise RuntimeError('New certificate fingerprint differs')
 if actual!=record['new']:
  saved=target.stat();temp=target.with_suffix('.crt.compat-stage');temp.write_bytes(content);os.chmod(temp,saved.st_mode & 0o777);os.chown(temp,saved.st_uid,saved.st_gid);os.replace(temp,target);changed=True
 value['hashes'][str(target)]=record['new']
group=pwd.getpwnam('immich-quorum').pw_gid
for name in ('probe.crt','probe.key'):os.chown(base/name,0,group);os.chmod(base/name,0o640)
content=(json.dumps(new,indent=2)+'\n').encode();saved=path.stat();temp=path.with_suffix('.json.tmp');temp.write_bytes(content);os.chmod(temp,saved.st_mode & 0o777);os.chown(temp,saved.st_uid,saved.st_gid);os.replace(temp,path)
value['hashes'][str(path)]=hashlib.sha256(content).hexdigest();temp=marker.with_suffix('.json.tmp');temp.write_text(json.dumps(value,indent=2));os.chmod(temp,0o600);os.replace(temp,marker)
if changed:subprocess.run(['systemctl','restart','immich-ha-quorum.service'],check=True,timeout=30)
print(json.dumps({'tlsCompatibilityUpdated':True,'restarted':changed,'caAndPrivateKeysUnchanged':True}))
'''
