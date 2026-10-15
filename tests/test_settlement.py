from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from criee.config import Config
from criee.core import Core
from criee.domain import Category, ClearingMode, LotListing, utcnow
from criee.engine import MatchStatus
from criee.settlement.paypal import FakePayPal
from helpers import lot_terms, wish_terms


def make_config(tmp: Path, **kw) -> Config:
    base = Config(data_dir=tmp, public_url="http://test", paypal_client_id=None, paypal_secret=None, currency="EUR",
                  fee_bps=400, demo_vault_id="VAULT-DEMO", seller_payout_email="seller@example.com",
                  sandbox_buyer_email=None, sandbox_buyer_password=None, webhook_id="WH", ai_provider="recorded",
                  gemini_api_key=None, gemini_model="x", ai_until=None, ai_daily_cap=10, ai_per_visitor_hourly=10,
                  session_secret="s" * 64, rp_id=None, clock_scale=1.0)
    return replace(base, **kw)


@pytest.fixture
def core(tmp_path):
    c = Core(make_config(tmp_path), FakePayPal())
    return c


def _buyer(core: Core) -> str:
    acc = core.create_account("Léa", ["buyer"])
    core.ledger.kv_set(f"vault:{acc.id}", "VAULT-LEA")
    core.emit("buyer.vaulted", {"account_id": acc.id, "method": "paypal"})
    return acc.id


async def _settle(core: Core, rounds: int = 6) -> None:
    for _ in range(rounds):
        core.tick()
        await core.worker.step()


def test_clock_sale_is_held_captured_tracked_and_paid(core):
    from criee.settlement.worker import SettlementWorker
    core.worker = SettlementWorker(core)
    seller = core.create_account("Boulangerie", ["seller"]).id
    buyer = _buyer(core)
    now = utcnow()
    core.sign_wish(buyer, wish_terms(limit=700, expires=now + timedelta(days=5)), {"kind": "test"},
                   at=now - timedelta(minutes=5))
    lot = core.list_lot(seller, lot_terms(ClearingMode.CLOCK, qty=3, start=600, floor=200, opens=now - timedelta(minutes=1)),
                        LotListing(name="Pain au levain"))
    asyncio.run(_settle(core))
    [mid] = core.market.by_lot[lot]
    assert core.market.matches[mid].status is MatchStatus.HELD
    core.emit("pickup.scanned", {"match_id": mid})
    asyncio.run(_settle(core))
    s = core.book.s(mid)
    assert core.market.matches[mid].status is MatchStatus.CAPTURED
    assert s.capture_id and s.tracked and s.payout_id
    p = core.book.payouts[s.payout_id]
    assert p.gross == 600 and p.fee == 24 and p.net == 576
    assert core.ledger.verify()["ok"]


def test_no_show_is_voided_and_the_unit_resells(core):
    from criee.settlement.worker import SettlementWorker
    core.worker = SettlementWorker(core)
    seller = core.create_account("Boulangerie", ["seller"]).id
    a, b = _buyer(core), _buyer(core)
    t0 = utcnow() - timedelta(minutes=10)
    core.sign_wish(a, wish_terms(limit=700, expires=t0 + timedelta(days=5)), {"kind": "test"}, at=t0 - timedelta(minutes=1))
    lot = core.list_lot(seller, lot_terms(ClearingMode.CLOCK, qty=1, start=600, floor=200, minutes=240, opens=t0),
                        LotListing(name="Kouglof"), at=t0)
    asyncio.run(_settle(core, 2))
    [first] = core.market.by_lot[lot]
    assert core.market.matches[first].status is MatchStatus.HELD
    core.sign_wish(b, wish_terms(limit=600, expires=t0 + timedelta(days=5)), {"kind": "test"})
    core.tick(utcnow() + timedelta(minutes=25))
    asyncio.run(_settle(core, 2))
    assert core.market.matches[first].status is MatchStatus.VOIDED
    assert ("void", first) in core.paypal.calls
    second = [m for m in core.market.by_lot[lot] if m != first]
    assert len(second) == 1 and core.market.matches[second[0]].fill.unit_cents <= 600


