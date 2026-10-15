from __future__ import annotations

from datetime import timedelta

import pytest

from criee.domain import Attr, Category, ClearingMode, Constraint, Op
from criee.engine import MatchStatus, Skip, clock_price, clock_schedule
from helpers import T0, GeoPoint, Harness, fact, lot_terms, wish_terms


# ----------------------------------------------------------------------------- call auction

def test_call_auction_uniform_price_is_highest_losing_limit():
    h = Harness()
    for limit in (500, 450, 420, 380, 300):
        h.wish(wish_terms(limit=limit))
    lot = h.lot(lot_terms(ClearingMode.CALL, qty=3, start=600, floor=250))
    h.run(T0 + timedelta(hours=3))
    fills = h.fills(lot)
    assert sorted(f.unit_cents for f in fills) == [380, 380, 380]
    assert len(fills) == 3


def test_call_auction_pays_floor_when_demand_is_short():
    h = Harness()
    h.wish(wish_terms(limit=500))
    h.wish(wish_terms(limit=450))
    lot = h.lot(lot_terms(ClearingMode.CALL, qty=5, floor=250))
    h.run(T0 + timedelta(hours=3))
    assert [f.unit_cents for f in h.fills(lot)] == [250, 250]


def test_call_auction_ignores_limits_under_floor():
    h = Harness()
    h.wish(wish_terms(limit=200))
    lot = h.lot(lot_terms(ClearingMode.CALL, qty=5, floor=250))
    h.run(T0 + timedelta(hours=3))
    assert h.fills(lot) == []


# ----------------------------------------------------------------------------- clock

def test_clock_reaches_floor_before_deadline():
    t = lot_terms(ClearingMode.CLOCK, start=600, floor=200, minutes=60, step_s=60)
    dec, total = clock_schedule(t)
    assert total == 60
    assert clock_price(t, 0) == 600
    assert clock_price(t, int(total * 0.8)) == 200
    assert clock_price(t, total - 1) == 200


def test_clock_fills_each_wish_when_price_reaches_its_limit():
    h = Harness()
    hi = h.wish(wish_terms(limit=550))
    lo = h.wish(wish_terms(limit=300))
    lot = h.lot(lot_terms(ClearingMode.CLOCK, qty=5, start=600, floor=200))
    h.run(T0 + timedelta(hours=3))
    by_wish = {f.wish_id: f for f in h.fills(lot)}
    assert by_wish[hi].unit_cents <= 550 and by_wish[hi].unit_cents > 500
    assert by_wish[lo].unit_cents <= 300
    assert by_wish[hi].round < by_wish[lo].round


def test_clock_no_show_returns_unit_and_it_resells():
    h = Harness()
    first = h.wish(wish_terms(limit=550))
    lot = h.lot(lot_terms(ClearingMode.CLOCK, qty=1, start=600, floor=200, minutes=90))
    h.run(T0 + timedelta(minutes=15))
    [f1] = h.fills(lot)
    assert f1.wish_id == first
    second = h.wish(wish_terms(limit=300), at=T0 + timedelta(minutes=20))
    h.run(T0 + timedelta(minutes=38))
    assert len(h.fills(lot)) == 1, "the unit is held for the first buyer"
    h.run(T0 + timedelta(hours=3))
    assert h.market.matches[f1.match_id].status is MatchStatus.NO_SHOW
    resold = [f for f in h.fills(lot) if f.wish_id == second]
    assert len(resold) == 1 and resold[0].unit_cents <= 300


def test_scan_before_deadline_prevents_no_show():
    h = Harness()
    h.wish(wish_terms(limit=550))
    lot = h.lot(lot_terms(ClearingMode.CLOCK, qty=1, start=600, floor=200, minutes=90))
    h.run(T0 + timedelta(minutes=15))
    [f1] = h.fills(lot)
    h.emit("pickup.scanned", {"match_id": f1.match_id}, T0 + timedelta(minutes=16))
    h.run(T0 + timedelta(hours=3))
    assert h.market.matches[f1.match_id].status is not MatchStatus.NO_SHOW


def test_revoked_wish_is_removed_from_the_next_tick():
    h = Harness()
    w = h.wish(wish_terms(limit=300))
    lot = h.lot(lot_terms(ClearingMode.CLOCK, qty=5, start=600, floor=200))
    h.run(T0 + timedelta(minutes=10))
    assert h.fills(lot) == []
    h.emit("wish.revoked", {"wish_id": w}, T0 + timedelta(minutes=10, seconds=30))
    h.run(T0 + timedelta(hours=3))
    assert h.fills(lot) == []


def test_wish_signed_after_a_tick_is_not_in_that_tick():
    h = Harness()
    lot = h.lot(lot_terms(ClearingMode.CLOCK, qty=5, start=600, floor=200))
    w = h.wish(wish_terms(limit=700), at=T0 + timedelta(minutes=3, seconds=10))
    h.run(T0 + timedelta(minutes=10))
    [f] = h.fills(lot)
    assert f.wish_id == w and f.round == 4


