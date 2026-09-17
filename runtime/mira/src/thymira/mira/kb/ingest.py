"""Ingest the requirements->controls mapping and the seed corpus into a RegulationStore JSONL.

The mapping (``docs/governance/requirements_controls.json``) and the packaged seed corpus are
loaded, validated, hashed into :class:`RegulationChunk` records, and written as one compact,
key-sorted JSON object per line -- exactly the format ``LocalRegulationStore`` (KB-01) parses and
hash-verifies on load. The command wrapper lives in ``scripts/ingest_regulation.py``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

from thymira.mira.kb.models import (
    CorpusSeedEntry,
    RegulationChunk,
    RegulationCorpus,
    RequirementsControls,
)

CORPUS_PACKAGE = "thymira.mira.kb.corpus"
CORPUS_RESOURCE = "regulation_corpus.json"


@dataclass(frozen=True, slots=True)
class IngestReport:
    """What one ingest produced: the JSONL path written and the chunk counts."""

    out_path: Path
    chunk_count: int
    requirement_count: int
    corpus_count: int


def load_requirements(path: Path | str) -> RequirementsControls:
    """Load and validate the requirements->controls mapping document."""
    return RequirementsControls.model_validate(_read_json(Path(path)))


def load_seed_corpus(path: Path | str | None = None) -> RegulationCorpus:
    """Load the seed corpus, defaulting to the reviewed corpus packaged with ``thymira.mira``."""
    if path is None:
        resource = resources.files(CORPUS_PACKAGE).joinpath(CORPUS_RESOURCE)
        data: Any = json.loads(resource.read_text(encoding="utf-8"))
    else:
        data = _read_json(Path(path))
    return RegulationCorpus.model_validate(data)


def build_chunks(
    mapping: RequirementsControls,
    corpus: RegulationCorpus,
) -> tuple[RegulationChunk, ...]:
    """Turn every corpus entry and every requirement into a hash-verified searchable chunk."""
    chunks: list[RegulationChunk] = [
        _corpus_chunk(entry, corpus.version) for entry in corpus.entries
    ]
    chunks.extend(
        RegulationChunk.from_text(
            source_id=_requirement_source_id(requirement.requirement_id),
            framework=requirement.framework,
            location=requirement.location,
            text=requirement.requirement,
            version=mapping.sources[requirement.source].version,
        )
        for requirement in mapping.requirements
    )
    seen: set[str] = set()
    for chunk in chunks:
        if chunk.source_id in seen:
            msg = f"duplicate chunk source_id: {chunk.source_id!r}"
            raise ValueError(msg)
        seen.add(chunk.source_id)
    return tuple(chunks)


def write_jsonl(chunks: tuple[RegulationChunk, ...], out_path: Path | str) -> None:
    """Write chunks as one compact, key-sorted JSON object per line (LF-terminated)."""
    destination = Path(out_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    lines = "".join(
        f"{json.dumps(chunk.model_dump(mode='json'), sort_keys=True)}\n" for chunk in chunks
    )
    destination.write_text(lines, encoding="utf-8", newline="\n")


def ingest(
    mapping_path: Path | str,
    out_path: Path | str,
    corpus_path: Path | str | None = None,
) -> IngestReport:
    """Load the mapping and seed corpus, build chunks, and write the RegulationStore JSONL."""
    mapping = load_requirements(mapping_path)
    corpus = load_seed_corpus(corpus_path)
    chunks = build_chunks(mapping, corpus)
    write_jsonl(chunks, out_path)
    return IngestReport(
        out_path=Path(out_path),
        chunk_count=len(chunks),
        requirement_count=len(mapping.requirements),
        corpus_count=len(corpus.entries),
    )


def _requirement_source_id(requirement_id: str) -> str:
    """Return the stable chunk id for a mapped requirement."""
    return f"req-{requirement_id}".casefold()


def _corpus_chunk(entry: CorpusSeedEntry, version: str) -> RegulationChunk:
    """Build a hash-verified chunk from one seed-corpus entry."""
    return RegulationChunk.from_text(
        source_id=entry.source_id,
        framework=entry.framework,
        location=entry.location,
        text=entry.text,
        version=version,
    )


def _read_json(path: Path) -> dict[str, Any]:
    """Read a JSON object from a file, rejecting a non-object top level."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        msg = f"{path}: expected a JSON object at the top level"
        raise TypeError(msg)
    return data


__all__ = [
    "CORPUS_PACKAGE",
    "CORPUS_RESOURCE",
    "IngestReport",
    "build_chunks",
    "ingest",
    "load_requirements",
    "load_seed_corpus",
    "write_jsonl",
]
