from pathlib import Path

import pytest

from bub.channels.cli import CliChannel


class _DummyRuntime:
    def __init__(self) -> None:
        self.workspace = Path.cwd()

        class _Settings:
            model = "openrouter:test"

            @staticmethod
            def resolve_home() -> Path:
                return Path.cwd()

        self.settings = _Settings()

    def get_session(self, _session_id: str):
        class _Tape:
            @staticmethod
            def info():
                class _Info:
                    entries = 0
                    anchors = 0
                    last_anchor = None

                return _Info()

        class _ModelRunner:
            def set_live_callback(self, cb):
                pass

            def inject_message(self, text):
                pass

            def request_pause(self):
                pass

            def request_resume(self):
                pass

            def request_step(self):
                pass

        class _Session:
            tape = _Tape()
            tool_view = type("_ToolView", (), {"all_tools": staticmethod(lambda: [])})()
            model_runner = _ModelRunner()

        return _Session()


def test_normalize_input_keeps_agent_mode_text() -> None:
    cli = CliChannel(_DummyRuntime())  # type: ignore[arg-type]
    cli._mode = "agent"
    assert cli._normalize_input("echo hi") == "echo hi"


def test_normalize_input_adds_shell_prefix_in_shell_mode() -> None:
    cli = CliChannel(_DummyRuntime())  # type: ignore[arg-type]
    cli._mode = "shell"
    assert cli._normalize_input("echo hi") == ", echo hi"


def test_normalize_input_keeps_explicit_prefixes_in_shell_mode() -> None:
    cli = CliChannel(_DummyRuntime())  # type: ignore[arg-type]
    cli._mode = "shell"
    assert cli._normalize_input(",help") == ",help"
    assert cli._normalize_input(",ls -la") == ",ls -la"
    assert cli._normalize_input(", ls -la") == ", ls -la"


def test_cli_channel_disables_debounce() -> None:
    cli = CliChannel(_DummyRuntime())  # type: ignore[arg-type]
    assert cli.debounce_enabled is False


def test_cli_channel_does_not_wrap_prompt() -> None:
    cli = CliChannel(_DummyRuntime())  # type: ignore[arg-type]
    assert cli.format_prompt("plain prompt") == "plain prompt"


def test_command_registry_has_builtin_commands() -> None:
    cli = CliChannel(_DummyRuntime())  # type: ignore[arg-type]
    registry = cli.command_registry
    assert registry.get("fold") is not None
    assert registry.get("unfold") is not None
    assert registry.get("panels") is not None
    assert registry.get("stop") is not None
    assert registry.get("help") is not None
    assert registry.get("status") is not None
    assert registry.get("pause") is not None
    assert registry.get("resume") is not None
    assert registry.get("clear") is not None
    assert registry.get("search") is not None
    assert registry.get("inject") is not None
    assert registry.get("context") is not None
    assert registry.get("step") is not None
    assert registry.get("tasks") is not None


def test_command_registry_aliases() -> None:
    cli = CliChannel(_DummyRuntime())  # type: ignore[arg-type]
    registry = cli.command_registry
    # f -> fold, u -> unfold, p -> panels, s -> status, h -> help
    assert registry.get("f") is not None
    assert registry.get("f").name == "fold"
    assert registry.get("u").name == "unfold"
    assert registry.get("p").name == "panels"
    assert registry.get("s").name == "status"
    assert registry.get("h").name == "help"
    assert registry.get("?").name == "help"
    assert registry.get("ctx").name == "context"
    assert registry.get("t").name == "tasks"


def test_is_command_recognizes_slash_commands() -> None:
    cli = CliChannel(_DummyRuntime())  # type: ignore[arg-type]
    registry = cli.command_registry
    assert registry.is_command("/fold") is True
    assert registry.is_command("/fold 3") is True
    assert registry.is_command("/f") is True
    assert registry.is_command("/help") is True
    assert registry.is_command("/status") is True
    assert registry.is_command("/unknown_cmd") is False
    assert registry.is_command("hello world") is False
    assert registry.is_command(",help") is False


