"""Tests for UID set rendering."""

from __future__ import annotations

from imap_mcp.adapters.imap.uidset import render_uid_sets


class TestRenderUidSets:
    def test_runs_collapse_into_closed_ranges(self) -> None:
        assert render_uid_sets([5, 1, 2, 3, 9, 3]) == ["1:3,5,9"]

    def test_no_set_is_open_ended(self) -> None:
        assert all("*" not in part for part in render_uid_sets(range(1, 100_000, 2)))

    def test_a_long_set_splits_below_the_limit_and_keeps_every_uid(self) -> None:
        uids = list(range(1, 40_000, 2))

        parts = render_uid_sets(uids, max_chars=1000)

        assert all(len(p) <= 1000 for p in parts)
        rendered = {int(u) for p in parts for u in p.split(",")}
        assert rendered == set(uids)

    def test_no_uids_gives_no_parts(self) -> None:
        assert render_uid_sets([]) == []
