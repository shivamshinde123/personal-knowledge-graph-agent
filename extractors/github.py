"""GitHub extractor: commits, PRs, issues, READMEs, stars → normalized items.

Per ``docs/Data_Extraction_Specification.docx`` section 6's field-level
table: commit messages (yes), commit diffs (selectively — changed file
names only, not raw code; an LLM summary of those is a pipeline concern,
not this extractor's — see ``docs/Component_Map.docx``'s rule that
extractors never call an LLM provider), READMEs (yes, periodically
re-checked for updates), PR titles/descriptions and review comments (yes,
if present), issues (yes), starred repos (yes, lightweight — name,
description, topics only), and repo topics/tags (folded into the README
item's text rather than a separate item kind — see ``DECISIONS.md``).

If ``settings.env.github_repos_list`` is non-empty, ingestion is scoped to
just those ``owner/repo`` full names instead of every repository the
token can access — same convention as ``extractors/notion.py``'s
``notion_page_ids_list``. An empty/unset list means "every accessible
repo" (owned, collaborator, and organization repos).
"""

from __future__ import annotations

import base64
import logging
from datetime import UTC, datetime, time, timedelta

import httpx

from config.settings import get_settings
from extractors.base import ExtractedItem, ExtractorError, OnProgress

logger = logging.getLogger(__name__)

SOURCE_TYPE = "github"

_API_BASE = "https://api.github.com"
_PER_PAGE = 100
_API_VERSION = "2022-11-28"


def _effective_window(
    since: datetime | None, range_start, range_end
) -> tuple[datetime | None, datetime | None]:
    """Combine the incremental cursor with a configured date-range scope.

    ``range_start`` (``GITHUB_DATE_RANGE_START``, a ``date``) is a floor:
    it also caps the very first backfill, but never moves ``since``
    backward once the incremental cursor has passed it. ``range_end`` is
    a ceiling applied on every run, not just the first — a real fixed
    window, per ``DECISIONS.md``. Same helper shape as
    ``extractors/gmail.py``'s ``_effective_window``.

    Returns:
        ``(effective_since, until)`` — either may be ``None``.
    """
    if range_start is not None:
        floor = datetime.combine(range_start, time.min, tzinfo=UTC)
        since = floor if since is None else max(since, floor)
    until = None
    if range_end is not None:
        until = datetime.combine(range_end + timedelta(days=1), time.min, tzinfo=UTC)
    return since, until


def extract_new_items(
    since: datetime | None = None, on_progress: OnProgress | None = None
) -> list[ExtractedItem]:
    """Extract GitHub activity — commits, PRs, issues, READMEs, stars.

    Args:
        since: Only include items created/updated after this time. ``None``
            (the first-ever run) includes each repository's full history —
            same "first run backfills everything" behavior as the other
            extractors. Combined with ``config/.env``'s
            ``GITHUB_DATE_RANGE_START``/``GITHUB_DATE_RANGE_END``, if set —
            see :func:`_effective_window`.
        on_progress: Called once per repo (``current``, ``total=len(repos)``,
            ``label=full_name``) — repo count is known upfront regardless
            of scope, unlike the items within each repo. Returning
            ``False`` stops after the repo just reported, before starting
            the next one. See ``extractors/base.py``, ``DECISIONS.md``.

    Returns:
        One item per commit/PR/issue/README/starred-repo surviving
        ``since`` filtering. A single repo, or a single category within a
        repo, failing (deleted mid-run, access revoked, a malformed
        response) is logged and skipped rather than aborting the whole
        run — same reasoning as every other extractor here.

    Raises:
        ExtractorError: If ``GITHUB_TOKEN`` isn't configured, or listing
            accessible repositories fails outright (bad token, unreachable
            API) — a source-level failure the daily batch records and
            moves past.
    """
    settings = get_settings().env
    token = settings.github_token
    if not token:
        raise ExtractorError("GITHUB_TOKEN is not configured")

    since, until = _effective_window(
        since, settings.github_date_range_start, settings.github_date_range_end
    )

    with httpx.Client(
        base_url=_API_BASE,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": _API_VERSION,
        },
        timeout=30.0,
    ) as client:
        repos = settings.github_repos_list or _list_accessible_repos(client)
        logger.info(
            "GitHub extraction starting (since=%s, until=%s, scope=%s)",
            since,
            until,
            (
                f"{len(repos)} configured repo(s)"
                if settings.github_repos_list
                else f"{len(repos)} accessible repo(s)"
            ),
        )

        items: list[ExtractedItem] = []
        try:
            items.extend(_extract_starred_repos(client, since, until))
        except Exception as exc:
            logger.warning("Could not extract starred repos: %s", exc)

        for index, full_name in enumerate(repos, start=1):
            try:
                items.extend(_extract_repo_items(client, full_name, since, until))
            except Exception as exc:
                logger.warning("Could not process GitHub repo %s: %s", full_name, exc)
            if on_progress is not None and not on_progress(
                index, len(repos), full_name
            ):
                logger.info("GitHub extraction stopped early (cancelled)")
                break

        logger.info(
            "GitHub extraction finished: %d repo(s) scanned, %d item(s) extracted",
            len(repos),
            len(items),
        )
        return items


