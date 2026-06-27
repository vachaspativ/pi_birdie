# pi_birdie

> **Offline bird identification station for Raspberry Pi 5**  
> Listens to environmental audio, identifies birds using Cornell Lab's BirdNET model,
> tracks detections in a local database, shows direction-of-arrival, and exports
> observations to eBird-compatible CSV files.

---

## Features

| Feature | Description |
|---|---|
| 🐦 Offline Identification | BirdNET-Lite (6000+ species) — no internet required |
| 🧭 Direction of Arrival | GCC-PHAT acoustic localization with compass display |
| 📡 Live GPS | gpsd integration with config.yaml fallback coordinates |
| 🌟 Rarity Indicator | eBird notable/species list — shown in real-time |
| 🖥 Adaptive UI | HDMI and touchscreen layouts, dark mode |
| 📤 eBird Export | eBird Record Format CSV export for manual upload |
| 🗄 Local Database | SQLite with 2-month auto-retention |
| 🔄 Two Modes | `on_demand` (windowed) and `backyard` (kiosk/systemd) |

---

## Hardware Requirements

| Component | Requirement |
|---|---|
| **Compute** | Raspberry Pi 5 (4GB or 8GB RAM recommended) |
| **OS** | Raspberry Pi OS Bookworm 64-bit |
| **Microphone** | USB mic (mono, DoA disabled) **or** ReSpeaker 4-Mic HAT (DoA enabled) |
| **Display** | HDMI monitor (1024×600+) **or** SPI touchscreen (800×480) |
| **GPS** (optional) | Any GPS module supported by gpsd (e.g., u-blox USB GPS dongle) |
| **Internet** (optional) | Required only for eBird rarity cache and CSV upload |

---

## Software Prerequisites

```bash
# Raspberry Pi OS Bookworm 64-bit
getconf LONG_BIT        # Must return 64
python3 --version       # Must be 3.11+
```

---

## Installation

### 1. Clone the Repository

```bash
git clone https://github.com/yourname/pi_birdie.git
cd pi_birdie
```

### 2. Run the Installation Script

```bash
chmod +x scripts/install.sh
./scripts/install.sh
```

This will:
- Install system packages (`ffmpeg`, `libasound2-dev`, `gpsd`, etc.)
- Create a Python venv at `~/pi_birdie_env/`
- Install all Python dependencies
- Install TFLite runtime (`ai-edge-litert` → `tflite-runtime` → `tensorflow` fallback)
- Register the `pi_birdie.service` systemd unit

### 3. Configure

```bash
nano config.yaml
```

**Essential settings:**

```yaml
location:
  latitude: 30.2672      # Your fallback latitude
  longitude: -97.7431    # Your fallback longitude
  state_province: US-TX
  country_code: US

ebird:
  api_token: ""          # Get from https://ebird.org/api/keygen
  region_code: US-TX-453 # Your county (CC-ST-FFF eBird format)

audio:
  channels: 1            # Change to 4 if using ReSpeaker 4-Mic array
```

### 4. Download Bird Images (Optional)

```bash
source ~/pi_birdie_env/bin/activate

# Quick start — download species for your region first (200 most common)
python scripts/download_bird_images.py \
    --api-key YOUR_EBIRD_KEY \
    --region US-TX \
    --limit 200

# Full download — all ~6000 species (1-2 hours)
python scripts/download_bird_images.py --api-key YOUR_EBIRD_KEY
```

---

## Running pi_birdie

### On-Demand Mode (Windowed Desktop App)

```bash
source ~/pi_birdie_env/bin/activate
python main.py
# or
python main.py --mode on_demand --config config.yaml
```

### Backyard Mode (Fullscreen Kiosk via systemd)

```bash
# Start
sudo systemctl start pi_birdie

# Stop
sudo systemctl stop pi_birdie

# View logs
journalctl -u pi_birdie -f

# Enable auto-start on boot
sudo systemctl enable pi_birdie
```

Or use the convenience scripts:

```bash
./scripts/start_backyard.sh
./scripts/stop_backyard.sh
./scripts/restart_backyard.sh
```

---

## Configuration Reference (`config.yaml`)

