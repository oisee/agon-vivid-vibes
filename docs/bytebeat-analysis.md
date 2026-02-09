# Bytebeat Formula Analysis Report

Analysis of complex bytebeat/synthesizer formulas, decomposed into layers.

---

## Formula 1: Stereo Synth at 57,000 Hz

```js
x=j=>(b=a=>t*2**('6666666622224449'[t>>14&15]/12-2)*a%256/2.1,
c=a=>t*2**('111111115555AAAE'[t>>14&15]/12-2)*a%256/3,
d=a=>t*2**('111111119999DDDE'[t>>14&15]/12-2)*a%256/4,
e=b(1)+b(2)+b(4)+c(1)+c(2)+c(4)+d(1)+d(2)+d(4),
f=j?t*2**(9/12)/3%64:t*2**(9/12)/5%64,
e/3+f)
x(0)+x(1)*256
```

**Sample rate:** 57,000 Hz stereo (left = x(0), right = x(1))

### Layer Decomposition

#### Layer 1: Melody sequencer (shared across all oscillators)

The 16-character strings are note sequences indexed by `t>>14&15`, giving 16 steps. At 57kHz, each step lasts `2^14 / 57000 = 0.288s`, so the full pattern is ~4.6s.

| String | Notes (base-36 digits) | Musical interpretation |
|--------|----------------------|----------------------|
| `6666666622224449` | 6,6,6,6,6,6,6,6,2,2,2,2,4,4,4,9 | Sustained note, drop, rise, high |
| `111111115555AAAE` | 1,1,1,1,1,1,1,1,5,5,5,5,A,A,A,E | Low drone, mid, high, highest |
| `111111119999DDDE` | 1,1,1,1,1,1,1,1,9,9,9,9,D,D,D,E | Low drone, upper mid, very high |

#### Layer 2: Oscillator `b` — primary voice

```js
b = a => t * 2**('6666...'[t>>14&15]/12 - 2) * a % 256 / 2.1
```

- Frequency: `t * 2^(note/12 - 2)` — chromatic pitch from sequence
- `* a` — harmonic multiplier (1x, 2x, 4x = fundamental + octave + 2 octaves)
- `% 256` — sawtooth wave (wraps 0-255)
- `/ 2.1` — amplitude scaling (~121 peak)

#### Layer 3: Oscillator `c` — second voice

```js
c = a => t * 2**('1111...'[t>>14&15]/12 - 2) * a % 256 / 3
```

- Same structure as `b` but with different note sequence and `/3` scaling (~85 peak)
- Acts as a bass/pad layer

#### Layer 4: Oscillator `d` — third voice

```js
d = a => t * 2**('1111...'[t>>14&15]/12 - 2) * a % 256 / 4
```

- Third note sequence, `/4` scaling (~64 peak)
- Provides harmonic depth

#### Layer 5: Additive mix `e`

```js
e = b(1) + b(2) + b(4) + c(1) + c(2) + c(4) + d(1) + d(2) + d(4)
```

- 9 oscillators total: 3 voices x 3 harmonics (fundamental, octave, double octave)
- Rich, organ-like timbre from additive synthesis

#### Layer 6: Stereo hi-hat / texture `f`

```js
f = j ? t*2**(9/12)/3%64 : t*2**(9/12)/5%64
```

- Fixed pitch: `2^(9/12)` = A above the base octave
- Left channel: `/5 % 64` — slower, quieter
- Right channel: `/3 % 64` — faster, brighter
- Creates stereo width through different modulo rates

#### Output

```js
e/3 + f  // per channel, then packed: x(0) + x(1)*256
```

16-bit stereo: left in low byte, right in high byte.

---

## Formula 2: Stereo Synth at 48,000 Hz

