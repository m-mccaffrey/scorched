"""Client smoke tests.

They run against SDL's dummy video driver, so they check that every screen can
actually be drawn and that a whole match can be played through the real client
code path -- which is where most regressions would otherwise hide.
"""

import time

import pygame
import pytest

from scorched.game import Settings


@pytest.fixture(scope="module")
def app():
    from scorched.client.app import App
    instance = App(name="Tester", sound=False)
    yield instance
    instance.disconnect()
    pygame.quit()


def drive(app, seconds, on_tick=None):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        pygame.event.pump()
        app._pump_network()
        app._update()
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


def test_full_match_through_the_real_client(app):
    app.host_game(Settings(rounds=1, turn_time=6, buy_time=2), bots=1,
                  skill="expert")
    assert app.conn is not None, app.error

    assert drive(app, 5, lambda: len(app.state.players) >= 2)
    app.send({"t": "start"})

    def play():
        if app.state.my_turn and app.shot is None:
            me = app.state.me
            me["angle"], me["power"] = 48, 520
            app._fire()
        if app.mode == "shop" and (app.state.me or {}).get("buying"):
            app.send({"t": "buydone"})
        return app.mode == "gameover"

    assert drive(app, 90, play), f"match did not finish (mode={app.mode})"
    assert app.state.standings
    assert not app.error

    # Every screen the match passed through must still render.
    for mode in ("game", "pause", "shop", "gameover"):
        app.mode = mode
        app._draw()


def test_weapon_cycling_stays_within_the_inventory(app):
    state = app.state
    if state.me is None:
        pytest.skip("no live match")
    owned = state.my_weapons()
    for _ in range(len(owned) * 2 + 3):
        app._cycle_weapon(1)
        assert state.me["weapon"] in owned


def test_aim_helpers_clamp(app):
    if app.state.me is None:
        pytest.skip("no live match")
    for _ in range(400):
        app._nudge_angle(5)
        app._nudge_power(9)
    assert app.state.me["angle"] <= 180
    assert app.state.me["power"] <= 1000
    for _ in range(500):
        app._nudge_angle(-5)
        app._nudge_power(-9)
    assert app.state.me["angle"] >= 0
    assert app.state.me["power"] >= 0
