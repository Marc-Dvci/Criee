"""Property tests for the clearing engine's invariants (plan section 5.3)."""
from __future__ import annotations

import hashlib
import typing
from datetime import timedelta

from hypothesis import HealthCheck, event, given, settings, strategies as st
from pydantic import BaseModel

from criee.domain import (Attr, Category, ClearingMode, Constraint, Fill, GeoPoint, LotTerms, Op, WishTerms)
from criee.engine import LIVE, Market, MatchStatus, constraint_holds, haversine_m, windows_overlap
from helpers import T0, Harness, fact, lot_terms, wish_terms

MODES = [ClearingMode.CALL, ClearingMode.CLOCK, ClearingMode.THRESHOLD]
CATS = [Category.BREAD, Category.PASTRY]
SETTINGS = settings(max_examples=int(__import__("os").environ.get("CRIEE_HYPOTHESIS_EXAMPLES", "150")),
                    deadline=None, suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large])


def _where(dx: int, dy: int) -> GeoPoint:
    return GeoPoint(lat=48.58 + dy / 1000, lon=7.75 + dx / 1000)


wish_st = st.fixed_dictionaries({
    "limit": st.integers(350, 900), "units": st.integers(1, 3), "cat": st.sampled_from(CATS),
    "gf": st.sampled_from([None, None, None, True, False]), "radius": st.sampled_from([1500, 5000, 20000]),
    "dx": st.integers(-10, 10), "dy": st.integers(-10, 10), "per_week": st.integers(1, 4),
    "modes": st.lists(st.sampled_from(MODES), min_size=1, max_size=3, unique=True),
    "at_min": st.integers(-60, 200), "revoke_at": st.one_of(st.none(), st.none(), st.integers(0, 360)),
})

lot_st = st.fixed_dictionaries({
    "mode": st.sampled_from(MODES), "qty": st.integers(1, 12), "start": st.integers(300, 700),
    "drop": st.integers(0, 300), "minutes": st.integers(20, 180), "cat": st.sampled_from(CATS),
    "gf": st.sampled_from([None, True, False]), "open_min": st.integers(0, 120),
    "threshold": st.integers(2, 6), "long": st.booleans(),
})


def _build(h: Harness, wishes: list[dict], lots: list[dict]):
    actions = []
    for i, w in enumerate(wishes):
        cons = [Constraint(attr=Attr.GLUTEN_FREE, op=Op.IS, value=w["gf"])] if w["gf"] is not None else []
        terms = wish_terms(limit=w["limit"], units=w["units"], category=w["cat"], constraints=cons,
                           radius=w["radius"], where=_where(w["dx"], w["dy"]), per_week=w["per_week"],
                           modes=w["modes"])
        actions.append((T0 + timedelta(minutes=w["at_min"]), 0, i, "wish", terms))
        if w["revoke_at"] is not None:
            at = T0 + timedelta(minutes=max(w["revoke_at"], w["at_min"]), seconds=17)
            actions.append((at, 2, i, "revoke", None))
    for j, l in enumerate(lots):
        mode = l["mode"]
        qty = l["qty"]
        if mode is ClearingMode.THRESHOLD and qty < 2:
            continue
        facts = [fact(Attr.GLUTEN_FREE, l["gf"])] if l["gf"] is not None else []
        minutes = l["minutes"] if not (mode is ClearingMode.THRESHOLD and l["long"]) else 5 * 24 * 60
        terms = lot_terms(mode, qty=qty, start=l["start"], floor=max(50, l["start"] - l["drop"]),
                          minutes=minutes, step_s=60, facts=facts, category=l["cat"],
                          threshold=min(l["threshold"], qty) if mode is ClearingMode.THRESHOLD and qty >= 2 else None,
                          opens=T0 + timedelta(minutes=l["open_min"]))
        actions.append((T0 + timedelta(minutes=l["open_min"]), 1, j, "lot", terms))
    return sorted(actions, key=lambda a: (a[0], a[1], a[2]))


def _decide(match_id: str, salt: str, pct: int) -> bool:
    return int(hashlib.sha256(f"{salt}:{match_id}".encode()).hexdigest(), 16) % 100 < pct


