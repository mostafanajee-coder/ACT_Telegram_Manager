"""Explicitly interactive, local-only Telegram personal account login.

No login happens on import. Uses a named Telethon file session instead of
printing a highly sensitive StringSession or storing it in a chat transcript.
"""
from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent


async def _run_login() -> None:
    from dotenv import load_dotenv
    from telethon import TelegramClient

    load_dotenv(ROOT / ".env", override=False)
    api_id = os.environ.get("TELEGRAM_API_ID", "").strip()
    api_hash = os.environ.get("TELEGRAM_API_HASH", "").strip()
    if not api_id.isdecimal() or not re.fullmatch(r"[a-fA-F0-9]{32}", api_hash) or api_hash.casefold() == ("".join(format(i, "x") for i in range(16)) * 2):
        raise RuntimeError("Enter valid Telegram API_ID / API_HASH into the local .env file first.")
    if os.getenv("TELEGRAM_SESSION_STRING") or os.getenv("TELEGRAM_SESSION_STRINGS"):
        raise RuntimeError("This login helper requires a named file session, not a StringSession.")
    name = os.environ.get("TELEGRAM_SESSION_NAME", "act_personal").strip()
    if not name or name in (".", "..") or "/" in name or "\\" in name or ":" in name:
        raise RuntimeError("TELEGRAM_SESSION_NAME must be a simple filename, e.g. act_personal")
    session_path = ROOT / name
    if session_path.is_symlink() or Path(f"{session_path}.session").is_symlink():
        raise RuntimeError("Session path is a symbolic link; refused.")
    print("Interactive Telegram login on THIS PC only. Your verification code stays in this terminal.")
    print("No session string will be printed or sent to ChatGPT/Codex.")
    # Telethon start prompts only if the session is not yet authorized.
    async with TelegramClient(str(session_path), int(api_id), api_hash) as client:
        if not await client.is_user_authorized():
            await client.start()
        me = await client.get_me()
        if me is None or getattr(me, "bot", False):
            raise RuntimeError("A personal Telegram user account is required; bot login refused.")
        print(f"Authorized personal user ID: {me.id}")
        print("Store this numeric ID in act_config.json as expected_user_id.")
        print(f"Session file saved locally: {session_path.name}.session")
        print("Treat that session file and .env as equivalent to account credentials.")


def main() -> int:
    if not sys.stdin.isatty():
        print("DENIED: run interactively in your own local terminal, not through a pipe.", file=sys.stderr)
        return 2
    try:
        asyncio.run(_run_login())
    except (RuntimeError, OSError) as exc:
        print(f"DENIED: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
