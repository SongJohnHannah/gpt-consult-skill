"""Extract the LATEST AI message text from the active tab of the given backend.

Usage:
    C:/Python312/python.exe -X utf8 extract_reply.py [backend]

Behavior:
- Finds the active tab for `backend` by URL host (does NOT assume pages[0]).
- If no tab is open for this backend, opens a new one.
- Exits 1 if no reply found.
"""
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

from playwright.sync_api import sync_playwright
from backend_config import cfg, get_order, find_tab
from _helpers import watchdog, find_real_reply_text
from media_kit import connect_browser


_LOC_TIMEOUT_MS = 3000
_WATCHDOG_S = 45


def main():
    backend = sys.argv[1] if len(sys.argv) > 1 else get_order()[0]
    c = cfg(backend)

    # M2 rev-3 (GPT Round 4): mark created_tab ownership BEFORE goto so any
    # exception between new_page() and the end of the with block still closes
    # the tab. Watchdog os._exit bypasses finally — round.py covers that.
    created_tab = None
    with watchdog(_WATCHDOG_S, 'extract_reply'):
        try:
            with sync_playwright() as p:
                browser = connect_browser(p)
                ctx = browser.contexts[0]

                page = find_tab(ctx, backend)
                if page is None:
                    page = ctx.new_page()
                    created_tab = page
                    page.goto(c['url'], wait_until='domcontentloaded', timeout=30000)

                # Priority #1: use semantic helper — refuses to return text
                # from hidden / stale assistant nodes. skip_baseline=True
                # because extract_reply wants the latest reply regardless of
                # round tracking (it may be called on a fresh conversation).
                text = find_real_reply_text(page, c, baseline_user=0,
                                            skip_baseline=True)
                if not text:
                    print(f'[extract_reply] no reply found on {c["display"]}',
                          file=sys.stderr)
                    sys.exit(1)

                print(text)
        finally:
            if created_tab is not None:
                try:
                    created_tab.close()
                except Exception:
                    pass


if __name__ == '__main__':
    main()