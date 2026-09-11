"""Fog-of-war tests.

Leaking information here would be invisible in play but would quietly ruin the
game, so these check the negative case hard: what a player must *not* receive.
"""

from standing_orders.fog import (VisionCache, filter_events, team_vision,
                                 visible_state)
from standing_orders.game import Match, Settings
from standing_orders.grid import TileMap
from standing_orders.resolve import resolve_turn
from standing_orders.state import MatchState, Player


def arena(width=24, height=10):
    rows = ["." * width for _ in range(height)]
    rows[0] = "1" + rows[0][1:]
    rows[-1] = rows[-1][:-1] + "2"
    state = MatchState(TileMap.parse("!players 2\n" + "\n".join(rows)))
    for pid in (0, 1):
        state.players[pid] = Player(pid=pid, name=f"P{pid}", team=pid, color=pid)
    return state


def test_vision_covers_your_own_units_and_nothing_else():
    state = arena()
    state.add_unit(0, "trooper", 3, 3)
    state.add_unit(1, "trooper", 20, 8)
    cache = VisionCache(state.map)
    mine = team_vision(state, 0, cache)
    assert (3, 3) in mine
    assert (20, 8) not in mine


def test_allies_share_vision():
    state = arena()
    state.players[1].team = 0
    state.add_unit(0, "trooper", 3, 3)
    state.add_unit(1, "trooper", 20, 8)
    seen = team_vision(state, 0, VisionCache(state.map))
    assert (3, 3) in seen and (20, 8) in seen


def test_forest_blocks_vision():
    rows = ["." * 12 for _ in range(5)]
    rows[0] = "1" + rows[0][1:]
    rows[-1] = rows[-1][:-1] + "2"
    rows[2] = ".%%%%%%%%%%."
    state = MatchState(TileMap.parse("!players 2\n" + "\n".join(rows)))
    state.players[0] = Player(pid=0, name="P", team=0)
    state.add_unit(0, "scout", 5, 0)
    seen = team_vision(state, 0, VisionCache(state.map))
    assert (5, 4) not in seen


def test_visible_state_hides_unseen_enemies():
    state = arena()
    state.add_unit(0, "trooper", 2, 2)
    hidden = state.add_unit(1, "trooper", 21, 8)
    vision = team_vision(state, 0, VisionCache(state.map))
    view = visible_state(state, 0, vision)
    uids = {u["uid"] for u in view["units"]}
    assert hidden.uid not in uids
    assert len(uids) == 1


def test_visible_state_shows_enemies_you_can_see():
    state = arena()
    state.add_unit(0, "scout", 5, 5)
    spotted = state.add_unit(1, "trooper", 7, 5)
    vision = team_vision(state, 0, VisionCache(state.map))
    view = visible_state(state, 0, vision)
    assert spotted.uid in {u["uid"] for u in view["units"]}


def test_you_always_see_your_own_actions():
    state = arena()
    mine = state.add_unit(0, "scout", 2, 2)
    result, _ = resolve_turn(state, {0: [{"o": "move", "uid": mine.uid,
                                          "to": [8, 2]}]})
    owner = {("move", mine.uid): 0}
    seen = filter_events(result.events, result.vision[0], 0, 0, owner)
    assert [e for e in seen if e["e"] == "move"]


def test_income_is_private_to_its_owner():
    state = arena()
    state.add_building(0, "base", 2, 2)
    state.add_building(1, "base", 20, 8)
    result, _ = resolve_turn(state, {})
    mine = filter_events(result.events, result.vision[0], 0, 0, {})
    incomes = [e for e in mine if e["e"] == "income"]
    assert incomes and all(e["pid"] == 0 for e in incomes)


def test_eliminations_are_public():
    state = arena()
    doomed = state.add_building(1, "base", 6, 5)
    doomed.hp = 1
    state.add_unit(0, "bruiser", 5, 5)
    result, _ = resolve_turn(state, {})
    for team in (0, 1):
        seen = filter_events(result.events, result.vision[team], team, team, {})
        assert [e for e in seen if e["e"] == "eliminated"]


