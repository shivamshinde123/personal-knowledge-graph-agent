"""Tests for the GitHub extractor.

The real GitHub REST API isn't reachable in tests, so ``httpx.Client`` is
given a ``MockTransport`` that serves canned JSON per endpoint, shaped
like the real API's responses.
"""

import base64
from types import SimpleNamespace

import httpx
import pytest

from extractors.base import ExtractorError
from extractors.github import extract_new_items


def _readme_response(content: str = "# Hello\n\nSome docs.") -> dict:
    return {
        "path": "README.md",
        "content": base64.b64encode(content.encode()).decode(),
        "html_url": "https://github.com/octocat/hello/blob/main/README.md",
    }


def _commit(sha: str, message: str, date: str = "2026-08-01T00:00:00Z") -> dict:
    return {
        "sha": sha,
        "html_url": f"https://github.com/octocat/hello/commit/{sha}",
        "commit": {
            "message": message,
            "author": {"name": "Octo Cat"},
            "committer": {"date": date},
        },
    }


def _pr(number: int, title: str, created_at: str = "2026-08-01T00:00:00Z") -> dict:
    return {
        "number": number,
        "title": title,
        "body": "PR body",
        "html_url": f"https://github.com/octocat/hello/pull/{number}",
        "user": {"login": "octocat"},
        "created_at": created_at,
        "updated_at": created_at,
    }


def _issue(number: int, title: str, created_at: str = "2026-08-01T00:00:00Z") -> dict:
    return {
        "number": number,
        "title": title,
        "body": "Issue body",
        "html_url": f"https://github.com/octocat/hello/issues/{number}",
        "user": {"login": "octocat"},
        "created_at": created_at,
        "updated_at": created_at,
    }


def make_handler(
    *,
    repos=("octocat/hello",),
    readme=None,
    commits=(),
    commit_details=None,
    prs=(),
    pr_comments=None,
    issues=(),
    starred=(),
    repo_meta=None,
    captured_commit_params=None,
):
    """Build an ``httpx.MockTransport`` handler faking the GitHub REST API.

    Args:
        repos: Full names returned by ``GET /user/repos``.
        readme: Response body for ``GET /repos/{repo}/readme``, or ``None``
            for a 404 (no README).
        commits: List of commit dicts returned for every repo's commits
            endpoint (paginated in one page for simplicity).
        commit_details: ``{sha: [filename, ...]}`` for the per-commit
            changed-files lookup.
        prs: List of PR dicts.
        pr_comments: ``{number: [comment body, ...]}`` for PR review
            comments.
        issues: List of issue dicts (may include PR-shaped entries with a
            ``pull_request`` key, to verify they're excluded).
        starred: List of ``{"starred_at": ..., "repo": {...}}`` entries.
        repo_meta: Response body for ``GET /repos/{repo}``.
        captured_commit_params: If given, every commits-endpoint request's
            query params are appended to this list, for asserting on
            ``since``/``until`` in date-range tests.
    """
    commit_details = commit_details or {}
    pr_comments = pr_comments or {}
    repo_meta = repo_meta or {"description": "", "topics": []}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path

        if path == "/user/repos":
            return httpx.Response(200, json=[{"full_name": name} for name in repos])
        if path == "/user/starred":
            return httpx.Response(200, json=list(starred))
        if path.endswith("/readme"):
            if readme is None:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, json=readme)
        segments = [s for s in path.split("/") if s]
        if len(segments) == 3 and segments[0] == "repos":
            # GET /repos/{owner}/{repo}
            return httpx.Response(200, json=repo_meta)
        if path.endswith("/commits") and "path" in request.url.params:
            # README last-modified lookup
            return httpx.Response(200, json=[_commit("readme-sha", "docs: update")])
        if path.endswith("/commits"):
            if captured_commit_params is not None:
                captured_commit_params.append(dict(request.url.params))
            return httpx.Response(200, json=list(commits))
        if "/commits/" in path:
            sha = path.rsplit("/", 1)[-1]
            files = commit_details.get(sha, [])
            return httpx.Response(200, json={"files": [{"filename": f} for f in files]})
        if path.endswith("/pulls"):
            return httpx.Response(200, json=list(prs))
        if "/pulls/" in path and path.endswith("/comments"):
            number = int(path.split("/")[-2])
            bodies = pr_comments.get(number, [])
            return httpx.Response(200, json=[{"body": b} for b in bodies])
        if path.endswith("/issues"):
            return httpx.Response(200, json=list(issues))
        return httpx.Response(404, json={"message": f"unhandled path: {path}"})

    return handler


