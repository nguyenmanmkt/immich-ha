"""Local fixed-action bootstrap runner. No SSH or controller-supplied code."""
import contextlib
import hashlib
import importlib.util
import io
import ipaddress
import json
import os
from pathlib import Path
import pwd
import re
import shlex
import shutil
import subprocess
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET

APP=Path(__file__).resolve().parents[1]
MEDIA=Path('/mnt/immich-storage')
MANAGED=('library','upload','thumbs','encoded-video','profile')


def run(args,timeout=180):
    result=subprocess.run(args,capture_output=True,text=True,timeout=timeout)
    if result.returncode:raise RuntimeError('Local command failed: '+args[0]+' (see node journal; no secret output forwarded)')
    return result.stdout.strip()


def atomic(path,value,mode=0o600):
    path=Path(path);path.parent.mkdir(parents=True,mode=0o700,exist_ok=True)
    temp=path.with_name(path.name+'.tmp');temp.write_text(value);temp.chmod(mode)
    with temp.open('r+b') as stream:os.fsync(stream.fileno())
    os.replace(temp,path)
    if os.name=='posix':
        descriptor=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(descriptor)
        finally:os.close(descriptor)


def media_inventory():
    rows=[]
    for folder in MANAGED:
        base=MEDIA/folder
        if base.is_symlink():raise RuntimeError('Managed media directory is a symlink')
        if not base.exists():continue
        for item in base.rglob('*'):
            if item.is_symlink():raise RuntimeError('Managed media contains symlinks; inspect manually')
            if item.is_file() and item.name!='.immich':rows.append((str(item.relative_to(MEDIA)),item.stat().st_size))
    rows.sort();return {'digest':hashlib.sha256(json.dumps(rows,separators=(',',':')).encode()).hexdigest(),'files':len(rows),'bytes':sum(x[1] for x in rows)}


def native_env():
    values={}
    for line in Path('/opt/immich/.env').read_text().splitlines():
        if line.lstrip().startswith('#') or '=' not in line:continue
        key,_,text=line.partition('=');parsed=shlex.split(text,comments=True)
        values[key.strip()]=parsed[0] if parsed else ''
    return values


def native_sql(query):
    name=native_env().get('DB_DATABASE_NAME','')
    if not re.fullmatch('[A-Za-z_][A-Za-z0-9_]{0,62}',name):raise RuntimeError('Invalid native database name')
    return run(['runuser','-u','postgres','--','psql','-XAt','-p','5432','-d',name,'-v','ON_ERROR_STOP=1','-c',query])


def inspect(local):
    from pg_stream import replication_address
    ip=str(replication_address(local['ip'],local['networkMode']))
    addresses=json.loads(run(['ip','-j','-4','addr','show']))
    if not any(x.get('local')==ip for interface in addresses for x in interface.get('addr_info',[])):raise RuntimeError('Selected address is not local')
    if local['networkMode']=='tailscale':
        if run(['tailscale','ip','-4'])!=ip:raise RuntimeError('Tailscale mode requires the local tailnet address')
    if not os.path.ismount(MEDIA):raise RuntimeError('Missing media mount')
    if native_env().get('DB_USERNAME')!='immich':raise RuntimeError('Native Immich database role must be immich')
    if local['node']=='primary' and native_env().get('IMMICH_MEDIA_LOCATION')!=str(MEDIA):raise RuntimeError('Node A must already use /mnt/immich-storage')
    inventory=media_inventory()
    table=native_sql("SELECT to_regclass('public.asset') IS NOT NULL;")
    if table!='t':raise RuntimeError('Native Immich schema is not initialized; finish its startup/migrations before joining HA')
    assets=int(native_sql('SELECT count(*) FROM asset;'))
    return {'ip':ip,'replicationIp':ip,'tailscaleIp':ip if local['networkMode']=='tailscale' else None,
      'machineId':Path('/etc/machine-id').read_text().strip(),'mediaMounted':True,
      'existingHa':Path('/etc/immich-ha/config.json').exists() or Path('/etc/postgresql/16/ha').exists(),
      'rootFree':shutil.disk_usage('/var/lib/postgresql').free,'mediaFree':shutil.disk_usage(MEDIA).free,
      'mediaFiles':inventory['files'],'mediaBytes':inventory['bytes'],'assets':assets,
      'version':json.loads(Path('/opt/immich/app/package.json').read_text())['version'],
      'postgres':native_sql('SHOW server_version;'),
      'superuser':native_sql("SELECT rolsuper FROM pg_roles WHERE rolname='immich';")=='t',
      'extensions':native_sql('SELECT extname||\':\'||extversion FROM pg_extension ORDER BY extname;').splitlines()}


