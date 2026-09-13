"""Client smoke tests for Standing Orders.

Run against SDL's dummy drivers, so they work on a build machine with no
display and no sound card. They check that every screen draws and that a real
match can be played through the actual client code path.
"""

import time

import pygame
import pytest

from standing_orders.game import Settings
from standing_orders.client.render import SCREEN_H, SCREEN_W
from standing_orders.units import AIRSTRIKE_COST, UNIT, promotion_cost


@pytest.fixture(scope="module")
def app():
    from standing_orders.client.app import App
    instance = App(name="Ada", sound=False)
    yield instance
    instance.disconnect()
    pygame.quit()


def drive(app, seconds, on_tick=None):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        pygame.event.pump()
        app._pump()
        app._update(1 / 60)
        app._draw()
        if on_tick and on_tick():
            return True
        time.sleep(0.005)
    return False


@pytest.mark.parametrize("mode", ["menu", "hostsetup", "browse", "direct"])
def test_front_end_screens_draw(app, mode):
    app.mode = mode
    for _ in range(3):
        app._draw()


def test_a_match_plays_through_the_real_client(app):
    app.host_game(Settings(map_name="duel", order_time=4), bots=1,
                  skill="moderate")
    assert app.conn is not None, app.error
    assert drive(app, 6, lambda: len(app.players) >= 2)
    app.send({"t": "start"})
    assert drive(app, 8, lambda: app.renderer.board is not None and app.view.units)

    turns = {"n": 0}

    def play():
        if app.phase == "orders" and app.replay is None and not app.ready_sent:
            app._select_all_units()
            board = app.renderer.board
            app._issue_move((board.map.width // 2, board.map.height // 2), False)
            app._send_ready()
            turns["n"] += 1
        return turns["n"] >= 3 and app.turn >= 3

    assert drive(app, 60, play), f"match did not progress (turn={app.turn})"
    assert app.view.units
    assert app.view.explored >= app.view.visible
    for mode in ("game", "pause"):
        app.mode = mode
        app._draw()
    app.mode = "game"


def test_selection_only_picks_your_own_units(app):
    if not app.view.units:
        pytest.skip("no live match")
    app.selected.clear()
    for unit in app.view.units.values():
        app._select_at((unit["x"], unit["y"]), additive=True)
    assert all(app.view.units[uid]["owner"] == app.my_pid
               for uid in app.selected if uid in app.view.units)


def test_select_all_takes_the_whole_army(app):
    if not app.view.units:
        pytest.skip("no live match")
    app._select_all_units()
    assert len(app.selected) == len(app.view.mine(app.my_pid))


def test_orders_fan_out_instead_of_stacking_on_one_tile(app):
    """Sending six units to one tile must not give six units the same goal."""
    if len(app.view.mine(app.my_pid)) < 2:
        pytest.skip("not enough units")
    app._select_all_units()
    board = app.renderer.board
    app._issue_move((board.map.width // 2, board.map.height // 2), False)
    goals = [tuple(o["to"]) for o in app.unit_orders.values()]
    assert len(goals) == len(set(goals))


def test_formation_only_uses_passable_tiles(app):
    board = app.renderer.board
    tiles = app._formation((board.map.width // 2, board.map.height // 2), 8, set())
    assert len(tiles) == len(set(tiles))
    assert all(board.map.passable(*t) for t in tiles)


def test_queued_production_is_costed_and_capped(app):
    if not app.view.buildings:
        pytest.skip("no live match")
    app.queued.clear()
    base = next((b for b in app.view.buildings.values()
                 if b["owner"] == app.my_pid and b["code"] == "base"), None)
    if base is None:
        pytest.skip("no base")
    app.selected_building = base["bid"]
    app.view.supply = 500
    before = app._army_size()
    app._queue_train("scout")
    assert app._spent() == UNIT["scout"].cost
    assert app._army_size() == before + 1
    # Fill to the cap and confirm the client refuses rather than sending junk.
    guard = 0
    while app._army_size() < app._army_cap() and guard < 40:
        app._queue_train("scout")
        guard += 1
    count = len(app.queued)
    app._queue_train("scout")
    assert len(app.queued) == count
    app.queued.clear()
    app.selected_building = None


def test_clear_wipes_every_pending_order(app):
    app.unit_orders[999] = {"o": "move", "uid": 999, "to": [1, 1]}
    app.queued.append({"o": "train", "bid": 1, "code": "scout"})
    app._clear_orders()
    assert not app.unit_orders and not app.queued and not app.selected


def test_orders_sent_to_the_server_carry_no_client_only_fields(app):
    """The path is a drawing aid; the server computes its own."""
    if not app.view.mine(app.my_pid):
        pytest.skip("no units")
    app._select_all_units()
    board = app.renderer.board
    app._issue_move((board.map.width // 2, board.map.height // 2), False)
    assert any("path" in o for o in app.unit_orders.values())
    sent = [{k: v for k, v in o.items() if k != "path"}
            for o in app.unit_orders.values()]
    assert all("path" not in o for o in sent)
    assert all(set(o) <= {"o", "uid", "to"} for o in sent)


def test_promotion_is_offered_only_to_units_that_have_earned_it(app):
    if not app.view.mine(app.my_pid):
        pytest.skip("no units")
    app.queued.clear()
    app.view.supply = 500
    unit = app.view.mine(app.my_pid)[0]
    app.selected = {unit["uid"]}

    unit["blooded"] = False
    unit["rank"] = 0
    app._queue_promotions()
    assert not app.queued, "a fresh recruit is not up for promotion"

    unit["blooded"] = True
    app._queue_promotions()
    assert [o["o"] for o in app.queued] == ["promote"]
    assert app._spent() == promotion_cost(0)

    # Asking twice must not buy the same promotion twice.
    app._queue_promotions()
    assert len(app.queued) == 1
    app.queued.clear()
    app.selected.clear()


def test_an_airstrike_needs_an_airfield_and_is_costed(app):
    if not app.view.buildings:
        pytest.skip("no live match")
    app.queued.clear()
    app.view.supply = 500
    base = next((b for b in app.view.buildings.values()
                 if b["owner"] == app.my_pid and b["code"] == "base"), None)
    if base is None:
        pytest.skip("no base")

    app.selected_building = base["bid"]
    app._begin_airstrike()
    assert app.aiming is None, "a Command Post is not an Airfield"

    # Stand one up in the client's view and aim it.
    app.view.buildings[9901] = {"bid": 9901, "owner": app.my_pid,
                                "code": "airfield", "x": base["x"],
                                "y": base["y"], "hp": 45, "under": 0}
    app.selected_building = 9901
    app._begin_airstrike()
    assert app.aiming == 9901
    app._call_airstrike((4, 4))
    assert app.queued == [{"o": "airstrike", "bid": 9901, "to": [4, 4]}]
    assert app._spent() == AIRSTRIKE_COST
    assert app.aiming is None

    # One sortie a turn.
    app._begin_airstrike()
    assert app.aiming is None
    app.queued.clear()
    app.view.buildings.pop(9901, None)
    app.selected_building = None


def test_a_map_that_fits_the_screen_never_scrolls(app):
    """The three original maps must draw exactly as they always did."""
    from standing_orders.client.render import Board
    from standing_orders.game import load_map

    small = Board(load_map("duel"))
    assert not small.scrolls
    assert small.max_cam == (0, 0)
    before = (small.ox, small.oy)
    small.scroll_by(500, 500)
    assert (small.ox, small.oy) == before, "a fitting map cannot be scrolled"


def test_the_camera_stays_inside_a_large_map(app):
    from standing_orders.client.render import BOARD_H, BOARD_W, Board
    from standing_orders.game import load_map

    big = Board(load_map("valley"))
    assert big.scrolls
    big.scroll_by(-9999, -9999)
    assert (big.cam_x, big.cam_y) == (0, 0)
    big.scroll_by(9999, 9999)
    assert (big.cam_x, big.cam_y) == big.max_cam
    # At the far corner the last tile of the map is still on screen.
    assert big.cam_x + BOARD_W >= big.pixel_w
    assert big.cam_y + BOARD_H >= big.pixel_h
    big.centre_on((32, 22))
    assert big.on_screen((32, 22))
    assert not big.on_screen((0, 0))


def test_a_click_on_the_panel_is_not_a_click_on_a_tile(app):
    """With a camera, panel coordinates otherwise map onto real tiles."""
    from standing_orders.client.render import SCREEN_W, Board
    from standing_orders.game import load_map

    big = Board(load_map("valley"))
    big.centre_on((40, 30))
    assert big.to_tile((SCREEN_W - 40, 200)) is None, "that is the command panel"
    assert big.to_tile((SCREEN_W // 4, 100)) is not None


def test_the_minimap_moves_the_camera_and_ignores_clicks_elsewhere(app):
    if app.renderer.board is None:
        pytest.skip("no live match")
    rect = app._minimap_rect()
    assert rect is not None
    assert not app._minimap_click((10, 10)), "a click on the board is not ours"
    assert app._minimap_click(rect.center)


def test_the_minimap_band_is_reserved_in_every_panel_state(app):
    """It is wanted most while something is selected, which is most of the
    time, so it cannot live only in the overview."""
    if not app.view.buildings:
        pytest.skip("no live match")
    rect = app._minimap_rect()
    mine = app.view.mine(app.my_pid)
    base = next(b for b in app.view.buildings.values()
                if b["owner"] == app.my_pid and b["code"] == "base")
    states = [(set(), None), ({u["uid"] for u in mine}, None),
              (set(), base["bid"])]
    for selected, building in states:
        app.selected, app.selected_building = set(selected), building
        app._draw()
        assert app._minimap_rect() == rect, "the minimap moved between states"
    app.selected.clear()
    app.selected_building = None


def test_the_table_offers_only_what_the_relation_allows(app):
    if not app.standings:
        pytest.skip("no live match")
    app.queued.clear()
    app.parley = True
    app._draw()
    actions = {b.action for b in app._buttons if b.action.startswith("dip:")}
    app.parley = False
    assert actions, "the table offered nothing at all"
    assert any(a.startswith("dip:truce") or a.startswith("dip:accept")
               for a in actions)
    assert "dip:armistice" in actions


def test_diplomatic_orders_replace_rather_than_stack(app):
    """Offering a truce and then an alliance to the same commander should send
    the second, not both."""
    if not app.standings:
        pytest.skip("no live match")
    app.queued.clear()
    app.view.supply = 500
    other = next(r["pid"] for r in app.standings if r["pid"] != app.my_pid)
    app._queue_diplomacy(["truce", other])
    app._queue_diplomacy(["alliance", other])
    toward = [o for o in app.queued if o.get("to") == other]
    assert len(toward) == 1 and toward[0]["pact"] == "alliance"
    app.queued.clear()


def test_a_gift_is_costed_against_the_turn_budget(app):
    from standing_orders.client.app import GIFT_SIZE
    if not app.standings:
        pytest.skip("no live match")
    app.queued.clear()
    app.view.supply = GIFT_SIZE + 1
    other = next(r["pid"] for r in app.standings if r["pid"] != app.my_pid)
    app._queue_diplomacy(["gift", other])
    assert app._spent() == GIFT_SIZE
    app._queue_diplomacy(["gift", other])
    assert len(app.queued) == 1, "a second gift you cannot afford is refused"
    app.queued.clear()


def test_the_client_pays_for_the_window_not_for_the_world():
    """Terrain used to be composited whole -- 45MB and 435ms before the first
    frame on a 224x128 world, and 257MB on the largest one. It is painted in
    patches around the camera now, so the bill is the same at every size."""
    from standing_orders.client.render import PATCH_CACHE, Renderer
    from standing_orders.game import load_map

    held = {}
    for name in ("duel", "reach"):
        renderer = Renderer()
        renderer.begin_match(load_map(name))
        assert not renderer._patches, \
            f"{name}: begin_match painted terrain nobody has looked at yet"
        surface = pygame.Surface((SCREEN_W, SCREEN_H))
        for _ in range(60):
            renderer.board.scroll_by(37, 23)
            renderer.draw_terrain(surface)
        held[name] = len(renderer._patches)

    assert held["reach"] <= PATCH_CACHE, "the patch cache is not capped"
    assert held["reach"] <= held["duel"] + PATCH_CACHE


def test_the_shroud_follows_the_camera():
    """The shroud is window-sized now, so moving the camera makes it stale in
    exactly the way changing what you can see does."""
    from standing_orders.client.render import Renderer
    from standing_orders.game import load_map

    renderer = Renderer()
    renderer.begin_match(load_map("reach"))
    visible = {(x, y) for x in range(20, 40) for y in range(20, 40)}
    renderer.set_fog(visible, visible)
    first = renderer._fog_key
    renderer.board.scroll_by(400, 300)
    renderer.set_fog(visible, visible)
    assert renderer._fog_key != first, "the shroud did not notice the camera"


def _tip_over(app, tile):
    """What the board tooltip says about ``tile``, as plain strings."""
    app._tips = []
    x, y = app.renderer.board.to_screen(tile)
    app.mouse = (x + 1, y + 1)
    app._collect_board_tip()
    return [text for rect, lines in app._tips for text, _, _ in lines]


def test_the_tooltip_does_not_read_through_the_fog(app):
    """The client is handed the whole terrain map so it can draw ground under
    the shroud, which turned this tooltip into a scouting instrument: run the
    pointer across the black and it named the terrain and every resource node
    behind it, owner included. Fog you can read is not fog."""
    from standing_orders.client.render import Renderer
    from standing_orders.client.view import WorldView
    from standing_orders.game import load_map

    app.renderer = Renderer()
    app.renderer.begin_match(load_map("basin"))
    app.view = WorldView()
    app.my_pid = 0
    app.players = {0: {"name": "Ada", "team": 0}, 1: {"name": "Bo", "team": 1}}
    app.colors = {0: 0, 1: 1}

    node = sorted(app.renderer.board.map.nodes)[0]
    app.view.node_owner[node] = 1

    dark = _tip_over(app, node)
    assert dark == ["Unscouted"], f"the fog leaked: {dark}"

    # Scouted once, but not in sight now: the ground is yours to remember,
    # who holds the node this turn is not.
    app.view.explored.add(node)
    remembered = _tip_over(app, node)
    assert any("Out of sight" in line for line in remembered), remembered
    assert not any("Bo" in line for line in remembered), remembered

    app.view.visible.add(node)
    live = _tip_over(app, node)
    assert any("Held by Bo" in line for line in live), live


def test_node_colours_do_not_leak_through_the_fog(app):
    """Same leak by another route: explored ground stays drawn under a veil,
    so a crate painted in its current holder's colour announced a capture from
    across the map."""
    from standing_orders.client.render import C_NODE, Renderer, team_color
    from standing_orders.game import load_map

    renderer = Renderer()
    renderer.begin_match(load_map("basin"))
    node = next(t for t in sorted(renderer.board.map.nodes)
                if renderer.board.on_screen(t))
    surface = pygame.Surface((SCREEN_W, SCREEN_H))

    def crate_colour(visible, mine):
        surface.fill((0, 0, 0))
        renderer.draw_nodes(surface, {node: 1}, {1: 1}, visible, mine)
        x, y = renderer.board.to_screen(node)
        from standing_orders.client.render import TILE
        return {surface.get_at((x + dx, y + dy))[:3]
                for dx in range(TILE) for dy in range(TILE)}

    theirs = team_color(1)[:3]
    assert theirs in crate_colour({node}, 0), "a node in sight lost its owner"
    assert theirs not in crate_colour(set(), 0), "an unseen node named its owner"
    assert theirs in crate_colour(set(), 1), "our own node went neutral"
    assert tuple(C_NODE[:3]) in crate_colour(set(), 0)


def test_tooltips_can_be_turned_off(app, monkeypatch):
    """I toggles them. Tooltips follow the pointer everywhere, which is right
    while you are learning the sprites and a curtain across the board after."""
    from standing_orders.client import app as app_module

    drawn = []
    monkeypatch.setattr(app_module, "draw_tooltip",
                        lambda *a, **k: drawn.append(a))

    # _draw rebuilds _tips through the painter for the current mode, so stand
    # a painter in that parks one tip over the whole screen. Anything the real
    # _draw does with the flag after that is what is under test.
    was, app.mode = app.mode, "probe"
    monkeypatch.setattr(app, "_draw_menu", lambda: app._tips.append(
        (pygame.Rect(0, 0, SCREEN_W, SCREEN_H), [("x", None, 14)])),
        raising=False)

    def hover():
        app.mouse = (10, 10)
        app._draw()

    assert app.tips_on
    hover()
    assert drawn, "a tooltip was not drawn with tooltips on"

    press = pygame.event.Event(pygame.KEYDOWN, key=pygame.K_i)
    app._game_key(press)
    assert not app.tips_on
    drawn.clear()
    hover()
    assert not drawn, "a tooltip was drawn with tooltips off"

    app._game_key(press)
    assert app.tips_on
    app.mode = was


def test_the_pointer_at_the_edge_of_the_board_does_not_pan(app):
    """Edge-pan scrolled the board out from under every trip down to the build
    menu, because reaching for a button crosses the bottom edge of the map."""
    from standing_orders.client.render import Board, Renderer
    from standing_orders.game import load_map

    app.renderer = Renderer()
    app.renderer.begin_match(load_map("reach"))
    board = app.renderer.board
    board.cam_x = board.cam_y = 0
    for spot in ((Board.VIEW.x + 1, Board.VIEW.centery),
                 (Board.VIEW.right - 1, Board.VIEW.centery),
                 (Board.VIEW.centerx, Board.VIEW.bottom - 1)):
        app.mouse = spot
        app._scroll_camera(0.5)
    assert (board.cam_x, board.cam_y) == (0, 0), "the pointer moved the camera"
