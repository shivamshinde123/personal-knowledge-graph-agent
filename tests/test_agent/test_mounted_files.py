"""Tests for agent/mounted_files.py."""

from agent.mounted_files import _MAX_FILES, list_host_data_files


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
