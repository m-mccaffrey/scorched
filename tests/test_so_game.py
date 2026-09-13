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


def test_a_war_ends_when_every_commander_calls_it():
    """The armistice: not the same question as "is anybody shooting".

    Two neighbours going quiet does not end a four-way war, and conflating
    the two had bots shaking hands on turn twenty with nobody having played.
    """
    match = started(4)
    state = match.state
    living = [p.pid for p in state.players.values() if p.alive]

    for pid in living[:-1]:
        match.submit(pid, [{"o": "armistice"}])
    match.submit(living[-1], [])
    match.resolve()
    assert match.begin_orders(), "a war does not end while somebody wants it"

    match.submit(living[-1], [{"o": "armistice"}])
    for pid in living[:-1]:
        match.submit(pid, [])
    match.resolve()
    assert not match.begin_orders()
    assert match.phase == PHASE_OVER and match.peace


def test_declaring_war_withdraws_your_call_for_peace():
    match = started(2)
    a, b = sorted(match.state.players)
    match.submit(a, [{"o": "armistice"}])
    match.submit(b, [])
    match.resolve()
    match.begin_orders()
    assert a in match.state.armistice

    match.submit(a, [{"o": "propose", "to": b, "pact": "truce"}])
    match.submit(b, [])
    match.resolve(); match.begin_orders()
    match.submit(b, [{"o": "accept", "from": a}])
    match.submit(a, [])
    match.resolve(); match.begin_orders()
    match.state.turn += 50
    match.submit(a, [{"o": "declare", "to": b}])
    match.submit(b, [])
    match.resolve()
    assert a not in match.state.armistice, \
        "you cannot ask for peace and declare war in the same breath"


def test_an_armistice_is_settled_on_ground_held():
    """Peace is a settlement. Whoever holds most has won the war without
    finishing it -- otherwise stopping is free and every bot works that out."""
    match = started(2)
    state = match.state
    a, b = sorted(state.players)
    for tile in list(state.map.nodes)[:3]:
        state.node_owner[tile] = b
    for pid in (a, b):
        match.submit(pid, [{"o": "armistice"}])
    match.resolve()
    match.begin_orders()
    assert match.peace
    assert match.winner_team == state.bloc_of(b), \
        "the commander holding the ground carries the settlement"


def test_starting_teams_become_signed_alliances():
    match = started(4, teams=True)
    state = match.state
    for player in state.players.values():
        for other in state.players.values():
            if player.pid == other.pid:
                continue
            same = player.team == other.team
            assert state.allied(player.pid, other.pid) is same
            assert state.hostile(player.pid, other.pid) is not same


def test_every_skill_files_diplomatic_orders_the_rules_accept():
    """Bots negotiate. What they must never do is propose the illegal."""
    for skill in SKILLS:
        match = started(4)
        match.state.turn = 60           # past every "not yet" gate
        brain = BotBrain(skill, random.Random(2))
        for pid in sorted(match.state.players):
            match.submit(pid, brain.plan(match, match.state.players[pid]))
        timelines = match.resolve()
        for pid, timeline in timelines.items():
            bad = [r for r in timeline["rejected"]
                   if any(word in r for word in
                          ("offered", "already", "no such commander", "holds for"))]
            assert not bad, f"{skill}: {bad}"


def test_a_long_war_starts_looking_for_terms():
    """A web of half-signed truces can reach a state that is neither winnable
    nor endable: two commanders grinding a third they cannot finish, nobody
    losing badly enough to sue, and the one who could be offered terms a
    Novice who accepts anything but never asks. Five matches in six ran to the
    turn limit. Wars end."""
    from standing_orders.ai import WAR_WEARY
    import random as _random

    # A four-spawn map, or this is not the situation the docstring describes:
    # the default map seats two, so started(4) leaves half the roster dead on
    # arrival and the "2v2" was a duel with spectators.
    match = started(4, teams=True, map_name="crossroads")
    state = match.state
    assert sum(1 for p in state.players.values() if p.alive) == 4
    brain = BotBrain("veteran", _random.Random(1))

    state.turn = WAR_WEARY - 1
    early = brain._diplomacy(match, state.players[0])
    state.turn = WAR_WEARY
    weary = brain._diplomacy(match, state.players[0])

    offers = [o for o in weary if o["o"] == "propose"]
    assert offers, "a bot in a long war never asks for terms"
    assert len(offers) > len([o for o in early if o["o"] == "propose"])


