---
name: gpt-consult
description: Long-running "AI consultant loop" between me (Claude) and ChatGPT/DeepSeek/Gemini, with the user observing live in their logged-in Chrome. Use when the user asks "让 GPT 帮我...", "问 GPT 怎么...", "和 GPT 一起做...", "GPT 顾问模式", "多 AI 一起搞", or any task where the user wants an AI as a sounding-board collaborator that I orchestrate step-by-step.
---

# GPT Consult Loop — Level 2 Collaboration

## Changelog

### 2026-08-28 — Round 7: Priority #2 + #3 — Idempotency + CDP lifecycle

Implemented GPT Round 5's #2 and #3 priorities. Two independent fixes:

#### Priority #2 — send_message transaction / idempotency

GPT Round 5 flagged: `wait_for_reply` can time out AFTER the message was
submitted. round.py may then retry the call. Without idempotency, the
retry would submit the same prompt twice.

Fix: each user message gets a `data-gpt-consult-hash="<md5[:8] of text>"`
attribute. Before filling, `send_message.py` checks for a marked message
with the same hash. If found → skip fill, resume wait.

New idempotency helpers in `templates/_helpers.py`:

| Helper | Purpose |
|---|---|
| `text_marker(text)` | Stable md5[:8] hash of `text`. Used as the DOM marker. |
| `find_existing_send(page, text)` | True if `[data-gpt-consult-hash="<marker>"]` exists in DOM (i.e. this is a retry) |
| `mark_last_user_message(page, text)` | Stamp the last user message with `data-gpt-consult-hash`. Called after `verify_message_sent` succeeds. |

Callers updated:

| File | Change |
|---|---|
| `send_message.py` | Before fill: `find_existing_send` → if True, skip fill, `baseline_user -= 1`, resume wait. After `verify_message_sent`: `mark_last_user_message`. Logs "[send_message] idempotent retry detected" on skip. |
| `send_with_images.py` | Same pattern. Idempotent path skips `set_input_files` + upload-wait + fill, jumps straight to `wait_for_reply_with_images`. |

**Why this matters**: the duplicate-send risk was concrete — a 240s timeout
followed by a round.py retry would result in two identical user messages
in chatgpt, fragmenting the AI's response across both rounds. Now retries
are at-most-once.

#### Priority #3 — CDP + tab lifecycle / round.py recovery matrix

GPT Round 5 flagged: the `connect_browser()` helper existed (C3 rev-2
fix from Round 3) but **was never wired into any caller**. Every template
called `p.chromium.connect_over_cdp(cdp_url())` directly. So stale-URL
and dead-Chrome states were not recovered — only the existing connection
errors would surface.

Fix: wire `connect_browser()` into all 11 templates, upgrade it to
call `ensure_browser()` if Chrome is dead (not just retry with the same
stale URL), and add a CDP-dead detection branch to `round.py`.

`templates/media_kit.py` — `connect_browser()` upgraded:

```python
def connect_browser(p):
    for attempt in range(2):
        try:
            if not is_browser_ready():     # ← NEW: ask for Chrome if dead
                ensure_browser()
            return p.chromium.connect_over_cdp(cdp_url())
        except Exception as e:
            refresh_cdp_url()
            continue
    raise last_err
```

Wired into all 11 templates — every `p.chromium.connect_over_cdp(cdp_url())`
became `connect_browser(p)`. Affected files:
`send_message.py`, `send_with_images.py`, `check_status.py`,
`extract_reply.py`, `is_logged_in.py`, `set_backend.py`,
`reset_to_new_chat.py`, `open_chatgpt.py`.

`open_chatgpt.py` was also modernized: dropped the powershell-based port
detector (no longer needed — media-kit owns the CDP endpoint) and the
substring hostname check (`'chatgpt.com' in page.url` → `page_host_matches`).

`templates/round.py` — recovery matrix formalized:

