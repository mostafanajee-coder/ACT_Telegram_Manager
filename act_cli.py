"""Local-only, operator-facing approvals. Never an MCP tool."""
from __future__ import annotations
import argparse
import asyncio
import json
import sys
from act_manager import policy


def _ensure_utf8_stdio() -> None:
    """Ensure UTF-8 output on Windows consoles for Arabic text and group labels."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


async def _local_telegram_info(command: str) -> None:
    """Print metadata locally to the operator; never expose through MCP."""
    from telegram_mcp.runtime import get_client
    client = get_client()
    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise policy.PolicyError("Telegram session not authorized; complete upstream login first.")
        me = await client.get_me()
        if me is None or getattr(me, "bot", False):
            raise policy.PolicyError("Personal user account required; bot accounts are not permitted.")
        print("Your Telegram user ID:", me.id)
        if command == "discover":
            print("Groups/channels (numeric IDs) -- no message content is fetched:")
            from telethon import utils
            async for dialog in client.iter_dialogs(limit=200):
                if dialog.is_group or dialog.is_channel:
                    safe_name = policy.sanitize_terminal_text((dialog.name or "")[:100])
                    print(utils.get_peer_id(dialog.entity), "-", safe_name)
    finally:
        await client.disconnect()


def main(argv: list[str] | None = None) -> None:
    _ensure_utf8_stdio()
    parser = argparse.ArgumentParser(description="ACT local approval and audit console")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="Create empty deny-all config (no Telegram login)")
    commands.add_parser("whoami", help="Show your own Telegram numeric account ID locally (requires login)")
    commands.add_parser("discover", help="List group and channel IDs locally (requires login)")
    commands.add_parser("policy", help="Show configured destinations and limits")
    commands.add_parser("list", help="List local drafts and audit records")
    approve = commands.add_parser("approve", help="Human-only interactive approval")
    approve.add_argument("draft_id")
    cancel = commands.add_parser("cancel", help="Cancel a draft or unexecuted approval")
    cancel.add_argument("draft_id")
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            if policy.CONFIG_PATH.is_symlink():
                raise policy.PolicyError("Configuration path must not be a symbolic link.")
            try:
                with policy.CONFIG_PATH.open("x", encoding="utf-8") as fh:
                    fh.write(json.dumps(policy.config_example(), ensure_ascii=False, indent=2) + "\n")
            except FileExistsError:
                raise policy.PolicyError(
                    "Configuration already exists. Edit it manually; refusing to overwrite."
                ) from None
            print(f"Created DENY-ALL config: {policy.CONFIG_PATH}")
            print("Add specific Telegram numeric chat IDs and your own numeric expected_user_id.")
            return
        if args.command in ("whoami", "discover"):
            asyncio.run(_local_telegram_info(args.command))
            return
        if args.command == "policy":
            print(json.dumps(policy.load_config(), ensure_ascii=False, indent=2))
            return
        if args.command == "list":
            print(json.dumps(policy.list_posts(), ensure_ascii=False, indent=2))
            return
        if args.command == "cancel":
            print(json.dumps(policy.cancel(args.draft_id), ensure_ascii=False, indent=2))
            return
        if args.command == "approve":
            post = policy.get_post(args.draft_id)
            if post["status"] != "draft":
                raise policy.PolicyError("Only an unapproved draft can be approved.")
            cfg = policy.load_config()
            destination_type = post.get("destination_type") or "group"
            recipient_user_id = post.get("recipient_user_id")
            if destination_type == "group":
                if recipient_user_id is not None:
                    raise policy.PolicyError("Group draft contains a private recipient.")
                chat = policy.find_chat(cfg, post["chat_id"], post["kind"])
                safe_label = policy.sanitize_terminal_text(chat["label"])
                destination = f"{safe_label} ID: {post['chat_id']}"
                approval_phrase = f"APPROVE {post['id']}"
                restricted_reply = post["kind"] == "reply" and not chat.get("ads_allowed", False)
                if restricted_reply:
                    approval_phrase = f"APPROVE REPLY {post['id']}"
            elif destination_type == "private_user":
                recipient_user_id = policy._strict_user_id(recipient_user_id)
                if (
                    post.get("chat_id") is not None
                    or post["kind"] != "reply"
                    or type(post.get("reply_to")) is not int
                    or post["reply_to"] <= 0
                    or post.get("schedule_utc") is not None
                ):
                    raise policy.PolicyError("Invalid private reply draft.")
                destination = f"Private Telegram User ID: {recipient_user_id}"
                approval_phrase = f"APPROVE PRIVATE {recipient_user_id} {post['id']}"
                restricted_reply = False
            else:
                raise policy.PolicyError("Unknown draft destination type.")
            expected_fp = policy.compute_fingerprint(
                post["chat_id"], post["kind"], post["reply_to"], post["content"],
                destination_type=destination_type, recipient_user_id=recipient_user_id,
            )
            # Bind approval to destination identity, reply target, content and schedule.
            expected_ih = policy.compute_integrity_hash(
                post["id"], post["chat_id"], post["kind"], post["reply_to"],
                post["content"], post["schedule_utc"],
                destination_type=destination_type, recipient_user_id=recipient_user_id,
            )
            safe_content = policy.sanitize_terminal_text(post["content"])
            print("\nLOCAL OPERATOR APPROVAL -- PREVIEW")
            print("Destination:", destination)
            print("Type:", post["kind"], "Reply to:", post["reply_to"])
            if restricted_reply:
                print(
                    f"WARNING: Group '{safe_label}' has ads_allowed=False. "
                    f"Confirm this is a genuine, non-promotional reply to message #{post['reply_to']}."
                )
            elif destination_type == "private_user":
                print(
                    "WARNING: This is a one-time private reply to the specified User ID. "
                    "Confirm the displayed message and recipient; no other recipient is authorized."
                )
            print("Schedule (UTC):", post["schedule_utc"] or "immediate")
            print("Status:", post["status"])
            print("--------- TEXT ---------")
            print(safe_content)
            print("--------- END ---------")
            if not sys.stdin.isatty():
                raise policy.PolicyError("Interactive terminal REQUIRED. No piped approval.")
            answer = input(f"Type exactly '{approval_phrase}' to authorize this single message: ")
            if answer != approval_phrase:
                raise policy.PolicyError("Approval aborted; no changes.")
            print(
                json.dumps(
                    policy.approve(
                        post["id"],
                        expected_fingerprint=expected_fp,
                        expected_integrity=expected_ih,
                    ),
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return
    except (policy.PolicyError, OSError) as exc:
        print(f"DENIED: {exc}", file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
