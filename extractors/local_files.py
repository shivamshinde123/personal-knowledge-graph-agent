"""Local file extractor: watched folders → normalized items.

Per ``docs/Data_Extraction_Specification.docx`` section 3: during the daily
batch, files under ``settings.env.watch_dirs`` with a modification time after
``since`` are read and their text extracted. Only the extracted text and the
file path are kept — the original file is never copied into the system.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

import docx
from pypdf import PdfReader

from config.settings import get_settings
from extractors.base import ExtractedItem, OnProgress

logger = logging.getLogger(__name__)

SOURCE_TYPE = "local_file"

_EXTRACTORS = {
    ".pdf": lambda path: "\n\n".join(
        page.extract_text() or "" for page in PdfReader(path).pages
    ),
    ".docx": lambda path: "\n".join(p.text for p in docx.Document(path).paragraphs),
    ".txt": lambda path: path.read_text(encoding="utf-8", errors="replace"),
    ".md": lambda path: path.read_text(encoding="utf-8", errors="replace"),
}


def extract_new_items(
    since: datetime | None = None, on_progress: OnProgress | None = None
) -> list[ExtractedItem]:
    """Extract text from files modified after ``since`` in the watched folders.

    Args:
        since: Only include files modified after this time. ``None`` (the
            first-ever run) includes every matching file.
        on_progress: Called once per candidate file (``current``,
            ``total``, ``label=path.name``) — the matching-suffix file
            count across every watch dir is a cheap local filesystem walk,
            done upfront so ``total`` is always known. Returning ``False``
            stops after the file just reported. See
            ``extractors/base.py``, ``DECISIONS.md``.

    Returns:
        One item per successfully read file. A file that can't be read (a
        corrupted PDF, a permissions error, an unreadable stat) is logged
        and skipped rather than aborting the rest of the scan.
    """
    watch_dirs = get_settings().env.watch_dirs
    candidates: list[Path] = []
    for directory in watch_dirs:
        if not directory.is_dir():
            logger.warning("Watch directory does not exist, skipping: %s", directory)
            continue
        candidates.extend(
            path
            for path in sorted(directory.rglob("*"))
            if path.suffix.lower() in _EXTRACTORS and path.is_file()
        )

    items: list[ExtractedItem] = []
    for index, path in enumerate(candidates, start=1):
        item = _extract_item(path, since)
        if item is not None:
            items.append(item)
        if on_progress is not None and not on_progress(
            index, len(candidates), path.name
        ):
            logger.info("Local files extraction stopped early (cancelled)")
            break
    return items


def _extract_item(path: Path, since: datetime | None) -> ExtractedItem | None:
    extractor = _EXTRACTORS.get(path.suffix.lower())
    if extractor is None or not path.is_file():
        return None
    try:
        modified_at = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    except OSError as exc:
        logger.warning("Could not stat %s: %s", path, exc)
        return None
    if since is not None and modified_at <= since:
        return None
    try:
        text = extractor(path)
    except Exception as exc:
        logger.warning("Could not extract text from %s: %s", path, exc)
        return None
    if not text.strip():
        return None
    return ExtractedItem(
        source_type=SOURCE_TYPE,
        source_ref_id=str(path),
        title=path.name,
        url_or_path=str(path),
        raw_text=text,
        author_or_sender=None,
        created_at=_created_at(path),
        last_edited_at=modified_at,
    )


def _created_at(path: Path) -> datetime | None:
    """Best-effort file creation time.

    ``st_ctime`` is creation time on Windows but metadata-change time on
    Linux/Mac, where true creation time isn't available without a
    platform-specific extension. Since this is close enough for a personal
    tool and never used for anything beyond display, no such extension is
    used here — see DECISIONS.md.
    """
    try:
        return datetime.fromtimestamp(path.stat().st_ctime, tz=UTC)
    except OSError:
        return None
