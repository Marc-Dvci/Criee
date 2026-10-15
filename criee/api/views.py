"""JSON views over the market. Nothing here mutates state."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
from typing import Any, Optional

from ..canonical import iso
from ..core import Core
from ..domain import ClearingMode, money
from ..engine import LIVE, MatchStatus, Skip, clock_price, clock_schedule, haversine_m

SKIP_TEXT = {
    Skip.CATEGORY: "different kind of item", Skip.MODE: "a clearing mode you excluded",
    Skip.FACT_UNKNOWN: "a fact you asked for is not shown on the label", Skip.FACT_MISMATCH: "does not meet your conditions",
    Skip.DISTANCE: "too far", Skip.WINDOW: "pickup hours do not fit", Skip.PRICE: "still above your price",
    Skip.WEEK_COUNT: "weekly limit reached", Skip.WEEK_BUDGET: "weekly budget reached",
    Skip.ALREADY_MATCHED: "already matched", Skip.INACTIVE: "paused", Skip.EXPIRED: "expired", Skip.NOT_YET: "too early",
    Skip.UNITS: "not enough left",
}


def shop_of(core: Core, seller_id: str) -> dict:
    acc = core.accounts.get(seller_id)
    shop = (acc.shop if acc else None) or {}
    return {"name": shop.get("name") or (acc.handle if acc else "Seller"), "place": shop.get("place"),
            "lat": shop.get("lat"), "lon": shop.get("lon")}


def price_now(core: Core, lot_id: str, now: datetime) -> int:
    lot = core.market.lots[lot_id]
    t = lot.terms
    if t.mode is ClearingMode.CLOCK:
        return lot.last_price if lot.last_price is not None else t.start_cents
    if t.mode is ClearingMode.CALL and lot.last_price is not None:
        return lot.last_price
    return t.start_cents


def lot_status(core: Core, lot_id: str, now: datetime) -> str:
    lot = core.market.lots[lot_id]
    if lot.closed:
        return lot.closed
    if now < lot.terms.opens_at:
        return "upcoming"
    if lot.terms.mode is ClearingMode.THRESHOLD and lot.reached:
        return "threshold_met"
    if lot.remaining <= 0:
        return "sold_out"
    return "open"


def lot_view(core: Core, lot_id: str, now: datetime, detail: bool = False) -> dict[str, Any]:
    mk = core.market
    lot = mk.lots[lot_id]
    t = lot.terms
    listing = core.listings.get(lot_id)
    mids = mk.by_lot.get(lot_id, [])
    live = [mk.matches[m] for m in mids if mk.matches[m].status in LIVE]
    sold = sum(m.fill.units for m in live)
    price = price_now(core, lot_id, now)
    waiting = 0
    if not lot.closed:
        for w in mk.wishes.values():
            why = mk.check(w, lot, now, price)
            if why is None or why is Skip.PRICE:
                waiting += 1
    out: dict[str, Any] = {
        "id": lot_id, "name": listing.name if listing else "Lot", "name_fr": listing.name_fr if listing else None,
        "unit": listing.unit_label if listing else "item", "category": t.category.value, "mode": t.mode.value,
        "shop": shop_of(core, lot.seller_id), "seller_id": lot.seller_id, "quantity": t.quantity,
        "remaining": max(0, lot.remaining), "sold": sold, "price": price, "start": t.start_cents,
        "floor": t.floor_cents, "opens_at": iso(t.opens_at), "closes_at": iso(t.closes_at),
        "pickup_from": iso(t.pickup_from), "pickup_until": iso(t.pickup_until), "status": lot_status(core, lot_id, now),
        "waiting": waiting, "simulated": lot.simulated,
        "facts": [{"attr": f.attr.value, "value": f.value, "evidence": f.evidence} for f in t.facts],
        "image_id": listing.image_id if listing else None,
        "crops": [c.model_dump(mode="json") for c in listing.crops] if listing else [],
    }
    if t.mode is ClearingMode.CLOCK:
        dec, total = clock_schedule(t)
        nxt = t.opens_at + timedelta(seconds=lot.next_round * t.clock_step_s)
        out["clock"] = {"round": lot.next_round - 1, "rounds": total, "step_s": t.clock_step_s, "step_cents": dec,
                        "next_at": iso(nxt) if lot.next_round < total and not lot.closed else None,
                        "next_price": clock_price(t, lot.next_round) if lot.next_round < total else None}
    if t.mode is ClearingMode.THRESHOLD:
        committed = sum(u for w, u in lot.committed if mk.wishes[w].status == "active")
        out["threshold"] = {"target": t.threshold_qty, "committed": committed, "reached": lot.reached,
                            "burst": next((b for b in core.book.bursts if b["lot_id"] == lot_id), None)}
    if detail:
        out["matches"] = [match_view(core, m, for_buyer=False) for m in mids]
    return out


def match_view(core: Core, mid: str, for_buyer: bool) -> dict[str, Any]:
    mk = core.market
    m = mk.matches[mid]
    lot = mk.lots[m.fill.lot_id]
    listing = core.listings.get(lot.id)
    s = core.book.settle.get(mid)
    out = {"id": mid, "lot_id": lot.id, "wish_id": m.fill.wish_id, "name": listing.name if listing else "Lot",
           "unit": listing.unit_label if listing else "item", "shop": shop_of(core, lot.seller_id),
           "units": m.fill.units, "unit_cents": m.fill.unit_cents, "total": m.fill.units * m.fill.unit_cents,
           "rule": m.fill.rule.value, "round": m.fill.round, "at": iso(m.at), "status": m.status.value,
           "pickup_by": iso(m.pickup_by) if m.pickup_by else None, "pickup_from": iso(lot.terms.pickup_from),
           "pickup_until": iso(lot.terms.pickup_until), "scanned": m.scanned,
           "simulated": core.market.wishes[m.fill.wish_id].simulated,
           "paypal": {"order_id": s.order_id if s else None, "authorization_id": s.authorization_id if s else None,
                      "capture_id": s.capture_id if s else None, "refunded": s.refunded_cents if s else 0,
                      "payout_id": s.payout_id if s else None, "auth_ms": s.auth_ms if s else None}}
    if for_buyer and m.status in (MatchStatus.PENDING, MatchStatus.HELD):
        out["pickup_code"] = m.pickup_code
    return out


def wish_view(core: Core, wid: str, now: datetime) -> dict[str, Any]:
    mk = core.market
    w = mk.wishes[wid]
    meta = core.wish_meta.get(wid, {})
    mids = mk.by_wish.get(wid, [])
    reasons: Counter = Counter()
    open_lots = 0
    for lot in mk.lots.values():
        if lot.closed or now < lot.terms.opens_at or lot.terms.category is not w.terms.category:
            continue
        open_lots += 1
        why = mk.check(w, lot, now, price_now(core, lot.id, now))
        if why is not None:
            reasons[why] += 1
    n, spent = mk.usage(wid, now)
    commitments = []
    for lot in mk.lots.values():
        if lot.terms.mode is ClearingMode.THRESHOLD and mk.is_committed(lot, wid) and                 not any(mk.matches[m].fill.wish_id == wid for m in mk.by_lot.get(lot.id, [])):
            listing = core.listings.get(lot.id)
            commitments.append({"lot_id": lot.id, "name": listing.name if listing else "Group order",
                                "shop": shop_of(core, lot.seller_id)["name"], "price": lot.terms.start_cents,
                                "committed": sum(u for w2, u in lot.committed if mk.wishes[w2].status == "active"),
                                "target": lot.terms.threshold_qty, "closes_at": iso(lot.terms.closes_at),
                                "status": lot_status(core, lot.id, now)})
    return {"commitments": commitments, "id": wid, "label": meta.get("label"), "readback": meta.get("readback"), "status": w.status,
            "terms": w.terms.model_dump(mode="json"), "terms_hash": meta.get("terms_hash"),
            "signature": {"kind": (meta.get("signature") or {}).get("kind"),
                          "credential_id": (meta.get("signature") or {}).get("credential_id")},
            "signed_at": iso(w.signed_at), "matches": [match_view(core, m, for_buyer=True) for m in mids],
            "week": {"matches": n, "spent": spent, "max_matches": w.terms.max_matches_per_week,
                     "budget": w.terms.budget_cents_per_week},
            "watching": {"open_lots": open_lots,
                         "reasons": [{"code": k.value, "text": SKIP_TEXT.get(k, k.value), "lots": v}
                                     for k, v in reasons.most_common()]}}


def board(core: Core, now: datetime) -> dict[str, Any]:
    mk = core.market
    lots = sorted(mk.lots.values(), key=lambda l: (l.closed is not None, l.terms.closes_at))
    open_ = [lot_view(core, l.id, now) for l in lots if not l.closed or (l.closed_at and now - l.closed_at < timedelta(hours=3))]
    recent = sorted(mk.matches.values(), key=lambda m: m.at, reverse=True)[:30]
    week = now - timedelta(days=7)
    cleared = [m for m in mk.matches.values() if m.status in (MatchStatus.CAPTURED, MatchStatus.HELD) and m.at > week]
    return {"now": iso(now), "lots": open_[:60],
            "recent": [{"id": m.fill.match_id, "lot": core.listings[m.fill.lot_id].name if m.fill.lot_id in core.listings else "",
                        "shop": shop_of(core, mk.lots[m.fill.lot_id].seller_id)["name"], "units": m.fill.units,
                        "unit_cents": m.fill.unit_cents, "rule": m.fill.rule.value, "at": iso(m.at),
                        "status": m.status.value} for m in recent],
            "totals": {"wishes": sum(1 for w in mk.wishes.values() if w.status == "active"),
                       "lots_open": sum(1 for l in mk.lots.values() if not l.closed),
                       "units_week": sum(m.fill.units for m in cleared),
                       "value_week": sum(m.fill.units * m.fill.unit_cents for m in cleared),
                       "ledger_events": core.ledger.head[0]}}


def nearby_wishes(core: Core, lat: float, lon: float, now: datetime) -> list[dict]:
    """The anonymised order book a seller sees: no ids, no exact locations."""
    out = []
    for w in core.market.wishes.values():
        t = w.terms
        if w.status != "active" or t.expires_at <= now:
            continue
        d = haversine_m(t.pickup.lat, t.pickup.lon, lat, lon)
        if d > t.radius_m:
            continue
        out.append({"category": t.category.value, "limit": t.limit_cents, "units": t.units,
                    "distance_band": "<500 m" if d < 500 else "<1 km" if d < 1000 else "<2 km" if d < 2000 else "2 km+",
                    "conditions": [f"{c.attr.value}{'' if c.op.value == 'is' else ' ' + c.op.value + ' ' + str(c.value)}"
                                   + ("" if c.value is not False else " = no") for c in t.constraints],
                    "modes": [m.value for m in t.modes], "per_week": t.max_matches_per_week,
                    "simulated": w.simulated})
    return out
