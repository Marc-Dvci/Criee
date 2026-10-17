"""Draw the two settlement figures from timings measured on the PayPal sandbox.

`--db` reads a ledger and writes the timings to data/settlement_timings.json; without it the
figures are drawn from that file. Writes docs/figures/settlement_flow.png (one sale, end to end)
and docs/figures/burst.png (the 40 holds of a group order). White background, no title.

Usage:
    python scripts/settlement_figures.py --db var/criee.db
    python scripts/settlement_figures.py
"""
from __future__ import annotations

import argparse
import os
import json
import sqlite3
from datetime import datetime
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # no GUI windows
import matplotlib.pyplot as plt  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
TIMINGS = ROOT / "data" / "settlement_timings.json"
FIGS = ROOT / "docs" / "figures"
AMBER = "#E0A100"
INK = "#222222"
AXIS = "#BDBDBD"


def _ts(at: str) -> datetime:
    return datetime.fromisoformat(at.replace("Z", "+00:00"))


def extract(db: Path) -> dict:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = [(seq, at, t, json.loads(b)) for seq, at, t, b in con.execute("select seq, at, type, body from events order by seq")]
    burst = next(b for _, _, t, b in rows if t == "burst.measured")
    lot = burst["lot_id"]
    in_lot = {b["fill"]["match_id"] for _, _, t, b in rows if t == "match.made" and b["fill"]["lot_id"] == lot}
    holds = sorted(b["ms"] for _, _, t, b in rows if t == "settlement.authorized" and b["match_id"] in in_lot)
    # the one sale a person collected at the counter
    sale = next(b["match_id"] for _, _, t, b in rows if t == "settlement.captured")
    ev = {t: (at, b) for _, at, t, b in rows if sale in json.dumps(b)}
    batch = ev["payout.sent"][1]["batch_id"]
    settled = next(at for _, at, t, b in rows if t == "payout.settled" and b["batch_id"] == batch)
    return {
        "burst": {"holds_ms": holds, "wall_ms": burst["wall_ms"], "p50_ms": burst["p50_ms"]},
        "sale": {
            "authorize_ms": ev["settlement.authorized"][1]["ms"],
            "capture_ms": ev["settlement.captured"][1]["ms"],
            "payout_settled_s": round((_ts(settled) - _ts(ev["payout.sent"][0])).total_seconds()),
            "gross_cents": ev["payout.sent"][1]["gross"],
            "net_cents": ev["payout.sent"][1]["net"],
        },
    }


FLOW = """<!doctype html><meta charset="utf-8">
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;600&family=IBM+Plex+Mono:wght@500&display=swap" rel="stylesheet">
<style>
body{margin:0;background:#fff;font-family:'IBM Plex Sans',sans-serif;color:#222}
#fig{width:1200px;padding:28px 24px 24px;box-sizing:border-box;display:grid;grid-template-columns:118px repeat(5,1fr);column-gap:14px;row-gap:14px;align-items:stretch}
.lane{font-size:14px;font-weight:600;color:#666;align-self:center}
.step{background:#f4f4f2;border-radius:10px;padding:14px 14px 12px;position:relative}
.step h3{margin:0 0 6px;font-size:16px;font-weight:600}
.step p{margin:0;font-size:13px;line-height:1.4;color:#555}
.step:not(.last)::after{content:'→';position:absolute;right:-13px;top:50%;transform:translateY(-55%);font-size:15px;color:#9a9a9a}
.pp{border:1.5px solid #c9d6ea;border-radius:10px;padding:12px 14px;background:#fff}
.pp code{display:block;font-family:'IBM Plex Mono',monospace;font-size:12.5px;color:#0b3a7e;line-height:1.45}
.pp .m{margin-top:8px;font-size:13px;color:#222}
.pp .m b{color:{{amber}};font-weight:600}
.alt{grid-column:5 / span 1;font-size:12.5px;color:#666;border:1.5px dashed #d0d0d0;border-radius:10px;padding:10px 12px;line-height:1.4}
.alt code{font-family:'IBM Plex Mono',monospace;font-size:12px;color:#0b3a7e}
</style>
<div id="fig">
<div class="lane">Criée</div>
<div class="step"><h3>Wish signed</h3><p>A passkey signs the SHA&#8209;256 of the exact terms.</p></div>
<div class="step"><h3>Match clears</h3><p>The engine finds a lot inside the wish. Nobody is at the screen.</p></div>
<div class="step"><h3>Code typed</h3><p>The buyer reads six digits at the counter.</p></div>
<div class="step"><h3>Seller paid</h3><p>Captured amount minus a 4% fee.</p></div>
<div class="step last"><h3>Dispute answered</h3><p>One PDF built from the signed ledger.</p></div>
<div class="lane">PayPal sandbox</div>
<div class="pp"><code>Vault v3</code><code>payment token</code><div class="m">saved <b>once</b></div></div>
<div class="pp"><code>Orders v2 AUTHORIZE</code><code>vault_id, merchant-initiated</code><div class="m">hold in <b>{{auth}} s</b></div></div>
<div class="pp"><code>Payments v2 capture</code><code>Orders v2 track</code><div class="m">captured in <b>{{cap}} s</b>, &ldquo;Collected in store&rdquo;</div></div>
<div class="pp"><code>Payouts</code><code>batch status</code><div class="m"><b>SUCCESS</b> {{pay}} s after sending</div></div>
<div class="pp"><code>Disputes</code><code>provide-evidence</code><div class="m">evidence <b>accepted</b></div></div>
<div></div><div></div><div></div>
<div class="alt" style="grid-column:4 / span 1">No-show: <code>Payments v2 void</code>, and the loaf goes back on the clock.</div>
</div>
"""


