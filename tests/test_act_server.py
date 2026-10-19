"""The server's tool-registry boundary is testable without mcp/telethon installed."""

import pytest
from act_server import ALLOWED, restrict_tools


class Tool:
    def __init__(self, name):
        self.name = name


class Manager:
    def __init__(self, names):
        self.tools = {x: Tool(x) for x in names}

    def list_tools(self):
        return list(self.tools.values())

    def remove_tool(self, name):
        del self.tools[name]


class Server:
    def __init__(self, names):
        self._tool_manager = Manager(names)


def test_only_act_tools_exposed():
    s = Server(
        set(ALLOWED)
        | {
            "send_message",
            "reply_to_message",
            "delete_messages_bulk",
            "list_chats",
            "get_messages",
            "send_scheduled_message",
        }
    )
    assert restrict_tools(s) == ALLOWED
    assert set(s._tool_manager.tools) == ALLOWED


def test_fails_closed_on_missing_safe_tool():
    s = Server({"send_message", "act_policy"})
    with pytest.raises(RuntimeError, match="Missing ACT tools"):
        restrict_tools(s)


def test_tool_names_are_exact():
    assert ALLOWED == {
        "act_policy",
        "act_prepare_post",
        "act_get_post",
        "act_list_posts",
        "act_dispatch_post",
    }
