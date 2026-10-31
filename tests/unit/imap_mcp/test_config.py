"""Tests for loading and checking the config file."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from imap_mcp.config import CONFIG_ENV, load_config
from imap_mcp.domain.errors import ConfigError

if TYPE_CHECKING:
    from pathlib import Path

MINIMAL = """
host = "imap.mail.me.com"
username = "ben@icloud.com"
from_address = "ben@thecrisp.io"
"""


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point `~` at a temporary directory."""
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def write_config(home: Path, text: str) -> Path:
    path = home / ".config" / "imap-mcp" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text(text)
    return path


class TestLoadConfig:
    def test_a_minimal_file_gets_the_documented_defaults(self, home: Path) -> None:
        write_config(home, MINIMAL)

        config = load_config(env={})

        assert config.port == 993
        assert config.sync_interval_minutes == 15
        assert config.sync_budget_seconds == 20
        assert config.download_dir == home / "Downloads"
        assert config.db_path == (
            home / "Library" / "Application Support" / "imap-mcp" / "imap-mcp.db"
        )
        assert config.exclude_folders == ()

    def test_the_environment_variable_overrides_the_path(
        self, home: Path, tmp_path: Path
    ) -> None:
        other = tmp_path / "other.toml"
        other.write_text(MINIMAL.replace("ben@icloud.com", "other@icloud.com"))

        config = load_config(env={CONFIG_ENV: str(other)})

        assert config.username == "other@icloud.com"

    def test_a_missing_file_names_the_path_it_read(self, home: Path) -> None:
        with pytest.raises(ConfigError, match=r"config\.toml"):
            load_config(env={})

    def test_an_unknown_key_is_rejected(self, home: Path) -> None:
        write_config(home, MINIMAL + 'smtp_host = "smtp.example.com"\n')

        with pytest.raises(ConfigError, match="smtp_host"):
            load_config(env={})

    @pytest.mark.parametrize(
        "location",
        [
            "~/Library/Mobile Documents/com~apple~CloudDocs/imap-mcp.db",
            "~/Documents/imap-mcp.db",
            "~/Desktop/imap-mcp.db",
            "~/Library/CloudStorage/Dropbox/imap-mcp.db",
        ],
        ids=["icloud-drive", "documents", "desktop", "cloud-storage"],
    )
    def test_a_database_in_a_synced_folder_is_refused(
        self, home: Path, location: str
    ) -> None:
        write_config(home, MINIMAL + f'db_path = "{location}"\n')

        with pytest.raises(ConfigError, match="synced"):
            load_config(env={})

    def test_a_zero_interval_is_rejected(self, home: Path) -> None:
        write_config(home, MINIMAL + "sync_interval_minutes = 0\n")

        with pytest.raises(ConfigError, match="sync_interval_minutes"):
            load_config(env={})
