"""Simulated comparison of three end-of-day policies for 12 bakeries in Strasbourg.

Every number this module produces is simulated. Policy C ("Criee", a descending clock) runs
through the real clearing engine (criee.engine.Market and run_until) over an in-memory signed
ledger. Policies A and B are plain rules written out in docs/SIMULATION.md. The three policies
share one world per seed: the same bakeries, supply, wish-holders and limits.

Usage:
    python -m criee.sim --seeds 20 --out data/sim/results.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import statistics
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .demo import NEIGHBOURHOODS
from .domain import Category, ClearingMode, GeoPoint, LotTerms, Weekday, WeeklyWindow, WishTerms
from .engine import Market, MatchStatus, haversine_m, run_until
from .ledger import Ledger, Signer

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = ROOT / "data" / "sim" / "results.json"

TZ = ZoneInfo("Europe/Paris")
WEEK_MONDAY = (2026, 10, 19)          # the simulated week runs Mon 19 Oct to Sun 25 Oct 2026
N_DAYS = 7
N_BAKERIES = 12
N_RESIDENTS = 10_000
BAKERY_SCATTER_M = 600.0
M_PER_DEG_LAT = 111_320.0
PRICE_EUR = (3.50, 5.50)              # list price of the loaf, uniform
COST_SHARE = 0.35                     # unit cost as a share of list price (recorded, not used in metrics)
LAMBDA_RANGE = (8.0, 20.0)            # leftover loaves per bakery per evening ~ Poisson(lambda)
LIMIT_SHARE_RANGE = (0.35, 0.95)      # limit = U(0.35, 0.95) x 4.50 EUR
LIMIT_REF_EUR = 4.50
RADIUS_M = (1500, 2500, 4000)
RADIUS_WEIGHTS = (0.50, 0.35, 0.15)
MAX_MATCHES_RANGE = (2, 4)
JITTER_LAT = 0.004
JITTER_LON = 0.006
WISH_WINDOW_MIN = (17 * 60, 21 * 60)  # standing wish window, every day, minutes after midnight
CLOCK_OPEN_MIN = 18 * 60 + 30
CLOCK_MINUTES = 40                    # listed 18:30, closes 19:10
PICKUP_MINUTES = 120                  # pickup 18:30 to 20:30
CLOCK_STEP_S = 60
FLOOR_SHARE = 0.30                    # clock floor = 0.30 x list price
NOSHOW_RATE = 0.08                    # policy C only
PLATFORM_FEE = 0.04                   # policy C only
A_PRICE_SHARE = 0.50                  # fixed markdown from 18:30
A_WALK_BY_P = 0.10                    # per wish-holder, per bakery in radius, per evening
B_BAG_LOAVES = 3
B_OPEN_P = 0.25                       # per wish-holder per evening
DEFAULT_ADOPTION = 0.03
ADOPTION_GRID = (0.01, 0.02, 0.03, 0.05)
SENSITIVITY_SEEDS = 10
POLICY_NAMES = {"A": "Fixed markdown at 18:30", "B": "Surprise bag", "C": "Criee clock"}

# Two-sided 95% Student-t critical values for df = 1..30 (1.96 above that).
T95 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365, 8: 2.306, 9: 2.262,
       10: 2.228, 11: 2.201, 12: 2.179, 13: 2.160, 14: 2.145, 15: 2.131, 16: 2.120, 17: 2.110,
       18: 2.101, 19: 2.093, 20: 2.086, 21: 2.080, 22: 2.074, 23: 2.069, 24: 2.064, 25: 2.060,
       26: 2.056, 27: 2.052, 28: 2.048, 29: 2.045, 30: 2.042}


@dataclass(frozen=True)
class Bakery:
    idx: int
    neighbourhood: str
    lat: float
    lon: float
    price_cents: int
    floor_cents: int
    cost_cents: int
    lam: float


@dataclass(frozen=True)
class Holder:
    """A resident with a standing bread wish. Index j is the holder's rank in the adoption ranking."""
    idx: int
    lat: float
    lon: float
    limit_cents: int
    radius_m: int
    max_matches: int


