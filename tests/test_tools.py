"""Unit tests for terminal + filesystem tools."""

from __future__ import annotations

import pytest

from errors import SecurityError, ToolError
from tools.filesystem import FilesystemTools, filesystem_tools
from tools.terminal import TerminalTool, run_command

# --- filesystem ---------------------------------------------------------------


@pytest.fixture
def fs(workspace):
    return FilesystemTools(workspace)


async def test_read_file(fs):
    res = await fs.read_file({"path": "README.md"})
    assert "Hello from sample repo" in res["content"][0]["text"]


async def test_read_missing_raises(fs):
    with pytest.raises(ToolError):
        await fs.read_file({"path": "nope.txt"})


async def test_read_dir_raises(fs):
    with pytest.raises(ToolError):
        await fs.read_file({"path": "src"})


async def test_write_then_read(fs):
    r = await fs.write_file({"path": "out.txt", "content": "hi"})
    assert "wrote 2 bytes" in r["content"][0]["text"]
    r = await fs.read_file({"path": "out.txt"})
    assert r["content"][0]["text"] == "hi"


async def test_edit_unique(fs):
    await fs.write_file({"path": "f.txt", "content": "foo bar baz"})
    r = await fs.edit_file({"path": "f.txt", "old_str": "bar", "new_str": "QUX"})
    assert "replaced 1 occurrence" in r["content"][0]["text"]
    assert (fs._root / "f.txt").read_text() == "foo QUX baz"


async def test_edit_not_found(fs):
    await fs.write_file({"path": "f.txt", "content": "x"})
    with pytest.raises(ToolError, match="old_str not found"):
        await fs.edit_file({"path": "f.txt", "old_str": "y", "new_str": "z"})


async def test_edit_non_unique(fs):
    await fs.write_file({"path": "f.txt", "content": "a a a"})
    with pytest.raises(ToolError, match="matches 3"):
        await fs.edit_file({"path": "f.txt", "old_str": "a", "new_str": "b"})


async def test_list_dir(fs):
    r = await fs.list_dir({"path": "."})
    text = r["content"][0]["text"]
    assert "README.md" in text
    assert "src" in text


async def test_search_files(fs):
    await fs.write_file({"path": "a.txt", "content": "needle here\nother"})
    r = await fs.search_files({"pattern": "needle"})
    assert "a.txt:1" in r["content"][0]["text"]


async def test_path_traversal_blocked(fs):
    with pytest.raises(SecurityError, match="traversal"):
        await fs.read_file({"path": "../../../etc/passwd"})


async def test_filesystem_tools_count(workspace):
    tools = filesystem_tools(workspace)
    assert {t.name for t in tools} == {"read_file", "write_file", "edit_file", "list_dir", "search_files"}


# --- terminal -----------------------------------------------------------------


async def test_run_command_success(tmp_path):
    res = await run_command("echo hello world", cwd=str(tmp_path), timeout=10)
    assert res.exit_code == 0
    assert "hello world" in res.stdout
    assert not res.timed_out


async def test_run_command_nonzero_exit(tmp_path):
    res = await run_command("exit 3", cwd=str(tmp_path), timeout=10)
    assert res.exit_code == 3


async def test_run_command_timeout(tmp_path):
    res = await run_command("sleep 5", cwd=str(tmp_path), timeout=0.3)
    assert res.timed_out
    assert res.exit_code == -1


async def test_terminal_tool_missing_command(tmp_path):
    tt = TerminalTool(cwd=str(tmp_path), default_timeout=5, max_timeout=10)
    with pytest.raises(ToolError):
        await tt.run({"command": ""})


async def test_terminal_tool_streams_exit_code(tmp_path):
    tt = TerminalTool(cwd=str(tmp_path), default_timeout=10, max_timeout=20)
    r = await tt.run({"command": "echo hi"})
    text = r["content"][0]["text"]
    assert "[exit 0]" in text
    assert "hi" in text
