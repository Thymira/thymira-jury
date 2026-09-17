"""Agent-proposed evidence is resolved against the Run before it can become a citation.

A model that authors a finding also authors its ``evidence`` tuple. Until code resolves those
references they are claims, and an unresolved claim used to travel all the way into a verifiable
assurance bundle -- a bundle that replayed as valid while citing an artifact the Run never produced
and an event sequence that never existed. These tests pin the resolution: what the Run's log can
back, what it refuses, that the refusal is itself recorded, and that the bundle can no longer carry
a fabricated reference.
"""

from __future__ import annotations

from typing import Literal

import pytest

from thymira.events import InMemoryEventLog, sha256_text
from thymira.mira import assemble_run_assurance, verify_assurance_bundle
from thymira.mira.checks import AuditReport
from thymira.mira.grounding import RunEvidenceIndex, ground_finding, ground_findings
from thymira.schemas import (
    Actor,
    AuditFinding,
    Decision,
    EventType,
    Evidence,
    Framework,
    PolicyDecision,
    Severity,
    new_id,
)

_EvidenceKind = Literal["event", "artifact", "experiment", "tool_call", "external"]

_ARTIFACT_SHA = "a" * 64
_FOREIGN_SHA = "f" * 64
_REGULATION_FRAGMENT = "Article 14 requires effective human oversight."
_REGULATION_SHA = sha256_text(_REGULATION_FRAGMENT)
_REGULATION_SOURCE = "eu-ai-act-2024"
_REGULATION_LOCATION = "Article 14"
_REGULATION_REF = f"{_REGULATION_SOURCE}/{_REGULATION_LOCATION}"


class _Recorded:
    """A Run whose log announces one artifact, one experiment and one completed tool call."""

    def __init__(
        self,
        *,
        search_exit_code: int | None = None,
        regulation_sha256: str = _REGULATION_SHA,
    ) -> None:
        self.run_id = new_id("run")
        log = InMemoryEventLog(self.run_id)
        system = Actor.system()
        self.started = log.append(EventType.RUN_STARTED, system, {"run_environment": {}})
        self.artifact_id = new_id("artifact")
        log.append(
            EventType.ARTIFACT_CREATED,
            system,
            {
                "artifact_id": self.artifact_id,
                "name": "reports/validation.md",
                "sha256": _ARTIFACT_SHA,
            },
            subject_id=self.artifact_id,
        )
        self.experiment_id = new_id("experiment")
        log.append(
            EventType.EXPERIMENT_COMPLETED,
            system,
            {"experiment_id": self.experiment_id},
            subject_id=self.experiment_id,
        )
        self.tool_call_id = new_id("tool")
        log.append(
            EventType.TOOL_COMPLETED,
            system,
            {
                "tool_call_id": self.tool_call_id,
                "tool": "search_regulation",
                "status": "COMPLETED",
                "exit_code": search_exit_code,
                "value": {
                    "kind": "success",
                    "value": {
                        "text": _REGULATION_FRAGMENT,
                        "result_count": 1,
                        "matches": [
                            {
                                "source_id": _REGULATION_SOURCE,
                                "version": "2024/1689",
                                "framework": "EU_AI_ACT",
                                "location": _REGULATION_LOCATION,
                                "fragment": _REGULATION_FRAGMENT,
                                "sha256": regulation_sha256,
                                "score": 1.0,
                                "backend": "test",
                            }
                        ],
                    },
                },
            },
            subject_id=self.tool_call_id,
        )
        self.events = tuple(log.events())
        self.index = RunEvidenceIndex.from_events(self.events)


@pytest.fixture
def recorded() -> _Recorded:
    """Provide one Run whose log can back a reference of every kind."""
    return _Recorded()


def _finding(run_id: str, evidence: tuple[Evidence, ...]) -> AuditFinding:
    """Build one agent-authored candidate carrying the supplied references."""
    return AuditFinding(
        id=new_id("finding"),
        run_id=run_id,
        agent_id=new_id("agent"),
        control_id="EUAIACT-9",
        framework=Framework.EU_AI_ACT,
        title="Risk management system is undocumented",
        finding="No risk management documentation was produced.",
        severity=Severity.HIGH,
        confidence=0.95,
        evidence=evidence,
    )


def test_the_index_backs_a_reference_the_log_recorded(recorded: _Recorded) -> None:
    """Every reference kind the Run's own log records resolves."""
    backed = (
        Evidence(kind="event", ref="seq:0", sha256=recorded.started.hash),
        Evidence(kind="event", ref="seq:1"),
        Evidence(kind="artifact", ref="reports/validation.md", sha256=_ARTIFACT_SHA),
        Evidence(kind="artifact", ref=recorded.artifact_id),
        Evidence(kind="experiment", ref=recorded.experiment_id),
        Evidence(kind="tool_call", ref=recorded.tool_call_id),
    )

    assert all(recorded.index.backs(evidence) for evidence in backed)


@pytest.mark.parametrize(
    ("kind", "ref", "sha256", "why"),
    [
        ("event", "seq:9999", None, "an event sequence the Run never reached"),
        ("event", "seq:0", _FOREIGN_SHA, "an event hash the chain does not carry"),
        ("artifact", "reports/invented.md", None, "an artifact the Run never announced"),
        ("artifact", "reports/validation.md", _FOREIGN_SHA, "a digest the log contradicts"),
        ("experiment", "experiment_" + "0" * 32, None, "an experiment the Run never ran"),
        ("tool_call", "tool_" + "0" * 32, None, "a tool call the Run never made"),
    ],
)
def test_the_index_refuses_a_reference_the_log_cannot_back(
    recorded: _Recorded, kind: _EvidenceKind, ref: str, sha256: str | None, why: str
) -> None:
    """A reference the Run cannot back is refused, whatever it asserts about itself."""
    assert not recorded.index.backs(Evidence(kind=kind, ref=ref, sha256=sha256)), why


