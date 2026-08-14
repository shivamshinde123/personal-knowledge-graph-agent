"""Query and reasoning layer: the LangGraph agent.

Wires the Router, Vector Search, Keyword Search, Graph Traversal, Merger, and
Synthesizer nodes. Depends on ``storage`` and ``providers`` only — never on
``extractors`` or ``pipeline``.
"""
