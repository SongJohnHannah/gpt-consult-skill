"""Recovery: open a NEW TAB on the current backend with a fresh conversation.

ARCHITECTURE: media-kit opens the Chrome process (CDP port). Playwright
connects to that Chrome and handles everything else: open tabs, navigate,
click, close. The OLD stuck tab is closed AFTER the new tab is verified
working.

Usage:
    C:/Python312/python.exe -X utf8 reset_to_new_chat.py [backend]

Exit codes:
  0 = new tab ready
  3 = not logged in
  4 = no input appeared
"""
import sys, io, time
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

from playwright.sync_api import sync_playwright
from backend_config import cfg, find_tab
from _helpers import watchdog, check_logged_in, check_input_visible, write_active_url, ensure_expert_mode
from media_kit import connect_browser


_LOC_TIMEOUT_MS = 3000
_WATCHDOG_S = 90


def main():
    backend = sys.argv[1] if len(sys.argv) > 1 else 'chatgpt'
    c = cfg(backend)

    with watchdog(_WATCHDOG_S, 'reset_to_new_chat'):
        with sync_playwright() as p:
            browser = connect_browser(p)
            ctx = browser.contexts[0]

            # Snapshot the OLD tab for this backend — we'll close it later
            old_tab = find_tab(ctx, backend)

            # Open a NEW tab
            page = ctx.new_page()
            page.goto(c['url'], wait_until='domcontentloaded', timeout=30000)
            time.sleep(2.0)

            # M6 audit fix: use shared login helper
            if not check_logged_in(page, c):
                try:
                    page.close()
                except Exception:
                    pass
                print(f"[reset_to_new_chat] {c['display']} NOT logged in — please log in manually",
                      file=sys.stderr)
                sys.exit(3)

            # Try "New chat" sidebar in new tab
            clicked = False
            for sel in c['new_chat_selectors']:
                try:
                    loc = page.locator(sel).first
                    if loc.count() > 0:
                        loc.click(timeout=3000)
                        clicked = True
                        break
                except Exception:
                    continue

            if not clicked:
                page.goto(c['url'], wait_until='domcontentloaded', timeout=30000)
                time.sleep(1.0)

            # Wait for input box (M6: use shared helper)
            if not check_input_visible(page, c, timeout_per_sel=10000):
                try:
                    page.close()
                except Exception:
                    pass
                print(f"[reset_to_new_chat] {c['display']} no input appeared", file=sys.stderr)
                sys.exit(4)

            # Round 14: deepseek fresh tab defaults to fast mode — click
            # 深度思考 to switch to R1 expert. No-op for other backends.
            ensure_expert_mode(page, c)

            # New tab is verified working. Close the OLD stuck tab.
            old_closed = False
            if old_tab is not None and old_tab != page:
                try:
                    old_tab.close()
                    old_closed = True
                except Exception:
                    pass

            write_active_url(backend, page.url)
            time.sleep(0.5)
            active = find_tab(ctx, backend)
            print(f"[reset_to_new_chat] OK backend={backend} url={page.url} "
                  f"tab_active={active is not None} old_closed={old_closed}")


if __name__ == '__main__':
    main()