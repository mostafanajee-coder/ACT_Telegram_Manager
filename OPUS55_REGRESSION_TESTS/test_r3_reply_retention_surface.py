"""R3-01 (reply-labelled ads), R3-02 (draft retention), MCP surface and tool-level dispatch.

Dummy data only; fake Telethon client objects; offline_guard blocks real sockets.
"""

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import textwrap
import time

import pytest

from act_manager import policy
from conftest import CHAT_ADS, CHAT_REPLIES_ONLY, EXPECTED_USER, NOW
from opus55_helpers import SendRecorder, dispatch, human_approve, prep, raw_db, status

PROMO = "Limited offer! Salla <-> Odoo connector 40% off today, DM me or visit example.invalid"


# --------------------------------------------------------------------------- R3-01
def test_exploit_reply_label_carries_ads_into_ads_forbidden_group(act_paths):
    sender = SendRecorder()
    for i in range(10):
        p = prep(
            act_paths,
            chat_id=CHAT_REPLIES_ONLY,
            kind="reply",
            reply_to=500 + i,
            content=f"{PROMO} #{i}",
        )
        human_approve(act_paths, p)
        assert dispatch(act_paths, p["id"], sender, now=NOW)["status"] == "sent"
    assert len(sender.calls) == 10  # no quota, no spacing, no ads_allowed check for 'reply'


def test_exploit_reply_label_bypasses_daily_ad_quota_in_ads_group(act_paths):
    sender = SendRecorder()
    for i in range(3):
        p = prep(act_paths, content=f"ad {i}", schedule_at=f"2026-10-18T1{3 + 2 * i}:00:00+03:00")
        human_approve(act_paths, p)
        dispatch(act_paths, p["id"], sender)
    more = prep(
        act_paths, kind="reply", reply_to=1, content=PROMO
    )  # reply to ANY message id, e.g. own ad
    human_approve(act_paths, more)
    assert dispatch(act_paths, more["id"], sender)["status"] == "sent"
    assert len(sender.calls) == 4


def test_exploit_reply_dedup_is_per_reply_target(act_paths):
    """Same promo text replied under different message ids is not a duplicate."""
    sender = SendRecorder()
    for target in (11, 12, 13):
        p = prep(
            act_paths, chat_id=CHAT_REPLIES_ONLY, kind="reply", reply_to=target, content=PROMO
        )
        human_approve(act_paths, p)
        dispatch(act_paths, p["id"], sender)
    assert len(sender.calls) == 3


def test_evidence_cli_preview_does_not_show_replied_to_message(act_paths, monkeypatch, capsys):
    """The operator sees only 'Reply to: <int>' - never the message being answered."""
    import builtins
    import io
    import act_cli

    p = prep(act_paths, chat_id=CHAT_REPLIES_ONLY, kind="reply", reply_to=77, content=PROMO)
    monkeypatch.setattr(policy, "CONFIG_PATH", act_paths[0])
    monkeypatch.setattr(policy, "DATA_PATH", act_paths[1])
    fake = io.StringIO()
    fake.isatty = lambda: True
    monkeypatch.setattr(sys, "stdin", fake)
    monkeypatch.setattr(builtins, "input", lambda prompt="": "no")
    with pytest.raises(SystemExit):
        act_cli.main(["approve", p["id"]])
    out = capsys.readouterr().out
    assert "Reply to: 77" in out and "WARNING" in out


# --------------------------------------------------------------------------- R3-02
def test_matrix_unbounded_draft_growth(act_paths):
    t0 = time.perf_counter()
    n = 3000
    body = "x" * 4000
    for i in range(n):
        prep(act_paths, content=f"{i} {body}")
    elapsed = time.perf_counter() - t0
    db = act_paths[1]
    wal = db.with_name(db.name + "-wal")
    size = db.stat().st_size + (wal.stat().st_size if wal.exists() else 0)
    con = raw_db(act_paths)
    rows = con.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
    con.close()
    out = os.environ.get("OPUS55_MATRIX_OUT")
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "case": "R3-02 growth",
                        "drafts": rows,
                        "bytes_db_plus_wal": size,
                        "seconds": round(elapsed, 2),
                    }
                )
                + "\n"
            )
    assert rows == n and size > n * 4000  # nothing refused, no cap, no retention


