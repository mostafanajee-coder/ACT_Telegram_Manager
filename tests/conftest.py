"""Shared pytest setup for import-time Telegram configuration."""

import os

import pytest

os.environ.setdefault("TELEGRAM_API_ID", "12345")
os.environ.setdefault("TELEGRAM_API_HASH", "dummy_hash")
os.environ.setdefault("TELEGRAM_SESSION_NAME", "test_session")


@pytest.fixture
def transcript_cache_dir(tmp_path, monkeypatch):
    """Isolated, per-test SQLite transcript cache directory."""
    d = tmp_path / "transcripts"
    monkeypatch.setenv("TELEGRAM_TRANSCRIPT_CACHE_DIR", str(d))
    return d


@pytest.fixture(autouse=True)
def _clear_expected_username(monkeypatch):
    """Keep a developer's TELEGRAM_EXPECTED_USERNAME* (.env) away from fake clients."""
    for key in list(os.environ):
        if key.startswith("TELEGRAM_EXPECTED_USERNAME"):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def _preserve_mcp_tool_registry():
    """Restore runtime.mcp tool registry and EXTENSION_ALLOWLISTS after tests."""
    import sys

    rt = sys.modules.get("telegram_mcp.runtime")
    saved_tools = None
    saved_pruned = None
    saved_ext = None
    if rt is not None and hasattr(rt.mcp, "_tool_manager"):
        saved_tools = dict(rt.mcp._tool_manager._tools)
        saved_pruned = getattr(rt.mcp, "_act_pruned_tool_names", None)
        saved_ext = dict(getattr(rt, "EXTENSION_ALLOWLISTS", {}))
    yield
    rt = sys.modules.get("telegram_mcp.runtime")
    if rt is not None and saved_tools is not None and hasattr(rt.mcp, "_tool_manager"):
        rt.mcp._tool_manager._tools.clear()
        rt.mcp._tool_manager._tools.update(saved_tools)
        if saved_pruned is None:
            if hasattr(rt.mcp, "_act_pruned_tool_names"):
                delattr(rt.mcp, "_act_pruned_tool_names")
        else:
            rt.mcp._act_pruned_tool_names = set(saved_pruned)
        if saved_ext is not None:
            rt.EXTENSION_ALLOWLISTS = dict(saved_ext)
