"""Cross-source content-quality filtering.

Runs on every extracted item, regardless of source, before chunking.
Source-specific noise rules — browser history's domain blocklist and visit
count threshold, Gmail's excluded labels — are applied inside their own
extractors instead, since only the extractor has access to that
source-native data (a URL's domain, a message's labels) before it's
normalized into the common ``ExtractedItem`` shape. See DECISIONS.md.
"""

from __future__ import annotations

from config.settings import get_settings
from extractors.base import ExtractedItem


def apply_noise_filter(item: ExtractedItem) -> bool:
    """Decide whether an extracted item is worth carrying into the pipeline.

    Args:
        item: The item to evaluate.

    Returns:
        ``True`` if the item should proceed to chunking, ``False`` if it
        should be dropped as noise (empty, or too short to be useful).
    """
    text = item.raw_text.strip()
    if not text:
        return False
    min_length = get_settings().config.filters.min_content_length
    return len(text) >= min_length
