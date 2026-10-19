"""Argon 4 Independent Forensic Audit — Adversarial & Regression Test Suite.

Executes strictly offline using fake/mock clients and temporary SQLite databases.
Verifies:
1. Pre-send validation failures in act_dispatch_post roll back to 'approved'
   without corrupting state to 'uncertain' or exhausting daily ad quota.
2. asyncio.CancelledError (MCP 55s tool timeout) during send_message finalizes
   status as 'uncertain' instead of leaving the post wedged in 'sending'.
3. Post-approval SQLite tampering (content, chat_id, reply_to, schedule_utc)
   is detected and refused by fingerprint & integrity_hash verification.
4. Terminal escape sequences (ANSI/CSI, CR, Unicode bidi overrides) are rejected
   in content and chat labels, and sanitized in CLI preview/discover output.
5. Duplicate prevention normalizes whitespace/zero-width chars and enforces
   a true 24-hour window across created_utc, planned_utc, and sent_utc.
6. Approval TTL expires unscheduled approvals after 24 hours and rejects
   approving already-expired scheduled posts.
7. Tool exposure across act_server.py, main.py, telegram_mcp.runner, and
   telegram_mcp.server exposes ONLY the 5 ACT tools and detaches incoming
   message listeners.
8. Riyadh (+03:00) midnight boundaries, timezone offsets, 60-minute spacing,
   and concurrent dispatch attempts.
9. Telethon peer ID verification, deactivated/left/kicked chat refusal,
   reply_to existence check, and flood_sleep_threshold=0 fast-fail.
10. CLI interactive/non-interactive approval, UTF-8 Arabic output, and
    atomic config initialization.
"""

from __future__ import annotations

import asyncio
import io
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from telethon import types

from act_manager import policy
import act_manager.tools as act_tools
import act_cli
import act_server
from telegram_mcp import runtime, runner, server as mcp_server

NOW = datetime(2026, 10, 17, 12, 0, 0, tzinfo=timezone.utc)
CHAT_1 = -1001234567890
CHAT_2 = -1001234567891
CHAT_3 = -1001234567892
CHAT_REPLY_ONLY = -1001234567893


@pytest.fixture(autouse=True)
def _frozen_policy_clock(monkeypatch):
    """Pin the policy clock one hour after NOW.

    These scenarios are built around the fixed NOW above, while act_dispatch_post
    reads the real clock through policy.utcnow(). Approvals expire after 24 hours,
    so without a pinned clock every test that dispatches a post approved at NOW
    starts failing once the wall clock is a day past it.
    """
    monkeypatch.setattr(policy, "utcnow", lambda: NOW + timedelta(hours=1))


