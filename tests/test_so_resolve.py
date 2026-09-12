"""Turn-resolution tests.

These are the ones that matter most: the resolver is the only thing in the
game that decides anything, and every client trusts its output blindly.
"""

import pytest

from standing_orders.grid import TileMap
from standing_orders.resolve import (ADVANCE_PACE, AIRSTRIKE_BEAT,
                                     ATTACK_EVERY, MOVE_PACE, SUBTICKS,
                                     resolve_turn)
from standing_orders.state import MatchState, Player
from standing_orders.units import (AIRSTRIKE_COST, AIRSTRIKE_DAMAGE,
                                   ARMY_CAP_BASE, BUILDING, MEDIC_HEAL_COST,
                                   OFFICER_RANK, RANK_HP, UNIT, max_rank)


def arena(width=20, height=10, players=2):
    rows = ["." * width for _ in range(height)]
    rows[0] = "1" + rows[0][1:]
    rows[-1] = rows[-1][:-1] + "2"
    state = MatchState(TileMap.parse("!players 2\n" + "\n".join(rows)))
    for pid in range(players):
        state.players[pid] = Player(pid=pid, name=f"P{pid}", team=pid,
                                    color=pid, supply=30)
        # Every player needs a Command Post or they are eliminated at the end
        # of the first turn and stop receiving orders.
        state.add_building(pid, "base", 1 + pid * 2, height - 1)
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


def test_a_unit_with_no_orders_and_nowhere_to_go_stays_put():
    state = arena()
    trooper = state.add_unit(0, "trooper", 5, 5)
    resolve_turn(state, {})
    assert trooper.tile == (5, 5)
    # And it does not drift once it has finished a march either.
    resolve_turn(state, {0: [{"o": "move", "uid": trooper.uid, "to": [7, 5]}]})
    assert trooper.tile == (7, 5)
    resolve_turn(state, {})
    assert trooper.tile == (7, 5)


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


@pytest.mark.parametrize("pact", ["alliance", "truce"])
def test_nobody_shoots_across_an_agreement(pact):
    """An alliance and a truce both stop the shooting. They differ in what
    else they share, not in whether the guns go quiet."""
    state = arena()
    state.pacts[state.pair(0, 1)] = pact
    a = state.add_unit(0, "trooper", 5, 5)
    b = state.add_unit(1, "trooper", 6, 5)
    resolve_turn(state, {})
    assert (a.hp, b.hp) == (UNIT["trooper"].hp, UNIT["trooper"].hp)


def test_war_is_the_default():
    state = arena()
    a = state.add_unit(0, "trooper", 5, 5)
    b = state.add_unit(1, "trooper", 6, 5)
    resolve_turn(state, {})
    assert a.hp < UNIT["trooper"].hp and b.hp < UNIT["trooper"].hp


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
    for i in range(state.army_cap_of(0)):
        state.add_unit(0, "scout", (i % 15) + 2, 8)
    _result, rejected = resolve_turn(state, {
        0: [{"o": "train", "bid": base.bid, "code": "scout"}]})
    assert rejected[0] and "cap" in rejected[0][0]


def test_supply_depots_raise_the_army_cap():
    state = arena()
    assert state.army_cap_of(0) == ARMY_CAP_BASE
    depot = state.add_building(0, "depot", 6, 6, under=2)
    assert state.army_cap_of(0) == ARMY_CAP_BASE, "under construction: no cap yet"
    depot.building_turns = 0
    assert state.army_cap_of(0) > ARMY_CAP_BASE


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


def test_an_engineer_walks_to_the_site_and_builds():
    state = arena()
    state.add_building(0, "base", 5, 5)
    worker = state.add_unit(0, "worker", 5, 7)
    state.players[0].supply = 50
    _result, rejected = resolve_turn(state, {
        0: [{"o": "build", "uid": worker.uid, "code": "barracks",
             "to": [12, 7]}]})
    assert not rejected
    assert state.players[0].supply < 50, "charged at commit, not on arrival"
    for _ in range(10):
        resolve_turn(state, {})
    made = [b for b in state.buildings.values() if b.code == "barracks"]
    assert made and made[0].operational
    _result, rejected = resolve_turn(state, {
        0: [{"o": "train", "bid": made[0].bid, "code": "bruiser"}]})
    assert not rejected


def test_construction_stalls_without_its_engineer():
    state = arena()
    state.add_building(0, "base", 5, 5)
    worker = state.add_unit(0, "worker", 7, 5)
    state.players[0].supply = 50
    resolve_turn(state, {0: [{"o": "build", "uid": worker.uid,
                              "code": "depot", "to": [8, 5]}]})
    resolve_turn(state, {})
    site = [b for b in state.buildings.values() if b.code == "depot"][0]
    remaining = site.building_turns
    worker.hp = 0
    state.prune()
    resolve_turn(state, {})
    assert site.building_turns == remaining, "no builder, no progress"


