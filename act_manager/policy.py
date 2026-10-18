"""ACT Telegram safety policy, local configuration, and SQLite audit store.

All policy rules are enforced in code before any Telegram network call:
- Deny-all until `act_config.json` contains explicit negative numeric chat IDs.
- Group destinations use negative numeric IDs from the allowlist. A `private_user`
  destination stores one fixed positive User ID per reply draft and is never an
  allowlist or standing authorization.
- Only two post kinds exist: `ad` and `reply`; private-user drafts are reply-only.
- Ads require `ads_allowed: true`, max 1..3 per Riyadh calendar day (+03:00),
  and >= 60 minutes spacing across all groups (sent, scheduled, sending, uncertain).
- Replies require `reply_to` > 0 and cannot be scheduled.
- Every post starts as `draft`, requires local CLI approval (`approved`),
  is atomically reserved (`sending`), and finalizes as `sent`, `scheduled`,
  or `uncertain` (which never auto-retries).
- Pre-send policy/validation refusals roll back `sending` -> `approved` so
  configuration or identity errors before `send_message()` do not corrupt state
  to `uncertain` or exhaust daily advertisement quota.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
from typing import Any, Awaitable, Callable
import unicodedata

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "act_config.json"
DATA_PATH = ROOT / "act_data" / "posts.sqlite3"
RIYADH = timezone(timedelta(hours=3))
UTC = timezone.utc
MAX_TEXT_LENGTH = 4096
MAX_FUTURE_DAYS = 7
MIN_AD_SPACING = timedelta(minutes=60)
MAX_APPROVAL_AGE = timedelta(hours=24)

# Control chars (excluding \n and \t in message bodies) and Unicode bidi/spoofing controls
_FORBIDDEN_CONTROL_RE = re.compile(
    r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f"
    r"\u200e\u200f\u202a-\u202e\u2066-\u2069\u061c]"
)
_ZERO_WIDTH_RE = re.compile(r"[\u200b-\u200d\ufeff\u2060]")


class PolicyError(ValueError):
    """A refused operation has not contacted Telegram."""


def utcnow() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def _dt(value: str) -> datetime:
    if not isinstance(value, str):
        raise TypeError("Timestamp must be an ISO-8601 string.")
    if value.endswith(("Z", "z")):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value)


def sanitize_terminal_text(value: str) -> str:
    """Strip terminal escape sequences, CR, and bidi overrides for safe CLI display."""
    if not isinstance(value, str):
        return ""
    cleaned = value.replace("\r\n", "\n").replace("\r", "")
    return _FORBIDDEN_CONTROL_RE.sub("\ufffd", cleaned)


def _validate_safe_text(value: str, *, field_name: str = "Content", allow_newlines: bool = True) -> None:
    if not isinstance(value, str):
        raise PolicyError(f"{field_name} must be a string.")
    if "\r" in value:
        raise PolicyError(f"{field_name} must not contain carriage-return (CR) control characters.")
    if not allow_newlines and ("\n" in value or "\t" in value):
        raise PolicyError(f"{field_name} must be a single line without tabs or newlines.")
    if _FORBIDDEN_CONTROL_RE.search(value):
        raise PolicyError(
            f"{field_name} contains forbidden terminal control or bidirectional override characters."
        )


def normalize_content_for_dedup(content: str) -> str:
    """Normalize text for duplicate detection (NFKC, strip zero-width, collapse whitespace, casefold)."""
    norm = unicodedata.normalize("NFKC", content)
    norm = _ZERO_WIDTH_RE.sub("", norm)
    norm = " ".join(norm.split())
    return norm.casefold()


def compute_fingerprint(
    chat_id: int | None,
    kind: str,
    reply_to: int | None,
    content: str,
    *,
    destination_type: str = "group",
    recipient_user_id: int | None = None,
) -> str:
    """Compute a normalized duplicate key bound to the destination."""
    norm_content = normalize_content_for_dedup(content)
    if destination_type == "group":
        if recipient_user_id is not None:
            raise PolicyError("Group drafts cannot contain a private recipient.")
        binding = [chat_id, kind, reply_to, norm_content]
    elif destination_type == "private_user":
        recipient_user_id = _strict_user_id(recipient_user_id)
        binding = ["private_user", recipient_user_id, kind, reply_to, norm_content]
    else:
        raise PolicyError("Unknown destination type.")
    return hashlib.sha256(
        json.dumps(binding, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def compute_integrity_hash(
    draft_id: str,
    chat_id: int | None,
    kind: str,
    reply_to: int | None,
    content: str,
    schedule_utc: str | None,
    *,
    destination_type: str = "group",
    recipient_user_id: int | None = None,
) -> str:
    """Compute a strict hash over immutable fields, including private recipients."""
    if destination_type == "group":
        if recipient_user_id is not None:
            raise PolicyError("Group drafts cannot contain a private recipient.")
        binding = [draft_id, chat_id, kind, reply_to, content, schedule_utc]
    elif destination_type == "private_user":
        recipient_user_id = _strict_user_id(recipient_user_id)
        binding = [draft_id, "private_user", recipient_user_id, kind, reply_to, content, schedule_utc]
    else:
        raise PolicyError("Unknown destination type.")
    return hashlib.sha256(
        json.dumps(
            binding,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def parse_time(value: str | None, *, now: datetime | None = None) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise PolicyError("Use a full ISO date/time with timezone, e.g. 2026-10-19T11:00:00+03:00")
    if not value.strip():
        return None
    try:
        parsed = _dt(value.strip())
    except (TypeError, ValueError) as exc:
        raise PolicyError("Use a full ISO date/time with timezone, e.g. 2026-10-19T11:00:00+03:00") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PolicyError("Timezone is mandatory (+03:00 for Riyadh).")
    now = now or utcnow()
    if not (now + timedelta(seconds=90) <= parsed <= now + timedelta(days=MAX_FUTURE_DAYS)):
        raise PolicyError("Scheduled time must be 90 seconds to 7 days in the future.")
    return parsed.astimezone(UTC)


def _strict_chat_id(value: Any) -> int:
    if isinstance(value, bool) or type(value) is not int or value >= 0:
        raise PolicyError("Chat ID must be a negative integer, not a name or link.")
    return value


def _strict_user_id(value: Any) -> int:
    if isinstance(value, bool) or type(value) is not int or not 1 <= value <= (2**63 - 1):
        raise PolicyError("Private recipient must be a positive Telegram User ID.")
    return value


def config_example() -> dict:
    return {
        "expected_user_id": 0,
        "daily_ad_limit": 3,
        "allowed_chats": [],
    }


_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def _is_link_or_reparse(path: Path) -> bool:
    """True for symlinks and, on Windows, any reparse point (e.g. NTFS junctions)."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    if path.is_symlink():
        return True
    return bool(getattr(st, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT)


def _resolve_config_path(path: Path | None) -> Path:
    resolved = Path(path) if path is not None else CONFIG_PATH
    if _is_link_or_reparse(resolved):
        raise PolicyError("Configuration path must not be a symbolic link or reparse point.")
    return resolved


def _resolve_db_path(path: Path | None) -> Path:
    resolved = Path(path) if path is not None else DATA_PATH
    if _is_link_or_reparse(resolved) or _is_link_or_reparse(resolved.parent):
        raise PolicyError("Database path must not be a symbolic link or reparse point.")
    return resolved


def load_config(path: Path | None = None, *, require_identity: bool = False) -> dict:
    cfg_path = _resolve_config_path(path)
    if not cfg_path.is_file():
        raise PolicyError(f"Configuration missing: {cfg_path}. Use: python act_cli.py init")
    try:
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PolicyError("Invalid config JSON.") from exc
    if not isinstance(data, dict):
        raise PolicyError("Config must be an object.")
    limit = data.get("daily_ad_limit")
    if type(limit) is not int or limit < 1 or limit > 3:
        raise PolicyError("daily_ad_limit must be an integer from 1 to 3.")
    identity = data.get("expected_user_id")
    if type(identity) is not int or identity < 0:
        raise PolicyError("expected_user_id must be a non-negative integer.")
    if require_identity and identity <= 0:
        raise PolicyError("Set expected_user_id before dispatch; no messages sent.")
    chats = data.get("allowed_chats")
    if not isinstance(chats, list) or len(chats) > 100:
        raise PolicyError("allowed_chats must be a list (maximum 100).")
    seen = set()
    for c in chats:
        if not isinstance(c, dict):
            raise PolicyError("Invalid chat entry.")
        chat_id = _strict_chat_id(c.get("chat_id"))
        if chat_id in seen:
            raise PolicyError("Duplicate allowed chat ID.")
        seen.add(chat_id)
        label = c.get("label")
        if not isinstance(label, str) or not label.strip() or len(label) > 200:
            raise PolicyError("Every allowed chat needs a non-empty label (max 200 chars).")
        _validate_safe_text(label, field_name="Chat label", allow_newlines=False)
        if type(c.get("ads_allowed")) is not bool:
            raise PolicyError("ads_allowed must be true or false.")
    open_limit = data.get("max_open_drafts")
    if open_limit is not None and (type(open_limit) is not int or not 1 <= open_limit <= 10000):
        raise PolicyError("max_open_drafts, when set, must be an integer from 1 to 10000.")
    return data


def find_chat(config: dict, chat_id: int, kind: str) -> dict:
    _strict_chat_id(chat_id)
    for c in config["allowed_chats"]:
        if c["chat_id"] == chat_id:
            if kind == "ad" and not c["ads_allowed"]:
                raise PolicyError("Advertising not authorized for this group.")
            return c
    raise PolicyError("Chat ID is not in the explicitly authorized allowlist.")


@contextmanager
def _db(path: Path | None = None):
    db_path = _resolve_db_path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    first = not db_path.exists()
    db = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=30000")
    db.execute("PRAGMA journal_mode=WAL")
    if first and os.name != "nt":
        os.chmod(db_path, 0o600)
    db.execute("""CREATE TABLE IF NOT EXISTS posts (
        id TEXT PRIMARY KEY,
        chat_id INTEGER NOT NULL,
        destination_type TEXT NOT NULL DEFAULT 'group',
        recipient_user_id INTEGER,
        kind TEXT NOT NULL,
        reply_to INTEGER,
        content TEXT NOT NULL,
        fingerprint TEXT NOT NULL,
        integrity_hash TEXT,
        schedule_utc TEXT,
        planned_utc TEXT,
        status TEXT NOT NULL,
        created_utc TEXT NOT NULL,
        approved_utc TEXT,
        sent_utc TEXT,
        external_id INTEGER,
        error TEXT
    )""")
    cols = {r["name"] for r in db.execute("PRAGMA table_info(posts)").fetchall()}
    if "integrity_hash" not in cols:
        db.execute("ALTER TABLE posts ADD COLUMN integrity_hash TEXT")
    if "destination_type" not in cols:
        db.execute("ALTER TABLE posts ADD COLUMN destination_type TEXT NOT NULL DEFAULT 'group'")
    if "recipient_user_id" not in cols:
        db.execute("ALTER TABLE posts ADD COLUMN recipient_user_id INTEGER")
    db.execute("CREATE INDEX IF NOT EXISTS posts_time_idx ON posts(kind,status,planned_utc)")
    db.execute("CREATE INDEX IF NOT EXISTS posts_fingerprint_idx ON posts(fingerprint,created_utc)")
    # This schema never defines triggers or views. One found here was planted
    # outside policy and could rewrite rows after verification; refuse to use it.
    unexpected = db.execute(
        "SELECT type, name FROM sqlite_master WHERE type IN ('trigger','view')"
    ).fetchall()
    if unexpected:
        db.close()
        raise PolicyError(
            "ACT database contains unexpected triggers or views; refusing to use it."
        )
    try:
        yield db
    finally:
        db.close()


def _verify_row_integrity(row: sqlite3.Row | dict) -> None:
    """Verify stored row fields match both deduplication fingerprint and integrity hash."""
    r = dict(row)
    destination_type = r.get("destination_type", "group")
    recipient_user_id = r.get("recipient_user_id")
    if destination_type == "group":
        _strict_chat_id(r["chat_id"])
        if recipient_user_id is not None:
            raise PolicyError("Group draft contains a private recipient.")
    elif destination_type == "private_user":
        if type(r.get("chat_id")) is not int or r["chat_id"] != 0:
            raise PolicyError("Private draft has an invalid group destination sentinel.")
        _strict_user_id(recipient_user_id)
        if r.get("kind") != "reply" or type(r.get("reply_to")) is not int or r["reply_to"] <= 0:
            raise PolicyError("Private messages must reply to one specified incoming message.")
        if r.get("schedule_utc") is not None:
            raise PolicyError("Private replies cannot be scheduled.")
    else:
        raise PolicyError("Draft has an unknown destination type.")
    expected_fp = compute_fingerprint(
        r["chat_id"], r["kind"], r["reply_to"], r["content"],
        destination_type=destination_type, recipient_user_id=recipient_user_id,
    )
    if r.get("fingerprint") != expected_fp:
        raise PolicyError("Draft integrity check failed: post record was modified outside policy.")
    stored_ih = r.get("integrity_hash")
    if not stored_ih:
        # A missing hash must not mean "skip the check": that would let any edit
        # through. Legacy drafts without a hash have to be recreated.
        raise PolicyError(
            "Draft integrity check failed: integrity hash missing; create a new draft."
        )
    expected_ih = compute_integrity_hash(
        r["id"], r["chat_id"], r["kind"], r["reply_to"], r["content"], r.get("schedule_utc"),
        destination_type=destination_type, recipient_user_id=recipient_user_id,
    )
    if stored_ih != expected_ih:
        raise PolicyError("Draft integrity check failed: post fields or schedule were tampered with.")


def _preview(row: sqlite3.Row | dict) -> dict:
    r = dict(row)
    preview = {k: r.get(k) for k in (
        "id", "chat_id", "destination_type", "recipient_user_id", "kind", "reply_to", "content", "schedule_utc",
        "status", "created_utc", "approved_utc", "sent_utc", "external_id", "error"
    )}
    if "destination_type" not in r:
        preview["destination_type"] = "group"
    if preview.get("destination_type") == "private_user":
        preview["chat_id"] = None
    return preview


def prepare(*, chat_id: int | None, kind: str, content: str, reply_to: int | None = None,
            schedule_at: str | None = None, config_path: Path | None = None,
            db_path: Path | None = None, now: datetime | None = None,
            destination_type: str = "group", recipient_user_id: int | None = None) -> dict:
    if destination_type not in ("group", "private_user"):
        raise PolicyError("destination_type must be 'group' or 'private_user'.")
    config = load_config(config_path, require_identity=(destination_type == "private_user"))
    if kind not in ("ad", "reply"):
        raise PolicyError("kind must be 'ad' or 'reply'.")
    if destination_type == "group":
        if chat_id is None:
            raise PolicyError("Group destinations require a negative numeric chat_id.")
        _strict_chat_id(chat_id)
        if recipient_user_id is not None:
            raise PolicyError("Group drafts cannot include recipient_user_id.")
        find_chat(config, chat_id, kind)
        stored_chat_id = chat_id
    else:
        if chat_id is not None:
            raise PolicyError("private_user drafts must not use chat_id.")
        if kind != "reply":
            raise PolicyError("private_user destinations support replies only.")
        if schedule_at is not None:
            raise PolicyError("Private replies cannot be scheduled.")
        if type(reply_to) is not int or reply_to <= 0:
            raise PolicyError("Private replies require one specific incoming message ID.")
        recipient_user_id = _strict_user_id(recipient_user_id)
        stored_chat_id = 0
    if not isinstance(content, str) or not content.strip() or len(content) > MAX_TEXT_LENGTH:
        raise PolicyError(f"Content must contain 1..{MAX_TEXT_LENGTH} characters.")
    _validate_safe_text(content, field_name="Content", allow_newlines=True)
    if not normalize_content_for_dedup(content):
        raise PolicyError("Content must contain visible characters.")
    if kind == "reply":
        if type(reply_to) is not int or reply_to <= 0:
            raise PolicyError("Replies require a positive original message ID.")
        if schedule_at:
            raise PolicyError("Reply scheduling disabled in this safe edition.")
    elif reply_to is not None:
        raise PolicyError("Advertisements cannot be replies in this edition.")
    now = now or utcnow()
    schedule = parse_time(schedule_at, now=now)
    planned = schedule or now
    schedule_iso = _iso(schedule) if schedule else None
    fingerprint = compute_fingerprint(
        stored_chat_id, kind, reply_to, content,
        destination_type=destination_type, recipient_user_id=recipient_user_id,
    )
    draft_id = secrets.token_hex(8)
    integrity_hash = compute_integrity_hash(
        draft_id, stored_chat_id, kind, reply_to, content, schedule_iso,
        destination_type=destination_type, recipient_user_id=recipient_user_id,
    )
    open_limit = config.get("max_open_drafts")
    with _db(db_path) as db:
        db.execute("BEGIN IMMEDIATE")
        if open_limit is not None:
            open_count = db.execute(
                "SELECT COUNT(*) FROM posts WHERE status IN ('draft','approved')"
            ).fetchone()[0]
            if open_count >= open_limit:
                raise PolicyError(
                    "Too many open drafts (max_open_drafts reached); the operator must approve "
                    "or cancel existing drafts first."
                )
        db.execute(
            """INSERT INTO posts (id,chat_id,destination_type,recipient_user_id,kind,reply_to,content,
               fingerprint,integrity_hash,schedule_utc,planned_utc,status,created_utc)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (draft_id, stored_chat_id, destination_type, recipient_user_id, kind, reply_to,
             content, fingerprint, integrity_hash, schedule_iso, _iso(planned), "draft", _iso(now))
        )
        db.commit()
    return get_post(draft_id, db_path=db_path)


def get_post(draft_id: str, *, db_path: Path | None = None) -> dict:
    with _db(db_path) as db:
        row = db.execute("SELECT * FROM posts WHERE id=?", (draft_id,)).fetchone()
    if row is None:
        raise PolicyError("Draft not found.")
    return _preview(row)


def list_posts(db_path: Path | None = None, limit: int = 20) -> list[dict]:
    if type(limit) is not int or not 1 <= limit <= 50:
        raise PolicyError("limit must be 1..50")
    with _db(db_path) as db:
        rows = db.execute("SELECT * FROM posts ORDER BY created_utc DESC LIMIT ?", (limit,)).fetchall()
    return [_preview(r) for r in rows]


def approve(draft_id: str, *, db_path: Path | None = None,
            now: datetime | None = None, expected_fingerprint: str | None = None,
            expected_integrity: str | None = None) -> dict:
    effective_now = now or utcnow()
    with _db(db_path) as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM posts WHERE id=?", (draft_id,)).fetchone()
        if row is None or row["status"] != "draft":
            raise PolicyError("Only an unapproved draft can be approved.")
        _verify_row_integrity(row)
        if expected_fingerprint is not None and row["fingerprint"] != expected_fingerprint:
            raise PolicyError("Draft content changed since preview; approval aborted.")
        if expected_integrity is not None and row["integrity_hash"] != expected_integrity:
            raise PolicyError(
                "Draft destination, content or schedule changed since preview; approval aborted."
            )
        if now is not None and row["schedule_utc"]:
            planned = _dt(row["schedule_utc"])
            if planned < now + timedelta(seconds=75):
                raise PolicyError("Scheduled time is too near/past; create a new draft.")
        db.execute("UPDATE posts SET status='approved',approved_utc=? WHERE id=?",
                   (_iso(effective_now), draft_id))
        db.commit()
    return get_post(draft_id, db_path=db_path)


def cancel(draft_id: str, *, db_path: Path | None = None) -> dict:
    with _db(db_path) as db:
        db.execute("BEGIN IMMEDIATE")
        changed = db.execute(
            "UPDATE posts SET status='cancelled',error='Cancelled by operator' WHERE id=? AND status IN ('draft','approved')",
            (draft_id,)
        )
        if changed.rowcount != 1:
            raise PolicyError("Only a pending draft can be cancelled. Scheduled messages must be cancelled inside Telegram.")
        db.commit()
    return get_post(draft_id, db_path=db_path)


def _check_quota(db: sqlite3.Connection, row: sqlite3.Row, config: dict,
                 now: datetime) -> datetime:
    planned = _dt(row["schedule_utc"]) if row["schedule_utc"] else now
    # A scheduled post cannot be sent to Telegram after its target time.
    if row["schedule_utc"] and planned < now + timedelta(seconds=75):
        raise PolicyError("Scheduled time is too near/past; create a new draft.")
    if row["kind"] != "ad":
        return planned
    day = planned.astimezone(RIYADH).date()
    reservations = db.execute(
        """SELECT id,planned_utc FROM posts
           WHERE kind='ad' AND status IN ('sending','sent','scheduled','uncertain')"""
    ).fetchall()
    same_day = 0
    for item in reservations:
        item_time = _dt(item["planned_utc"])
        if item_time.astimezone(RIYADH).date() == day:
            same_day += 1
        if abs(item_time - planned) < MIN_AD_SPACING:
            raise PolicyError("Advertisements must be at least 60 minutes apart across all chats.")
    if same_day >= config["daily_ad_limit"]:
        raise PolicyError("Daily advertisement quota exhausted (Riyadh +03:00).")
    return planned


def reserve(draft_id: str, *, config_path: Path | None = None,
            db_path: Path | None = None, now: datetime | None = None) -> dict:
    config = load_config(config_path, require_identity=True)
    now = now or utcnow()
    with _db(db_path) as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM posts WHERE id=?", (draft_id,)).fetchone()
        if row is None or row["status"] != "approved":
            raise PolicyError("Draft must have separate local approval and be unused.")
        _verify_row_integrity(row)
        _validate_safe_text(row["content"], field_name="Content", allow_newlines=True)
        if not row["approved_utc"]:
            raise PolicyError("Draft is missing approval timestamp.")
        if not row["schedule_utc"]:
            approved_dt = _dt(row["approved_utc"])
            if now - approved_dt > MAX_APPROVAL_AGE:
                raise PolicyError("Approval expired (>24h old); create and approve a new draft.")
        destination_type = row["destination_type"]
        if destination_type == "group":
            if row["recipient_user_id"] is not None:
                raise PolicyError("Group draft contains a private recipient.")
            find_chat(config, row["chat_id"], row["kind"])
        elif destination_type == "private_user":
            # Private recipients are explicit per-draft targets, never members of
            # the persistent group allowlist. _verify_row_integrity validates the
            # positive User ID and the reply-only/no-schedule shape.
            if row["chat_id"] != 0:
                raise PolicyError("Private draft has an invalid destination sentinel.")
        else:
            raise PolicyError("Draft has an unknown destination type.")
        window_start = _iso(now - timedelta(hours=24))
        other = db.execute(
            """SELECT id FROM posts WHERE fingerprint=? AND id<>? AND
               status IN ('sending','sent','scheduled','uncertain') AND
               (created_utc>=? OR planned_utc>=? OR (sent_utc IS NOT NULL AND sent_utc>=?))
               LIMIT 1""",
            (row["fingerprint"], row["id"], window_start, window_start, window_start)
        ).fetchone()
        if other:
            raise PolicyError("Potential duplicate for this destination within 24h. Create new content later.")
        planned = _check_quota(db, row, config, now)
        db.execute("UPDATE posts SET status='sending',planned_utc=?,error=NULL WHERE id=?",
                   (_iso(planned), draft_id))
        # Hand the sender exactly the row that was verified, read back inside this
        # same transaction, instead of a later read on a new connection.
        after = db.execute("SELECT * FROM posts WHERE id=?", (draft_id,)).fetchone()
        immutable = ("id", "chat_id", "destination_type", "recipient_user_id", "kind",
                     "reply_to", "content", "fingerprint", "integrity_hash", "schedule_utc")
        if after is None or after["status"] != "sending" or any(
            after[k] != row[k] for k in immutable
        ):
            raise PolicyError("Draft changed during reservation; no message attempted.")
        _verify_row_integrity(after)
        db.commit()
    return _preview(after)


def unreserve(draft_id: str, *, error: str | None = None, db_path: Path | None = None) -> dict:
    """Roll back a 'sending' reservation to 'approved' when a pre-send check fails before Telegram send."""
    with _db(db_path) as db:
        db.execute("BEGIN IMMEDIATE")
        changed = db.execute(
            """UPDATE posts SET status='approved',error=?
               WHERE id=? AND status='sending'""",
            ((error or "")[:200] if error else None, draft_id)
        )
        if changed.rowcount != 1:
            raise PolicyError("Cannot unreserve a post that is not in sending state.")
        db.commit()
    return get_post(draft_id, db_path=db_path)


def finalize(draft_id: str, *, status: str, external_id: int | None = None,
             error: str | None = None, db_path: Path | None = None) -> dict:
    if status not in ("sent", "scheduled", "uncertain"):
        raise PolicyError("Invalid final status.")
    with _db(db_path) as db:
        db.execute("BEGIN IMMEDIATE")
        changed = db.execute(
            """UPDATE posts SET status=?,external_id=?,sent_utc=?,error=?
               WHERE id=? AND status='sending'""",
            (status, external_id, _iso(utcnow()),
             (error or "")[:200] if status == "uncertain" else None, draft_id)
        )
        if changed.rowcount != 1:
            raise PolicyError("Cannot finalize a post that is not in sending state.")
        db.commit()
    return get_post(draft_id, db_path=db_path)


async def execute(
    draft_id: str,
    sender: Callable[..., Awaitable[int | None]],
    *,
    preflight: Callable[[dict], Awaitable[Any]] | None = None,
    config_path: Path | None = None,
    db_path: Path | None = None,
    now: datetime | None = None,
) -> dict:
    """Reserve atomically, complete preflight, send at most once, then record.

    `preflight`, when provided, must finish every check that cannot transmit a
    message. Errors or cancellation in that phase roll the reservation back to
    `approved`. Once `sender` is invoked, every exception or cancellation is
    ambiguous and the post becomes `uncertain`; it is never automatically
    retried, even when the exception is a `PolicyError`.

    Omitting `preflight` preserves the one-callback test/API form: that callback
    is treated as the send phase and any failure is therefore non-retryable.
    """
    reserved = reserve(draft_id, config_path=config_path, db_path=db_path, now=now)
    prepared: Any = None
    if preflight is not None:
        try:
            prepared = await preflight(reserved)
        except BaseException as exc:
            rollback_error = str(exc) if isinstance(exc, PolicyError) else type(exc).__name__
            unreserve(draft_id, error=rollback_error, db_path=db_path)
            if isinstance(exc, PolicyError):
                raise
            if isinstance(exc, Exception):
                raise PolicyError(
                    f"Pre-send validation failed ({type(exc).__name__}); no message attempted."
                ) from exc
            # In particular, propagate asyncio.CancelledError after restoring
            # state so the MCP host still observes its cancellation/timeout.
            raise

    try:
        msg_id = await sender(reserved, prepared) if preflight is not None else await sender(reserved)
    except BaseException as exc:
        finalize(draft_id, status="uncertain", error=type(exc).__name__, db_path=db_path)
        raise PolicyError("Send outcome uncertain; do NOT retry. Check Telegram and local audit.") from exc
    try:
        return finalize(
            draft_id,
            status="scheduled" if reserved["schedule_utc"] else "sent",
            external_id=msg_id,
            db_path=db_path,
        )
    except Exception as exc:
        # Telegram accepted the message but the audit row stays 'sending'. Say so
        # explicitly so the caller does not treat a generic error as "not sent".
        raise PolicyError(
            f"Telegram accepted the message but the local audit update failed "
            f"({type(exc).__name__}); do NOT retry. Check Telegram and local audit."
        ) from exc
