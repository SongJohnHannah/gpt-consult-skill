"""Health check + user-interrupt detector for the GPT consult loop.

Usage:
    C:/Python312/python.exe -X utf8 check_status.py [backend]

Finds the active tab for `backend` by URL host. If no tab is open for it,
opens a new one (and CLOSES it at the end — M2 audit fix: prior versions
leaked a tab on every run). NEVER closes the browser.

Prints JSON:
{
  "backend": "chatgpt",
  "cdp_ok": bool,
  "backend_open": bool,
  "logged_in": bool,
  "stream_running": bool,
  "user_typed": bool,
  "user_text_snippet": str,
  "tabs_total": int
}
"""
import sys, io, json, subprocess
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

from playwright.sync_api import sync_playwright
from backend_config import cfg, get_order, find_tab, page_host_matches
from _helpers import watchdog, check_logged_in, is_real_streaming
from media_kit import connect_browser, cdp_url


_LOC_TIMEOUT_MS = 3000
_WATCHDOG_S = 60  # check_status should be fast


def cdp_alive_from_url(url: str) -> bool:
    """Hit /json/version on the same port as the media-kit CDP URL.

    M2 audit fix: prior version hardcoded 9333, which broke if media-kit
    exposed a different port via MEDIAKIT_BROWSER_URL env var. Derive the
    port from the CDP URL we actually plan to connect to.
    """
    from urllib.parse import urlparse
    parsed = urlparse(url)
    port = parsed.port or (443 if parsed.scheme == 'https' else 80)
    try:
        out = subprocess.check_output(
            ['curl', '-s', '-m', '2', f'http://127.0.0.1:{port}/json/version'],
            stderr=subprocess.DEVNULL,
        )
        return b'Browser' in out
    except Exception:
        return False


def check(backend: str):
    c = cfg(backend)
    status = {
        'backend': backend,
        'display': c['display'],
        'cdp_ok': False,
        'backend_open': False,
        'logged_in': True,
        'stream_running': False,
        'user_typed': False,
        'user_text_snippet': '',
        'tabs_total': 0,
    }

    url = cdp_url()
    status['cdp_ok'] = cdp_alive_from_url(url)
    if not status['cdp_ok']:
        print(json.dumps(status, ensure_ascii=False))
        return

    # Track if we created a tab so we can close it at the end (M2 audit fix).
    created_tab = None
    status_emitted = False
    with watchdog(_WATCHDOG_S, 'check_status'):
        try:
            with sync_playwright() as p:
                browser = connect_browser(p)
                ctx = browser.contexts[0]
                status['tabs_total'] = len(ctx.pages)

                page = find_tab(ctx, backend)
                if page is None:
                    # M2 rev-3 (GPT Round 4): mark ownership BEFORE goto so
                    # navigation timeout triggers finally → close().
                    page = ctx.new_page()
                    created_tab = page
                    page.goto(c['url'], wait_until='domcontentloaded', timeout=30000)
                status['backend_open'] = page_host_matches(page, backend)

                # M6 audit fix: use shared helper
                if not check_logged_in(page, c):
                    status['logged_in'] = False

                # Stream detection (Priority #1: semantic check — visible
                # stop button, not just count > 0).
                if is_real_streaming(page, c):
                    status['stream_running'] = True

                # User-interrupt detection
                asst_selectors = c['reply_selectors']
                user_selectors = ['[data-message-author-role="user"]', '[data-role="user"]']
                asst_count = 0
                for sel in asst_selectors:
                    try:
                        asst_count = page.locator(sel).count()
                        if asst_count > 0:
                            break
                    except Exception:
                        continue
                user_count = 0
                for sel in user_selectors:
                    try:
                        user_count = page.locator(sel).count()
                        if user_count > 0:
                            break
                    except Exception:
                        continue
                if asst_count > 0 and user_count > asst_count:
                    for sel in user_selectors:
                        try:
                            cnt = page.locator(sel).count()
                            if cnt > 0:
                                last = page.locator(sel).nth(cnt - 1)
                                status['user_typed'] = True
                                status['user_text_snippet'] = last.inner_text(timeout=2000)[:200]
                                break
                        except Exception:
                            continue

                print(json.dumps(status, ensure_ascii=False))
                status_emitted = True
        finally:
            # M2 rev-2: finally-block guarantees cleanup on normal errors.
            # (Watchdog's os._exit still bypasses this — documented as a known
            # limitation. Round.py is the recovery layer for that case.)
            if created_tab is not None:
                try:
                    created_tab.close()
                except Exception:
                    pass


if __name__ == '__main__':
    backend = sys.argv[1] if len(sys.argv) > 1 else get_order()[0]
    check(backend)