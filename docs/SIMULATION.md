# Simulated comparison of three end-of-day policies

This document describes a simulation. Every number in it is simulated. It is not a forecast, a pilot, or a measurement of real bakeries or real buyers. It compares three ways a bakery can clear its unsold bread at the end of the day, over one simulated week in Strasbourg.

## The three policies

- **A: fixed markdown.** From 18:30 the bakery sells every leftover loaf at half its list price.
- **B: surprise bag.** The bakery packs leftovers into bags of three loaves, sold at one list price per bag through an app. Leftovers that do not fill a bag are wasted.
- **C: Criée clock.** From 18:30 the bakery lists a descending clock. The price starts at list price, reaches 30% of list at about 19:02, and holds there until the clock closes at 19:10. A buyer takes one loaf at the first tick where the price is at or below their limit, while loaves remain. Pickup runs from 18:30 to 20:30.

## Method

- **Week and places.** The simulated week runs from Monday 19 to Sunday 25 October 2026, Europe/Paris, with seven evenings. There are 12 bakeries. Each sits at a random point within 600 m of one of the 11 neighbourhood centroids in `criee/demo.py`, cycling through the list, so two neighbourhoods get two bakeries.
- **Residents and holders.** 10,000 residents are placed by neighbourhood weight. A share equal to the adoption rate holds a standing bread wish. The adopters are the lowest-ranked residents of one random ranking, so the 1% group sits inside the 2% group, and so on. For each seed, all three policies run on the same world: the same bakeries, supply, holders, limits, radii and weekly caps.
- **Policy C uses the real engine.** Policy C runs through `criee.engine` (`Market` and `run_until`) on an in-memory signed ledger, with the real `WishTerms` and `LotTerms`. The engine is not changed. Policies A and B are plain rules in `criee/sim.py`. They measure distance with the engine's `haversine_m`, so every policy uses the same distance rule.
- **Three additions around the engine for C.** The brief rules out some behaviour that the engine, used as is, would allow or produce. I handle it in three ways, and each one appears in the assumptions table.
  1. *One loaf per resident per evening.* The engine has no per-evening cap. After a match, the holder's wish is paused with the engine's own pause event and resumed the next morning. A holder at their weekly cap stays paused.
  2. *Lots close at sell-out.* Without this, the engine keeps emitting empty clock ticks until 19:10. No fill can happen after sell-out. Holders at their weekly cap are also kept paused, which avoids the same kind of empty work. Allocations were checked to be identical to the unshortened code on four seed and adoption pairs, with both shortcuts in place.
  3. *No-shows.* 8% of matches are no-shows, chosen by a hash of the seed and the match id. A no-show's loaf is never returned to the clock and earns nothing, and the buyer's weekly match still counts. The engine's own no-show path would return the loaf to the clock, so the simulation does not use that path.
- **Metric definitions.** "Per bakery-evening" means the week total divided by 84 (12 bakeries times 7 evenings). Seller net revenue is gross revenue minus the 4% platform fee for C, and equals gross for A and B. Mean price paid is revenue divided by loaves sold. Mean buyer surplus is the sum of (limit minus price paid) over loaves sold, divided by loaves sold. For a bag, each of its three loaves counts at its limit minus one third of the bag price. A holder counts as served if they completed at least one purchase in the week. A no-show does not count.
- **Statistics.** The sd is the sample standard deviation over seeds. The 95% interval is the Student-t interval of the mean across seeds, used for the figure whiskers and bands. The drop-one range is the lowest and highest value of the mean when each seed in turn is left out.
- **Seeds.** The headline uses seeds 0 to 19 at 3% adoption. The sensitivity uses seeds 0 to 9 at each adoption rate. Its 3% entry therefore uses 10 seeds and differs slightly from the headline.

## Assumptions

"Brief" marks a value given in the brief. "Added" marks a choice I made where the brief was silent or where a rule had to be written down to run.

