#!/usr/bin/env python3
"""Cut over the serving database after staging; never deletes 16/main.

Before apps can use the new cluster, errors restore saved runtime/agent files.
After apps have been enabled, errors leave streaming enabled and automation OFF:
returning to an older database would lose accepted writes.
"""
import json
from pathlib import Path
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import deployment as m
from streaming_setup import COPY

SAVE = '''
import json,os,shutil,subprocess
from pathlib import Path
from datetime import datetime
base=Path('/etc/immich-ha/streaming-rollback')/INPUT['id'];base.mkdir(parents=True,mode=0o700)
files={
 'agent-exec':'/usr/local/sbin/immich-ha-agent',
 'agent-module':'/usr/local/lib/immich-ha/ha_agent.py',
 'runtime-module':'/usr/local/lib/immich-ha/ha_runtime.py',
 'app-run':'/usr/local/sbin/immich-ha-app-run',
 'streaming.json':'/etc/immich-ha/streaming.json',
 **{n:'/var/lib/immich-ha/runtime/'+n for n in ['current-db','mode','seed.json']}}
owners={}
for name,path in files.items():
 if Path(path).exists():
  stat=Path(path).stat();owners[name]=[stat.st_uid,stat.st_gid]
  shutil.copy2(path,base/name)
(base/'files.json').write_text(json.dumps(files))
(base/'owners.json').write_text(json.dumps(owners))
units=['immich-ha-agent.service','immich-ha-dump.timer','immich-ha-refresh.timer',
 'immich-ha-dump.service','immich-ha-refresh.service']
subprocess.run(['systemctl','stop',*units],check=True,capture_output=True,timeout=180)
print(json.dumps({'saved':str(base)}))
'''

FREEZE = '''
import subprocess,json
from pathlib import Path
subprocess.run(['systemctl','stop','immich-ha-app.service','immich-ml.service'],check=True,capture_output=True,timeout=120)
p=Path('/var/lib/immich-ha/runtime/mode');p.write_text('frozen\\n')
print(json.dumps({'appStopped':True}))
'''

ENABLE = '''
import json,os,pwd,sys,subprocess,time
from pathlib import Path
sys.path.insert(0,'/usr/local/lib/immich-ha')
from pg_stream import status
c=Path('/etc/immich-ha/streaming.json');v=json.loads(c.read_text());v['enabled']=True
assert v['node']==INPUT['node']
state=status();assert state['inRecovery']==(not INPUT['source'])
c.write_text(json.dumps(v));os.chmod(c,0o600)
r=Path('/var/lib/immich-ha/runtime');g=pwd.getpwnam('immich').pw_gid
def write(n,text):
 p=r/n;p.write_text(text+'\\n');os.chmod(p,0o640);os.chown(p,0,g)
write('current-db','immich_stream')
write('mode','active' if INPUT['source'] else 'standby')
write('seed.json',json.dumps({'source':INPUT['owner'],'sourceEpoch':INPUT['epoch'],
 'systemId':state['systemId'],'appliedAt':int(time.time()*1000),'mode':'streaming'}))
write('stream-last-connected',str(int(time.time())))
os.replace(r/'streaming-app.env.staged',r/'streaming-app.env')
subprocess.run(['systemctl','enable','postgresql@16-ha.service'],check=True,capture_output=True)
print(json.dumps({'enabled':True,'systemId':state['systemId']}))
'''

