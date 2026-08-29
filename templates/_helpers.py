"""Shared helpers for gpt-consult templates.

Three concerns live here:

1. watchdog(): force-exit if sync_playwright / connect_over_cdp / page.locator
   hangs longer than the budget. round.py's recovery chain only fires if
   send_message.py exits cleanly with rc=1 — a silent hang breaks the chain.
   Hard-kills the process with rc=124 (matches GNU timeout convention) so
   the caller can distinguish from rc=1 (genuine timeout).

2. check_logged_in(): single source of truth for "does this backend tab
   look logged in". login_selectors match = NOT logged in. Replaces the
   three duplicated loops in set_backend.py / check_status.py /
   is_logged_in.py so a selector update only changes one place.

3. active-conversation tracker (read_active_url / write_active_url):
   remembers the URL of the tab currently considered "active" for each
   backend. find_tab() uses it to disambiguate multiple same-host tabs.
   Set by send_message.py after a successful round; consumed by the next
   find_tab() call.

4. Semantic-identity helpers (Round 5+ Priority #1): the
   FIND → ASSERT SEMANTIC IDENTITY → ACT → ASSERT EFFECT pattern.
   A selector match that points at a hidden / stale element is treated
   as "not found" so the skill never silently operates on the wrong DOM node.

   - find_real_composer(): returns the first selector match that is
     actually editable (contenteditable OR visible textarea, never a
     hidden fallback).
   - find_real_reply_text(): returns the text of the latest assistant
     message that is non-empty AND visible AND newer than baseline_user
     (i.e. actually belongs to this round, not a stale node from before).
   - is_real_streaming(): the stop-button is visible AND attached to
     active generation (not a stale button from a previous round).
   - verify_message_sent(): after fill_input, asserts a user message
     actually appeared in DOM (the "ACT → ASSERT EFFECT" step).
"""
from __future__ import annotations

import enum
import hashlib
import os
import sys
import threading
import time
from contextlib import contextmanager


ACTIVE_DIR = os.environ.get('GPT_CONSULT_DIR', '.gpt_consult')


# Round 8 P2: stream-aware reply terminal status. round.py consults this to
# decide whether to retry (TIMEOUT / BROWSER_DEAD) vs continue waiting
# (STREAMING) vs declare success (DONE).
class ReplyStatus(enum.Enum):
    DONE = 'done'
    TIMEOUT = 'timeout'         # not streaming AND not stable within hard_deadline
    STREAMING = 'streaming'     # still streaming at hard_deadline (will retry)
    BROWSER_DEAD = 'browser_dead'  # CDP gone / page unresponsive


# Round 8 P2: timeout configuration. Per GPT design:
#   - hard_deadline = start + timeout_s (overall budget)
#   - if streaming, extend window via stream_grace_s
#   - if streaming is idle (no content change) for > stream_idle_s, treat as
#     stuck and return TIMEOUT (otherwise a frozen stream would loop forever)
GPT_CONSULT_REPLY_TIMEOUT_S = float(os.environ.get(
    'GPT_CONSULT_REPLY_TIMEOUT_S', '600'))
GPT_CONSULT_STREAM_GRACE_S = float(os.environ.get(
    'GPT_CONSULT_STREAM_GRACE_S', '120'))
GPT_CONSULT_STREAM_IDLE_S = float(os.environ.get(
    'GPT_CONSULT_STREAM_IDLE_S', '90'))
GPT_CONSULT_MAX_STREAM_S = float(os.environ.get(
    'GPT_CONSULT_MAX_STREAM_S', '900'))


def active_conv_path(backend: str) -> str:
    return os.path.join(ACTIVE_DIR, f'active_{backend}.txt')


def read_active_url(backend: str) -> str:
    try:
        return open(active_conv_path(backend), encoding='utf-8').read().strip()
    except FileNotFoundError:
        return ''