| Assumption | Value | Source | Rationale |
|---|---|---|---|
| Week | Mon 19 to Sun 25 Oct 2026, Europe/Paris, 7 evenings | Added | An ordinary week with no public holiday. |
| Bakeries | 12, one per cycle through the 11 neighbourhood centroids | Brief | Two neighbourhoods get two shops. |
| Bakery placement | Uniform within 600 m of the centroid | Brief | Keeps each shop in its neighbourhood. |
| List price | Uniform EUR 3.50 to 5.50 per loaf, one draw per bakery | Brief | The spread of loaf prices given in the brief. |
| Unit cost | 0.35 x list price | Brief | Recorded only. No metric uses it, because leftovers are baked whatever the policy. |
| Leftover supply | Poisson(lambda) per bakery per evening, lambda uniform 8 to 20 | Brief | Leftovers vary by bakery and by evening. |
| Residents | 10,000, placed by neighbourhood weight with jitter of 0.004 deg lat and 0.006 deg lon | Brief | Only holders buy in the model, so only holders affect results. |
| Adoption, headline | 3% (300 holders) | Brief | The default in the brief. |
| Adoption, sensitivity | 1%, 2%, 3%, 5%, with nested holder sets | Brief | The sensitivity grid. Nesting keeps the comparison paired. |
| Holder limit | Uniform 0.35 to 0.95 x EUR 4.50, rounded to cents (EUR 1.58 to 4.28) | Brief | The range given in the brief. |
| Holder radius | 1,500 m (50%), 2,500 m (35%), 4,000 m (15%) | Brief | The mix given in the brief. |
| Holder weekly cap | Uniform integer 2 to 4 matches | Brief | Enforced in all three policies. |
| Holder units | 1 loaf per match | Brief | One loaf per standing wish. |
| Holder window | Every day 17:00 to 21:00 | Brief | Covers the 18:30 to 20:30 pickup window. |
| Holder budget | Limit x max matches per week | Added | Set so the budget can never bind before the match cap does. |
| Distance rule | Engine's `haversine_m` for all policies | Added | One distance rule for every policy. |
| A price | 0.50 x list price from 18:30, every loaf | Brief | A fixed markdown. |
| A walk-by | 0.10 per holder, per bakery in radius, per evening | Brief | Stands in for passers-by who would buy. |
| A arrival order | Random within 18:30 to 20:30; earlier arrivals take loaves first | Added | The brief does not order passers-by. |
| A one per evening | One loaf per holder per evening | Brief | Applies to A. |
| B bag | Three loaves at one list price per bag | Brief | One third of the three-loaf value. |
| B open rate | 0.25 per holder per evening | Brief | The chance a holder opens the bag app on a given evening. |
| B bakery choice | Nearest bakery in radius with bags left and price within the limit | Added | The brief does not say which bakery a buyer picks. |
| B buyer order | Random, first come first served | Added | The brief does not order buyers. |
| B weekly cap | One bag counts as one weekly match | Added | The literal reading of the cap. A holder can then take up to 3 x max matches of loaves, which favours B on units. |
| B commission | Not modelled | Brief | The brief leaves it out. |
| C engine | `criee.engine` `Market` and `run_until`, in-memory signed ledger | Brief | The real engine, unchanged. |
| C clock | Start at list price, floor 0.30 x list, open 18:30, close 19:10, one-minute ticks | Brief | The price reaches the floor at 80% of the window, about 19:02. |
| C pickup window | 18:30 to 20:30 | Brief | Pickup window of each clock lot. |
| C one loaf per evening | Wish paused after a match, resumed next morning; holders at cap stay paused | Added | The engine has no per-evening cap. |
| C sell-out close | Lot closes the moment it sells out | Added | Avoids empty ticks. Allocations checked unchanged on four pairs. |
| C tie order | Listing order shuffled each evening | Added | The engine breaks same-minute ties by listing order. |
| C no-show rate | 8% of matches, chosen by hash of seed and match id | Brief | The rate given in the brief. |
| C no-show treatment | Captured then fully refunded at match time: unit not returned, cap still used, zero revenue | Added | The conservative treatment the brief asks for. |
| C platform fee | 4% of gross revenue | Brief | Applied to C only. |
| Seeds | 0 to 19 headline; 0 to 9 per sensitivity rate | Brief | As given. |
| Interval | Student-t 95% interval of the mean across seeds | Added | Used for the figure whiskers and bands. |
| Random streams | One per seed and policy, seeded from a string | Added | Changing one policy does not shift another. |

## Simulated results

### Headline: 3% adoption, 20 seeds

Each cell gives mean ± sd over 20 seeds, with the drop-one-seed range of the mean in brackets. All values are simulated.

| Metric | A: fixed markdown | B: surprise bag | C: Criée clock |
|---|---|---|---|
| **Units wasted per bakery-evening** | 6.81 ± 0.76 (6.76 to 6.92) | 1.06 ± 0.13 (1.05 to 1.07) | 3.96 ± 0.64 (3.90 to 4.03) |
| **Seller net revenue per bakery-evening, EUR** | 15.27 ± 0.49 (15.23 to 15.32) | 19.05 ± 1.23 (18.94 to 19.18) | 26.75 ± 0.73 (26.69 to 26.85) |
| Units sold per bakery-evening | 6.85 ± 0.40 (6.82 to 6.90) | 12.61 ± 0.72 (12.56 to 12.69) | 9.71 ± 0.18 (9.69 to 9.73) |
| Seller gross revenue per bakery-evening, EUR | 15.27 ± 0.49 (15.23 to 15.32) | 19.05 ± 1.23 (18.94 to 19.18) | 27.86 ± 0.76 (27.80 to 27.97) |
| Mean price paid, EUR per loaf | 2.23 ± 0.10 (2.22 to 2.24) | 1.51 ± 0.06 (1.50 to 1.52) | 2.87 ± 0.05 (2.86 to 2.87) |
| Mean buyer surplus, EUR per loaf | 0.96 ± 0.06 (0.96 to 0.97) | 1.42 ± 0.06 (1.42 to 1.43) | 0.07 ± 0.01 (0.06 to 0.07) |
| Share of holders served at least once | 0.789 ± 0.043 (0.786 to 0.795) | 0.738 ± 0.032 (0.735 to 0.741) | 0.998 ± 0.002 (0.998 to 0.998) |