| state | rc seen | action |
|---|---|---|
| OK | 0 | return reply |
| user editing | 6 | exit 6 (no retry) |
| **CDP DEAD** | any non-0 | `ensure_browser()` → retry `send_message` |
| TAB STUCK | 1 | `reset_to_new_chat` → retry |
| NO TAB | 5 | `reset_to_new_chat` → retry |
| SUBPROCESS HUNG | 124 | (handled as CDP DEAD if Chrome dead, else as TAB STUCK) |
| STILL STUCK | after 1 retry | exit 7 (failover to next backend) |

The new `ensure_cdp_alive()` helper in `round.py` logs "[round.py]
Chrome dead — calling ensure_browser()" before restart.

**Why this matters**: prior to Round 7, if media-kit died or Chrome was
restarted between calls, every template would silently fail with
ECONNREFUSED on the first `connect_over_cdp`. Now `connect_browser`
detects the dead state via `is_browser_ready()`, asks media-kit to start
Chrome, refreshes the URL cache, and retries once. The entire
send-message round becomes self-healing across Chrome restarts.

### 2026-08-28 — Round 6: Priority #1 — Selector resilience / semantic UI contracts

Implemented GPT Round 5's top-priority recommendation: close the gap
between "element detected" and "operation succeeded". Pattern:
**FIND → ASSERT SEMANTIC IDENTITY → ACT → ASSERT EFFECT**.

New semantic helpers in `templates/_helpers.py`:

| Helper | Purpose |
|---|---|
| `_is_visible(loc)` | True if `offsetParent !== null` — rejects hidden / detached DOM |
| `_is_editable(loc)` | True if contenteditable=true OR visible non-readonly textarea/input |
| `find_real_composer(page, c)` | First selector match that is BOTH visible AND editable — rejects hidden fallback |
| `find_real_reply_text(page, c, baseline_user, skip_baseline=False)` | Latest assistant text that is non-empty + visible + (optionally) newer than baseline |
| `is_real_streaming(page, c)` | Stop-button visible AND attached to active generation |
| `verify_message_sent(page, c, baseline_user, timeout_s=5)` | After fill_input, assert a user message actually appeared in DOM (the "ASSERT EFFECT" step) |

Callers updated to use the new helpers:

| File | Change |
|---|---|
| `send_message.py` | `find_real_composer()` replaces inline selector loop. `verify_message_sent()` added right after `fill_input` (rc=8 on failure). `wait_for_reply` uses `is_real_streaming` + `find_real_reply_text`. |
| `send_with_images.py` | Same pattern applied. Replaced inferior inline state machine (saw_streaming → sleep(3)) with rev-3 unified terminal. Added `verify_message_sent`. |
| `check_status.py` | `is_real_streaming()` replaces naive `count() > 0` for stream detection. |
| `extract_reply.py` | `find_real_reply_text(skip_baseline=True)` replaces raw count + nth().inner_text() chain. |
| `backend_config.py` | No change — selectors already work because `_is_visible` + `_is_editable` reject hidden fallback textareas. |

**Why this matters**: chatgpt UI has shifted from `#prompt-textarea`
(hidden textarea fallback) to `#mobile-composer-prompt` (mobile ProseMirror)
to `#prompt-textarea` (ProseMirror div with contenteditable=true). Each
shift could silently make `count() > 0` match a stale element. The new
helpers refuse to operate on hidden/non-editable nodes, so the skill
fails loud (rc=4 with diagnostic) instead of writing into the void.

Audit order remaining:
2. send_message transaction / idempotency
3. CDP + tab lifecycle / round.py recovery

### 2026-08-28 — Round 5: GPT verdict (historical note — STATUS: COMPLETE marker now removed in Round 12)

All Round 4 findings closed. GPT verdict per ID:

| ID | Verdict | Notes |
|---|---|---|
| M4 rev-3 | ✅ COMPLETE | Unified terminal rule closes Gap A (continuous polling replaces sleep+return) and Gap B (empty containers reset stability, not accumulate) |
| M2 rev-3 | ✅ COMPLETE | `created_tab = page` immediately after `new_page()` closes navigation-timeout leak. Propagated to 3 sibling files (extract_reply / is_logged_in / set_backend). send_message.py correctly excluded (tab deliberately survives across two sync_playwright blocks) |
| C3 | ✅ COMPLETE | Cleaned unused `last_err` |
| M1 | ✅ COMPLETE | No new issue |

