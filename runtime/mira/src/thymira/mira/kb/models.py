"""Contracts for MIRA's read-only regulation knowledge base."""

from __future__ import annotations

import hashlib
from typing import Protocol

from pydantic import Field, model_validator

from thymira.schemas import Framework, ThymiraModel


class RegulationChunk(ThymiraModel):
    """One hash-verified regulatory text chunk from a local JSONL corpus."""

    source_id: str = Field(min_length=1)
    version: str = Field(default="unknown", min_length=1)
    framework: Framework
    location: str = Field(min_length=1)
    text: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @classmethod
    def from_text(
        cls,
        *,
        source_id: str,
        framework: Framework,
        location: str,
        text: str,
        version: str = "unknown",
    ) -> RegulationChunk:
        """Build a chunk whose ``sha256`` is the digest of its exact UTF-8 ``text``."""
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return cls(
            source_id=source_id,
            version=version,
            framework=framework,
            location=location,
            text=text,
            sha256=digest,
        )


def verifies_sha256(chunk: RegulationChunk) -> bool:
    """Return whether ``chunk.sha256`` matches its exact UTF-8 text."""
    digest = hashlib.sha256(chunk.text.encode("utf-8")).hexdigest()
    return digest == chunk.sha256


class RegulationStore(Protocol):
    """Read-only search boundary for regulatory chunks."""

    def search(
        self,
        query: str,
        *,
        framework: Framework | None,
        k: int,
    ) -> tuple[RegulationSearchResult, ...]:
        """Return at most ``k`` matches with citable text and ranking provenance."""
        ...


class RegulationSearchResult(ThymiraModel):
    """One citable regulation match with its query score and backend provenance."""

    source_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    framework: Framework
    location: str = Field(min_length=1)
    fragment: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    score: float = Field(ge=0.0)
    backend: str = Field(min_length=1)

    @property
    def text(self) -> str:
        """Expose the fragment under the chunk name for read-only compatibility."""
        return self.fragment

    @classmethod
    def from_chunk(
        cls,
        chunk: RegulationChunk,
        *,
        score: float,
        backend: str,
    ) -> RegulationSearchResult:
        """Build a public match from a hash-verified chunk and backend ranking facts."""
        return cls(
            source_id=chunk.source_id,
            version=chunk.version,
            framework=chunk.framework,
            location=chunk.location,
            fragment=chunk.text,
            sha256=chunk.sha256,
            score=score,
            backend=backend,
        )


class RegulationSource(ThymiraModel):
    """A named, versioned regulatory or methodological source a requirement cites."""

    title: str = Field(min_length=1)
    version: str = Field(min_length=1)
    reference: str = Field(min_length=1)


class RequirementControlMapping(ThymiraModel):
    """One governance requirement mapped to the MIRA control(s) whose evidence covers it.

    This is the stable schema the Compliance audit agent (AUD-COMPLIANCE) and the
    requirements-coverage control (CTRL-COVERAGE) read: a requirement, its named source and
    location, the deterministic control ids MIRA evaluates for it, and the evidence those
    controls fold. It is an audit-preparation aid, not a guarantee of compliance.
    """

    framework: Framework
    requirement_id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    location: str = Field(min_length=1)
    requirement: str = Field(min_length=1)
    control_ids: tuple[str, ...] = Field(min_length=1)
    evidence: str = Field(min_length=1)


class RequirementsControls(ThymiraModel):
    """The requirements->controls mapping document, keyed by framework + requirement id."""

    schema_version: str = Field(min_length=1)
    description: str = Field(min_length=1)
    disclaimer: str = Field(min_length=1)
    sources: dict[str, RegulationSource] = Field(min_length=1)
    requirements: tuple[RequirementControlMapping, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_keys_and_sources(self) -> RequirementsControls:
        """Reject a duplicate (framework, requirement_id) key or an unknown cited source."""
        seen: set[tuple[Framework, str]] = set()
        for entry in self.requirements:
            key = (entry.framework, entry.requirement_id)
            if key in seen:
                msg = f"duplicate requirement key: {entry.framework.value}/{entry.requirement_id}"
                raise ValueError(msg)
            seen.add(key)
            if entry.source not in self.sources:
                msg = f"requirement {entry.requirement_id} cites unknown source {entry.source!r}"
                raise ValueError(msg)
        return self


class CorpusSeedEntry(ThymiraModel):
    """One seed regulatory or methodological text, hashed into a RegulationChunk at ingest."""

    source_id: str = Field(min_length=1)
    framework: Framework
    location: str = Field(min_length=1)
    text: str = Field(min_length=1)


class RegulationCorpus(ThymiraModel):
    """A small, reviewed seed corpus of citable regulatory and methodological texts."""

    version: str = Field(min_length=1)
    entries: tuple[CorpusSeedEntry, ...] = Field(min_length=1)


__all__ = [
    "CorpusSeedEntry",
    "RegulationChunk",
    "RegulationCorpus",
    "RegulationSearchResult",
    "RegulationSource",
    "RegulationStore",
    "RequirementControlMapping",
    "RequirementsControls",
    "verifies_sha256",
]