def test_the_lobby_army_cap_governs_what_a_player_can_field():
    """The slider is worthless if the match ignores it: the ceiling has to
    reach MatchState, and it has to reach the *new* board built at kickoff,
    not the throwaway one the lobby was sitting on."""
    from standing_orders.units import ARMY_CAP_FLOOR, ARMY_CAP_ROOF

    for asked in (ARMY_CAP_FLOOR, 60, ARMY_CAP_ROOF):
        match = started(2, army_cap=asked)
        assert match.state.army_ceiling == asked
        # Enough depots to blow past any ceiling, so the cap *is* the ceiling.
        from standing_orders.units import army_cap
        assert army_cap(["depot"] * 40, match.state.army_ceiling) == asked

    assert Settings(army_cap=1).clamp().army_cap == ARMY_CAP_FLOOR
    assert Settings(army_cap=999).clamp().army_cap == ARMY_CAP_ROOF
    assert Settings().army_cap == 60


def test_a_bot_never_bricks_itself_in():
    """The bug behind "the AI doesn't seem to want to win".

    Sites were picked by closeness to the Command Post and nothing else, and
    movement in this game is four-directional, so twenty structures around a
    base is a wall. Measured on a duel at turn 150: a Veteran with eleven
    Depots, six Barracks and an army of sixty had *every one* of its thirty-five
    fighters with no route to the enemy, ordered to attack every turn, penned
    inside its own yard -- and the enemy Command Post finished the match at full
    health.
    """
    from standing_orders.ai import BotBrain
    from standing_orders.grid import find_path
    from standing_orders.resolve import static_obstacles

    match = started(2, map_name="basin")
    state = match.state
    brain = BotBrain("veteran", random.Random(5))
    base = next(b for b in state.buildings_of(0) if b.code == "base")
    far = max(state.map.nodes, key=lambda t: abs(t[0] - base.x) + abs(t[1] - base.y))

    # Build out the yard the way a rich bot does: twenty structures, each on
    # the site the bot itself would choose next.
    placed = 0
    for _ in range(20):
        site = brain._site_near(state, base.tile, radius=4)
        if site is None:
            break
        state.add_building(0, "depot", site[0], site[1])
        placed += 1
        walls = static_obstacles(state.occupancy())
        assert find_path(state.map, base.tile, far, walls, partial=False), \
            f"the yard sealed itself after {placed} buildings at {site}"
    assert placed >= 12, f"only found room for {placed} buildings"


def test_a_yard_full_of_engineers_is_not_a_sealed_yard():
    """The escape test counts structures and terrain, never bodies. Counting
    units meant a yard with somebody standing in each gap read as sealed for
    good: every candidate site looked equally hopeless, the bot answered
    "nowhere to build" for the rest of the match, and it banked 311 supply at an
    army cap it could have been raising."""
    from standing_orders.ai import BotBrain

    match = started(2, map_name="basin")
    state = match.state
    base = next(b for b in state.buildings_of(0) if b.code == "base")
    for dx in range(-2, 3):
        for dy in range(-2, 3):
            tile = (base.x + dx, base.y + dy)
            if tile != base.tile and state.map.passable(*tile):
                state.add_unit(0, "trooper", tile[0], tile[1])
    site = BotBrain("veteran", random.Random(6))._site_near(state, base.tile,
                                                            radius=4)
    assert site is not None, "a crowd around the base is not a wall"


def test_even_a_novice_builds_something():
    """A Novice builds no Depots by definition, and the first Barracks was
    gated on having one -- so it built *nothing at all*, measured at turn 100
    with one Command Post, an army of twelve and 305 supply banked. A
    beginners' opponent should be beatable, not inert."""
    from standing_orders.ai import BotBrain

    match = started(2, map_name="basin")
    state = match.state
    state.players[0].supply = 60
    orders = BotBrain("novice", random.Random(7)).plan(match, state.players[0])
    assert [o for o in orders if o["o"] == "build" and o["code"] == "barracks"]


