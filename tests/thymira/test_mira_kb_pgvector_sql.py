"""Fast unit coverage for the pgvector regulation store's SQL and row mapping (KB-04).

``tests/thymira/test_mira_kb_pgvector.py`` proves the backend is a drop-in against a real
``pgvector/pgvector:pg16`` container, and skips wherever docker is unavailable. These tests take
the other half: they replace ``psycopg.connect`` with a recording double, so the statements the
store issues, the parameters it binds and the way it maps rows back to
:class:`~thymira.mira.kb.RegulationSearchResult` are checked in the fast lane, on every machine.
No database is contacted; the real ``psycopg.sql`` composition is used unchanged.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Self

import psycopg
import pytest

from thymira.events import sha256_text
from thymira.mira.kb import PgVectorRegulationStore, RegulationChunk
from thymira.mira.kb.pgvector import DEFAULT_TABLE, embed_text, ingest_pgvector
from thymira.schemas import Framework

if TYPE_CHECKING:
    from types import TracebackType

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MAPPING_PATH = _REPO_ROOT / "docs" / "governance" / "requirements_controls.json"
_DIMENSIONS = 8


class _Recorder:
    """The statements one store issued, and the rows its next fetch returns."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.rows: list[tuple[Any, ...]] = []

    @property
    def statements(self) -> list[str]:
        """Every recorded statement, in execution order."""
        return [statement for statement, _ in self.calls]


class _FakeCursor:
    """Record every statement executed and replay the recorder's rows."""

    def __init__(self, recorder: _Recorder) -> None:
        self._recorder = recorder

    def __enter__(self) -> Self:
        """Enter the cursor context."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Leave the cursor context."""

    def execute(self, statement: Any, params: Any = None) -> None:
        """Record one statement and its bound parameters."""
        text = statement if isinstance(statement, str) else statement.as_string()
        self._recorder.calls.append((text, params))

    def fetchall(self) -> list[tuple[Any, ...]]:
        """Return the rows this double was primed with."""
        return list(self._recorder.rows)


class _FakeConnection:
    """A connection double that hands out one recording cursor."""

    def __init__(self, recorder: _Recorder) -> None:
        self._recorder = recorder

    def __enter__(self) -> Self:
        """Enter the connection context."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Leave the connection context."""

    def cursor(self) -> _FakeCursor:
        """Return the recording cursor."""
        return _FakeCursor(self._recorder)


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    """Replace ``psycopg.connect`` with a double and expose what the store sent it."""
    recorded = _Recorder()

    def connect(_dsn: str, **_kwargs: object) -> _FakeConnection:
        return _FakeConnection(recorded)

    monkeypatch.setattr(psycopg, "connect", connect)
    return recorded


def _chunk(source_id: str, text: str) -> RegulationChunk:
    """Build one hash-verified regulation chunk."""
    return RegulationChunk.from_text(
        source_id=source_id,
        framework=Framework.EU_AI_ACT,
        location="Article 14(1)",
        text=text,
    )


def _store() -> PgVectorRegulationStore:
    """Build the store under test with a small, collision-tolerant dimensionality."""
    return PgVectorRegulationStore("dsn", dimensions=_DIMENSIONS)


def test_embed_text_is_deterministic_and_normalised() -> None:
    """The lexical projection is stable and unit-length, so cosine distance is meaningful."""
    vector = embed_text("Human oversight requires human oversight measures.")

    assert vector == embed_text("Human oversight requires human oversight measures.")
    assert sum(value * value for value in vector) == pytest.approx(1.0)


def test_embed_text_of_a_text_without_tokens_is_the_zero_vector() -> None:
    """A text with no alphanumeric token cannot be normalised, and stays zero."""
    assert embed_text("--- ___ ---", _DIMENSIONS) == [0.0] * _DIMENSIONS


def test_the_store_rejects_an_unsafe_table_name_and_a_non_positive_dimension() -> None:
    """The table name reaches DDL, so only a bare identifier is accepted."""
    with pytest.raises(ValueError, match="bare identifier"):
        PgVectorRegulationStore("dsn", table="regulation; DROP TABLE x")
    with pytest.raises(ValueError, match="greater than zero"):
        PgVectorRegulationStore("dsn", dimensions=0)


