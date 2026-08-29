"""Send a message into the current backend and wait for the full reply.

Usage:
    C:/Python312/python.exe -X utf8 send_message.py "<text>" [backend]
    echo "text" | python send_message.py - [backend]

Behavior:
- Finds the active tab for `backend` (URL host match). If none, opens one.
- Stays in that tab (preserves GPT conversation memory).
- Does NOT close the browser.

Architecture:
- media-kit owns the Chrome process (CDP port).
- Playwright connects to that Chrome via CDP and handles everything inside.
"""
import sys, io, time, os
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

from playwright.sync_api import sync_playwright
from backend_config import cfg, get_order, find_tab, page_host_matches, BACKENDS
from _helpers import (
    watchdog, check_logged_in, check_input_visible, write_active_url,
    find_real_composer, find_real_reply_text, is_real_streaming,
    verify_message_sent,
    find_existing_send, mark_last_user_message,
    detect_pending_attachments, clear_pending_attachments, submit_message,
    get_max_input_chars,
    ReplyStatus,
    GPT_CONSULT_REPLY_TIMEOUT_S,
)
from journal import RequestJournal, recover_request, STATUS_SENT_CONFIRMED
from media_kit import connect_browser


# Locator count timeout for reply/stream detection loops. Short enough that
# a wedged DOM doesn't stall the wait loop; long enough to handle normal
# React renders.
_LOC_TIMEOUT_MS = 3000
# Outer hard-kill budget for the entire send phase. Covers page.goto (30s)
# + fill (3s) + 240s stream wait + 3s buffer.
_SEND_WATCHDOG_S = 300


def fill_input(page, loc, text, backend):
    """Focus the input then insert text via Playwright keyboard.insert_text.

    Why not clipboard paste: contenteditable components (ChatGPT ProseMirror,
    Gemini rich-textarea) often don't update their React state on raw paste
    events. `keyboard.insert_text` dispatches `beforeinput`/`input` events
    directly, which all React-based frameworks handle correctly. Avoids IME
    issues with Chinese/unicode too — no keystroke simulation.

    Safety: refuses to insert into a non-empty input box (would corrupt the
    user's draft if they are editing concurrently).

    Round 8 P3: after insert_text, detect any auto-attached preview (chatgpt
    parses URLs/data: URIs into image attachments), clear it, then submit via
    the Send button. Enter is fallback only — it gets swallowed by pending
    attachment previews, focus loss, IME, etc.

    The caller MUST pass a Locator from find_real_composer() — a raw locator
    could point at a hidden fallback and silently drop the message.
    """
    loc.click()
    current = loc.evaluate("el => (el.innerText || el.value || '').trim()")
    if current:
        print(f"[send_message] REFUSED: input not empty ({len(current)} chars). "
              f"User may be editing — please clear the input or open a fresh tab.",
              file=sys.stderr)
        print(f"  preview: {repr(current[:80])}", file=sys.stderr)
        sys.exit(6)
    page.keyboard.insert_text(text)
    # P3: chatgpt auto-attach from URL/data: can swallow Enter. Clear first.
    if detect_pending_attachments(page):
        print('[send_message] P3: pending auto-attached preview detected — '
              'clearing before submit.', file=sys.stderr)
        if not clear_pending_attachments(page):
            print('[send_message] P3 ERROR: failed to clear auto-attached '
                  'preview — refusing to submit (would be stuck).',
                  file=sys.stderr)
            sys.exit(7)
    if not submit_message(page, backend):
        print('[send_message] P3 ERROR: submit_message returned False — '
              'no Send button and Enter fallback failed.', file=sys.stderr)
        sys.exit(7)


_USER_SELECTORS = [
    # Round 13b: deepseek uses class-based selectors, not data-attribute.
    # .ds-collapsible-text wraps the user-content bubble; div.ds-message is
    # the parent container shared with assistant messages (filtered by the
    # child .ds-collapsible-text to distinguish user from assistant).
    'div.ds-message .ds-collapsible-text',
    # chatgpt uses ProseMirror + data-attribute (unchanged).
    '[data-message-author-role="user"]',
    # generic data-role fallback.
    '[data-role="user"]',
]


def _count_user(page) -> int:
    n = 0
    for s in _USER_SELECTORS:
        try:
            n = max(n, page.locator(s).count())
        except Exception:
            continue
    return n


def _count_assistant(page, c) -> int:
    n = 0
    for s in c['reply_selectors']:
        try:
            n = max(n, page.locator(s).count())
        except Exception:
            continue
    return n


