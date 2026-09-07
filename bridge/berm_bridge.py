"""BERM Bridge — read your BERM Base's EEG stream from your own software.

    python3 berm_bridge.py                          # Base at 192.168.4.1
    python3 berm_bridge.py --berm-host 10.0.1.42    # Base on your own network
    python3 berm_bridge.py --port 8520              # REST port (default)

A BLE headband pairs with one device at a time, so once BERM Base owns the
headband nothing else can read it. Base re-broadcasts the raw EEG it is
already receiving over WebSocket (ws://<host>:81/); this process subscribes
to that and re-serves it as a plain local HTTP API, so your visualizer,
analyzer, patch or game sees the same brain data live while Base keeps
driving your rig.

Enable RAW STREAM in BERM Base's web panel or no samples will arrive.

Wire protocol:
    magic "BRMR" | version u8 | frame_id u32 BE | ts_ms u32 BE
    sample_rate u16 BE | num_channels u8 | channel_mask u8
    num_samples u16 BE | payload float32 LE [num_samples x num_channels]
    sample-interleaved: s0_ch0, s0_ch1, ..., s1_ch0, s1_ch1, ...

Copyright (C) 2026 Ronnie B North / Northtron LLC

This program is free software: you can redistribute it and/or modify it
under the terms of the GNU General Public License as published by the Free
Software Foundation, either version 3 of the License, or (at your option)
any later version. See ../LICENSE-GPL-3.0.txt.

It is distributed in the hope that it will be useful, but WITHOUT ANY
WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
FOR A PARTICULAR PURPOSE. See the GNU General Public License for details.

Note that this is a separate process with an HTTP API. If your software
READS from it over localhost, the GPL does not reach your code — write
whatever you like, closed or open. It binds you only if you modify this
file and redistribute it.
"""

import argparse
import asyncio
import logging
import os
import struct
import sys
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import websockets
from flask import Flask, jsonify, request
from scipy.signal import butter, cheby2, sosfilt

# ── Logging ─────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("berm_bridge")
logging.getLogger("werkzeug").setLevel(logging.WARNING)

# ── Band / filter config ────────────────────────────────────────────
NOMINAL_SAMPLE_RATE = 256.0  # Muse default; overwritten by frame metadata
HP_CUTOFF = float(os.environ.get("HP_CUTOFF", "0.3"))

BAND_DEFS = {
    "delta": (0.5, 4.0),
    "theta": (4.0, 8.0),
    "alpha": (8.0, 13.0),
    "beta":  (13.0, 30.0),
    "gamma": (30.0, 100.0),
}

GUIDED_FREQ = {
    "delta": 3.5,   # Between delta and theta — not too deep
    "theta": 6.0,   # Theta entrainment
    "alpha": 10.0,  # IAF / alpha reinforcement
    "beta":  10.0,  # Guide beta down toward alpha
    "gamma": 10.0,  # Guide gamma down toward alpha
}

# 10 s rolling buffer per channel
def _max_samples(fs): return int(fs * 10)

# Channel ID → buffer name. Matches RawStream::Channel in firmware.
CHANNEL_NAMES = {0: "af7", 1: "af8", 2: "tp9", 3: "tp10"}

# ── Filter state (notch is runtime-configurable) ─────────────────────
_NOTCH_HZ_INIT = float(os.environ.get("NOTCH_HZ", "60"))
_filter_low      = _NOTCH_HZ_INIT - 2.0
_filter_high     = _NOTCH_HZ_INIT + 2.0
_filter_harmonic = True
_notch_enabled   = True

