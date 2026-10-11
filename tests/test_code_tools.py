from __future__ import annotations

import pytest

from cybergym_harness.code_tools import CodeTools


@pytest.mark.asyncio
async def test_code_navigation_tools(tmp_path):
    root = tmp_path / "input"
    root.mkdir()
    (root / "parser.c").write_text(
        "int helper(int x) { return x + 1; }\n"
        "int parse(const char *p) { return helper(p[0]); }\n"
    )
    tools = CodeTools(root)
    assert "parser.c" in tools.list_files()
    assert "parse" in tools.read_file("parser.c", 1, 2)
    assert "parser.c:2" in await tools.search_code(r"parse\(")
    assert "helper" in await tools.symbol_context("helper")


def test_read_rejects_workspace_escape(tmp_path):
    root = tmp_path / "input"
    root.mkdir()
    tools = CodeTools(root)
    with pytest.raises(ValueError, match="escapes"):
        tools.read_file("../secret")