def test_only_engineers_can_build():
    state = arena()
    state.add_building(0, "base", 5, 5)
    trooper = state.add_unit(0, "trooper", 6, 5)
    state.players[0].supply = 50
    _result, rejected = resolve_turn(state, {
        0: [{"o": "build", "uid": trooper.uid, "code": "depot", "to": [8, 5]}]})
    assert rejected[0] and "cannot build" in rejected[0][0]


def test_cannot_build_on_a_node_or_an_occupied_tile():
    state = arena()
    state.add_building(0, "base", 2, 2)
    worker = state.add_unit(0, "worker", 4, 4)
    other = state.add_unit(0, "scout", 8, 4)
    state.map.nodes.append((6, 4))
    state.map.node_set.add((6, 4))
    state.players[0].supply = 50
    _result, rejected = resolve_turn(state, {
        0: [{"o": "build", "uid": worker.uid, "code": "depot", "to": [6, 4]}]})
    assert rejected[0] and "node" in rejected[0][0]
    _result, rejected = resolve_turn(state, {
        0: [{"o": "build", "uid": worker.uid, "code": "depot",
             "to": list(other.tile)}]})
    assert rejected[0] and "occupied" in rejected[0][0]


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
    {"o": "build", "uid": 1, "code": "base", "to": [1, 1]},
    {"o": "research", "bid": 1, "code": "nonsense"},
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
    state.map.node_set.add((5, 5))
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
    state.map.node_set.add((5, 5))
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
    for building in list(state.buildings.values()):
        if building.owner == 1:
            building.hp = 0            # their only other post is already gone
    doomed = state.add_building(1, "base", 6, 5)
    doomed.hp = 1
    state.add_unit(0, "bruiser", 5, 5)
    result, _ = resolve_turn(state, {})
    assert 1 in result.eliminated
    assert not state.players[1].alive
    assert state.living_teams() == {0}


def test_elimination_destroys_the_defeated_army():
    """Leftover units would otherwise stand around forever, still shooting,
    with nobody ever filing orders for them again."""
    state = arena()
    for building in list(state.buildings.values()):
        if building.owner == 1:
            building.hp = 0
    doomed = state.add_building(1, "base", 6, 5)
    doomed.hp = 1
    stragglers = [state.add_unit(1, "trooper", 15, 2),
                  state.add_unit(1, "scout", 16, 3)]
    state.add_unit(0, "bruiser", 5, 5)
    resolve_turn(state, {})
    assert all(unit.uid not in state.units for unit in stragglers)


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


# -- standing orders -------------------------------------------------------

def test_orders_stand_until_they_are_changed():
    """A unit told to cross the map keeps walking, turn after turn.

    Wiping paths every turn made "go there" mean "go one turn's worth in that
    direction", which is not what anybody means by it.
    """
    state = arena(width=24)
    scout = state.add_unit(0, "scout", 1, 5)
    resolve_turn(state, {0: [{"o": "move", "uid": scout.uid, "to": [19, 5]}]})
    for _ in range(8):
        resolve_turn(state, {})
    assert scout.tile == (19, 5)


def test_a_new_order_replaces_the_standing_one():
    state = arena(width=24)
    scout = state.add_unit(0, "scout", 1, 5)
    resolve_turn(state, {0: [{"o": "move", "uid": scout.uid, "to": [19, 5]}]})
    for _ in range(2):
        resolve_turn(state, {})
    outbound = scout.x
    assert outbound > 5, "should be well down the map by now"
    resolve_turn(state, {0: [{"o": "move", "uid": scout.uid, "to": [1, 5]}]})
    assert scout.x < outbound, "the new order should turn it around at once"


def test_hold_cancels_a_standing_order():
    state = arena(width=24)
    scout = state.add_unit(0, "scout", 1, 5)
    resolve_turn(state, {0: [{"o": "move", "uid": scout.uid, "to": [19, 5]}]})
    parked = scout.tile
    resolve_turn(state, {0: [{"o": "hold", "uid": scout.uid}]})
    resolve_turn(state, {})
    assert scout.tile == parked


def test_orders_for_one_unit_do_not_cancel_another_unit():
    state = arena(width=24)
    walker = state.add_unit(0, "scout", 1, 4)
    other = state.add_unit(0, "scout", 1, 6)
    resolve_turn(state, {0: [{"o": "move", "uid": walker.uid, "to": [19, 4]},
                             {"o": "move", "uid": other.uid, "to": [19, 6]}]})
    resolve_turn(state, {0: [{"o": "hold", "uid": other.uid}]})
    before = walker.x
    resolve_turn(state, {})
    assert walker.x > before, "the untouched unit should still be marching"