def wait_for_reply(page, c, baseline_user, timeout_s: float | None = None) -> ReplyStatus:
    """Wait for round completion — true assistant-side completion.

    Round 8 P2 (GPT design): returns ReplyStatus, not bool.
    State machine (M4 rev-3 + Priority #1 semantic helpers + P2 streaming-aware):

    Stages we require, in order:
      1. user_count > baseline_user (round started)
      2. assistant_count > baseline_assistant (reply began)
      3. terminal: assistant text is non-empty AND !streaming AND
         text unchanged for >= 3s (continuously polled)

    P2 timeouts (env-var configurable, defaults from GPT design):
      - timeout_s (hard_deadline): 600s — overall budget before declaring
        TIMEOUT (only fires if not streaming).
      - max_stream_s: 900s — absolute cap. Even if streaming keeps
        extending the deadline, we won't wait forever.
      - stream_idle_s: 90s — if no streaming AND no text change AND no new
        assistant msg for 90s, the stream is frozen and we TIMEOUT.
      - stream_grace_s: 120s — when streaming is detected, hard_deadline is
        pushed forward to (now + stream_grace_s), giving the stream more
        runway while it's actively producing.

    Returns:
      ReplyStatus.DONE       — round finished cleanly.
      ReplyStatus.TIMEOUT    — hard_deadline / max_stream_s / stream_idle hit.
      ReplyStatus.STREAMING  — streaming at hard_deadline; round.py should
                                NOT reset_to_new_chat in this case.
      ReplyStatus.BROWSER_DEAD — CDP / page is unresponsive (future hook).
    """
    if timeout_s is None:
        timeout_s = GPT_CONSULT_REPLY_TIMEOUT_S
    baseline_assistant = _count_assistant(page, c)
    start = time.time()
    hard_deadline = start + timeout_s
    max_stream_s = float(os.environ.get(
        'GPT_CONSULT_MAX_STREAM_S', '900'))
    stream_idle_s = float(os.environ.get(
        'GPT_CONSULT_STREAM_IDLE_S', '90'))
    stream_grace_s = float(os.environ.get(
        'GPT_CONSULT_STREAM_GRACE_S', '120'))

    user_seen = False
    assistant_seen = False
    last_text = ''
    stable_for = 0
    last_activity_at = start  # updated on streaming / text change / new asst
    stable_secs_required = 3
    POLL = 1
    while True:
        now = time.time()
        elapsed = now - start

        # Absolute cap — never wait longer than this even if deadline
        # keeps getting pushed.
        if elapsed > max_stream_s:
            print(f"[send_message] {c['display']} TIMEOUT reason=ABSOLUTE_CAP "
                  f"(elapsed={elapsed:.1f}s > max_stream_s={max_stream_s})",
                  file=sys.stderr)
            return ReplyStatus.TIMEOUT

        # Round 9: pass last_text so is_real_streaming can also detect
        # text-change as a streaming signal (defends against UI changes
        # that hide the stop button during long reasoning pauses).
        streaming = is_real_streaming(page, c, prev_reply_text=last_text)
        user_count = _count_user(page)
        assistant_count = _count_assistant(page, c)

        if user_count > baseline_user:
            user_seen = True
        if user_count == 0 and baseline_user == 0:
            user_seen = True
        if assistant_count > baseline_assistant:
            assistant_seen = True

        if streaming:
            last_activity_at = now
            # While streaming, push hard_deadline forward so we don't
            # TIMEOUT mid-stream. Cap so it can't grow unbounded.
            hard_deadline = max(hard_deadline, now + stream_grace_s)

        # TERMINAL: assistant exists, non-empty, not streaming, stable 3s.
        if user_seen and assistant_seen and not streaming:
            cur = find_real_reply_text(page, c, baseline_user)
            if cur:
                if cur == last_text:
                    stable_for += POLL
                    if stable_for >= stable_secs_required:
                        print(f"[send_message] {c['display']} reply complete "
                              f"(text stable {stable_for}s, {len(cur)} chars)",
                              file=sys.stderr)
                        return ReplyStatus.DONE
                else:
                    last_text = cur
                    stable_for = 0
                    last_activity_at = now  # text changing = activity
            else:
                # Empty container / hidden reply — reset stability counter.
                last_text = ''
                stable_for = 0

        # Stream idle: if we've started the round and haven't seen any
        # activity (no streaming AND no text change) for stream_idle_s,
        # the stream is frozen.
        if user_seen and assistant_seen:
            if now - last_activity_at > stream_idle_s:
                print(f"[send_message] {c['display']} TIMEOUT "
                      f"reason=STREAM_IDLE "
                      f"(no activity for {now - last_activity_at:.1f}s, "
                      f"threshold={stream_idle_s}s)",
                      file=sys.stderr)
                return ReplyStatus.TIMEOUT

        # Hard deadline + not streaming: final timeout.
        if now > hard_deadline and not streaming:
            print(f"[send_message] {c['display']} TIMEOUT "
                  f"reason=DEADLINE "
                  f"(elapsed={elapsed:.1f}s > hard_deadline="
                  f"{hard_deadline - start:.1f}s, not streaming)",
                  file=sys.stderr)
            return ReplyStatus.TIMEOUT

        time.sleep(POLL)