### `operation_mode`
- `"on_demand"` — Standard windowed app with minimize/maximize/close buttons
- `"backyard"` — Fullscreen kiosk managed by systemd; cursor hidden on touchscreen

### `audio`

| Key | Default | Description |
|---|---|---|
| `device_name` | `null` | ALSA device name. `null` = auto-detect |
| `sample_rate` | `48000` | **Do not change** — BirdNET requires 48kHz |
| `channels` | `1` | `1` = mono (no DoA), `4` = ReSpeaker 4-mic (DoA enabled) |
| `chunk_duration_sec` | `3.0` | **Do not change** — BirdNET model hardcoded |
| `min_confidence` | `0.25` | Minimum detection threshold (lower = more detections, more false positives) |
| `sensitivity` | `1.0` | BirdNET sensitivity (0.5–1.5) |

### `gps`

| Key | Default | Description |
|---|---|---|
| `enabled` | `true` | Connect to gpsd daemon |
| `gpsd_host` | `localhost` | gpsd hostname |
| `gpsd_port` | `2947` | gpsd port |
| `fallback_to_config` | `true` | Use `location.*` coordinates when GPS unavailable |

### `ui`

| Key | Options | Description |
|---|---|---|
| `display_mode` | `"hdmi"` / `"touchscreen"` | Controls layout density and touch target sizes |
| `fullscreen` | `true` / `false` | Auto-set to `true` in backyard mode |
| `hdmi_width` / `hdmi_height` | integers | Window size for HDMI display |
| `touchscreen_width` / `touchscreen_height` | integers | Window size for touchscreen |
| `touchscreen_font_scale` | `0.85` | Font scale multiplier for smaller displays |
| `touch_target_min_px` | `44` | Minimum button height for finger input |

### `doa`

| Key | Default | Description |
|---|---|---|
| `enabled` | `true` | DoA is also auto-disabled if `audio.channels < 2` |
| `mic_array_type` | `"respeaker_4mic"` | `"respeaker_4mic"` / `"linear_2mic"` / `"custom"` |
| `mic_radius_m` | `0.035` | For `respeaker_4mic`: 35mm radius (70mm diameter array) |
| `mic_spacing_m` | `0.05` | For `linear_2mic`: spacing between mics in metres |
| `confidence_threshold` | `0.3` | DoA results below this are shown as "No signal" |

### `rarity`

| Key | Default | Description |
|---|---|---|
| `enabled` | `true` | Enable rarity classification |
| `notable_refresh_hours` | `24` | How often to refresh eBird notable sightings |
| `spplist_refresh_days` | `30` | How often to refresh regional species list |

> **Note:** Rarity requires `ebird.api_token` to be set. Without it, all species show "Unknown".

---

## GPS Setup

### Install gpsd

```bash
sudo apt install gpsd gpsd-clients
```

### Connect GPS Module

For a USB GPS dongle (e.g. u-blox):

```bash
# Identify device
ls /dev/tty*                  # Usually /dev/ttyACM0 or /dev/ttyUSB0

# Configure gpsd
sudo nano /etc/default/gpsd
```

Set in `/etc/default/gpsd`:
```
DEVICES="/dev/ttyACM0"
GPSD_OPTIONS="-n"
START_DAEMON="true"
USBAUTO="true"
```

```bash
sudo systemctl restart gpsd
cgps -s                       # Verify fix (wait up to 5 minutes outdoors)
```

### GPS Status Indicators

| Badge | Meaning |
|---|---|
| 🛰 Live GPS (3D Fix) | Full 3D position from gpsd |
| 🛰 Live GPS (2D Fix) | 2D position (no altitude) |
| ⏳ Acquiring GPS… | Connected to gpsd, waiting for satellite fix |
| ⚠ Offline GPS | gpsd not reachable — using config.yaml fallback |
| 📍 Static Location | GPS disabled (`gps.enabled: false`) |

---

## ReSpeaker 4-Mic Array Setup (DoA)

The Seeed ReSpeaker 4-Mic HAT provides 4-channel audio for direction-of-arrival localization.

### Driver Installation

```bash
# Install community-maintained driver (HinTak fork — supports Bookworm)
git clone https://github.com/HinTak/seeed-voicecard.git
cd seeed-voicecard
sudo ./install.sh
sudo reboot
```

