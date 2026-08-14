"""Storage layer: all persistence logic for SQLite, Chroma, and Neo4j.

This is the system's leaf layer — it has no outgoing dependencies on any other
layer. These modules are the only ones permitted to hold a live database
connection or client object.
"""
