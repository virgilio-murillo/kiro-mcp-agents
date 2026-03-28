"""Tests for kiro-agents MCP server.

NOTE: kiro-agents is macOS-only (uses osascript, Ghostty, os.setsid).
These tests run only via `make test-mac`. Linux CI skips this directory.
TODO: Add Linux support — mock _open_ghostty_tab and _open_dashboard.
"""

import importlib


def test_module_imports():
    mod = importlib.import_module("server")
    assert hasattr(mod, "mcp")
    assert hasattr(mod, "main")


def test_tool_count():
    mod = importlib.import_module("server")
    for name in (
        "profound_investigation",
        "investigation_status",
        "investigation_result",
        "stop_investigation",
        "write_correspondence",
        "correspondence_status",
        "generate_report",
    ):
        assert hasattr(mod, name), f"Missing tool: {name}"
