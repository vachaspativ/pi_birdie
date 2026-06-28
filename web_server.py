"""
web_server.py — Local HTTP Dashboard Server for pi_birdie

Runs a concurrent HTTP server in a background thread to expose a Web UI
dashboard and REST APIs. Allows users on the same Wi-Fi network to monitor
the bird station.

Key endpoints:
  - GET /                 : Serves the Web Dashboard (HTML/CSS/JS)
  - GET /api/status       : Returns GPS, DoA capability, and connectivity status
  - GET /api/detections   : Returns a list of recent detections
  - GET /api/spectrogram  : Returns the raw real-time spectrogram buffer
  - GET /audio/<file>     : Serves captured WAV clips (from audio_samples/)
  - GET /images/<file>    : Serves downloaded bird images (from bird_images/)
"""

import json
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


# ── Web Dashboard HTML/CSS/JS ─────────────────────────────────────────────────

_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>pi_birdie Station Dashboard 🐦</title>
    <style>
        :root {
            --bg: #0D1117;
            --surface: #161B22;
            --surface2: #21262D;
            --border: #30363D;
            --accent: #00BFA5;
            --accent-dim: #0097A7;
            --highlight: #FFB300;
            --text: #E6EDF3;
            --text-dim: #8B949E;
            --success: #4CAF50;
            --warning: #FFC107;
            --danger: #F44336;
            --notable: #E91E63;
            --accidental: #9C27B0;
        }

        body {
            margin: 0;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            background-color: var(--bg);
            color: var(--text);
        }

        header {
            background-color: var(--surface);
            border-bottom: 1px solid var(--border);
            padding: 10px 20px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }

        header h1 {
            margin: 0;
            font-size: 1.2rem;
            color: var(--accent);
            display: flex;
            align-items: center;
            gap: 8px;
        }

        .badges {
            display: flex;
            gap: 10px;
            align-items: center;
        }

        .badge {
            font-size: 0.8rem;
            padding: 4px 8px;
            border-radius: 4px;
            font-weight: bold;
            background-color: var(--surface2);
            border: 1px solid var(--border);
        }

        .container {
            display: grid;
            grid-template-columns: 1fr 1.5fr;
            gap: 20px;
            padding: 20px;
            max-width: 1400px;
            margin: 0 auto;
        }

        @media (max-width: 900px) {
            .container {
                grid-template-columns: 1fr;
            }
        }

        .panel {
            background-color: var(--surface);
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 15px;
            display: flex;
            flex-direction: column;
        }

        .panel-title {
            font-size: 0.9rem;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            color: var(--text-dim);
            margin-top: 0;
            margin-bottom: 15px;
            border-bottom: 1px solid var(--border);
            padding-bottom: 8px;
        }

        /* Spectrogram */
        #spectrogram {
            width: 100%;
            height: 180px;
            background-color: #050810;
            border-radius: 4px;
        }

        /* Compass */
        .compass-container {
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            height: 220px;
        }

        #compass-svg {
            width: 180px;
            height: 180px;
        }

        .bearing-text {
            font-size: 1.2rem;
            font-weight: bold;
            color: var(--highlight);
            margin-top: 8px;
        }

        /* Last Identified Card */
        .bird-card {
            display: flex;
            gap: 15px;
        }

        .bird-img-frame {
            width: 120px;
            height: 120px;
            background-color: var(--surface2);
            border-radius: 6px;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 3rem;
            overflow: hidden;
        }

        .bird-img-frame img {
            width: 100%;
            height: 100%;
            object-fit: cover;
        }

        .bird-details {
            display: flex;
            flex-direction: column;
            gap: 6px;
            flex: 1;
        }

        .bird-name {
            font-size: 1.3rem;
            font-weight: bold;
            margin: 0;
        }

        .bird-sci {
            font-size: 0.9rem;
            color: var(--text-dim);
            font-style: italic;
            margin: 0;
        }

        .rarity-chip {
            align-self: flex-start;
            font-size: 0.75rem;
            padding: 2px 6px;
            border-radius: 3px;
            font-weight: bold;
        }

        .conf-container {
            display: flex;
            align-items: center;
            gap: 8px;
            margin-top: 5px;
        }

        .progress-bar {
            flex: 1;
            height: 8px;
            background-color: var(--surface2);
            border-radius: 4px;
            overflow: hidden;
        }

        .progress-fill {
            height: 100%;
            background-color: var(--accent);
            width: 0%;
            transition: width 0.3s ease;
        }

        .conf-val {
            font-size: 0.85rem;
            width: 35px;
        }

        .card-footer {
            font-size: 0.75rem;
            color: var(--text-dim);
            margin-top: auto;
        }

        /* Detections List */
        .detections-table {
            width: 100%;
            border-collapse: collapse;
        }

        .detections-table th, .detections-table td {
            text-align: left;
            padding: 8px 12px;
            font-size: 0.9rem;
        }

        .detections-table th {
            color: var(--text-dim);
            border-bottom: 1px solid var(--border);
            font-weight: 600;
        }

        .detections-table tr:nth-child(even) {
            background-color: var(--surface2);
        }

        .detections-table tr:hover {
            background-color: rgba(0, 191, 165, 0.08);
        }

        .audio-btn {
            background: none;
            border: none;
            cursor: pointer;
            font-size: 1.1rem;
            color: var(--accent);
            padding: 0;
        }

        .audio-btn:hover {
            color: var(--text);
        }

        .audio-btn:disabled {
            opacity: 0.3;
            cursor: not-allowed;
        }
    </style>
