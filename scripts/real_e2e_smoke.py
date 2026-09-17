"""Drive the credit-risk demo end to end against a real model (RA-SMOKE-01).

``tests/thymira/test_demo_governance.py`` pins the Policy Engine's decision ladder, but its own
docstring says why it cannot prove the product phrase: it swaps a double for ``ThyGraph`` because
"a real ThyGraph would ask the shared Gate to authorise its ``plan.proposed`` action, which the
base policy escalates to REQUIRE_HUMAN_REVIEW"; the composed graph, MIRA's real agent fan-out and
the deterministic controls are never exercised together against a live model. No test in this
repository starts ``thymira-api``, drives the real ``thymira`` CLI as a subprocess, and lets a real
model plan, execute and get audited end to end. This script is that missing check.

It is deliberately outside ``just test`` / ``just check``: it spends real API budget, is
non-deterministic, and can take several minutes. Run it by hand after a change that touches the
composed graph, the risk interview, the Gate, or MIRA's evidence path — or nightly, per the
bug-hunt report's own recommendation (2026-09-02, section 13, action 15).

Usage::

    uv run python scripts/real_e2e_smoke.py [--workspace examples/credit-risk] [--port 8000]
        [--timeout 900] [--keep-server] [--max-approvals 4]

Requires a real provider key in ``.env`` (``ANTHROPIC_API_KEY`` or ``OPENAI_API_KEY``) and
``THYMIRA_THY_MODEL`` / ``THYMIRA_MIRA_MODEL`` set to a real model.

The API server it starts is launched with ``--replace-credential``, so a token file a previous,
uncleanly terminated run of this script left behind does not fail this run's start-up.

Exit code 0 only when the Run's canonical event chain verifies, its terminal lifecycle facts agree,
and a real Policy Engine decision is durably bound to the preceding MIRA audit report (``PASS`` /
``WARNING`` completed, or a recorded ``BLOCK`` / rejected review). Anything else — a crash,
malformed or contradictory evidence, timeout, an unhandled 500 from the CLI, or a Run stuck with
no terminal event — exits 1 with the redacted event timeline printed for diagnosis.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from thymira.events import canonical_json, read_events, redact_value, verify_log
from thymira.mira.checks import AuditReport
from thymira.schemas import (
    Actor,
    Decision,
    Event,
    EventType,
    PolicyDecision,
    RunCondition,
    RunOutcome,
    RunState,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
DEMO_PROMPT = (
    "Analyze the dataset, build two baseline models, compare them and determine whether the "
    "best model is suitable for a credit-risk use case."
)
_RUN_ID_RE = re.compile(r"ID\s+(run_\S+)")
_MAX_QUESTIONS = 12
_MAX_APPROVALS = 4
_POLL_SECONDS = 5

_INTERVIEW_ANSWERS: dict[str, str] = {
    "purpose": (
        "Build and compare baseline credit-risk models on the retrospective applications dataset "
        "(the workspace's data/applications.csv, target is_high_risk) to judge whether such a "
        "model is suitable for informing credit approval decisions; this run is a retrospective "
        "benchmark and produces no decision about a real applicant."
    ),
    "affected_population": (
        "Individuals applying for consumer credit (loan applicants) whose applications are scored "
        "by the trained model; they are the only population the model's output describes."
    ),
    "decision_effect": (
        "The model's output informs a credit approval or denial decision for the applicant."
    ),
    "autonomy": (
        "The system can analyze data and train or evaluate models; it does not autonomously "
        "approve or deny credit."
    ),
    "human_oversight": (
        "A human reviewer examines MIRA's findings and the Policy Engine's decision before any "
        "credit outcome is acted on."
    ),
    "jurisdiction": (
        "European Union: the credit data and its intended use fall under EU law (GDPR and the EU "
        "AI Act); no other jurisdiction applies."
    ),
    "data_categories": (
        "The dataset's 21 columns: account and credit history (checking_status, credit_history, "
        "savings_status, existing_credits, other_installment_plans), the loan itself "
        "(duration_months, purpose, credit_amount, installment_rate_pct, other_debtors), "
        "employment and finances (employment_since, job, property_magnitude, housing), "
        "residence and contact (residence_since, own_telephone), personal attributes "
        "(personal_status_sex, age, "
        "num_dependents, foreign_worker) and the target is_high_risk. No other data is present."
    ),
    "sensitive_attributes": (
        "Three columns carry sensitive or protected attributes: personal_status_sex (sex together "
        "with marital status), age, and foreign_worker (nationality or immigration status); the "
        "job column additionally encodes a resident/non-resident split. No race, ethnicity, "
        "disability, religion, health or biometric data is present; nothing else is protected."
    ),
    "potential_consequences": (
        "Decisions at stake: consumer-credit approval or denial and the terms offered. Harms to "
        "applicants: wrongful denial of credit, worse pricing or limits, and over-indebtedness "
        "from wrongful approval, falling disproportionately on a protected group (sex, age, "
        "nationality). Regulatory exposure: GDPR Article 22 (decisions based solely on automated "
        "processing) and Article 5 fairness, and the EU AI Act's obligations for high-risk "
        "creditworthiness systems (risk management, data governance, human oversight, logging)."
    ),
}
_FOLLOW_UP_ANSWERS: dict[str, str] = {
    "purpose": (
        "That is the complete purpose: a retrospective benchmark of baseline credit-risk models; "
        "no decision about a real applicant is produced."
    ),
    "affected_population": (
        "Loan applicants remain the complete and only affected population; no other group is "
        "in scope."
    ),
    "decision_effect": (
        "That is the complete description of the decision effect; the model's output only "
        "informs the credit approval or denial decision."
    ),
    "autonomy": (
        "That is the complete description of the system's autonomy; it never autonomously "
        "approves or denies credit."
    ),
    "human_oversight": (
        "That is the complete description of human oversight; a human reviewer always examines "
        "the findings and decision before any credit outcome is acted on."
    ),
    "jurisdiction": "The European Union is the complete and only jurisdiction in scope.",
    "data_categories": (
        "That is the complete list of data categories: the 21 columns already named are the "
        "only attributes in the dataset."
    ),
    "sensitive_attributes": (
        "That is the complete list: personal_status_sex, age and foreign_worker (plus the "
        "resident/non-resident split inside job) are the only protected attributes processed."
    ),
    "potential_consequences": (
        "That is the complete list: credit approval, denial and pricing decisions; wrongful "
        "denial, worse terms or over-indebtedness for applicants, disproportionately by sex, "
        "age or nationality; and breaches of GDPR Articles 5 and 22 or the EU AI Act's "
        "high-risk obligations. No other consequence is in scope."
    ),
}
_FALLBACK_ANSWER = "Not further specified for this smoke test; see docs/governance/quickstart.md."
_FOLLOW_UP_FALLBACK_ANSWER = (
    "The previous answer is complete and accurate; there is nothing further to add for this fact."
)
_FINAL_ANSWER = (
    "No further information exists for this fact: the two answers already recorded are complete, "
    "and the interview may proceed on them."
)
_FINAL_ANSWER_ASK_INDEX = 2

type EventLike = Event | Mapping[str, object]

_TERMINAL_COMMAND_OUTCOMES = {
    "complete": RunOutcome.COMPLETED,
    "block": RunOutcome.BLOCKED,
    "fail": RunOutcome.FAILED,
    "cancel": RunOutcome.CANCELLED,
}
_TERMINAL_OUTCOMES = {outcome.value for outcome in _TERMINAL_COMMAND_OUTCOMES.values()}


_ALLOWED_MODEL_ROUTES_ENV_VAR = "THYMIRA_ALLOWED_MODEL_ROUTES"
_MODEL_ENV_VARS = (
    "THYMIRA_THY_MODEL",
    "THYMIRA_MIRA_MODEL",
    "THYMIRA_ORCHESTRATOR_MODEL",
    "THYMIRA_MODEL_FRONTIER",
    "THYMIRA_MODEL_STANDARD",
    "THYMIRA_MODEL_FAST",
    "THYMIRA_AGENT_MODEL",
    "THYMIRA_MODEL",
)


def _require_model_route_allowlist(environment: Mapping[str, str]) -> tuple[str, ...]:
    """Return the operator's model-route allowlist or refuse to start without one.

    Found live on `main` after the session model-route policy landed: the API snapshots
    ``THYMIRA_ALLOWED_MODEL_ROUTES`` into every Session, and an unset variable is an *empty*
    fail-closed allowlist, so the real Run died on its first provider call with
    ``model route '...' is not allowed by session policy``. The smoke is the operator here; it
    must declare the routes explicitly rather than derive them, and it says which ids to list.
    """
    raw = environment.get(_ALLOWED_MODEL_ROUTES_ENV_VAR, "")
    routes = tuple(item.strip() for item in raw.split(",") if item.strip())
    if routes:
        return routes
    configured = sorted(
        {
            value.strip()
            for name in _MODEL_ENV_VARS
            if (value := environment.get(name, "")) and value.strip()
        }
    )
    hint = ", ".join(configured) if configured else "<the model ids you configure>"
    raise SmokeError(
        f"{_ALLOWED_MODEL_ROUTES_ENV_VAR} is unset or empty, so the session model-route policy "
        f"would deny every model call; set it to the exact routes this smoke may use "
        f"(configured model ids: {hint})"
    )


def _announce_model_routes() -> bool:
    """Print the allowlist this smoke runs under, or the operator's missing setting."""
    try:
        routes = _require_model_route_allowlist(os.environ)
    except SmokeError as exc:
        print(f"FAIL: {exc}")
        return False
    print(f"model routes allowed for this smoke: {', '.join(routes)}")
    return True


