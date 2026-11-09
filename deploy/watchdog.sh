#!/usr/bin/env bash
# Vigila que el bot responda y avisa (ALERT_WEBHOOK_URL) cuando se cae y cuando vuelve.
#
# Cron (cada 5 minutos):
#   */5 * * * * /ruta/a/ventasgg/deploy/watchdog.sh >> $HOME/ventasgg-backups/watchdog.log 2>&1
#
# Revisa http://127.0.0.1:8003/health (contenedor y base) y, si WATCHDOG_PUBLIC_URL está en .env
# (p. ej. https://ventas.proadel.app/webhook/health), también el acceso por el túnel.
# Alerta tras 2 revisiones fallidas seguidas, para no avisar por un reinicio o un despliegue.
set -uo pipefail

cd "$(dirname "$0")/.."
# shellcheck source=deploy/lib.sh
source deploy/lib.sh

STATE_DIR="${STATE_DIR:-$HOME/.ventasgg}"
STATE_FILE="$STATE_DIR/watchdog.state"
mkdir -p "$STATE_DIR"

problem=""
if ! curl -fsS --max-time 10 http://127.0.0.1:8003/health >/dev/null 2>&1; then
  state="$(docker inspect -f '{{.State.Status}}/{{if .State.Health}}{{.State.Health.Status}}{{end}}' ventasgg-sales-bot 2>/dev/null | head -n 1)"
  [ -n "$state" ] || state="no existe"
  problem="El bot no responde en el servidor (contenedor: ${state})."
else
  public_url="$(env_value WATCHDOG_PUBLIC_URL)"
  if [ -n "$public_url" ] && ! curl -fsS --max-time 15 "$public_url" >/dev/null 2>&1; then
    problem="El bot funciona en el servidor pero no se alcanza desde internet (${public_url}): revisar el túnel de Cloudflare."
  fi
fi

failures="$(cat "$STATE_FILE" 2>/dev/null || echo 0)"
case "$failures" in ''|*[!0-9]*) failures=0 ;; esac

if [ -n "$problem" ]; then
  failures=$((failures + 1))
  echo "$failures" > "$STATE_FILE"
  if [ "$failures" -eq 2 ]; then
    send_alert "Servicio caído" "$problem Los clientes no reciben respuesta. Revisar con: docker compose logs --tail 100" urgente
  fi
  echo "$(date '+%F %T') FALLA ($failures): $problem"
  exit 1
fi

if [ "$failures" -ge 2 ]; then
  send_alert "Servicio restablecido" "El bot vuelve a responder."
fi
echo 0 > "$STATE_FILE"