def main():
    if len(sys.argv) < 2:
        print('usage: send_message.py <text | -> [backend]', file=sys.stderr)
        sys.exit(2)

    backend = None
    text_arg = None
    if len(sys.argv) >= 3 and sys.argv[-1] in BACKENDS:
        backend = sys.argv[-1]
        text_arg = sys.argv[1]
    else:
        backend = get_order()[0]
        text_arg = sys.argv[1]

    text = sys.stdin.read() if text_arg == '-' else text_arg
    c = cfg(backend)

    # Round 10: pre-submit text-size guard.
    # Refuses to send (rc=11) if the message exceeds the backend's
    # max_input_chars. Prevents paste-stall, UI truncation, and DOM
    # corruption on oversized payloads. Override via
    # GPT_CONSULT_MAX_INPUT_CHARS (validated by get_max_input_chars).
    max_input_chars = get_max_input_chars(c)
    if len(text) > max_input_chars:
        print(f"[send_message] REFUSED: text too long for {c['display']} "
              f"({len(text)} chars > max_input_chars={max_input_chars}). "
              f"Split the request, switch to a backend with a higher limit, "
              f"or override via GPT_CONSULT_MAX_INPUT_CHARS.",
              file=sys.stderr)
        sys.exit(11)

    # Round 8 P4: journal this round so retries / cross-process recovery
    # know whether the message was sent, whether the round is ambiguous,
    # or whether it definitely failed. We construct the journal BEFORE any
    # browser work so the CREATED row exists even if the playwright
    # session crashes mid-flight.
    prior = recover_request(backend, text)
    if prior is not None and prior['status'] == STATUS_SENT_CONFIRMED:
        # Defensive guard: refuse to re-send a confirmed round. (round.py
        # also checks this before calling us, but a malicious or buggy
        # caller might bypass it.)
        print(f"[send_message] REFUSED: round already SENT_CONFIRMED for "
              f"this text (request_id={prior['request_id']}). Will not "
              f"re-send. Use a different prompt to retry.",
              file=sys.stderr)
        sys.exit(9)
    journal_row = RequestJournal(backend, text)

    with watchdog(_SEND_WATCHDOG_S, 'send_message'):
        with sync_playwright() as p:
            browser = connect_browser(p)
            ctx = browser.contexts[0]

            page = find_tab(ctx, backend)
            if page is None:
                page = ctx.new_page()
                page.goto(c['url'], wait_until='domcontentloaded', timeout=30000)
            elif not page_host_matches(page, backend):
                page.goto(c['url'], wait_until='domcontentloaded', timeout=30000)

            # M3 audit fix: snapshot ONLY user message count before send.
            # baseline_asst was unsafe — if the page already had a pending
            # assistant reply, the baseline was inflated and we'd never see
            # "asst_count > baseline_asst". Round marker is now "new user
            # message appeared" instead.
            #
            # Priority #2 (idempotency): if a user message with the same
            # text-hash is already marked in the DOM, this is a retry after
            # a wait_for_reply timeout. Skip the fill step and resume the
            # wait with baseline_user = current_user_count - 1 (the existing
            # round's user message IS in DOM).
            is_idempotent_retry = find_existing_send(page, text)
            baseline_user = _count_user(page)
            if is_idempotent_retry:
                baseline_user -= 1
                print(f"[send_message] idempotent retry detected "
                      f"(text-hash match in DOM); skipping fill, "
                      f"resuming wait with baseline_user={baseline_user}.",
                      file=sys.stderr)
                journal_row.mark_submitting()
            else:
                # FIND → ASSERT SEMANTIC IDENTITY: pick a composer that is
                # actually editable, not a hidden fallback.
                real_box = find_real_composer(page, c, timeout_ms=5000)
                if real_box is None:
                    print(f"[send_message] no editable composer matched for "
                          f"{c['display']} (selectors exist but none is visible+editable).",
                          file=sys.stderr)
                    journal_row.mark_failed('no editable composer')
                    journal_row.close()
                    sys.exit(4)

                journal_row.mark_submitting()
                fill_input(page, real_box, text, backend)

                # ACT → ASSERT EFFECT: verify the user message actually appeared
                # in DOM after fill_input. Bug A: timeout is 10s with a final
                # post-deadline check (chatgpt can be slow under load).
                if not verify_message_sent(page, c, baseline_user, timeout_s=10.0):
                    # Bug B: even if verify timed out, the message may have
                    # landed just after. Re-check once; if a new user message
                    # is in DOM, mark it for idempotency and continue. Only
                    # rc=8 when no message appeared at all.
                    if _count_user(page) > baseline_user:
                        print(f"[send_message] verify timed out but user message "
                              f"is in DOM — proceeding (Bug A final-poll safety net).",
                              file=sys.stderr)
                    else:
                        print(f"[send_message] ASSERT EFFECT FAILED: no user message "
                              f"appeared in DOM after fill_input. The composer accepted "
                              f"text but the round didn't start. Aborting to avoid "
                              f"waiting on a reply that will never come.",
                              file=sys.stderr)
                        journal_row.mark_failed(
                            'no message in DOM after fill_input')
                        journal_row.close()
                        sys.exit(8)

                # Priority #2: stamp the new user message with a hash of the
                # text so that a later retry (after wait_for_reply timeout)
                # is detected as idempotent and does not send again. Bug B:
                # run this EVEN if verify timed out, as long as a new user
                # message is in DOM.
                if not mark_last_user_message(page, text):
                    print(f"[send_message] WARNING: failed to mark user message "
                          f"with idempotency hash. Retries may re-send.",
                          file=sys.stderr)

            # Snapshot the active conversation URL for find_tab() next time.
            # If the URL is a /c/<id> path, that's the persistent thread;
            # if it's the bare domain, it's a fresh / root chat.
            write_active_url(backend, page.url)

    print(f"[send_message] {c['display']} submitted, waiting for reply...", file=sys.stderr)

    # Stream-end detection (separate sync_playwright block to avoid holding
    # the playwright handle during the long wait).
    time.sleep(2)
    status = ReplyStatus.TIMEOUT
    with watchdog(_SEND_WATCHDOG_S, 'send_message.wait'):
        with sync_playwright() as p:
            browser = connect_browser(p)
            ctx = browser.contexts[0]
            page = find_tab(ctx, backend) or ctx.pages[-1]

            status = wait_for_reply(page, c, baseline_user, timeout_s=240)

    if status == ReplyStatus.DONE:
        # P4: mark confirmed so a future retry refuses to re-send.
        try:
            excerpt = ''
            try:
                excerpt = find_real_reply_text(
                    page, c, baseline_user, skip_baseline=True)[:200]
            except Exception:
                excerpt = ''
            journal_row.mark_sent_confirmed(reply_excerpt=excerpt)
        finally:
            journal_row.close()
        return
    # Round 8 P2: when the reply is still streaming at hard_deadline, write a
    # marker file so round.py can distinguish "AI is just slow, don't reset"
    # from "AI is wedged, do reset". DO NOT exit 1 yet — round.py reads the
    # marker, decides whether to wait longer, and only then resets.
    os.makedirs('.gpt_consult', exist_ok=True)
    if status == ReplyStatus.STREAMING:
        with open('.gpt_consult/last_reply_status.txt', 'w', encoding='utf-8') as f:
            f.write('STREAMING')
        # P4: STREAMING is ambiguous (AI may have completed just after our
        # snapshot). mark_unknown so a future retry doesn't re-send.
        try:
            journal_row.mark_unknown(
                error='hard_deadline hit while streaming')
        finally:
            journal_row.close()
        print(f"[send_message] {c['display']} still streaming at hard_deadline "
              f"— caller (round.py) will keep waiting instead of resetting.",
              file=sys.stderr)
        sys.exit(1)
    with open('.gpt_consult/last_reply_status.txt', 'w', encoding='utf-8') as f:
        f.write(status.value)
    # P4: TIMEOUT/BROWSER_DEAD — round may have completed. mark_unknown
    # so a future retry doesn't re-send.
    try:
        journal_row.mark_unknown(error=f'wait_for_reply returned {status.value}')
    finally:
        journal_row.close()
    print(f"[send_message] TIMEOUT ({status.value}) waiting for "
          f"{c['display']} reply", file=sys.stderr)
    sys.exit(1)


if __name__ == '__main__':
    main()