def install_fake_client(
    monkeypatch,
    handler,
    *,
    github_token="fake-token",
    github_repos_list=None,
    github_date_range_start=None,
    github_date_range_end=None,
):
    monkeypatch.setattr(
        "extractors.github.get_settings",
        lambda: SimpleNamespace(
            env=SimpleNamespace(
                github_token=github_token,
                github_repos_list=github_repos_list or [],
                github_date_range_start=github_date_range_start,
                github_date_range_end=github_date_range_end,
            )
        ),
    )

    real_client_cls = httpx.Client

    def fake_client(*args, **kwargs):
        kwargs.pop("base_url", None)
        return real_client_cls(
            base_url="https://api.github.com",
            transport=httpx.MockTransport(handler),
            **{k: v for k, v in kwargs.items() if k != "timeout"},
        )

    monkeypatch.setattr("extractors.github.httpx.Client", fake_client)


class TestNoToken:
    def test_raises_extractor_error_when_github_token_is_unset(self, monkeypatch):
        monkeypatch.setattr(
            "extractors.github.get_settings",
            lambda: SimpleNamespace(
                env=SimpleNamespace(github_token=None, github_repos_list=[])
            ),
        )

        with pytest.raises(ExtractorError, match="GITHUB_TOKEN"):
            extract_new_items()


class TestReadme:
    def test_extracts_readme_with_description_and_topics(self, monkeypatch):
        handler = make_handler(
            readme=_readme_response("# Hello\n\nDocs here."),
            repo_meta={"description": "A test repo", "topics": ["python", "rag"]},
        )
        install_fake_client(monkeypatch, handler)

        items = extract_new_items()

        readme_items = [i for i in items if i.source_ref_id.endswith(":readme")]
        assert len(readme_items) == 1
        item = readme_items[0]
        assert item.source_type == "github"
        assert "A test repo" in item.raw_text
        assert "python" in item.raw_text
        assert "Docs here." in item.raw_text

    def test_missing_readme_is_skipped_not_an_error(self, monkeypatch):
        handler = make_handler(readme=None)
        install_fake_client(monkeypatch, handler)

        items = extract_new_items()

        assert not any(i.source_ref_id.endswith(":readme") for i in items)

    def test_readme_older_than_since_is_excluded(self, monkeypatch):
        from datetime import UTC, datetime

        handler = make_handler(readme=_readme_response())
        install_fake_client(monkeypatch, handler)

        items = extract_new_items(since=datetime(2027, 1, 1, tzinfo=UTC))

        assert not any(i.source_ref_id.endswith(":readme") for i in items)


class TestCommits:
    def test_extracts_commit_with_changed_files(self, monkeypatch):
        handler = make_handler(
            commits=[_commit("abc123", "fix: a bug\n\nDetails.")],
            commit_details={"abc123": ["a.py", "b.py"]},
        )
        install_fake_client(monkeypatch, handler)

        items = extract_new_items()

        commit_items = [i for i in items if ":commit:" in i.source_ref_id]
        assert len(commit_items) == 1
        item = commit_items[0]
        assert item.source_ref_id == "octocat/hello:commit:abc123"
        assert "fix: a bug" in item.title
        assert "a.py" in item.raw_text
        assert "b.py" in item.raw_text
        assert item.author_or_sender == "Octo Cat"

    def test_empty_repo_returns_no_commits(self, monkeypatch):
        def handler(request):
            if (
                request.url.path.endswith("/commits")
                and "path" not in request.url.params
            ):
                return httpx.Response(409, json={"message": "Git Repository is empty."})
            return make_handler()(request)

        install_fake_client(monkeypatch, handler)

        items = extract_new_items()

        assert not any(":commit:" in i.source_ref_id for i in items)


