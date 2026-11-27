#!/bin/bash
# Usage: bash install-node.sh primary|backup
set -euo pipefail
node=${1:?Specify primary or backup}
case "$node" in primary|backup) ;; *) echo 'Invalid node'; exit 1 ;; esac
[[ $(id -u) == 0 ]]
base=$(cd -- "$(dirname -- "$0")" && pwd)
test -f "$base/config.json" || { echo 'Copy generated node config to config.json first'; exit 1; }
expected=$(python3 -c 'import json,sys,ipaddress; x=json.load(open(sys.argv[1]))["ip_address"]; print(ipaddress.IPv4Address(x))' "$base/config.json")
ip -o -4 addr show | awk '{print $4}' | cut -d/ -f1 | grep -qxF "$expected" || {
  echo "STOP: This node must have IP $expected"; exit 1;
}
python3 - "$base/config.json" "$node" <<'PY'
import json, sys
c = json.load(open(sys.argv[1]))
assert c['node'] == sys.argv[2]
assert c.get('enforce') is False, 'First install must be in observe mode'
assert len(c['agent_token']) >= 32 and len(c['origin_token']) >= 32
PY
mountpoint -q /mnt/immich-storage
apt-get update
apt-get install -y nginx python3 iproute2
if [[ -e /etc/immich-ha/config.json ]]; then
  echo 'STOP: An existing agent config was found; do not overwrite it'; exit 1;
fi
install -d -m 700 /etc/immich-ha /var/lib/immich-ha
install -m 600 "$base/config.json" /etc/immich-ha/config.json
install -m 700 "$base/agent.py" /usr/local/sbin/immich-ha-agent
install -m 644 "$base/immich-ha.nginx.conf" /etc/nginx/conf.d/immich-ha.conf
if [[ "$node" == backup && -f /var/lib/immich-standby/current-db ]]; then
  cp -an /usr/local/sbin/immich-standby-run /usr/local/sbin/immich-standby-run.before-ha
  install -o root -g immich -m 750 "$base/standby-run" /usr/local/sbin/immich-standby-run
  chown root:immich /var/lib/immich-standby/role
  chmod 640 /var/lib/immich-standby/role
fi
cat > /etc/systemd/system/immich-ha-agent.service <<'UNIT'
[Unit]
Description=Immich HA lease agent and local origin authorization
After=network-online.target
Wants=network-online.target
RequiresMountsFor=/mnt/immich-storage

[Service]
Type=simple
ExecStart=/usr/local/sbin/immich-ha-agent
Restart=always
RestartSec=3
UMask=0077

[Install]
WantedBy=multi-user.target
UNIT
nginx -t
systemctl daemon-reload
systemctl enable --now immich-ha-agent.service
systemctl enable --now nginx.service
systemctl reload nginx.service
echo "INSTALLED $node IN OBSERVE MODE. Production routing and auto takeover are unchanged."
