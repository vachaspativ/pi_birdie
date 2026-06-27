"""
main.py — Application Entrypoint for pi_birdie

Responsibilities:
    1. Parse command-line arguments (--mode, --config)
    2. Load and validate config.yaml
    3. Initialise all subsystems in dependency order
    4. Wire up cross-service callbacks
    5. Start all services
    6. Run the UI mainloop (or headless loop in kiosk mode)
    7. Handle SIGTERM/SIGINT for clean shutdown

Subsystem initialisation order:
    Database → BirdNET Analyzer → DOALocator → GPSService →
    RarityService → AudioProcessor → SyncService → UI

Key invariants (for AI agents):
    - The BirdNET Analyzer is initialised ONCE here and shared with AudioProcessor
    - DOALocator is only created when config.audio.channels >= 2 and doa.enabled is true
    - ALL service.stop() calls happen in finally block regardless of error
    - In 'kiosk' mode the UI runs fullscreen kiosk; in 'on_demand' it runs windowed
    - config.yaml path defaults to ./config.yaml but can be overridden via --config
    - SIGTERM is handled so the systemd service can stop cleanly
"""

import argparse
import logging
import os
import signal
import sys
import threading
from pathlib import Path

import yaml

# ── Logging setup (before any imports that log) ───────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger("pi_birdie")

# ── Module imports ────────────────────────────────────────────────────────────
from database        import Database
from gps_service     import GPSService
from doa_locator     import DOALocator
from rarity_service  import RarityService
from audio_processor import AudioProcessor
from sync_service    import SyncService
from ui              import PiBirdieApp


# ── Argument Parsing ──────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="pi_birdie — Offline bird identification station",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--mode",
        choices=["on_demand", "kiosk"],
        default=None,
        help="Override operation_mode from config.yaml",
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to the YAML configuration file",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity",
    )
    return parser.parse_args()


# ── Config Loading ────────────────────────────────────────────────────────────

