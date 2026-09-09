"""Destructible terrain, stored as one ground height per screen column.

Scorched Earth's terrain is column-based: dirt hangs from a surface height down
to the bottom of the world, and when you blow a hole in it whatever was above
the hole falls straight down.  That model is cheap enough to update thousands of
times a second on a Pi 400, and it makes overhang-free collision a single array
lookup.

Everything here is *integer* arithmetic on purpose.  The server and every client
replay the same list of crater/dirt operations, so identical results across
x86-64 Windows and 32-bit ARM Linux matter -- and integers are the only way to
promise that without auditing every libm on every platform.
"""

from __future__ import annotations

import math
import random

# World geometry.  640x400 is deliberately small: it is the classic VGA-ish
# canvas the original game used, it keeps the Pi's fill rate happy, and the
# client scales it up to whatever the monitor actually is.
WORLD_W = 640
WORLD_H = 400
PLAY_H = 356          # everything below this is the HUD strip
HUD_H = WORLD_H - PLAY_H

SKY_MARGIN = 40       # peaks may reach this high, never higher
#: Valleys never drop below this, so every map keeps a solid mass of dirt to
#: dig through.  Without it a low-lying map is a thin crust over bedrock and
#: the first Nuke exposes the floor of the world.
FLOOR_MARGIN = 112

STYLES = ("hills", "mountains", "valley", "plateau", "canyon", "flat", "random")


