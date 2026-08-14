"""Storage layer: all persistence logic for SQLite, Chroma, and Neo4j.

This is the system's foundation layer: it depends on no other functional layer,
and every layer above it depends on it. Its only import outside itself is
``config.settings``, the dependency-free configuration leaf that every layer is
free to read. These modules are the only ones permitted to hold a live database
connection or client object.
"""
