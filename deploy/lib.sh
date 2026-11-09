#!/usr/bin/env bash
# Funciones comunes de los scripts de deploy/. Se cargan con:  source "$(dirname "$0")/lib.sh"
# Requieren estar en la carpeta del repo (los scripts hacen cd antes de cargarlo).

# Valor de una variable de .env (sin comillas). Vacío si no existe.
env_value() {
  [ -f .env ] || return 0
  { grep -E "^$1=" .env || true; } | tail -n 1 | cut -d= -f2- | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' \
    -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'$/\1/"
}

_json_escape() {
  printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' | tr '\n' ' '
}

# send_alert "Título" "Mensaje" [urgente]  -> ALERT_WEBHOOK_URL de .env (ntfy, Slack o Discord).
send_alert() {
  local url title message priority body host
  url="$(env_value ALERT_WEBHOOK_URL)"
  title="Ventas GG: $1"
  message="$2"
  echo "$(date '+%F %T') ALERTA ${title}: ${message}" >&2
  [ -n "$url" ] || return 0
  host="$(printf '%s' "$url" | sed -E 's#^[a-z]+://([^/]+).*#\1#')"
  if printf '%s' "$host" | grep -q ntfy; then
    priority=4; [ "${3:-}" = "urgente" ] && priority=5
    body="{\"topic\":\"$(_json_escape "${url##*/}")\",\"title\":\"$(_json_escape "$title")\",\"message\":\"$(_json_escape "$message")\",\"priority\":${priority},\"tags\":[\"warning\"]}"
    url="$(printf '%s' "$url" | sed -E 's#^([a-z]+://[^/]+).*#\1#')"
  else
    body="{\"text\":\"*$(_json_escape "$title")*\\n$(_json_escape "$message")\",\"content\":\"*$(_json_escape "$title")*\\n$(_json_escape "$message")\"}"
  fi
  curl -fsS --max-time 15 -H 'Content-Type: application/json' -d "$body" "$url" >/dev/null \
    || echo "$(date '+%F %T') No se pudo enviar la alerta" >&2
}
