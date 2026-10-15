"""Draw the two simulated figures from data/sim/results.json.

Every plotted number is simulated. Writes docs/figures/sim_clearance.png and
docs/figures/sim_adoption.png: white background, no title, no footnote, grey left and bottom spines.
Whiskers and bands are the Student-t 95% interval of the mean across seeds.

Usage:
    python scripts/sim_figures.py
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # no GUI windows
import matplotlib.image as mpimg  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.ticker import FormatStrFormatter  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
COLOURS = {"A": "#555555", "B": "#9AA0A6", "C": "#E0A100"}
NAMES = {"A": "A: fixed markdown", "B": "B: surprise bag", "C": "C: Criée clock"}
INK = "#222222"
AXIS = "#BDBDBD"
LABEL_PT = 13
FIGSIZE = (6, 3.5)
DPI = 200

plt.rcParams.update({
    "font.size": LABEL_PT,
    "axes.labelsize": LABEL_PT,
    "xtick.labelsize": LABEL_PT,
    "ytick.labelsize": LABEL_PT,
    "legend.fontsize": LABEL_PT,
    "text.color": INK,
    "axes.labelcolor": INK,
    "xtick.color": INK,
    "ytick.color": INK,
    "axes.edgecolor": AXIS,
    "axes.linewidth": 0.8,
    "axes.facecolor": "white",
    "figure.facecolor": "white",
    "savefig.facecolor": "white",
})


def _style(ax) -> None:
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_color(AXIS)
    ax.spines["bottom"].set_color(AXIS)
    ax.tick_params(length=3, color=AXIS)


def _interval(entry: dict) -> tuple[float, float, float]:
    return entry["mean"], entry["ci95_low"], entry["ci95_high"]


def clearance_figure(results: dict, out: Path) -> tuple[int, int]:
    summary = results["headline"]["summary"]
    metrics = ["units_sold_per_be", "units_wasted_per_be"]
    group_labels = ["Units sold", "Units wasted"]
    width = 0.22
    fig, ax = plt.subplots(figsize=FIGSIZE)
    tops = []
    for i, pol in enumerate(["A", "B", "C"]):
        xs = [g + (i - 1) * 0.25 for g in range(len(metrics))]
        means, lo_err, hi_err = [], [], []
        for m in metrics:
            mean, lo, hi = _interval(summary[pol][m])
            means.append(mean)
            lo_err.append(mean - lo)
            hi_err.append(hi - mean)
            tops.append(hi)
        ax.bar(xs, means, width=width, color=COLOURS[pol], label=NAMES[pol], edgecolor="none",
               yerr=[lo_err, hi_err], capsize=3, error_kw={"ecolor": INK, "elinewidth": 1.0})
    ax.set_xticks(range(len(metrics)))
    ax.set_xticklabels(group_labels)
    ax.set_xlim(-0.6, len(metrics) - 0.4)
    ax.set_ylim(0, max(tops) * 1.18)
    ax.yaxis.set_major_formatter(FormatStrFormatter("%g"))
    ax.set_ylabel("Simulated units\nper bakery-evening")
    ax.legend(frameon=False, loc="upper right")
    _style(ax)
    fig.tight_layout()
    fig.savefig(out, dpi=DPI, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return _pixels(out)


def adoption_figure(results: dict, out: Path) -> tuple[int, int]:
    grid = sorted(results["sensitivity"].values(), key=lambda b: b["adoption"])
    xs = [b["adoption"] for b in grid]
    fig, ax = plt.subplots(figsize=FIGSIZE)
    for pol in ["A", "B", "C"]:
        means, lows, highs = [], [], []
        for b in grid:
            mean, lo, hi = _interval(b["summary"][pol]["units_wasted_per_be"])
            means.append(mean)
            lows.append(lo)
            highs.append(hi)
        ax.fill_between(xs, lows, highs, color=COLOURS[pol], alpha=0.18, linewidth=0)
        ax.plot(xs, means, color=COLOURS[pol], marker="o", markersize=5, linewidth=1.8, label=NAMES[pol])
    ax.set_xticks(xs)
    ax.set_xticklabels([f"{x:.0%}" for x in xs])
    ax.set_xlim(min(xs) - 0.004, max(xs) + 0.004)
    ax.set_ylim(0, None)
    ax.set_xlabel("Adoption (share of residents with a bread wish)")
    ax.set_ylabel("Simulated units wasted\nper bakery-evening")
    ax.legend(frameon=False, loc="upper right")
    _style(ax)
    fig.tight_layout()
    fig.savefig(out, dpi=DPI, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return _pixels(out)


def _pixels(path: Path) -> tuple[int, int]:
    h, w = mpimg.imread(path).shape[:2]
    return w, h


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Draw the simulated figures.")
    ap.add_argument("--results", type=Path, default=ROOT / "data" / "sim" / "results.json")
    ap.add_argument("--out-dir", type=Path, default=ROOT / "docs" / "figures")
    args = ap.parse_args(argv)
    results = json.loads(args.results.read_text(encoding="utf-8"))
    args.out_dir.mkdir(parents=True, exist_ok=True)
    c_out = args.out_dir / "sim_clearance.png"
    a_out = args.out_dir / "sim_adoption.png"
    cw, ch = clearance_figure(results, c_out)
    aw, ah = adoption_figure(results, a_out)
    print(f"wrote {c_out} ({cw}x{ch} px)")
    print(f"wrote {a_out} ({aw}x{ah} px)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