**GPT priority pick for next round: #1 Selector resilience / UI contract**

The next-highest-risk class of failure is NOT a single bug — it's **silent
semantic failure from selector drift**:

```
selector exists
      ↓
but points to wrong/stale UI element
      ↓
automation continues
      ↓
incorrect result
```

GPT-recommended audit shape:
```
FIND → ASSERT SEMANTIC IDENTITY → ACT → ASSERT EFFECT
```

For example, for `send_message`:
- composer located → verify it is the editable composer (not a hidden fallback textarea)
- inject text → verify user message actually appeared in DOM
- send → verify assistant response belongs to THIS request (not stale node)

Concrete: ChatGPT UI changed from `#prompt-textarea` to `#mobile-composer-prompt`
and back — selectors that just check "did the locator exist?" can match a
hidden/stale element while the real composer is somewhere else. The
audit is about closing the gap between "element detected" and "operation succeeded".

Audit order GPT suggested:
1. Selector resilience + semantic UI contracts   ← START HERE
2. send_message transaction / idempotency
3. CDP + tab lifecycle / round.py recovery

### 2026-08-28 — Round 4 audit (GPT verdict on P0/P1 fixes)

GPT Round 3 had flagged 4 P0/P1 findings. Round 4 sent the revised code back for re-verdict. Result: M4 still partial, M2 still had gap, C3 complete, M1 complete.

| ID | Round 4 fix | File |
|---|---|---|
| M4 rev-3 | Unified terminal: assistant exists + non-empty text + !streaming + stable 3s (continuously polled). Replaces dual-path state machine. Reject empty assistant containers. | `templates/send_message.py` `wait_for_reply()` |
| M2 rev-3 | `created_tab = page` now set **immediately after `ctx.new_page()` and BEFORE `page.goto()`** so navigation timeout triggers finally → close(). Same fix propagated to `extract_reply.py`, `is_logged_in.py`, `set_backend.py` (all 3 had same ownership-too-late bug). | `templates/check_status.py`, `extract_reply.py`, `is_logged_in.py`, `set_backend.py` |
| C3 final | Dropped unused `last_err` variable in `connect_browser()`. GPT verdict: 1 retry is sufficient, no need to classify error types. | `templates/media_kit.py` |
| M1 final | Already complete; only outstanding item is regression test for trailing-dot hostnames and chatgpt-clone.attacker.com variants. Not a real-world risk unless backend URLs appear that way. | `templates/backend_config.py` `page_host_matches()` |

### 2026-08-28 — Audit + 14 defects fixed

Audit source: GPT (Round 1+2 review of `templates/*.py`), verified against source. All 14 defects addressed:

| ID | Fix | Files |
|---|---|---|
| C1 | round.py `subprocess.run` now has `timeout=300`; `TimeoutExpired` → rc=124 treated as stuck | `templates/round.py` |
| C2 | Every `sync_playwright` block wrapped with `watchdog(N, label)` (force-exits after budget) | new `templates/_helpers.py`, all templates |
| C3 | `CDP_URL` module-level constant → `cdp_url()` function + `refresh_cdp_url()` (no stale cache after Chrome restart) | `templates/media_kit.py`, all callers |
| M1 | `find_tab` reads `.gpt_consult/active_<backend>.txt` to disambiguate multiple same-host tabs | `templates/backend_config.py`, `_helpers.py`, `send_message.py` writes the file |
| M2 | `check_status` closes the tab it auto-creates (was leaking one tab per run) | `templates/check_status.py` |
| M3 | Dropped `baseline_asst` snapshot. Round marker is now `user_count > baseline_user` (safer when page already has pending reply) | `templates/send_message.py`, `templates/send_with_images.py` |
| M4 | Stream-completion state machine: stop-button OR (no-stream + new-user-msg) → done. Not stop-button-only | `templates/send_message.py`, `templates/send_with_images.py` |
| M5 | Upload detection adds `[aria-busy="true"]` + re-sets files if input cleared | `templates/send_with_images.py` |
| M6 | Login-check loop extracted to `check_logged_in()` helper; 4 callers now use one source of truth | `templates/_helpers.py`, `set_backend.py`, `check_status.py`, `reset_to_new_chat.py`, `is_logged_in.py` |
| N1 | `get_order()` validates `CONSULT_BACKENDS` env, exits 2 on unknown name | `templates/backend_config.py` |
| N2 | `host_of()` uses `urllib.parse.urlparse` instead of brittle `split('//',1)` | `templates/backend_config.py` |
| N3 | Deleted dead `paste_text()` from `send_message.py` (replaced by `keyboard.insert_text` long ago) | `templates/send_message.py` |
| N4 | Selector-version comment at top of `backend_config.py` (re-verify after each backend UI update) | `templates/backend_config.py` |

