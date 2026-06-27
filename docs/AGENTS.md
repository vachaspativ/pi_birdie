# AGENTS.md — Context for AI Agents Modifying pi_birdie

This document provides essential context for AI agents (Antigravity, GitHub Copilot, etc.)
working on this codebase. Read this file before making any modifications.

---

## Module Map

| Module | Responsibility | Imports From |
|---|---|---|
| `main.py` | Entrypoint, service orchestration, signal handling | All modules |
| `config.yaml` | All configuration — loaded once at startup | — |
| `database.py` | SQLite schema, detection logging, retention sweep | stdlib only |
| `gps_service.py` | gpsd live GPS, fallback to config.yaml | stdlib, gps |
| `doa_locator.py` | GCC-PHAT TDOA → DoA angle (numpy) | numpy, scipy |
| `rarity_service.py` | eBird notable + spplist caching, O(1) rarity lookup | requests |
| `audio_processor.py` | sounddevice capture, BirdNET analysis, DoA feeding | birdnetlib, sounddevice, numpy, doa_locator |
| `sync_service.py` | Connectivity monitoring, eBird CSV export | requests, database |
| `ui.py` | CustomTkinter adaptive UI, spectrogram, compass | customtkinter, matplotlib, PIL, gps_service |

---

## Threading Model

```
Main Thread (Tk mainloop)
├── ui.py — ALL widget mutations happen here
│   └── Timer callbacks: _tick_spectrogram, _tick_clock, _tick_compass
│
├── gps-service thread (daemon)
│   └── gpsd poll loop → status_callback → ui._schedule()
│
├── birdnet-analysis thread (daemon)
│   └── RecordingBuffer.analyze() → detection_callback → main._on_detection()
│
├── doa-estimation thread (daemon)
│   └── DOALocator.estimate_doa() → doa_callback → ui._schedule()
│
├── sync-service thread (daemon)
│   └── Connectivity check → online_callback → ui._schedule()
│
└── rarity-refresh thread (daemon)
    └── eBird API refresh (hourly wake) → sets in-memory cache sets
```

**Rule: NEVER call Tk widget methods from any non-main thread.**
Use `ui._schedule(lambda: ...)` to safely marshal updates to the main thread.

---

## Critical Invariants

### Audio Processing
- **BirdNET requires exactly 3-second segments at 48000 Hz mono float32.**
  `chunk_duration_sec: 3.0` in config.yaml is model-hardcoded — changing it breaks inference.
- Use `RecordingBuffer` (not `Recording`) for real-time stream analysis.
  `Recording` is for file paths only; `RecordingBuffer` accepts numpy arrays.
- The audio callback (`_audio_callback`) must NEVER block.
  All heavy work (BirdNET, DoA) is queued to worker threads.

### GPS Service
- `GPSService.get_position()` ALWAYS returns a valid `(lat, lon)` tuple.
  It never raises and never returns None — falls back to config.yaml coordinates.
- `GPSService.get_status()` ALWAYS returns one of the five GPS_STATUS_* constants.

### Rarity Service
- `RarityService.get_rarity()` ALWAYS returns a `RarityResult` — never raises.
  Returns `RarityResult("Unknown", ...)` when cache is not yet populated.
- Per-detection rarity lookup is O(1) — Python `set.__contains__` only.
  No API calls happen at detection time. API calls are in the background refresh thread.

### Database
- All write operations are protected by `self._lock` (threading.Lock).
- SQLite WAL mode is enabled — reads are safe without the lock.
- `log_detection()` always returns a positive integer id — never None.
- The retention sweep runs at startup in `initialize()` — it is idempotent.

### DoA Locator
- `estimate_doa()` NEVER raises — returns `(None, 0.0)` on any error.
- Input `audio_block` shape must be `(n_samples, n_channels)` — 2D array.
- DoA capability is detected ONCE at `AudioProcessor.start()` via device query.
  Do not re-query device capabilities at runtime.

### UI
- `ui._schedule(fn)` is the only safe way to update widgets from other threads.
- Bird images are stored in `self._image_cache` to prevent PIL garbage collection.
  Removing images from the cache will cause them to disappear from the UI.
