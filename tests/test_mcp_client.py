"""Tests for MCP client integration."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from bub.mcp.client import McpClientManager, load_mcp_configs, parse_mcp_configs


class TestParseConfigs:
    def test_empty(self) -> None:
        assert parse_mcp_configs({}) == []

    def test_stdio_config(self) -> None:
        raw = {
            "filesystem": {
                "command": "npx",
                "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
            }
        }
        configs = parse_mcp_configs(raw)
        assert len(configs) == 1
        assert configs[0].name == "filesystem"
        assert configs[0].command == "npx"
        assert configs[0].args == ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
        assert configs[0].transport == "stdio"

    def test_http_config(self) -> None:
        raw = {"remote": {"url": "http://localhost:8000/mcp"}}
        configs = parse_mcp_configs(raw)
        assert len(configs) == 1
        assert configs[0].name == "remote"
        assert configs[0].url == "http://localhost:8000/mcp"
        assert configs[0].transport == "streamable-http"

    def test_env_config(self) -> None:
        raw = {
            "github": {
                "command": "uvx",
                "args": ["mcp-server-github"],
                "env": {"GITHUB_TOKEN": "test-token"},
            }
        }
        configs = parse_mcp_configs(raw)
        assert configs[0].env == {"GITHUB_TOKEN": "test-token"}

    def test_multiple_servers(self) -> None:
        raw = {
            "server1": {"command": "cmd1"},
            "server2": {"url": "http://localhost:8000/mcp"},
        }
        configs = parse_mcp_configs(raw)
        assert len(configs) == 2
        names = {c.name for c in configs}
        assert names == {"server1", "server2"}

    def test_ignores_non_dict_values(self) -> None:
        raw: dict[str, Any] = {"bad": "string", "good": {"command": "test"}}
        configs = parse_mcp_configs(raw)
        assert len(configs) == 1
        assert configs[0].name == "good"

    def test_non_dict_input(self) -> None:
        assert parse_mcp_configs("not a dict") == []  # type: ignore[arg-type]


class TestLoadMcpConfigs:
    def test_no_files(self, tmp_path: Path) -> None:
        configs = load_mcp_configs(tmp_path, tmp_path / "home")
        assert configs == []

    def test_global_config(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        home.mkdir()
        (home / "mcp_servers.yaml").write_text("myserver:\n  command: echo\n  args: [hello]\n")
        configs = load_mcp_configs(tmp_path, home)
        assert len(configs) == 1
        assert configs[0].name == "myserver"
        assert configs[0].command == "echo"

    def test_project_config(self, tmp_path: Path) -> None:
        bub_dir = tmp_path / ".bub"
        bub_dir.mkdir()
        (bub_dir / "mcp_servers.yaml").write_text("local:\n  url: http://localhost:8000/mcp\n")
        configs = load_mcp_configs(tmp_path, tmp_path / "nonexistent_home")
        assert len(configs) == 1
        assert configs[0].name == "local"

    def test_project_overrides_global(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        home.mkdir()
        (home / "mcp_servers.yaml").write_text("server:\n  command: global_cmd\n")
        bub_dir = tmp_path / ".bub"
        bub_dir.mkdir()
        (bub_dir / "mcp_servers.yaml").write_text("server:\n  command: project_cmd\n")
        configs = load_mcp_configs(tmp_path, home)
        assert len(configs) == 1
        assert configs[0].command == "project_cmd"

    def test_merge_global_and_project(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        home.mkdir()
        (home / "mcp_servers.yaml").write_text("global_server:\n  command: g\n")
        bub_dir = tmp_path / ".bub"
        bub_dir.mkdir()
        (bub_dir / "mcp_servers.yaml").write_text("project_server:\n  command: p\n")
        configs = load_mcp_configs(tmp_path, home)
        assert len(configs) == 2
        names = {c.name for c in configs}
        assert names == {"global_server", "project_server"}


class TestMcpClientManager:
    def test_init_empty(self) -> None:
        manager = McpClientManager([])
        assert manager.all_tools() == []

    def test_available(self) -> None:
        manager = McpClientManager([])
        # Should be True since we installed mcp
        assert manager.available is True

    @pytest.mark.asyncio
    async def test_connect_all_empty(self) -> None:
        manager = McpClientManager([])
        tools = await manager.connect_all()
        assert tools == []

    @pytest.mark.asyncio
    async def test_close_empty(self) -> None:
        manager = McpClientManager([])
        await manager.close()  # should not raise


class TestMcpBridge:
    def test_build_pydantic_model(self) -> None:
        from bub.mcp.bridge import _build_pydantic_model

        schema = {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path"},
                "recursive": {"type": "boolean", "description": "Recurse?"},
            },
            "required": ["path"],
        }
        model = _build_pydantic_model("test_tool", schema)
        instance = model(path="/tmp")
        assert instance.path == "/tmp"  # type: ignore[attr-defined]
        assert instance.recursive is None  # type: ignore[attr-defined]

    def test_build_pydantic_model_with_defaults(self) -> None:
        from bub.mcp.bridge import _build_pydantic_model

        schema = {
            "type": "object",
            "properties": {
                "count": {"type": "integer", "description": "Count", "default": 10},
            },
        }
        model = _build_pydantic_model("test_tool", schema)
        instance = model()
        assert instance.count == 10  # type: ignore[attr-defined]
