#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."; source config/estate.env
[ -n "${AIOBS_HUB_TAILNET_IP}" ] || { echo "AIOBS_HUB_TAILNET_IP unset (run Tailscale task first)"; exit 1; }
ssh "$AIOBS_HUB_SSH_HOST" "sudo mkdir -p /opt/observability && sudo chown \$(whoami) /opt/observability"
rsync -az --delete hub/ "$AIOBS_HUB_SSH_HOST:/opt/observability/"
scp config/estate.env "$AIOBS_HUB_SSH_HOST:/opt/observability/.env"
ssh "$AIOBS_HUB_SSH_HOST" "sudo test -s ${AIOBS_GRAFANA_ADMIN_PASSWORD_FILE} || { echo 'missing admin password file'; exit 1; }"
# secret must be group-readable (root group) or the non-root grafana container falls back to admin/admin
ssh "$AIOBS_HUB_SSH_HOST" "sudo chmod 640 ${AIOBS_GRAFANA_ADMIN_PASSWORD_FILE}"
ssh "$AIOBS_HUB_SSH_HOST" "cd /opt/observability && sudo docker compose --env-file .env up -d --remove-orphans"