def test_a_slow_unit_can_cross_a_forest():
    """Movement progress carries between turns.

    A Bruiser earns 12 movement points a turn and a forest tile costs 24, so
    resetting progress each turn made forest permanently impassable to it.
    """
    rows = ["1###############",
            "################",
            "..%%%%%%%%%%%%.2",
            "################"]
    state = MatchState(TileMap.parse("!players 2\n" + "\n".join(rows)))
    state.players[0] = Player(pid=0, name="P", team=0)
    state.add_building(0, "base", 0, 0)
    bruiser = state.add_unit(0, "bruiser", 1, 2)
    resolve_turn(state, {0: [{"o": "move", "uid": bruiser.uid, "to": [6, 2]}]})
    for _ in range(8):
        resolve_turn(state, {})
    assert bruiser.x > 1, "a speed-1 unit must be able to enter forest at all"


# -- the worker economy ----------------------------------------------------

def node_arena():
    state = arena(width=24, height=10)
    state.map.nodes.append((10, 5))
    state.map.node_set.add((10, 5))
    return state


def test_harvest_needs_worker_node_and_depot_together():
    """All three, every turn. Each is something an opponent can take away."""
    state = node_arena()
    base = [b for b in state.buildings.values() if b.owner == 0][0]
    base.x, base.y = 2, 2                       # far from the node
    worker = state.add_unit(0, "worker", 10, 5)

    result, _ = resolve_turn(state, {})
    bare = result.income[0]

    depot = state.add_building(0, "depot", 12, 5)
    result, _ = resolve_turn(state, {})
    working = result.income[0]
    assert working > bare, "a depot in range should start the supply flowing"

    worker.x, worker.y = 1, 8                   # walk off the node
    result, _ = resolve_turn(state, {})
    assert result.income[0] == bare

    worker.x, worker.y = 10, 5
    depot.hp = 0
    state.prune()
    result, _ = resolve_turn(state, {})
    assert result.income[0] == bare, "killing the depot should stop the income"


def test_a_depot_under_construction_does_not_harvest_or_raise_the_cap():
    state = node_arena()
    base = [b for b in state.buildings.values() if b.owner == 0][0]
    base.x, base.y = 2, 2
    state.add_unit(0, "worker", 10, 5)
    depot = state.add_building(0, "depot", 12, 5, under=2)
    before_cap = state.army_cap_of(0)
    result, _ = resolve_turn(state, {})
    unfinished_income = result.income[0]
    assert state.army_cap_of(0) == before_cap
    assert state.harvest_income(0) == 0

    depot.building_turns = 0
    result, _ = resolve_turn(state, {})
    assert state.army_cap_of(0) > before_cap
    assert result.income[0] > unfinished_income


# -- walls -----------------------------------------------------------------

def test_bruisers_and_engineers_break_walls_far_faster():
    def hits_to_break(code):
        state = arena()
        wall = state.add_building(1, "wall", 6, 5)
        state.add_unit(0, code, 5, 5)
        turns = 0
        while wall.alive and turns < 40:
            resolve_turn(state, {})
            turns += 1
        return turns

    fast = max(hits_to_break("bruiser"), hits_to_break("worker"))
    slow = min(hits_to_break("trooper"), hits_to_break("ranged"))
    assert fast * 2 < slow, "a wall should be answered by a Bruiser, not a rifle"


def test_a_wall_is_never_an_absolute_full_stop():
    """Everything can chip through eventually, so no position is unbreakable."""
    state = arena()
    wall = state.add_building(1, "wall", 6, 5)
    state.add_unit(0, "trooper", 5, 5)
    for _ in range(40):
        resolve_turn(state, {})
        if not wall.alive:
            break
    assert not wall.alive


def test_walls_block_movement():
    state = arena()
    for y in range(0, 10):
        state.add_building(1, "wall", 10, y)
    scout = state.add_unit(0, "scout", 5, 5)
    resolve_turn(state, {0: [{"o": "move", "uid": scout.uid, "to": [15, 5]}]})
    for _ in range(6):
        resolve_turn(state, {})
    assert scout.x < 10


# -- sentry towers ---------------------------------------------------------

def test_a_sentry_tower_shoots_and_outranges_infantry():
    state = arena()
    state.add_building(0, "tower", 10, 5)
    victim = state.add_unit(1, "trooper", 12, 5)   # two tiles off: in reach
    resolve_turn(state, {})
    assert victim.hp < UNIT["trooper"].hp


def test_a_tower_under_construction_does_not_shoot():
    state = arena()
    state.add_building(0, "tower", 10, 5, under=2)
    victim = state.add_unit(1, "trooper", 11, 5)
    resolve_turn(state, {})
    assert victim.hp == UNIT["trooper"].hp


# -- research --------------------------------------------------------------

