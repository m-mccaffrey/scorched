"""The client's view of the world.

Purely a mirror of what the server has told us.  Nothing here decides anything;
when the server's next ``state`` message disagrees with a value the animation
predicted, the server simply wins.
"""

from __future__ import annotations

from ..terrain import Terrain
from ..weapons import WEAPON_BY_CODE, WEAPONS


class ClientState:
    def __init__(self) -> None:
        self.players: dict[int, dict] = {}
        self.terrain: Terrain | None = None
        self.my_pid: int = -1
        self.turn_pid: int = -1
        self.phase: str = "lobby"
        self.round: int = 0
        self.rounds: int = 0
        self.wind: int = 0
        self.time_left: float = -1.0
        self.settings: dict = {}
        self.catalogue: list = []
        self.colors: list = []
        self.is_host: bool = False
        self.server_name: str = ""
        self.standings: list = []
        self.log: list[str] = []
        self.chat: list[tuple[str, str, tuple]] = []
        self.round_seed: int = 0

    # -- convenience -------------------------------------------------------
    @property
    def me(self) -> dict | None:
        return self.players.get(self.my_pid)

    @property
    def my_turn(self) -> bool:
        return self.turn_pid == self.my_pid and self.phase == "aim"

    def sync_players(self, rows: list) -> None:
        seen = set()
        for row in rows:
            pid = row["pid"]
            seen.add(pid)
            existing = self.players.get(pid)
            if existing is None:
                self.players[pid] = dict(row)
            else:
                existing.update(row)
        for pid in list(self.players):
            if pid not in seen:
                del self.players[pid]

    def my_ammo(self, code: str) -> int | None:
        me = self.me
        if me is None:
            return None
        weapon = WEAPON_BY_CODE.get(code)
        if weapon is not None and weapon.unlimited:
            return -1
        return me.get("inv", {}).get("ammo", {}).get(code, 0)

    def my_items(self) -> dict:
        me = self.me
        return (me or {}).get("inv", {}).get("items", {})

    def my_weapons(self) -> list[str]:
        """Codes I can fire right now, in catalogue order."""
        me = self.me
        if me is None:
            return ["bmis"]
        ammo = me.get("inv", {}).get("ammo", {})
        out = []
        for weapon in WEAPONS:
            if weapon.unlimited or ammo.get(weapon.code, 0) > 0:
                out.append(weapon.code)
        return out or ["bmis"]

    def note(self, text: str) -> None:
        self.log.append(text)
        del self.log[:-8]