# ── Channel montage ──────────────────────────────────────────────────
# Which electrodes feed the left/right analysis signals. BERM Base streams
# all four Muse channels separately over the raw WebSocket, whatever montage
# Base itself is using for its own DSP, so the choice here is independent:
# you can watch TP9/TP10 in a browser while Base drives your rig from the
# frontal pair.
#
#   frontal  — AF7 / AF8 alone. ~6-8 cm apart, on the forehead. Cleaner
#              gamma, least EMG, least alpha.
#   averaged — (AF7+TP9)/2 and (AF8+TP10)/2. Best alpha sensitivity, more
#              EMG. Worst of the three for left/right comparison: averaging
#              two sites emphasises what is common to them.
#   temporal — TP9 / TP10 alone, behind the ears, ~15+ cm apart. The
#              genuinely wide-spaced pair, and the one to use for
#              interhemispheric work. Strongest alpha, most EMG-prone.
#
# Only offered when the stream actually carries the temporal channels — a
# FlowTime headband is frontal-only hardware, so there is no choice to make
# and /eeg reports no montage list at all.
CHAN_FRONTAL  = "frontal"
CHAN_AVERAGED = "averaged"
CHAN_TEMPORAL = "temporal"
CHAN_MODES    = (CHAN_FRONTAL, CHAN_AVERAGED, CHAN_TEMPORAL)

# Integer spellings match museChanMode in the firmware, so a value read off
# the Base panel can be passed straight in; "all" is what the UI calls the
# four-channel average.
CHAN_ALIASES = {
    "0": CHAN_FRONTAL,  "frontal":  CHAN_FRONTAL,  "af": CHAN_FRONTAL,
    "af7/af8": CHAN_FRONTAL,
    "1": CHAN_AVERAGED, "averaged": CHAN_AVERAGED, "avg": CHAN_AVERAGED,
    "all": CHAN_AVERAGED,
    "2": CHAN_TEMPORAL, "temporal": CHAN_TEMPORAL, "tp": CHAN_TEMPORAL,
    "tp9/tp10": CHAN_TEMPORAL,
}


def _parse_chan_mode(value):
    """Normalise a montage name/int to one of CHAN_MODES, or None if unknown."""
    if value is None:
        return None
    return CHAN_ALIASES.get(str(value).strip().lower())


_chan_mode = _parse_chan_mode(os.environ.get("BERM_CHAN")) or CHAN_AVERAGED


def _has_temporal() -> bool:
    """True when the stream carries TP9/TP10 — i.e. a Muse, not a FlowTime."""
    return bool(_state.channel_mask & 0x0C)


def _build_notch_filters(fs: float) -> list:
    """cheby2 bandstop for fundamental + optional 2nd harmonic. Equiripple
    stopband stays deep across the full configured range — see the
    cheby2 rather than butter: steeper skirt for the same order, which
    matters at 50/60 Hz where mains sits right beside the gamma band."""
    nyq = fs / 2.0
    hp_guard = max(HP_CUTOFF, 0.5)
    chunks = []
    lo = max(hp_guard, _filter_low)
    hi = min(nyq - 1.0, _filter_high)
    if hi - lo >= 0.5:
        chunks.append(cheby2(6, 30, [lo, hi], btype="bandstop", fs=fs, output="sos"))
    if _filter_harmonic:
        h_lo = max(hp_guard, 2 * _filter_low)
        h_hi = min(nyq - 1.0, 2 * _filter_high)
        if h_hi - h_lo >= 0.5:
            chunks.append(cheby2(6, 30, [h_lo, h_hi], btype="bandstop", fs=fs, output="sos"))
    return chunks


def _build_filters(fs: float) -> dict:
    f = {}
    f["hp"] = butter(2, HP_CUTOFF, btype="high", fs=fs, output="sos")
    f["notch"] = _build_notch_filters(fs)
    nyq = fs / 2.0
    for name, (lo, hi) in BAND_DEFS.items():
        hi_eff = min(hi, nyq - 1.0)
        if lo < hi_eff:
            f[name] = butter(2, [lo, hi_eff], btype="band", fs=fs, output="sos")
    return f


def _apply_notch(sig: np.ndarray, filters: dict) -> np.ndarray:
    if not _notch_enabled: return sig
    for sos in filters.get("notch", []):
        sig = sosfilt(sos, sig)
    return sig


