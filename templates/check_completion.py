"""Detect whether the latest AI reply signals task completion.

Usage:
    C:/Python312/python.exe -X utf8 check_completion.py [backend]

Finds the active tab for `backend` by URL host (does NOT assume pages[0]).
NEVER closes the browser.

Logic (priority order):
1. Literal "STATUS: COMPLETE" line in reply → COMPLETE
2. Strong completion phrases in last 500 chars → COMPLETE
3. Otherwise → NOT_COMPLETE

Exit code: 0 = complete, 1 = not complete, 2 = no reply found / no tab.
"""
import sys, io, re
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

from playwright.sync_api import sync_playwright
from backend_config import cfg, get_order, find_tab
from _helpers import watchdog
from media_kit import connect_browser


_LOC_TIMEOUT_MS = 3000
_WATCHDOG_S = 45


STRONG_MARKERS = [
    r'STATUS:\s*COMPLETE',
    r'STATUS:\s*DONE',
    r'\[TASK COMPLETE\]',
    r'\[完成\]',
    r'任务完成[，,。.\s]',
    r'已完成.*无需',
    r'所有需求已满足',
    r'无需进一步修改',
    r'no further changes',
    r'all requirements met',
    r'task is (?:complete|done)',
    r'work is (?:complete|done)',
]


def main():
    backend = sys.argv[1] if len(sys.argv) > 1 else get_order()[0]
    c = cfg(backend)

    with watchdog(_WATCHDOG_S, 'check_completion'):
        with sync_playwright() as p:
            browser = connect_browser(p)
            ctx = browser.contexts[0]
            page = find_tab(ctx, backend)
            if page is None:
                print(f'NO_TAB for {backend}', file=sys.stderr)
                sys.exit(2)

            msgs = None
            for sel in c['reply_selectors']:
                try:
                    cnt = page.locator(sel).count()
                    if cnt > 0:
                        msgs = page.locator(sel)
                        break
                except Exception:
                    continue

            if msgs is None or msgs.count() == 0:
                print('NO_REPLY', file=sys.stderr)
                sys.exit(2)

            text = msgs.nth(msgs.count() - 1).inner_text(timeout=5000)
            tail = text[-500:]

            for pat in STRONG_MARKERS:
                if re.search(pat, text, re.IGNORECASE | re.MULTILINE):
                    print(f'COMPLETE: matched "{pat}"')
                    sys.exit(0)

            soft = re.search(r'(完成|done|finished|无需再|all set)', tail, re.IGNORECASE)
            if soft:
                print(f'NOT_COMPLETE: soft marker "{soft.group(0)}" in tail (not strong enough)')
                sys.exit(1)

            print('NOT_COMPLETE: no completion marker found')
            sys.exit(1)


if __name__ == '__main__':
    main()