"""Exercise the built app in Chromium with an isolated, keyless server.

Run after `cd web && npm run build`: python scripts/judge_smoke.py
Optional: --server-python .venv/Scripts/python.exe --channel chrome --out var/judge-qa
PayPal v6 callbacks are contract-tested with an explicitly mocked SDK, never a live charge.
--live-paypal-ui checks the real sandbox button using .env, without approving a wallet or paying.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parent.parent


def create_smoke_app():
    from dataclasses import replace
    from criee.api.app import create_app
    from criee.config import load

    cfg = replace(load(Path(os.environ["CRIEE_DATA_DIR"]) / "no.env"), paypal_client_id=None,
                  paypal_secret=None, gemini_api_key=None, ai_provider="recorded")
    return create_app(cfg)


def create_paypal_preview_app():
    from dataclasses import replace
    from criee.api.app import create_app
    from criee.config import load

    cfg = replace(load(), gemini_api_key=None, ai_provider="recorded")
    if not cfg.paypal_live:
        raise RuntimeError("This optional check requires PayPal sandbox app credentials in .env.")
    # No clocks, simulated actors, settlement, webhooks or dispute polling in this check.
    return create_app(cfg, start_loops=False)


MOCK_SDK = """
window.paypal = {createInstance: async (options) => {
  window.sdkOptions = options;
  return {
    findEligibleMethods: async (options) => {
      window.eligibilityOptions = options;
      return {isEligible: (method) => method === 'paypal'};
    },
    createPayPalSavePaymentSession: (callbacks) => {
      window.sdkCallbacks = callbacks;
      return {start: async (options, promise) => {
        const token = await promise;
        await callbacks.onApprove({vaultSetupToken: token.setupToken});
      }};
    }
  };
}};
customElements.define('paypal-button', class extends HTMLElement {
  connectedCallback() { this.innerHTML = '<button type="button">PayPal SDK test double</button>'; }
});
"""


def wait_json(request, url, predicate, timeout=25):
    """Poll completed HTTP results, rather than treating an in-page Promise as truthy."""
    deadline = time.monotonic() + timeout
    while True:
        response = request.get(url, timeout=5000)
        assert response.ok, response.text()
        data = response.json()
        if predicate(data):
            return data
        if time.monotonic() > deadline:
            raise AssertionError(f"Timed out waiting for {url}: {data}")
        time.sleep(0.15)


def main():
    from playwright.sync_api import expect, sync_playwright

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-python", default=sys.executable)
    parser.add_argument("--channel", default=os.environ.get("CRIEE_BROWSER_CHANNEL"))
    parser.add_argument("--out", type=Path)
    parser.add_argument("--live-paypal-ui", action="store_true")
    args = parser.parse_args()
    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="criee-judge-", ignore_cleanup_errors=True) as tmp:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        env = {**os.environ, "CRIEE_DATA_DIR": tmp, "CRIEE_PUBLIC_URL": base, "CRIEE_DEMO": "1", "PYTHONUNBUFFERED": "1"}
        log = open(Path(tmp) / "server.log", "w+", encoding="utf-8")
        factory = "create_paypal_preview_app" if args.live_paypal_ui else "create_smoke_app"
        process = subprocess.Popen([args.server_python, "-m", "uvicorn", f"scripts.judge_smoke:{factory}",
                                    "--factory", "--host", "127.0.0.1", "--port", str(port)],
                                   cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        try:
            print(f"Starting isolated smoke server (pid {process.pid}) at {base}", flush=True)
            deadline = time.monotonic() + 60
            while True:
                try:
                    with urllib.request.urlopen(base + "/api/status", timeout=1) as response:
                        assert json.load(response)["paypal"] == ("sandbox" if args.live_paypal_ui else "offline double")
                    break
                except (OSError, AssertionError):
                    if process.poll() is not None or time.monotonic() > deadline:
                        log.seek(0)
                        raise RuntimeError("Smoke server did not start: " + log.read())
                    time.sleep(0.2)
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True, channel=args.channel)
                context = browser.new_context(viewport={"width": 1440, "height": 1000})
                page = context.new_page()
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                if args.live_paypal_ui:
                    created = context.request.post(base + "/api/session", data={"role": "buyer"})
                    assert created.ok, created.text()
                    page.goto(base + "/wish")
                    expect(page.locator("paypal-button")).to_be_visible(timeout=30000)
                    expect(page.get_by_text("PayPal JavaScript SDK v6", exact=False)).to_be_visible()
                    assert not errors, str(errors)
                    if args.out:
                        page.screenshot(path=str(args.out / "paypal-v6-real-sandbox.png"))
                    browser.close()
                    print("PASS: real sandbox client token, v6 SDK load, vault eligibility and official button")
                    print("No wallet approval, payment, settlement or webhook registration was performed.")
                    return
                page.goto(base)
                page.get_by_role("link", name="Take the guided tour", exact=True).click()
                page.get_by_role("button", name="Start the guided tour", exact=True).click()
                expect(page.locator("textarea").first).to_have_value("A 10 kg vegetable box from a local farm, up to €28")
                page.get_by_role("button", name="Read it back", exact=True).click()
                expect(page.locator(".readback")).to_be_visible(timeout=20000)
                page.get_by_role("button", name="Sign with this browser's key", exact=True).click()
                wait_json(context.request, base + "/api/matches", lambda data: any(m["rule"] == "threshold" and m["status"] == "captured" for m in data["matches"]))
                expect(page.get_by_text("Your group purchase:")).to_contain_text("captured", timeout=15000)
                print("PASS: signed buyer mandate clears and captures the 40-person group")
                if args.out:
                    page.screenshot(path=str(args.out / "tour-buyer.png"), full_page=True)
                page.get_by_role("button", name="Next: Read a shelf").click()
                page.get_by_role("button", name="Use the sample shelf photo", exact=True).click()
                page.get_by_role("button", name="Read my shelf", exact=True).click()
                expect(page.get_by_role("heading", name="4 items found")).to_be_visible(timeout=20000)
                if args.out:
                    page.screenshot(path=str(args.out / "tour-seller.png"), full_page=True)
                page.get_by_role("button", name="Start the clock", exact=True).first.click()
                expect(page.get_by_role("tab", name="Live & pickup")).to_have_attribute("aria-selected", "true")
                # A stock fixture leaves room for this buyer alongside the active simulated market.
                lot = context.request.post(base + "/api/lots", data={
                    "name": "Smoke-test loaf", "category": "bread", "quantity": 500,
                    "mode": "clock", "start": 100, "floor": 100, "duration_min": 2,
                    "step_s": 5, "pickup_window_min": 90,
                })
                assert lot.ok, lot.text()
                lot_id = lot.json()["id"]
                page.get_by_role("button", name="Next: Find your pickup").click()
                page.locator("textarea").first.fill("A loaf under 1 euro")
                page.get_by_role("button", name="Read it back", exact=True).click()
                expect(page.locator(".readback")).to_be_visible(timeout=20000)
                page.get_by_role("button", name="Sign with this browser's key", exact=True).click()
                matches = wait_json(context.request, base + "/api/matches", lambda data: any(m["lot_id"] == lot_id and m["status"] == "held" for m in data["matches"]))["matches"]
                match = next((m for m in matches if m["lot_id"] == lot_id), None)
                assert match is not None, f"Expected pickup for lot {lot_id}, got {matches}"
                page.get_by_role("button", name="Next: Collect & get paid").click()
                for digit in match["pickup_code"]:
                    page.get_by_role("button", name=digit, exact=True).click()
                page.get_by_role("button", name="OK", exact=True).click()
                wait_json(context.request, base + "/api/console", lambda data: any(m["id"] == match["id"] and m["status"] == "captured" and m["paypal"]["payout_id"] for m in data["sales"]))
                pdf = context.request.get(base + f"/api/evidence/{match['id']}.pdf")
                assert pdf.ok and pdf.body().startswith(b"%PDF")
                print("PASS: seller photo, listing, buyer pickup, capture, payout and evidence PDF")
                page.get_by_role("button", name="Next: Inspect the proof").click()
                expect(page.locator("h1").filter(has_text="· console")).to_be_visible(timeout=15000)
                page.get_by_role("button", name="Quick check · latest 200", exact=True).click()
                expect(page.get_by_text("Recent segment verified:")).to_be_visible(timeout=20000)
                page.get_by_role("button", name="Verify the whole chain", exact=True).click()
                expect(page.get_by_text("Full chain verified:")).to_be_visible(timeout=20000)
                print("PASS: recent and full ledger verification in the production build")
                page.reload()
                expect(page.get_by_role("heading", name="Inspect the proof", exact=True)).to_be_visible()
                expect(page.locator("h1").filter(has_text="· console")).to_be_visible(timeout=15000)
                page.set_viewport_size({"width": 390, "height": 844})
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "Mobile layout overflows"
                if args.out:
                    page.screenshot(path=str(args.out / "tour-mobile.png"))
                page.route("**/api/ledger/verify/stream?recent=200", lambda route: route.fulfill(status=503, body="Unavailable"))
                page.get_by_role("button", name="Quick check · latest 200", exact=True).click()
                expect(page.get_by_role("alert")).to_contain_text("connection interrupted", timeout=10000)
                expect(page.get_by_role("button", name="Quick check · latest 200", exact=True)).to_be_enabled()
                print("PASS: interrupted audit reports failure and allows retry")
                # Contract test the real product component's SDK wiring against a mocked SDK.
                def sandbox_status(route):
                    response = route.fetch()
                    route.fulfill(response=response, json={**response.json(), "paypal": "sandbox"})
                page.route("**/api/status", sandbox_status)
                page.route("**/api/paypal/client-token", lambda route: route.fulfill(json={"client_token": "BROWSER-SAFE-TEST", "currency": "EUR"}))
                page.route("https://www.sandbox.paypal.com/web-sdk/v6/core", lambda route: route.fulfill(content_type="application/javascript", body=MOCK_SDK))
                page.goto(base + "/wish")
                expect(page.locator("paypal-button")).to_be_visible(timeout=15000)
                assert page.evaluate("window.sdkOptions.clientToken") == "BROWSER-SAFE-TEST"
                assert page.evaluate("window.eligibilityOptions.paymentFlow") == "VAULT_WITHOUT_PAYMENT"
                page.locator("paypal-button").click()
                expect(page.get_by_text("PayPal (sandbox)", exact=True)).to_be_visible(timeout=10000)
                print("PASS: v6 Save PayPal initialization and approval callback (mocked provider)")
                assert not errors, "Browser errors: " + str(errors)
                browser.close()
                print("PASS: mobile layout, reload/resume and zero uncaught browser errors")
        finally:
            if os.name == "nt" and process.poll() is None:
                # The Windows venv launcher can have a child interpreter; stop our whole server tree.
                subprocess.run(["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=subprocess.CREATE_NO_WINDOW, check=False)
            elif process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            log.close()


if __name__ == "__main__":
    main()