def _list_accessible_repos(client: httpx.Client) -> list[str]:
    try:
        full_names: list[str] = []
        page = 1
        while True:
            response = client.get(
                "/user/repos",
                params={
                    "per_page": _PER_PAGE,
                    "page": page,
                    "affiliation": "owner,collaborator,organization_member",
                },
            )
            response.raise_for_status()
            batch = response.json()
            full_names.extend(repo["full_name"] for repo in batch)
            if len(batch) < _PER_PAGE:
                return full_names
            page += 1
    except httpx.HTTPError as exc:
        raise ExtractorError(
            f"Could not list accessible GitHub repositories: {exc}"
        ) from exc


def _extract_repo_items(
    client: httpx.Client,
    full_name: str,
    since: datetime | None,
    until: datetime | None,
) -> list[ExtractedItem]:
    items: list[ExtractedItem] = []
    for extract in (
        _extract_readme,
        _extract_commits,
        _extract_pull_requests,
        _extract_issues,
    ):
        try:
            items.extend(extract(client, full_name, since, until))
        except Exception as exc:
            logger.warning(
                "Could not extract %s for %s: %s", extract.__name__, full_name, exc
            )
    return items


def _extract_readme(
    client: httpx.Client,
    full_name: str,
    since: datetime | None,
    until: datetime | None,
) -> list[ExtractedItem]:
    # `until` is accepted (for a uniform call shape with the other
    # extract_* functions in _extract_repo_items()'s loop) but unused — a
    # README is a single always-current document, not a dated feed, so an
    # end-date cap doesn't apply to it. See DECISIONS.md.
    del until
    response = client.get(f"/repos/{full_name}/readme")
    if response.status_code == 404:
        return []
    response.raise_for_status()
    data = response.json()
    content = base64.b64decode(data["content"]).decode("utf-8", errors="replace")
    if not content.strip():
        return []

    last_modified = _readme_last_modified(client, full_name, data["path"])
    if since is not None and last_modified is not None and last_modified <= since:
        return []

    repo = client.get(f"/repos/{full_name}")
    repo.raise_for_status()
    repo_data = repo.json()
    description = repo_data.get("description") or ""
    topics = repo_data.get("topics") or []
    prefix_parts = [part for part in (description, ", ".join(topics)) if part]
    text = "\n\n".join([*prefix_parts, content]) if prefix_parts else content

    return [
        ExtractedItem(
            source_type=SOURCE_TYPE,
            source_ref_id=f"{full_name}:readme",
            title=f"{full_name} — README",
            url_or_path=data.get("html_url", ""),
            raw_text=text,
            author_or_sender=None,
            created_at=None,
            last_edited_at=last_modified,
        )
    ]


def _readme_last_modified(
    client: httpx.Client, full_name: str, path: str
) -> datetime | None:
    """The README's own last-changed date, via its most recent commit.

    Lets the README participate in the same ``since`` filtering as every
    other item, rather than being unconditionally re-extracted (and
    re-embedded) on every single run regardless of whether it changed —
    "periodically re-checked for updates" per the spec, not "every run."
    """
    response = client.get(
        f"/repos/{full_name}/commits", params={"path": path, "per_page": 1}
    )
    if response.status_code != 200:
        return None
    batch = response.json()
    if not batch:
        return None
    return _parse_timestamp(batch[0]["commit"]["committer"]["date"])


