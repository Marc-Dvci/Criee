"""A living demo market for judges: simulated buyers and sellers in Strasbourg.

Every simulated object is flagged `simulated` in the ledger and labelled on every screen. Their
matches settle through the same PayPal sandbox path as anyone else's, on the demo wallet.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import random
from datetime import datetime, timedelta

from .domain import (Attr, Category, ClearingMode, Constraint, Fact, GeoPoint, LotListing, LotTerms, Op, Weekday,
                     WeeklyWindow, WishTerms, utcnow)
from .engine import MatchStatus

log = logging.getLogger("criee.demo")

NEIGHBOURHOODS = [  # (name, lat, lon, weight ~ residents)
    ("Krutenau", 48.5795, 7.7555, 9), ("Petite France", 48.5810, 7.7400, 5), ("Neudorf", 48.5700, 7.7600, 14),
    ("Esplanade", 48.5790, 7.7680, 10), ("Gare", 48.5850, 7.7350, 8), ("Contades", 48.5900, 7.7550, 6),
    ("Bourse", 48.5760, 7.7470, 6), ("Orangerie", 48.5900, 7.7700, 5), ("Cronenbourg", 48.5950, 7.7200, 7),
    ("Koenigshoffen", 48.5800, 7.7150, 6), ("Centre", 48.5834, 7.7458, 8),
]
SHOPS = [
    ("Fournil de la Krutenau", "Krutenau", 48.5797, 7.7540, [("Pain au levain", "Pain au levain 800 g", Category.BREAD, "loaf", 520, 250, {Attr.ORGANIC: True, Attr.WEIGHT_G: 800}),
                                                             ("Croissant", "Croissant pur beurre", Category.PASTRY, "piece", 160, 60, {Attr.CONTAINS_DAIRY: True})]),
    ("Boulangerie du Pont", "Petite France", 48.5812, 7.7413, [("Baguette tradition", "Baguette tradition", Category.BREAD, "baguette", 140, 50, {}),
                                                                ("Kougelhopf", "Kougelhopf", Category.PASTRY, "cake", 900, 400, {Attr.CONTAINS_NUTS: True})]),
    ("Pains et Graines", "Neudorf", 48.5712, 7.7590, [("Gluten-free loaf", "Pain sans gluten", Category.BREAD, "loaf", 650, 300, {Attr.GLUTEN_FREE: True, Attr.ORGANIC: True}),
                                                      ("Rye loaf", "Pain de seigle", Category.BREAD, "loaf", 480, 220, {Attr.VEGAN: True})]),
    ("Traiteur de l'Esplanade", "Esplanade", 48.5785, 7.7675, [("Vegetable tart", "Tarte aux légumes", Category.PREPARED_MEAL, "portion", 750, 300, {Attr.VEGETARIAN: True})]),
]
FARM = ("Ferme du Ried", "Neudorf", 48.5690, 7.7650)
WISH_MIX = [
    (Category.BREAD, "sourdough loaf", 300, 650, [Attr.ORGANIC], 0.35),
    (Category.BREAD, "baguette", 80, 150, [], 0.3),
    (Category.BREAD, "gluten-free loaf", 400, 700, [Attr.GLUTEN_FREE], 0.1),
    (Category.PASTRY, "croissants", 60, 150, [], 0.25),
    (Category.PREPARED_MEAL, "vegetarian dinner", 350, 800, [Attr.VEGETARIAN], 0.15),
    (Category.GROCERY_BOX, "vegetable box", 1800, 2600, [], 0.0),
]
EVENING = WeeklyWindow(days=tuple(Weekday), start_min=6 * 60, end_min=23 * 60 + 59)


def _rng(tag: str) -> random.Random:
    return random.Random(int(hashlib.sha256(tag.encode()).hexdigest(), 16) % 2**32)


def _place(r: random.Random) -> tuple[str, GeoPoint]:
    name, lat, lon, _ = r.choices(NEIGHBOURHOODS, weights=[n[3] for n in NEIGHBOURHOODS])[0]
    return name, GeoPoint(lat=lat + r.uniform(-0.004, 0.004), lon=lon + r.uniform(-0.006, 0.006))


def _sim_account(core, handle: str, roles: list[str], shop: dict | None = None) -> str:
    key = f"sim_account:{handle}"
    aid = core.ledger.kv_get(key)
    if aid and aid in core.accounts:
        return aid
    acc = core.create_account(handle, roles, simulated=True)
    core.ledger.kv_set(key, acc.id)
    if shop:
        core.emit("seller.shop", {"account_id": acc.id, "shop": shop})
    return acc.id


def seed_buyers(core, n: int = 70) -> None:
    if core.ledger.kv_get("demo_buyers"):
        return
    r = _rng("buyers")
    now = utcnow()
    for i in range(n):
        bid = _sim_account(core, f"Simulated buyer {i + 1:02d}", ["buyer"])
        cat, label, lo, hi, attrs, _ = r.choices(WISH_MIX[:-1], weights=[w[5] for w in WISH_MIX[:-1]])[0]
        place, where = _place(r)
        limit = r.randrange(lo, hi + 1, 10)
        units = 2 if cat is Category.PASTRY and r.random() < 0.5 else 1
        terms = WishTerms(category=cat, constraints=tuple(Constraint(attr=a, op=Op.IS, value=True) for a in attrs),
                          limit_cents=limit, units=units, max_matches_per_week=7,
                          budget_cents_per_week=limit * units * 7, pickup=where, radius_m=r.choice([1500, 2500, 4000]),
                          windows=(EVENING,), modes=tuple(ClearingMode), expires_at=now + timedelta(days=60))
        core.sign_wish(bid, terms, {"kind": "simulated"}, simulated=True, label=label, place=place,
                       readback=f"Simulated standing wish: {label} up to €{limit / 100:.2f}.")
    core.ledger.kv_set("demo_buyers", "1")


def _open_lots(core, seller_id: str) -> list:
    return [l for l in core.market.lots.values() if l.seller_id == seller_id and not l.closed]


def ensure_demo_market(core, now: datetime | None = None) -> int:
    """Keep a few clocks, one call auction and one group order open. Returns lots listed."""
    now = (now or utcnow()).replace(microsecond=0)
    seed_buyers(core)
    listed = 0
    for i, (name, place, lat, lon, items) in enumerate(SHOPS):
        sid = _sim_account(core, name, ["seller"], {"name": name, "place": place, "lat": lat, "lon": lon})
        if _open_lots(core, sid):
            continue
        r = _rng(f"{name}:{now.isoformat()}")
        en, fr, cat, unit, start, floor, facts = items[r.randrange(len(items))]
        mode = ClearingMode.CALL if i == 1 else ClearingMode.CLOCK
        minutes = r.choice([25, 35, 45]) if mode is ClearingMode.CLOCK else 20
        terms = LotTerms(category=cat, facts=tuple(Fact(attr=a, value=v, evidence="sc_demo000" + a.value[:3])
                                                   for a, v in facts.items()),
                         quantity=r.randint(6, 14), mode=mode, start_cents=start, floor_cents=floor, opens_at=now,
                         closes_at=now + timedelta(minutes=minutes), pickup=GeoPoint(lat=lat, lon=lon),
                         pickup_from=now, pickup_until=now + timedelta(minutes=minutes + 60),
                         clock_step_s=max(15, minutes * 60 // 40))
        core.list_lot(sid, terms, LotListing(name=en, name_fr=fr, unit_label=unit), simulated=True)
        listed += 1
    name, place, lat, lon = FARM
    fid = _sim_account(core, name, ["seller"], {"name": name, "place": place, "lat": lat, "lon": lon})
    if not _open_lots(core, fid):
        terms = LotTerms(category=Category.GROCERY_BOX, facts=(Fact(attr=Attr.ORGANIC, value=True, evidence="sc_demo000org"),
                                                                Fact(attr=Attr.LOCAL, value=True, evidence="sc_demo000loc"),
                                                                Fact(attr=Attr.WEIGHT_G, value=10000, evidence="sc_demo000wei")),
                         quantity=40, mode=ClearingMode.THRESHOLD, start_cents=2400, floor_cents=2400, opens_at=now,
                         closes_at=now + timedelta(days=7), pickup=GeoPoint(lat=lat, lon=lon),
                         pickup_from=now + timedelta(days=8), pickup_until=now + timedelta(days=8, hours=4),
                         threshold_qty=40)
        lid = core.list_lot(fid, terms, LotListing(name="Vegetable box 10 kg", name_fr="Colis de légumes 10 kg",
                                                   unit_label="box"), simulated=True)
        r = _rng(f"farm:{lid}")
        for i in range(39):
            bid = _sim_account(core, f"Simulated box buyer {lid[-4:]}-{i + 1:02d}", ["buyer"])
            pl, where = _place(r)
            t = WishTerms(category=Category.GROCERY_BOX, limit_cents=r.randrange(2400, 2900, 50), units=1,
                          max_matches_per_week=1, budget_cents_per_week=3000, pickup=where, radius_m=6000,
                          windows=(WeeklyWindow(days=tuple(Weekday), start_min=0, end_min=1440),),
                          modes=(ClearingMode.THRESHOLD,), expires_at=now + timedelta(days=30))
            core.sign_wish(bid, t, {"kind": "simulated"}, simulated=True, label="vegetable box", place=pl,
                           readback="Simulated standing wish: a 10 kg vegetable box up to €28.")
        listed += 1
    return listed


def simulated_pickups(core, now: datetime) -> int:
    """Simulated buyers collect a few minutes after their match; about one in eight does not come."""
    n = 0
    for m in list(core.market.matches.values()):
        if m.status is not MatchStatus.HELD or m.scanned or m.pickup_by is None:
            continue
        if not core.market.wishes[m.fill.wish_id].simulated:
            continue
        h = int(hashlib.sha256(m.fill.match_id.encode()).hexdigest(), 16)
        if h % 8 == 0:
            continue
        if now >= m.at + timedelta(minutes=2 + h % 9) and now >= core.market.lots[m.fill.lot_id].terms.pickup_from:
            core.emit("pickup.scanned", {"match_id": m.fill.match_id, "simulated": True})
            n += 1
    return n


async def demo_loop(core, every_s: float = 20.0) -> None:
    while True:
        try:
            ensure_demo_market(core)
            simulated_pickups(core, utcnow())
        except Exception:
            log.exception("demo market step failed")
        await asyncio.sleep(every_s)
