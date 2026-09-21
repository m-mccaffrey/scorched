"""Holdout: the co-operative scenario.

The point of these is that Holdout is not a second game bolted on. It is the
same resolver, the same fog, the same pathfinder and the same combat, with the
enemy supplied by a schedule instead of by a person -- so most of what is worth
pinning here is that the ordinary rules still hold with a Swarm on the board.
"""

import random

import pytest

from standing_orders.ai import BotBrain
from standing_orders.game import (PHASE_OVER, Match, Settings, load_map,
                                  map_modes)
from standing_orders.grid import MODE_HOLDOUT, MapError, TileMap
from standing_orders.state import SWARM_PID
from standing_orders.units import BUILDING, UNIT, damage_to_building
from standing_orders import waves as W


def holdout(players=2, **kwargs):
    kwargs.setdefault("map_name", "holdout")
    match = Match(Settings(**kwargs))
    for i in range(players):
        match.add_player(f"P{i}")
    match.start_match()
    return match


def run(match, turns, brains=None):
    """Play the bots through ``turns`` turns, or until it ends."""
    for _ in range(turns):
        if match.phase == PHASE_OVER:
            break
        for player in list(match.state.players.values()):
            if player.alive and player.pid != SWARM_PID:
                orders = brains[player.pid].plan(match, player) if brains else []
                match.submit(player.pid, orders)
        match.resolve(timelines=False)
        if not match.begin_orders():
            break
    return match


# -- the map carries the mode --------------------------------------------

def test_the_map_decides_the_mode_not_the_lobby():
    """There is no settings row that can contradict the terrain: a Holdout map
    has gates and its starting positions clustered, and a war map has neither
    and would be nonsense with waves walking into it."""
    assert load_map("holdout").info.mode == MODE_HOLDOUT
    assert load_map("basin").info.mode == "war"
    assert map_modes()["holdout"] == MODE_HOLDOUT
    assert map_modes()["basin"] == "war"


def test_a_holdout_map_without_gates_is_refused():
    with pytest.raises(MapError, match="gate"):
        TileMap.parse("!name Nope\n!mode holdout\n1...\n..2.\n", "nope")


def test_an_unknown_mode_is_refused():
    with pytest.raises(MapError, match="unknown mode"):
        TileMap.parse("!name Nope\n!mode sandwich\n1...\n..2.\n", "nope")


def test_gates_survive_the_trip_to_a_client():
    """Every ``!`` header has to be reapplied when a client rebuilds the map,
    or it is silently lost -- pace learned that the hard way."""
    original = load_map("holdout")
    copy = TileMap.from_wire(original.to_wire())
    assert copy.info.mode == MODE_HOLDOUT
    assert copy.gates == original.gates


# -- the Swarm ------------------------------------------------------------

def test_the_swarm_is_a_player_that_nobody_sits_behind():
    match = holdout(2)
    swarm = match.state.players[SWARM_PID]
    assert not swarm.connected, "a disconnected Swarm cannot hold up a turn"
    assert match.everyone_ready() is False or swarm.ready
    assert match.state.hostile(0, SWARM_PID)
    assert match.state.allied(0, 1), "commanders are on the same side"
    assert not match.state.hostile(0, 1)


def test_the_swarm_is_not_eliminated_for_having_no_command_post():
    """Losing your Command Post takes your force with it -- and the Swarm has
    never had one, so without an exception it dies on the first prune."""
    match = holdout(2)
    run(match, 5)
    assert match.state.players[SWARM_PID].alive


def test_the_swarm_takes_no_spawn_and_no_seat_at_the_table():
    match = holdout(4)
    posts = [b.owner for b in match.state.buildings.values() if b.code == "base"]
    assert SWARM_PID not in posts
    assert all(p.team == 0 for p in match.state.players.values()
               if p.pid != SWARM_PID)


def test_a_war_map_raises_no_swarm():
    match = Match(Settings(map_name="basin"))
    for i in range(2):
        match.add_player(f"P{i}")
    match.start_match()
    assert SWARM_PID not in match.state.players
    assert not match.state.waves


# -- the schedule ---------------------------------------------------------