### Verify

```bash
arecord -l                           # Should list seeed-4mic-voicecard
arecord -Dac108 -f S32_LE -r 16000 -c 4 -d 5 test.wav
aplay test.wav                       # Should hear 5 seconds of recording
```

### Configure pi_birdie

```yaml
# config.yaml
audio:
  channels: 4               # Enable 4-channel capture
  device_name: null         # Auto-detect ReSpeaker

doa:
  enabled: true
  mic_array_type: respeaker_4mic
```

> **Warning:** Raspberry Pi kernel updates can break the seeed-voicecard driver.
> If DoA stops working after `apt upgrade`, reinstall the driver.

---

## eBird Integration

### Getting an API Token

1. Create an account at [ebird.org](https://ebird.org)
2. Visit [ebird.org/api/keygen](https://ebird.org/api/keygen)
3. Copy your token and add it to `config.yaml`:

```yaml
ebird:
  api_token: "your_token_here"
  region_code: US-TX-453    # Your county eBird code
```

### Finding Your Region Code

eBird county codes follow the format `CC-ST-FFF` (e.g. `US-TX-453` = Travis County, TX).
Find yours at [ebird.org/region/world/regions](https://ebird.org/region/world/regions).

### Exporting to eBird

1. Click **"Export eBird CSV"** button in the UI, or wait for a sync when online
2. Find the CSV in `./exports/pi_birdie_ebird_YYYYMMDD_HHMMSS.csv`
3. Upload at [ebird.org/submit/import](https://ebird.org/submit/import)

> **Important:** The eBird API is read-only — there is no programmatic submission.
> You must upload the CSV file manually at the link above.

---

## Architecture Overview

```
config.yaml
    │
    ├── main.py (entrypoint + orchestration)
    │   ├── database.py        → SQLite: log detections, 2-month retention, sync tracking
    │   ├── gps_service.py     → gpsd live GPS with config.yaml fallback
    │   ├── doa_locator.py     → GCC-PHAT DoA (numpy, ~1.2ms for 4-mic)
    │   ├── rarity_service.py  → eBird notable + spplist (O(1) set lookups)
    │   ├── audio_processor.py → sounddevice + BirdNET RecordingBuffer
    │   ├── sync_service.py    → connectivity monitor + eBird CSV export
    │   └── ui.py              → CustomTkinter adaptive UI
    │
    ├── scripts/
    │   ├── install.sh                 → Full Pi 5 setup
    │   └── download_bird_images.py    → eBird → Wikimedia image fetcher
    │
    └── data/                    → Rarity cache (spplist + notable JSON)
        bird_images/             → Species images ({Scientific_Name}.jpg)
        audio_samples/           → 3-sec WAV clips of detections
        exports/                 → eBird Record Format CSV files
```

---

## Troubleshooting

### No microphone detected

```bash
arecord -l                     # List recording devices
python3 -c "import sounddevice; print(sounddevice.query_devices())"
```

### BirdNET model not loading (TFLite error)

```bash
source ~/pi_birdie_env/bin/activate
pip install ai-edge-litert
# or
pip install tflite-runtime
```

### gpsd not connecting

```bash
sudo systemctl status gpsd
sudo systemctl restart gpsd
cgps -s                        # Test GPS fix
```

### No rarity data showing

- Set `ebird.api_token` in `config.yaml`
- Ensure `ebird.region_code` is set to a valid county code
- pi_birdie will auto-refresh rarity caches on first internet connection

### UI not appearing (headless / SSH session)

```bash
export DISPLAY=:0              # Set display before running
python main.py
```

---

## License

MIT License — see LICENSE file.

## Credits

- [BirdNET-Analyzer](https://github.com/kahst/BirdNET-Analyzer) — Cornell Lab of Ornithology
- [birdnetlib](https://github.com/joeweiss/birdnetlib) — Python bindings by Joe Weiss
- [eBird API](https://documenter.getpostman.com/view/664302/S1ENwy59) — Cornell Lab
- [Seeed ReSpeaker](https://github.com/HinTak/seeed-voicecard) — HinTak driver fork
