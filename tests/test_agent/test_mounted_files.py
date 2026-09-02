"""Tests for agent/mounted_files.py."""

from agent.mounted_files import (
    _MAX_FILES,
    is_within_watched_root,
    list_host_data_files,
    list_watched_directories,
)


class TestListHostDataFiles:
    def test_empty_when_the_mount_does_not_exist(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "agent.mounted_files._HOST_DATA_ROOT", tmp_path / "does-not-exist"
        )

        assert list_host_data_files() == []

    def test_lists_files_with_posix_relative_paths(self, monkeypatch, tmp_path):
        (tmp_path / "History").write_text("fake", encoding="utf-8")
        subdir = tmp_path / "creds"
        subdir.mkdir()
        (subdir / "gmail.json").write_text("{}", encoding="utf-8")
        monkeypatch.setattr("agent.mounted_files._HOST_DATA_ROOT", tmp_path)

        files = list_host_data_files()

        assert files == ["History", "creds/gmail.json"]

    def test_directories_themselves_are_not_listed(self, monkeypatch, tmp_path):
        (tmp_path / "empty-dir").mkdir()
        monkeypatch.setattr("agent.mounted_files._HOST_DATA_ROOT", tmp_path)

        assert list_host_data_files() == []

    def test_caps_the_result_at_max_files(self, monkeypatch, tmp_path):
        for i in range(_MAX_FILES + 10):
            (tmp_path / f"file-{i}.txt").write_text("x", encoding="utf-8")
        monkeypatch.setattr("agent.mounted_files._HOST_DATA_ROOT", tmp_path)

        assert len(list_host_data_files()) == _MAX_FILES


class TestListWatchedDirectories:
    def test_empty_when_the_mount_does_not_exist(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "agent.mounted_files._WATCHED_ROOT", tmp_path / "does-not-exist"
        )

        assert list_watched_directories() == []

    def test_lists_immediate_subdirectories_as_full_paths(self, monkeypatch, tmp_path):
        (tmp_path / "project-a").mkdir()
        (tmp_path / "project-b").mkdir()
        monkeypatch.setattr("agent.mounted_files._WATCHED_ROOT", tmp_path)

        dirs = list_watched_directories()

        assert dirs == sorted(
            [str(tmp_path / "project-a"), str(tmp_path / "project-b")]
        )

    def test_files_directly_under_the_root_are_not_listed(self, monkeypatch, tmp_path):
        (tmp_path / "notes.txt").write_text("x", encoding="utf-8")
        monkeypatch.setattr("agent.mounted_files._WATCHED_ROOT", tmp_path)

        assert list_watched_directories() == []

    def test_only_one_level_deep(self, monkeypatch, tmp_path):
        nested = tmp_path / "project-a" / "nested"
        nested.mkdir(parents=True)
        monkeypatch.setattr("agent.mounted_files._WATCHED_ROOT", tmp_path)

        dirs = list_watched_directories()

        assert dirs == [str(tmp_path / "project-a")]

    def test_caps_the_result_at_max_files(self, monkeypatch, tmp_path):
        for i in range(_MAX_FILES + 10):
            (tmp_path / f"dir-{i}").mkdir()
        monkeypatch.setattr("agent.mounted_files._WATCHED_ROOT", tmp_path)

        assert len(list_watched_directories()) == _MAX_FILES


class TestIsWithinWatchedRoot:
    def test_the_root_itself_is_within(self):
        assert is_within_watched_root("/data/watched") is True

    def test_a_subdirectory_is_within(self):
        assert is_within_watched_root("/data/watched/project-a") is True

    def test_a_nested_subdirectory_is_within(self):
        assert is_within_watched_root("/data/watched/project-a/nested") is True

    def test_an_unrelated_path_is_not_within(self):
        assert is_within_watched_root("/etc/passwd") is False

    def test_a_sibling_that_merely_shares_a_prefix_is_not_within(self):
        # "/data/watched-evil" starts with the same characters as
        # "/data/watched" but is not a real descendant of it.
        assert is_within_watched_root("/data/watched-evil") is False

    def test_a_path_traversal_attempt_is_not_within(self):
        assert is_within_watched_root("/data/watched/../etc") is False

    def test_a_deeper_path_traversal_attempt_is_not_within(self):
        assert is_within_watched_root("/data/watched/project-a/../../../etc") is False

    def test_a_traversal_that_still_lands_inside_the_root_is_within(self):
        # Legitimate after normalizing -- .. cancels out project-a/nested,
        # leaving a path that's still under /data/watched.
        assert is_within_watched_root("/data/watched/project-a/nested/..") is True

    def test_a_relative_path_is_not_within(self):
        assert is_within_watched_root("watched/project-a") is False
