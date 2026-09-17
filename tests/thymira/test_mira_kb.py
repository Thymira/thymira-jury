"""Unit tests for MIRA's local regulation knowledge base (KB-01, KB-02)."""

from __future__ import annotations

import importlib
import json
import re
import sys
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from pydantic import ValidationError

from thymira.events import InMemoryEventLog, sha256_text
from thymira.mira.checks import CONTROLS
from thymira.mira.kb import (
    CorpusSeedEntry,
    LocalRegulationStore,
    RegulationChunk,
    RegulationCorpus,
    RegulationSearchResult,
    RequirementsControls,
    load_default_regulation_store,
    verifies_sha256,
)
from thymira.mira.kb.ingest import build_chunks, ingest, load_requirements, load_seed_corpus
from thymira.mira.preflight import load_default_packs
from thymira.mira.tools import (
    SearchRegulation,
    SearchRegulationArguments,
    build_mira_tool_registry,
)
from thymira.policies import Gate, PolicyEngine, RiskProfile, auto_approve, load_policy_stack
from thymira.schemas import EventType, Framework, new_id
from thymira.state import LocalArtifactStore
from thymira.tools import ToolContext, ToolInvocation, ToolManager, ToolRegistry

if TYPE_CHECKING:
    from thymira.tools import Tool


def _record(
    source_id: str,
    framework: Framework,
    location: str,
    text: str,
) -> dict[str, str]:
    """Build one valid JSONL record with the digest of its exact UTF-8 text."""
    return {
        "source_id": source_id,
        "version": "test-2026",
        "framework": framework.value,
        "location": location,
        "text": text,
        "sha256": sha256_text(text),
    }


def _write_jsonl(path: Path, records: list[dict[str, str]]) -> Path:
    """Write a compact local JSONL corpus."""
    path.write_text(
        "".join(f"{json.dumps(record, sort_keys=True)}\n" for record in records),
        encoding="utf-8",
        newline="\n",
    )
    return path


@pytest.fixture
def regulation_path(tmp_path: Path) -> Path:
    """Create a small corpus with an exact-score tie and a non-match."""
    return _write_jsonl(
        tmp_path / "regulation.jsonl",
        [
            _record(
                "eu-14-first",
                Framework.EU_AI_ACT,
                "Article 14(1)",
                "Human oversight supports safe use.",
            ),
            _record(
                "eu-14-repeated",
                Framework.EU_AI_ACT,
                "Article 14(2)",
                "Human oversight requires human oversight measures.",
            ),
            _record(
                "gdpr-22",
                Framework.GDPR,
                "Article 22",
                "Human oversight applies to automated decisions.",
            ),
            _record(
                "eu-12",
                Framework.EU_AI_ACT,
                "Article 12",
                "Logging records system operation.",
            ),
        ],
    )


def _tool_context(tmp_path: Path) -> ToolContext:
    """Build the real authorization and event context for a local tool execution."""
    run_id = new_id("run")
    log = InMemoryEventLog(run_id)
    return ToolContext(
        run_id=run_id,
        agent_id=new_id("agent"),
        workspace=tmp_path / "workspace",
        event_log=log,
        gate=Gate(PolicyEngine(load_policy_stack()), log, approver=auto_approve),
        artifact_store=LocalArtifactStore(tmp_path / "artifacts", run_id),
        risk_profile=RiskProfile(risk_level="limited", activity_category="audit", confidence=1.0),
    )


def test_local_store_loads_hash_verified_chunks(regulation_path: Path) -> None:
    store = LocalRegulationStore(regulation_path)

    chunks = store.search("human", framework=None, k=10)

    assert [chunk.source_id for chunk in chunks] == [
        "eu-14-repeated",
        "eu-14-first",
        "gdpr-22",
    ]
    assert all(sha256_text(chunk.fragment) == chunk.sha256 for chunk in chunks)


def test_local_store_reports_path_and_line_for_invalid_json(tmp_path: Path) -> None:
    path = tmp_path / "regulation.jsonl"
    valid = _record("valid", Framework.EU_AI_ACT, "Article 1", "Valid text.")
    path.write_text(
        f"{json.dumps(valid)}\nnot-json\n",
        encoding="utf-8",
        newline="\n",
    )

    with pytest.raises(ValueError, match=rf"{re.escape(str(path))}:2: invalid JSON"):
        LocalRegulationStore(path)