class SmokeError(RuntimeError):
    """Raised for a diagnosed failure so the timeline still prints before exiting."""


_API_TOKEN_ENV_VAR = "THYMIRA_API_TOKEN"  # noqa: S105 - a variable name, not a value.
_api_token = ""


def _print_safe(value: object) -> None:
    """Print a diagnostic after applying the event redaction rules."""
    print(redact_value(value))


def _select_interview_answer(field: str, times_asked: int) -> str:
    """Pick the canned answer for ``field`` from how many times it has been asked.

    The first question for a field gets the dataset-grounded canned answer from
    ``_INTERVIEW_ANSWERS``. A real model that judges that answer incomplete re-asks the field
    with a sharper follow-up question -- observed live, an open-ended answer (e.g. "sex and age
    ... may act as protected attributes") drew nine identical follow-ups in a row until
    the runtime's ``MAX_INTERVIEW_QUESTIONS`` forced a human-review escalation. The second ask
    gets a closing statement
    from ``_FOLLOW_UP_ANSWERS``; from the third ask on the smoke returns ``_FINAL_ANSWER``,
    deliberately the same text every time: the smoke has no further facts to offer and must not
    invent any, so a model that still re-asks runs into the runtime's bounded question limit and
    its governed human review, which is the behaviour the smoke then exercises.
    """
    if times_asked <= 0:
        return _INTERVIEW_ANSWERS.get(field, _FALLBACK_ANSWER)
    if times_asked < _FINAL_ANSWER_ASK_INDEX:
        return _FOLLOW_UP_ANSWERS.get(field, _FOLLOW_UP_FALLBACK_ANSWER)
    return _FINAL_ANSWER


