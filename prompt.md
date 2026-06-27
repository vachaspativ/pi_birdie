System Role & Context:
You are an expert embedded systems developer and senior DevOps engineer specializing in Python-based edge AI, real-time audio DSP, and automation on Raspberry Pi 5 (running Raspberry Pi OS Bookworm 64-bit).

Project Overview:
Create a Python-based desktop application named "pi_birdie" for a Raspberry Pi 5. The primary purpose is to listen to environmental audio via an external microphone, identify bird species offline using Cornell Lab data, display results on a simple UI, track detection metadata in a local database, provide acoustic direction-of-arrival (DoA) guidance, and optionally sync data to eBird/Merlin when an internet connection becomes available.

Project Name: pi_birdie

Hardware Target:
- Raspberry Pi 5 (running Raspberry Pi OS Bookworm)
- External USB microphone or I2S microphone array (e.g., ReSpeaker)
- Local display (HDMI or SPI touchscreen)

Core Requirements & Architecture:

1. Offline Bird Identification Engine
- Implement an offline audio classification pipeline using the BirdNET-Lite or BirdNET-Go Python bindings (utilizing Cornell Lab’s pre-trained TFLite model).
- The system must process continuous audio streams from the external microphone in chunks (e.g., 3-second segments) entirely offline.
- Match identified species IDs with a locally stored repository of bird images and metadata (pre-downloaded Cornell/Wikipedia dataset).

2. Real-Time Direction of Arrival (DoA) Tracking
- Implement a sound localization module utilizing Generalized Cross-Correlation with Phase Transform (GCC-PHAT) or Time Difference of Arrival (TDOA).
- If a multi-channel microphone array is detected, calculate the approximate angle/direction of the bird call.
- Provide a visual compass or directional arrow on the UI to guide the user's gaze ("Look 45° Northeast").

3. Local Database & Retention Policy (SQLite)
- Use SQLite to log all detections.
- Database Schema: id (PK), timestamp (DATETIME), species_common_name, species_scientific_name, confidence_score, latitude, longitude (fallback to static config if offline without GPS), and audio_sample_path.
- Implement an automatic data retention script: Upon startup, run a maintenance query that deletes all records and associated audio files older than 2 months (`strftime('%s','now') - strftime('%s', timestamp) > 5184000`).

4. Simple Graphical User Interface (UI)
- Build a lightweight, responsive UI using Tkinter, CustomTkinter, or PyQt6, optimized for a small screen.
- UI Components:
  - Real-time audio waveform/spectrogram visualization.
  - Active visual Direction Indicator (Compass/Arrow).
  - "Last Identified" Panel: Shows the bird's common name, confidence percentage, timestamp, and local image.
  - "Sync Status" indicator (Online/Offline toggle).

5. Online Sync Module (eBird / Merlin API Integration)
- Implement a background network listener that detects internet connectivity.
- When online, allow the user to trigger a sync sequence that pushes the SQLite log history to eBird using the eBird API (or valid automated eBird Data Exchange formats). 
- Ensure synced records are marked as `is_synced = 1` in the database to prevent duplicate submissions.

Core Migration & Architectural Update:
- DEPRECATION NOTICE: The old BirdNET TFLite direct wrapper is deprecated. You MUST use the modern BirdNET-Analyzer engine. Implement the local classification pipeline using `birdnetlib` or a structured integration with the official `BirdNET-Analyzer` Python class bindings.
- DUAL-MODE EXECUTION: The app must toggle modes dynamically based on a config parameter (`config.json -> OPERATION_MODE`):
  1. "backyard": Headless or full-screen Kiosk mode, managed entirely via a systemd background service that auto-starts on boot with auto-crash recovery.
  2. "on_demand": A standard desktop application with standard window decorations (minimize/maximize/close buttons) launched manually by the user via terminal or desktop shortcut, releasing all system and mic assets cleanly upon exit.

---

Required Project Directory Structure:
The generated project must adhere strictly to this layout:
pi_birdie/
├── config.json # Operational profiles, eBird tokens, fallback coordinates
├── main.py # Application entrypoint parsing execution modes
├── audio_processor.py # PyAudio / SoundDevice chunking and BirdNET-Analyzer engine calls
├── doa_locator.py # Direction of Arrival (GCC-PHAT/TDOA) algorithm for multi-channel array
├── database.py # SQLite setup, logs, and a 2-month strict metadata retention filter
├── ui.py # CustomTkinter or PyQt6 lightweight interface with adaptive layouts
├── sync_service.py # Network observer to sync offline logs securely to eBird APIs when online
├── requirements.txt # Python library declarations (librosa, birdnetlib, pyaudio, numpy, etc.)
├── docs/
│ ├── README.md # End-user setup, architectural breakdown, and hardware schematic guidelines
│ └── AGENTS.md # Context documentation for AI agents modifying this project in the future
└── scripts/
    ├── install.sh # Universal installation script automating OS updates, apt-deps, and venv setup
    ├── start_backyard.sh # Script to trigger background/service mode 
    ├── stop_backyard.sh # Script to safely kill background loops
    └── restart_backyard.sh # Clean reload script for maintenance cycles

---

Detailed Component Specifications:

1. Offline Analytics & Localization
- Use `birdnetlib.analyzer.Analyzer` initialized with local BirdNET-Analyzer weights to run offline 3-second slice inferences.
- Parse acoustic multi-channel audio frames through a Generalized Cross-Correlation with Phase Transform (GCC-PHAT) function to compute a real-time relative degree/angle of direction to feed into the UI compass arrow.

2. SQLite Lifecycle & Data Retention
- Enforce data hygiene: run a maintenance hook on app startup that evaluates existing records and sweeps away any entry, including its locally written audio sample file, older than 2 months (`NOW - 60 days`).

3. System Automation Scripts
- 'install.sh' must handle: system package installation (`sudo apt install ffmpeg libasound2-dev`), Python 3.11+ virtual environment (`venv`) instantiation, PIP dependencies compilation, and linking a dynamically generated `pi_birdie.service` file to `/etc/systemd/system/`.

4. Documentation Artifacts (Crucial)
- Generate a comprehensive 'README.md' noting structural layouts, exact prerequisite installations (including requesting an eBird API token), configuration maps, and systemd manual registration protocols.
- Generate an 'AGENTS.md' summarizing the codebase layout, module dependencies, variable state contexts, and rules to prevent breaking the offline engine loops during future iterations.


Deliverables Required:
1. Directory Structure: A clean, modular project structure (e.g., `main.py`, `audio_processor.py`, `database.py`, `ui.py`, `sync_service.py`).
2. Requirements.txt: Complete list of dependencies (e.g., `tflite-runtime`, `pyaudio`, `numpy`, `scipy`, `customtkinter`).
3. Complete Source Code: Provide robust, well-commented Python code for each module, including error handling for missing microphone input or corrupted database files.
4. Provide the fully fleshed-out, robust Python codebase for all files, complete JSON templates, raw bash shell scripts for the `scripts/` directory, and the absolute markdown content for both documentation files. Ensure no code stubs or 'placeholders' are left behind.