def _extract_commits(
    client: httpx.Client,
    full_name: str,
    since: datetime | None,
    until: datetime | None,
) -> list[ExtractedItem]:
    params: dict = {"per_page": _PER_PAGE}
    if since is not None:
        params["since"] = _to_github_timestamp(since)
    if until is not None:
        params["until"] = _to_github_timestamp(until)

    items: list[ExtractedItem] = []
    page = 1
    while True:
        params["page"] = page
        response = client.get(f"/repos/{full_name}/commits", params=params)
        if response.status_code == 409:
            # Empty repo (no commits yet) — GitHub returns 409, not an
            # empty list.
            return items
        response.raise_for_status()
        batch = response.json()
        for commit in batch:
            items.append(_commit_to_item(client, full_name, commit))
        if len(batch) < _PER_PAGE:
            return items
        page += 1


def _commit_to_item(
    client: httpx.Client, full_name: str, commit: dict
) -> ExtractedItem:
    sha = commit["sha"]
    message = commit["commit"]["message"]
    author = commit["commit"].get("author") or {}
    committed_at = _parse_timestamp(commit["commit"]["committer"]["date"])
    changed_files = _commit_changed_files(client, full_name, sha)

    text = message
    if changed_files:
        text += "\n\nChanged files:\n" + "\n".join(f"- {f}" for f in changed_files)

    first_line = message.splitlines()[0] if message else sha[:7]
    return ExtractedItem(
        source_type=SOURCE_TYPE,
        source_ref_id=f"{full_name}:commit:{sha}",
        title=f"{full_name}: {first_line[:200]}",
        url_or_path=commit.get("html_url", ""),
        raw_text=text,
        author_or_sender=author.get("name"),
        created_at=committed_at,
        last_edited_at=committed_at,
    )


def _commit_changed_files(client: httpx.Client, full_name: str, sha: str) -> list[str]:
    try:
        response = client.get(f"/repos/{full_name}/commits/{sha}")
        response.raise_for_status()
        return [f["filename"] for f in response.json().get("files", [])]
    except httpx.HTTPError as exc:
        logger.debug("Could not fetch changed files for %s@%s: %s", full_name, sha, exc)
        return []


def _extract_pull_requests(
    client: httpx.Client,
    full_name: str,
    since: datetime | None,
    until: datetime | None,
) -> list[ExtractedItem]:
    items: list[ExtractedItem] = []
    page = 1
    while True:
        response = client.get(
            f"/repos/{full_name}/pulls",
            params={
                "state": "all",
                "sort": "created",
                "direction": "desc",
                "per_page": _PER_PAGE,
                "page": page,
            },
        )
        response.raise_for_status()
        batch = response.json()
        if not batch:
            return items

        reached_cutoff = False
        for pr in batch:
            created_at = _parse_timestamp(pr["created_at"])
            if since is not None and created_at is not None and created_at <= since:
                # Sorted newest-first by creation, so every later PR in
                # this page (and every subsequent page) is older still.
                reached_cutoff = True
                break
            if until is not None and created_at is not None and created_at > until:
                # Newer than the configured end date — skip it, but don't
                # treat it as the cutoff: an older PR later in this same
                # page (or a later page) may still be within range.
                continue
            items.append(_pr_to_item(client, full_name, pr, created_at))

        if reached_cutoff or len(batch) < _PER_PAGE:
            return items
        page += 1


def _pr_to_item(
    client: httpx.Client, full_name: str, pr: dict, created_at: datetime | None
) -> ExtractedItem:
    number = pr["number"]
    body = pr.get("body") or ""
    text = f"{pr['title']}\n\n{body}".strip()

    comments = _pr_review_comments(client, full_name, number)
    if comments:
        text += "\n\nReview comments:\n" + "\n".join(f"- {c}" for c in comments)

    return ExtractedItem(
        source_type=SOURCE_TYPE,
        source_ref_id=f"{full_name}:pr:{number}",
        title=f"{full_name}#{number}: {pr['title']}",
        url_or_path=pr.get("html_url", ""),
        raw_text=text,
        author_or_sender=(pr.get("user") or {}).get("login"),
        created_at=created_at,
        last_edited_at=_parse_timestamp(pr.get("updated_at")),
    )


