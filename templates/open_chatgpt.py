"""Open ChatGPT in user's logged-in Chrome via CDP.

Usage:
    C:/Python312/python.exe -X utf8 open_chatgpt.py

Prints the page object ID and URL on success.
"""
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

from playwright.sync_api import sync_playwright
from backend_config import find_tab, page_host_matches
from media_kit import connect_browser


def main():
    # Priority #3 (Round 7): use media-kit's connect_browser so that
    # dead-Chrome and stale-URL states are recovered automatically. The
    # previous version hardcoded the port via detect_cdp_port(); media-kit
    # now owns the CDP endpoint.
    with sync_playwright() as p:
        browser = connect_browser(p)
        ctx = browser.contexts[0]

        # Reuse existing ChatGPT tab if any, else open new
        page = find_tab(ctx, 'chatgpt')
        if page is None:
            page = ctx.new_page()
            page.goto('https://chatgpt.com', wait_until='domcontentloaded', timeout=30000)

        if not page_host_matches(page, 'chatgpt'):
            page.goto('https://chatgpt.com', wait_until='domcontentloaded', timeout=30000)

        print(f"[open_chatgpt] OK url={page.url} title={page.title()}")


if __name__ == '__main__':
    main()