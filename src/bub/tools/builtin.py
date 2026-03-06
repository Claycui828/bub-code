






"""Built-in tool definitions."""

from __future__ import annotations

import asyncio
import fnmatch
import json
import os
import re as re_module
import shutil
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from urllib import parse as urllib_parse

from apscheduler.jobstores.base import ConflictingIdError, JobLookupError
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger
from pydantic import BaseModel, Field
from republic import ToolContext

from bub.tape.service import TapeService
from bub.tools.agent import register_agent_tools
from bub.tools.registry import ToolGuidance, ToolRegistry
from bub.tools.task import register_task_tools

if TYPE_CHECKING:
    from bub.app.runtime import AppRuntime

DEFAULT_OLLAMA_WEB_API_BASE = "https://ollama.com/api"
WEB_REQUEST_TIMEOUT_SECONDS = 20
SUBPROCESS_TIMEOUT_SECONDS = 30
MAX_FETCH_BYTES = 1_000_000
WEB_USER_AGENT = "bub-web-tools/1.0"
SESSION_ID_ENV_VAR = "BUB_SESSION_ID"


class BashInput(BaseModel):
    cmd: str = Field(..., description="The bash command to execute. Use && to chain dependent commands. Quote paths with spaces.")
    cwd: str | None = Field(default=None, description="Working directory override. Defaults to workspace root if not specified.")
    timeout_seconds: int = Field(
        default=SUBPROCESS_TIMEOUT_SECONDS,
        ge=1,
        description="Maximum seconds before the command is killed. Default 30s. Increase for long builds or downloads.",
    )


class ReadInput(BaseModel):
    path: str = Field(..., description="Absolute or workspace-relative file path to read")
    offset: int = Field(default=0, ge=0, description="Line number to start reading from (0-based). Use with limit for large files.")
    limit: int | None = Field(default=None, ge=1, description="Maximum number of lines to return. Omit to read entire file.")


class WriteInput(BaseModel):
    path: str = Field(..., description="Absolute or workspace-relative file path. Parent directories are created automatically.")
    content: str = Field(..., description="Complete file content to write as UTF-8 text. This replaces the entire file.")


class EditInput(BaseModel):
    path: str = Field(..., description="Absolute or workspace-relative path to the file to edit. File must exist.")
    old: str = Field(..., description="Exact text to find in the file. Must match verbatim including whitespace and indentation.")
    new: str = Field(..., description="Replacement text. All occurrences of old text (from start_line onward) are replaced.")
    start_line: int = Field(
        default=0,
        ge=0,
        description="Line number to start searching from (0-based). Use when old text appears multiple times to narrow scope.",
    )


class GrepInput(BaseModel):
    pattern: str = Field(..., description="Regular expression pattern (Python re / ripgrep syntax). E.g. 'def\\s+\\w+', 'TODO|FIXME'.")
    path: str = Field(default=".", description="File or directory to search in. Relative to workspace root.")
    include: str | None = Field(default=None, description="Glob pattern to filter files. E.g. '*.py', '*.{ts,tsx}', 'src/**/*.js'.")
    max_results: int = Field(default=50, ge=1, le=200, description="Maximum matching lines to return. Increase for broad searches.")
    context_lines: int = Field(default=0, ge=0, le=5, description="Lines of context to show before and after each match (like grep -C).")


class GlobInput(BaseModel):
    pattern: str = Field(..., description="Glob pattern to match files. E.g. '**/*.py', 'src/**/*.ts', '**/test_*.py', '*.md'.")
    path: str = Field(default=".", description="Root directory to search from. Relative to workspace root.")
    max_results: int = Field(default=100, ge=1, le=500, description="Maximum number of file paths to return.")


class FetchInput(BaseModel):
    url: str = Field(..., description="Full URL to fetch (http/https). URLs without scheme default to https.")


class SearchInput(BaseModel):
    query: str = Field(..., description="Search query string. Be specific for better results.")
    max_results: int = Field(default=5, ge=1, le=10, description="Number of search results to return.")


class HandoffInput(BaseModel):
    name: str | None = Field(default=None, description="Anchor name for this checkpoint. Defaults to 'handoff'. Use descriptive names like 'phase-1-bootstrap'.")
    summary: str | None = Field(default=None, description="Self-contained summary of what was accomplished. A reader with no prior context must understand this.")
    next_steps: str | None = Field(default=None, description="Clear, actionable next steps for the continued work after this checkpoint.")
    files_modified: list[str] | None = Field(default=None, description="List of file paths that were created or modified in this phase.")
    decisions: list[str] | None = Field(default=None, description="Key architectural or design decisions made, with brief rationale.")


