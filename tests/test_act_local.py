"""STDLib-only offline integration tests for Windows local-pilot workflow."""

from __future__ import annotations

import json
from pathlib import Path

import act_local
from act_manager import policy


def test_demo_complete_offline():
    result = act_local.demo()
    assert result["ok"] is True
    assert result["real_messages_sent"] == 0
    assert result["blocked_before_approval"] is True
    assert result["replay_blocked"] is True
    assert result["temporary_state_removed"] is True


def test_env_keys_only_does_not_expose_values(monkeypatch, tmp_path):
    # Credential content must never be printed by doctor.
    token = "NEVER_ECHO_THIS_FAKE_SECRET_123"
    (tmp_path / ".env").write_text(
        f"TELEGRAM_API_ID=12345\nTELEGRAM_API_HASH={token}\n"
        "# TELEGRAM_SESSION_NAME=unconfigured\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(act_local, "ROOT", tmp_path)
    monkeypatch.setattr(policy, "CONFIG_PATH", tmp_path / "missing.json")
    result = act_local.doctor()
    assert token not in json.dumps(result)
    assert result["env_field_names_present"]["TELEGRAM_API_HASH"] is True
    assert result["env_field_names_present"]["TELEGRAM_SESSION_NAME"] is False
    assert result["configuration"]["exists"] is False


def test_draft_is_local_pending_and_creates_no_session(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(policy, "CONFIG_PATH", tmp_path / "act_config.json")
    monkeypatch.setattr(policy, "DATA_PATH", tmp_path / "state" / "posts.sqlite3")
    policy.CONFIG_PATH.write_text(
        json.dumps(
            {
                "expected_user_id": 123456,
                "daily_ad_limit": 3,
                "allowed_chats": [
                    {"chat_id": -1001234567890, "label": "Demo", "ads_allowed": True}
                ],
            }
        ),
        encoding="utf-8",
    )
    rc = act_local.main(["draft", "--chat-id", "-1001234567890", "--text", "Offline hello"])
    assert rc == 0
    output = json.loads(capsys.readouterr().out)
    assert output["telegram_called"] is False
    assert output["draft"]["status"] == "draft"
    assert policy.get_post(output["draft"]["id"])["status"] == "draft"
    assert not list(tmp_path.rglob("*.session"))


def test_draft_deny_all_by_default(monkeypatch, tmp_path):
    monkeypatch.setattr(policy, "CONFIG_PATH", tmp_path / "act_config.json")
    monkeypatch.setattr(policy, "DATA_PATH", tmp_path / "db.sqlite3")
    policy.CONFIG_PATH.write_text(json.dumps(policy.config_example()), encoding="utf-8")
    assert act_local.main(["draft", "--chat-id", "-1001234567890", "--text", "No"]) == 2
    assert not (tmp_path / "db.sqlite3").exists()


def test_text_file_utf8_bom(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(policy, "CONFIG_PATH", tmp_path / "act_config.json")
    monkeypatch.setattr(policy, "DATA_PATH", tmp_path / "db.sqlite3")
    policy.CONFIG_PATH.write_text(
        json.dumps(
            {
                "expected_user_id": 4,
                "daily_ad_limit": 3,
                "allowed_chats": [
                    {"chat_id": -1001234567890, "label": "Test", "ads_allowed": True}
                ],
            }
        ),
        encoding="utf-8",
    )
    content = tmp_path / "msg.txt"
    content.write_text("رسالة اختبار", encoding="utf-8-sig")
    assert (
        act_local.main(["draft", "--chat-id", "-1001234567890", "--text-file", str(content)]) == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["draft"]["content"] == "رسالة اختبار"


def test_no_real_telegram_import_on_module_import():
    # act_local itself must not import Telethon or the MCP runtime on import.
    import inspect

    source = inspect.getsource(act_local)
    assert "from telegram_mcp.runtime import" not in source
    assert "from telethon import" not in source


def test_login_is_opt_in_not_on_import():
    import act_login

    assert callable(act_login.main)
    assert callable(act_login._run_login)


def test_windows_connect_script_is_preview_first():
    source = (Path(act_local.__file__).parent / "CODEX_CONNECT_WINDOWS.ps1").read_text(
        encoding="utf-8"
    )
    assert "if (-not $Apply)" in source
    assert "mcp add act-telegram" in source
    assert "act_server.py" in source
