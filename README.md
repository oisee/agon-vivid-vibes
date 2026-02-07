# Agon Vivid Vibes

A demoscene demo for [Agon Light 2](https://www.thebyteattic.com/p/agon.html), inspired by [vivid-vibes](https://github.com/oisee/vivid-vibes) - the first ever demo for SAP ABAP.

## Effects

| Effect | Description | Status |
|--------|-------------|--------|
| **Plasma** | Classic sine-wave color plasma | ✅ BASIC |
| **Copper Bars** | Amiga-style oscillating color bars | ✅ BASIC |
| **Starfield** | Hyperspace warp effect | ✅ BASIC |
| **Combined Demo** | All effects with transitions | ✅ BASIC |

## Quick Start (BBC BASIC)

Copy `basic/*.bas` to your Agon SD card, then:

```
LOAD "vibes.bas"
RUN
```

Or test in the emulator:

```bash
fab-agon-emulator --sdcard /path/to/agon-vivid-vibes/basic
```

## Building (C Version)

Requires [AgDev](https://github.com/pcawte/AgDev) toolchain.

```bash
make
# Produces bin/vibes.bin
```

## Architecture

```
Original vivid-vibes:          Agon port:
┌─────────────────┐            ┌─────────────────┐
│  ABAP (eZ80)    │            │  eZ80 (C/BASIC) │
│  Scene Engine   │            │  Scene Engine   │
├─────────────────┤            ├─────────────────┤
│  JSON stream    │            │  VDU stream     │
│  via WebSocket  │            │  via UART       │
├─────────────────┤            ├─────────────────┤
│  JS/Canvas      │            │  ESP32 VDP      │
│  Renderer       │            │  Renderer       │
└─────────────────┘            └─────────────────┘
```

The architecture maps naturally:
- ABAP → eZ80 (both calculate effects)
- WebSocket → UART (both stream commands)
- Browser/Canvas → VDP (both render graphics)

## Files

```
agon-vivid-vibes/
├── basic/              # BBC BASIC versions (run directly)
│   ├── plasma.bas
│   ├── plasma2.bas     # Optimized plasma
│   ├── copper.bas
│   ├── stars.bas
│   └── vibes.bas       # Combined demo
├── src/                # C source (requires AgDev)
│   └── main.c
└── bin/                # Compiled binaries
    └── vibes.bin
```

## Credits

- **Original ABAP demo**: [OISEE](https://github.com/oisee) + Claude
- **Agon port**: Claude Code
- **Music** (original): Oisee - "EA Rulez!" / "Ole Lukøjle" (AY-8910)

## License

MIT License - see [LICENSE](LICENSE)

## Links

- [Original vivid-vibes](https://github.com/oisee/vivid-vibes)
- [Agon Light Documentation](https://agonplatform.github.io/agon-docs/)
- [fab-agon-emulator](https://github.com/tomm/fab-agon-emulator)
