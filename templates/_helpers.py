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
    'GPT_CONSULT_REPLY_TIMEOUT_S', '1500'))  # Round 16: gemini 3.1 Pro + thinking can take >10min on complex prompts
GPT_CONSULT_STREAM_GRACE_S = float(os.environ.get(
    'GPT_CONSULT_STREAM_GRACE_S', '120'))
GPT_CONSULT_STREAM_IDLE_S = float(os.environ.get(
    'GPT_CONSULT_STREAM_IDLE_S', '90'))
GPT_CONSULT_MAX_STREAM_S = float(os.environ.get(
    'GPT_CONSULT_MAX_STREAM_S', '1800'))  # Round 16: absolute cap raised to match


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


# Round 13c: deepseek-aware user-message selector list. Order matters (first
# match wins for find_existing_send / mark_last_user_message). Verified live:
#   - deepseek user messages: <div class="ds-message"><div class="ds-collapsible-text">TEXT</div></div>
#   - deepseek assistant messages (fast + R1 expert): NO .ds-collapsible-text inside
#   - chatgpt user messages: <div data-message-author-role="user">...</div>
# So 'div.ds-message .ds-collapsible-text' matches ONLY user msgs on deepseek
# (zero overlap with chatgpt, which uses no .ds-message class). Putting
# deepseek first makes the common case fast; chatgpt selectors stay as fallback
# for completeness.
_USER_SELECTORS = (
    'div.ds-message .ds-collapsible-text',     # deepseek user bubble
    '[data-message-author-role="user"]',       # chatgpt + ProseMirror
    '[data-role="user"]',                       # generic
    'user-query',                               # gemini user bubble
    '.query-content',                           # gemini (user-query inner div)
)


# Round 13c: deepseek R1 expert mode shows transient "thinking placeholder"
# text inside the assistant div.ds-message BEFORE the final answer is ready:
#   "正在思考" / "正在思考\n正在思考"     (placeholder while R1 reasons)
#   "深度思考"                            (alt placeholder)
#   "已思考（用时 N 秒）"                  (thinking summary line, alone)
# If find_real_reply_text returned any of these as the reply, wait_for_reply
# would:
#   1. falsely detect DONE in ~3s with a placeholder, OR
#   2. never update last_text while streaming=True (Signal 2 keeps firing
#      because prev="正在思考" != cur="已思考（用时 2 秒）..."), so the
#      terminal block is permanently skipped → only max_stream_s (900s)
#      or a brief not-streaming window can fire hard_deadline (observed: 240s
#      DEADLINE when streaming briefly returned False mid-transition).
# Fix: filter these strings in find_real_reply_text so we never accept
# a placeholder as a stable reply.
import re as _re
# Round 15: STRICT placeholder regex. The previous "any non-whitespace char
# in vocab" pattern matched "深度思考是一种重要的认知能力。" because 深度思考
# + 是 + 一 + ... all matched individual vocab chars. Now the regex matches
# ONLY exact placeholder strings (allowing whitespace). Strings like
# "深度思考是一种重要的认知能力。" will NOT match (good — it's a real reply).
#
# Deepseek R1 expert (Round 15) flagged this as a Bug B corner case:
# "回复文本恰好是'深度思考'开头但实际是有效中文回复".
_THINKING_PLACEHOLDER_RE = _re.compile(
    r'^\s*(?:正在思考|深度思考|已思考[（(]用时\s*\d+\s*秒[）)])\s*$'
)
# Summary line on its own (kept for explicit summary use).
_THINKING_SUMMARY_RE = _re.compile(
    r'^\s*已思考[（(]用时\s*\d+\s*秒[）)]\s*$'
)


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


def _is_thinking_placeholder(text: str) -> bool:
    """True if `text` is EXACTLY a deepseek R1 thinking placeholder.

    Round 15: strict full-string match. Accepts only:
      "正在思考"
      "深度思考"
      "已思考（用时 N 秒）"
      (also allows surrounding whitespace and either full-width or
      half-width parens.)
    Rejects anything that contains real content beyond the placeholder —
    e.g. "深度思考是一种重要的认知能力。" returns False (it's a real reply).

    The previous cheap fast-path (any short text containing "深度思考" etc.)
    was too aggressive and silently filtered valid Chinese replies.
    """
    if not text:
        return False
    return _THINKING_PLACEHOLDER_RE.match(text) is not None


