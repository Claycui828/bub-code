"""Application settings."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _load_yaml_config(workspace: Path | None = None) -> dict[str, Any]:
    """Load YAML config from workspace or cwd, returns empty dict if not found."""
    search_dirs = []
    if workspace:
        search_dirs.append(workspace)
    search_dirs.append(Path.cwd())

    for d in search_dirs:
        for name in ("bub.yaml", "bub.yml"):
            path = d / name
            if path.is_file():
                with open(path) as f:
                    data = yaml.safe_load(f)
                return data if isinstance(data, dict) else {}
    return {}


class Settings(BaseSettings):
    """Runtime settings loaded from YAML, environment, and .env files.

    Priority (highest wins): env vars > .env file > bub.yaml defaults.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="BUB_",
        case_sensitive=False,
        extra="ignore",
        env_parse_none_str="null",
    )

    model: str = "openrouter:minimax/minimax-m2.5"
    api_key: str | None = None
    api_base: str | None = None
    ollama_api_key: str | None = None
    ollama_api_base: str | None = None
    exa_api_key: str | None = None
    brave_api_key: str | None = None
    llm_api_key: str | None = Field(default=None, validation_alias="LLM_API_KEY")
    openrouter_api_key: str | None = Field(default=None, validation_alias="OPENROUTER_API_KEY")
    max_tokens: int = Field(default=1024, ge=1)
    model_timeout_seconds: int | None = 90
    system_prompt: str = ""

    home: str | None = None
    workspace_path: str | None = None
    tape_name: str = "bub"
    max_steps: int = Field(default=20, ge=1)

    proactive_response: bool = False
    message_delay_seconds: int = 10
    message_debounce_seconds: int = 1
    active_time_window_seconds: int = 60

    telegram_enabled: bool = False
    telegram_token: str | None = None
    telegram_allow_from: list[str] = Field(default_factory=list)
    telegram_allow_chats: list[str] = Field(default_factory=list)
    telegram_proxy: str | None = Field(default=None)

    discord_enabled: bool = False
    discord_token: str | None = None
    discord_allow_from: list[str] = Field(default_factory=list)
    discord_allow_channels: list[str] = Field(default_factory=list)
    discord_command_prefix: str = "!"
    discord_proxy: str | None = None

    feishu_enabled: bool = False
    feishu_app_id: str | None = None
    feishu_app_secret: str | None = None
    feishu_allow_from: list[str] = Field(default_factory=list)
    feishu_allow_chats: list[str] = Field(default_factory=list)

    trace_enabled: bool = False
    trace_backend: str = "langfuse"  # langfuse | otel
    langfuse_public_key: str | None = None
    langfuse_secret_key: str | None = None
    langfuse_host: str | None = None
    otel_service_name: str = "bub"
    otel_endpoint: str | None = None

    @property
    def resolved_api_key(self) -> str | None:
        if self.api_key:
            return self.api_key
        if self.llm_api_key:
            return self.llm_api_key
        if self.openrouter_api_key:
            return self.openrouter_api_key
        return os.getenv("LLM_API_KEY") or os.getenv("OPENROUTER_API_KEY")

    def resolve_home(self) -> Path:
        if self.home:
            return Path(self.home).expanduser().resolve()
        return (Path.home() / ".bub").resolve()


def load_settings(workspace_path: Path | None = None) -> Settings:
    """Load settings with optional workspace override.

    Loads bub.yaml from the workspace (or cwd) as base defaults,
    then lets env vars and .env override on top.
    """
    yaml_data = _load_yaml_config(workspace_path)

    if workspace_path is not None:
        yaml_data["workspace_path"] = str(workspace_path.resolve())

    # Pass YAML values as init kwargs — pydantic-settings gives env vars
    # higher priority than init values, so env/.env will override YAML.
    return Settings(**yaml_data)
