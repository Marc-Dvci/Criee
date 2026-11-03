"""Verify a published release, its ledger, and its real browser surfaces without purchases.

python scripts/verify_release.py BASE_URL COMMIT --channel chrome --out var/release-qa
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import time
import urllib.request


def audit(base: str, suffix: str, expected_scope: str):
    started = time.perf_counter()
    request = urllib.request.Request(base + "/api/ledger/verify/jobs" + suffix,
                                     data=b"{}", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=15) as response:
        first = json.load(response)
    assert not first["done"] and first["checked"] == 0
    result = first
    while not result["done"]:
        assert time.perf_counter() - started < 120, "Audit timed out"
        with urllib.request.urlopen(base + "/api/ledger/verify/jobs/" + first["id"], timeout=15) as response:
            result = json.load(response)
        if not result["done"]:
            time.sleep(0.2)
    assert result.get("done") and result.get("ok") and result["scope"] == expected_scope, result
    print(f"PASS: public {expected_scope} audit, {result['events']:,} events, {time.perf_counter() - started:.2f}s", flush=True)


def main():
    from playwright.sync_api import expect, sync_playwright

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base")
    parser.add_argument("commit")
    parser.add_argument("--channel")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    base = args.base.rstrip("/")
    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(base + "/api/status", timeout=20) as response:
        status = json.load(response)
    assert status["release"] == args.commit and status["paypal"] == "sandbox", status
    print(f"PASS: public server runs {args.commit}; ledger has {status['ledger']['events']:,} events", flush=True)
    with urllib.request.urlopen(base + "/api/samples/shelf.jpg", timeout=20) as response:
        assert response.read(2) == b"\xff\xd8"
    audit(base, "?recent=200", "recent")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, channel=args.channel)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        errors = []
        stream_requests = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("request", lambda request: stream_requests.append(request.url) if "/api/stream" in request.url else None)
        page.goto(base, wait_until="domcontentloaded")
        expect(page.get_by_role("link", name="Take the guided tour", exact=True)).to_be_visible()
        if args.out:
            page.screenshot(path=str(args.out / "live-home.png"))
        page.get_by_role("link", name="Take the guided tour", exact=True).click()
        page.get_by_role("button", name="Start the guided tour", exact=True).click()
        expect(page.locator("paypal-button")).to_be_visible(timeout=25000)
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "Tour overflows on mobile"
        page.get_by_role("link", name="Make your wish", exact=False).click()
        expect(page.locator("textarea").first).to_be_in_viewport()
        if args.out:
            page.screenshot(path=str(args.out / "live-tour-mobile.png"))
        page.get_by_role("button", name="2 Read a shelf", exact=True).click()
        page.get_by_role("button", name="Use the sample shelf photo", exact=True).click()
        expect(page.get_by_role("button", name="Read my shelf", exact=True)).to_be_enabled()
        page.get_by_role("button", name="4 Collect & get paid", exact=True).click()
        expect(page.get_by_role("button", name="OK", exact=True)).to_be_visible()
        page.get_by_role("button", name="5 Inspect the proof", exact=True).click()
        expect(page.locator("h1").filter(has_text="· console")).to_be_visible(timeout=20000)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "Proof overflows on mobile"
        page.get_by_role("button", name="Quick check · latest 200", exact=True).click()
        expect(page.get_by_text("Recent segment verified:")).to_be_visible(timeout=15000)
        started = time.perf_counter()
        with page.expect_response(lambda response: response.url.endswith('/api/ledger/verify/jobs') and response.request.method == 'POST') as initial:
            page.get_by_role("button", name="Verify the whole chain", exact=True).click()
        job = initial.value.json()
        page.wait_for_function("(() => { const p = document.querySelector('.verify-progress'); return p && p.value > 0 && p.value < p.max })()", timeout=20000)
        print("PASS: hosted audit progress changes on screen before completion", flush=True)
        if args.out:
            page.locator('.verify-progress').scroll_into_view_if_needed()
            page.screenshot(path=str(args.out / "live-audit-progress.png"))
        expect(page.get_by_text("Full chain verified:")).to_be_visible(timeout=90000)
        result = page.request.get(base + '/api/ledger/verify/jobs/' + job['id']).json()
        assert result['ok'] and result['scope'] == 'full' and result['checked'] == result['total']
        print(f"PASS: public full audit, {result['events']:,} events, {time.perf_counter() - started:.2f}s", flush=True)
        if 'trycloudflare.com' in base:
            assert not stream_requests, "The quick-tunnel demo must use its supported JSON polling transport"
            expect(page.locator('.status-pill .dot')).to_have_attribute('aria-label', 'Market refreshes every 4 seconds')
        page.set_viewport_size({"width": 1440, "height": 1000})
        page.get_by_role("link", name="Inspect the console", exact=False).click()
        if args.out:
            page.screenshot(path=str(args.out / "live-console.png"))
        page.get_by_role("link", name="Criée", exact=True).click()
        expect(page).to_have_title("Criée")
        assert not errors, str(errors)
        browser.close()
        print("PASS: hosted tour, official PayPal button, sample photo, keypad, console, mobile layout and title cleanup; no uncaught browser errors", flush=True)


if __name__ == "__main__":
    main()