def load_config(config_path: str) -> dict:
    """
    Load and parse config.yaml.
    Exits with a clear error message if the file is missing or malformed.
    """
    path = Path(config_path)
    if not path.exists():
        logger.critical("Config file not found: %s", path.resolve())
        sys.exit(1)

    try:
        with path.open("r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
        if not isinstance(config, dict):
            raise ValueError("Config file must be a YAML mapping (dict) at the top level.")
        logger.info("Config loaded from %s", path.resolve())
        return config
    except yaml.YAMLError as exc:
        logger.critical("Failed to parse config.yaml: %s", exc)
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001
        logger.critical("Error loading config: %s", exc)
        sys.exit(1)


# ── BirdNET Analyzer Initialisation ──────────────────────────────────────────

def init_analyzer():
    """
    Initialise and return the BirdNET Analyzer.
    This loads the TFLite model (~100MB) — done ONCE at startup.
    Exits with a clear error if birdnetlib or TFLite runtime is missing.
    """
    logger.info("Loading BirdNET Analyzer (this may take a few seconds)…")
    try:
        from birdnetlib.analyzer import Analyzer  # type: ignore[import]
        analyzer = Analyzer()
        logger.info("BirdNET Analyzer ready.")
        return analyzer
    except ImportError as exc:
        logger.critical(
            "birdnetlib not found. Install with: pip install birdnetlib\n"
            "Also install a TFLite runtime: pip install ai-edge-litert\n"
            "Error: %s", exc
        )
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001
        logger.critical("Failed to initialise BirdNET Analyzer: %s", exc)
        sys.exit(1)


# ── Main Application ──────────────────────────────────────────────────────────

class PiBirdieMain:
    """Orchestrates all pi_birdie services and their lifecycle."""

    def __init__(self, config: dict, mode: str):
        self._config = config
        self._mode   = mode

        # Service instances (initialised in run())
        self._database       = None
        self._analyzer       = None
        self._doa_locator    = None
        self._gps_service    = None
        self._rarity_service = None
        self._audio_proc     = None
        self._sync_service   = None
        self._app            = None

        # Shutdown coordination
        self._shutdown_event = threading.Event()

    def run(self) -> None:
        """Initialise all services, wire callbacks, and enter the main loop."""
        try:
            self._init_services()
            self._wire_callbacks()
            self._start_services()
            self._enter_main_loop()
        except KeyboardInterrupt:
            logger.info("Keyboard interrupt received — shutting down.")
        except Exception as exc:  # noqa: BLE001
            logger.critical("Fatal error: %s", exc, exc_info=True)
        finally:
            self._shutdown()

    def request_shutdown(self) -> None:
        """Signal a clean shutdown (called from signal handlers)."""
        logger.info("Shutdown requested.")
        self._shutdown_event.set()
        if self._app:
            try:
                self._app.destroy()
            except Exception:  # noqa: BLE001
                pass

    # ── Service Initialisation ────────────────────────────────────────────────

    def _init_services(self) -> None:
        """Initialise all services in dependency order."""
        cfg = self._config

        # 1. Database
        db_cfg   = cfg.get("database", {})
        audio_cfg = cfg.get("audio", {})
        self._database = Database(
            db_path=db_cfg.get("path", "./pi_birdie.db"),
            audio_samples_dir=audio_cfg.get("audio_samples_dir", "./audio_samples"),
            retention_days=int(db_cfg.get("retention_days", 60)),
        )
        self._database.initialize()
        logger.info("Database ready.")

        # 2. BirdNET Analyzer (heavy — loaded once)
        self._analyzer = init_analyzer()

        # 3. DoA Locator (conditional on channels and doa.enabled)
        doa_cfg   = cfg.get("doa", {})
        channels  = int(audio_cfg.get("channels", 1))
        doa_enabled = bool(doa_cfg.get("enabled", True))

        if doa_enabled and channels >= 2:
            try:
                self._doa_locator = DOALocator(cfg)
                logger.info("DoA Locator initialised.")
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Could not initialise DoA Locator: %s. DoA will be disabled.", exc
                )
                self._doa_locator = None
        else:
            logger.info(
                "DoA disabled (channels=%d, doa.enabled=%s).", channels, doa_enabled
            )

        # 4. GPS Service
        self._gps_service = GPSService(cfg)

        # 5. Rarity Service
        self._rarity_service = RarityService(cfg)

        # 6. Audio Processor
        self._audio_proc = AudioProcessor(
            config=cfg,
            analyzer=self._analyzer,
            doa_locator=self._doa_locator,
            gps_service=self._gps_service,
        )

        # 7. Sync Service
        self._sync_service = SyncService(
            config=cfg,
            database=self._database,
            rarity_service=self._rarity_service,
        )

        # 8. UI
        if self._mode == "kiosk":
            # Kiosk mode: force fullscreen
            self._config["ui"]["fullscreen"] = True
            self._config["ui"]["display_mode"] = self._config["ui"].get(
                "display_mode", "hdmi"
            )

        self._app = PiBirdieApp(cfg)
        self._app.set_services(
            audio_proc=self._audio_proc,
            gps_svc=self._gps_service,
            rarity_svc=self._rarity_service,
            sync_svc=self._sync_service,
            database=self._database,
        )

        logger.info("All services initialised.")

    # ── Callback Wiring ───────────────────────────────────────────────────────

    def _wire_callbacks(self) -> None:
        """Connect service outputs to service inputs and UI update methods."""

        # GPS → UI
        self._gps_service.set_status_callback(self._app.on_gps_status)

        # Audio detections → enrich with rarity → database → UI
        self._audio_proc.set_detection_callback(self._on_detection)
        self._audio_proc.set_doa_callback(self._app.on_doa_update)

        # Sync → UI
        self._sync_service.set_online_callback(self._app.on_sync_status)
        self._sync_service.set_export_callback(self._app.on_export_ready)

        logger.info("Callbacks wired.")

    def _on_detection(self, event: dict) -> None:
        """
        Central detection handler:
        1. Enrich with GPS source tag
        2. Classify rarity
        3. Log to database
        4. Forward to UI
        """
        try:
            # Rarity classification (O(1) in-memory lookup)
            common_name     = event.get("common_name", "")
            scientific_name = event.get("scientific_name", "")
            rarity          = self._rarity_service.get_rarity(common_name, scientific_name)
            event["rarity"] = rarity

            # GPS source tag
            gps_status = self._gps_service.get_status()
            event["gps_source"] = gps_status

            # Persist to database
            det_id = self._database.log_detection(
                timestamp=event.get("timestamp"),
                species_common_name=common_name,
                species_scientific_name=scientific_name,
                confidence_score=float(event.get("confidence", 0.0)),
                latitude=float(event.get("lat", 0.0)),
                longitude=float(event.get("lon", 0.0)),
                gps_source=gps_status,
                audio_sample_path=event.get("audio_sample_path"),
                doa_angle=event.get("doa_angle"),
                rarity_label=rarity.label,
                rarity_is_notable=rarity.is_notable,
            )
            event["db_id"] = det_id

            # Forward to UI
            self._app.on_detection(event)

        except Exception as exc:  # noqa: BLE001
            logger.error("Error processing detection: %s", exc, exc_info=True)

    # ── Service Lifecycle ─────────────────────────────────────────────────────

    def _start_services(self) -> None:
        """Start all background services before entering the main loop."""
        self._rarity_service.start()
        self._gps_service.start()
        self._audio_proc.start()

        # Inform UI whether DoA is available (affects layout)
        self._app.set_doa_capable(self._audio_proc.is_doa_capable())

        # Build UI widgets AFTER DoA capability is known
        self._app.setup()

        self._sync_service.start()
        logger.info("All services started.")

    def _enter_main_loop(self) -> None:
        """Enter the UI mainloop (blocks until window is closed)."""
        logger.info("Entering UI mainloop (mode=%s).", self._mode)
        self._app.run()

    def _shutdown(self) -> None:
        """Stop all services in reverse order of start. Always runs in finally."""
        logger.info("Shutting down pi_birdie…")
        for name, svc in [
            ("sync_service",    self._sync_service),
            ("audio_processor", self._audio_proc),
            ("gps_service",     self._gps_service),
            ("rarity_service",  self._rarity_service),
        ]:
            if svc:
                try:
                    svc.stop()
                    logger.info("%s stopped.", name)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Error stopping %s: %s", name, exc)

        if self._database:
            try:
                self._database.close()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Error closing database: %s", exc)

        logger.info("pi_birdie shutdown complete.")


# ── Entrypoint ────────────────────────────────────────────────────────────────

def main() -> None:
    args = parse_args()

    # Set log level
    logging.getLogger().setLevel(getattr(logging, args.log_level.upper()))

    config = load_config(args.config)

    # CLI --mode overrides config.yaml operation_mode
    mode = args.mode or config.get("operation_mode", "on_demand")
    if mode not in ("on_demand", "kiosk"):
        logger.critical("Invalid operation_mode: %r. Must be 'on_demand' or 'kiosk'.", mode)
        sys.exit(1)

    logger.info("Starting pi_birdie in '%s' mode.", mode)

    app_main = PiBirdieMain(config, mode)

    # SIGTERM handler for systemd graceful stop
    def _sigterm_handler(signum, frame):  # noqa: ANN001
        logger.info("SIGTERM received.")
        app_main.request_shutdown()

    signal.signal(signal.SIGTERM, _sigterm_handler)
    signal.signal(signal.SIGINT,  _sigterm_handler)

    app_main.run()


if __name__ == "__main__":
    main()