class ToolNameInput(BaseModel):
    name: str = Field(..., description="Tool name")


class TapeSearchInput(BaseModel):
    query: str = Field(..., description="Query")
    limit: int = Field(default=20, ge=1)


class TapeResetInput(BaseModel):
    archive: bool = Field(default=False)


class EmptyInput(BaseModel):
    pass


class ScheduleAddInput(BaseModel):
    after_seconds: int | None = Field(None, description="If set, schedule to run after this many seconds from now")
    interval_seconds: int | None = Field(None, description="If set, repeat at this interval")
    cron: str | None = Field(
        None, description="If set, run with cron expression in crontab format: minute hour day month day_of_week"
    )
    message: str = Field(..., description="Reminder message to send")


class ScheduleRemoveInput(BaseModel):
    job_id: str = Field(..., description="Job id to remove")


def _resolve_path(workspace: Path, raw: str) -> Path:
    path = Path(raw).expanduser()
    if path.is_absolute():
        return path
    return workspace / path


def _normalize_url(raw_url: str) -> str | None:
    normalized = raw_url.strip()
    if not normalized:
        return None

    parsed = urllib_parse.urlparse(normalized)
    if parsed.scheme and parsed.netloc:
        if parsed.scheme not in {"http", "https"}:
            return None
        return normalized

    if parsed.scheme == "" and parsed.netloc == "" and parsed.path:
        with_scheme = f"https://{normalized}"
        parsed = urllib_parse.urlparse(with_scheme)
        if parsed.netloc:
            return with_scheme

    return None


def _normalize_api_base(raw_api_base: str) -> str | None:
    normalized = raw_api_base.strip().rstrip("/")
    if not normalized:
        return None

    parsed = urllib_parse.urlparse(normalized)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return normalized
    return None


def _format_search_results(results: list[object]) -> str:
    lines: list[str] = []
    for idx, item in enumerate(results, start=1):
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "(untitled)")
        url = str(item.get("url") or "")
        content = str(item.get("content") or "")
        lines.append(f"{idx}. {title}")
        if url:
            lines.append(f"   {url}")
        if content:
            lines.append(f"   {content}")
    return "\n".join(lines) if lines else "none"


def _grep_ripgrep(rg: str, params: GrepInput, search_path: Path) -> str:
    """Run ripgrep for fs.grep."""
    import subprocess

    cmd = [rg, "--no-heading", "--line-number", "--color=never", f"--max-count={params.max_results}"]
    if params.context_lines > 0:
        cmd.append(f"-C{params.context_lines}")
    if params.include:
        cmd.extend(["--glob", params.include])
    cmd.extend([params.pattern, str(search_path)])
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return "(search timed out)"
    if result.returncode == 1:
        return "(no matches)"
    if result.returncode > 1:
        return f"error: {result.stderr.strip()}"
    lines = result.stdout.strip().splitlines()
    if len(lines) > params.max_results:
        lines = lines[: params.max_results]
        lines.append(f"... (truncated at {params.max_results} results)")
    return "\n".join(lines) if lines else "(no matches)"


def _grep_python(params: GrepInput, search_path: Path) -> str:
    """Pure-Python fallback for fs.grep."""
    try:
        pattern = re_module.compile(params.pattern)
    except re_module.error as exc:
        raise RuntimeError(f"invalid regex: {exc}") from exc

    results: list[str] = []

    def _search_file(fpath: Path) -> None:
        try:
            text = fpath.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return
        for line_no, line in enumerate(text.splitlines(), 1):
            if len(results) >= params.max_results:
                return
            if pattern.search(line):
                rel = str(fpath.relative_to(search_path)) if fpath.is_relative_to(search_path) else str(fpath)
                results.append(f"{rel}:{line_no}:{line}")

    if search_path.is_file():
        if params.include is None or fnmatch.fnmatch(search_path.name, params.include):
            _search_file(search_path)
    elif search_path.is_dir():
        for root, _dirs, files in os.walk(search_path):
            root_path = Path(root)
            # Skip hidden dirs and common noise
            if any(part.startswith(".") for part in root_path.relative_to(search_path).parts):
                continue
            for fname in sorted(files):
                if params.include and not fnmatch.fnmatch(fname, params.include):
                    continue
                _search_file(root_path / fname)
                if len(results) >= params.max_results:
                    break
            if len(results) >= params.max_results:
                break
    else:
        raise RuntimeError(f"path not found: {search_path}")

    if not results:
        return "(no matches)"
    return "\n".join(results)


