# 009 — Music Pipeline: WAV Stems to VDP Audio

## Summary

Built an end-to-end pipeline (`tools/gen_music_vdp.py`) that converts WAV stems from
Suno AI into VDP audio commands for the Agon Light. Two synthesis modes: AY-chip
(waveform synthesis) and Ableton (loop-based PCM sampling). The Ableton mode is the
primary focus — it chops stems into 2-bar loops, fingerprints them using multiple
similarity metrics (MFCC + rhythm + chroma + MIDI), deduplicates via agglomerative
clustering, and outputs 8-bit PCM at 8kHz (Agon native sample rate).

## Pipeline Architecture

```
WAV stems (48kHz stereo) + BPM
      |
      v
  [Load & Mono mix & Resample to 22050Hz for analysis]
      |
      v
  [Onset detection, pyin pitch, RMS, spectral centroid per stem]
      |
      +---> AY-chip mode: onset/pitch -> waveform events -> VDP commands
      |
      +---> Ableton mode:
              |
              v
          [Intro detection] --skip irregular bars-->
              |
              v
          [Chop to 2-bar loops at beat grid boundaries]
              |
              v
          [Fingerprint: MFCC + rhythm + chroma + MIDI contour]
              |
              v
          [Agglomerative clustering -> unique loop library]
              |
              v
          [8-bit signed PCM @ 8kHz]
              |
              +---> HTML preview (Web Audio API, mute/solo/volume mixer)
              +---> WAV render (offline, per-stem + master)
              +---> VDP buffer upload (PSRAM) + VSYNC playback
```

## Key Features

### Intro Detection (`--skip-intro`)
Analyzes onset regularity and energy across loop-sized chunks. Computes a "body
fingerprint" (median of all loops except the first few) and flags early loops that
are >2x further from the body than the median. Skips these before loop detection.

- **cc-acid-a**: detected 1 loop (3.9s) intro, skipped
- **nn-q3**: no intro detected (consistent from the start)

### Multi-Metric Similarity Fingerprinting
Six fingerprint functions, each L2-normalized before concatenation:

| Metric | Dimensions | What it captures |
|--------|-----------|-----------------|
| `mfcc` | 15 | Timbral texture (spectral envelope) |
| `chroma` | 12 | Pitch class profile (key/harmony) |
| `rhythm` | 32 | Onset strength autocorrelation (beat pattern) |
| `rms` | 16 | Volume envelope shape |
| `spectral` | 7 | Spectral contrast (peak/valley per band) |
| `midi` | 32 | Pitch contour via pyin (melodic fingerprint) |

Default: `mfcc,rhythm,chroma` (59 dimensions). Adding `midi` gives 91 dimensions
but is slower (pyin per chunk).

### MIDI Pitch Contour Fingerprint
Extracts f0 via pyin per loop, converts to MIDI note numbers, resamples to 32 time
bins. Captures melodic/harmonic structure independent of timbre. Useful for
distinguishing verse vs chorus in pitched stems (vocals, melody, synth).

### Loop Deduplication
Agglomerative clustering (greedy merge) with per-stem constraint (only loops from the
same stem can merge). Distance threshold controls granularity:

**cc-acid-a** (8 stems, 52 loops/stem = 416 raw):

| Threshold | Unique loops | Per-stem avg | PCM size |
|-----------|-------------|-------------|----------|
| 0 (none) | 416 | 52 | 12.7 MB |
| 0.2 | 209 | ~26 | 6.4 MB |
| **0.3** | **159** | **~20** | **4.9 MB** |
| 0.4 | 119 | ~15 | 3.6 MB |
| 0.5 | 75 | ~9 | 2.3 MB |
| 0.7 | 25 | ~3 | 0.8 MB |
| 1.0 | 8 | 1 | 0.25 MB |

**nn-q3** (4 stems, 64 loops/stem = 256 raw):

| Threshold | Unique loops | Per-stem avg | PCM size |
|-----------|-------------|-------------|----------|
| 0 (none) | 256 | 64 | 7.8 MB |
| **0.3** | **120** | **30** | **3.7 MB** |

### Per-stem breakdown (cc-acid-a, t=0.3)

| Stem | Raw | Deduped | Notes |
|------|-----|---------|-------|
| lead vocals | 52 | 20 | Most variation (verse/chorus/bridge) |
| backing vocals | 52 | 8 | Sparse, few distinct sections |
| drums | 52 | 21 | Steady pattern, fills at section boundaries |
| bass | 52 | 24 | Some variation with chord changes |
| guitar | 52 | 24 | Section-dependent riffs |
| percussion | 52 | 6 | Minimal, mostly silent bars deduped |
| synth | 52 | 35 | Most variation — evolving timbres |
| other | 52 | 21 | Background textures |

## PSRAM Budget

| Component | Size | Notes |
|-----------|------|-------|
| Video (charprint BA2S) | 3.4 MB | 6572 frames compressed |
| Audio (t=0.3, 8 stems) | 4.9 MB | 159 loops @ 8kHz 8-bit |
| Audio (t=0.5, 8 stems) | 2.3 MB | 75 loops @ 8kHz 8-bit |
| **Total (t=0.5)** | **5.7 MB** | Fits in 8MB PSRAM |
| **Total (t=0.3)** | **8.3 MB** | Tight — may need t=0.4 |

