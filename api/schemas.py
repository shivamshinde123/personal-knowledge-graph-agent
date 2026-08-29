"""Pydantic request/response models for every API endpoint.

Per ``docs/API_Specification.docx``. One module, per
``docs/File_Folder_Structure.docx``'s documented layout — route modules
import the shapes they need from here rather than defining their own.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

ServiceStatus = Literal["ok", "error"]


class HealthResponse(BaseModel):
    """``GET /api/health`` response body."""

    status: Literal["ok", "degraded"]
    services: dict[str, ServiceStatus]
