"""Outbound HTTPS init/join worker. Executes only the packaged action catalog."""
import json
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
sys.path.insert(0,str(Path(__file__).resolve().parent))
from node.cluster_bootstrap import APP,atomic,execute


def exchange(controller,path,body,token=None):
    headers={'Content-Type':'application/json','User-Agent':'ImmichHA-Setup/1.0'}
    if token:headers['Authorization']='Bearer '+token
    request=urllib.request.Request(controller.rstrip('/')+path,data=json.dumps(body).encode(),headers=headers)
    try:
        with urllib.request.urlopen(request,timeout=30) as response:return json.load(response)
    except urllib.error.HTTPError as error:
        raise RuntimeError('Setup API returned HTTP '+str(error.code)+'; verify URL, enrollment and installer version') from None


def run_local(command,local):
    if not re.fullmatch('[a-f0-9-]{36}-[0-9]+-[0-9]+',command.get('id','')):raise RuntimeError('Invalid command receipt ID')
    directory=APP/'.install/receipts';directory.mkdir(mode=0o700,parents=True,exist_ok=True)
    receipt=directory/(command['id']+'.json');started=directory/(command['id']+'.started')
    if receipt.exists():return json.loads(receipt.read_text())
    result={'id':command['id']}
    if started.exists():
        result.update(status='failed',error='Interrupted local action; retained all files. Inspect before retrying.')
    else:
        atomic(started,'started\n')
        try:
            if command.get('action','').startswith('witness-'):
                from witness_bootstrap import execute as extension_execute
                value=extension_execute(command,local)
            else:value=execute(command,local)
            result.update(status='succeeded',result=value)
        except Exception as error:
            if command.get('action','').startswith('witness-'):
                import traceback
                atomic(directory/(command['id']+'.error'),traceback.format_exc())
            # Bootstrap action errors are bounded; redact every credential in the request.
            message=str(error)
            def redact(value):
                nonlocal message
                if isinstance(value,dict):
                    for key,item in value.items():
                        if re.search('token|password|secret',key,re.I) and isinstance(item,str):message=message.replace(item,'<redacted>')
                        else:redact(item)
            redact(command)
            if command.get('action','').startswith('witness-'):message='Witness step failed; inspect the local private checkpoint and journal; files retained'
            result.update(status='failed',error=message[:300])
    atomic(receipt,json.dumps(result))
    return result


def main():
    local=json.loads((APP/'.install/node.json').read_text())
    receipt=None
    base_complete=local['node']=='witness'
    while True:
        try:
            response=exchange(local['controller'],'/__ha/witness-node' if base_complete else '/__ha/setup-node',{'node':local['node'],'token':local['token'],'receipt':receipt})
            if not base_complete and response.get('complete'):
                if local['node']=='primary' and (APP/'project.json').is_file():
                    project=json.loads((APP/'project.json').read_text())
                    for node,info in response.get('nodes',{}).items():
                        if info.get('ip'):project['nodes'][node]['ip']=info['ip']
                        if info.get('tailscaleIp'):project['nodes'][node]['tailscaleIp']=info['tailscaleIp']
                    atomic(APP/'project.json',json.dumps(project,ensure_ascii=False,indent=2)+'\n')
                print('HA setup completed; waiting for optional witness enrollment.',flush=True)
                base_complete=True;receipt=None
                continue
            if base_complete and local['node']=='witness' and response.get('funnel'):
                from witness_bootstrap import reconcile_funnel
                report=reconcile_funnel(response['funnel'])
                if report:exchange(local['controller'],'/__ha/witness-node',{'node':local['node'],'token':local['token'],'funnel':report})
            if response.get('error'):print('Setup paused: '+response['error'],flush=True)
            command=response.get('command')
            if command:
                print('Local step: '+command['action'],flush=True)
                receipt=run_local(command,local)
                continue
        except (OSError,RuntimeError,ValueError):
            print('Setup connection unavailable; preserving local state and retrying.',flush=True)
        extension=response.get('extension') if 'response' in locals() else None
        time.sleep((15 if local['node']=='witness' else 60) if base_complete and (not extension or extension.get('status')=='completed') else 10)


if __name__=='__main__':main()
