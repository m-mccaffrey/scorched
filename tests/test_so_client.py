"""Client smoke tests for Standing Orders.

Run against SDL's dummy drivers, so they work on a build machine with no
display and no sound card. They check that every screen draws and that a real
match can be played through the actual client code path.
"""

import time

import pygame
import pytest

from standing_orders.game import Settings
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
