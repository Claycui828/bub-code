"""Tests for CLI renderer with collapsible panels."""

from io import StringIO

import pytest
from rich.console import Console

from bub.cli.render import CliRenderer, _truncate


def _make_renderer() -> CliRenderer:
    console = Console(file=StringIO(), force_terminal=True, width=120)
    return CliRenderer(console=console)


def _output(renderer: CliRenderer) -> str:
    return renderer.console.file.getvalue()  # type: ignore[union-attr]


class TestPanelTracking:
    def test_assistant_output_creates_panel(self) -> None:
        r = _make_renderer()
        r.assistant_output("hello world")
        assert len(r.panels) == 1
        assert r.panels[0].title == "Assistant"
        assert r.panels[0].body == "hello world"
        assert r.panels[0].folded is False
        assert r.panels[0].kind == "assistant"

    def test_command_output_creates_panel(self) -> None:
        r = _make_renderer()
        r.command_output("ok")
        assert len(r.panels) == 1
        assert r.panels[0].title == "Command"
        assert r.panels[0].style == "green"
        assert r.panels[0].kind == "command"

    def test_error_creates_panel(self) -> None:
        r = _make_renderer()
        r.error("something broke")
        assert len(r.panels) == 1
        assert r.panels[0].title == "Error"
        assert r.panels[0].style == "red"
        assert r.panels[0].kind == "error"

    def test_multiple_outputs_track_indices(self) -> None:
        r = _make_renderer()
        r.assistant_output("first")
        r.command_output("second")
        r.error("third")
        assert len(r.panels) == 3
        assert [p.index for p in r.panels] == [0, 1, 2]

    def test_empty_text_does_not_create_panel(self) -> None:
        r = _make_renderer()
        r.assistant_output("")
        r.assistant_output("   ")
        r.command_output("")
        r.error("")
        assert len(r.panels) == 0


class TestFoldUnfold:
    def test_fold_last(self) -> None:
        r = _make_renderer()
        r.assistant_output("hello")
        r.assistant_output("world")
        r.fold_last()
        assert r.panels[0].folded is False
        assert r.panels[1].folded is True

    def test_unfold_last(self) -> None:
        r = _make_renderer()
        r.assistant_output("hello")
        r.panels[0].folded = True
        r.unfold_last()
        assert r.panels[0].folded is False

    def test_fold_at_index(self) -> None:
        r = _make_renderer()
        r.assistant_output("a")
        r.assistant_output("b")
        r.assistant_output("c")
        r.fold_at(1)
        assert r.panels[0].folded is False
        assert r.panels[1].folded is True
        assert r.panels[2].folded is False

    def test_unfold_at_index(self) -> None:
        r = _make_renderer()
        r.assistant_output("a")
        r.panels[0].folded = True
        r.unfold_at(0)
        assert r.panels[0].folded is False

    def test_fold_at_invalid_index_raises(self) -> None:
        r = _make_renderer()
        r.assistant_output("a")
        with pytest.raises(IndexError):
            r.fold_at(5)

    def test_unfold_at_invalid_index_raises(self) -> None:
        r = _make_renderer()
        with pytest.raises(IndexError):
            r.unfold_at(0)

    def test_fold_empty_panels_shows_info(self) -> None:
        r = _make_renderer()
        r.fold_last()
        output = _output(r)
        assert "No panels" in output

    def test_folded_panel_shows_summary(self) -> None:
        r = _make_renderer()
        r.assistant_output("this is a long response with lots of detail")
        r.fold_last()
        output = _output(r)
        assert "folded" in output


class TestListPanels:
    def test_list_panels_empty(self) -> None:
        r = _make_renderer()
        r.list_panels()
        output = _output(r)
        assert "No output panels" in output

    def test_list_panels_shows_all(self) -> None:
        r = _make_renderer()
        r.assistant_output("first")
        r.command_output("second")
        r.panels[0].folded = True
        r.list_panels()
        output = _output(r)
        assert "[0]" in output
        assert "[1]" in output
        assert "folded" in output
        assert "open" in output