def test_help_text_includes_categories() -> None:
    cli = CliChannel(_DummyRuntime())  # type: ignore[arg-type]
    text = cli.command_registry.help_text()
    assert "DISPLAY" in text
    assert "CONTROL" in text
    assert "CONTEXT" in text
    assert "/fold" in text
    assert "/stop" in text
    assert "/help" in text


class TestSlashCommands:
    def _cli(self) -> CliChannel:
        return CliChannel(_DummyRuntime())  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_non_command_returns_false(self) -> None:
        cli = self._cli()
        assert await cli._handle_slash_command("hello world") is False
        assert await cli._handle_slash_command(",help") is False

    @pytest.mark.asyncio
    async def test_slash_command_returns_true(self) -> None:
        cli = self._cli()
        cli._renderer.assistant_output("hello")
        assert await cli._handle_slash_command("/fold") is True
        assert cli._renderer.panels[0].folded is True

    @pytest.mark.asyncio
    async def test_slash_fold_at_index(self) -> None:
        cli = self._cli()
        cli._renderer.assistant_output("a")
        cli._renderer.assistant_output("b")
        assert await cli._handle_slash_command("/fold 0") is True
        assert cli._renderer.panels[0].folded is True
        assert cli._renderer.panels[1].folded is False

    @pytest.mark.asyncio
    async def test_slash_panels(self) -> None:
        cli = self._cli()
        cli._renderer.assistant_output("hello")
        assert await cli._handle_slash_command("/panels") is True
        assert await cli._handle_slash_command("/p") is True

    @pytest.mark.asyncio
    async def test_slash_clear(self) -> None:
        cli = self._cli()
        cli._renderer.assistant_output("hello")
        assert len(cli._renderer.panels) == 1
        assert await cli._handle_slash_command("/clear") is True
        assert len(cli._renderer.panels) == 0

    @pytest.mark.asyncio
    async def test_invalid_fold_index_handled(self) -> None:
        cli = self._cli()
        cli._renderer.assistant_output("a")
        # Should not raise, just prints info
        assert await cli._handle_slash_command("/fold 99") is True
        assert await cli._handle_slash_command("/fold abc") is True


class TestAtReferences:
    def _cli(self, workspace: Path | None = None) -> CliChannel:
        runtime = _DummyRuntime()
        if workspace:
            runtime.workspace = workspace
        return CliChannel(runtime)  # type: ignore[arg-type]

    def test_expand_file_reference(self, tmp_path: Path) -> None:
        (tmp_path / "hello.txt").write_text("world")
        cli = self._cli(workspace=tmp_path)
        result = cli._expand_at_references(f"read @{tmp_path / 'hello.txt'}")
        assert "<file" in result
        assert "world" in result

    def test_expand_relative_file(self, tmp_path: Path) -> None:
        (tmp_path / "foo.py").write_text("print('hi')")
        cli = self._cli(workspace=tmp_path)
        result = cli._expand_at_references("check @foo.py")
        assert "<file" in result
        assert "print('hi')" in result

    def test_expand_directory_reference(self, tmp_path: Path) -> None:
        sub = tmp_path / "src"
        sub.mkdir()
        (sub / "a.py").write_text("")
        (sub / "b.py").write_text("")
        cli = self._cli(workspace=tmp_path)
        result = cli._expand_at_references(f"list @{sub}")
        assert "<directory" in result
        assert "a.py" in result
        assert "b.py" in result

    def test_nonexistent_path_kept_as_is(self) -> None:
        cli = self._cli()
        text = "look at @nonexistent_file_xyz"
        result = cli._expand_at_references(text)
        assert "@nonexistent_file_xyz" in result

    def test_no_at_references(self) -> None:
        cli = self._cli()
        text = "just a normal message"
        assert cli._expand_at_references(text) == text

    def test_email_not_expanded(self) -> None:
        cli = self._cli()
        # email-like patterns don't match because @ must be preceded by space/start
        text = "send to user@example.com"
        result = cli._expand_at_references(text)
        # The regex won't match "user@example.com" because "user" prefix
        # is part of the word — @example.com would match but example.com
        # won't be a valid path, so it stays as-is
        assert "example.com" in result