def _settle(h: Harness, at, fail_pct: int, scan_pct: int) -> None:
    """External world at a fixed action time: PayPal answers holds, some buyers turn up."""
    for m in sorted(h.market.matches.values(), key=lambda m: m.fill.match_id):
        mid = m.fill.match_id
        if m.status is MatchStatus.PENDING:
            if _decide(mid, "fail", fail_pct):
                h.emit("settlement.auth_failed", {"match_id": mid, "reason": "DECLINED"}, at)
            else:
                h.emit("settlement.authorized", {"match_id": mid, "order_id": "o", "authorization_id": "a"}, at)
        elif m.status is MatchStatus.HELD and m.pickup_by is not None and _decide(mid, "scan", scan_pct):
            h.emit("pickup.scanned", {"match_id": mid}, at)


def _simulate(wishes, lots, fail_pct, scan_pct, extra_runs=()):
    h = Harness(seed="prop")
    wids: dict[int, str] = {}
    actions = _build(h, wishes, lots)
    extra = sorted(T0 + timedelta(minutes=m) for m in extra_runs)
    for at, _, idx, kind, terms in actions:
        while extra and extra[0] < at:
            h.run(extra.pop(0))
        h.run(at)
        if kind == "wish":
            wids[idx] = h.wish(terms, at=at)
        elif kind == "lot":
            h.lot(terms, at=at)
        elif kind == "revoke":
            h.emit("wish.revoked", {"wish_id": wids[idx]}, at)
        _settle(h, at, fail_pct, scan_pct)
    end = T0 + timedelta(days=6)
    for at in extra:
        h.run(at)
    h.run(end)
    _settle(h, end, fail_pct, scan_pct)
    h.run(end + timedelta(days=1))
    return h


market_st = dict(wishes=st.lists(wish_st, min_size=5, max_size=20), lots=st.lists(lot_st, min_size=2, max_size=5),
                 fail_pct=st.sampled_from([0, 10, 40]), scan_pct=st.sampled_from([0, 50, 100]))


@SETTINGS
@given(**market_st)
def test_units_never_exceed_quantity(wishes, lots, fail_pct, scan_pct):
    h = _simulate(wishes, lots, fail_pct, scan_pct)
    statuses = {m.status.value for m in h.market.matches.values()}
    event(f"matches: {'none' if not h.market.matches else 'some'}")
    for st_ in ("captured", "no_show", "failed", "held"):
        event(f"has {st_}: {st_ in statuses}")
    for lot in h.market.lots.values():
        live = sum(m.fill.units for m in h.market.matches.values()
                   if m.fill.lot_id == lot.id and m.status in LIVE)
        assert live <= lot.terms.quantity
        assert lot.remaining >= 0 or lot.closed


@SETTINGS
@given(**market_st)
def test_every_match_is_inside_its_signed_wish(wishes, lots, fail_pct, scan_pct):
    h = _simulate(wishes, lots, fail_pct, scan_pct)
    mk = h.market
    for m in mk.matches.values():
        w, lot = mk.wishes[m.fill.wish_id], mk.lots[m.fill.lot_id]
        wt, lt = w.terms, lot.terms
        assert m.fill.unit_cents <= wt.limit_cents
        assert m.fill.unit_cents >= lt.floor_cents
        assert m.fill.units == wt.units
        assert wt.category is lt.category and lt.mode in wt.modes
        for c in wt.constraints:
            assert constraint_holds(c, lt.fact(c.attr)) is True
        assert haversine_m(wt.pickup.lat, wt.pickup.lon, lt.pickup.lat, lt.pickup.lon) <= wt.radius_m
        assert windows_overlap(wt, lt.pickup_from, lt.pickup_until)
        assert w.signed_at <= m.at < wt.expires_at
        prior = [x for x in mk.matches.values() if x.fill.wish_id == w.id and x.status in LIVE
                 and m.at - timedelta(days=7) < x.at <= m.at]
        if m.status in LIVE:
            assert len(prior) <= wt.max_matches_per_week
            assert sum(x.fill.units * x.fill.unit_cents for x in prior) <= wt.budget_cents_per_week


@SETTINGS
@given(**market_st, extra_runs=st.lists(st.integers(0, 600), max_size=25))
def test_outcome_does_not_depend_on_scheduler_wakeups(wishes, lots, fail_pct, scan_pct, extra_runs):
    a = _simulate(wishes, lots, fail_pct, scan_pct)
    b = _simulate(wishes, lots, fail_pct, scan_pct, extra_runs=extra_runs)
    key = lambda h: sorted((m.fill.model_dump_json(), m.status.value) for m in h.market.matches.values())
    assert key(a) == key(b)


