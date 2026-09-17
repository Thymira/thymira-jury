"""Recomputation of the termination and cleanup control A30.

The Tool Manager records how each sandboxed execution ended. That record is a property of the
producer, and a control that only asked the producer whether it had ended well would be no control
at all. This module re-derives the verdict from the persisted chain, comparing facts *different
observers* wrote against each other:

* the parent's measured ``duration_s`` against the deadline the call itself requested;
* the daemon's own ``docker inspect`` probe of the container the run actually used against the
  ``ResolvedExecutionSpec`` the runtime built before starting it;
* the recorded status and exit code against ``timed_out``, ``reap``, ``signalled_after_reap`` and
  ``sandbox_cleanup_confirmed``;
* the observed reap duration against its own cleanup bound, and the terminal outcome against the
  control channel that is able to establish it.

A producer that lies in one place is caught by the other. Nothing here imports
:mod:`thymira.tools` -- the layer check forbids it, and two readers of one constant are not
independent evidence anyway: the key list, the ``"64m" -> 67108864`` memory parser and the severity
table below are MIRA's own, exactly as A29 keeps its own credential-fragment list.

The checks are built from named conjuncts so the control can be shown not to be blind: grading the
same failing record with one conjunct removed must turn the verdict back to ``PASSED``. Each
conjunct is individually defensive and fires only on evidence that is *present and wrong*;
``shape_and_types`` is the separate fail-closed net for evidence that is missing or unreadable
(G.2), so an execution that reached a backend and recorded nothing is a finding, not a pass.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from thymira.mira.checks.models import ControlStatus
from thymira.schemas import Event, EventType, Evidence, Severity

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

    from thymira.mira.checks.controls import AuditContext

__all__ = [
    "TERMINATION_CONJUNCTS",
    "TerminationConjunct",
    "TerminationRecord",
    "check_termination_evidence",
]

_TERMINATION_KEY = "sandbox_termination"
"""The ``tool.completed`` payload key MIRA reads. Named here, never imported from the producer."""

_ENFORCED = frozenset({"partial", "full"})
"""Enforcement values that mean a backend actually ran a child, so evidence must exist."""

_SUCCESS_STATUS = "COMPLETED"
"""The persisted ``ToolCallStatus.COMPLETED`` value, kept as an independent MIRA literal."""

_STATUSES = frozenset({_SUCCESS_STATUS, "FAILED"})
_OUTCOMES = frozenset({"completed", "timed_out", "output_exceeded", "unavailable"})
_EXIT_SOURCES = frozenset({"os_wait", "container_state", "runtime_refusal"})
_CONTROL_CHANNELS = frozenset({"os_wait", "container_state", "none"})
_REAP_VALUES = frozenset({"not_required", "quiesced", "unbounded", "skipped_reaped", "unavailable"})
_TREE_SCOPES = frozenset({"none", "process_group", "process_tree", "direct_child", "container"})

_TEXT_KEYS = ("outcome", "exit_source", "control_channel", "reap", "tree_scope")
_FLAG_KEYS = (
    "control_channel_validated",
    "deadline_exceeded",
    "timed_out",
    "signalled_after_reap",
)
_NUMBER_KEYS = ("deadline_s", "duration_s", "reap_deadline_s", "reap_duration_s")
_REAP_NUMBER_KEYS = ("reap_deadline_s", "reap_duration_s")

_DEADLINE_SLACK_S = 0.25
"""Slack before a measured duration past its deadline is treated as a contradiction.

The producer rounds durations to milliseconds and measures across its own cleanup, so a run that
finished right at its deadline can round a hair over it. A quarter second is far below any real
overrun and far above that rounding.
"""

_MEMORY_UNITS = {"b": 1, "k": 1024, "m": 1024**2, "g": 1024**3}
_MEMORY_RE = re.compile(r"^([0-9]+)([bkmg]?)$")
_CPUS_RE = re.compile(r"^[0-9]+(?:\.[0-9]+)?$")
_MAX_NUMERIC_TEXT = 128
_CPUS_FRACTION_DIGITS = 9

_SEVERITY_RANK = {Severity.CRITICAL: 3, Severity.HIGH: 2, Severity.MEDIUM: 1}


def _memory_bytes(value: Any) -> int | None:
    """Parse a Docker-style memory string into bytes, with MIRA's own parser."""
    if not isinstance(value, str):
        return None
    match = _MEMORY_RE.match(value.strip().lower())
    if match is None:
        return None
    if len(match.group(1)) > _MAX_NUMERIC_TEXT:
        return None
    try:
        return int(match.group(1)) * _MEMORY_UNITS[match.group(2) or "b"]
    except (OverflowError, ValueError):
        return None