# Round 15: deepseek model-selection selectors.
# Deepseek shows 3 model cards on the welcome screen of a fresh tab:
#   快速模式 (fast, DEFAULT), 专家模式 (R1 expert), 识图模式 (image).
# Clicking a card selects it; the conversation starts in the SELECTED mode
# when the first message is sent. Once a conversation starts, model selection
# is LOCKED — no in-conversation model switch exists.
#
# Round 14 used hash classes (_9f2341b._18572c1 + _31a22b0) which are
# build-specific and may change every release. Round 15 prefers ARIA/data
# attributes for selection state and uses text-matching for card location,
# so the selector survives class hash churn.
_DEEPSEEK_EXPERT_CARD_TEXT = '专家模式'
# Common attribute names a radio-style card might expose. We read each in
# order; the first one present wins.
_DEEPSEEK_SELECTION_ATTRS = ('aria-pressed', 'aria-checked', 'data-selected')
# Final fallback: the hash class from Round 14. May stop working after
# a deepseek UI update; kept as last resort.
_DEEPSEEK_EXPERT_SELECTED_CLASS_FALLBACK = '_31a22b0'


def _card_is_selected(card) -> bool:
    """True if the deepseek model card is currently selected.

    Round 15: prefers ARIA/data attributes; falls back to the Round 14
    hash class. Per deepseek R1 expert: "div._9f2341b._18572c1 + _31a22b0
    是 hash class，DeepSeek 前端每次构建都可能变。必须优先读 aria-pressed /
    aria-checked / data-selected，class 仅作 fallback".
    """
    try:
        for attr in _DEEPSEEK_SELECTION_ATTRS:
            val = card.get_attribute(attr)
            if val is not None:
                return val.lower() == 'true'
    except Exception:
        pass
    try:
        cls = card.get_attribute('class') or ''
        return _DEEPSEEK_EXPERT_SELECTED_CLASS_FALLBACK in cls
    except Exception:
        return False


def _find_expert_card(page):
    """Locate the 专家模式 card by visible text (survives hash-class churn).

    Tries `div[role="radio"]:has-text("专家模式")` first (semantic), then
    any visible element containing the exact text. Returns None when the
    welcome screen is gone (mid-conversation).
    """
    for sel in (
        f'div[role="radio"]:has-text("{_DEEPSEEK_EXPERT_CARD_TEXT}")',
        f'[role="radio"]:has-text("{_DEEPSEEK_EXPERT_CARD_TEXT}")',
        f':has-text("{_DEEPSEEK_EXPERT_CARD_TEXT}")',
    ):
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible(timeout=1500):
                return loc
        except Exception:
            continue
    return None


def ensure_expert_mode(page, c: dict, timeout_s: float = 6.0) -> bool:
    """On a fresh deepseek welcome screen, select 专家模式 (R1 expert).

    Backend-specific: only deepseek exposes this control. For other backends
    (chatgpt, gemini) this is a no-op that returns True.

    Round 15: locator uses text-matching (survives class hash churn);
    selection state read from ARIA/data attrs first, class fallback.

    Caller contract:
      - Invoke BEFORE first message in a fresh conversation.
      - Idempotent: if 专家模式 is already selected, returns True without click.
      - If the welcome screen is gone (cards.count() == 0), the tab is mid-
        conversation — return True (no-op, can't change model anyway).
      - Failure non-fatal: returns False on timeout; caller logs + continues.

    Returns True if expert mode is selected (was already, OR clicked, OR tab
    is mid-conversation and we couldn't help). False only on timeout.
    """
    if c.get('display') != 'DeepSeek':
        return True

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        card = _find_expert_card(page)
        if card is None:
            # Welcome screen is gone → mid-conversation, can't change model.
            return True
        try:
            if _card_is_selected(card):
                return True
            card.click(timeout=3000)
            time.sleep(0.4)
            if _card_is_selected(card):
                return True
        except Exception:
            pass
        time.sleep(0.4)
    return False


