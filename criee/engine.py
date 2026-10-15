"""The clearing engine.

`Market` is a pure fold over ledger events plus a decision function. `next_moment(now)` returns
the events for the earliest decision that is due at or before `now`, stamped with that moment's
logical time. Calling it in a loop until it returns nothing brings the market up to `now`. The
result depends only on the events and the per-lot seeds, never on when or how often the loop runs.

Three clearing rules:
* call      uniform price at the deadline: the k highest limits win and all pay the higher of the
            floor and the highest losing limit.
* clock     the price steps down from the start price to the floor; at each tick every eligible
            wish whose limit covers the price fills at that price.
* threshold one fixed price; the lot happens only if `threshold_qty` units are committed by the
            deadline. Short windows (3 days or less) take holds at commitment, longer windows take
            every hold in one burst when the threshold is reached.
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Callable, Iterable, Optional
from zoneinfo import ZoneInfo

from .canonical import iso, parse_iso
from .domain import (Attr, BOOL_ATTRS, ClearingMode, Constraint, Fill, LotTerms, Op, WishTerms,
                     is_short_threshold)

WEEK = timedelta(days=7)
DEFAULT_HOLD = timedelta(minutes=30)
WAITLIST_SHARE = 0.10


class Skip(str, Enum):
    """Why a wish did not match a lot. Shown to the buyer as an explanation."""
    INACTIVE = "inactive"
    EXPIRED = "expired"
    NOT_YET = "not_yet"
    CATEGORY = "category"
    MODE = "mode"
    FACT_UNKNOWN = "fact_unknown"
    FACT_MISMATCH = "fact_mismatch"
    DISTANCE = "distance"
    WINDOW = "window"
    PRICE = "price"
    WEEK_COUNT = "week_count"
    WEEK_BUDGET = "week_budget"
    ALREADY_MATCHED = "already_matched"
    UNITS = "units"


class MatchStatus(str, Enum):
    PENDING = "pending"        # decided, hold not yet taken
    HELD = "held"              # PayPal authorization created
    FAILED = "failed"          # authorization refused
    CAPTURED = "captured"
    VOIDED = "voided"
    NO_SHOW = "no_show"        # pickup deadline passed, hold being voided
    REFUNDED = "refunded"


LIVE = {MatchStatus.PENDING, MatchStatus.HELD, MatchStatus.CAPTURED, MatchStatus.REFUNDED}


@dataclass
class WishState:
    id: str
    buyer_id: str
    terms: WishTerms
    signed_at: datetime
    seq: int
    status: str = "active"
    simulated: bool = False


@dataclass
class LotState:
    id: str
    seller_id: str
    terms: LotTerms
    listed_at: datetime
    seq: int
    remaining: int
    next_round: int = 0
    closed: Optional[str] = None
    closed_at: Optional[datetime] = None
    committed: list[tuple[str, int]] = field(default_factory=list)   # (wish_id, units) in commit order
    reached: bool = False
    reached_at: Optional[datetime] = None
    last_price: Optional[int] = None
    simulated: bool = False


@dataclass
class MatchState:
    fill: Fill
    at: datetime
    pickup_by: Optional[datetime]
    status: MatchStatus = MatchStatus.PENDING
    pickup_code: Optional[str] = None
    scanned: bool = False


# ----------------------------------------------------------------------------- pure helpers

def haversine_m(a_lat: float, a_lon: float, b_lat: float, b_lon: float) -> float:
    r = 6_371_000.0
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dp, dl = p2 - p1, math.radians(b_lon - a_lon)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(h)))


def constraint_holds(c: Constraint, value: Any) -> Optional[bool]:
    """None when the fact is unknown: an unknown fact never satisfies a constraint."""
    if value is None:
        return None
    if c.attr in BOOL_ATTRS:
        return value is c.value
    if c.op is Op.GTE:
        return value >= c.value
    return value <= c.value


def windows_overlap(w: WishTerms, start: datetime, end: datetime) -> bool:
    """True when [start, end) intersects one of the wish's weekly windows (local time)."""
    tz = ZoneInfo(w.timezone)
    s, e = start.astimezone(tz), end.astimezone(tz)
    day = s.replace(hour=0, minute=0, second=0, microsecond=0)
    while day < e:
        for win in w.windows:
            if day.weekday() in {d.value for d in win.days}:
                ws = day + timedelta(minutes=win.start_min)
                we = day + timedelta(minutes=win.end_min)
                if ws < e and s < we:
                    return True
        day += timedelta(days=1)
    return False


