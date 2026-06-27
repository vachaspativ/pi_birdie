#!/usr/bin/env bash
# =============================================================================
# install.sh — pi_birdie Installation Script
# Target: Raspberry Pi 5 running Raspberry Pi OS Bookworm (64-bit)
#
# What this script does:
#   1. Updates system packages
#   2. Installs required apt dependencies (ffmpeg, libasound2-dev, gpsd, etc.)
#   3. Creates a Python 3 virtual environment at ~/pi_birdie_env/
#   4. Installs pip dependencies from requirements.txt
#   5. Installs TFLite runtime (ai-edge-litert preferred, tflite-runtime fallback)
#   6. Creates required project directories
#   7. Generates and installs a systemd service unit for backyard mode
#   8. Prints next steps
#
# Usage:
#   chmod +x scripts/install.sh
#   ./scripts/install.sh
#
# After install:
#   source ~/pi_birdie_env/bin/activate
#   python main.py                          # on_demand mode
#   sudo systemctl start pi_birdie.service  # backyard mode
# =============================================================================

set -euo pipefail

# ── Colours ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

info()    { echo -e "${CYAN}[INFO]${RESET}  $*"; }
success() { echo -e "${GREEN}[OK]${RESET}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${RESET}  $*"; }
error()   { echo -e "${RED}[ERROR]${RESET} $*"; }
header()  { echo -e "\n${BOLD}${CYAN}━━━ $* ━━━${RESET}"; }

# ── Configuration ─────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
VENV_DIR="$HOME/pi_birdie_env"
SERVICE_NAME="pi_birdie"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

info "Project directory: $PROJECT_DIR"
info "Virtual environment: $VENV_DIR"

# ── Step 1: System packages ───────────────────────────────────────────────────
header "Step 1: Installing system packages"

sudo apt-get update -qq
sudo apt-get install -y \
    ffmpeg \
    libsndfile1 \
    libasound2-dev \
    libportaudio2 \
    portaudio19-dev \
    gpsd \
    gpsd-clients \
    python3-gps \
    python3-venv \
    python3-dev \
    python3-pip \
    git \
    wget \
    curl

success "System packages installed."

# ── Step 2: Enable gpsd ───────────────────────────────────────────────────────
header "Step 2: Configuring gpsd"

if systemctl list-unit-files gpsd.socket &>/dev/null; then
    sudo systemctl enable --now gpsd.socket || warn "Could not enable gpsd.socket (no GPS hardware?)"
    success "gpsd socket enabled."
else
    warn "gpsd.socket not found — skipping. Install gpsd if you have a GPS module."
fi

# ── Step 3: Python virtual environment ───────────────────────────────────────
header "Step 3: Creating Python virtual environment"

if [ -d "$VENV_DIR" ]; then
    warn "Virtual environment already exists at $VENV_DIR — reusing it."
else
    python3 -m venv "$VENV_DIR"
    success "Virtual environment created at $VENV_DIR"
fi

# Activate venv for subsequent pip commands
# shellcheck disable=SC1090
source "$VENV_DIR/bin/activate"

pip install --upgrade pip wheel setuptools -q
success "pip upgraded."

# ── Step 4: Install pip dependencies ─────────────────────────────────────────
header "Step 4: Installing Python dependencies"

pip install "numpy<2.0" -q
pip install -r "$PROJECT_DIR/requirements.txt" -q
success "Python dependencies installed."

# ── Step 5: TFLite Runtime ───────────────────────────────────────────────────
header "Step 5: Installing TFLite Runtime"

TFLITE_INSTALLED=0

info "Attempting to install ai-edge-litert (preferred for Pi 5 + Python 3.11+)…"
if pip install ai-edge-litert -q 2>/dev/null; then
    success "ai-edge-litert installed successfully."
    TFLITE_INSTALLED=1
else
    warn "ai-edge-litert not available for this platform. Trying tflite-runtime…"
    if pip install tflite-runtime -q 2>/dev/null; then
        success "tflite-runtime installed successfully."
        TFLITE_INSTALLED=1
    else
        warn "tflite-runtime also failed. Trying full tensorflow (large download)…"
        if pip install tensorflow -q 2>/dev/null; then
            success "Full tensorflow installed (fallback)."
            TFLITE_INSTALLED=1
        else
            error "Could not install any TFLite runtime."
            error "pi_birdie requires one of: ai-edge-litert, tflite-runtime, or tensorflow."
            error "Please install manually: pip install ai-edge-litert"
            exit 1
        fi
    fi
