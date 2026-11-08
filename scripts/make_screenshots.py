"""Take the README screenshots from a running demo instance, with Chrome and Playwright.

Dev tool only (not a dependency of the app): `pip install playwright`; it drives the Google Chrome
that is already installed (no browser download). Steps:
    DATA_DIR=./demo-data python scripts/seed_demo.py
    DATA_DIR=./demo-data JOBS_ENABLED=false uvicorn app.main:app --port 8100 &
    python scripts/make_screenshots.py [http://localhost:8100]
"""

import re
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8100"
OUT = Path(__file__).resolve().parent.parent / "docs" / "assets"
PASSWORD = "demo-password-1"
SIZE = {"width": 1280, "height": 800}


def sign_in(browser, username: str, theme: str):
    context = browser.new_context(
        viewport=SIZE, device_scale_factor=1, color_scheme=theme, locale="en-US"
    )
    page = context.new_page()
    page.goto(f"{BASE}/login")
    page.fill("input[name=username]", username)
    page.fill("input[name=password]", PASSWORD)
    page.click("button[type=submit]")
    page.wait_for_url(re.compile(r"/(dashboard|artists)"))
    return context, page


def shot(page, path: str, name: str, *, wait: str | None = None) -> None:
    page.goto(f"{BASE}{path}")
    page.wait_for_load_state("load")
    page.wait_for_timeout(1500)  # the Tailwind CDN script styles the page after it loads
    if wait:
        page.wait_for_selector(wait)
    page.wait_for_timeout(400)
    page.screenshot(path=str(OUT / name))
    print("saved", name)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        # one browser per account: simple and robust
        for username, theme in (("admin", "dark"), ("marco", "light")):
            browser = p.chromium.launch(channel="chrome", headless=True)
            context, page = sign_in(browser, username, theme)
            if theme == "dark":
                shot(page, "/artists", "screenshot-artists.png")
                shot(page, "/requests", "screenshot-requests.png")
            else:
                page.goto(f"{BASE}/artists")
                link = page.locator("a[href^='/artist/']", has_text="Aurora Vale").first
                page.set_viewport_size({"width": 1280, "height": 1300})  # the whole artist page
                shot(page, link.get_attribute("href"), "screenshot-artist.png")
                page.set_viewport_size(SIZE)
                shot(page, "/albums", "screenshot-albums.png")
            context.close()
            browser.close()


if __name__ == "__main__":
    main()
