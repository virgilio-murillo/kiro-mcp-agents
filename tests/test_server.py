"""Tests for kiro-agents MCP server.

NOTE: kiro-agents is macOS-only (uses osascript, Ghostty, os.setsid).
These tests run only via `make test-mac`. Linux CI skips this directory.
TODO: Add Linux support — mock _open_ghostty_tab and _open_dashboard.
"""

import importlib.util
from pathlib import Path

SERVER = Path(__file__).parent.parent / "server.py"


def _load():
    spec = importlib.util.spec_from_file_location("kiro_agents_server", SERVER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_module_imports():
    mod = _load()
    assert hasattr(mod, "mcp")
    assert hasattr(mod, "main")


def test_tool_count():
    mod = _load()
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
