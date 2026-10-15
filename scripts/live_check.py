"""One sale end to end against the PayPal sandbox: hold, pickup capture, tracking, payout."""
import asyncio
import tempfile
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from criee.config import load
from criee.core import Core
from criee.domain import ClearingMode, GeoPoint, LotListing, LotTerms, Category, WishTerms, WeeklyWindow, Weekday, utcnow
from criee.settlement.paypal import PayPalClient
from criee.settlement.worker import SettlementWorker


async def main() -> None:
    cfg = replace(load(), data_dir=Path(tempfile.mkdtemp()))
    pp = PayPalClient(cfg.paypal_client_id, cfg.paypal_secret, cfg.currency)
    core = Core(cfg, pp)
    core.worker = SettlementWorker(core)
    buyer = core.create_account("Live check buyer", ["buyer"])
    core.ledger.kv_set(f"vault:{buyer.id}", cfg.demo_vault_id)
    seller = core.create_account("Live check bakery", ["seller"])
    now = utcnow().replace(microsecond=0)
    shop = GeoPoint(lat=48.5797, lon=7.7540)
    core.sign_wish(buyer.id, WishTerms(category=Category.BREAD, limit_cents=540, units=1, max_matches_per_week=3,
                   budget_cents_per_week=1500, pickup=shop, radius_m=2000,
                   windows=(WeeklyWindow(days=tuple(Weekday), start_min=0, end_min=1440),),
                   modes=tuple(ClearingMode), expires_at=now + timedelta(days=2)), {"kind": "live-check"},
                   label="sourdough loaf")
    lid = core.list_lot(seller.id, LotTerms(category=Category.BREAD, quantity=2, mode=ClearingMode.CLOCK,
                        start_cents=520, floor_cents=300, opens_at=now - timedelta(seconds=90),
                        closes_at=now + timedelta(minutes=20), pickup=shop, pickup_from=now - timedelta(minutes=1),
                        pickup_until=now + timedelta(minutes=60), clock_step_s=15),
                        LotListing(name="Pain au levain"))
    for _ in range(3):
        core.tick(); await core.worker.step()
    [mid] = core.market.by_lot[lid]
    m = core.market.matches[mid]
    s = core.book.s(mid)
    print("match", mid, "price", m.fill.unit_cents, "status", m.status.value, "order", s.order_id, "auth", s.authorization_id, f"{s.auth_ms} ms")
    core.emit("pickup.scanned", {"match_id": mid})
    for _ in range(4):
        core.tick(); await core.worker.step()
    s = core.book.s(mid)
    print("capture", s.capture_id, "tracked", s.tracked, "payout", s.payout_id)
    p = core.book.payouts[s.payout_id]
    print("payout gross", p.gross, "fee", p.fee, "net", p.net, "batch", p.batch_id)
    await asyncio.sleep(8)
    await core.worker.poll_payouts()
    print("payout status", core.book.payouts[s.payout_id].status, "| ledger", core.ledger.verify())
    await pp.aclose()

asyncio.run(main())