class TestPullRequests:
    def test_extracts_pr_with_review_comments(self, monkeypatch):
        handler = make_handler(
            prs=[_pr(5, "Add feature")],
            pr_comments={5: ["Looks good", "One nit"]},
        )
        install_fake_client(monkeypatch, handler)

        items = extract_new_items()

        pr_items = [i for i in items if ":pr:" in i.source_ref_id]
        assert len(pr_items) == 1
        item = pr_items[0]
        assert item.source_ref_id == "octocat/hello:pr:5"
        assert "Add feature" in item.title
        assert "Looks good" in item.raw_text
        assert "One nit" in item.raw_text

    def test_prs_older_than_since_are_excluded(self, monkeypatch):
        from datetime import UTC, datetime

        handler = make_handler(
            prs=[_pr(1, "Old PR", created_at="2026-01-01T00:00:00Z")]
        )
        install_fake_client(monkeypatch, handler)

        items = extract_new_items(since=datetime(2026, 6, 1, tzinfo=UTC))

        assert not any(":pr:" in i.source_ref_id for i in items)


class TestIssues:
    def test_extracts_issues_and_excludes_pull_requests(self, monkeypatch):
        real_issue = _issue(10, "A real issue")
        pr_shaped_issue = {**_issue(11, "Actually a PR"), "pull_request": {}}
        handler = make_handler(issues=[real_issue, pr_shaped_issue])
        install_fake_client(monkeypatch, handler)

        items = extract_new_items()

        issue_items = [i for i in items if ":issue:" in i.source_ref_id]
        assert [i.source_ref_id for i in issue_items] == ["octocat/hello:issue:10"]


class TestStarredRepos:
    def test_extracts_starred_repos_lightweight(self, monkeypatch):
        handler = make_handler(
            starred=[
                {
                    "starred_at": "2026-08-01T00:00:00Z",
                    "repo": {
                        "full_name": "someone/cool-project",
                        "description": "A cool project",
                        "topics": ["ml"],
                        "html_url": "https://github.com/someone/cool-project",
                    },
                }
            ],
            repos=(),
        )
        install_fake_client(monkeypatch, handler)

        items = extract_new_items()

        starred_items = [i for i in items if i.source_ref_id.endswith(":starred")]
        assert len(starred_items) == 1
        assert starred_items[0].source_ref_id == "someone/cool-project:starred"
        assert "A cool project" in starred_items[0].raw_text
        assert "ml" in starred_items[0].raw_text


