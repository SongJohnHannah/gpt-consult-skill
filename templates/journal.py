"""Round 8 P4 — SQLite durable request_id journal.

Why this exists (per GPT design):
Without persistence, when `send_message.py` reaches hard_deadline we can't
tell apart:
  - "round finished but we missed seeing it" (UNKNOWN)
  - "round failed / never started" (FAILED)
  - "round is genuinely still streaming" (still SUBMITTING)
A retry that doesn't know this distinction will either drop a real reply
or re-send a message the AI already responded to.

State machine (GPT design M5):
    CREATED        — entry recorded before send
    SUBMITTING     — fill_input + verify_message_sent phase
    SENT_CONFIRMED — verify_message_sent saw the message in DOM
    UNKNOWN        — wait_for_reply hard_deadline hit but message was in
                     DOM (round may have completed; do NOT auto-resend)
    FAILED         — verify_message_sent returned False AND no message in
                     DOM (round never started; safe to retry)

Rule: UNKNOWN is NOT FAILED. round.py / send_message.py must check
recover_request() before retrying and refuse to retry UNKNOWN.

Schema (one row per round attempt):

    CREATE TABLE requests (
        request_id   TEXT PRIMARY KEY,    -- UUID4
        backend      TEXT NOT NULL,
        text_hash    TEXT NOT NULL,       -- sha256 of input text
        status       TEXT NOT NULL,       -- CREATED|SUBMITTING|SENT_CONFIRMED|UNKNOWN|FAILED
        created_at   REAL NOT NULL,       -- unix seconds
        updated_at   REAL NOT NULL,
        reply_excerpt TEXT,               -- first ~200 chars of reply if known
        error        TEXT                 -- short error tag if any
    )

The journal lives at `.gpt_consult/journal.sqlite3` — same folder as the
rest of the skill's runtime state. Failures inside the journal NEVER
block the round: we log and continue, so a broken DB cannot wedge the
skill (this is the "失败就抛错，不兜底" rule applied at the boundary:
the BOUNDARY is the call into the skill, not the journal itself).
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import sys
import time
import uuid


JOURNAL_PATH = os.path.join(
    os.environ.get('GPT_CONSULT_JOURNAL_DIR', '.gpt_consult'),
    'journal.sqlite3',
)

# Status values — exported as a frozen set so callers can validate.
STATUS_CREATED = 'CREATED'
STATUS_SUBMITTING = 'SUBMITTING'
STATUS_SENT_CONFIRMED = 'SENT_CONFIRMED'
STATUS_UNKNOWN = 'UNKNOWN'
STATUS_FAILED = 'FAILED'
ALL_STATUSES = frozenset({
    STATUS_CREATED, STATUS_SUBMITTING, STATUS_SENT_CONFIRMED,
    STATUS_UNKNOWN, STATUS_FAILED,
})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    request_id    TEXT PRIMARY KEY,
    backend       TEXT NOT NULL,
    text_hash     TEXT NOT NULL,
    status        TEXT NOT NULL,
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL,
    reply_excerpt TEXT,
    error         TEXT
);
CREATE INDEX IF NOT EXISTS idx_requests_backend ON requests(backend);
CREATE INDEX IF NOT EXISTS idx_requests_status ON requests(status);
CREATE INDEX IF NOT EXISTS idx_requests_text_hash ON requests(text_hash);
"""


def _connect() -> sqlite3.Connection:
    """Open (and create) the journal DB. Caller closes the connection."""
    os.makedirs(os.path.dirname(JOURNAL_PATH), exist_ok=True)
    conn = sqlite3.connect(JOURNAL_PATH, timeout=5.0)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA synchronous=NORMAL')
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


def text_hash(text: str) -> str:
    """Stable hash of the input text for idempotency lookup."""
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def new_request_id() -> str:
    return str(uuid.uuid4())


