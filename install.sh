#!/bin/bash
# Run init on node A, join on node B. No SSH transport is used.
set -euo pipefail
case "${1:-}" in
  init|join|witness-init|witness-join|funnel-enable) ;;
  -h|--help) echo 'Usage: bash install.sh init | join | witness-init | witness-join | funnel-enable. LAN or Tailscale; no SSH.'; exit 0 ;;
  *) echo 'Usage: bash install.sh --help'; exit 2 ;;
esac
[[ $(id -u) == 0 ]] || { echo 'Run as root inside the selected LXC.'; exit 1; }
base=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
if [[ "$1" == witness-init || "$1" == witness-join || "$1" == funnel-enable ]]; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y python3 python3-venv ca-certificates curl iproute2
  python3 -m venv "$base/cloudflare-ha/.venv"
  "$base/cloudflare-ha/.venv/bin/python" -m pip install -r "$base/cloudflare-ha/requirements-local.txt"
  exec "$base/cloudflare-ha/.venv/bin/python" "$base/cloudflare-ha/tools/install-witness-local.py" "$@"
fi
test -f /opt/immich/.env || { echo 'This must be an existing native Immich LXC.'; exit 1; }
if [[ -f /etc/immich-ha/config.json ]]; then
  echo 'Existing HA detected. This installer is for new installations; it will not overwrite it.'
  exit 1
fi
test -f "$base/cloudflare-ha/tools/install-cluster.py"
mountpoint -q /mnt/immich-storage || { echo 'Mount the media volume at /mnt/immich-storage first.'; exit 1; }
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y python3 python3-venv ca-certificates curl
python3 -m venv "$base/cloudflare-ha/.venv"
"$base/cloudflare-ha/.venv/bin/python" -m pip install -r "$base/cloudflare-ha/requirements-local.txt"
exec "$base/cloudflare-ha/.venv/bin/python" "$base/cloudflare-ha/tools/install-cluster.py" "$@"