</head>
<body>
    <header>
        <h1>🐦 pi_birdie</h1>
        <div class="badges">
            <span id="badge-gps" class="badge">⏳ Acquiring GPS…</span>
            <span id="badge-doa" class="badge">Checking Mic…</span>
            <span id="badge-online" class="badge">Checking…</span>
        </div>
    </header>

    <div class="container">
        <!-- Left Pane: Real-time Data -->
        <div style="display: flex; flex-direction: column; gap: 20px;">
            <div class="panel">
                <h3 class="panel-title">Audio Spectrum</h3>
                <canvas id="spectrogram"></canvas>
            </div>

            <div class="panel" id="panel-doa">
                <h3 class="panel-title">Direction of Arrival</h3>
                <div class="compass-container">
                    <svg id="compass-svg" viewBox="0 0 100 100">
                        <!-- Outer circle -->
                        <circle cx="50" cy="50" r="42" stroke="#30363D" stroke-width="2" fill="#161B22" />
                        <!-- Card directions -->
                        <text x="50" y="16" fill="#8B949E" font-size="8" text-anchor="middle" font-weight="bold">N</text>
                        <text x="86" y="53" fill="#8B949E" font-size="8" text-anchor="middle" font-weight="bold">E</text>
                        <text x="50" y="90" fill="#8B949E" font-size="8" text-anchor="middle" font-weight="bold">S</text>
                        <text x="14" y="53" fill="#8B949E" font-size="8" text-anchor="middle" font-weight="bold">W</text>
                        <!-- Arrow group -->
                        <g id="compass-arrow" style="transform-origin: 50px 50px; transition: transform 0.5s ease-out;">
                            <line x1="50" y1="70" x2="50" y2="24" stroke="#00BFA5" stroke-width="3" />
                            <polygon points="50,18 45,26 55,26" fill="#00BFA5" />
                            <circle cx="50" cy="50" r="3" fill="#00BFA5" />
                        </g>
                    </svg>
                    <div id="bearing-val" class="bearing-text">No Signal</div>
                </div>
            </div>
        </div>

        <!-- Right Pane: Detections & Dashboard -->
        <div style="display: flex; flex-direction: column; gap: 20px;">
            <div class="panel">
                <h3 class="panel-title">Last Identified</h3>
                <div class="bird-card">
                    <div class="bird-img-frame" id="last-img">🐦</div>
                    <div class="bird-details">
                        <p class="bird-name" id="last-name">Listening…</p>
                        <p class="bird-sci" id="last-sci"></p>
                        <span class="rarity-chip" id="last-rarity" style="display: none;"></span>
                        <div class="conf-container">
                            <div class="progress-bar">
                                <div class="progress-fill" id="last-conf-fill"></div>
                            </div>
                            <span class="conf-val" id="last-conf-text">–</span>
                        </div>
                        <div class="card-footer" id="last-footer"></div>
                    </div>
                </div>
            </div>

            <div class="panel">
                <h3 class="panel-title">Recent Detections</h3>
                <table class="detections-table">
                    <thead>
                        <tr>
                            <th>Species</th>
                            <th>Confidence</th>
                            <th>Rarity</th>
                            <th>DoA</th>
                            <th>Time</th>
                            <th>Audio</th>
                        </tr>
                    </thead>
                    <tbody id="detections-list">
                        <tr>
                            <td colspan="6" style="text-align: center; color: var(--text-dim);">No detections logged yet</td>
                        </tr>
                    </tbody>
                </table>
            </div>
        </div>
    </div>

    <!-- Hidden audio element for clip playback -->
    <audio id="audio-player"></audio>

    <script>
        const API_STATUS = '/api/status';
        const API_DETECTIONS = '/api/detections';
        const API_SPECTROGRAM = '/api/spectrogram';

        const rarityMap = {
            'Notable': { text: '🌟 Notable', color: '#E91E63' },
            'Accidental': { text: '⭐ Accidental', color: '#9C27B0' },
            'Expected': { text: '🟢 Expected', color: '#4CAF50' },
            'Unknown': { text: '⬜ Unknown', color: '#8B949E' }
        };

        const gpsLabels = {
            'live_3d': { text: '🛰 Live GPS (3D Fix)', color: '#4CAF50' },
            'live_2d': { text: '🛰 Live GPS (2D Fix)', color: '#8BC34A' },
            'no_fix': { text: '⏳ Acquiring GPS…', color: '#FFC107' },
            'gpsd_unavailable': { text: '⚠ Offline GPS', color: '#FF5722' },
            'gps_disabled': { text: '📍 Static Location', color: '#9E9E9E' }
        };

        // Smooth compass transition
        let currentRotation = 0;
        function updateCompass(angle) {
            const arrow = document.getElementById('compass-arrow');
            const val = document.getElementById('bearing-val');
            if (angle === null || angle === undefined) {
                val.innerText = "No Signal";
                return;
            }

            // Normalise angle to navigate shortest rotation path
            let diff = (angle - currentRotation) % 360;
            if (diff < -180) diff += 360;
            if (diff > 180) diff -= 360;
            currentRotation += diff;

            arrow.style.transform = `rotate(${currentRotation}deg)`;
            val.innerText = `${Math.round(angle)}°`;
        }

        // Draw live audio spectrogram
        const canvas = document.getElementById('spectrogram');
        const ctx = canvas.getContext('2d');
        function drawSpectrogram(magArray) {
            const w = canvas.width = canvas.clientWidth;
            const h = canvas.height = canvas.clientHeight;
            ctx.clearRect(0, 0, w, h);
            if (!magArray || magArray.length === 0) return;

            const max = Math.max(...magArray);
            const nBars = Math.min(magArray.length, Math.floor(w / 2));
            const barW = Math.max(1, w / nBars);

            for (let i = 0; i < nBars; i++) {
                const idx = Math.floor(i * (magArray.length / nBars));
                const normVal = max > 0 ? magArray[idx] / max : 0;
                const barH = normVal * h;
                const x = i * barW;
                const y = h - barH;

                let color = '#00BFA5';
                if (normVal > 0.75) color = '#FFB300';
                else if (normVal > 0.5) color = '#00E5CC';

                ctx.fillStyle = color;
                ctx.fillRect(x, y, barW - 0.5, barH);
            }
        }

        // Play recorded clip
        function playAudio(path) {
            const player = document.getElementById('audio-player');
            player.src = `/audio/${encodeURIComponent(path.split(/[\\\\/]/).pop())}`;
            player.play();
        }

        // Main update cycles
        async function fetchStatus() {
            try {
                const res = await fetch(API_STATUS);
                const status = await res.json();

                // GPS badge
                const gps = gpsLabels[status.gps_status] || { text: 'GPS Status', color: '#9E9E9E' };
                const badgeGps = document.getElementById('badge-gps');
                badgeGps.innerText = gps.text;
                badgeGps.style.color = gps.color;
                if (status.gps_coords) {
                    badgeGps.title = `${status.gps_coords[0].toFixed(4)}°, ${status.gps_coords[1].toFixed(4)}°`;
                }

                // DoA mic badge & panel visibility
                const badgeDoa = document.getElementById('badge-doa');
                const panelDoa = document.getElementById('panel-doa');
                if (status.doa_capable) {
                    badgeDoa.innerText = "✅ DoA-capable Mic";
                    badgeDoa.style.color = "#4CAF50";
                    panelDoa.style.display = "flex";
                } else {
                    badgeDoa.innerText = "⚠ Mono Mic (No DoA)";
                    badgeDoa.style.color = "#FFC107";
                    panelDoa.style.display = "none";
                }

                // Online/Offline status
                const badgeOnline = document.getElementById('badge-online');
                if (status.online) {
                    badgeOnline.innerText = "⬤ Online";
                    badgeOnline.style.color = "#4CAF50";
                } else {
                    badgeOnline.innerText = "⬤ Offline";
                    badgeOnline.style.color = "#F44336";
                }
            } catch (err) {
                console.error("Failed to fetch status:", err);
            }
        }

        async function fetchSpectrogram() {
            try {
                const res = await fetch(API_SPECTROGRAM);
                const data = await res.json();
                drawSpectrogram(data.spectrogram);
            } catch (err) {
                console.error(err);
            }
        }

        async function fetchDetections() {
            try {
                const res = await fetch(API_DETECTIONS);
                const list = await res.json();

                if (list.length === 0) return;

                // Update last identified
                const last = list[0];
                document.getElementById('last-name').innerText = last.species_common_name;
                document.getElementById('last-sci').innerText = last.species_scientific_name;
                
                const chip = document.getElementById('last-rarity');
                const rData = rarityMap[last.rarity_label] || rarityMap['Unknown'];
                chip.innerText = rData.text;
                chip.style.backgroundColor = rData.color + '20'; // transparent variant
                chip.style.color = rData.color;
                chip.style.border = `1px solid ${rData.color}`;
                chip.style.display = 'inline-block';

                document.getElementById('last-conf-fill').style.width = `${last.confidence_score * 100}%`;
                document.getElementById('last-conf-text').innerText = `${Math.round(last.confidence_score * 100)}%`;

                const d = new Date(last.timestamp);
                document.getElementById('last-footer').innerText = `${d.toLocaleTimeString()} | GPS: ${last.latitude.toFixed(4)}°, ${last.longitude.toFixed(4)}°`;

                // Set image fallback
                const imgFrame = document.getElementById('last-img');
                if (last.species_scientific_name) {
                    const safeName = last.species_scientific_name.replace(/ /g, '_').replace(/\\//g, '_');
                    imgFrame.innerHTML = `<img src="/images/${encodeURIComponent(safeName)}.jpg" onerror="this.parentElement.innerHTML='🐦'" />`;
                } else {
                    imgFrame.innerHTML = '🐦';
                }

                // Update compass target bearing if direction exists
                if (last.doa_angle !== null) {
                    updateCompass(last.doa_angle);
                }

                // Populate recent detections table
                const tbody = document.getElementById('detections-list');
                tbody.innerHTML = '';
                list.forEach(det => {
                    const tr = document.createElement('tr');
                    const d = new Date(det.timestamp);
                    const timeStr = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
                    const conf = `${Math.round(det.confidence_score * 100)}%`;
                    const r = rarityMap[det.rarity_label] || rarityMap['Unknown'];
                    const doaVal = det.doa_angle !== null ? `${Math.round(det.doa_angle)}°` : '–';

                    const playBtn = det.audio_sample_path 
                        ? `<button class="audio-btn" onclick="playAudio('${det.audio_sample_path.replace(/\\\\/g, '/')}')">▶</button>`
                        : '–';

                    tr.innerHTML = `
                        <td style="font-weight: 600;">${det.species_common_name}</td>
                        <td style="color: var(--accent);">${conf}</td>
                        <td style="color: ${r.color};">${r.text}</td>
                        <td style="color: var(--highlight);">${doaVal}</td>
                        <td style="color: var(--text-dim);">${timeStr}</td>
                        <td>${playBtn}</td>
                    `;
                    tbody.appendChild(tr);
                });
            } catch (err) {
                console.error("Failed to fetch detections:", err);
            }
        }

        // Start polling triggers
        fetchStatus();
        fetchDetections();
        setInterval(fetchStatus, 3000);
        setInterval(fetchDetections, 1500);
        setInterval(fetchSpectrogram, 200);
    </script>
</body>
</html>
"""


# ── HTTP Request Handler ──────────────────────────────────────────────────────

class DashboardHandler(BaseHTTPRequestHandler):
    """
    Subclass BaseHTTPRequestHandler to serve JSON APIs, static assets, and
    the web dashboard HTML page.
    """

    server: "DashboardServer"

    def log_message(self, format, *args):  # noqa: A002
        """Override to write requests logs to debug level (quiets output)."""
        logger.debug(format, *args)

    def do_GET(self) -> None:
        """Handle GET requests for static assets, audio files, and API endpoints."""
        parsed_url = urlparse(self.path)
        path = parsed_url.path

        # 1. API Endpoints
        if path == "/api/status":
            self._handle_api_status()
        elif path == "/api/detections":
            self._handle_api_detections()
        elif path == "/api/spectrogram":
            self._handle_api_spectrogram()

        # 2. Local Audio Clips
        elif path.startswith("/audio/"):
            self._handle_audio_file(path)

        # 3. Downloaded Bird Images
        elif path.startswith("/images/"):
            self._handle_image_file(path)

        # 4. Web Dashboard (index page)
        elif path == "/":
            self._send_html(_DASHBOARD_HTML)

        # 5. 404 fallback
        else:
            self.send_error(404, "Page Not Found")

    # ── API Handlers ──────────────────────────────────────────────────────────

    def _handle_api_status(self) -> None:
        """Serve station diagnostics as a JSON response."""
        gps_status = "gps_disabled"
        gps_coords = None
        if self.server.gps_service:
            gps_status = self.server.gps_service.get_status()
            gps_coords = self.server.gps_service.get_position()

        doa_capable = False
        if self.server.audio_proc:
            doa_capable = self.server.audio_proc.is_doa_capable()

        online = False
        if self.server.sync_service:
            online = self.server.sync_service.is_online()

        data = {
            "gps_status":   gps_status,
            "gps_coords":   gps_coords,
            "doa_capable":  doa_capable,
            "online":       online,
        }
        self._send_json(data)

    def _handle_api_detections(self) -> None:
        """Serve the list of recent detections from database as a JSON response."""
        detections = []
        if self.server.database:
            # Query recent detections from the DB
            detections = self.server.database.get_recent_detections(limit=30)
        self._send_json(detections)

    def _handle_api_spectrogram(self) -> None:
        """Serve the raw scrolling spectrogram magnitude array."""
        mags = []
        if self.server.audio_proc:
            mags = self.server.audio_proc.get_spectrogram_buffer().tolist()
        self._send_json({"spectrogram": mags})

    # ── Static File Handlers ──────────────────────────────────────────────────

    def _handle_audio_file(self, path: str) -> None:
        """Serve a saved WAV audio sample, validating path constraints."""
        filename = path[len("/audio/"):]
        # Path traversal guard
        safe_path = Path("audio_samples") / filename
        if not self._is_safe_path("audio_samples", safe_path):
            self.send_error(403, "Access Denied")
            return

        if not safe_path.exists():
            self.send_error(404, "WAV File Not Found")
            return

        self._send_file(safe_path, "audio/wav")

    def _handle_image_file(self, path: str) -> None:
        """Serve a downloaded bird species image, validating path constraints."""
        filename = path[len("/images/"):]
        safe_path = Path("bird_images") / filename
        if not self._is_safe_path("bird_images", safe_path):
            self.send_error(403, "Access Denied")
            return

        if not safe_path.exists():
            self.send_error(404, "Image File Not Found")
            return

        self._send_file(safe_path, "image/jpeg")

    # ── Response Helpers ──────────────────────────────────────────────────────

    def _send_html(self, html: str) -> None:
        """Send an HTML text response."""
        content = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _send_json(self, data) -> None:  # noqa: ANN001
        """Send a JSON payload response."""
        content = json.dumps(data).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _send_file(self, path: Path, content_type: str) -> None:
        """Send raw binary file contents."""
        try:
            stat = path.stat()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(stat.st_size))
            self.end_headers()
            with path.open("rb") as f:
                # Chunked copy to prevent consuming excessive memory
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except OSError as exc:
            logger.error("Failed to serve file %s: %s", path, exc)
            self.send_error(500, "Internal Server Error")

    @staticmethod
    def _is_safe_path(base_dir: str, target_path: Path) -> bool:
        """Ensure the target path is strictly located inside base_dir."""
        try:
            base = Path(base_dir).resolve()
            target = target_path.resolve()
            return base in target.parents or base == target
        except Exception:  # noqa: BLE001
            return False


# ── Dashboard Server ──────────────────────────────────────────────────────────

class DashboardServer(ThreadingHTTPServer):
    """Threading HTTPServer subclass to pass services down to request handlers."""

    def __init__(
        self,
        server_address: tuple[str, int],
        RequestHandlerClass,
        database,
        audio_proc,
        gps_service,
        sync_service,
    ):
        super().__init__(server_address, RequestHandlerClass)
        self.database    = database
        self.audio_proc  = audio_proc
        self.gps_service = gps_service
        self.sync_service = sync_service


class WebDashboardService:
    """
    Manager service starting/stopping the local HTTP dashboard server
    in a background thread.
    """

    def __init__(self, config: dict, database, audio_proc, gps_svc, sync_svc):
        self._cfg = config.get("web_server", {})
        self._enabled: bool = self._cfg.get("enabled", True)
        self._host: str     = self._cfg.get("host", "0.0.0.0")
        self._port: int     = int(self._cfg.get("port", 8080))

        self._database = database
        self._audio_proc = audio_proc
        self._gps_svc = gps_svc
        self._sync_svc = sync_svc

        self._server: Optional[DashboardServer] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """Start the HTTP server thread."""
        if not self._enabled:
            logger.info("Web server disabled in config.")
            return

        try:
            self._server = DashboardServer(
                (self._host, self._port),
                DashboardHandler,
                self._database,
                self._audio_proc,
                self._gps_svc,
                self._sync_svc,
            )
            self._thread = threading.Thread(
                target=self._server.serve_forever,
                name="web-dashboard",
                daemon=True,
            )
            self._thread.start()
            logger.info("Web dashboard server listening at http://%s:%d/",
                        self._host, self._port)
        except Exception as exc:  # noqa: BLE001
            logger.critical("Failed to start web server on %s:%d: %s",
                            self._host, self._port, exc)

    def stop(self) -> None:
        """Shutdown the HTTP server cleanly."""
        if self._server:
            try:
                self._server.shutdown()
                self._server.server_close()
                logger.info("Web dashboard server stopped.")
            except Exception as exc:  # noqa: BLE001
                logger.warning("Error stopping web server: %s", exc)
            finally:
                self._server = None
