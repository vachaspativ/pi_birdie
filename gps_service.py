"""
gps_service.py — GPS Integration for pi_birdie

Connects to the gpsd daemon to obtain live GNSS position fixes.
Automatically falls back to static coordinates from config.yaml when
gpsd is unavailable or has no fix.

GPS Status States:
    "live_3d"          - Full 3D fix from gpsd (lat, lon, altitude)
    "live_2d"          - 2D fix from gpsd (lat, lon, no altitude)
    "no_fix"           - gpsd connected but waiting for satellite fix
    "gpsd_unavailable" - Cannot connect to gpsd daemon
    "gps_disabled"     - gps.enabled is false in config.yaml

Key invariants (for AI agents):
    - get_position() ALWAYS returns a valid (lat, lon) tuple — never None or raises
    - get_status() ALWAYS returns one of the five status strings above
    - status callbacks are invoked from the GPS background thread — UI must use
      root.after() to marshal updates to the Tk mainloop
    - The service reconnects automatically if the gpsd connection drops
"""

import logging
import threading
import time
from typing import Callable, Optional, Tuple

logger = logging.getLogger(__name__)

# Status constants — use these instead of raw strings
GPS_STATUS_LIVE_3D = "live_3d"
GPS_STATUS_LIVE_2D = "live_2d"
GPS_STATUS_NO_FIX = "no_fix"
GPS_STATUS_UNAVAILABLE = "gpsd_unavailable"
GPS_STATUS_DISABLED = "gps_disabled"

# Human-readable labels and badge colours for the UI
GPS_STATUS_UI = {
    GPS_STATUS_LIVE_3D:      ("🛰 Live GPS (3D Fix)",         "#4CAF50"),  # green
    GPS_STATUS_LIVE_2D:      ("🛰 Live GPS (2D Fix)",         "#8BC34A"),  # light-green
    GPS_STATUS_NO_FIX:       ("⏳ Acquiring GPS…",            "#FFC107"),  # amber
    GPS_STATUS_UNAVAILABLE:  ("⚠ Offline GPS",               "#FF5722"),  # orange-red
    GPS_STATUS_DISABLED:     ("📍 Static Location",           "#9E9E9E"),  # grey
}


