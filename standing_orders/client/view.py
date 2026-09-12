"""The client's model of the battlefield -- only ever what the server sent."""

from __future__ import annotations


class WorldView:
    def __init__(self) -> None:
        self.units: dict[int, dict] = {}
        self.buildings: dict[int, dict] = {}
        self.node_owner: dict[tuple, int] = {}
        self.visible: set = set()
        #: Everywhere we have ever seen. Ground you have scouted stays drawn,
        #: just dimmed -- only genuinely unexplored map is blacked out, which
        #: is the difference between a readable board and a dark void.
        self.explored: set = set()
        #: Enemies we have seen before but cannot see now, drawn ghosted so
        #: you are not made to re-scout ground you already scouted.
        self.remembered: dict[int, dict] = {}
        self.supply: int = 0
        self.cap: int = 0
        self.army: int = 0
        self.turn: int = 0
        #: Diplomacy, as the server last reported it. Public knowledge: every
        #: commander sees who signed what, who is about to tear it up, and who
        #: is calling for the war to end.
        self.pacts: dict = {}
        self.offers: dict = {}
        self.breaking: set = set()
        self.armistice: set = set()
        self.ground: dict = {}
        #: uid -> True when the unit is facing left. Kept outside the unit
        #: dicts because those are rebuilt from scratch every state sync.
        self.facing: dict[int, bool] = {}

    def apply_state(self, state: dict) -> None:
        fresh = {u["uid"]: dict(u) for u in state.get("units", [])}
        # Anything we could see last turn but cannot now becomes a memory.
        for uid, unit in self.units.items():
            if uid not in fresh:
                ghost = dict(unit)
                ghost["ghost"] = True
                self.remembered[uid] = ghost
        for uid in fresh:
            self.remembered.pop(uid, None)
        self.units = fresh
        self.buildings = {b["bid"]: dict(b) for b in state.get("buildings", [])}
        self.node_owner = {tuple(t): o for t, o in state.get("nodes", [])}
        self.visible = {tuple(t) for t in state.get("vision", [])}
        self.explored |= self.visible
        self.supply = state.get("supply", self.supply)
        self.cap = state.get("cap", self.cap)
        self.army = state.get("army", self.army)
        self.turn = state.get("turn", self.turn)
        self.pacts = {tuple(pair): pact for pair, pact in state.get("pacts", [])}
        self.offers = {tuple(pair): pact for pair, pact in state.get("offers", [])}
        self.breaking = {tuple(pair) for pair in state.get("breaking", [])}
        self.armistice = set(state.get("armistice", []))
        self.ground = {int(k): v for k, v in state.get("ground", {}).items()}
        # A remembered unit standing on ground we can now see is simply gone.
        for uid in [u for u, g in self.remembered.items()
                    if (g["x"], g["y"]) in self.visible]:
            del self.remembered[uid]

    # -- diplomacy ---------------------------------------------------------
    @staticmethod
    def pair(a: int, b: int) -> tuple:
        return (a, b) if a <= b else (b, a)

    def pact_with(self, a: int, b: int) -> str:
        return "alliance" if a == b else self.pacts.get(self.pair(a, b), "war")

    def offer_from(self, sender: int, to: int) -> str | None:
        return self.offers.get((sender, to))

    def breaking_with(self, a: int, b: int) -> bool:
        return self.pair(a, b) in self.breaking

    def face_left(self, uid: int) -> bool:
        return self.facing.get(uid, False)

    def turn_to(self, uid: int, dx: int) -> None:
        if dx:
            self.facing[uid] = dx < 0

    def mine(self, pid: int) -> list:
        return [u for u in self.units.values() if u["owner"] == pid]

    def unit_at(self, tile) -> dict | None:
        for unit in self.units.values():
            if (unit["x"], unit["y"]) == tile:
                return unit
        return None

    def building_at(self, tile) -> dict | None:
        for building in self.buildings.values():
            if (building["x"], building["y"]) == tile:
                return building
        return None