Side fixes found during implementation:
- `check_status.cdp_alive(port=9333)` hardcoded → derives port from `cdp_url()`.
- All `count()` calls wrapped in try/except per selector so one stuck selector can't take down the whole loop.
- `wait_for_reply` extracted as a helper function in `send_message.py`.

Verified:
- `check_status chatgpt` × 2 runs → `tabs_total` stable at 2 (M2 leak confirmed fixed).
- `is_logged_in chatgpt` → exit 0 (logged in).
- `check_status chatgpt` → JSON shape unchanged.

Not verified (Chrome CDP driver broke after `TaskStop` on a hung send):
- E2E `send_message` round-trip with new baseline logic.
- Round-3 GPT verify of fixes (file changes only — verify was deferred).

**Recovery notes**:
- If `sync_playwright` wedges despite watchdog, kill `chrome.exe` and call `media-kit ensure_browser` to respawn Chrome (loses login state).
- The watchdog kills with `os._exit(124)` — no cleanup. `round.py` treats rc=124 as stuck and triggers recovery.

## What this skill does

Run a **persistent multi-AI loop** with the user watching live:

```
┌─────────────┐  post message   ┌──────────────┐
│   Claude    │ ──────────────▶ │ ChatGPT/DS/  │   ← user watches this live
│  (orchestr.) │ ◀────────────── │ Gemini       │      in their Chrome
└──────┬──────┘   read reply    └──────────────┘
       │
       │ run code / read files / write tests  ← local work between rounds
       │
       ▼
   progress to user
```

**Roles are strictly separated:**
- **ChatGPT / DeepSeek / Gemini = consultant** (suggests approaches, gives opinions)
- **Claude (me) = executor** (runs code, reads/writes files, formats next prompt)
- **User = observer + can interject by typing into the browser directly**

The user sees every AI reply in real-time and can break in by typing in their Chrome.

## When to invoke

User says any of:
- "让 GPT 帮我 X" / "让 ChatGPT 帮我 X"
- "问下 GPT 怎么 X"
- "和 GPT 一起做 X"
- "GPT 顾问模式" / "GPT 协作模式"
- "GPT + Claude 一起搞"
- "多 AI 一起搞" / "DeepSeek 也来"
- "先问 GPT 然后做"
- "你俩配合"

**Don't invoke** when:
- Task is trivial and I can do it directly
- The AI would be slower than just doing it
- User wants pure speed (skip the back-and-forth)
- Task has no design component (just execution)

## Architecture

```
mediaKit MCP  ──── opens the Chrome PROCESS (chrome.exe, CDP port 9333)
   │
   ▼
Browser:  user's real Chrome (CDP port 9333, already open with login)
   │
   ▼
Playwright (Python) ──── connects via CDP. Handles EVERYTHING inside:
   ctx.new_page()  ·  page.goto()  ·  click  ·  type  ·  paste  ·  read  ·  close
```

The split is **process vs content**:
- **mediaKit** = lifecycle of the Chrome process itself (launch).
- **Playwright** = all in-Chrome work, against that same Chrome via CDP port 9333.

**Hard rules:**
1. **The Chrome process is NEVER closed by the skill.** Only the user closes Chrome
   manually. All scripts use `with sync_playwright()` which only disconnects —
   `connect_over_cdp()` does not terminate Chrome on context exit.