def test_two_rules_in_one_turn_cannot_claim_the_same_square():
    """Every rule asked for "the closest free tile", so a Depot and a Barracks
    were ordered onto one square, both charged for, and whichever Engineer
    arrived second had its job refunded -- a bot that thought it was raising
    three buildings raised one and walked two Engineers nowhere."""
    from standing_orders.ai import BotBrain

    match = started(2, map_name="basin")
    state = match.state
    me = state.players[0]
    me.supply = 400
    for i in range(6):
        state.add_unit(0, "worker", 6 + i, 6)
    brain = BotBrain("cyborg", random.Random(8))
    orders = brain.plan(match, me)
    sites = [tuple(o["to"]) for o in orders if o["o"] == "build"]
    assert len(sites) == len(set(sites)), f"two builds on one tile: {sites}"


def test_nobody_trucks_with_their_last_enemy():
    """A truce with your only remaining enemy is not diplomacy, it is quitting:
    nothing else can happen afterwards and the match ends in an armistice with
    one side clearly ahead. Half of all two-player matches ended that way, and
    several on the exact turn peace became legal."""
    from standing_orders.ai import DIPLOMACY_EARLIEST, BotBrain

    match = started(2, map_name="duel")
    state = match.state
    state.turn = DIPLOMACY_EARLIEST + 5
    # Make player 0 hopelessly behind, which is exactly when it used to fold.
    for _ in range(12):
        state.add_unit(1, "bruiser", 20, 10)
    brain = BotBrain("veteran", random.Random(9))
    orders = brain._diplomacy(match, state.players[0])
    assert not [o for o in orders if o["o"] in ("propose", "accept")], orders


def test_a_bot_that_is_winning_does_not_sue_for_peace():
    """Weariness used to fire on the turn clock alone, which handed won wars
    away from in front: a bot three times its enemy's size and marching on
    their Command Post proposed terms, the loser accepted gratefully, and the
    win went down as a draw. A war you are getting somewhere in is not a war
    that is going nowhere."""
    from standing_orders.ai import STALE_TURNS, WAR_WEARY, BotBrain

    match = started(4, teams=True, map_name="crossroads")
    state = match.state
    state.turn = WAR_WEARY
    for _ in range(14):                    # player 0 is streets ahead
        state.add_unit(0, "bruiser", 4, 4)

    winning = BotBrain("veteran", random.Random(10))
    assert not [o for o in winning._diplomacy(match, state.players[0])
                if o["o"] == "propose"], "a bot handed away a war it was winning"

    # ...but being ahead is not the same as getting anywhere. A commander whose
    # holding has not grown in STALE_TURNS turns is stuck, however big it is,
    # and a stuck war is what the armistice machinery is for.
    stuck = BotBrain("veteran", random.Random(11))
    stuck._diplomacy(match, state.players[0])          # records the high-water
    state.turn = WAR_WEARY + STALE_TURNS
    assert [o for o in stuck._diplomacy(match, state.players[0])
            if o["o"] == "propose"], "a stalled war never gets talked about"


def test_production_lines_scale_with_the_army_cap():
    """One Barracks is a queue, not a factory: a cap of 36 reinforced out of a
    single door at one unit every other turn, which is how a bot ends a match
    with 280 supply banked against a human running twenty Barracks."""
    from standing_orders.ai import BotBrain

    match = started(2, map_name="basin")
    state = match.state
    brain = BotBrain("cyborg", random.Random(12))
    small = brain._lines_wanted(state, state.players[0])
    for i in range(8):                     # depots raise the cap
        state.add_building(0, "depot", 6 + i, 14)
    big = brain._lines_wanted(state, state.players[0])
    assert big > small, f"{small} -> {big}: production ignores the economy"


def test_the_builder_corps_grows_with_the_treasury():
    """One shovel raises a structure every five turns however rich the bot is,
    which is why bots banked hundreds of supply -- not for want of anything to
    buy, but for want of anybody free to buy it with."""
    from standing_orders.ai import BUILDERS_MAX, BotBrain

    match = started(2, map_name="basin")
    state = match.state
    me = state.players[0]
    crew = [state.add_unit(0, "worker", 6 + i, 6) for i in range(BUILDERS_MAX + 2)]
    brain = BotBrain("veteran", random.Random(13))
    receivers = state.receivers_of(0)

    me.supply = 0
    assert not brain._builder_corps(state, me, crew, receivers)
    me.supply = 20
    poor = len(brain._builder_corps(state, me, crew, receivers))
    me.supply = 400
    rich = len(brain._builder_corps(state, me, crew, receivers))
    assert 0 < poor < rich <= BUILDERS_MAX
    assert rich < len(crew), "somebody has to keep the lights on"