```js
x=t=>(1.05*t/(4-2*(t>>18&1))*2**('6645'[t>>16&3]/12)%128
+1.05*t/(4-2*(t>>18&1))*2**('1109'[t>>16&3]/12)/2%128
+t*2**(9/12)/5%64)*(t>>19&1)
x()+x()*256
```

**Sample rate:** 48,000 Hz stereo (mono signal duplicated to both channels)

### Layer Decomposition

#### Layer 1: Timing gate (intro/silence)

```js
*(t>>19&1)
```

- Alternates between silence and sound every `2^19 / 48000 = 10.9s`
- Creates a dramatic on/off structure

#### Layer 2: Pitch bend / vibrato

```js
1.05 * t / (4 - 2*(t>>18&1))
```

- Base frequency multiplied by 1.05 (slight sharp)
- Denominator alternates between 4 and 2 every `2^18 / 48000 = 5.5s`
- When denom=4: lower pitch; when denom=2: octave up
- Creates alternating octave pattern

#### Layer 3: Primary melody oscillator

```js
... * 2**('6645'[t>>16&3]/12) % 128
```

- 4-note sequence: 6, 6, 4, 5 (chromatic scale degrees)
- Each step: `2^16 / 48000 = 1.37s`
- Full pattern: ~5.5s (aligns with octave switch)
- `% 128` — sawtooth wave

#### Layer 4: Bass/harmony oscillator

```js
... * 2**('1109'[t>>16&3]/12) / 2 % 128
```

- 4-note sequence: 1, 1, 0, 9
- `/2` before modulo — half amplitude, different harmonic content
- Provides bass foundation

#### Layer 5: Fixed hi-hat texture

```js
t * 2**(9/12) / 5 % 64
```

- Constant pitch, no sequencing
- `/ 5 % 64` — quiet, fast sawtooth adding shimmer
- Same as Formula 1's left-channel texture

#### Output structure

All layers summed, gated by the `t>>19&1` on/off switch. Packed as 16-bit stereo (identical L/R).

---

## Formula 3: BPM-Synced Arpeggiator at 44,100 Hz

```js
Bpm=160, Hz=44100,
tf = abs(t/Hz/180*3*32768*Bpm),
pitch  = [1,2,3,4,4.5,4.75,6,8][(abs(tf)>>14)%8],
pitch2 = [4,4.5,4.75,5+1/3][(abs(tf)>>18)%4] / 8,
speed  = 2,
x = abs(255 * floor(
      t*pitch % 256 /
      (speed/4*tf%65536 | (speed/4*tf/32 ^ speed/4*tf/64) % 65536)
      * 65536/256 % 2
    )),
abs(128 - x%256) + abs(t*pitch2 % 256) / 2
```

**Sample rate:** 44,100 Hz mono

### Layer Decomposition

#### Layer 1: BPM timebase `tf`

```js
tf = abs(t/Hz/180*3*32768*Bpm)
```

Simplifying: `tf = t * (3 * 32768 * 160) / (44100 * 180) = t * 15728640 / 7938000 ≈ t * 1.9816`

So `tf ≈ 2*t` — a tempo-scaled sample counter. This drives all rhythm.

**Timing at 44,100 Hz:**
- `tf>>14` changes every `2^14 / 1.98 / 44100 ≈ 0.187s` — this is one **half-beat** at 160 BPM (0.375s per beat / 2)
- `tf>>18` changes every `2^18 / 1.98 / 44100 ≈ 2.99s` — approximately **8 beats** (one bar of 4/4 × 2)

#### Layer 2: Melodic arpeggio `pitch`

```js
pitch = [1, 2, 3, 4, 4.5, 4.75, 6, 8][(abs(tf)>>14) % 8]
```

- 8-step sequence cycling every half-beat
- Full pattern = 4 beats (one bar)
- Ascending pattern: unison → octave → octave+fifth → 2 octaves → ...
- Creates a rapid upward arpeggio with increasingly tight intervals at the top (4 → 4.5 → 4.75)
- Reaches 3 octaves (8x) at the peak before resetting

