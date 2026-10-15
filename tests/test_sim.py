"""Fast checks on the simulated policy comparison: conservation, clock price bounds, determinism."""
from criee import sim

SEEDS = (0, 1)
N_BAKERIES = 3
ADOPTION = 0.03


def test_conservation_for_every_policy_and_bakery_evening():
    for seed in SEEDS:
        res = sim.simulate(seed, ADOPTION, n_bakeries=N_BAKERIES)
        for name, pr in res.policies.items():
            assert len(pr.lots) == N_BAKERIES * sim.N_DAYS
            for row in pr.lots:
                assert row["sold"] + row["wasted"] == row["supplied"], (name, row)
            assert sum(s["units"] for s in pr.sales) == sum(r["sold"] for r in pr.lots), name


def test_criee_sale_prices_sit_between_floor_and_buyer_limit():
    for seed in SEEDS:
        res = sim.simulate(seed, ADOPTION, n_bakeries=N_BAKERIES)
        sales = res.policies["C"].sales
        assert sales, "the clock should sell something in this setup"
        for s in sales:
            assert s["floor_cents"] <= s["unit_price_cents"] <= s["limit_cents"], s
        for s in res.policies["A"].sales:
            assert s["unit_price_cents"] <= s["limit_cents"], s


def test_weekly_loaf_allowance_holds_for_every_policy():
    for seed in SEEDS:
        res = sim.simulate(seed, ADOPTION, n_bakeries=N_BAKERIES)
        for name, pr in res.policies.items():
            loaves: dict[int, int] = {}
            for s in pr.sales:
                loaves[s["holder"]] = loaves.get(s["holder"], 0) + s["units"]
            for j, n in loaves.items():
                assert n <= res.world.holders[j].max_matches, (name, j, n)
            if name == "B":
                assert all(s["units"] == sim.B_BAG_LOAVES for s in pr.sales)


def test_same_seed_gives_same_results():
    for seed in SEEDS:
        first = sim.run(seed, ADOPTION, n_bakeries=N_BAKERIES)
        second = sim.run(seed, ADOPTION, n_bakeries=N_BAKERIES)
        assert first == second
