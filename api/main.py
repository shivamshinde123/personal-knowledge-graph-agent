"""FastAPI app entrypoint: exposes the LangGraph agent over HTTP.

Run via ``uv run uvicorn api.main:app``, bound to ``settings.env.fastapi_port``
(``localhost`` only — see ``docs/System_Architecture_Document.docx`` section
6.2). Holds the long-lived SQLite connection, Chroma collection, and Neo4j
driver for the process's lifetime, opened once at startup and closed on
shutdown via FastAPI's lifespan context manager, and read by route handlers
from ``request.app.state`` — see ``DECISIONS.md``.

Per ``docs/File_Folder_Structure.docx`` section 4: a new API endpoint adds
a route module under ``api/routes/`` and registers it here.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from api.routes import health, query
from providers.base import ProviderError
from storage.chroma_store import VectorStoreError, get_collection
from storage.neo4j_store import GraphStoreError, get_driver
from storage.sqlite_store import StorageError, connect


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Open real, settings-derived storage clients for the app's lifetime."""
    app.state.conn = connect()
    app.state.collection = get_collection()
    app.state.driver = get_driver()
    try:
        yield
    finally:
        app.state.driver.close()
        app.state.conn.close()


def create_app(*, lifespan_fn=lifespan) -> FastAPI:
    """Build the FastAPI app, with every route module registered.

    Args:
        lifespan_fn: The lifespan context manager to use. Defaults to the
            real one above; tests substitute a no-op lifespan and set
            ``app.state`` directly to test doubles instead, so a test run
            never opens the real, settings-derived SQLite/Chroma/Neo4j
            connections — see ``tests/test_api/conftest.py``.
    """
    app = FastAPI(title="Personal Knowledge Graph Agent", lifespan=lifespan_fn)
    app.include_router(health.router, prefix="/api")
    app.include_router(query.router, prefix="/api")
    _register_exception_handlers(app)
    return app


def _register_exception_handlers(app: FastAPI) -> None:
    """Map every error to the ``{"error": str, "detail": str}`` shape.

    Per ``docs/API_Specification.docx`` section 2. The exception *types*
    caught here (``ProviderError``, ``VectorStoreError``, ``GraphStoreError``,
    ``StorageError``) are imported from ``storage``/``providers`` purely for
    HTTP status mapping, not to call anything in those layers — this is
    presentation-layer plumbing done once, here, not business logic
    scattered across route modules. See ``DECISIONS.md``.
    """

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={"error": "validation_error", "detail": str(exc)},
        )

    @app.exception_handler(ProviderError)
    async def handle_provider_error(
        request: Request, exc: ProviderError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=502, content={"error": "provider_error", "detail": str(exc)}
        )

    @app.exception_handler(VectorStoreError)
    @app.exception_handler(GraphStoreError)
    @app.exception_handler(StorageError)
    async def handle_storage_error(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(
            status_code=500, content={"error": "storage_error", "detail": str(exc)}
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(
            status_code=500, content={"error": "internal_error", "detail": str(exc)}
        )


app = create_app()