def compute_band_powers(left: np.ndarray, right: np.ndarray, fs: float, filters: dict):
    if len(left) < int(fs * 3) or len(right) < int(fs * 3):
        return None
    left_hp  = sosfilt(filters["hp"], left)
    right_hp = sosfilt(filters["hp"], right)
    left_hp  = _apply_notch(left_hp, filters)
    right_hp = _apply_notch(right_hp, filters)
    analysis_len = int(fs * 1.0)
    left_hp  = left_hp[-analysis_len:]
    right_hp = right_hp[-analysis_len:]

    L, R = {}, {}
    for b in BAND_DEFS:
        if b not in filters: continue
        L[b] = float(np.sqrt(np.mean(sosfilt(filters[b], left_hp) ** 2)))
        R[b] = float(np.sqrt(np.mean(sosfilt(filters[b], right_hp) ** 2)))
    lt = sum(v ** 2 for v in L.values())
    rt = sum(v ** 2 for v in R.values())
    result = {}
    for b in L:
        result[f"{b}_left_pct"]  = (L[b] ** 2 / lt * 100 if lt > 0 else 0.0)
        result[f"{b}_right_pct"] = (R[b] ** 2 / rt * 100 if rt > 0 else 0.0)
        result[f"{b}_abs"]       = (L[b] + R[b]) / 2.0
    result["total_power_left"]  = float(np.sqrt(lt)) if lt > 0 else 0.0
    result["total_power_right"] = float(np.sqrt(rt)) if rt > 0 else 0.0
    result["total_power"]       = (result["total_power_left"] + result["total_power_right"]) / 2.0
    return result


# ── Shared bridge state ─────────────────────────────────────────────
@dataclass
class State:
    fs: float = NOMINAL_SAMPLE_RATE
    channel_mask: int = 0
    af7:  deque = field(default_factory=lambda: deque(maxlen=_max_samples(NOMINAL_SAMPLE_RATE)))
    af8:  deque = field(default_factory=lambda: deque(maxlen=_max_samples(NOMINAL_SAMPLE_RATE)))
    tp9:  deque = field(default_factory=lambda: deque(maxlen=_max_samples(NOMINAL_SAMPLE_RATE)))
    tp10: deque = field(default_factory=lambda: deque(maxlen=_max_samples(NOMINAL_SAMPLE_RATE)))
    connected: bool = False
    last_frame_id: Optional[int] = None
    frames_received: int = 0
    samples_dropped: int = 0   # detected from gaps in frame_id
    berm_host: str = ""
    berm_ws_port: int = 81


_state = State()
_filters = _build_filters(NOMINAL_SAMPLE_RATE)


def _resize_buffers(new_fs: float):
    """Resize per-channel deques when sample rate changes (Muse 256, FT 241)."""
    cap = _max_samples(new_fs)
    for name in ("af7", "af8", "tp9", "tp10"):
        old = getattr(_state, name)
        new = deque(old, maxlen=cap)
        setattr(_state, name, new)


# ── WebSocket client (berm source mode) ─────────────────────────────
HEADER_FMT = ">4sBIIH BBH"   # magic | ver | fid | ts | sr | nch | mask | nsamp
HEADER_SIZE = struct.calcsize(HEADER_FMT)


def _parse_frame(buf: bytes):
    if len(buf) < HEADER_SIZE:
        return None
    magic, ver, fid, ts, sr, nch, mask, nsamp = struct.unpack(HEADER_FMT, buf[:HEADER_SIZE])
    if magic != b"BRMR" or ver != 1:
        log.warning("bad frame magic=%r ver=%d", magic, ver)
        return None
    payload = buf[HEADER_SIZE:]
    expected = nsamp * nch * 4
    if len(payload) != expected:
        log.warning("frame size mismatch: got %d, expected %d", len(payload), expected)
        return None
    # Sample-interleaved float32 LE → np array shape (nsamp, nch)
    samples = np.frombuffer(payload, dtype="<f4").reshape(nsamp, nch)
    return {
        "frame_id": fid, "ts_ms": ts, "sample_rate": sr,
        "num_channels": nch, "channel_mask": mask, "samples": samples,
    }


