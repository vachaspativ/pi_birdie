#!/usr/bin/env bash
# start_backyard.sh — Start pi_birdie in backyard (kiosk/systemd) mode
set -euo pipefail

SERVICE="pi_birdie"

echo "Starting ${SERVICE} service…"
sudo systemctl start "${SERVICE}.service"

sleep 1
sudo systemctl status "${SERVICE}.service" --no-pager

echo ""
echo "pi_birdie is running in backyard mode."
echo "View logs:  journalctl -u ${SERVICE} -f"
echo "Stop:       sudo systemctl stop ${SERVICE}"