def clock_schedule(t: LotTerms) -> tuple[int, int]:
    """(decrement per tick in cents, number of ticks). The floor is reached at 80 % of the window."""
    total_ticks = max(1, int((t.closes_at - t.opens_at).total_seconds() // t.clock_step_s))
    to_floor = max(1, int(total_ticks * 0.8))
    dec = math.ceil((t.start_cents - t.floor_cents) / to_floor) if t.start_cents > t.floor_cents else 0
    return dec, total_ticks


def clock_price(t: LotTerms, round_: int) -> int:
    dec, _ = clock_schedule(t)
    return max(t.floor_cents, t.start_cents - round_ * dec)


def lottery_key(seed: str, lot_id: str, round_: int, wish_id: str) -> str:
    return hashlib.sha256(f"{seed}:{lot_id}:{round_}:{wish_id}".encode()).hexdigest()


def match_id_for(lot_id: str, wish_id: str, round_: int) -> str:
    h = hashlib.sha256(f"{lot_id}:{wish_id}:{round_}".encode()).hexdigest()
    alphabet = "0123456789abcdefghjkmnpqrstvwxyz"
    return "m_" + "".join(alphabet[int(h[i:i + 2], 16) % 32] for i in range(0, 24, 2))


def pickup_code_for(match_id: str, seed: str) -> str:
    h = int(hashlib.sha256(f"pickup:{seed}:{match_id}".encode()).hexdigest(), 16)
    return f"{h % 1_000_000:06d}"


# ----------------------------------------------------------------------------- the market

@dataclass
class Moment:
    at: datetime
    order: int
    lot_seq: int
    kind: str
    ref: str
    extra: Any = None


class Market:
    def __init__(self, seed_for: Callable[[str], str]):
        self.seed_for = seed_for
        self.wishes: dict[str, WishState] = {}
        self.lots: dict[str, LotState] = {}
        self.matches: dict[str, MatchState] = {}
        self.by_wish: dict[str, list[str]] = {}
        self.by_lot: dict[str, list[str]] = {}
        self.last_seq = 0
        self.revealed: dict[str, str] = {}

    # ------------------------------------------------------------------ fold

    def apply(self, seq: int, type_: str, body: dict[str, Any], at: datetime) -> None:
        self.last_seq = seq
        fn = getattr(self, "_on_" + type_.replace(".", "_"), None)
        if fn:
            fn(body, at, seq)

    def apply_all(self, events: Iterable[Any]) -> None:
        for ev in events:
            self.apply(ev.seq, ev.type, ev.body, ev.at)

    def _on_wish_signed(self, b: dict, at: datetime, seq: int) -> None:
        self.wishes[b["wish_id"]] = WishState(b["wish_id"], b["buyer_id"], WishTerms.model_validate(b["terms"]),
                                              at, seq, simulated=b.get("simulated", False))

    def _on_wish_paused(self, b: dict, at: datetime, seq: int) -> None:
        w = self.wishes.get(b["wish_id"])
        if w and w.status == "active":
            w.status = "paused"

    def _on_wish_resumed(self, b: dict, at: datetime, seq: int) -> None:
        w = self.wishes.get(b["wish_id"])
        if w and w.status == "paused":
            w.status = "active"

    def _on_wish_revoked(self, b: dict, at: datetime, seq: int) -> None:
        w = self.wishes.get(b["wish_id"])
        if w:
            w.status = "revoked"

    def _on_lot_listed(self, b: dict, at: datetime, seq: int) -> None:
        t = LotTerms.model_validate(b["terms"])
        self.lots[b["lot_id"]] = LotState(b["lot_id"], b["seller_id"], t, at, seq, remaining=t.quantity,
                                          simulated=b.get("simulated", False))

    def _on_lot_withdrawn(self, b: dict, at: datetime, seq: int) -> None:
        lot = self.lots.get(b["lot_id"])
        if lot and not lot.closed:
            lot.closed, lot.closed_at = "withdrawn", at

    def _on_clock_tick(self, b: dict, at: datetime, seq: int) -> None:
        lot = self.lots[b["lot_id"]]
        lot.next_round = b["round"] + 1
        lot.last_price = b["unit_cents"]

    def _on_call_cleared(self, b: dict, at: datetime, seq: int) -> None:
        lot = self.lots[b["lot_id"]]
        lot.next_round = 1
        lot.last_price = b["unit_cents"]

    def _on_match_made(self, b: dict, at: datetime, seq: int) -> None:
        f = Fill.model_validate(b["fill"])
        pickup_by = parse_iso(b["pickup_by"]) if b.get("pickup_by") else None
        self.matches[f.match_id] = MatchState(f, at, pickup_by, pickup_code=b.get("pickup_code"))
        self.by_wish.setdefault(f.wish_id, []).append(f.match_id)
        self.by_lot.setdefault(f.lot_id, []).append(f.match_id)
        self.lots[f.lot_id].remaining -= f.units

    def _return_units(self, m: MatchState) -> None:
        lot = self.lots[m.fill.lot_id]
        if not lot.closed:
            lot.remaining += m.fill.units

    def _on_settlement_authorized(self, b: dict, at: datetime, seq: int) -> None:
        m = self.matches.get(b["match_id"])
        if m and m.status is MatchStatus.PENDING:
            m.status = MatchStatus.HELD

    def _on_settlement_auth_failed(self, b: dict, at: datetime, seq: int) -> None:
        m = self.matches.get(b["match_id"])
        if m and m.status is MatchStatus.PENDING:
            m.status = MatchStatus.FAILED
            self._return_units(m)
            lot = self.lots[m.fill.lot_id]
            if lot.terms.mode is ClearingMode.THRESHOLD:
                lot.committed = [(w, u) for (w, u) in lot.committed if w != m.fill.wish_id]

    def _on_pickup_scanned(self, b: dict, at: datetime, seq: int) -> None:
        m = self.matches.get(b["match_id"])
        if m and m.status in (MatchStatus.PENDING, MatchStatus.HELD):
            m.pickup_by = None
            m.scanned = True

    def _on_pickup_no_show(self, b: dict, at: datetime, seq: int) -> None:
        m = self.matches.get(b["match_id"])
        if m and m.status in (MatchStatus.PENDING, MatchStatus.HELD):
            m.status = MatchStatus.NO_SHOW
            m.pickup_by = None
            self._return_units(m)

    def _on_settlement_captured(self, b: dict, at: datetime, seq: int) -> None:
        m = self.matches.get(b["match_id"])
        if m:
            m.status = MatchStatus.CAPTURED
            m.pickup_by = None

    def _on_settlement_voided(self, b: dict, at: datetime, seq: int) -> None:
        m = self.matches.get(b["match_id"])
        if m and m.status in (MatchStatus.PENDING, MatchStatus.HELD, MatchStatus.NO_SHOW):
            m.status = MatchStatus.VOIDED
            m.pickup_by = None

    def _on_settlement_refunded(self, b: dict, at: datetime, seq: int) -> None:
        m = self.matches.get(b["match_id"])
        if m and b.get("full"):
            m.status = MatchStatus.REFUNDED

    def _on_threshold_committed(self, b: dict, at: datetime, seq: int) -> None:
        self.lots[b["lot_id"]].committed.append((b["wish_id"], b["units"]))

    def _on_threshold_reached(self, b: dict, at: datetime, seq: int) -> None:
        lot = self.lots[b["lot_id"]]
        lot.reached, lot.reached_at = True, at

    def _on_lot_closed(self, b: dict, at: datetime, seq: int) -> None:
        lot = self.lots[b["lot_id"]]
        lot.closed, lot.closed_at = b["reason"], at
        if b.get("seed"):
            self.revealed[lot.id] = b["seed"]

    # ------------------------------------------------------------------ queries

    def seed(self, lot_id: str) -> str:
        return self.revealed.get(lot_id) or self.seed_for(lot_id)

    def usage(self, wish_id: str, at: datetime) -> tuple[int, int]:
        """Live matches and cents committed by a wish in the 7 days up to `at`."""
        n = cents = 0
        for mid in self.by_wish.get(wish_id, []):
            m = self.matches[mid]
            if m.status in LIVE and at - WEEK < m.at <= at:
                n += 1
                cents += m.fill.units * m.fill.unit_cents
        return n, cents

    def check(self, w: WishState, lot: LotState, at: datetime, unit_cents: int) -> Optional[Skip]:
        """None when the wish may take `w.terms.units` units of the lot at `unit_cents`."""
        wt, lt = w.terms, lot.terms
        if w.status != "active":
            return Skip.INACTIVE
        if wt.expires_at <= at:
            return Skip.EXPIRED
        if w.signed_at > at:
            return Skip.NOT_YET
        if wt.category is not lt.category:
            return Skip.CATEGORY
        if lt.mode not in wt.modes:
            return Skip.MODE
        for c in wt.constraints:
            ok = constraint_holds(c, lt.fact(c.attr))
            if ok is None:
                return Skip.FACT_UNKNOWN
            if not ok:
                return Skip.FACT_MISMATCH
        if haversine_m(wt.pickup.lat, wt.pickup.lon, lt.pickup.lat, lt.pickup.lon) > wt.radius_m:
            return Skip.DISTANCE
        if not windows_overlap(wt, lt.pickup_from, lt.pickup_until):
            return Skip.WINDOW
        if wt.limit_cents < unit_cents:
            return Skip.PRICE
        if any(self.matches[mid].fill.lot_id == lot.id for mid in self.by_wish.get(w.id, [])):
            return Skip.ALREADY_MATCHED
        n, spent = self.usage(w.id, at)
        if n + 1 > wt.max_matches_per_week:
            return Skip.WEEK_COUNT
        if spent + wt.units * unit_cents > wt.budget_cents_per_week:
            return Skip.WEEK_BUDGET
        return None

    def explain(self, wish_id: str, lot_id: str, at: datetime) -> Optional[Skip]:
        lot = self.lots[lot_id]
        price = lot.last_price if lot.last_price is not None else lot.terms.floor_cents
        return self.check(self.wishes[wish_id], lot, at, price)

    def is_committed(self, lot: LotState, wish_id: str) -> bool:
        return any(w == wish_id for w, _ in lot.committed)

    # ------------------------------------------------------------------ decisions

    def _moments(self, now: datetime) -> list[Moment]:
        out: list[Moment] = []
        for lot in self.lots.values():
            t = lot.terms
            if lot.closed:
                continue
            if t.mode is ClearingMode.CLOCK:
                _, total = clock_schedule(t)
                if lot.next_round < total:
                    tick_at = t.opens_at + timedelta(seconds=lot.next_round * t.clock_step_s)
                    out.append(Moment(tick_at, 2, lot.seq, "tick", lot.id, lot.next_round))
                out.append(Moment(t.closes_at, 5, lot.seq, "close", lot.id))
            elif t.mode is ClearingMode.CALL:
                out.append(Moment(t.closes_at, 3, lot.seq, "call", lot.id))
            else:
                if not self._threshold_full(lot):
                    for w in self.wishes.values():
                        if w.status == "active" and not self.is_committed(lot, w.id):
                            when = max(w.signed_at, t.opens_at, lot.listed_at)
                            if when < t.closes_at and self.check(w, lot, when, t.start_cents) is None:
                                out.append(Moment(when, 1, lot.seq, "commit", lot.id, w.id))
                if lot.reached and self._threshold_short_of_fills(lot):
                    out.append(Moment(lot.reached_at or t.opens_at, 4, lot.seq, "refill", lot.id))
                out.append(Moment(t.closes_at, 5, lot.seq, "close", lot.id))
        for m in self.matches.values():
            if m.pickup_by is not None and m.status in (MatchStatus.PENDING, MatchStatus.HELD):
                out.append(Moment(m.pickup_by, 0, self.lots[m.fill.lot_id].seq, "noshow", m.fill.match_id))
        return [m for m in out if m.at <= now]

    def next_moment(self, now: datetime) -> list[tuple[str, dict[str, Any], datetime]]:
        due = self._moments(now)
        if not due:
            return []
        mo = min(due, key=lambda m: (m.at, m.order, m.lot_seq, m.ref, str(m.extra)))
        return getattr(self, "_do_" + mo.kind)(mo)

    # each _do_* returns events; it never mutates state (the caller applies the events)

    def _fill_event(self, lot: LotState, wish: WishState, units: int, unit_cents: int, round_: int,
                    at: datetime) -> tuple[str, dict[str, Any], datetime]:
        mid = match_id_for(lot.id, wish.id, round_)
        fill = Fill(match_id=mid, wish_id=wish.id, lot_id=lot.id, units=units, unit_cents=unit_cents,
                    rule=lot.terms.mode, round=round_)
        t = lot.terms
        pickup_by = None
        if t.mode is ClearingMode.CLOCK:
            pickup_by = min(t.pickup_until, max(at, t.pickup_from) + DEFAULT_HOLD)
        elif t.mode is ClearingMode.CALL:
            pickup_by = t.pickup_until
        body = {"fill": fill.model_dump(mode="json"), "buyer_id": wish.buyer_id, "seller_id": lot.seller_id,
                "pickup_by": iso(pickup_by) if pickup_by else None,
                "pickup_code": pickup_code_for(mid, self.seed(lot.id)),
                "simulated": wish.simulated}
        return ("match.made", body, at)

    def _eligible(self, lot: LotState, at: datetime, unit_cents: int) -> list[WishState]:
        return [w for w in self.wishes.values() if self.check(w, lot, at, unit_cents) is None]

    def _do_tick(self, mo: Moment) -> list:
        lot = self.lots[mo.ref]
        k = mo.extra
        price = clock_price(lot.terms, k)
        evs: list = [("clock.tick", {"lot_id": lot.id, "round": k, "unit_cents": price}, mo.at)]
        seed = self.seed(lot.id)
        cands = sorted(self._eligible(lot, mo.at, price),
                       key=lambda w: (-w.terms.limit_cents, lottery_key(seed, lot.id, k, w.id)))
        left = lot.remaining
        for w in cands:
            if left <= 0:
                break
            if w.terms.units <= left:
                evs.append(self._fill_event(lot, w, w.terms.units, price, k, mo.at))
                left -= w.terms.units
        return evs

    def _do_call(self, mo: Moment) -> list:
        lot = self.lots[mo.ref]
        t = lot.terms
        seed = self.seed(lot.id)
        bids = sorted(self._eligible(lot, mo.at, t.floor_cents),
                      key=lambda w: (-w.terms.limit_cents, lottery_key(seed, lot.id, 0, w.id)))
        winners, left, losing = [], lot.remaining, None
        for w in bids:
            if w.terms.units <= left:
                winners.append(w)
                left -= w.terms.units
            elif losing is None:
                losing = w.terms.limit_cents
        price = max(t.floor_cents, losing if losing is not None else 0)
        winners = [w for w in winners if w.terms.limit_cents >= price]
        evs: list = [("call.cleared", {"lot_id": lot.id, "unit_cents": price, "bidders": len(bids),
                                       "winners": len(winners)}, mo.at)]
        evs += [self._fill_event(lot, w, w.terms.units, price, 0, mo.at) for w in winners]
        sold = sum(w.terms.units for w in winners)
        reason = "sold_out" if sold >= lot.remaining else "deadline"
        evs.append(("lot.closed", {"lot_id": lot.id, "reason": reason, "seed": seed,
                                   "unsold": lot.remaining - sold}, mo.at))
        return evs

    def _threshold_cap(self, lot: LotState) -> int:
        q = lot.terms.threshold_qty or lot.terms.quantity
        return lot.terms.quantity + math.ceil(q * WAITLIST_SHARE)

    def _threshold_full(self, lot: LotState) -> bool:
        return sum(u for _, u in lot.committed) >= self._threshold_cap(lot)

    def _filled_wishes(self, lot: LotState) -> set[str]:
        return {self.matches[mid].fill.wish_id for mid in self.by_lot.get(lot.id, [])
                if self.matches[mid].status in LIVE}

    def _threshold_short_of_fills(self, lot: LotState) -> bool:
        if lot.remaining <= 0:
            return False
        matched = {self.matches[mid].fill.wish_id for mid in self.by_lot.get(lot.id, [])}
        return any(w not in matched and u <= lot.remaining for w, u in lot.committed
                   if self.wishes[w].status == "active")

    def _do_commit(self, mo: Moment) -> list:
        lot = self.lots[mo.ref]
        w = self.wishes[mo.extra]
        t = lot.terms
        order = len(lot.committed)
        evs: list = [("threshold.committed", {"lot_id": lot.id, "wish_id": w.id, "units": w.terms.units,
                                              "order": order}, mo.at)]
        committed_units = sum(u for wid, u in lot.committed if self.wishes[wid].status == "active") + w.terms.units
        short = is_short_threshold(t)
        if lot.reached or short:
            if w.terms.units <= lot.remaining:
                evs.append(self._fill_event(lot, w, w.terms.units, t.start_cents, order, mo.at))
        if not lot.reached and committed_units >= (t.threshold_qty or t.quantity):
            evs.append(("threshold.reached", {"lot_id": lot.id, "committed": committed_units,
                                              "burst": not short}, mo.at))
            if not short:
                left = lot.remaining
                for i, (wid, units) in enumerate(lot.committed + [(w.id, w.terms.units)]):
                    if units <= left and self.wishes[wid].status == "active":
                        evs.append(self._fill_event(lot, self.wishes[wid], units, t.start_cents, i, mo.at))
                        left -= units
        return evs

    def _do_refill(self, mo: Moment) -> list:
        lot = self.lots[mo.ref]
        filled = self._filled_wishes(lot)
        left = lot.remaining
        evs: list = []
        matched = {self.matches[mid].fill.wish_id for mid in self.by_lot.get(lot.id, [])}
        for i, (wid, units) in enumerate(lot.committed):
            w = self.wishes[wid]
            if wid in filled or wid in matched or w.status != "active" or units > left:
                continue
            evs.append(self._fill_event(lot, w, units, lot.terms.start_cents, i, mo.at))
            left -= units
            if left <= 0:
                break
        return evs

    def _do_close(self, mo: Moment) -> list:
        lot = self.lots[mo.ref]
        t = lot.terms
        if t.mode is ClearingMode.THRESHOLD:
            reason = "threshold_met" if lot.reached else "threshold_failed"
        else:
            reason = "sold_out" if lot.remaining <= 0 else "deadline"
        return [("lot.closed", {"lot_id": lot.id, "reason": reason, "seed": self.seed(lot.id),
                                "unsold": max(0, lot.remaining)}, mo.at)]

    def _do_noshow(self, mo: Moment) -> list:
        return [("pickup.no_show", {"match_id": mo.ref}, mo.at)]


def run_until(market: Market, now: datetime, append: Callable[[list], list], limit: int = 100_000) -> int:
    """Bring the market up to `now`. `append` persists events and returns them as ledger events."""
    n = 0
    while n < limit:
        evs = market.next_moment(now)
        if not evs:
            break
        for ev in append(evs):
            market.apply(ev.seq, ev.type, ev.body, ev.at)
        n += len(evs)
    return n
