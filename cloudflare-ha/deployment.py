"""Shared deployment transport. No connections or config reads at import time."""
import json
from pathlib import Path
import urllib.request

from local_env import load_env
from project_config import load_project
from ssh_nodes import connect, execute, settings

ROOT = Path(__file__).resolve().parent


def jump_settings(env):
    """Generic optional jump host, with legacy env aliases for existing installs."""
    if env.get('SSH_JUMP_HOST'):
        values = env
        known = None
    elif env.get('SSH_ASUS_PVE_HOST'):
        # Compatibility for old private deployments; no private Python helper.
        values = {'SSH_JUMP_'+key: env.get('SSH_ASUS_PVE_'+key) or env.get('SSH_BACKUP_'+key, '')
                  for key in ('HOST','PORT','USER','PASSWORD','KEY_PATH','KEY_PASSPHRASE')}
        known = ROOT/'generated/asus-pve-known_hosts'
    else:
        return None
    options = settings('jump', values, {'nodes': {'jump': {'ip': values['SSH_JUMP_HOST']}}})
    return options, known if known and known.is_file() else None


def connection(node):
    env = load_env()
    options = settings(node, env, load_project())
    try:
        return connect(options), None
    except Exception:
        jump = jump_settings(env)
        if jump is None:
            raise
    gateway = connect(jump[0], extra_known_hosts=jump[1])
    try:
        options['sock'] = gateway.get_transport().open_channel('direct-tcpip',
            (options['hostname'], options['port']), ('127.0.0.1', 0), timeout=10)
        return connect(options), gateway
    except Exception:
        gateway.close()
        raise


def remote(client, script, data=None, timeout=180):
    code, out, err = execute(client, 'python3 -', 'INPUT = '+repr(data)+'\n'+script, timeout=timeout)
    if code:
        raise RuntimeError((out+'\n'+err)[-2500:])
    return json.loads(out) if out.strip() else None


def control(action='status', **extra):
    private = ROOT/'generated'
    config = json.loads((private/'primary/config.json').read_text(encoding='utf-8'))
    token = json.loads((private/'worker-secrets.json').read_text(encoding='utf-8'))['ADMIN_TOKEN']
    path = '/__ha/status' if action == 'status' else '/__ha/admin'
    body = None if action == 'status' else json.dumps({'action':action,'actor':'deployment',**extra}).encode()
    request = urllib.request.Request(config['controller_url']+path, data=body, headers={
        'Authorization':'Bearer '+token, 'Content-Type':'application/json','User-Agent':'ImmichHA-Admin/1.0'})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


def upload_module(client, local, target, mode=0o600):
    with client.open_sftp() as sftp:
        temporary = target+'.stream-stage'
        sftp.put(str(local), temporary)
        sftp.chmod(temporary, mode)
        sftp.posix_rename(temporary, target)