def draw_flow(t: dict) -> None:
    from playwright.sync_api import sync_playwright

    s = t["sale"]
    html = FLOW
    for k, v in {"amber": AMBER, "auth": f"{s['authorize_ms'] / 1000:.1f}", "cap": f"{s['capture_ms'] / 1000:.1f}",
                 "pay": str(s["payout_settled_s"])}.items():
        html = html.replace("{{" + k + "}}", v)
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=os.environ.get("CHROMIUM_PATH") or None)  # headless
        page = b.new_page(viewport={"width": 1200, "height": 600}, device_scale_factor=2)
        page.set_content(html, wait_until="networkidle")
        page.locator("#fig").screenshot(path=str(FIGS / "settlement_flow.png"))
        b.close()


def draw_burst(t: dict) -> None:
    plt.rcParams.update({"font.size": 13, "text.color": INK, "axes.labelcolor": INK, "xtick.color": INK, "ytick.color": INK,
                         "axes.edgecolor": AXIS, "axes.linewidth": 0.8})
    holds = [ms / 1000 for ms in t["burst"]["holds_ms"]]
    wall = t["burst"]["wall_ms"] / 1000
    fig, ax = plt.subplots(figsize=(6, 3.4), dpi=200)
    ax.barh(range(1, len(holds) + 1), holds, height=0.62, color=AMBER, left=0)
    ax.axvline(wall, color=INK, lw=1, ls=(0, (3, 3)))
    ax.text(wall + 0.04, len(holds) + 0.5, f"all 40 held: {wall:.1f} s", va="top", fontsize=12)
    ax.set_xlim(0, 3.4)
    ax.set_ylim(0.3, len(holds) + 0.7)
    ax.set_yticks([1, 10, 20, 30, 40])
    ax.set_xlabel("Seconds after the burst starts")
    ax.set_ylabel("PayPal hold, fastest first")
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.tick_params(length=3, color=AXIS)
    fig.tight_layout()
    fig.savefig(FIGS / "burst.png", facecolor="white")
    plt.close(fig)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path)
    a = ap.parse_args()
    if a.db:
        TIMINGS.write_text(json.dumps(extract(a.db), indent=1) + "\n", encoding="utf-8")
    t = json.loads(TIMINGS.read_text(encoding="utf-8"))
    FIGS.mkdir(parents=True, exist_ok=True)
    draw_burst(t)
    draw_flow(t)
    print(json.dumps({k: v for k, v in t.items() if k != "burst"}, indent=1), "sum of holds s:", round(sum(t["burst"]["holds_ms"]) / 1000, 1))


if __name__ == "__main__":
    main()