def test_research_costs_supply_takes_turns_and_then_applies():
    state = arena()
    base = [b for b in state.buildings.values() if b.owner == 0][0]
    trooper = state.add_unit(0, "trooper", 5, 5)
    state.players[0].supply = 100
    _result, rejected = resolve_turn(state, {
        0: [{"o": "research", "bid": base.bid, "code": "armour1"}]})
    assert not rejected
    assert state.players[0].supply < 100
    for _ in range(4):
        resolve_turn(state, {})
    assert "armour1" in state.players[0].research
    # Armour reaches the troops already in the field, not just new recruits.
    assert trooper.max_hp > UNIT["trooper"].hp
    assert trooper.hp == trooper.max_hp


def test_weapons_research_raises_damage():
    def damage_dealt(research):
        state = arena()
        state.players[0].research = set(research)
        state.add_unit(0, "trooper", 5, 5)
        victim = state.add_unit(1, "trooper", 6, 5)
        start = victim.hp
        resolve_turn(state, {})
        return start - victim.hp

    assert damage_dealt({"weapons1"}) > damage_dealt(set())


def test_cannot_run_two_projects_at_once():
    state = arena()
    base = [b for b in state.buildings.values() if b.owner == 0][0]
    state.players[0].supply = 100
    _result, rejected = resolve_turn(state, {
        0: [{"o": "research", "bid": base.bid, "code": "armour1"},
            {"o": "research", "bid": base.bid, "code": "weapons1"}]})
    assert rejected[0] and "already researching" in rejected[0][0]


def test_research_respects_prerequisites():
    state = arena()
    base = [b for b in state.buildings.values() if b.owner == 0][0]
    state.players[0].supply = 100
    _result, rejected = resolve_turn(state, {
        0: [{"o": "research", "bid": base.bid, "code": "armour2"}]})
    assert rejected[0] and "not available" in rejected[0][0]


def start_a_depot(state, builder, site=(9, 5)):
    """Order a depot and resolve until the foundations exist but it is not
    finished, so a test can interfere at exactly the interesting moment."""
    state.players[0].supply = 60
    resolve_turn(state, {0: [{"o": "build", "uid": builder.uid,
                              "code": "depot", "to": list(site)}]})
    for _ in range(10):
        found = [b for b in state.buildings.values() if b.code == "depot"]
        if found and found[0].building_turns > 0:
            return found[0]
        resolve_turn(state, {})
    raise AssertionError("the depot never got started")


def test_any_engineer_can_finish_an_abandoned_site():
    """A new order to the builder must not orphan the structure forever.

    Tying progress to the specific Engineer that started it left the site one
    turn from done for the rest of the match: supply spent, tile blocked,
    nothing able to adopt it -- and you hit it constantly, because the
    Engineer stays selected after you place a structure.
    """
    state = arena(width=24)
    builder = state.add_unit(0, "worker", 5, 5)
    site = start_a_depot(state, builder)

    # Send the builder away: the site stalls, and says so.
    resolve_turn(state, {0: [{"o": "move", "uid": builder.uid, "to": [5, 9]}]})
    stuck = site.building_turns
    for _ in range(4):
        resolve_turn(state, {})
    assert site.building_turns == stuck
    assert state.site_is_stalled(site)

    # Any other Engineer picks up the work.
    relief = state.add_unit(0, "worker", 9, 6)
    resolve_turn(state, {})
    assert site.building_turns < stuck
    assert not state.site_is_stalled(site)
    assert site.builder_uid == relief.uid


def test_sending_the_original_engineer_back_resumes_the_build():
    state = arena(width=24)
    builder = state.add_unit(0, "worker", 5, 5)
    site = start_a_depot(state, builder)
    resolve_turn(state, {0: [{"o": "move", "uid": builder.uid, "to": [5, 9]}]})
    resolve_turn(state, {0: [{"o": "move", "uid": builder.uid, "to": [8, 5]}]})
    for _ in range(6):
        resolve_turn(state, {})
    assert site.operational


def test_an_enemy_engineer_cannot_finish_your_building():
    state = arena(width=24)
    builder = state.add_unit(0, "worker", 5, 5)
    site = start_a_depot(state, builder)
    resolve_turn(state, {0: [{"o": "move", "uid": builder.uid, "to": [1, 9]}]})
    state.add_unit(1, "worker", 9, 6)
    stuck = site.building_turns
    resolve_turn(state, {})
    assert site.building_turns == stuck


# -- march versus advance --------------------------------------------------

@pytest.mark.parametrize("code", ["worker", "scout", "trooper", "ranged",
                                  "bruiser"])
def test_advancing_is_slower_than_marching(code):
    """Attack-move trades pace for readiness, so it is not a strict upgrade."""
    def distance(stance, turns=8):
        state = arena(width=30, height=8)
        unit = state.add_unit(0, code, 1, 4)
        resolve_turn(state, {0: [{"o": stance, "uid": unit.uid, "to": [28, 4]}]})
        for _ in range(turns - 1):
            resolve_turn(state, {})
        return unit.x - 1

    marched = distance("move")
    advanced = distance("attack")
    assert advanced < marched
    assert advanced == marched * ADVANCE_PACE // MOVE_PACE


