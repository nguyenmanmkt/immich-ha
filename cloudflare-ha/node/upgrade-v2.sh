#!/bin/bash
# Run inside one existing Immich LXC, with a matching generated config.json beside this file.
# Production remains on the same node; the app restarts during this upgrade.
set -euo pipefail
[[ $(id -u) == 0 ]]
base=$(cd -- "$(dirname -- "$0")" && pwd)
test -f /etc/immich-ha/config.json
test -f "$base/config.json"
for file in agent.py ha_runtime.py pg_stream.py immich_upgrade.py build-immich-release.sh maintenance-v2.py app-run-v2; do test -f "$base/$file"; done
bash -n "$base/build-immich-release.sh"
bash -n "$base/app-run-v2"
mountpoint -q /mnt/immich-storage

python3 - "$base/config.json" <<'PY'
import ipaddress, json, os, subprocess, sys, urllib.request
from pathlib import Path
old = json.loads(Path('/etc/immich-ha/config.json').read_text())
new = json.loads(Path(sys.argv[1]).read_text())
for key in ('node', 'controller_url', 'agent_token', 'origin_token'):
    if old[key] != new[key]:
        raise SystemExit('STOP: Node identity or keys do not match; no changes made')
expected = str(ipaddress.IPv4Address(new['ip_address']))
addresses = subprocess.check_output(['ip', '-o', '-4', 'addr', 'show'], text=True)
if not any(line.split()[3].split('/')[0] == expected for line in addresses.splitlines()):
    raise SystemExit('STOP: Wrong LXC/IP address')
request = urllib.request.Request(old['controller_url'].rstrip('/') + '/__ha/node-info', headers={
    'Authorization': 'Bearer ' + old['agent_token'], 'User-Agent': 'ImmichHA-Upgrade/2.0'})
with urllib.request.urlopen(request, timeout=10) as response:
    info = json.load(response)
if info.get('apiVersion', 0) < 2:
    raise SystemExit('STOP: Deploy the dashboard/backend Worker first')
if info.get('auto'):
    raise SystemExit('STOP: Turn auto OFF before upgrading nodes')
if info.get('pending') or (info.get('operation') and (info['operation'].get('maintenance') or info['operation']['status'] not in ('succeeded','failed'))):
    raise SystemExit('STOP: Finish/recover the current operation before upgrading')
Path('/run/immich-ha-upgrade-info.json').write_text(json.dumps(info))
os.chmod('/run/immich-ha-upgrade-info.json', 0o600)
print('Preflight OK:', old['node'], 'owner=' + info['owner'], 'epoch=' + str(info['epoch']))
PY

stamp=$(date +%Y%m%d-%H%M%S)
save="/etc/immich-ha/upgrade-backups/$stamp"
install -d -m 700 "$save"
cp -a /etc/immich-ha/config.json "$save/config.json"
cp -a /usr/local/sbin/immich-ha-agent "$save/agent.py"
if [[ -f /var/lib/immich-standby/role ]]; then
  cp -a /var/lib/immich-standby/role "$save/standby-role"
  printf 'maintenance\n' > /var/lib/immich-standby/role
fi
systemctl stop immich-ha-agent.service
for unit in immich-db-backup.timer immich-standby-update.timer; do
  if systemctl cat "$unit" >/dev/null 2>&1; then systemctl disable --now "$unit"; fi
done
for unit in immich-db-backup.service immich-standby-update.service; do
  if systemctl cat "$unit" >/dev/null 2>&1; then systemctl stop "$unit"; fi
done

install -d -o root -g immich -m 750 /var/lib/immich-ha /var/lib/immich-ha/runtime
install -d -o root -g root -m 700 /usr/local/lib/immich-ha
python3 - "$base/config.json" <<'PY'
import json, os, pwd
from pathlib import Path
config = Path('/etc/immich-ha/config.json')
old = json.loads(config.read_text())
new = json.loads(Path(__import__('sys').argv[1]).read_text())
info = json.loads(Path('/run/immich-ha-upgrade-info.json').read_text())
root = Path('/var/lib/immich-ha/runtime')
group = pwd.getpwnam('immich').pw_gid
def write(name, value):
    path = root / name
    path.write_text(value + '\n')
    os.chmod(path, 0o640)
    os.chown(path, 0, group)
