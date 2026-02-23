#!/usr/bin/env python3
"""Bad Apple music pipeline — WAV stems → VDP audio commands.

Two modes:
  - AY-chip: synthesize via VDP waveform channels (square/tri/saw/noise)
  - Ableton: find minimal repeating loops, upload as 8-bit PCM samples

Usage:
  # Analysis only
  python gen_music_vdp.py --stems drums.wav bass.wav --bpm 160 --analyze-only

  # AY-chip mode → HTML preview
  python gen_music_vdp.py --stems drums.wav bass.wav melody.wav --bpm 160 --mode ay --html /tmp/music.html

  # Ableton mode → HTML preview
  python gen_music_vdp.py --stems drums.wav bass.wav melody.wav --bpm 160 --mode ableton --html /tmp/music.html

  # VDP audio data output
  python gen_music_vdp.py --stems drums.wav bass.wav --bpm 160 --mode ay --output music.dat
"""

import argparse
import json
import math
import struct
import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

# Optional imports — fail gracefully with clear message
try:
    import librosa
except ImportError:
    print("Error: librosa required. Install with: pip install librosa", file=sys.stderr)
    sys.exit(1)

try:
    import soundfile as sf
except ImportError:
    sf = None  # fallback to librosa.load


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_CHANNELS = 32  # VDP supports up to 32 channels

# Known stem types — used for auto-detection from filenames and AY waveform mapping
KNOWN_STEMS = [
    "drums", "bass", "melody", "vocals", "lead vocals", "backing vocals",
    "guitar", "keyboard", "synth", "percussion", "other",
]

# VDP audio waveform types
WF_SQUARE = 0
WF_TRIANGLE = 1
WF_SAWTOOTH = 2
WF_SINE = 3
WF_NOISE = 4
WF_SAMPLE = 8

# Default video fps for frame mapping
DEFAULT_VIDEO_FPS = 30


# ---------------------------------------------------------------------------
# VDP Audio byte builders
# ---------------------------------------------------------------------------

def vdu_enable_channel(channel: int) -> bytes:
    """VDU 23, 0, &85, channel, 8 — enable audio channel."""
    return bytes([23, 0, 0x85, channel, 8])


def vdu_reset_channel(channel: int) -> bytes:
    """VDU 23, 0, &85, channel, 10 — reset channel."""
    return bytes([23, 0, 0x85, channel, 10])


def vdu_set_waveform(channel: int, waveform: int) -> bytes:
    """VDU 23, 0, &85, channel, 4, waveform — set waveform type."""
    return bytes([23, 0, 0x85, channel, 4, waveform])


def vdu_set_waveform_sample(channel: int, buffer_id: int) -> bytes:
    """VDU 23, 0, &85, channel, 4, 8, id_lo, id_hi — set sample from buffer."""
    return bytes([23, 0, 0x85, channel, 4, WF_SAMPLE,
                  buffer_id & 0xFF, (buffer_id >> 8) & 0xFF])


def vdu_play_note(channel: int, volume: int, freq: int, duration: int) -> bytes:
    """VDU 23, 0, &85, channel, 0, vol, freq_lo, freq_hi, dur_lo, dur_hi."""
    volume = max(0, min(127, volume))
    freq = max(0, min(65535, freq))
    duration = max(0, min(65535, duration))
    return bytes([23, 0, 0x85, channel, 0, volume,
                  freq & 0xFF, (freq >> 8) & 0xFF,
                  duration & 0xFF, (duration >> 8) & 0xFF])


def vdu_set_volume(channel: int, volume: int) -> bytes:
    """VDU 23, 0, &85, channel, 2, volume."""
    volume = max(0, min(127, volume))
    return bytes([23, 0, 0x85, channel, 2, volume])


def vdu_set_frequency(channel: int, freq: int) -> bytes:
    """VDU 23, 0, &85, channel, 3, freq_lo, freq_hi."""
    freq = max(0, min(65535, freq))
    return bytes([23, 0, 0x85, channel, 3,
                  freq & 0xFF, (freq >> 8) & 0xFF])


def vdu_adsr(channel: int, attack_ms: int, decay_ms: int,
             sustain: int, release_ms: int) -> bytes:
    """VDU 23, 0, &85, channel, 6, 1, atk_lo, atk_hi, dec_lo, dec_hi, sus, rel_lo, rel_hi."""
    return bytes([23, 0, 0x85, channel, 6, 1,
                  attack_ms & 0xFF, (attack_ms >> 8) & 0xFF,
                  decay_ms & 0xFF, (decay_ms >> 8) & 0xFF,
                  max(0, min(127, sustain)),
                  release_ms & 0xFF, (release_ms >> 8) & 0xFF])


def vdu_no_envelope(channel: int) -> bytes:
    """VDU 23, 0, &85, channel, 6, 0 — disable volume envelope."""
    return bytes([23, 0, 0x85, channel, 6, 0])


def vdu_set_sample_rate(channel: int, rate: int) -> bytes:
    """VDU 23, 0, &85, channel, 13, rate_lo, rate_hi."""
    return bytes([23, 0, 0x85, channel, 13,
                  rate & 0xFF, (rate >> 8) & 0xFF])


def vdu_sample_from_buffer(channel: int, buffer_id: int,
                           fmt: int = 0, sample_rate: int = None) -> bytes:
    """VDU 23, 0, &85, channel, 5, 2, buf_lo, buf_hi, format[, rate_lo, rate_hi].

    format flags: 0=8-bit signed, 1=8-bit unsigned, bit3=rate follows, bit4=tunable.
    """
    if sample_rate is not None:
        fmt |= 0x08  # rate follows
        return bytes([23, 0, 0x85, channel, 5, 2,
                      buffer_id & 0xFF, (buffer_id >> 8) & 0xFF,
                      fmt,
                      sample_rate & 0xFF, (sample_rate >> 8) & 0xFF])
    return bytes([23, 0, 0x85, channel, 5, 2,
                  buffer_id & 0xFF, (buffer_id >> 8) & 0xFF, fmt])


def vdu_sample_set_repeat(channel: int, start: int = 0,
                          length: int = 0xFFFFFF) -> bytes:
    """Set sample repeat start and length (24-bit each)."""
    out = bytearray()
    # repeat start
    out.extend([23, 0, 0x85, channel, 5, 5,
                start & 0xFF, (start >> 8) & 0xFF, (start >> 16) & 0xFF])
    # repeat length
    out.extend([23, 0, 0x85, channel, 5, 7,
                length & 0xFF, (length >> 8) & 0xFF, (length >> 16) & 0xFF])
    return bytes(out)


def wrap_vdp_buffer(buffer_id: int, payload: bytes) -> bytes:
    """VDU 23, 0, &A0, id_lo, id_hi, 0, len_lo, len_hi, <payload>."""
    header = bytearray([
        23, 0, 0xA0,
        buffer_id & 0xFF, (buffer_id >> 8) & 0xFF,
        0,  # command 0 = write
        len(payload) & 0xFF, (len(payload) >> 8) & 0xFF
    ])
    return bytes(header) + payload


def vdu_call_buffer(buffer_id: int) -> bytes:
    """VDU 23, 0, &A0, id_lo, id_hi, 1 — execute buffer."""
    return bytes([23, 0, 0xA0,
                  buffer_id & 0xFF, (buffer_id >> 8) & 0xFF, 1])


def vdu_clear_all_buffers() -> bytes:
    """VDU 23, 0, &A0, &FF, &FF, 2 — clear all buffers."""
    return bytes([23, 0, 0xA0, 0xFF, 0xFF, 2])


# ---------------------------------------------------------------------------
# Audio analysis
# ---------------------------------------------------------------------------

def load_stem(path: str, sr: int = 22050) -> tuple[np.ndarray, int]:
    """Load a WAV file, return (mono samples, sample_rate)."""
    if sf is not None:
        y, orig_sr = sf.read(path, dtype='float32')
        if y.ndim > 1:
            y = y.mean(axis=1)
        if orig_sr != sr:
            y = librosa.resample(y, orig_sr=orig_sr, target_sr=sr)
        return y, sr
    y, sr_out = librosa.load(path, sr=sr, mono=True)
    return y, sr_out


def build_beat_grid(bpm: float, duration_sec: float,
                    video_fps: float = DEFAULT_VIDEO_FPS):
    """Build beat/bar time arrays and frame mappings.

    Returns dict with:
      beat_times: array of beat times in seconds
      bar_times: array of bar start times (4 beats per bar)
      beat_frames: beat times mapped to video frame indices
      bar_frames: bar times mapped to video frame indices
    """
    beat_period = 60.0 / bpm
    beat_times = np.arange(0, duration_sec, beat_period)
    bar_times = beat_times[::4]  # 4/4 time
    beat_frames = np.round(beat_times * video_fps).astype(int)
    bar_frames = np.round(bar_times * video_fps).astype(int)
    return {
        "bpm": bpm,
        "beat_period": beat_period,
        "beat_times": beat_times,
        "bar_times": bar_times,
        "beat_frames": beat_frames,
        "bar_frames": bar_frames,
        "duration_sec": duration_sec,
        "video_fps": video_fps,
    }