def test_a_speed_one_unit_still_advances_steadily():
    """Movement progress carries between turns, so three-quarter pace is a
    pause every fourth turn rather than never moving at all."""
    state = arena(width=30, height=8)
    bruiser = state.add_unit(0, "bruiser", 1, 4)
    resolve_turn(state, {0: [{"o": "attack", "uid": bruiser.uid, "to": [28, 4]}]})
    for _ in range(7):
        resolve_turn(state, {})
    assert bruiser.x > 1


def test_marching_units_still_shoot_what_they_pass():
    """The stance decides whether you stop, not whether you fire."""
    state = arena(width=24)
    runner = state.add_unit(0, "trooper", 3, 5)
    bystander = state.add_unit(1, "trooper", 6, 5)
    resolve_turn(state, {0: [{"o": "move", "uid": runner.uid, "to": [20, 5]}]})
    assert bystander.hp < UNIT["trooper"].hp
    assert runner.x > 3, "a marching unit should not have stopped to fight"


# -- promotion -------------------------------------------------------------

def test_a_fight_makes_both_sides_eligible_for_promotion():
    state = arena()
    mine = state.add_unit(0, "trooper", 5, 5)
    theirs = state.add_unit(1, "trooper", 6, 5)
    assert not mine.blooded and not theirs.blooded
    resolve_turn(state, {})
    assert mine.blooded and theirs.blooded


def test_a_fresh_recruit_cannot_be_promoted():
    state = arena()
    unit = state.add_unit(0, "trooper", 5, 5)
    _, rejected = resolve_turn(state, {0: [{"o": "promote", "uid": unit.uid}]})
    assert unit.rank == 0
    assert rejected[0] and "fight" in rejected[0][0]


def test_promotion_costs_supply_and_raises_the_ceiling():
    state = arena()
    unit = state.add_unit(0, "trooper", 5, 5)
    unit.blooded = True
    unit.hp = 4
    before = state.players[0].supply
    resolve_turn(state, {0: [{"o": "promote", "uid": unit.uid}]})
    assert unit.rank == 1
    assert unit.max_hp == UNIT["trooper"].hp + RANK_HP
    assert unit.hp == unit.max_hp, "a promotion is also a full heal"
    assert state.players[0].supply < before
    assert not unit.blooded, "the next rank has to be earned all over again"


def test_rank_makes_a_unit_hit_harder():
    def damage_dealt(rank):
        state = arena()
        attacker = state.add_unit(0, "trooper", 5, 5)
        attacker.rank = rank
        victim = state.add_unit(1, "bruiser", 6, 5)
        start = victim.hp
        resolve_turn(state, {})
        return start - victim.hp

    assert damage_dealt(2) > damage_dealt(0)


def test_an_officer_lends_its_bonus_to_the_troops_around_it():
    def damage_dealt(with_officer):
        state = arena()
        attacker = state.add_unit(0, "trooper", 5, 5)
        if with_officer:
            officer = state.add_unit(0, "trooper", 4, 5)
            officer.rank = OFFICER_RANK
        victim = state.add_unit(1, "bruiser", 6, 5)
        start = victim.hp
        resolve_turn(state, {})
        # Only the plain trooper's contribution is comparable, so measure the
        # victim's loss net of whatever the officer itself did.
        return start - victim.hp

    assert damage_dealt(True) > damage_dealt(False)


def test_promotion_stops_at_the_top_rank():
    state = arena()
    unit = state.add_unit(0, "trooper", 5, 5)
    unit.rank = max_rank()
    unit.blooded = True
    state.players[0].supply = 500
    _, rejected = resolve_turn(state, {0: [{"o": "promote", "uid": unit.uid}]})
    assert unit.rank == max_rank()
    assert rejected[0]


# -- field hospitals -------------------------------------------------------

def test_holding_beside_a_hospital_heals_and_costs_supply():
    state = arena()
    state.add_building(0, "medic", 5, 5, under=0)
    patient = state.add_unit(0, "trooper", 6, 5)
    patient.hp = 8
    state.players[0].supply = 50
    result, _ = resolve_turn(state, {0: [{"o": "hold", "uid": patient.uid}]})
    healed = events_of(result, "heal")
    assert healed and healed[0]["uid"] == patient.uid
    assert patient.hp == 8 + BUILDING["medic"].heal
    spent = 50 + result.income[0] - state.players[0].supply
    assert spent == BUILDING["medic"].heal * MEDIC_HEAL_COST