def write_active_url(backend: str, url: str) -> None:
    """Persist the active conversation URL for `backend`.

    Round 8 P1: only stores the URL if a conversation_id is extractable.
    For URLs with no conversation_id (e.g. fresh / root), clears the stored
    URL instead — this signals "no specific active conversation" to
    find_tab, which then uses discovery rules (and refuses to guess when
    multiple tabs match).
    """
    os.makedirs(ACTIVE_DIR, exist_ok=True)
    # Late import: backend_config imports nothing from _helpers at module
    # load, but extract_conversation_id depends on it. Importing here avoids
    # any circular-import surprises.
    from backend_config import extract_conversation_id
    conv_id = extract_conversation_id(backend, url)
    path = active_conv_path(backend)
    if conv_id:
        with open(path, 'w', encoding='utf-8') as f:
            f.write(url)
    else:
        # No identifiable conversation — clear, so find_tab uses discovery.
        try:
            os.remove(path)
        except FileNotFoundError:
            pass


@contextmanager
def watchdog(timeout_s: float, label: str):
    """Force-exit the process after `timeout_s` seconds.

    Uses os._exit (skips cleanup) because Playwright sync API can wedge the
    main thread in C code that ignores signals. round.py is the recovery
    layer and is designed to handle abrupt exits.
    """
    def _kill():
        sys.stderr.write(f'[{label}] watchdog: {timeout_s}s elapsed, force exit (rc=124)\n')
        sys.stderr.flush()
        os._exit(124)
    t = threading.Timer(timeout_s, _kill)
    t.daemon = True
    t.start()
    try:
        yield
    finally:
        t.cancel()


def check_logged_in(page, c: dict) -> bool:
    """True if `page` looks logged into backend `c`.

    login_selectors match = NOT logged in (login walls expose a button/anchor).
    On timeout/exception per selector, that selector is treated as not matching
    (preserves prior default). Caller can still treat the whole backend as
    "could not verify" by combining with check_input_visible().
    """
    for sel in c['login_selectors']:
        try:
            if page.locator(sel).count() > 0:
                return False
        except Exception:
            continue
    return True


def check_input_visible(page, c: dict, timeout_per_sel: int = 4000) -> bool:
    """True if any input_selectors is visible (proves the composer is loaded).

    Replaces duplicated input-wait loops in set_backend / reset_to_new_chat /
    is_logged_in. Returns True on first visible selector; False if none of the
    selectors becomes visible within timeout_per_sel each.
    """
    for sel in c['input_selectors']:
        try:
            page.locator(sel).first.wait_for(state='visible', timeout=timeout_per_sel)
            return True
        except Exception:
            continue
    return False


# ---------------------------------------------------------------------------
# Semantic-identity helpers (Priority #1 audit fix)
# ---------------------------------------------------------------------------

def _is_visible(loc) -> bool:
    """True if `loc`'s first element is in the rendered tree."""
    try:
        return loc.first.evaluate("e => e.offsetParent !== null")
    except Exception:
        return False


def _is_editable(loc) -> bool:
    """True if `loc`'s first element is actually editable.

    Catches the failure mode of stale selectors matching a hidden fallback
    textarea or a contenteditable that's been disabled by the framework.
    """
    try:
        return loc.first.evaluate(r"""e => {
            if (!e) return false;
            const ce = e.getAttribute && e.getAttribute('contenteditable');
            if (ce === 'true' || ce === 'plaintext-only') return true;
            const tag = e.tagName;
            if (tag === 'TEXTAREA' || tag === 'INPUT') {
                if (e.disabled || e.readOnly) return false;
                return true;
            }
            if (e.isContentEditable) return true;
            return false;
        }""")
    except Exception:
        return False


def find_real_composer(page, c: dict, timeout_ms: int = 5000):
    """Return the first composer selector that is both VISIBLE and EDITABLE.

    Returns a Locator pointing at the match, or None if no selector in
    c['input_selectors'] passes the semantic check within timeout_ms.
    """
    for sel in c['input_selectors']:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state='attached', timeout=timeout_ms)
            if _is_visible(loc) and _is_editable(loc):
                return loc
        except Exception:
            continue
    return None


