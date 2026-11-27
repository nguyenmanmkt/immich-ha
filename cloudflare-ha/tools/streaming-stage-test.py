"""Exercise physical promote/rejoin twice, only when streaming is disabled.

Production apps and 16/main are untouched. No HTTP role/lease changes.
"""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import deployment as m
PREFIX="""import sys,json
sys.path.insert(0,'/usr/local/lib/immich-ha')
from pg_stream import configuration,ctl,status,pg,clone
assert configuration(enabled=False)['enabled'] is False, 'Never test an enabled production cluster'
"""
def main():
    clients={};gateways=[]
    try:
        state=m.control()
        source=state['owner'];target='backup' if source=='primary' else 'primary'
        for node in (source,target):
            clients[node],g=m.connection(node)
            if g:gateways.append(g)
            m.upload_module(clients[node],m.ROOT/'node/pg_stream.py','/usr/local/lib/immich-ha/pg_stream.py')
            for name in ('quorum_lock.py','quorum_authority.py'):
                m.upload_module(clients[node],m.ROOT/'node'/name,'/usr/local/lib/immich-ha/'+name)
        for old,new in [(source,target),(target,source)]:
            print('STOP-STAGED',old,m.remote(clients[old],PREFIX+"ctl('stop');print(json.dumps(True))"),flush=True)
            print('PROMOTE-STAGED',new,m.remote(clients[new],PREFIX+"assert pg('SELECT pg_promote(true,30);',timeout=40)=='t';print(json.dumps(status()))",timeout=60),flush=True)
            print('REJOIN-STAGED',old,m.remote(clients[old],PREFIX+"clone(configuration(enabled=False));print(json.dumps(status()))",timeout=1500),flush=True)
        print('BIDIRECTIONAL STAGE TEST OK',flush=True)
    finally:
        for c in clients.values():c.close()
        for g in gateways:g.close()


if __name__ == '__main__':
    main()