def test_local_store_rejects_mismatched_chunk_digest(tmp_path: Path) -> None:
    path = _write_jsonl(
        tmp_path / "regulation.jsonl",
        [
            {
                **_record("eu-14", Framework.EU_AI_ACT, "Article 14", "Human oversight."),
                "sha256": "0" * 64,
            }
        ],
    )

    with pytest.raises(ValueError, match=rf"{re.escape(str(path))}:1: sha256"):
        LocalRegulationStore(path)


@pytest.mark.parametrize("query", ["", "   "])
def test_local_store_rejects_blank_queries(regulation_path: Path, query: str) -> None:
    with pytest.raises(ValueError, match="blank"):
        LocalRegulationStore(regulation_path).search(query, framework=None, k=1)


def test_local_store_rejects_non_positive_k(regulation_path: Path) -> None:
    with pytest.raises(ValueError, match="greater than zero"):
        LocalRegulationStore(regulation_path).search("human", framework=None, k=0)


def test_local_store_ranks_phrase_frequency_and_framework_filter(regulation_path: Path) -> None:
    store = LocalRegulationStore(regulation_path)

    chunks = store.search("human oversight", framework=Framework.EU_AI_ACT, k=10)

    assert [chunk.source_id for chunk in chunks] == ["eu-14-repeated", "eu-14-first"]


def test_local_store_excludes_non_matches_and_respects_k(regulation_path: Path) -> None:
    store = LocalRegulationStore(regulation_path)

    assert store.search("human oversight", framework=None, k=1)[0].source_id == "eu-14-repeated"
    assert store.search("unrelated", framework=None, k=10) == ()


def test_local_store_breaks_equal_scores_by_jsonl_order(tmp_path: Path) -> None:
    path = _write_jsonl(
        tmp_path / "ties.jsonl",
        [
            _record("first", Framework.EU_AI_ACT, "Article 1", "Human oversight."),
            _record("second", Framework.EU_AI_ACT, "Article 2", "Human oversight."),
        ],
    )

    chunks = LocalRegulationStore(path).search("human oversight", framework=None, k=2)

    assert [chunk.source_id for chunk in chunks] == ["first", "second"]


def test_search_arguments_reject_blank_query() -> None:
    with pytest.raises(ValidationError, match="blank"):
        SearchRegulationArguments(query=" ")


def test_search_tool_returns_stable_json_and_verifiable_result_digest(
    regulation_path: Path, tmp_path: Path
) -> None:
    tool = SearchRegulation(LocalRegulationStore(regulation_path))
    invocation = ToolInvocation(
        run_id=new_id("run"),
        agent_id=new_id("agent"),
        workspace=tmp_path,
        artifact_store=LocalArtifactStore(tmp_path / "artifacts", new_id("run")),
    )

    result = tool.execute(
        invocation,
        {"query": "human oversight", "framework": Framework.EU_AI_ACT, "k": 2},
    )

    assert result.success is True
    assert json.loads(result.stdout) == [
        {
            "source_id": "eu-14-repeated",
            "version": "test-2026",
            "framework": "EU_AI_ACT",
            "location": "Article 14(2)",
            "fragment": "Human oversight requires human oversight measures.",
            "sha256": sha256_text("Human oversight requires human oversight measures."),
            "score": 5.0,
            "backend": "local-keyword",
        },
        {
            "source_id": "eu-14-first",
            "version": "test-2026",
            "framework": "EU_AI_ACT",
            "location": "Article 14(1)",
            "fragment": "Human oversight supports safe use.",
            "sha256": sha256_text("Human oversight supports safe use."),
            "score": 3.0,
            "backend": "local-keyword",
        },
    ]
    assert result.result_sha256 == sha256_text(result.stdout)


def test_search_tool_serializes_no_matches_as_an_empty_list(
    regulation_path: Path, tmp_path: Path
) -> None:
    tool = SearchRegulation(LocalRegulationStore(regulation_path))
    invocation = ToolInvocation(
        run_id=new_id("run"),
        agent_id=new_id("agent"),
        workspace=tmp_path,
        artifact_store=LocalArtifactStore(tmp_path / "artifacts", new_id("run")),
    )

    result = tool.execute(invocation, {"query": "unrelated", "framework": None, "k": 1})

    assert result.success is True
    assert result.stdout == "[]"
    assert result.result_sha256 == sha256_text(result.stdout)


