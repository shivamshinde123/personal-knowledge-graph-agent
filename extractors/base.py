"""Shared extractor contract.

The common intermediate format every source extractor normalizes into
before the pipeline sees it. Every extractor module (``local_files.py``,
``notion.py``, ...) exposes one function,
``extract_new_items(since) -> list[ExtractedItem]``, and depends on nothing
outside this module and ``config.settings`` — see ``docs/Component_Map.docx``'s
dependency rule that extractors never import from ``storage/`` or ``agent/``
directly, and ``docs/Data_Extraction_Specification.docx`` section 2 for the
field contract this dataclass implements.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


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
