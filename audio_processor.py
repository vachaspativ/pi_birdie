"""
audio_processor.py — Audio Capture and BirdNET Classification for pi_birdie

Manages:
    1. Continuous audio capture from microphone via sounddevice.InputStream
    2. Auto-detection of microphone device and DoA capability
    3. Rolling 3-second buffer feeding birdnetlib.RecordingBuffer (BirdNET analysis)
    4. Multi-channel buffer feeding DOALocator (direction of arrival)
    5. Detection events dispatched via callback to database / UI layers
    6. 3-second WAV clip saving for each detection

Key invariants (for AI agents):
    - The sounddevice callback NEVER blocks — all heavy work is queued
    - BirdNET analysis runs in a separate thread (analysis_thread)
    - DoA estimation runs in a separate thread (doa_thread)
    - is_doa_capable() result is determined once at start() and does not change
    - Detection callback dict keys:
        timestamp, common_name, scientific_name, confidence,
        audio_sample_path, doa_angle, lat, lon
    - get_mono_buffer() returns a copy — safe to use from UI thread
    - BirdNET RecordingBuffer expects: mono, float32, 48000 Hz
"""

import collections
import logging
import queue
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional, Tuple

import numpy as np
import sounddevice as sd

logger = logging.getLogger(__name__)

# Type alias for detection callback
DetectionCallback = Callable[[dict], None]
DOACallback       = Callable[[float, float], None]  # (angle_deg, confidence)


