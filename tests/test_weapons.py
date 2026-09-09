from scorched import weapons as W


def test_catalogue_covers_everything_purchasable():
    codes = {row["code"] for row in W.catalogue()}
    assert codes == set(W.SHOP_ORDER)
    assert "bmis" not in codes          # free weapons are not sold


def test_every_weapon_has_a_price_or_is_free():
    for weapon in W.WEAPONS:
        if weapon.unlimited:
            assert weapon.price == 0
        else:
            assert weapon.price > 0 and weapon.pack > 0


def test_unlimited_ammo_never_runs_out():
    inv = W.Inventory.starting()
    for _ in range(50):
        assert inv.consume("bmis")
    assert inv.count("bmis") == -1


def test_consuming_ammo_removes_the_entry_when_empty():
    inv = W.Inventory()
    inv.add("mis", 2)
    assert inv.consume("mis") and inv.consume("mis")
    assert not inv.consume("mis")
    assert "mis" not in inv.ammo


def test_available_weapons_follows_catalogue_order():
    inv = W.Inventory()
    inv.add("nuke", 1)
    inv.add("mis", 1)
    available = inv.available_weapons()
    assert available.index("mis") < available.index("nuke")
    assert available[0] == "bmis"


def test_inventory_wire_roundtrip():
    inv = W.Inventory.starting()
    inv.add("hshld", 2)
    copy = W.Inventory.from_wire(inv.to_wire())
    assert copy.ammo == inv.ammo and copy.items == inv.items


def test_lookup_helpers():
    assert W.display_name("nuke") == "Nuke"
    assert W.display_name("shld") == "Shield"
    assert W.display_name("nonsense") == "nonsense"
    assert W.price_of("nonsense") is None
    assert W.pack_of("nuke") == 2