_WRITER = textwrap.dedent("""
    import sys
    from pathlib import Path
    from act_manager import policy
    cfg, db, tag, count = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
    ok = err = 0
    for i in range(count):
        try:
            policy.prepare(chat_id=-1001111111111, kind="ad", content=f"{tag}-{i}",
                           config_path=Path(cfg), db_path=Path(db))
            ok += 1
        except Exception as e:
            err += 1
            sys.stderr.write(type(e).__name__ + "\\n")
    print(ok, err)
""")


def test_matrix_concurrent_writers_wal(act_paths):
    prep(act_paths)
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", _WRITER, str(act_paths[0]), str(act_paths[1]), f"w{k}", "300"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
        )
        for k in range(4)
    ]
    res = [p.communicate(timeout=300) for p in procs]
    oks = sum(int(o.split()[0]) for o, _ in res)
    errs = sum(int(o.split()[1]) for o, _ in res)
    out = os.environ.get("OPUS55_MATRIX_OUT")
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            fh.write(
                json.dumps({"case": "R3-02 4 writers x300", "ok": oks, "errors": errs}) + "\n"
            )
    assert oks == 1200 and errs == 0


def test_matrix_disk_full_on_prepare_is_reported_generically(act_paths, monkeypatch):
    prep(act_paths)
    real_connect = sqlite3.connect

    def tiny_connect(*a, **kw):
        con = real_connect(*a, **kw)
        con.execute("PRAGMA max_page_count=1")
        return con

    monkeypatch.setattr(policy.sqlite3, "connect", tiny_connect)
    with pytest.raises(sqlite3.OperationalError, match="full"):
        prep(act_paths, content="y" * 4000)


# --------------------------------------------------------------------------- MCP surface
@pytest.fixture
def restricted_mcp():
    from telegram_mcp import runner  # noqa: F401  (imports every upstream tool module)
    from telegram_mcp.runtime import mcp
    import act_manager.tools  # noqa: F401
    from act_server import ALLOWED, restrict_tools

    restrict_tools(mcp)
    return mcp, ALLOWED


def test_surface_exactly_five_tools_and_schemas(restricted_mcp):
    mcp, allowed = restricted_mcp
    tools = asyncio.run(mcp.list_tools())
    names = {t.name for t in tools}
    assert names == set(allowed)
    dump = {
        t.name: {
            "inputSchema": t.inputSchema,
            "annotations": t.annotations.model_dump(exclude_none=True) if t.annotations else None,
        }
        for t in tools
    }
    out = os.environ.get("OPUS55_MATRIX_OUT")
    if out:
        with open(out.replace(".matrix.jsonl", ".tool_schemas.json"), "w", encoding="utf-8") as fh:
            json.dump(dump, fh, indent=2, sort_keys=True)
    assert dump["act_dispatch_post"]["annotations"].get("destructiveHint") is True
    assert dump["act_policy"]["annotations"].get("readOnlyHint") is True
    assert set(dump["act_prepare_post"]["inputSchema"]["properties"]) == {
        "chat_id",
        "kind",
        "content",
        "reply_to",
        "schedule_at",
        "destination_type",
        "recipient_user_id",
        "recipient_username",
    }
    assert (
        dump["act_prepare_post"]["inputSchema"]["properties"]["destination_type"].get("default")
        == "group"
    )


def test_surface_no_prompts_or_resources(restricted_mcp):
    mcp, _ = restricted_mcp
    assert asyncio.run(mcp.list_prompts()) == []
    assert asyncio.run(mcp.list_resources()) == []
    assert asyncio.run(mcp.list_resource_templates()) == []


def test_surface_http_factory_still_builds_unauthenticated_app(restricted_mcp):
    """Not reachable from act_server/main/runner (stdio enforced) but importable by any local code."""
    import telegram_mcp.server as srv

    app = srv.get_http_app()
    assert app is not None


# --------------------------------------------------------------------------- tool-level dispatch
class _Me:
    def __init__(self, uid, bot=False):
        self.id, self.bot = uid, bot


class _Chan:
    def __init__(self, cid):
        from telethon.tl.types import Channel

        self.__class__ = type("FakeChannel", (Channel,), {})
        self.id = abs(cid) - 1000000000000
        self.left = self.kicked = self.deactivated = False


