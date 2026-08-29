"""Shared backend config for gpt-consult skill.

Each entry tells the loop:
- how to reach the backend (url)
- how to find its input box (input_selectors, tried in order)
- how to find AI replies (reply_selectors)
- how to detect streaming-in-progress (stream_selectors)
- how to detect login state (login_selectors = visible = NOT logged in)
- how to start a fresh conversation (new_chat_selectors)
- display name for user-facing logs
- max_input_chars: pre-submit guard. Refuses to send (rc=11) when the
  message exceeds this limit. Conservative defaults derived from web-UI
  safe limits, not raw API limits (web UIs truncate, paste stalls, etc.).
  Override per-call with env var GPT_CONSULT_MAX_INPUT_CHARS.

Add a new backend by appending an entry. Loop uses CONSULT_BACKENDS env var
to pick the priority order, default = chatgpt → deepseek → gemini.

# Backend selector versions (N4 audit fix):
#   chatgpt: 2026-08-28 (chatgpt.com)  — re-verify after any UI update
#   deepseek: 2026-08-28 (chat.deepseek.com)
#   gemini:  2026-08-28 (gemini.google.com/app)
"""
from __future__ import annotations

import os
import re
import sys
from urllib.parse import urlparse

DEFAULT_ORDER = ['chatgpt', 'deepseek', 'gemini']


def get_order() -> list[str]:
    """Return backend priority list. Validates env var against BACKENDS."""
    env = os.environ.get('CONSULT_BACKENDS', '').strip()
    if env:
        order = [b.strip() for b in env.split(',') if b.strip()]
        bad = [b for b in order if b not in BACKENDS]
        if bad:
            sys.stderr.write(
                f'[backend_config] CONSULT_BACKENDS has unknown: {bad}. '
                f'Valid: {list(BACKENDS)}\n'
            )
            sys.exit(2)
        return order
    return list(DEFAULT_ORDER)


