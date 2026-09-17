"""Immutable inputs and outputs shared by MIRA audit-agent nodes."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self

from pydantic import Field

from thymira.agents.llm.routing import ModelChoice
from thymira.mira.checks import AuditReport
from thymira.mira.finding_safety import safe_finding
from thymira.schemas import AuditFinding, Event, EventType, Framework, ThymiraModel

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from thymira.mira.agents.spec import AuditAgentSpec


class AuditInput(ThymiraModel):
    """The immutable Run evidence supplied to every MIRA audit agent."""

    run_id: str = Field(min_length=1)
    events: tuple[Event, ...] = ()
    report: AuditReport | None = None
    artifact_names: tuple[str, ...] = ()
    experiment_ids: tuple[str, ...] = ()
    frameworks: tuple[Framework, ...] = ()

    @classmethod
    def from_run(cls, events: Sequence[Event], report: AuditReport | None) -> Self:
        """Build one audit input from explicit Run events and an optional report.

        The Run id is derived from the supplied evidence. Artifact names, experiment ids, and
        frameworks are included only when a matching event records them explicitly.

        Raises:
            ValueError: If no Run id can be derived or the supplied evidence spans Runs.
        """
        event_tuple = tuple(events)
        event_run_ids = tuple(dict.fromkeys(event.run_id for event in event_tuple))
        if len(event_run_ids) > 1:
            raise ValueError(f"audit events span multiple runs: {list(event_run_ids)}")

        event_run_id = event_run_ids[0] if event_run_ids else None
        if report is not None and event_run_id is not None and report.run_id != event_run_id:
            raise ValueError(
                f"audit report run_id {report.run_id!r} does not match events run_id "
                f"{event_run_id!r}"
            )
        run_id = event_run_id or (report.run_id if report is not None else None)
        if run_id is None:
            raise ValueError("cannot derive audit run_id without events or a report")

        return cls(
            run_id=run_id,
            events=event_tuple,
            report=report,
            artifact_names=_artifact_names(event_tuple),
            experiment_ids=_experiment_ids(event_tuple),
            frameworks=_frameworks(event_tuple),
        )


class AuditAgentOutput(ThymiraModel):
    """The validated findings and model choice returned by one MIRA audit agent."""

    agent_name: str = Field(min_length=1)
    findings: tuple[AuditFinding, ...] = ()
    model_choice: ModelChoice | None = None

    @classmethod
    def from_spec(
        cls,
        spec: AuditAgentSpec,
        *,
        findings: Sequence[AuditFinding] = (),
        model_choice: ModelChoice | None = None,
    ) -> Self:
        """Build output for ``spec`` and reject findings for another framework.

        Raises:
            ValueError: If any finding's framework differs from the spec's framework.
        """
        finding_tuple = tuple(safe_finding(finding) for finding in findings)
        mismatches = tuple(
            finding for finding in finding_tuple if finding.framework is not spec.framework
        )
        if mismatches:
            frameworks = sorted({finding.framework.value for finding in mismatches})
            raise ValueError(
                f"audit agent {spec.name!r} declares framework {spec.framework.value!r} "
                f"but returned findings for {frameworks}"
            )
        return cls(agent_name=spec.name, findings=finding_tuple, model_choice=model_choice)


def _artifact_names(events: Sequence[Event]) -> tuple[str, ...]:
    """Return explicitly recorded artifact names in first-seen order."""
    names = (
        event.payload.get("name") for event in events if event.type is EventType.ARTIFACT_CREATED
    )
    return _unique_strings(name for name in names if isinstance(name, str) and name)


def _experiment_ids(events: Sequence[Event]) -> tuple[str, ...]:
    """Return explicitly recorded experiment ids in first-seen order."""
    identifiers: list[str] = []
    for event in events:
        if event.type not in {EventType.EXPERIMENT_STARTED, EventType.EXPERIMENT_COMPLETED}:
            continue
        direct = event.payload.get("experiment_id")
        if isinstance(direct, str) and direct:
            identifiers.append(direct)
        experiment = event.payload.get("experiment")
        if isinstance(experiment, dict):
            nested = experiment.get("id")
            if isinstance(nested, str) and nested:
                identifiers.append(nested)
    return _unique_strings(identifiers)


def _frameworks(events: Sequence[Event]) -> tuple[Framework, ...]:
    """Return frameworks explicitly recorded by audit-finding events."""
    frameworks: list[Framework] = []
    for event in events:
        if event.type is not EventType.AUDIT_FINDING:
            continue
        finding = event.payload.get("finding")
        if not isinstance(finding, dict) or "framework" not in finding:
            continue
        value = finding["framework"]
        if not isinstance(value, str):
            raise TypeError(f"event seq {event.seq} records a non-string finding framework")
        try:
            frameworks.append(Framework(value))
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"event seq {event.seq} records an invalid audit finding framework"
            ) from exc
    return tuple(dict.fromkeys(frameworks))


def _unique_strings(values: Iterable[str]) -> tuple[str, ...]:
    """Return unique strings while preserving their first-seen order."""
    return tuple(dict.fromkeys(values))


__all__ = ["AuditAgentOutput", "AuditInput"]
