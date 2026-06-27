#!/usr/bin/env bash
# restart.sh — Restart pi_birdie service (clean reload)
set -euo pipefail

SERVICE="pi_birdie"

echo "Restarting ${SERVICE} service…"
sudo systemctl restart "${SERVICE}.service"

sleep 2
sudo systemctl status "${SERVICE}.service" --no-pager

echo ""
echo "pi_birdie restarted."
echo "View logs:  journalctl -u ${SERVICE} -f"
