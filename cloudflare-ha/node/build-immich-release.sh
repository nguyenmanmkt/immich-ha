#!/bin/bash
# Fixed staging-only build, derived from the reviewed native community build.
# Portions adapted from community-scripts/ProxmoxVE/ct/immich.sh (MIT),
# Copyright (c) 2021-2026 community-scripts ORG; author vhsdream.
# See ../../THIRD_PARTY_NOTICES.md for the upstream license notice.
# No apt, systemctl, database connection, .env sourcing or service replacement.
set -euo pipefail
source_dir=$1
bundle=$2
[[ $source_dir == /mnt/immich-storage/backups/immich-upgrades/*/source/immich-* ]]
[[ $bundle == /mnt/immich-storage/backups/immich-upgrades/*/bundle ]]
[[ ! -e $bundle ]]
cd "$source_dir"
[[ $(node -p 'require("./server/package.json").version') == 3.4.2 ]]
export SHARP_FORCE_GLOBAL_LIBVIPS=true
pnpm_version=$(node -p 'require("./package.json").packageManager.split("@")[1].split("+")[0]')
corepack prepare "pnpm@$pnpm_version" --activate
corepack pnpm --config.dangerouslyAllowAllBuilds=true install --frozen-lockfile
corepack pnpm --filter @immich/sdk --filter @immich/plugin-sdk --filter immich build
corepack pnpm --filter immich --prod --no-optional deploy "$bundle"
corepack pnpm --dir "$bundle/node_modules/sharp" exec npm run build
unset SHARP_FORCE_GLOBAL_LIBVIPS
corepack pnpm --filter @immich/sdk --filter immich-web --filter @immich/cli build
corepack pnpm --filter @immich/cli --prod --no-optional deploy "$bundle/cli"
cp -a web/build "$bundle/www"
cp LICENSE "$bundle/"
# Native binaries/libraries and unchanged ML/plugin assets are checked/reused by
# immich_upgrade.py; the new source never runs against a standby database.
