# Bytebeat Formula Analysis

**Date:** 2026-02-12
**Context:** Audio for Agon Vivid Vibes demo — computed at runtime on eZ80, uploaded as PCM samples to VDP

## Overview

The demo uses two bytebeat songs computed entirely with integer/fixed-point math on the eZ80. Each generates 65,536 samples of 8-bit signed PCM, played at 8,000 Hz (~8.2 seconds per loop, repeating).

## Song 1: JS-256 Engine — Base-36 Melody with Echo

### Original Formula (JavaScript)
```
r=t/1.205
a=r=>(t*2^(parseInt(melody[(r>>13)%32],36)/12)/2.67%128+(r>>6&127)&128)*(1-r%8192/8192)
a(r)+a(r-d)/2+a(r-2d)/4+a(r-3d)/8   where d=12288
```

### Melody String
```
"99C9E9GECCGCJCGC77B7C7EC5595C5CB"
```
32 characters, each a base-36 digit mapping to a chromatic semitone (0-19).

### eZ80 Implementation

**Frequency LUT** — `2^(n/12) / 2.67 * 128` precomputed for semitones 0-19:
```c
static const uint8_t js_freq[20] = {
    48, 51, 54, 57, 60, 64, 68, 72, 76, 81,
    85, 90, 96, 102, 108, 114, 121, 128, 136, 144
};
```

**Time scaling** — `r = t * 213 / 256` (integer approximation of t/1.205)

**Voice function:**
```c
uint8_t saw = (t * js_freq[note]) >> 7;  // sawtooth from freq LUT
uint8_t sub = (r >> 6) & 127;            // sub-oscillator
if (!((saw + sub) & 128)) return 0;      // gate: only output when sum overflows
uint16_t env = 8192 - (r & 8191);        // linear decay (period 8192)
return env >> 6;                          // scale to 0..128
```

**Echo** — 4 voices summed with halving amplitude, delay = 12,288 samples:
```c
val = voice(t, r) + voice(t, r-12288)/2 + voice(t, r-24576)/4 + voice(t, r-36864)/8;
```

### Character

Warm, rich saw-wave melody with reverb-like echo tail. The base-36 melody creates a repeating 32-note pattern that cycles through two variations (notes 5-9 range vs notes 7-C range). The echo at 3 delay taps creates spatial depth.

---

## Song 2: BPM=160 Arpeggiator with XOR Rhythm Gate

### Original Formula
```
Bpm=160, Hz=44100
tf = abs(t/Hz/180*3*32768*Bpm)
pitch = [1,2,3,4,4.5,4.75,6,8][(tf>>14)&7]
pitch2 = [.5,.5625,.59375,.6667][(tf>>18)&3]
(((t*pitch&255)<<8) / ((tf>>1) | (tf>>1>>5)^(tf>>1>>6)) &1 ? 128 : 0)
  + (t*pitch2 & 255) / 2
```

### eZ80 Implementation

**Pitch LUTs** — 8.8 fixed-point:
```c
// Main arpeggio (8 steps): [1, 2, 3, 4, 4.5, 4.75, 6, 8]
static const uint16_t bpm_pitch[8] = {
    256, 512, 768, 1024, 1152, 1216, 1536, 2048
};

// Bass sub-oscillator (4 steps): [0.5, 0.5625, 0.59375, 0.6667]
static const uint8_t bpm_pitch2[4] = { 128, 144, 152, 171 };
```

**Tempo scaling** — `tf = t * 11` (approximation of `t/Hz/180*3*32768*Bpm` at 8kHz)

**Synthesis:**
```c
uint8_t saw = (t * pitch_fp) >> 8;           // main sawtooth
uint16_t a = half_tf & 0xFFFF;               // rhythm component
uint16_t b = ((half_tf >> 5) ^ (half_tf >> 6)) & 0xFFFF;  // XOR gate
uint16_t denom = a | b;                      // combined gate
uint24_t pulse = (saw << 8) / denom;         // gated pulse division
uint8_t main_val = (pulse & 1) ? 128 : 0;   // 1-bit quantize
uint8_t bass = (t * pitch2_fp) >> 8;         // bass sub-oscillator
result = main_val + (bass >> 1);             // combine
```

### Character

Aggressive, driving arpeggiator with an 8-step ascending pitch pattern. The XOR rhythm gate (`(tf>>5)^(tf>>6)`) creates complex rhythmic subdivision that varies with time. The bass sub-oscillator provides low-frequency foundation. More energetic and percussive than Song 1.

---

## VDP Audio API

```c
// Upload 8-bit signed PCM sample (slot uses negative IDs)
vdp_audio_load_sample(-1, 65536, buffer);

// Configure looping
vdp_audio_set_sample_repeat_start(-1, 0);
vdp_audio_set_sample_repeat_length(-1, 65536);

// Set playback rate
vdp_audio_sample_rate(0, 8000);  // channel 0, 8kHz

// Start playback
vdp_audio_set_waveform(0, -1);       // assign sample to channel
vdp_audio_play_note(0, 127, 0, -1);  // vol=127, freq=0 (native rate), duration=forever
```

## Computation Cost

Each song computes 65,536 samples. Song 1 is heavier (4 echo taps × lookup + multiply per sample). On the eZ80 at 18.432 MHz, both songs together take a few seconds — visible as a startup delay. Currently disabled for debugging.

## Parameters

| | Song 1 (JS-256) | Song 2 (BPM Arp) |
|---|---|---|
| Sample count | 65,536 | 65,536 |
| Playback rate | 8,000 Hz | 8,000 Hz |
| Loop duration | ~8.2 seconds | ~8.2 seconds |
| Waveform | Sawtooth + gate | Pulse + sawtooth bass |
| Melody | 32-note base-36 pattern | 8-step ascending arpeggio |
| Effects | 4-tap echo (d=12288) | XOR rhythm gate |
| VDP slot | -1 | -2 |