class TestDateRangeScoping:
    def test_end_date_is_sent_as_the_commits_until_param(self, monkeypatch):
        from datetime import date

        captured = []
        handler = make_handler(captured_commit_params=captured)
        install_fake_client(
            monkeypatch, handler, github_date_range_end=date(2026, 6, 30)
        )

        extract_new_items()

        assert len(captured) == 1
        # Inclusive of the configured end date: the day after it.
        assert captured[0]["until"] == "2026-07-01T00:00:00Z"

    def test_no_range_configured_sends_no_until_param(self, monkeypatch):
        captured = []
        handler = make_handler(captured_commit_params=captured)
        install_fake_client(monkeypatch, handler)

        extract_new_items()

        assert "until" not in captured[0]

    def test_start_date_floors_an_earlier_since_cursor(self, monkeypatch):
        from datetime import UTC, date, datetime

        captured = []
        handler = make_handler(captured_commit_params=captured)
        install_fake_client(
            monkeypatch, handler, github_date_range_start=date(2026, 6, 1)
        )
        earlier_since = datetime(2020, 1, 1, tzinfo=UTC)

        extract_new_items(earlier_since)

        assert captured[0]["since"] == "2026-06-01T00:00:00Z"

    def test_start_date_does_not_move_a_later_since_cursor_backward(self, monkeypatch):
        from datetime import UTC, date, datetime

        captured = []
        handler = make_handler(captured_commit_params=captured)
        install_fake_client(
            monkeypatch, handler, github_date_range_start=date(2020, 1, 1)
        )
        later_since = datetime(2026, 1, 1, tzinfo=UTC)

        extract_new_items(later_since)

        assert captured[0]["since"] == "2026-01-01T00:00:00Z"

    def test_prs_newer_than_the_end_date_are_excluded(self, monkeypatch):
        from datetime import date

        handler = make_handler(
            prs=[_pr(1, "Too new", created_at="2026-08-01T00:00:00Z")]
        )
        install_fake_client(
            monkeypatch, handler, github_date_range_end=date(2026, 6, 30)
        )

        items = extract_new_items()

        assert not any(":pr:" in i.source_ref_id for i in items)

    def test_issues_newer_than_the_end_date_are_excluded(self, monkeypatch):
        from datetime import date

        handler = make_handler(
            issues=[_issue(1, "Too new", created_at="2026-08-01T00:00:00Z")]
        )
        install_fake_client(
            monkeypatch, handler, github_date_range_end=date(2026, 6, 30)
        )

        items = extract_new_items()

        assert not any(":issue:" in i.source_ref_id for i in items)

    def test_starred_repos_newer_than_the_end_date_are_excluded(self, monkeypatch):
        from datetime import date

        handler = make_handler(
            starred=[
                {
                    "starred_at": "2026-08-01T00:00:00Z",
                    "repo": {
                        "full_name": "someone/cool-project",
                        "description": "",
                        "topics": [],
                        "html_url": "https://github.com/someone/cool-project",
                    },
                }
            ],
            repos=(),
        )
        install_fake_client(
            monkeypatch, handler, github_date_range_end=date(2026, 6, 30)
        )

        items = extract_new_items()

        assert not any(i.source_ref_id.endswith(":starred") for i in items)


class TestScopedToConfiguredRepos:
    def test_only_configured_repos_are_processed_not_every_accessible_one(
        self, monkeypatch
    ):
        calls = []

        def handler(request):
            calls.append(request.url.path)
            if request.url.path == "/user/repos":
                raise AssertionError("should not list all repos when scoped")
            return make_handler(readme=_readme_response())(request)

        install_fake_client(monkeypatch, handler, github_repos_list=["scoped/repo"])

        items = extract_new_items()

        assert any(i.source_ref_id.startswith("scoped/repo:") for i in items)


class TestOnProgress:
    def test_called_once_per_repo_with_total_and_label(self, monkeypatch):
        calls = []
        handler = make_handler()
        install_fake_client(
            monkeypatch, handler, github_repos_list=["a/one", "b/two", "c/three"]
        )

        extract_new_items(
            on_progress=lambda current, total, label: (
                calls.append((current, total, label)) or True
            )
        )

        assert calls == [
            (1, 3, "a/one"),
            (2, 3, "b/two"),
            (3, 3, "c/three"),
        ]

    def test_returning_false_stops_after_the_current_repo(self, monkeypatch):
        handler = make_handler()
        install_fake_client(
            monkeypatch, handler, github_repos_list=["a/one", "b/two", "c/three"]
        )
        calls = []

        def on_progress(current, total, label):
            calls.append(label)
            return current < 2

        extract_new_items(on_progress=on_progress)

        assert calls == ["a/one", "b/two"]

    def test_no_callback_is_the_default(self, monkeypatch):
        handler = make_handler()
        install_fake_client(monkeypatch, handler, github_repos_list=["a/one"])

        items = extract_new_items()  # must not raise

        assert isinstance(items, list)


class TestRepoLevelFailureIsolation:
    def test_one_failing_repo_does_not_abort_the_whole_run(self, monkeypatch):
        def handler(request):
            if "broken/repo" in request.url.path:
                return httpx.Response(500, json={"message": "server error"})
            return make_handler(readme=_readme_response())(request)

        install_fake_client(
            monkeypatch, handler, github_repos_list=["broken/repo", "octocat/hello"]
        )

        items = extract_new_items()

        assert any(i.source_ref_id.startswith("octocat/hello:") for i in items)