@dataclass
class StemAnalysis:
    """Analysis results for a single stem."""
    name: str
    duration: float
    sr: int
    onset_times: np.ndarray = field(default_factory=lambda: np.array([]))
    onset_frames: np.ndarray = field(default_factory=lambda: np.array([]))
    pitches: np.ndarray = field(default_factory=lambda: np.array([]))
    pitch_confidence: np.ndarray = field(default_factory=lambda: np.array([]))
    pitch_times: np.ndarray = field(default_factory=lambda: np.array([]))
    rms: np.ndarray = field(default_factory=lambda: np.array([]))
    rms_times: np.ndarray = field(default_factory=lambda: np.array([]))
    spectral_centroid: np.ndarray = field(default_factory=lambda: np.array([]))
    centroid_times: np.ndarray = field(default_factory=lambda: np.array([]))
    y: np.ndarray = field(default_factory=lambda: np.array([]))


def analyze_stem(y: np.ndarray, sr: int, name: str,
                 video_fps: float = DEFAULT_VIDEO_FPS,
                 do_pitch: bool = True) -> StemAnalysis:
    """Full analysis of one stem: onsets, pitch, RMS, spectral centroid."""
    duration = len(y) / sr
    analysis = StemAnalysis(name=name, duration=duration, sr=sr, y=y)

    # Onset detection
    onset_env = librosa.onset.onset_strength(y=y, sr=sr)
    onset_frames_lib = librosa.onset.onset_detect(
        y=y, sr=sr, onset_envelope=onset_env, backtrack=True)
    analysis.onset_times = librosa.frames_to_time(onset_frames_lib, sr=sr)
    analysis.onset_frames = np.round(
        analysis.onset_times * video_fps).astype(int)

    # Pitch (pyin) — skip for drums
    if do_pitch:
        f0, voiced_flag, voiced_prob = librosa.pyin(
            y, fmin=librosa.note_to_hz('C2'),
            fmax=librosa.note_to_hz('C7'), sr=sr)
        analysis.pitches = np.where(np.isnan(f0), 0, f0)
        analysis.pitch_confidence = voiced_prob
        analysis.pitch_times = librosa.times_like(f0, sr=sr)

    # RMS energy
    rms = librosa.feature.rms(y=y)[0]
    analysis.rms = rms
    analysis.rms_times = librosa.times_like(rms, sr=sr)

    # Spectral centroid
    cent = librosa.feature.spectral_centroid(y=y, sr=sr)[0]
    analysis.spectral_centroid = cent
    analysis.centroid_times = librosa.times_like(cent, sr=sr)

    return analysis


def print_analysis(stem: StemAnalysis, beat_grid: dict):
    """Print summary of stem analysis."""
    print(f"\n  [{stem.name}]")
    print(f"    Duration: {stem.duration:.2f}s, {len(stem.onset_times)} onsets")
    if len(stem.pitches) > 0:
        voiced = stem.pitches > 0
        if voiced.any():
            p = stem.pitches[voiced]
            print(f"    Pitch: {p.min():.0f}-{p.max():.0f} Hz "
                  f"(median {np.median(p):.0f} Hz), "
                  f"{voiced.sum()}/{len(voiced)} voiced frames")
    rms_peak = stem.rms.max() if len(stem.rms) > 0 else 0
    print(f"    RMS peak: {rms_peak:.4f}")
    if len(stem.spectral_centroid) > 0:
        print(f"    Spectral centroid: "
              f"{stem.spectral_centroid.mean():.0f} Hz mean")


# ---------------------------------------------------------------------------
# AY-chip mode — event generation
# ---------------------------------------------------------------------------

@dataclass
class AYEvent:
    """A single audio event for AY-chip synthesis."""
    time: float       # seconds
    frame: int        # video frame index
    channel: int      # VDP channel 0-3
    freq: int         # Hz (0 = noise/percussive)
    volume: int       # 0-127
    duration_ms: int  # note duration in ms
    waveform: int     # WF_SQUARE etc
    # ADSR (0 = use channel default)
    attack_ms: int = 0
    decay_ms: int = 0
    sustain: int = 127
    release_ms: int = 0


def classify_drum_hit(centroid: float) -> tuple[str, int, int, int, int]:
    """Classify drum hit by spectral centroid.

    Returns (name, freq, attack_ms, decay_ms, release_ms).
    """
    if centroid < 1500:
        return ("kick", 60, 1, 40, 20)
    elif centroid < 4000:
        return ("snare", 200, 1, 60, 30)
    else:
        return ("hihat", 8000, 1, 20, 10)


# Per-stem-type AY synthesis parameters:
#   waveform, pitch_conf_threshold, do_pitch, attack, decay, sustain, release, max_dur_ms
AY_PROFILES = {
    "drums":          (WF_NOISE,    0.0, False,  1, 40, 30,  20, 100),
    "percussion":     (WF_NOISE,    0.0, False,  1, 30, 20,  15, 80),
    "bass":           (WF_TRIANGLE, 0.5, True,   5, 50, 100, 80, 2000),
    "melody":         (WF_SQUARE,   0.4, True,  10, 30, 110, 100, 3000),
    "lead vocals":    (WF_SINE,     0.7, True,  20, 40, 100, 150, 4000),
    "vocals":         (WF_SINE,     0.7, True,  20, 40, 100, 150, 4000),
    "backing vocals": (WF_SINE,     0.6, True,  15, 30,  90, 120, 3000),
    "guitar":         (WF_SAWTOOTH, 0.4, True,   5, 40, 100,  80, 3000),
    "keyboard":       (WF_SQUARE,   0.4, True,   3, 20, 110,  60, 3000),
    "synth":          (WF_SAWTOOTH, 0.3, True,  10, 50, 100, 100, 3000),
    "other":          (WF_TRIANGLE, 0.4, True,  10, 40,  90, 100, 3000),
}


def generate_ay_percussive(stem: StemAnalysis, beat_grid: dict,
                           channel: int, profile: tuple) -> list[AYEvent]:
    """Generate AY events for percussive stems (drums, percussion)."""
    wf, _, _, atk_default, dec_default, sus_default, rel_default, _ = profile
    events = []
    fps = beat_grid["video_fps"]

    for onset_time in stem.onset_times:
        if len(stem.centroid_times) > 0:
            idx = np.argmin(np.abs(stem.centroid_times - onset_time))
            centroid = float(stem.spectral_centroid[idx])
        else:
            centroid = 2000

        if len(stem.rms_times) > 0:
            rms_idx = np.argmin(np.abs(stem.rms_times - onset_time))
            rms_val = float(stem.rms[rms_idx])
        else:
            rms_val = 0.5

        name, freq, atk, dec, rel = classify_drum_hit(centroid)
        vol = int(min(127, rms_val * 127 / max(stem.rms.max(), 1e-6) * 1.2))
        vol = max(20, vol)
        dur = atk + dec + rel + 20

        events.append(AYEvent(
            time=onset_time,
            frame=int(round(onset_time * fps)),
            channel=channel,
            freq=freq,
            volume=vol,
            duration_ms=dur,
            waveform=wf,
            attack_ms=atk,
            decay_ms=dec,
            sustain=60 if name == "snare" else sus_default,
            release_ms=rel,
        ))

    return events


def generate_ay_pitched(stem: StemAnalysis, beat_grid: dict,
                        channel: int, profile: tuple) -> list[AYEvent]:
    """Generate AY events for pitched stems (bass, melody, vocals, guitar, etc)."""
    wf, conf_thresh, _, atk, dec, sus, rel, max_dur = profile
    events = []
    fps = beat_grid["video_fps"]

    if len(stem.pitches) == 0 or len(stem.onset_times) == 0:
        return events

    for i, onset_time in enumerate(stem.onset_times):
        if len(stem.pitch_times) > 0:
            idx = np.argmin(np.abs(stem.pitch_times - onset_time))
            freq = float(stem.pitches[idx])
            conf = float(stem.pitch_confidence[idx]) if len(stem.pitch_confidence) > idx else 0
        else:
            continue

        if freq <= 0 or conf < conf_thresh:
            continue

        midi = round(librosa.hz_to_midi(freq))
        freq = int(round(librosa.midi_to_hz(midi)))

        if i + 1 < len(stem.onset_times):
            dur = stem.onset_times[i + 1] - onset_time
        else:
            dur = beat_grid["beat_period"]
        dur_ms = int(min(dur * 1000, max_dur))

        if len(stem.rms_times) > 0:
            rms_idx = np.argmin(np.abs(stem.rms_times - onset_time))
            vol = int(min(127, float(stem.rms[rms_idx]) /
                         max(stem.rms.max(), 1e-6) * 120))
        else:
            vol = 80

        events.append(AYEvent(
            time=onset_time,
            frame=int(round(onset_time * fps)),
            channel=channel,
            freq=freq,
            volume=max(30, vol),
            duration_ms=dur_ms,
            waveform=wf,
            attack_ms=atk,
            decay_ms=dec,
            sustain=sus,
            release_ms=rel,
        ))

    return events


