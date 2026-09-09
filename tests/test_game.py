from scorched.game import PHASE_BUY, PHASE_GAME_OVER, Game, Settings
from scorched.terrain import WORLD_W


def make_game(players=3, **kwargs):
    game = Game(Settings(**kwargs), seed=17)
    for i in range(players):
        game.add_player(f"P{i}")
    game.start_match()
    return game


def test_settings_are_clamped():
    settings = Settings(rounds=999, gravity=99, wind_max=-5,
                        terrain_style="nonsense", wall_mode="nonsense").clamp()
    assert settings.rounds <= 50
    assert 0.02 <= settings.gravity <= 0.6
    assert settings.wind_max >= 0
    assert settings.terrain_style == "random"
    assert settings.wall_mode == "none"


def test_settings_wire_roundtrip():
    original = Settings(rounds=7, wind_max=30, terrain_style="canyon")
    assert Settings.from_wire(original.to_wire()).to_wire() == original.to_wire()


def test_players_get_distinct_colors():
    game = Game(Settings(), seed=1)
    colors = {game.add_player(f"P{i}").color for i in range(8)}
    assert len(colors) == 8
    assert game.add_player("nine") is None       # roster is full


def test_names_are_sanitised():
    game = Game(Settings(), seed=1)
    assert game.add_player("  ").name == "Player"
    assert len(game.add_player("x" * 50).name) <= 14


def test_tanks_start_apart_and_on_the_ground():
    game = make_game(4)
    xs = sorted(int(p.x) for p in game.players.values())
    assert all(b - a > 20 for a, b in zip(xs, xs[1:]))
    for player in game.players.values():
        assert player.y == game.terrain.surface(int(player.x))
        assert 0 < player.x < WORLD_W


def test_turn_order_skips_the_dead():
    game = make_game(3)
    victim = game.players[game.order[1]]
    victim.alive = False
    first = game.current_or_none()
    game.advance_turn()
    assert game.current_or_none() is not victim
    assert game.current_or_none() is not first


def test_firing_consumes_ammo():
    game = make_game(2)
    player = game.current_or_none()
    player.weapon = "mis"
    before = player.inv.count("mis")
    game.fire(player)
    assert player.inv.count("mis") == before - 1


def test_firing_out_of_turn_is_refused():
    game = make_game(2)
    other = next(p for p in game.players.values() if p is not game.current_or_none())
    assert game.fire(other) is None


def test_firing_an_unowned_weapon_falls_back_to_the_free_one():
    game = make_game(2)
    player = game.current_or_none()
    player.weapon = "nuke"               # never bought
    payload = game.fire(player)
    assert payload["weapon"] == "bmis"


def test_aim_is_clamped():
    game = make_game(2)
    player = game.current_or_none()
    game.set_aim(player, angle=999, power=-50)
    assert player.angle == 180 and player.power == 0


def test_moving_costs_fuel_and_refuses_cliffs():
    game = make_game(2, fuel_per_round=10)
    player = game.current_or_none()
    x = int(player.x)
    game.terrain.height[x + 1] = game.terrain.height[x] - 40   # a wall
    assert not game.move(player, 1)
    assert player.fuel == 10
    game.terrain.height[x + 1] = game.terrain.height[x]
    assert game.move(player, 1)
    assert player.fuel < 10


def test_move_stops_at_the_map_edge():
    game = make_game(2, fuel_per_round=500)
    player = game.current_or_none()
    player.x = 10.0
    for _ in range(20):
        game.move(player, -1)
    assert player.x >= 10


def test_shield_activation_consumes_one_and_refuses_a_second():
    game = make_game(2)
    player = game.current_or_none()
    player.inv.add("shld", 2)
    assert game.activate_shield(player, "shld")
    assert player.shield_hp > 0
    assert not game.activate_shield(player, "shld")   # already up


def test_buy_and_sell():
    game = make_game(2)
    player = game.current_or_none()
    player.cash = 10000
    assert game.buy(player, "mis", 1)
    assert player.cash == 10000 - 1900
    assert game.sell(player, "mis")
    assert player.cash == 10000 - 1900 + 950
    player.cash = 0
    assert not game.buy(player, "nuke", 1)


def test_selling_what_you_do_not_have_is_refused():
    game = make_game(2)
    player = game.current_or_none()
    assert not game.sell(player, "nuke")


def test_damage_pays_and_a_kill_pays_more():
    game = make_game(2)
    shooter = game.players[0]
    shooter.cash = 0

    class Result:
        damage_dealt = {1: 40}
        killed = []
    game._settle_economy(shooter, Result())
    assert shooter.cash == 40 * game.settings.cash_per_damage

    Result.killed = [1]
    game._settle_economy(shooter, Result())
    assert shooter.cash > 40 * game.settings.cash_per_damage * 2


def test_self_harm_is_penalised():
    game = make_game(2)
    shooter = game.players[0]
    shooter.cash = 50000

    class Result:
        damage_dealt = {0: 30}
        killed = [0]
    game._settle_economy(shooter, Result())
    assert shooter.cash < 50000


def test_round_ends_when_one_tank_is_left_and_a_shop_opens():
    game = make_game(3, rounds=3)
    survivors = list(game.players.values())
    for player in survivors[1:]:
        player.alive = False
    assert game.check_round_over()
    assert game.phase == PHASE_BUY
    assert survivors[0].wins == 1


def test_last_round_ends_the_match():
    game = make_game(2, rounds=1)
    list(game.players.values())[1].alive = False
    assert game.check_round_over()
    assert game.phase == PHASE_GAME_OVER


def test_sudden_death_eventually_kills_everyone():
    game = make_game(2, rounds=1, sudden_death=3)
    for _ in range(200):
        if game.phase == PHASE_GAME_OVER:
            break
        game.advance_turn()
    assert game.phase == PHASE_GAME_OVER


def test_sudden_death_can_be_switched_off():
    game = make_game(2, rounds=1, sudden_death=0)
    game.turns_this_round = 5000
    game._sudden_death()
    assert all(p.hp == 100 for p in game.players.values())


def test_repair_kit_only_fires_when_badly_hurt():
    game = make_game(2)
    player = game.current_or_none()
    player.inv.add("rep", 3)
    player.hp = 80
    game._auto_repair(player)
    assert player.inv.item_count("rep") == 3      # not wasted
    player.hp = 20
    game._auto_repair(player)
    assert player.hp > 20
    assert player.inv.item_count("rep") == 2      # exactly one kit


def test_new_round_resets_health_and_reseeds_terrain():
    game = make_game(2, rounds=5)
    before = list(game.terrain.height)
    for player in game.players.values():
        player.hp = 3
    game.start_round()
    assert all(p.hp == p.max_hp for p in game.players.values())
    assert game.terrain.height != before


def test_state_wire_is_json_safe():
    import json
    game = make_game(3)
    json.dumps(game.state_wire())
    json.dumps(game.round_wire())
    json.dumps(game.standings())


def test_removing_a_player_keeps_the_turn_order_valid():
    game = make_game(4)
    for _ in range(3):
        game.advance_turn()
    game.remove_player(game.order[0])
    assert 0 <= game.turn_index < len(game.order)
    assert game.current_or_none() is not None