def find_real_reply_text(page, c: dict, baseline_user: int,
                         skip_baseline: bool = False) -> str:
    """Return the inner text of the latest assistant message that is non-empty
    AND visible AND newer than baseline_user.

    "Newer than baseline_user" is approximated by checking that the user-count
    has actually exceeded baseline_user — if not, the round hasn't visibly
    started and we refuse to return any text (avoid returning stale nodes from
    a prior round).

    Round 9 audit invariant (GPT-verified): a fast poll immediately after
    submission MUST refuse text until `user_count > baseline_user`. Otherwise
    a stale assistant reply from the previous round could satisfy the
    `text stable 3s` condition in wait_for_reply and report a false DONE.

    Implementation enforces this via:
        if baseline_user > 0 and user_present <= baseline_user:
            return ''

    Pass `skip_baseline=True` from callers that want the latest reply
    regardless of round tracking (e.g. extract_reply.py). Defaults to False
    for safety.

    Returns '' if no selector passes the semantic check.
    """
    if not skip_baseline:
        try:
            user_present = 0
            for sel in ['[data-message-author-role="user"]', '[data-role="user"]']:
                try:
                    user_present = max(user_present, page.locator(sel).count())
                except Exception:
                    continue
            if baseline_user > 0 and user_present <= baseline_user:
                return ''
        except Exception:
            return ''

    for sel in c['reply_selectors']:
        try:
            loc = page.locator(sel)
            cnt = loc.count()
            if cnt == 0:
                continue
            last = loc.nth(cnt - 1)
            if not _is_visible(last):
                continue
            text = last.inner_text(timeout=2000).strip()
            if text:
                return text
        except Exception:
            continue
    return ''


def is_real_streaming(page, c: dict, prev_reply_text: str = '') -> bool:
    """True if any streaming signal is active.

    Round 9 minor improvement (GPT suggestion): combine multiple signals
    rather than relying on stop-button alone. A UI redesign could leave
    the stop button visible for non-generation reasons; conversely, a
    long reasoning pause could hide it while the model is still working.

    Signals (OR-combined):
      1. stop-button selector visible (any selector in c['stream_selectors'])
      2. assistant reply text changed since the caller's `prev_reply_text`

    Returns True if ANY signal is active.
    """
    # Signal 1: stop button visible
    for sel in c['stream_selectors']:
        try:
            loc = page.locator(sel).first
            if loc.count() == 0:
                continue
            if _is_visible(loc):
                return True
        except Exception:
            continue

    # Signal 2: reply text changed since last call (caller-supplied)
    if prev_reply_text:
        try:
            for sel in c['reply_selectors']:
                loc = page.locator(sel)
                cnt = loc.count()
                if cnt == 0:
                    continue
                last = loc.nth(cnt - 1)
                if not _is_visible(last):
                    continue
                cur_text = last.inner_text(timeout=2000).strip()
                if cur_text and cur_text != prev_reply_text:
                    return True
        except Exception:
            pass

    return False


def verify_message_sent(page, c: dict, baseline_user: int,
                        timeout_s: float = 10.0) -> bool:
    """After fill_input, assert a user message actually appeared in DOM.

    Used as the "ASSERT EFFECT" step: even if fill_input exited without
    error, the message may have been dropped (React state didn't sync,
    Enter was swallowed, etc.). Polls for user_count > baseline_user within
    timeout_s.

    Returns True if a new user message appeared in DOM. Caller should treat
    False as a hard failure (the round cannot proceed without a user
    message in DOM).

    Default 10s (was 5s): chatgpt can take 5-8s to commit a sent message
    in the DOM under load — see Bug A repro. A final post-deadline poll
    catches the case where the message lands just after the deadline.
    """
    def _user_count() -> int:
        n = 0
        for sel in ['[data-message-author-role="user"]', '[data-role="user"]']:
            try:
                n = max(n, page.locator(sel).count())
            except Exception:
                continue
        return n

    def _has_text() -> bool:
        for sel in ['[data-message-author-role="user"]', '[data-role="user"]']:
            try:
                loc = page.locator(sel)
                cnt = loc.count()
                if cnt == 0:
                    continue
                if loc.nth(cnt - 1).inner_text(timeout=2000).strip():
                    return True
            except Exception:
                continue
        return False

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if _user_count() > baseline_user and _has_text():
            return True
        time.sleep(0.3)
    # Final post-deadline check: catch messages that land just after the
    # deadline (chatgpt sub-100ms misses).
    if _user_count() > baseline_user and _has_text():
        return True
    return False


