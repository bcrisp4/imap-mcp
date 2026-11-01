"""Tests for the CLI commands that need no IMAP server."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

import pytest

from imap_mcp.adapters.flock import FileLock
from imap_mcp.adapters.sqlite.connection import has_other_connections, open_database
from imap_mcp.config import CONFIG_ENV
from imap_mcp.entrypoints import cli
from imap_mcp.entrypoints.cli import (
    EXIT_BUSY,
    EXIT_ERROR,
    EXIT_INTERRUPTED,
    EXIT_OK,
    main,
)

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def config_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        'host = "imap.example.com"\nusername = "u@example.com"\n'
        'from_address = "u@example.com"\n'
        f'db_path = "{tmp_path / "state" / "imap-mcp.db"}"\n'
    )
    monkeypatch.setenv(CONFIG_ENV, str(path))
    return path


class TestCli:
    def test_status_on_a_new_index_reports_no_sync(
        self, config_file: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["status"]) == EXIT_OK

        err = capsys.readouterr().err
        assert "Messages: 0" in err
        assert "never" in err

    def test_purge_needs_yes_and_then_deletes_the_files(
        self, config_file: Path, tmp_path: Path
    ) -> None:
        main(["status"])
        db = tmp_path / "state" / "imap-mcp.db"
        assert db.exists()

        assert main(["purge"]) == EXIT_ERROR
        assert db.exists()
        assert main(["purge", "--yes"]) == EXIT_OK
        assert not db.exists()
        assert not (tmp_path / "state" / "sync.lock").exists()

    def test_a_missing_config_is_a_clear_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:  # fmt: skip
        monkeypatch.setenv(CONFIG_ENV, str(tmp_path / "nope.toml"))

        assert main(["status"]) == EXIT_ERROR
        assert "No config file" in capsys.readouterr().err

    def test_ctrl_c_exits_with_one_line_and_no_traceback(
        self, config_file: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:  # fmt: skip
        def interrupted(_config: object) -> int:
            raise KeyboardInterrupt

        monkeypatch.setattr(cli, "cmd_status", interrupted)

        assert main(["status"]) == EXIT_INTERRUPTED
        assert capsys.readouterr().err == "imap-mcp: interrupted.\n"

    def test_an_unexpected_error_hides_its_message(
        self, config_file: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:  # fmt: skip
        mail_text = "Subject: " + "private"

        def broken(_config: object) -> int:
            raise ValueError(mail_text)

        monkeypatch.setattr(cli, "cmd_status", broken)

        assert main(["status"]) == EXIT_ERROR
        err = capsys.readouterr().err
        assert "unexpected ValueError" in err
        assert "private" not in err
        assert "Traceback" not in err

        monkeypatch.setenv(cli.DEBUG_ENV, "1")
        assert main(["status"]) == EXIT_ERROR
        err = capsys.readouterr().err
        assert "in broken" in err
        assert "private" not in err

    def test_vacuum_runs_on_a_new_index(self, config_file: Path) -> None:
        assert main(["vacuum"]) == EXIT_OK


class TestRunningProcesses:
    def test_purge_refuses_while_another_process_has_the_index_open(
        self, config_file: Path, tmp_path: Path
    ) -> None:
        main(["status"])
        db = tmp_path / "state" / "imap-mcp.db"
        other = open_database(db)  # an idle MCP server's connection

        try:
            assert main(["purge", "--yes"]) == EXIT_BUSY
            assert db.exists()
        finally:
            other.close()

    def test_status_does_not_wait_for_a_running_sync(
        self, config_file: Path, tmp_path: Path
    ) -> None:
        main(["status"])
        sync = FileLock(tmp_path / "state" / "sync.lock")
        assert sync.try_acquire()

        try:
            assert main(["status"]) == EXIT_OK
        finally:
            sync.release()


def test_the_purge_probe_leaves_the_database_in_wal_mode(tmp_path: Path) -> None:
    db = tmp_path / "x.db"
    open_database(db).close()

    assert has_other_connections(db) is False

    # A plain connection, because open_database would switch to WAL itself.
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        conn.close()
