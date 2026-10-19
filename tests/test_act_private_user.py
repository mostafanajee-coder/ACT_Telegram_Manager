"""Offline tests for group reply policy and explicit private-user replies."""

from __future__ import annotations

import asyncio
import builtins
import io
import json
import sqlite3
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from telethon.tl.types import PeerUser, User

import act_cli
from act_manager import policy
import act_manager.tools as act_tools

GROUP_ID = -1001000000001
ACCOUNT_ID = 7000001
USER_ID = 7000002
OTHER_USER_ID = 7000003
REPLY_ID = 771
NOW = datetime(2026, 10, 18, 9, 0, tzinfo=timezone.utc)


def _user(user_id: int, *, username: str | None = None, bot: bool = False) -> User:
    return User(
        id=user_id,
        first_name=f"User {user_id}",
        username=username,
        bot=bot,
        access_hash=user_id * 13,
    )


def _config(path, *, ads_allowed: bool = False) -> None:
    path.write_text(
        json.dumps(
            {
                "expected_user_id": ACCOUNT_ID,
                "daily_ad_limit": 3,
                "allowed_chats": [
                    {
                        "chat_id": GROUP_ID,
                        "label": "Synthetic Reply Group",
                        "ads_allowed": ads_allowed,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


@pytest.fixture
def act_paths(tmp_path, monkeypatch):
    config = tmp_path / "act_config.json"
    db = tmp_path / "act_data" / "posts.sqlite3"
    _config(config)
    monkeypatch.setattr(policy, "CONFIG_PATH", config)
    monkeypatch.setattr(policy, "DATA_PATH", db)
    return config, db


class FakeTelegramClient:
    def __init__(self, *, target=None, message=None, me=None):
        self.me = me or _user(ACCOUNT_ID, username="owner")
        self.message = message
        self.by_id = {}
        self.by_username = {}
        self.entity_calls = []
        self.message_calls = []
        self.sent = []
        self.flood_sleep_threshold = 60
        self.history_calls = 0
        if target is not None:
            self.by_id[target.id] = target
            if target.username:
                self.by_username[target.username.casefold()] = target

    def is_connected(self):
        return True

    async def get_me(self):
        return self.me

    async def get_entity(self, target):
        self.entity_calls.append(target)
        if isinstance(target, str):
            return self.by_username.get(target.casefold())
        return self.by_id.get(target)

    async def get_messages(self, entity, ids=None):
        self.message_calls.append((getattr(entity, "id", None), ids))
        return self.message

    async def iter_dialogs(self, *args, **kwargs):
        self.history_calls += 1
        raise AssertionError("private reply path must not read dialog history")

    async def send_message(self, entity, content, **options):
        self.sent.append((getattr(entity, "id", None), content, options))
        return SimpleNamespace(id=len(self.sent))


def _install_client(monkeypatch, client):
    monkeypatch.setattr(act_tools, "get_client", lambda: client)

    async def already_connected(_client):
        return None

    monkeypatch.setattr(act_tools, "ensure_connected", already_connected)


def _prepare_private(
    client,
    monkeypatch,
    *,
    username="alice",
    user_id=None,
    content="Thanks for contacting me.",
    reply_to=REPLY_ID,
):
    _install_client(monkeypatch, client)
    return asyncio.run(
        act_tools.act_prepare_post(
            chat_id=None,
            kind="reply",
            content=content,
            reply_to=reply_to,
            destination_type="private_user",
            recipient_user_id=user_id,
            recipient_username=username,
        )
    )


def _approve(post, db_path):
    fingerprint = policy.compute_fingerprint(
        post["chat_id"],
        post["kind"],
        post["reply_to"],
        post["content"],
        destination_type=post["destination_type"],
        recipient_user_id=post["recipient_user_id"],
    )
    integrity = policy.compute_integrity_hash(
        post["id"],
        post["chat_id"],
        post["kind"],
        post["reply_to"],
        post["content"],
        post["schedule_utc"],
        destination_type=post["destination_type"],
        recipient_user_id=post["recipient_user_id"],
    )
    return policy.approve(
        post["id"],
        db_path=db_path,
        expected_fingerprint=fingerprint,
        expected_integrity=integrity,
    )


def _incoming_message(*, user_id=USER_ID, message_id=REPLY_ID, out=False, peer_id=None):
    return SimpleNamespace(
        id=message_id,
        sender_id=user_id,
        out=out,
        peer_id=PeerUser(user_id=peer_id if peer_id is not None else user_id),
    )


def test_requested_group_is_the_only_destination_and_is_reply_only(act_paths):
    config_path, db_path = act_paths
    config = policy.load_config(config_path)
    assert config["daily_ad_limit"] == 3
    assert config["allowed_chats"] == [
        {"chat_id": GROUP_ID, "label": "Synthetic Reply Group", "ads_allowed": False}
    ]
    reply = policy.prepare(
        chat_id=GROUP_ID,
        kind="reply",
        content="Answer",
        reply_to=4,
        config_path=config_path,
        db_path=db_path,
    )
    assert reply["destination_type"] == "group"
    with pytest.raises(policy.PolicyError, match="Advertising not authorized"):
        policy.prepare(
            chat_id=GROUP_ID, kind="ad", content="Promo", config_path=config_path, db_path=db_path
        )
    with pytest.raises(policy.PolicyError, match="allowlist"):
        policy.prepare(
            chat_id=-1001505872606,
            kind="reply",
            content="No",
            reply_to=5,
            config_path=config_path,
            db_path=db_path,
        )
    with pytest.raises(policy.PolicyError):
        policy._strict_chat_id(USER_ID)


def test_private_user_is_per_draft_and_stores_fixed_numeric_id(act_paths, monkeypatch):
    config_path, _ = act_paths
    target = _user(USER_ID, username="alice")
    client = FakeTelegramClient(target=target)
    result = _prepare_private(client, monkeypatch)
    assert result["ok"] is True
    post = result["draft"]
    assert post["destination_type"] == "private_user"
    assert post["chat_id"] is None
    assert post["recipient_user_id"] == USER_ID
    assert type(post["recipient_user_id"]) is int
    assert "recipient_username" not in post
    assert client.entity_calls == ["alice"]
    assert client.message_calls == []
    assert client.history_calls == 0
    assert json.loads(config_path.read_text(encoding="utf-8"))["allowed_chats"] == [
        {"chat_id": GROUP_ID, "label": "Synthetic Reply Group", "ads_allowed": False}
    ]


def test_explicit_numeric_private_user_id_is_resolved_and_accepted(act_paths, monkeypatch):
    target = _user(USER_ID, username="alice")
    client = FakeTelegramClient(target=target)
    result = _prepare_private(client, monkeypatch, username=None, user_id=USER_ID)
    assert result["ok"] is True
    assert result["draft"]["recipient_user_id"] == USER_ID
    assert type(result["draft"]["recipient_user_id"]) is int
    assert client.entity_calls == [USER_ID]


def test_private_target_must_be_exactly_one_user_id_or_username(act_paths, monkeypatch):
    client = FakeTelegramClient(target=_user(USER_ID, username="alice"))
    _install_client(monkeypatch, client)
    missing = asyncio.run(
        act_tools.act_prepare_post(
            chat_id=None,
            kind="reply",
            content="Reply",
            reply_to=REPLY_ID,
            destination_type="private_user",
        )
    )
    both = asyncio.run(
        act_tools.act_prepare_post(
            chat_id=None,
            kind="reply",
            content="Reply",
            reply_to=REPLY_ID,
            destination_type="private_user",
            recipient_user_id=USER_ID,
            recipient_username="alice",
        )
    )
    assert missing["ok"] is False and both["ok"] is False
    assert client.entity_calls == []


def test_invalid_username_is_rejected_before_user_lookup(act_paths, monkeypatch):
    client = FakeTelegramClient(target=_user(USER_ID, username="alice"))
    result = _prepare_private(client, monkeypatch, username="https://t.me/alice")
    assert result["ok"] is False
    assert client.entity_calls == []


def test_username_that_resolves_to_a_different_identity_is_refused(act_paths, monkeypatch):
    client = FakeTelegramClient()
    client.by_username["alice"] = _user(USER_ID, username="bob")
    result = _prepare_private(client, monkeypatch, username="alice")
    assert result["ok"] is False
    assert client.entity_calls == ["alice"]
    assert not act_paths[1].exists()


@pytest.mark.parametrize("target", [object(), _user(USER_ID, username="alice", bot=True)])
def test_private_recipient_must_be_a_non_bot_user(act_paths, monkeypatch, target):
    client = FakeTelegramClient()
    client.by_username["alice"] = target
    result = _prepare_private(client, monkeypatch)
    assert result["ok"] is False
    assert "draft" not in result
    assert not act_paths[1].exists()


def test_numeric_private_id_must_match_resolved_telegram_user(act_paths, monkeypatch):
    client = FakeTelegramClient(target=_user(OTHER_USER_ID, username="other"))
    client.by_id[USER_ID] = _user(OTHER_USER_ID, username="other")
    result = _prepare_private(client, monkeypatch, username=None, user_id=USER_ID)
    assert result["ok"] is False
    assert client.entity_calls == [USER_ID]
    assert not act_paths[1].exists()


def test_private_target_resolution_requires_the_configured_personal_account(
    act_paths, monkeypatch
):
    client = FakeTelegramClient(target=_user(USER_ID, username="alice"), me=_user(ACCOUNT_ID + 1))
    result = _prepare_private(client, monkeypatch)
    assert result["ok"] is False
    assert client.entity_calls == []
    assert not act_paths[1].exists()


def test_private_recipient_cannot_be_the_configured_account_itself(act_paths, monkeypatch):
    client = FakeTelegramClient(target=_user(ACCOUNT_ID, username="owner"))
    result = _prepare_private(client, monkeypatch, username=None, user_id=ACCOUNT_ID)
    assert result["ok"] is False
    assert "draft" not in result
    assert not act_paths[1].exists()


def test_group_peer_id_is_not_accepted_as_a_private_user_id(act_paths, monkeypatch):
    client = FakeTelegramClient(target=_user(USER_ID, username="alice"))
    result = _prepare_private(client, monkeypatch, username=None, user_id=GROUP_ID)
    assert result["ok"] is False
    assert client.entity_calls == []
    assert not act_paths[1].exists()


def test_private_dispatch_checks_only_the_selected_incoming_message(act_paths, monkeypatch):
    target = _user(USER_ID, username="alice")
    client = FakeTelegramClient(target=target, message=_incoming_message())
    result = _prepare_private(client, monkeypatch)
    post = result["draft"]
    _approve(post, act_paths[1])
    sent = asyncio.run(act_tools.act_dispatch_post(post["id"]))
    assert sent["ok"] is True
    assert client.message_calls == [(USER_ID, REPLY_ID)]
    assert client.history_calls == 0
    assert len(client.sent) == 1
    assert client.sent[0][0] == USER_ID
    assert client.sent[0][2]["reply_to"] == REPLY_ID


@pytest.mark.parametrize(
    "message",
    [
        None,
        _incoming_message(user_id=OTHER_USER_ID),
        _incoming_message(out=True),
        _incoming_message(peer_id=OTHER_USER_ID),
        _incoming_message(message_id=REPLY_ID + 1),
    ],
)
def test_private_dispatch_refuses_missing_or_wrong_message(act_paths, monkeypatch, message):
    target = _user(USER_ID, username="alice")
    client = FakeTelegramClient(target=target, message=message)
    post = _prepare_private(client, monkeypatch)["draft"]
    _approve(post, act_paths[1])
    refused = asyncio.run(act_tools.act_dispatch_post(post["id"]))
    assert refused["ok"] is False
    assert client.sent == []
    assert policy.get_post(post["id"], db_path=act_paths[1])["status"] == "approved"


def test_recipient_tampering_after_approval_is_refused_before_resolution(act_paths, monkeypatch):
    target = _user(USER_ID, username="alice")
    client = FakeTelegramClient(target=target, message=_incoming_message())
    post = _prepare_private(client, monkeypatch)["draft"]
    _approve(post, act_paths[1])
    with sqlite3.connect(act_paths[1]) as db:
        db.execute("UPDATE posts SET recipient_user_id=? WHERE id=?", (OTHER_USER_ID, post["id"]))
    calls_before = list(client.entity_calls)
    refused = asyncio.run(act_tools.act_dispatch_post(post["id"]))
    assert refused["ok"] is False
    assert client.entity_calls == calls_before
    assert client.sent == []


def test_private_content_tampering_after_approval_is_refused(act_paths, monkeypatch):
    target = _user(USER_ID, username="alice")
    client = FakeTelegramClient(target=target, message=_incoming_message())
    post = _prepare_private(client, monkeypatch)["draft"]
    _approve(post, act_paths[1])
    with sqlite3.connect(act_paths[1]) as db:
        db.execute("UPDATE posts SET content=? WHERE id=?", ("Changed after approval", post["id"]))
    calls_before = list(client.entity_calls)
    refused = asyncio.run(act_tools.act_dispatch_post(post["id"]))
    assert refused["ok"] is False
    assert client.entity_calls == calls_before
    assert client.sent == []


def test_duplicate_private_reply_is_sent_once(act_paths, monkeypatch):
    target = _user(USER_ID, username="alice")
    client = FakeTelegramClient(target=target, message=_incoming_message())
    first = _prepare_private(client, monkeypatch, content="Same reply")["draft"]
    _approve(first, act_paths[1])
    assert asyncio.run(act_tools.act_dispatch_post(first["id"]))["ok"] is True
    second = _prepare_private(client, monkeypatch, content="Same reply")["draft"]
    _approve(second, act_paths[1])
    assert asyncio.run(act_tools.act_dispatch_post(second["id"]))["ok"] is False
    assert len(client.sent) == 1


def test_cancelled_private_reply_cannot_be_dispatched(act_paths, monkeypatch):
    client = FakeTelegramClient(target=_user(USER_ID, username="alice"))
    post = _prepare_private(client, monkeypatch)["draft"]
    policy.cancel(post["id"], db_path=act_paths[1])
    result = asyncio.run(act_tools.act_dispatch_post(post["id"]))
    assert result["ok"] is False
    assert client.sent == []


def test_private_destination_rejects_ads_and_scheduling(act_paths):
    common = {
        "chat_id": None,
        "destination_type": "private_user",
        "recipient_user_id": USER_ID,
        "content": "One reply",
        "config_path": act_paths[0],
        "db_path": act_paths[1],
    }
    with pytest.raises(policy.PolicyError, match="replies only"):
        policy.prepare(**common, kind="ad")
    with pytest.raises(policy.PolicyError, match="cannot be scheduled"):
        policy.prepare(
            **common, kind="reply", reply_to=REPLY_ID, schedule_at="2026-10-19T12:00:00+03:00"
        )


def test_private_cli_approval_phrase_binds_recipient_and_draft(act_paths, monkeypatch):
    post = policy.prepare(
        chat_id=None,
        kind="reply",
        content="One private response",
        reply_to=REPLY_ID,
        destination_type="private_user",
        recipient_user_id=USER_ID,
        config_path=act_paths[0],
        db_path=act_paths[1],
    )
    monkeypatch.setattr(policy, "CONFIG_PATH", act_paths[0])
    monkeypatch.setattr(policy, "DATA_PATH", act_paths[1])
    fake_stdin = io.StringIO()
    fake_stdin.isatty = lambda: True
    monkeypatch.setattr(sys, "stdin", fake_stdin)
    monkeypatch.setattr(
        builtins, "input", lambda _prompt="": f"APPROVE PRIVATE {OTHER_USER_ID} {post['id']}"
    )
    with pytest.raises(SystemExit):
        act_cli.main(["approve", post["id"]])
    assert policy.get_post(post["id"], db_path=act_paths[1])["status"] == "draft"
    monkeypatch.setattr(
        builtins, "input", lambda _prompt="": f"APPROVE PRIVATE {USER_ID} {post['id']}"
    )
    act_cli.main(["approve", post["id"]])
    assert policy.get_post(post["id"], db_path=act_paths[1])["status"] == "approved"


def test_no_ads_group_reply_requires_reply_specific_cli_confirmation(
    act_paths, monkeypatch, capsys
):
    post = policy.prepare(
        chat_id=GROUP_ID,
        kind="reply",
        content="Answer the question",
        reply_to=19,
        config_path=act_paths[0],
        db_path=act_paths[1],
    )
    monkeypatch.setattr(policy, "CONFIG_PATH", act_paths[0])
    monkeypatch.setattr(policy, "DATA_PATH", act_paths[1])
    fake_stdin = io.StringIO()
    fake_stdin.isatty = lambda: True
    monkeypatch.setattr(sys, "stdin", fake_stdin)
    monkeypatch.setattr(builtins, "input", lambda _prompt="": f"APPROVE {post['id']}")
    with pytest.raises(SystemExit):
        act_cli.main(["approve", post["id"]])
    assert policy.get_post(post["id"], db_path=act_paths[1])["status"] == "draft"
    output = capsys.readouterr().out
    assert "ads_allowed=False" in output
    monkeypatch.setattr(builtins, "input", lambda _prompt="": f"APPROVE REPLY {post['id']}")
    act_cli.main(["approve", post["id"]])
    assert policy.get_post(post["id"], db_path=act_paths[1])["status"] == "approved"