async def _web_search_exa(api_key: str, params: SearchInput) -> str:
    """Search via Exa API (https://exa.ai)."""
    import aiohttp

    payload = {
        "query": params.query,
        "numResults": params.max_results,
        "type": "auto",
        "contents": {"text": {"maxCharacters": 300}},
    }
    try:
        async with (
            aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=WEB_REQUEST_TIMEOUT_SECONDS)) as session,
            session.post(
                "https://api.exa.ai/search",
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "x-api-key": api_key,
                    "User-Agent": WEB_USER_AGENT,
                },
            ) as response,
        ):
            data = await response.json()
    except aiohttp.ClientError as exc:
        return f"exa error: {exc!s}"

    results = data.get("results")
    if not isinstance(results, list) or not results:
        return "none"
    lines: list[str] = []
    for idx, item in enumerate(results, start=1):
        title = str(item.get("title") or "(untitled)")
        url = str(item.get("url") or "")
        text = str(item.get("text") or "")
        lines.append(f"{idx}. {title}")
        if url:
            lines.append(f"   {url}")
        if text:
            lines.append(f"   {text}")
    return "\n".join(lines) if lines else "none"


async def _web_search_brave(api_key: str, params: SearchInput) -> str:
    """Search via Brave Search API (https://brave.com/search/api/)."""
    import aiohttp

    query = urllib_parse.quote_plus(params.query)
    url = f"https://api.search.brave.com/res/v1/web/search?q={query}&count={params.max_results}"
    try:
        async with (
            aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=WEB_REQUEST_TIMEOUT_SECONDS)) as session,
            session.get(
                url,
                headers={
                    "Accept": "application/json",
                    "Accept-Encoding": "gzip",
                    "X-Subscription-Token": api_key,
                    "User-Agent": WEB_USER_AGENT,
                },
            ) as response,
        ):
            data = await response.json()
    except aiohttp.ClientError as exc:
        return f"brave error: {exc!s}"

    web_results = data.get("web", {}).get("results")
    if not isinstance(web_results, list) or not web_results:
        return "none"
    return _format_search_results(
        [{"title": r.get("title"), "url": r.get("url"), "content": r.get("description")} for r in web_results]
    )


async def _web_search_ollama(runtime: AppRuntime, params: SearchInput) -> str:
    """Search via Ollama web search endpoint."""
    import aiohttp

    api_key = runtime.settings.ollama_api_key
    if not api_key:
        return "error: ollama api key is not configured"

    api_base = _normalize_api_base(runtime.settings.ollama_api_base or DEFAULT_OLLAMA_WEB_API_BASE)
    if not api_base:
        return "error: invalid ollama api base url"

    endpoint = f"{api_base}/web_search"
    payload = {"query": params.query, "max_results": params.max_results}
    try:
        async with (
            aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=WEB_REQUEST_TIMEOUT_SECONDS)) as session,
            session.post(
                endpoint,
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                    "User-Agent": WEB_USER_AGENT,
                },
            ) as response,
        ):
            response_body = await response.text()
    except aiohttp.ClientError as exc:
        return f"ollama error: {exc!s}"

    try:
        data = json.loads(response_body)
    except json.JSONDecodeError as exc:
        return f"error: invalid json response: {exc!s}"

    results = data.get("results")
    if not isinstance(results, list) or not results:
        return "none"
    return _format_search_results(results)


