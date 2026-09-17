"""Ingest the requirements->controls mapping and the seed corpus into a RegulationStore JSONL.

Run through ``uv run`` so the workspace is importable::

    uv run python scripts/ingest_regulation.py --out data/regulation.jsonl

The mapping lives in ``docs/governance/requirements_controls.json`` and the reviewed seed corpus
ships with ``thymira.mira.kb``; both are loaded, hashed into RegulationChunks, and written as one
JSON object per line for ``LocalRegulationStore`` (KB-01). All ingest logic lives in
``thymira.mira.kb.ingest``; this script is only its command wrapper.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import TYPE_CHECKING

from thymira.mira.kb.ingest import ingest
from thymira.mira.kb.pgvector import ingest_pgvector

if TYPE_CHECKING:
    from collections.abc import Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MAPPING = REPO_ROOT / "docs" / "governance" / "requirements_controls.json"
DEFAULT_OUT = REPO_ROOT / "data" / "regulation.jsonl"


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the ingest command."""
    parser = argparse.ArgumentParser(description="Ingest the regulation mapping + seed corpus.")
    parser.add_argument(
        "--mapping", type=Path, default=DEFAULT_MAPPING, help="requirements->controls mapping JSON"
    )
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="destination JSONL path")
    parser.add_argument(
        "--corpus", type=Path, default=None, help="seed corpus JSON (default: packaged)"
    )
    parser.add_argument(
        "--pgvector-dsn",
        type=str,
        default=None,
        help=(
            "also ingest deterministic lexical vectors into a PostgreSQL + pgvector store "
            "at this DSN (KB-04)"
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Load the mapping and corpus, write the JSONL store, and report what was written."""
    args = build_parser().parse_args(argv)
    report = ingest(mapping_path=args.mapping, out_path=args.out, corpus_path=args.corpus)
    print(
        f"ingested {report.chunk_count} chunks "
        f"({report.requirement_count} requirements + {report.corpus_count} corpus) "
        f"-> {report.out_path}"
    )
    if args.pgvector_dsn is not None:
        written = ingest_pgvector(
            args.pgvector_dsn, str(args.mapping), corpus_path=_optional_str(args.corpus)
        )
        print(f"ingested {written} chunks -> pgvector ({args.pgvector_dsn})")
    return 0


def _optional_str(path: Path | None) -> str | None:
    """Return a path as a string, or None when no path was supplied."""
    return str(path) if path is not None else None


if __name__ == "__main__":
    raise SystemExit(main())
