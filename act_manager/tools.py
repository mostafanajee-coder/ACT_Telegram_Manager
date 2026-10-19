"""Only these narrow ACT tools are exposed by act_server.py."""

from __future__ import annotations

from datetime import datetime
import re
from typing import Literal
from mcp.types import ToolAnnotations
from telegram_mcp.runtime import mcp, get_client, ensure_connected, resolve_entity
from act_manager import policy

# id(client) -> [active sends, operator's flood_sleep_threshold]
_flood_holders: dict[int, list] = {}


def _flood_threshold_enter(client) -> None:
    prev = getattr(client, "flood_sleep_threshold", None)
    if prev is None:
        return
    entry = _flood_holders.get(id(client))
    if entry is None:
        _flood_holders[id(client)] = [1, prev]
    else:
        entry[0] += 1
    try:
        client.flood_sleep_threshold = 0
    except Exception:
        pass


def _flood_threshold_exit(client) -> None:
    entry = _flood_holders.get(id(client))
    if entry is None:
        return
    entry[0] -= 1
    if entry[0] > 0:
        return
    del _flood_holders[id(client)]
    try:
        client.flood_sleep_threshold = entry[1]
    except Exception:
        pass


def _error(exc: Exception) -> dict:
    # Return controlled refusals without leaking credentials, paths, or message bodies.
    return {
        "ok": False,
        "error": str(exc) if isinstance(exc, policy.PolicyError) else type(exc).__name__,
    }


_TELEGRAM_USERNAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{4,31}\Z")


def _normalize_private_username(value: str) -> str:
    if not isinstance(value, str):
        raise policy.PolicyError("Private recipient username must be a Telegram username.")
    username = value.strip()
    if username.startswith("@"):
        username = username[1:]
    if not _TELEGRAM_USERNAME_RE.fullmatch(username):
        raise policy.PolicyError("Invalid Telegram username; no draft created.")
    return username


def _verified_private_user_id(
    entity,
    *,
    expected_user_id: int | None,
    expected_username: str | None = None,
    account_user_id: int,
) -> int:
    from telethon.tl.types import User

    if not isinstance(entity, User):
        raise policy.PolicyError(
            "Private recipient must resolve to a Telegram User; no draft created."
        )
    if getattr(entity, "bot", False) or getattr(entity, "deleted", False):
        raise policy.PolicyError("Bots and deleted users cannot be private reply targets.")
    user_id = policy._strict_user_id(getattr(entity, "id", None))
    if user_id == account_user_id:
        raise policy.PolicyError("The account cannot be its own private reply target.")
    if expected_user_id is not None and user_id != expected_user_id:
        raise policy.PolicyError(
            "Resolved Telegram User ID does not match the requested recipient."
        )
    if expected_username is not None:
        usernames = {getattr(entity, "username", None)}
        usernames.update(
            getattr(alias, "username", None)
            for alias in (getattr(entity, "usernames", None) or [])
        )
        normalized = {name.casefold() for name in usernames if isinstance(name, str)}
        if expected_username.casefold() not in normalized:
            raise policy.PolicyError(
                "Resolved Telegram User does not match the requested username."
            )
    return user_id


async def _resolve_private_user(
    client, *, recipient_user_id: int | None, recipient_username: str | None, account_user_id: int
) -> int:
    if (recipient_user_id is None) == (recipient_username is None):
        raise policy.PolicyError("Provide exactly one private recipient User ID or username.")
    if recipient_user_id is not None:
        requested_id = policy._strict_user_id(recipient_user_id)
        lookup = requested_id
    else:
        lookup = _normalize_private_username(recipient_username)
        requested_id = None
    try:
        entity = await client.get_entity(lookup)
    except Exception as exc:
        raise policy.PolicyError(
            f"Could not resolve the specified Telegram User ({type(exc).__name__}); no draft created."
        ) from exc
    return _verified_private_user_id(
        entity,
        expected_user_id=requested_id,
        expected_username=lookup if isinstance(lookup, str) else None,
        account_user_id=account_user_id,
    )


@mcp.tool(annotations=ToolAnnotations(title="ACT Policy", readOnlyHint=True))
async def act_policy() -> dict:
    """View approved chats and current local safety policy; never contacts Telegram."""
    try:
        cfg = policy.load_config()
        return {
            "ok": True,
            "allowed_chats": cfg["allowed_chats"],
            "daily_ad_limit": cfg["daily_ad_limit"],
            "expected_user_id_configured": cfg["expected_user_id"] > 0,
            "approval": "LOCAL TERMINAL ONLY: python act_cli.py approve <draft_id>",
            "telegram_reading": False,
        }
    except Exception as exc:
        return _error(exc)