def generate_ay_for_stem(stem: StemAnalysis, beat_grid: dict,
                         channel: int) -> list[AYEvent]:
    """Generate AY events for any stem type, using profile lookup."""
    profile = AY_PROFILES.get(stem.name, AY_PROFILES["other"])
    _, _, do_pitch, *_ = profile
    if do_pitch:
        return generate_ay_pitched(stem, beat_grid, channel, profile)
    else:
        return generate_ay_percussive(stem, beat_grid, channel, profile)


def generate_all_ay_events(stems: dict[str, StemAnalysis],
                           beat_grid: dict) -> list[AYEvent]:
    """Generate AY events for all stems, sorted by time."""
    all_events = []
    for channel, (name, stem) in enumerate(stems.items()):
        events = generate_ay_for_stem(stem, beat_grid, channel=channel)
        all_events.extend(events)
        print(f"  ch{channel} {name}: {len(events)} AY events", file=sys.stderr)
    all_events.sort(key=lambda e: (e.time, e.channel))
    return all_events


def ay_events_to_frame_commands(events: list[AYEvent],
                                num_frames: int) -> list[list[bytes]]:
    """Group AY events by video frame, emit VDP command bytes per frame.

    Returns list[frame_idx] → list of VDU byte sequences.
    """
    frame_cmds: list[list[bytes]] = [[] for _ in range(num_frames)]

    # Track per-channel state to avoid redundant waveform/ADSR changes
    num_channels = max((ev.channel for ev in events), default=0) + 1
    ch_waveform = [-1] * num_channels
    ch_adsr = [None] * num_channels

    for ev in events:
        if ev.frame < 0 or ev.frame >= num_frames:
            continue

        cmds = frame_cmds[ev.frame]

        # Set waveform if changed
        if ch_waveform[ev.channel] != ev.waveform:
            cmds.append(vdu_set_waveform(ev.channel, ev.waveform))
            ch_waveform[ev.channel] = ev.waveform

        # Set ADSR if changed
        adsr_key = (ev.attack_ms, ev.decay_ms, ev.sustain, ev.release_ms)
        if adsr_key != (0, 0, 127, 0) and ch_adsr[ev.channel] != adsr_key:
            cmds.append(vdu_adsr(ev.channel, ev.attack_ms, ev.decay_ms,
                                 ev.sustain, ev.release_ms))
            ch_adsr[ev.channel] = adsr_key

        # Play note
        cmds.append(vdu_play_note(ev.channel, ev.volume,
                                  ev.freq, ev.duration_ms))

    return frame_cmds


# ---------------------------------------------------------------------------
# Ableton mode — loop detection and PCM samples
# ---------------------------------------------------------------------------

@dataclass
class LoopLibrary:
    """Result of loop deduplication."""
    loops: list[np.ndarray]          # unique PCM loops
    sequence: list[list[int]]        # per-stem: bar_idx → loop_id
    loop_ids_per_stem: list[list[int]]  # which loop IDs belong to each stem
    sample_rate: int
    bar_duration_sec: float
    bar_samples: int


# ---------------------------------------------------------------------------
# Similarity metrics for loop dedup
# ---------------------------------------------------------------------------

def fingerprint_mfcc(chunk: np.ndarray, sr: int, n_mfcc: int = 15) -> np.ndarray:
    """Timbral fingerprint — MFCC mean across time."""
    mfcc = librosa.feature.mfcc(y=chunk, sr=sr, n_mfcc=n_mfcc)
    return mfcc.mean(axis=1)


def fingerprint_chroma(chunk: np.ndarray, sr: int) -> np.ndarray:
    """Pitch class profile — 12-dim chroma vector, ignores timbre."""
    chroma = librosa.feature.chroma_stft(y=chunk, sr=sr)
    return chroma.mean(axis=1)


def fingerprint_rhythm(chunk: np.ndarray, sr: int) -> np.ndarray:
    """Rhythmic fingerprint — onset strength autocorrelation."""
    oenv = librosa.onset.onset_strength(y=chunk, sr=sr)
    # Autocorrelation of onset envelope — captures beat pattern
    ac = librosa.autocorrelate(oenv, max_size=len(oenv))
    # Normalize and take first 32 lags
    if ac.max() > 0:
        ac = ac / ac.max()
    return ac[:32]


def fingerprint_rms_envelope(chunk: np.ndarray, sr: int) -> np.ndarray:
    """Volume envelope shape — 16 RMS bins across the loop."""
    rms = librosa.feature.rms(y=chunk)[0]
    # Resample to fixed 16 bins
    n_bins = 16
    if len(rms) >= n_bins:
        indices = np.linspace(0, len(rms) - 1, n_bins).astype(int)
        env = rms[indices]
    else:
        env = np.pad(rms, (0, n_bins - len(rms)))
    peak = env.max()
    if peak > 0:
        env = env / peak
    return env


def fingerprint_spectral_contrast(chunk: np.ndarray, sr: int) -> np.ndarray:
    """Spectral contrast — peak/valley per frequency band."""
    contrast = librosa.feature.spectral_contrast(y=chunk, sr=sr)
    return contrast.mean(axis=1)


def fingerprint_midi(chunk: np.ndarray, sr: int, n_bins: int = 32) -> np.ndarray:
    """MIDI pitch contour fingerprint — quantized pitch over time.

    Uses pyin to extract f0, converts to MIDI note numbers, then
    resamples to n_bins time slots. Unvoiced frames are 0.
    This captures melodic/harmonic structure independent of timbre.
    """
    fmax = min(librosa.note_to_hz('C7'), sr / 2 - 1)
    f0, voiced_flag, voiced_prob = librosa.pyin(
        chunk, fmin=librosa.note_to_hz('C2'),
        fmax=fmax, sr=sr)
    # Convert to MIDI note numbers (0 for unvoiced)
    midi = np.zeros_like(f0)
    voiced = ~np.isnan(f0) & (f0 > 0)
    if voiced.any():
        midi[voiced] = librosa.hz_to_midi(f0[voiced])
    # Resample to fixed n_bins
    if len(midi) >= n_bins:
        indices = np.linspace(0, len(midi) - 1, n_bins).astype(int)
        result = midi[indices]
    else:
        result = np.pad(midi, (0, n_bins - len(midi)))
    # Normalize to 0-1 range (MIDI notes typically 36-96)
    result = result / 127.0
    return result


SIMILARITY_METRICS = {
    "mfcc": fingerprint_mfcc,
    "chroma": fingerprint_chroma,
    "rhythm": fingerprint_rhythm,
    "rms": fingerprint_rms_envelope,
    "spectral": fingerprint_spectral_contrast,
    "midi": fingerprint_midi,
}


def compute_fingerprints(all_chunks: list[np.ndarray], sr: int,
                         metrics: list[str]) -> np.ndarray:
    """Compute combined fingerprint vectors for a list of audio chunks.

    Each metric is L2-normalized before concatenation so they contribute
    equally regardless of scale.
    """
    n = len(all_chunks)
    parts = []

    for metric_name in metrics:
        fn = SIMILARITY_METRICS[metric_name]
        vecs = []
        for chunk in all_chunks:
            if metric_name == "mfcc":
                v = fn(chunk, sr, n_mfcc=15)
            else:
                v = fn(chunk, sr)
            vecs.append(v)
        mat = np.array(vecs, dtype=np.float64)
        # L2 normalize per-metric so each has equal weight
        norms = np.linalg.norm(mat, axis=1, keepdims=True)
        norms[norms == 0] = 1
        mat = mat / norms
        parts.append(mat)

    return np.hstack(parts)


# ---------------------------------------------------------------------------
# Intro detection
# ---------------------------------------------------------------------------