class AudioProcessor:
    """
    Continuous audio capture, BirdNET classification, and DoA estimation.
    """

    def __init__(
        self,
        config: dict,
        analyzer,                          # birdnetlib.analyzer.Analyzer instance
        doa_locator=None,                  # DOALocator | None
        gps_service=None,                  # GPSService | None
    ):
        """
        Args:
            config:      Full parsed config.yaml dict.
            analyzer:    Pre-initialised birdnetlib Analyzer (loaded once in main.py).
            doa_locator: DOALocator instance, or None to disable DoA.
            gps_service: GPSService instance for position tagging, or None.
        """
        self._config      = config
        self._analyzer    = analyzer
        self._doa_locator = doa_locator
        self._gps_service = gps_service

        audio_cfg = config.get("audio", {})
        self._fs:            int   = int(audio_cfg.get("sample_rate", 48000))
        self._channels:      int   = int(audio_cfg.get("channels", 1))
        self._chunk_sec:     float = float(audio_cfg.get("chunk_duration_sec", 3.0))
        self._min_conf:      float = float(audio_cfg.get("min_confidence", 0.25))
        self._sensitivity:   float = float(audio_cfg.get("sensitivity", 1.0))
        self._save_samples:  bool  = bool(audio_cfg.get("save_audio_samples", True))
        self._samples_dir:   Path  = Path(audio_cfg.get("audio_samples_dir", "./audio_samples"))
        self._device_name:   Optional[str] = audio_cfg.get("device_name") or None

        loc_cfg = config.get("location", {})
        self._fallback_lat: float = float(loc_cfg.get("latitude", 0.0))
        self._fallback_lon: float = float(loc_cfg.get("longitude", 0.0))

        # Chunk size in samples
        self._chunk_samples: int = int(self._fs * self._chunk_sec)

        # Rolling mono audio buffer (3 seconds at 48kHz = 144000 samples)
        self._mono_buffer: collections.deque = collections.deque(
            maxlen=self._chunk_samples
        )
        self._buffer_lock = threading.Lock()

        # Multi-channel buffer for DoA (same length as mono buffer)
        self._mc_buffer: collections.deque = collections.deque(
            maxlen=self._chunk_samples
        )

        # Queues feeding worker threads
        self._analysis_queue: queue.Queue = queue.Queue(maxsize=4)
        self._doa_queue:       queue.Queue = queue.Queue(maxsize=4)

        # Callbacks (registered by main.py / UI)
        self._detection_cb: Optional[DetectionCallback] = None
        self._doa_cb:       Optional[DOACallback]       = None

        # Runtime state
        self._stream:          Optional[sd.InputStream] = None
        self._analysis_thread: Optional[threading.Thread] = None
        self._doa_thread:      Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._doa_capable: bool = False   # Set by _detect_doa_capability() in start()

        # Smoothed spectrogram buffer exposed to UI
        self._spec_buffer: np.ndarray = np.zeros(self._chunk_samples // 2 + 1)
        self._spec_lock = threading.Lock()

        # Ensure audio sample directory exists
        self._samples_dir.mkdir(parents=True, exist_ok=True)

    # ── Public API ────────────────────────────────────────────────────────────

    def start(self) -> None:
        """
        Detect mic capability, open the audio stream, and start worker threads.
        Raises RuntimeError if no usable input device is found.
        """
        self._stop_event.clear()
        device_idx = self._select_device()
        self._doa_capable = self._detect_doa_capability(device_idx)

        logger.info(
            "Opening audio stream: device=%s, %d ch, %d Hz, DoA=%s",
            device_idx, self._channels, self._fs, self._doa_capable,
        )

        channels_to_open = self._channels if self._doa_capable else 1

        self._stream = sd.InputStream(
            device=device_idx,
            samplerate=self._fs,
            channels=channels_to_open,
            blocksize=4096,                # ~85ms blocks
            dtype="float32",
            callback=self._audio_callback,
        )

        # Start worker threads
        self._analysis_thread = threading.Thread(
            target=self._analysis_loop, name="birdnet-analysis", daemon=True
        )
        self._doa_thread = threading.Thread(
            target=self._doa_loop, name="doa-estimation", daemon=True
        )
        self._analysis_thread.start()
        self._doa_thread.start()

        self._stream.start()
        logger.info("Audio capture started.")

    def stop(self) -> None:
        """Stop audio capture and all worker threads cleanly."""
        self._stop_event.set()
        if self._stream:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Error closing audio stream: %s", exc)
            self._stream = None

        # Unblock queued threads with sentinel values
        self._analysis_queue.put(None)
        self._doa_queue.put(None)

        if self._analysis_thread:
            self._analysis_thread.join(timeout=5.0)
        if self._doa_thread:
            self._doa_thread.join(timeout=3.0)
        logger.info("Audio processor stopped.")

    def is_doa_capable(self) -> bool:
        """Return True if the connected microphone supports multi-channel DoA."""
        return self._doa_capable

    def get_channel_count(self) -> int:
        """Return the number of channels being captured."""
        return self._channels if self._doa_capable else 1

    def set_detection_callback(self, cb: DetectionCallback) -> None:
        """Register a callback invoked for each bird detection."""
        self._detection_cb = cb

    def set_doa_callback(self, cb: DOACallback) -> None:
        """Register a callback invoked for each DoA angle estimate."""
        self._doa_cb = cb

    def get_mono_buffer(self) -> np.ndarray:
        """
        Return a copy of the current mono audio buffer as a numpy array.
        Safe to call from the UI thread. Returns zeros if buffer is empty.
        """
        with self._buffer_lock:
            buf = np.array(self._mono_buffer, dtype=np.float32)
        return buf

    # ── Audio Callback (runs in sounddevice audio thread) ─────────────────────

    def _audio_callback(
        self, indata: np.ndarray, frames: int, time_info, status
    ) -> None:
        """
        sounddevice audio callback — called at ~85ms intervals.
        MUST NOT block. Appends to rolling buffers and queues work when 3s elapsed.
        """
        if status:
            logger.debug("Audio stream status: %s", status)

        # Extract mono (channel 0) for BirdNET
        mono = indata[:, 0].copy() if indata.ndim > 1 else indata[:, 0].copy()

        with self._buffer_lock:
            self._mono_buffer.extend(mono)

        # Multi-channel buffer for DoA
        if self._doa_capable and indata.ndim > 1 and indata.shape[1] >= 2:
            self._mc_buffer.extend(list(indata.copy()))   # list of (n_channels,) rows

        # Update spectrogram buffer
        if len(mono) > 0:
            fft_mag = np.abs(np.fft.rfft(mono, n=self._chunk_samples // 4))
            with self._spec_lock:
                self._spec_buffer = np.maximum(
                    self._spec_buffer * 0.85,   # Exponential decay
                    fft_mag[:len(self._spec_buffer)],
                )

        # When we have a full 3-second chunk, queue it for analysis
        if len(self._mono_buffer) >= self._chunk_samples:
            mono_chunk = np.array(list(self._mono_buffer), dtype=np.float32)
            try:
                self._analysis_queue.put_nowait(mono_chunk)
            except queue.Full:
                logger.debug("Analysis queue full — dropping chunk.")

            if self._doa_capable and len(self._mc_buffer) >= self._chunk_samples:
                mc_chunk = np.array(list(self._mc_buffer), dtype=np.float32)
                try:
                    self._doa_queue.put_nowait(mc_chunk)
                except queue.Full:
                    pass

            # Clear buffers for next 3-second window
            with self._buffer_lock:
                self._mono_buffer.clear()
            if self._doa_capable:
                self._mc_buffer.clear()

    # ── BirdNET Analysis Thread ────────────────────────────────────────────────

    def _analysis_loop(self) -> None:
        """Dequeue 3-second mono chunks and run BirdNET inference."""
        from birdnetlib import RecordingBuffer  # type: ignore[import]

        while not self._stop_event.is_set():
            try:
                chunk = self._analysis_queue.get(timeout=1.0)
            except queue.Empty:
                continue
            if chunk is None:   # Sentinel — exit signal
                break

            ts = datetime.now()
            lat, lon = self._get_position()

            try:
                rec = RecordingBuffer(
                    self._analyzer,
                    chunk,
                    sample_rate=self._fs,
                    lat=lat,
                    lon=lon,
                    date=ts,
                    min_conf=self._min_conf,
                    sensitivity=self._sensitivity,
                )
                rec.analyze()
                detections = rec.detections   # List[dict]
            except Exception as exc:  # noqa: BLE001
                logger.warning("BirdNET analysis error: %s", exc)
                continue

            for det in detections:
                self._handle_detection(det, chunk, ts, lat, lon)

    def _handle_detection(
        self,
        det:   dict,
        chunk: np.ndarray,
        ts:    datetime,
        lat:   float,
        lon:   float,
    ) -> None:
        """Process a single detection dict from BirdNET, save audio, fire callback."""
        common_name      = det.get("common_name", "Unknown")
        scientific_name  = det.get("scientific_name", "")
        confidence       = float(det.get("confidence", 0.0))

        audio_path: Optional[str] = None
        if self._save_samples:
            audio_path = self._save_audio_clip(chunk, ts, common_name)

        logger.info(
            "Detection: %s (%.0f%% conf) @ %.4f,%.4f",
            common_name, confidence * 100, lat, lon,
        )

        event = {
            "timestamp":        ts,
            "common_name":      common_name,
            "scientific_name":  scientific_name,
            "confidence":       confidence,
            "audio_sample_path": audio_path,
            "doa_angle":        None,   # Filled by DoA thread if available
            "lat":              lat,
            "lon":              lon,
        }

        if self._detection_cb:
            try:
                self._detection_cb(event)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Detection callback raised: %s", exc)

    # ── DoA Thread ────────────────────────────────────────────────────────────

    def _doa_loop(self) -> None:
        """Dequeue multi-channel chunks and run GCC-PHAT DoA estimation."""
        while not self._stop_event.is_set():
            try:
                mc_chunk = self._doa_queue.get(timeout=1.0)
            except queue.Empty:
                continue
            if mc_chunk is None:
                break

            if self._doa_locator is None:
                continue

            try:
                angle, confidence = self._doa_locator.estimate_doa(mc_chunk)
                if angle is not None and self._doa_cb:
                    self._doa_cb(angle, confidence)
            except Exception as exc:  # noqa: BLE001
                logger.debug("DoA estimation error (non-fatal): %s", exc)

    # ── Device Detection ──────────────────────────────────────────────────────

    def _select_device(self) -> Optional[int]:
        """
        Select the best input device.
        Priority: config device_name → ReSpeaker/ac108 → system default.
        Returns device index, or None for system default.
        """
        devices = sd.query_devices()

        if self._device_name:
            for idx, dev in enumerate(devices):
                if self._device_name.lower() in dev["name"].lower():
                    logger.info("Using configured device: %s (index %d)", dev["name"], idx)
                    return idx
            logger.warning("Configured device '%s' not found — using default.", self._device_name)

        # Auto-detect ReSpeaker or multi-channel mic
        for idx, dev in enumerate(devices):
            name = dev["name"].lower()
            if any(kw in name for kw in ("ac108", "seeed", "respeaker")):
                logger.info("Auto-detected ReSpeaker: %s (index %d)", dev["name"], idx)
                return idx

        logger.info("Using system default input device.")
        return None   # sounddevice uses default when None

    def _detect_doa_capability(self, device_idx: Optional[int]) -> bool:
        """Return True if the selected device has enough channels for DoA."""
        if self._doa_locator is None:
            return False
        if self._channels < 2:
            logger.info("DoA disabled: config audio.channels=%d (need ≥2).", self._channels)
            return False
        try:
            info = sd.query_devices(device_idx or sd.default.device[0])
            max_ch = int(info["max_input_channels"])
            if max_ch >= 2:
                logger.info("DoA capable: device has %d input channels.", max_ch)
                return True
            logger.info(
                "DoA disabled: device has only %d channel(s) (need ≥2).", max_ch
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not query device capabilities: %s", exc)
        return False

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _get_position(self) -> Tuple[float, float]:
        """Return current (lat, lon) from GPS service or config fallback."""
        if self._gps_service:
            try:
                return self._gps_service.get_position()
            except Exception:  # noqa: BLE001
                pass
        return self._fallback_lat, self._fallback_lon

    def _save_audio_clip(
        self, chunk: np.ndarray, ts: datetime, common_name: str
    ) -> Optional[str]:
        """Save a 3-second WAV clip to audio_samples_dir. Returns file path or None."""
        try:
            from scipy.io import wavfile  # type: ignore[import]

            safe_name = "".join(c if c.isalnum() or c in "_ " else "_" for c in common_name)
            safe_name = safe_name.replace(" ", "_")[:40]
            filename  = f"{ts.strftime('%Y%m%d_%H%M%S')}_{safe_name}.wav"
            filepath  = self._samples_dir / filename

            # Convert float32 [-1, 1] → int16 for WAV
            audio_int16 = (chunk * 32767).clip(-32768, 32767).astype(np.int16)
            wavfile.write(str(filepath), self._fs, audio_int16)
            return str(filepath)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not save audio clip: %s", exc)
            return None

    def get_spectrogram_buffer(self) -> np.ndarray:
        """Return a copy of the current spectrogram magnitude buffer."""
        with self._spec_lock:
            return self._spec_buffer.copy()