def _dispatch_frame(frame: dict):
    """Push samples into per-channel deques based on channel_mask."""
    if frame["sample_rate"] and abs(frame["sample_rate"] - _state.fs) > 1.0:
        _state.fs = float(frame["sample_rate"])
        _resize_buffers(_state.fs)
        global _filters
        _filters = _build_filters(_state.fs)
        log.info("sample rate updated → %.1f Hz; filters rebuilt", _state.fs)
    if frame["channel_mask"] != _state.channel_mask:
        _state.channel_mask = frame["channel_mask"]
        log.info("channel mask = 0x%02X", _state.channel_mask)

    # Detect dropped frames via the frame_id counter
    if _state.last_frame_id is not None:
        expected = (_state.last_frame_id + 1) & 0xFFFFFFFF
        if frame["frame_id"] != expected:
            gap = (frame["frame_id"] - expected) & 0xFFFFFFFF
            _state.samples_dropped += gap * frame["samples"].shape[0]
            log.warning("dropped %d frame(s)", gap)
    _state.last_frame_id = frame["frame_id"]
    _state.frames_received += 1

    # Sample-interleaved layout: each row is one sample across all channels.
    # The channels in the row appear in ascending channel-ID order limited to
    # those set in the mask. Walk the mask bits in order to know which deque
    # gets each column.
    active = [ch for ch in range(8) if frame["channel_mask"] & (1 << ch)]
    samples = frame["samples"]  # shape (nsamp, nch_active)
    for col, ch in enumerate(active):
        name = CHANNEL_NAMES.get(ch)
        if name is None: continue
        buf = getattr(_state, name)
        buf.extend(samples[:, col].tolist())


async def _ws_client_loop(host: str, port: int):
    url = f"ws://{host}:{port}/"
    backoff = 1.0
    while True:
        try:
            log.info("connecting → %s", url)
            async with websockets.connect(url, max_size=None, ping_interval=20) as ws:
                _state.connected = True
                backoff = 1.0
                log.info("connected — receiving frames")
                async for msg in ws:
                    if isinstance(msg, bytes):
                        f = _parse_frame(msg)
                        if f is not None:
                            _dispatch_frame(f)
                    # ignore text messages
        except Exception as e:
            _state.connected = False
            log.warning("ws disconnected: %s — reconnecting in %.1fs", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)


def _run_ws_in_thread(host: str, port: int):
    """Start the WS client coroutine in its own thread (so Flask can serve
    REST in the main thread without an asyncio conflict)."""
    def thread_main():
        asyncio.new_event_loop().run_until_complete(_ws_client_loop(host, port))
    t = threading.Thread(target=thread_main, daemon=True, name="berm-ws")
    t.start()


# ── REST endpoints (Flask) ──────────────────────────────────────────
app = Flask(__name__)


@app.after_request
def _add_cors_headers(resp):
    """eeg_tones.html / heatmap are loaded as file:// — fetch() against
    localhost:8520 is cross-origin and needs explicit CORS. Mirrors the
    a page served from anywhere can fetch from it. That is the point of
    this process — it exists to be read by other people's software."""
    resp.headers["Access-Control-Allow-Origin"]  = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp


