# gpt-consult

> Multi-AI consultation loop for Claude Code. Claude (executor) + ChatGPT / DeepSeek / Gemini (consultants), with the user observing live in their own Chrome.

![status: verified](https://img.shields.io/badge/audit-Round_8_VERIFIED-2ea44f)
![license: MIT](https://img.shields.io/badge/license-MIT-blue)
![Claude Code skill](https://img.shields.io/badge/Claude_Code-skill-D97757)

---

## What is it?

A Claude Code **skill** that turns any debatable design question into a
multi-AI deliberation. Instead of Claude answering alone, it pulls
ChatGPT, DeepSeek, and Gemini into the loop as consultants — and you
watch their replies live in your own Chrome, free to interject at any
moment.

```
┌──────────┐  write msg    ┌──────────────┐  ← you watch live in Chrome
│  Claude  │ ────────────▶ │ ChatGPT       │
│ (exec)   │ ◀──────────── │ DeepSeek      │
│          │   read reply  │ Gemini        │
└────┬─────┘               └──────────────┘
     │
     │ write code / run tests / read files
     ▼
  local work between rounds
```

Strict role separation:

| role | what it does |
|---|---|
| **ChatGPT / DeepSeek / Gemini** | consultants — suggest, critique, vote |
| **Claude** | executor — runs code, reads/writes files, formats next prompt |
| **You** | observer + can interject by typing in the browser |

## Why use it?

- **Single-AI blind spots**: Claude writing code can't see how GPT would do it.
- **No human-in-the-loop**: most AI agents run unattended; this one you watch.
- **Audit trail**: every round lands in `.gpt_consult/<slug>/transcript.md`.
- **Stuck recovery**: a hung tab → auto-opens a new tab. A dead backend →
  auto-failover to the next AI.

## Install

This is a Claude Code skill — it auto-loads from `~/.claude/skills/`.

```bash
git clone https://github.com/SongJohnHannah/gpt-consult-skill.git \
  ~/.claude/skills/gpt-consult
```

**Prerequisites**:

- Claude Code (any recent version)
- A logged-in Chrome instance reachable via CDP on port 9333.
  This skill uses the [`media-kit`](https://github.com/SongJohnHannah/media-kit)
  MCP for browser control.
- Playwright (Python): `pip install playwright && playwright install chromium`

## Use

Just say any of:

- "让 GPT 帮我设计 X"
- "问下 GPT 怎么 X"
- "和 GPT 一起做 X"
- "GPT 顾问模式"
- "DeepSeek 也来"
- "多 AI 一起搞"

Then Claude will:

1. Confirm: "OK 启动 GPT 顾问模式，task 是 X，预计 N 轮"
2. Open ChatGPT (and/or DeepSeek, Gemini) in your Chrome
3. Send the first message
4. Show you the reply + what it did locally
5. Loop until you stop or the AIs signal completion

You can interrupt from three places:

| where you type | what Claude does |
|---|---|
| in the AI's page (your Chrome) | reads your message, folds in, keeps looping |
| in this Claude Code conversation | stops the loop, full attention to you |
| `Ctrl+C` on terminal | bash subprocess killed, loop ends mid-round |

## Architecture

```
media-kit MCP  ──► opens the Chrome process (chrome.exe, CDP 9333)
                  │
                  ▼
Browser:  user's real Chrome (CDP 9333, already open with login)
                  │
                  ▼
Playwright (Python) ──► connectOverCDP. handles EVERYTHING inside:
   ctx.new_page() · page.goto() · click · type · paste · read · close
```

**Hard rules** (see `SKILL.md` for full contract):

1. The Chrome process is NEVER closed by the skill.
2. All in-Chrome work uses Playwright. media-kit only launched Chrome.
3. The active tab is identified by URL host, never by `pages[0]`.
4. Large text → `page.keyboard.insert_text()`, not `.fill()`, not `.type()`.
5. Image upload MUST finish before text paste (no orphan messages).

## Files

| path | what |
|---|---|
| `SKILL.md` | full Claude-facing contract (load this first) |
| `templates/backend_config.py` | chatgpt/deepseek/gemini URLs + selectors + `find_tab()` |
| `templates/_helpers.py` | `find_real_composer`, `ReplyStatus`, `submit_message`, `detect_pending_attachments` |
| `templates/send_message.py` | text-only send + wait_for_reply (P2 state machine) |
| `templates/send_with_images.py` | image-aware send (hard rule #6) |
| `templates/round.py` | orchestrator wrapper (failover, journal, recovery) |
| `templates/journal.py` | SQLite request_id journal (P4 — UNKNOWN sticky) |
| `templates/reset_to_new_chat.py` | opens NEW TAB on same backend |
| `templates/set_backend.py` | opens NEW TAB for different backend |
| `templates/check_status.py` | health check + interrupt detection |
| `templates/extract_reply.py` | pulls newest AI message text |
| `templates/is_logged_in.py` | verify backend session |
| `templates/open_chatgpt.py` | legacy single-backend helper |
| `templates/media_kit.py` | CDP connect helper |

## Audit status

This skill was shipped with **GPT-verified source-level review**.

| round | scope | result |
|---|---|---|
| Round 7 | 3 bugs (A/B/C) in chatgpt send flow | ✅ fixed + GPT VERIFIED |
| Round 8 P1 | `find_tab` strict-match (fail-closed) | ✅ fixed + GPT VERIFIED |
| Round 8 P2 | `ReplyStatus` state machine (DONE/TIMEOUT/STREAMING/BROWSER_DEAD) | ✅ fixed + GPT VERIFIED |
| Round 8 P3 | auto-attach cleanup before submit | ✅ fixed + GPT VERIFIED |
| Round 8 P4 | SQLite journal with UNKNOWN sticky | ✅ fixed + GPT VERIFIED |
| Round 9 | wait-design SOUND + 3 minor improvements (combined streaming signals, baseline isolation, TIMEOUT sub-reason log) | ✅ fixed + GPT SOUND |

GPT flagged 5 minor suggestions (UUID-based round_id, atomic claim via
conditional UPDATE, journal ordering, timeout centralization, etc.) —
documented in the project memory and tracked as post-ship follow-ups.

## Exit conditions (user-controlled — one hard exit)

| signal | source | behavior |
|---|---|---|
| you say 停 / 结束 / stop / done / 够了 | this conversation | immediate stop, no questions |

Anything else (long thinking time, many rounds, AI keeps suggesting,
backend fails, you typed in the AI page, AI says "task complete" or
`STATUS: COMPLETE`) **does NOT** exit the loop. The AI is a
consultant — only you decide when you're done. Round 12 removed the
AI-driven exit because GPT kept emitting the marker too readily,
breaking multi-round iteration. Failover and tab reset still handle
the other non-exit conditions.

## Limitations

- **Per-backend text-size cap** (refuses with `rc=11` if exceeded):
  | backend | max chars | ~tokens | override | empirical ceiling |
  |---|---|---|---|---|
  | `chatgpt` | 200,000 | ~64K | `GPT_CONSULT_MAX_INPUT_CHARS` | 200K OK / 400K REJECTED (v7) |
  | `deepseek` | 400,000 | ~128K | `GPT_CONSULT_MAX_INPUT_CHARS` | 400K OK, ceiling not yet probed |
  | `gemini` | 1,000,000 | ~250K | `GPT_CONSULT_MAX_INPUT_CHARS` | 1M OK, ceiling not yet probed |
  These are conservative web-UI safe limits derived from real-machine probes
  (Round 11 v7, this user — `D:/www/scratch/round11_chat_box_v7.md`). Web UIs
  truncate, paste-stall, or DOM-corrupt above these. Switch backends or
  split the request if you're hitting the cap.
- Single-process orchestrator. Concurrent `round.py` invocations on the
  same journal would need atomic claim (GPT minor suggestion #2).
- ChatGPT requires a ProseMirror-aware composer selector (`#prompt-textarea`).
  Other ProseMirror-based AIs may need their own selector map.
- Stream detection depends on backend-specific stop-button selectors
  (see `stream_selectors` in `backend_config.py`).

## License

MIT — see [LICENSE](LICENSE).