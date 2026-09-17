"""Read-only local regulation knowledge-base contracts and implementations.

``LocalRegulationStore`` (KB-01) and ``PgVectorRegulationStore`` (KB-04) both satisfy the
``RegulationStore`` protocol, so either backs the unchanged ``search_regulation`` tool. Search
results carry their source version, fragment, score, and backend. The pgvector backend imports its
PostgreSQL driver lazily, so importing this package needs no database.
"""

from thymira.mira.kb.local import LocalRegulationStore, load_default_regulation_store
from thymira.mira.kb.models import (
    CorpusSeedEntry,
    RegulationChunk,
    RegulationCorpus,
    RegulationSearchResult,
    RegulationSource,
    RegulationStore,
    RequirementControlMapping,
    RequirementsControls,
    verifies_sha256,
)
from thymira.mira.kb.pgvector import PgVectorRegulationStore, embed_text, ingest_pgvector

__all__ = [
    "CorpusSeedEntry",
    "LocalRegulationStore",
    "PgVectorRegulationStore",
    "RegulationChunk",
    "RegulationCorpus",
    "RegulationSearchResult",
    "RegulationSource",
    "RegulationStore",
    "RequirementControlMapping",
    "RequirementsControls",
    "embed_text",
    "ingest_pgvector",
    "load_default_regulation_store",
    "verifies_sha256",
]