@mcp.tool(
    annotations=ToolAnnotations(
        title="ACT Prepare Post", readOnlyHint=False, destructiveHint=False
    )
)
async def act_prepare_post(
    chat_id: int | None,
    kind: str,
    content: str,
    reply_to: int | None = None,
    schedule_at: str | None = None,
    destination_type: Literal["group", "private_user"] = "group",
    recipient_user_id: int | None = None,
    recipient_username: str | None = None,
) -> dict:
    """Create a LOCAL draft only. `kind`: ad or reply. NEVER sends to Telegram.

    For `group`, `chat_id` must be a negative numeric ID already in the local
    allowlist. For `private_user`, set `chat_id` to null, use `kind="reply"`,
    provide one of `recipient_user_id` or `recipient_username`, and provide the
    specific incoming `reply_to` message ID. The username is resolved once to a
    Telegram User and only its numeric ID is stored. No conversation history is
    read; dispatch verifies only that one message before sending.

    Replies are never scheduled. For group ads only, `schedule_at` may be
    ISO-8601 with timezone. Every draft needs separate local CLI approval.
    """
    try:
        if destination_type not in ("group", "private_user"):
            raise policy.PolicyError("destination_type must be 'group' or 'private_user'.")
        resolved_user_id = None
        if destination_type == "private_user":
            if chat_id is not None:
                raise policy.PolicyError("private_user drafts must set chat_id to null.")
            if kind != "reply":
                raise policy.PolicyError("private_user destinations support replies only.")
            if type(reply_to) is not int or reply_to <= 0:
                raise policy.PolicyError(
                    "Private replies require one specific incoming message ID."
                )
            if schedule_at is not None:
                raise policy.PolicyError("Private replies cannot be scheduled.")
            if (recipient_user_id is None) == (recipient_username is None):
                raise policy.PolicyError(
                    "Provide exactly one private recipient User ID or username."
                )
            if recipient_user_id is not None:
                policy._strict_user_id(recipient_user_id)
            else:
                recipient_username = _normalize_private_username(recipient_username)
            cfg = policy.load_config(require_identity=True)
            client = get_client()
            await ensure_connected(client)
            me = await client.get_me()
            if (
                me is None
                or getattr(me, "id", None) != cfg["expected_user_id"]
                or getattr(me, "bot", False)
            ):
                raise policy.PolicyError("Account mismatch or bot account; no draft created.")
            resolved_user_id = await _resolve_private_user(
                client,
                recipient_user_id=recipient_user_id,
                recipient_username=recipient_username,
                account_user_id=me.id,
            )
        elif recipient_user_id is not None or recipient_username is not None:
            raise policy.PolicyError("Group drafts cannot include private recipient fields.")
        draft = policy.prepare(
            chat_id=chat_id,
            kind=kind,
            content=content,
            reply_to=reply_to,
            schedule_at=schedule_at,
            destination_type=destination_type,
            recipient_user_id=resolved_user_id,
        )
        return {
            "ok": True,
            "draft": draft,
            "next_step": f"Operator run: python act_cli.py approve {draft['id']}",
        }
    except Exception as exc:
        return _error(exc)


@mcp.tool(annotations=ToolAnnotations(title="ACT Draft Status", readOnlyHint=True))
async def act_get_post(draft_id: str) -> dict:
    """View a local draft or its audit status without contacting Telegram."""
    try:
        return {"ok": True, "post": policy.get_post(draft_id)}
    except Exception as exc:
        return _error(exc)


@mcp.tool(annotations=ToolAnnotations(title="ACT Recent Drafts", readOnlyHint=True))
async def act_list_posts(limit: int = 10) -> dict:
    """Show local ACT drafts and audit status, not Telegram messages."""
    try:
        return {"ok": True, "posts": policy.list_posts(limit=limit)}
    except Exception as exc:
        return _error(exc)