# ---------------------------------------------------------------------------
# Round 8 P3 — pending-attachment detection + Send-button submission
# ---------------------------------------------------------------------------
# Problem: chatgpt auto-attaches an image preview when the text contains a URL
# or `data:` URI. The preview loads (or fails to load) asynchronously; while
# it's pending, Enter is swallowed and the message stays stuck in the composer.
# Symptom observed 2026-08-28: 4229-char markdown payload had Enter silently
# dropped. User had to manually cancel the auto-attached preview to proceed.
#
# Fix per GPT Round 8 design (D + C + B): after insert_text, detect pending
# attachments via multi-signal DOM check, clear them, then submit via the Send
# button (not Enter). Enter remains a fallback only.
#
# `submit_message` is preferred over Enter because Enter is influenced by
# focus state, IME, attachment previews, markdown processing, autocomplete,
# modals, upload state — all of which can silently swallow the keystroke.

_ATTACHMENT_REMOVE_SELECTORS = [
    '[aria-label*="Remove"]',
    '[aria-label*="Cancel"]',
    'button[aria-label*="Remove attachment"]',
    '[data-testid*="remove-attachment"]',
]


def detect_pending_attachments(page) -> bool:
    """True if the composer currently shows an attachment preview that hasn't
    been finalized yet.

    Multi-signal check — file input alone is not enough because it persists
    even after successful uploads. The signal we want is "user-visible
    attachment preview that is still pending".
    """
    try:
        # File input has files AND there is a visible preview with a remove
        # button — that's the auto-attach-from-text signature.
        state = page.evaluate(r"""() => {
            const fi = document.querySelector('input[type="file"]');
            const fileCount = fi ? fi.files.length : 0;
            // Visible attachment item with remove control = pending.
            const removeBtns = document.querySelectorAll(
                '[aria-label*="Remove" i], [aria-label*="Cancel" i], ' +
                '[data-testid*="remove-attachment" i]'
            );
            let visibleRemove = 0;
            for (const b of removeBtns) {
                if (b.offsetParent !== null) visibleRemove++;
            }
            // Upload progress (image not finished loading) — async attribute
            // or aria-busy on attachment container.
            const busy = !!document.querySelector(
                '[data-testid*="attachment" i][aria-busy="true"], ' +
                '[class*="attachment" i][aria-busy="true"]'
            );
            return { fileCount, visibleRemove, busy };
        }""")
        # Pending signature: files in input AND (visible remove btn OR busy)
        if state['fileCount'] > 0 and (state['visibleRemove'] > 0
                                       or state['busy']):
            return True
        return False
    except Exception:
        return False


def clear_pending_attachments(page, timeout_ms: int = 5000) -> bool:
    """Click visible remove/cancel buttons until attachment count is 0.

    Returns True if all pending attachments cleared within timeout_ms.
    """
    deadline = time.time() + timeout_ms / 1000.0
    while time.time() < deadline:
        if not detect_pending_attachments(page):
            return True
        clicked = False
        for sel in _ATTACHMENT_REMOVE_SELECTORS:
            try:
                loc = page.locator(sel)
                cnt = loc.count()
                for i in range(cnt - 1, -1, -1):
                    el = loc.nth(i)
                    if el.evaluate("e => e.offsetParent !== null"):
                        el.click(force=True, timeout=1000)
                        clicked = True
            except Exception:
                continue
        if not clicked:
            # Nothing to click but state still shows pending — bail.
            return not detect_pending_attachments(page)
        time.sleep(0.4)
    return not detect_pending_attachments(page)