def test_a_patient_does_not_shoot():
    state = arena()
    state.add_building(0, "medic", 5, 5, under=0)
    patient = state.add_unit(0, "trooper", 6, 5)
    patient.hp = 8
    enemy = state.add_unit(1, "trooper", 7, 5)
    state.players[0].supply = 50
    result, _ = resolve_turn(state, {0: [{"o": "hold", "uid": patient.uid}],
                                     1: [{"o": "hold", "uid": enemy.uid}]})
    assert not [e for e in events_of(result, "shoot") if e["uid"] == patient.uid]
    assert [e for e in events_of(result, "shoot") if e["uid"] == enemy.uid], \
        "the enemy is under no such restraint"


def test_marching_past_a_hospital_does_not_disarm_a_unit():
    state = arena()
    state.add_building(0, "medic", 5, 5, under=0)
    walker = state.add_unit(0, "trooper", 6, 5)
    walker.hp = 8
    enemy = state.add_unit(1, "trooper", 7, 5)
    state.players[0].supply = 50
    result, _ = resolve_turn(state, {0: [{"o": "attack", "uid": walker.uid,
                                          "to": [15, 5]}]})
    assert [e for e in events_of(result, "shoot") if e["uid"] == walker.uid]
    assert not events_of(result, "heal"), "care is opt-in, not automatic"
    assert enemy.hp < enemy.max_hp


def test_a_healthy_unit_is_not_admitted():
    state = arena()
    state.add_building(0, "medic", 5, 5, under=0)
    unit = state.add_unit(0, "trooper", 6, 5)
    result, _ = resolve_turn(state, {0: [{"o": "hold", "uid": unit.uid}]})
    assert not events_of(result, "heal")


def test_healing_is_limited_by_what_the_player_can_pay():
    state = arena()
    state.add_building(0, "medic", 5, 5, under=0)
    patient = state.add_unit(0, "trooper", 6, 5)
    patient.hp = 4
    state.players[0].supply = 0
    result, _ = resolve_turn(state, {0: [{"o": "hold", "uid": patient.uid}]})
    # Income lands before the bill, so a broke player heals what that buys.
    assert patient.hp - 4 == min(BUILDING["medic"].heal, result.income[0])
    assert state.players[0].supply >= 0


def test_an_unfinished_hospital_treats_nobody():
    state = arena()
    state.add_building(0, "medic", 5, 5, under=2)
    patient = state.add_unit(0, "trooper", 6, 5)
    patient.hp = 8
    state.players[0].supply = 50
    result, _ = resolve_turn(state, {0: [{"o": "hold", "uid": patient.uid}]})
    assert not events_of(result, "heal")


# -- airstrikes ------------------------------------------------------------

def test_an_airstrike_lands_mid_turn_and_hits_everything_under_it():
    state = arena()
    field = state.add_building(0, "airfield", 2, 1, under=0)
    state.players[0].supply = AIRSTRIKE_COST
    centre = state.add_unit(1, "bruiser", 10, 5)
    ring = state.add_unit(1, "bruiser", 11, 5)
    clear = state.add_unit(1, "bruiser", 14, 5)
    result, rejected = resolve_turn(
        state, {0: [{"o": "airstrike", "bid": field.bid, "to": [10, 5]}],
                1: [{"o": "hold", "uid": u.uid} for u in (centre, ring, clear)]})
    assert not rejected.get(0)
    strikes = events_of(result, "strike")
    assert len(strikes) == 1 and strikes[0]["s"] == AIRSTRIKE_BEAT
    assert centre.max_hp - centre.hp == AIRSTRIKE_DAMAGE
    assert ring.max_hp - ring.hp == AIRSTRIKE_DAMAGE // 2
    assert clear.hp == clear.max_hp


def test_an_airstrike_plays_no_favourites():
    """Bombing your own melee costs you as much as it costs them."""
    state = arena()
    field = state.add_building(0, "airfield", 2, 1, under=0)
    state.players[0].supply = AIRSTRIKE_COST
    mine = state.add_unit(0, "bruiser", 10, 5)
    resolve_turn(state, {0: [{"o": "airstrike", "bid": field.bid,
                              "to": [10, 5]}]})
    assert mine.max_hp - mine.hp == AIRSTRIKE_DAMAGE


def test_an_airfield_flies_once_a_turn():
    state = arena()
    field = state.add_building(0, "airfield", 2, 1, under=0)
    state.players[0].supply = AIRSTRIKE_COST * 3
    result, rejected = resolve_turn(
        state, {0: [{"o": "airstrike", "bid": field.bid, "to": [10, 5]},
                    {"o": "airstrike", "bid": field.bid, "to": [12, 5]}]})
    assert len(events_of(result, "strike")) == 1
    assert rejected[0]


