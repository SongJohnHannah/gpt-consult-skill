"""media-kit bridge — lifecycle + CDP URL for gpt-consult.

Replaces hardcoded `http://127.0.0.1:9333` with calls into the media-kit
facade CLI so the Chrome endpoint is owned by media-kit, not gpt-consult.

Usage:
    from media_kit import cdp_url, is_browser_ready, ensure_browser

    if not is_browser_ready():
        ensure_browser()
    browser = p.chromium.connect_over_cdp(cdp_url())

cdp_url() is cached at module scope. Call refresh_cdp_url() if media-kit
restarts Chrome (new CDP URL); the next cdp_url() will re-fetch.

Use ensure_browser() explicitly before a session starts if Chrome may
not be running.
"""
import json
import os
import subprocess

MEDIA_KIT_REPO = os.environ.get("MEDIA_KIT_REPO", r"D:\www\claude_mcp\media-kit")

_cache: dict[str, str] = {}


def _call(fn_name: str):
    out = subprocess.check_output(
        ["node", "bin/media-kit-facade.mjs", fn_name],
        cwd=MEDIA_KIT_REPO,
        text=True,
        stderr=subprocess.PIPE,
    )
    return json.loads(out)


def cdp_url() -> str:
    """Return current CDP URL. Cached; call refresh_cdp_url() after Chrome restart."""
    if "url" not in _cache:
        _cache["url"] = _call("browserCdpUrl")
    return _cache["url"]


def refresh_cdp_url() -> None:
    """Drop the cache so the next cdp_url() call hits media-kit."""
    _cache.clear()


def is_browser_ready() -> bool:
    refresh_cdp_url()
    return _call("isBrowserReady")


def ensure_browser() -> str:
    refresh_cdp_url()
    return _call("ensureBrowser")


# C3 rev-2 (P1 from GPT Round 3) + Priority #3 (Round 7): wrap connect_over_cdp
# with full auto-recovery. Callers should use this instead of
# `p.chromium.connect_over_cdp(cdp_url())` directly.
#
# Recovery chain (Priority #3 — CDP lifecycle matrix):
#   1. Check is_browser_ready(). If False, ask media-kit to start Chrome
#      via ensure_browser() — this handles the case where the user closed
#      Chrome or media-kit itself died between calls.
#   2. Try connect_over_cdp with the current cached URL.
#   3. On failure, refresh the URL (Chrome may have been restarted by
#      media-kit under us, generating a new port) and retry ONCE.
#   4. If both attempts fail, raise — "no fallback". round.py is the
#      orchestrator that handles rc=124 separately.
_CDP_RETRY = 1


def connect_browser(p):
    """Connect Playwright to Chrome via CDP, with full auto-recovery.

    `p` is the sync_playwright() context value. Returns a connected Browser.

    On stale-URL or dead-Chrome failure, refreshes the cache, ensures
    Chrome is running, and retries. Raises the final error after the
    retry budget is exhausted.
    """
    last_err = None
    for attempt in range(_CDP_RETRY + 1):
        try:
            # Priority #3: if Chrome is dead, restart it BEFORE attempting
            # connect. is_browser_ready() refreshes the URL cache too, so
            # the subsequent cdp_url() reflects the current state.
            if not is_browser_ready():
                ensure_browser()
            return p.chromium.connect_over_cdp(cdp_url())
        except Exception as e:
            last_err = e
            if attempt < _CDP_RETRY:
                refresh_cdp_url()
                continue
            raise last_err