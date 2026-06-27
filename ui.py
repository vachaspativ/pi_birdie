"""
ui.py — CustomTkinter User Interface for pi_birdie

Provides a dark-mode, responsive UI that adapts to:
    - HDMI display (1024×600 default) — standard cursor interaction
    - Touchscreen (800×480 default) — larger targets, finger-friendly

Adaptive layout:
    DoA CAPABLE:  Spectrogram | Compass | Last Identified | Recent Detections (right panel)
    NO DoA:       Spectrogram | Last Identified | Recent Detections (full-width expanded)

Panels:
    Header:          App title, GPS status badge, Mic/DoA status badge, live clock
    Spectrogram:     Real-time scrolling FFT magnitude display (Canvas)
    DoA Compass:     Animated bearing arrow (Canvas) — hidden when DoA unavailable
    Last Identified: Bird image, name, confidence bar, rarity badge, timestamp
    Sync Panel:      Online/Offline indicator, Export to eBird CSV button
    Recent Detections: Scrollable table with species, confidence, rarity, time

Threading:
    ALL UI mutations happen on the Tk mainloop thread via root.after().
    Worker thread callbacks (GPS, audio, DoA) must use self._schedule() to update UI.

Key invariants (for AI agents):
    - Never call Tk widget methods directly from a non-main thread
    - _schedule(fn) is the ONLY safe way to marshal updates from callbacks
    - The spectrogram uses a numpy FFT buffer — updated via periodic after() timer
    - Compass uses smooth exponential moving average to reduce jitter
    - Bird images are cached in self._image_cache to avoid GC collection (PIL quirk)
"""

import logging
import math
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

import customtkinter as ctk
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from PIL import Image, ImageTk

logger = logging.getLogger(__name__)

# ── Colour palette (dark theme) ───────────────────────────────────────────────
COLOUR = {
    "bg":           "#0D1117",   # Deep navy background
    "surface":      "#161B22",   # Card/panel surface
    "surface2":     "#21262D",   # Secondary surface (alternating rows)
    "border":       "#30363D",   # Subtle border
    "accent":       "#00BFA5",   # Teal accent (BirdNET-ish)
    "accent2":      "#0097A7",   # Darker teal
    "highlight":    "#FFB300",   # Amber highlight
    "text":         "#E6EDF3",   # Primary text
    "text_dim":     "#8B949E",   # Secondary / dimmed text
    "success":      "#4CAF50",   # Green
    "warning":      "#FFC107",   # Amber
    "danger":       "#F44336",   # Red
    "notable_pink": "#E91E63",   # Notable rarity
    "purple":       "#9C27B0",   # Accidental rarity
}

# Rarity label → (display text, colour)
RARITY_UI = {
    "Notable":    ("🌟 Notable",    COLOUR["notable_pink"]),
    "Accidental": ("⭐ Accidental",  COLOUR["purple"]),
    "Expected":   ("🟢 Expected",   COLOUR["success"]),
    "Unknown":    ("⬜ Unknown",     COLOUR["text_dim"]),
}

# GPS status → (label, colour)
from gps_service import (
    GPS_STATUS_LIVE_3D, GPS_STATUS_LIVE_2D, GPS_STATUS_NO_FIX,
    GPS_STATUS_UNAVAILABLE, GPS_STATUS_DISABLED, GPS_STATUS_UI,
)


