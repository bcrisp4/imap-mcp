"""The config file: where to connect and how to sync.

The file holds no secret. The IMAP password lives in the credential store.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from imap_mcp.domain.errors import ConfigError

if TYPE_CHECKING:
    from collections.abc import Mapping

CONFIG_ENV = "IMAP_MCP_CONFIG"
DEFAULT_CONFIG_PATH = "~/.config/imap-mcp/config.toml"
DEFAULT_DB_PATH = "~/Library/Application Support/imap-mcp/imap-mcp.db"

# File locking over a synced folder can corrupt SQLite. These folders are
# synced by iCloud Drive (including Desktop and Documents sync) or by other
# file providers.
_SYNCED_FOLDERS = (
    "~/Library/Mobile Documents",
    "~/Library/CloudStorage",
    "~/Documents",
    "~/Desktop",
)


class Config(BaseModel):
    """The validated contents of `config.toml`."""

    model_config = ConfigDict(frozen=True, extra="forbid", validate_default=True)

    host: str
    port: int = Field(default=993, ge=1, le=65535)
    username: str
    from_address: str
    credential_source: Literal["keychain"] = "keychain"
    download_dir: Path = Path("~/Downloads")
    sync_interval_minutes: int = Field(default=15, ge=1)
    sync_budget_seconds: int = Field(default=20, ge=1)
    db_path: Path = Path(DEFAULT_DB_PATH)
    drafts_folder: str = ""
    exclude_folders: tuple[str, ...] = ()

    @field_validator("download_dir", "db_path", mode="after")
    @classmethod
    def _expand(cls, value: Path) -> Path:
        return value.expanduser()

    @field_validator("db_path", mode="after")
    @classmethod
    def _refuse_synced_folder(cls, value: Path) -> Path:
        resolved = value.resolve()
        for folder in _SYNCED_FOLDERS:
            root = Path(folder).expanduser().resolve()
            if resolved.is_relative_to(root):
                msg = (
                    f"db_path is inside {folder}, which a file provider can sync. "
                    "SQLite locking is unsafe there. Use a local path."
                )
                raise ValueError(msg)
        return value


def config_path(env: Mapping[str, str]) -> Path:
    """Return the config file path.

    Args:
        env: The process environment.

    Returns:
        The path from `IMAP_MCP_CONFIG`, or the default path.
    """
    return Path(env.get(CONFIG_ENV, DEFAULT_CONFIG_PATH)).expanduser()


def load_config(env: Mapping[str, str]) -> Config:
    """Read and validate the config file.

    Args:
        env: The process environment.

    Returns:
        The validated config.

    Raises:
        ConfigError: If the file is missing, is not valid TOML, or fails
            validation.
    """
    path = config_path(env)
    try:
        raw = tomllib.loads(path.read_text())
    except FileNotFoundError as exc:
        msg = f"No config file at {path}. Create it as the README describes."
        raise ConfigError(msg) from exc
    except tomllib.TOMLDecodeError as exc:
        msg = f"{path} is not valid TOML: {exc}"
        raise ConfigError(msg) from exc
    try:
        return Config.model_validate(raw)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}"
            for err in exc.errors()
        )
        msg = f"{path} is invalid: {problems}"
        raise ConfigError(msg) from exc
