#!/usr/bin/env python3
"""Staged upgrade to a dedicated PostgreSQL 16/ha cluster; secrets stay on SSH.

Run from the project root. Stage never changes the serving app or 16/main.
Cutover requires the staged WAL test, pauses automation, and retains rollback files.
"""
import argparse
import json
import sys
import uuid

from deployment import ROOT, connection, remote, control, upload_module


PREFLIGHT = '''
import json,os,subprocess
from pathlib import Path
c=json.loads(Path('/etc/immich-ha/config.json').read_text())
r=Path('/var/lib/immich-ha/runtime')
assert os.path.ismount('/mnt/immich-storage')
tail=subprocess.check_output(['tailscale','ip','-4'],text=True).strip()
assert tail.startswith('100.')
def sql(q):return subprocess.check_output(['runuser','-u','postgres','--','psql','-XAt','-d','postgres','-c',q],text=True).strip()
print(json.dumps({'node':c['node'],'ip':tail,'db':(r/'current-db').read_text().strip(),
 'existingHa':Path('/etc/immich-ha/streaming.json').exists() or Path('/etc/postgresql/16/ha').exists(),
 'mode':(r/'mode').read_text().strip(),'version':sql('SHOW server_version;'),
 'superuser':sql("SELECT rolsuper FROM pg_roles WHERE rolname='immich';")=='t',
 'free':__import__('shutil').disk_usage('/var/lib/postgresql').free}))
'''

STAGE = '''
import json,os,pwd,re,secrets,shlex,subprocess,sys
from pathlib import Path
sys.path.insert(0,'/usr/local/lib/immich-ha')
from pg_stream import clone,pg,ctl,status,install_network_guard
c=INPUT
assert c['node']==json.loads(Path('/etc/immich-ha/config.json').read_text())['node']
assert not Path('/etc/immich-ha/streaming.json').exists(), 'Already staged; inspect instead of overwriting'
def run(a,**kw):
 p=subprocess.run(a,capture_output=True,**kw)
 if p.returncode:raise RuntimeError('Stage command failed: '+a[0])
 return p.stdout
def write(p,text,uid=0,gid=0,mode=0o600):
 p=Path(p);p.write_text(text);os.chmod(p,mode);os.chown(p,uid,gid)
account=pwd.getpwnam('postgres')
write('/var/lib/postgresql/.immich-ha-replication.pass',c['peer_ip']+':5433:*:immich_ha_repl:'+c.pop('replication_password')+'\\n',account.pw_uid,account.pw_gid)
app_password=c.pop('app_password')
app_super=c.pop('app_superuser')
repl_password=c.pop('role_password')
run(['pg_createcluster','16','ha','--port','5433','--start-conf','manual','--locale','C.UTF-8','--','--data-checksums'])
conf=Path('/etc/postgresql/16/ha/postgresql.conf')
with conf.open('a') as f:f.write("\\n# Managed Immich streaming cluster\\nlisten_addresses = '127.0.0.1,"+c['local_ip']+"'\\nshared_preload_libraries='vchord.so'\\nwal_level=replica\\nwal_log_hints=on\\nfull_page_writes=on\\nhot_standby=on\\nmax_wal_senders=4\\nmax_replication_slots=4\\nwal_keep_size='512MB'\\nmax_wal_size='512MB'\\nmax_slot_wal_keep_size='512MB'\\nwal_receiver_status_interval='1s'\\nwal_sender_timeout='30s'\\nwal_receiver_timeout='30s'\\n")
with Path('/etc/postgresql/16/ha/pg_hba.conf').open('a') as f:f.write('\\nhost replication immich_ha_repl '+c['peer_ip']+'/32 scram-sha-256\\n')
write('/etc/immich-ha/streaming.json',json.dumps(c))
install_network_guard()
if c['source']:
 ctl('start')
 def literal(v):return "'"+v.replace("'","''")+"'"
 pg('CREATE ROLE immich LOGIN '+('SUPERUSER' if app_super else '')+' PASSWORD '+literal(app_password)+';')
 pg('CREATE ROLE immich_ha_repl LOGIN REPLICATION PASSWORD '+literal(repl_password)+';')
 pg('CREATE DATABASE immich_stream OWNER immich;')
else:
 clone(c)
app=Path('/var/lib/immich-ha/runtime/streaming-app.env.staged')
write(app,'DB_HOSTNAME=127.0.0.1\\nDB_PORT=5433\\nDB_USERNAME=immich\\nDB_PASSWORD='+shlex.quote(app_password)+'\\n',0,pwd.getpwnam('immich').pw_gid,0o640)
print(json.dumps({'staged':True,'postgres':status()}))
'''

