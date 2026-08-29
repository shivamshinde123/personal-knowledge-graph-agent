"""LangGraph wiring: the agent's single entry point.

Per ``docs/Technical_Design_Document.docx`` section 8.2/8.3, builds a
``StateGraph`` running the Router, Vector Search, Keyword Search, Graph
Traversal, Merger, and Synthesizer nodes in order. A node whose
corresponding ``agent/router.py::RouteDecision`` flag is ``False`` runs as
a cheap no-op (contributing an empty hit list) rather than being excluded
from the graph via conditional edges — see ``DECISIONS.md`` for why.

Scope note: ``run()`` accepts ``session_id`` (per
``docs/API_Specification.docx``'s ``POST /api/query`` contract) but this
first pass does not yet implement multi-turn conversation memory —
follow-up question resolution ("tell me more about the second one") needs
its own state schema and prompt work, and is deliberately left as a
follow-up unit of work rather than bolted on here. See ``DECISIONS.md``.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import TypedDict
from uuid import uuid4

import neo4j
from chromadb.api.models.Collection import Collection
from langgraph.graph import END, START, StateGraph

from agent.graph_traversal import graph_traversal
from agent.merger import MergedResult, merge
from agent.router import RouteDecision, route
from agent.search_nodes import SearchHit, keyword_search_node, vector_search
from agent.synthesizer import Source, synthesize


class _AgentState(TypedDict):
    """The graph's shared state, threaded through every node."""

    question: str
    decision: RouteDecision
    vector_hits: list[SearchHit]
    keyword_hits: list[SearchHit]
    graph_hits: list[SearchHit]
    merged: list[MergedResult]
    answer: str
    sources: list[Source]


@dataclass(frozen=True, slots=True)
class QueryResult:
    """The agent's response to one question."""

    session_id: str
    answer: str
    sources: list[Source]
    retrieval_methods_used: list[str]


def run(
    conn: sqlite3.Connection,
    collection: Collection,
    driver: neo4j.Driver,
    question: str,
    session_id: str | None = None,
) -> QueryResult:
    """Answer one question by running it through the full agent graph.

    Args:
        conn: An open SQLite connection.
        collection: An open Chroma collection.
        driver: An open Neo4j driver.
        question: The user's natural language question.
        session_id: An existing session to continue, or ``None`` to start
            a new one (a fresh id is generated either way — see the module
            docstring's scope note on conversation memory).

    Returns:
        The synthesized answer, its sources, and which retrieval methods
        actually ran (per ``agent/router.py::route()``'s decision).
    """
    graph = _build_graph(conn, collection, driver)
    final_state = graph.invoke(
        {
            "question": question,
            "decision": RouteDecision(
                vector_search=False, keyword_search=False, graph_traversal=False
            ),
            "vector_hits": [],
            "keyword_hits": [],
            "graph_hits": [],
            "merged": [],
            "answer": "",
            "sources": [],
        }
    )
    decision = final_state["decision"]
    retrieval_methods_used = [
        name
        for name, used in (
            ("vector_search", decision.vector_search),
            ("keyword_search", decision.keyword_search),
            ("graph_traversal", decision.graph_traversal),
        )
        if used
    ]
    return QueryResult(
        session_id=session_id or str(uuid4()),
        answer=final_state["answer"],
        sources=final_state["sources"],
        retrieval_methods_used=retrieval_methods_used,
    )


def _build_graph(
    conn: sqlite3.Connection, collection: Collection, driver: neo4j.Driver
):
    """Build (but don't compile-cache) the StateGraph for one query.

    Rebuilt per call rather than held as a module-level singleton, since
    each call closes over this call's own ``conn``/``collection``/
    ``driver`` — cheap for a graph this small, and keeps the nodes simple
    closures instead of needing dependency injection via LangGraph's
    ``config`` parameter.
    """

    def router_node(state: _AgentState) -> dict:
        return {"decision": route(state["question"])}

    def vector_node(state: _AgentState) -> dict:
        if not state["decision"].vector_search:
            return {"vector_hits": []}
        return {"vector_hits": vector_search(collection, state["question"])}

    def keyword_node(state: _AgentState) -> dict:
        if not state["decision"].keyword_search:
            return {"keyword_hits": []}
        return {"keyword_hits": keyword_search_node(conn, state["question"])}

    def graph_node(state: _AgentState) -> dict:
        if not state["decision"].graph_traversal:
            return {"graph_hits": []}
        seeds = state["vector_hits"] + state["keyword_hits"]
        return {"graph_hits": graph_traversal(driver, seeds)}

    def merge_node(state: _AgentState) -> dict:
        merged = merge(state["vector_hits"], state["keyword_hits"], state["graph_hits"])
        return {"merged": merged}

    def synthesize_node(state: _AgentState) -> dict:
        result = synthesize(conn, state["question"], state["merged"])
        return {"answer": result.answer, "sources": result.sources}

    builder = StateGraph(_AgentState)
    builder.add_node("router", router_node)
    builder.add_node("vector_search", vector_node)
    builder.add_node("keyword_search", keyword_node)
    builder.add_node("graph_traversal", graph_node)
    builder.add_node("merge", merge_node)
    builder.add_node("synthesize", synthesize_node)

    builder.add_edge(START, "router")
    builder.add_edge("router", "vector_search")
    builder.add_edge("vector_search", "keyword_search")
    builder.add_edge("keyword_search", "graph_traversal")
    builder.add_edge("graph_traversal", "merge")
    builder.add_edge("merge", "synthesize")
    builder.add_edge("synthesize", END)

    return builder.compile()