def test_a_player_never_receives_events_from_the_dark():
    """The whole match, end to end: nothing outside a player's sight reaches
    them, and their own events always do."""
    match = Match(Settings(map_name="duel"))
    match.add_player("Ann")
    match.add_player("Ben")
    match.start_match()
    match.submit(0, [])
    match.submit(1, [])
    timelines = match.resolve()

    cache = VisionCache(match.state.map)
    for pid in (0, 1):
        vision = team_vision(match.state, match.state.team_of(pid), cache)
        for event in timelines[pid]["events"]:
            if event["e"] in ("income", "eliminated"):
                continue
            tiles = [tuple(event[k]) for k in ("at", "to") if k in event]
            if not tiles:
                continue
            actor = match.state.units.get(event.get("uid"))
            if actor is not None and actor.owner == pid:
                continue
            assert any(t in vision for t in tiles), \
                f"player {pid} was told about {event} in the dark"


def test_seeing_an_enemy_does_not_reveal_its_orders():
    """Position is observable; intent is not.

    Unit.to_wire carries the remaining path and stance, which would hand the
    enemy's whole plan to anyone who spotted a single scout.
    """
    state = arena()
    state.add_unit(0, "scout", 6, 5)
    theirs = state.add_unit(1, "trooper", 7, 5)
    theirs.path = [(8, 5), (9, 5), (10, 5)]
    theirs.stance = "attack"
    mine = state.add_unit(0, "trooper", 5, 5)
    mine.path = [(4, 5)]

    vision = team_vision(state, 0, VisionCache(state.map))
    view = visible_state(state, 0, vision)
    by_uid = {u["uid"]: u for u in view["units"]}

    assert theirs.uid in by_uid, "the enemy should be visible"
    assert "path" not in by_uid[theirs.uid]
    assert "stance" not in by_uid[theirs.uid]
    # Your own orders still come back, because the client draws them.
    assert by_uid[mine.uid]["path"] == [[4, 5]]


def test_rank_is_public_but_readiness_for_promotion_is_not():
    """An enemy officer is meant to be a visible target. Whether they are one
    promotion away from being a better one is your own business."""
    state = arena()
    state.add_unit(0, "scout", 6, 5)
    theirs = state.add_unit(1, "trooper", 7, 5)
    theirs.rank = 2
    theirs.blooded = True

    vision = team_vision(state, 0, VisionCache(state.map))
    view = visible_state(state, 0, vision)
    seen = next(u for u in view["units"] if u["uid"] == theirs.uid)
    assert seen["rank"] == 2
    assert "blooded" not in seen


def test_bombing_the_dark_tells_you_nothing_about_what_you_hit():
    """You always know your own plane flew. What it landed on is another
    matter -- the fog keeps its own counsel."""
    from standing_orders.game import Match, Settings
    match = Match(Settings(map_name="duel"))
    match.add_player("A")
    match.add_player("B")
    match.start_match()
    state = match.state
    state.players[0].supply = 500
    field = state.add_building(0, "airfield", 5, 5, under=0)
    far = max(state.buildings.values(), key=lambda b: b.bid if b.owner == 1 else -1)
    victim = state.add_unit(1, "trooper", far.x, far.y - 1)

    match.submit(0, [{"o": "airstrike", "bid": field.bid,
                      "to": [victim.x, victim.y]}])
    match.submit(1, [])
    timelines = match.resolve()

    bomber = [e["e"] for e in timelines[0]["events"]]
    bombed = [e["e"] for e in timelines[1]["events"]]
    assert bomber.count("strike") == 1, "you always know your own plane flew"
    assert "shoot" not in bomber, "but not what it found in the dark"
    assert bombed.count("strike") == 1 and "shoot" in bombed