def syncthing():
    home=Path(pwd.getpwnam('immich').pw_dir)
    choices=[Path('/var/lib/syncthing-immich/config.xml'),home/'.local/state/syncthing/config.xml',home/'.config/syncthing/config.xml']
    found=[x for x in choices if x.is_file()]
    if len(found)!=1:raise RuntimeError('Cannot identify the immich Syncthing instance')
    gui=ET.parse(found[0]).getroot().find('gui');address=gui.findtext('address')
    if address!='127.0.0.1:8384' or gui.get('tls')=='true':raise RuntimeError('Bootstrap expects Syncthing GUI at loopback HTTP 8384')
    key=gui.findtext('apikey')
    def api(path,data=None,method=None):
        request=urllib.request.Request('http://'+address+path,data=None if data is None else json.dumps(data).encode(),method=method,
            headers={'X-API-Key':key,'Content-Type':'application/json'})
        with urllib.request.urlopen(request,timeout=30) as response:
            value=response.read();return json.loads(value) if value else None
    return api,str(found[0])


def prepare_base(args):
    run(['apt-get','update']);run(['apt-get','install','-y','nginx','syncthing','python3','curl','ca-certificates'],600)
    if args['node']=='backup':
        if int(native_sql('SELECT count(*) FROM asset;')):raise RuntimeError('Backup contains assets; retained')
        run(['systemctl','stop','immich-web.service','immich-ml.service'],120)
        if int(native_sql('SELECT count(*) FROM asset;')) or media_inventory()['files']:
            raise RuntimeError('Backup received data before install; retained it with its app stopped')
        path=Path('/opt/immich/.env');text=path.read_text()
        backup=path.with_name('.env.before-cluster-join')
        if not backup.exists():shutil.copy2(path,backup);backup.chmod(0o600)
        text='\n'.join(x for x in text.splitlines() if not x.startswith('IMMICH_MEDIA_LOCATION='))+'\nIMMICH_MEDIA_LOCATION=/mnt/immich-storage\n'
        metadata=path.stat();atomic(path,text,metadata.st_mode&0o777);os.chown(path,metadata.st_uid,metadata.st_gid)
    account=pwd.getpwnam('immich')
    os.chown(MEDIA,account.pw_uid,account.pw_gid)
    for path in [*[MEDIA/x for x in MANAGED],MEDIA/'backups',MEDIA/'backups/postgres']:
        if not path.exists():path.mkdir();os.chown(path,account.pw_uid,account.pw_gid)
    run(['systemctl','enable','--now','syncthing@immich.service'])
    for _ in range(30):
        try:api,location=syncthing();device=api('/rest/system/status')['myID'];break
        except Exception:time.sleep(1)
    else:raise RuntimeError('Syncthing did not start')
    config=dict(args['config'],syncthing_config=location)
    stage=APP/'.install/node-bundle';stage.mkdir(parents=True,mode=0o700,exist_ok=True)
    for name in ['install-node.sh','agent.py','standby-run','immich-ha.nginx.conf','enable-enforcement.sh','ha_runtime.py','pg_stream.py','quorum_lock.py','quorum_authority.py','immich_upgrade.py','build-immich-release.sh','maintenance-v2.py','app-run-v2','upgrade-v2.sh']:
        (stage/name).write_bytes((APP/'node'/name).read_bytes().replace(b'\r\n',b'\n'))
    atomic(stage/'config.json',json.dumps(config))
    run(['bash',str(stage/'install-node.sh'),args['node']],600)
    if not shutil.which('cloudflared'):
        # Fetch binary key bytes without a shell pipeline or token in argv.
        with urllib.request.urlopen('https://pkg.cloudflare.com/cloudflare-main.gpg',timeout=30) as response:key=response.read()
        key_path=Path('/usr/share/keyrings/cloudflare-main.gpg');key_path.write_bytes(key);key_path.chmod(0o644)
        atomic('/etc/apt/sources.list.d/immich-ha-cloudflared.list','deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main\n',0o644)
        run(['apt-get','update']);run(['apt-get','install','-y','cloudflared'],600)
    atomic('/etc/immich-ha/tunnel-token',args['tunnelToken'])
    atomic('/etc/systemd/system/immich-ha-tunnel.service','''[Unit]
Description=Immich HA dedicated tunnel
After=network-online.target
Wants=network-online.target
[Service]
ExecStart=/usr/bin/cloudflared tunnel --no-autoupdate run --token-file /etc/immich-ha/tunnel-token
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
''',0o644)
    run(['systemctl','daemon-reload']);run(['systemctl','enable','--now','immich-ha-tunnel.service'])
    return {'deviceId':device}