@dataclass
class World:
    seed: int
    adoption: float
    bakeries: list[Bakery]
    supply: list[list[int]]          # supply[day][bakery]
    holders: list[Holder]
    dist_m: list[list[float]]        # dist_m[holder][bakery]


@dataclass
class PolicyRun:
    name: str
    lots: list[dict]                 # one row per bakery-evening
    sales: list[dict]                # one row per sale (a bag is one sale of 3 units)
    fee: float


@dataclass
class SimResult:
    world: World
    policies: dict[str, PolicyRun]


# ----------------------------------------------------------------------------- world

def _digest(text: str) -> bytes:
    return hashlib.sha256(text.encode()).digest()


def _poisson(rng: random.Random, lam: float) -> int:
    """Knuth's multiplicative method; exact for the lambdas used here (at most 20)."""
    limit = math.exp(-lam)
    k = 0
    p = rng.random()
    while p > limit:
        k += 1
        p *= rng.random()
    return k


def build_world(seed: int, adoption: float = DEFAULT_ADOPTION, n_bakeries: int = N_BAKERIES) -> World:
    rng = random.Random(f"criee-sim/{seed}/world")
    bakeries: list[Bakery] = []
    for b in range(n_bakeries):
        name, lat0, lon0, _ = NEIGHBOURHOODS[b % len(NEIGHBOURHOODS)]
        r = BAKERY_SCATTER_M * math.sqrt(rng.random())          # uniform over the disc
        theta = 2 * math.pi * rng.random()
        lat = lat0 + r * math.cos(theta) / M_PER_DEG_LAT
        lon = lon0 + r * math.sin(theta) / (M_PER_DEG_LAT * math.cos(math.radians(lat0)))
        price = round(rng.uniform(*PRICE_EUR) * 100)
        bakeries.append(Bakery(idx=b, neighbourhood=name, lat=lat, lon=lon, price_cents=price,
                               floor_cents=round(FLOOR_SHARE * price), cost_cents=round(COST_SHARE * price),
                               lam=rng.uniform(*LAMBDA_RANGE)))
    supply = [[_poisson(rng, bk.lam) for bk in bakeries] for _ in range(N_DAYS)]

    weights = [n[3] for n in NEIGHBOURHOODS]
    residents = []
    for _ in range(N_RESIDENTS):
        _, lat0, lon0, _ = rng.choices(NEIGHBOURHOODS, weights=weights)[0]
        lat = lat0 + rng.uniform(-JITTER_LAT, JITTER_LAT)
        lon = lon0 + rng.uniform(-JITTER_LON, JITTER_LON)
        rank = rng.random()
        limit = round(rng.uniform(*LIMIT_SHARE_RANGE) * LIMIT_REF_EUR * 100)
        radius = rng.choices(RADIUS_M, weights=RADIUS_WEIGHTS)[0]
        max_matches = rng.randint(*MAX_MATCHES_RANGE)
        residents.append((rank, lat, lon, limit, radius, max_matches))
    # Adopters are the k lowest-ranked residents, so the 1% group sits inside the 2% group, and so on.
    residents.sort(key=lambda r: r[0])
    k = round(adoption * N_RESIDENTS)
    holders = [Holder(j, lat, lon, limit, radius, mm) for j, (_, lat, lon, limit, radius, mm) in
               enumerate(residents[:k])]
    dist = [[haversine_m(h.lat, h.lon, bk.lat, bk.lon) for bk in bakeries] for h in holders]
    return World(seed=seed, adoption=adoption, bakeries=bakeries, supply=supply, holders=holders, dist_m=dist)


def _is_noshow(seed: int, match_id: str) -> bool:
    h = int(hashlib.sha256(f"criee-sim/{seed}/noshow/{match_id}".encode()).hexdigest(), 16)
    return h % 10_000 < round(NOSHOW_RATE * 10_000)


# ----------------------------------------------------------------------------- row helpers

def _lot_row(policy: str, day: int, bakery: int, supplied: int, sold: int, wasted: int, revenue: int) -> dict:
    if sold + wasted != supplied:
        raise RuntimeError(f"conservation broken: {policy} day {day} bakery {bakery}: "
                           f"sold {sold} + wasted {wasted} != supplied {supplied}")
    return {"policy": policy, "day": day, "bakery": bakery, "supplied": supplied, "sold": sold,
            "wasted": wasted, "revenue_cents": revenue}


