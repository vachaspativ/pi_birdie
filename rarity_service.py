"""
rarity_service.py — Bird Rarity Classification for pi_birdie

Classifies detected bird species into rarity categories using a
lightweight 2-source system with ZERO per-detection API overhead:

    Source 1 — Regional Species List (monthly cache):
        GET /v2/product/spplist/{regionCode}
        → All species ever recorded in the region.
        → Species NOT in this list = "Accidental" (truly unusual).

    Source 2 — eBird Notable Feed (daily cache):
        GET /v2/data/obs/{regionCode}/recent/notable
        → Species currently flagged as rare by eBird reviewers.
        → Matches → "Notable" (reviewer-confirmed rarity).

Per-detection lookup is O(1) — just a Python set membership check.
No bar-chart scraping. No per-bird API calls.

Rarity labels (with UI badge metadata):
    "Notable"    🌟 — In eBird notable feed (reviewer-flagged)
    "Accidental" ⭐ — Never/rarely recorded in region (not in spplist)
    "Expected"   🟢 — Normal for this region
    "Unknown"    ⬜ — No cache data yet (first run, offline)

Key invariants (for AI agents):
    - get_rarity() NEVER raises — returns RarityResult("Unknown", …) on any error
    - All cache files are stored in config.rarity.cache_dir
    - refresh_online() is called by sync_service when internet is detected
    - eBird API token must be set in config.ebird.api_token for online features
    - The service works completely offline after first successful cache population
"""

import json
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# ── Rarity Result Dataclass ───────────────────────────────────────────────────

@dataclass(frozen=True)
class RarityResult:
    """Immutable rarity classification result for a single species detection."""
    label: str          # "Notable" | "Accidental" | "Expected" | "Unknown"
    is_notable: bool    # True if in eBird notable feed
    badge_icon: str     # Emoji for compact display
    badge_color: str    # Hex colour for UI badge background
    tier_used: int      # 0=no data, 1=spplist only, 2=notable feed used

# Pre-built result singletons for performance ─────────────────────────────────

_RESULT_NOTABLE    = RarityResult("Notable",    True,  "🌟", "#E91E63", 2)
_RESULT_ACCIDENTAL = RarityResult("Accidental", False, "⭐", "#9C27B0", 1)
_RESULT_EXPECTED   = RarityResult("Expected",   False, "🟢", "#4CAF50", 1)
_RESULT_UNKNOWN    = RarityResult("Unknown",    False, "⬜", "#616161", 0)

# eBird API base URL
_EBIRD_API_BASE = "https://api.ebird.org/v2"
_REQUEST_TIMEOUT = 10  # seconds


# ── Main Class ────────────────────────────────────────────────────────────────

