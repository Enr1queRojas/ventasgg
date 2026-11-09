#!/usr/bin/env bash
# Despliega o actualiza el bot en la VM: respaldo, código nuevo, reconstrucción y verificación.
#
# Uso:  deploy/deploy.sh [rama]      (por defecto: main)
set -euo pipefail

cd "$(dirname "$0")/.."
BRANCH="${1:-main}"

say() { printf '\n==> %s\n' "$*"; }
fail() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

[ -f .env ] || fail "falta .env en $(pwd) (ver «Configuración» en el README)"
if [ "$(stat -c '%a' .env)" != "600" ]; then
  chmod 600 .env
  echo "Permisos de .env ajustados a 600"
fi
for var in OPENAI_API_KEY OPENAI_MODEL WHATSAPP_ACCESS_TOKEN WHATSAPP_PHONE_NUMBER_ID WHATSAPP_VERIFY_TOKEN \
           WHATSAPP_APP_SECRET WHATSAPP_API_VERSION SALES_PHONE; do
  grep -Eq "^${var}=.+" .env || fail "falta ${var} en .env"
done

if docker compose ps --status running --services 2>/dev/null | grep -qx sales-bot; then
  say "Respaldo previo"
  deploy/backup.sh
fi

say "Actualizando código (${BRANCH})"
git fetch origin "$BRANCH"
git checkout "$BRANCH"
git pull --ff-only origin "$BRANCH"
git log --oneline -1

say "Construyendo y reiniciando"
docker compose up -d --build

say "Esperando a que el servicio esté sano"
for _ in $(seq 1 30); do
  state="$(docker inspect -f '{{.State.Health.Status}}' ventasgg-sales-bot 2>/dev/null || echo starting)"
  [ "$state" = "healthy" ] && break
  sleep 3
done
[ "$state" = "healthy" ] || { docker compose logs --tail 50; fail "el servicio no quedó sano (estado: $state)"; }
curl -fsS http://127.0.0.1:8003/health && echo

say "Arranque"
docker compose logs --since 3m | grep -E "Servicio iniciado|WhatsApp conectado|No se pudo validar|Worker iniciado|ERROR" || true

docker image prune -f >/dev/null
say "Listo"