def _sale(policy: str, day: int, bakery: int, holder: int, units: int, revenue: int, limit: int,
          floor: Optional[int] = None) -> dict:
    return {"policy": policy, "day": day, "bakery": bakery, "holder": holder, "units": units,
            "revenue_cents": revenue, "limit_cents": limit, "floor_cents": floor,
            "unit_price_cents": revenue / units}


# ----------------------------------------------------------------------------- policy A

def _policy_a(world: World, seed: int) -> PolicyRun:
    rng = random.Random(f"criee-sim/{seed}/{world.adoption}/A")
    bakeries, holders, dist = world.bakeries, world.holders, world.dist_m
    nb, k = len(bakeries), len(holders)
    weekly = [0] * k
    lots: list[dict] = []
    sales: list[dict] = []
    for d in range(N_DAYS):
        supply = world.supply[d]
        price = [round(bk.price_cents * A_PRICE_SHARE) for bk in bakeries]
        passes: list[tuple[float, int, int]] = []
        for b in range(nb):
            if supply[b] <= 0:
                continue
            for j in range(k):
                if dist[j][b] <= holders[j].radius_m and rng.random() < A_WALK_BY_P:
                    passes.append((rng.random(), b, j))          # arrival time within 18:30-20:30
        passes.sort()
        remaining = list(supply)
        sold = [0] * nb
        revenue = [0] * nb
        bought: set[int] = set()
        for _, b, j in passes:
            h = holders[j]
            if j in bought or remaining[b] <= 0 or h.limit_cents < price[b] or weekly[j] >= h.max_matches:
                continue
            remaining[b] -= 1
            sold[b] += 1
            revenue[b] += price[b]
            weekly[j] += 1
            bought.add(j)
            sales.append(_sale("A", d, b, j, 1, price[b], h.limit_cents))
        for b in range(nb):
            lots.append(_lot_row("A", d, b, supply[b], sold[b], supply[b] - sold[b], revenue[b]))
    return PolicyRun("A", lots, sales, fee=0.0)


# ----------------------------------------------------------------------------- policy B

