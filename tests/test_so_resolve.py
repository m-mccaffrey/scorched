"""Turn-resolution tests.

These are the ones that matter most: the resolver is the only thing in the
game that decides anything, and every client trusts its output blindly.
"""

import pytest

from standing_orders.grid import TileMap
from standing_orders.resolve import (ATTACK_EVERY, SUBTICKS,
                                     resolve_turn)
from standing_orders.state import MatchState, Player
from standing_orders.units import UNIT, UNIT_CAP


def arena(width=20, height=10, players=2):
    rows = ["." * width for _ in range(height)]
    rows[0] = "1" + rows[0][1:]
    rows[-1] = rows[-1][:-1] + "2"
    state = MatchState(TileMap.parse("!players 2\n" + "\n".join(rows)))
    for pid in range(players):
        state.players[pid] = Player(pid=pid, name=f"P{pid}", team=pid,
                                    color=pid, supply=30)
    return state


def events_of(result, kind):
    return [e for e in result.events if e["e"] == kind]


# -- movement --------------------------------------------------------------

def test_speed_governs_distance_travelled():
    state = arena()
    scout = state.add_unit(0, "scout", 2, 5)
    bruiser = state.add_unit(0, "bruiser", 2, 7)
    resolve_turn(state, {0: [{"o": "move", "uid": scout.uid, "to": [15, 5]},
                             {"o": "move", "uid": bruiser.uid, "to": [15, 7]}]})
    assert scout.x - 2 == UNIT["scout"].speed
    assert bruiser.x - 2 == UNIT["bruiser"].speed


def test_movement_is_spread_across_the_turn():
    """Steps must land on different beats, or the replay is a teleport."""
    state = arena()
    scout = state.add_unit(0, "scout", 2, 5)
    result, _ = resolve_turn(state, {0: [{"o": "move", "uid": scout.uid,
                                          "to": [15, 5]}]})
    beats = [e["s"] for e in events_of(result, "move")]
    assert len(beats) == 3 and len(set(beats)) == 3
    assert max(beats) <= SUBTICKS


def test_forest_costs_a_unit_half_its_turn():
    rows = ["." * 12 for _ in range(6)]
    rows[0] = "1" + rows[0][1:]
    rows[-1] = rows[-1][:-1] + "2"
    rows[3] = "..%%%%%....."
    state = MatchState(TileMap.parse("!players 2\n" + "\n".join(rows)))
    state.players[0] = Player(pid=0, name="P", team=0)
    trooper = state.add_unit(0, "trooper", 1, 3)
    resolve_turn(state, {0: [{"o": "move", "uid": trooper.uid, "to": [8, 3]}]})
    assert trooper.x == 2                      # one forest tile, not two


def test_contested_tile_goes_to_one_unit_and_blocks_the_other():
    state = arena()
    left = state.add_unit(0, "trooper", 4, 5)
    right = state.add_unit(1, "trooper", 6, 5)
    result, _ = resolve_turn(state, {
        0: [{"o": "move", "uid": left.uid, "to": [5, 5]}],
        1: [{"o": "move", "uid": right.uid, "to": [5, 5]}]})
    occupied = [u for u in (left, right) if u.tile == (5, 5)]
    assert len(occupied) == 1
    assert len(events_of(result, "block")) == 1


def test_faster_units_win_a_race_to_a_tile():
    state = arena()
    scout = state.add_unit(0, "scout", 3, 5)
    bruiser = state.add_unit(1, "bruiser", 5, 5)
    resolve_turn(state, {0: [{"o": "move", "uid": scout.uid, "to": [4, 5]}],
                         1: [{"o": "move", "uid": bruiser.uid, "to": [4, 5]}]})
    assert scout.tile == (4, 5)


def test_units_hold_when_given_no_order():
    state = arena()
    trooper = state.add_unit(0, "trooper", 5, 5)
    resolve_turn(state, {0: [{"o": "move", "uid": trooper.uid, "to": [9, 5]}]})
    moved_to = trooper.tile
    resolve_turn(state, {})
    assert trooper.tile == moved_to, "a unit should not continue last turn's march"


# -- combat ----------------------------------------------------------------

def test_engaged_units_fire_three_times_a_turn():
    state = arena()
    mine = state.add_unit(0, "trooper", 5, 5)
    theirs = state.add_unit(1, "trooper", 6, 5)
    result, _ = resolve_turn(state, {})
    volleys = SUBTICKS // ATTACK_EVERY
    assert theirs.hp == UNIT["trooper"].hp - UNIT["trooper"].attack * volleys
    assert len(events_of(result, "shoot")) == 2 * volleys
    assert mine.hp < UNIT["trooper"].hp