def _text(value: Any) -> str | None:
    """A readable string from an untrusted payload value, or ``None``."""
    return value if isinstance(value, str) and value else None


def _flag(value: Any) -> bool | None:
    """A readable boolean from an untrusted payload value, or ``None``."""
    return value if isinstance(value, bool) else None


def _number(value: Any) -> float | None:
    """A readable finite number from an untrusted payload value, or ``None``.

    ``bool`` is an ``int`` in Python, so a payload claiming ``duration_s: true`` would otherwise
    read as one second.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        converted = float(value)
    except OverflowError:
        return None
    return converted if math.isfinite(converted) else None


def _whole(value: Any) -> int | None:
    """A readable integer from an untrusted payload value, or ``None``."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _required_number_keys(reap: Any) -> tuple[str, ...]:
    """Return timing fields required for this reap outcome."""
    return ("deadline_s", "duration_s") + (() if reap == "not_required" else _REAP_NUMBER_KEYS)


def _number_shape_issues(payload: dict[str, Any]) -> list[str]:
    """Validate numeric timing fields, including the nullability of a skipped reap."""
    issues = [
        key
        for key in _NUMBER_KEYS
        if key in payload
        and _number(payload[key]) is None
        and not (
            key in _REAP_NUMBER_KEYS
            and payload.get("reap") == "not_required"
            and payload[key] is None
        )
    ]
    for key in _NUMBER_KEYS:
        if key not in payload:
            continue
        number = _number(payload[key])
        if number is None:
            continue
        minimum = 0.0 if key in ("duration_s", "reap_duration_s") else 0.000001
        if number < minimum:
            issues.append(key)
    if payload.get("reap") == "not_required" and any(
        payload.get(key) is not None for key in _REAP_NUMBER_KEYS
    ):
        issues.append("reap_timing_for_not_required_reap")
    return issues


@dataclass(frozen=True, slots=True)
class TerminationRecord:
    """One execution's recorded evidence, as MIRA reads it back off the chain."""

    seq: int
    tool: str
    enforcement: str
    status: str | None
    exit_code: int | None
    cleanup_confirmed: bool | None
    spec: dict[str, Any] | None
    termination: Any

    @property
    def record(self) -> dict[str, Any] | None:
        """The termination payload, when it is a mapping at all."""
        return self.termination if isinstance(self.termination, dict) else None

    def field(self, key: str) -> Any:
        """One recorded field, or ``None`` when the record cannot be read."""
        record = self.record
        return None if record is None else record.get(key)

    @property
    def probe(self) -> dict[str, Any] | None:
        """The live backend profile, when one was recorded as a mapping."""
        probe = self.field("probe")
        return probe if isinstance(probe, dict) else None

    @property
    def claims_success(self) -> bool:
        """Whether this execution was reported to the Run as having gone well.

        The point of comparison for every conjunct below: a *failure* that also reports an
        unbounded reap or a passed deadline is coherent and says so. Only a claim of success
        contradicts them.
        """
        return self.exit_code == 0 or self.status == _SUCCESS_STATUS


@dataclass(frozen=True, slots=True)
class TerminationConjunct:
    """One named property of a termination record, and the finding it raises when broken."""

    name: str
    evaluate: Callable[[TerminationRecord], tuple[str, Severity] | None]