def test_threshold_burst_takes_forty_holds_and_one_payout(core):
    from criee.settlement.worker import SettlementWorker
    core.worker = SettlementWorker(core)
    farm = core.create_account("Ferme", ["seller"]).id
    now = utcnow()
    lot = core.list_lot(farm, lot_terms(ClearingMode.THRESHOLD, qty=40, start=2400, threshold=40,
                                        minutes=10 * 24 * 60, category=Category.GROCERY_BOX,
                                        opens=now - timedelta(hours=1)), LotListing(name="Colis 10 kg"))
    buyers = [_buyer(core) for _ in range(40)]
    for i, b in enumerate(buyers):
        core.sign_wish(b, wish_terms(limit=2500, category=Category.GROCERY_BOX, expires=now + timedelta(days=20)),
                       {"kind": "test"}, at=now - timedelta(minutes=50 - i))
    asyncio.run(_settle(core, 8))
    [burst] = core.book.bursts
    assert burst["holds"] == 40 and burst["authorized"] == 40
    statuses = {core.market.matches[m].status for m in core.market.by_lot[lot]}
    assert statuses == {MatchStatus.CAPTURED}
    [p] = core.book.payouts.values()
    assert p.gross == 40 * 2400 and len(p.match_ids) == 40


def test_declined_hold_is_replaced_from_the_waitlist(tmp_path):
    from criee.settlement.worker import SettlementWorker
    core = Core(make_config(tmp_path), FakePayPal())
    core.worker = SettlementWorker(core)
    farm = core.create_account("Ferme", ["seller"]).id
    now = utcnow()
    lot = core.list_lot(farm, lot_terms(ClearingMode.THRESHOLD, qty=5, start=2400, threshold=5,
                                        minutes=10 * 24 * 60, category=Category.GROCERY_BOX,
                                        opens=now - timedelta(hours=1)), LotListing(name="Colis"))
    buyers = [_buyer(core) for _ in range(6)]
    for i, b in enumerate(buyers):
        core.sign_wish(b, wish_terms(limit=2500, category=Category.GROCERY_BOX, expires=now + timedelta(days=20)),
                       {"kind": "test"}, at=now - timedelta(minutes=50 - i))
    first = sorted(core.market.by_lot[lot])[0]
    core.paypal.decline.add(first)
    asyncio.run(_settle(core, 10))
    st = [core.market.matches[m].status for m in core.market.by_lot[lot]]
    assert st.count(MatchStatus.CAPTURED) == 5 and st.count(MatchStatus.FAILED) == 1


def test_refund_after_payout_is_netted_from_the_next_payout(core):
    from criee.settlement.worker import SettlementWorker
    core.worker = SettlementWorker(core)
    seller = core.create_account("Boulangerie", ["seller"]).id
    now = utcnow()
    mids = []
    for limit in (700, 690):
        b = _buyer(core)
        core.sign_wish(b, wish_terms(limit=limit, expires=now + timedelta(days=5)), {"kind": "t"},
                       at=now - timedelta(minutes=5))
    lot = core.list_lot(seller, lot_terms(ClearingMode.CLOCK, qty=1, start=600, floor=200,
                                          opens=now - timedelta(minutes=1)), LotListing(name="Tarte"))
    lot2 = core.list_lot(seller, lot_terms(ClearingMode.CLOCK, qty=1, start=600, floor=200,
                                           opens=now - timedelta(minutes=1)), LotListing(name="Tarte 2"))
    asyncio.run(_settle(core))
    m1, m2 = core.market.by_lot[lot][0], core.market.by_lot[lot2][0]
    core.emit("pickup.scanned", {"match_id": m1})
    asyncio.run(_settle(core))
    asyncio.run(core.worker.refund(m1, 200, "one slice missing"))
    core.emit("pickup.scanned", {"match_id": m2})
    asyncio.run(_settle(core))
    p2 = core.book.payouts[core.book.s(m2).payout_id]
    assert p2.debit == 200 and p2.net == p2.gross - p2.fee - 200