class GPSService:
    """
    Background GPS service that streams position from gpsd and falls back
    to static config.yaml coordinates when necessary.
    """

    _RECONNECT_DELAY_S = 10     # Seconds between gpsd reconnection attempts
    _POLL_INTERVAL_S = 1.0      # Seconds between gpsd poll cycles

    def __init__(self, config: dict):
        """
        Args:
            config: Parsed config.yaml as a dict (full document).
        """
        self._cfg = config
        self._gps_cfg = config.get("gps", {})
        self._loc_cfg = config.get("location", {})

        self._enabled: bool = self._gps_cfg.get("enabled", True)
        self._host: str = self._gps_cfg.get("gpsd_host", "localhost")
        self._port: int = int(self._gps_cfg.get("gpsd_port", 2947))
        self._fallback: bool = self._gps_cfg.get("fallback_to_config", True)

        # Fallback coordinates from config
        self._fallback_lat: float = float(self._loc_cfg.get("latitude", 0.0))
        self._fallback_lon: float = float(self._loc_cfg.get("longitude", 0.0))

        # Runtime state (guarded by _lock for reads from outside the GPS thread)
        self._lock = threading.Lock()
        self._lat: float = self._fallback_lat
        self._lon: float = self._fallback_lon
        self._status: str = GPS_STATUS_DISABLED if not self._enabled else GPS_STATUS_UNAVAILABLE

        # Callbacks (called from background thread — UI must marshal via root.after)
        self._status_cb: Optional[Callable[[str], None]] = None
        self._position_cb: Optional[Callable[[float, float], None]] = None

        # Threading
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    # ── Public API ────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the background GPS polling thread."""
        if not self._enabled:
            logger.info("GPS disabled in config — using static coordinates.")
            self._set_status(GPS_STATUS_DISABLED)
            return

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._gps_loop,
            name="gps-service",
            daemon=True,
        )
        self._thread.start()
        logger.info("GPS service started (gpsd @ %s:%d).", self._host, self._port)

    def stop(self) -> None:
        """Signal the background thread to stop and wait for it."""
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)
        logger.info("GPS service stopped.")

    def get_position(self) -> Tuple[float, float]:
        """
        Return the best available (latitude, longitude).
        ALWAYS returns valid coordinates — never raises.
        Falls back to static config values when GPS is unavailable.
        """
        with self._lock:
            return self._lat, self._lon

    def get_status(self) -> str:
        """Return the current GPS status string (one of the GPS_STATUS_* constants)."""
        with self._lock:
            return self._status

    def set_status_callback(self, callback: Callable[[str], None]) -> None:
        """Register a callback invoked whenever the GPS status changes."""
        self._status_cb = callback

    def set_position_callback(self, callback: Callable[[float, float], None]) -> None:
        """Register a callback invoked whenever a new position fix is received."""
        self._position_cb = callback

    def get_status_ui(self) -> Tuple[str, str]:
        """
        Return a (label_text, badge_color_hex) tuple suitable for the UI status badge.
        Always returns a valid pair.
        """
        return GPS_STATUS_UI.get(self.get_status(), ("Unknown GPS", "#9E9E9E"))

    # ── Background Thread ─────────────────────────────────────────────────────

    def _gps_loop(self) -> None:
        """Main GPS polling loop — reconnects automatically on errors."""
        while not self._stop_event.is_set():
            try:
                self._connect_and_poll()
            except Exception as exc:  # noqa: BLE001
                logger.warning("gpsd connection lost: %s. Retrying in %ds…",
                               exc, self._RECONNECT_DELAY_S)
                self._set_status(GPS_STATUS_UNAVAILABLE)
                if self._fallback:
                    self._reset_to_fallback()
                self._stop_event.wait(timeout=self._RECONNECT_DELAY_S)

    def _connect_and_poll(self) -> None:
        """
        Attempt to connect to gpsd and stream fixes.
        Raises on connection failure so _gps_loop can retry.
        """
        try:
            import gps as gpsd_module  # type: ignore[import]
        except ImportError:
            logger.error(
                "gps Python module not found. Install with: pip install gps\n"
                "Or: sudo apt install python3-gps"
            )
            self._set_status(GPS_STATUS_UNAVAILABLE)
            self._stop_event.wait(timeout=self._RECONNECT_DELAY_S * 6)
            return

        logger.info("Connecting to gpsd at %s:%d…", self._host, self._port)
        session = gpsd_module.gps(
            host=self._host,
            port=self._port,
            mode=gpsd_module.WATCH_ENABLE | gpsd_module.WATCH_NEWSTYLE,
        )
        self._set_status(GPS_STATUS_NO_FIX)
        logger.info("Connected to gpsd. Waiting for fix…")

        while not self._stop_event.is_set():
            report = session.next()
            if report["class"] == "TPV":
                self._handle_tpv(report)
            time.sleep(self._POLL_INTERVAL_S * 0.1)   # Tight loop inside poll

    def _handle_tpv(self, report) -> None:  # noqa: ANN001
        """Process a gpsd TPV (time-position-velocity) report."""
        fix_mode = getattr(report, "mode", 0)   # 0=no data, 1=no fix, 2=2D, 3=3D

        if fix_mode < 2:
            # No fix yet — keep previous coordinates or fallback
            self._set_status(GPS_STATUS_NO_FIX)
            return

        lat = getattr(report, "lat", None)
        lon = getattr(report, "lon", None)
        if lat is None or lon is None:
            self._set_status(GPS_STATUS_NO_FIX)
            return

        new_status = GPS_STATUS_LIVE_3D if fix_mode >= 3 else GPS_STATUS_LIVE_2D

        with self._lock:
            self._lat = float(lat)
            self._lon = float(lon)
            old_status = self._status
            self._status = new_status

        if new_status != old_status:
            logger.info("GPS fix acquired: %.4f, %.4f (mode=%s)", lat, lon, new_status)
            if self._status_cb:
                self._status_cb(new_status)

        if self._position_cb:
            self._position_cb(float(lat), float(lon))

    def _set_status(self, status: str) -> None:
        """Update status and fire callback if it changed."""
        with self._lock:
            if self._status == status:
                return
            self._status = status
        logger.debug("GPS status → %s", status)
        if self._status_cb:
            try:
                self._status_cb(status)
            except Exception as exc:  # noqa: BLE001
                logger.warning("GPS status callback raised: %s", exc)

    def _reset_to_fallback(self) -> None:
        """Reset coordinates to config.yaml fallback values."""
        with self._lock:
            self._lat = self._fallback_lat
            self._lon = self._fallback_lon
        if self._position_cb:
            try:
                self._position_cb(self._fallback_lat, self._fallback_lon)
            except Exception as exc:  # noqa: BLE001
                logger.warning("GPS position callback raised: %s", exc)
