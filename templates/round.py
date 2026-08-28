"""One round of the consult loop with auto-recovery on stuck.

Usage:
    C:/Python312/python.exe -X utf8 round.py "<text>" <backend>
    C:/Python312/python.exe -X utf8 round.py - <backend>      ← read text from stdin

Recovery matrix (Priority #3 — Round 7):

| state              | rc seen      | recovery action                                  |
|--------------------|--------------|--------------------------------------------------|
| OK                 | 0            | return reply                                     |
| user editing       | 6            | exit 6 (no retry — user has unsaved draft)       |
| TAB STUCK          | 1            | reset_to_new_chat → retry send_message           |
| NO TAB            | 5            | reset_to_new_chat → retry send_message           |
| SUBPROCESS HUNG    | 124          | check CDP → if dead, ensure_browser → retry      |
| CDP DEAD           | any non-0    | ensure_browser → retry send_message              |
| STILL STUCK        | (after 1 retry) | exit 7 (caller should failover to next backend)|

CDP-dead detection: `is_browser_ready()` from media_kit. If False, the
entire round can be salvaged by restarting Chrome via `ensure_browser()`
— all tabs were lost anyway, no need to call reset_to_new_chat.

Exit codes:
  0 = reply received
  6 = input not empty (refused — user editing)
  7 = STUCK even after recovery (caller should failover to next backend)
  124 = subprocess hung longer than timeout (treated as stuck by recovery)
  * = anything from send_message.py is passed through

Subprocess timeout (C1 audit fix): every subprocess.run is wrapped with
timeout=N. If send_message.py's internal watchdog doesn't kick in (e.g.
sync_playwright wedges before the watchdog thread starts), the outer
timeout catches it and propagates as rc=124, which the recovery layer
treats identically to rc=1.
"""
import sys, io, os, subprocess, time
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

HERE = os.path.dirname(os.path.abspath(__file__))
PY = 'C:/Python312/python.exe'
sys.path.insert(HERE, 0)  # so we can import media_kit when cwd differs
from media_kit import is_browser_ready, ensure_browser
from journal import recover_request, STATUS_SENT_CONFIRMED, STATUS_UNKNOWN

# send_message.py has its own 240s stream-wait budget. The reset script
# is much faster (~40s even on cold network). Round-trip budget per
# subprocess call is 300s — generous enough to cover the inner work plus
# media-kit bridge cold-start (~1-2s).
SUBPROCESS_TIMEOUT_S = 300


def call(*args):
    """Run a Python script in this folder, return (returncode, stdout, stderr)."""
    try:
        p = subprocess.run(
            [PY, '-X', 'utf8', os.path.join(HERE, args[0])] + list(args[1:]),
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=SUBPROCESS_TIMEOUT_S,
        )
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        sys.stderr.write(
            f'[round.py] {args[0]} hung > {SUBPROCESS_TIMEOUT_S}s — force-killed by outer timeout\n'
        )
        return 124, '', ''


def ensure_cdp_alive(label: str) -> bool:
    """If Chrome is dead, restart it. Returns True if Chrome was restarted.

    Used by the recovery flow before reset_to_new_chat, since reset_to_new_chat
    also goes through connect_browser() and would itself restart Chrome, but
    doing it explicitly here gives clearer logging and a guaranteed
    pre-recovery state.
    """
    if is_browser_ready():
        return False
    print(f'[round.py] {label}: CDP dead — calling ensure_browser()', file=sys.stderr)
    try:
        ensure_browser()
        return True
    except Exception as e:
        print(f'[round.py] {label}: ensure_browser failed: {e}', file=sys.stderr)
        return False


