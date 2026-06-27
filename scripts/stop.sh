#!/usr/bin/env bash
# stop.sh — Stop pi_birdie (systemd) service
set -euo pipefail

SERVICE="pi_birdie"

echo "Stopping ${SERVICE} service…"
sudo systemctl stop "${SERVICE}.service"

echo "pi_birdie stopped. Microphone and system resources released."
echo "Restart: ./scripts/start.sh"
