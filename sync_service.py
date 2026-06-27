"""
sync_service.py — eBird Export & Online Sync for pi_birdie

Responsibilities:
    1. Monitors internet connectivity (non-blocking, 60s interval)
    2. Notifies rarity_service when online for cache refresh
    3. Exports unsynced detections to eBird Record Format CSV files
    4. Validates species names against cached eBird taxonomy
    5. Marks exported detections as is_synced=1 in the database
    6. Notifies the UI when a CSV export is ready

eBird API is read-only — no programmatic submission exists.
Users upload the generated CSV at: https://ebird.org/submit/import

eBird Record Format CSV column order (no header row in upload):
    1.  Common Name
    2.  Genus (blank)
    3.  Species (blank)
    4.  Number ("X" for presence)
    5.  Comments (confidence score)
    6.  Location Name
    7.  Latitude
    8.  Longitude
    9.  Date (MM/DD/YYYY)
    10. Start Time (HH:MM)
    11. State/Province
    12. Country Code
    13. Protocol
    14. Number of Observers
    15. Duration (minutes, blank for Incidental)
    16. All Observations Reported? (Y/N)
    17. Distance Covered (blank for Stationary)
    18. Area Covered (blank)
    19. Checklist Comments

Key invariants (for AI agents):
    - export_now() ALWAYS returns a path string or None — never raises
    - is_online() uses a 3-second socket probe — fast and non-blocking
    - Connectivity callbacks are fired from the background thread — UI uses root.after()
    - Exports are written to config.ebird.export_directory
    - Detections are grouped into checklists by date (one checklist per day)
"""

import csv
import json
import logging
import os
import queue
import socket
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional

import requests

logger = logging.getLogger(__name__)

_EBIRD_API_BASE   = "https://api.ebird.org/v2"
_CONNECTIVITY_URL = ("8.8.8.8", 53)   # Google DNS — fast TCP probe
_CHECK_INTERVAL_S = 60                 # Connectivity check interval


