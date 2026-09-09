"""Terrain tests.

The determinism tests here are the ones that matter most: the whole
cross-platform design rests on a client and a server reaching byte-identical
terrain from the same list of operations.
"""

import random

from scorched.terrain import FLOOR_MARGIN, PLAY_H, SKY_MARGIN, STYLES, Terrain


def test_generation_stays_in_bounds():
    for style in STYLES:
        for seed in range(6):
            terrain = Terrain.generate(seed, style)
            assert len(terrain.height) == terrain.width
            assert min(terrain.height) >= SKY_MARGIN
            assert max(terrain.height) <= PLAY_H - FLOOR_MARGIN


def test_generation_is_seeded():
    assert Terrain.generate(7, "hills").height == Terrain.generate(7, "hills").height
    assert Terrain.generate(7, "hills").height != Terrain.generate(8, "hills").height


def test_wire_roundtrip():
    original = Terrain.generate(3, "mountains")
    copy = Terrain.from_wire(original.to_wire())
    assert copy.height == original.height
    assert copy.height_limit == original.height_limit


def test_replaying_ops_gives_identical_terrain():
    """A client replaying the server's ops must land on the same heightmap."""
    rng = random.Random(99)
    ops = [("crater" if rng.random() < 0.75 else "dirt",
            rng.randrange(640), rng.randrange(100, 340), rng.randrange(8, 60))
           for _ in range(120)]

    server = Terrain.generate(11, "hills")
    client = Terrain.from_wire(Terrain.generate(11, "hills").to_wire())
    for op, x, y, r in ops:
        server.apply(op, x, y, r)
        client.apply(op, x, y, r)
    assert server.height == client.height


def test_heights_stay_integral():
    """Floats would let two platforms' libm drift apart; integers cannot."""
    terrain = Terrain.generate(5, "valley")
    terrain.crater(300, 250, 40)
    terrain.add_dirt(120, 200, 30)
    assert all(isinstance(h, int) for h in terrain.height)


def test_surface_crater_digs_down_to_the_blast_floor():
    terrain = Terrain([200] * 64, PLAY_H)
    terrain.crater(32, 200, 20)          # centred on the surface
    # Nothing was hanging above the blast, so the hole's floor is the new
    # surface: 200 + 20.
    assert terrain.height[32] == 220
    # Columns outside the radius are untouched.
    assert terrain.height[0] == 200
    assert terrain.height[32 + 21] == 200
    # The rim is shallower than the middle -- it is a circle, not a trench.
    assert terrain.height[32] > terrain.height[32 + 16] > 200


def test_crater_entirely_in_the_air_changes_nothing():
    terrain = Terrain([300] * 32, PLAY_H)
    before = list(terrain.height)
    terrain.crater(16, 100, 20)
    assert terrain.height == before


def test_buried_crater_drops_the_ceiling():
    terrain = Terrain([100] * 32, PLAY_H)
    terrain.crater(16, 200, 10)      # a cavity 100 px below the surface
    # Ceiling falls in: surface drops by the cavity's height (2r).
    assert terrain.height[16] == 120


def test_dirt_ball_raises_the_ground():
    terrain = Terrain([300] * 32, PLAY_H)
    terrain.add_dirt(16, 300, 12)
    assert terrain.height[16] < 300


def test_crater_never_escapes_the_world():
    terrain = Terrain([340] * 32, PLAY_H)
    terrain.crater(16, 350, 90)
    assert 0 <= terrain.height[16] <= PLAY_H


def test_dirty_range_tracks_edits():
    terrain = Terrain.generate(1, "flat")
    terrain.take_dirty()
    terrain.crater(300, 300, 20)
    lo, hi = terrain.take_dirty()
    assert lo <= 280 and hi >= 320
    assert terrain.take_dirty() == (0, 0)      # cleared after reading