# Round 16: gemini 3.1 Pro selection for consults.
#
# User-confirmed (2026-08-30): gemini's welcome tab DEFAULTS to 3.1 Pro +
# the model already does visible thinking in its reply. The "扩展思考"
# dropdown row is an OPTIONAL toggle that controls whether chip shows
# "Pro 扩展" (visible UI marker) vs "Gemini Pro". It does NOT switch the
# model into a fundamentally different mode — Pro already produces
# step-by-step reasoning in the reply by default.
#
# Therefore the helper only needs to verify 3.1 Pro is selected. We do
# NOT enforce the 扩展思考 toggle (user prefers default behavior).
#
# Verified selectors (Round 16):
#   - Model chip trigger: button containing "Flash" or "Pro"
#   - 3.1 Pro entry: text="3.1 Pro"
#   - The 3.1 Pro row's gem-menu-item-content has class "selected checkmark-only"
#     when active (vs just "checkmark-only" when not)
_GEMINI_PRO_MODEL_TEXT = '3.1 Pro'


def _gemini_dropdown_is_open(page) -> bool:
    """True if the gemini model dropdown is currently visible."""
    try:
        return page.locator(f':text("{_GEMINI_PRO_MODEL_TEXT}"):visible').count() > 0
    except Exception:
        return False


def _gemini_open_dropdown(page) -> bool:
    """Click the model chip to open the dropdown. Returns True if dropdown visible."""
    for sel in ['button:has-text("Pro"):visible', 'button:has-text("Flash"):visible']:
        btns = page.locator(sel)
        for i in range(btns.count()):
            try:
                btn = btns.nth(i)
                txt = btn.inner_text(timeout=500)
                # Skip upsell button (has Google AI Pro branding)
                if 'Google AI' in txt or '升级' in txt:
                    continue
                btn.click(timeout=2000)
                time.sleep(0.5)
                if _gemini_dropdown_is_open(page):
                    return True
            except Exception:
                continue
    return _gemini_dropdown_is_open(page)


def _gemini_pro_is_selected(page) -> bool:
    """True if the chip currently shows 3.1 Pro as the active model.

    Detection: 3.1 Pro row's gem-menu-item-content has 'selected' class when active.
    """
    try:
        # Check chip text first (cheaper)
        chip = page.locator('button:has-text("Pro"):visible').first
        if chip.count() > 0:
            txt = chip.inner_text(timeout=500)
            if 'Pro' in txt and 'Flash' not in txt:
                return True
        # Fallback: open dropdown and check row class
        was_open = _gemini_dropdown_is_open(page)
        if not was_open:
            _gemini_open_dropdown(page)
            time.sleep(0.4)
        row = page.locator(f'gem-menu-item:has-text("{_GEMINI_PRO_MODEL_TEXT}"):visible').first
        if row.count() > 0:
            cls = row.locator('gem-menu-item-content').first.evaluate(
                "e => e ? e.getAttribute('class') : ''")
            return 'selected' in (cls or '')
    except Exception:
        return False
    return False


def ensure_gemini_pro_thinking(page, c: dict, timeout_s: float = 8.0) -> bool:
    """On a fresh gemini welcome screen, ensure 3.1 Pro is the active model.

    Backend-specific: only gemini exposes this control. For other backends
    (chatgpt, deepseek) this is a no-op that returns True.

    Per user (2026-08-30): gemini's welcome tab already defaults to 3.1 Pro
    which produces visible step-by-step reasoning in replies. The "扩展思考"
    dropdown toggle is an OPTIONAL UI marker and is NOT enforced here.

    Caller contract:
      - Invoke BEFORE first message in a fresh conversation.
      - Idempotent: if 3.1 Pro is already selected, no clicks.
      - If the welcome screen is gone (no chip visible), the tab is mid-
        conversation — return True (no-op, can't change model anyway).
      - Failure non-fatal: returns False on timeout; caller logs + continues.

    Returns True if Pro is selected (was already, OR clicked, OR tab is
    mid-conversation). False only on timeout.
    """
    if c.get('display') != 'Gemini':
        return True

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if _gemini_pro_is_selected(page):
            return True

        # Pro not selected — open dropdown and click 3.1 Pro
        if not _gemini_open_dropdown(page):
            time.sleep(0.4)
            continue

        try:
            pro = page.locator(f'gem-menu-item:has-text("{_GEMINI_PRO_MODEL_TEXT}"):visible').first
            if pro.count() > 0:
                pro.click(timeout=2000)
                time.sleep(0.4)
        except Exception:
            pass
        finally:
            try:
                page.keyboard.press('Escape')
                time.sleep(0.3)
            except Exception:
                pass

        time.sleep(0.4)

    return _gemini_pro_is_selected(page)


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

    Round 13c: also rejects deepseek R1 thinking placeholders ("正在思考",
    "深度思考", "已思考（用时 N 秒）") via `_is_thinking_placeholder`.
    These appear in the assistant div.ds-message BEFORE the final answer
    is ready; accepting them as a reply either falsely reports DONE in ~3s
    OR keeps `streaming=True` permanently (Signal 2 prev != cur forever),
    causing hard_deadline / max_stream_s timeouts.

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
            # Round 13c: use module-level _USER_SELECTORS (sync with
            # send_message.py / send_with_images.py and verify_message_sent).
            for sel in _USER_SELECTORS:
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
            if not text:
                continue
            # Round 13c: never accept a deepseek R1 thinking placeholder as
            # a reply. Try the next selector — the next-deepest assistant
            # element (typically `div.ds-markdown.ds-assistant-message-main-content`)
            # has the actual final answer without the thinking chain noise.
            if _is_thinking_placeholder(text):
                continue
            # Round 15: reject error / network-failure toasts that some
            # backends render inside reply containers. Per deepseek R1 expert
            # (Bug B 3.1c): "find_real_reply_text 需增加 ... 排除错误文本
            # （连接失败/请求出错等），否则可能假 DONE".
            if _is_error_text(text):
                continue
            return text
        except Exception:
            continue
    return ''


