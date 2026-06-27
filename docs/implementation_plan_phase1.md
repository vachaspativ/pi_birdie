# pi_birdie — Offline Bird Identification Station for Raspberry Pi 5

A Python desktop application that listens to environmental audio via an external microphone, identifies bird species offline using Cornell Lab's BirdNET model, displays results on a lightweight UI, tracks detections in SQLite, provides acoustic direction-of-arrival guidance, and exports data in eBird-compatible format.

## User Review Required

> [!IMPORTANT]
> **eBird API is Read-Only.** The eBird API 2.0 has **no POST endpoints** for submitting observations. The `sync_service.py` module will instead generate **eBird Record Format CSV files** that you manually upload at [ebird.org/submit/import](https://ebird.org/submit/import). The read-only API will be used for **taxonomy validation** only.

> [!WARNING]
> **ReSpeaker Driver Compatibility.** The original Seeed ReSpeaker drivers are unmaintained. The plan uses the community-maintained [HinTak fork](https://github.com/HinTak/seeed-voicecard). Kernel updates on the Pi can break these drivers. The DoA feature will **gracefully degrade** — if no multi-channel mic is detected, the compass UI is hidden and the system falls back to single-channel operation.

> [!IMPORTANT]
> **TFLite Runtime on Pi 5 / Python 3.11+.** The `tflite-runtime` package often lacks wheels for `aarch64 + Python 3.11`. The plan uses `ai-edge-litert` (Google's successor package) as the primary TFLite backend, with `tflite-runtime` as a fallback. `numpy` will be pinned to `<2.0` to avoid `_ARRAY_API` conflicts.

## Open Questions

1. **UI Framework Preference:** The prompt suggests CustomTkinter or PyQt6. This plan uses **CustomTkinter** for its lighter footprint and simpler dependency chain on Pi. Do you prefer PyQt6 instead?
2. **Bird Image Dataset:** The prompt mentions a "pre-downloaded Cornell/Wikipedia dataset" of bird images. Should we include a script to download bird images, or will you provide them manually? The plan includes a download helper script.
3. **GPS Module:** The schema includes `latitude`/`longitude`. Should the system attempt to read from a connected GPS module (e.g., via `gpsd`), or always use static fallback coordinates from `config.json`?
4. **Touchscreen vs HDMI:** Is your target display a touchscreen (SPI) or standard HDMI monitor? This affects UI element sizing and touch targets.

---

## Proposed Changes

### Project Directory Structure

```
pi_birdie/
├── config.json                  # Operational profiles, eBird token, fallback coordinates
├── main.py                      # Application entrypoint — parses mode, launches app
├── audio_processor.py           # sounddevice capture, buffering, BirdNET-Analyzer calls
├── doa_locator.py               # GCC-PHAT TDOA → DoA angle for multi-channel arrays
├── database.py                  # SQLite setup, detection logging, 2-month retention sweep
├── ui.py                        # CustomTkinter UI — spectrogram, compass, detection panel
├── sync_service.py              # eBird CSV export, taxonomy validation, connectivity check
├── requirements.txt             # Pinned Python dependencies
├── docs/
│   ├── README.md                # Setup guide, architecture, hardware schematic notes
│   └── AGENTS.md                # AI agent context — module map, state, rules
└── scripts/
    ├── install.sh               # Apt deps, venv, pip, systemd service registration
    ├── start_backyard.sh         # Start systemd service (backyard mode)
    ├── stop_backyard.sh          # Stop systemd service
    └── restart_backyard.sh       # Restart systemd service
```

---

### Component 1: Configuration (`config.json`)

#### [NEW] [config.json](file:///c:/Users/vacha/code/pi_birdie/config.json)

JSON configuration file with the following structure:

```json
{
  "OPERATION_MODE": "on_demand",
  "audio": {
    "device_name": null,
    "sample_rate": 48000,
    "channels": 1,
    "chunk_duration_sec": 3.0,
    "min_confidence": 0.25,
    "sensitivity": 1.0
  },
  "location": {
    "latitude": 0.0,
    "longitude": 0.0,
    "state_province": "US-TX",
    "country_code": "US",
    "location_name": "My Backyard"
  },
  "ebird": {
    "api_token": "",
    "default_protocol": "Stationary",
    "num_observers": 1,
    "complete_checklist": false,
    "export_directory": "./exports"
  },
  "database": {
    "path": "./pi_birdie.db",
    "retention_days": 60,
    "audio_samples_dir": "./audio_samples"
  },
  "doa": {
    "enabled": true,
    "mic_array_type": "respeaker_4mic",
    "mic_radius_m": 0.035,
    "speed_of_sound": 343.0,
    "bandpass_low_hz": 1000,
    "bandpass_high_hz": 6000
  },
  "ui": {
    "fullscreen": false,
    "theme": "dark",
    "window_width": 1024,
    "window_height": 600
  }
}
```

- `OPERATION_MODE`: `"backyard"` (headless/kiosk, systemd-managed) or `"on_demand"` (windowed desktop app).
- `audio.device_name`: `null` = auto-detect. Set to a specific ALSA device name if needed.
- `audio.channels`: Set to `4` for ReSpeaker 4-Mic to enable DoA. `1` for single USB mic.
- `doa.enabled`: Auto-disabled if `audio.channels < 2`.

---

### Component 2: Application Entrypoint (`main.py`)

#### [NEW] [main.py](file:///c:/Users/vacha/code/pi_birdie/main.py)

Responsibilities:
- Load and validate `config.json`
- Parse `OPERATION_MODE` to determine kiosk vs. windowed behavior
- Initialize all subsystems in order: Database → Analyzer → AudioProcessor → DOALocator → SyncService → UI
- Register signal handlers (`SIGTERM`, `SIGINT`) for clean shutdown
- In `backyard` mode: run headless (no UI) or fullscreen kiosk, logging to stdout/journald
- In `on_demand` mode: launch windowed CustomTkinter UI with standard decorations
- On exit: stop audio stream, close database, release all resources

**Key design decisions:**
- Uses a shared `queue.Queue` for audio→analysis pipeline (decoupled producer/consumer)
- Detection results dispatched to UI via thread-safe callback mechanism
- All threads are daemon threads to ensure clean exit

---

### Component 3: Audio Processing (`audio_processor.py`)

#### [NEW] [audio_processor.py](file:///c:/Users/vacha/code/pi_birdie/audio_processor.py)

Responsibilities:
- Capture continuous audio from the microphone using `sounddevice.InputStream`
- Auto-detect mic device (prefer ReSpeaker/ac108, fallback to default input)
- Maintain a rolling buffer of 3-second audio chunks (48kHz mono for BirdNET, multi-channel for DoA)
- Feed 3-second mono chunks to `birdnetlib.RecordingBuffer` for classification
- If multi-channel: split Channel 0 for BirdNET, all channels for DoA
- Emit detection events with species name, confidence, timestamp, and audio sample path
- Save audio samples of detections to disk (WAV, 3-second clips)

**Architecture:**

```
sounddevice.InputStream callback
    │
    ├──→ mono_buffer (deque, 3 sec)  ──→  BirdNET RecordingBuffer.analyze()
    │                                         │
    │                                         └──→  Detection Events
    │
    └──→ multichannel_buffer (deque)  ──→  DOALocator.estimate_doa()
                                              │
                                              └──→  Angle Events
```

**Key implementation details:**
- `sounddevice` chosen over `pyaudio` — it has better ALSA integration, numpy-native callbacks, and no portaudio build issues on Pi
- Audio resampled to 48kHz mono for BirdNET (model expects 48kHz)
- Buffer uses `collections.deque` with `maxlen` for automatic eviction
- Detection thread runs `RecordingBuffer.analyze()` in a loop, sleeping between chunks
- Audio sample files saved as `{timestamp}_{species}.wav` using `scipy.io.wavfile`

**BirdNET integration pattern:**
```python
from birdnetlib import RecordingBuffer
from birdnetlib.analyzer import Analyzer

analyzer = Analyzer()  # Load model once at startup

def classify_chunk(audio_np, sr=48000, lat=0.0, lon=0.0, min_conf=0.25):
    rec = RecordingBuffer(analyzer, audio_np, sample_rate=sr,
                          lat=lat, lon=lon, date=datetime.now(),
                          min_conf=min_conf)
    rec.analyze()
    return rec.detections  # List[dict] with common_name, scientific_name, confidence, etc.
```

---

### Component 4: Direction of Arrival (`doa_locator.py`)

#### [NEW] [doa_locator.py](file:///c:/Users/vacha/code/pi_birdie/doa_locator.py)

Responsibilities:
- Implement GCC-PHAT algorithm from scratch using numpy (no heavy external deps)
- Compute TDOA for all microphone pairs
- Convert TDOAs to a single DoA angle (0–360°) using least-squares estimation
- Apply bandpass pre-filtering (1000–6000 Hz) to isolate bird call frequencies
- Provide a confidence metric based on correlation peak sharpness
- Support configurable mic array geometries (ReSpeaker 4-mic circular, generic linear)

**Core algorithm (GCC-PHAT):**
1. FFT both mic signals
2. Cross-power spectrum: `R(f) = X1(f) · conj(X2(f))`
3. Phase transform: `R(f) / |R(f) + ε|`
4. IFFT → peak position = TDOA in samples
5. Sub-sample refinement via interpolation factor (16×)

**TDOA → Angle (circular array, least-squares):**
- Build overdetermined system from all 6 mic pairs
- Solve `A @ [sin(θ), cos(θ)]ᵀ = b` via `numpy.linalg.lstsq`
- `θ = atan2(sin_θ, cos_θ)` for full 360° coverage

**Performance budget:** ~1.2ms for all 6 pairs on Pi 5 — well within real-time constraints.

**Graceful degradation:**
- If `channels < 2` in config: module returns `None` for angle, UI hides compass
- If GCC-PHAT peak confidence < threshold: report "No directional data" instead of noisy angle

**Pre-configured geometries:**
```python
RESPEAKER_4MIC = np.array([
    [ 0.035,  0.000],  # Mic 0: East
    [ 0.000,  0.035],  # Mic 1: North
    [-0.035,  0.000],  # Mic 2: West
    [ 0.000, -0.035],  # Mic 3: South
])  # 70mm diameter circular array
```

---

### Component 5: Database (`database.py`)

#### [NEW] [database.py](file:///c:/Users/vacha/code/pi_birdie/database.py)

Responsibilities:
- Initialize SQLite database with schema on first run
- Log all bird detections with full metadata
- Run 2-month retention sweep on startup (delete old records + their audio files)
- Provide query methods for UI (recent detections, species counts, etc.)
- Mark records as synced after CSV export
- Handle database corruption gracefully (backup + recreate)

**Schema:**
```sql
CREATE TABLE IF NOT EXISTS detections (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       DATETIME NOT NULL DEFAULT (datetime('now', 'localtime')),
    species_common_name    TEXT NOT NULL,
    species_scientific_name TEXT NOT NULL,
    confidence_score       REAL NOT NULL,
    latitude        REAL,
    longitude       REAL,
    audio_sample_path TEXT,
    doa_angle       REAL,
    is_synced       INTEGER NOT NULL DEFAULT 0,
    created_at      DATETIME NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS idx_detections_timestamp ON detections(timestamp);
CREATE INDEX IF NOT EXISTS idx_detections_synced ON detections(is_synced);
CREATE INDEX IF NOT EXISTS idx_detections_species ON detections(species_common_name);
```

**Retention policy (runs on startup):**
```sql
DELETE FROM detections
WHERE julianday('now', 'localtime') - julianday(timestamp) > 60;
```
- Before deletion: collect `audio_sample_path` values and delete corresponding files from disk
- Wrapped in a transaction for atomicity
- Logs number of purged records

**Thread safety:**
- Uses `check_same_thread=False` with a `threading.Lock` for all write operations
- Read operations are safe without locking in WAL mode

---

### Component 6: User Interface (`ui.py`)

#### [NEW] [ui.py](file:///c:/Users/vacha/code/pi_birdie/ui.py)

Responsibilities:
- Build a responsive CustomTkinter UI optimized for small screens (800×480 SPI or 1024×600 HDMI)
- Display real-time audio waveform or spectrogram (matplotlib embedded via `FigureCanvasTkAgg`)
- Show a compass/arrow DoA indicator (Canvas-drawn, animated)
- "Last Identified" panel with species name, confidence bar, timestamp, and bird image
- Sync status indicator (Online/Offline badge + Export button)
- Recent detections list (scrollable table)
- Adapt layout based on `OPERATION_MODE`: fullscreen kiosk vs. windowed with decorations

**UI Layout (4-panel grid):**

```
┌──────────────────────────────────────────────────┐
│                  pi_birdie 🐦                     │
├────────────────────────┬─────────────────────────┤
│                        │   Last Identified        │
│   Audio Spectrogram    │   ┌─────────────┐       │
│   (real-time, 3-sec    │   │  [Bird Img]  │       │
│    scrolling FFT)      │   └─────────────┘       │
│                        │   House Finch            │
│                        │   Confidence: ██████ 87% │
│                        │   12:34:05 PM            │
├────────────────────────┤                         │
│   DoA Compass          │   Sync: 🟢 Online       │
│   ┌─────────┐          │   [Export to eBird]      │
│   │    N    │          ├─────────────────────────┤
│   │  ← ● → │ 45° NE   │   Recent Detections     │
│   │    S    │          │   ┌───┬───────┬───┬──┐  │
│   └─────────┘          │   │ # │Species│ % │⏰│  │
│                        │   ├───┼───────┼───┼──┤  │
│                        │   │ 1 │Finch  │87%│..│  │
│                        │   │ 2 │Robin  │72%│..│  │
└────────────────────────┴───┴───┴───────┴───┴──┘
```

**Key implementation details:**
- **Spectrogram:** Uses `numpy.fft.rfft` on rolling audio buffer, rendered as a color-mapped image on a `CTkCanvas` or embedded matplotlib figure. Updates every ~100ms via `after()` timer.
- **Compass:** Custom-drawn on a `CTkCanvas` with a rotating arrow. Smoothed with exponential moving average to avoid jitter. Cardinal direction labels (N/S/E/W) static, arrow animates.
- **Bird images:** Loaded from `./bird_images/{scientific_name}.jpg`. Falls back to a generic bird silhouette placeholder if not found.
- **Dark theme:** CustomTkinter `set_appearance_mode("dark")` with a custom color palette (deep navy background, teal accents, warm amber highlights).
- **Kiosk mode:** `root.attributes('-fullscreen', True)`, cursor hidden, no window decorations.
- **Thread-safe updates:** UI updates dispatched via `root.after(0, callback)` from worker threads.

---

### Component 7: Sync Service (`sync_service.py`)

#### [NEW] [sync_service.py](file:///c:/Users/vacha/code/pi_birdie/sync_service.py)

Responsibilities:
- Monitor network connectivity (periodic ping to `8.8.8.8` or DNS resolution check)
- Export unsynced detections to eBird Record Format CSV files
- Validate species names against eBird taxonomy (via read-only API `GET /ref/taxonomy/ebird`)
- Group observations into checklists by date + location
- Mark exported records as `is_synced = 1` in the database
- Notify the user via UI that a CSV is ready for upload
- Cache eBird taxonomy locally to reduce API calls

**eBird Record Format CSV columns** (exact order, no header):
1. Common Name
2. Genus (empty)
3. Species (empty)
4. Number ("X" for presence)
5. Comments (confidence score)
6. Location Name
7. Latitude
8. Longitude
9. Date (MM/DD/YYYY)
10. Start Time (HH:MM)
11. State/Province
12. Country Code
13. Protocol
14. Number of Observers
15. Duration (minutes)
16. All Observations Reported? (Y/N)
17. Distance Covered (empty for Stationary)
18. Area Covered (empty)
19. Checklist Comments

**Connectivity check (non-blocking):**
```python
def check_connectivity():
    try:
        socket.create_connection(("8.8.8.8", 53), timeout=3)
        return True
    except OSError:
        return False
```

**Architecture:**
- Runs as a background thread with a configurable poll interval (default: 60 seconds)
- Exposes `export_now()` method callable from UI button
- Downloads and caches eBird taxonomy on first successful connectivity check
- CSV files saved to `config.ebird.export_directory` with timestamped filenames

---

### Component 8: Dependencies (`requirements.txt`)

#### [NEW] [requirements.txt](file:///c:/Users/vacha/code/pi_birdie/requirements.txt)

```
# Core ML / Audio
birdnetlib>=0.7.0
numpy<2.0
scipy>=1.10.0
sounddevice>=0.4.6

# UI
customtkinter>=5.2.0
matplotlib>=3.7.0
Pillow>=10.0.0

# Database
# (sqlite3 is stdlib — no pip dependency)

# Networking / Sync
requests>=2.28.0

# TFLite Runtime (install ONE — ai-edge-litert preferred for Pi 5)
# ai-edge-litert     # Preferred for Pi 5 + Python 3.11+
# tflite-runtime     # Fallback if ai-edge-litert unavailable

# System deps (install via apt, not pip):
# sudo apt install ffmpeg libsndfile1 libasound2-dev
```

> [!NOTE]
> `ai-edge-litert` or `tflite-runtime` must be installed separately — they are platform-specific and not reliably installable from a generic `requirements.txt`. The `install.sh` script handles this with fallback logic.

---

### Component 9: Installation Script (`scripts/install.sh`)

#### [NEW] [scripts/install.sh](file:///c:/Users/vacha/code/pi_birdie/scripts/install.sh)

Steps:
1. Update system packages (`sudo apt update && sudo apt upgrade -y`)
2. Install system dependencies: `ffmpeg`, `libsndfile1`, `libasound2-dev`, `python3-venv`, `python3-dev`
3. Create Python venv at `~/pi_birdie_env/`
4. Activate venv and install pip dependencies from `requirements.txt`
5. Attempt `pip install ai-edge-litert`; if fail, attempt `pip install tflite-runtime`; if both fail, warn user
6. Generate `pi_birdie.service` systemd unit file dynamically (using current `$USER` and `$PWD`)
7. Copy service file to `/etc/systemd/system/` and enable it
8. Create necessary directories (`audio_samples/`, `exports/`, `bird_images/`)
9. Print success message with next steps

**Systemd service template:**
```ini
[Unit]
Description=pi_birdie Bird Identification Service
After=network.target sound.target

[Service]
Type=simple
User={USER}
WorkingDirectory={PROJECT_DIR}
ExecStart={VENV_DIR}/bin/python main.py --mode backyard
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

---

### Component 10: Service Management Scripts

#### [NEW] [scripts/start_backyard.sh](file:///c:/Users/vacha/code/pi_birdie/scripts/start_backyard.sh)
```bash
sudo systemctl start pi_birdie.service
sudo systemctl status pi_birdie.service
```

#### [NEW] [scripts/stop_backyard.sh](file:///c:/Users/vacha/code/pi_birdie/scripts/stop_backyard.sh)
```bash
sudo systemctl stop pi_birdie.service
echo "pi_birdie service stopped. Mic and system resources released."
```

#### [NEW] [scripts/restart_backyard.sh](file:///c:/Users/vacha/code/pi_birdie/scripts/restart_backyard.sh)
```bash
sudo systemctl restart pi_birdie.service
sudo systemctl status pi_birdie.service
```

---

### Component 11: Documentation

#### [NEW] [docs/README.md](file:///c:/Users/vacha/code/pi_birdie/docs/README.md)

Contents:
- Project overview and features
- Hardware requirements (Pi 5, microphone options, display options)
- Software prerequisites (OS, Python version, system packages)
- Installation walkthrough (clone → run `install.sh` → configure `config.json`)
- Configuration reference (`config.json` field-by-field documentation)
- Usage: on_demand mode (launch from terminal) vs. backyard mode (systemd service)
- eBird integration guide (API token setup, CSV export workflow, manual upload steps)
- ReSpeaker mic array setup (driver installation, ALSA config, channel verification)
- Troubleshooting (common issues: no mic detected, TFLite import errors, no detections)
- Architecture diagram (module dependency graph)

#### [NEW] [docs/AGENTS.md](file:///c:/Users/vacha/code/pi_birdie/docs/AGENTS.md)

Contents:
- Codebase layout summary (module → responsibility map)
- Module dependency graph (which modules import which)
- State management: shared queues, threading model, lock usage
- Critical invariants:
  - BirdNET expects 48kHz mono float32 audio
  - GCC-PHAT expects multi-channel audio at the configured sample rate
  - SQLite writes are single-threaded via lock
  - UI updates must go through `root.after()`
- Rules for future modifications:
  - Never block the audio callback thread
  - Always use `RecordingBuffer` (not `Recording`) for stream analysis
  - The 3-second chunk size is model-hardcoded — do not change
  - Keep `config.json` backward-compatible (add fields, don't remove)

---

## Verification Plan

### Automated Tests

Since this is a hardware-dependent Pi application, automated testing on the development machine is limited. However:

```bash
# Syntax and import verification
python -m py_compile main.py
python -m py_compile audio_processor.py
python -m py_compile doa_locator.py
python -m py_compile database.py
python -m py_compile ui.py
python -m py_compile sync_service.py

# Config validation
python -c "import json; json.load(open('config.json'))"

# Shell script syntax
bash -n scripts/install.sh
bash -n scripts/start_backyard.sh
bash -n scripts/stop_backyard.sh
bash -n scripts/restart_backyard.sh
```

### Manual Verification

1. **Config loading:** Run `python main.py --help` and verify it parses config without errors
2. **Database:** Verify table creation, insertion, retention sweep, and query methods
3. **UI launch:** Run `python main.py --mode on_demand` on a machine with a display — verify the window opens with all panels
4. **Code review:** Walk through each module for completeness, error handling, and adherence to the prompt requirements
5. **On Raspberry Pi 5 (user-performed):**
   - Run `install.sh` and verify venv + deps install
   - Connect USB mic, run in `on_demand` mode, verify audio capture + BirdNET analysis
   - If ReSpeaker available: verify multi-channel capture + DoA compass
   - Test `export_now()` and verify CSV format matches eBird Record Format
   - Test `backyard` mode via systemd: start, verify journald logs, stop