def _run_cli(*args: str, port: int) -> subprocess.CompletedProcess[str]:
    """Drive the real CLI against the server this smoke started, not the CLI's default host.

    Found live: ``--port`` moved the server and nothing else, so every CLI call still went to
    ``DEFAULT_API_URL`` (:8000). On a free port the smoke died on its first command; on a busy
    one it would have driven whatever else was listening there.
    """
    environment = dict(os.environ)
    environment["THYMIRA_API_URL"] = f"http://127.0.0.1:{port}"
    if _api_token:
        environment[_API_TOKEN_ENV_VAR] = _api_token
    return subprocess.run(
        ["uv", "run", "thymira", *args],
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=930,
    )


def _api_token_for(workspace: Path, deadline: float) -> str:
    """Return the credential the server is authenticating with without printing its value."""
    exported = os.environ.get(_API_TOKEN_ENV_VAR, "").strip()
    if exported:
        print(f"using the API token exported in ${_API_TOKEN_ENV_VAR}")
        return exported
    path = workspace / ".thymira" / "runtime" / "api-token"
    while time.monotonic() < deadline:
        if path.is_file():
            token = path.read_text(encoding="utf-8").strip()
            if token:
                print(f"read the minted API token from {path}")
                return token
        time.sleep(0.2)
    raise SmokeError(
        f"no API credential: ${_API_TOKEN_ENV_VAR} is unset and the server wrote no {path}"
    )


def _ensure_dataset(workspace: Path) -> None:
    target = workspace / "data" / "applications.csv"
    if target.exists():
        return
    source = REPO_ROOT / "data" / "german_credit.csv"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    print(f"copied demo dataset to {target}")


def _wait_for_health(
    port: int, deadline: float, *, server: subprocess.Popen[bytes] | None = None
) -> None:
    url = f"http://127.0.0.1:{port}/healthz"
    while time.monotonic() < deadline:
        if server is not None and server.poll() is not None:
            raise SmokeError(f"API server exited before answering {url}")
        try:
            with urllib.request.urlopen(url, timeout=2) as response:  # local, fixed URL
                if response.status == 200:
                    return
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.5)
    raise SmokeError(f"API server never answered {url} in time")


def _ensure_port_free(port: int) -> None:
    """Refuse a port held by another process rather than terminating it.

    The smoke owns only the server process it starts. A previous run, a developer service or a
    different test may be listening on the requested port; killing that process could destroy
    unrelated work and would make a green result ambiguous. The server is checked again while
    waiting for health, so an early server exit cannot be mistaken for a healthy external service.
    """
    if not 1 <= port <= 65_535:
        raise SmokeError(f"port must be between 1 and 65535, got {port}")
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", port))
    except OSError as exc:
        raise SmokeError(
            f"port {port} is already in use; refusing to terminate an external process"
        ) from exc
    finally:
        probe.close()


def _start_server(workspace: Path, port: int) -> tuple[subprocess.Popen[bytes], Path]:
    """Start the API and close the parent's log handle after handing it to the child.

    Passes ``--replace-credential`` so a leftover ``api-token`` from a prior smoke run that
    crashed or was killed before its clean-shutdown cleanup ran does not make this run fail with
    ``cannot establish the API credential: ... File exists`` -- the exact restart failure that
    flag exists to recover from.
    """
    log_path = REPO_ROOT / "scratch" / "real_e2e_smoke_server.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with log_path.open("wb") as log_file:
            process = subprocess.Popen(  # fixed argv, no shell, local dev server
                [
                    "uv",
                    "run",
                    "thymira-api",
                    "--workspace",
                    str(workspace),
                    "--port",
                    str(port),
                    "--replace-credential",
                ],
                cwd=REPO_ROOT,
                stdout=log_file,
                stderr=subprocess.STDOUT,
            )
    except OSError as exc:
        raise SmokeError(f"could not start API server: {type(exc).__name__}") from exc
    return process, log_path


def _events_path(workspace: Path, run_id: str) -> Path:
    return workspace / ".thymira" / "runtime" / "runs" / run_id / "events.jsonl"


def _read_events(workspace: Path, run_id: str) -> list[Event]:
    path = _events_path(workspace, run_id)
    if not path.exists():
        return []
    verification = verify_log(path)
    if not verification.valid:
        detail = verification.error or "unknown chain error"
        raise SmokeError(f"event log verification failed: {detail}")
    try:
        return read_events(path)
    except (OSError, ValueError) as exc:
        # ``read_events`` may include a malformed payload in its exception text. Keep the smoke
        # diagnostic useful without echoing a credential that arrived in a broken log line.
        raise SmokeError(f"event log could not be parsed: {type(exc).__name__}") from exc