def test_search_tool_registers_and_executes_through_tool_manager(
    regulation_path: Path, tmp_path: Path
) -> None:
    tool = SearchRegulation(LocalRegulationStore(regulation_path))
    registry = ToolRegistry((cast("Tool", tool),))
    context = _tool_context(tmp_path)

    execution = ToolManager(registry).execute(
        context,
        "search_regulation",
        {"query": "human oversight", "framework": Framework.EU_AI_ACT, "k": 2},
    )

    assert tuple(registry) == (tool,)
    assert tool.capability.data_access == ("regulation_store",)
    assert tool.capability.external_effects == ()
    assert execution.result.success is True
    assert execution.call.result_sha256 == execution.result.result_sha256
    assert execution.result.result_sha256 == sha256_text(execution.result.stdout)
    events = context.event_log.events()
    assert [event.type for event in events].count(EventType.TOOL_STARTED) == 1
    assert [event.type for event in events].count(EventType.TOOL_COMPLETED) == 1
    completed = next(event for event in events if event.type is EventType.TOOL_COMPLETED)
    assert completed.payload["result_sha256"] == execution.call.result_sha256


def test_mira_registry_is_explicit_and_binds_the_configured_store(
    regulation_path: Path,
) -> None:
    """MIRA owns one small registry; the tool, not the agent, owns the store binding."""
    store = LocalRegulationStore(regulation_path)
    registry = build_mira_tool_registry(store)
    tool = next(iter(registry))

    assert tuple(tool.name for tool in registry) == ("search_regulation",)
    assert isinstance(tool, SearchRegulation)
    assert tool.store is store


def test_packaged_local_backend_works_without_postgresql() -> None:
    """The default MIRA store is local and carries honest backend provenance."""
    result = load_default_regulation_store().search(
        "human oversight", framework=Framework.EU_AI_ACT, k=1
    )[0]

    assert result.backend == "local-keyword"
    assert result.version == "1.1"
    assert result.source_id == "eu-ai-act-2024-art-14"
    assert result.fragment
    assert sha256_text(result.fragment) == result.sha256


def test_store_failure_is_recorded_as_controlled_tool_failure(
    tmp_path: Path,
) -> None:
    class FailingStore:
        """A store failure must close the Tool Manager lifecycle without escaping."""

        def search(
            self,
            query: str,
            *,
            framework: Framework | None,
            k: int,
        ) -> tuple[RegulationSearchResult, ...]:
            del query, framework, k
            raise RuntimeError("regulation index unavailable")

    context = _tool_context(tmp_path)
    execution = ToolManager(build_mira_tool_registry(FailingStore())).execute(
        context, "search_regulation", {"query": "Article 14", "k": 1}
    )

    assert execution.result.success is False
    assert execution.call.status.value == "FAILED"
    assert "RuntimeError" in (execution.result.error or "")
    assert any(event.type is EventType.TOOL_COMPLETED for event in context.event_log.events())


def test_tool_manager_rejects_a_context_allowlist_before_policy(
    regulation_path: Path, tmp_path: Path
) -> None:
    """The shared manager enforces an agent allowlist before the Policy Engine is consulted."""
    context = replace(_tool_context(tmp_path), allowed_tools=frozenset())
    registry = build_mira_tool_registry(LocalRegulationStore(regulation_path))

    execution = ToolManager(registry).execute(
        context, "search_regulation", {"query": "human oversight", "k": 1}
    )

    assert execution.result.success is False
    assert execution.call.status.value == "DENIED"
    assert "not allowed" in (execution.result.error or "")
    assert not any(event.type is EventType.TOOL_STARTED for event in context.event_log.events())
    assert any(event.type is EventType.TOOL_DENIED for event in context.event_log.events())


def test_importing_mira_does_not_load_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    # The entry is deleted through monkeypatch so the module table is restored afterwards: a
    # `thymira.tools` left out of `sys.modules` is re-executed by the next importer, which
    # rebinds the parent attribute to a fresh module whose already-cached submodules are missing
    # from it, and every later `monkeypatch.setattr("thymira.tools....")` in the session fails.
    monkeypatch.delitem(sys.modules, "thymira.tools", raising=False)

    importlib.reload(importlib.import_module("thymira.mira"))

    assert "thymira.tools" not in sys.modules


