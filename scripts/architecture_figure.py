"""Draw docs/figures/architecture.png, the README's "How it works" figure: four bands, from people to PayPal.

    python scripts/architecture_figure.py

The figure is HTML rendered by headless Chromium (Playwright) at 2x, about 860 px wide so its text stays readable on
GitHub. It shows where the model stops: Gemini proposes, the deterministic core decides.
"""

from __future__ import annotations

from pathlib import Path

from playwright.sync_api import sync_playwright

OUT = Path(__file__).resolve().parents[1] / "docs" / "figures" / "architecture.png"

BANDS = [
    ("People", None, ["Buyer app<br><small>chat + passkey</small>", "Any AI agent<br><small>over MCP</small>",
                      "Seller phone app<br><small>photo + voice</small>"]),
    ("Gemini 3.7 Flash", "proposes, never decides",
     ["Wish compiler", "Shelf reader", "Advice writer", "Seller assistant"]),
    ("Deterministic core", "decides",
     ["Clearing engine<br><small>call · clock · threshold</small>", "Signed hash-chained ledger",
      "Settlement worker"]),
    ("PayPal sandbox", None,
     ["Vault v3", "Orders v2 +<br>Payments v2", "Payouts", "Disputes +<br>Webhooks",
      "Agent Toolkit<br><small>invoices, disputes</small>"]),
]
ARROWS = [
    "words, shelf photos, voice notes",
    "typed terms only: enums, integers, coordinates, timestamps, identifiers",
    "each match settled the moment it clears",
]

CSS = """
body { margin: 0; background: #fff; font-family: 'Segoe UI', Helvetica, Arial, sans-serif; color: #1f2328; }
#fig { width: 860px; padding: 22px; box-sizing: border-box; }
.band { border: 1px solid #d0d7de; background: #f6f8fa; border-radius: 6px; padding: 12px 14px 14px; }
.band.core { border-left: 5px solid #d4a017; }
.title { font-size: 14px; font-weight: 600; margin-bottom: 10px; }
.title span { font-weight: 400; color: #57606a; }
.row { display: flex; gap: 10px; }
.box { flex: 1; background: #fff; border: 1px solid #57606a; border-radius: 3px; padding: 9px 6px;
       text-align: center; font-size: 14px; line-height: 1.3; display: flex; align-items: center;
       justify-content: center; min-height: 40px; }
.box small { color: #57606a; font-size: 12.5px; }
.arrow { display: flex; align-items: center; gap: 10px; margin: 6px 0 6px 36px; color: #57606a; font-size: 13px; }
.arrow svg { flex: none; }
"""


def html() -> str:
    parts = []
    for i, (name, note, boxes) in enumerate(BANDS):
        cls = "band core" if name == "Deterministic core" else "band"
        title = f"{name}<span> · {note}</span>" if note else name
        row = "".join(f'<div class="box"><div>{b}</div></div>' for b in boxes)  # inner div keeps the <br>
        parts.append(f'<div class="{cls}"><div class="title">{title}</div><div class="row">{row}</div></div>')
        if i < len(ARROWS):
            parts.append(
                '<div class="arrow"><svg width="14" height="30" viewBox="0 0 14 30">'
                '<path d="M7 0 V24" stroke="#57606a" stroke-width="1.6"/>'
                '<path d="M1.5 21 L7 29 L12.5 21 Z" fill="#57606a"/></svg>'
                f"{ARROWS[i]}</div>"
            )
    return f"<html><head><style>{CSS}</style></head><body><div id='fig'>{''.join(parts)}</div></body></html>"


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(device_scale_factor=2, viewport={"width": 900, "height": 900})
        page.set_content(html())
        page.locator("#fig").screenshot(path=str(OUT))
        browser.close()
    print("wrote", OUT)


if __name__ == "__main__":
    main()