class FakeTelethon:
    def __init__(self, me_id=EXPECTED_USER, bot=False, reply_exists=True, send_delay=0.0):
        self.me = _Me(me_id, bot)
        self.reply_exists = reply_exists
        self.send_delay = send_delay
        self.flood_sleep_threshold = 60
        self.sent = []
        self.thresholds_seen = []

    def is_connected(self):
        return True

    async def get_me(self):
        return self.me

    async def get_entity(self, ident):
        from telethon.tl.types import PeerChannel
        from telethon import utils

        return type(
            "E",
            (),
            {
                "id": abs(ident) - 1000000000000,
                "left": False,
                "kicked": False,
                "deactivated": False,
            },
        )()

    async def get_messages(self, entity, ids=None):
        return object() if self.reply_exists else None

    async def send_message(self, entity, text, **kw):
        self.thresholds_seen.append(self.flood_sleep_threshold)
        if self.send_delay:
            await asyncio.sleep(self.send_delay)
        self.sent.append((text, kw))
        return type("M", (), {"id": len(self.sent)})()

    async def __call__(self, req, *a, **k):
        return None


@pytest.fixture
def tool_env(act_paths, monkeypatch):
    import act_manager.tools as tools
    from telethon import utils

    monkeypatch.setattr(policy, "CONFIG_PATH", act_paths[0])
    monkeypatch.setattr(policy, "DATA_PATH", act_paths[1])
    monkeypatch.setattr(utils, "get_peer_id", lambda e: -1000000000000 - e.id)
    return tools


def _approved(paths, **kw):
    p = prep(paths, now=None, **kw)
    human_approve(paths, p)
    return p


def test_tool_dispatch_rejects_wrong_account_and_bot(act_paths, tool_env, monkeypatch):
    for fake in (FakeTelethon(me_id=1), FakeTelethon(bot=True)):
        p = _approved(act_paths, content=f"id check {id(fake)}")
        monkeypatch.setattr(tool_env, "get_client", lambda: fake)
        monkeypatch.setattr(tool_env, "ensure_connected", lambda c: asyncio.sleep(0))
        monkeypatch.setattr(tool_env, "resolve_entity", lambda i, c: fake.get_entity(i))
        res = asyncio.run(tool_env.act_dispatch_post(p["id"]))
        assert res["ok"] is False and "mismatch" in res["error"] and not fake.sent
        assert status(act_paths, p["id"]) == "approved"


def test_tool_dispatch_missing_reply_target_rolls_back(act_paths, tool_env, monkeypatch):
    fake = FakeTelethon(reply_exists=False)
    p = _approved(act_paths, chat_id=CHAT_REPLIES_ONLY, kind="reply", reply_to=5, content="hi")
    monkeypatch.setattr(tool_env, "get_client", lambda: fake)
    monkeypatch.setattr(tool_env, "ensure_connected", lambda c: asyncio.sleep(0))
    monkeypatch.setattr(tool_env, "resolve_entity", lambda i, c: fake.get_entity(i))
    res = asyncio.run(tool_env.act_dispatch_post(p["id"]))
    assert res["ok"] is False and not fake.sent and status(act_paths, p["id"]) == "approved"


def test_regress_concurrent_dispatch_restores_flood_threshold(act_paths, tool_env, monkeypatch):
    """Two overlapping dispatches on one shared client must not leave/expose a wrong threshold."""
    fake = FakeTelethon(send_delay=0.2)
    a = _approved(act_paths, chat_id=CHAT_REPLIES_ONLY, kind="reply", reply_to=1, content="one")
    b = _approved(act_paths, chat_id=CHAT_REPLIES_ONLY, kind="reply", reply_to=2, content="two")
    monkeypatch.setattr(tool_env, "get_client", lambda: fake)
    monkeypatch.setattr(tool_env, "ensure_connected", lambda c: asyncio.sleep(0))
    monkeypatch.setattr(tool_env, "resolve_entity", lambda i, c: fake.get_entity(i))

    async def both():
        t1 = asyncio.ensure_future(tool_env.act_dispatch_post(a["id"]))
        await asyncio.sleep(0.05)
        t2 = asyncio.ensure_future(tool_env.act_dispatch_post(b["id"]))
        return await asyncio.gather(t1, t2)

    asyncio.run(both())
    out = os.environ.get("OPUS55_MATRIX_OUT")
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "case": "flood threshold race",
                        "seen_during_send": fake.thresholds_seen,
                        "final": fake.flood_sleep_threshold,
                    }
                )
                + "\n"
            )
    assert fake.thresholds_seen == [0, 0]
    assert fake.flood_sleep_threshold == 60
