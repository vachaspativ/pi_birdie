#!/usr/bin/env bash
# stop_backyard.sh — Stop pi_birdie backyard (systemd) service
set -euo pipefail

SERVICE="pi_birdie"

echo "Stopping ${SERVICE} service…"
sudo systemctl stop "${SERVICE}.service"

echo "pi_birdie stopped. Microphone and system resources released."
echo "Restart: ./scripts/start_backyard.sh"