#### Layer 3: Bass/pad `pitch2`

```js
pitch2 = [4, 4.5, 4.75, 5+1/3][(abs(tf)>>18) % 4] / 8
```

- 4-step sequence, each step = ~3s (8 beats)
- Full cycle = ~12s (32 beats)
- Values after `/8`: 0.5, 0.5625, 0.59375, 0.6667
- These are sub-octave pitches — a slow-moving bass line
- Intervals: roughly P4 → tritone area → P5 → minor 7th (relative)

#### Layer 4: Rhythm denominator (the "drum machine")

```js
speed/4 * tf = 0.5 * tf ≈ t
```

The denominator expression:
```js
(speed/4*tf % 65536) | (speed/4*tf/32 ^ speed/4*tf/64) % 65536
```

Breaking this down:
- `A = 0.5*tf % 65536` — 16-bit sawtooth ramp at tempo
- `B = (0.5*tf/32 ^ 0.5*tf/64) % 65536` — XOR of two slower ramps creates pseudo-random rhythmic pattern
- `A | B` — OR combination ensures non-zero denominator while creating complex rhythm

The XOR of `/32` and `/64` (differing by factor 2) creates a pattern that repeats every `2^16 * 64 / tf_rate` samples — essentially a rhythmic gate with complexity from bit-level interference.

#### Layer 5: Waveform shaping

```js
x = abs(255 * floor(t*pitch%256 / denominator * 65536/256 % 2))
```

Step by step:
1. `t * pitch % 256` — sawtooth at the arpeggio pitch
2. `/ denominator` — division by rhythmic pattern creates amplitude modulation + distortion
3. `* 65536/256 = * 256` — scale up
4. `% 2` — fold into 0-2 range (creates square-ish wave)
5. `floor(...)` — quantize to 0 or 1
6. `* 255` — full amplitude square wave
7. `abs(...)` — rectify (already positive, but ensures)

This creates a **pulse wave** whose duty cycle and timing are modulated by the rhythm denominator — producing rhythmic gating effects, stutters, and textural changes.

#### Layer 6: Final mix with triangle fold

```js
abs(128 - x%256) + abs(t*pitch2%256) / 2
```

- `abs(128 - x%256)` — triangle-wave fold of the pulse signal (0→128→0 fold)
- `abs(t*pitch2%256) / 2` — sawtooth bass folded to triangle, half amplitude
- Sum creates a layered sound: rhythmic arpeggio + sustained bass drone

### Overall Character

This formula creates a **rhythmic arpeggiator** locked to 160 BPM:
- Fast 8-note ascending arpeggio (2 bars per cycle)
- Slow bass movement (32 beats per cycle)
- Complex rhythmic gating from XOR bit patterns
- Bright, aggressive, "chiptune acid" character
- The division-based rhythm creates glitchy, broken-beat textures reminiscent of circuit-bent electronics

---

## Implementation Notes (Agon eZ80)

The formulas used in the Vivid Vibes demo (Songs 1-3 in `main.c`) are all computed on the eZ80 at runtime using integer math:

| Song | Formula | Approach |
|------|---------|----------|
| 1 | JS-256 base-36 melody + echo | LUT for `2^(n/12)/2.67*128`, 4-tap echo delay |
| 2 | `t*((t&4096?6:16)+(1&t>>14))>>(3&t>>8)\|t>>(t&4096?3:4)` | Direct integer math |
| 3 | `t*(t>>(t&4096?t*t>>12:t>>12))\|t<<(t>>8)\|t>>4` | Direct integer math |

All computed into 64KB buffers at 8kHz, uploaded to VDP sample slots via `vdp_audio_load_sample()`.

The stereo formulas (57kHz, 48kHz) and the BPM-synced formula (44100Hz) would require pre-rendering in Python due to their use of floating-point math, large lookup tables, and high sample rates.
