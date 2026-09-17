"""PostgreSQL + pgvector backend for the regulation knowledge base (KB-04).

:class:`PgVectorRegulationStore` satisfies the same :class:`~thymira.mira.kb.RegulationStore`
protocol as :class:`~thymira.mira.kb.LocalRegulationStore`, so it is a drop-in behind the unchanged
``search_regulation`` tool -- audit agents and the Tool Manager never learn which backend answers.

The stored vector is an experimental, deterministic lexical term-frequency hashing projection
(:func:`embed_text`), not a semantic embedding: it has no model, learned representation, or
language understanding. Cosine nearest-neighbour over that projection is therefore called
``lexical-vector`` search. ``psycopg`` is imported lazily inside the methods that talk to the
database, so importing this module (and ``thymira.mira``) never requires a live PostgreSQL nor
eagerly loads the driver.
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import TYPE_CHECKING

from thymira.mira.kb.models import RegulationChunk, RegulationSearchResult
from thymira.schemas import Framework

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence


_TOKEN_PATTERN = re.compile(r"[^\W_]+", flags=re.UNICODE)
_IDENTIFIER_PATTERN = re.compile(r"^[a-z_][a-z0-9_]*$")

DEFAULT_DIMENSIONS = 512
"""Lexical-vector dimensionality; wide enough that the fixture corpus avoids hash collisions."""

DEFAULT_TABLE = "regulation_chunk"
_BACKEND = "postgresql-pgvector-lexical-vector"

# Cosine distance of two non-negative term-frequency vectors sharing no token is exactly 1.0.
# A result at or above this bound shares nothing with the query and is excluded, mirroring
# LocalRegulationStore, which drops zero-score chunks.
_MAX_DISTANCE = 0.999999


def embed_text(text: str, dimensions: int = DEFAULT_DIMENSIONS) -> list[float]:
    """Return a deterministic lexical term-frequency hashing vector for ``text``.

    Each token is hashed into one of ``dimensions`` buckets and its frequency accumulated, then the
    vector is L2-normalised so cosine similarity reflects shared-token overlap. This is not a
    semantic embedding and does not capture synonyms or meaning. A text with no alphanumeric token
    produces the zero vector.
    """
    vector = [0.0] * dimensions
    for token in _TOKEN_PATTERN.findall(text.casefold()):
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        bucket = int.from_bytes(digest[:8], "big") % dimensions
        vector[bucket] += 1.0
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0.0:
        return vector
    return [value / norm for value in vector]


def _vector_literal(vector: Sequence[float]) -> str:
    """Render a vector as the ``'[v1,v2,...]'`` text literal pgvector parses for a ``vector``."""
    return "[" + ",".join(repr(value) for value in vector) + "]"


class PgVectorRegulationStore:
    """Experimental PostgreSQL + pgvector store using lexical vectors, not semantic embeddings."""

    def __init__(
        self,
        dsn: str,
        *,
        dimensions: int = DEFAULT_DIMENSIONS,
        table: str = DEFAULT_TABLE,
    ) -> None:
        """Store the connection string and schema parameters (no connection is opened here).

        Raises:
            ValueError: If ``table`` is not a bare SQL identifier or ``dimensions`` is not positive.
        """
        if not _IDENTIFIER_PATTERN.match(table):
            raise ValueError(f"table must be a bare identifier, got {table!r}")
        if dimensions <= 0:
            raise ValueError("dimensions must be greater than zero")
        self._dsn = dsn
        self._dimensions = dimensions
        self._table = table

    def initialize(self) -> None:
        """Create the ``vector`` extension, the regulation table, and its cosine vector index."""
        import psycopg  # noqa: PLC0415  # lazy: keep the driver out of import time
        from psycopg import sql  # noqa: PLC0415  # lazy: keep the driver out of import time

        create_table = sql.SQL(
            "CREATE TABLE IF NOT EXISTS {table} ("
            "source_id text PRIMARY KEY, framework text NOT NULL, location text NOT NULL, "
            "body text NOT NULL, version text NOT NULL DEFAULT 'unknown', "
            "sha256 text NOT NULL, embedding vector({dim}) NOT NULL)"
        ).format(table=sql.Identifier(self._table), dim=sql.Literal(self._dimensions))
        create_index = sql.SQL(
            "CREATE INDEX IF NOT EXISTS {index} ON {table} USING hnsw (embedding vector_cosine_ops)"
        ).format(
            index=sql.Identifier(f"{self._table}_embedding_idx"),
            table=sql.Identifier(self._table),
        )
        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
            cur.execute(create_table)
            cur.execute(
                sql.SQL(
                    "ALTER TABLE {table} ADD COLUMN IF NOT EXISTS version text NOT NULL "
                    "DEFAULT 'unknown'"
                ).format(table=sql.Identifier(self._table))
            )
            cur.execute(create_index)

    def upsert_chunks(self, chunks: Iterable[RegulationChunk]) -> int:
        """Project and upsert ``chunks`` by ``source_id``; return how many rows were written."""
        import psycopg  # noqa: PLC0415  # lazy: keep the driver out of import time
        from psycopg import sql  # noqa: PLC0415  # lazy: keep the driver out of import time

        statement = sql.SQL(
            "INSERT INTO {table} "
            "(source_id, framework, location, body, version, sha256, embedding) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s::vector) "
            "ON CONFLICT (source_id) DO UPDATE SET "
            "framework = EXCLUDED.framework, location = EXCLUDED.location, "
            "body = EXCLUDED.body, version = EXCLUDED.version, sha256 = EXCLUDED.sha256, "
            "embedding = EXCLUDED.embedding"
        ).format(table=sql.Identifier(self._table))
        written = 0
        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            for chunk in chunks:
                cur.execute(
                    statement,
                    (
                        chunk.source_id,
                        chunk.framework.value,
                        chunk.location,
                        chunk.text,
                        chunk.version,
                        chunk.sha256,
                        _vector_literal(embed_text(chunk.text, self._dimensions)),
                    ),
                )
                written += 1
        return written

    def search(
        self,
        query: str,
        *,
        framework: Framework | None,
        k: int,
    ) -> tuple[RegulationSearchResult, ...]:
        """Return lexical-vector matches nearest ``query`` by cosine distance.

        Raises:
            ValueError: If ``query`` is blank or ``k`` is not positive.
        """
        import psycopg  # noqa: PLC0415  # lazy: keep the driver out of import time
        from psycopg import sql  # noqa: PLC0415  # lazy: keep the driver out of import time

        if not query.strip():
            raise ValueError("query must not be blank")
        if k <= 0:
            raise ValueError("k must be greater than zero")
        vector = _vector_literal(embed_text(query, self._dimensions))
        framework_value = framework.value if framework is not None else None
        statement = sql.SQL(
            "SELECT source_id, framework, location, body, version, sha256, "
            "(embedding <=> %s::vector) AS distance FROM {table} "
            "WHERE (%s::text IS NULL OR framework = %s) "
            "AND (embedding <=> %s::vector) < %s "
            "ORDER BY embedding <=> %s::vector, source_id LIMIT %s"
        ).format(table=sql.Identifier(self._table))
        params = (vector, framework_value, framework_value, vector, _MAX_DISTANCE, vector, k)
        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            cur.execute(statement, params)
            rows = cur.fetchall()
        return tuple(
            RegulationSearchResult.from_chunk(
                RegulationChunk(
                    source_id=row[0],
                    framework=Framework(row[1]),
                    location=row[2],
                    text=row[3],
                    version=row[4],
                    sha256=row[5],
                ),
                score=max(0.0, 1.0 - float(row[6])),
                backend=_BACKEND,
            )
            for row in rows
        )


def ingest_pgvector(
    dsn: str,
    mapping_path: str,
    *,
    corpus_path: str | None = None,
    dimensions: int = DEFAULT_DIMENSIONS,
    table: str = DEFAULT_TABLE,
) -> int:
    """Ingest the requirements->controls mapping and seed corpus into a pgvector store.

    Reuses ``thymira.mira.kb.ingest.build_chunks`` -- the same seam ``scripts/ingest_regulation.py``
    writes JSONL from -- so the local and pgvector backends ingest identical, hash-verified chunks.

    Returns:
        The number of chunks written.
    """
    from pathlib import Path  # noqa: PLC0415  # keep the module import surface minimal

    from thymira.mira.kb.ingest import (  # noqa: PLC0415  # avoid a KB import cycle at module load
        build_chunks,
        load_requirements,
        load_seed_corpus,
    )

    chunks = build_chunks(
        load_requirements(Path(mapping_path)),
        load_seed_corpus(Path(corpus_path) if corpus_path is not None else None),
    )
    store = PgVectorRegulationStore(dsn, dimensions=dimensions, table=table)
    store.initialize()
    return store.upsert_chunks(chunks)


__all__ = [
    "DEFAULT_DIMENSIONS",
    "DEFAULT_TABLE",
    "PgVectorRegulationStore",
    "embed_text",
    "ingest_pgvector",
]
