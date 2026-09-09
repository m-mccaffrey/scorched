import pytest

from scorched.physics import (Simulation, explosion_frames, muzzle_velocity,
                              trace)
from scorched.terrain import PLAY_H, Terrain
from scorched.weapons import WEAPONS


class FakeTank:
    def __init__(self, pid, x, y, hp=100, shield=0):
        self.pid, self.x, self.y = pid, x, y
        self.hp, self.shield_hp = hp, shield
        self.alive = True
        self.parachutes = 0


def flat_world(level=250):
    return Terrain([level] * 640, PLAY_H)


def test_muzzle_velocity_directions():
    vx, vy = muzzle_velocity(90, 500)
    assert vy < 0 and abs(vx) < 1e-9          # straight up
    vx, vy = muzzle_velocity(0, 500)
    assert vx > 0 and abs(vy) < 1e-9          # due east
    vx, _ = muzzle_velocity(180, 500)
    assert vx < 0                             # due west


@pytest.mark.parametrize("weapon", [w.code for w in WEAPONS])
def test_every_weapon_resolves(weapon):
    """No weapon may hang, crash, or produce an empty timeline."""
    terrain = flat_world()
    shooter = FakeTank(0, 100, 250)
    target = FakeTank(1, 420, 250)
    result = Simulation(terrain, [shooter, target], wind=15, seed=1).fire(
        shooter, 45, 520, weapon)
    assert result.events, f"{weapon} produced no events"
    assert 0 < result.frames < 2000
    assert any(e["e"] == "traj" for e in result.events)


def test_shot_damages_a_nearby_tank():
    terrain = flat_world()
    shooter = FakeTank(0, 100, 250)
    target = FakeTank(1, 300, 250)
    sim = Simulation(terrain, [shooter, target], wind=0, seed=2)
    # Drop a Nuke straight onto the target by firing vertically from its tile.
    shooter.x = 300
    result = sim.fire(shooter, 90, 300, "nuke")
    assert target.hp < 100
    assert result.damage_dealt.get(1, 0) > 0


def test_damage_falls_off_with_distance():
    def damage_at(offset):
        terrain = flat_world()
        shooter = FakeTank(0, 300, 250)
        # A very tough target, so the readings are not all clipped at zero hp.
        target = FakeTank(1, 300 + offset, 250, hp=1000)
        Simulation(terrain, [shooter, target], wind=0, seed=3).fire(
            shooter, 90, 300, "nuke")
        return 1000 - target.hp

    assert damage_at(0) > damage_at(30) > damage_at(55) >= 0


def test_shield_absorbs_before_hull():
    terrain = flat_world()
    shooter = FakeTank(0, 300, 250)
    target = FakeTank(1, 300, 250, shield=500)
    Simulation(terrain, [shooter, target], wind=0, seed=4).fire(
        shooter, 90, 300, "nuke")
    assert target.hp == 100                    # hull untouched
    assert target.shield_hp < 500              # shield took it


def test_terrain_ops_are_recorded_for_replay():
    terrain = flat_world()
    shooter = FakeTank(0, 100, 250)
    result = Simulation(terrain, [shooter], wind=0, seed=5).fire(
        shooter, 60, 400, "mis")
    assert result.terrain_ops
    # Every op must also appear as a timeline event, or clients would never
    # learn about it.
    events = [(e["op"], e["x"], e["y"], e["r"])
              for e in result.events if e["e"] == "terrain"]
    assert events == result.terrain_ops


def test_timeline_events_are_frame_stamped_and_ordered():
    terrain = flat_world()
    shooter = FakeTank(0, 100, 250)
    target = FakeTank(1, 400, 250)
    result = Simulation(terrain, [shooter, target], wind=20, seed=6).fire(
        shooter, 50, 560, "mirv")
    assert all("f" in e and e["f"] >= 0 for e in result.events)
    last_traj = max(e["f"] + len(e["pts"])
                    for e in result.events if e["e"] == "traj")
    assert result.frames >= last_traj


def test_mirv_splits_into_several_warheads():
    terrain = flat_world()
    shooter = FakeTank(0, 100, 250)
    result = Simulation(terrain, [shooter], wind=0, seed=7).fire(
        shooter, 55, 560, "mirv")
    assert any(e["e"] == "split" for e in result.events)
    assert len([e for e in result.events if e["e"] == "traj"]) >= 4


def test_digger_moves_dirt_without_hurting_anyone():
    terrain = flat_world()
    # Both tanks well clear of where the round lands, so this measures blast
    # damage only -- a tank standing over a fresh hole rightly takes fall
    # damage, and that is tested separately.
    shooter = FakeTank(0, 100, 250)
    target = FakeTank(1, 560, 250)
    result = Simulation(terrain, [shooter, target], wind=0, seed=8).fire(
        shooter, 45, 400, "dig")
    assert (shooter.hp, target.hp) == (100, 100)
    assert result.damage_dealt == {}
    assert result.terrain_ops                    # but it did move dirt


def test_tracer_leaves_the_world_untouched():
    terrain = flat_world()
    before = list(terrain.height)
    shooter = FakeTank(0, 100, 250)
    target = FakeTank(1, 300, 250)
    result = Simulation(terrain, [shooter, target], wind=0, seed=9).fire(
        shooter, 45, 500, "trac")
    assert terrain.height == before
    assert target.hp == 100
    assert result.terrain_ops == []


def test_a_tank_left_hanging_falls_and_takes_damage():
    terrain = flat_world()
    shooter = FakeTank(0, 40, 250)
    victim = FakeTank(1, 300, 250)
    sim = Simulation(terrain, [shooter, victim], wind=0, seed=10)
    # Blow a deep hole under the victim without hitting it directly.
    sim.frame = 0
    sim._terrain_op("crater", 300, 300, 60)
    sim._settle_tanks()
    assert victim.y > 250
    assert any(e["e"] == "fall" for e in sim.result.events)


def test_parachute_cancels_fall_damage():
    terrain = flat_world()
    victim = FakeTank(1, 300, 250)
    victim.parachutes = 1
    sim = Simulation(terrain, [victim], wind=0, seed=11)
    sim._terrain_op("crater", 300, 300, 60)
    sim._settle_tanks()
    assert victim.hp == 100
    assert victim.parachutes == 0
    assert any(e["e"] == "chute" for e in sim.result.events)


def test_wind_pushes_the_shell():
    terrain = flat_world()
    shooter = FakeTank(0, 150, 250)
    downwind = trace(terrain, [shooter], shooter, 60, 420, wind=100)
    upwind = trace(terrain, [shooter], shooter, 60, 420, wind=-100)
    assert downwind and upwind
    assert downwind[0] > upwind[0]


def test_wrap_wall_mode_brings_the_shell_back():
    terrain = flat_world()
    shooter = FakeTank(0, 600, 250)
    impact = trace(terrain, [shooter], shooter, 30, 1000, wind=0, wall="wrap")
    assert impact is not None
    assert 0 <= impact[0] < 640


def test_no_walls_lets_a_shell_leave_the_map():
    terrain = flat_world()
    shooter = FakeTank(0, 600, 250)
    assert trace(terrain, [shooter], shooter, 20, 1000, wind=0, wall="none") is None


def test_shot_cannot_immediately_hit_its_own_tank():
    terrain = flat_world()
    shooter = FakeTank(0, 300, 250)
    Simulation(terrain, [shooter], wind=0, seed=12).fire(shooter, 45, 800, "mis")
    assert shooter.hp == 100


def test_explosion_frames_scale_with_size():
    assert explosion_frames(60) > explosion_frames(10) > 0