def pair(args):
    api,_=syncthing();peer=args['peerDeviceId'];devices=api('/rest/config/devices')
    if not any(x['deviceID']==peer for x in devices):
        api('/rest/config/devices',{'deviceID':peer,'name':'Immich HA peer','addresses':['tcp://'+args['peerIp']+':22000']},'POST')
    else:api('/rest/config/devices/'+peer,{'addresses':['tcp://'+args['peerIp']+':22000']},'PATCH')
    folders={x['id']:x for x in api('/rest/config/folders')}
    excludes=['/'+x+'/.immich' for x in MANAGED]+['/lost+found','/icloud-download','/backups']
    for name,path in [('immich-library',str(MEDIA)),('immich-database',str(MEDIA/'backups/postgres'))]:
        if name in folders:
            value=folders[name]
            if value['path']!=path or any(x['deviceID'] not in (peer,api('/rest/system/status')['myID']) for x in value['devices']):raise RuntimeError('Existing folder has another path/peer; retained')
            api('/rest/config/folders/'+name,{'type':'sendonly' if args['node']=='primary' else 'receiveonly','paused':False},'PATCH')
        else:
            value=api('/rest/config/defaults/folder');value.update(id=name,label=name,path=path,type='sendonly' if args['node']=='primary' else 'receiveonly',devices=[{'deviceID':peer}],rescanIntervalS=3600,fsWatcherEnabled=True)
            api('/rest/config/folders',value,'POST')
        if name=='immich-library':
            old=api('/rest/db/ignores?folder='+name).get('ignore',[])
            api('/rest/db/ignores?folder='+name,{'ignore':excludes+[x for x in old if x not in excludes]},'POST')
        api('/rest/db/scan?folder='+name,{},'POST')
    return {'paired':True}


def sync(args):
    api,_=syncthing();until=time.monotonic()+24*3600;stable=0
    while time.monotonic()<until:
        connected=api('/rest/system/connections').get('connections',{}).get(args['peerDeviceId'],{}).get('connected')
        states=[api('/rest/db/status?folder='+name) for name in ('immich-library','immich-database')]
        completions=[api('/rest/db/completion?folder='+name+'&device='+args['peerDeviceId']) for name in ('immich-library','immich-database')]
        complete=connected and all(x.get('state')=='idle' and not x.get('needTotalItems',x.get('needFiles',0)) and not x.get('pullErrors',0) for x in states) and all(x.get('completion',0)>=100 for x in completions)
        stable=stable+1 if complete else 0
        if stable>=3:return {'synced':True,'media':media_inventory()}
        time.sleep(5)
    raise RuntimeError('Media synchronization timed out; no database cutover performed')


