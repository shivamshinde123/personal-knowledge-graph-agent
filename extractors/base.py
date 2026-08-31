"""Shared extractor contract.

The common intermediate format every source extractor normalizes into
before the pipeline sees it. Every extractor module (``local_files.py``,
``notion.py``, ...) exposes one function,
``extract_new_items(since, on_progress=None) -> list[ExtractedItem]``, and
depends on nothing outside this module and ``config.settings`` — see
``docs/Component_Map.docx``'s dependency rule that extractors never import
from ``storage/`` or ``agent/`` directly, and
``docs/Data_Extraction_Specification.docx`` section 2 for the field
contract ``ExtractedItem`` implements.

``on_progress``, when given, is called with ``(current, total, label)`` as
the extractor works through countable units (repos, files, pages — total
is ``None`` when it isn't known upfront) and returns ``True`` to keep
going or ``False`` to stop early. One mechanism serves two purposes: a
live progress readout (``scheduler/daily_batch.py`` forwards each call
into ``ingestion_runs.current_item``) and cooperative cancellation (the
closure it hands in also checks a cancellation flag) — see
``OnProgress``, ``DECISIONS.md``. Only ``extractors/github.py``,
``local_files.py``, and ``notion.py`` call it as of 2026-08-31;
``gmail.py``/``calendar.py`` accept and ignore it, for a uniform call
shape in ``scheduler/daily_batch.py``'s ``_EXTRACTORS`` list.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

# (current, total, label) -> keep_going. total is None when it isn't known
# upfront. See the module docstring.
OnProgress = Callable[[int, int | None, str], bool]


class ExtractorError(Exception):
    """Raised when an extractor cannot complete its run for its source.

    A per-item failure (one unreadable file, one malformed email) should be
    logged and skipped rather than raised, so one bad item doesn't lose the
    rest of the source; this is reserved for a source-level failure (the
    watch directory is missing, the API is unreachable, auth failed) that
    the daily batch records in ``ingestion_runs.error_log`` before moving on
    to the next source.
    """


@dataclass(slots=True)
class ExtractedItem:
    """One normalized item, in the common format defined across all sources."""

    source_type: str
    source_ref_id: str
    title: str
    url_or_path: str
    raw_text: str
    author_or_sender: str | None = None
    created_at: datetime | None = None
    last_edited_at: datetime | None = None
