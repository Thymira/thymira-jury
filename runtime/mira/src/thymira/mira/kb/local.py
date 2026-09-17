"""Deterministic local JSONL implementation of the regulation store."""

from __future__ import annotations

import json
import re
from collections import Counter
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import ValidationError

from thymira.mira.kb.models import RegulationChunk, RegulationSearchResult, verifies_sha256

if TYPE_CHECKING:
    from thymira.schemas import Framework

_TOKEN_PATTERN = re.compile(r"[^\W_]+", flags=re.UNICODE)
_DEFAULT_BACKEND = "local-keyword"
_CORPUS_PACKAGE = "thymira.mira.kb.corpus"
_CORPUS_RESOURCE = "regulation.jsonl"


class LocalRegulationStore:
    """Load hash-verified JSONL chunks once and search them by deterministic keywords."""

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._chunks = self._load()

    def search(
        self,
        query: str,
        *,
        framework: Framework | None,
        k: int,
    ) -> tuple[RegulationSearchResult, ...]:
        """Return matching chunks ranked by token frequency, phrase match, and input order."""
        normalized_query = query.casefold().strip()
        if not normalized_query:
            raise ValueError("query must not be blank")
        if k <= 0:
            raise ValueError("k must be greater than zero")
        query_tokens = _tokenize(normalized_query)
        if not query_tokens:
            raise ValueError("query must contain an alphanumeric token")

        query_counts = Counter(query_tokens)
        scored: list[tuple[int, int, RegulationChunk]] = []
        for index, chunk in enumerate(self._chunks):
            if framework is not None and chunk.framework is not framework:
                continue
            text = chunk.text.casefold()
            chunk_counts = Counter(_tokenize(text))
            score = sum(count * chunk_counts[token] for token, count in query_counts.items())
            if normalized_query in text:
                score += 1
            if score:
                scored.append((score, index, chunk))

        scored.sort(key=lambda item: (-item[0], item[1]))
        return tuple(
            RegulationSearchResult.from_chunk(chunk, score=float(score), backend=_DEFAULT_BACKEND)
            for score, _, chunk in scored[:k]
        )

    def chunks(self) -> tuple[RegulationChunk, ...]:
        """Return the loaded hash-verified chunks for an authoritative citation index."""
        return self._chunks

    def _load(self) -> tuple[RegulationChunk, ...]:
        """Load every JSONL line once, retaining its source order for stable ranking."""
        chunks: list[RegulationChunk] = []
        try:
            handle = self._path.open(encoding="utf-8", newline="\n")
        except OSError as exc:
            raise ValueError(f"could not read regulation JSONL {self._path}: {exc}") from exc

        with handle:
            for line_number, line in enumerate(handle, start=1):
                chunks.append(self._parse_line(line, line_number))
        return tuple(chunks)

    def _parse_line(self, line: str, line_number: int) -> RegulationChunk:
        """Parse and integrity-check one JSONL record with a precise source location."""
        try:
            data = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"invalid regulation JSONL at {self._path}:{line_number}: invalid JSON"
            ) from exc
        try:
            chunk = RegulationChunk.model_validate(data)
        except ValidationError as exc:
            raise ValueError(
                f"invalid regulation JSONL at {self._path}:{line_number}: invalid RegulationChunk"
            ) from exc
        if not verifies_sha256(chunk):
            message = f"invalid regulation JSONL at {self._path}:{line_number}: sha256 mismatch"
            raise ValueError(message)
        return chunk


def _tokenize(text: str) -> tuple[str, ...]:
    """Return case-normalized Unicode alphanumeric tokens in their original order."""
    return tuple(match.group(0) for match in _TOKEN_PATTERN.finditer(text.casefold()))


def load_default_regulation_store(path: Path | str | None = None) -> LocalRegulationStore:
    """Load the packaged local corpus, or an explicitly configured JSONL path."""
    if path is not None:
        return LocalRegulationStore(path)
    resource = resources.files(_CORPUS_PACKAGE).joinpath(_CORPUS_RESOURCE)
    with resources.as_file(resource) as resolved:
        return LocalRegulationStore(resolved)


__all__ = ["LocalRegulationStore", "load_default_regulation_store"]