def payload(code,args):
    stream=io.StringIO()
    with contextlib.redirect_stdout(stream):
        try:exec(compile(code,'fixed local bootstrap payload','exec'),{'INPUT':dict(args)})
        except SystemExit as error:
            if error.code not in (None,0):raise RuntimeError('Local payload stopped unsuccessfully') from None
    return json.loads(stream.getvalue())


def cutover_module():
    spec=importlib.util.spec_from_file_location('local_cutover',APP/'tools/streaming-cutover.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module


def execute(command,local):
    action=command['action'];args=command['args']
    if args['node']!=local['node'] or args['networkMode']!=local['networkMode'] or args['setupId']!=local['id']:raise RuntimeError('Bootstrap identity mismatch')
    if not re.fullmatch('[a-f0-9-]{36}',args['setupId']):raise RuntimeError('Invalid setup ID')
    if action=='inspect':return inspect(local)
    if action=='base':return prepare_base(args)
    if action=='pair':return pair(args)
    if action in ('sync','sync-final'):return sync(args)
    if action=='runtime':
        run(['bash',str(APP/'.install/node-bundle/upgrade-v2.sh')],600);return {'installed':True}
    if action=='enforce':
        until=time.monotonic()+600
        while True:
            try:run(['bash',str(APP/'.install/node-bundle/enable-enforcement.sh')],180);return {'enforcing':True}
            except RuntimeError:
                if time.monotonic()>=until:raise
                time.sleep(10)
    if action=='stage':
        import streaming_setup
        return payload(streaming_setup.STAGE,args['config'])
    if action=='copy':
        import streaming_setup
        return payload(streaming_setup.COPY,{'id':command['id']})
    if action=='stage-check':
        from pg_stream import status
        value=status()
        if not value['inRecovery'] or not value['receiverConnected'] or value['pendingBytes']:raise RuntimeError('Replica WAL is not ready')
        return value
    if action=='save':return payload(cutover_module().SAVE,{'id':args['setupId']})
    if action=='freeze-copy':
        import streaming_setup
        from pg_stream import pg,status
        payload(cutover_module().FREEZE,{})
        atomic('/var/lib/immich-ha/runtime/mode','frozen\n',0o640)
        copied=payload(streaming_setup.COPY,{'id':command['id']})
        return {'copied':copied['copied'],'media':media_inventory(),'lsn':pg('SELECT pg_current_wal_lsn()::text;'),'systemId':status()['systemId']}
    if action=='verify-final':
        from pg_stream import status,pg
        sync(args);barrier=args['barrier'];value=status()
        if not re.fullmatch('[0-9A-F]+/[0-9A-F]+',barrier['lsn']):raise RuntimeError('Invalid final WAL location')
        if not value['inRecovery'] or value['systemId']!=barrier['systemId']:raise RuntimeError('Final lineage mismatch')
        if media_inventory()!=barrier['media']:raise RuntimeError('Final media manifest mismatch')
        for _ in range(120):
            if pg("SELECT COALESCE(pg_last_wal_replay_lsn()>='"+barrier['lsn']+"'::pg_lsn,false);")=='t':return {'verified':True}
            time.sleep(1)
        raise RuntimeError('Final WAL has not replayed')
    if action=='enable':
        return payload(cutover_module().ENABLE,{'node':args['node'],'source':args['node']=='primary','owner':'primary','epoch':args['epoch']})
    if action=='native-backups':
        spec=importlib.util.spec_from_file_location('local_backup_rules',APP/'tools/sync-native-backups.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        api,_=syncthing();old=api('/rest/db/ignores?folder=immich-library').get('ignore') or []
        api('/rest/db/ignores?folder=immich-library',{'ignore':module.rules(old)},'POST')
        return {'configured':True}
    if action=='start':
        atomic('/etc/immich-ha/disable-scheduled-dump','\n')
        run(['systemctl','disable','--now','immich-ha-dump.timer'])
        run(['systemctl','start','immich-ha-agent.service'])
        return {'started':True}
    raise RuntimeError('Unsupported bootstrap action')
