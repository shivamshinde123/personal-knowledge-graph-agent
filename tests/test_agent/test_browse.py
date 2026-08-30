"""Tests for agent/browse.py.

``subprocess.run`` is faked — a real run would open an actual native
folder dialog and block waiting for a human to click something, exactly
the kind of interaction a unit test can't do and shouldn't try to.
"""

from types import SimpleNamespace

from agent.browse import browse_folder


class TestBrowseFolder:
    def test_returns_the_selected_path(self, monkeypatch):
        calls = []

        def fake_run(argv, **kwargs):
            calls.append((argv, kwargs))
            return SimpleNamespace(stdout="C:\\Users\\you\\Documents\\Notes\n")

        monkeypatch.setattr("agent.browse.subprocess.run", fake_run)

        path = browse_folder()

        assert path == "C:\\Users\\you\\Documents\\Notes"
        assert len(calls) == 1
        argv, kwargs = calls[0]
        assert argv[0] == "powershell"
        assert kwargs["capture_output"] is True
        assert kwargs["text"] is True

    def test_returns_none_when_the_dialog_is_cancelled(self, monkeypatch):
        monkeypatch.setattr(
            "agent.browse.subprocess.run",
            lambda argv, **kwargs: SimpleNamespace(stdout=""),
        )

        assert browse_folder() is None

    def test_strips_surrounding_whitespace_from_the_path(self, monkeypatch):
        monkeypatch.setattr(
            "agent.browse.subprocess.run",
            lambda argv, **kwargs: SimpleNamespace(stdout="  D:\\Notes  \r\n"),
        )

        assert browse_folder() == "D:\\Notes"