class PiBirdieApp:
    """
    Main application window for pi_birdie.
    Instantiate, call setup(), then run().
    """

    def __init__(self, config: dict):
        self._config       = config
        self._ui_cfg       = config.get("ui", {})
        self._display_mode = self._ui_cfg.get("display_mode", "hdmi")
        self._is_touch     = self._display_mode == "touchscreen"
        self._theme        = self._ui_cfg.get("theme", "dark")
        self._fullscreen   = bool(self._ui_cfg.get("fullscreen", False))

        # Resolve display dimensions
        if self._is_touch:
            self._win_w      = int(self._ui_cfg.get("touchscreen_width",   800))
            self._win_h      = int(self._ui_cfg.get("touchscreen_height",  480))
            self._font_scale = float(self._ui_cfg.get("touchscreen_font_scale", 0.85))
            self._touch_h    = int(self._ui_cfg.get("touch_target_min_px", 44))
        else:
            self._win_w      = int(self._ui_cfg.get("hdmi_width",  1024))
            self._win_h      = int(self._ui_cfg.get("hdmi_height",  600))
            self._font_scale = float(self._ui_cfg.get("hdmi_font_scale", 1.0))
            self._touch_h    = 32

        self._spec_ms      = int(self._ui_cfg.get("spectrogram_update_ms", 100))
        self._det_limit    = int(self._ui_cfg.get("detection_list_limit",  50))

        # Service references (injected after __init__)
        self._audio_proc   = None
        self._gps_svc      = None
        self._rarity_svc   = None
        self._sync_svc     = None
        self._database     = None

        # DoA capability (set via set_doa_capable())
        self._doa_capable: bool = False

        # Smooth compass target angle (EMA-filtered to avoid jitter)
        self._compass_target: float = 0.0
        self._compass_current: float = 0.0
        self._compass_ema_alpha: float = 0.12  # Smoothing factor

        # Bird image cache (must hold reference to prevent GC)
        self._image_cache:      dict = {}
        self._placeholder_img:  Optional[ImageTk.PhotoImage] = None

        # Recent detections list (max self._det_limit entries)
        self._recent:  deque = deque(maxlen=self._det_limit)
        self._recent_lock = threading.Lock()

        # Spectrogram state
        self._spec_data:  Optional[np.ndarray] = None

        # Tk root and widgets (set in setup())
        self._root:        Optional[ctk.CTk] = None
        self._canvas_spec: Optional[ctk.CTkCanvas] = None
        self._canvas_comp: Optional[ctk.CTkCanvas] = None
        self._lbl_species: Optional[ctk.CTkLabel] = None
        self._lbl_sci:     Optional[ctk.CTkLabel] = None
        self._lbl_conf:    Optional[ctk.CTkLabel] = None
        self._lbl_time:    Optional[ctk.CTkLabel] = None
        self._lbl_rarity:  Optional[ctk.CTkLabel] = None
        self._lbl_img:     Optional[ctk.CTkLabel] = None
        self._progress_conf: Optional[ctk.CTkProgressBar] = None
        self._lbl_gps:     Optional[ctk.CTkLabel] = None
        self._lbl_doa_ind: Optional[ctk.CTkLabel] = None
        self._lbl_online:  Optional[ctk.CTkLabel] = None
        self._lbl_clock:   Optional[ctk.CTkLabel] = None
        self._det_frame:   Optional[ctk.CTkScrollableFrame] = None
        self._btn_export:  Optional[ctk.CTkButton] = None
        self._frame_compass: Optional[ctk.CTkFrame] = None

    # ── Injection ─────────────────────────────────────────────────────────────

    def set_services(self, audio_proc, gps_svc, rarity_svc, sync_svc, database):
        """Inject service references before calling setup()."""
        self._audio_proc = audio_proc
        self._gps_svc    = gps_svc
        self._rarity_svc = rarity_svc
        self._sync_svc   = sync_svc
        self._database   = database

    def set_doa_capable(self, capable: bool) -> None:
        """Called by main.py after AudioProcessor.start() to configure layout."""
        self._doa_capable = capable

    # ── Callbacks registered by main.py ──────────────────────────────────────

    def on_detection(self, event: dict) -> None:
        """Called from audio processor thread when a bird is detected."""
        self._schedule(lambda: self._handle_detection(event))

    def on_doa_update(self, angle: float, confidence: float) -> None:
        """Called from DoA thread with new bearing estimate."""
        self._schedule(lambda: self._update_compass_target(angle))

    def on_gps_status(self, status: str) -> None:
        """Called from GPS thread when status changes."""
        self._schedule(lambda: self._update_gps_badge(status))

    def on_sync_status(self, online: bool) -> None:
        """Called from sync thread when connectivity changes."""
        self._schedule(lambda: self._update_online_badge(online))

    def on_export_ready(self, csv_path: str) -> None:
        """Called when a CSV export file is ready."""
        self._schedule(lambda: self._show_export_toast(csv_path))

    # ── Setup & Run ───────────────────────────────────────────────────────────

    def setup(self) -> None:
        """Build the window and all widgets. Call before run()."""
        ctk.set_appearance_mode(self._theme)
        ctk.set_default_color_theme("blue")

        self._root = ctk.CTk()
        self._root.title("pi_birdie 🐦")
        self._root.configure(fg_color=COLOUR["bg"])
        self._root.geometry(f"{self._win_w}x{self._win_h}")
        self._root.resizable(True, True)

        if self._fullscreen:
            self._root.attributes("-fullscreen", True)
            if self._is_touch:
                self._root.config(cursor="none")   # Hide cursor on touchscreen kiosk

        # Prevent window from disappearing on Pi if display not ready
        self._root.after(100, lambda: self._root.lift())

        self._build_fonts()
        self._build_layout()
        self._load_placeholder_image()

        # Start periodic update timers
        self._root.after(self._spec_ms,  self._tick_spectrogram)
        self._root.after(500,            self._tick_clock)
        self._root.after(50,             self._tick_compass)

    def run(self) -> None:
        """Enter the Tk mainloop. Blocks until window is closed."""
        self._root.mainloop()

    def destroy(self) -> None:
        """Close the window programmatically (called on clean shutdown)."""
        if self._root:
            try:
                self._root.destroy()
            except Exception:  # noqa: BLE001
                pass

    # ── Font Helper ───────────────────────────────────────────────────────────

    def _build_fonts(self) -> None:
        """Pre-compute scaled font sizes for this display mode."""
        s = self._font_scale
        self._font  = lambda size, weight="normal": ctk.CTkFont(
            family="Inter", size=int(size * s), weight=weight
        )

    def _fs(self, size: int, weight: str = "normal") -> ctk.CTkFont:
        """Scaled font shortcut."""
        return ctk.CTkFont(family="Inter", size=int(size * self._font_scale), weight=weight)

    # ── Layout Construction ───────────────────────────────────────────────────

    def _build_layout(self) -> None:
        """Construct the full widget hierarchy."""
        root = self._root

        # ── Header bar ──────────────────────────────────────────────────
        header = ctk.CTkFrame(root, height=38, fg_color=COLOUR["surface"],
                              corner_radius=0)
        header.pack(fill="x", side="top")
        header.pack_propagate(False)

        ctk.CTkLabel(header, text="🐦 pi_birdie", font=self._fs(14, "bold"),
                     text_color=COLOUR["accent"]).pack(side="left", padx=12)

        self._lbl_clock = ctk.CTkLabel(header, text="", font=self._fs(12),
                                        text_color=COLOUR["text_dim"])
        self._lbl_clock.pack(side="right", padx=12)

        self._lbl_online = ctk.CTkLabel(header, text="⬤ Checking…",
                                         font=self._fs(11), text_color=COLOUR["text_dim"])
        self._lbl_online.pack(side="right", padx=(0, 8))

        self._lbl_doa_ind = ctk.CTkLabel(
            header,
            text="✅ DoA-capable" if self._doa_capable else "⚠ No DoA",
            font=self._fs(11),
            text_color=COLOUR["success"] if self._doa_capable else COLOUR["warning"],
        )
        self._lbl_doa_ind.pack(side="right", padx=(0, 12))

        self._lbl_gps = ctk.CTkLabel(header, text="⏳ Acquiring GPS…",
                                      font=self._fs(11), text_color=COLOUR["warning"])
        self._lbl_gps.pack(side="right", padx=(0, 12))

        # ── Main body ───────────────────────────────────────────────────
        body = ctk.CTkFrame(root, fg_color=COLOUR["bg"], corner_radius=0)
        body.pack(fill="both", expand=True)

        if self._doa_capable:
            self._build_layout_with_doa(body)
        else:
            self._build_layout_no_doa(body)

    def _build_layout_with_doa(self, body: ctk.CTkFrame) -> None:
        """Layout when DoA is available: left panel (spec + compass) + right panel."""
        body.columnconfigure(0, weight=2)
        body.columnconfigure(1, weight=3)
        body.rowconfigure(0, weight=1)

        # ── Left panel: Spectrogram + Compass ──────────────────────────
        left = ctk.CTkFrame(body, fg_color=COLOUR["surface"], corner_radius=8)
        left.grid(row=0, column=0, sticky="nsew", padx=(8, 4), pady=8)
        left.rowconfigure(0, weight=3)
        left.rowconfigure(1, weight=2)
        left.columnconfigure(0, weight=1)

        self._build_spectrogram_panel(left, row=0)
        self._build_compass_panel(left, row=1)

        # ── Right panel: Last Identified + Sync + Recent Detections ────
        right = ctk.CTkFrame(body, fg_color=COLOUR["bg"], corner_radius=0)
        right.grid(row=0, column=1, sticky="nsew", padx=(4, 8), pady=8)
        right.rowconfigure(0, weight=2)
        right.rowconfigure(1, weight=0)
        right.rowconfigure(2, weight=3)
        right.columnconfigure(0, weight=1)

        self._build_last_identified_panel(right, row=0)
        self._build_sync_panel(right, row=1)
        self._build_recent_detections_panel(right, row=2, expanded=False)

    def _build_layout_no_doa(self, body: ctk.CTkFrame) -> None:
        """
        Layout when DoA is NOT available.
        Spectrogram on left, right panel has Last Identified + EXPANDED Recent Detections.
        """
        body.columnconfigure(0, weight=2)
        body.columnconfigure(1, weight=3)
        body.rowconfigure(0, weight=1)

        # ── Left panel: Spectrogram only (+ No DoA notice) ──────────────
        left = ctk.CTkFrame(body, fg_color=COLOUR["surface"], corner_radius=8)
        left.grid(row=0, column=0, sticky="nsew", padx=(8, 4), pady=8)
        left.rowconfigure(0, weight=1)
        left.rowconfigure(1, weight=0)
        left.columnconfigure(0, weight=1)

        self._build_spectrogram_panel(left, row=0)

        notice = ctk.CTkFrame(left, fg_color=COLOUR["surface2"], corner_radius=6)
        notice.grid(row=1, column=0, sticky="ew", padx=8, pady=(0, 8))
        ctk.CTkLabel(
            notice,
            text="⚠  No DoA — Connect a multi-channel mic array\n"
                 "      (e.g. ReSpeaker 4-Mic) to enable direction finding",
            font=self._fs(10),
            text_color=COLOUR["warning"],
            justify="left",
        ).pack(padx=8, pady=6, anchor="w")

        # ── Right panel ─────────────────────────────────────────────────
        right = ctk.CTkFrame(body, fg_color=COLOUR["bg"], corner_radius=0)
        right.grid(row=0, column=1, sticky="nsew", padx=(4, 8), pady=8)
        right.rowconfigure(0, weight=1)
        right.rowconfigure(1, weight=0)
        right.rowconfigure(2, weight=3)   # Expanded detections
        right.columnconfigure(0, weight=1)

        self._build_last_identified_panel(right, row=0)
        self._build_sync_panel(right, row=1)
        self._build_recent_detections_panel(right, row=2, expanded=True)

    # ── Panel Builders ────────────────────────────────────────────────────────

    def _build_spectrogram_panel(self, parent: ctk.CTkFrame, row: int) -> None:
        """Build the real-time spectrogram canvas."""
        frame = ctk.CTkFrame(parent, fg_color=COLOUR["surface"], corner_radius=0)
        frame.grid(row=row, column=0, sticky="nsew", padx=4, pady=(8, 4))
        frame.rowconfigure(0, weight=0)
        frame.rowconfigure(1, weight=1)
        frame.columnconfigure(0, weight=1)

        ctk.CTkLabel(frame, text="Audio Spectrum", font=self._fs(10, "bold"),
                     text_color=COLOUR["text_dim"]).grid(
            row=0, column=0, sticky="w", padx=8, pady=(6, 0)
        )

        self._canvas_spec = ctk.CTkCanvas(
            frame, bg="#050810", highlightthickness=0
        )
        self._canvas_spec.grid(row=1, column=0, sticky="nsew", padx=4, pady=(2, 4))

    def _build_compass_panel(self, parent: ctk.CTkFrame, row: int) -> None:
        """Build the DoA compass with animated bearing arrow."""
        self._frame_compass = ctk.CTkFrame(parent, fg_color=COLOUR["surface"],
                                           corner_radius=0)
        self._frame_compass.grid(row=row, column=0, sticky="nsew", padx=4, pady=(4, 8))
        self._frame_compass.rowconfigure(1, weight=1)
        self._frame_compass.columnconfigure(0, weight=1)

        ctk.CTkLabel(
            self._frame_compass, text="Direction of Arrival", font=self._fs(10, "bold"),
            text_color=COLOUR["text_dim"]
        ).grid(row=0, column=0, sticky="w", padx=8, pady=(6, 0))

        self._canvas_comp = ctk.CTkCanvas(
            self._frame_compass, bg=COLOUR["surface"], highlightthickness=0
        )
        self._canvas_comp.grid(row=1, column=0, sticky="nsew", padx=4, pady=4)
        self._canvas_comp.bind("<Configure>", self._redraw_compass)

    def _build_last_identified_panel(self, parent: ctk.CTkFrame, row: int) -> None:
        """Build the 'Last Identified' bird card."""
        card = ctk.CTkFrame(parent, fg_color=COLOUR["surface"], corner_radius=8)
        card.grid(row=row, column=0, sticky="nsew", padx=0, pady=(0, 4))
        card.columnconfigure(1, weight=1)

        # Bird image (left side)
        img_frame = ctk.CTkFrame(card, fg_color=COLOUR["surface2"],
                                  corner_radius=6, width=110, height=110)
        img_frame.grid(row=0, column=0, rowspan=5, sticky="nw", padx=10, pady=10)
        img_frame.grid_propagate(False)
        self._lbl_img = ctk.CTkLabel(img_frame, text="🐦", font=self._fs(36),
                                      text_color=COLOUR["text_dim"])
        self._lbl_img.place(relx=0.5, rely=0.5, anchor="center")

        # Species name
        self._lbl_species = ctk.CTkLabel(
            card, text="Listening…", font=self._fs(15, "bold"),
            text_color=COLOUR["text"], anchor="w", wraplength=250
        )
        self._lbl_species.grid(row=0, column=1, sticky="w", padx=(8, 8), pady=(10, 0))

        # Scientific name
        self._lbl_sci = ctk.CTkLabel(
            card, text="", font=self._fs(10),
            text_color=COLOUR["text_dim"], anchor="w"
        )
        self._lbl_sci.grid(row=1, column=1, sticky="w", padx=(8, 8))

        # Rarity badge
        self._lbl_rarity = ctk.CTkLabel(
            card, text="⬜ Unknown", font=self._fs(10),
            text_color=COLOUR["text_dim"], fg_color=COLOUR["surface2"],
            corner_radius=4, padx=6, pady=2
        )
        self._lbl_rarity.grid(row=2, column=1, sticky="w", padx=(8, 8), pady=(2, 0))

        # Confidence progress bar + label
        conf_frame = ctk.CTkFrame(card, fg_color=COLOUR["surface"])
        conf_frame.grid(row=3, column=1, sticky="ew", padx=(8, 8), pady=(4, 0))
        conf_frame.columnconfigure(1, weight=1)
        ctk.CTkLabel(conf_frame, text="Conf:", font=self._fs(10),
                     text_color=COLOUR["text_dim"]).grid(row=0, column=0)
        self._progress_conf = ctk.CTkProgressBar(conf_frame, height=10,
                                                   progress_color=COLOUR["accent"])
        self._progress_conf.grid(row=0, column=1, sticky="ew", padx=(4, 4))
        self._progress_conf.set(0)
        self._lbl_conf = ctk.CTkLabel(conf_frame, text="–", font=self._fs(10),
                                       text_color=COLOUR["text"], width=40)
        self._lbl_conf.grid(row=0, column=2)

        # Timestamp + GPS
        self._lbl_time = ctk.CTkLabel(
            card, text="", font=self._fs(9), text_color=COLOUR["text_dim"], anchor="w"
        )
        self._lbl_time.grid(row=4, column=1, sticky="w", padx=(8, 8), pady=(2, 8))

    def _build_sync_panel(self, parent: ctk.CTkFrame, row: int) -> None:
        """Build the sync status and export button panel."""
        panel = ctk.CTkFrame(parent, fg_color=COLOUR["surface"], corner_radius=8)
        panel.grid(row=row, column=0, sticky="ew", pady=(0, 4))
        panel.columnconfigure(1, weight=1)

        self._lbl_online = ctk.CTkLabel(
            panel, text="⬤ Checking connectivity…",
            font=self._fs(10), text_color=COLOUR["text_dim"]
        )
        self._lbl_online.grid(row=0, column=0, padx=(10, 0), pady=6, sticky="w")

        self._btn_export = ctk.CTkButton(
            panel,
            text="Export eBird CSV",
            font=self._fs(10, "bold"),
            fg_color=COLOUR["accent2"],
            hover_color=COLOUR["accent"],
            height=self._touch_h,
            command=self._on_export_click,
        )
        self._btn_export.grid(row=0, column=1, padx=8, pady=6, sticky="e")

    def _build_recent_detections_panel(
        self, parent: ctk.CTkFrame, row: int, expanded: bool
    ) -> None:
        """Build the scrollable recent detections table."""
        panel = ctk.CTkFrame(parent, fg_color=COLOUR["surface"], corner_radius=8)
        panel.grid(row=row, column=0, sticky="nsew", pady=0)
        panel.rowconfigure(1, weight=1)
        panel.columnconfigure(0, weight=1)

        header_frame = ctk.CTkFrame(panel, fg_color=COLOUR["surface2"], corner_radius=0)
        header_frame.grid(row=0, column=0, sticky="ew")

        title = "Recent Detections" + (" (expanded)" if expanded else "")
        ctk.CTkLabel(header_frame, text=title, font=self._fs(10, "bold"),
                     text_color=COLOUR["text_dim"]).pack(
            side="left", padx=8, pady=4
        )

        # Table header row
        cols = ["Species", "Confidence", "Rarity", "Time"]
        if expanded:
            cols = ["Species", "Scientific Name", "Confidence", "Rarity", "DoA", "Time"]
        self._det_cols = cols
        self._det_expanded = expanded

        col_frame = ctk.CTkFrame(panel, fg_color=COLOUR["surface2"], corner_radius=0)
        col_frame.grid(row=1, column=0, sticky="ew")

        weights = [3, 2, 2, 2] if not expanded else [3, 3, 2, 2, 1, 2]
        for w in weights:
            col_frame.columnconfigure(len(col_frame.grid_slaves()), weight=w)
        for ci, col_name in enumerate(cols):
            ctk.CTkLabel(col_frame, text=col_name, font=self._fs(9, "bold"),
                         text_color=COLOUR["text_dim"]).grid(
                row=0, column=ci, sticky="w", padx=6, pady=3
            )

        self._det_frame = ctk.CTkScrollableFrame(
            panel, fg_color=COLOUR["surface"], corner_radius=0,
            scrollbar_button_color=COLOUR["border"],
        )
        self._det_frame.grid(row=2, column=0, sticky="nsew")
        panel.rowconfigure(2, weight=1)
        for ci in range(len(cols)):
            self._det_frame.columnconfigure(ci, weight=weights[ci] if ci < len(weights) else 1)

    # ── Periodic Timers ───────────────────────────────────────────────────────

    def _tick_spectrogram(self) -> None:
        """Redraw the spectrogram canvas every spec_ms milliseconds."""
        try:
            self._draw_spectrogram()
        except Exception as exc:  # noqa: BLE001
            logger.debug("Spectrogram draw error: %s", exc)
        if self._root:
            self._root.after(self._spec_ms, self._tick_spectrogram)

    def _tick_clock(self) -> None:
        """Update the header clock every 500ms."""
        if self._lbl_clock:
            self._lbl_clock.configure(text=datetime.now().strftime("%H:%M:%S"))
        if self._root:
            self._root.after(500, self._tick_clock)

    def _tick_compass(self) -> None:
        """Smoothly animate the compass towards the target angle."""
        try:
            a = self._compass_ema_alpha
            self._compass_current = (
                a * self._compass_target + (1 - a) * self._compass_current
            )
            if abs(self._compass_target - self._compass_current) > 0.5:
                self._redraw_compass()
        except Exception as exc:  # noqa: BLE001
            logger.debug("Compass tick error: %s", exc)
        if self._root:
            self._root.after(50, self._tick_compass)

    # ── Spectrogram Drawing ───────────────────────────────────────────────────

    def _draw_spectrogram(self) -> None:
        """Render the current FFT magnitude buffer onto the spectrogram canvas."""
        canvas = self._canvas_spec
        if canvas is None:
            return

        w = canvas.winfo_width()
        h = canvas.winfo_height()
        if w < 10 or h < 10:
            return

        canvas.delete("all")

        # Get audio buffer from processor
        if self._audio_proc:
            buf = self._audio_proc.get_spectrogram_buffer()
        else:
            buf = np.zeros(256)

        if buf is None or len(buf) == 0:
            return

        # Normalise to [0, 1]
        mag    = buf.astype(np.float32)
        maxval = np.max(mag)
        if maxval > 0:
            mag = mag / maxval

        # Draw bars (downsampled to canvas width)
        n_bars  = min(len(mag), w // 2)
        indices = np.linspace(0, len(mag) - 1, n_bars).astype(int)
        bar_w   = max(1, w // n_bars)

        for bi, fi in enumerate(indices):
            val     = float(mag[fi])
            bar_h   = int(val * h)
            x0      = bi * bar_w
            x1      = x0 + bar_w - 1
            y0      = h - bar_h
            y1      = h

            # Colour gradient: teal at bottom, amber at top peaks
            if val > 0.75:
                colour = "#FFB300"
            elif val > 0.5:
                colour = "#00E5CC"
            else:
                colour = "#00BFA5"

            canvas.create_rectangle(x0, y0, x1, y1, fill=colour, outline="")

    # ── Compass Drawing ───────────────────────────────────────────────────────

    def _redraw_compass(self, event=None) -> None:
        """Redraw the compass canvas with the current bearing."""
        canvas = self._canvas_comp
        if canvas is None:
            return

        w = canvas.winfo_width()
        h = canvas.winfo_height()
        if w < 20 or h < 20:
            return

        canvas.delete("all")

        cx   = w / 2
        cy   = h / 2
        r    = min(cx, cy) * 0.80   # Outer circle radius

        # Outer ring
        canvas.create_oval(cx - r, cy - r, cx + r, cy + r,
                            outline=COLOUR["border"], width=2, fill=COLOUR["surface2"])

        # Cardinal labels
        labels = [("N", 0), ("E", 90), ("S", 180), ("W", 270)]
        for lbl, deg in labels:
            rad    = math.radians(deg - 90)
            lx     = cx + (r + 12) * math.cos(rad)
            ly     = cy + (r + 12) * math.sin(rad)
            canvas.create_text(lx, ly, text=lbl, fill=COLOUR["text_dim"],
                                font=("Inter", max(8, int(10 * self._font_scale))))

        # Tick marks
        for deg in range(0, 360, 15):
            rad     = math.radians(deg - 90)
            inner_r = r * (0.85 if deg % 90 == 0 else 0.92)
            x0 = cx + inner_r * math.cos(rad)
            y0 = cy + inner_r * math.sin(rad)
            x1 = cx + r * math.cos(rad)
            y1 = cy + r * math.sin(rad)
            canvas.create_line(x0, y0, x1, y1, fill=COLOUR["border"], width=1)

        # Bearing arrow
        angle_rad = math.radians(self._compass_current - 90)
        arrow_len = r * 0.65
        tail_len  = r * 0.20

        ax  = cx + arrow_len * math.cos(angle_rad)
        ay  = cy + arrow_len * math.sin(angle_rad)
        tx  = cx - tail_len  * math.cos(angle_rad)
        ty  = cy - tail_len  * math.sin(angle_rad)

        canvas.create_line(tx, ty, ax, ay, fill=COLOUR["accent"],
                           width=4, arrow="last", arrowshape=(12, 14, 5))

        # Centre dot
        canvas.create_oval(cx - 4, cy - 4, cx + 4, cy + 4,
                            fill=COLOUR["accent"], outline="")

        # Bearing text
        bearing_str = f"{self._compass_current:.0f}°"
        canvas.create_text(cx, cy + r * 0.45, text=bearing_str,
                            fill=COLOUR["highlight"],
                            font=("Inter", max(9, int(11 * self._font_scale)), "bold"))

    # ── Detection Handling ────────────────────────────────────────────────────

    def _handle_detection(self, event: dict) -> None:
        """Update the 'Last Identified' card and recent detections list."""
        common_name     = event.get("common_name",     "Unknown")
        scientific_name = event.get("scientific_name", "")
        confidence      = float(event.get("confidence", 0.0))
        ts              = event.get("timestamp", datetime.now())
        lat             = event.get("lat", 0.0)
        lon             = event.get("lon", 0.0)
        gps_source      = event.get("gps_source", "")
        doa_angle       = event.get("doa_angle")
        rarity          = event.get("rarity",
                                    type("R", (), {"label": "Unknown",
                                                   "badge_icon": "⬜",
                                                   "badge_color": COLOUR["text_dim"]})())

        # Update compass if DoA angle is present
        if doa_angle is not None:
            self._update_compass_target(float(doa_angle))

        # Last Identified card
        self._lbl_species.configure(text=common_name)
        self._lbl_sci.configure(text=scientific_name)
        self._progress_conf.set(min(confidence, 1.0))
        self._lbl_conf.configure(text=f"{confidence:.0%}")

        rarity_label, rarity_colour = RARITY_UI.get(
            rarity.label, (f"{rarity.badge_icon} {rarity.label}", COLOUR["text_dim"])
        )
        self._lbl_rarity.configure(text=rarity_label, text_color=rarity_colour)

        gps_tag = ""
        if gps_source in ("live_3d", "live_2d"):
            gps_tag = "  🛰 Live GPS"
        elif gps_source in ("no_fix", "gpsd_unavailable"):
            gps_tag = "  ⚠ Offline GPS"

        time_str = ts.strftime("%H:%M:%S") if isinstance(ts, datetime) else str(ts)
        self._lbl_time.configure(
            text=f"{time_str}{gps_tag}  {lat:.4f}°, {lon:.4f}°"
        )

        # Bird image
        self._load_bird_image(scientific_name)

        # Update recent detections
        with self._recent_lock:
            self._recent.appendleft(event)
            snapshot = list(self._recent)
        self._refresh_detection_list(snapshot)

    def _refresh_detection_list(self, detections: list) -> None:
        """Rebuild the recent detections table rows."""
        if self._det_frame is None:
            return

        # Clear old rows
        for widget in self._det_frame.winfo_children():
            widget.destroy()

        weights = [3, 2, 2, 2] if not self._det_expanded else [3, 3, 2, 2, 1, 2]

        for ri, det in enumerate(detections):
            bg     = COLOUR["surface2"] if ri % 2 == 0 else COLOUR["surface"]
            common = det.get("common_name", "")
            sci    = det.get("scientific_name", "")
            conf   = float(det.get("confidence", 0.0))
            ts     = det.get("timestamp", datetime.now())
            doa    = det.get("doa_angle")
            rarity = det.get("rarity")

            r_label = "⬜"
            r_color = COLOUR["text_dim"]
            if rarity:
                icon     = getattr(rarity, "badge_icon", "⬜")
                lbl      = getattr(rarity, "label",      "Unknown")
                r_label  = f"{icon} {lbl[:5]}"
                r_color  = getattr(rarity, "badge_color", COLOUR["text_dim"])

            time_str = ts.strftime("%H:%M") if isinstance(ts, datetime) else "–"
            doa_str  = f"{doa:.0f}°" if doa is not None else "–"

            row_frame = ctk.CTkFrame(self._det_frame, fg_color=bg, corner_radius=0)
            row_frame.grid(sticky="ew", columnspan=len(self._det_cols))
            for ci in range(len(self._det_cols)):
                row_frame.columnconfigure(ci, weight=weights[ci] if ci < len(weights) else 1)

            cell_values = (
                [common, f"{conf:.0%}", r_label, time_str]
                if not self._det_expanded
                else [common, sci, f"{conf:.0%}", r_label, doa_str, time_str]
            )
            cell_colors = (
                [COLOUR["text"], COLOUR["accent"], r_color, COLOUR["text_dim"]]
                if not self._det_expanded
                else [COLOUR["text"], COLOUR["text_dim"], COLOUR["accent"],
                      r_color, COLOUR["text_dim"], COLOUR["text_dim"]]
            )

            for ci, (val, col) in enumerate(zip(cell_values, cell_colors)):
                ctk.CTkLabel(row_frame, text=val, font=self._fs(9),
                             text_color=col, anchor="w").grid(
                    row=0, column=ci, sticky="w", padx=6, pady=3
                )

    # ── Image Loading ─────────────────────────────────────────────────────────

    def _load_bird_image(self, scientific_name: str) -> None:
        """Load bird image from bird_images/ directory. Falls back to placeholder."""
        if scientific_name in self._image_cache:
            self._lbl_img.configure(image=self._image_cache[scientific_name], text="")
            return

        img_dir  = Path("bird_images")
        img_path = img_dir / f"{scientific_name}.jpg"

        if not img_path.exists():
            # Try PNG
            img_path = img_dir / f"{scientific_name}.png"

        if img_path.exists():
            try:
                img = Image.open(img_path).resize((100, 100), Image.LANCZOS)
                photo = ImageTk.PhotoImage(img)
                self._image_cache[scientific_name] = photo
                self._lbl_img.configure(image=photo, text="")
                return
            except Exception as exc:  # noqa: BLE001
                logger.debug("Could not load image %s: %s", img_path, exc)

        # No image found — show placeholder emoji
        self._lbl_img.configure(image=None, text="🐦")

    def _load_placeholder_image(self) -> None:
        """Load or create a placeholder bird image."""
        pass  # Placeholder emoji is used directly in _lbl_img widget text

    # ── Badge / Status Updates ────────────────────────────────────────────────

    def _update_gps_badge(self, status: str) -> None:
        """Update the GPS status badge in the header."""
        if self._lbl_gps is None:
            return
        label, colour = GPS_STATUS_UI.get(status, ("GPS: Unknown", COLOUR["text_dim"]))
        self._lbl_gps.configure(text=label, text_color=colour)

    def _update_online_badge(self, online: bool) -> None:
        """Update the online/offline indicator."""
        if self._lbl_online is None:
            return
        if online:
            self._lbl_online.configure(text="⬤ Online", text_color=COLOUR["success"])
        else:
            self._lbl_online.configure(text="⬤ Offline", text_color=COLOUR["danger"])

    def _update_compass_target(self, angle: float) -> None:
        """Set a new target angle for the smooth compass animation."""
        self._compass_target = float(angle) % 360.0

    # ── Export ────────────────────────────────────────────────────────────────

    def _on_export_click(self) -> None:
        """Handle Export button press — trigger CSV export."""
        self._btn_export.configure(state="disabled", text="Exporting…")
        if self._sync_svc:
            threading.Thread(
                target=self._do_export_async, daemon=True
            ).start()
        else:
            self._btn_export.configure(state="normal", text="Export eBird CSV")

    def _do_export_async(self) -> None:
        """Run export in background thread, update button on completion."""
        path = self._sync_svc.export_now() if self._sync_svc else None
        self._schedule(lambda: self._on_export_done(path))

    def _on_export_done(self, path: Optional[str]) -> None:
        """Restore export button after export completes."""
        self._btn_export.configure(state="normal", text="Export eBird CSV")
        if path:
            self._show_export_toast(path)

    def _show_export_toast(self, csv_path: str) -> None:
        """Show a brief toast overlay message with the export file path."""
        toast = ctk.CTkToplevel(self._root)
        toast.overrideredirect(True)
        toast.configure(fg_color=COLOUR["surface2"])
        # Centre the toast
        toast.geometry(f"420x80+{self._win_w//2 - 210}+{self._win_h//2 - 40}")
        toast.lift()
        msg = f"✅ CSV exported!\n{csv_path}\nUpload at ebird.org/submit/import"
        ctk.CTkLabel(toast, text=msg, font=self._fs(10), wraplength=400,
                     text_color=COLOUR["success"]).pack(padx=12, pady=12)
        toast.after(6000, toast.destroy)

    # ── Thread-Safe Scheduling ────────────────────────────────────────────────

    def _schedule(self, fn: Callable) -> None:
        """
        Schedule fn to run on the Tk mainloop thread.
        Safe to call from any thread. No-op if root is closed.
        """
        try:
            if self._root:
                self._root.after(0, fn)
        except Exception:  # noqa: BLE001
            pass
