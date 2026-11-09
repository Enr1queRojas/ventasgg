#!/usr/bin/env bash
# Respalda la base del bot y copia el respaldo fuera del volumen de Docker.
#
# Uso:   deploy/backup.sh            (desde la carpeta del repo o cualquier otra)
# Cron:  15 3 * * * /ruta/a/ventasgg/deploy/backup.sh >> $HOME/ventasgg-backups/backup.log 2>&1
#
# Variables opcionales:
#   BACKUP_DIR   carpeta en el host (por defecto $HOME/ventasgg-backups)
#   KEEP_DAYS    días que se conservan en el host (por defecto 30)
set -euo pipefail

cd "$(dirname "$0")/.."
BACKUP_DIR="${BACKUP_DIR:-$HOME/ventasgg-backups}"
KEEP_DAYS="${KEEP_DAYS:-30}"
mkdir -p "$BACKUP_DIR"

if ! docker compose ps --status running --services | grep -qx sales-bot; then
  echo "$(date '+%F %T') ERROR: el contenedor sales-bot no está corriendo; no se respaldó." >&2
  exit 1
fi

# Respaldo consistente dentro del contenedor (conserva los 7 más recientes en el volumen).
remote="$(docker compose exec -T sales-bot python -m scripts.backup_db --keep 7 | tail -n 1)"
docker compose cp "sales-bot:${remote}" "$BACKUP_DIR/"
find "$BACKUP_DIR" -name 'sales_bot-*.db' -type f -mtime +"$KEEP_DAYS" -delete

echo "$(date '+%F %T') OK: $BACKUP_DIR/$(basename "$remote")"