def test_initialize_creates_the_extension_table_and_cosine_index(recorder: _Recorder) -> None:
    """Initialisation is idempotent DDL over the configured table."""
    _store().initialize()

    statements = recorder.statements
    assert statements[0] == "CREATE EXTENSION IF NOT EXISTS vector"
    assert f'CREATE TABLE IF NOT EXISTS "{DEFAULT_TABLE}"' in statements[1]
    assert f"vector({_DIMENSIONS})" in statements[1]
    assert "ADD COLUMN IF NOT EXISTS version" in statements[2]
    assert "USING hnsw (embedding vector_cosine_ops)" in statements[3]


def test_upsert_binds_every_chunk_field_and_its_projection(recorder: _Recorder) -> None:
    """Each chunk is written by source_id, with the vector derived from its own text."""
    text = "Human oversight supports safe use."
    chunks = (_chunk("eu-14", text), _chunk("eu-12", "Logging records system operation."))

    written = _store().upsert_chunks(chunks)

    assert written == 2
    assert all(
        "ON CONFLICT (source_id) DO UPDATE SET" in statement for statement in recorder.statements
    )
    params = recorder.calls[0][1]
    assert params[0] == "eu-14"
    assert params[1] == Framework.EU_AI_ACT.value
    assert params[3] == text
    assert params[5] == sha256_text(text)
    assert params[6] == "[" + ",".join(repr(value) for value in embed_text(text, _DIMENSIONS)) + "]"


def test_search_filters_by_framework_and_maps_distance_to_a_score(recorder: _Recorder) -> None:
    """A row's cosine distance becomes the result score, and the fragment stays hash-verified."""
    text = "Human oversight supports safe use."
    recorder.rows.append(
        ("eu-14", Framework.EU_AI_ACT.value, "Article 14(1)", text, "1.0", sha256_text(text), 0.25)
    )

    results = _store().search("human oversight", framework=Framework.EU_AI_ACT, k=3)

    statement, params = recorder.calls[0]
    assert "ORDER BY embedding <=> %s::vector, source_id LIMIT %s" in statement
    assert params[1] == Framework.EU_AI_ACT.value
    assert params[-1] == 3
    assert len(results) == 1
    assert results[0].source_id == "eu-14"
    assert results[0].score == pytest.approx(0.75)
    assert results[0].sha256 == sha256_text(results[0].fragment)
    assert results[0].backend == "postgresql-pgvector-lexical-vector"


def test_search_without_a_framework_binds_a_null_filter(recorder: _Recorder) -> None:
    """An unfiltered search still binds the filter parameters, as NULL."""
    _store().search("human", framework=None, k=1)

    _statement, params = recorder.calls[0]
    assert params[1] is None
    assert params[2] is None


def test_search_clamps_a_distance_beyond_one_to_a_zero_score(recorder: _Recorder) -> None:
    """A score is never negative, however the backend reports the distance."""
    text = "Logging records system operation."
    recorder.rows.append(
        ("eu-12", Framework.EU_AI_ACT.value, "Article 12", text, "1.0", sha256_text(text), 1.5)
    )

    results = _store().search("human", framework=None, k=1)

    assert results[0].score == 0.0


def test_search_rejects_a_blank_query_and_a_non_positive_k() -> None:
    """The guards fire before any connection is opened."""
    store = PgVectorRegulationStore("dsn")

    with pytest.raises(ValueError, match="blank"):
        store.search("   ", framework=None, k=3)
    with pytest.raises(ValueError, match="greater than zero"):
        store.search("human", framework=None, k=0)


def test_ingest_pgvector_writes_the_chunks_the_shared_seam_builds(recorder: _Recorder) -> None:
    """Both backends ingest identical chunks: the shared build_chunks seam produces them."""
    written = ingest_pgvector(
        "dsn",
        str(_MAPPING_PATH),
        dimensions=_DIMENSIONS,
    )

    assert written > 0
    assert any("CREATE TABLE IF NOT EXISTS" in statement for statement in recorder.statements)
    assert (
        sum("ON CONFLICT (source_id)" in statement for statement in recorder.statements) == written
    )