BACKENDS: dict[str, dict] = {
    'chatgpt': {
        'display': 'ChatGPT',
        'url': 'https://chatgpt.com',
        'input_selectors': [
            '#prompt-textarea',
            'textarea[name="prompt-textarea"]',
        ],
        'reply_selectors': [
            '[data-message-author-role="assistant"]',
            '[data-message-author-role="model"]',  # fallback
        ],
        'stream_selectors': [
            'button[aria-label="Stop generating"]',
            'button[data-testid="stop-button"]',
        ],
        'login_selectors': [
            'button:has-text("Log in")',
            'a:has-text("Log in")',
            'button:has-text("Sign in")',
        ],
        'new_chat_selectors': [
            'a[href="/"]',
            'button:has-text("New chat")',
            '[data-testid="new-chat-button"]',
            'nav a:first-child',
        ],
        # ChatGPT web UI: empirical ceiling ~200K chars (v7: 200K OK,
        # 400K REJECTED). Set guard conservatively below rejection.
        # Override per-call with env var GPT_CONSULT_MAX_INPUT_CHARS.
        'max_input_chars': 200_000,
    },
    'deepseek': {
        'display': 'DeepSeek',
        'url': 'https://chat.deepseek.com',
        'input_selectors': [
            # Round 13c: actual placeholder is '给 DeepSeek 发送消息 ' — bare
            # substring match ('输入' / 'Send') doesn't hit it. Use the
            # placeholder directly + bare textarea as fallback.
            'textarea[placeholder="给 DeepSeek 发送消息 "]',
            'textarea[placeholder*="DeepSeek"]',
            'textarea',
        ],
        'reply_selectors': [
            # Round 13b: deepseek uses class-based, not data-attribute.
            # div.ds-message wraps BOTH user and assistant. .ds-collapsible-text
            # is the user-content bubble — :not(:has(...)) excludes user msgs.
            #
            # Round 13c: do NOT promote `div.ds-markdown.ds-assistant-message-
            # main-content` as the primary selector. Reasoning: R1 expert mode
            # produces TWO reply "sources" at different times — the WHOLE
            # assistant div.ds-message is always present (it contains the
            # thinking summary + chain + final answer), while .ds-markdown
            # only appears after R1 finishes reasoning. Using them as primary
            # + fallback shifts the canonical reply text mid-round, so
            # last_text (set from fallback during thinking) and cur (from
            # primary after R1 finishes) never match → Signal 2 fires
            # forever → terminal block never runs.
            # Round 13c placeholder filter in find_real_reply_text skips
            # pure-placeholder text ("正在思考" / "深度思考" /
            # "已思考（用时 N 秒）") so this selector won't falsely return
            # a placeholder as a stable reply.
            'div.ds-message:not(:has(.ds-collapsible-text))',
            # Generic data-role fallbacks (kept for hypothetical other backends).
            '[data-role="assistant"]',
            '.message-assistant',
            'div[class*="assistant"]:not([class*="user"])',
        ],
        'stream_selectors': [
            'button:has-text("停止生成")',
            'button:has-text("Stop")',
            '[class*="stop"]',
        ],
        'login_selectors': [
            'button:has-text("登录")',
            'a:has-text("登录")',
            'button:has-text("Log in")',
        ],
        'new_chat_selectors': [
            'button:has-text("新对话")',
            'a:has-text("新对话")',
            '[aria-label*="new"]',
        ],
        # DeepSeek web UI: empirical floor ~400K chars (v6/v7: 50K, 100K,
        # 200K, 400K all OK; ceiling not yet probed). Conservative
        # default set to tested-max (400K) — raise once higher probe
        # is run. Web UI is tighter than the API (which advertises
        # 64K-128K).
        'max_input_chars': 400_000,
    },
    'gemini': {
        'display': 'Gemini',
        'url': 'https://gemini.google.com/app',
        'input_selectors': [
            'rich-textarea',
            '.ql-editor[contenteditable="true"]',
            'div[contenteditable="true"][aria-label*="Enter"]',
        ],
        'reply_selectors': [
            'message-content',
            'model-response',
            '[class*="model-response"]',
            '[data-message-role="model"]',
        ],
        'stream_selectors': [
            'button[aria-label="Stop"]',
            '[class*="stop-icon"]',
        ],
        # Tightened: don't match generic google.com account links.
        # Only match an actual sign-in CTA, which Gemini shows as a modal
        # overlay when NOT logged in.
        'login_selectors': [
            'button:has-text("Sign in to Gemini")',
            'a:has-text("Sign in to Gemini")',
            'div[role="dialog"] button:has-text("Sign in")',
            'button:has-text("登录 Gemini")',
        ],
        'new_chat_selectors': [
            'a[href="/app"]',
            'button[aria-label*="New chat"]',
        ],
        # Gemini web UI: empirical floor ~1M chars (v7: 50K, 200K, 500K,
        # 800K, 1M all OK; ceiling not yet probed). Conservative
        # default set to tested-max (1M). Gemini 2.5 Pro advertises
        # 1M tokens but the web UI clamps input well below that for
        # many requests.
        'max_input_chars': 1_000_000,
    },
}


def cfg(backend: str) -> dict:
    if backend not in BACKENDS:
        raise ValueError(f'unknown backend: {backend}; choices={list(BACKENDS)}')
    return BACKENDS[backend]


def host_of(backend: str) -> str:
    """Extract host (e.g. 'chatgpt.com') from backend URL. Returns '' on parse failure."""
    parsed = urlparse(cfg(backend)['url'])
    return parsed.hostname or ''


def page_host_matches(page, backend: str) -> bool:
    """True if `page`'s URL hostname strictly equals backend's host.

    Use this instead of `host_of(backend) in page.url` — substring matching
    falsely matches e.g. "gemini" inside other hosts.
    """
    target = host_of(backend)
    if not target:
        return False
    try:
        pg_host = urlparse(page.url).hostname or ''
    except Exception:
        return False
    return pg_host == target