2. **All in-Chrome work uses Playwright.** Opening a tab is `ctx.new_page()`
   + `page.goto(...)`. Navigating an existing tab is `page.goto(...)`. Closing
   an extra tab is `page.close()`. mediaKit is NOT used for any of this — it
   only launched Chrome.
3. **The active tab is identified by URL host** (`host_of(backend)`), never by
   `pages[0]`. This lets the user have extra tabs open (e.g. docs, music) without
   breaking the skill.
4. **Large text → `page.keyboard.insert_text()`**, not `.fill()` and not char-by-char
   `.type()`. Dispatches `beforeinput`/`input` events that React-based frameworks
   (ChatGPT ProseMirror, Gemini rich-textarea) handle correctly, and avoids IME
   issues with Chinese / unicode.

## HARD RULE — image upload must finish before text

When sending a message WITH image attachments (`send_with_images.py`):

> **MUST wait until every image is fully uploaded to the backend before
> pasting text into the input box.** No exceptions. Skipping this produces
> orphan text messages sent without images (or images sent without their
> prompt text).

Implementation lives in `templates/send_with_images.py`:
1. Call `set_input_files()` on the hidden file input.
2. Poll until ALL three conditions hold for **5 consecutive seconds**:
   - thumbnail count ≥ expected image count (covers `img[src^="blob:"]`,
     `[data-testid="attachment"]`, `img[alt*="attachment"]`)
   - no `[role="progressbar"]`, `.animate-pulse`, or `[data-state="loading"]`
   - no "Uploading…" / "上传中" text in composer
3. Then paste text + press Enter.

If after 240s the count is still short, log a WARNING but proceed. Never type
text while a spinner is still visible.

## Tab policy — the most important section

> **One tab per backend conversation. Stay in that tab while it works. Open a NEW tab only on failure. Old tabs may be closed after the new one is operational.**

| State | What to do | Why |
|---|---|---|
| Backend OK, conversation flowing | **Stay in the same tab** | GPT/DeepSeek/Gemini has memory within one conversation. Switching tabs breaks that. |
| Current tab stuck / hung / page unresponsive | **Open a NEW TAB on the same backend. After new tab is verified working, you may close the old stuck tab.** | New tab starts a fresh conversation. Re-send last message into the new tab. |
| Current backend completely down (e.g. ChatGPT down) | **Open a NEW TAB on a different backend** (deepseek → gemini). After new tab is verified working, you may close the old backend's tab. | Failover. |
| User wants parallel consultations | **Open one tab per concern** | Each gets its own transcript. |

**`reset_to_new_chat.py` opens a NEW TAB** on the same backend (Playwright
`ctx.new_page()` + `page.goto(...)`), then closes the OLD stuck tab once the new
one is verified working. It does NOT navigate the existing tab.

