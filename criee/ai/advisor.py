"""The seller's view of standing demand: computed numbers, one model-written sentence.

Every figure comes from the order book. The model writes the sentence from those figures, and
`numbers_ok` rejects any sentence that contains a number outside the computed set.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional

from ..domain import Category, GeoPoint, money
from ..engine import Market, haversine_m, windows_overlap
from .provider import AI, AIUnavailable

MIN_BUYERS = 5


@dataclass
class Demand:
    category: Category
    wishes: int
    buyers: int
    curve: list[tuple[int, int]]          # (price cents, units wanted at that price or more)
    quantity: int
    best_price: Optional[int]             # highest single price that still clears `quantity`
    clear_at_best: int
    revenue_uniform: int
    revenue_clock: int                    # if each unit sells near its buyer's limit
    clock_start: Optional[int]
    clock_floor: Optional[int]
    tomorrow_wishes: int
    hidden: bool

    def numbers(self) -> dict[str, Any]:
        return {"standing_wishes": self.wishes, "distinct_buyers": self.buyers, "quantity_on_shelf": self.quantity,
                "single_price_eur": money(self.best_price) if self.best_price else None,
                "units_cleared_at_single_price": self.clear_at_best,
                "revenue_single_price_eur": money(self.revenue_uniform),
                "falling_clock_start_eur": money(self.clock_start) if self.clock_start else None,
                "falling_clock_floor_eur": money(self.clock_floor) if self.clock_floor else None,
                "revenue_falling_clock_eur": money(self.revenue_clock),
                "wishes_for_tomorrow_evening": self.tomorrow_wishes}


def demand_for(market: Market, shop: GeoPoint, category: Category, quantity: int, now: datetime,
               pickup_from: Optional[datetime] = None, pickup_until: Optional[datetime] = None) -> Demand:
    pf = pickup_from or now
    pu = pickup_until or now + timedelta(hours=2)
    limits: list[tuple[int, int]] = []
    buyers: set[str] = set()
    tomorrow = 0
    tf = (now + timedelta(days=1)).replace(hour=17, minute=0, second=0, microsecond=0)
    for w in market.wishes.values():
        t = w.terms
        if w.status != "active" or t.category is not category or t.expires_at <= now:
            continue
        if haversine_m(t.pickup.lat, t.pickup.lon, shop.lat, shop.lon) > t.radius_m:
            continue
        if windows_overlap(t, tf, tf + timedelta(hours=3)):
            tomorrow += 1
        if not windows_overlap(t, pf, pu):
            continue
        limits.append((t.limit_cents, t.units))
        buyers.add(w.buyer_id)
    limits.sort(reverse=True)
    prices = sorted({p for p, _ in limits}, reverse=True)
    curve, acc = [], 0
    for p in prices:
        acc += sum(u for lp, u in limits if lp == p)
        curve.append((p, acc))
    best, cleared, rev_u = None, 0, 0
    for p, units in sorted(curve):
        sold = min(units, quantity)
        if p * sold > rev_u:
            best, cleared, rev_u = p, sold, p * sold
    rev_clock, left = 0, quantity
    for p, u in limits:
        take = min(u, left)
        rev_clock += take * p
        left -= take
        if left <= 0:
            break
    hidden = len(buyers) < MIN_BUYERS
    floor_price = None
    if limits:
        k = min(quantity, sum(u for _, u in limits))
        acc_u, floor_price = 0, limits[-1][0]
        for p, u in limits:
            acc_u += u
            if acc_u >= k:
                floor_price = p
                break
    return Demand(category, len(limits), len(buyers), [] if hidden else curve, quantity, best, cleared, rev_u,
                  rev_clock, limits[0][0] if limits else None, floor_price, tomorrow, hidden)


NUM = re.compile(r"\d+(?:[.,]\d+)?")


def allowed_numbers(d: Demand) -> set[str]:
    out: set[str] = set()
    for v in d.numbers().values():
        if v is None:
            continue
        s = str(v)
        out |= {s, s.replace(".", ",")}
        if isinstance(v, str) and v.endswith(".00"):
            out |= {v[:-3]}
    return out


def numbers_ok(text: str, d: Demand) -> bool:
    ok = allowed_numbers(d)
    return all(n in ok or n.replace(",", ".") in ok for n in NUM.findall(text))


def template(d: Demand, item: str) -> str:
    if d.wishes == 0:
        return f"No standing wishes for {item} cover your shop at this time yet."
    if d.hidden:
        return (f"{d.wishes} standing wishes cover your shop for {item}. The demand curve appears once "
                f"{MIN_BUYERS} different buyers are waiting.")
    s = (f"{d.wishes} standing wishes from {d.buyers} buyers cover your shop for {item}. "
         f"At one price of €{money(d.best_price)} you would sell {d.clear_at_best} of {d.quantity} "
         f"for €{money(d.revenue_uniform)}")
    if d.clock_start and d.revenue_clock > d.revenue_uniform:
        s += (f"; a falling clock from €{money(d.clock_start)} to €{money(d.clock_floor)} "
              f"would bring about €{money(d.revenue_clock)}")
    return s + "."


SYSTEM = """You write one or two short sentences for a baker or grocer about tonight's standing demand.
Use only the numbers in the JSON, written exactly as given (euros with two decimals). No other numbers,
no percentages, no advice beyond what the numbers show. Plain, warm, direct. English."""


async def advise(ai: Optional[AI], d: Demand, item: str, visitor: Optional[str] = None) -> tuple[str, str]:
    if ai is None or d.hidden or d.wishes == 0:
        return template(d, item), "computed"
    schema = {"type": "object", "properties": {"sentence": {"type": "string"}}, "required": ["sentence"]}
    try:
        out = await ai.json("advice", SYSTEM, [f"Item: {item}\nNumbers: {d.numbers()}"], schema, visitor=visitor,
                            thinking="LOW")
        text = str(out.get("sentence", "")).strip()
        if text and numbers_ok(text, d) and len(text) < 400:
            return text, ai.last_source or "live"
    except AIUnavailable:
        pass
    return template(d, item), "computed"