def submit_message(page, c: dict) -> bool:
    """Submit composer contents via the Send button (preferred) or Enter (fallback).

    Send button is preferred because Enter can be swallowed by attachment
    previews, focus loss, IME state, or modal dialogs. Send button click is a
    direct DOM action on the form's submit handler.

    Returns True if the submit action was triggered (caller still needs
    verify_message_sent to confirm the user message actually appeared in DOM).
    """
    # Send button is backend-specific. Try the most common chatgpt selector
    # first, then a generic aria-label match.
    send_selectors = [
        'button[data-testid="send-button"]',
        'button[aria-label*="Send"]',
        'button[aria-label*="发送"]',
        'button[type="submit"]:not([aria-label*="Stop"])',
    ]
    for sel in send_selectors:
        try:
            btn = page.locator(sel).first
            if btn.count() == 0:
                continue
            if not btn.evaluate("e => e.offsetParent !== null"):
                continue
            if btn.evaluate("e => e.disabled || e.getAttribute('aria-disabled') === 'true'"):
                continue
            btn.click(force=True, timeout=2000)
            return True
        except Exception:
            continue
    # Fallback to Enter — but only if no pending attachments remain.
    if detect_pending_attachments(page):
        if not clear_pending_attachments(page):
            return False
    try:
        page.keyboard.press('Enter')
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Idempotency helpers (Priority #2 audit fix)
# ---------------------------------------------------------------------------
# Problem: send_message.py can time out in wait_for_reply AFTER the message
# was already submitted. round.py may then retry the call. Without idempotency,
# the retry would submit the same prompt twice.
#
# Fix: each user message gets a data-gpt-consult-hash="<md5[:8]>" attribute
# attached. Before filling, send_message.py checks if a marked message with
# the same hash already exists in the DOM. If yes → skip fill, resume wait.

def text_marker(text: str) -> str:
    """Short stable identifier for `text`. Used to mark user messages in DOM
    so a retry of send_message.py can detect an in-flight round and skip
    the fill step (idempotency)."""
    return hashlib.md5(text.encode('utf-8')).hexdigest()[:8]


def find_existing_send(page, text: str) -> bool:
    """True if a user message in the DOM is already marked with the hash of `text`.

    Use this BEFORE fill_input to detect that send_message was retried after
    a wait_for_reply timeout — in which case the user message is already
    queued and we should skip the fill step entirely.
    """
    marker = text_marker(text)
    try:
        return page.locator(f'[data-gpt-consult-hash="{marker}"]').count() > 0
    except Exception:
        return False


def mark_last_user_message(page, text: str) -> bool:
    """Set data-gpt-consult-hash on the last user message in the DOM.

    Called after verify_message_sent succeeds so the round becomes
    idempotent against later retries. Returns True if a user message was
    found and marked, False otherwise.
    """
    marker = text_marker(text)
    try:
        return page.evaluate(r"""(marker) => {
            const sels = ['[data-message-author-role="user"]', '[data-role="user"]'];
            for (const s of sels) {
                const els = document.querySelectorAll(s);
                if (els.length > 0) {
                    els[els.length - 1].setAttribute('data-gpt-consult-hash', marker);
                    return true;
                }
            }
            return false;
        }""", marker)
    except Exception:
        return False


def get_max_input_chars(c: dict) -> int:
    """Resolve the text-size limit for a backend.

    Priority:
      1. env var GPT_CONSULT_MAX_INPUT_CHARS (positive int) — caller override.
      2. backend's own max_input_chars from backend_config.
      3. fallback 400_000 (matches chatgpt conservative default).

    Validation: env var MUST be a positive int. Invalid values (non-numeric,
    zero, negative) exit rc=2 with a clear error — failure must propagate,
    no silent fallback to the default (per "失败就抛错，不兜底").

    Round 10 helper extracted from send_message.py / send_with_images.py to
    prevent drift between the two callers.
    """
    raw = os.environ.get('GPT_CONSULT_MAX_INPUT_CHARS')
    if raw is not None:
        try:
            v = int(raw)
        except ValueError:
            sys.stderr.write(
                f'[get_max_input_chars] invalid GPT_CONSULT_MAX_INPUT_CHARS='
                f'{raw!r}: must be a positive integer. Refusing to send.\n')
            sys.exit(2)
        if v <= 0:
            sys.stderr.write(
                f'[get_max_input_chars] invalid GPT_CONSULT_MAX_INPUT_CHARS='
                f'{v}: must be positive. Refusing to send.\n')
            sys.exit(2)
        return v
    return c.get('max_input_chars', 400_000)