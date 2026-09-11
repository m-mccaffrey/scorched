import random

from standing_orders.ai import SKILLS, BotBrain
from standing_orders.game import (PHASE_ORDERS, PHASE_OVER, PHASE_RESOLVE,
                                  Match, Settings, available_maps, load_map)
from standing_orders.units import UNIT, promotion_cost


def started(players=2, **kwargs):
    match = Match(Settings(**kwargs))
    for i in range(players):
        match.add_player(f"P{i}")
    match.start_match()
    return match


def test_settings_are_clamped_and_roundtrip():
    settings = Settings(start_supply=99999, order_time=-4).clamp()
    assert settings.start_supply <= 200 and settings.order_time >= 0
    assert Settings.from_wire(settings.to_wire()).to_wire() == settings.to_wire()


def test_match_setup_gives_everyone_a_base_and_an_opening_force():
    match = started(2)
    for player in match.state.players.values():
        assert match.state.has_base(player.pid)
        assert len(match.state.units_of(player.pid)) == 4
        assert player.supply == match.settings.start_supply
    assert match.phase == PHASE_ORDERS


def test_players_start_apart_on_their_own_spawns():
    match = started(2)
    spawns = set(match.state.map.spawns.values())
    bases = {b.tile for b in match.state.buildings.values() if b.code == "base"}
    assert bases <= spawns


def test_free_for_all_gives_everyone_their_own_team():
    match = Match(Settings(teams=False))
    for i in range(4):
        match.add_player(f"P{i}")
    assert len({p.team for p in match.state.players.values()}) == 4


def test_teams_split_four_players_two_and_two():
    match = Match(Settings(teams=True))
    for i in range(4):
        match.add_player(f"P{i}")
    teams = {}
    for player in match.state.players.values():
        teams.setdefault(player.team, []).append(player.pid)
    assert sorted(len(v) for v in teams.values()) == [2, 2]
    # Team mates must be spawn pairs 1&3 and 2&4, which every shipped team map
    # places on opposite sides.
    assert sorted(teams[0]) == [0, 2] and sorted(teams[1]) == [1, 3]


def test_teams_fall_back_to_free_for_all_when_not_four_players():
    match = Match(Settings(teams=True))
    for i in range(3):
        match.add_player(f"P{i}")
    assert len({p.team for p in match.state.players.values()}) == 3


def test_allies_are_not_enemies():
    match = Match(Settings(teams=True))
    for i in range(4):
        match.add_player(f"P{i}")
    match.start_match()
    assert match.state.allied(0, 2) and not match.state.allied(0, 1)


def test_submitting_orders_marks_you_ready():
    match = started(2)
    assert not match.everyone_ready()
    match.submit(0, [])
    match.submit(1, [])
    assert match.everyone_ready()


def test_resubmitting_replaces_the_previous_plan():
    match = started(2)
    unit = match.state.units_of(0)[0]
    match.submit(0, [{"o": "move", "uid": unit.uid, "to": [5, 5]}])
    match.submit(0, [])
    assert match.pending[0] == []


def test_unready_takes_you_back_out():
    match = started(2)
    match.submit(0, [])
    match.unready(0)
    assert not match.state.players[0].ready


def test_resolve_produces_one_timeline_per_player():
    match = started(2)
    match.submit(0, [])
    match.submit(1, [])
    timelines = match.resolve()
    assert set(timelines) == {0, 1}
    for payload in timelines.values():
        assert "events" in payload and "state" in payload
        assert payload["seq"] == 1
    assert match.phase == PHASE_RESOLVE


def test_begin_orders_advances_the_turn():
    match = started(2)
    match.submit(0, []); match.submit(1, [])
    match.resolve()
    assert match.begin_orders() is True
    assert match.phase == PHASE_ORDERS
    assert match.state.turn == 2


def test_match_ends_when_one_team_remains():
    match = started(2)
    for building in list(match.state.buildings.values()):
        if building.owner == 1:
            building.hp = 0
    match.state.prune()
    match.submit(0, []); match.submit(1, [])
    match.resolve()
    assert match.begin_orders() is False
    assert match.phase == PHASE_OVER
    assert match.winner_team == 0