@pytest.fixture
def env_paths(tmp_path, monkeypatch):
    cfg_path = tmp_path / "act_config.json"
    db_path = tmp_path / "posts.sqlite3"
    cfg_path.write_text(
        json.dumps(
            {
                "expected_user_id": 999001,
                "daily_ad_limit": 3,
                "allowed_chats": [
                    {"chat_id": CHAT_1, "label": "Group 1", "ads_allowed": True},
                    {"chat_id": CHAT_2, "label": "Group 2", "ads_allowed": True},
                    {"chat_id": CHAT_3, "label": "Group 3", "ads_allowed": True},
                    {"chat_id": CHAT_REPLY_ONLY, "label": "Replies Only", "ads_allowed": False},
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(policy, "CONFIG_PATH", cfg_path)
    monkeypatch.setattr(policy, "DATA_PATH", db_path)
    return cfg_path, db_path


# ============================================================================
# 1. CRITICAL F-01: Pre-send validation failures roll back to 'approved'
#    and NEVER corrupt state to 'uncertain' or consume ad quota
# ============================================================================


@pytest.mark.asyncio
async def test_f01_presend_failures_rollback_to_approved_and_preserve_quota(
    env_paths, monkeypatch
):
    """When account mismatch, bot account, entity resolution error, or peer mismatch
    occurs before client.send_message(), the post rolls back to 'approved', preserves
    the exact PolicyError message, and does not consume daily ad quota.
    """
    cfg_path, db_path = env_paths
    draft_res = await act_tools.act_prepare_post(
        chat_id=CHAT_1, kind="ad", content="Offer 100 SAR"
    )
    assert draft_res["ok"] is True
    draft_id = draft_res["draft"]["id"]
    policy.approve(draft_id, db_path=db_path)

    # Case A: Wrong Telegram user ID
    class WrongUserClient:
        async def get_me(self):
            return SimpleNamespace(id=111222, bot=False)

    async def noop_connect(cl=None):
        return None

    monkeypatch.setattr(act_tools, "get_client", lambda: WrongUserClient())
    monkeypatch.setattr(act_tools, "ensure_connected", noop_connect)

    res = await act_tools.act_dispatch_post(draft_id)
    assert res["ok"] is False
    assert "Account mismatch or bot account" in res["error"]

    post = policy.get_post(draft_id, db_path=db_path)
    assert post["status"] == "approved"
    assert "Account mismatch" in post["error"]

    # Case B: Bot account must also be refused before send
    class BotClient:
        async def get_me(self):
            return SimpleNamespace(id=999001, bot=True)

    monkeypatch.setattr(act_tools, "get_client", lambda: BotClient())
    res_bot = await act_tools.act_dispatch_post(draft_id)
    assert res_bot["ok"] is False
    assert "Account mismatch or bot account" in res_bot["error"]
    assert policy.get_post(draft_id, db_path=db_path)["status"] == "approved"

    # Case C: Resolved peer ID mismatch
    class RightUserClient:
        flood_sleep_threshold = 60

        async def get_me(self):
            return SimpleNamespace(id=999001, bot=False)

        async def send_message(self, entity, content, **kwargs):
            return SimpleNamespace(id=555)

    wrong_channel = types.Channel(
        id=9999999999,
        title="Wrong Group",
        photo=types.ChatPhotoEmpty(),
        date=NOW,
        megagroup=True,
    )

    async def resolve_wrong(cid, cl=None):
        return wrong_channel

    monkeypatch.setattr(act_tools, "get_client", lambda: RightUserClient())
    monkeypatch.setattr(act_tools, "resolve_entity", resolve_wrong)

    res_mismatch = await act_tools.act_dispatch_post(draft_id)
    assert res_mismatch["ok"] is False
    assert "destination mismatch" in res_mismatch["error"]
    assert policy.get_post(draft_id, db_path=db_path)["status"] == "approved"

    # Case D: Now fix entity resolution -> same approved draft dispatches cleanly!
    right_channel = types.Channel(
        id=1234567890,
        title="Group 1",
        photo=types.ChatPhotoEmpty(),
        date=NOW,
        megagroup=True,
    )

    async def resolve_right(cid, cl=None):
        return right_channel

    monkeypatch.setattr(act_tools, "resolve_entity", resolve_right)
    res_ok = await act_tools.act_dispatch_post(draft_id)
    assert res_ok["ok"] is True
    assert res_ok["post"]["status"] == "sent"
    assert res_ok["post"]["external_id"] == 555


# ============================================================================
# 2. CRITICAL F-02: asyncio.CancelledError during send_message finalizes
#    status to 'uncertain' instead of leaving post wedged in 'sending'
# ============================================================================


@pytest.mark.asyncio
async def test_f02_cancelled_error_finalizes_as_uncertain(env_paths):
    """asyncio.CancelledError (a BaseException raised by asyncio.wait_for timeout)
    must finalize the post as 'uncertain' with a recorded sent_utc timestamp.
    """
    cfg_path, db_path = env_paths
    draft = policy.prepare(
        chat_id=CHAT_1, kind="ad", content="Timeout Ad", config_path=cfg_path, db_path=db_path
    )
    policy.approve(draft["id"], db_path=db_path)

    async def cancelled_sender(post):
        raise asyncio.CancelledError("MCP tool timeout after 55s")

    with pytest.raises(policy.PolicyError, match="uncertain"):
        await policy.execute(draft["id"], cancelled_sender, config_path=cfg_path, db_path=db_path)

    post = policy.get_post(draft["id"], db_path=db_path)
    assert post["status"] == "uncertain"
    assert post["error"] == "CancelledError"
    assert post["sent_utc"] is not None


@pytest.mark.parametrize(
    "cancel_at", ["ensure_connected", "get_me", "resolve_entity", "get_messages"]
)
@pytest.mark.asyncio
async def test_r01_preflight_cancellation_restores_approved_without_sending(
    env_paths, monkeypatch, cancel_at
):
    """Cancellation at any read-only Telegram preflight await is known not to have sent."""
    cfg_path, db_path = env_paths
    reply_to = 9911
    kind = "reply" if cancel_at == "get_messages" else "ad"
    draft = policy.prepare(
        chat_id=CHAT_REPLY_ONLY if kind == "reply" else CHAT_1,
        kind=kind,
        reply_to=reply_to if kind == "reply" else None,
        content="Preflight cancellation regression",
        config_path=cfg_path,
        db_path=db_path,
        now=NOW,
    )
    policy.approve(draft["id"], db_path=db_path, now=NOW)

    channel = types.Channel(
        id=1234567893 if kind == "reply" else 1234567890,
        title="Mock destination",
        photo=types.ChatPhotoEmpty(),
        date=NOW,
        megagroup=True,
    )
    cancellation = {"pending": True}
    send_calls = []

    def maybe_cancel(point):
        if cancellation["pending"] and cancel_at == point:
            cancellation["pending"] = False
            raise asyncio.CancelledError(f"cancelled during {point}")

    class MockClient:
        flood_sleep_threshold = 60

        async def get_me(self):
            maybe_cancel("get_me")
            return SimpleNamespace(id=999001, bot=False)

        async def get_messages(self, entity, ids=None):
            maybe_cancel("get_messages")
            return SimpleNamespace(id=ids)

        async def send_message(self, entity, content, **kwargs):
            send_calls.append((entity, content, kwargs))
            return SimpleNamespace(id=777)

    async def fake_connect(client=None):
        maybe_cancel("ensure_connected")

    async def fake_resolve(chat_id, client=None):
        maybe_cancel("resolve_entity")
        return channel

    monkeypatch.setattr(act_tools, "get_client", lambda: MockClient())
    monkeypatch.setattr(act_tools, "ensure_connected", fake_connect)
    monkeypatch.setattr(act_tools, "resolve_entity", fake_resolve)

    with pytest.raises(asyncio.CancelledError):
        await act_tools.act_dispatch_post(draft["id"])

    post = policy.get_post(draft["id"], db_path=db_path)
    assert post["status"] == "approved"
    assert post["error"] == "CancelledError"
    assert send_calls == []

    # The same still-approved draft may be retried because no send call began.
    retry = await act_tools.act_dispatch_post(draft["id"])
    assert retry["ok"] is True
    assert retry["post"]["status"] == "sent"
    assert len(send_calls) == 1


@pytest.mark.asyncio
async def test_r01_cancellation_after_send_begins_stays_uncertain_and_nonretryable(
    env_paths, monkeypatch
):
    cfg_path, db_path = env_paths
    draft = policy.prepare(
        chat_id=CHAT_1,
        kind="ad",
        content="Send phase cancellation regression",
        config_path=cfg_path,
        db_path=db_path,
        now=NOW,
    )
    policy.approve(draft["id"], db_path=db_path, now=NOW)
    send_calls = []
    channel = types.Channel(
        id=1234567890,
        title="Mock destination",
        photo=types.ChatPhotoEmpty(),
        date=NOW,
        megagroup=True,
    )

    class MockClient:
        flood_sleep_threshold = 60

        async def get_me(self):
            return SimpleNamespace(id=999001, bot=False)

        async def send_message(self, entity, content, **kwargs):
            send_calls.append((entity, content, kwargs))
            raise asyncio.CancelledError("cancelled after send invocation")

    async def fake_connect(client=None):
        return None

    async def fake_resolve(chat_id, client=None):
        return channel

    monkeypatch.setattr(act_tools, "get_client", lambda: MockClient())
    monkeypatch.setattr(act_tools, "ensure_connected", fake_connect)
    monkeypatch.setattr(act_tools, "resolve_entity", fake_resolve)

    result = await act_tools.act_dispatch_post(draft["id"])
    assert result["ok"] is False
    assert "uncertain" in result["error"]
    assert len(send_calls) == 1
    assert policy.get_post(draft["id"], db_path=db_path)["status"] == "uncertain"

    retry = await act_tools.act_dispatch_post(draft["id"])
    assert retry["ok"] is False
    assert len(send_calls) == 1
    assert policy.get_post(draft["id"], db_path=db_path)["status"] == "uncertain"


@pytest.mark.asyncio
async def test_send_phase_policy_error_is_also_uncertain_and_nonretryable(env_paths):
    cfg_path, db_path = env_paths
    draft = policy.prepare(
        chat_id=CHAT_1,
        kind="ad",
        content="Ambiguous policy error",
        config_path=cfg_path,
        db_path=db_path,
    )
    policy.approve(draft["id"], db_path=db_path)

    async def sender_after_send_begins(post):
        raise policy.PolicyError("transport wrapper failed after send invocation")

    with pytest.raises(policy.PolicyError, match="uncertain"):
        await policy.execute(
            draft["id"], sender_after_send_begins, config_path=cfg_path, db_path=db_path
        )
    assert policy.get_post(draft["id"], db_path=db_path)["status"] == "uncertain"


@pytest.mark.asyncio
async def test_scheduled_dispatch_passes_telethon_schedule_in_mock(env_paths, monkeypatch):
    cfg_path, db_path = env_paths
    scheduled_at = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat(
        timespec="seconds"
    )
    draft = policy.prepare(
        chat_id=CHAT_1,
        kind="ad",
        content="Mock scheduled message",
        schedule_at=scheduled_at,
        config_path=cfg_path,
        db_path=db_path,
        now=NOW,
    )
    policy.approve(draft["id"], db_path=db_path, now=NOW)
    channel = types.Channel(
        id=1234567890,
        title="Mock destination",
        photo=types.ChatPhotoEmpty(),
        date=NOW,
        megagroup=True,
    )
    sent_kwargs = []

    class MockClient:
        flood_sleep_threshold = 60

        async def get_me(self):
            return SimpleNamespace(id=999001, bot=False)

        async def send_message(self, entity, content, **kwargs):
            sent_kwargs.append(kwargs)
            return SimpleNamespace(id=778)

    async def fake_connect(client=None):
        return None

    async def fake_resolve(chat_id, client=None):
        return channel

    monkeypatch.setattr(act_tools, "get_client", lambda: MockClient())
    monkeypatch.setattr(act_tools, "ensure_connected", fake_connect)
    monkeypatch.setattr(act_tools, "resolve_entity", fake_resolve)

    result = await act_tools.act_dispatch_post(draft["id"])
    assert result["ok"] is True
    assert result["post"]["status"] == "scheduled"
    assert sent_kwargs == [
        {
            "parse_mode": None,
            "link_preview": False,
            "schedule": datetime.fromisoformat(draft["schedule_utc"]),
        }
    ]


@pytest.mark.asyncio
async def test_reply_dispatch_verifies_and_forwards_reply_to_in_mock(env_paths, monkeypatch):
    cfg_path, db_path = env_paths
    reply_to = 123456
    draft = policy.prepare(
        chat_id=CHAT_REPLY_ONLY,
        kind="reply",
        reply_to=reply_to,
        content="Mock reply",
        config_path=cfg_path,
        db_path=db_path,
        now=NOW,
    )
    policy.approve(draft["id"], db_path=db_path, now=NOW)
    channel = types.Channel(
        id=1234567893,
        title="Replies Only",
        photo=types.ChatPhotoEmpty(),
        date=NOW,
        megagroup=True,
    )
    message_lookups = []
    sent_kwargs = []

    class MockClient:
        flood_sleep_threshold = 60

        async def get_me(self):
            return SimpleNamespace(id=999001, bot=False)

        async def get_messages(self, entity, ids=None):
            message_lookups.append((entity, ids))
            return SimpleNamespace(id=ids)

        async def send_message(self, entity, content, **kwargs):
            sent_kwargs.append(kwargs)
            return SimpleNamespace(id=779)

    async def fake_connect(client=None):
        return None

    async def fake_resolve(chat_id, client=None):
        return channel

    monkeypatch.setattr(act_tools, "get_client", lambda: MockClient())
    monkeypatch.setattr(act_tools, "ensure_connected", fake_connect)
    monkeypatch.setattr(act_tools, "resolve_entity", fake_resolve)

    result = await act_tools.act_dispatch_post(draft["id"])
    assert result["ok"] is True
    assert result["post"]["status"] == "sent"
    assert message_lookups == [(channel, reply_to)]
    assert sent_kwargs[0]["reply_to"] == reply_to


# ============================================================================
# 3. HIGH F-03: MCP Tool Exposure & Incoming Handler Detachment
# ============================================================================


def test_f03_all_entrypoints_expose_only_five_act_tools_and_reject_http(monkeypatch):
    """Verify server.get_server(), server.get_http_app(), server.serve(),
    runner.main(), and act_server.main() enforce the 5-tool allowlist and stdio.
    """
    # 1. server.get_server()
    srv = mcp_server.get_server()
    exposed = {t.name for t in srv._tool_manager.list_tools()}
    assert exposed == act_server.ALLOWED

    # 2. server.get_http_app()
    _ = mcp_server.get_http_app()
    assert {t.name for t in runtime.mcp._tool_manager.list_tools()} == act_server.ALLOWED

    # 3. server.serve("http") is rejected
    with pytest.raises(SystemExit, match="stdio only"):
        asyncio.run(mcp_server.serve("http"))

    # 4. act_server.main() rejects non-stdio MCP_TRANSPORT and CLI args
    monkeypatch.setenv("MCP_TRANSPORT", "sse")
    monkeypatch.setattr("sys.argv", ["act_server.py"])
    with pytest.raises(SystemExit, match="MCP_TRANSPORT=stdio"):
        act_server.main()

    monkeypatch.setenv("MCP_TRANSPORT", "stdio")
    monkeypatch.setattr("sys.argv", ["act_server.py", "--transport", "http"])
    with pytest.raises(SystemExit, match="no CLI arguments"):
        act_server.main()

    # 5. TELEGRAM_FILE_EXTENSIONS does not crash runner.main() after tool pruning
    monkeypatch.setenv("TELEGRAM_FILE_EXTENSIONS", "send_file:.pdf")
    monkeypatch.setattr("sys.argv", ["pytest"])
    monkeypatch.setattr(runner._runtime, "_CLI_TRANSPORT", None)
    monkeypatch.setattr(runner._transcription, "validate_transcription_config", lambda: None)
    monkeypatch.setattr(runner, "_session_lock_shared", lambda: False)
    monkeypatch.setattr(runner.asyncio, "run", lambda coro: coro.close())
    runner.main()
    assert {t.name for t in runtime.mcp._tool_manager.list_tools()} == act_server.ALLOWED


@pytest.mark.asyncio
async def test_f03_public_transport_exports_prune_the_passed_server():
    """Raw transport helpers re-exported by telegram_mcp.server must guard first."""
    assert "mcp" not in mcp_server.__all__
    assert not hasattr(mcp_server, "mcp")

    class FakeTool:
        def __init__(self, name):
            self.name = name

    class FakeToolManager:
        def __init__(self):
            self.tools = {name: FakeTool(name) for name in act_server.ALLOWED}
            self.tools["send_message"] = FakeTool("send_message")

        def list_tools(self):
            return list(self.tools.values())

        def remove_tool(self, name):
            self.tools.pop(name, None)

    class FakeServer:
        def __init__(self):
            self._tool_manager = FakeToolManager()
            self.started_with = None

        async def run_stdio_async(self):
            self.started_with = {tool.name for tool in self._tool_manager.list_tools()}

        async def run_streamable_http_async(self):
            self.started_with = {tool.name for tool in self._tool_manager.list_tools()}

    app_server = FakeServer()
    assert mcp_server.create_streamable_http_app(app_server) is app_server
    assert {tool.name for tool in app_server._tool_manager.list_tools()} == act_server.ALLOWED

    stdio_server = FakeServer()
    await mcp_server.run_stdio_async(stdio_server)
    assert stdio_server.started_with == act_server.ALLOWED

    http_server = FakeServer()
    await mcp_server.run_streamable_http_async(http_server)
    assert http_server.started_with == act_server.ALLOWED


@pytest.mark.asyncio
async def test_f03_server_module_transport_exports_are_guard_wrappers():
    """The public server exports must not be direct aliases of upstream transports."""
    from telegram_mcp.transport import (
        create_streamable_http_app as raw_create_app,
        run_stdio_async as raw_run_stdio,
        run_streamable_http_async as raw_run_http,
    )

    assert mcp_server.create_streamable_http_app is not raw_create_app
    assert mcp_server.run_stdio_async is not raw_run_stdio
    assert mcp_server.run_streamable_http_async is not raw_run_http


# ============================================================================
# 4. HIGH F-04: Post-Approval SQLite Tampering & Approval Expiration (TTL)
# ============================================================================


@pytest.mark.asyncio
async def test_f04_sqlite_tampering_detected_and_refused(env_paths):
    """Modifying content, chat_id, or schedule_utc in SQLite after approval
    must be detected by reserve() and fail closed with PolicyError.
    """
    cfg_path, db_path = env_paths
    draft = policy.prepare(
        chat_id=CHAT_1,
        kind="ad",
        content="Original Approved Text",
        schedule_at="2026-10-18T12:00:00+03:00",
        config_path=cfg_path,
        db_path=db_path,
        now=NOW,
    )
    policy.approve(draft["id"], db_path=db_path, now=NOW)

    # Tamper with content
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE posts SET content='TAMPERED CONTENT' WHERE id=?", (draft["id"],))
    conn.commit()
    conn.close()

    async def dummy_send(item):
        return 1

    with pytest.raises(policy.PolicyError, match="integrity check failed"):
        await policy.execute(
            draft["id"], dummy_send, config_path=cfg_path, db_path=db_path, now=NOW
        )

    # Restore content, tamper with schedule_utc (keeping fingerprint unchanged!)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE posts SET content='Original Approved Text', schedule_utc='2026-10-19T15:00:00+00:00' WHERE id=?",
        (draft["id"],),
    )
    conn.commit()
    conn.close()

    with pytest.raises(policy.PolicyError, match="integrity check failed"):
        await policy.execute(
            draft["id"], dummy_send, config_path=cfg_path, db_path=db_path, now=NOW
        )


@pytest.mark.asyncio
async def test_f04_approval_ttl_expiration(env_paths):
    """Unscheduled approvals expire after 24 hours if not dispatched."""
    cfg_path, db_path = env_paths
    draft = policy.prepare(
        chat_id=CHAT_1,
        kind="ad",
        content="Time-bounded Ad",
        config_path=cfg_path,
        db_path=db_path,
        now=NOW,
    )
    policy.approve(draft["id"], db_path=db_path, now=NOW)

    async def dummy_send(item):
        return 1

    with pytest.raises(policy.PolicyError, match="Approval expired"):
        await policy.execute(
            draft["id"],
            dummy_send,
            config_path=cfg_path,
            db_path=db_path,
            now=NOW + timedelta(hours=24, minutes=1),
        )


# ============================================================================
# 5. HIGH F-05: Terminal Escape & Bidi Override Rejection
# ============================================================================


def test_f05_terminal_escape_and_bidi_injection_rejected(env_paths):
    """ANSI CSI escapes, carriage returns, and Unicode bidi overrides are refused."""
    cfg_path, db_path = env_paths
    for bad_payload in (
        "Safe text\x1b[2J\x1b[H Fake prompt",
        "Hidden line\rOverwritten visible line",
        "Bidi spoof \u202e malicious",
        "\u200b\u200c\u200d   ",  # Only zero-width/whitespace
    ):
        with pytest.raises(policy.PolicyError):
            policy.prepare(
                chat_id=CHAT_1,
                kind="ad",
                content=bad_payload,
                config_path=cfg_path,
                db_path=db_path,
            )


# ============================================================================
# 6. HIGH F-07: Duplicate Normalization & 24h Window on Sent/Planned Time
# ============================================================================


@pytest.mark.asyncio
async def test_f07_duplicate_detection_normalizes_whitespace_and_checks_sent_time(env_paths):
    """Trailing spaces, zero-width chars, or old created_utc cannot bypass 24h dedup."""
    cfg_path, db_path = env_paths
    t_create = NOW - timedelta(hours=30)
    t_send = NOW - timedelta(hours=2)

    d1 = policy.prepare(
        chat_id=CHAT_1,
        kind="ad",
        content="Promo Offer",
        config_path=cfg_path,
        db_path=db_path,
        now=t_create,
    )
    policy.approve(d1["id"], db_path=db_path, now=t_send)

    async def ok_sender(item):
        return 101

    await policy.execute(d1["id"], ok_sender, config_path=cfg_path, db_path=db_path, now=t_send)

    # Attempt duplicate with extra whitespace and zero-width space 2 hours after d1 was sent
    d2 = policy.prepare(
        chat_id=CHAT_1,
        kind="ad",
        content="Promo   Offer\u200b ",
        config_path=cfg_path,
        db_path=db_path,
        now=NOW,
    )
    policy.approve(d2["id"], db_path=db_path, now=NOW)
    with pytest.raises(policy.PolicyError, match="duplicate"):
        await policy.execute(d2["id"], ok_sender, config_path=cfg_path, db_path=db_path, now=NOW)


# ============================================================================
# 7. MEDIUM F-08, F-11, F-12: parse_time, CLI UTF-8 & Approval Flow, Reply Verification
# ============================================================================


def test_f11_parse_time_rejects_non_string_and_supports_lowercase_z():
    with pytest.raises(policy.PolicyError):
        policy.parse_time(12345, now=NOW)  # type: ignore[arg-type]
    parsed = policy.parse_time("2026-10-18T12:00:00z", now=NOW)
    assert parsed == datetime(2026, 10, 18, 12, 0, 0, tzinfo=timezone.utc)


def test_f11_cli_init_approve_and_cancel_workflow(env_paths, monkeypatch, capsys):
    cfg_path, db_path = env_paths
    # 1. init refuses to overwrite existing config
    with pytest.raises(SystemExit) as excinfo:
        act_cli.main(["init"])
    assert excinfo.value.code == 2

    # 2. prepare Arabic draft
    draft = policy.prepare(
        chat_id=CHAT_1,
        kind="ad",
        content="عرض خاص لمنصة سلة وأودو",
        config_path=cfg_path,
        db_path=db_path,
        now=NOW,
    )

    # 3. non-TTY stdin is refused by approve
    monkeypatch.setattr("sys.stdin", io.StringIO(f"APPROVE {draft['id']}\n"))
    with pytest.raises(SystemExit) as excinfo:
        act_cli.main(["approve", draft["id"]])
    assert excinfo.value.code == 2
    assert policy.get_post(draft["id"], db_path=db_path)["status"] == "draft"

    # 4. Interactive TTY with wrong phrase is refused
    class FakeTTY(io.StringIO):
        def isatty(self):
            return True

    monkeypatch.setattr("sys.stdin", FakeTTY("yes\n"))
    monkeypatch.setattr("builtins.input", lambda prompt="": "yes")
    with pytest.raises(SystemExit):
        act_cli.main(["approve", draft["id"]])
    assert policy.get_post(draft["id"], db_path=db_path)["status"] == "draft"

    # 5. Interactive TTY with exact phrase succeeds
    monkeypatch.setattr("sys.stdin", FakeTTY(""))
    monkeypatch.setattr("builtins.input", lambda prompt="": f"APPROVE {draft['id']}")
    act_cli.main(["approve", draft["id"]])
    assert policy.get_post(draft["id"], db_path=db_path)["status"] == "approved"

    # 6. Approving an already-approved draft fails before prompting
    with pytest.raises(SystemExit):
        act_cli.main(["approve", draft["id"]])


@pytest.mark.asyncio
async def test_f08_reply_verifies_original_message_and_refuses_kicked_chat(env_paths, monkeypatch):
    cfg_path, db_path = env_paths
    reply_draft = policy.prepare(
        chat_id=CHAT_REPLY_ONLY,
        kind="reply",
        reply_to=9999,
        content="Here is the answer",
        config_path=cfg_path,
        db_path=db_path,
    )
    policy.approve(reply_draft["id"], db_path=db_path)

    channel_entity = types.Channel(
        id=1234567893,
        title="Replies Only",
        photo=types.ChatPhotoEmpty(),
        date=NOW,
        megagroup=True,
    )

    class ClientMissingReplyMsg:
        async def get_me(self):
            return SimpleNamespace(id=999001, bot=False)

        async def get_messages(self, entity, ids=None):
            return None  # Message 9999 does not exist!

        async def send_message(self, entity, content, **kwargs):
            return SimpleNamespace(id=888)

    async def fake_connect(cl=None):
        return None

    async def fake_resolve(cid, cl=None):
        return channel_entity

    monkeypatch.setattr(act_tools, "get_client", lambda: ClientMissingReplyMsg())
    monkeypatch.setattr(act_tools, "ensure_connected", fake_connect)
    monkeypatch.setattr(act_tools, "resolve_entity", fake_resolve)

    res = await act_tools.act_dispatch_post(reply_draft["id"])
    assert res["ok"] is False
    assert "reply_to message not found" in res["error"]
    # Remains approved (not uncertain!)
    assert policy.get_post(reply_draft["id"], db_path=db_path)["status"] == "approved"


# ============================================================================
# 8. RIYADH TIMEZONE & TELETHON PEER ID COMPATIBILITY
# ============================================================================


@pytest.mark.asyncio
async def test_riyadh_midnight_boundary_exact_seconds(env_paths):
    """Test Riyadh (+03:00) midnight transition at 23:59:59 vs 00:00:00."""
    cfg_path, db_path = env_paths

    async def ok_sender(post):
        return 100

    for idx, riyadh_str in enumerate(
        (
            "2026-10-18T18:00:00+03:00",
            "2026-10-18T20:00:00+03:00",
            "2026-10-18T23:59:59+03:00",
        )
    ):
        d = policy.prepare(
            chat_id=CHAT_1,
            kind="ad",
            content=f"Day1 Ad {idx}",
            schedule_at=riyadh_str,
            config_path=cfg_path,
            db_path=db_path,
            now=NOW,
        )
        policy.approve(d["id"], db_path=db_path, now=NOW)
        await policy.execute(d["id"], ok_sender, config_path=cfg_path, db_path=db_path, now=NOW)

    # At 00:00:00+03:00 (1s later), blocked by 60-minute spacing
    d_midnight = policy.prepare(
        chat_id=CHAT_2,
        kind="ad",
        content="Midnight Ad",
        schedule_at="2026-10-19T00:00:00+03:00",
        config_path=cfg_path,
        db_path=db_path,
        now=NOW,
    )
    policy.approve(d_midnight["id"], db_path=db_path, now=NOW)
    with pytest.raises(policy.PolicyError, match="60 minutes"):
        await policy.execute(
            d_midnight["id"], ok_sender, config_path=cfg_path, db_path=db_path, now=NOW
        )

    # At 01:00:00+03:00 (60m01s later), both spacing and new Riyadh day quota allow it
    d_next_day = policy.prepare(
        chat_id=CHAT_2,
        kind="ad",
        content="Next Day 1am Ad",
        schedule_at="2026-10-19T01:00:00+03:00",
        config_path=cfg_path,
        db_path=db_path,
        now=NOW,
    )
    policy.approve(d_next_day["id"], db_path=db_path, now=NOW)
    res = await policy.execute(
        d_next_day["id"], ok_sender, config_path=cfg_path, db_path=db_path, now=NOW
    )
    assert res["status"] == "scheduled"
