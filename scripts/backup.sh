#!/bin/sh
set -eu
umask 077
mkdir -p backups
file="backups/maxost-$(date -u +%Y%m%dT%H%M%SZ).dump"
docker compose exec -T postgres pg_dump -U maxost -d maxost -Fc > "$file"
echo "Database backup: $file"
echo 'Keep ENCRYPTION_KEYS in a separate protected backup. Do not commit either backup.'
