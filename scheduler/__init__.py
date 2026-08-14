"""Scheduler layer: the daily batch entrypoint invoked by cron / Task Scheduler.

Coordinates the six extractors and the ingestion pipeline. Depends on
``extractors`` and ``pipeline``.
"""
