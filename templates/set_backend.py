"""Switch to a different AI backend by opening / navigating a tab.

ARCHITECTURE: media-kit opens the Chrome process (CDP port). Playwright
connects and handles everything else: open tabs, navigate, close.

Usage:
    C:/Python312/python.exe -X utf8 set_backend.py <backend>

Behavior:
- If a tab for this backend already exists, navigate it to backend URL
- Else open a new tab to backend URL
- Detect login state
- Print JSON status
"""
import sys, io, json, time
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

from playwright.sync_api import sync_playwright
from backend_config import cfg, BACKENDS, page_host_matches, find_tab
from _helpers import watchdog, check_logged_in, check_input_visible, write_active_url
from media_kit import connect_browser


_LOC_TIMEOUT_MS = 3000
_WATCHDOG_S = 60


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in BACKENDS:
        print(f'usage: set_backend.py <{"|".join(BACKENDS)}>', file=sys.stderr)
        sys.exit(2)

    backend = sys.argv[1]
    c = cfg(backend)

    created_tab = None
    success = False
    with watchdog(_WATCHDOG_S, 'set_backend'):
        try:
            with sync_playwright() as p:
                browser = connect_browser(p)
                ctx = browser.contexts[0]

                # If a tab for this backend already exists, reuse it. Else open new.
                existing = find_tab(ctx, backend)
                if existing is not None:
                    page = existing
                    if not page_host_matches(page, backend):
                        page.goto(c['url'], wait_until='domcontentloaded', timeout=30000)
                else:
                    page = ctx.new_page()
                    created_tab = page
                    page.goto(c['url'], wait_until='domcontentloaded', timeout=30000)

                time.sleep(2.0)

                # M6 audit fix: use shared login helper
                logged_in = check_logged_in(page, c)
                ready = logged_in and check_input_visible(page, c, timeout_per_sel=5000)

                # Snapshot the active conversation for find_tab() next time
                write_active_url(backend, page.url)

                print(json.dumps({
                    'backend': backend,
                    'display': c['display'],
                    'url': page.url,
                    'ready': ready,
                    'logged_in': logged_in,
                    'tabs_total': len(ctx.pages),
                }, ensure_ascii=False))

                if not logged_in or not ready:
                    sys.exit(3)

                # Mark success BEFORE exiting the try block. On success the
                # newly opened tab MUST stay open so find_tab() can route
                # subsequent rounds to it.
                success = True
        finally:
            # On failure (sys.exit(3), exception, watchdog timeout) we close
            # the orphan tab we created so it doesn't leak. On success we
            # keep it open — that's the whole point of opening it.
            if not success and created_tab is not None:
                try:
                    created_tab.close()
                except Exception:
                    pass


if __name__ == '__main__':
    main()