def _hemi_buf(front: deque, temp: deque, temp_present: bool) -> np.ndarray:
    """One hemisphere's analysis signal, per the selected montage.

    FlowTime sends no temporal channel (the firmware reuses the AF7/AF8 slots
    for its left/right raw channels), so with temp_present false this is always
    the frontal buffer whatever the montage says.

    A channel that is in the mask but silent falls back to the other one rather
    than returning an empty array — a working hemisphere on the wrong electrode
    beats a dead one. /eeg reports that substitution as chan_fallback so it
    cannot pass for the montage you asked for."""
    if not temp_present:
        return np.array(list(front), dtype=np.float64)

    f, t = list(front), list(temp)
    if _chan_mode == CHAN_FRONTAL:
        return np.array(f if f else t, dtype=np.float64)
    if _chan_mode == CHAN_TEMPORAL:
        return np.array(t if t else f, dtype=np.float64)

    # averaged
    if f and t:
        n = min(len(f), len(t))
        return (np.array(f[-n:], dtype=np.float64)
                + np.array(t[-n:], dtype=np.float64)) * 0.5
    return np.array(f or t, dtype=np.float64)


def _left_buf() -> np.ndarray:
    return _hemi_buf(_state.af7, _state.tp9, bool(_state.channel_mask & (1 << 2)))


def _right_buf() -> np.ndarray:
    return _hemi_buf(_state.af8, _state.tp10, bool(_state.channel_mask & (1 << 3)))


def _chan_payload() -> dict:
    """Montage fields for the JSON endpoints. Emits no montage list at all when
    the stream has no temporal channels, which is how the UIs know to hide the
    selector on a FlowTime."""
    if not _has_temporal():
        return {}
    need = {
        CHAN_FRONTAL:  (_state.af7, _state.af8),
        CHAN_TEMPORAL: (_state.tp9, _state.tp10),
        CHAN_AVERAGED: (_state.af7, _state.af8, _state.tp9, _state.tp10),
    }[_chan_mode]
    return {
        "chan_mode":     _chan_mode,
        "chan_modes":    list(CHAN_MODES),
        "chan_fallback": _state.connected and any(len(b) == 0 for b in need),
        "chan_counts":   {"af7":  len(_state.af7),  "af8":  len(_state.af8),
                          "tp9":  len(_state.tp9),  "tp10": len(_state.tp10)},
    }


def _compute_derived(bands: dict) -> dict:
    """Derived per-band values. Consumers (Chloe3, eeg_tones,
    brain-machine UIs) gate their whole EEG feature set on this block being
    present, so the bridge must emit it in exactly the same shape.

    Delta is excluded from dominant_active_band: it is always the
    highest-amplitude band at forehead sites and says nothing about state."""
    def avg(b):
        return (bands.get(f"{b}_left_pct", 0) +
                bands.get(f"{b}_right_pct", 0)) / 2.0

    avgs     = {b: avg(b) for b in BAND_DEFS}
    dominant = max(avgs, key=avgs.get)
    active   = {b: v for b, v in avgs.items() if b != "delta"}
    dominant_active = max(active, key=active.get)
    alpha, theta = avgs["alpha"], avgs["theta"]
    slow = alpha + theta
    fast = avgs["beta"] + avgs["gamma"]
    return {
        "dominant_band":        dominant,
        "dominant_active_band": dominant_active,
        "alpha_theta_ratio":    round(alpha / theta, 2) if theta > 0 else 0.0,
        "relaxation_index":     round(slow / (slow + fast), 3) if (slow + fast) > 0 else 0.0,
        "guided_frequency":     GUIDED_FREQ.get(dominant, 10.0),
    }


