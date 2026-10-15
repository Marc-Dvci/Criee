"""Capture the gallery from a running Criée: python scripts/gallery.py <base_url> <out_dir>

Drives the real UI in headless Chromium as a judge would: play both sides, wish, read a shelf,
clear a clock, collect, then the console, the assistant and the ledger.
"""
import re
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE, OUT = sys.argv[1].rstrip("/"), Path(sys.argv[2])
EXE = sys.argv[3] if len(sys.argv) > 3 else None
SHELF = Path(__file__).resolve().parent.parent / "data" / "samples" / "test_shelf.jpg"
OUT.mkdir(parents=True, exist_ok=True)
errors: list[str] = []

with sync_playwright() as p:
    b = p.chromium.launch(headless=True, executable_path=EXE)
    desk = b.new_context(viewport={"width": 1440, "height": 900}, device_scale_factor=2)
    page = desk.new_page()
    page.on("pageerror", lambda e: errors.append(str(e)))

    def shot(pg, name, full=False, wait=1200, clip=None):
        pg.wait_for_timeout(wait)
        pg.screenshot(path=str(OUT / f"{name}.png"), full_page=full, clip=clip)

    page.goto(BASE + "/board", wait_until="networkidle"); shot(page, "01_board", wait=4000)
    page.goto(BASE + "/", wait_until="networkidle"); shot(page, "02_home")
    page.goto(BASE + "/play"); page.get_by_role("button", name="Start").click(); page.wait_for_timeout(3000)

    page.goto(BASE + "/wish")
    page.fill("textarea", "An organic sourdough loaf twice a week, under €5, after 18:00 on weekdays")
    page.get_by_role("button", name="Read it back").click(); page.wait_for_selector(".readback", timeout=90000)
    card = page.locator(".card.hero").first
    card.scroll_into_view_if_needed(); shot(page, "03_readback", clip=None)
    page.get_by_role("button", name="Change").click()
    page.fill("textarea", "Pain de seigle sans gluten, up to 6 euros")
    page.get_by_role("button", name="Read it back").click(); page.wait_for_selector(".readback", timeout=90000)
    page.get_by_role("button", name="Sign with this browser's key").click(); page.wait_for_timeout(3000)

    mob = b.new_context(viewport={"width": 430, "height": 932}, device_scale_factor=3, storage_state=desk.storage_state())
    phone = mob.new_page()
    phone.on("pageerror", lambda e: errors.append(str(e)))
    phone.goto(BASE + "/sell"); phone.set_input_files("input[type=file]", str(SHELF))
    phone.get_by_role("button", name="Read my shelf").click(); phone.wait_for_selector("text=found", timeout=120000)
    shot(phone, "04_shelf_read", wait=2500)
    rye = phone.locator(".card", has=phone.locator("h3", has_text="seigle")).first
    rye.scroll_into_view_if_needed(); shot(phone, "05_lot_editor")
    rye.locator("input[type=range]").fill("15")
    rye.get_by_role("button", name="Start the clock").click(); phone.wait_for_timeout(9000)
    shot(phone, "06_live_clock", wait=2000)

    page.goto(BASE + "/wish"); page.wait_for_selector(".pickup-code", timeout=60000); shot(page, "07_pickup_code", wait=2500)
    code = re.search(r"\d{6}", page.locator(".pickup-code").first.inner_text()).group(0)
    phone.reload(); phone.get_by_role("tab", name="Live & pickup").click()
    for ch in code:
        phone.get_by_role("button", name=ch, exact=True).click()
    shot(phone, "08_keypad", wait=400)
    phone.get_by_role("button", name="OK").click(); phone.wait_for_timeout(12000)
    phone.locator("text=Today's sales").scroll_into_view_if_needed(); shot(phone, "09_paid")

    page.goto(BASE + "/console"); shot(page, "10_console", wait=3000)
    page.get_by_role("tab", name=re.compile("Order book")).click(); shot(page, "11_orderbook", wait=2500)
    page.get_by_role("tab", name=re.compile("Payouts")).click(); shot(page, "12_payouts", wait=1500)

    phone.goto(BASE + "/sell"); phone.get_by_role("tab", name="Assistant").click()
    box = phone.locator("input.input.grow")
    box.fill("Invoice Café Brant for 20 baguettes at 1.10 euros each, email cafe.brant@example.com, send it and show me the QR code")
    phone.get_by_role("button", name="Send").click()
    phone.wait_for_selector("img[alt='PayPal invoice QR code']", timeout=150000)
    shot(phone, "13_assistant_invoice", wait=1500)

    page.goto(BASE + "/ledger"); page.get_by_role("button", name="Verify the whole chain").click(); shot(page, "14_ledger", wait=3000)
    page.goto(BASE + "/board", wait_until="networkidle"); shot(page, "15_board_after", wait=4000)
    b.close()

print("\n".join(errors) or "no page errors")
