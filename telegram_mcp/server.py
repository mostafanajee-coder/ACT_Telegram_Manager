"""High-level server module and exports for Telegram MCP.

Provides access to the FastMCP instance, HTTP ASGI application wrapped with
protocol negotiation for MCP 2026-07-28, and transport serving routines.
In the ACT edition, all helper functions enforce the 5-tool ACT allowlist.
"""

from __future__ import annotations

from typing import Any

from telegram_mcp.transport import (
    DEFAULT_PROTOCOL_VERSION,
    LATEST_PROTOCOL_VERSION,
    SUPPORTED_PROTOCOL_VERSIONS,
    create_streamable_http_app as _create_streamable_http_app,
    install_protocol_compatibility,
    run_stdio_async as _run_stdio_async,
    run_streamable_http_async as _run_streamable_http_async,
)
from telegram_mcp.runtime import mcp as _mcp
from telegram_mcp import runner as _runner
from act_server import restrict_tools
import act_manager.tools  # noqa: F401: ensure ACT tools are registered

# Ensure protocol compatibility is installed upon server module import
install_protocol_compatibility()

__all__ = [
    "DEFAULT_PROTOCOL_VERSION",
    "LATEST_PROTOCOL_VERSION",
    "SUPPORTED_PROTOCOL_VERSIONS",
    "create_streamable_http_app",
    "get_http_app",
    "get_server",
    "main",
    "run_stdio_async",
    "run_streamable_http_async",
    "serve",
]


def _enforce_act_surface(server: Any | None = None) -> Any:
    target = _mcp if server is None else server
    if not hasattr(target, "_tool_manager"):
        raise RuntimeError("Cannot verify ACT tool registry; refusing to start server.")
    restrict_tools(target)
    return target


def create_streamable_http_app(mcp_instance: Any | None = None) -> Any:
    """Create an ACT-restricted HTTP ASGI app for an explicit host to mount."""
    return _create_streamable_http_app(_enforce_act_surface(mcp_instance))


async def run_stdio_async(mcp_instance: Any | None = None) -> None:
    """Run stdio only after pruning the supplied server to the ACT tool set."""
    await _run_stdio_async(_enforce_act_surface(mcp_instance))


async def run_streamable_http_async(mcp_instance: Any | None = None) -> None:
    """Run the supplied server over HTTP only after ACT tool pruning."""
    await _run_streamable_http_async(_enforce_act_surface(mcp_instance))


def get_server() -> Any:
    """Return the underlying FastMCP server instance restricted to ACT tools."""
    return _enforce_act_surface()


def get_http_app() -> Any:
    """Return the Starlette ASGI application with protocol negotiation and ACT tool restriction."""
    return create_streamable_http_app(_enforce_act_surface())


async def serve(transport: str = "stdio") -> None:
    """Serve the Telegram MCP server over stdio only in the ACT edition."""
    if transport.lower() != "stdio":
        raise SystemExit("ACT edition supports stdio only.")
    _enforce_act_surface()
    await _runner._serve("stdio")


def main() -> None:
    """Server CLI entrypoint."""
    _runner.main()


if __name__ == "__main__":
    main()