def detect_intro(stems: dict[str, StemAnalysis], beat_grid: dict,
                 bars_per_loop: int = 2, target_sr: int = 8000,
                 method: str = "auto") -> float:
    """Detect where the regular pattern starts (skip intro).

    Analyzes onset regularity and RMS energy across bars to find the
    first bar where repetitive structure begins. Returns the start
    time in seconds (0 = no intro detected).

    Strategy:
      1. Chop all stems into bar-sized chunks at bar boundaries
      2. For each bar, compute a combined fingerprint (RMS + onset density)
      3. Compare consecutive bar-groups (loop-sized) via self-similarity
      4. The intro ends where self-similarity first exceeds a threshold
         (i.e., where bars start repeating)
    """
    beat_period = beat_grid["beat_period"]
    bar_dur = beat_period * 4  # 4/4 time
    loop_dur = bar_dur * bars_per_loop

    # Mix all stems to mono for analysis
    max_len = max(len(s.y) for s in stems.values())
    mixed = np.zeros(max_len, dtype=np.float32)
    for s in stems.values():
        if len(s.y) < max_len:
            padded = np.pad(s.y, (0, max_len - len(s.y)))
        else:
            padded = s.y[:max_len]
        mixed += padded
    sr = list(stems.values())[0].sr

    # Resample to target
    if sr != target_sr:
        mixed = librosa.resample(mixed, orig_sr=sr, target_sr=target_sr)
        sr = target_sr

    loop_samples = int(loop_dur * sr)
    num_loops = int(len(mixed) / loop_samples)
    if num_loops < 3:
        return 0.0  # Too short to detect intro

    # Compute per-loop fingerprints (onset density + RMS + chroma)
    fps = []
    for i in range(num_loops):
        chunk = mixed[i * loop_samples:(i + 1) * loop_samples]
        # Onset density
        oenv = librosa.onset.onset_strength(y=chunk, sr=sr)
        onset_density = np.mean(oenv)
        # RMS
        rms = np.sqrt(np.mean(chunk ** 2))
        # Simple chroma vector
        chroma = librosa.feature.chroma_stft(y=chunk, sr=sr)
        chroma_mean = chroma.mean(axis=1)
        fp = np.concatenate([[onset_density, rms], chroma_mean])
        # L2 normalize
        norm = np.linalg.norm(fp)
        if norm > 0:
            fp = fp / norm
        fps.append(fp)

    fps = np.array(fps)

    # Compute self-similarity: distance of each loop to the next loop
    similarities = []
    for i in range(num_loops - 1):
        dist = np.linalg.norm(fps[i] - fps[i + 1])
        similarities.append(dist)

    # Also compute distance of each loop to the "body" (median of loops 2..end)
    if num_loops > 4:
        body_fp = np.median(fps[2:], axis=0)
        body_dists = [np.linalg.norm(fp - body_fp) for fp in fps]
    else:
        body_dists = [0.0] * num_loops

    # Find intro boundary: where distance-to-body drops below threshold
    if body_dists:
        median_body_dist = np.median(body_dists[2:]) if len(body_dists) > 2 else body_dists[-1]
        threshold = median_body_dist * 2.0  # intro loops are > 2x further from body
        intro_end_loop = 0
        for i in range(num_loops):
            if body_dists[i] > threshold:
                intro_end_loop = i + 1
            else:
                break
    else:
        intro_end_loop = 0

    intro_time = intro_end_loop * loop_dur

    if intro_end_loop > 0:
        print(f"\n  Intro detected: {intro_end_loop} loop(s) = {intro_time:.2f}s",
              file=sys.stderr)
        print(f"    Body starts at bar {intro_end_loop * bars_per_loop} "
              f"(loop {intro_end_loop})", file=sys.stderr)
        print(f"    Body distances: {['%.3f' % d for d in body_dists[:min(8, num_loops)]]}",
              file=sys.stderr)
    else:
        print(f"\n  No intro detected (all loops similar)", file=sys.stderr)

    return intro_time


# ---------------------------------------------------------------------------
# Loop detection
# ---------------------------------------------------------------------------

def detect_loops(stems: dict[str, StemAnalysis], beat_grid: dict,
                 target_sr: int = 8000, bars_per_loop: int = 2,
                 distance_threshold: float = 35.0,
                 metrics: list[str] = None,
                 skip_intro_sec: float = 0.0) -> LoopLibrary:
    """Chop stems into loops (N bars each), fingerprint, deduplicate.

    Args:
        bars_per_loop: how many bars per loop/pattern (default 2)
        distance_threshold: <=0 means no dedup (every loop unique)
        metrics: list of similarity metrics to use (default: ["mfcc"])
        skip_intro_sec: skip this many seconds from the start (intro)
    """
    if metrics is None:
        metrics = ["mfcc"]

    loop_dur = beat_grid["beat_period"] * 4 * bars_per_loop  # 4/4 time
    loop_samples = int(loop_dur * target_sr)
    skip_samples = int(skip_intro_sec * target_sr)

    all_loops = []      # (stem_idx, loop_idx, pcm)
    stem_names = list(stems.keys())

    for stem_idx, name in enumerate(stem_names):
        y = stems[name].y
        sr = stems[name].sr

        # Resample to target
        if sr != target_sr:
            y_rs = librosa.resample(y, orig_sr=sr, target_sr=target_sr)
        else:
            y_rs = y

        # Skip intro
        if skip_samples > 0:
            y_rs = y_rs[skip_samples:]

        num_loops = int(len(y_rs) / loop_samples)
        for loop_idx in range(num_loops):
            start = loop_idx * loop_samples
            end = start + loop_samples
            chunk = y_rs[start:end]
            if len(chunk) < loop_samples:
                chunk = np.pad(chunk, (0, loop_samples - len(chunk)))

            all_loops.append((stem_idx, loop_idx, chunk))

    if not all_loops:
        return LoopLibrary([], [], [], target_sr, loop_dur, loop_samples)

    n = len(all_loops)
    num_stems = len(stem_names)

    loop_dur_str = f"{bars_per_loop} bar{'s' if bars_per_loop > 1 else ''}"
    print(f"  Loop size: {loop_dur_str} = {loop_dur:.2f}s = "
          f"{loop_samples} samples @ {target_sr}Hz", file=sys.stderr)

    # No dedup: threshold <= 0 — every loop is unique
    if distance_threshold <= 0:
        loops = [lp[2] for lp in all_loops]
        sequences = [[] for _ in range(num_stems)]
        loop_ids_per_stem = [[] for _ in range(num_stems)]

        for lid, (stem_idx, loop_idx, _) in enumerate(all_loops):
            while len(sequences[stem_idx]) <= loop_idx:
                sequences[stem_idx].append(0)
            sequences[stem_idx][loop_idx] = lid
            loop_ids_per_stem[stem_idx].append(lid)

        print(f"\n  No dedup: {n} loops = {n} unique", file=sys.stderr)
        for si, name in enumerate(stem_names):
            if loop_ids_per_stem[si]:
                print(f"    {name}: {len(loop_ids_per_stem[si])} loops",
                      file=sys.stderr)
    else:
        # Compute fingerprints using selected metrics
        print(f"  Fingerprinting with: {', '.join(metrics)}...",
              file=sys.stderr)
        chunks = [lp[2] for lp in all_loops]
        vecs = compute_fingerprints(chunks, target_sr, metrics)

        # Agglomerative clustering via greedy merge
        cluster_id = list(range(n))

        while True:
            best_dist = float('inf')
            best_i, best_j = -1, -1

            active_map = {}
            for idx in range(n):
                cid = cluster_id[idx]
                if cid not in active_map:
                    active_map[cid] = []
                active_map[cid].append(idx)

            active_ids = list(active_map.keys())
            for ai in range(len(active_ids)):
                for aj in range(ai + 1, len(active_ids)):
                    ci, cj = active_ids[ai], active_ids[aj]
                    # Same stem loops can merge; cross-stem cannot
                    stems_i = set(all_loops[m][0] for m in active_map[ci])
                    stems_j = set(all_loops[m][0] for m in active_map[cj])
                    if stems_i != stems_j:
                        continue

                    ci_vec = np.mean(vecs[active_map[ci]], axis=0)
                    cj_vec = np.mean(vecs[active_map[cj]], axis=0)
                    dist = np.linalg.norm(ci_vec - cj_vec)
                    if dist < best_dist:
                        best_dist = dist
                        best_i, best_j = ci, cj

            if best_dist > distance_threshold or best_i < 0:
                break

            for idx in range(n):
                if cluster_id[idx] == best_j:
                    cluster_id[idx] = best_i

        # Build unique loops — pick representative (highest RMS) per cluster
        cluster_map = {}
        for idx in range(n):
            cid = cluster_id[idx]
            if cid not in cluster_map:
                cluster_map[cid] = []
            cluster_map[cid].append(idx)

        loops = []
        loop_id_map = {}
        loop_stem_map = {}

        for cid, member_indices in sorted(cluster_map.items()):
            best_idx = max(member_indices,
                           key=lambda i: np.sqrt(np.mean(all_loops[i][2] ** 2)))
            lid = len(loops)
            loop_id_map[cid] = lid
            loops.append(all_loops[best_idx][2])
            loop_stem_map[lid] = all_loops[best_idx][0]

        sequences = [[] for _ in range(num_stems)]
        loop_ids_per_stem = [[] for _ in range(num_stems)]

        for idx in range(n):
            stem_idx, loop_idx, _ = all_loops[idx]
            lid = loop_id_map[cluster_id[idx]]
            while len(sequences[stem_idx]) <= loop_idx:
                sequences[stem_idx].append(0)
            sequences[stem_idx][loop_idx] = lid

        for lid, stem_idx in loop_stem_map.items():
            loop_ids_per_stem[stem_idx].append(lid)

        print(f"\n  Loop dedup: {n} → {len(loops)} unique loops",
              file=sys.stderr)
        for si, name in enumerate(stem_names):
            if loop_ids_per_stem[si]:
                print(f"    {name}: {len(loop_ids_per_stem[si])} loops",
                      file=sys.stderr)

    return LoopLibrary(
        loops=loops,
        sequence=sequences,
        loop_ids_per_stem=loop_ids_per_stem,
        sample_rate=target_sr,
        bar_duration_sec=loop_dur,
        bar_samples=loop_samples,
    )


