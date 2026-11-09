#!/usr/bin/env bash
# Respalda la base del bot: copia consistente dentro del contenedor, copia en el host y,
# si BACKUP_REMOTE está en .env, copia comprimida fuera de la VM con rclone.
#
# Uso:   deploy/backup.sh
# Cron:  15 3 * * * /ruta/a/ventasgg/deploy/backup.sh >> $HOME/ventasgg-backups/backup.log 2>&1
#
# Variables (en .env o en el entorno):
#   BACKUP_DIR          carpeta en el host (por defecto $HOME/ventasgg-backups)
#   KEEP_DAYS           días que se conservan en el host (por defecto 30)
#   BACKUP_REMOTE       destino rclone fuera de la VM, p. ej. ventasgg-cifrado:   (ver README)
#   REMOTE_KEEP_DAYS    días que se conservan en el destino remoto (por defecto 90)
#   SKIP_REMOTE=1       solo respaldo local (lo usa deploy.sh antes de actualizar)
# Si algo falla, avisa por ALERT_WEBHOOK_URL.
set -euo pipefail

cd "$(dirname "$0")/.."
# shellcheck source=deploy/lib.sh
source deploy/lib.sh

setting() { local value="${!1:-}"; [ -n "$value" ] || value="$(env_value "$1")"; printf '%s' "${value:-$2}"; }
BACKUP_DIR="$(setting BACKUP_DIR "$HOME/ventasgg-backups")"
KEEP_DAYS="$(setting KEEP_DAYS 30)"
BACKUP_REMOTE="$(setting BACKUP_REMOTE "")"
REMOTE_KEEP_DAYS="$(setting REMOTE_KEEP_DAYS 90)"

step="inicio"
on_error() {
  send_alert "Respaldo fallido" "El respaldo de la base falló en el paso «${step}». Revisar ${BACKUP_DIR}/backup.log." urgente
}
trap on_error ERR

mkdir -p "$BACKUP_DIR"

step="verificar contenedor"
if ! docker compose ps --status running --services | grep -qx sales-bot; then
  echo "$(date '+%F %T') ERROR: el contenedor sales-bot no está corriendo; no se respaldó." >&2
  false
fi

step="respaldo en el contenedor"
# Respaldo consistente con el bot en marcha; conserva los 7 más recientes en el volumen.
remote_path="$(docker compose exec -T sales-bot python -m scripts.backup_db --keep 7 | tail -n 1)"
name="$(basename "$remote_path")"

step="copia al host"
docker compose cp "sales-bot:${remote_path}" "$BACKUP_DIR/"
find "$BACKUP_DIR" -name 'sales_bot-*.db' -type f -mtime +"$KEEP_DAYS" -delete
echo "$(date '+%F %T') OK host: $BACKUP_DIR/$name"

if [ -n "$BACKUP_REMOTE" ] && [ "${SKIP_REMOTE:-0}" != "1" ]; then
  step="compresión"
  command -v rclone >/dev/null || { echo "rclone no está instalado (ver README)" >&2; false; }
  gzip -c "$BACKUP_DIR/$name" > "$BACKUP_DIR/$name.gz"

  step="subida a ${BACKUP_REMOTE}"
  rclone copy "$BACKUP_DIR/$name.gz" "$BACKUP_REMOTE" --retries 3 --low-level-retries 5
  rclone lsf "$BACKUP_REMOTE" --include "$name.gz" | grep -qx "$name.gz"
  rm -f "$BACKUP_DIR/$name.gz"

  step="limpieza remota"
  rclone delete "$BACKUP_REMOTE" --include 'sales_bot-*.db.gz' --min-age "${REMOTE_KEEP_DAYS}d"
  echo "$(date '+%F %T') OK remoto: ${BACKUP_REMOTE}${name}.gz"
fi
