#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if [ -d .git ]; then
  git pull --ff-only
fi

mkdir -p data
if [ -d data ]; then
  backup_dir="${BACKUP_DIR:-/backups/btc_dca_2026}"
  mkdir -p "$backup_dir"
  tar -czf "$backup_dir/data-$(date +%Y%m%d-%H%M%S).tgz" data
fi

export APP_UID="${APP_UID:-$(id -u)}"
export APP_GID="${APP_GID:-$(id -g)}"

docker compose -f docker-compose.prod.yml up -d --build --remove-orphans
docker compose -f docker-compose.prod.yml ps