def _pr_review_comments(client: httpx.Client, full_name: str, number: int) -> list[str]:
    try:
        response = client.get(
            f"/repos/{full_name}/pulls/{number}/comments",
            params={"per_page": _PER_PAGE},
        )
        response.raise_for_status()
        return [c["body"] for c in response.json() if c.get("body")]
    except httpx.HTTPError as exc:
        logger.debug(
            "Could not fetch review comments for %s#%d: %s", full_name, number, exc
        )
        return []


def _extract_issues(
    client: httpx.Client,
    full_name: str,
    since: datetime | None,
    until: datetime | None,
) -> list[ExtractedItem]:
    params: dict = {"state": "all", "per_page": _PER_PAGE}
    if since is not None:
        params["since"] = _to_github_timestamp(since)

    items: list[ExtractedItem] = []
    page = 1
    while True:
        params["page"] = page
        response = client.get(f"/repos/{full_name}/issues", params=params)
        response.raise_for_status()
        batch = response.json()
        for issue in batch:
            if "pull_request" in issue:
                # GitHub's issues endpoint also returns PRs; those are
                # handled separately by _extract_pull_requests().
                continue
            created_at = _parse_timestamp(issue.get("created_at"))
            if until is not None and created_at is not None and created_at > until:
                # The issues API has no server-side "until" param (unlike
                # commits) — filter client-side instead.
                continue
            items.append(_issue_to_item(full_name, issue))
        if len(batch) < _PER_PAGE:
            return items
        page += 1


def _issue_to_item(full_name: str, issue: dict) -> ExtractedItem:
    number = issue["number"]
    body = issue.get("body") or ""
    text = f"{issue['title']}\n\n{body}".strip()
    return ExtractedItem(
        source_type=SOURCE_TYPE,
        source_ref_id=f"{full_name}:issue:{number}",
        title=f"{full_name}#{number}: {issue['title']}",
        url_or_path=issue.get("html_url", ""),
        raw_text=text,
        author_or_sender=(issue.get("user") or {}).get("login"),
        created_at=_parse_timestamp(issue.get("created_at")),
        last_edited_at=_parse_timestamp(issue.get("updated_at")),
    )


def _extract_starred_repos(
    client: httpx.Client, since: datetime | None, until: datetime | None
) -> list[ExtractedItem]:
    items: list[ExtractedItem] = []
    page = 1
    while True:
        response = client.get(
            "/user/starred",
            params={"per_page": _PER_PAGE, "page": page},
            # This Accept header (in place of the client's default) is
            # what makes GitHub include `starred_at` on each entry,
            # wrapping the repo under a `repo` key rather than returning
            # the repo object directly.
            headers={"Accept": "application/vnd.github.star+json"},
        )
        response.raise_for_status()
        batch = response.json()
        for entry in batch:
            starred_at = _parse_timestamp(entry.get("starred_at"))
            if since is not None and starred_at is not None and starred_at <= since:
                continue
            if until is not None and starred_at is not None and starred_at > until:
                continue
            items.append(_starred_repo_to_item(entry["repo"], starred_at))
        if len(batch) < _PER_PAGE:
            return items
        page += 1


def _starred_repo_to_item(repo: dict, starred_at: datetime | None) -> ExtractedItem:
    full_name = repo["full_name"]
    description = repo.get("description") or ""
    topics = repo.get("topics") or []
    parts = [description]
    if topics:
        parts.append("Topics: " + ", ".join(topics))
    text = "\n".join(part for part in parts if part) or full_name
    return ExtractedItem(
        source_type=SOURCE_TYPE,
        source_ref_id=f"{full_name}:starred",
        title=f"Starred: {full_name}",
        url_or_path=repo.get("html_url", ""),
        raw_text=text,
        author_or_sender=None,
        created_at=starred_at,
        last_edited_at=starred_at,
    )


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _to_github_timestamp(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")