# --------------------------------------------------------------- KB-02: requirements -> controls

REPO_ROOT = Path(__file__).resolve().parents[2]
REQUIREMENTS_CONTROLS_PATH = REPO_ROOT / "docs" / "governance" / "requirements_controls.json"

_REQUIRED_REQUIREMENT_IDS = frozenset(
    {
        "EU-AI-ACT-ART-11",
        "EU-AI-ACT-ART-12",
        "EU-AI-ACT-ART-14",
        "GDPR-ART-5",
        "GDPR-ART-9",
        "GDPR-ART-22",
    }
)


def _registered_control_ids() -> set[str]:
    """Control ids MIRA already evaluates: deterministic A-controls plus reviewed-pack controls."""
    a_controls = {control.control_id for control in CONTROLS}
    pack_controls = {control.id for pack in load_default_packs() for control in pack.controls}
    return a_controls | pack_controls


def test_requirements_controls_wellformed(tmp_path: Path) -> None:
    mapping = load_requirements(REQUIREMENTS_CONTROLS_PATH)
    registered = _registered_control_ids()

    keys: set[tuple[str, str]] = set()
    for requirement in mapping.requirements:
        assert requirement.control_ids  # at least one control_id
        assert set(requirement.control_ids) <= registered, requirement.requirement_id
        assert requirement.source in mapping.sources
        key = (requirement.framework.value, requirement.requirement_id)
        assert key not in keys
        keys.add(key)

    present = {requirement.requirement_id for requirement in mapping.requirements}
    assert present >= _REQUIRED_REQUIREMENT_IDS
    frameworks = {requirement.framework for requirement in mapping.requirements}
    assert {Framework.EU_AI_ACT, Framework.GDPR, Framework.METHODOLOGY} <= frameworks

    out = tmp_path / "regulation.jsonl"
    report = ingest(REQUIREMENTS_CONTROLS_PATH, out)
    assert report.chunk_count == report.requirement_count + report.corpus_count

    store = LocalRegulationStore(out)
    for requirement in mapping.requirements:
        results = store.search(
            requirement.requirement, framework=requirement.framework, k=report.chunk_count
        )
        assert f"req-{requirement.requirement_id}".casefold() in {c.source_id for c in results}


def test_seed_corpus_entries_are_hash_verified_searchable_chunks() -> None:
    mapping = load_requirements(REQUIREMENTS_CONTROLS_PATH)
    corpus = load_seed_corpus()

    chunks = build_chunks(mapping, corpus)
    by_id = {chunk.source_id: chunk for chunk in chunks}

    for entry in corpus.entries:
        assert entry.source_id in by_id
        assert verifies_sha256(by_id[entry.source_id])


def test_build_chunks_rejects_duplicate_source_ids() -> None:
    mapping = load_requirements(REQUIREMENTS_CONTROLS_PATH)
    first = mapping.requirements[0]
    colliding = RegulationCorpus(
        version="1.0",
        entries=(
            CorpusSeedEntry(
                source_id=f"req-{first.requirement_id}".casefold(),
                framework=first.framework,
                location="collision",
                text="A seed entry whose id collides with a mapped requirement chunk.",
            ),
        ),
    )

    with pytest.raises(ValueError, match="duplicate chunk source_id"):
        build_chunks(mapping, colliding)


def test_requirements_controls_rejects_duplicate_keys() -> None:
    data = json.loads(REQUIREMENTS_CONTROLS_PATH.read_text(encoding="utf-8"))
    data["requirements"].append(dict(data["requirements"][0]))

    with pytest.raises(ValidationError, match="duplicate requirement key"):
        RequirementsControls.model_validate(data)


def test_regulation_chunk_from_text_digests_its_own_text() -> None:
    chunk = RegulationChunk.from_text(
        source_id="x",
        framework=Framework.GDPR,
        location="Article 5",
        text="Data minimisation.",
    )

    assert verifies_sha256(chunk)
    assert chunk.sha256 == sha256_text("Data minimisation.")