- The compass uses exponential moving average smoothing (alpha=0.12).
  Changing alpha significantly will affect responsiveness vs. jitter tradeoff.

---

## Configuration Contract

- `config.yaml` is loaded ONCE at startup in `main.py`. There is no live-reload.
  Restart the application after any config change.
- Config values should always be accessed via `.get()` with defaults to maintain
  backward compatibility. Never assume a key exists.
- The `audio.channels` value controls DoA capability. The `doa.enabled` flag is
  a second gate — both must be true for DoA to activate.
- `ui.display_mode` affects widget sizes but NOT widget functionality.

---

## Data Flow

```
sounddevice.InputStream callback
    → mono_buffer (deque, 3s)     → RecordingBuffer.analyze()
    → mc_buffer   (deque, 3s)     → DOALocator.estimate_doa()
                                            │
                              ┌─────────────┤
                              │             │
                    detection event     doa event (angle, conf)
                              │             │
                    main._on_detection()    ui.on_doa_update()
                              │                    │
                    rarity_service.get_rarity()    ui._schedule() → compass
                              │
                    database.log_detection()
                              │
                    ui.on_detection() → ui._schedule() → card + table update
```

---

## Rules for Future Modifications

1. **Do not change `chunk_duration_sec` or `sample_rate`.**
   These are hardcoded in the BirdNET model architecture.

2. **Do not add blocking calls to the audio callback.**
   The callback must return within the block duration (~85ms at default blocksize).

3. **Do not call Tk widget methods from background threads.**
   Always use `ui._schedule()`.

4. **Do not remove the `is_synced` flag from the database schema.**
   It is used by `sync_service.py` to prevent duplicate eBird exports.

5. **Keep `get_position()` and `get_rarity()` infallible.**
   Both methods must always return valid data and never raise.

6. **Config.yaml changes should be additive — never remove existing keys.**
   Old config files must remain valid after software updates.

7. **Do not scrape eBird bar chart data per detection.**
   The rarity system uses two bulk cached API calls only. Per-detection API
   calls would create unacceptable overhead.

8. **The DoA layout decision is made once at startup.**
   `audio_proc.is_doa_capable()` is called in `_start_services()` and passed
   to `ui.set_doa_capable()` before `ui.setup()`. The layout is then built
   once. Do not attempt to toggle the layout at runtime.

---

## Adding a New Feature

### New detection metadata field
1. Add column to `database.py` → `_CREATE_DETECTIONS` SQL
2. Add parameter to `database.log_detection()` signature
3. Add to the detection event dict in `audio_processor._handle_detection()`
4. Populate in `main._on_detection()`
5. Display in `ui._handle_detection()` and `_refresh_detection_list()`
6. Update this AGENTS.md

### New UI panel
1. Add build method `_build_X_panel()` in `ui.py`
2. Call it from `_build_layout_with_doa()` AND `_build_layout_no_doa()`
3. Add update method `_update_X()` and wire via `_schedule()`
4. Add thread-safe callback if driven by a service

### New service
1. Follow the start()/stop()/set_*_callback() pattern
2. Make all public methods infallible (try/except with logging)
3. Use daemon threads
4. Register in `main._init_services()`, `_wire_callbacks()`, `_start_services()`, `_shutdown()`
5. Add to this module map

---

## File Locations

| Purpose | Path |
|---|---|
| Configuration | `./config.yaml` |
| Database | `./pi_birdie.db` |
| Audio clips | `./audio_samples/YYYYMMDD_HHMMSS_{Species}.wav` |
| eBird exports | `./exports/pi_birdie_ebird_YYYYMMDD_HHMMSS.csv` |
| Rarity cache | `./data/spplist_{region}.json`, `./data/notable_{region}.json` |
| Bird images | `./bird_images/{Scientific_Name}.jpg` |
| Image manifest | `./bird_images/manifest.json` |
| Systemd service | `/etc/systemd/system/pi_birdie.service` |