def _shape_and_types(record: TerminationRecord) -> tuple[str, Severity] | None:
    """Fail closed on evidence that is absent or unreadable, before anything reads it."""
    if record.termination is None:
        return ("no termination evidence recorded", Severity.CRITICAL)
    payload = record.record
    if payload is None:
        return ("termination evidence is not a record", Severity.CRITICAL)
    issues: list[str] = []
    if record.status not in _STATUSES:
        issues.append("tool completion status is missing or unknown")
    if record.status == _SUCCESS_STATUS and record.exit_code is None:
        issues.append("a completed tool call is missing its event exit code")
    if record.cleanup_confirmed is None:
        issues.append("sandbox cleanup evidence is missing")
    required_numbers = _required_number_keys(payload.get("reap"))
    missing = [
        key
        for key in (*_TEXT_KEYS, *_FLAG_KEYS, *required_numbers, "child_exit_code", "probe")
        if key not in payload
    ]
    if missing:
        issues.append(f"termination evidence is missing {', '.join(sorted(missing))}")
    else:
        known_values = {
            "outcome": _OUTCOMES,
            "exit_source": _EXIT_SOURCES,
            "control_channel": _CONTROL_CHANNELS,
            "reap": _REAP_VALUES,
            "tree_scope": _TREE_SCOPES,
        }
        unreadable = [
            key
            for key in _TEXT_KEYS
            if _text(payload[key]) is None or payload[key] not in known_values[key]
        ]
        unreadable.extend(key for key in _FLAG_KEYS if _flag(payload[key]) is None)
        unreadable.extend(_number_shape_issues(payload))
        if payload["child_exit_code"] is not None and _whole(payload["child_exit_code"]) is None:
            unreadable.append("child_exit_code")
        unreadable.extend(_spec_shape(record.spec))
        unreadable.extend(_probe_shape(payload["probe"]))
        if unreadable:
            issues.append(f"termination evidence is malformed: {', '.join(sorted(unreadable))}")
    return ("; ".join(issues), Severity.CRITICAL) if issues else None


def _spec_shape(spec: dict[str, Any] | None) -> list[str]:
    """Return malformed resolved-spec fields, keeping A30 fail-closed on hostile payloads."""
    if spec is None:
        return ["sandbox_spec"]
    issues: list[str] = []
    if spec.get("backend") not in ("local_subprocess", "container"):
        issues.append("sandbox_spec.backend")
    issues.extend(
        f"sandbox_spec.{key}"
        for key in ("workspace_mount", "network")
        if _text(spec.get(key)) is None
    )
    for key in ("environment_names", "excluded_environment_names", "unenforced"):
        value = spec.get(key)
        if not isinstance(value, list) or any(_text(item) is None for item in value):
            issues.append(f"sandbox_spec.{key}")
    pids_limit = spec.get("pids_limit")
    if pids_limit is not None and (_whole(pids_limit) is None or pids_limit <= 0):
        issues.append("sandbox_spec.pids_limit")
    for key in ("image", "memory", "cpus"):
        value = spec.get(key)
        if value is not None and _text(value) is None:
            issues.append(f"sandbox_spec.{key}")
    memory = spec.get("memory")
    memory_bytes = _memory_bytes(memory)
    if memory is not None and (memory_bytes is None or memory_bytes <= 0):
        issues.append("sandbox_spec.memory")
    cpus = spec.get("cpus")
    cpus_nano = _cpus_nano(cpus)
    if cpus is not None and (cpus_nano is None or cpus_nano <= 0):
        issues.append("sandbox_spec.cpus")
    for key in ("output_limit_bytes", "workspace_quota_bytes"):
        value = spec.get(key)
        if value is not None and (_whole(value) is None or value <= 0):
            issues.append(f"sandbox_spec.{key}")
    return issues


def _probe_shape(probe: Any) -> list[str]:
    """Return malformed live-profile fields without interpreting partial evidence."""
    if probe is None:
        return []
    if not isinstance(probe, dict):
        return ["probe"]
    required = (
        "network",
        "memory_bytes",
        "cpus_nano",
        "pids_limit",
        "read_only_rootfs",
        "mounts",
        "oom_killed",
    )
    issues = [f"probe.{key}" for key in required if key not in probe]
    if _text(probe.get("network")) is None:
        issues.append("probe.network")
    memory = _whole(probe.get("memory_bytes"))
    if memory is None or memory < 0:
        issues.append("probe.memory_bytes")
    cpus_nano = _whole(probe.get("cpus_nano"))
    if cpus_nano is None or cpus_nano <= 0:
        issues.append("probe.cpus_nano")
    pids_limit = probe.get("pids_limit")
    if pids_limit is not None and (_whole(pids_limit) is None or pids_limit < 0):
        issues.append("probe.pids_limit")
    if _flag(probe.get("read_only_rootfs")) is None:
        issues.append("probe.read_only_rootfs")
    if _flag(probe.get("oom_killed")) is None:
        issues.append("probe.oom_killed")
    mounts = probe.get("mounts")
    if not isinstance(mounts, list):
        issues.append("probe.mounts")
    else:
        for index, mount in enumerate(mounts):
            if (
                not isinstance(mount, dict)
                or _text(mount.get("destination")) is None
                or _flag(mount.get("read_write")) is None
            ):
                issues.append(f"probe.mounts[{index}]")
    return issues