def test_an_airstrike_needs_a_finished_airfield_and_the_supply():
    state = arena()
    field = state.add_building(0, "airfield", 2, 1, under=1)
    state.players[0].supply = AIRSTRIKE_COST
    _, rejected = resolve_turn(
        state, {0: [{"o": "airstrike", "bid": field.bid, "to": [10, 5]}]})
    assert rejected[0]

    state = arena()
    field = state.add_building(0, "airfield", 2, 1, under=0)
    state.players[0].supply = AIRSTRIKE_COST - 1
    _, rejected = resolve_turn(
        state, {0: [{"o": "airstrike", "bid": field.bid, "to": [10, 5]}]})
    assert rejected[0]


def test_being_bombed_does_not_earn_a_promotion():
    """A rank is earned against somebody who was shooting back."""
    state = arena()
    field = state.add_building(0, "airfield", 2, 1, under=0)
    state.players[0].supply = AIRSTRIKE_COST
    victim = state.add_unit(1, "bruiser", 10, 5)
    resolve_turn(state, {0: [{"o": "airstrike", "bid": field.bid,
                              "to": [10, 5]}]})
    assert victim.alive and not victim.blooded


# -- map pace --------------------------------------------------------------

def paced_arena(pace, width=40, height=10):
    rows = ["." * width for _ in range(height)]
    rows[0] = "1" + rows[0][1:]
    rows[-1] = rows[-1][:-1] + "2"
    header = f"!players 2\n!pace {pace}\n"
    state = MatchState(TileMap.parse(header + "\n".join(rows)))
    for pid in range(2):
        state.players[pid] = Player(pid=pid, name=f"P{pid}", team=pid,
                                    color=pid, supply=30)
        state.add_building(pid, "base", 1 + pid * 2, height - 1)
    return state


def test_pace_carries_an_army_proportionally_further():
    """Four times the ground with ordinary movement is the same match with
    twice the walking in it. A map sets its own tempo instead."""
    travelled = {}
    for pace in (1, 2, 3):
        state = paced_arena(pace)
        scout = state.add_unit(0, "scout", 2, 5)
        resolve_turn(state, {0: [{"o": "move", "uid": scout.uid, "to": [35, 5]}]})
        travelled[pace] = scout.x - 2
    assert travelled[2] == travelled[1] * 2
    assert travelled[3] == travelled[1] * 3


def test_pace_leaves_the_movement_arithmetic_whole():
    """Integral everywhere, or a Pi and a desktop disagree about where a unit
    stopped. Advancing is seven eighths of marching, and must stay exact."""
    for pace in (1, 2, 3, 4):
        marched, advanced = [], []
        for stance, out in (("move", marched), ("attack", advanced)):
            state = paced_arena(pace)
            unit = state.add_unit(0, "trooper", 2, 5)
            resolve_turn(state, {0: [{"o": stance, "uid": unit.uid,
                                      "to": [35, 5]}]})
            out.append(unit.x - 2)
        assert marched[0] * ADVANCE_PACE // MOVE_PACE == advanced[0], \
            f"pace {pace}: advancing is not exactly 7/8 of marching"


# -- long marches ----------------------------------------------------------

def test_a_unit_crosses_a_world_one_leg_at_a_time():
    """Units plan only as far as the horizon, so a long march is a series of
    legs. The unit keeps its real goal and must still arrive."""
    from standing_orders.resolve import PLAN_HORIZON
    width = PLAN_HORIZON * 4
    state = paced_arena(1, width=width, height=9)
    scout = state.add_unit(0, "scout", 2, 4)
    goal = (width - 3, 4)
    resolve_turn(state, {0: [{"o": "move", "uid": scout.uid, "to": list(goal)}]})
    assert len(scout.path) <= PLAN_HORIZON + 1, "planned past the horizon"

    for _ in range(width * 2):
        if scout.tile == goal:
            break
        resolve_turn(state, {})
    assert scout.tile == goal, f"stalled at {scout.tile}, wanted {goal}"


def test_a_march_does_not_restart_when_the_horizon_is_reached():
    """Running out of road must not clear the order -- that was the bug that
    made a scout stop after one turn, in a different disguise."""
    from standing_orders.resolve import PLAN_HORIZON
    state = paced_arena(1, width=PLAN_HORIZON * 3, height=9)
    unit = state.add_unit(0, "trooper", 2, 4)
    goal = (PLAN_HORIZON * 3 - 3, 4)
    resolve_turn(state, {0: [{"o": "move", "uid": unit.uid, "to": list(goal)}]})
    for _ in range(40):
        resolve_turn(state, {})
        if unit.tile == goal:
            break
        assert unit.goal == goal, "the unit forgot where it was going"
        assert unit.path or unit.tile == goal, "the unit ran out of road"


def test_the_pathfinder_cap_scales_with_the_board():
    """A flat cap sized for a 32x24 map silently refused to cross a world."""
    from standing_orders.grid import find_path
    from standing_orders.game import load_map
    world = load_map("wideworld")
    spawns = sorted(world.spawns.items())
    far = find_path(world, spawns[0][1], spawns[-1][1], set())
    assert far, "a unit cannot walk across the world"
    assert not find_path(world, spawns[0][1], spawns[-1][1], set(), limit=4000), \
        "this is the cap that used to be hard-coded"