# Round 15: error-text patterns that some backends render inside reply
# containers. Filter them so wait_for_reply doesn't terminate on a "reply"
# that is actually a network/permission error toast.
_ERROR_TEXT_PATTERNS = (
    '连接失败', '请求出错', '网络错误', 'network error',
    '请检查网络', '服务异常', 'try again', 'request failed',
)


def _is_error_text(text: str) -> bool:
    """True if `text` looks like a backend error toast, not a real reply.

    Conservative: only rejects text that is short (<= 80 chars) AND contains
    a known error keyword. Longer replies that happen to mention an error
    (e.g. code-review of a failing test) are still accepted.
    """
    if not text or len(text) > 80:
        return False
    return any(pat in text for pat in _ERROR_TEXT_PATTERNS)


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

    Round 13c Bug B: Signal 2 was iterating ALL reply_selectors and
    returning True if ANY one of them showed a text change. With the
    deepseek R1 fix that promotes `div.ds-markdown.ds-assistant-message-main-content`
    as the PRIMARY reply selector (clean final-answer text, no thinking
    chain) while keeping `div.ds-message:not(:has(.ds-collapsible-text))`
    as the FALLBACK (whole assistant div including "已思考（用时 N 秒）" +
    thinking chain + final answer), Signal 2 fired FOREVER even after
    R1 finished:

      - find_real_reply_text uses PRIMARY → returns "2"
      - prev_reply_text = "2"
      - Signal 2 iterates selectors:
          PRIMARY  → "2"     == prev, no fire
          FALLBACK → "已思考（用时 15 秒）\n\n..." != prev → fires

    → streaming=True permanently → terminal block never runs →
    stable_for never accumulates → only max_stream_s / hard_deadline
    can fire.

    Fix: Signal 2 uses the SAME canonical reply text that
    find_real_reply_text returns (same selector priority + same
    placeholder filter). prev_reply_text and cur_text are now guaranteed
    to come from the same source, so they only differ when the actual
    final-answer content has changed.
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

    # Signal 2: reply text changed since last call (caller-supplied).
    # Use the canonical text source — same logic as find_real_reply_text
    # (first non-empty selector, placeholder-filtered). This guarantees
    # `cur_text` comes from the same element type as `prev_reply_text`,
    # so they only differ when actual final-answer content has changed.
    if prev_reply_text:
        try:
            cur_text = find_real_reply_text(
                page, c, baseline_user=0, skip_baseline=True)
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
        # Round 13c: use module-level _USER_SELECTORS (deepseek-aware).
        for sel in _USER_SELECTORS:
            try:
                n = max(n, page.locator(sel).count())
            except Exception:
                continue
        return n

    def _has_text() -> bool:
        for sel in _USER_SELECTORS:
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


# Round 15: backend-aware Send button selectors.
# chatgpt uses native <button>; deepseek uses <div role="button"> with no
# aria-label. Generic :has(svg) fallback catches both new UIs and future
# backends (e.g. gemini variants).
DEFAULT_SEND_SELECTORS = [
    'button[data-testid="send-button"]',
    'button[aria-label*="Send"]',
    'button[aria-label*="发送"]',
    'button[type="submit"]:not([aria-label*="Stop"])',
    'div[role="button"]:has(svg)',
]