def test_waves_arrive_on_schedule_and_walk_at_a_command_post():
    match = holdout(2, waves=4)
    assert match.state.waves[0]["at"] == W.FIRST_WAVE
    run(match, W.FIRST_WAVE)
    creeps = [u for u in match.state.units.values()
              if u.alive and u.owner == SWARM_PID]
    assert creeps, "the first wave never arrived"
    posts = {b.tile for b in match.state.buildings.values()
             if b.code == "base" and b.alive}
    assert all(c.stance == "attack" for c in creeps)
    assert all(c.goal in posts for c in creeps), \
        "a creep with nowhere to be is a creep that stands at the gate"


def test_the_schedule_hardens_by_rank_rather_than_by_headcount():
    """More bodies through a doorway is more income for the defence -- wave
    curves twice and four times as steep measured *easier* to survive than the
    shallow one. What breaks a line is something that survives the doorway."""
    early = W.compose(1, 4)
    late = W.compose(12, 4)
    assert W.veterancy(1) == 0
    assert W.veterancy(12) > W.veterancy(6) > 0
    assert all(rank == 0 for _code, rank, _n in early)
    assert all(rank > 0 for _code, rank, _n in late)


def test_the_rabble_retires_as_the_armour_arrives():
    """Spending a growing budget on a fixed mix makes a late wave more
    numerous rather than harder: wave twelve came out as thirteen Scouts."""
    late = {code for code, _rank, _n in W.compose(12, 4)}
    assert "scout" not in late
    assert "bruiser" in late and "siege" in late


def test_a_champion_leads_every_fifth_wave():
    champions = [w for w in W.schedule(10, 4)
                 if any(rank >= W.CHAMPION_RANK for _c, rank, _n in w["pack"])]
    assert [w["n"] for w in champions] == [5, 10]


def test_fewer_commanders_face_a_smaller_wave():
    assert W.wave_budget(8, 2) < W.wave_budget(8, 4)


# -- the economy ----------------------------------------------------------

def test_a_kill_pays_the_commander_who_made_it():
    match = holdout(2)
    state = match.state
    state.units.clear()                  # a quiet board, so the shot is ours
    state.players[0].supply = 0
    victim = state.add_unit(SWARM_PID, "trooper", 16, 9)
    victim.hp = 1
    state.add_unit(0, "bruiser", 16, 10)
    run(match, 2)
    assert not victim.alive
    assert state.players[0].supply >= W.bounty_for("trooper"), \
        "killing a creep paid nothing"


def test_a_tower_earns_its_own_bounty():
    """A tower defence where towers do not pay for themselves is a tower
    defence where nobody builds towers."""
    match = holdout(2)
    state = match.state
    state.players[0].supply = 0
    state.units.clear()
    tower = state.add_building(0, "tower", 16, 11)
    victim = state.add_unit(SWARM_PID, "scout", 16, 12)
    victim.hp = 1
    run(match, 1)
    assert tower.alive and not victim.alive
    assert state.players[0].supply >= W.bounty_for("scout")


def test_everyone_is_paid_as_a_wave_musters():
    """Paying on a clear looks fairer and is unplayable: from about wave six
    the waves overlap, the board is never empty again, and the income simply
    stops at the point a defence most needs to be spending."""
    match = holdout(2, waves=4)
    for player in match.state.players.values():
        player.supply = 0
    run(match, W.FIRST_WAVE)
    expected = W.muster_pay(match.state.waves[0])
    assert expected > 0
    for pid in (0, 1):
        assert match.state.players[pid].supply >= expected
    assert match.state.players[SWARM_PID].supply == 0


# -- winning and losing ---------------------------------------------------

def test_the_line_holds_when_the_schedule_runs_out():
    match = holdout(2, waves=5)
    run(match, 1)
    match.state.wave_at = len(match.state.waves)      # the last one has landed
    for unit in list(match.state.units.values()):
        if unit.owner == SWARM_PID:
            unit.hp = 0
    match.state.prune()
    assert match.check_over()
    assert match.phase == PHASE_OVER
    assert match.winner_team == 0


def test_the_table_loses_together():
    match = holdout(2, waves=4)
    for building in match.state.buildings.values():
        if building.code == "base":
            building.hp = 0
    match.state.prune()
    assert match.check_over()
    assert match.winner_team == SWARM_PID


def test_a_match_is_not_over_while_creeps_are_still_standing():
    match = holdout(2, waves=5)
    run(match, W.FIRST_WAVE)
    match.state.wave_at = len(match.state.waves)
    assert match.state.wave_at
    assert any(u.alive and u.owner == SWARM_PID
               for u in match.state.units.values())
    assert not match.check_over(), "the schedule is done but the board is not"