# ----------------------------------------------------------------------------- eligibility

def test_unknown_fact_never_satisfies_a_constraint():
    h = Harness()
    gf = Constraint(attr=Attr.GLUTEN_FREE, op=Op.IS, value=True)
    w = h.wish(wish_terms(limit=700, constraints=[gf]))
    unknown = h.lot(lot_terms(ClearingMode.CALL))
    known = h.lot(lot_terms(ClearingMode.CALL, facts=[fact(Attr.GLUTEN_FREE, True)]))
    assert h.market.explain(w, unknown, T0) is Skip.FACT_UNKNOWN
    h.run(T0 + timedelta(hours=3))
    assert [f.lot_id for f in h.fills()] == [known]


def test_distance_window_mode_and_category_filters():
    h = Harness()
    far = h.wish(wish_terms(where=GeoPoint(lat=48.70, lon=7.90), radius=2000))
    pastry = h.wish(wish_terms(category=Category.PASTRY))
    clock_only = h.wish(wish_terms(modes=[ClearingMode.CLOCK]))
    lot = h.lot(lot_terms(ClearingMode.CALL))
    assert h.market.explain(far, lot, T0) is Skip.DISTANCE
    assert h.market.explain(pastry, lot, T0) is Skip.CATEGORY
    assert h.market.explain(clock_only, lot, T0) is Skip.MODE


def test_weekly_count_and_budget_caps():
    h = Harness()
    w = h.wish(wish_terms(limit=500, per_week=2, budget=1000))
    lots = [h.lot(lot_terms(ClearingMode.CALL, floor=400, start=600), at=T0) for _ in range(3)]
    h.run(T0 + timedelta(hours=3))
    assert len(h.fills()) == 2
    assert sum(f.unit_cents for f in h.fills()) <= 1000


# ----------------------------------------------------------------------------- threshold

def test_short_threshold_holds_at_commitment_and_fails_below_quorum():
    h = Harness()
    for _ in range(3):
        h.wish(wish_terms(limit=2500, category=Category.GROCERY_BOX))
    lot = h.lot(lot_terms(ClearingMode.THRESHOLD, qty=10, start=2000, threshold=5, minutes=120,
                          category=Category.GROCERY_BOX))
    h.run(T0 + timedelta(minutes=1))
    assert len(h.fills(lot)) == 3, "short windows take a hold at each commitment"
    h.run(T0 + timedelta(hours=5))
    assert h.market.lots[lot].closed == "threshold_failed"


def test_long_threshold_fires_one_burst_at_quorum():
    h = Harness()
    lot = h.lot(lot_terms(ClearingMode.THRESHOLD, qty=40, start=2000, threshold=40, minutes=10 * 24 * 60,
                          category=Category.GROCERY_BOX))
    for i in range(37):
        h.wish(wish_terms(limit=2500, category=Category.GROCERY_BOX), at=T0 + timedelta(minutes=i))
    h.run(T0 + timedelta(hours=2))
    assert h.fills(lot) == [] and len(h.market.lots[lot].committed) == 37
    for i in range(3):
        h.wish(wish_terms(limit=2500, category=Category.GROCERY_BOX), at=T0 + timedelta(hours=3, minutes=i))
    h.run(T0 + timedelta(hours=4))
    assert len(h.fills(lot)) == 40
    assert h.market.lots[lot].reached


def test_threshold_failed_hold_is_replaced_from_waitlist():
    h = Harness()
    lot = h.lot(lot_terms(ClearingMode.THRESHOLD, qty=10, start=2000, threshold=10, minutes=10 * 24 * 60,
                          category=Category.GROCERY_BOX))
    for i in range(11):
        h.wish(wish_terms(limit=2500, category=Category.GROCERY_BOX), at=T0 + timedelta(minutes=i))
    h.run(T0 + timedelta(hours=1))
    fills = h.fills(lot)
    assert len(fills) == 10
    h.emit("settlement.auth_failed", {"match_id": fills[0].match_id, "reason": "INSTRUMENT_DECLINED"},
           T0 + timedelta(hours=1))
    h.run(T0 + timedelta(hours=2))
    live = [m for m in h.market.matches.values() if m.status is not MatchStatus.FAILED]
    assert len(live) == 10


# ----------------------------------------------------------------------------- ledger

def test_ledger_chain_verifies_and_detects_tampering():
    h = Harness()
    h.wish(wish_terms())
    h.lot(lot_terms(ClearingMode.CALL))
    h.run(T0 + timedelta(hours=3))
    assert h.ledger.verify()["ok"]
    with pytest.raises(Exception):
        h.ledger._db.execute("UPDATE events SET body = '{}' WHERE seq = 1")
