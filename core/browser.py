import os
import json
from playwright.sync_api import sync_playwright

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def get_cookies_path():
    """Path of cookies.json in the project root (one level above core/)."""
    project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(project_dir, "cookies.json")


def launch_authenticated_browser(headless=False):
    """Launch Chrome with the saved Instagram cookies. Returns (playwright, browser, context, page)."""
    p = sync_playwright().start()

    browser = p.chromium.launch(
        channel="chrome",
        headless=headless,
        args=["--start-maximized"]
    )

    context = browser.new_context(
        viewport={"width": 1280, "height": 720},
        user_agent=USER_AGENT
    )

    cookies_path = get_cookies_path()

    if os.path.exists(cookies_path):
        try:
            with open(cookies_path, "r", encoding="utf-8") as f:
                cookies = json.load(f)
            context.add_cookies(cookies)
            print(f"[ OK ] Browser ready, {len(cookies)} cookies loaded")
        except Exception as e:
            print(f"[FAIL] Could not read cookies.json: {e}")
    else:
        print(f"[WARN] cookies.json not found at {cookies_path}")

    page = context.new_page()
    return p, browser, context, page


if __name__ == "__main__":
    playwright_instance, browser, context, page = launch_authenticated_browser(headless=False)

    page.goto("https://www.instagram.com/", wait_until="domcontentloaded")
    print(f"[INFO] Current URL: {page.url}")

    input("\nPress ENTER to close the browser...")

    context.close()
    browser.close()
    playwright_instance.stop()