@app.route("/eeg")
def eeg():
    left = _left_buf()
    right = _right_buf()
    bp = compute_band_powers(left, right, _state.fs, _filters)
    headband_kind = "muse" if (_state.channel_mask & 0x0C) else ("flowtime" if _state.channel_mask else "unknown")
    payload = {
        "ok": True,
        "connected": _state.connected,
        "contact_ok": _state.connected,
        "heart_rate": 0,
        "battery_pct": -1,
        "packet_count": _state.frames_received,
        "samples_dropped": _state.samples_dropped,
        "status": "streaming" if _state.connected else "idle",
        "bands": None,
        "derived": None,
        "source": "berm",
        "berm_host": _state.berm_host,
        "headband": headband_kind,
        "sample_rate": _state.fs,
        "channel_mask": _state.channel_mask,
        "notch_enabled":   _notch_enabled,
        "filter_low":      _filter_low,
        "filter_high":     _filter_high,
        "filter_harmonic": _filter_harmonic,
    }
    payload.update(_chan_payload())
    if bp:
        payload["bands"] = {
            b: {
                "left_pct":  bp[f"{b}_left_pct"],
                "right_pct": bp[f"{b}_right_pct"],
                "abs_power": bp[f"{b}_abs"],
            } for b in BAND_DEFS if f"{b}_left_pct" in bp
        }
        payload["derived"] = _compute_derived(bp)
        payload["total_power"] = bp["total_power"]
    return jsonify(payload)


# ── /peaks — Welch PSD peak per band (drives the dominant-frequency
# labels and the dotted peak traces in eeg_tones.html). Logic mirrors
# Per-band peaks, over the bridge's per-channel buffers and
# the runtime-variable sample rate. ─────────────────────────────────
@app.route("/peaks")
def peaks():
    if not _state.connected:
        return jsonify({"ok": False, "error": "Not connected to BERM Base"})
    left, right = _left_buf(), _right_buf()
    fs = _state.fs
    if len(left) < int(fs * 3):
        return jsonify({"ok": False,
                        "error": f"Not enough samples yet ({len(left)}/{int(fs*3)})"})

    from scipy.signal import welch as _welch
    sig = (left + right) / 2.0
    hp_sos = _filters.get("hp")
    if hp_sos is not None:
        sig = sosfilt(hp_sos, sig)
    sig = _apply_notch(sig, _filters)

    nperseg = min(int(fs * 4), len(sig))
    freqs, psd = _welch(sig, fs=fs, nperseg=nperseg)

    # Exclude the bandstopped frequencies so residual mains energy doesn't
    # dominate per-band peak detection (60 Hz sits inside the gamma band).
    keep = np.ones_like(freqs, dtype=bool)
    if _notch_enabled and _filter_high > _filter_low:
        keep &= ~((freqs >= _filter_low) & (freqs <= _filter_high))
        if _filter_harmonic:
            keep &= ~((freqs >= 2 * _filter_low) & (freqs <= 2 * _filter_high))

    peak_freqs, edge_freqs = {}, {}
    for band, (lo, hi) in BAND_DEFS.items():
        mask = (freqs >= lo) & (freqs < hi) & keep
        if mask.any():
            band_freqs = freqs[mask]
            band_psd   = psd[mask]
            peak_freqs[band] = round(float(band_freqs[int(band_psd.argmax())]), 2)
            total = float(band_psd.sum())
            if total > 0:
                cumsum = np.cumsum(band_psd)
                edge_idx = min(int(np.searchsorted(cumsum, total * 0.95)),
                               len(band_freqs) - 1)
                edge_freqs[band] = round(float(band_freqs[edge_idx]), 2)
            else:
                edge_freqs[band] = round((lo + hi) / 2.0, 2)
        else:
            peak_freqs[band] = round((lo + hi) / 2.0, 2)
            edge_freqs[band] = round((lo + hi) / 2.0, 2)

    # SMR (12-15 Hz beta sub-range, no mains conflict)
    smr_mask = (freqs >= 12.0) & (freqs < 15.0)
    peak_freqs["smr"] = (round(float(freqs[smr_mask][int(psd[smr_mask].argmax())]), 2)
                        if smr_mask.any() else 13.5)

    return jsonify({"ok": True, "peaks": peak_freqs, "edges": edge_freqs,
                    "samples": len(left),
                    "freq_resolution_hz": round(fs / nperseg, 3),
                    **_chan_payload()})


