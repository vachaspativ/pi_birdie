"""
doa_locator.py — Direction of Arrival (DoA) Estimation for pi_birdie

Implements GCC-PHAT (Generalized Cross-Correlation with Phase Transform)
from scratch using NumPy only. Converts Time Difference of Arrival (TDOA)
measurements across all microphone pairs into a single 0–360° bearing
via least-squares estimation.

Performance budget: ~1.2ms for all 6 pairs of a 4-mic array on Pi 5.

Supported mic array presets (configured via config.yaml doa.mic_array_type):
    "respeaker_4mic"  — Seeed ReSpeaker 4-Mic Array (circular, 70mm ⌀)
    "linear_2mic"     — Generic 2-mic linear array
    "custom"          — User-defined positions via config.yaml

Key invariants (for AI agents):
    - estimate_doa() NEVER raises — returns (None, 0.0) on any error
    - Input audio_block shape must be (n_samples, n_channels)
    - Audio must already be at the sample rate specified in config.yaml
    - max_tau is pre-computed from mic geometry — always < speed_of_sound / mic_spacing
    - Bandpass filter is designed once at __init__ — not per-call
"""

import itertools
import logging
from typing import Optional, Tuple

import numpy as np
from scipy.signal import butter, sosfilt

logger = logging.getLogger(__name__)


# ── Pre-defined mic array geometries ─────────────────────────────────────────

def _respeaker_4mic_positions(radius_m: float) -> np.ndarray:
    """
    ReSpeaker 4-Mic Array: 4 mics at 90° intervals on a circle.
    Mic 0 at 0° (East), going counter-clockwise.

    Returns:
        np.ndarray of shape (4, 2) — XY positions in metres.
    """
    angles = [0, 90, 180, 270]   # degrees
    return np.array([
        [radius_m * np.cos(np.radians(a)),
         radius_m * np.sin(np.radians(a))]
        for a in angles
    ], dtype=np.float64)


def _linear_2mic_positions(spacing_m: float) -> np.ndarray:
    """
    Linear 2-mic array. Mics placed symmetrically about the origin.

    Returns:
        np.ndarray of shape (2, 2) — XY positions in metres.
    """
    return np.array([
        [-spacing_m / 2.0, 0.0],
        [ spacing_m / 2.0, 0.0],
    ], dtype=np.float64)


# ── Main class ────────────────────────────────────────────────────────────────