def _event_type(event: EventLike) -> str:
    """Return an event type from a canonical Event or a small test mapping."""
    raw_type = event.type if isinstance(event, Event) else event.get("type")
    if isinstance(raw_type, EventType):
        return raw_type.value
    return raw_type if isinstance(raw_type, str) else ""


def _event_seq(event: EventLike) -> int:
    """Return an event sequence, or ``-1`` for an unsequenced test mapping."""
    raw_seq = event.seq if isinstance(event, Event) else event.get("seq")
    return raw_seq if isinstance(raw_seq, int) and not isinstance(raw_seq, bool) else -1


def _event_run_id(event: EventLike) -> str | None:
    """Return the event's Run id where the input carries one."""
    raw_run_id = event.run_id if isinstance(event, Event) else event.get("run_id")
    return raw_run_id if isinstance(raw_run_id, str) else None


def _event_hash(event: EventLike) -> str | None:
    """Return the event hash where the input carries one."""
    raw_hash = event.hash if isinstance(event, Event) else event.get("hash")
    return raw_hash if isinstance(raw_hash, str) else None


def _payload(event: EventLike) -> dict[str, object]:
    """Return a validated event payload from a canonical Event or a test mapping."""
    payload = event.payload if isinstance(event, Event) else event.get("payload")
    if not isinstance(payload, dict):
        raise SmokeError(f"event at seq {_event_seq(event)} has a non-dict payload")
    return payload


def _pending_question(events: Sequence[EventLike]) -> dict[str, object] | None:
    """Return the latest interview question that no later answer has settled.

    The interview is sequential, so the newest interview event decides: a question with no answer
    after it is pending, an answer means nothing is. Pairing by field would be wrong -- a real
    model re-asks a field with a sharper question when it judges the first answer insufficient,
    and that second question needs a second answer (reproduced by the real-model smoke).
    """
    for event in reversed(events):
        kind = _event_type(event)
        if kind == EventType.ACTIVITY_PROFILE_ANSWERED.value:
            return None
        if kind == EventType.ACTIVITY_PROFILE_QUESTIONED.value:
            return _payload(event)
    return None


def _pending_approval(events: Sequence[EventLike]) -> str | None:
    answered_ids = set()
    for event in events:
        if _event_type(event) == EventType.HUMAN_APPROVAL.value:
            payload = _payload(event)
            decision_id = payload.get("decision_id") or payload.get("policy_decision_id")
            if decision_id:
                answered_ids.add(decision_id)
    for event in reversed(events):
        if _event_type(event) == EventType.HUMAN_APPROVAL_REQUESTED.value:
            decision_id = _payload(event)["decision_id"]
            if decision_id not in answered_ids:
                return cast("str", decision_id)
            return None
    return None


def _recorded_policy_decisions(
    events: Sequence[EventLike],
) -> tuple[list[tuple[int, PolicyDecision]], str | None]:
    """Validate every recorded policy decision and return it in event order."""
    decisions: list[tuple[int, PolicyDecision]] = []
    for event in events:
        if _event_type(event) != EventType.POLICY_DECISION.value:
            continue
        try:
            decision = PolicyDecision.model_validate(_payload(event))
        except (TypeError, ValueError):
            return [], f"policy.decision at seq {_event_seq(event)} is malformed"
        event_run_id = _event_run_id(event)
        if event_run_id is not None and decision.run_id != event_run_id:
            return [], f"policy.decision at seq {_event_seq(event)} names another Run"
        decisions.append((_event_seq(event), decision))
    return decisions, None


def _approval_outcome(
    events: Sequence[EventLike], decision: PolicyDecision
) -> tuple[bool | None, str | None]:
    """Return a valid human answer for a review decision, rejecting contradictory evidence."""
    requested = False
    human_answers: list[bool] = []
    for event in events:
        kind = _event_type(event)
        payload = _payload(event)
        if kind == EventType.HUMAN_APPROVAL_REQUESTED.value:
            if payload.get("decision_id") == decision.id:
                requested = True
            continue
        if kind != EventType.HUMAN_APPROVAL.value:
            continue
        decision_id = payload.get("decision_id") or payload.get("policy_decision_id")
        if decision_id != decision.id:
            continue
        approved = payload.get("approved")
        if not isinstance(approved, bool):
            return None, f"human.approval for {decision.id} is malformed"
        actor = event.actor if isinstance(event, Event) else event.get("actor")
        actor_kind = actor.kind.value if isinstance(actor, Actor) else None
        if isinstance(actor, Mapping):
            raw_kind = actor.get("kind")
            actor_kind = raw_kind.value if isinstance(raw_kind, EventType) else raw_kind
        if actor_kind != "human" or payload.get("automatic") is True:
            continue
        human_answers.append(approved)
    if not requested:
        return None, f"review decision {decision.id} has no approval request"
    if len(set(human_answers)) > 1:
        return None, f"review decision {decision.id} has contradictory human answers"
    return (human_answers[0] if human_answers else None), None