# ── /spectrum — per-bin Welch PSD per hemisphere for the heatmap and
# Mind Mirror viewers. Log-ish bin edges chosen for visual readability
# (more resolution in delta/theta/alpha where it matters). ────────────
SPECTRUM_BIN_EDGES = [
    0.75, 1.0, 1.5, 2.0, 2.5, 3.0,
    4.0, 5.0, 6.0, 7.0,
    8.0, 9.0, 10.0, 11.5, 13.0,
    15.0, 17.5, 20.0, 23.0, 27.0, 30.0,
    35.0, 40.0, 45.0, 50.0, 55.0, 60.0, 70.0, 80.0, 90.0, 100.0, 110.0, 120.0,
]


def _band_for_freq(f):
    if f < 4.0:  return "delta"
    if f < 8.0:  return "theta"
    if f < 13.0: return "alpha"
    if f < 30.0: return "beta"
    return "gamma"


@app.route("/spectrum")
def spectrum():
    if not _state.connected:
        return jsonify({"ok": False, "error": "Not connected to BERM Base"})
    left, right = _left_buf(), _right_buf()
    fs = _state.fs
    if len(left) < int(fs * 3):
        return jsonify({"ok": False,
                        "error": f"Not enough samples yet ({len(left)}/{int(fs*3)})"})

    from scipy.signal import welch as _welch
    hp_sos = _filters.get("hp")
    if hp_sos is not None:
        left  = sosfilt(hp_sos, left)
        right = sosfilt(hp_sos, right)
    left  = _apply_notch(left,  _filters)
    right = _apply_notch(right, _filters)

    nperseg = min(int(fs * 4), len(left))
    freqs, lpsd = _welch(left,  fs=fs, nperseg=nperseg)
    _,     rpsd = _welch(right, fs=fs, nperseg=nperseg)

    # Cap bin edges at Nyquist - 1 so FlowTime (fs=241.5 → nyq=120.75) doesn't
    # produce empty top bins. Edges above Nyquist contribute no real signal.
    nyq = fs / 2.0
    bins = []
    for lo, hi in zip(SPECTRUM_BIN_EDGES[:-1], SPECTRUM_BIN_EDGES[1:]):
        if lo >= nyq: break
        hi_eff = min(hi, nyq)
        mask = (freqs >= lo) & (freqs < hi_eff)
        if mask.any():
            l_pwr = float(lpsd[mask].mean())
            r_pwr = float(rpsd[mask].mean())
        else:
            l_pwr = r_pwr = 0.0
        bins.append({
            "f_lo":  lo, "f_hi":  hi_eff, "label": lo,
            "band":  _band_for_freq(lo),
            "l":     round(l_pwr, 4),
            "r":     round(r_pwr, 4),
        })

    return jsonify({"ok": True, "bins": bins, "fs": fs,
                    "freq_resolution_hz": round(fs / nperseg, 3),
                    "samples": len(left),
                    **_chan_payload()})


