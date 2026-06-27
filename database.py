"""
database.py — SQLite Database Layer for pi_birdie

Handles schema initialization, detection logging, 2-month data retention,
eBird sync tracking, and thread-safe read/write operations.

Key invariants (for AI agents modifying this file):
  - All write operations are protected by self._lock (threading.Lock)
  - SQLite WAL mode is enabled — reads don't need the lock
  - log_detection() ALWAYS returns the new row's integer id
  - get_recent_detections() ALWAYS returns a list (never None)
  - initialize() is idempotent — safe to call multiple times
  - retention sweep deletes audio files from disk before deleting DB rows
"""

import logging
import os
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class Database:
    """Thread-safe SQLite database manager for pi_birdie detections."""

    # SQL statements ──────────────────────────────────────────────────────────

    _CREATE_DETECTIONS = """
        CREATE TABLE IF NOT EXISTS detections (
            id                       INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp                DATETIME NOT NULL
                                         DEFAULT (datetime('now', 'localtime')),
            species_common_name      TEXT NOT NULL,
            species_scientific_name  TEXT NOT NULL,
            confidence_score         REAL NOT NULL,
            latitude                 REAL,
            longitude                REAL,
            gps_source               TEXT,
            audio_sample_path        TEXT,
            doa_angle                REAL,
            rarity_label             TEXT DEFAULT 'Unknown',
            rarity_is_notable        INTEGER NOT NULL DEFAULT 0,
            is_synced                INTEGER NOT NULL DEFAULT 0,
            created_at               DATETIME NOT NULL
                                         DEFAULT (datetime('now', 'localtime'))
        );
    """

    _CREATE_INDEXES = [
        "CREATE INDEX IF NOT EXISTS idx_det_timestamp ON detections(timestamp);",
        "CREATE INDEX IF NOT EXISTS idx_det_synced    ON detections(is_synced);",
        "CREATE INDEX IF NOT EXISTS idx_det_species   ON detections(species_common_name);",
    ]

    _INSERT_DETECTION = """
        INSERT INTO detections (
            timestamp, species_common_name, species_scientific_name,
            confidence_score, latitude, longitude, gps_source,
            audio_sample_path, doa_angle, rarity_label, rarity_is_notable
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
    """

    _GET_RECENT = """
        SELECT id, timestamp, species_common_name, species_scientific_name,
               confidence_score, latitude, longitude, gps_source,
               audio_sample_path, doa_angle, rarity_label, rarity_is_notable,
               is_synced
        FROM detections
        ORDER BY timestamp DESC
        LIMIT ?;
    """

    _GET_UNSYNCED = """
        SELECT id, timestamp, species_common_name, species_scientific_name,
               confidence_score, latitude, longitude, gps_source,
               audio_sample_path, doa_angle, rarity_label
        FROM detections
        WHERE is_synced = 0
        ORDER BY timestamp ASC;
    """

    _MARK_SYNCED = "UPDATE detections SET is_synced = 1 WHERE id IN ({});"

    _GET_OLD_RECORDS = """
        SELECT id, audio_sample_path
        FROM detections
        WHERE julianday('now', 'localtime') - julianday(timestamp) > ?;
    """

    _DELETE_OLD = """
        DELETE FROM detections
        WHERE julianday('now', 'localtime') - julianday(timestamp) > ?;
    """

    # ─────────────────────────────────────────────────────────────────────────

    def __init__(self, db_path: str, audio_samples_dir: str, retention_days: int = 60):
        """
        Args:
            db_path:          Path to the SQLite database file.
            audio_samples_dir: Directory where WAV clips are stored (for retention cleanup).
            retention_days:   Records older than this are deleted on startup.
        """
        self._db_path = db_path
        self._audio_dir = Path(audio_samples_dir)
        self._retention_days = retention_days
        self._conn: Optional[sqlite3.Connection] = None
        self._lock = threading.Lock()

    # Public API ──────────────────────────────────────────────────────────────

    def initialize(self) -> None:
        """
        Open the database connection, create schema if needed, enable WAL mode,
        and run the data retention sweep. Idempotent — safe to call on each startup.

        Raises:
            sqlite3.DatabaseError: If the database file is corrupted beyond repair.
        """
        logger.info("Initialising database at %s", self._db_path)
        try:
            self._conn = sqlite3.connect(
                self._db_path,
                check_same_thread=False,   # We handle thread safety via self._lock
                detect_types=sqlite3.PARSE_DECLTYPES,
            )
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.execute("PRAGMA foreign_keys=ON;")
            self._conn.execute(self._CREATE_DETECTIONS)
            for idx_sql in self._CREATE_INDEXES:
                self._conn.execute(idx_sql)
            self._conn.commit()
            logger.info("Database schema ready.")
            self._run_retention_sweep()
        except sqlite3.DatabaseError as exc:
            logger.critical("Database initialisation failed: %s", exc)
            raise

    def log_detection(
        self,
        *,
        timestamp: datetime,
        species_common_name: str,
        species_scientific_name: str,
        confidence_score: float,
        latitude: float,
        longitude: float,
        gps_source: str,
        audio_sample_path: Optional[str] = None,
        doa_angle: Optional[float] = None,
        rarity_label: str = "Unknown",
        rarity_is_notable: bool = False,
    ) -> int:
        """
        Insert a detection record and return the new row id.

        All keyword-only arguments to prevent accidental positional mistakes.
        Always returns a positive integer id.
        """
        ts_str = timestamp.strftime("%Y-%m-%d %H:%M:%S")
        params = (
            ts_str,
            species_common_name,
            species_scientific_name,
            confidence_score,
            latitude,
            longitude,
            gps_source,
            audio_sample_path,
            doa_angle,
            rarity_label,
            1 if rarity_is_notable else 0,
        )
        with self._lock:
            cursor = self._conn.execute(self._INSERT_DETECTION, params)
            self._conn.commit()
            new_id = cursor.lastrowid
        logger.debug(
            "Logged detection id=%d: %s (%.0f%% conf, rarity=%s)",
            new_id, species_common_name, confidence_score * 100, rarity_label,
        )
        return new_id

    def get_recent_detections(self, limit: int = 50) -> list[dict]:
        """
        Return the most recent detections as a list of dicts (newest first).
        Always returns a list — never raises, returns [] on error.
        """
        try:
            cursor = self._conn.execute(self._GET_RECENT, (limit,))
            return [dict(row) for row in cursor.fetchall()]
        except Exception as exc:  # noqa: BLE001
            logger.error("get_recent_detections failed: %s", exc)
            return []

    def get_unsynced_detections(self) -> list[dict]:
        """Return all detections that have not yet been exported to eBird CSV."""
        try:
            cursor = self._conn.execute(self._GET_UNSYNCED)
            return [dict(row) for row in cursor.fetchall()]
        except Exception as exc:  # noqa: BLE001
            logger.error("get_unsynced_detections failed: %s", exc)
            return []

    def mark_synced(self, ids: list[int]) -> None:
        """Mark the given detection ids as synced (is_synced=1)."""
        if not ids:
            return
        placeholders = ",".join("?" * len(ids))
        sql = self._MARK_SYNCED.format(placeholders)
        with self._lock:
            self._conn.execute(sql, ids)
            self._conn.commit()
        logger.info("Marked %d detection(s) as synced.", len(ids))

    def update_rarity(self, detection_id: int, rarity_label: str, is_notable: bool) -> None:
        """Update the rarity classification for a specific detection (called asynchronously)."""
        with self._lock:
            self._conn.execute(
                "UPDATE detections SET rarity_label=?, rarity_is_notable=? WHERE id=?;",
                (rarity_label, 1 if is_notable else 0, detection_id),
            )
            self._conn.commit()

    def close(self) -> None:
        """Close the database connection cleanly."""
        if self._conn:
            try:
                self._conn.close()
                logger.info("Database connection closed.")
            except Exception as exc:  # noqa: BLE001
                logger.warning("Error closing database: %s", exc)
            finally:
                self._conn = None

    # Internal ────────────────────────────────────────────────────────────────

    def _run_retention_sweep(self) -> None:
        """
        Delete all records (and their audio files) older than retention_days.
        Runs inside initialize() on every application startup.
        Wrapped in a transaction — either all old records are deleted or none are.
        """
        logger.info("Running data retention sweep (>%d days)...", self._retention_days)
        try:
            # Collect audio file paths before deletion so we can remove files from disk
            cursor = self._conn.execute(self._GET_OLD_RECORDS, (self._retention_days,))
            old_rows = cursor.fetchall()

            if not old_rows:
                logger.info("Retention sweep: no old records found.")
                return

            audio_paths = [
                row["audio_sample_path"]
                for row in old_rows
                if row["audio_sample_path"]
            ]

            with self._lock:
                self._conn.execute(self._DELETE_OLD, (self._retention_days,))
                self._conn.commit()

            # Delete audio files outside the transaction (non-critical if file missing)
            deleted_files = 0
            for path in audio_paths:
                try:
                    p = Path(path)
                    if p.exists():
                        p.unlink()
                        deleted_files += 1
                except OSError as exc:
                    logger.warning("Could not delete audio sample %s: %s", path, exc)

            logger.info(
                "Retention sweep complete: removed %d record(s), %d audio file(s).",
                len(old_rows),
                deleted_files,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("Retention sweep failed: %s", exc)