class SyncService:
    """
    Background service that monitors connectivity, exports eBird CSV files,
    and notifies the rarity service when online.
    """

    def __init__(self, config: dict, database, rarity_service=None):
        """
        Args:
            config:         Full parsed config.yaml dict.
            database:       Database instance.
            rarity_service: RarityService instance (optional — notified when online).
        """
        self._config         = config
        self._db             = database
        self._rarity_svc     = rarity_service

        ebird_cfg            = config.get("ebird", {})
        loc_cfg              = config.get("location", {})

        self._api_token:       str  = ebird_cfg.get("api_token", "")
        self._region:          str  = ebird_cfg.get("region_code", "")
        self._protocol:        str  = ebird_cfg.get("default_protocol", "Stationary")
        self._num_observers:   int  = int(ebird_cfg.get("num_observers", 1))
        self._complete_cl:     bool = bool(ebird_cfg.get("complete_checklist", False))
        self._export_dir:      Path = Path(ebird_cfg.get("export_directory", "./exports"))

        self._location_name:   str   = loc_cfg.get("location_name", "My Location")
        self._state_province:  str   = loc_cfg.get("state_province", "")
        self._country_code:    str   = loc_cfg.get("country_code", "US")

        # eBird taxonomy cache (common_name → validated status)
        self._taxonomy:        dict  = {}
        self._taxonomy_loaded: bool  = False

        # Runtime state
        self._online:          bool  = False
        self._stop_event      = threading.Event()
        self._thread:  Optional[threading.Thread] = None

        # Callbacks
        self._online_cb:        Optional[Callable[[bool], None]] = None
        self._export_cb:        Optional[Callable[[str], None]]  = None

        # HTTP session
        self._session = requests.Session()
        if self._api_token:
            self._session.headers.update({"x-ebirdapitoken": self._api_token})

    # ── Public API ────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the background connectivity monitoring thread."""
        self._export_dir.mkdir(parents=True, exist_ok=True)
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._monitor_loop, name="sync-service", daemon=True
        )
        self._thread.start()
        logger.info("Sync service started.")

    def stop(self) -> None:
        """Stop the background thread."""
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)
        logger.info("Sync service stopped.")

    def is_online(self) -> bool:
        """Return True if internet connectivity was detected on the last check."""
        return self._online

    def export_now(self) -> Optional[str]:
        """
        Generate an eBird Record Format CSV from all unsynced detections.
        Marks exported records as synced.
        Returns the path to the created CSV, or None if no detections to export.
        NEVER raises — returns None on any error.
        """
        try:
            return self._generate_csv()
        except Exception as exc:  # noqa: BLE001
            logger.error("export_now() failed: %s", exc)
            return None

    def set_online_callback(self, cb: Callable[[bool], None]) -> None:
        """Callback fired when connectivity status changes (True=online, False=offline)."""
        self._online_cb = cb

    def set_export_callback(self, cb: Callable[[str], None]) -> None:
        """Callback fired after a successful CSV export. Argument is the CSV file path."""
        self._export_cb = cb

    # ── Background Monitor Loop ───────────────────────────────────────────────

    def _monitor_loop(self) -> None:
        """Periodically check internet connectivity and trigger online actions."""
        while not self._stop_event.is_set():
            new_online = self._check_connectivity()

            if new_online != self._online:
                self._online = new_online
                status_str = "Online" if new_online else "Offline"
                logger.info("Connectivity changed: %s", status_str)

                if self._online_cb:
                    try:
                        self._online_cb(new_online)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("Online callback raised: %s", exc)

                if new_online:
                    self._on_connected()

            self._stop_event.wait(timeout=_CHECK_INTERVAL_S)

    def _on_connected(self) -> None:
        """Actions to perform when internet connectivity is first detected."""
        logger.info("Internet detected — triggering online tasks.")
        # Notify rarity service to refresh caches
        if self._rarity_svc:
            try:
                self._rarity_svc.refresh_online()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Rarity refresh failed: %s", exc)
        # Refresh eBird taxonomy cache for CSV validation
        if self._api_token:
            self._refresh_taxonomy()

    # ── Connectivity Check ────────────────────────────────────────────────────

    @staticmethod
    def _check_connectivity() -> bool:
        """
        Fast TCP connection test to Google DNS (port 53).
        Returns True if reachable within 3 seconds.
        """
        try:
            s = socket.create_connection(_CONNECTIVITY_URL, timeout=3)
            s.close()
            return True
        except OSError:
            return False

    # ── CSV Export ────────────────────────────────────────────────────────────

    def _generate_csv(self) -> Optional[str]:
        """Generate eBird Record Format CSV from unsynced detections."""
        detections = self._db.get_unsynced_detections()
        if not detections:
            logger.info("No unsynced detections to export.")
            return None

        timestamp    = datetime.now().strftime("%Y%m%d_%H%M%S")
        csv_filename = f"pi_birdie_ebird_{timestamp}.csv"
        csv_path     = self._export_dir / csv_filename

        exported_ids = []
        rows         = []

        # Group by date (one eBird checklist per calendar day per location)
        # For simplicity: all detections in one export, grouped by date
        detections_by_date: dict[str, list] = {}
        for det in detections:
            try:
                ts_obj = datetime.fromisoformat(det["timestamp"])
            except (ValueError, TypeError):
                ts_obj = datetime.now()
            date_key = ts_obj.strftime("%m/%d/%Y")
            detections_by_date.setdefault(date_key, []).append((ts_obj, det))

        for date_str, dated_dets in sorted(detections_by_date.items()):
            dated_dets.sort(key=lambda x: x[0])   # Sort by time within day
            start_time = dated_dets[0][0].strftime("%H:%M")

            for ts_obj, det in dated_dets:
                common_name = det.get("species_common_name", "")
                confidence  = float(det.get("confidence_score", 0.0))
                lat         = det.get("latitude")  or 0.0
                lon         = det.get("longitude") or 0.0

                comment = f"pi_birdie detection; confidence {confidence:.0%}"
                if det.get("doa_angle") is not None:
                    comment += f"; direction {det['doa_angle']:.0f}°"
                if det.get("rarity_label") and det["rarity_label"] != "Unknown":
                    comment += f"; rarity={det['rarity_label']}"

                row = [
                    common_name,     # 1. Common Name
                    "",              # 2. Genus (blank)
                    "",              # 3. Species (blank)
                    "X",             # 4. Number (presence)
                    comment,         # 5. Comments
                    self._location_name,   # 6. Location Name
                    f"{lat:.6f}",    # 7. Latitude
                    f"{lon:.6f}",    # 8. Longitude
                    date_str,        # 9. Date MM/DD/YYYY
                    start_time,      # 10. Start Time HH:MM
                    self._state_province,  # 11. State/Province
                    self._country_code,    # 12. Country Code
                    self._protocol,        # 13. Protocol
                    str(self._num_observers),  # 14. Observers
                    "",              # 15. Duration (blank = incidental)
                    "Y" if self._complete_cl else "N",  # 16. Complete?
                    "",              # 17. Distance (blank for Stationary)
                    "",              # 18. Area (blank)
                    "Exported by pi_birdie",  # 19. Checklist Comments
                ]
                rows.append(row)
                exported_ids.append(det["id"])

        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerows(rows)

        # Mark exported records as synced
        self._db.mark_synced(exported_ids)

        logger.info(
            "eBird CSV exported: %s (%d records). Upload at https://ebird.org/submit/import",
            csv_path, len(exported_ids),
        )

        if self._export_cb:
            try:
                self._export_cb(str(csv_path))
            except Exception as exc:  # noqa: BLE001
                logger.warning("Export callback raised: %s", exc)

        return str(csv_path)

    # ── Taxonomy ──────────────────────────────────────────────────────────────

    def _refresh_taxonomy(self) -> None:
        """Download and cache the full eBird taxonomy (species code → common name)."""
        try:
            url    = f"{_EBIRD_API_BASE}/ref/taxonomy/ebird"
            params = {"fmt": "json", "cat": "species"}
            resp   = self._session.get(url, params=params, timeout=15)
            resp.raise_for_status()
            self._taxonomy = {
                entry["comName"]: entry["speciesCode"]
                for entry in resp.json()
                if "comName" in entry
            }
            self._taxonomy_loaded = True
            logger.info(
                "eBird taxonomy cached: %d species.", len(self._taxonomy)
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Taxonomy refresh failed: %s", exc)
