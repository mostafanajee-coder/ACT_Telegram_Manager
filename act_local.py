"""ACT Telegram Manager local Windows workflow.

Only 'login' (separate file), 'whoami', 'discover', or the MCP server
can contact Telegram. This command's doctor/demo/draft/status paths never do.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from typing import Sequence

from act_manager import policy

ROOT = Path(__file__).resolve().parent


def _print_json(obj: object) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _has_var(key: str, lines: list[str]) -> bool:
    # Intentionally inspect variable NAMES only, never print or parse secrets.
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        if stripped.split("=", 1)[0].strip() == key:
            return True
    return False


def doctor() -> dict:
    env_file = ROOT / ".env"
    try:
        env_lines = env_file.read_text(encoding="utf-8").splitlines() if env_file.exists() else []
    except OSError:
        env_lines = []
    config_ok = False
    config_details: dict = {"exists": policy.CONFIG_PATH.is_file()}
    if config_details["exists"]:
        try:
            cfg = policy.load_config()
            config_ok = True
            config_details.update(
                {
                    "valid": True,
                    "account_id_configured": cfg["expected_user_id"] > 0,
                    "allowed_chat_count": len(cfg["allowed_chats"]),
                    "daily_ad_limit": cfg["daily_ad_limit"],
                    "max_open_drafts": cfg.get("max_open_drafts"),
                }
            )
        except policy.PolicyError as exc:
            config_details.update({"valid": False, "error": str(exc)})
    return {
        "product": "ACT Telegram Manager — Windows local pilot",
        "live_actions_performed": False,
        "python_version": ".".join(str(x) for x in sys.version_info[:3]),
        "python_supported": (3, 10) <= sys.version_info[:2] < (3, 13),
        "dependencies": {
            name: bool(importlib.util.find_spec(name))
            for name in ("mcp", "telethon", "pytest", "dotenv")
        },
        "uv_lock_present": (ROOT / "uv.lock").is_file(),
        "configuration": config_details,
        "env_file_present": env_file.is_file(),
        "env_field_names_present": {
            key: _has_var(key, env_lines)
            for key in ("TELEGRAM_API_ID", "TELEGRAM_API_HASH", "TELEGRAM_SESSION_NAME")
        },
        "session_file_count": len(list(ROOT.glob("*.session"))),
        "limits": [
            "No Telegram login, authorization or transmission tested here.",
            "Full-shell Codex on the same Windows account can bypass app controls.",
            "Reply-labelled messages are currently EXEMPT from advertisement quotas.",
        ],
    }


async def _fake_sender(post: dict) -> int:
    # No Telethon, HTTP, network, files outside the temporary directory.
    return 999001


def demo() -> dict:
    """Exercise the full local policy state machine with disposable dummy data."""
    with tempfile.TemporaryDirectory(prefix="act-telegram-demo-") as tmp:
        root = Path(tmp)
        config = root / "act_config.json"
        state = root / "posts.sqlite3"
        dummy_chat = -1001234567890
        config.write_text(
            json.dumps(
                {
                    "expected_user_id": 123456789,
                    "daily_ad_limit": 3,
                    "max_open_drafts": 20,
                    "allowed_chats": [
                        {"chat_id": dummy_chat, "label": "DEMO ONLY", "ads_allowed": True}
                    ],
                }
            ),
            encoding="utf-8",
        )
        item = policy.prepare(
            chat_id=dummy_chat,
            kind="ad",
            content="DEMO ONLY: never send",
            config_path=config,
            db_path=state,
        )
        assert item["status"] == "draft"
        try:
            asyncio.run(
                policy.execute(item["id"], _fake_sender, config_path=config, db_path=state)
            )
        except policy.PolicyError as exc:
            blocked_before_approval = "approval" in str(exc)
        else:
            blocked_before_approval = False
        assert blocked_before_approval
        policy.approve(item["id"], db_path=state)
        result = asyncio.run(
            policy.execute(item["id"], _fake_sender, config_path=config, db_path=state)
        )
        assert result["status"] == "sent" and result["external_id"] == 999001
        try:
            asyncio.run(
                policy.execute(item["id"], _fake_sender, config_path=config, db_path=state)
            )
        except policy.PolicyError:
            replay_blocked = True
        else:
            replay_blocked = False
        assert replay_blocked
        result = {
            "mode": "OFFLINE SIMULATION — NO TELEGRAM",
            "ok": True,
            "dummy_chat_id": dummy_chat,
            "blocked_before_approval": blocked_before_approval,
            "approved_once": True,
            "fake_dispatch_recorded": True,
            "replay_blocked": replay_blocked,
            "real_messages_sent": 0,
        }
    result["temporary_state_removed"] = not root.exists()
    return result


def _local_draft(args: argparse.Namespace) -> dict:
    content = args.text
    if args.text_file:
        content = Path(args.text_file).read_text(encoding="utf-8-sig")
    if content is None:
        raise policy.PolicyError("Provide --text or --text-file for the local draft.")
    post = policy.prepare(
        chat_id=args.chat_id,
        kind=args.kind,
        content=content,
        reply_to=args.reply_to,
        schedule_at=args.schedule,
    )
    return {
        "ok": True,
        "telegram_called": False,
        "draft": post,
        "next": f".\\.venv\\Scripts\\python.exe act_local.py approve {post['id']}",
    }


def main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    parser = argparse.ArgumentParser(description="ACT Telegram — local-first operator console")
    subs = parser.add_subparsers(dest="command", required=True)
    subs.add_parser("doctor", help="Show local configuration readiness; no Telegram connection")
    subs.add_parser("demo", help="Run disposable local mock workflow; no Telegram connection")
    create = subs.add_parser("draft", help="Prepare a LOCAL draft only; does not send")
    create.add_argument("--chat-id", type=int, required=True)
    create.add_argument("--kind", choices=("ad", "reply"), default="ad")
    txt = create.add_mutually_exclusive_group(required=True)
    txt.add_argument("--text")
    txt.add_argument("--text-file", help="UTF-8 text file")
    create.add_argument("--reply-to", type=int)
    create.add_argument(
        "--schedule", help="ISO-8601 with timezone, e.g. 2026-10-20T14:00:00+03:00"
    )
    for cmd in ("init", "policy", "list", "approve", "cancel", "whoami", "discover"):
        item = subs.add_parser(cmd)
        if cmd in ("approve", "cancel"):
            item.add_argument("draft_id")
    args = parser.parse_args(argv)
    try:
        if args.command == "doctor":
            _print_json(doctor())
        elif args.command == "demo":
            _print_json(demo())
        elif args.command == "draft":
            _print_json(_local_draft(args))
        else:
            # The existing act_cli.py is the single owner of the interactive
            # approval UI. Do not reimplement approval or dispatch here.
            from act_cli import main as cli_main

            forwarded = [args.command]
            if args.command in ("approve", "cancel"):
                forwarded.append(args.draft_id)
            cli_main(forwarded)
    except (policy.PolicyError, OSError, UnicodeError) as exc:
        print(f"DENIED: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