COPY = '''
import json,os,subprocess
from pathlib import Path
db=Path('/var/lib/immich-ha/runtime/current-db').read_text().strip()
assert __import__('re').fullmatch('[A-Za-z_][A-Za-z0-9_]{0,62}',db)
folder=Path('/mnt/immich-storage/backups/pg-stream');folder.mkdir(exist_ok=True,mode=0o700)
path=folder/('migration-'+INPUT['id']+'.dump')
with path.open('xb') as f:
 os.chmod(path,0o600)
 result=subprocess.run(['runuser','-u','postgres','--','pg_dump','-p','5432','-Fc','-d',db],stdout=f,stderr=subprocess.PIPE,timeout=900)
 if result.returncode:raise RuntimeError('Migration dump failed')
# Recreate only the dedicated staging database, never the serving old database.
def cmd(a,**kw):
 p=subprocess.run(['runuser','-u','postgres','--',*a],capture_output=True,timeout=900,**kw)
 if p.returncode:raise RuntimeError('Staging restore failed: '+a[0])
cmd(['dropdb','-p','5433','--force','immich_stream'])
cmd(['createdb','-p','5433','-O','immich','immich_stream'])
with path.open('rb') as f:cmd(['pg_restore','-p','5433','--exit-on-error','--single-transaction','--no-owner','--role=immich','-d','immich_stream'],stdin=f)
print(json.dumps({'copied':True,'bytes':path.stat().st_size,'rollbackDump':str(path)}))
'''


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('phase',choices=['stage','status'])
    args=parser.parse_args()
    clients={};gateways=[]
    try:
        for node in ('primary','backup'):
            clients[node],g=connection(node)
            if g:gateways.append(g)
        state=control()
        if args.phase=='status':
            for node,c in clients.items():
                print(node,remote(c,"import sys,json;sys.path.insert(0,'/usr/local/lib/immich-ha');from pg_stream import status;print(json.dumps(status()))"))
            return
        if state.get('pending') or (state.get('operation') or {}).get('status') not in (None,'succeeded','failed'):
            raise RuntimeError('Finish the current HA operation before staging')
        source=state['owner'];target='backup' if source=='primary' else 'primary'
        info={n:remote(c,PREFLIGHT) for n,c in clients.items()}
        if any(i['existingHa'] for i in info.values()):raise RuntimeError('An HA cluster/config already exists; inspect instead of staging again')
        if any(i['free']<3*1024**3 for i in info.values()):raise RuntimeError('Need 3 GiB root space for staging')
        if info[source]['version']!=info[target]['version']:raise RuntimeError('PostgreSQL versions differ')
        if not info[source]['version'].startswith('16.'):raise RuntimeError('This migration requires PostgreSQL 16 on both nodes')
        # Read the existing password only inside this process; never print/persist it locally.
        password=remote(clients[source],"import json,shlex;from pathlib import Path\nv={}\nfor l in Path('/opt/immich/.env').read_text().splitlines():\n if '=' in l and not l.lstrip().startswith('#'):\n  k,_,x=l.partition('=');v[k]=shlex.split(x,comments=True)[0] if shlex.split(x,comments=True) else ''\nassert v.get('DB_USERNAME')=='immich'\nprint(json.dumps(v['DB_PASSWORD']))")
        repl=__import__('secrets').token_hex(32)
        for node in (source,target):
            upload_module(clients[node],ROOT/'node/pg_stream.py','/usr/local/lib/immich-ha/pg_stream.py')
            config={'enabled':False,'node':node,'peer':target if node==source else source,
                'peer_ip':info[target if node==source else source]['ip'],'local_ip':info[node]['ip'],
                'port':5433,'database':'immich_stream','source':node==source,
                'replication_password':repl,'role_password':repl,'app_password':password,'app_superuser':info[source]['superuser']}
            print(node,remote(clients[node],STAGE,config,timeout=1500))
            if node==source:print('staged-copy',remote(clients[node],COPY,{'id':uuid.uuid4().hex[:12]},timeout=1500))
        # A disposable database proves actual commit/replay, independently of Immich.
        probe='ha_replication_probe_'+uuid.uuid4().hex[:8]
        script="import sys,json;sys.path.insert(0,'/usr/local/lib/immich-ha');from pg_stream import pg\npg('CREATE DATABASE '+INPUT['db']+';');pg('CREATE TABLE probe(value text); INSERT INTO probe VALUES (\\'wal-ok\\');',INPUT['db']);print(json.dumps(True))"
        remote(clients[source],script,{'db':probe})
        check="import sys,json,time;sys.path.insert(0,'/usr/local/lib/immich-ha');from pg_stream import pg\nfor i in range(20):\n try:\n  ok=pg('SELECT value FROM probe;',INPUT['db'])=='wal-ok'\n  if ok:break\n except Exception:ok=False\n time.sleep(1)\nassert ok,'WAL replay probe failed'\nprint(json.dumps({'commitReplayed':True,'standbyReadOnly':pg('SHOW transaction_read_only;')=='on'}))"
        print('WAL-TEST',remote(clients[target],check,{'db':probe},timeout=30))
        remote(clients[source],"import sys,json;sys.path.insert(0,'/usr/local/lib/immich-ha');from pg_stream import pg;pg('DROP DATABASE '+INPUT['db']+';');print(json.dumps(True))",{'db':probe})
        receipt={'source':source,'target':target,'epoch':state['epoch'],'stageId':uuid.uuid4().hex,'walTest':True}
        (ROOT/'generated/streaming-stage.json').write_text(json.dumps(receipt,indent=2))
        print('STAGE OK; production still uses PostgreSQL 16/main on port 5432')
    finally:
        for c in clients.values():c.close()
        for g in gateways:g.close()


if __name__=='__main__':main()
