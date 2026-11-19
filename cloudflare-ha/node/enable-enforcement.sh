#!/bin/bash
# Only after production routes through the Worker and both origin gates.
set -euo pipefail
[[ $(id -u) == 0 ]]
headers=$(mktemp)
trap 'rm -f -- "$headers"' EXIT
public_url=$(python3 -c 'import json; print(json.load(open("/etc/immich-ha/config.json"))["public_url"])')
curl --fail --silent --show-error --max-time 10 --dump-header "$headers" \
  "${public_url%/}/api/server/ping" >/dev/null
grep -qi '^X-Immich-HA-Node: primary' "$headers" || {
  echo 'STOP: First attach the production domain to the Worker and verify primary routing'; exit 1;
}
node=$(python3 -c 'import json; print(json.load(open("/etc/immich-ha/config.json"))["node"])')
case "$node" in primary|backup) ;; *) exit 1 ;; esac
python3 <<'PY'
import json, os, shutil
from pathlib import Path
env = Path('/opt/immich/.env')
backup = Path('/opt/immich/.env.before-ha-bind')
if not backup.exists():
    shutil.copy2(env, backup)
stat = env.stat()
lines = [line for line in env.read_text().splitlines() if not line.startswith('IMMICH_HOST=')]
lines.append('IMMICH_HOST=127.0.0.1')
temporary = env.with_name('.env.ha-tmp')
temporary.write_text('\n'.join(lines) + '\n')
os.chmod(temporary, stat.st_mode & 0o777)
os.chown(temporary, stat.st_uid, stat.st_gid)
os.replace(temporary, env)
PY
runtime_version=$(python3 -c 'import json; print(json.load(open("/etc/immich-ha/config.json")).get("runtime_version", 1))')
if (( runtime_version >= 2 )); then
  systemctl restart immich-ha-app.service
elif [[ "$node" == primary ]]; then
  systemctl restart immich-web.service
else
  systemctl restart immich-standby.service
fi
python3 <<'PY'
import json, os
from pathlib import Path
path = Path('/etc/immich-ha/config.json')
value = json.loads(path.read_text())
value['enforce'] = True
temporary = path.with_name('config.json.tmp')
temporary.write_text(json.dumps(value, indent=2) + '\n')
os.chmod(temporary, 0o600)
os.replace(temporary, path)
PY
systemctl kill --kill-who=main --signal=HUP immich-ha-agent.service
echo "Enforcement enabled for $node. Auto takeover still requires a separate admin action."