class DOALocator:
    """
    Direction-of-Arrival estimator using GCC-PHAT for a microphone array.

    Usage:
        doa = DOALocator(config)
        angle, confidence = doa.estimate_doa(audio_block)  # audio_block: (N, channels)
    """

    def __init__(self, config: dict):
        """
        Args:
            config: Full parsed config.yaml dict.
        """
        doa_cfg = config.get("doa", {})
        audio_cfg = config.get("audio", {})

        self._fs: int = int(audio_cfg.get("sample_rate", 48000))
        self._c: float = float(doa_cfg.get("speed_of_sound", 343.0))
        self._confidence_threshold: float = float(doa_cfg.get("confidence_threshold", 0.3))
        self._interp: int = int(doa_cfg.get("interp_factor", 16))

        # Build mic positions ------------------------------------------------
        array_type = doa_cfg.get("mic_array_type", "respeaker_4mic")
        if array_type == "respeaker_4mic":
            radius = float(doa_cfg.get("mic_radius_m", 0.035))
            self._mic_pos = _respeaker_4mic_positions(radius)
        elif array_type == "linear_2mic":
            spacing = float(doa_cfg.get("mic_spacing_m", 0.05))
            self._mic_pos = _linear_2mic_positions(spacing)
        elif array_type == "custom":
            raw = doa_cfg.get("custom_mic_positions_m", [])
            if not raw:
                raise ValueError(
                    "doa.mic_array_type is 'custom' but "
                    "doa.custom_mic_positions_m is empty in config.yaml"
                )
            self._mic_pos = np.array(raw, dtype=np.float64)
        else:
            raise ValueError(f"Unknown doa.mic_array_type: {array_type!r}")

        n_mics = len(self._mic_pos)
        self._mic_pairs: list[Tuple[int, int]] = list(
            itertools.combinations(range(n_mics), 2)
        )

        # Pre-compute max_tau: maximum possible TDOA for the largest baseline
        max_dist = max(
            np.linalg.norm(self._mic_pos[i] - self._mic_pos[j])
            for i, j in self._mic_pairs
        )
        self._max_tau: float = max_dist / self._c

        # Pre-compute bandpass filter sos coefficients (Butterworth 4th-order)
        low  = float(doa_cfg.get("bandpass_low_hz",  1000))
        high = float(doa_cfg.get("bandpass_high_hz", 6000))
        nyq  = self._fs / 2.0
        if low >= nyq or high >= nyq:
            logger.warning(
                "Bandpass cutoffs (%d/%d Hz) exceed Nyquist (%d Hz). "
                "Skipping bandpass filter.", int(low), int(high), int(nyq)
            )
            self._sos = None
        else:
            self._sos = butter(
                N=4,
                Wn=[low / nyq, high / nyq],
                btype="band",
                output="sos",
            )

        logger.info(
            "DOALocator ready: %s, %d mics, %d pairs, max_tau=%.4fms",
            array_type, n_mics, len(self._mic_pairs), self._max_tau * 1000,
        )

    # ── Public API ────────────────────────────────────────────────────────────

    def estimate_doa(self, audio_block: np.ndarray) -> Tuple[Optional[float], float]:
        """
        Estimate the direction of arrival of a sound source.

        Args:
            audio_block: NumPy array of shape (n_samples, n_channels), float32 or float64.
                         Must have at least 2 channels.

        Returns:
            (angle_degrees, confidence):
                angle_degrees — 0–360° bearing, or None if confidence < threshold.
                confidence    — 0.0–1.0 peak sharpness metric from GCC-PHAT.
        """
        try:
            if audio_block.ndim != 2 or audio_block.shape[1] < 2:
                logger.debug("DOA: audio_block wrong shape %s", audio_block.shape)
                return None, 0.0

            # 1. Bandpass filter (isolates bird call frequency range)
            filtered = self._bandpass(audio_block.astype(np.float64))

            # 2. Compute TDOA for all mic pairs via GCC-PHAT
            tdoas: dict[Tuple[int, int], float] = {}
            peak_heights: list[float] = []

            for i, j in self._mic_pairs:
                tau, peak = self._gcc_phat(filtered[:, i], filtered[:, j])
                tdoas[(i, j)] = tau
                peak_heights.append(peak)

            # Confidence: mean of normalised cross-correlation peaks
            confidence = float(np.mean(peak_heights))

            if confidence < self._confidence_threshold:
                return None, confidence

            # 3. Least-squares angle from all TDOA measurements
            angle = self._tdoa_to_angle(tdoas)
            return angle, confidence

        except Exception as exc:  # noqa: BLE001
            logger.debug("DOA estimation failed (non-fatal): %s", exc)
            return None, 0.0

    # ── Core GCC-PHAT ─────────────────────────────────────────────────────────

    def _gcc_phat(
        self, sig: np.ndarray, refsig: np.ndarray
    ) -> Tuple[float, float]:
        """
        Compute Time Difference of Arrival between two 1-D signals using GCC-PHAT.

        Algorithm:
            1. FFT both signals (length n = len(sig) + len(refsig) to avoid aliasing)
            2. Cross-power spectrum: R(f) = X1(f) · conj(X2(f))
            3. Phase Transform (PHAT): R(f) / |R(f)| — strips amplitude, keeps phase
            4. IFFT — peak location = TDOA in (interpolated) samples

        Args:
            sig:    Signal from microphone i (1-D, float64).
            refsig: Reference signal from microphone j (1-D, float64).

        Returns:
            (tau_seconds, peak_height):
                tau_seconds  — Estimated TDOA in seconds (can be negative).
                peak_height  — Normalised peak height (0–1), used for confidence.
        """
        n = sig.shape[0] + refsig.shape[0]

        SIG    = np.fft.rfft(sig,    n=n)
        REFSIG = np.fft.rfft(refsig, n=n)

        # Cross-power spectrum with PHAT whitening (epsilon for numerical stability)
        R = SIG * np.conj(REFSIG)
        eps = 1e-10
        R_phat = R / (np.abs(R) + eps)

        # Interpolated IFFT
        n_interp = self._interp * n
        cc = np.fft.irfft(R_phat, n=n_interp)

        # Search window constrained by physical max_tau
        max_shift = min(
            int(self._interp * self._fs * self._max_tau),
            n_interp // 2,
        )

        # Rearrange cc so lag=0 is at centre, then restrict search window
        cc_wrapped = np.concatenate((cc[-max_shift:], cc[:max_shift + 1]))
        peak_idx   = int(np.argmax(np.abs(cc_wrapped)))
        peak_val   = float(np.abs(cc_wrapped[peak_idx]))

        # Normalise peak height (approximately 0–1 for unit-norm signals)
        norm = float(np.max(np.abs(cc)))
        peak_height = min(peak_val / (norm + eps), 1.0)

        # Lag in fractional samples → convert to seconds
        lag_samples = peak_idx - max_shift
        tau = lag_samples / float(self._interp * self._fs)

        return tau, peak_height

    # ── TDOA → Angle ──────────────────────────────────────────────────────────

    def _tdoa_to_angle(self, tdoas: dict[Tuple[int, int], float]) -> float:
        """
        Convert a dict of TDOA values (one per mic pair) to a single azimuth angle.

        Uses least-squares on the overdetermined system:
            Δd_ij ≈ (px_i − px_j) sin(θ) + (py_i − py_j) cos(θ)
        where Δd_ij = τ_ij × c is the measured path-length difference.

        Returns:
            Angle in degrees, 0–360°.
        """
        pairs = list(tdoas.keys())
        A = np.zeros((len(pairs), 2), dtype=np.float64)
        b = np.zeros(len(pairs),     dtype=np.float64)

        for idx, (i, j) in enumerate(pairs):
            dx = self._mic_pos[i, 0] - self._mic_pos[j, 0]
            dy = self._mic_pos[i, 1] - self._mic_pos[j, 1]
            A[idx, 0] = dx           # coefficient for sin(θ)
            A[idx, 1] = dy           # coefficient for cos(θ)
            b[idx]    = tdoas[(i, j)] * self._c  # measured path-length diff (m)

        # Solve: min ‖A·[sin θ, cos θ]ᵀ − b‖
        result, *_ = np.linalg.lstsq(A, b, rcond=None)
        sin_theta, cos_theta = result

        angle_rad = np.arctan2(sin_theta, cos_theta)
        angle_deg = float(np.degrees(angle_rad) % 360)
        return angle_deg

    # ── Bandpass Filter ────────────────────────────────────────────────────────

    def _bandpass(self, audio_block: np.ndarray) -> np.ndarray:
        """Apply the pre-designed bandpass filter to each channel."""
        if self._sos is None:
            return audio_block
        return sosfilt(self._sos, audio_block, axis=0)