if not (root / 'current-db').exists():
    legacy = Path('/var/lib/immich-standby/current-db')
    if old['node'] == 'backup' and legacy.exists():
        database = legacy.read_text().strip()
    else:
        values = [line.split('=', 1)[1].strip().strip('"\'') for line in Path('/opt/immich/.env').read_text().splitlines() if line.startswith('DB_DATABASE_NAME=')]
        if len(values) != 1:
            raise SystemExit('Cannot identify current database')
        database = values[0]
    write('current-db', database)
    is_owner = info['owner'] == old['node']
    # A recovered old primary is never silently seeded from stale data.
    known_standby = old['node'] == 'backup' and bool(info.get('agent', {}).get('canPromote'))
    write('mode', 'active' if is_owner else 'standby' if known_standby else 'fenced')
    seed = {'source': info['owner'], 'sourceEpoch': info['epoch'], 'appliedAt': None, 'dumpName': 'Bản sao trước nâng cấp'} if known_standby else None
    write('seed.json', json.dumps(seed))
for key in ('public_url', 'admin_url', 'display_name', 'ip_address'):
    if key in new: old[key] = new[key]
old['runtime_version'] = 2
temporary = config.with_name('config.json.tmp')
temporary.write_text(json.dumps(old, indent=2) + '\n')
os.chmod(temporary, 0o600)
os.replace(temporary, config)
PY

install -m 700 "$base/agent.py" /usr/local/sbin/immich-ha-agent
install -m 600 "$base/agent.py" /usr/local/lib/immich-ha/ha_agent.py
install -m 600 "$base/ha_runtime.py" /usr/local/lib/immich-ha/ha_runtime.py
install -m 600 "$base/pg_stream.py" /usr/local/lib/immich-ha/pg_stream.py
install -m 600 "$base/immich_upgrade.py" /usr/local/lib/immich-ha/immich_upgrade.py
install -m 700 "$base/build-immich-release.sh" /usr/local/lib/immich-ha/build-immich-release.sh
python3 - <<'PY'
import sys
sys.path.insert(0, '/usr/local/lib/immich-ha')
from pg_stream import install_network_guard
install_network_guard()
PY
install -m 700 "$base/maintenance-v2.py" /usr/local/sbin/immich-ha-maintenance
install -o root -g immich -m 750 "$base/app-run-v2" /usr/local/sbin/immich-ha-app-run
for unit in immich-web.service immich-standby.service immich-ml.service; do
  if systemctl cat "$unit" >/dev/null 2>&1; then systemctl disable --now "$unit"; fi
done

cat > /etc/systemd/system/immich-ha-app.service <<'UNIT'
[Unit]
Description=Immich managed active/standby application
After=postgresql.service redis-server.service
Requires=postgresql.service redis-server.service
RequiresMountsFor=/mnt/immich-storage

[Service]
Type=simple
User=immich
Group=immich
WorkingDirectory=/opt/immich/app
ExecStart=/usr/local/sbin/immich-ha-app-run
Restart=on-failure
RestartSec=5
UMask=0077

[Install]
WantedBy=multi-user.target
UNIT

for action in dump refresh; do
  cat > "/etc/systemd/system/immich-ha-$action.service" <<UNIT
[Unit]
Description=Immich managed $action
After=postgresql.service redis-server.service
Requires=postgresql.service redis-server.service
RequiresMountsFor=/mnt/immich-storage

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/immich-ha-maintenance $action
TimeoutStartSec=40min
UNIT
done
cat > /etc/systemd/system/immich-ha-dump.timer <<'UNIT'
[Unit]
Description=Hourly backup on the current active server
[Timer]
OnCalendar=hourly
Persistent=false
[Install]
WantedBy=timers.target
UNIT
cat > /etc/systemd/system/immich-ha-refresh.timer <<'UNIT'
[Unit]
Description=Refresh a prepared standby when a new completed snapshot arrives
[Timer]
OnBootSec=90s
OnUnitInactiveSec=60s
[Install]
WantedBy=timers.target
UNIT
systemctl daemon-reload
systemctl start postgresql.service redis-server.service
systemctl enable --now immich-ha-app.service immich-ha-agent.service
systemctl enable --now immich-ha-refresh.timer
if [[ -f /etc/immich-ha/disable-scheduled-dump ]]; then
  systemctl disable --now immich-ha-dump.timer
else
  systemctl enable --now immich-ha-dump.timer
fi
echo "UPGRADE V2 INSTALLED. Previous files: $save"
echo 'The first upgraded agent may wait for the previous session lease to expire.'
systemctl --no-pager status immich-ha-agent.service
