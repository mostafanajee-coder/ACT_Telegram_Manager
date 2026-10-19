"""PRIORITY 0 - is human approval a real boundary against a same-user agent?

`test_exploit_*` tests demonstrate a bypass. They PASS when the bypass works.
They are expected to keep passing on any build where the coding agent shares
the operator's Windows identity, because no in-process control can stop a
process that may edit the DB, the config, the code, or reuse the session.
Dummy SQLite + fake sender only. No Telegram.
"""

import builtins
import io
import json
import sqlite3
import sys

import pytest

from act_manager import policy
from conftest import CHAT_ADS, CHAT_ADS_2, CHAT_ADS_3, NOW
from opus55_helpers import (
    SendRecorder,
    dispatch,
    human_approve,
    later,
    prep,
    raw_db,
    run_child,
    status,
    unkeyed_integrity,
)


def test_exploit_direct_policy_approve_from_non_tty_child_process(act_paths):
    """A same-user process with stdin=/dev/null calls policy.approve() directly."""
    post = prep(act_paths)
    code = (
        "import sys; from pathlib import Path; from act_manager import policy;"
        "sys.stderr.write('stdin_isatty=%s' % sys.stdin.isatty());"
        "print(policy.approve(sys.argv[1], db_path=Path(sys.argv[2]))['status'])"
    )
    child = run_child(code, post["id"], act_paths[1])
    assert child.returncode == 0, child.stderr
    assert child.stdout.strip() == "approved"
    # No terminal interaction occurred; approval came from a script.
    sender = SendRecorder()
    assert dispatch(act_paths, post["id"], sender)["status"] == "sent"
    assert len(sender.calls) == 1


def test_exploit_cli_isatty_and_typed_phrase_are_spoofable_in_process(act_paths, monkeypatch):
    """act_cli's TTY check and typed phrase can be satisfied by the same user's code."""
    import act_cli

    post = prep(act_paths)
    monkeypatch.setattr(policy, "CONFIG_PATH", act_paths[0])
    monkeypatch.setattr(policy, "DATA_PATH", act_paths[1])
    fake_stdin = io.StringIO()
    fake_stdin.isatty = lambda: True
    monkeypatch.setattr(sys, "stdin", fake_stdin)
    monkeypatch.setattr(builtins, "input", lambda prompt="": f"APPROVE {post['id']}")
    act_cli.main(["approve", post["id"]])
    assert status(act_paths, post["id"]) == "approved"


def test_exploit_sqlite_status_flip_without_any_approval_call(act_paths):
    post = prep(act_paths)
    con = raw_db(act_paths)
    con.execute(
        "UPDATE posts SET status='approved', approved_utc=? WHERE id=?",
        (NOW.isoformat(timespec="seconds"), post["id"]),
    )
    con.close()
    sender = SendRecorder()
    assert dispatch(act_paths, post["id"], sender)["status"] == "sent"


def test_exploit_post_approval_content_swap_with_recomputed_unkeyed_hash(act_paths):
    post = prep(act_paths, content="Operator saw this harmless text")
    human_approve(act_paths, post)
    evil = "Different promotional text the operator never saw"
    fp = policy.compute_fingerprint(post["chat_id"], "ad", None, evil)
    ih = unkeyed_integrity(post["id"], post["chat_id"], "ad", None, evil, None)
    con = raw_db(act_paths)
    con.execute(
        "UPDATE posts SET content=?, fingerprint=?, integrity_hash=? WHERE id=?",
        (evil, fp, ih, post["id"]),
    )
    con.close()
    sender = SendRecorder()
    dispatch(act_paths, post["id"], sender)
    assert sender.calls[0]["content"] == evil


def test_exploit_forged_row_inserted_directly_as_approved(act_paths):
    prep(act_paths)  # creates schema
    forged_id = "f0f0f0f0f0f0f0f0"
    content = "Forged message never drafted through MCP"
    con = raw_db(act_paths)
    con.execute(
        "INSERT INTO posts (id,chat_id,kind,reply_to,content,fingerprint,integrity_hash,schedule_utc,"
        "planned_utc,status,created_utc,approved_utc) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            forged_id,
            CHAT_ADS_2,
            "ad",
            None,
            content,
            policy.compute_fingerprint(CHAT_ADS_2, "ad", None, content),
            unkeyed_integrity(forged_id, CHAT_ADS_2, "ad", None, content, None),
            None,
            NOW.isoformat(timespec="seconds"),
            "approved",
            NOW.isoformat(timespec="seconds"),
            NOW.isoformat(timespec="seconds"),
        ),
    )
    con.close()
    sender = SendRecorder()
    assert dispatch(act_paths, forged_id, sender)["status"] == "sent"


def test_exploit_persistent_sqlite_trigger_auto_approves_every_new_draft(act_paths):
    """Planted once; afterwards act_prepare_post output is dispatchable with no human step."""
    prep(act_paths)
    con = raw_db(act_paths)
    con.execute("""CREATE TRIGGER auto_ok AFTER INSERT ON posts BEGIN
        UPDATE posts SET status='approved', approved_utc=NEW.created_utc WHERE id=NEW.id; END""")
    con.close()
    try:
        post = prep(act_paths, chat_id=CHAT_ADS_3, content="Never reviewed by a person")
    except policy.PolicyError:
        pytest.skip("build refuses databases containing triggers (hardened candidate)")
    assert post["status"] == "approved"
    sender = SendRecorder()
    assert dispatch(act_paths, post["id"], sender)["status"] == "sent"