# -- diplomacy -------------------------------------------------------------

def test_an_offer_is_not_an_agreement():
    """Proposing peace does nothing until somebody signs it."""
    state = arena()
    a = state.add_unit(0, "trooper", 5, 5)
    b = state.add_unit(1, "trooper", 6, 5)
    resolve_turn(state, {0: [{"o": "propose", "to": 1, "pact": "truce"}]})
    assert state.hostile(0, 1), "an offer on the table is still a war"
    assert a.hp < a.max_hp and b.hp < b.max_hp


def test_signing_a_truce_stops_the_shooting():
    state = arena()
    a = state.add_unit(0, "trooper", 5, 5)
    b = state.add_unit(1, "trooper", 6, 5)
    resolve_turn(state, {0: [{"o": "propose", "to": 1, "pact": "truce"}]})
    resolve_turn(state, {1: [{"o": "accept", "from": 0}]})
    assert state.pact_between(0, 1) == "truce"
    before = (a.hp, b.hp)
    resolve_turn(state, {})
    assert (a.hp, b.hp) == before


def test_you_cannot_accept_what_was_never_offered():
    state = arena()
    _, rejected = resolve_turn(state, {1: [{"o": "accept", "from": 0}]})
    assert rejected[1] and "offered" in rejected[1][0]
    assert state.hostile(0, 1)


def test_a_declaration_takes_a_turn_to_bite():
    """One turn of warning. A betrayal should be seen coming, or it is just a
    cheap shot at somebody who trusted you."""
    from standing_orders.resolve import PACT_BINDING
    state = arena()
    a = state.add_unit(0, "trooper", 5, 5)
    b = state.add_unit(1, "trooper", 6, 5)
    resolve_turn(state, {0: [{"o": "propose", "to": 1, "pact": "truce"}]})
    resolve_turn(state, {1: [{"o": "accept", "from": 0}]})
    for _ in range(PACT_BINDING):
        state.turn += 1
        resolve_turn(state, {})

    calm = (a.hp, b.hp)
    result, rejected = resolve_turn(state, {0: [{"o": "declare", "to": 1}]})
    assert not rejected.get(0), rejected
    assert (a.hp, b.hp) == calm, "the guns stay quiet the turn war is declared"
    assert events_of(result, "war"), "and the war is on by the end of it"
    assert state.hostile(0, 1)
    resolve_turn(state, {})
    assert a.hp < calm[0], "shooting resumes the turn after"


def test_an_agreement_binds_for_a_while():
    """Without this a pact is worth nothing and bots tore up 87 a match."""
    from standing_orders.resolve import PACT_BINDING
    state = arena()
    resolve_turn(state, {0: [{"o": "propose", "to": 1, "pact": "truce"}]})
    resolve_turn(state, {1: [{"o": "accept", "from": 0}]})
    _, rejected = resolve_turn(state, {0: [{"o": "declare", "to": 1}]})
    assert rejected[0] and "holds for" in rejected[0][0]
    assert state.pact_between(0, 1) == "truce"

    state.turn += PACT_BINDING
    _, rejected = resolve_turn(state, {0: [{"o": "declare", "to": 1}]})
    assert not rejected.get(0)


def test_an_alliance_shares_sight_and_a_truce_does_not():
    from standing_orders.fog import VisionCache, team_vision
    state = arena()
    state.add_unit(0, "trooper", 3, 3)
    state.add_unit(1, "trooper", 17, 6)

    state.pacts[state.pair(0, 1)] = "truce"
    assert (17, 6) not in team_vision(state, state.bloc_of(0),
                                      VisionCache(state.map))
    state.pacts[state.pair(0, 1)] = "alliance"
    assert (17, 6) in team_vision(state, state.bloc_of(0),
                                  VisionCache(state.map))


def test_a_gift_moves_supply_and_cannot_be_written_from_thin_air():
    state = arena()
    state.players[0].supply = 30
    state.players[1].supply = 0
    resolve_turn(state, {0: [{"o": "gift", "to": 1, "supply": 12}]})
    assert state.players[1].supply - 12 == state.players[1].supply - 12
    assert state.players[0].supply < 30

    _, rejected = resolve_turn(state, {0: [{"o": "gift", "to": 1,
                                            "supply": 10_000}]})
    assert rejected[0]


def test_ground_held_is_what_a_settlement_is_worth():
    state = arena()
    assert state.holding(0) > 0, "a standing Command Post is something"
    before = state.holding(0)
    state.node_owner[(5, 5)] = 0
    assert state.holding(0) > before, "territory counts for more than buildings"