Units supplied are 13.67 ± 0.75 per bakery-evening in all three policies, because the supply draws are shared.

Under these assumptions, C has the highest seller net revenue, B has the lowest waste, and A has the most waste. In C, buyers pay on average 0.07 EUR below their limit, so the clock takes almost all of the buyer surplus. C serves almost every holder at least once, while A serves 79% and B 74%.

## Sensitivity to adoption

Each cell gives mean ± sd over 10 seeds. All values are simulated.

**Units wasted per bakery-evening**

| Adoption | Holders | A: fixed markdown | B: surprise bag | C: Criée clock |
|---|---|---|---|---|
| 1% | 100 | 11.39 ± 0.97 | 8.01 ± 0.95 | 10.56 ± 0.93 |
| 2% | 200 | 9.01 ± 0.96 | 3.01 ± 0.54 | 7.23 ± 0.92 |
| 3% | 300 | 6.81 ± 0.94 | 1.07 ± 0.12 | 4.06 ± 0.82 |
| 5% | 500 | 3.29 ± 0.67 | 0.97 ± 0.11 | 1.10 ± 0.13 |

**Seller net revenue per bakery-evening, EUR**

| Adoption | Holders | A: fixed markdown | B: surprise bag | C: Criée clock |
|---|---|---|---|---|
| 1% | 100 | 5.38 ± 0.25 | 8.68 ± 0.71 | 9.05 ± 0.37 |
| 2% | 200 | 10.63 ± 0.29 | 16.23 ± 1.10 | 18.15 ± 0.55 |
| 3% | 300 | 15.49 ± 0.39 | 19.17 ± 1.53 | 27.05 ± 0.66 |
| 5% | 500 | 23.43 ± 0.90 | 19.30 ± 1.59 | 38.43 ± 2.12 |

Waste falls as adoption rises for all three policies. B has the lowest waste at every rate. C has the highest seller net revenue at every rate. At 5%, every bag sells: B's waste equals the loaves left over after packing (0.975 per bakery-evening, seeds 0 to 9), so supply limits B there. B's net revenue then falls below A's, and only 54% of holders are served by B, against 87% under C.

![Simulated units sold and wasted per bakery-evening by policy](figures/sim_clearance.png)

Simulated units sold and wasted per bakery-evening, mean with 95% interval over 20 seeds at 3% adoption.

![Simulated units wasted per bakery-evening against adoption rate](figures/sim_adoption.png)

Simulated units wasted per bakery-evening against adoption rate, mean over 10 seeds with a shaded 95% interval.

## What this does not show

This model shows what happens to the same bread under the stated rules and the stated demand. It does not show whether anyone wants the bread. It models whether a holder is present to buy, not whether they like the product, so taste, price perception and loyalty are absent. The real adoption rate is unknown, and the sensitivity table shows how strongly the results depend on it. The policies do not share an attention rule: a standing wish in C is always live, while A and B assume that only 10% and 25% of holders act on a given evening. That gap is the largest single driver of the difference between C and the other two, and I did not model it on a common basis. C also carries an 8% no-show rate that A and B do not carry. B's weekly cap counts bags, so a holder can receive three loaves per match. B's commission is left out. The clock floor (0.30 of list) is below the modelled unit cost (0.35 of list), so floor sales bring in less than cost. The results cover one week and one set of neighbourhoods, and they assume that bakeries and buyers do not change their behaviour.

## Reproduce

Run from the repository root with the project's Python environment:

- `python -m criee.sim --seeds 20 --out data/sim/results.json` runs the headline and sensitivity and writes `results.json` and `assumptions.json`. It took 65 to 70 seconds on the machine used, in one process. Three full runs on that machine produced a byte-identical `results.json`.
- `python scripts/sim_figures.py` reads `results.json` and writes the two figures into `docs/figures/`. It needs matplotlib, which is in the `dev` extra.
- `python -m pytest tests/test_sim.py` runs the three fast checks (conservation, clock price bounds, determinism) in about 3.5 seconds.

## Files

- `criee/sim.py`: the simulation and the CLI.
- `data/sim/results.json`: all simulated results (headline per seed, summaries, sensitivity).
- `data/sim/assumptions.json`: every parameter with its value, source and rationale.
- `scripts/sim_figures.py`: draws the two figures.
- `docs/figures/sim_clearance.png` and `docs/figures/sim_adoption.png`: the figures.
- `tests/test_sim.py`: the tests.