# -- the new roster -------------------------------------------------------

def test_a_mortar_outranges_a_sentry_tower_and_a_longbow_outranges_it():
    """The range ladder is the whole counter-play: a line of Sentries is free
    food for something that shells it from a tile it cannot shoot back at."""
    assert UNIT["siege"].reach > BUILDING["tower"].reach
    assert BUILDING["longbow"].reach > UNIT["siege"].reach


def test_a_mortar_flattens_masonry_and_is_useless_against_people():
    from standing_orders.units import damage_between
    assert damage_to_building("siege", "tower") > damage_to_building("trooper", "tower")
    assert damage_between("siege", "trooper") < damage_between("trooper", "trooper")


def test_a_mortar_shells_a_tower_it_cannot_be_shot_back_by():
    match = holdout(2)
    state = match.state
    state.units.clear()      # nothing else in reach: units are shot at first
    tower = state.add_building(0, "tower", 16, 9)
    mortar = state.add_unit(SWARM_PID, "siege", 16, 13)   # four tiles away
    before = tower.hp
    run(match, 1)
    assert tower.hp < before, "the Mortar did not fire"
    assert mortar.alive and mortar.hp == mortar.max_hp, \
        "the tower reached a unit that outranges it"


# -- the bots -------------------------------------------------------------

def test_bots_defend_the_doorways_rather_than_marching_on_nothing():
    """Left to the war planner a bot in Holdout looks for the weakest enemy
    Command Post, the Swarm has never had one, and the fallback picks an enemy
    *spawn point* -- which on a co-operative map is a teammate's front door."""
    match = holdout(2)
    run(match, W.FIRST_WAVE + 1)
    brain = BotBrain("veteran", random.Random(4))
    me = match.state.players[0]
    home = next(b.tile for b in match.state.buildings_of(0) if b.code == "base")
    doors = brain._doorways(match.state, home)
    assert len(doors) == len(match.state.map.gates)
    orders = brain.plan(match, me)
    spawns = set(match.state.map.spawns.values())
    aimed = {tuple(o["to"]) for o in orders if o["o"] == "attack"}
    assert not (aimed & spawns), "a bot is attacking a friendly spawn point"


def test_bots_do_not_try_to_negotiate_with_the_swarm():
    match = holdout(4)
    match.state.turn = 60
    brain = BotBrain("veteran", random.Random(5))
    assert brain._diplomacy(match, match.state.players[0]) == []


def test_bots_build_towers_before_anything_else_in_a_holdout():
    """Masonry is the plan, not a luxury bought once the army is paid for."""
    match = holdout(2)
    me = match.state.players[0]
    me.supply = 60
    brain = BotBrain("veteran", random.Random(6))
    builds = [o for o in brain.plan(match, me)
              if o["o"] == "build" and o["code"] in ("tower", "longbow")]
    assert builds, "a Holdout bot opened without a single gun"


def test_bots_mix_longbows_into_the_line():
    """A line of nothing but Sentries is free food for a Mortar Team."""
    match = holdout(2)
    state = match.state
    me = state.players[0]
    me.supply = 400
    home = next(b.tile for b in state.buildings_of(0) if b.code == "base")
    for i in range(4):
        state.add_building(0, "tower", home[0] + 2 + i, home[1] + 3)
    for i in range(6):
        state.add_unit(0, "worker", home[0] - 2, home[1] - 2 + i)
    brain = BotBrain("veteran", random.Random(7))
    codes = {o["code"] for o in brain.plan(match, me) if o["o"] == "build"}
    assert "longbow" in codes


def test_a_holdout_bot_does_not_send_engineers_prospecting():
    """Every node on the map is outside the wall. An Engineer sent across open
    ground to found a forward depot while a wave is walking it is a donation."""
    match = holdout(2)
    state = match.state
    me = state.players[0]
    me.supply = 200
    brain = BotBrain("veteran", random.Random(8))
    home = next(b.tile for b in state.buildings_of(0) if b.code == "base")
    from standing_orders.ai import HOLDOUT_REACH
    from standing_orders.grid import manhattan
    for order in brain.plan(match, me):
        if order["o"] == "build":
            assert manhattan(tuple(order["to"]), home) <= HOLDOUT_REACH + 4, \
                f"building {order['code']} out at {order['to']}"