def test_rejections_go_only_to_their_author():
    match = started(2)
    theirs = match.state.units_of(1)[0]
    match.submit(0, [{"o": "move", "uid": theirs.uid, "to": [3, 3]}])
    match.submit(1, [])
    timelines = match.resolve()
    assert timelines[0]["rejected"]
    assert not timelines[1]["rejected"]


def test_status_and_lobby_wire_are_json_safe():
    import json
    match = started(2)
    json.dumps(match.status_wire())
    json.dumps(match.lobby_wire())
    json.dumps(match.standings())
    match.submit(0, []); match.submit(1, [])
    json.dumps(match.resolve())


def test_unknown_map_falls_back_rather_than_crashing():
    assert load_map("does-not-exist") is not None


def test_every_shipped_map_can_host_a_match():
    for name in available_maps():
        tilemap = load_map(name)
        match = Match(Settings(map_name=name))
        for i in range(len(tilemap.spawns)):
            match.add_player(f"P{i}")
        match.start_match()
        assert len(match.state.buildings) == len(tilemap.spawns)


# -- bots ------------------------------------------------------------------

def test_a_match_opens_with_engineers():
    """The economy cannot start without them, so nobody should have to build
    one before they can do anything at all."""
    match = started(2)
    for player in match.state.players.values():
        workers = [u for u in match.state.units_of(player.pid)
                   if UNIT[u.code].builder]
        assert len(workers) >= 2


def test_every_skill_produces_orders_the_rules_accept():
    for skill in SKILLS:
        match = started(2)
        brain = BotBrain(skill, random.Random(1))
        for pid in (0, 1):
            match.submit(pid, brain.plan(match, match.state.players[pid]))
        timelines = match.resolve()
        assert not timelines[0]["rejected"], timelines[0]["rejected"]


def test_unknown_skill_degrades_to_moderate():
    assert BotBrain("wizard").skill == "moderate"


def test_skill_covers_economy_as_well_as_fighting():
    """Difficulty that only changed how a bot fought stopped meaning anything
    once the economy existed."""
    from standing_orders.ai import SKILLS
    novice, veteran = SKILLS["novice"], SKILLS["veteran"]
    assert veteran.workers > novice.workers
    assert veteran.barracks > novice.barracks
    assert veteran.researches and not novice.researches
    assert veteran.expands and not novice.expands


def test_bots_respect_the_army_cap():
    match = started(2)
    match.state.players[0].supply = 999
    for i in range(match.state.army_cap_of(0)):
        match.state.add_unit(0, "scout", 2 + i % 10, 6)
    orders = BotBrain("veteran", random.Random(2)).plan(match, match.state.players[0])
    assert not [o for o in orders if o["o"] == "train"]


def test_bots_only_act_on_what_they_can_see():
    """A bot must not order an attack on a unit hidden in fog."""
    match = started(2)
    hidden = match.state.units_of(1)[0]
    brain = BotBrain("cyborg", random.Random(3))
    orders = brain.plan(match, match.state.players[0])
    targets = [tuple(o["to"]) for o in orders if o["o"] in ("move", "attack")]
    # The enemy's opening units are across the map and unseen on turn one, so
    # nothing should be aimed precisely at one of them.
    assert hidden.tile not in targets or match.state.units_of(0)[0].tile == hidden.tile


def test_a_bot_match_reaches_a_winner():
    """A mismatched pair, which is what any real game is.

    Two bots of the *same* skill run identical economies and grind for a very
    long time -- symmetric AI does that in any RTS. The interesting property
    is that a difference in skill converts into a win, and quickly.
    """
    match = Match(Settings(map_name="duel"))
    match.add_player("Ada", bot=True, skill="veteran")
    match.add_player("Grace", bot=True, skill="novice")
    match.start_match()
    brains = {p.pid: BotBrain(p.skill, random.Random(p.pid))
              for p in match.state.players.values()}
    for _ in range(200):
        if match.phase == PHASE_OVER:
            break
        for player in list(match.state.players.values()):
            if player.alive:
                match.submit(player.pid, brains[player.pid].plan(match, player))
        match.resolve()
        if not match.begin_orders():
            break
    assert match.phase == PHASE_OVER
    assert match.winner_team == match.state.players[0].team, \
        "the better bot should win"