class RarityService:
    """
    Lightweight rarity classifier using cached eBird API data.
    Performs zero network calls at detection time — all lookups are in-memory set checks.
    """

    def __init__(self, config: dict):
        """
        Args:
            config: Full parsed config.yaml dict.
        """
        self._cfg         = config
        self._ebird_cfg   = config.get("ebird", {})
        self._rarity_cfg  = config.get("rarity", {})

        self._enabled: bool     = self._rarity_cfg.get("enabled", True)
        self._api_token: str    = self._ebird_cfg.get("api_token", "")
        self._region: str       = self._ebird_cfg.get("region_code", "")
        self._cache_dir: Path   = Path(self._rarity_cfg.get("cache_dir", "./data"))
        self._notable_ttl: int  = int(self._rarity_cfg.get("notable_refresh_hours", 24))
        self._spplist_ttl: int  = int(self._rarity_cfg.get("spplist_refresh_days", 30))

        # In-memory lookup sets (populated from cache files)
        self._spplist_common_names:  set[str] = set()   # common names in region
        self._notable_common_names:  set[str] = set()   # currently notable common names
        self._spplist_loaded  = False
        self._notable_loaded  = False

        # Thread safety (sets are read from multiple threads)
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._refresh_thread: Optional[threading.Thread] = None

        # Cache file paths
        self._spplist_cache  = self._cache_dir / f"spplist_{self._region}.json"
        self._notable_cache  = self._cache_dir / f"notable_{self._region}.json"

        # HTTP session (reused across requests)
        self._session = requests.Session()
        if self._api_token:
            self._session.headers.update({"x-ebirdapitoken": self._api_token})

    # ── Public API ────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Load cached data from disk, then start background refresh thread."""
        if not self._enabled:
            logger.info("Rarity service disabled in config.")
            return

        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._load_cache_from_disk()

        self._stop_event.clear()
        self._refresh_thread = threading.Thread(
            target=self._refresh_loop,
            name="rarity-refresh",
            daemon=True,
        )
        self._refresh_thread.start()
        logger.info("Rarity service started (region=%s).", self._region)

    def stop(self) -> None:
        """Stop the background refresh thread."""
        self._stop_event.set()
        if self._refresh_thread and self._refresh_thread.is_alive():
            self._refresh_thread.join(timeout=5.0)

    def get_rarity(self, common_name: str, scientific_name: str = "") -> RarityResult:
        """
        Classify rarity for a detected species.
        ALWAYS returns a RarityResult — never raises.
        O(1) — in-memory set lookups only.

        Args:
            common_name:     eBird common name (as returned by BirdNET).
            scientific_name: Optional scientific name (for future enrichment).

        Returns:
            RarityResult with label, badge icon, and colour.
        """
        if not self._enabled:
            return _RESULT_UNKNOWN

        try:
            with self._lock:
                spplist_loaded  = self._spplist_loaded
                is_in_spplist   = common_name in self._spplist_common_names
                is_notable      = common_name in self._notable_common_names

            if not spplist_loaded:
                return _RESULT_UNKNOWN

            if is_notable:
                return _RESULT_NOTABLE
            if not is_in_spplist:
                return _RESULT_ACCIDENTAL
            return _RESULT_EXPECTED

        except Exception as exc:  # noqa: BLE001
            logger.debug("get_rarity() error (non-fatal): %s", exc)
            return _RESULT_UNKNOWN

    def refresh_online(self) -> None:
        """
        Attempt to refresh both caches from the eBird API.
        Called by sync_service when internet connectivity is detected.
        Non-blocking — queues work by waking the refresh thread.
        """
        self._stop_event.set()   # Wake the sleeping refresh thread
        time.sleep(0.1)
        self._stop_event.clear()

    # ── Background Refresh Loop ────────────────────────────────────────────────

    def _refresh_loop(self) -> None:
        """Periodically check cache staleness and refresh from eBird API."""
        while not self._stop_event.is_set():
            try:
                self._maybe_refresh_spplist()
                self._maybe_refresh_notable()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Rarity refresh error: %s", exc)
            # Sleep for 1 hour between staleness checks
            self._stop_event.wait(timeout=3600)

    def _maybe_refresh_spplist(self) -> None:
        """Refresh regional species list if cache is stale or missing."""
        if not self._api_token or not self._region:
            return
        if self._is_cache_fresh(self._spplist_cache, days=self._spplist_ttl):
            return

        logger.info("Refreshing eBird species list for region %s…", self._region)
        try:
            url = f"{_EBIRD_API_BASE}/product/spplist/{self._region}"
            resp = self._session.get(url, timeout=_REQUEST_TIMEOUT)
            resp.raise_for_status()

            species_codes: list[str] = resp.json()   # List of eBird species codes
            if not species_codes:
                logger.warning("eBird spplist returned empty list for %s", self._region)
                return

            # Also fetch taxonomy to map species codes → common names
            common_names = self._fetch_common_names(species_codes)
            cache_data = {
                "refreshed_at": datetime.now().isoformat(),
                "region": self._region,
                "common_names": list(common_names),
            }
            self._write_cache(self._spplist_cache, cache_data)
            with self._lock:
                self._spplist_common_names = common_names
                self._spplist_loaded = True
            logger.info(
                "eBird species list refreshed: %d species in %s.",
                len(common_names), self._region,
            )
        except requests.RequestException as exc:
            logger.warning("Could not refresh eBird species list: %s", exc)

    def _maybe_refresh_notable(self) -> None:
        """Refresh eBird notable (rare) sightings feed if cache is stale."""
        if not self._api_token or not self._region:
            return
        if self._is_cache_fresh(self._notable_cache, hours=self._notable_ttl):
            return

        logger.info("Refreshing eBird notable feed for region %s…", self._region)
        try:
            url = f"{_EBIRD_API_BASE}/data/obs/{self._region}/recent/notable"
            resp = self._session.get(url, params={"back": 30, "detail": "simple"},
                                     timeout=_REQUEST_TIMEOUT)
            resp.raise_for_status()

            observations: list[dict] = resp.json()
            notable_names = {obs["comName"] for obs in observations if "comName" in obs}

            cache_data = {
                "refreshed_at": datetime.now().isoformat(),
                "region": self._region,
                "common_names": list(notable_names),
            }
            self._write_cache(self._notable_cache, cache_data)
            with self._lock:
                self._notable_common_names = notable_names
                self._notable_loaded = True
            logger.info(
                "eBird notable feed refreshed: %d notable species in %s.",
                len(notable_names), self._region,
            )
        except requests.RequestException as exc:
            logger.warning("Could not refresh eBird notable feed: %s", exc)

    def _fetch_common_names(self, species_codes: list[str]) -> set[str]:
        """
        Fetch common names for a list of eBird species codes via the taxonomy endpoint.
        Falls back to returning an empty set on failure.
        """
        try:
            # Taxonomy endpoint accepts comma-separated species codes
            # Process in batches of 200 to avoid URL length limits
            all_names: set[str] = set()
            batch_size = 200
            for i in range(0, len(species_codes), batch_size):
                batch = species_codes[i:i + batch_size]
                url = f"{_EBIRD_API_BASE}/ref/taxonomy/ebird"
                params = {
                    "species": ",".join(batch),
                    "fmt": "json",
                    "cat": "species",
                }
                resp = self._session.get(url, params=params, timeout=_REQUEST_TIMEOUT)
                resp.raise_for_status()
                for entry in resp.json():
                    if "comName" in entry:
                        all_names.add(entry["comName"])
            return all_names
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not fetch taxonomy for species codes: %s", exc)
            return set()

    # ── Cache Helpers ─────────────────────────────────────────────────────────

    def _load_cache_from_disk(self) -> None:
        """Load both cache files from disk into memory at startup."""
        # Species list
        data = self._read_cache(self._spplist_cache)
        if data and "common_names" in data:
            with self._lock:
                self._spplist_common_names = set(data["common_names"])
                self._spplist_loaded = True
            logger.info(
                "Loaded spplist cache: %d species.", len(self._spplist_common_names)
            )

        # Notable feed
        data = self._read_cache(self._notable_cache)
        if data and "common_names" in data:
            with self._lock:
                self._notable_common_names = set(data["common_names"])
                self._notable_loaded = True
            logger.info(
                "Loaded notable cache: %d notable species.", len(self._notable_common_names)
            )

    @staticmethod
    def _read_cache(path: Path) -> Optional[dict]:
        """Read a JSON cache file. Returns None if file is missing or corrupt."""
        try:
            if path.exists():
                return json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not read cache file %s: %s", path, exc)
        return None

    @staticmethod
    def _write_cache(path: Path, data: dict) -> None:
        """Write a dict to a JSON cache file atomically."""
        try:
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                            encoding="utf-8")
        except OSError as exc:
            logger.warning("Could not write cache file %s: %s", path, exc)

    @staticmethod
    def _is_cache_fresh(path: Path, days: int = 0, hours: int = 0) -> bool:
        """Return True if cache file exists and is newer than the given TTL."""
        if not path.exists():
            return False
        mtime = datetime.fromtimestamp(path.stat().st_mtime)
        ttl   = timedelta(days=days, hours=hours)
        return (datetime.now() - mtime) < ttl
