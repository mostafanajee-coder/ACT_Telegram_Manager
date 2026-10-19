"""Shared helpers for Opus 5.5 adversarial tests (dummy data only)."""

import asyncio
import hashlib
import json
import sqlite3
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

from act_manager import policy

from conftest import NOW, CHAT_ADS


class SendRecorder:
    """Fake Telegram send phase that records every invocation."""

    def __init__(self, result=42, exc=None, delay=0.0):
        self.calls = []
        self.result = result
        self.exc = exc
        self.delay = delay

    async def __call__(self, post, prepared=None):
        self.calls.append(dict(post))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.exc is not None:
            raise self.exc
        return self.result


def prep(
    paths,
    *,
    chat_id=CHAT_ADS,
    kind="ad",
    content="Dummy ad text",
    reply_to=None,
    schedule_at=None,
    now=NOW,
):
    cfg, db = paths
    return policy.prepare(
        chat_id=chat_id,
        kind=kind,
        content=content,
        reply_to=reply_to,
        schedule_at=schedule_at,
        config_path=cfg,
        db_path=db,
        now=now,
    )


def human_approve(paths, post):
    """Equivalent of the CLI path after a correct typed phrase."""
    fp = policy.compute_fingerprint(
        post["chat_id"], post["kind"], post["reply_to"], post["content"]
    )
    return policy.approve(post["id"], db_path=paths[1], expected_fingerprint=fp)


def dispatch(paths, draft_id, sender, *, preflight=None, now=NOW):
    cfg, db = paths
    return asyncio.run(
        policy.execute(draft_id, sender, preflight=preflight, config_path=cfg, db_path=db, now=now)
    )


def raw_db(paths):
    con = sqlite3.connect(paths[1], isolation_level=None)
    con.row_factory = sqlite3.Row
    return con


def status(paths, draft_id):
    return policy.get_post(draft_id, db_path=paths[1])["status"]


def unkeyed_integrity(draft_id, chat_id, kind, reply_to, content, schedule_utc):
    """Attacker-side recomputation: the 'integrity' hash needs no secret."""
    return hashlib.sha256(
        json.dumps(
            [draft_id, chat_id, kind, reply_to, content, schedule_utc],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def run_child(code: str, *args, timeout=60):
    """Run python code in a separate same-user process with stdin=/dev/null (no TTY)."""
    return subprocess.run(
        [sys.executable, "-c", code, *map(str, args)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def later(minutes=0, hours=0, days=0):
    return NOW + timedelta(minutes=minutes, hours=hours, days=days)