def test_skill_tiers_are_ordered_on_the_support_game():
    """Novice plays none of it, Moderate the cheap half, the top two all of
    it. The tiers are a ladder, not a set of flags."""
    from standing_orders.ai import SKILLS, SKILL_ORDER
    tiers = [SKILLS[name].supports for name in SKILL_ORDER]
    assert tiers == sorted(tiers)
    assert SKILLS["novice"].supports == 0
    assert SKILLS["cyborg"].supports == max(tiers)


def test_a_bot_never_bombs_more_of_its_own_troops_than_the_enemys():
    """The blast plays no favourites, so the scoring has to."""
    import random as _random
    from standing_orders.ai import BotBrain
    from standing_orders.grid import chebyshev
    from standing_orders.units import AIRSTRIKE_RADIUS

    match = started(2)
    state = match.state
    me = state.players[0]
    me.supply = 500
    state.add_building(0, "airfield", 6, 6, under=0)
    # One enemy in a crowd of our own: bombing it would be a net loss.
    state.add_unit(1, "trooper", 12, 6)
    for offset in (-1, 0, 1):
        state.add_unit(0, "trooper", 11, 6 + offset)

    orders = BotBrain("cyborg", _random.Random(1)).plan(match, me)
    for order in orders:
        if order["o"] != "airstrike":
            continue
        target = tuple(order["to"])
        mine = sum(1 for u in state.units.values()
                   if u.alive and u.owner == 0
                   and chebyshev(u.tile, target) <= AIRSTRIKE_RADIUS)
        theirs = sum(1 for u in state.units.values()
                     if u.alive and u.owner == 1
                     and chebyshev(u.tile, target) <= AIRSTRIKE_RADIUS)
        assert theirs > mine, f"bot bombed {mine} of its own to kill {theirs}"


def test_a_bot_does_not_promise_the_same_supply_twice():
    """Strikes and promotions used to be planned against separate copies of
    the budget, so the server threw one of them out."""
    import random as _random
    from standing_orders.ai import BotBrain
    from standing_orders.units import AIRSTRIKE_COST, UNIT, cost_of_building

    for skill in SKILLS:
        match = started(2)
        state = match.state
        me = state.players[0]
        me.supply = 60
        state.add_building(0, "airfield", 6, 6, under=0)
        state.add_building(0, "medic", 7, 6, under=0)
        for index in range(6):
            unit = state.add_unit(0, "trooper", 9 + index, 8)
            unit.blooded = True
            unit.hp = 6
        for index in range(3):
            state.add_unit(1, "trooper", 12 + index, 9)

        spent = 0
        for order in BotBrain(skill, _random.Random(2)).plan(match, me):
            if order["o"] == "airstrike":
                spent += AIRSTRIKE_COST
            elif order["o"] == "promote":
                spent += promotion_cost(state.units[order["uid"]].rank)
            elif order["o"] == "train":
                spent += UNIT[order["code"]].cost
            elif order["o"] == "build":
                spent += cost_of_building(order["code"], me.research)
        assert spent <= me.supply, f"{skill} overspent: {spent} of {me.supply}"


def test_tuning_candidates_always_describe_a_real_ladder():
    """The search may only propose difficulty tables that mean something.

    Left free, an optimiser scored purely on win rates produced Novice running
    five Engineers to Cyborg's two and a supports column reading 1, 0, 1, 1 --
    every target met, and not a difficulty setting among them. The ladder is
    now ordered by construction rather than by hoping the objective notices,
    so this guards the constraint rather than the outcome.
    """
    import random as _random
    from tools.aiparams import ANCHOR, MONOTONE, TIERS, baseline, profiles
    import standing_orders.ai as ai
    from tools.aiparams import spec

    shipped = dict(ai.SKILLS)
    rng = _random.Random(4)
    for _ in range(50):
        candidate = {key: rng.uniform(low, high)
                     for key, low, high, _whole in spec()}
        built = profiles(candidate)
        for field in MONOTONE:
            rungs = [getattr(built[tier], field) for tier in TIERS]
            assert rungs == sorted(rungs), f"{field} is not a ladder: {rungs}"
        assert built[ANCHOR] == shipped[ANCHOR], "the beginners' bot is pinned"

    assert profiles(baseline()) == shipped, "baseline must round-trip exactly"