def _terminal_transition_state_is_coherent(event: Event, expected: RunOutcome) -> str | None:
    """Validate the state snapshot carried by a canonical terminal transition."""
    state_payload = _payload(event).get("state")
    if not isinstance(state_payload, dict):
        return f"terminal transition at seq {event.seq} has no state snapshot"
    try:
        state = RunState.model_validate_json(canonical_json(state_payload))
    except (TypeError, ValueError):
        return f"terminal transition at seq {event.seq} has a malformed state snapshot"
    if state.condition is not RunCondition.TERMINAL or state.outcome is not expected:
        return f"terminal transition at seq {event.seq} disagrees with its state snapshot"
    payload = _payload(event)
    if (
        payload.get("stage") != state.stage.value
        or payload.get("condition") != state.condition.value
    ):
        return f"terminal transition at seq {event.seq} disagrees with its state fields"
    if payload.get("outcome") != state.outcome.value:
        return f"terminal transition at seq {event.seq} disagrees with its outcome field"
    return None


@dataclass(frozen=True, slots=True)
class _TerminalFacts:
    """Terminal markers collected from one event history."""

    started: bool
    transitions: tuple[EventLike, ...]
    completed: tuple[EventLike, ...]
    failed: tuple[EventLike, ...]
    error: str | None = None


@dataclass(frozen=True, slots=True)
class _TerminalClosure:
    """One coherent terminal transition and its optional lifecycle fact."""

    transition: EventLike
    outcome: RunOutcome
    completed: EventLike | None
    failed: EventLike | None


def _collect_terminal_facts(events: Sequence[EventLike]) -> _TerminalFacts:
    """Collect lifecycle terminal markers and reject malformed transition markers."""
    started = False
    transitions: list[EventLike] = []
    completed: list[EventLike] = []
    failed: list[EventLike] = []
    for event in events:
        kind = _event_type(event)
        payload = _payload(event)
        if kind == EventType.RUN_STARTED.value:
            started = True
        elif kind == EventType.RUN_COMPLETED.value:
            completed.append(event)
        elif kind == EventType.RUN_FAILED.value:
            failed.append(event)
        elif kind == EventType.RUN_TRANSITIONED.value:
            command = payload.get("command")
            condition = payload.get("condition")
            outcome = payload.get("outcome")
            if isinstance(command, str) and command in _TERMINAL_COMMAND_OUTCOMES:
                expected = _TERMINAL_COMMAND_OUTCOMES[command]
                if condition != RunCondition.TERMINAL.value or outcome != expected.value:
                    return _TerminalFacts(
                        started,
                        tuple(transitions),
                        tuple(completed),
                        tuple(failed),
                        f"terminal command {command!r} has an incoherent outcome",
                    )
                transitions.append(event)
            elif condition == RunCondition.TERMINAL.value or outcome in _TERMINAL_OUTCOMES:
                return _TerminalFacts(
                    started,
                    tuple(transitions),
                    tuple(completed),
                    tuple(failed),
                    f"run.transitioned at seq {_event_seq(event)} has an invalid terminal marker",
                )
    return _TerminalFacts(started, tuple(transitions), tuple(completed), tuple(failed))


def _terminal_closure(  # noqa: PLR0911, PLR0912  # explicit fact checks
    facts: _TerminalFacts, events: Sequence[EventLike]
) -> tuple[_TerminalClosure | None, str | None]:
    """Validate the lifecycle facts around the sole terminal transition."""
    if facts.error is not None:
        return None, facts.error
    if not facts.transitions and not facts.completed and not facts.failed:
        return None, None
    if not facts.started:
        return None, "terminal Run has no run.started fact"
    if len(facts.transitions) != 1:
        return None, "Run has no single coherent terminal transition"

    transition = facts.transitions[0]
    terminal_seq = _event_seq(transition)
    payload = _payload(transition)
    command = cast("str", payload["command"])
    outcome = _TERMINAL_COMMAND_OUTCOMES[command]
    if isinstance(transition, Event):
        state_error = _terminal_transition_state_is_coherent(transition, outcome)
        if state_error is not None:
            return None, state_error
    if len(facts.completed) > 1 or len(facts.failed) > 1:
        return None, "Run contains duplicate terminal facts"
    completed = facts.completed[0] if facts.completed else None
    failed = facts.failed[0] if facts.failed else None
    if isinstance(transition, Event):
        if completed is not None and _event_seq(completed) <= terminal_seq:
            return None, "run.completed appears before its terminal transition"
        if failed is not None and _event_seq(failed) <= terminal_seq:
            return None, "run.failed appears before its terminal transition"
    closure_seq = max(
        (_event_seq(event) for event in (transition, completed, failed) if event), default=-1
    )
    if closure_seq >= 0 and any(
        _event_seq(event) > closure_seq for event in events if _event_seq(event) >= 0
    ):
        return None, "event log contains evidence after the terminal close"
    if outcome is RunOutcome.COMPLETED and (completed is None or failed is not None):
        return None, "completed transition is missing its run.completed fact or has a failure"
    if outcome is RunOutcome.FAILED and (failed is None or completed is not None):
        error = _payload(failed).get("error", "no error recorded") if failed else "no failure fact"
        return None, f"Run failed without a successful governance decision: {redact_value(error)}"
    if outcome in {RunOutcome.BLOCKED, RunOutcome.CANCELLED} and (
        completed is not None or failed is not None
    ):
        return None, "blocked or cancelled transition contradicts a run terminal fact"
    if completed is not None:
        reported_decision = _payload(completed).get("final_decision") or _payload(completed).get(
            "decision"
        )
        if reported_decision is not None and not isinstance(reported_decision, str):
            return None, "run.completed carries a malformed decision"
    return _TerminalClosure(transition, outcome, completed, failed), None


