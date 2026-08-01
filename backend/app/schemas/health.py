"""Health and readiness payloads."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class HealthOut(BaseModel):
    status: str = Field(description="`ok` while the process is serving requests")
    service: str = Field(description="Application name")
    version: str = Field(description="Deployed application version")
    environment: str = Field(description="`development`, `staging` or `production`")

    model_config = {
        "json_schema_extra": {
            "example": {
                "status": "ok",
                "service": "CAFlow",
                "version": "0.1.0",
                "environment": "production",
            }
        }
    }


class ReadinessOut(HealthOut):
    checks: dict[str, Any] = Field(
        description=(
            "One entry per dependency: `ok`, whether it is `required` for readiness, "
            "and the error class when it failed."
        )
    )
    duration_ms: float = Field(description="How long the checks took")

    model_config = {
        "json_schema_extra": {
            "example": {
                "status": "ok",
                "service": "CAFlow",
                "version": "0.1.0",
                "environment": "production",
                "checks": {
                    "database": {"ok": True, "required": True, "error": None},
                    "broker": {"ok": True, "required": False, "error": None},
                },
                "duration_ms": 3.4,
            }
        }
    }