fi

# ── Step 6: Create project directories ───────────────────────────────────────
header "Step 6: Creating project directories"

mkdir -p \
    "$PROJECT_DIR/audio_samples" \
    "$PROJECT_DIR/exports" \
    "$PROJECT_DIR/data" \
    "$PROJECT_DIR/bird_images" \
    "$PROJECT_DIR/logs"

success "Project directories created."

# ── Step 7: Generate and install systemd service ─────────────────────────────
header "Step 7: Installing systemd service"

CURRENT_USER="$(whoami)"

cat > "/tmp/${SERVICE_NAME}.service" <<EOF
[Unit]
Description=pi_birdie Bird Identification Service
Documentation=file://${PROJECT_DIR}/docs/README.md
After=network.target sound.target graphical.target
Wants=gpsd.service

[Service]
Type=simple
User=${CURRENT_USER}
WorkingDirectory=${PROJECT_DIR}
Environment="DISPLAY=:0"
Environment="XAUTHORITY=/home/${CURRENT_USER}/.Xauthority"
ExecStart=${VENV_DIR}/bin/python ${PROJECT_DIR}/main.py --mode backyard
Restart=on-failure
RestartSec=10
TimeoutStopSec=15
StandardOutput=journal
StandardError=journal
SyslogIdentifier=pi_birdie

[Install]
WantedBy=graphical.target
EOF

sudo cp "/tmp/${SERVICE_NAME}.service" "$SERVICE_FILE"
sudo systemctl daemon-reload
sudo systemctl enable "${SERVICE_NAME}.service"
success "Systemd service installed and enabled: $SERVICE_FILE"
info "  Start:   sudo systemctl start ${SERVICE_NAME}"
info "  Stop:    sudo systemctl stop ${SERVICE_NAME}"
info "  Logs:    journalctl -u ${SERVICE_NAME} -f"

# ── Step 8: Validate installation ────────────────────────────────────────────
header "Step 8: Validating installation"

info "Checking Python imports…"
"$VENV_DIR/bin/python" -c "
import birdnetlib
import customtkinter
import sounddevice
import yaml
import gps
import requests
print('  ✓ All core imports OK')
"

"$VENV_DIR/bin/python" -c "
try:
    import ai_edge_litert
    print('  ✓ ai-edge-litert available')
except ImportError:
    try:
        import tflite_runtime
        print('  ✓ tflite-runtime available')
    except ImportError:
        import tensorflow
        print('  ✓ tensorflow available (fallback)')
"

"$VENV_DIR/bin/python" -m py_compile "$PROJECT_DIR/main.py" && \
    echo "  ✓ main.py syntax OK"

success "Installation validated."

# ── Done ─────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}${GREEN}╔══════════════════════════════════════════════════════╗"
echo -e "║         pi_birdie installation complete! 🐦          ║"
echo -e "╚══════════════════════════════════════════════════════╝${RESET}"
echo ""
echo -e "${BOLD}Next steps:${RESET}"
echo ""
echo -e "  1. ${CYAN}Edit your configuration:${RESET}"
echo -e "     nano $PROJECT_DIR/config.yaml"
echo -e "     → Set your eBird API token (ebird.api_token)"
echo -e "     → Set your location / GPS fallback coordinates"
echo -e "     → Set audio.channels=4 if using ReSpeaker mic array"
echo ""
echo -e "  2. ${CYAN}Download bird images (optional, ~2h for all species):${RESET}"
echo -e "     source $VENV_DIR/bin/activate"
echo -e "     python $PROJECT_DIR/scripts/download_bird_images.py \\"
echo -e "            --api-key YOUR_EBIRD_KEY --region US-TX --limit 200"
echo ""
echo -e "  3. ${CYAN}Run in on_demand mode (standard window):${RESET}"
echo -e "     source $VENV_DIR/bin/activate && python $PROJECT_DIR/main.py"
echo ""
echo -e "  4. ${CYAN}Start in backyard/kiosk mode (systemd):${RESET}"
echo -e "     sudo systemctl start ${SERVICE_NAME}"
echo ""
echo -e "  5. ${CYAN}Get your eBird API token:${RESET}"
echo -e "     https://ebird.org/api/keygen"
echo ""
