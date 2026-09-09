import random

from scorched.ai import SKILLS, BotBrain
from scorched.game import Game, Settings
from scorched.weapons import WEAPON_BY_CODE


def bot_game(players=3, skill="expert", **kwargs):
    game = Game(Settings(**kwargs), seed=23)
    brains = {}
    for i in range(players):
        player = game.add_player(f"B{i}", bot=True, skill=skill)
        brains[player.pid] = BotBrain(skill, random.Random(i))
    game.start_match()
    return game, brains


def test_every_skill_produces_a_legal_action():
    for skill in SKILLS:
        game, brains = bot_game(skill=skill)
        player = game.current_or_none()
        action = brains[player.pid].take_turn(game, player)
        assert 0 <= action["angle"] <= 180
        assert 0 <= action["power"] <= 1000
        assert action["weapon"] in WEAPON_BY_CODE
        assert player.inv.has(action["weapon"])


def test_bot_aims_toward_its_target():
    game, brains = bot_game(players=2)
    shooter = game.current_or_none()
    target = next(p for p in game.players.values() if p is not shooter)
    action = brains[shooter.pid].take_turn(game, shooter)
    if target.x > shooter.x:
        assert action["angle"] < 90       # pointing right
    else:
        assert action["angle"] > 90


def test_unknown_skill_degrades_to_moderate():
    assert BotBrain("wizard").skill == "moderate"


def test_better_bots_hit_more_often():
    def hit_rate(skill, trials=45):
        hits = shots = 0
        for seed in range(trials):
            game = Game(Settings(wind_max=40), seed=seed)
            for i in range(2):
                game.add_player(f"B{i}", bot=True, skill=skill)
            game.start_match()
            brain = BotBrain(skill, random.Random(seed))
            player = game.current_or_none()
            action = brain.take_turn(game, player)
            game.set_weapon(player, action["weapon"])
            game.set_aim(player, action["angle"], action["power"])
            payload = game.fire(player)
            shots += 1
            if payload and any(e["e"] == "hp" for e in payload["events"]):
                hits += 1
        return hits / max(1, shots)

    assert hit_rate("cyborg") > hit_rate("novice")


def test_bot_shopping_spends_but_never_overdraws():
    game, brains = bot_game(players=2)
    player = game.current_or_none()
    player.cash = 30000
    brains[player.pid].shop(game, player)
    assert player.cash >= 0
    assert player.cash < 30000
    assert player.done_buying


def test_a_broke_bot_still_finishes_shopping():
    game, brains = bot_game(players=2)
    player = game.current_or_none()
    player.cash = 0
    brains[player.pid].shop(game, player)
    assert player.cash == 0
    assert player.done_buying


def test_bots_can_finish_a_whole_match_without_stalling():
    game, brains = bot_game(players=4, skill="moderate", rounds=2,
                            sudden_death=40)
    for _ in range(2000):
        if game.phase == "gameover":
            break
        if game.phase == "buy":
            for player in game.players.values():
                brains[player.pid].shop(game, player)
            game.start_round()
            continue
        player = game.current_or_none()
        if player is None:
            break
        action = brains[player.pid].take_turn(game, player)
        if action.get("shield"):
            game.activate_shield(player, action["shield"])
        game.set_weapon(player, action["weapon"])
        game.set_aim(player, action["angle"], action["power"])
        game.fire(player)
        game.advance_turn()
    assert game.phase == "gameover"
    assert sum(p.wins for p in game.players.values()) >= 1
