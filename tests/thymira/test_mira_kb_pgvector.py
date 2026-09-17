"""PgVectorRegulationStore over a dockerized pgvector, drop-in for LocalRegulationStore (KB-04).

The pgvector backend must be a true drop-in: for a shared fixture query it returns the same top
chunk keyword search does, and every chunk it returns carries the sha256 of its ingested source.
The test runs against a real ``pgvector/pgvector:pg16`` container; it skips with an explicit reason
only when the environment genuinely cannot provide one (no docker, image, or driver).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from typing import TYPE_CHECKING

import pytest

from thymira.events import sha256_text
from thymira.mira.kb import LocalRegulationStore, PgVectorRegulationStore, RegulationChunk
from thymira.mira.kb.ingest import write_jsonl
from thymira.schemas import Framework

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

pytestmark = [pytest.mark.integration, pytest.mark.slow]

_IMAGE = "pgvector/pgvector:pg16"
_READY_TIMEOUT_S = 60.0


def _fixture_chunks() -> tuple[RegulationChunk, ...]:
    """A small shared corpus with a clear top match for 'human oversight' under EU_AI_ACT."""
    return (
        RegulationChunk.from_text(
            source_id="eu-14-first",
            framework=Framework.EU_AI_ACT,
            location="Article 14(1)",
            text="Human oversight supports safe use.",
        ),
        RegulationChunk.from_text(
            source_id="eu-14-repeated",
            framework=Framework.EU_AI_ACT,
            location="Article 14(2)",
            text="Human oversight requires human oversight measures.",
        ),
        RegulationChunk.from_text(
            source_id="gdpr-22",
            framework=Framework.GDPR,
            location="Article 22",
            text="Human oversight applies to automated decisions.",
        ),
        RegulationChunk.from_text(
            source_id="eu-12",
            framework=Framework.EU_AI_ACT,
            location="Article 12",
            text="Logging records system operation.",
        ),
    )


def _published_port(container: str) -> int:
    """Return the host port docker mapped to the container's 5432."""
    mapping = subprocess.run(
        ["docker", "port", container, "5432/tcp"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    # e.g. "127.0.0.1:49153" (possibly several lines); take the first.
    return int(mapping.splitlines()[0].rsplit(":", 1)[1])


def _wait_ready(dsn: str, timeout: float) -> None:
    """Poll until PostgreSQL accepts a connection, or fail after ``timeout`` seconds."""
    import psycopg

    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with psycopg.connect(dsn, connect_timeout=3):
                return
        except psycopg.Error as exc:  # not ready yet
            last = exc
            time.sleep(1.0)
    raise TimeoutError(f"pgvector did not become ready within {timeout}s: {last}")


@pytest.fixture(scope="module")
def pgvector_dsn() -> Iterator[str]:
    """Start a pgvector container for the module and yield its DSN, or skip if unavailable."""
    if shutil.which("docker") is None:
        pytest.skip("docker is not available")
    try:
        import psycopg  # noqa: F401
    except ImportError:
        pytest.skip("psycopg is not installed")
    if (
        subprocess.run(
            ["docker", "image", "inspect", _IMAGE], capture_output=True, text=True, check=False
        ).returncode
        != 0
    ):
        pytest.skip(f"{_IMAGE} image is not available locally")

    name = f"thymira-pgvector-{os.getpid()}-{int(time.time())}"
    started = subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--rm",
            "--name",
            name,
            "-e",
            "POSTGRES_PASSWORD=thymira",
            "-e",
            "POSTGRES_DB=regulation",
            "-p",
            "127.0.0.1::5432",
            _IMAGE,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if started.returncode != 0:
        pytest.skip(f"could not start pgvector container: {started.stderr.strip()}")
    container = started.stdout.strip()
    try:
        port = _published_port(container)
        dsn = f"host=127.0.0.1 port={port} dbname=regulation user=postgres password=thymira"
        _wait_ready(dsn, _READY_TIMEOUT_S)
        yield dsn
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, text=True, check=False)


def test_pgvector_matches_local_top_chunk_and_verifies_sha256(
    pgvector_dsn: str, tmp_path: Path
) -> None:
    chunks = _fixture_chunks()
    by_id = {chunk.source_id: chunk for chunk in chunks}

    jsonl = tmp_path / "regulation.jsonl"
    write_jsonl(chunks, jsonl)
    local = LocalRegulationStore(jsonl)

    store = PgVectorRegulationStore(pgvector_dsn)
    store.initialize()
    assert store.upsert_chunks(chunks) == len(chunks)

    query = "human oversight"

    local_hits = local.search(query, framework=Framework.EU_AI_ACT, k=3)
    pg_hits = store.search(query, framework=Framework.EU_AI_ACT, k=3)

    # Same top chunk (the KB-04 done-when), and here the whole EU_AI_ACT ranking agrees.
    assert pg_hits[0].source_id == local_hits[0].source_id == "eu-14-repeated"
    assert [c.source_id for c in pg_hits] == [c.source_id for c in local_hits]

    top = pg_hits[0]
    assert sha256_text(top.fragment) == top.sha256
    assert top.sha256 == by_id[top.source_id].sha256  # matches the ingested source

    # Unfiltered search agrees on the top chunk too, and excludes the non-matching corpus entry.
    pg_all = store.search(query, framework=None, k=10)
    assert pg_all[0].source_id == "eu-14-repeated"
    assert "eu-12" not in {c.source_id for c in pg_all}


def test_pgvector_search_is_a_drop_in_for_the_regulation_store_protocol(
    pgvector_dsn: str,
) -> None:
    store = PgVectorRegulationStore(pgvector_dsn)
    store.initialize()
    store.upsert_chunks(_fixture_chunks())

    with pytest.raises(ValueError, match="blank"):
        store.search("   ", framework=None, k=3)
    with pytest.raises(ValueError, match="greater than zero"):
        store.search("human", framework=None, k=0)