class RequestJournal:
    """Thin wrapper around the SQLite journal for one round attempt.

    Usage:
        j = RequestJournal('chatgpt', '<text>', '11111111-...')
        j.mark_submitting()
        if verify_message_sent(...):
            j.mark_sent_confirmed()
        elif message_in_dom:
            j.mark_unknown('verify timed out but msg landed')
        else:
            j.mark_failed('no message in DOM after fill_input')

    All methods are idempotent on the latest row: if you call mark_failed
    after mark_sent_confirmed, the row stays SENT_CONFIRMED — we never
    regress a confirmed state. UNKNOWN and FAILED only overwrite earlier
    CREATED / SUBMITTING states.
    """

    def __init__(self, backend: str, text: str, request_id: str | None = None):
        self.backend = backend
        self.text_hash = text_hash(text)
        self.request_id = request_id or new_request_id()
        self._conn = _connect()
        self._created_at = time.time()
        self._insert_row()

    def _insert_row(self) -> None:
        try:
            self._conn.execute(
                'INSERT OR IGNORE INTO requests '
                '(request_id, backend, text_hash, status, created_at, updated_at) '
                'VALUES (?, ?, ?, ?, ?, ?)',
                (self.request_id, self.backend, self.text_hash,
                 STATUS_CREATED, self._created_at, self._created_at),
            )
            self._conn.commit()
        except Exception as e:
            # Boundary rule: never block the round on a journal failure.
            sys.stderr.write(
                f'[journal] WARNING: insert row failed: {e}\n')

    def _set_status(self, new_status: str,
                    reply_excerpt: str | None = None,
                    error: str | None = None) -> None:
        if new_status not in ALL_STATUSES:
            raise ValueError(f'unknown status: {new_status}')
        try:
            # Don't regress a confirmed state.
            row = self._conn.execute(
                'SELECT status FROM requests WHERE request_id = ?',
                (self.request_id,),
            ).fetchone()
            if row is None:
                # Row never made it in (insert failed). Best-effort re-insert.
                self._insert_row()
            elif row[0] == STATUS_SENT_CONFIRMED:
                # Already confirmed — never regress.
                return
            elif row[0] == STATUS_UNKNOWN:
                # UNKNOWN is sticky: round state is ambiguous. A later
                # attempt must NOT silently flip to FAILED (which would
                # mark the round as safely retryable — but the AI may
                # have already responded). UNKNOWN stays UNKNOWN until
                # the user (or a future verified outcome) resolves it.
                # We DO update the error/excerpt/timestamp so the
                # operator can see the latest observation.
                self._conn.execute(
                    'UPDATE requests SET updated_at = ?, '
                    'error = COALESCE(?, error) '
                    'WHERE request_id = ?',
                    (time.time(), error, self.request_id),
                )
                self._conn.commit()
                return
            self._conn.execute(
                'UPDATE requests '
                'SET status = ?, updated_at = ?, '
                'reply_excerpt = COALESCE(?, reply_excerpt), '
                'error = COALESCE(?, error) '
                'WHERE request_id = ?',
                (new_status, time.time(), reply_excerpt, error,
                 self.request_id),
            )
            self._conn.commit()
        except Exception as e:
            sys.stderr.write(
                f'[journal] WARNING: set_status({new_status}) failed: {e}\n')

    def mark_submitting(self) -> None:
        """Round entered the fill_input / verify_message_sent phase."""
        self._set_status(STATUS_SUBMITTING)

    def mark_sent_confirmed(self, reply_excerpt: str | None = None) -> None:
        """verify_message_sent saw the user message in DOM AND wait_for_reply
        returned DONE with a non-empty reply. The round is complete."""
        self._set_status(STATUS_SENT_CONFIRMED, reply_excerpt=reply_excerpt)

    def mark_unknown(self, error: str | None = None,
                     reply_excerpt: str | None = None) -> None:
        """Round state is ambiguous (e.g. wait_for_reply hard_deadline hit
        but message was in DOM). Do NOT auto-retry — the AI may have already
        responded and a retry would re-send or duplicate."""
        self._set_status(STATUS_UNKNOWN, error=error,
                         reply_excerpt=reply_excerpt)

    def mark_failed(self, error: str | None = None) -> None:
        """Round definitely failed (verify returned False AND no msg in DOM).
        Safe to retry from scratch."""
        self._set_status(STATUS_FAILED, error=error)

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass


def recover_request(backend: str, text: str) -> dict | None:
    """Look up the most recent journal entry for this (backend, text_hash).

    Returns a dict with keys: request_id, status, created_at, updated_at,
    reply_excerpt, error. Returns None if no row matches.

    Callers (round.py, send_message.py on idempotent retry) should
    inspect `status` and refuse to retry if it is UNKNOWN or SENT_CONFIRMED.
    """
    h = text_hash(text)
    try:
        conn = _connect()
    except Exception as e:
        sys.stderr.write(f'[journal] WARNING: open for recover failed: {e}\n')
        return None
    try:
        row = conn.execute(
            'SELECT request_id, status, created_at, updated_at, '
            'reply_excerpt, error '
            'FROM requests '
            'WHERE backend = ? AND text_hash = ? '
            'ORDER BY created_at DESC LIMIT 1',
            (backend, h),
        ).fetchone()
    except Exception as e:
        sys.stderr.write(f'[journal] WARNING: recover query failed: {e}\n')
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass
    if row is None:
        return None
    return {
        'request_id': row[0],
        'status': row[1],
        'created_at': row[2],
        'updated_at': row[3],
        'reply_excerpt': row[4],
        'error': row[5],
    }


if __name__ == '__main__':
    # Tiny smoke test.
    j = RequestJournal('chatgpt', 'hello world')
    j.mark_submitting()
    j.mark_sent_confirmed('Hi there!')
    j.close()
    rec = recover_request('chatgpt', 'hello world')
    print('recovered:', rec, file=sys.stderr)
    assert rec is not None and rec['status'] == STATUS_SENT_CONFIRMED
    print('OK', file=sys.stderr)