def _decision_for_closure(
    decisions: Sequence[tuple[int, PolicyDecision]], closure: _TerminalClosure
) -> PolicyDecision | None:
    """Select only the decision named by a terminal transition's durable binding."""
    terminal_seq = _event_seq(closure.transition)
    bound_id = _payload(closure.transition).get("policy_decision_id")
    if not isinstance(bound_id, str):
        return None
    bound = [
        decision for seq, decision in decisions if decision.id == bound_id and seq < terminal_seq
    ]
    return bound[0] if len(bound) == 1 else None


def _terminal_policy_binding_error(
    closure: _TerminalClosure, decision: PolicyDecision
) -> str | None:
    """Require the terminal transition to carry the exact recorded policy decision."""
    payload = _payload(closure.transition)
    required = ("policy_decision_id", "policy_sha256", "finding_ids")
    if any(key not in payload for key in required):
        return "terminal transition has no policy decision binding"
    if payload["policy_decision_id"] != decision.id:
        return "terminal binding names a different policy decision"
    if payload["policy_sha256"] != decision.policy_sha256:
        return "terminal binding disagrees with the policy snapshot"
    finding_ids = payload["finding_ids"]
    if not isinstance(finding_ids, list) or finding_ids != list(decision.finding_ids):
        return "terminal binding disagrees with the policy decision findings"
    return None


