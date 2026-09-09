"""The armoury: weapons, defensive items, and the between-round shop.

Weapon behaviour lives in :mod:`scorched.physics`; this module is pure data plus
the small amount of book-keeping that inventories need.  Keeping it declarative
means the client can render a shop and an ammo bar without knowing how anything
actually flies.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Weapon:
    code: str
    name: str
    kind: str                 # dispatch tag used by the simulator
    damage: int = 0           # peak damage at ground zero
    radius: int = 0           # blast radius in pixels
    price: int = 0            # cost of one *pack*
    pack: int = 1             # rounds gained per purchase
    unlimited: bool = False
    tier: int = 1             # shop grouping / AI preference
    children: int = 0         # sub-munitions, for the splitting weapons
    blurb: str = ""

    @property
    def free(self) -> bool:
        return self.unlimited


@dataclass(frozen=True)
class Item:
    code: str
    name: str
    kind: str                 # "shield" | "repair" | "fuel" | "parachute" | "battery"
    price: int
    pack: int = 1
    strength: int = 0
    blurb: str = ""


# ---------------------------------------------------------------------------
# Weapons.  Prices are tuned so that a round's winnings buy roughly one good
# pack -- the original game's economy pacing, which keeps escalation gradual.
# ---------------------------------------------------------------------------
WEAPONS: tuple[Weapon, ...] = (
    Weapon("bmis", "Baby Missile", "he", damage=25, radius=14, price=0, pack=0,
           unlimited=True, tier=0, blurb="Free forever. Never impressive."),
    Weapon("mis", "Missile", "he", damage=45, radius=22, price=1900, pack=5,
           tier=1, blurb="The honest workhorse."),
    Weapon("bnuk", "Baby Nuke", "he", damage=70, radius=38, price=4600, pack=3,
           tier=2, blurb="A serious hole in the landscape."),
    Weapon("nuke", "Nuke", "he", damage=110, radius=62, price=12000, pack=2,
           tier=3, blurb="Ends arguments and hillsides."),
    Weapon("mirv", "MIRV", "mirv", damage=35, radius=18, price=8000, pack=3,
           tier=3, children=5, blurb="Splits into five at the top of its arc."),
    Weapon("funk", "Funky Bomb", "funky", damage=28, radius=16, price=7000, pack=3,
           tier=3, children=6, blurb="Scatters bouncing bomblets. Chaotic."),
    Weapon("leap", "Leapfrog", "leapfrog", damage=32, radius=18, price=5000, pack=4,
           tier=2, children=3, blurb="Detonates, hops, detonates again."),
    Weapon("roll", "Roller", "roller", damage=40, radius=20, price=2800, pack=5,
           tier=1, blurb="Rolls downhill into whatever is hiding there."),
    Weapon("hroll", "Heavy Roller", "roller", damage=75, radius=34, price=6800, pack=3,
           tier=2, blurb="A Roller that means it."),
    Weapon("dig", "Digger", "digger", damage=0, radius=30, price=1600, pack=5,
           tier=1, blurb="Excavates. Harms nobody, exposes everybody."),
    Weapon("dirt", "Dirt Ball", "dirt", damage=0, radius=32, price=1800, pack=5,
           tier=1, blurb="Buries a neighbour, or rebuilds your hill."),
    Weapon("trac", "Tracer", "tracer", damage=0, radius=0, price=0, pack=0,
           unlimited=True, tier=0, blurb="Free ranging shot. Costs you the turn."),
)

WEAPON_BY_CODE: dict[str, Weapon] = {w.code: w for w in WEAPONS}

ITEMS: tuple[Item, ...] = (
    Item("shld", "Shield", "shield", price=2500, pack=3, strength=75,
         blurb="Soaks 75 damage before your hull does."),
    Item("hshld", "Heavy Shield", "shield", price=6000, pack=2, strength=150,
         blurb="Soaks 150. Worth every credit."),
    Item("rep", "Repair Kit", "repair", price=2000, pack=3, strength=50,
         blurb="Auto-patches 50 hull when you are nearly out."),
    Item("fuel", "Fuel", "fuel", price=1200, pack=3, strength=100,
         blurb="100 more units of tread before you fire."),
    Item("para", "Parachute", "parachute", price=1500, pack=3, strength=0,
         blurb="Cancels falling damage once."),
)

ITEM_BY_CODE: dict[str, Item] = {i.code: i for i in ITEMS}

#: Everything purchasable, in the order the shop lists it.
SHOP_ORDER: tuple[str, ...] = tuple(
    [w.code for w in WEAPONS if not w.unlimited] + [i.code for i in ITEMS]
)


def catalogue() -> list[dict]:
    """Shop rows, ready to hand to a client that knows nothing about rules."""
    rows = []
    for w in WEAPONS:
        if w.unlimited:
            continue
        rows.append({
            "code": w.code, "name": w.name, "price": w.price, "pack": w.pack,
            "kind": "weapon", "damage": w.damage, "radius": w.radius,
            "blurb": w.blurb,
        })
    for i in ITEMS:
        rows.append({
            "code": i.code, "name": i.name, "price": i.price, "pack": i.pack,
            "kind": "item", "damage": 0, "radius": 0, "blurb": i.blurb,
        })
    return rows


@dataclass
class Inventory:
    """Per-player ammunition and equipment counts."""

    ammo: dict[str, int] = field(default_factory=dict)
    items: dict[str, int] = field(default_factory=dict)

    @classmethod
    def starting(cls) -> "Inventory":
        inv = cls()
        # A couple of real missiles so round one is not entirely Baby Missiles.
        inv.ammo["mis"] = 5
        inv.ammo["roll"] = 2
        inv.items["shld"] = 1
        return inv

    # -- weapons ---------------------------------------------------------
    def count(self, code: str) -> int:
        weapon = WEAPON_BY_CODE.get(code)
        if weapon is not None and weapon.unlimited:
            return -1                       # -1 renders as the infinity glyph
        return self.ammo.get(code, 0)

    def has(self, code: str) -> bool:
        return self.count(code) != 0

    def consume(self, code: str) -> bool:
        weapon = WEAPON_BY_CODE.get(code)
        if weapon is None:
            return False
        if weapon.unlimited:
            return True
        left = self.ammo.get(code, 0)
        if left <= 0:
            return False
        if left == 1:
            del self.ammo[code]
        else:
            self.ammo[code] = left - 1
        return True

    def available_weapons(self) -> list[str]:
        """Codes the player can actually fire, in catalogue order."""
        return [w.code for w in WEAPONS if self.has(w.code)]

    # -- items -----------------------------------------------------------
    def item_count(self, code: str) -> int:
        return self.items.get(code, 0)

    def consume_item(self, code: str) -> bool:
        left = self.items.get(code, 0)
        if left <= 0:
            return False
        if left == 1:
            del self.items[code]
        else:
            self.items[code] = left - 1
        return True

    def add(self, code: str, qty: int) -> None:
        if code in WEAPON_BY_CODE:
            self.ammo[code] = self.ammo.get(code, 0) + qty
        elif code in ITEM_BY_CODE:
            self.items[code] = self.items.get(code, 0) + qty

    # -- wire ------------------------------------------------------------
    def to_wire(self) -> dict:
        return {"ammo": dict(self.ammo), "items": dict(self.items)}

    @classmethod
    def from_wire(cls, data: dict) -> "Inventory":
        return cls(ammo=dict(data.get("ammo", {})), items=dict(data.get("items", {})))


def price_of(code: str) -> int | None:
    if code in WEAPON_BY_CODE:
        return WEAPON_BY_CODE[code].price
    if code in ITEM_BY_CODE:
        return ITEM_BY_CODE[code].price
    return None


def pack_of(code: str) -> int:
    if code in WEAPON_BY_CODE:
        return WEAPON_BY_CODE[code].pack
    if code in ITEM_BY_CODE:
        return ITEM_BY_CODE[code].pack
    return 0


def display_name(code: str) -> str:
    if code in WEAPON_BY_CODE:
        return WEAPON_BY_CODE[code].name
    if code in ITEM_BY_CODE:
        return ITEM_BY_CODE[code].name
    return code
