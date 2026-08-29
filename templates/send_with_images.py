"""Send a message WITH image attachments into a backend tab, wait for reply.

Usage:
    python -X utf8 send_with_images.py <textfile> <img1> [img2 ...] [--backend chatgpt]

- Finds the active tab by URL host (stays in same conversation = keeps memory).
- Uploads via the hidden <input type=file> (set_input_files), waits for the
  composer to show all thumbnails, then inserts text and presses Enter.
- Never closes Chrome.
"""
import sys, io, os, time
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

from playwright.sync_api import sync_playwright
from backend_config import cfg, get_order, find_tab, page_host_matches
from _helpers import (
    watchdog, write_active_url,
    find_real_composer, find_real_reply_text, is_real_streaming,
    verify_message_sent,
    find_existing_send, mark_last_user_message,
    get_max_input_chars,
    _USER_SELECTORS,
    ReplyStatus,
    GPT_CONSULT_REPLY_TIMEOUT_S,
)
from media_kit import connect_browser


_LOC_TIMEOUT_MS = 3000
_WATCHDOG_S = 360  # image round is slower than text round


# Round 13c: _USER_SELECTORS imported from _helpers (single source of truth).


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


def wait_for_reply_with_images(page, c, baseline_user,
                                timeout_s: float | None = None) -> ReplyStatus:
    """Round 8 P2 — same state machine as send_message.wait_for_reply.

    Returns ReplyStatus so callers can distinguish DONE from STREAMING
    (still producing at hard_deadline; do NOT reset tab).
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
    last_activity_at = start
    stable_secs_required = 3
    POLL = 1

    while True:
        now = time.time()
        elapsed = now - start

        if elapsed > max_stream_s:
            print(f"[send_with_images] {c['display']} TIMEOUT "
                  f"reason=ABSOLUTE_CAP "
                  f"(elapsed={elapsed:.1f}s > max_stream_s={max_stream_s})",
                  file=sys.stderr)
            return ReplyStatus.TIMEOUT

        # Round 9: pass last_text so is_real_streaming also detects
        # text-change as a streaming signal.
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
            hard_deadline = max(hard_deadline, now + stream_grace_s)

        if user_seen and assistant_seen and not streaming:
            cur = find_real_reply_text(page, c, baseline_user)
            if cur:
                if cur == last_text:
                    stable_for += POLL
                    if stable_for >= stable_secs_required:
                        print(f"[send_with_images] {c['display']} reply complete "
                              f"(text stable {stable_for}s, {len(cur)} chars)",
                              file=sys.stderr)
                        return ReplyStatus.DONE
                else:
                    last_text = cur
                    stable_for = 0
                    last_activity_at = now
            else:
                last_text = ''
                stable_for = 0

        if user_seen and assistant_seen:
            if now - last_activity_at > stream_idle_s:
                print(f"[send_with_images] {c['display']} TIMEOUT "
                      f"reason=STREAM_IDLE "
                      f"(no activity for {now - last_activity_at:.1f}s, "
                      f"threshold={stream_idle_s}s)",
                      file=sys.stderr)
                return ReplyStatus.TIMEOUT

        if now > hard_deadline and not streaming:
            print(f"[send_with_images] {c['display']} TIMEOUT "
                  f"reason=DEADLINE "
                  f"(elapsed={elapsed:.1f}s > hard_deadline="
                  f"{hard_deadline - start:.1f}s, not streaming)",
                  file=sys.stderr)
            return ReplyStatus.TIMEOUT

        time.sleep(POLL)


def main():
    args = [a for a in sys.argv[1:]]
    backend = get_order()[0]
    if '--backend' in args:
        i = args.index('--backend')
        backend = args[i + 1]
        del args[i:i + 2]

    textfile, images = args[0], args[1:]
    text = open(textfile, encoding='utf-8').read()
    images = [os.path.abspath(p) for p in images]
    for p in images:
        if not os.path.exists(p):
            print(f'missing image: {p}', file=sys.stderr)
            sys.exit(2)

    c = cfg(backend)

    # Round 10: pre-submit text-size guard.
    # Refuses to send (rc=11) if the message exceeds the backend's
    # max_input_chars. Same semantics as send_message.py — image+text
    # still counts as text for the limit. Validated by get_max_input_chars.
    max_input_chars = get_max_input_chars(c)
    if len(text) > max_input_chars:
        print(f"[send_with_images] REFUSED: text too long for {c['display']} "
              f"({len(text)} chars > max_input_chars={max_input_chars}). "
              f"Split the request or override via GPT_CONSULT_MAX_INPUT_CHARS.",
              file=sys.stderr)
        sys.exit(11)

    with watchdog(_WATCHDOG_S, 'send_with_images'):
        with sync_playwright() as p:
            browser = connect_browser(p)
            ctx = browser.contexts[0]
            page = find_tab(ctx, backend)
            if page is None or not page_host_matches(page, backend):
                print('no tab for backend', file=sys.stderr)
                sys.exit(5)
            page.bring_to_front()

            # M3 audit fix: snapshot ONLY user count. baseline_asst was unsafe.
            baseline_user = _count_user(page)

            # FIND → ASSERT SEMANTIC IDENTITY: composer that is editable,
            # not a hidden fallback.
            box = find_real_composer(page, c, timeout_ms=5000)
            if box is None:
                print('[send_with_images] no editable composer matched', file=sys.stderr)
                sys.exit(4)
            page.keyboard.press('End')
            box.evaluate("el => { el.scrollIntoView({block:'center'}); el.focus(); }")
            time.sleep(0.3)
            current = box.evaluate("el => (el.innerText || el.value || '').trim()")
            if current:
                print(f'REFUSED: input not empty ({len(current)} chars)',
                      file=sys.stderr)
                sys.exit(6)

            # Priority #2 (idempotency): if a user message with the same
            # text-hash is already marked in the DOM, this is a retry after
            # a wait_for_reply timeout. Skip upload + fill, just resume wait.
            # Bug C: do NOT open a second sync_playwright inside this block —
            # the outer `with sync_playwright() as p` is still active. Instead
            # set a flag and run the wait phase below, AFTER the outer block
            # exits.
            is_idempotent_retry = find_existing_send(page, text)
            if is_idempotent_retry:
                baseline_user -= 1
                print(f"[send_with_images] idempotent retry detected "
                      f"(text-hash match in DOM); skipping upload+fill, "
                      f"resuming wait with baseline_user={baseline_user}.",
                      file=sys.stderr)
            else:
                fi = page.locator('input[type="file"]').last
                fi.set_input_files(images, timeout=60000)
                print(f'[upload] {len(images)} files set, waiting for upload to finish...',
                      file=sys.stderr)

                # HARD RULE: wait until every attachment is fully uploaded before typing text.
                # Conditions (ALL must hold for 5 consecutive ticks):
                #   - file input still has all images (fileCount == len(images))
                #   - ChatGPT composer shows thumbnails for each image (M5: any blob/file/attachment
                #     testid/alt pattern)
                #   - no progress bars / spinners / aria-busy in composer
                #   - no "Uploading..." / "loading" / "上传中" text near attachments
                deadline = time.time() + 240
                stable = 0
                last_n = -1
                while time.time() < deadline:
                    state = page.evaluate(r"""() => {
                        const fi = document.querySelector('input[type="file"]');
                        const fileCount = fi ? fi.files.length : 0;
                        const form = document.querySelector('form') || document.body;
                        const thumbs = form.querySelectorAll(
                            'img[src^="blob:"], img[src*="file-"], ' +
                            '[data-testid="attachment"], [data-testid*="attachment"], ' +
                            'img[alt*="attachment" i], img[alt*="uploaded" i]'
                        );
                        // M5 audit fix: include [aria-busy="true"] (broader spinner signal)
                        const busy = !!document.querySelector(
                            '[role="progressbar"], .animate-pulse, [data-state="loading"], [aria-busy="true"]'
                        );
                        const txt = (form.innerText || '').toLowerCase();
                        const uploadingText = txt.includes('uploading')
                            || txt.includes('上传中')
                            || txt.includes('loading');
                        return { n: thumbs.length, fileCount, busy, uploadingText };
                    }""")
                    n, fileCount = state['n'], state['fileCount']
                    busy, uploadingText = state['busy'], state['uploadingText']

                    # Bail early if files were lost (e.g. set_input_files silently failed)
                    if fileCount == 0 and len(images) > 0:
                        print(f'[upload] file input cleared unexpectedly — re-setting',
                              file=sys.stderr)
                        fi.set_input_files(images, timeout=60000)
                        stable = 0
                        time.sleep(1)
                        continue

                    if n != last_n:
                        stable = 0
                        last_n = n
                    elif (n >= len(images) and fileCount >= len(images)
                          and not busy and not uploadingText):
                        stable += 1
                        if stable >= 5:
                            break
                    else:
                        stable = 0
                    time.sleep(1)
                print(f'[upload] ready (n={last_n}, fileCount={fileCount}, stable={stable}s)',
                      file=sys.stderr)
                if last_n < len(images):
                    print(f'[upload] WARNING: only {last_n}/{len(images)} thumbnails detected',
                          file=sys.stderr)
                # Final safety pause — give the UI a moment to settle
                time.sleep(2)

                box.evaluate("el => el.focus()")
                time.sleep(0.3)
                page.keyboard.insert_text(text)
                time.sleep(0.5)
                page.keyboard.press('Enter')
                print('[send] submitted', file=sys.stderr)

                # ACT → ASSERT EFFECT: user message must appear in DOM.
                # Bug A: timeout 10s + final post-deadline check.
                if not verify_message_sent(page, c, baseline_user, timeout_s=10.0):
                    # Bug B: if verify timed out but message IS in DOM, mark
                    # it and proceed rather than rc=8 — idempotency needs the
                    # marker to detect retries correctly.
                    if _count_user(page) > baseline_user:
                        print('[send_with_images] verify timed out but user message '
                              'is in DOM — proceeding (Bug A safety net).',
                              file=sys.stderr)
                    else:
                        print('[send_with_images] ASSERT EFFECT FAILED: no user message '
                              'appeared in DOM after fill_input', file=sys.stderr)
                        sys.exit(8)

                # Priority #2: stamp the new user message with the text-hash so a
                # later retry (after wait_for_reply timeout) is detected as
                # idempotent and does not re-upload + re-send.
                if not mark_last_user_message(page, text):
                    print('[send_with_images] WARNING: failed to mark user message '
                          'with idempotency hash. Retries may re-send.',
                          file=sys.stderr)

            write_active_url(backend, page.url)

    # Stream-end detection (P2: returns ReplyStatus so we can distinguish
    # still-streaming from wedged). Bug C fix: this block runs in a SEPARATE
    # sync_playwright context — the outer block has already exited above.
    time.sleep(3)
    status = ReplyStatus.TIMEOUT
    with watchdog(_WATCHDOG_S, 'send_with_images.wait'):
        with sync_playwright() as p:
            browser = connect_browser(p)
            ctx = browser.contexts[0]
            page = find_tab(ctx, backend) or ctx.pages[-1]

            status = wait_for_reply_with_images(
                page, c, baseline_user, timeout_s=420)

    if status == ReplyStatus.DONE:
        return
    # P2: write marker so a parent round.py can distinguish slow-but-still-
    # streaming from truly stuck. round.py reads .gpt_consult/last_reply_status.txt
    # and skips reset_to_new_chat when status == STREAMING.
    os.makedirs('.gpt_consult', exist_ok=True)
    with open('.gpt_consult/last_reply_status.txt', 'w', encoding='utf-8') as f:
        f.write(status.value)
    print(f'[send] TIMEOUT ({status.value})', file=sys.stderr)
    sys.exit(1)


if __name__ == '__main__':
    main()