def _final_audit_binding_error(  # noqa: PLR0911, PLR0912  # terminal evidence checks fail closed
    events: Sequence[EventLike], closure: _TerminalClosure, decision: PolicyDecision
) -> str | None:
    """Require a governed terminal Run's findings decision to bind to its audit report."""
    if closure.outcome not in {RunOutcome.COMPLETED, RunOutcome.BLOCKED}:
        return None
    payload = _payload(closure.transition)
    required = (
        "policy_decision_id",
        "policy_sha256",
        "finding_ids",
        "audit_revision",
        "audit_completed_seq",
        "audit_terminal_hash",
    )
    if any(key not in payload for key in required):
        return "terminal transition has no final audit/governance binding"
    if payload["policy_decision_id"] != decision.id:
        return "terminal binding names a different policy decision"
    if payload["policy_sha256"] != decision.policy_sha256:
        return "terminal binding disagrees with the policy snapshot"
    finding_ids = payload["finding_ids"]
    if not isinstance(finding_ids, list) or finding_ids != list(decision.finding_ids):
        return "terminal binding disagrees with the policy decision findings"
    revision = payload["audit_revision"]
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        return "terminal binding has an invalid audit revision"
    audit_seq = payload["audit_completed_seq"]
    if isinstance(audit_seq, bool) or not isinstance(audit_seq, int) or audit_seq < 0:
        return "terminal binding has an invalid audit.completed sequence"
    terminal_hash = payload["audit_terminal_hash"]
    if not isinstance(terminal_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", terminal_hash):
        return "terminal binding has an invalid audit terminal hash"
    terminal_seq = _event_seq(closure.transition)
    if audit_seq >= terminal_seq:
        return "audit.completed must precede the terminal transition"
    terminal_position = next(
        index for index, event in enumerate(events) if event is closure.transition
    )
    audits_before_terminal = [
        event
        for event in events[:terminal_position]
        if _event_type(event) == EventType.AUDIT_COMPLETED.value
    ]
    if not audits_before_terminal:
        return "terminal binding does not name one audit.completed event"
    latest_audit_seq = _event_seq(audits_before_terminal[-1])
    if latest_audit_seq != audit_seq:
        return "terminal binding does not name the latest audit.completed before closure"
    audit_events = [
        event
        for event in events
        if _event_type(event) == EventType.AUDIT_COMPLETED.value and _event_seq(event) == audit_seq
    ]
    if len(audit_events) != 1:
        return "terminal binding does not name one audit.completed event"
    audit_event = audit_events[0]
    audit_run_id = _event_run_id(audit_event)
    if audit_run_id is not None and audit_run_id != decision.run_id:
        return "audit event belongs to another Run"
    raw_report = _payload(audit_event).get("audit_report")
    if not isinstance(raw_report, dict):
        return "audit.completed has no persisted audit report"
    try:
        report = AuditReport.model_validate_json(canonical_json(raw_report))
    except (TypeError, ValueError):
        return "audit.completed has a malformed audit report"
    if report.run_id != decision.run_id:
        return "audit report belongs to another Run"
    if report.terminal_hash != terminal_hash:
        return "terminal binding disagrees with the audit report terminal hash"
    if report.policy_sha256 != decision.policy_sha256:
        return "audit report disagrees with the policy snapshot"
    report_finding_ids = [finding.id for finding in report.findings]
    if report_finding_ids != list(decision.finding_ids):
        return "audit report findings do not match the final policy decision"
    recorded_revision = _payload(audit_event).get("audit_revision")
    if recorded_revision != revision:
        return "terminal binding disagrees with the audit revision"
    audit_position = next(index for index, event in enumerate(events) if event is audit_event)
    expected_revision = sum(
        _event_type(event) == EventType.AUDIT_COMPLETED.value
        for event in events[: audit_position + 1]
    )
    if revision != expected_revision:
        return "audit.completed revision does not match its history"
    audit_seq_from_event = _event_seq(audit_event)
    decision_seq = next(
        (
            _event_seq(event)
            for event in events
            if _event_type(event) == EventType.POLICY_DECISION.value
            and _payload(event).get("id") == decision.id
        ),
        -1,
    )
    if audit_seq_from_event >= decision_seq or decision_seq >= terminal_seq:
        return "audit, policy decision and terminal transition are not causally ordered"
    if not any(
        _event_hash(event) == terminal_hash and _event_seq(event) < audit_seq for event in events
    ):
        return "audit report terminal hash does not name a preceding event"
    return None


def _decision_matches_closure(  # noqa: PLR0911, PLR0912  # terminal rule checks stay explicit
    events: Sequence[EventLike], closure: _TerminalClosure, decision: PolicyDecision
) -> tuple[bool, str]:
    """Check that the policy decision and any human review authorize this terminal outcome."""
    if decision.subject_kind not in {"run", "findings"}:
        return False, "terminal Run is backed only by a task or tool-call policy decision"
    if decision.subject_kind == "findings" and decision.subject_id not in {"run", decision.run_id}:
        return False, "findings policy decision names another subject"
    if decision.subject_kind == "run" and decision.subject_id != decision.run_id:
        return False, "run policy decision names another subject"
    basic_binding_error = _terminal_policy_binding_error(closure, decision)
    if basic_binding_error is not None:
        return False, basic_binding_error
    if closure.completed is not None:
        reported_decision = _payload(closure.completed).get("final_decision") or _payload(
            closure.completed
        ).get("decision")
        if reported_decision is not None and reported_decision != decision.decision.value:
            return False, "run.completed disagrees with its recorded policy decision"
    if closure.outcome is RunOutcome.COMPLETED:
        if decision.subject_kind != "findings":
            return False, "completed Run has no findings policy decision"
        binding_error = _final_audit_binding_error(events, closure, decision)
        if binding_error is not None:
            return False, binding_error
        if decision.decision in {Decision.PASS, Decision.WARNING}:
            return True, f"{closure.outcome.value} with policy decision {decision.decision.value}"
        if decision.decision is Decision.REQUIRE_HUMAN_REVIEW:
            approved, error = _approval_outcome(events, decision)
            if error is not None:
                return False, error
            if approved is True:
                return True, f"{closure.outcome.value} with approved human review"
            return False, "completed review decision has no valid human approval"
        return False, f"completed Run ended with policy decision {decision.decision.value}"
    if closure.outcome is RunOutcome.BLOCKED:
        if decision.subject_kind == "findings":
            binding_error = _final_audit_binding_error(events, closure, decision)
            if binding_error is not None:
                return False, binding_error
        if decision.decision is Decision.BLOCK:
            return True, f"{closure.outcome.value} with policy decision {decision.decision.value}"
        if decision.decision is Decision.REQUIRE_HUMAN_REVIEW:
            approved, error = _approval_outcome(events, decision)
            if error is not None:
                return False, error
            if approved is False:
                return True, f"{closure.outcome.value} with rejected human review"
            return False, "blocked Run has no valid human rejection"
        return False, f"blocked Run ended with policy decision {decision.decision.value}"
    return False, f"Run ended with unsupported outcome {closure.outcome.value}"


def _terminal_outcome(  # noqa: PLR0911  # terminal verdict branches remain explicit
    events: Sequence[EventLike],
) -> tuple[bool, str] | None:
    """Return a verified governance verdict once the Run has one coherent terminal outcome."""
    decisions, decision_error = _recorded_policy_decisions(events)
    if decision_error is not None:
        return False, decision_error
    facts = _collect_terminal_facts(events)
    closure, closure_error = _terminal_closure(facts, events)
    if closure_error is not None:
        return False, closure_error
    if closure is None:
        return None
    if closure.outcome is RunOutcome.FAILED:
        failed = _payload(closure.failed) if closure.failed is not None else {}
        error = failed.get("error", "no error recorded")
        return False, f"Run failed without a successful governance decision: {redact_value(error)}"
    decision = _decision_for_closure(decisions, closure)
    if decision is None:
        if closure.outcome in {RunOutcome.COMPLETED, RunOutcome.BLOCKED}:
            return False, (
                "terminal Run has no recorded policy.decision or final audit/governance binding "
                "to audit.completed"
            )
        return False, "terminal Run has no recorded policy.decision"
    return _decision_matches_closure(events, closure, decision)


def _print_timeline(events: Sequence[EventLike]) -> None:
    print("\n--- event timeline -----------------------------------------------------")
    for event in events:
        summary = json.dumps(redact_value(_payload(event)), default=str)[:160]
        print(f"{_event_seq(event):>4}  {_event_type(event):<32} {summary}")
    print("---------------------------------------------------------------------------\n")


def _drive_run(
    workspace: Path,
    run_id: str,
    deadline: float,
    *,
    port: int,
    max_approvals: int = _MAX_APPROVALS,
) -> tuple[bool, str]:
    """Answer the risk interview and approvals as they appear; return the terminal verdict."""
    questions_answered = 0
    approvals_given = 0
    field_ask_counts: dict[str, int] = {}
    while time.monotonic() < deadline:
        events = _read_events(workspace, run_id)
        verdict = _terminal_outcome(events)
        if verdict is not None:
            return verdict

        question = _pending_question(events)
        if question is not None:
            if questions_answered >= _MAX_QUESTIONS:
                raise SmokeError("risk interview asked more questions than expected — loop?")
            field = cast("str", question["field"])
            times_asked = field_ask_counts.get(field, 0)
            answer = _select_interview_answer(field, times_asked)
            if times_asked >= _FINAL_ANSWER_ASK_INDEX:
                _print_safe(
                    f"WARNING: field {field!r} re-asked after a complete answer and its closing "
                    "follow-up; the smoke has nothing further to add"
                )
            field_ask_counts[field] = times_asked + 1
            question_text = question.get("question", "")
            _print_safe(
                f"risk-interview field {field!r} (asked {times_asked + 1}x): "
                f"{str(question_text)[:120]}"
            )
            print(f"answering risk-interview field {field!r}: {answer[:70]}...")
            result = _run_cli("answer", run_id, answer, port=port)
            questions_answered += 1
            if result.returncode != 0:
                _print_safe(result.stdout)
                _print_safe(result.stderr)
                raise SmokeError(
                    f"'thymira answer' failed for field {field!r} (exit {result.returncode})"
                )
            continue

        decision_id = _pending_approval(events)
        if decision_id is not None:
            if approvals_given >= max_approvals:
                raise SmokeError("more approval requests than expected — approval loop?")
            print(f"approval requested for {decision_id}; auditing then approving")
            _run_cli("audit", run_id, port=port)
            result = _run_cli("approve", run_id, "--note", "real_e2e_smoke.py", port=port)
            approvals_given += 1
            if result.returncode != 0:
                _print_safe(result.stdout)
                _print_safe(result.stderr)
                raise SmokeError(f"'thymira approve' failed (exit {result.returncode})")
            continue

        time.sleep(_POLL_SECONDS)

    raise SmokeError(f"timed out waiting for a terminal Run state ({run_id})")


def _stop_server(server: subprocess.Popen[bytes]) -> None:
    """Stop only the process tree this smoke started, with bounded cleanup.

    Found live: on Windows ``uv run`` does not always exec into its child, so
    ``Popen.terminate()`` can leave the real ``uvicorn`` process orphaned and still listening on
    the port -- the next run's health check then talks to that stale process, silently testing
    old code while looking exactly like a fresh, passing (or failing) run.
    """
    if server.poll() is None:
        if os.name == "nt":
            try:
                subprocess.run(
                    ["taskkill", "/T", "/F", "/PID", str(server.pid)],
                    check=False,
                    capture_output=True,
                    timeout=10,
                )
            except subprocess.TimeoutExpired:
                server.kill()
        else:
            server.terminate()
    try:
        server.wait(timeout=10)
    except subprocess.TimeoutExpired:
        server.kill()
        server.wait(timeout=10)


def _create_run(prompt: str, port: int) -> str:
    created = _run_cli("run", prompt, port=port)
    if created.returncode != 0:
        _print_safe(created.stdout)
        _print_safe(created.stderr)
        raise SmokeError(f"'thymira run' failed (exit {created.returncode})")
    match = _RUN_ID_RE.search(created.stdout)
    if match is None:
        safe_stdout = redact_value(created.stdout)
        raise SmokeError(f"could not find a run id in:\n{safe_stdout}")
    return match.group(1)


def main() -> int:
    """Run the real end-to-end smoke test and return its process exit status."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=REPO_ROOT / "examples" / "credit-risk")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--timeout", type=float, default=900.0)
    parser.add_argument("--prompt", default=DEMO_PROMPT)
    parser.add_argument("--keep-server", action="store_true")
    parser.add_argument("--max-approvals", type=int, default=_MAX_APPROVALS)
    args = parser.parse_args()

    workspace = args.workspace.resolve()
    if not _announce_model_routes():
        return 2
    _ensure_dataset(workspace)
    server: subprocess.Popen[bytes] | None = None
    log_path: Path | None = None
    run_id: str | None = None
    events: list[Event] = []
    ok = False
    verdict = "never reached a terminal state"
    try:
        _ensure_port_free(args.port)
        server, log_path = _start_server(workspace, args.port)
        _wait_for_health(args.port, time.monotonic() + 30, server=server)
        print(f"API up on :{args.port}, log at {log_path}")

        global _api_token  # noqa: PLW0603  # the credential every CLI subprocess inherits
        _api_token = _api_token_for(workspace, time.monotonic() + 10)

        run_id = _create_run(args.prompt, args.port)
        print(f"created {run_id}")

        ok, verdict = _drive_run(
            workspace,
            run_id,
            time.monotonic() + args.timeout,
            port=args.port,
            max_approvals=args.max_approvals,
        )
    except SmokeError as exc:
        verdict = str(exc)
        print(f"\nFAIL: {exc}")
    finally:
        if server is not None and not args.keep_server:
            _stop_server(server)
        if run_id is not None:
            try:
                events = _read_events(workspace, run_id)
            except SmokeError as exc:
                ok = False
                verdict = str(exc)
                print(f"\nFAIL: {exc}")
            _print_timeline(events)
            run_json = workspace / ".thymira" / "runtime" / "runs" / run_id / "run.json"
            if run_json.exists():
                print("final run.json:")
                _print_safe(run_json.read_text(encoding="utf-8"))

    if ok:
        print(f"PASS: {run_id} {verdict}.")
        return 0
    print(f"FAIL: {run_id} {verdict}. See timeline above.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
