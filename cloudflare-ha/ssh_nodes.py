#!/usr/bin/env python3
"""SSH checks/upgrades using local .env credentials; never put secrets in argv."""
import argparse
import io
import json
from pathlib import Path
import re
import shlex
import sys
import uuid
import zipfile

from local_env import load_env
from project_config import load_project

ROOT = Path(__file__).resolve().parent

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(errors="backslashreplace")


def settings(node, env, project):
    prefix = "SSH_" + node.upper() + "_"
    host = env.get(prefix + "HOST") or project["nodes"][node]["ip"]
    user = env.get(prefix + "USER") or "root"
    port = int(env.get(prefix + "PORT") or 22)
    if not re.fullmatch(r"[a-zA-Z0-9.:-]+", host) or not 1 <= port <= 65535:
        raise ValueError("Invalid SSH host/port")
    if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_-]*", user):
        raise ValueError("Invalid SSH username")
    key = env.get(prefix + "KEY_PATH") or None
    if key:
        path = Path(key).expanduser().resolve(strict=True)
        if path.suffix == ".pub":
            private = path.with_suffix("")
            if not private.is_file():
                raise ValueError("SSH KEY_PATH points to a public key; matching private key is missing")
            path = private
        key = str(path)
    return dict(hostname=host, port=port, username=user,
                password=env.get(prefix + "PASSWORD") or None, key_filename=key,
                passphrase=env.get(prefix + "KEY_PASSPHRASE") or None)


def connect(options, *, extra_known_hosts=None):
    import paramiko
    client = paramiko.SSHClient()
    known = Path.home() / ".ssh/known_hosts"
    if known.is_file():
        client.load_system_host_keys(str(known))
    tailnet_known = ROOT / "generated/tailscale-known_hosts"
    if tailnet_known.is_file():
        client.load_host_keys(str(tailnet_known))
    if extra_known_hosts is not None:
        client.load_host_keys(str(extra_known_hosts))
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    try:
        client.connect(**options, timeout=8, banner_timeout=10, auth_timeout=15,
                       allow_agent=not bool(options["password"]),
                       look_for_keys=not bool(options["password"]))
        return client
    except Exception:
        client.close()
        raise


def execute(client, command, payload=None, timeout=180):
    stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
    if payload is not None:
        stdin.write(payload)
        stdin.flush()
    stdin.channel.shutdown_write()
    output = stdout.read(512 * 1024).decode(errors="replace")
    error = stderr.read(128 * 1024).decode(errors="replace")
    status = stdout.channel.recv_exit_status()
    return status, output, error


def inspect_node(client):
    code = '''import json,os,subprocess
from pathlib import Path
c=json.loads(Path('/etc/immich-ha/config.json').read_text())
mounted=os.path.ismount('/mnt/immich-storage')
free=os.statvfs('/mnt/immich-storage') if mounted else None
print(json.dumps({'uid':os.getuid(),'node':c['node'],'runtimeVersion':c.get('runtime_version',1),
 'mediaMounted':mounted,'mediaFreeBytes':free.f_bavail*free.f_frsize if free else None,
 'agent':subprocess.run(['systemctl','is-active','immich-ha-agent.service'],capture_output=True,text=True).stdout.strip()}))
'''
    status, out, err = execute(client, "python3 -", code)
    if status:
        raise RuntimeError("Remote preflight failed (check installed config/service)")
    return json.loads(out)


def upgrade(client, node):
    info = inspect_node(client)
    if info["uid"] != 0 or info["node"] != node or not info["mediaMounted"]:
        raise RuntimeError("Wrong LXC identity, missing root login or missing media mount")
    if info["mediaFreeBytes"] < 1024 ** 3:
        raise RuntimeError("Less than 1 GiB free on media volume; upgrade stopped")
    bundle = ROOT / "generated" / (node + "-v2.zip")
    expected = {"config.json", "agent.py", "ha_runtime.py", "pg_stream.py", "immich_upgrade.py", "build-immich-release.sh", "maintenance-v2.py", "app-run-v2", "upgrade-v2.sh"}
    with zipfile.ZipFile(bundle) as archive:
        if set(archive.namelist()) != expected:
            raise RuntimeError("Unexpected upgrade bundle files")
        files = {name: archive.read(name) for name in expected}
    if json.loads(files["config.json"])["node"] != node:
        raise RuntimeError("Upgrade bundle belongs to another node")
    stage = "/root/immich-ha-upgrade-" + node + "-" + uuid.uuid4().hex[:12]
    with client.open_sftp() as sftp:
        sftp.mkdir(stage, mode=0o700)
        for name, content in files.items():
            remote = stage + "/" + name
            with sftp.open(remote, "wb") as target:
                sftp.chmod(remote, 0o600)
                target.write(content)
    print("Uploaded private bundle to", stage, flush=True)
    # The installer independently checks controller role, auto=OFF, local IP and
    # existing node keys before changing services. The path is generated here.
    return execute(client, "bash -- " + shlex.quote(stage + "/upgrade-v2.sh"), timeout=600)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("node", choices=("primary", "backup"))
    parser.add_argument("--upgrade", action="store_true", help="Upload and install the matching prepared v2 bundle")
    args = parser.parse_args()
    env = load_env()
    options = settings(args.node, env, load_project())
    secrets = [value for key, value in env.items() if value and any(word in key for word in ("PASSWORD", "PASSPHRASE", "SECRET", "TOKEN"))]
    def safe(value):
        for secret in secrets:
            value = value.replace(secret, "<redacted>")
        return value
    client = None
    try:
        client = connect(options)
        if not args.upgrade:
            print(json.dumps(inspect_node(client), indent=2))
        else:
            status, output, error = upgrade(client, args.node)
            print(safe(output), end="")
            if error:
                print(safe(error), file=sys.stderr, end="")
            if status:
                raise RuntimeError("Remote upgrade failed; retained staged files and backup for diagnosis")
    except Exception as error:
        print("STOP:", safe(str(error)), file=sys.stderr)
        raise SystemExit(1)
    finally:
        if client:
            client.close()


if __name__ == "__main__":
    main()