def _policy_b(world: World, seed: int) -> PolicyRun:
    rng = random.Random(f"criee-sim/{seed}/{world.adoption}/B")
    bakeries, holders, dist = world.bakeries, world.holders, world.dist_m
    nb, k = len(bakeries), len(holders)
    weekly = [0] * k
    lots: list[dict] = []
    sales: list[dict] = []
    for d in range(N_DAYS):
        supply = world.supply[d]
        bags = [s // B_BAG_LOAVES for s in supply]
        bags_sold = [0] * nb
        revenue = [0] * nb
        opened = [j for j in range(k) if rng.random() < B_OPEN_P]
        rng.shuffle(opened)                                   # first come, first served
        for j in opened:
            h = holders[j]
            if weekly[j] >= h.max_matches:
                continue
            options = [b for b in range(nb)
                       if dist[j][b] <= h.radius_m and bags[b] > 0
                       and B_BAG_LOAVES * h.limit_cents >= bakeries[b].price_cents]
            if not options:
                continue
            b = min(options, key=lambda x: (dist[j][x], x))   # nearest eligible bakery
            bags[b] -= 1
            bags_sold[b] += 1
            revenue[b] += bakeries[b].price_cents
            weekly[j] += 1
            sales.append(_sale("B", d, b, j, B_BAG_LOAVES, bakeries[b].price_cents, h.limit_cents))
        for b in range(nb):
            sold_loaves = bags_sold[b] * B_BAG_LOAVES
            wasted = bags[b] * B_BAG_LOAVES + supply[b] % B_BAG_LOAVES
            lots.append(_lot_row("B", d, b, supply[b], sold_loaves, wasted, revenue[b]))
    return PolicyRun("B", lots, sales, fee=0.0)


# ----------------------------------------------------------------------------- policy C

def _policy_c(world: World, seed: int) -> PolicyRun:
    ledger = Ledger(":memory:", Signer(Ed25519PrivateKey.from_private_bytes(_digest(f"criee-sim/{seed}/key"))))
    market = Market(lambda lot_id: f"criee-sim/{seed}/{lot_id}")
    rng = random.Random(f"criee-sim/{seed}/{world.adoption}/C")
    bakeries, holders = world.bakeries, world.holders
    nb = len(bakeries)
    try:
        def emit(type_: str, body: dict[str, Any], at: datetime) -> None:
            ev = ledger.append(type_, body, at)
            market.apply(ev.seq, ev.type, ev.body, ev.at)

        def append(evs: list) -> list:
            stored = ledger.append_many(evs)
            follow: list[tuple[str, dict[str, Any], datetime]] = []
            sold_now: dict[str, tuple[int, datetime]] = {}
            for ev in stored:
                if ev.type != "match.made":
                    continue
                fill = ev.body["fill"]
                mid, wid, lid = fill["match_id"], fill["wish_id"], fill["lot_id"]
                units_so_far = sold_now.get(lid, (0, ev.at))[0]
                sold_now[lid] = (units_so_far + fill["units"], ev.at)
                # The buyer's hold is captured at match time, so the engine never schedules its own
                # no-show return and never resells a unit during the clock.
                follow.append(("settlement.captured", {"match_id": mid}, ev.at))
                if _is_noshow(seed, mid):
                    follow.append(("settlement.refunded", {"match_id": mid, "full": True}, ev.at))
                # One loaf per resident per evening: the wish is paused until the next morning.
                follow.append(("wish.paused", {"wish_id": wid}, ev.at))
            for lid, (units, at) in sold_now.items():
                # Sold out: no tick after this can fill anything, so the sim closes the lot now
                # instead of letting the engine emit empty ticks until 19:10. Allocations are unchanged.
                if market.lots[lid].remaining - units <= 0:
                    follow.append(("lot.closed", {"lot_id": lid, "reason": "sold_out",
                                                  "seed": market.seed(lid), "unsold": 0}, at))
            return stored + (ledger.append_many(follow) if follow else [])

        for j, h in enumerate(holders):
            terms = WishTerms(
                category=Category.BREAD, constraints=(), limit_cents=h.limit_cents, units=1,
                max_matches_per_week=h.max_matches, budget_cents_per_week=h.limit_cents * h.max_matches,
                pickup=GeoPoint(lat=h.lat, lon=h.lon), radius_m=h.radius_m,
                windows=(WeeklyWindow(days=tuple(Weekday), start_min=WISH_WINDOW_MIN[0],
                                      end_min=WISH_WINDOW_MIN[1]),),
                modes=(ClearingMode.CLOCK,), expires_at=datetime(*WEEK_MONDAY, tzinfo=TZ) + timedelta(days=8))
            emit("wish.signed", {"wish_id": f"w_{j:08d}", "buyer_id": f"b_{j:08d}",
                                 "terms": terms.model_dump(mode="json")},
                 datetime(*WEEK_MONDAY, tzinfo=TZ))

        lot_meta: dict[str, tuple[int, int, int]] = {}      # lot_id -> (day, bakery, floor_cents)
        for d in range(N_DAYS):
            day0 = datetime(WEEK_MONDAY[0], WEEK_MONDAY[1], WEEK_MONDAY[2] + d, 0, 0, tzinfo=TZ)
            # Resume yesterday's pauses. A holder at the weekly cap stays paused: every match counts
            # toward the cap for the rest of the week, so the engine would refuse them anyway.
            paused = [wid for wid, w in market.wishes.items() if w.status == "paused"]
            for wid in paused:
                j = int(wid.split("_")[1])
                if len(market.by_wish.get(wid, [])) < holders[j].max_matches:
                    emit("wish.resumed", {"wish_id": wid}, day0)
            opens = datetime(WEEK_MONDAY[0], WEEK_MONDAY[1], WEEK_MONDAY[2] + d, 18, 30, tzinfo=TZ)
            closes = opens + timedelta(minutes=CLOCK_MINUTES)
            order = [b for b in range(nb) if world.supply[d][b] > 0]
            rng.shuffle(order)                                # listing order decides ties within a minute
            for b in order:
                bk = bakeries[b]
                lot_id = f"lot_{d * 100 + b:06d}"
                lot_meta[lot_id] = (d, b, bk.floor_cents)
                terms = LotTerms(
                    category=Category.BREAD, quantity=world.supply[d][b], mode=ClearingMode.CLOCK,
                    start_cents=bk.price_cents, floor_cents=bk.floor_cents, opens_at=opens, closes_at=closes,
                    pickup=GeoPoint(lat=bk.lat, lon=bk.lon), pickup_from=opens,
                    pickup_until=opens + timedelta(minutes=PICKUP_MINUTES), clock_step_s=CLOCK_STEP_S)
                emit("lot.listed", {"lot_id": lot_id, "seller_id": f"s_{b:08d}",
                                    "terms": terms.model_dump(mode="json")}, opens)
            run_until(market, closes, append)

        sold: dict[str, int] = {}
        noshow: dict[str, int] = {}
        revenue: dict[str, int] = {}
        sales: list[dict] = []
        for m in market.matches.values():
            f = m.fill
            if m.status is MatchStatus.CAPTURED:
                d, b, floor = lot_meta[f.lot_id]
                sold[f.lot_id] = sold.get(f.lot_id, 0) + f.units
                revenue[f.lot_id] = revenue.get(f.lot_id, 0) + f.units * f.unit_cents
                limit = market.wishes[f.wish_id].terms.limit_cents
                j = int(f.wish_id.split("_")[1])
                sales.append(_sale("C", d, b, j, f.units, f.units * f.unit_cents, limit, floor))
            elif m.status is MatchStatus.REFUNDED:
                noshow[f.lot_id] = noshow.get(f.lot_id, 0) + f.units
            else:
                raise RuntimeError(f"unexpected match status {m.status} after the clock closed")

        lots: list[dict] = []
        for d in range(N_DAYS):
            for b in range(nb):
                supplied = world.supply[d][b]
                if supplied == 0:
                    lots.append(_lot_row("C", d, b, 0, 0, 0, 0))
                    continue
                lot_id = f"lot_{d * 100 + b:06d}"
                remaining = market.lots[lot_id].remaining
                s, ns = sold.get(lot_id, 0), noshow.get(lot_id, 0)
                if s + ns + remaining != supplied:
                    raise RuntimeError(f"engine lot state does not add up for {lot_id}")
                lots.append(_lot_row("C", d, b, supplied, s, ns + remaining, revenue.get(lot_id, 0)))
        return PolicyRun("C", lots, sales, fee=PLATFORM_FEE)
    finally:
        ledger.close()


# ----------------------------------------------------------------------------- metrics

def _policy_metrics(pr: PolicyRun, world: World) -> dict[str, Any]:
    n_be = len(world.bakeries) * N_DAYS
    supplied = sum(r["supplied"] for r in pr.lots)
    sold = sum(r["sold"] for r in pr.lots)
    wasted = sum(r["wasted"] for r in pr.lots)
    gross = sum(r["revenue_cents"] for r in pr.lots)
    if sum(s["units"] for s in pr.sales) != sold or sum(s["revenue_cents"] for s in pr.sales) != gross:
        raise RuntimeError(f"policy {pr.name}: sale rows disagree with lot rows")
    net = gross * (1 - pr.fee)
    surplus = sum(s["units"] * s["limit_cents"] - s["revenue_cents"] for s in pr.sales)
    served = len({s["holder"] for s in pr.sales})
    n_h = len(world.holders)
    out = {
        "units_supplied_per_be": supplied / n_be,
        "units_sold_per_be": sold / n_be,
        "units_wasted_per_be": wasted / n_be,
        "seller_revenue_gross_eur_per_be": gross / 100 / n_be,
        "seller_revenue_net_eur_per_be": net / 100 / n_be,
        "mean_price_paid_eur": (gross / sold / 100) if sold else None,
        "mean_buyer_surplus_eur": (surplus / sold / 100) if sold else None,
        "share_holders_served": (served / n_h) if n_h else None,
        "units_supplied_week": supplied,
        "units_sold_week": sold,
        "units_wasted_week": wasted,
        "seller_revenue_gross_eur_week": gross / 100,
        "seller_revenue_net_eur_week": net / 100,
    }
    return {k: (round(v, 6) if isinstance(v, float) else v) for k, v in out.items()}


def simulate(seed: int, adoption: float = DEFAULT_ADOPTION, n_bakeries: int = N_BAKERIES) -> SimResult:
    world = build_world(seed, adoption, n_bakeries)
    policies = {"A": _policy_a(world, seed), "B": _policy_b(world, seed), "C": _policy_c(world, seed)}
    return SimResult(world=world, policies=policies)


def summarise(res: SimResult) -> dict[str, Any]:
    w = res.world
    return {
        "seed": w.seed,
        "adoption": w.adoption,
        "n_bakeries": len(w.bakeries),
        "n_holders": len(w.holders),
        "policies": {name: _policy_metrics(pr, w) for name, pr in res.policies.items()},
    }


def run(seed: int, adoption: float = DEFAULT_ADOPTION, n_bakeries: int = N_BAKERIES) -> dict:
    """One seed, one adoption rate. Returns simulated metrics for policies A, B and C. Deterministic."""
    return summarise(simulate(seed, adoption, n_bakeries))


# ----------------------------------------------------------------------------- statistics over seeds

def _t95(df: int) -> float:
    return T95.get(df, 1.96)


def stats(values: list[float]) -> dict[str, Any]:
    n = len(values)
    mean = statistics.fmean(values)
    if n < 2:
        return {"n": n, "mean": mean, "sd": None, "ci95_low": None, "ci95_high": None,
                "drop_one_min": None, "drop_one_max": None}
    sd = statistics.stdev(values)
    half = _t95(n - 1) * sd / math.sqrt(n)
    loo = [statistics.fmean(values[:i] + values[i + 1:]) for i in range(n)]
    return {"n": n, "mean": mean, "sd": sd, "ci95_low": mean - half, "ci95_high": mean + half,
            "drop_one_min": min(loo), "drop_one_max": max(loo)}


def summarise_runs(runs: list[dict]) -> dict[str, dict[str, dict[str, Any]]]:
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for name in runs[0]["policies"]:
        out[name] = {}
        for metric in runs[0]["policies"][name]:
            vals = [r["policies"][name][metric] for r in runs]
            if any(v is None for v in vals):
                continue
            out[name][metric] = {k: (round(v, 6) if isinstance(v, float) else v)
                                 for k, v in stats(vals).items()}
    return out


# ----------------------------------------------------------------------------- assumptions

def assumptions_document() -> dict[str, Any]:
    p = []

    def add(id_: str, value: Any, unit: str, origin: str, rationale: str) -> None:
        p.append({"id": id_, "value": value, "unit": unit, "origin": origin, "rationale": rationale})

    add("week", "2026-10-19 to 2026-10-25, Europe/Paris, 7 evenings", "dates", "added",
        "An ordinary week with no public holiday, so each of the seven evenings is a normal trading evening.")
    add("bakeries", N_BAKERIES, "count", "brief",
        "Twelve shops, one per cycle through the 11 demo neighbourhood centroids (two neighbourhoods get two).")
    add("bakery_scatter_m", BAKERY_SCATTER_M, "m", "brief",
        "Each shop is placed uniformly over a disc of this radius around its neighbourhood centroid.")
    add("list_price_eur", list(PRICE_EUR), "EUR, uniform", "brief",
        "Given in the brief. Each bakery gets one draw, so the spread across bakeries is part of the simulated variation.")
    add("unit_cost_share", COST_SHARE, "share of list price", "brief",
        "Recorded for reference. No metric uses it: leftovers are baked whatever the policy, and "
        "the clock floor (0.30 x list) sits below this cost.")
    add("leftover_lambda", list(LAMBDA_RANGE), "loaves per evening, uniform lambda", "brief",
        "Leftover loaves per bakery per evening are Poisson(lambda), lambda drawn once per bakery.")
    add("residents", N_RESIDENTS, "count", "brief",
        "Placed by neighbourhood weight from demo.NEIGHBOURHOODS, with the brief's position jitter.")
    add("resident_jitter_deg", {"lat": JITTER_LAT, "lon": JITTER_LON}, "degrees, uniform +/-", "brief",
        "Keeps residents from sitting exactly on a centroid.")
    add("adoption_default", DEFAULT_ADOPTION, "share of residents", "brief",
        "Share of residents holding a standing bread wish in the headline run.")
    add("adoption_grid", list(ADOPTION_GRID), "share of residents", "brief",
        "Sensitivity grid. Adopter sets are nested: each rate includes every holder of the lower rates.")
    add("holder_limit_eur", {"share_range": list(LIMIT_SHARE_RANGE), "reference_eur": LIMIT_REF_EUR,
                             "rounding": "cents"}, "EUR, uniform", "brief",
        "Limits run from about 1.58 to 4.28 EUR, the range given in the brief.")
    add("holder_radius_m", {"values": list(RADIUS_M), "weights": list(RADIUS_WEIGHTS)}, "m", "brief",
        "Half of holders accept 1.5 km, a third 2.5 km and the rest 4 km, as given in the brief.")
    add("holder_max_matches_per_week", list(MAX_MATCHES_RANGE), "matches, uniform integer", "brief",
        "Weekly cap per holder. Enforced in all three policies.")
    add("holder_units", 1, "loaves per match", "brief", "One loaf per standing wish.")
    add("holder_wish_window", {"days": "all 7", "start": "17:00", "end": "21:00"}, "local time", "brief",
        "Covers the 18:30 to 20:30 pickup window in every policy.")
    add("holder_budget_per_week_cents", "limit x max_matches", "cents", "added",
        "Set so the weekly budget can never bind before the match cap does (every price is at most the limit).")
    add("policy_A_price_share", A_PRICE_SHARE, "share of list price", "brief",
        "Fixed markdown from 18:30, the same for every loaf of the bakery that evening.")
    add("policy_A_walk_by_p", A_WALK_BY_P, "probability per holder per bakery in radius per evening",
        "brief", "Stands in for passers-by who would buy. Same demand pool as the other policies.")
    add("policy_A_arrival", "uniform over 18:30-20:30", "time", "added",
        "Passers arrive in a random order within the pickup window; earlier arrivals take loaves first.")
    add("policy_A_one_per_evening", True, "flag", "brief", "A holder buys at most one loaf per evening.")
    add("policy_B_bag_loaves", B_BAG_LOAVES, "loaves per bag", "brief", "Bags of three loaves.")
    add("policy_B_bag_price", "list price", "EUR per bag", "brief",
        "One third of the three-loaf value, which is the list price per bag.")
    add("policy_B_open_p", B_OPEN_P, "probability per holder per evening", "brief",
        "Each holder opens the bag app on a given evening with this probability.")
    add("policy_B_choice", "nearest eligible bakery", "rule", "added",
        "A holder who opens the app takes a bag from the nearest bakery in radius with bags left.")
    add("policy_B_order", "random, first come first served", "rule", "added",
        "Holders who open the app are served in random order.")
    add("policy_B_loaf_per_match", "a bag is one weekly match", "rule", "added",
        "The weekly cap counts bags, so a holder can take up to 3 x max_matches loaves in B. This favours B on units.")
    add("policy_B_commission", "not modelled", "rule", "brief", "The brief leaves B's platform commission out.")
    add("policy_C_engine", "criee.engine Market + run_until, in-memory signed ledger", "rule", "brief",
        "Policy C is the real clearing engine, not a re-implementation.")
    add("policy_C_clock", {"start": "list price", "floor": "0.30 x list", "open": "18:30",
                           "close": "19:10", "clock_step_s": CLOCK_STEP_S}, "EUR and time", "brief",
        "Starts at list price, reaches the floor at 80 percent of the window (about 19:02), then holds it until 19:10.")
    add("policy_C_pickup_window", "18:30-20:30", "time", "brief", "Pickup window of each clock lot.")
    add("policy_C_one_per_evening", "wish paused after its match, resumed next morning", "rule", "added",
        "The engine has no per-evening cap, so the sim pauses a holder's wish after a match (the engine's "
        "own pause state) so the holder cannot buy from a second bakery that evening. Holders at their weekly cap "
        "stay paused for the rest of the week.")
    add("policy_C_listing_order", "random each evening", "rule", "added",
        "The engine breaks same-minute ties by listing order, so the order is shuffled each evening.")
    add("policy_C_sold_out_close", "a lot closes the moment it sells out", "rule", "added",
        "The engine would keep emitting empty ticks until 19:10. No fill can happen after sell-out, so "
        "allocations are unchanged; checked against the unshortened run on four seed and adoption pairs.")
    add("policy_C_noshow_rate", NOSHOW_RATE, "share of matches", "brief",
        "A match is a no-show when a hash of the seed and match id falls in the lowest 8 percent, so each seed has a fixed set of no-shows.")
    add("policy_C_noshow_treatment", "captured then fully refunded at match time", "rule", "added",
        "A no-show's unit never returns to the clock, its buyer's weekly match still counts, and the seller "
        "gets zero revenue. This is the conservative treatment the brief asks for.")
    add("policy_C_platform_fee", PLATFORM_FEE, "share of gross revenue", "brief",
        "Applied to policy C only. Policies A and B carry no fee in the model.")
    add("seeds_headline", "0..19", "seed", "brief", "Headline run: twenty seeds at the default adoption.")
    add("seeds_sensitivity", SENSITIVITY_SEEDS, "seeds per adoption rate", "brief",
        "Sensitivity uses seeds 0..9; the 3% entry reuses the headline runs for those seeds.")
    add("ci_method", "Student-t 95% interval of the mean across seeds", "statistic", "added",
        "Used for the figure whiskers and the shaded band. The sd is the sample sd (n-1).")
    add("drop_one_seed", "min and max of the mean with one seed left out", "statistic", "brief",
        "Shows how much one seed can move the mean.")
    add("rng", "random.Random seeded from a string, per seed and per policy", "rule", "added",
        "Each policy has its own stream, so changing one policy cannot shift another.")
    return {"label": "simulated", "parameters": p}


# ----------------------------------------------------------------------------- CLI

def _run_all(n_seeds: int, sens_seeds: int, log) -> dict[str, Any]:
    cache: dict[tuple[int, float], dict] = {}

    def get(seed: int, adoption: float) -> dict:
        key = (seed, round(adoption, 6))
        if key not in cache:
            cache[key] = run(seed, adoption)
        return cache[key]

    headline_runs = []
    for s in range(n_seeds):
        headline_runs.append(get(s, DEFAULT_ADOPTION))
        log(f"  headline seed {s:2d} done")
    by_adoption = {}
    for a in ADOPTION_GRID:
        runs = [get(s, a) for s in range(sens_seeds)]
        by_adoption[f"{a:.2f}"] = {"adoption": a, "seeds": list(range(sens_seeds)),
                                   "n_holders": runs[0]["n_holders"], "summary": summarise_runs(runs)}
        log(f"  sensitivity adoption {a:.2f} done")
    return {
        "label": "simulated",
        "note": "Every number in this file is simulated. Method and assumptions: docs/SIMULATION.md.",
        "week": "2026-10-19 to 2026-10-25, Europe/Paris, 7 evenings",
        "policy_names": POLICY_NAMES,
        "headline": {"adoption": DEFAULT_ADOPTION, "seeds": list(range(n_seeds)),
                     "runs": headline_runs, "summary": summarise_runs(headline_runs)},
        "sensitivity": by_adoption,
    }


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Simulated comparison of three end-of-day policies.")
    ap.add_argument("--seeds", type=int, default=20, help="headline seeds, 0..N-1 (default 20)")
    ap.add_argument("--sensitivity-seeds", type=int, default=SENSITIVITY_SEEDS,
                    help="seeds per adoption rate in the sensitivity run (default 10)")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help="results JSON path")
    ap.add_argument("--assumptions", type=Path, default=None,
                    help="assumptions JSON path (default: assumptions.json next to --out)")
    args = ap.parse_args(argv)
    assumptions = args.assumptions or (args.out.parent / "assumptions.json")

    def log(msg: str) -> None:
        print(msg, flush=True)

    t0 = time.perf_counter()
    results = _run_all(args.seeds, args.sensitivity_seeds, log)
    elapsed = time.perf_counter() - t0
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    assumptions.parent.mkdir(parents=True, exist_ok=True)
    assumptions.write_text(json.dumps(assumptions_document(), indent=2) + "\n", encoding="utf-8")
    log(f"simulated runs finished in {elapsed:.1f} s wall time (one process)")
    log(f"wrote {args.out}")
    log(f"wrote {assumptions}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