**`set_backend.py` opens a NEW TAB** for a different backend (or navigates an
existing tab if one is already on that backend's host).

All templates identify the "active tab" by URL host (`host_of(backend)`), not by `pages[0]`. So the script is robust to multiple open tabs of the same or different backends.

## Supported backends

| Backend | URL | Verified logged-in | Notes |
|---|---|---|---|
| `chatgpt` | https://chatgpt.com | ✅ (this machine, 2026-08) | Default first |
| `deepseek` | https://chat.deepseek.com | ✅ | Faster replies in Chinese |
| `gemini` | https://gemini.google.com/app | ✅ | Google account required |

All three verified end-to-end with the "1+1=?" smoke test.

Selectors live in `templates/backend_config.py`. Add a new backend by appending an entry (display name, URL, input_selectors, reply_selectors, stream_selectors, login_selectors, new_chat_selectors).

## Multi-backend failover

When a backend gets stuck, the loop can **switch to a different AI**:

```
Backend priority (env var CONSULT_BACKENDS, default below):
    CONSULT_BACKENDS=chatgpt,deepseek,gemini
```

State tracked per round:
- `current_backend` — which AI we're talking to right now
- `strikes[current_backend]` — count of "stuck" incidents on this backend

**Failover trigger** (`FAILOVER_AFTER_STRIKES`, default 2):
After N stuck incidents on the current backend:
1. Append transcript entry: `[backend X] FAILOVER — switching to Y`
2. Run `set_backend.py Y` → opens a NEW TAB for backend Y
3. If Y is not logged in → skip it, try the next one
4. If all backends are unavailable → pause and ask the user
5. Re-send the last message into the fresh backend (clean context)

**Reset strikes** on any successful round (got a non-empty reply).

**Don't failover** when:
- User just typed in the current chat (their message is the latest user role) — read their message first
- The stuck detection is "stream too long" but the stream eventually finished — just log the slow round, no failover

**Important: failover to a different backend ALWAYS opens a new tab. Failover to the same backend (after a stuck tab) ALSO opens a new tab.** Either way, the old tab is never closed.

## Pre-flight checks (run before each round)

```python
# In Python, before every send_and_wait():
# 1. CDP port alive?  (curl http://127.0.0.1:9333/json/version)
# 2. Active tab for this backend open?  (find_tab by URL host)
# 3. Last response fully streamed (stop button gone)?
# 4. Did user type anything new (interrupt signal)?
# 5. tabs_total? (sanity check that browser is alive)
```

Templates in `templates/` (all Playwright-CDP — connect to mediaKit's Chrome on 9333):
- `backend_config.py` — backend URL + selector map (chatgpt/deepseek/gemini) + `find_tab()` helper
- `check_status.py [backend]` — health check + interrupt detection, finds tab by URL
- `send_message.py "<text>" [backend]` — fills input + waits for stream completion (240s timeout); auto-opens a new tab if none exists
- `extract_reply.py [backend]` — pulls the newest AI message text
- `reset_to_new_chat.py [backend]` — opens a NEW TAB on the same backend, closes the old stuck tab
- `set_backend.py <backend>` — opens a NEW TAB for a different backend (or reuses existing); verifies logged-in + ready
- `is_logged_in.py [backend]` — check if a backend is logged in
- `open_chatgpt.py` — legacy single-backend helper

## Execution loop

**No round limit.** Loops until the user stops me or the AI signals completion.

```
loop:
    1. check_status(backend)
       - if user_typed_anything_new: PAUSE, read what they wrote, fold in
    2. compose next message to GPT
       - context: original task + last AI reply + my execution result
    3. send_message(text)             ← STAYS in same tab (preserves memory)
    4. extract_reply()
    5. execute local work based on AI's advice
    6. write transcript entry (append-only log)
    7. check exit conditions (only ONE hard exit):
       - **EXIT A — you (user) say "停/结束/stop/done" to me in this Claude
         Code conversation** → IMMEDIATE stop, no questions
       - everything else → loop again
    8. **STUCK? OPEN NEW TAB on same backend** (reset_to_new_chat.py)
       - only if hung/empty/repeated for N strikes on current backend
    9. **BACKEND DOWN? OPEN NEW TAB on different backend** (set_backend.py)
       - only after FAILOVER_AFTER_STRIKES on the current backend
   10. **PIVOT MODE** (not an exit): if you change the task direction by
       talking to me, I compose a pivot message to AI describing the new
       direction, send it, then loop continues under the new direction.
```

## Exit conditions (exactly one — user-controlled)

| # | Signal | Source | Behavior |
|---|---|---|---|
| A | User says "停 / 结束 / stop / done / 够了" to me | This Claude Code conversation | **IMMEDIATE STOP**. No "are you sure?", no questions. Final summary emitted, transcript closed. |

**Round 12 change** (this commit): the previous Exit B (AI emits
`STATUS: COMPLETE` or free-form equivalent) was removed. The AI is a
consultant — it doesn't decide when the user is done. Empirically GPT
emitted the marker too readily, breaking multi-round iteration. The
loop now runs until the user explicitly says stop. To end a
consultation, say "停 / 结束 / stop / done" to Claude.

## Pivot handling (NOT an exit)

If you talk to me **without** saying stop, but you **change direction**:

1. I read your new direction from your message
2. I do NOT stop the loop
3. I compose a new prompt to AI: *"Previous direction was X. New direction from user: Y. Please adjust."*
4. Send to AI, continue looping under new direction
5. Same transcript file (task slug unchanged)

Only the literal stop words trigger Exit A. Anything else is treated as direction.

## What does NOT exit the loop

- ❌ AI takes long time (>2 min thinking) → wait, no exit
- ❌ Many rounds (no cap) → keep going, no exit
- ❌ AI keeps suggesting more ideas → keep doing them, no exit
- ❌ User types in the AI page → fold in, no exit
- ❌ Stream gets stuck (temporary) → open new tab, no exit
- ❌ One backend fails → failover, no exit
- ❌ All backends fail → PAUSE (ask user), NOT exit by itself
- ❌ You ask a clarifying question in this conversation → answer, then continue

**Important exception**: if ALL backends are unavailable AND user doesn't respond for a long time, the loop may pause. But pause ≠ exit. Resume when user comes back.

## Stuck / Hang detection & recovery

A conversation is "stuck" if **any** of:

| Signal | Detection | Threshold |
|---|---|---|
| Stream running forever | `check_status.py` `stream_running=true` | > 180s |
| Empty / tiny assistant reply | `extract_reply.py` returns "" or < 5 chars | 1 occurrence |
| Repeated content | last reply > 70% similar to previous reply | 2 occurrences |
| Error toast / blocked | `text=Something went wrong` visible | 1 occurrence |
| Page completely unresponsive (click/Enter ignored) | `check_status.py` shows stale state > 60s | 1 occurrence |
| User says "卡了/卡死/重开/新对话" | literal match | immediate |

**Two-layer auto-recovery** (handled by `round.py` wrapper, NOT the orchestrator):

```
Layer 1: send_message.py self-heals if no tab exists
   ├─ find_tab() returns None → ctx.new_page() + page.goto() → fresh tab
   └─ User is editing (input not empty) → refuse with rc=6 (don't corrupt draft)

Layer 2: round.py handles timeout / persistent stuck
   ├─ send_message times out (rc=1) OR no tab (rc=5) → reset_to_new_chat.py
   │     ├─ Opens NEW tab on same backend
   │     ├─ Closes OLD stuck tab (user said: 关掉多余的标签)
   │     └─ Waits for input box
   ├─ Retry send_message.py once
   └─ If still rc=1 → rc=7 to orchestrator → failover to next backend
```

**Why this matters**: the user explicitly said "你把浏览器关闭，然后再重新打开操作，你不要在测试的时候用原来的，因为这个页面非常卡" — stuck tabs MUST be auto-recovered without manual intervention. `round.py` makes this automatic.

**Recovery flow (same backend)** — via round.py:

```
1. send_message.py waits ≤ 240s for reply
2. Timeout → rc=1
3. round.py calls reset_to_new_chat.py
   - OPENS A NEW TAB on the same backend
   - In the new tab, clicks sidebar "New chat" (or hard-navigates)
   - Waits for empty input box
4. round.py calls send_message.py again
5. If reply OK → done
6. If still timeout → rc=7 (orchestrator failover to deepseek/gemini)
```

**Why this works**: Within a tab, the AI remembers the conversation. When that tab is dead, opening a NEW tab on the same backend still gives us a clean (memory-less) conversation, but the old tab is closed (no clutter). My transcript file remembers the task — I re-feed the last message into the new tab so the AI has the immediate context it needs.

**Don't reset when**:
- User typed in the OLD conversation (their message is the latest user role). Instead, ask: "你是想在新对话重新开始，还是继续在这个对话里回复?"
- User has a draft in the input box (`localStorage['oai/apps/conversationDrafts']` for ChatGPT) — round.py refuses with rc=6 and asks user to clear or open a fresh tab.

## Multi-conversation fallback (advanced)

If user wants two parallel AI consultations ("ChatGPT 设计, DeepSeek 评审"):

```python
page_a = find_tab(ctx, 'chatgpt')   # chatgpt.com/c/AAA (or open new)
page_b = find_tab(ctx, 'deepseek')  # chat.deepseek.com (or open new)
```

Each is its own tab. Tag each transcript entry with the backend name. Skill still works — `find_tab` returns the correct one for each operation.

## Transcript format

`.gpt_consult/<slug>/transcript.md`:

```markdown
# Task: <one-line description>

## Round 1 (backend: chatgpt)
**To GPT**: <message sent>
**GPT reply**: <verbatim or summarized>
**My execution**: <what I ran, results>
**Files touched**: <paths>

## Round 2 (backend: chatgpt, tab_id: ABC123)
...

## Round 3 (backend: deepseek, NEW TAB after chatgpt stuck)
**Context**: chatgpt tab hung at round 2, opened new tab on deepseek
**To DS**: <message re-fed>
...
```

This is the audit trail — user can `cat` it any time.

## How user triggers this

Just say any of:
- "让 GPT 帮我设计一个 GraciousWeb 教师登录的 E2E 测例"
- "用 GPT 顾问模式调这个 bug"
- "你问下 GPT 这个 SQL 怎么优化"
- "和 GPT 一起 review 这段代码"
- "DeepSeek 也来一起看"
- "Gemini 兜底"

Then I will:
1. Confirm "OK 启动 GPT 顾问模式，task 是 X，预计 N 轮" — quick yes/no
2. Open ChatGPT (and/or others) in your Chrome
3. Send first message
4. Show you the reply + what I did locally
5. Loop until done / interrupted / completion

## How to interrupt (3 entry points)

| Where you type | What I do |
|---|---|
| In AI's page (your Chrome) | Next round, I read your message from the page and incorporate it |
| In this Claude Code conversation | I stop the loop, full attention to you |
| Ctrl+C on terminal | Bash subprocess killed, loop ends mid-round |

## Trade-offs (transparency)

✅ **Pros of this pattern**
- Three AI perspectives reduce blind spots
- User has live visibility into AI's reasoning
- Strong audit trail (.gpt_consult/...)
- Easy to step in and redirect
- Failover means we never get permanently stuck

⚠️ **Cons**
- ~3x slower than me working solo (round-trip latency)
- ~3x more tokens (I read AI's replies, write them back)
- AI can't see my local code — I must transcribe
- LLM-asks-LLM can spiral if no human steering

## Anti-pattern: don't use this for

- Trivial tasks ("把这个文件 rename")
- Pure speed work ("grep 这个正则")
- Production deployments (too slow, too many tokens)
- Tasks with no design component (just need to run X)

## File layout

```
~/.claude/skills/gpt-consult/
├── SKILL.md           ← this file
└── templates/
    ├── backend_config.py     ← URL + selectors + find_tab() helper
    ├── check_status.py
    ├── open_chatgpt.py
    ├── send_message.py
    ├── extract_reply.py
    ├── reset_to_new_chat.py  ← opens NEW TAB
    ├── set_backend.py        ← opens NEW TAB for different backend
    └── is_logged_in.py
```

## Usage example

> **You**: 用 GPT 顾问模式帮我设计 GraciousWeb 教师登录的 E2E 测例，覆盖弱网、重试、鉴权失败
>
> **Me**: OK 启动 GPT 顾问模式。
> [opens ChatGPT in your Chrome — uses existing tab if present, else new]
> **To GPT (round 1)**: 设计一个 Vue 教师登录 E2E 测试...
> **GPT reply**: 建议用 page.route() 拦截 /api/auth/login...
> **My execution**: 打开 GraciousWeb/playwright.config.ts 看现有配置，照 GPT 建议写 L8-弱网.spec.ts
> **To GPT (round 2)**: 写完了 L8，跑通过，L9 报 'mocked response not triggered'，为什么?
> **GPT reply**: page.route() 必须用 **/* glob 或者完整 URL...
> ...
> [if chatgpt tab hangs] **Me**: ChatGPT tab 卡了，开新 tab 继续...
> [if chatgpt down] **Me**: ChatGPT 整个挂了，切 DeepSeek...
> **Me**: 三轮搞定，写入 .gpt_consult/gracious-e2e/，要不要继续加 L10-并发登录?
