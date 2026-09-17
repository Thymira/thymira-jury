"""Artifact: anything a run produces that evidence can point to."""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from thymira.schemas.base import ThymiraModel, utc_now
from thymira.schemas.enums import ArtifactKind
from thymira.schemas.ids import Id


class Artifact(ThymiraModel):
    """A content-addressed file in the ArtifactStore with its lineage.

    ``sha256`` makes tampering detectable; ``input_artifact_ids`` + ``execution_key`` make the
    artifact reproducible and let the runtime invalidate it when an input changes (the
    invalidation carries a reason and a timestamp instead of deleting the file).
    """

    id: Id
    run_id: Id
    name: str = Field(min_length=1, description="logical name, e.g. 'metrics.json'")
    kind: ArtifactKind = ArtifactKind.OTHER
    uri: str = Field(min_length=1, description="store-relative path or object URI")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    media_type: str | None = None
    produced_by: Id = Field(description="agent or tool call id")
    created_at: datetime = Field(default_factory=utc_now)
    input_artifact_ids: tuple[Id, ...] = ()
    execution_key: str | None = Field(
        default=None, description="digest of (code, parameters, inputs) that produced it"
    )
    valid: bool = True
    invalidated_reason: str | None = None
    invalidated_at: datetime | None = None