def test_an_exact_external_citation_is_backed_by_the_recorded_match(recorded: _Recorded) -> None:
    cited = Evidence(kind="external", ref=_REGULATION_REF, sha256=_REGULATION_SHA)

    assert recorded.index.backs(cited)


def test_an_external_citation_cannot_use_a_digest_that_does_not_hash_its_fragment() -> None:
    recorded = _Recorded(regulation_sha256=_FOREIGN_SHA)
    cited = Evidence(kind="external", ref=_REGULATION_REF, sha256=_FOREIGN_SHA)

    assert not recorded.index.backs(cited)


def test_a_nonzero_regulation_search_cannot_back_an_external_citation() -> None:
    recorded = _Recorded(search_exit_code=1)
    cited = Evidence(kind="external", ref=_REGULATION_REF, sha256=_REGULATION_SHA)

    assert not recorded.index.backs(cited)


def test_an_invented_external_citation_is_refused_after_a_search(recorded: _Recorded) -> None:
    cited = Evidence(kind="external", ref="invented-source/Article 999", sha256=_REGULATION_SHA)

    assert not recorded.index.backs(cited)


def test_an_external_citation_with_the_wrong_digest_is_refused(recorded: _Recorded) -> None:
    cited = Evidence(kind="external", ref=_REGULATION_REF, sha256=_FOREIGN_SHA)

    assert not recorded.index.backs(cited)


def test_an_external_citation_needs_a_recorded_regulation_search(recorded: _Recorded) -> None:
    cited = Evidence(kind="external", ref=_REGULATION_REF, sha256=_REGULATION_SHA)

    log = InMemoryEventLog(new_id("run"))
    log.append(EventType.RUN_STARTED, Actor.system(), {})
    never_searched = RunEvidenceIndex.from_events(log.events())

    assert not never_searched.backs(cited)


def test_an_external_citation_without_a_digest_is_refused(recorded: _Recorded) -> None:
    """A citation that pins nothing cannot be checked by anyone later."""
    assert not recorded.index.backs(
        Evidence(kind="external", ref="eu-ai-act-2024-art-14/Article 14")
    )


def test_grounding_strips_the_unbacked_references_and_keeps_the_finding(
    recorded: _Recorded,
) -> None:
    """A finding survives its fabricated citations; the citations do not."""
    real = Evidence(kind="event", ref="seq:1")
    invented = Evidence(kind="artifact", ref="reports/invented.md", sha256=_FOREIGN_SHA)

    result = ground_finding(_finding(recorded.run_id, (real, invented)), recorded.index)

    assert result.changed
    assert result.finding.evidence == (real,)
    assert result.unbacked == (invented,)
    # Suppression is not the runner's call: the Policy Engine still decides over the finding.
    assert result.finding.title == "Risk management system is undocumented"


def test_grounding_leaves_a_fully_backed_finding_untouched(recorded: _Recorded) -> None:
    """Resolution must not perturb a finding whose every reference resolves."""
    original = _finding(recorded.run_id, (Evidence(kind="event", ref="seq:1"),))

    result = ground_finding(original, recorded.index)

    assert not result.changed
    assert result.finding is original


def test_grounding_reports_every_reference_dropped_across_findings(recorded: _Recorded) -> None:
    """The caller can record exactly what it refused."""
    first = Evidence(kind="event", ref="seq:9999")
    second = Evidence(kind="artifact", ref="reports/invented.md")

    findings, dropped = ground_findings(
        (_finding(recorded.run_id, (first,)), _finding(recorded.run_id, (second,))),
        recorded.index,
    )

    assert [finding.evidence for finding in findings] == [(), ()]
    assert dropped == (first, second)


def test_a_bundle_can_no_longer_carry_a_reference_the_run_never_produced(
    recorded: _Recorded,
) -> None:
    """The end-to-end defect: a valid bundle that cited evidence which did not exist.

    Grounding is what closes it. The same fabricated finding, resolved first, reaches the bundle
    with an empty evidence index instead of two invented citations.
    """
    fabricated = _finding(
        recorded.run_id,
        (
            Evidence(kind="artifact", ref="reports/does_not_exist.md", sha256=_FOREIGN_SHA),
            Evidence(kind="event", ref="seq:9999", sha256=_FOREIGN_SHA),
        ),
    )
    grounded, dropped = ground_findings((fabricated,), recorded.index)
    assert len(dropped) == 2

    log = InMemoryEventLog(recorded.run_id)
    system = Actor.system()
    started = log.append(EventType.RUN_STARTED, system, {"run_environment": {}})
    decision = PolicyDecision(
        id=new_id("decision"),
        run_id=recorded.run_id,
        subject_kind="findings",
        subject_id="findings",
        decision=Decision.BLOCK,
        rule_id="r",
        reason="unsupported finding",
        policy_name="p@1",
        policy_sha256="d" * 64,
    )
    log.append(EventType.POLICY_DECISION, system, decision.to_json_dict())
    report = AuditReport(
        run_id=recorded.run_id,
        status="failed",
        controls=(),
        findings=grounded,
        terminal_hash=started.hash,
    )
    log.append(
        EventType.AUDIT_COMPLETED,
        system,
        {"status": report.status, "audit_report": report.model_dump(mode="json")},
    )
    events = tuple(log.events())

    bundle = assemble_run_assurance(recorded.run_id, events=events)

    assert bundle is not None
    assert verify_assurance_bundle(bundle, events=events).valid
    assert bundle.evidence_index == ()
    assert [source for trace in bundle.traceability for source in trace.sources] == []
