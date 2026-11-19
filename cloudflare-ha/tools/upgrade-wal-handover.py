"""Roll out WAL+manifest handover without switching the serving node.

Requires an idle, healthy streaming system. Saves modules and automation settings,
updates both agents before enabling new Worker commands, restores automation only
after both runtimes advertise the capability and health has been confirmed.
"""
import datetime as dt
import json
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import deployment as m


def idle(s):
    return not s.get('pending') and (not s.get('operation') or s['operation']['status'] in ('succeeded','failed')) and not (s.get('operation') or {}).get('maintenance')


def main():
    state=m.control()
    assert idle(state) and state['ready'] and state['standbyEligible'], 'Finish the current operation before upgrading'
    assert all(a.get('metrics',{}).get('postgresStreaming') for a in state['agents'].values())
    stamp=dt.datetime.now().strftime('%Y%m%d-%H%M%S')
    (m.ROOT/'generated'/('before-wal-handover-'+stamp+'.json')).write_text(json.dumps(state,indent=2))
    clients={};gateways=[]
    try:
        for n in ('primary','backup'):
            clients[n],g=m.connection(n)
            if g:gateways.append(g)
        p=state.get('preference',{})
        if p.get('node'):m.control('set-preference',target=p['node'],enabled=False)
        m.control('set-recovery',enabled=False);m.control('set-auto',enabled=False)
        now=m.control();assert idle(now) and now['owner']==state['owner'] and now['epoch']==state['epoch']
        for node in ('backup','primary'):
            c=clients[node]
            code="""import json,shutil,sys
from pathlib import Path
sys.path.insert(0,'/usr/local/lib/immich-ha')
from pg_stream import configuration,status
assert configuration()['enabled'];assert status()['systemId']
save=Path('/etc/immich-ha')/('wal-handover-'+INPUT['stamp']);save.mkdir(mode=0o700)
for name in ['ha_runtime.py','pg_stream.py','ha_agent.py']:shutil.copy2('/usr/local/lib/immich-ha/'+name,save/name)
shutil.copy2('/usr/local/sbin/immich-ha-agent',save/'agent-exec')
print(json.dumps({'saved':str(save)}))
"""
            print(node,m.remote(c,code,{'stamp':stamp}),flush=True)
            for name in ('pg_stream.py','ha_runtime.py'):
                m.upload_module(c,m.ROOT/'node'/name,'/usr/local/lib/immich-ha/'+name)
            m.upload_module(c,m.ROOT/'node/agent.py','/usr/local/lib/immich-ha/ha_agent.py')
            m.upload_module(c,m.ROOT/'node/agent.py','/usr/local/sbin/immich-ha-agent',0o700)
            code="""import json,py_compile,subprocess
for name in ['ha_runtime.py','pg_stream.py','ha_agent.py']:py_compile.compile('/usr/local/lib/immich-ha/'+name,doraise=True)
subprocess.run(['systemctl','restart','immich-ha-agent.service'],check=True,capture_output=True,timeout=30)
print(json.dumps({'agentReloaded':True}))
"""
            print(node,m.remote(c,code),flush=True)
        for i in range(24):
            live=m.control()
            assert live['owner']==state['owner'] and live['epoch']==state['epoch'], 'Owner changed during rollout'
            if live['ready'] and live['standbyEligible'] and live['primaryHealthy'] and live['backupHealthy'] and \
                    all(a.get('metrics',{}).get('postgresStreaming',{}).get('handoverVersion',0)>=2 and a.get('metrics',{}).get('postgresStreaming',{}).get('fastHandoverVersion')==1 for a in live['agents'].values()):break
            time.sleep(5)
        else:raise RuntimeError('Runtime readiness not confirmed; automation remains OFF')
        subprocess.run([sys.executable,str(m.ROOT/'deploy.py'),'--update'],cwd=m.ROOT,check=True)
        live=m.control();assert idle(live) and live['ready'] and live['standbyEligible']
        m.control('set-recovery',enabled=state.get('recovery',{}).get('enabled',False))
        if p.get('node'):m.control('set-preference',target=p['node'],enabled=p.get('enabled',False))
        m.control('set-auto',enabled=state.get('autoRequested',state.get('auto',False)))
        print('WAL HANDOVER INSTALLED; owner unchanged; automation restored',flush=True)
    finally:
        for c in clients.values():c.close()
        for g in gateways:g.close()


if __name__=='__main__':main()
