"""Apply only the common witness Funnel setting; never grants or app services."""
import copy
import json
import subprocess
import urllib.request
from pathlib import Path

from funnel_setup import validate_existing, validate_after


def current(config, settings):
    address=f"{config['dnsName']}:{config['port']}"
    validate_existing(settings,config['dnsName'],config['port'],config['gatewayPort'])
    enabled=(settings.get('AllowFunnel') or {}).get(address) is True
    if enabled:validate_after(settings,settings,config['dnsName'],config['port'],config['gatewayPort'])
    return enabled


def unrelated(config, settings):
    result=copy.deepcopy(settings)
    for key,item in [('TCP',str(config['port'])),('Web',f"{config['dnsName']}:{config['port']}"),('AllowFunnel',f"{config['dnsName']}:{config['port']}")]:
        if isinstance(result.get(key),dict):
            result[key].pop(item,None)
            if not result[key]:result.pop(key)
    return result


def apply(config, enabled, run=subprocess.check_output):
    def inspect():return json.loads(run(['tailscale','serve','status','--json'],text=True,timeout=15)) or {}
    before=inspect()
    if current(config,before)==enabled:return
    args=['tailscale','funnel',f"--https={config['port']}"]
    args+=['--bg','--yes',f"http://127.0.0.1:{config['gatewayPort']}"] if enabled else ['off']
    run(args,text=True,stderr=subprocess.STDOUT,timeout=30)
    after=inspect()
    if current(config,after)!=enabled or unrelated(config,before)!=unrelated(config,after):
        raise RuntimeError('Funnel setting not confirmed; inspect existing Serve configuration')
    if enabled:validate_after(before,after,config['dnsName'],config['port'],config['gatewayPort'])


def main():
    config=json.loads(Path('/etc/immich-ha/witness-funnel-control.json').read_text())
    def inspect():return json.loads(subprocess.check_output(['tailscale','serve','status','--json'],text=True,timeout=15)) or {}
    def exchange(report):
        request=urllib.request.Request(config['controller']+'/__ha/funnel-agent',data=json.dumps(report).encode(),
            headers={'Authorization':'Bearer '+config['token'],'Content-Type':'application/json','User-Agent':'ImmichHA-FunnelControl/1.0'})
        with urllib.request.urlopen(request,timeout=15) as response:return json.load(response)
    # A coordinator outage never toggles Funnel: the existing route stays in place.
    state=exchange({'enabled':current(config,inspect()),'revision':None})
    try:
        apply(config,state['enabled'])
        exchange({'enabled':current(config,inspect()),'revision':state['revision']})
    except Exception:
        exchange({'enabled':None,'revision':state['revision'],'error':'Không áp dụng được Funnel; kiểm tra dịch vụ witness'})
        raise


if __name__=='__main__':main()