def loops_to_pcm8(loops: list[np.ndarray],
                  global_peak: float = None) -> list[bytes]:
    """Convert float32 loop arrays to 8-bit signed PCM.

    If global_peak is given, all loops are scaled by that single value
    (preserves relative loudness between loops). Otherwise uses the
    global max across all loops.
    """
    if global_peak is None:
        global_peak = max((np.abs(lp).max() for lp in loops), default=1.0)
    if global_peak == 0:
        global_peak = 1.0

    pcm_list = []
    for loop in loops:
        scaled = loop / global_peak
        samples = np.clip(scaled * 127, -128, 127).astype(np.int8)
        pcm_list.append(samples.tobytes())
    return pcm_list


def render_wav(lib: LoopLibrary, output_path: str, stem_names: list[str]):
    """Offline render: mix all loops into a WAV file.

    Also writes per-stem WAVs as <output_path>.stem_name.wav for analysis.
    Simulates exactly what the HTML player does: 8-bit PCM loops placed
    on a timeline at loop boundaries.
    """
    sr = lib.sample_rate
    num_stems = len(lib.sequence)
    num_loops_max = max((len(seq) for seq in lib.sequence if seq), default=0)
    total_samples = num_loops_max * lib.bar_samples

    if total_samples == 0:
        print("  Nothing to render.", file=sys.stderr)
        return

    # Convert loops to 8-bit then back to float — this is what Agon hears
    pcm8 = loops_to_pcm8(lib.loops)
    loops_f32 = []
    for pcm in pcm8:
        arr = np.frombuffer(pcm, dtype=np.int8).astype(np.float32) / 128.0
        loops_f32.append(arr)

    # Render per-stem tracks
    stem_tracks = []
    base = Path(output_path).stem
    base_dir = Path(output_path).parent

    for si in range(num_stems):
        track = np.zeros(total_samples, dtype=np.float32)
        seq = lib.sequence[si] if si < len(lib.sequence) else []
        for loop_idx, loop_id in enumerate(seq):
            if loop_id >= len(loops_f32):
                continue
            start = loop_idx * lib.bar_samples
            chunk = loops_f32[loop_id]
            end = min(start + len(chunk), total_samples)
            track[start:end] = chunk[:end - start]
        stem_tracks.append(track)

        # Write per-stem WAV
        name = stem_names[si] if si < len(stem_names) else f"ch{si}"
        stem_path = base_dir / f"{base}.{name.replace(' ', '_')}.wav"
        sf.write(str(stem_path), track, sr)
        peak = np.abs(track).max()
        rms = np.sqrt(np.mean(track ** 2))
        # Check for clipping (consecutive max values)
        clips = np.sum(np.abs(track) >= 0.99)
        print(f"    {name}: peak={peak:.3f} rms={rms:.4f} clips={clips}",
              file=sys.stderr)

    # Mix all stems
    mix = np.zeros(total_samples, dtype=np.float32)
    for track in stem_tracks:
        mix += track

    # Normalize mix to prevent clipping
    peak = np.abs(mix).max()
    if peak > 0:
        mix = mix / peak * 0.95

    sf.write(output_path, mix, sr)
    print(f"\n  Rendered: {output_path} ({total_samples/sr:.1f}s, "
          f"{sr}Hz, peak={peak:.2f})", file=sys.stderr)
    print(f"  + {num_stems} stem WAVs in {base_dir}/", file=sys.stderr)


def ableton_frame_commands(lib: LoopLibrary, beat_grid: dict,
                           num_frames: int,
                           buffer_base: int = 2000) -> list[list[bytes]]:
    """Generate per-frame VDP commands for Ableton sample playback.

    At each bar boundary, trigger the appropriate sample loop on each channel.
    """
    frame_cmds: list[list[bytes]] = [[] for _ in range(num_frames)]
    fps = beat_grid["video_fps"]
    bar_dur = lib.bar_duration_sec
    dur_ms = int(bar_dur * 1000)

    for stem_idx in range(len(lib.sequence)):
        seq = lib.sequence[stem_idx]
        if not seq:
            continue

        for bar_idx, loop_id in enumerate(seq):
            bar_time = bar_idx * bar_dur
            frame = int(round(bar_time * fps))
            if frame < 0 or frame >= num_frames:
                continue

            buf_id = buffer_base + loop_id
            cmds = frame_cmds[frame]

            # Set waveform to sample buffer, then play
            cmds.append(vdu_set_waveform_sample(stem_idx, buf_id))
            cmds.append(vdu_play_note(stem_idx, 100,
                                      lib.sample_rate, dur_ms))

    return frame_cmds


# ---------------------------------------------------------------------------
# VDP data output
# ---------------------------------------------------------------------------

def generate_vdp_setup(mode: str, num_channels: int = 3,
                       stems: dict[str, StemAnalysis] = None,
                       loop_lib: LoopLibrary = None,
                       buffer_base: int = 2000) -> bytes:
    """Generate VDP setup commands (channel enable, waveforms, sample uploads)."""
    out = bytearray()

    # Enable all channels needed (0-2 are on by default, enable 3+)
    for ch in range(num_channels):
        if ch >= 3:
            out.extend(vdu_enable_channel(ch))

    if mode == "ay" and stems:
        # Set initial waveforms from AY profiles
        for ch, name in enumerate(stems.keys()):
            profile = AY_PROFILES.get(name, AY_PROFILES["other"])
            out.extend(vdu_set_waveform(ch, profile[0]))

    elif mode == "ableton" and loop_lib:
        # Upload PCM loops as VDP buffers
        pcm_list = loops_to_pcm8(loop_lib.loops)
        for i, pcm in enumerate(pcm_list):
            buf_id = buffer_base + i
            out.extend(wrap_vdp_buffer(buf_id, pcm))
            # Create sample from buffer
            out.extend(vdu_sample_from_buffer(
                0, buf_id, fmt=0, sample_rate=loop_lib.sample_rate))

    return bytes(out)


def write_output(path: str, setup: bytes,
                 frame_cmds: list[list[bytes]],
                 video_fps: float):
    """Write standalone VDP audio data file.

    Format:
      Header: "MUSC" + version(1B) + mode(1B) + fps(1B) + num_frames(2B LE)
      Setup block: len(4B LE) + setup_bytes
      Frame blocks: [num_cmds(2B LE) + [cmd_len(2B LE) + cmd_bytes]...]
    """
    with open(path, 'wb') as f:
        num_frames = len(frame_cmds)
        # Header
        f.write(b"MUSC")
        f.write(struct.pack("<BBH", 1, int(video_fps), num_frames))

        # Setup block
        f.write(struct.pack("<I", len(setup)))
        f.write(setup)

        # Frame blocks
        for cmds in frame_cmds:
            f.write(struct.pack("<H", len(cmds)))
            for cmd in cmds:
                f.write(struct.pack("<H", len(cmd)))
                f.write(cmd)


# ---------------------------------------------------------------------------
# HTML preview
# ---------------------------------------------------------------------------

def _channel_colors(n: int) -> list[str]:
    """Generate n visually distinct channel colors."""
    base = ['#f44', '#4f4', '#44f', '#f4f', '#fa0', '#0cf', '#f84',
            '#8f4', '#84f', '#ff4', '#4ff', '#f48']
    # Extend with HSL rotation if needed
    colors = list(base)
    while len(colors) < n:
        hue = (len(colors) * 137) % 360  # golden angle
        colors.append(f'hsl({hue},70%,55%)')
    return colors[:n]


