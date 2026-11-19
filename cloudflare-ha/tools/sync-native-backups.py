"""Allow Immich SQL backups in the existing role-aware library sync.

Keep PGDATA archives, custom HA dumps and recovery archives excluded. No new
Syncthing folder or fixed sender: the existing HA direction follows ownership.
"""
import json
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import deployment as m

PREFIX = [
    '/backups/pg-stream', '/backups/postgres', '/backups/recovery', '/backups/.immich',
    *['!/backups/'+prefix+'immich-db-backup-*'+suffix
      for prefix in ('','uploaded-','pre-restore-') for suffix in ('.sql.gz','.sql')],
    '/backups/*',
]


def rules(existing):
    old = {'/backups','/backups/','/backups/*','/backups/**','backups','backups/'}
    return PREFIX + [line for line in existing if line.strip() not in old and line not in PREFIX]


def main():
    state=m.control();owner=state['owner'];epoch=state['epoch']
    assert state['ready'] and not state.get('pending')
    assert not state.get('operation') or state['operation']['status'] in ('succeeded','failed')
    clients={};gateways=[];before={}
    try:
        for node in ('primary','backup'):
            clients[node],g=m.connection(node)
            if g:gateways.append(g)
            code="""import sys,json
from pathlib import Path
sys.path.insert(0,'/usr/local/lib/immich-ha')
from ha_runtime import Syncthing
from ha_agent import syncthing_endpoint
c=json.loads(Path('/etc/immich-ha/config.json').read_text());s=Syncthing(c,syncthing_endpoint)
f=s.folder('immich-library');assert not f['paused']
assert f['path']=='/mnt/immich-storage'
print(json.dumps({'ignores':s.api('/rest/db/ignores?folder=immich-library').get('ignore') or [],'type':f['type']}))
"""
            before[node]=m.remote(clients[node],code)
            assert before[node]['type']==('sendonly' if node==owner else 'receiveonly')
        (m.ROOT/'generated/native-backup-sync-before.json').write_text(json.dumps(before,indent=2))
        for node in (('backup' if owner=='primary' else 'primary'),owner):
            live=m.control();assert live['owner']==owner and live['epoch']==epoch and not live.get('pending')
            code="""import sys,json
from pathlib import Path
sys.path.insert(0,'/usr/local/lib/immich-ha')
from ha_runtime import Syncthing
from ha_agent import syncthing_endpoint
c=json.loads(Path('/etc/immich-ha/config.json').read_text());s=Syncthing(c,syncthing_endpoint)
s.api('/rest/db/ignores?folder=immich-library',{'ignore':INPUT['rules']},'POST',timeout=180)
assert s.api('/rest/db/ignores?folder=immich-library')['ignore']==INPUT['rules']
print(json.dumps({'nativeSqlSync':True}))
"""
            print(node,m.remote(clients[node],code,{'rules':rules(before[node]['ignores'])},timeout=200),flush=True)
        # A subdirectory scan discovers just the newly allowed backup files.
        for node in (owner, 'backup' if owner=='primary' else 'primary'):
            code="""import sys,json
from pathlib import Path
sys.path.insert(0,'/usr/local/lib/immich-ha')
from ha_runtime import Syncthing
from ha_agent import syncthing_endpoint
c=json.loads(Path('/etc/immich-ha/config.json').read_text());s=Syncthing(c,syncthing_endpoint)
s.api('/rest/db/scan?folder=immich-library&sub=backups',{},'POST',timeout=180)
print(json.dumps({'backupScan':True}))
"""
            print(node,m.remote(clients[node],code,timeout=200),flush=True)
    finally:
        for c in clients.values():c.close()
        for g in gateways:g.close()


if __name__=='__main__':main()
