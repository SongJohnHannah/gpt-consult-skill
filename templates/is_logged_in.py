"""Check if the given backend is logged in.

Usage:
    C:/Python312/python.exe -X utf8 is_logged_in.py [backend]

Finds an existing tab for this backend, OR opens a new one (does NOT close
the browser). If logged in → exits 0. Otherwise → exits 3.
"""
import sys, io, json
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

from playwright.sync_api import sync_playwright
from backend_config import cfg, BACKENDS, get_order, find_tab, page_host_matches
from _helpers import watchdog, check_logged_in, check_input_visible
from media_kit import connect_browser


_WATCHDOG_S = 45


def main():
    backend = sys.argv[1] if len(sys.argv) > 1 else get_order()[0]
    if backend not in BACKENDS:
        print(f'unknown backend: {backend}', file=sys.stderr)
        sys.exit(2)

    c = cfg(backend)
    created_tab = None
    with watchdog(_WATCHDOG_S, 'is_logged_in'):
        try:
            with sync_playwright() as p:
                browser = connect_browser(p)
                ctx = browser.contexts[0]

                page = find_tab(ctx, backend)
                if page is None:
                    page = ctx.new_page()
                    created_tab = page
                    page.goto(c['url'], wait_until='domcontentloaded', timeout=30000)
                elif not page_host_matches(page, backend):
                    page.goto(c['url'], wait_until='domcontentloaded', timeout=30000)

                logged_in = check_logged_in(page, c)
                if logged_in:
                    # composer present is the stronger signal
                    logged_in = check_input_visible(page, c, timeout_per_sel=4000)

                print(json.dumps({'backend': backend, 'logged_in': logged_in},
                                 ensure_ascii=False))
                sys.exit(0 if logged_in else 3)
        finally:
            if created_tab is not None:
                try:
                    created_tab.close()
                except Exception:
                    pass


if __name__ == '__main__':
    main()