"""Pre-flight: list all open tabs in user's Chrome."""
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp("http://127.0.0.1:9333")
    ctx = browser.contexts[0] if browser.contexts else browser.new_context()
    pages = ctx.pages
    print(f"Total tabs: {len(pages)}")
    for i, page in enumerate(pages):
        url = page.url
        title = ""
        try:
            title = page.title()
        except Exception:
            pass
        print(f"  [{i}] {url}  |  {title[:60]}")
    browser.close()