def test_exploit_quota_reset_by_editing_audit_rows(act_paths):
    sender = SendRecorder()
    for i, chat in enumerate((CHAT_ADS, CHAT_ADS_2, CHAT_ADS_3)):
        p = prep(
            act_paths,
            chat_id=chat,
            content=f"Ad {i}",
            schedule_at=later(hours=1 + 2 * i).isoformat(),
        )
        human_approve(act_paths, p)
        dispatch(act_paths, p["id"], sender)
    fourth = prep(act_paths, content="Ad 4", schedule_at=later(hours=8).isoformat())
    human_approve(act_paths, fourth)
    with pytest.raises(policy.PolicyError, match="quota"):
        dispatch(act_paths, fourth["id"], sender)
    con = raw_db(act_paths)
    con.execute("UPDATE posts SET status='cancelled' WHERE status='scheduled'")
    con.execute("UPDATE posts SET status='approved', error=NULL WHERE id=?", (fourth["id"],))
    con.close()
    assert dispatch(act_paths, fourth["id"], sender)["status"] == "scheduled"
    assert len(sender.calls) == 4


def test_exploit_quota_is_per_database_path_not_per_account(act_paths, tmp_path):
    """A second project copy / launch path has its own posts.sqlite3 and its own quota."""
    cfg = act_paths[0]
    sender = SendRecorder()
    sent = 0
    for copy in ("copyA", "copyB"):
        paths = (cfg, tmp_path / copy / "posts.sqlite3")
        for i, chat in enumerate((CHAT_ADS, CHAT_ADS_2, CHAT_ADS_3)):
            p = prep(
                paths,
                chat_id=chat,
                content=f"{copy} ad {i}",
                schedule_at=later(
                    hours=1 + 2 * i, minutes=1 if copy == "copyB" else 0
                ).isoformat(),
            )
            human_approve(paths, p)
            dispatch(paths, p["id"], sender)
            sent += 1
    assert sent == 6 and len(sender.calls) == 6  # 6 ads on one Riyadh day, 1 minute apart pairwise


def test_exploit_config_is_same_user_writable_policy(act_paths):
    """Allowlist and ads_allowed are plain JSON the agent can rewrite."""
    cfg = json.loads(act_paths[0].read_text(encoding="utf-8"))
    new_chat = -1009999999999
    cfg["allowed_chats"].append(
        {"chat_id": new_chat, "label": "added by agent", "ads_allowed": True}
    )
    act_paths[0].write_text(json.dumps(cfg), encoding="utf-8")
    p = prep(act_paths, chat_id=new_chat, content="ad to a chat the operator never listed")
    assert p["status"] == "draft"


def test_exploit_upstream_send_tool_is_importable_and_ignores_act_policy(monkeypatch):
    """MCP pruning removes registry entries; the Python functions remain callable."""
    import asyncio
    from telegram_mcp import runtime
    from telegram_mcp.tools import messages

    class FakeEntity:
        id = 1111111111
        title = "dummy"

    class FakeClient:
        sent = []

        def is_connected(self):
            return True

        async def get_entity(self, ident):
            return FakeEntity()

        async def send_message(self, entity, text, **kw):
            FakeClient.sent.append(text)

            class R:
                id = 99

            return R()

        async def __call__(self, req, *a, **kw):
            return None

    fake = FakeClient()
    monkeypatch.setattr(runtime, "clients", {"default": fake})
    monkeypatch.setattr(runtime, "_last_conn_verified", {id(fake): 10**12})
    monkeypatch.setattr(messages, "get_client", lambda account=None: fake, raising=False)
    result = asyncio.run(
        messages.send_message(chat_id=-1001234567890, message="bypasses ACT policy")
    )
    assert FakeClient.sent == ["bypasses ACT policy"], result


def test_evidence_windows_isatty_true_for_nul_device():
    """On Windows, stdin redirected from the NUL character device reports isatty()==True.

    The CLI's `sys.stdin.isatty()` check therefore does not even prove a console is attached.
    """
    import os
    import subprocess

    if os.name != "nt":
        pytest.skip("Windows-specific evidence")
    out = subprocess.run(
        [sys.executable, "-c", "import sys;print(sys.stdin.isatty())"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
    )
    assert out.stdout.strip() == "True"


def test_exploit_package_level_raw_mcp_exposes_full_upstream_toolset():
    """`telegram_mcp.mcp` (package __getattr__) returns the unpruned server object.

    One import away from a 140-tool stdio server using the operator's session; the ACT
    pruning only happens inside the supported entrypoints.
    """
    code = (
        "import telegram_mcp; m = telegram_mcp.mcp; import telegram_mcp.tools;"
        "names = {t.name for t in m._tool_manager.list_tools()};"
        "print(len(names), 'send_message' in names, 'delete_messages_bulk' in names)"
    )
    from opus55_helpers import run_child

    child = run_child(code)
    assert child.returncode == 0, child.stderr[-2000:]
    count, has_send, has_bulk_delete = child.stdout.split()
    assert int(count) > 100 and has_send == "True" and has_bulk_delete == "True"
