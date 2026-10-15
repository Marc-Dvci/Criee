"""Builders shared by the engine tests: a market over a real in-memory ledger."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from criee.domain import (Category, ClearingMode, Fact, GeoPoint, LotTerms, Weekday, WeeklyWindow, WishTerms)
from criee.engine import Market, run_until
from criee.ledger import Ledger, Signer

T0 = datetime(2026, 11, 3, 17, 0, tzinfo=timezone.utc)   # a Tuesday, 18:00 in Strasbourg
SHOP = GeoPoint(lat=48.5839, lon=7.7455)                 # Petite France
NEAR = GeoPoint(lat=48.5800, lon=7.7500)
ALL_DAYS = tuple(Weekday)


def wish_terms(limit: int = 400, units: int = 1, category: Category = Category.BREAD, constraints=(),
               modes=(ClearingMode.CALL, ClearingMode.CLOCK, ClearingMode.THRESHOLD), radius: int = 3000,
               per_week: int = 5, budget: Optional[int] = None, where: GeoPoint = NEAR,
               windows=None, expires: Optional[datetime] = None) -> WishTerms:
    return WishTerms(category=category, constraints=tuple(constraints), limit_cents=limit, units=units,
                     max_matches_per_week=per_week, budget_cents_per_week=budget or limit * units * per_week,
                     pickup=where, radius_m=radius,
                     windows=windows or (WeeklyWindow(days=ALL_DAYS, start_min=0, end_min=1440),),
                     modes=tuple(modes), expires_at=expires or T0 + timedelta(days=30))


def lot_terms(mode: ClearingMode = ClearingMode.CLOCK, qty: int = 10, start: int = 600, floor: int = 200,
              minutes: int = 60, step_s: int = 60, facts=(), category: Category = Category.BREAD,
              threshold: Optional[int] = None, opens: datetime = T0) -> LotTerms:
    return LotTerms(category=category, facts=tuple(facts), quantity=qty, mode=mode, start_cents=start,
                    floor_cents=floor if mode is not ClearingMode.THRESHOLD else start,
                    opens_at=opens, closes_at=opens + timedelta(minutes=minutes), pickup=SHOP,
                    pickup_from=opens, pickup_until=opens + timedelta(minutes=minutes + 60),
                    threshold_qty=threshold, clock_step_s=step_s)


def fact(attr, value) -> Fact:
    return Fact(attr=attr, value=value, evidence="crop_000000")


class Harness:
    def __init__(self, seed: str = "seed-1"):
        self.ledger = Ledger(":memory:", Signer(Ed25519PrivateKey.generate()))
        self.market = Market(lambda lot_id: f"{seed}:{lot_id}")
        self._n = 0

    def _id(self, p: str) -> str:
        self._n += 1
        return f"{p}_{self._n:08d}"

    def append(self, evs: list[tuple[str, dict[str, Any], datetime]]):
        out = self.ledger.append_many(evs)
        return out

    def emit(self, type_: str, body: dict[str, Any], at: datetime):
        ev = self.ledger.append(type_, body, at)
        self.market.apply(ev.seq, ev.type, ev.body, ev.at)
        return ev

    def wish(self, terms: WishTerms, at: datetime = T0 - timedelta(hours=1), buyer: Optional[str] = None) -> str:
        wid = self._id("w")
        self.emit("wish.signed", {"wish_id": wid, "buyer_id": buyer or self._id("b"),
                                  "terms": terms.model_dump(mode="json")}, at)
        return wid

    def lot(self, terms: LotTerms, at: datetime = T0) -> str:
        lid = self._id("lot")
        self.emit("lot.listed", {"lot_id": lid, "seller_id": "s_00000001", "terms": terms.model_dump(mode="json")}, at)
        return lid

    def run(self, now: datetime) -> int:
        return run_until(self.market, now, self.append)

    def fills(self, lot_id: Optional[str] = None):
        return [m.fill for m in self.market.matches.values() if lot_id is None or m.fill.lot_id == lot_id]