def find_tab(ctx, backend: str, conv_url: str | None = None,
             force_new: bool = False):
    """Return the open tab for this backend, with conversation-aware disambiguation.

    Round 8 P1: refuse to pick the wrong tab when multiple tabs exist for the
    same backend. Resolves a target conversation_id from `conv_url` (explicit)
    or from the active-conversation file (fallback), then matches STRICTLY by
    conversation_id.

    Round 14: 0 exact matches fell through to discovery (reuse any open tab).
    Round 15: that fallthrough was TOO LOOSE — it could silently reuse a tab
    the user had just opened with their own draft. Now: only reuse when
    discovery returns exactly 1 tab AND its URL has no conversation_id
    (i.e. it's a blank root URL, not someone else's conversation). Otherwise
    fail-closed.

    Per deepseek R1 expert: "仅当 discovery 返回恰好一个 open tab，且该 tab
    是空白新对话或 URL 不含 conversation_id 时才复用；否则返回 None，保持
    fail-closed".

    Args:
        ctx: Playwright BrowserContext.
        backend: backend key from BACKENDS.
        conv_url: optional explicit conversation URL.
        force_new: bypass fallback entirely (always open a new tab).
            Used by reset_to_new_chat.py to guarantee a fresh conversation.

    Returns:
        Page or None. None means caller should open a new tab.
    """
    if force_new:
        return None
    host = host_of(backend)
    matches = []
    for pg in ctx.pages:
        if page_host_matches(pg, backend):
            matches.append(pg)

    # Resolve target conversation_id.
    target_id = None
    if conv_url:
        target_id = extract_conversation_id(backend, conv_url)
    else:
        from _helpers import read_active_url
        active = read_active_url(backend).strip()
        if active:
            target_id = extract_conversation_id(backend, active)

    if target_id is not None:
        # Strict match by conversation_id.
        exact = [
            pg for pg in matches
            if extract_conversation_id(backend, pg.url) == target_id
        ]
        if len(exact) == 1:
            return exact[0]
        # Round 15: 0 matches means the active conversation is gone. Do NOT
        # silently reuse any other open tab (could be the user's own).
        # Fail-closed — caller will open a fresh tab.
        return None

    # No target conversation_id -> discovery rules.
    if len(matches) == 0:
        return None
    if len(matches) == 1:
        # Round 15: only reuse this single tab if its URL has no
        # conversation_id (blank new-chat root). Otherwise it might be a
        # real conversation the user is mid-way through — leave alone.
        only = matches[0]
        if extract_conversation_id(backend, only.url) is None:
            return only
        return None
    # >=2 matches with no disambiguator -> ambiguity. Refuse to guess.
    sys.stderr.write(
        f'[find_tab] AMBIGUITY: {len(matches)} {backend} tabs open and no '
        f'active conversation URL stored. Pass conv_url explicitly or open '
        f'only one tab. Tabs: '
        f'{[pg.url for pg in matches]}\n'
    )
    sys.exit(9)


# Round 8 P1: extract a stable conversation identifier from a backend URL.
# Per-backend patterns — these match the URL structure that uniquely identifies
# a conversation thread (vs. the bare domain). Returning None means "no
# identifiable conversation" (e.g. fresh / root URL), which is treated as
# "no specific active conversation" by find_tab / write_active_url.
_CONVERSATION_ID_PATTERNS = {
    'chatgpt':  re.compile(r'/c/([0-9a-f-]{36})'),
    'gemini':   re.compile(r'/app/([0-9a-f]{16,})'),
    # deepseek typically does not embed conversation IDs in the URL; treat
    # any non-root path segment as the ID (best-effort).
    'deepseek': re.compile(r'/(chat/)?s/([0-9a-f-]{8,})'),
}


def extract_conversation_id(backend: str, url: str) -> str | None:
    """Return the conversation identifier embedded in `url`, or None.

    None means the URL is the bare domain / no specific conversation —
    callers should treat this as "no specific active conversation" rather
    than as a unique key.
    """
    if not url:
        return None
    pat = _CONVERSATION_ID_PATTERNS.get(backend)
    if pat is None:
        return None
    m = pat.search(url)
    if not m:
        return None
    # For deepseek the regex has an optional non-capturing group; the ID
    # is the last captured group.
    return m.group(m.lastindex)