class Terrain:
    """A column heightmap. ``height[x]`` is the y of the topmost dirt pixel."""

    __slots__ = ("width", "height_limit", "height", "dirty_lo", "dirty_hi")

    def __init__(self, heights: list[int], height_limit: int = PLAY_H) -> None:
        self.width = len(heights)
        self.height_limit = height_limit
        self.height = list(heights)
        # Columns changed since the renderer last looked, so the client can
        # repaint a 30-pixel-wide crater instead of the whole screen.
        self.dirty_lo = 0
        self.dirty_hi = self.width

    # -- construction ----------------------------------------------------
    @classmethod
    def generate(cls, seed: int, style: str = "random", width: int = WORLD_W,
                 limit: int = PLAY_H) -> "Terrain":
        rng = random.Random(seed)
        if style == "random" or style not in STYLES:
            style = rng.choice([s for s in STYLES if s != "random"])

        lo = SKY_MARGIN
        hi = limit - FLOOR_MARGIN

        if style == "flat":
            # "Flat" means an easy, readable landscape -- not a ruled line.
            base = rng.randint(lo + (hi - lo) * 2 // 3, hi)
            heights = _rolling(rng, width, base, amplitude=(hi - lo) * 0.10)
        elif style == "plateau":
            heights = _plateau(rng, width, lo, hi)
        elif style == "canyon":
            heights = _canyon(rng, width, lo, hi)
        elif style == "valley":
            heights = _fractal(rng, width, lo, hi, roughness=0.52)
            _bias(heights, width, lo, hi, shape="valley")
        elif style == "mountains":
            heights = _fractal(rng, width, lo, hi, roughness=0.60)
        else:  # hills
            heights = _fractal(rng, width, lo, hi, roughness=0.42)

        _smooth(heights, 2)
        clamped = [max(lo, min(hi, int(h))) for h in heights]
        return cls(clamped, limit)

    @classmethod
    def from_wire(cls, data: dict) -> "Terrain":
        return cls(list(data["h"]), int(data.get("limit", PLAY_H)))

    def to_wire(self) -> dict:
        return {"h": self.height, "limit": self.height_limit}

    # -- queries ---------------------------------------------------------
    def surface(self, x: int) -> int:
        """Ground height at column ``x``, clamped to the world edges."""
        if x < 0:
            x = 0
        elif x >= self.width:
            x = self.width - 1
        return self.height[x]

    def is_solid(self, x: int, y: int) -> bool:
        if x < 0 or x >= self.width:
            return False
        return y >= self.height[x]

    def slope(self, x: int, span: int = 3) -> int:
        """Signed height difference across ``x``; positive means downhill right."""
        return self.surface(x - span) - self.surface(x + span)

    # -- destruction -----------------------------------------------------
    def _touch(self, lo: int, hi: int) -> None:
        self.dirty_lo = min(self.dirty_lo, max(0, lo))
        self.dirty_hi = max(self.dirty_hi, min(self.width, hi))

    def take_dirty(self) -> tuple[int, int]:
        lo, hi = self.dirty_lo, self.dirty_hi
        self.dirty_lo, self.dirty_hi = self.width, 0
        return (lo, hi) if lo < hi else (0, 0)

    def crater(self, cx: int, cy: int, radius: int) -> None:
        """Punch a circular hole; dirt above the hole collapses into it.

        For each column the circle covers the vertical span ``[top, bot]``.  The
        dirt that survives above the hole is ``[surface, top)``, and it slides
        down to rest on ``bot`` -- so the new surface is ``bot - (top-surface)``.
        When the circle's top is above the surface that term is zero and the
        column is simply carved down to ``bot``.
        """
        cx, cy, radius = int(cx), int(cy), int(radius)
        if radius <= 0:
            return
        x0 = max(0, cx - radius)
        x1 = min(self.width, cx + radius + 1)
        rr = radius * radius
        limit = self.height_limit
        h = self.height
        for x in range(x0, x1):
            dx = x - cx
            dy = math.isqrt(rr - dx * dx)
            bot = cy + dy
            ground = h[x]
            if bot <= ground:
                continue                      # hole floats entirely in the air
            overhead = max(cy - dy, ground) - ground
            h[x] = max(0, min(limit, bot - overhead))
        self._touch(x0, x1)

    def add_dirt(self, cx: int, cy: int, radius: int) -> None:
        """Drop a ball of dirt; it piles onto whatever surface is below it."""
        cx, cy, radius = int(cx), int(cy), int(radius)
        if radius <= 0:
            return
        x0 = max(0, cx - radius)
        x1 = min(self.width, cx + radius + 1)
        rr = radius * radius
        h = self.height
        for x in range(x0, x1):
            dx = x - cx
            dy = math.isqrt(rr - dx * dx)
            ground = h[x]
            added = min(cy + dy, ground) - (cy - dy)
            if added > 0:
                h[x] = max(0, ground - added)
        self._touch(x0, x1)

    def apply(self, op: str, x: int, y: int, r: int) -> None:
        """Replay a single wire-encoded terrain operation."""
        if op == "crater":
            self.crater(x, y, r)
        elif op == "dirt":
            self.add_dirt(x, y, r)


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------

def _fractal(rng: random.Random, width: int, lo: int, hi: int,
             roughness: float) -> list[float]:
    """Midpoint displacement -- the classic artillery-game landscape."""
    size = 1
    while size < width:
        size *= 2
    pts = [0.0] * (size + 1)
    pts[0] = rng.uniform(0.25, 0.75)
    pts[size] = rng.uniform(0.25, 0.75)
    step = size
    scale = 0.5
    while step > 1:
        half = step // 2
        for i in range(half, size, step):
            avg = (pts[i - half] + pts[i + half]) * 0.5
            pts[i] = avg + rng.uniform(-scale, scale)
        step = half
        scale *= roughness
    span = hi - lo
    return [lo + max(0.0, min(1.0, pts[i])) * span for i in range(width)]


def _bias(heights: list[float], width: int, lo: int, hi: int, shape: str) -> None:
    """Blend the landscape toward a large-scale shape (a valley, say)."""
    span = hi - lo
    for x in range(width):
        t = x / max(1, width - 1)
        if shape == "valley":
            target = lo + span * (0.30 + 0.6 * (1.0 - abs(t - 0.5) * 2))
        else:
            target = lo + span * 0.5
        heights[x] = heights[x] * 0.45 + target * 0.55


def _plateau(rng: random.Random, width: int, lo: int, hi: int) -> list[float]:
    heights = _fractal(rng, width, lo, hi, roughness=0.45)
    steps = rng.randint(2, 4)
    edges = sorted(rng.randint(width // 8, width - width // 8) for _ in range(steps))
    level = rng.uniform(lo + (hi - lo) * 0.3, hi)
    idx = 0
    out = []
    for x in range(width):
        while idx < len(edges) and x > edges[idx]:
            idx += 1
            level = rng.uniform(lo + (hi - lo) * 0.2, hi)
        out.append(heights[x] * 0.45 + level * 0.55)
    return out


def _canyon(rng: random.Random, width: int, lo: int, hi: int) -> list[float]:
    heights = _fractal(rng, width, lo, hi, roughness=0.5)
    centre = width // 2 + rng.randint(-width // 6, width // 6)
    half = rng.randint(width // 14, width // 8)
    for x in range(width):
        d = abs(x - centre)
        if d < half:
            # Smooth walls rather than a rectangular slot.
            t = 1.0 - (d / half) ** 2
            heights[x] = heights[x] * (1 - t) + hi * t
    return heights


def _rolling(rng: random.Random, width: int, base: float,
             amplitude: float) -> list[float]:
    """A few summed sine waves: gentle, obviously artificial, easy to read."""
    waves = [(rng.uniform(0.004, 0.012), rng.uniform(0, 6.283),
              rng.uniform(0.35, 1.0)) for _ in range(3)]
    out = []
    for x in range(width):
        offset = sum(math.sin(x * freq + phase) * weight for freq, phase, weight in waves)
        out.append(base + offset * amplitude)
    return out


def _smooth(heights: list[float], passes: int) -> None:
    n = len(heights)
    for _ in range(passes):
        prev = heights[0]
        for x in range(1, n - 1):
            cur = heights[x]
            heights[x] = (prev + cur * 2.0 + heights[x + 1]) * 0.25
            prev = cur