def main():
    if len(sys.argv) < 2:
        print('usage: round.py <text | -> <backend>', file=sys.stderr)
        sys.exit(2)
    if len(sys.argv) >= 3 and sys.argv[-1] in ('chatgpt', 'deepseek', 'gemini'):
        backend = sys.argv[-1]
        text_arg = sys.argv[1]
    else:
        print('round.py: backend required', file=sys.stderr)
        sys.exit(2)

    text = sys.stdin.read() if text_arg == '-' else text_arg
    if not text.strip():
        print('round.py: empty text', file=sys.stderr)
        sys.exit(2)

    # Save last message for retry / failover / debugging
    os.makedirs('.gpt_consult', exist_ok=True)
    with open('.gpt_consult/last_msg.txt', 'w', encoding='utf-8') as f:
        f.write(text)
    with open('.gpt_consult/last_backend.txt', 'w', encoding='utf-8') as f:
        f.write(backend)

    # Round 8 P4: consult the journal BEFORE attempting. If the most-recent
    # round for this (backend, text) was SENT_CONFIRMED, the AI already
    # replied — refuse to re-send. If it was UNKNOWN, refuse to retry as
    # well (round may have actually completed; we just couldn't confirm).
    prior = recover_request(backend, text)
    if prior is not None and prior['status'] == STATUS_SENT_CONFIRMED:
        print(f'[round.py] REFUSED: round already SENT_CONFIRMED for this '
              f'text (request_id={prior["request_id"]}, '
              f'excerpt={prior.get("reply_excerpt")!r}). '
              f'Extract the reply instead of re-sending.',
              file=sys.stderr)
        sys.exit(10)
    if prior is not None and prior['status'] == STATUS_UNKNOWN:
        print(f'[round.py] REFUSED: prior round is UNKNOWN (round may have '
              f'completed but we lost visibility). request_id='
              f'{prior["request_id"]}. Extract the reply or use a different '
              f'prompt to retry.',
              file=sys.stderr)
        sys.exit(10)

    # Round 1
    print(f'[round.py] attempt 1 → {backend}', file=sys.stderr)
    rc, out, err = call('send_message.py', text, backend)
    if rc == 0:
        print(out, end='')
        print(f'[round.py] {backend} replied OK', file=sys.stderr)
        sys.exit(0)
    if rc == 6:
        print(err, end='', file=sys.stderr)
        sys.exit(6)

    # Priority #3: if Chrome is dead (often the cause of rc=124, sometimes
    # of rc=1/5), restart it FIRST. After restart, all tabs are gone, so
    # we skip reset_to_new_chat and just retry send_message — which will
    # find no tab and open a fresh one.
    if not is_browser_ready():
        print(f'[round.py] Chrome dead after rc={rc} — restarting via ensure_browser()',
              file=sys.stderr)
        ensure_cdp_alive('pre-recovery')
        rc, out, err = call('send_message.py', text, backend)
        if rc == 0:
            print(out, end='')
            print(f'[round.py] {backend} replied OK after CDP restart', file=sys.stderr)
            sys.exit(0)
        if rc == 6:
            print(err, end='', file=sys.stderr)
            sys.exit(6)
        # Still stuck after restart — bail to caller
        print(f'[round.py] STILL rc={rc} after CDP restart — signal failover',
              file=sys.stderr)
        print(err, end='', file=sys.stderr)
        sys.exit(7)

    # rc in (1, 5, 124) → recover via tab reset (Chrome alive).
    if rc not in (1, 5, 124):
        print(err, end='', file=sys.stderr)
        sys.exit(rc)

    reason_map = {1: 'TIMEOUT (tab stuck)', 5: 'NO TAB FOUND', 124: 'SUBPROCESS HUNG'}
    # Round 8 P2: if send_message exited rc=1 because the AI was still
    # STREAMING at hard_deadline (not actually stuck), DO NOT open a new
    # tab — that would discard an in-progress reply. Wait for the existing
    # round to finish instead.
    last_status = ''
    try:
        with open('.gpt_consult/last_reply_status.txt', 'r', encoding='utf-8') as f:
            last_status = f.read().strip()
    except FileNotFoundError:
        pass
    if last_status == 'STREAMING':
        print(f'[round.py] {backend} status=STREAMING (still producing) — '
              f'waiting for completion instead of opening new tab.',
              file=sys.stderr)
        # Reuse the existing round; just resume the wait.
        time.sleep(10)
        rc_re, out_re, err_re = call('send_message.py', text, backend)
        if rc_re == 0:
            print(out_re, end='')
            print(f'[round.py] {backend} replied OK after extended wait',
                  file=sys.stderr)
            sys.exit(0)
        # If still streaming on second pass → truly stuck, proceed to recovery.
        if rc_re == 1:
            try:
                with open('.gpt_consult/last_reply_status.txt', 'r', encoding='utf-8') as f:
                    if f.read().strip() == 'STREAMING':
                        print(f'[round.py] {backend} STILL STREAMING after '
                              f'extended wait — treating as stuck.',
                              file=sys.stderr)
                        rc = rc_re  # fall through to recovery
            except FileNotFoundError:
                pass
    reason = reason_map.get(rc, f'rc={rc}')
    print(f'[round.py] {backend} {reason} (rc={rc}) — opening NEW TAB', file=sys.stderr)
    rc2, out2, err2 = call('reset_to_new_chat.py', backend)
    if rc2 != 0:
        print(f'[round.py] reset failed (rc={rc2}): {err2}', file=sys.stderr)
        # Even if reset failed, Chrome might now be dead. Try one more
        # CDP-restart recovery as a last resort.
        if ensure_cdp_alive('reset-failed'):
            rc, out, err = call('send_message.py', text, backend)
            if rc == 0:
                print(out, end='')
                print(f'[round.py] {backend} replied OK after CDP restart',
                      file=sys.stderr)
                sys.exit(0)
        sys.exit(7)
    print(f'[round.py] new tab ready on {backend}, retrying...', file=sys.stderr)
    time.sleep(2)  # let the new tab settle

    # Round 2
    rc, out, err = call('send_message.py', text, backend)
    if rc == 0:
        print(out, end='')
        print(f'[round.py] {backend} replied OK after recovery', file=sys.stderr)
        sys.exit(0)

    # Still stuck after new tab → tell orchestrator to failover
    print(f'[round.py] {backend} STILL STUCK after recovery (rc={rc}) — signal failover',
          file=sys.stderr)
    print(err, end='', file=sys.stderr)
    sys.exit(7)


if __name__ == '__main__':
    main()