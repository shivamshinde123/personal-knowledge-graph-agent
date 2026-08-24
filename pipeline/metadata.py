"""Metadata generation: LLM-derived ``project_name``/``topic`` for items.

Per ``config.yaml``'s ``ingestion.batch_metadata_group_size``, items are
grouped before each LLM call rather than classified one at a time — this is
the cost-conscious batching ``docs/Technical_Design_Document.docx``
section 11 describes for ingestion-time LLM usage.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from config.settings import get_settings
from extractors.base import ExtractedItem
from providers.base import ItemMetadata, ProviderError, get_provider

logger = logging.getLogger(__name__)

_MAX_CHARS_PER_ITEM = 2000


def generate_metadata(items: Sequence[ExtractedItem]) -> list[ItemMetadata]:
    """Derive ``project_name``/``topic`` for a batch of items.

    Groups items into calls of ``config.yaml``'s
    ``ingestion.batch_metadata_group_size``, so classifying many items costs
    far fewer LLM calls than one per item would. A group whose call fails
    after retries degrades to empty metadata for just that group rather than
    aborting the whole batch — ``project_name``/``topic`` are optional
    everywhere they're stored, so one bad group shouldn't cost every other
    item in the run its metadata too.

    Args:
        items: The items to classify, in the order results are returned.

    Returns:
        One :class:`ItemMetadata` per input item, same order, same length.
    """
    if not items:
        return []
    provider = get_provider("metadata")
    group_size = get_settings().config.ingestion.batch_metadata_group_size

    results: list[ItemMetadata] = []
    for start in range(0, len(items), group_size):
        group = items[start : start + group_size]
        texts = [_representative_text(item) for item in group]
        try:
            results.extend(provider.generate_metadata(texts))
        except ProviderError as exc:
            logger.warning(
                "Metadata generation failed for %d item(s), leaving them "
                "unclassified: %s",
                len(group),
                exc,
            )
            results.extend(ItemMetadata(project_name=None, topic=None) for _ in group)
    return results


def _representative_text(item: ExtractedItem) -> str:
    """Truncate an item's text to a size reasonable for a batched LLM prompt."""
    return item.raw_text.strip()[:_MAX_CHARS_PER_ITEM]
