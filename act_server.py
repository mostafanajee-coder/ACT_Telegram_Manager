"""SAFE ENTRYPOINT ONLY: narrow, human-approved Telegram MCP server.

Preferred entrypoint for managed use: act_server.py. Legacy main.py,
telegram_mcp.server, and console script are also gated by restrict_tools().
"""
from __future__ import annotations

import os
import sys

ALLOWED = frozenset({
    "act_policy", "act_prepare_post", "act_get_post", "act_list_posts", "act_dispatch_post"
})


def restrict_tools(server) -> set[str]:
    """Fail closed if tool registration differs from the expected safe surface."""
    seen = {t.name for t in server._tool_manager.list_tools()}
    if not ALLOWED.issubset(seen):
        raise RuntimeError(f"Missing ACT tools: {sorted(ALLOWED - seen)}")
    pruned = set(getattr(server, "_act_pruned_tool_names", set()))
    for tool in list(server._tool_manager.list_tools()):
        if tool.name not in ALLOWED:
            pruned.add(tool.name)
            server._tool_manager.remove_tool(tool.name)
    server._act_pruned_tool_names = pruned
    exposed = {t.name for t in server._tool_manager.list_tools()}
    if exposed != ALLOWED:
        raise RuntimeError("Unsafe tools remain exposed; refusing to run")
    rt_mod = sys.modules.get("telegram_mcp.runtime")
    ev_mod = sys.modules.get("telegram_mcp.tools.events")
    if rt_mod is not None and ev_mod is not None and server is getattr(rt_mod, "mcp", None):
        if hasattr(ev_mod, "unregister_incoming_handlers"):
            ev_mod.unregister_incoming_handlers()
        os.environ.pop("TELEGRAM_EVENT_FEED", None)
    return exposed


def main() -> None:
    if len(sys.argv) > 1:
        raise SystemExit("ACT safe edition accepts no CLI arguments; stdio only.")
    if os.getenv("MCP_TRANSPORT", "stdio").lower() != "stdio":
        raise SystemExit("ACT safe edition only permits MCP_TRANSPORT=stdio")
    # Restriction is performed explicitly before the runner starts; do not depend
    # on upstream hint-based filtering (some benign-labelled tools mutate data).
    from telegram_mcp import runner
    from telegram_mcp.runtime import mcp
    import act_manager.tools  # noqa: F401: registers ACT tools
    exposed = restrict_tools(mcp)
    os.environ["TELEGRAM_EXPOSED_TOOLS"] = "all"   # only five ACT tools remain
    os.environ["MCP_TRANSPORT"] = "stdio"
    print(f"ACT safe MCP tools only: {', '.join(sorted(exposed))}", file=sys.stderr)
    runner.main()


if __name__ == "__main__":
    main()