def test_counter_triangle_applies_both_ways():
    state = arena()
    scout = state.add_unit(0, "scout", 5, 5)
    gunner = state.add_unit(1, "ranged", 6, 5)
    resolve_turn(state, {})
    volleys = SUBTICKS // ATTACK_EVERY
    # Scout counters Gunner: 1.5x out, 0.5x back.
    assert gunner.hp == UNIT["ranged"].hp - (UNIT["scout"].attack * 3 // 2) * volleys
    assert scout.hp == UNIT["scout"].hp - (UNIT["ranged"].attack // 2) * volleys


def test_gunners_outrange_melee():
    state = arena()
    gunner = state.add_unit(0, "ranged", 5, 5)
    bruiser = state.add_unit(1, "bruiser", 7, 5)
    resolve_turn(state, {})
    assert bruiser.hp < UNIT["bruiser"].hp     # gunner reaches two tiles
    assert gunner.hp == UNIT["ranged"].hp      # bruiser cannot reach back


def test_allies_do_not_shoot_each_other():
    state = arena()
    state.players[1].team = 0                  # same team
    a = state.add_unit(0, "trooper", 5, 5)
    b = state.add_unit(1, "trooper", 6, 5)
    resolve_turn(state, {})
    assert (a.hp, b.hp) == (UNIT["trooper"].hp, UNIT["trooper"].hp)


def test_deaths_are_reported_and_stop_the_unit_acting():
    state = arena()
    victim = state.add_unit(1, "scout", 6, 5)
    victim.hp = 1
    state.add_unit(0, "bruiser", 5, 5)
    result, _ = resolve_turn(state, {})
    kills = events_of(result, "kill")
    assert len(kills) == 1 and kills[0]["uid"] == victim.uid
    assert victim.uid not in state.units


def test_units_target_the_weakest_in_reach():
    state = arena()
    state.add_unit(0, "trooper", 5, 5)
    healthy = state.add_unit(1, "trooper", 6, 5)
    hurt = state.add_unit(1, "trooper", 4, 5)
    # Tough enough to survive the whole turn, so focus fire stays on it and
    # does not spill onto the healthy one after a kill.
    hurt.hp = 20
    resolve_turn(state, {})
    assert hurt.hp == 20 - UNIT["trooper"].attack * (SUBTICKS // ATTACK_EVERY)
    assert healthy.hp == UNIT["trooper"].hp


def test_buildings_are_attacked_only_when_no_unit_is_in_reach():
    state = arena()
    state.add_unit(0, "trooper", 5, 5)
    guard = state.add_unit(1, "trooper", 6, 5)
    base = state.add_building(1, "base", 5, 4)
    resolve_turn(state, {})
    assert guard.hp < UNIT["trooper"].hp
    assert base.hp == base.type.hp


# -- orders ----------------------------------------------------------------

def test_training_charges_supply_and_produces_on_time():
    state = arena()
    base = state.add_building(0, "base", 5, 5)
    before = state.players[0].supply
    result, rejected = resolve_turn(state, {
        0: [{"o": "train", "bid": base.bid, "code": "trooper"}]})
    assert not rejected
    spent = UNIT["trooper"].cost
    income = result.income[0]
    assert state.players[0].supply == before - spent + income
    # Troopers take two turns, so nothing yet.
    assert not events_of(result, "spawn")
    result, _ = resolve_turn(state, {})
    assert len(events_of(result, "spawn")) == 1


def test_cannot_train_beyond_the_army_cap():
    state = arena()
    base = state.add_building(0, "base", 5, 5)
    state.players[0].supply = 999
    for i in range(UNIT_CAP):
        state.add_unit(0, "scout", (i % 15) + 2, 8)
    _result, rejected = resolve_turn(state, {
        0: [{"o": "train", "bid": base.bid, "code": "scout"}]})
    assert rejected[0] and "cap" in rejected[0][0]


def test_cannot_spend_the_same_supply_twice():
    state = arena()
    base = state.add_building(0, "base", 5, 5)
    state.players[0].supply = UNIT["trooper"].cost
    _result, rejected = resolve_turn(state, {
        0: [{"o": "train", "bid": base.bid, "code": "trooper"},
            {"o": "train", "bid": base.bid, "code": "trooper"}]})
    assert rejected[0] and "supply" in rejected[0][0]


def test_barracks_gates_the_advanced_units():
    state = arena()
    base = state.add_building(0, "base", 5, 5)
    _result, rejected = resolve_turn(state, {
        0: [{"o": "train", "bid": base.bid, "code": "bruiser"}]})
    assert rejected[0] and "cannot train" in rejected[0][0]


def test_construction_takes_time_then_works():
    state = arena()
    base = state.add_building(0, "base", 5, 5)
    state.players[0].supply = 50
    result, rejected = resolve_turn(state, {
        0: [{"o": "build", "bid": base.bid, "code": "barracks", "to": [8, 5]}]})
    assert not rejected
    new = [b for b in state.buildings.values() if b.code == "barracks"][0]
    assert not new.operational
    for _ in range(3):
        resolve_turn(state, {})
    assert new.operational
    _result, rejected = resolve_turn(state, {
        0: [{"o": "train", "bid": new.bid, "code": "bruiser"}]})
    assert not rejected


def test_cannot_build_far_from_your_territory():
    state = arena()
    base = state.add_building(0, "base", 2, 2)
    state.players[0].supply = 50
    _result, rejected = resolve_turn(state, {
        0: [{"o": "build", "bid": base.bid, "code": "barracks", "to": [18, 8]}]})
    assert rejected[0] and "within" in rejected[0][0]


def test_cannot_order_another_players_units():
    state = arena()
    theirs = state.add_unit(1, "trooper", 5, 5)
    _result, rejected = resolve_turn(state, {
        0: [{"o": "move", "uid": theirs.uid, "to": [9, 5]}]})
    assert rejected[0]
    assert theirs.tile == (5, 5)


@pytest.mark.parametrize("order", [
    {"o": "nonsense"}, {"o": "move", "uid": 999, "to": [1, 1]},
    {"o": "move", "uid": 1, "to": "over there"},
    {"o": "train", "bid": 999, "code": "scout"},
    {"o": "build", "bid": 1, "code": "base", "to": [1, 1]},
])
def test_malformed_orders_are_rejected_not_fatal(order):
    state = arena()
    state.add_unit(0, "trooper", 5, 5)
    state.add_building(0, "base", 3, 3)
    result, rejected = resolve_turn(state, {0: [order]})
    assert rejected.get(0)
    assert result.subticks == SUBTICKS


# -- economy and victory ---------------------------------------------------

def test_node_capture_persists_after_the_unit_leaves():
    state = arena()
    state.map.nodes.append((5, 5))
    state.add_building(0, "base", 2, 2)
    unit = state.add_unit(0, "trooper", 5, 5)
    result, _ = resolve_turn(state, {})
    assert state.node_owner[(5, 5)] == 0
    assert len(events_of(result, "capture")) == 1
    base_income = result.income[0]
    # March away; the node keeps paying.
    resolve_turn(state, {0: [{"o": "move", "uid": unit.uid, "to": [9, 9]}]})
    assert state.node_owner[(5, 5)] == 0
    result, _ = resolve_turn(state, {})
    assert result.income[0] == base_income


def test_a_node_changes_hands():
    state = arena()
    state.map.nodes.append((5, 5))
    mine = state.add_unit(0, "trooper", 5, 5)
    resolve_turn(state, {})
    assert state.node_owner[(5, 5)] == 0
    mine.hp = 0
    state.prune()
    state.add_unit(1, "trooper", 5, 5)
    resolve_turn(state, {})
    assert state.node_owner[(5, 5)] == 1


def test_losing_your_base_eliminates_you():
    state = arena()
    base = state.add_building(1, "base", 6, 5)
    base.hp = 1
    state.add_unit(0, "bruiser", 5, 5)
    result, _ = resolve_turn(state, {})
    assert 1 in result.eliminated
    assert not state.players[1].alive
    assert state.living_teams() == set()


def test_resolution_is_deterministic():
    """The same state and orders must always produce the same timeline."""
    def once():
        state = arena()
        state.add_unit(0, "trooper", 4, 5)
        state.add_unit(0, "scout", 4, 6)
        state.add_unit(1, "ranged", 8, 5)
        state.add_building(1, "base", 12, 5)
        orders = {0: [{"o": "attack", "uid": 1, "to": [12, 5]},
                      {"o": "move", "uid": 2, "to": [10, 7]}],
                  1: [{"o": "attack", "uid": 3, "to": [4, 5]}]}
        result, _ = resolve_turn(state, orders)
        return result.events
    assert once() == once()