@mcp.tool(
    annotations=ToolAnnotations(
        title="ACT Dispatch Approved Post", destructiveHint=True, idempotentHint=False
    )
)
async def act_dispatch_post(draft_id: str) -> dict:
    """Send ONCE from personal Telegram account only after separate local CLI approval.

    Fail-closed on missing config, unauthorized chat, wrong user, daily ad cap,
    spacing, duplicate or ambiguous failure. Never auto retry.
    """
    try:
        cfg = policy.load_config(require_identity=True)

        async def _preflight(item: dict) -> dict:
            # Phase 1: Complete every read-only check before entering the send
            # phase. Cancellation here is known to precede send_message().
            try:
                client = get_client()
                await ensure_connected(client)
                me = await client.get_me()
            except policy.PolicyError:
                raise
            except Exception as exc:
                raise policy.PolicyError(
                    f"Pre-send Telegram connection/auth check failed ({type(exc).__name__}); no send attempted."
                ) from exc

            if (
                me is None
                or getattr(me, "id", None) != cfg["expected_user_id"]
                or getattr(me, "bot", False)
            ):
                raise policy.PolicyError("Account mismatch or bot account; no send attempted.")

            from telethon import utils

            destination_type = item.get("destination_type") or "group"
            if destination_type == "group":
                try:
                    entity = await resolve_entity(item["chat_id"], client)
                except Exception as exc:
                    raise policy.PolicyError(
                        f"Target chat could not be resolved ({type(exc).__name__}); no send attempted."
                    ) from exc
                try:
                    resolved_peer_id = utils.get_peer_id(entity)
                except Exception as exc:
                    raise policy.PolicyError(
                        "Resolved Telegram entity has invalid peer ID; no send attempted."
                    ) from exc
                if resolved_peer_id != item["chat_id"]:
                    raise policy.PolicyError(
                        "Resolved Telegram destination mismatch; no send attempted."
                    )
                if (
                    getattr(entity, "deactivated", False)
                    or getattr(entity, "left", False)
                    or getattr(entity, "kicked", False)
                ):
                    raise policy.PolicyError(
                        "Cannot post to a deactivated, left, or kicked chat; no send attempted."
                    )
            elif destination_type == "private_user":
                recipient_id = policy._strict_user_id(item.get("recipient_user_id"))
                try:
                    entity = await client.get_entity(recipient_id)
                except Exception as exc:
                    raise policy.PolicyError(
                        f"Private recipient could not be resolved ({type(exc).__name__}); no send attempted."
                    ) from exc
                actual_user_id = _verified_private_user_id(
                    entity, expected_user_id=recipient_id, account_user_id=me.id
                )
                try:
                    resolved_peer_id = utils.get_peer_id(entity)
                except Exception as exc:
                    raise policy.PolicyError(
                        "Resolved private recipient has invalid peer ID; no send attempted."
                    ) from exc
                if resolved_peer_id != actual_user_id:
                    raise policy.PolicyError(
                        "Resolved private recipient mismatch; no send attempted."
                    )
            else:
                raise policy.PolicyError("Unknown destination type; no send attempted.")

            if item["kind"] == "reply":
                get_messages = getattr(client, "get_messages", None)
                if not callable(get_messages):
                    raise policy.PolicyError(
                        "Cannot verify the specified reply target; no send attempted."
                    )
                try:
                    orig_msg = await get_messages(entity, ids=item["reply_to"])
                except Exception as exc:
                    raise policy.PolicyError(
                        f"Could not verify original reply_to message ({type(exc).__name__}); no send attempted."
                    ) from exc
                if isinstance(orig_msg, list):
                    orig_msg = orig_msg[0] if len(orig_msg) == 1 else None
                if orig_msg is None:
                    raise policy.PolicyError(
                        "Original reply_to message not found in target conversation; no send attempted."
                    )
                if destination_type == "private_user":
                    recipient_id = item["recipient_user_id"]
                    if getattr(orig_msg, "id", None) != item["reply_to"]:
                        raise policy.PolicyError(
                            "Private reply target ID mismatch; no send attempted."
                        )
                    if (
                        getattr(orig_msg, "sender_id", None) != recipient_id
                        or getattr(orig_msg, "out", None) is not False
                    ):
                        raise policy.PolicyError(
                            "Private reply target is not an incoming message from the specified user; no send attempted."
                        )
                    peer = getattr(orig_msg, "peer_id", None)
                    try:
                        message_peer_id = utils.get_peer_id(peer) if peer is not None else None
                    except Exception:
                        message_peer_id = None
                    if message_peer_id != recipient_id:
                        raise policy.PolicyError(
                            "Private reply message belongs to a different conversation; no send attempted."
                        )

            opts = {"parse_mode": None, "link_preview": False}
            if item["reply_to"]:
                opts["reply_to"] = item["reply_to"]
            if item["schedule_utc"]:
                opts["schedule"] = datetime.fromisoformat(item["schedule_utc"])

            return {"client": client, "entity": entity, "opts": opts}

        async def _send(item: dict, prepared: dict) -> int | None:
            # Phase 2: Irreversible network send. From this call onward every
            # failure is recorded as uncertain, including PolicyError.
            client = prepared["client"]
            entity = prepared["entity"]
            opts = prepared["opts"]
            # Disable Telethon auto-sleep retry so flood waits fail immediately
            # rather than auto-retrying across timeouts. The client is shared, so
            # overlapping dispatches count holders: only the first saves the
            # operator's value and only the last restores it.
            _flood_threshold_enter(client)
            try:
                result = await client.send_message(entity, item["content"], **opts)
            finally:
                _flood_threshold_exit(client)
            if isinstance(result, list):
                return getattr(result[0], "id", None) if result else None
            return getattr(result, "id", None)

        post = await policy.execute(draft_id, _send, preflight=_preflight)
        return {"ok": True, "post": post}
    except Exception as exc:
        return _error(exc)