@app.route("/notch", methods=["POST"])
def set_notch():
    global _notch_enabled, _filter_low, _filter_high, _filter_harmonic, _filters
    try:
        d = request.get_json(force=True) or {}
        rebuild = False
        if "enabled"  in d: _notch_enabled   = bool(d["enabled"])
        if "low_hz"   in d:
            _filter_low  = max(0.5, min(200.0, float(d["low_hz"])));  rebuild = True
        if "high_hz"  in d:
            _filter_high = max(0.5, min(200.0, float(d["high_hz"]))); rebuild = True
        if "harmonic" in d: _filter_harmonic = bool(d["harmonic"]);   rebuild = True
        if _filter_high - _filter_low < 0.5:
            return jsonify({"ok": False, "error": "high_hz must be ≥ 0.5 above low_hz"}), 400
        if rebuild:
            _filters["notch"] = _build_notch_filters(_state.fs)
        return jsonify({
            "ok": True,
            "notch_enabled":   _notch_enabled,
            "filter_low":      _filter_low,
            "filter_high":     _filter_high,
            "filter_harmonic": _filter_harmonic,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/channels", methods=["GET", "POST"])
def channels():
    """Get or set the channel montage.

    GET  -> {"ok": true, "chan_mode": "averaged", "chan_modes": [...]}
    POST {"mode": "frontal" | "averaged" | "temporal"}

    Also accepts the firmware's integer spellings (0/1/2) and "all" for
    averaged. Takes effect on the next request: every channel is buffered
    continuously, so switching costs nothing and loses no data.

    This is the bridge's own view of the stream. It does not change what
    BERM Base is doing with the same samples — Base has its own montage
    setting in its web panel."""
    global _chan_mode
    if request.method == "GET":
        return jsonify({"ok": True, "chan_mode": _chan_mode,
                        "chan_modes": list(CHAN_MODES) if _has_temporal() else [],
                        "has_temporal": _has_temporal()})
    try:
        data = request.get_json(force=True) or {}
        mode = _parse_chan_mode(data.get("mode", data.get("chan_mode")))
        if mode is None:
            return jsonify({"ok": False,
                            "error": "unknown mode %r - use one of %s"
                                     % (data.get("mode"), ", ".join(CHAN_MODES))}), 400
        _chan_mode = mode
        log.info("channel montage = %s", mode)
        return jsonify({"ok": True, "chan_mode": _chan_mode,
                        "chan_modes": list(CHAN_MODES) if _has_temporal() else [],
                        "has_temporal": _has_temporal()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/connect", methods=["POST"])
def connect_noop():
    """In berm mode BERM Base owns the BLE connection. This endpoint is a
    no-op kept for compatibility with the eeg_tones / heatmap auto-connect
    handshake."""
    return jsonify({"ok": True, "noop": True, "reason": "berm mode — BERM Base owns the headband"})


@app.route("/state")
def state():
    return jsonify({
        "source": "berm",
        "berm_host": _state.berm_host,
        "berm_ws_port": _state.berm_ws_port,
        "connected": _state.connected,
        "frames_received": _state.frames_received,
        "samples_dropped": _state.samples_dropped,
        "sample_rate": _state.fs,
        "channel_mask": _state.channel_mask,
        "buffer_samples": {
            "af7":  len(_state.af7),
            "af8":  len(_state.af8),
            "tp9":  len(_state.tp9),
            "tp10": len(_state.tp10),
        },
        "chan_mode": _chan_mode,
        "has_temporal": _has_temporal(),
    })


# ── Entry point ─────────────────────────────────────────────────────
def main():
    p = argparse.ArgumentParser(description="BERM Bridge — unified EEG proxy/relay")
    p.add_argument("--berm-host", default=os.environ.get("BERM_HOST", "192.168.4.1"),
                   help="BERM Base IP (default: 192.168.4.1 = the AP)")
    p.add_argument("--berm-ws-port", type=int, default=81, help="BERM Base WebSocket port (default: 81)")
    p.add_argument("--host", default="0.0.0.0", help="REST bind address (default: 0.0.0.0)")
    p.add_argument("--port", "-p", type=int, default=8520,
                   help="REST port (default: 8520)")
    p.add_argument("--channels", "-c", default=None,
                   choices=list(CHAN_MODES) + ["all"],
                   help="initial channel montage (default: averaged, or the "
                        "BERM_CHAN env var). Changeable at runtime from the "
                        "EEG-Tones / Mirror / Heatmap pages, or POST /channels.")
    args = p.parse_args()

    global _chan_mode
    if args.channels:
        _chan_mode = _parse_chan_mode(args.channels) or _chan_mode

    _state.berm_host = args.berm_host
    _state.berm_ws_port = args.berm_ws_port
    log.info("BERM Bridge — BERM Base at ws://%s:%d/, REST on :%d",
             args.berm_host, args.berm_ws_port, args.port)
    log.info("Enable RAW STREAM in the BERM Base web panel for this to receive data.")

    _run_ws_in_thread(args.berm_host, args.berm_ws_port)
    app.run(host=args.host, port=args.port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
