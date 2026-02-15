# 006: VDP Coordinate System Investigation

## Problem

Bad Apple replay renders at 1/4 screen (top-left corner). Pixel coordinate
mode (`VDU 23, 0, 0xC0, 0`) doesn't seem to take effect.

## Source Code Findings

All paths below relative to `/Users/alice/dev/fab-agon-emulator/src/vdp/vdp-console8/video/`

### Bitmap plot coordinate flow

1. `VDU 25, 0xE8` (PLOT bitmap) → `plot(x, y, 0xE8)` in `context/graphics.h:624`
2. `pushPoint(x, y)` → `toScreenCoordinates(x, y)` in `context/viewport.h:198`
3. `toScreenCoordinates()` calls `scale(X, Y)`:

```cpp
// viewport.h:178
Point Context::scale(int16_t X, int16_t Y) {
    if (logicalCoords) {
        return Point((double)X / logicalScaleX, -(double)Y / logicalScaleY);
    }
    return Point(X, Y);  // pixel mode: no transformation
}
```

4. Then `plotBitmap()` → `drawBitmap(p1.X, p1.Y, ...)` in `context/graphics.h:330`
5. Height compensation in drawBitmap (`graphics.h:824`):
```cpp
auto yPos = (compensateHeight && logicalCoords) ? (y + 1 - bitmap->height) : y;
```

**Conclusion: bitmap plot DOES respect logicalCoords flag.** When pixel mode
is active, coordinates pass through unchanged. When logical mode is active,
they get scaled by logicalScaleX/Y with Y-flip.

### VDU 23, 0, 0xC0 handler

```cpp
// vdu_sys.h:318
case VDP_LOGICALCOORDS: {       // VDU 23, 0, &C0, n
    auto b = readByte_t();      // 0 = pixel, 1 = logical
    if (b >= 0) {
        context->setLogicalCoords((bool) b);
    }
}   break;
```

### setLogicalCoords implementation

```cpp
// viewport.h:158
void Context::setLogicalCoords(bool b) {
    if (b != logicalCoords) {
        origin.Y = canvasH - origin.Y - 1;  // flip origin
        logicalCoords = b;
        if (b) {
            up1 = Point(up1.X * logicalScaleX, LOGICAL_SCRH - (up1.Y * logicalScaleY));
            uOrigin = Point(origin.X * logicalScaleX, LOGICAL_SCRH - (origin.Y * logicalScaleY));
        } else {
            up1 = Point(up1.X / logicalScaleX, canvasH - (up1.Y / logicalScaleY));
            uOrigin = origin;
        }
    }
}
```

Default: `logicalCoords = true` (declared in `context.h:108`)

### CRITICAL: Mode switch does NOT reset logicalCoords

When `VDU 22, N` runs:
- `resetAllContexts()` → `resetContext(0)` → `context->reset()`
- `reset()` calls `resetGraphicsPainting()`, `resetGraphicsPositioning()`, etc.
- **But `reset()` does NOT call `setLogicalCoords(true)`**
- The flag retains its previous value across mode switches!

There IS one path that resets it — selective context reset with `CONTEXT_RESET_GPOS`:
```cpp
// vdu_context.h:109
if (flags & CONTEXT_RESET_GPOS) {
    context->setLogicalCoords(true);   // explicit reset
    context->resetGraphicsPositioning();
}
```

But this path is NOT taken during normal `VDU 22` mode switch.

## Implications

1. **The 1/4 screen issue is NOT caused by mode switch resetting logicalCoords.**
   Since mode switch preserves the flag, sending pixel coords before or after
   mode switch should both work — the flag survives.

2. **Order shouldn't matter:** `VDU 23, 0, 0xC0, 0` then `VDU 22, 136` should
   keep pixel mode. And `VDU 22, 136` then `VDU 23, 0, 0xC0, 0` should also work.

3. **So why 1/4 screen?** Possible causes:
   - The `VDU 23, 0, 0xC0, 0` command is being parsed incorrectly (byte timing?)
   - The replay file has a framing issue that corrupts the command
   - The `resetAllContexts()` path DOES reset via some other mechanism
   - The VDP version used differs from this source

## Next Steps

1. **Test with minimal replay file** (`/tmp/test_bitmap.vdu`) to confirm
   bitmap rendering works at all
2. **Try sending pixel coords BEFORE mode switch** — since mode switch
   doesn't reset the flag, this should work
3. **Add VDU 23, 0, 0xC0, 0 to EVERY frame chunk** as a belt-and-suspenders
   fix (only 4 extra bytes per frame)
4. **Use --replay-log** to check for VDU parse warnings that might indicate
   the 0xC0 command is being swallowed or misinterpreted

## Coordinate Math (if we must use logical coords as fallback)

Mode 8: 320x240 pixels, logical space 1280x1024, origin bottom-left, Y up.

```
logicalScaleX = 1280 / 320 = 4.0
logicalScaleY = 1024 / 240 = 4.2667
```

Pixel (px, py) → Logical:
```
lx = px * 4
ly = (239 - py) * 4.2667  ≈ 1024 - py * 4.2667
```

Problem: Y scale is not integer → 8px tile rows don't align cleanly.
8 pixels × 4.2667 = 34.13 logical units (not integer).

This makes logical-coord fallback impractical for tile grids.
**Must get pixel coords working.**
