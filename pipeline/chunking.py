"""Chunking: splits an item's extracted text into pieces sized for embedding.

Text is split at natural boundaries (paragraph breaks) wherever they exist,
per ``docs/Data_Extraction_Specification.docx`` section 4.3 ("long pages are
split at natural boundaries... rather than arbitrary character counts").
Only a paragraph with no internal boundary to split on — long unstructured
text — falls back to a fixed sliding window with overlap.
"""

from __future__ import annotations

import re

from config.settings import ChunkingConfig, get_settings

_PARAGRAPH_BREAK = re.compile(r"\n\s*\n")


def chunk_text(text: str) -> list[str]:
    """Split text into chunks sized around ``config.yaml``'s chunking settings.

    Paragraphs are packed greedily into a chunk until the next paragraph
    would push it over ``target_chunk_size_tokens``, then a new chunk
    starts. A single paragraph already over the target on its own is split
    with a sliding window using ``chunk_overlap_tokens`` of overlap between
    windows, since there's no natural boundary inside it to prefer instead.

    Args:
        text: The item's full extracted text.

    Returns:
        The text's chunks, in order. Empty for blank/whitespace-only input.
    """
    config = get_settings().config.chunking
    paragraphs = [p.strip() for p in _PARAGRAPH_BREAK.split(text.strip()) if p.strip()]
    if not paragraphs:
        return []

    chunks: list[str] = []
    current: list[str] = []
    current_tokens = 0

    for paragraph in paragraphs:
        paragraph_tokens = _count_tokens(paragraph)
        if paragraph_tokens > config.target_chunk_size_tokens:
            if current:
                chunks.append("\n\n".join(current))
                current, current_tokens = [], 0
            chunks.extend(_sliding_window_split(paragraph, config))
            continue
        if (
            current
            and current_tokens + paragraph_tokens > config.target_chunk_size_tokens
        ):
            chunks.append("\n\n".join(current))
            current, current_tokens = [], 0
        current.append(paragraph)
        current_tokens += paragraph_tokens

    if current:
        chunks.append("\n\n".join(current))
    return chunks


def _count_tokens(text: str) -> int:
    """Approximate token count via whitespace word count.

    Chunk sizing here is a coarse knob for keeping embedding input a
    reasonable size, not a hard context-window limit tied to one specific
    tokenizer (sentence-transformers' own tokenizer differs from any
    general-purpose one anyway). A dependency-free word count keeps
    chunking fully local, with no risk of needing network access to
    download a real tokenizer's vocabulary on a machine running
    ``provider_mode: fully_local``. See DECISIONS.md.
    """
    return len(text.split())


def _sliding_window_split(paragraph: str, config: ChunkingConfig) -> list[str]:
    """Split one oversized paragraph into overlapping fixed-size windows."""
    words = paragraph.split()
    target = config.target_chunk_size_tokens
    step = max(target - config.chunk_overlap_tokens, 1)
    windows: list[str] = []
    for start in range(0, len(words), step):
        windows.append(" ".join(words[start : start + target]))
        if start + target >= len(words):
            break
    return windows