# Deepseek's primary Send button is a <div role="button"> with the
# `--primary --filled --circle` modifier classes (verified 2026-08-29).
# Secondary: any visible :has(svg) div[role=button] as final fallback.
DEEPSEEK_SEND_SELECTORS = [
    'div[role="button"].ds-button--primary.ds-button--filled.ds-button--circle',
    'div[role="button"].ds-button--circle',
    'div[role="button"]:has(svg)',
]


def _get_send_selectors(backend: str) -> list:
    if backend == 'deepseek':
        return DEEPSEEK_SEND_SELECTORS
    return DEFAULT_SEND_SELECTORS


def _is_send_button_enabled(loc) -> bool:
    """Guard for Send button candidates: visible + not disabled.

    Rejects hidden elements (offsetParent null), aria-disabled, or class-
    based ds-button--disabled markers (deepseek).
    """
    try:
        if not loc.evaluate("e => e.offsetParent !== null"):
            return False
        if loc.evaluate(
            "e => e.disabled || e.getAttribute('aria-disabled') === 'true'"
        ):
            return False
        if loc.evaluate("e => e.className && e.className.includes"
                        "('ds-button--disabled')"):
            return False
        return True
    except Exception:
        return False


def _find_send_button(page, selectors: list):
    """Iterate selectors and return the first VISIBLE+ENABLED match.

    Filters every candidate with _is_send_button_enabled before clicking,
    so hidden/fallback buttons can't cause silent failure (the rc=8 case
    where the wrong button "succeeds" but doesn't submit).
    """
    for sel in selectors:
        try:
            loc = page.locator(sel)
            cnt = loc.count()
            for i in range(cnt):
                el = loc.nth(i)
                if _is_send_button_enabled(el):
                    return el
        except Exception:
            continue
    return None


def submit_message(page, c: dict) -> bool:
    """Submit composer contents via Send button click (preferred).

    Backend-aware selector list (Round 15): deepseek uses <div role="button">
    instead of <button>; chatgpt/gemini use <button>. Generic :has(svg)
    fallback catches both.

    Send button is preferred because Enter is unreliable: deepseek binds its
    own keydown handler that swallows Enter for newline-only. Enter fallback
    is disabled entirely for deepseek (verified: Enter does nothing).

    Returns True if the submit action was triggered (caller still needs
    verify_message_sent to confirm the user message actually appeared in DOM).
    """
    backend = c.get('name') or c.get('display', '').lower()
    selectors = _get_send_selectors(backend)
    btn = _find_send_button(page, selectors)
    if btn is not None:
        try:
            btn.click(force=True, timeout=2000)
            return True
        except Exception:
            pass

    # Enter fallback. Deepseek confirmed to swallow Enter (newline-only
    # binding), so skip it for deepseek to avoid silent rc=8.
    if backend == 'deepseek':
        print('[send] Enter fallback SKIPPED for deepseek — Send button '
              'required.', file=sys.stderr)
        return False

    # Other backends: clear any pending attachments, then press Enter.
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

    Round 13c: use module-level _USER_SELECTORS. The deepseek selector
    `div.ds-message .ds-collapsible-text` matches ONLY user messages
    (verified: assistant messages in fast + R1 expert mode have NO
    .ds-collapsible-text descendant). No role-filter needed.
    """
    marker = text_marker(text)
    try:
        return page.evaluate(r"""({marker, sels}) => {
            for (const s of sels) {
                const els = document.querySelectorAll(s);
                if (els.length > 0) {
                    els[els.length - 1].setAttribute('data-gpt-consult-hash', marker);
                    return true;
                }
            }
            return false;
        }""", {"marker": marker, "sels": list(_USER_SELECTORS)})
    except Exception:
        return False


def get_max_input_chars(c: dict) -> int:
    """Resolve the text-size limit for a backend.

    Priority:
      1. env var GPT_CONSULT_MAX_INPUT_CHARS (positive int) — caller override.
      2. backend's own max_input_chars from backend_config.
      3. fallback 200_000 (matches chatgpt empirical ceiling; Round 11 v7).

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
    return c.get('max_input_chars', 200_000)