def generate_html_ay(events: list[AYEvent], beat_grid: dict,
                     total_duration: float,
                     stem_names: list[str] = None) -> str:
    """Generate HTML with Web Audio API oscillators for AY-chip preview."""
    if stem_names is None:
        num_channels = max((ev.channel for ev in events), default=0) + 1 if events else 0
        stem_names = [f"ch{i}" for i in range(num_channels)]
    num_channels = len(stem_names)
    colors = _channel_colors(num_channels)
    canvas_h = max(200, num_channels * 40)

    # Convert events to JSON-friendly format
    events_json = []
    for ev in events:
        events_json.append({
            "t": round(ev.time, 4),
            "ch": ev.channel,
            "f": ev.freq,
            "v": ev.volume,
            "d": ev.duration_ms,
            "w": ev.waveform,
            "atk": ev.attack_ms,
            "dec": ev.decay_ms,
            "sus": ev.sustain,
            "rel": ev.release_ms,
        })

    beats_json = [round(float(t), 4) for t in beat_grid["beat_times"]]
    bars_json = [round(float(t), 4) for t in beat_grid["bar_times"]]

    # Build channel legend HTML
    legend_parts = []
    for i, name in enumerate(stem_names):
        legend_parts.append(
            f'<span style="display:inline-block;width:12px;height:12px;'
            f'background:{colors[i]};border-radius:2px;vertical-align:middle;'
            f'margin:0 4px 0 8px"></span>{name}')
    legend_html = "\n  ".join(legend_parts)

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Bad Apple Music — AY-chip Preview</title>
<style>
  body {{ background: #111; color: #ccc; font-family: monospace; margin: 20px; }}
  canvas {{ border: 1px solid #333; display: block; margin: 10px 0; }}
  button {{ font: 14px monospace; padding: 6px 16px; margin: 4px; cursor: pointer;
           background: #333; color: #ccc; border: 1px solid #555; }}
  button:hover {{ background: #444; }}
  .info {{ color: #888; font-size: 12px; margin: 4px 0; }}
</style>
</head>
<body>
<h3>Bad Apple Music — AY-chip Synthesis Preview ({num_channels} channels)</h3>
<p class="info">
  BPM: {beat_grid['bpm']:.0f} |
  Duration: {total_duration:.1f}s |
  Events: {len(events_json)} |
  Channels: {legend_html}
</p>
<div>
  <button id="playBtn" onclick="togglePlay()">Play</button>
  <button onclick="stop()">Stop</button>
  <span id="timeDisplay" class="info">0.0s</span>
</div>
<canvas id="timeline" width="1200" height="{canvas_h}"></canvas>
<script>
const EVENTS = {json.dumps(events_json, separators=(',', ':'))};
const BEATS = {json.dumps(beats_json, separators=(',', ':'))};
const BARS = {json.dumps(bars_json, separators=(',', ':'))};
const DURATION = {total_duration:.4f};
const BPM = {beat_grid['bpm']:.1f};
const NUM_CH = {num_channels};

const WF_MAP = {{0:'square', 1:'triangle', 2:'sawtooth', 3:'sine', 4:'square'}};
const CH_COLORS = {json.dumps(colors, separators=(',', ':'))};
const CH_NAMES = {json.dumps(stem_names, separators=(',', ':'))};

let actx = null;
let playing = false;
let startTime = 0;
let scheduledNodes = [];
let animFrame = null;

function getAudioContext() {{
  if (!actx) actx = new AudioContext();
  return actx;
}}

function togglePlay() {{
  if (playing) {{ stop(); return; }}
  play();
}}

function play() {{
  const ctx = getAudioContext();
  if (ctx.state === 'suspended') ctx.resume();
  stop();
  playing = true;
  document.getElementById('playBtn').textContent = 'Pause';
  startTime = ctx.currentTime;

  // Schedule all events
  for (const ev of EVENTS) {{
    const t = startTime + ev.t;
    const dur = ev.d / 1000;
    const freq = ev.f;
    const vol = ev.v / 127;

    // Attack/decay/sustain/release envelope
    const atk = ev.atk / 1000;
    const dec = ev.dec / 1000;
    const sus = (ev.sus / 127) * vol;
    const rel = ev.rel / 1000;

    const osc = ctx.createOscillator();
    const gain = ctx.createGain();

    if (ev.w === 4) {{
      // Noise: use short buffer of random samples
      const bufLen = ctx.sampleRate * dur;
      const noiseBuf = ctx.createBuffer(1, Math.max(1, bufLen), ctx.sampleRate);
      const data = noiseBuf.getChannelData(0);
      for (let i = 0; i < data.length; i++) data[i] = Math.random() * 2 - 1;
      const src = ctx.createBufferSource();
      src.buffer = noiseBuf;

      // Apply bandpass for drum character
      const filter = ctx.createBiquadFilter();
      filter.type = freq < 150 ? 'lowpass' : freq < 5000 ? 'bandpass' : 'highpass';
      filter.frequency.value = freq;
      filter.Q.value = freq < 150 ? 1 : 2;

      gain.gain.setValueAtTime(0, t);
      gain.gain.linearRampToValueAtTime(vol, t + atk);
      gain.gain.linearRampToValueAtTime(sus, t + atk + dec);
      gain.gain.setValueAtTime(sus, t + dur - rel);
      gain.gain.linearRampToValueAtTime(0, t + dur);

      src.connect(filter).connect(gain).connect(ctx.destination);
      src.start(t);
      src.stop(t + dur);
      scheduledNodes.push(src, gain, filter);
      continue;
    }}

    osc.type = WF_MAP[ev.w] || 'square';
    osc.frequency.value = freq;

    // ADSR envelope
    gain.gain.setValueAtTime(0, t);
    gain.gain.linearRampToValueAtTime(vol, t + atk);
    gain.gain.linearRampToValueAtTime(sus, t + atk + dec);
    gain.gain.setValueAtTime(sus, t + dur - rel);
    gain.gain.linearRampToValueAtTime(0, t + dur);

    osc.connect(gain).connect(ctx.destination);
    osc.start(t);
    osc.stop(t + dur + 0.01);
    scheduledNodes.push(osc, gain);
  }}

  drawLoop();
}}

function stop() {{
  playing = false;
  document.getElementById('playBtn').textContent = 'Play';
  for (const node of scheduledNodes) {{
    try {{ node.stop && node.stop(); }} catch(e) {{}}
    try {{ node.disconnect(); }} catch(e) {{}}
  }}
  scheduledNodes = [];
  if (animFrame) {{ cancelAnimationFrame(animFrame); animFrame = null; }}
}}

function drawLoop() {{
  if (!playing) return;
  const ctx = getAudioContext();
  const elapsed = ctx.currentTime - startTime;

  document.getElementById('timeDisplay').textContent =
    elapsed.toFixed(1) + 's / ' + DURATION.toFixed(1) + 's';

  if (elapsed > DURATION + 1) {{ stop(); return; }}

  drawTimeline(elapsed);
  animFrame = requestAnimationFrame(drawLoop);
}}

function drawTimeline(elapsed) {{
  const c = document.getElementById('timeline');
  const ctx = c.getContext('2d');
  const W = c.width, H = c.height;
  ctx.fillStyle = '#111';
  ctx.fillRect(0, 0, W, H);

  const pps = W / DURATION;  // pixels per second

  // Bar lines
  ctx.strokeStyle = '#333';
  ctx.lineWidth = 1;
  for (const t of BARS) {{
    const x = t * pps;
    ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, H); ctx.stroke();
  }}

  // Beat lines (lighter)
  ctx.strokeStyle = '#222';
  for (const t of BEATS) {{
    const x = t * pps;
    ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, H); ctx.stroke();
  }}

  // Events as rectangles
  const chH = H / NUM_CH;
  for (const ev of EVENTS) {{
    const x = ev.t * pps;
    const w = Math.max(2, (ev.d / 1000) * pps);
    const y = ev.ch * chH;
    const alpha = ev.v / 127;
    ctx.fillStyle = CH_COLORS[ev.ch % CH_COLORS.length];
    ctx.globalAlpha = 0.3 + alpha * 0.7;
    ctx.fillRect(x, y + 2, w, chH - 4);
  }}
  ctx.globalAlpha = 1;

  // Playhead
  ctx.strokeStyle = '#fff';
  ctx.lineWidth = 2;
  const px = elapsed * pps;
  ctx.beginPath(); ctx.moveTo(px, 0); ctx.lineTo(px, H); ctx.stroke();

  // Channel labels
  ctx.font = '11px monospace';
  for (let i = 0; i < NUM_CH; i++) {{
    ctx.fillStyle = CH_COLORS[i % CH_COLORS.length];
    ctx.fillText(CH_NAMES[i] || ('ch'+i), 4, i * chH + 14);
  }}
}}
</script>
</body>
</html>"""


def generate_html_ableton(lib: LoopLibrary, beat_grid: dict,
                          stems: dict[str, StemAnalysis],
                          total_duration: float) -> str:
    """Generate HTML with Web Audio API sample playback for Ableton preview."""
    stem_names = list(stems.keys())
    num_channels = len(stem_names)
    colors = _channel_colors(num_channels)
    canvas_h = max(200, num_channels * 40)

    # Convert loops to base64-encoded PCM for embedding
    pcm_list = loops_to_pcm8(lib.loops)

    # Encode as arrays of signed int8 values
    loops_json = []
    for pcm in pcm_list:
        arr = np.frombuffer(pcm, dtype=np.int8)
        loops_json.append(arr.tolist())

    seq_json = [seq for seq in lib.sequence]
    bars_json = [round(float(t), 4) for t in beat_grid["bar_times"]]
    beats_json = [round(float(t), 4) for t in beat_grid["beat_times"]]

    # Build channel legend HTML
    legend_parts = []
    for i, name in enumerate(stem_names):
        legend_parts.append(
            f'<span style="display:inline-block;width:12px;height:12px;'
            f'background:{colors[i]};border-radius:2px;vertical-align:middle;'
            f'margin:0 4px 0 8px"></span>{name}')
    legend_html = "\n  ".join(legend_parts)

    # Build mixer HTML — mute/solo buttons per channel
    mixer_rows = []
    for i, name in enumerate(stem_names):
        mixer_rows.append(
            f'<div class="mixer-ch" id="mixCh{i}">'
            f'<span class="ch-dot" style="background:{colors[i]}"></span>'
            f'<span class="ch-name">{name}</span>'
            f'<button class="mbtn" onclick="toggleMute({i})">M</button>'
            f'<button class="sbtn" onclick="toggleSolo({i})">S</button>'
            f'<input type="range" min="0" max="100" value="{int(min(70, 100 / num_channels))}" '
            f'class="vol-slider" oninput="setVolume({i},this.value)">'
            f'</div>')
    mixer_html = "\n    ".join(mixer_rows)

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Bad Apple Music — Ableton Loop Preview</title>
<style>
  body {{ background: #111; color: #ccc; font-family: monospace; margin: 20px; }}
  canvas {{ border: 1px solid #333; display: block; margin: 10px 0; }}
  button {{ font: 13px monospace; padding: 4px 12px; margin: 2px; cursor: pointer;
           background: #333; color: #ccc; border: 1px solid #555; }}
  button:hover {{ background: #444; }}
  .info {{ color: #888; font-size: 12px; margin: 4px 0; }}
  #transport {{ margin: 8px 0; }}
  #transport button {{ padding: 6px 16px; }}
  .mixer {{ display: flex; flex-wrap: wrap; gap: 4px; margin: 8px 0; }}
  .mixer-ch {{ display: flex; align-items: center; gap: 4px; padding: 4px 8px;
               background: #1a1a1a; border-radius: 4px; }}
  .ch-dot {{ width: 10px; height: 10px; border-radius: 2px; flex-shrink: 0; }}
  .ch-name {{ width: 100px; font-size: 11px; overflow: hidden; text-overflow: ellipsis;
              white-space: nowrap; }}
  .mbtn {{ font-size: 11px; padding: 2px 6px; min-width: 24px; }}
  .sbtn {{ font-size: 11px; padding: 2px 6px; min-width: 24px; }}
  .mbtn.active {{ background: #a33; color: #fff; }}
  .sbtn.active {{ background: #3a3; color: #fff; }}
  .muted .ch-name {{ opacity: 0.3; }}
  .vol-slider {{ width: 60px; height: 4px; }}
</style>
</head>
<body>
<h3>Ableton Loop Preview ({num_channels}ch, {lib.sample_rate}Hz, {lib.bar_duration_sec:.1f}s/loop)</h3>
<p class="info">
  BPM: {beat_grid['bpm']:.0f} |
  Duration: {total_duration:.1f}s |
  Loops: {len(lib.loops)} unique
</p>
<div id="transport">
  <button id="playBtn" onclick="togglePlay()">Play</button>
  <button onclick="stop()">Stop</button>
  <span id="timeDisplay" class="info">0.0s</span>
</div>
<div class="mixer">
    {mixer_html}
</div>
<canvas id="timeline" width="1200" height="{canvas_h}"></canvas>
<script>
const LOOPS_PCM = {json.dumps(loops_json, separators=(',', ':'))};
const SEQUENCES = {json.dumps(seq_json, separators=(',', ':'))};
const SAMPLE_RATE = {lib.sample_rate};
const BAR_DUR = {lib.bar_duration_sec:.6f};
const BEATS = {json.dumps(beats_json, separators=(',', ':'))};
const BARS = {json.dumps(bars_json, separators=(',', ':'))};
const DURATION = {total_duration:.4f};
const NUM_CH = {num_channels};
const CH_COLORS = {json.dumps(colors, separators=(',', ':'))};
const STEM_NAMES = {json.dumps(stem_names, separators=(',', ':'))};

let actx = null;
let audioBuffers = [];
let playing = false;
let startTime = 0;
let scheduledNodes = [];  // list of objects
let animFrame = null;

// Mixer state
const chMuted = new Array(NUM_CH).fill(false);
const chSolo = new Array(NUM_CH).fill(false);
const chVolume = new Array(NUM_CH).fill(Math.min(0.7, 1.0 / NUM_CH));
let masterGain = null;

function getAudioContext() {{
  if (!actx) {{
    actx = new AudioContext({{latencyHint: 'playback', sampleRate: 44100}});
    masterGain = actx.createDynamicsCompressor();
    masterGain.threshold.value = -3;
    masterGain.knee.value = 6;
    masterGain.ratio.value = 12;
    masterGain.connect(actx.destination);
  }}
  return actx;
}}

function initBuffers() {{
  const ctx = getAudioContext();
  audioBuffers = [];
  for (const pcm of LOOPS_PCM) {{
    const buf = ctx.createBuffer(1, pcm.length, SAMPLE_RATE);
    const data = buf.getChannelData(0);
    for (let i = 0; i < pcm.length; i++) data[i] = pcm[i] / 128;
    audioBuffers.push(buf);
  }}
}}

function getEffectiveGain(ch) {{
  if (chMuted[ch]) return 0;
  const anySolo = chSolo.some(s => s);
  if (anySolo && !chSolo[ch]) return 0;
  return chVolume[ch];
}}

function updateAllGains() {{
  for (const item of scheduledNodes) {{
    if (item.gain && item.ch !== undefined) {{
      item.gain.gain.value = getEffectiveGain(item.ch);
    }}
  }}
  updateMixerUI();
}}

function toggleMute(ch) {{
  chMuted[ch] = !chMuted[ch];
  // Clear solo if muting
  if (chMuted[ch]) chSolo[ch] = false;
  updateAllGains();
}}

function toggleSolo(ch) {{
  chSolo[ch] = !chSolo[ch];
  // Clear mute if soloing
  if (chSolo[ch]) chMuted[ch] = false;
  updateAllGains();
}}

function setVolume(ch, val) {{
  chVolume[ch] = val / 100;
  updateAllGains();
}}

function updateMixerUI() {{
  const anySolo = chSolo.some(s => s);
  for (let i = 0; i < NUM_CH; i++) {{
    const el = document.getElementById('mixCh' + i);
    const mbtn = el.querySelector('.mbtn');
    const sbtn = el.querySelector('.sbtn');
    const muted = chMuted[i] || (anySolo && !chSolo[i]);
    el.classList.toggle('muted', muted);
    mbtn.classList.toggle('active', chMuted[i]);
    sbtn.classList.toggle('active', chSolo[i]);
  }}
}}

function togglePlay() {{
  if (playing) {{ stop(); return; }}
  play();
}}

function play() {{
  const ctx = getAudioContext();
  if (ctx.state === 'suspended') ctx.resume();
  stop();
  if (audioBuffers.length === 0) initBuffers();

  playing = true;
  document.getElementById('playBtn').textContent = 'Pause';
  startTime = ctx.currentTime;

  // Schedule bar triggers with per-channel gain nodes
  for (let ch = 0; ch < SEQUENCES.length; ch++) {{
    const seq = SEQUENCES[ch];
    for (let bar = 0; bar < seq.length; bar++) {{
      const loopId = seq[bar];
      if (loopId >= audioBuffers.length) continue;
      const t = startTime + bar * BAR_DUR;

      const src = ctx.createBufferSource();
      src.buffer = audioBuffers[loopId];
      const gain = ctx.createGain();
      gain.gain.value = getEffectiveGain(ch);
      src.connect(gain).connect(masterGain);
      src.start(t);
      src.stop(t + BAR_DUR);
      scheduledNodes.push({{src:src, gain:gain, ch:ch}});
    }}
  }}

  drawLoop();
}}

function stop() {{
  playing = false;
  document.getElementById('playBtn').textContent = 'Play';
  for (const item of scheduledNodes) {{
    try {{ item.src.stop(); }} catch(e) {{}}
    try {{ item.src.disconnect(); }} catch(e) {{}}
    try {{ item.gain.disconnect(); }} catch(e) {{}}
  }}
  scheduledNodes = [];
  if (animFrame) {{ cancelAnimationFrame(animFrame); animFrame = null; }}
}}

function drawLoop() {{
  if (!playing) return;
  const ctx = getAudioContext();
  const elapsed = ctx.currentTime - startTime;
  document.getElementById('timeDisplay').textContent =
    elapsed.toFixed(1) + 's / ' + DURATION.toFixed(1) + 's';
  if (elapsed > DURATION + 1) {{ stop(); return; }}
  drawTimeline(elapsed);
  animFrame = requestAnimationFrame(drawLoop);
}}

function drawTimeline(elapsed) {{
  const c = document.getElementById('timeline');
  const ctx = c.getContext('2d');
  const W = c.width, H = c.height;
  ctx.fillStyle = '#111';
  ctx.fillRect(0, 0, W, H);

  const pps = W / DURATION;
  const chH = H / NUM_CH;
  const anySolo = chSolo.some(s => s);

  // Bar lines
  ctx.strokeStyle = '#333';
  for (const t of BARS) {{
    const x = t * pps;
    ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, H); ctx.stroke();
  }}

  // Beat lines
  ctx.strokeStyle = '#222';
  for (const t of BEATS) {{
    const x = t * pps;
    ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, H); ctx.stroke();
  }}

  // Sequence blocks
  for (let ch = 0; ch < SEQUENCES.length; ch++) {{
    const muted = chMuted[ch] || (anySolo && !chSolo[ch]);
    const seq = SEQUENCES[ch];
    for (let bar = 0; bar < seq.length; bar++) {{
      const x = bar * BAR_DUR * pps;
      const w = BAR_DUR * pps;
      const y = ch * chH;
      ctx.fillStyle = CH_COLORS[ch % CH_COLORS.length];
      ctx.globalAlpha = muted ? 0.15 : 0.5;
      ctx.fillRect(x, y + 2, w - 1, chH - 4);
      ctx.globalAlpha = muted ? 0.3 : 1;
      ctx.fillStyle = '#fff';
      ctx.font = '9px monospace';
      ctx.fillText('L' + seq[bar], x + 3, y + chH / 2 + 3);
    }}
  }}
  ctx.globalAlpha = 1;

  // Playhead
  ctx.strokeStyle = '#fff';
  ctx.lineWidth = 2;
  ctx.beginPath();
  ctx.moveTo(elapsed * pps, 0);
  ctx.lineTo(elapsed * pps, H);
  ctx.stroke();

  // Labels
  ctx.font = '11px monospace';
  for (let i = 0; i < NUM_CH; i++) {{
    const muted = chMuted[i] || (anySolo && !chSolo[i]);
    ctx.fillStyle = CH_COLORS[i % CH_COLORS.length];
    ctx.globalAlpha = muted ? 0.3 : 1;
    ctx.fillText(STEM_NAMES[i] || ('ch'+i), 4, i * chH + 14);
  }}
  ctx.globalAlpha = 1;
}}

updateMixerUI();
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def guess_stem_type(path: str) -> str:
    """Guess stem type from filename.

    Handles numbered Suno stems like '2 Drums.wav' → 'drums',
    '0 Lead Vocals.wav' → 'lead vocals'.
    """
    name = Path(path).stem.lower()
    # Strip leading number prefix (e.g. "2 drums" → "drums")
    import re
    name = re.sub(r'^\d+\s*', '', name).strip()

    # Try exact match first, then substring
    for stem in KNOWN_STEMS:
        if name == stem:
            return stem
    for stem in KNOWN_STEMS:
        if stem in name:
            return stem
    # Use cleaned filename as stem name
    return name if name else None


def is_percussive(name: str) -> bool:
    """Check if stem type is percussive (no pitch tracking)."""
    return name in ("drums", "percussion")


def main():
    parser = argparse.ArgumentParser(
        description="Bad Apple music pipeline — WAV stems to VDP audio commands")
    parser.add_argument("--stems", nargs="+", required=True,
                        help="WAV stem files (auto-detected from filename)")
    parser.add_argument("--bpm", type=float, required=True,
                        help="Song tempo in BPM")
    parser.add_argument("--mode", choices=["ay", "ableton"], default="ay",
                        help="Synthesis mode (default: ay)")
    parser.add_argument("--analyze-only", action="store_true",
                        help="Print analysis only, no output")
    parser.add_argument("--html", type=str, default=None,
                        help="Write HTML preview file")
    parser.add_argument("--output", "-o", type=str, default=None,
                        help="Write VDP audio data file")
    parser.add_argument("--fps", type=int, default=DEFAULT_VIDEO_FPS,
                        help=f"Video frame rate (default: {DEFAULT_VIDEO_FPS})")
    parser.add_argument("--sample-rate", type=int, default=8000,
                        help="Ableton mode sample rate in Hz (default: 8000)")
    parser.add_argument("--bars-per-loop", type=int, default=2,
                        help="Bars per loop/pattern (default: 2)")
    parser.add_argument("--loop-threshold", type=float, default=0.3,
                        help="Distance threshold for loop dedup; 0 = no dedup (default: 0.3)")
    parser.add_argument("--render-wav", type=str, default=None,
                        help="Render offline mix to WAV (+ per-stem WAVs)")
    parser.add_argument("--similarity", type=str, default="mfcc,rhythm,chroma",
                        help="Similarity metrics, comma-separated: "
                             "mfcc,chroma,rhythm,rms,spectral,midi (default: mfcc,rhythm,chroma)")
    parser.add_argument("--skip-intro", action="store_true",
                        help="Auto-detect and skip irregular intro")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    if not args.analyze_only and not args.html and not args.output and not args.render_wav:
        parser.error("Specify --analyze-only, --html FILE, --render-wav FILE, and/or --output FILE")

    # -- Load and classify stems --
    print(f"Loading {len(args.stems)} stems...", file=sys.stderr)
    stems: dict[str, StemAnalysis] = {}
    used_names = set()

    for path in sorted(args.stems):
        name = guess_stem_type(path)
        if not name or name in used_names:
            # Disambiguate: append channel number
            base = name or Path(path).stem.lower()
            i = 2
            while f"{base}_{i}" in used_names:
                i += 1
            name = f"{base}_{i}"
        used_names.add(name)

        print(f"  ch{len(stems)} {name}: {path}", file=sys.stderr)
        y, sr = load_stem(path)
        do_pitch = not is_percussive(name)
        analysis = analyze_stem(y, sr, name, video_fps=args.fps,
                                do_pitch=do_pitch)
        stems[name] = analysis

    num_channels = len(stems)
    print(f"\n  {num_channels} channels", file=sys.stderr)

    # -- Beat grid --
    max_dur = max(s.duration for s in stems.values())
    beat_grid = build_beat_grid(args.bpm, max_dur, video_fps=args.fps)
    num_frames = int(math.ceil(max_dur * args.fps))

    print(f"\nBeat grid: {args.bpm} BPM, {len(beat_grid['beat_times'])} beats, "
          f"{len(beat_grid['bar_times'])} bars, {num_frames} frames",
          file=sys.stderr)

    # -- Analysis summary --
    for name in stems:
        print_analysis(stems[name], beat_grid)

    if args.analyze_only:
        print("\nDone (analysis only).", file=sys.stderr)
        return

    # -- Generate events/loops --
    if args.mode == "ay":
        print("\nGenerating AY-chip events...", file=sys.stderr)
        events = generate_all_ay_events(stems, beat_grid)
        frame_cmds = ay_events_to_frame_commands(events, num_frames)

        # Stats
        total_bytes = sum(sum(len(c) for c in cmds) for cmds in frame_cmds)
        active_frames = sum(1 for cmds in frame_cmds if cmds)
        print(f"  Total: {len(events)} events, {total_bytes} bytes, "
              f"{active_frames}/{num_frames} active frames", file=sys.stderr)

        if args.html:
            html = generate_html_ay(events, beat_grid, max_dur,
                                    stem_names=list(stems.keys()))
            with open(args.html, "w") as f:
                f.write(html)
            print(f"\nWrote HTML preview: {args.html}", file=sys.stderr)

        if args.output:
            setup = generate_vdp_setup("ay", num_channels=num_channels,
                                       stems=stems)
            write_output(args.output, setup, frame_cmds, args.fps)
            print(f"Wrote VDP data: {args.output}", file=sys.stderr)

    elif args.mode == "ableton":
        metrics = [m.strip() for m in args.similarity.split(",")]

        # Intro detection
        skip_intro_sec = 0.0
        if args.skip_intro:
            print("\nDetecting intro...", file=sys.stderr)
            skip_intro_sec = detect_intro(
                stems, beat_grid,
                bars_per_loop=args.bars_per_loop,
                target_sr=args.sample_rate)

        print(f"\nDetecting loops (Ableton mode, {args.bars_per_loop} bars/loop, "
              f"metrics={','.join(metrics)}"
              f"{f', skip={skip_intro_sec:.1f}s' if skip_intro_sec > 0 else ''})...",
              file=sys.stderr)
        lib = detect_loops(stems, beat_grid,
                           target_sr=args.sample_rate,
                           bars_per_loop=args.bars_per_loop,
                           distance_threshold=args.loop_threshold,
                           metrics=metrics,
                           skip_intro_sec=skip_intro_sec)
        frame_cmds = ableton_frame_commands(lib, beat_grid, num_frames)

        # Stats
        pcm_sizes = loops_to_pcm8(lib.loops)
        total_pcm = sum(len(p) for p in pcm_sizes)
        print(f"  PCM data: {total_pcm:,} bytes ({len(lib.loops)} loops × "
              f"{lib.bar_samples} samples @ {lib.sample_rate}Hz)",
              file=sys.stderr)

        if args.render_wav:
            print(f"\n  Rendering WAV...", file=sys.stderr)
            render_wav(lib, args.render_wav, list(stems.keys()))

        if args.html:
            html = generate_html_ableton(lib, beat_grid, stems, max_dur)
            with open(args.html, "w") as f:
                f.write(html)
            print(f"\nWrote HTML preview: {args.html}", file=sys.stderr)

        if args.output:
            setup = generate_vdp_setup("ableton", num_channels=num_channels,
                                       loop_lib=lib)
            write_output(args.output, setup, frame_cmds, args.fps)
            print(f"Wrote VDP data: {args.output}", file=sys.stderr)

    print("\nDone.", file=sys.stderr)


if __name__ == "__main__":
    main()