@SETTINGS
@given(**market_st)
def test_replaying_the_ledger_reproduces_state(wishes, lots, fail_pct, scan_pct):
    h = _simulate(wishes, lots, fail_pct, scan_pct)
    fresh = Market(h.market.seed_for)
    fresh.apply_all(h.ledger.events())
    assert {k: (v.fill, v.status) for k, v in fresh.matches.items()} == \
           {k: (v.fill, v.status) for k, v in h.market.matches.items()}
    assert {k: (v.remaining, v.closed) for k, v in fresh.lots.items()} == \
           {k: (v.remaining, v.closed) for k, v in h.market.lots.items()}
    assert h.ledger.verify()["ok"]


@SETTINGS
@given(**market_st)
def test_revoked_wish_never_matches_after_revocation(wishes, lots, fail_pct, scan_pct):
    h = _simulate(wishes, lots, fail_pct, scan_pct)
    revoked_at = {ev.body["wish_id"]: ev.at for ev in h.ledger.events(types=["wish.revoked"])}
    for m in h.market.matches.values():
        if m.fill.wish_id in revoked_at:
            assert m.at <= revoked_at[m.fill.wish_id]


@SETTINGS
@given(**market_st)
def test_every_hold_ends_settled(wishes, lots, fail_pct, scan_pct):
    """With the settlement policy applied, no match is left pending once the market has closed."""
    from criee.settlement.policy import settlement_actions
    h = _simulate(wishes, lots, fail_pct, scan_pct)
    end = T0 + timedelta(days=8)
    for _ in range(5):
        for act in settlement_actions(h.market, end):
            if act.kind == "capture":
                h.emit("settlement.captured", {"match_id": act.match_id, "capture_id": "c"}, end)
            elif act.kind == "void":
                h.emit("settlement.voided", {"match_id": act.match_id}, end)
            elif act.kind == "authorize":
                h.emit("settlement.authorized", {"match_id": act.match_id, "order_id": "o",
                                                 "authorization_id": "a"}, end)
        h.run(end)
    open_ = [m for m in h.market.matches.values() if m.status in (MatchStatus.PENDING, MatchStatus.HELD,
                                                                   MatchStatus.NO_SHOW)]
    assert open_ == []


# ----------------------------------------------------------------------------- invariant 6

FREE_TEXT_OK = {"timezone"}  # a Literal, not free text


def _walk(model: type[BaseModel], path: str = ""):
    for name, field in model.model_fields.items():
        yield f"{path}{name}", field.annotation, field.metadata


def _string_fields(model: type, path: str = "", seen=None) -> list[str]:
    """Every field that would accept arbitrary text. Enums, Literals and pattern-checked ids pass."""
    import enum
    seen = seen or set()
    bad = []
    if model in seen:
        return bad
    seen.add(model)
    for name, field in model.model_fields.items():
        bad += _check_type(field.annotation, field.metadata, f"{path}{name}", seen)
    return bad


def _check_type(tp, metadata, path, seen) -> list[str]:
    import enum
    origin = typing.get_origin(tp)
    args = typing.get_args(tp)
    if origin is typing.Annotated:
        return _check_type(args[0], list(args[1:]) + list(metadata), path, seen)
    if origin in (typing.Union, tuple, list) or (origin is not None and args):
        if origin is typing.Literal:
            return []
        out = []
        for a in args:
            if a is not Ellipsis:
                out += _check_type(a, metadata, path, seen)
        return out
    if tp is str:
        has_pattern = any(getattr(m, "pattern", None) for m in metadata)
        return [] if has_pattern or path.split(".")[-1] in FREE_TEXT_OK else [path]
    if isinstance(tp, type) and issubclass(tp, BaseModel):
        return _string_fields(tp, path + ".", seen)
    if isinstance(tp, type) and issubclass(tp, enum.Enum):
        return []
    return []


def test_engine_inputs_have_no_free_text_field():
    for model in (WishTerms, LotTerms, Fill):
        assert _string_fields(model) == [], model.__name__
