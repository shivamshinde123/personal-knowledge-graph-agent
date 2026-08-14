"""Ingestion layer: one module per data source.

Each extractor pulls raw data from a single external source and normalizes it
into the shared intermediate item format defined in ``extractors/base.py``.
Extractors never import from ``storage`` or ``agent``; they only return
normalized items to their pipeline caller.
"""
