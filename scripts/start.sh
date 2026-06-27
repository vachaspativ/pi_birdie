#!/usr/bin/env bash
# start.sh — Start pi_birdie in kiosk (systemd) mode
set -euo pipefail

SERVICE="pi_birdie"

echo "Starting ${SERVICE} service…"
sudo systemctl start "${SERVICE}.service"

sleep 1
sudo systemctl status "${SERVICE}.service" --no-pager

echo ""
echo "pi_birdie is running in kiosk mode."
echo "View logs:  journalctl -u ${SERVICE} -f"
echo "Stop:       sudo systemctl stop ${SERVICE}"