def _clean_success_over_a_passed_deadline(
    record: TerminationRecord,
) -> tuple[str, Severity] | None:
    """A deadline that passed can never be reported to the Run as a clean success."""
    if not record.claims_success:
        return None
    if _flag(record.field("timed_out")) or _flag(record.field("deadline_exceeded")):
        return ("a clean success is recorded over a deadline that passed", Severity.CRITICAL)
    return None


def _duration_over_the_deadline_without_a_timeout(
    record: TerminationRecord,
) -> tuple[str, Severity] | None:
    """The parent's own two numbers must agree with the verdict recorded beside them."""
    duration = _number(record.field("duration_s"))
    deadline = _number(record.field("deadline_s"))
    if duration is None or deadline is None or duration <= deadline + _DEADLINE_SLACK_S:
        return None
    if _flag(record.field("timed_out")) or _flag(record.field("deadline_exceeded")):
        return None
    return (
        f"duration {duration}s passed the {deadline}s deadline with no timeout recorded",
        Severity.CRITICAL,
    )


def _unbounded_reap(record: TerminationRecord) -> tuple[str, Severity] | None:
    """A reap that never reached quiescence is not a finished execution."""
    if record.claims_success and record.field("reap") == "unbounded":
        return ("a success is recorded over a reap that never reached quiescence", Severity.HIGH)
    return None


def _signalled_after_reap(record: TerminationRecord) -> tuple[str, Severity] | None:
    """A signal aimed at an already-reaped pid may have reached an unrelated process."""
    if _flag(record.field("signalled_after_reap")):
        return ("a signal was sent to a pid that had already been reaped", Severity.CRITICAL)
    return None


def _cleanup_not_confirmed_on_a_success(record: TerminationRecord) -> tuple[str, Severity] | None:
    """Cleanup that was not confirmed contradicts a success, though not a failure."""
    if record.claims_success and record.cleanup_confirmed is False:
        return ("a success is recorded over a cleanup that was not confirmed", Severity.HIGH)
    return None


def _probe_disagrees_with_the_specification(
    record: TerminationRecord,
) -> tuple[str, Severity] | None:
    """The daemon's own account of the container must match what the runtime recorded."""
    probe, spec = record.probe, record.spec
    if probe is None or spec is None:
        return None
    disagreements = [
        *_network_disagreement(probe, spec),
        *_ceiling_disagreements(probe, spec),
        *_filesystem_disagreements(probe, spec),
    ]
    if disagreements:
        return ("; ".join(disagreements), Severity.CRITICAL)
    return None


def _network_disagreement(probe: dict[str, Any], spec: dict[str, Any]) -> list[str]:
    observed, recorded = _text(probe.get("network")), _text(spec.get("network"))
    if observed is None or recorded is None or observed == recorded:
        return []
    return [f"probe network {observed!r} disagrees with recorded {recorded!r}"]