def register_builtin_tools(
    registry: ToolRegistry,
    *,
    workspace: Path,
    tape: TapeService,
    runtime: AppRuntime,
) -> None:
    """Register built-in tools and internal commands."""
    from bub.tools.schedule import run_scheduled_reminder

    register = registry.register

    @register(
        name="bash",
        short_description="Execute a shell command in the workspace directory",
        model=BashInput,
        context=True,
        always_expand=True,
        guidance=ToolGuidance(
            when_to_use="Git operations, running tests (pytest/jest/make test), package management (uv/pip/npm), docker, make, and multi-step shell pipelines.",
            when_not_to="File reads (use fs.read), content search (use fs.grep), file pattern matching (use fs.glob), file writes (use fs.write/fs.edit). Never use bash grep/cat/find/sed when a dedicated tool exists.",
            examples="bash cmd='git status' | bash cmd='uv run pytest tests/' | bash cmd='docker build -t app .' | bash cmd='git diff HEAD~1'",
            constraints="Non-zero exit code raises RuntimeError. Default timeout 30s — increase for builds. The workspace .env file is auto-loaded. Do NOT use sleep for delays — use schedule.add instead.",
        ),
    )
    async def run_bash(params: BashInput, context: ToolContext) -> str:
        """Execute a bash command in the workspace directory.

        Usage:
        - Prefer dedicated tools over bash: fs.read over cat, fs.write over echo/cat heredoc,
          fs.edit over sed/awk, fs.grep over grep/rg, fs.glob over find/ls.
        - Use bash for: git, make, docker, npm/pip/uv, running tests, and any command not covered by dedicated tools.
        - Always quote file paths containing spaces with double quotes.
        - Use && to chain dependent commands; use ; when order matters but failure is OK.
        - The workspace .env file is automatically loaded into the environment.
        - Non-zero exit code raises RuntimeError with stderr/stdout as the message.
        - Default timeout is 30s. Increase timeout_seconds for long builds or downloads.
        """
        import dotenv

        cwd = params.cwd or str(workspace)
        executable = shutil.which("bash") or "bash"
        env = dict(os.environ)
        workspace_env = workspace / ".env"
        if workspace_env.is_file():
            env.update((k, v) for k, v in dotenv.dotenv_values(workspace_env).items() if v is not None)
        env[SESSION_ID_ENV_VAR] = context.state.get("session_id", "")
        completed = await asyncio.create_subprocess_exec(
            executable,
            "-lc",
            params.cmd,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        async with asyncio.timeout(params.timeout_seconds):
            stdout_bytes, stderr_bytes = await completed.communicate()
        stdout_text = (stdout_bytes or b"").decode("utf-8", errors="replace").strip()
        stderr_text = (stderr_bytes or b"").decode("utf-8", errors="replace").strip()
        if completed.returncode != 0:
            message = stderr_text or stdout_text or f"exit={completed.returncode}"
            raise RuntimeError(f"exit={completed.returncode}: {message}")
        return stdout_text or "(no output)"

    @register(
        name="fs.read",
        short_description="Read a file's text content with optional line range",
        model=ReadInput,
        always_expand=True,
        guidance=ToolGuidance(
            when_to_use="Inspecting source code, config files, logs. Use offset/limit for large files.",
            when_not_to="Searching across many files (use fs.grep). Finding files by name (use fs.glob).",
        ),
    )
    def fs_read(params: ReadInput) -> str:
        """Read UTF-8 text content from a file.

        Usage:
        - Returns file content as plain text lines.
        - Use offset and limit for large files (e.g. offset=100, limit=50 to read lines 100-149).
        - Omit offset/limit to read the entire file — recommended for most source files.
        - Read files before editing them to understand the current state.
        - You can read multiple files in parallel by issuing multiple tool calls.
        - For searching across many files, use fs.grep instead.
        """
        file_path = _resolve_path(workspace, params.path)
        text = file_path.read_text(encoding="utf-8")
        lines = text.splitlines()
        start = min(params.offset, len(lines))
        end = len(lines) if params.limit is None else min(len(lines), start + params.limit)
        return "\n".join(lines[start:end])

    @register(
        name="fs.write",
        short_description="Create or overwrite a file with UTF-8 text",
        model=WriteInput,
        always_expand=True,
        guidance=ToolGuidance(
            when_to_use="Creating new files or completely rewriting existing files.",
            when_not_to="Making targeted edits to existing files (use fs.edit instead).",
            constraints="Overwrites existing content completely. Creates parent directories automatically.",
        ),
    )
    def fs_write(params: WriteInput) -> str:
        """Create a new file or completely overwrite an existing file with UTF-8 text.

        Usage:
        - Creates parent directories automatically if they don't exist.
        - OVERWRITES the entire file — all previous content is lost.
        - For targeted edits to existing files, use fs.edit instead.
        - Prefer fs.edit when only changing a few lines in a large file.
        - Use this tool for: creating new files, or complete rewrites where most content changes.
        """
        file_path = _resolve_path(workspace, params.path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(params.content, encoding="utf-8")
        return f"wrote: {file_path}"

    @register(
        name="fs.edit",
        short_description="Find and replace text in an existing file",
        model=EditInput,
        always_expand=True,
        guidance=ToolGuidance(
            when_to_use="Targeted modifications to existing files — changing function signatures, fixing bugs, updating config values.",
            when_not_to="Creating new files (use fs.write). The old text must exist exactly as specified.",
            constraints="Replaces ALL occurrences of old text from start_line onward. Use start_line to narrow the search scope when old text appears multiple times.",
        ),
    )
    def fs_edit(params: EditInput) -> str:
        """Find and replace text in an existing file.

        Usage:
        - The old text must match EXACTLY — including whitespace, indentation, and line breaks.
        - Replaces ALL occurrences of old text from start_line onward.
        - Use start_line to narrow the search when old text appears multiple times in the file.
        - Always read the file first (fs.read) to get the exact text to match.
        - For creating new files or complete rewrites, use fs.write instead.
        - The file must already exist; raises RuntimeError if not found.
        """
        file_path = _resolve_path(workspace, params.path)
        if not file_path.is_file():
            raise RuntimeError(f"file not found: {file_path}")
        text = file_path.read_text(encoding="utf-8")
        lines = text.splitlines()
        start_line = min(params.start_line, len(lines))
        prev, to_replace = "\n".join(lines[:start_line]), "\n".join(lines[start_line:])
        if params.old not in to_replace:
            raise RuntimeError(f"'{params.old}' not found in {file_path} from line {start_line}")
        new_text = to_replace.replace(params.old, params.new)
        file_path.write_text(f"{prev}\n{new_text}", encoding="utf-8")
        return f"edited: {file_path}"

    @register(
        name="fs.grep",
        short_description="Search file contents by regex pattern",
        model=GrepInput,
        always_expand=True,
        guidance=ToolGuidance(
            when_to_use="Searching for patterns across files: function definitions, imports, string literals, error messages, TODOs.",
            when_not_to="Finding files by name (use fs.glob). Reading a known file (use fs.read). Running shell commands (use bash).",
            examples="fs.grep pattern='def\\s+handle_' include='*.py' | fs.grep pattern='TODO|FIXME' | fs.grep pattern='import.*json' path='src/'",
            constraints="Returns up to max_results matching lines. Binary files are skipped. Uses ripgrep if available, otherwise Python re. Hidden directories (.*) are skipped.",
        ),
    )
    def fs_grep(params: GrepInput) -> str:
        """Search file contents for lines matching a regex pattern.

        Usage:
        - Supports full regex syntax: 'def\\s+\\w+', 'TODO|FIXME', 'import.*json'.
        - Uses ripgrep (rg) when available for speed; falls back to Python re module.
        - Filter files with include glob: '*.py', '*.{ts,tsx}'.
        - Binary files and hidden directories (.*) are automatically skipped.
        - Use context_lines to see surrounding code for each match.
        - For finding files by name pattern, use fs.glob instead.
        - For reading a specific known file, use fs.read instead.
        """
        search_path = _resolve_path(workspace, params.path)
        rg = shutil.which("rg")
        if rg:
            return _grep_ripgrep(rg, params, search_path)
        return _grep_python(params, search_path)

    @register(
        name="fs.glob",
        short_description="Find files matching a glob pattern",
        model=GlobInput,
        always_expand=True,
        guidance=ToolGuidance(
            when_to_use="Finding files by name pattern: discovering project structure, locating config files, finding test files.",
            when_not_to="Searching file contents (use fs.grep). Reading a known file (use fs.read). Listing directory contents (use bash ls).",
            examples="fs.glob pattern='**/*.py' | fs.glob pattern='src/**/*.ts' | fs.glob pattern='**/test_*.py' | fs.glob pattern='*.yaml'",
            constraints="Returns sorted file paths relative to the search root. Uses pathlib.Path.glob(). Max 500 results.",
        ),
    )
    def fs_glob(params: GlobInput) -> str:
        """Find files matching a glob pattern, sorted by path.

        Usage:
        - Common patterns: '**/*.py' (all Python files), 'src/**/*.ts' (TypeScript in src),
          '**/test_*.py' (test files), '*.md' (markdown in root).
        - Returns file paths relative to the search root directory.
        - Results are sorted alphabetically by path.
        - For searching file CONTENTS, use fs.grep instead.
        - For reading a specific file, use fs.read instead.
        """
        root = _resolve_path(workspace, params.path)
        if not root.is_dir():
            raise RuntimeError(f"not a directory: {root}")
        matches = sorted(root.glob(params.pattern))
        files = [p for p in matches if p.is_file()]
        if not files:
            return "(no matches)"
        truncated = len(files) > params.max_results
        files = files[: params.max_results]
        lines = []
        for p in files:
            try:
                lines.append(str(p.relative_to(root)))
            except ValueError:
                lines.append(str(p))
        if truncated:
            lines.append(f"... (truncated, showing {params.max_results} of {len(matches)} matches)")
        return "\n".join(lines)

    @register(
        name="web.fetch",
        short_description="Fetch a URL and return content as text",
        model=FetchInput,
        always_expand=False,
        guidance=ToolGuidance(
            when_to_use="Reading web pages, API documentation, or fetching remote resources.",
            when_not_to="Searching the web for information (use web.search first to find URLs).",
            constraints="20s timeout. 1MB max response. HTML is converted to markdown-like text.",
        ),
    )
    async def web_fetch_default(params: FetchInput) -> str:
        """Fetch a URL and return its content as text.

        Usage:
        - HTML content is converted to markdown-like text for readability.
        - 20-second timeout; responses larger than 1MB are truncated.
        - HTTP URLs are automatically upgraded to HTTPS.
        - Use web.search first to find relevant URLs, then web.fetch to read them.
        - For local files, use fs.read instead.
        """
        import aiohttp

        url = _normalize_url(params.url)
        if not url:
            return "error: invalid url"

        try:
            async with (
                aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=WEB_REQUEST_TIMEOUT_SECONDS)) as session,
                session.get(url, headers={"User-Agent": WEB_USER_AGENT, "Accept": "text/markdown"}) as response,
            ):
                content_bytes = await response.content.read(MAX_FETCH_BYTES + 1)
                truncated = len(content_bytes) > MAX_FETCH_BYTES
                content = content_bytes[:MAX_FETCH_BYTES].decode("utf-8", errors="replace")
        except aiohttp.ClientError as exc:
            return f"HTTP error: {exc!s}"
        if not content:
            return "error: empty response body"
        if truncated:
            return f"{content}\n\n[truncated: response exceeded byte limit]"
        return content

    @register(
        name="schedule.add",
        short_description="Schedule a future or recurring reminder message",
        model=ScheduleAddInput,
        context=True,
        guidance=ToolGuidance(
            when_to_use="Setting reminders, polling at intervals, scheduling periodic checks or notifications.",
            when_not_to="Immediate actions (just do them). Waiting for a bash command (use timeout_seconds instead).",
            constraints="Exactly one of after_seconds, interval_seconds, or cron must be set. Messages are delivered to the current session.",
        ),
    )
    def schedule_add(params: ScheduleAddInput, context: ToolContext) -> str:
        """Schedule a reminder message to be sent to the current session in the future.

        Scheduling options (specify exactly one):
        - after_seconds: run once after N seconds from now (one-shot timer)
        - interval_seconds: run repeatedly at this interval (periodic)
        - cron: crontab format 'minute hour day month day_of_week' (e.g. '*/5 * * * *' for every 5 min)

        The message will be delivered as a user message to the current session when triggered.
        """
        job_id = str(uuid.uuid4())[:8]
        if params.after_seconds is not None:
            trigger = DateTrigger(run_date=datetime.now(UTC) + timedelta(seconds=params.after_seconds))
        elif params.interval_seconds is not None:
            trigger = IntervalTrigger(seconds=params.interval_seconds)
        else:
            try:
                trigger = CronTrigger.from_crontab(params.cron)
            except ValueError as exc:
                raise RuntimeError(f"invalid cron expression: {params.cron}") from exc

        try:
            job = runtime.scheduler.add_job(
                run_scheduled_reminder,
                trigger=trigger,
                id=job_id,
                kwargs={
                    "message": params.message,
                    "session_id": context.state.get("session_id", ""),
                    "workspace": str(runtime.workspace),
                },
                coalesce=True,
                max_instances=1,
            )
        except ConflictingIdError as exc:
            raise RuntimeError(f"job id already exists: {job_id}") from exc

        next_run = "-"
        if isinstance(job.next_run_time, datetime):
            next_run = job.next_run_time.isoformat()
        return f"scheduled: {job.id} next={next_run}"

    @register(name="schedule.remove", short_description="Remove a scheduled job", model=ScheduleRemoveInput)
    def schedule_remove(params: ScheduleRemoveInput) -> str:
        """Remove one scheduled job by id."""
        try:
            runtime.scheduler.remove_job(params.job_id)
        except JobLookupError as exc:
            raise RuntimeError(f"job not found: {params.job_id}") from exc
        return f"removed: {params.job_id}"

    @register(name="schedule.list", short_description="List scheduled jobs", model=EmptyInput, context=True)
    def schedule_list(_params: EmptyInput, context: ToolContext) -> str:
        """List scheduled jobs for current workspace."""
        jobs = runtime.scheduler.get_jobs()
        rows: list[str] = []
        for job in jobs:
            next_run = "-"
            if isinstance(job.next_run_time, datetime):
                next_run = job.next_run_time.isoformat()
            message = str(job.kwargs.get("message", ""))
            job_session = job.kwargs.get("session_id")
            if job_session and job_session != context.state.get("session_id", ""):
                continue
            rows.append(f"{job.id} next={next_run} msg={message}")

        if not rows:
            return "(no scheduled jobs)"

        return "\n".join(rows)

    @register(
        name="web.search",
        short_description="Search the web using Exa, Brave, or Ollama",
        model=SearchInput,
        always_expand=False,
        guidance=ToolGuidance(
            when_to_use="Finding information, documentation, articles, or URLs on the internet.",
            when_not_to="Reading a known URL (use web.fetch). Searching local files (use fs.grep).",
            constraints="Backend priority: exa > brave > ollama > duckduckgo URL fallback. Configure API keys in settings.",
        ),
    )
    async def web_search(params: SearchInput) -> str:
        """Search the web and return ranked results.

        Usage:
        - Results include title, URL, and a content snippet.
        - Configure at least one search backend API key for real results.
        - Without any API key, falls back to returning a DuckDuckGo search URL.
        - Backend priority: exa_api_key > brave_api_key > ollama_api_key > fallback.
        """
        if runtime.settings.exa_api_key:
            return await _web_search_exa(runtime.settings.exa_api_key, params)
        if runtime.settings.brave_api_key:
            return await _web_search_brave(runtime.settings.brave_api_key, params)
        if runtime.settings.ollama_api_key:
            return await _web_search_ollama(runtime, params)
        query = urllib_parse.quote_plus(params.query)
        return f"(no search API key configured, try: https://duckduckgo.com/?q={query})"

    @register(name="help", short_description="Show command help", model=EmptyInput)
    def command_help(_params: EmptyInput) -> str:
        """Show Bub internal command usage and examples."""
        return (
            "Commands use ',' at line start.\n"
            "Known names map to internal tools; other commands run through bash.\n"
            "Examples:\n"
            "  ,help\n"
            "  ,git status\n"
            "  , ls -la\n"
            "  ,tools\n"
            "  ,tool.describe name=fs.read\n"
            "  ,tape.handoff name=phase-1 summary='Bootstrap complete'\n"
            "  ,tape.anchors\n"
            "  ,tape.info\n"
            "  ,tape.search query=error\n"
            "  ,schedule.add cron='*/5 * * * *' message='echo hello'\n"
            "  ,schedule.list\n"
            "  ,schedule.remove job_id=my-job\n"
            "  ,skills.list\n"
            "  ,quit\n"
        )

    @register(name="tools", short_description="List available tools", model=EmptyInput, always_expand=True)
    def list_tools(_params: EmptyInput) -> str:
        """List all tools in compact mode."""
        return "\n".join(registry.compact_rows())

    @register(name="tool.describe", short_description="Show tool detail", model=ToolNameInput, always_expand=True)
    def tool_describe(params: ToolNameInput) -> str:
        """Expand one tool description and schema."""
        return registry.detail(params.name)

    @register(
        name="tape.handoff",
        short_description="Checkpoint context and start a new conversation phase",
        model=HandoffInput,
        always_expand=True,
        guidance=ToolGuidance(
            when_to_use=(
                "1. Context is getting long (>30 tool calls or >20 messages since last anchor). "
                "2. Completed a logical phase of work and starting a new one. "
                "3. A model call fails with context length error. "
                "4. Before delegating to a sub-agent that needs a clean starting point."
            ),
            when_not_to=(
                "1. In the middle of an active debugging session where recent context is critical. "
                "2. Only a few messages have been exchanged since the last anchor."
            ),
            constraints=(
                "After handoff, ALL messages before the anchor are dropped from context. "
                "Only the handoff state (summary, next_steps, files_modified, decisions) survives as a system message. "
                "Write SELF-CONTAINED summaries: a reader with no prior context must understand what was done. "
                "Always include file paths for modified files. After handoff, re-read files before modifying them."
            ),
        ),
    )
    async def handoff(params: HandoffInput) -> str:
        """Create a context checkpoint (anchor) that resets the conversation window.

        After this call, ALL messages before the anchor are permanently dropped from LLM context.
        Only the handoff state fields survive as a system message in the next phase:
        - summary: what was accomplished (required for useful handoffs)
        - next_steps: what to do next
        - files_modified: paths that were changed
        - decisions: key choices made and why

        The summary must be SELF-CONTAINED: a reader with zero prior context must understand
        what was done, what files were touched, and what to do next. After handoff, always
        re-read files before modifying them — do not rely on memory of file contents.
        """
        anchor_name = params.name or "handoff"
        state: dict[str, object] = {}
        if params.summary:
            state["summary"] = params.summary
        if params.next_steps:
            state["next_steps"] = params.next_steps
        if params.files_modified:
            state["files_modified"] = params.files_modified
        if params.decisions:
            state["decisions"] = params.decisions
        await tape.handoff(anchor_name, state=state or None)
        return f"handoff created: {anchor_name}"

    @register(name="tape.anchors", short_description="List tape anchors", model=EmptyInput)
    async def anchors(_params: EmptyInput) -> str:
        """List recent tape anchors."""
        rows = []
        for anchor in await tape.anchors(limit=50):
            rows.append(f"{anchor.name} state={json.dumps(anchor.state, ensure_ascii=False)}")
        return "\n".join(rows) if rows else "(no anchors)"

    @register(
        name="tape.info",
        short_description="Show tape context size and anchor status",
        model=EmptyInput,
        guidance=ToolGuidance(
            when_to_use="Checking how much context has been used. Deciding whether a tape.handoff is needed.",
            when_not_to="No need to check routinely — check when context feels large or before a complex phase.",
        ),
    )
    async def tape_info(_params: EmptyInput) -> str:
        """Show tape summary: entry count, anchor count, entries since last anchor, and token usage estimate.

        Use this to decide whether a tape.handoff checkpoint is needed (e.g. entries_since_last_anchor > 30).
        """
        info = await tape.info()
        return "\n".join((
            f"tape={info.name}",
            f"entries={info.entries}",
            f"anchors={info.anchors}",
            f"last_anchor={info.last_anchor or '-'}",
            f"entries_since_last_anchor={info.entries_since_last_anchor}",
            f"last_token_usage={info.last_token_usage or 'unknown'}",
        ))

    @register(
        name="tape.search",
        short_description="Search conversation history by keyword",
        model=TapeSearchInput,
        guidance=ToolGuidance(
            when_to_use="Finding previous tool results, user messages, or decisions from earlier in the conversation.",
            when_not_to="Searching file contents (use fs.grep). Searching for files (use fs.glob).",
        ),
    )
    async def tape_search(params: TapeSearchInput) -> str:
        """Search entries in the conversation tape by keyword query. Results are returned in reverse chronological order."""
        entries = await tape.search(params.query, limit=params.limit)
        if not entries:
            return "(no matches)"
        return "\n".join(f"#{entry.id} {entry.kind} {entry.payload}" for entry in entries)

    @register(name="tape.reset", short_description="Reset tape", model=TapeResetInput, context=True)
    async def tape_reset(params: TapeResetInput, context: ToolContext) -> str:
        """Reset current tape; can archive before clearing."""
        result = await tape.reset(archive=params.archive)
        runtime.reset_session_context(context.state.get("session_id", ""))
        return result

    @register(name="skills.list", short_description="List skills", model=EmptyInput)
    def list_skills(_params: EmptyInput) -> str:
        """List all discovered skills in compact form."""
        skills = runtime.discover_skills()
        if not skills:
            return "(no skills)"
        return "\n".join(f"{skill.name}: {skill.description}" for skill in skills)

    @register(name="quit", short_description="Exit program", model=EmptyInput)
    def quit_command(_params: EmptyInput) -> str:
        """Request exit from interactive CLI."""
        return "exit"

    register_task_tools(registry, workspace=workspace)
    register_agent_tools(registry, runtime=runtime)