class TestLiveEvents:
    def test_sub_agent_events_create_panels(self) -> None:
        r = _make_renderer()
        r.live_event("sub_agent.start", {"description": "test", "agent_id": "a-1", "agent_type": "explore", "prompt": "do stuff"})
        # start no longer creates a panel — it prints a header line
        assert len(r.panels) == 0
        r.live_event("sub_agent.end", {"agent_id": "a-1", "status": "completed", "result": "done"})
        # end creates a result panel
        assert len(r.panels) == 1
        assert r.panels[0].style == "green"
        assert "a-1" in r.panels[0].title

    def test_sub_agent_tool_events_render(self) -> None:
        r = _make_renderer()
        r.live_event("sub_agent.start", {"agent_id": "a-1", "agent_type": "explore", "description": "test", "prompt": "hi"})
        r.live_event("sub_agent.tool.start", {"agent_id": "a-1", "name": "fs.grep", "args_summary": "pattern='def'"})
        r.live_event("sub_agent.tool.end", {"agent_id": "a-1", "name": "fs.grep", "status": "ok", "elapsed_ms": 50, "output_preview": "3 lines"})
        r.live_event("sub_agent.step.start", {"agent_id": "a-1", "step": 2})
        r.live_event("sub_agent.think.start", {"agent_id": "a-1", "step": 2})
        output = _output(r)
        assert "a-1" in output
        assert "fs.grep" in output

    def test_step_start_does_not_create_panel(self) -> None:
        r = _make_renderer()
        r.live_event("step.start", {"step": 1, "model": "test"})
        assert len(r.panels) == 0

    def test_tool_start_renders(self) -> None:
        r = _make_renderer()
        r.live_event("tool.start", {"name": "bash", "args_summary": "cmd=ls"})
        output = _output(r)
        assert "bash" in output
        assert "cmd=ls" in output

    def test_tool_end_success_renders(self) -> None:
        r = _make_renderer()
        r.live_event("tool.end", {"name": "bash", "status": "ok", "elapsed_ms": 42.5, "output_preview": "3 lines"})
        output = _output(r)
        assert "bash" in output
        assert "42ms" in output
        assert "3 lines" in output

    def test_tool_end_error_renders(self) -> None:
        r = _make_renderer()
        r.live_event("tool.end", {"name": "fs.read", "status": "error", "elapsed_ms": 5, "output_preview": ""})
        output = _output(r)
        assert "fs.read" in output

    def test_tool_error_renders(self) -> None:
        r = _make_renderer()
        r.live_event("tool.error", {"name": "bash", "error": "FileNotFoundError"})
        output = _output(r)
        assert "bash" in output
        assert "FileNotFoundError" in output

    def test_think_start_renders(self) -> None:
        r = _make_renderer()
        r.live_event("think.start", {"step": 1, "model": "qwen"})
        output = _output(r)
        assert "thinking" in output

    def test_tape_anchor_renders(self) -> None:
        r = _make_renderer()
        r.live_event("tape.anchor", {"name": "research-done", "summary": "Found 3 APIs"})
        output = _output(r)
        assert "research-done" in output
        assert "Found 3 APIs" in output


class TestClear:
    def test_clear_resets_panels(self) -> None:
        r = _make_renderer()
        r.assistant_output("hello")
        r.command_output("world")
        assert len(r.panels) == 2
        r.clear()
        assert len(r.panels) == 0


class TestSearchPanels:
    def test_search_finds_matching(self) -> None:
        r = _make_renderer()
        r.assistant_output("hello world")
        r.command_output("goodbye moon")
        r.search_panels("hello")
        output = _output(r)
        assert "1 panel(s)" in output
        assert "[0]" in output

    def test_search_no_match(self) -> None:
        r = _make_renderer()
        r.assistant_output("hello")
        r.search_panels("xyz")
        output = _output(r)
        assert "No panels matching" in output

    def test_search_case_insensitive(self) -> None:
        r = _make_renderer()
        r.assistant_output("Hello World")
        r.search_panels("hello")
        output = _output(r)
        assert "1 panel(s)" in output


class TestTruncate:
    def test_short_text_unchanged(self) -> None:
        assert _truncate("hello", 80) == "hello"

    def test_long_text_truncated(self) -> None:
        result = _truncate("a" * 100, 20)
        assert len(result) == 20
        assert result.endswith("\u2026")

    def test_whitespace_collapsed(self) -> None:
        assert _truncate("hello\n  world\n  foo", 80) == "hello world foo"