def _ceiling_disagreements(probe: dict[str, Any], spec: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    observed_memory = _whole(probe.get("memory_bytes"))
    recorded_memory = _memory_bytes(spec.get("memory"))
    if (
        observed_memory is not None
        and recorded_memory is not None
        and observed_memory != recorded_memory
    ):
        issues.append(f"probe memory {observed_memory} disagrees with recorded {recorded_memory}")
    observed_pids = _whole(probe.get("pids_limit"))
    recorded_pids = _whole(spec.get("pids_limit"))
    if recorded_pids is not None and observed_pids is None:
        issues.append("probe pids limit is missing while a limit was recorded")
    elif observed_pids is not None and recorded_pids is not None and observed_pids != recorded_pids:
        issues.append(f"probe pids limit {observed_pids} disagrees with recorded {recorded_pids}")
    observed_cpus = _whole(probe.get("cpus_nano"))
    recorded_cpus = _cpus_nano(spec.get("cpus"))
    if observed_cpus is not None and recorded_cpus is not None and observed_cpus != recorded_cpus:
        issues.append(f"probe cpus {observed_cpus} disagrees with recorded {recorded_cpus}")
    return issues


def _cpus_nano(value: Any) -> int | None:
    """Parse Docker's decimal CPU value into the daemon's nanocpu representation."""
    if (
        not isinstance(value, str)
        or len(value) > _MAX_NUMERIC_TEXT
        or _CPUS_RE.fullmatch(value) is None
    ):
        return None
    whole, _, fraction = value.partition(".")
    if len(fraction) > _CPUS_FRACTION_DIGITS:
        return None
    try:
        return int(whole) * 1_000_000_000 + int(fraction.ljust(_CPUS_FRACTION_DIGITS, "0") or "0")
    except ValueError:
        return None


def _filesystem_disagreements(probe: dict[str, Any], spec: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    unenforced = spec.get("unenforced")
    declared = (
        {value for value in unenforced if isinstance(value, str)}
        if isinstance(unenforced, list)
        else set()
    )
    read_only_rootfs = _flag(probe.get("read_only_rootfs"))
    if "filesystem" not in declared and read_only_rootfs is False:
        issues.append("probe reports a writable root filesystem under a confined execution")
    mount = _text(spec.get("workspace_mount"))
    mounts = probe.get("mounts")
    if mount is None or not isinstance(mounts, list) or not mount.endswith((":ro", ":rw")):
        return issues
    expected_writable = mount.endswith(":rw")
    for entry in mounts:
        if not isinstance(entry, dict) or entry.get("destination") != "/workspace":
            continue
        writable = _flag(entry.get("read_write"))
        if writable is not None and writable != expected_writable:
            issues.append(f"probe workspace mount read_write={writable} disagrees with {mount!r}")
    return issues


def _probe_missing_on_a_confirmed_container_child(
    record: TerminationRecord,
) -> tuple[str, Severity] | None:
    """Where the platform can provide a live profile, its absence is missing evidence (F6.4).

    Only the container backend can: the daemon holds an account of the container that a local
    subprocess has no equivalent of. A local execution honestly records no probe, and that is not
    penalised here.
    """
    spec = record.spec
    if spec is None or spec.get("backend") != "container":
        return None
    if not _flag(record.field("control_channel_validated")):
        return None
    if record.probe is None:
        return ("a confirmed container child recorded no live backend probe", Severity.HIGH)
    return None


def _reap_within_deadline(record: TerminationRecord) -> tuple[str, Severity] | None:
    """Require an observed reap to finish within the bound recorded beside it."""
    reap = record.field("reap")
    if reap == "not_required":
        return None
    duration = _number(record.field("reap_duration_s"))
    deadline = _number(record.field("reap_deadline_s"))
    if duration is None or deadline is None:
        return None
    if duration <= deadline + _DEADLINE_SLACK_S:
        return None
    return (
        f"observed reap duration {duration}s passed its {deadline}s cleanup bound",
        Severity.CRITICAL,
    )


def _outcome_channel_relationship(record: TerminationRecord) -> tuple[str, Severity] | None:
    """Require a completed execution to carry a validated channel for its backend."""
    outcome = record.field("outcome")
    if record.claims_success and outcome != "completed":
        return (
            "a successful execution has an incompatible terminal outcome",
            Severity.CRITICAL,
        )
    if outcome != "completed":
        return None
    spec = record.spec
    backend = None if spec is None else spec.get("backend")
    expected_channel = {
        "local_subprocess": "os_wait",
        "container": "container_state",
    }.get(backend)
    child_exit_code = _whole(record.field("child_exit_code"))
    if (
        expected_channel is None
        or record.field("exit_source") != expected_channel
        or record.field("control_channel") != expected_channel
        or record.field("control_channel_validated") is not True
        or child_exit_code is None
        or (record.claims_success and child_exit_code != 0)
    ):
        return (
            "completed execution has an incompatible or unvalidated exit channel",
            Severity.CRITICAL,
        )
    return None


TERMINATION_CONJUNCTS: tuple[TerminationConjunct, ...] = (
    TerminationConjunct("shape_and_types", _shape_and_types),
    TerminationConjunct(
        "clean_success_over_a_passed_deadline", _clean_success_over_a_passed_deadline
    ),
    TerminationConjunct(
        "duration_over_the_deadline_without_a_timeout",
        _duration_over_the_deadline_without_a_timeout,
    ),
    TerminationConjunct("unbounded_reap", _unbounded_reap),
    TerminationConjunct("signalled_after_reap", _signalled_after_reap),
    TerminationConjunct("cleanup_not_confirmed_on_a_success", _cleanup_not_confirmed_on_a_success),
    TerminationConjunct(
        "probe_disagrees_with_the_specification", _probe_disagrees_with_the_specification
    ),
    TerminationConjunct(
        "probe_missing_on_a_confirmed_container_child",
        _probe_missing_on_a_confirmed_container_child,
    ),
    TerminationConjunct("reap_within_deadline", _reap_within_deadline),
    TerminationConjunct("outcome_channel_relationship", _outcome_channel_relationship),
)


def _records(events: Sequence[Event]) -> list[TerminationRecord]:
    """Read every execution that actually reached a backend off the replayed chain."""
    records: list[TerminationRecord] = []
    for event in events:
        if event.type is not EventType.TOOL_COMPLETED:
            continue
        enforcement = _text(event.payload.get("sandbox_enforcement"))
        if enforcement is None or enforcement.lower() not in _ENFORCED:
            continue
        spec = event.payload.get("sandbox_spec")
        records.append(
            TerminationRecord(
                seq=event.seq,
                tool=_text(event.payload.get("tool")) or "tool",
                enforcement=enforcement,
                status=_text(event.payload.get("status")),
                exit_code=_whole(event.payload.get("exit_code")),
                cleanup_confirmed=_flag(event.payload.get("sandbox_cleanup_confirmed")),
                spec=spec if isinstance(spec, dict) else None,
                termination=event.payload.get(_TERMINATION_KEY),
            )
        )
    return records


def _evidence(events: Sequence[Event], seqs: Iterable[int]) -> tuple[Evidence, ...]:
    """Hash-pin the events a finding rests on, in the order they were selected."""
    by_seq = {event.seq: event for event in events}
    return tuple(
        Evidence(kind="event", ref=f"seq:{seq}", sha256=by_seq[seq].hash)
        for seq in dict.fromkeys(seqs)
        if seq in by_seq
    )


def check_termination_evidence(
    ctx: AuditContext,
    *,
    conjuncts: Sequence[TerminationConjunct] = TERMINATION_CONJUNCTS,
) -> tuple[ControlStatus, str] | tuple[ControlStatus, str, Severity, tuple[Evidence, ...]]:
    """Recompute how every sandboxed execution in this Run ended (A30).

    ``conjuncts`` is injected so a test can grade the same events with one property removed and
    prove the control is not passing by accident.
    """
    records = _records(ctx.events)
    if not records:
        return ControlStatus.NOT_APPLICABLE, "no execution recorded a sandbox enforcement value"
    problems: list[str] = []
    seqs: list[int] = []
    worst = Severity.MEDIUM
    worst_rank = 0
    for record in records:
        shape = next(
            (conjunct for conjunct in conjuncts if conjunct.name == "shape_and_types"), None
        )
        shape_outcome = None if shape is None else shape.evaluate(record)
        if shape_outcome is not None:
            outcomes = (shape_outcome,)
        else:
            outcomes = tuple(
                conjunct.evaluate(record) for conjunct in conjuncts if conjunct is not shape
            )
        for outcome in outcomes:
            if outcome is None:
                continue
            detail, severity = outcome
            problems.append(f"{record.tool} at seq {record.seq}: {detail}")
            seqs.append(record.seq)
            rank = _SEVERITY_RANK.get(severity, 1)
            if rank > worst_rank:
                worst, worst_rank = severity, rank
    if not problems:
        return ControlStatus.PASSED, "every execution recorded coherent termination evidence"
    return (
        ControlStatus.FAILED,
        "; ".join(problems),
        worst,
        _evidence(ctx.events, seqs),
    )