For 4-stem tracks (nn-q3): 3.4 + 3.7 = 7.1 MB (fits in 8MB).

## Audio Quality

### Gain staging (fixed)
- Global peak normalization across all loops (not per-loop) preserves relative loudness
- DynamicsCompressor as master bus (threshold=-3dB, ratio=12)
- Per-channel volume scaled by `1/num_channels`
- `latencyHint: 'playback'` for larger audio buffer (eliminates realtime crackle)

### WAV render verification
Offline render produces clean output with no clipping artifacts:
- Per-stem WAVs for individual analysis
- Normalized master mix at 95% headroom

## CLI Reference

```bash
# Ableton mode with all features
python gen_music_vdp.py \
  --stems assets/music/cc-acid-a/*.wav \
  --bpm 123 \
  --mode ableton \
  --bars-per-loop 2 \
  --similarity mfcc,rhythm,chroma \
  --skip-intro \
  --loop-threshold 0.3 \
  --html output.html \
  --render-wav output.wav

# No dedup (baseline comparison)
python gen_music_vdp.py \
  --stems assets/music/nn-q3/*.wav \
  --bpm 123 \
  --mode ableton \
  --bars-per-loop 2 \
  --loop-threshold 0 \
  --html output.html

# AY-chip mode (waveform synthesis)
python gen_music_vdp.py \
  --stems drums.wav bass.wav melody.wav \
  --bpm 160 \
  --mode ay \
  --html output.html

# With MIDI fingerprint (slower but captures melody)
python gen_music_vdp.py \
  --stems assets/music/cc-acid-a/*.wav \
  --bpm 123 \
  --mode ableton \
  --similarity mfcc,rhythm,chroma,midi \
  --skip-intro \
  --html output.html
```

### Key arguments

| Arg | Default | Description |
|-----|---------|-------------|
| `--mode` | `ay` | `ay` (waveform) or `ableton` (PCM loops) |
| `--bars-per-loop` | 2 | Bars per loop pattern |
| `--loop-threshold` | 0.3 | Dedup distance; 0=none, 0.3=balanced, 0.5=aggressive |
| `--similarity` | `mfcc,rhythm,chroma` | Fingerprint metrics (mfcc,chroma,rhythm,rms,spectral,midi) |
| `--skip-intro` | off | Auto-detect and skip irregular intro |
| `--sample-rate` | 8000 | PCM sample rate (Agon native) |
| `--render-wav` | — | Offline WAV render (+ per-stem) |

## Generated Outputs

```
badapple/music/
  cc-acid-a_t03.html           # 8ch, t=0.3, 159 loops (interactive)
  cc-acid-a_t03.wav            # Master mix render
  cc-acid-a_t03.*.wav          # Per-stem renders (8 files)
  cc-acid-a_t05.html           # 8ch, t=0.5, 75 loops
  cc-acid-a_nodedup.html       # 8ch, no dedup, 416 loops (baseline)
  cc-acid-a_nodedup.wav        # Baseline master mix
  cc-acid-a_nodedup.*.wav      # Baseline per-stem (8 files)
  nn-q3_t03.html               # 4ch, t=0.3, 120 loops
  nn-q3_t03.wav                # Master mix render
  nn-q3_t03.*.wav              # Per-stem renders (4 files)
  nn-q3_nodedup.html           # 4ch, no dedup, 256 loops (baseline)
  nn-q3_nodedup.wav            # Baseline master mix
  nn-q3_nodedup.*.wav          # Baseline per-stem (4 files)
```

## Distance Distribution (L2-normalized fingerprints)

Measured across all loop pairs within each stem (cc-acid-a, mfcc+rhythm+chroma):

| Stem | Min | Median | P90 | Max |
|------|-----|--------|-----|-----|
| Lead Vocals | 0.066 | 0.596 | 1.042 | 1.364 |
| Drums | 0.125 | 0.621 | 0.937 | 1.406 |
| Bass | 0.109 | 0.699 | 0.969 | 1.443 |
| Synth | 0.106 | 0.732 | 1.063 | 1.517 |

Section boundaries visible as large consecutive-distance jumps (>0.9).

## Known Issues & Next Steps

1. **Clustering is O(n^3)** — slow for >200 loops. Could use scipy's `fcluster` for speed.
2. **Cross-stem dedup disabled** — same audio in different stems won't merge. May want
   cross-stem similarity for stereo-separated content.
3. **Intro detection is simple** — works for typical pop/electronic structure but may
   miss complex intros. Could use structural segmentation (librosa `segment`).
4. **Video+audio merge not yet tested** — VDP buffer encoding works but hasn't been
   verified end-to-end on the emulator.
5. **Sample rate** — 8kHz is lo-fi. Could test 16kHz for tracks that fit in PSRAM.

## Source Stems Available

| Set | Stems | Duration | BPM | Genre |
|-----|-------|----------|-----|-------|
| `cc-acid-a` | 8 (vocals, drums, bass, guitar, perc, synth, other) | 208s | 123 | Acid house |
| `cc-acid-b` | 9 | 208s | 123 | Acid house |
| `nn-q3` | 4 (drums, bass, synth, other) | 253s | 123 | Electronic |
