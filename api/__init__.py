"""Interface layer: the FastAPI application.

Exposes the agent over HTTP on localhost. Depends only on the agent's public
entrypoint, never reaching into ``storage`` or ``providers`` directly.
"""