ROLLBACK = '''
import json,os,shutil,subprocess
from pathlib import Path
base=Path('/etc/immich-ha/streaming-rollback')/INPUT['id']
files=json.loads((base/'files.json').read_text())
owners=json.loads((base/'owners.json').read_text())
subprocess.run(['systemctl','stop','immich-ha-agent.service','immich-ha-app.service','immich-ml.service'],capture_output=True,timeout=120)
for name,path in files.items():
 if (base/name).exists():
  shutil.copy2(base/name,path);os.chown(path,*owners[name])
env=Path('/var/lib/immich-ha/runtime/streaming-app.env')
if env.exists():os.replace(env,env.with_name('streaming-app.env.staged'))
units=['immich-ha-agent.service','immich-ha-refresh.timer']
if not Path('/etc/immich-ha/disable-scheduled-dump').exists():units.append('immich-ha-dump.timer')
subprocess.run(['systemctl','start',*units],check=True,capture_output=True)
print(json.dumps({'rolledBack':True}))
'''


def main():
    receipt=json.loads((m.ROOT/'generated/streaming-stage.json').read_text())
    state=m.control();source=state['owner'];target='backup' if source=='primary' else 'primary'
    if not receipt['walTest'] or receipt['source']!=source or receipt['epoch']!=state['epoch']:
        raise RuntimeError('The staging receipt is stale; inspect current source before proceeding')
    if state.get('pending') or (state.get('operation') or {}).get('status') not in (None,'succeeded','failed'):
        raise RuntimeError('A role operation is still running')
    clients={};gateways=[];saved=[];started=False
    identity=__import__('datetime').datetime.now().strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:6]
    (m.ROOT/'generated/streaming-before-cutover.json').write_text(json.dumps(state,indent=2))
    try:
        for node in (source,target):
            clients[node],g=m.connection(node)
            if g:gateways.append(g)
            value=m.remote(clients[node],"import json;from pathlib import Path;print(json.dumps(json.loads(Path('/etc/immich-ha/streaming.json').read_text())))")
            if value['enabled']:raise RuntimeError('Already enabled; refusing to overwrite production')
        # Disable all automatic role/reseed jobs during the brief mixed-version window.
        pref=state.get('preference',{})
        if pref.get('node'):m.control('set-preference',target=pref['node'],enabled=False)
        m.control('set-recovery',enabled=False);m.control('set-auto',enabled=False)
        state2=m.control()
        if state2['owner']!=source or state2['epoch']!=state['epoch']:
            raise RuntimeError('Owner changed; cutover stopped before freezing')
        for node,c in clients.items():
            print(node,m.remote(c,SAVE,{'id':identity},timeout=240),flush=True);saved.append(node)
        for node,c in clients.items():
            print('FREEZE',node,m.remote(c,FREEZE,timeout=150),flush=True)
        print('FINAL-COPY',m.remote(clients[source],COPY,{'id':identity},timeout=1500),flush=True)
        barrier=m.remote(clients[source],"import sys,json;sys.path.insert(0,'/usr/local/lib/immich-ha');from pg_stream import pg,status;print(json.dumps({'lsn':pg('SELECT pg_current_wal_flush_lsn();'),'systemId':status()['systemId']}))")
        wait='''
import sys,json,time
sys.path.insert(0,'/usr/local/lib/immich-ha')
from pg_stream import pg,status
assert __import__('re').fullmatch('[0-9A-F]+/[0-9A-F]+',INPUT['lsn'])
for i in range(90):
 value=status()
 assert value['inRecovery'] and value['systemId']==INPUT['systemId']
 if pg("SELECT pg_last_wal_replay_lsn()>='"+INPUT['lsn']+"'::pg_lsn;")=='t':break
 time.sleep(1)
else:raise RuntimeError('Final WAL did not reach standby')
print(json.dumps({'finalWalReplayed':True}))
'''
        print('FINAL-WAL',m.remote(clients[target],wait,barrier,timeout=110),flush=True)
        count='''
import sys,json,subprocess
sys.path.insert(0,'/usr/local/lib/immich-ha');from pg_stream import pg
q='SELECT count(*) FROM asset;'
old=subprocess.check_output(['runuser','-u','postgres','--','psql','-XAt','-p','5432','-d',INPUT['db'],'-c',q],text=True).strip() if INPUT['source'] else None
new=pg(q,'immich_stream')
assert old is None or old==new,'Database asset count mismatch'
print(json.dumps({'assetRows':int(new)}))
'''
        counts={}
        for node,c in clients.items():
            db=m.remote(c,"import json;from pathlib import Path;print(json.dumps(Path('/var/lib/immich-ha/runtime/current-db').read_text().strip()))")
            counts[node]=m.remote(c,count,{'db':db,'source':node==source})['assetRows']
        if counts[source]!=counts[target]:raise RuntimeError('Replica count differs from frozen source')
        print('COUNTS',counts,flush=True)
        for node,c in clients.items():
            for local,path,mode in [
                ('pg_stream.py','/usr/local/lib/immich-ha/pg_stream.py',0o600),
                ('quorum_lock.py','/usr/local/lib/immich-ha/quorum_lock.py',0o600),
                ('quorum_authority.py','/usr/local/lib/immich-ha/quorum_authority.py',0o600),
                ('ha_runtime.py','/usr/local/lib/immich-ha/ha_runtime.py',0o600),
                ('agent.py','/usr/local/lib/immich-ha/ha_agent.py',0o600),
                ('agent.py','/usr/local/sbin/immich-ha-agent',0o700),
                ('app-run-v2','/usr/local/sbin/immich-ha-app-run',0o750)]:
                m.upload_module(c,m.ROOT/'node'/local,path,mode)
            # Preserve app-run's immich group after replacing the executable.
            m.remote(c,"import os,pwd,json,subprocess;os.chown('/usr/local/sbin/immich-ha-app-run',0,pwd.getpwnam('immich').pw_gid);subprocess.run(['bash','-n','/usr/local/sbin/immich-ha-app-run'],check=True);print(json.dumps(True))")
            print(node,m.remote(c,ENABLE,{'node':node,'source':node==source,'owner':source,'epoch':state['epoch']}),flush=True)
        # From here onward, rollback to 16/main is deliberately NOT automatic.
        # The new app may accept writes as soon as its grant is acknowledged.
        started=True
        for node,c in clients.items():
            m.remote(c,"import subprocess,json;from pathlib import Path;units=['immich-ha-agent.service','immich-ha-refresh.timer'];units+=[] if Path('/etc/immich-ha/disable-scheduled-dump').exists() else ['immich-ha-dump.timer'];subprocess.run(['systemctl','start',*units],check=True,capture_output=True);print(json.dumps(True))")
        verified=False
        for i in range(24):
            live=m.control()
            agents=live.get('agents',{})
            if live['owner']==source and live['epoch']==state['epoch'] and live['ready'] and live.get('standbyEligible') and \
                    all(agents.get(n,{}).get('metrics',{}).get('postgresStreaming') for n in clients):
                verified=True;break
            time.sleep(5)
        if not verified:raise RuntimeError('Streaming enabled; verify health before restoring automation')
        m.control('set-recovery',enabled=state.get('recovery',{}).get('enabled',False))
        if pref.get('node'):m.control('set-preference',target=pref['node'],enabled=pref.get('enabled',False))
        m.control('set-auto',enabled=state.get('autoRequested',state.get('auto',False)))
        (m.ROOT/'generated/streaming-cutover.json').write_text(json.dumps({'id':identity,'source':source,'epoch':state['epoch'],'counts':counts,'barrier':barrier,'verified':True},indent=2))
        print('CUTOVER VERIFIED; scheduled-backup policy preserved; old PostgreSQL 16/main retained',flush=True)
    except Exception:
        if not started:
            for node in saved:
                print('ROLLBACK',node,m.remote(clients[node],ROLLBACK,{'id':identity},timeout=180),flush=True)
        else:print('STREAMING REMAINS ENABLED. Automation is OFF until health is verified.',flush=True)
        raise
    finally:
        for c in clients.values():c.close()
        for g in gateways:g.close()


if __name__=='__main__':main()
