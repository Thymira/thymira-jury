# Harness basics 3: failure as data and the real gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A tool call the Policy Engine escalates to `REQUIRE_HUMAN_REVIEW` ends the agent's step cleanly, parks the Run before MIRA, and — once a human answers — resumes into THY, where the Tool Manager runs that exact call once under the recorded approval (or denies it as a recorded rejection the agent adapts to), with every step of it on the hash-chained log and no LLM output ever becoming an authorization.

**Architecture:** Candidate (A) of the PR 3 brief. The Tool Manager gains a one-shot *ticket*: the identity of a call is `tool_intent_sha256` (tool name + validated, redacted, effect-bearing arguments), carried on `human.approval_requested`, `tool.denied` and `tool.started`, and matched by a fold over the log that reconstructs a real `Approval` and re-verifies it through `allows_execution(decision, approval)` — never a new lenient branch. A review-required denial makes the agent bridge raise PydanticAI 2.33's `ApprovalRequired`; with `DeferredToolRequests` in the agent's `output_type` the step ends cleanly (paired `agent.started`/`agent.completed`, status `PENDING`, `end_reason: awaiting_approval`). THY's Execute stops at the pending task, `_route_after_execute` ends the graph before Summarize, and `run_thy` hands the Core a typed `ThyProgress` (the gated plan, every decided outcome, the experiments and artifacts so far). The Core carries it on `RuntimeState.thy_progress`, parks the Run through `RunController.park_for_review` on the tool-level decision, and a new `route_after_thy` conditional edge ends the turn before MIRA. `RunService.resolve_approval` resumes a tool-level decision — approved or rejected — with `resume_from: "execution"`, which the dispatcher maps to `update_state(as_node="execution_gate")`, so the whole `thy` node re-runs: Inspect (idempotent), Plan (short-circuits on the seeded plan), Execute (skips decided tasks, re-runs only the one that asked), Summarize, then MIRA and the Gate as before. Denials render with a `[denied]` marker last; `agent.completed` carries an `end_reason`; the dispatcher's error keeps its cause (bug-hunt C2); the governance route refuses an anonymous approval; the CLI shows the human the tool and arguments they are answering.

**Tech Stack:** Python 3.12+, pydantic (frozen `ThymiraModel`), PydanticAI 2.33.0 (`ApprovalRequired`, `DeferredToolRequests`), LangGraph (`ThyGraph`, the Core composition), pytest, ruff, ty, uv.

**Spec:** `docs/adr/0013-harness-fundamentals-six-decisions.md` (decision 4, "two-layer permissions": denial as a result fact, the `never` class checked before any answerer; "Second pull request: failure as data + the real gate"), the PR 3 brief `pr3-brief-resume.md` (session 847d2b6b, scratchpad; candidate (A) with the plan seeded) and the PR 2 brief `pr2-brief-gate.md` (§10 risks and seams). The invariants in `AGENTS.md` ("LLM proposes, code authorizes"; `RunController` is the only persistent transition writer; THY and MIRA never import each other; no event written outside the chain) are binding.

**Baseline:** branch `feat/harness-basics-3` at `983a9d6` (`main` after PR #111). Run the fast lane once before Task 1 and record the count in the ledger (PR #111's final count on this tree: 1743 passed, 3 skipped, 31 deselected).

**Design decisions fixed by this plan (do not re-litigate in a task):**

1. **The intent is the tool plus its effect-bearing arguments, nothing else.** `tool_intent_sha256(tool_name, arguments)` hashes the validated, redacted arguments minus the `description` key. `description` is the model's narration (harness basics 1 made it mandatory on every effectful tool) and a resumed step phrases it differently while asking for exactly the same call. The agent id is excluded too: `ThySubgraph.invoke` mints a fresh `ToolContext.agent_id` per pass and `Delegator` a fresh `Task.agent_id` per delegation, so any hash over them can never match across a resume. The human approves *the call in this Run*.
2. **`description` is the justification.** The brief's `justification` keyword on `ToolManager.execute` has no caller in this PR (the step ends on the denial; there is no same-call retry for the model to justify), so it is not added — "no abstraction without a caller in the same PR". The human reads the recorded `description` inside `arguments`.
3. **An automatic answer authorizes nothing.** The Gate's synchronous test approvers record `"automatic": true`; the ticket fold ignores those answers. A real human's answer — `Gate.resolve_pending_approval` with a `human`, `LocalApprovalService.resolve`, a control-plane `Approval` — is the only thing a ticket is built from.
4. **A deferred step is `TaskStatus.PENDING`.** No new `TaskStatus` member (an enum member is a contract change); the task that asked is *pending* a human, and it is the one task the resumed pass re-runs. Every already-decided outcome (COMPLETED, FAILED, SKIPPED) is carried as it was and never retried.
5. **A rejection resumes the Run; only the findings review blocks it.** A human rejecting a tool call is not a human rejecting the Run. `resolve_approval` resumes on either answer for a `subject_kind == "tool_call"` decision; the Tool Manager then denies that exact call as `tool call rejected by a human` without asking the Gate or a human again (final for this Run), the model sees `[denied]` and adapts.
6. **An escalated call counts twice against `max_tool_calls`.** The denied attempt and the approved execution are both attempts the model made; the fold in `_constraint_error` already counts `tool.denied` like `tool.started`. Pinned by a test, not changed.
7. **No `interrupt()`, no `MiraControlPlane.handle(REQUEST_APPROVAL)`.** The park/resume mechanism is the risk-interview one (a conditional edge to `END` read from the persisted Run state, a `resume_from` marker, `update_state(as_node=…)`). `MiraControlPlane.handle` cannot park under the shipped policy (`GOV-006` makes `request_approval` itself need an approval).
8. **The composed graph's definition hash changes** (`("thy", "mira")` becomes conditional). Runs mid-flight across the deploy will have their audit snapshot flagged stale; accepted (ADR-0013 decision 6: no compatibility until 1.0), stated in the CHANGELOG.
9. **The acceptance Run stays on ThyGraph.** The end-to-end proof of the gate is the new composed test (Task 6); moving `test_acceptance_demo_run.py` onto the Core composition is a separate pull request.
10. **MIRA reads the decision a tool event names.** A3/A6 paired tool events with decisions by `subject_id` only; a human-approved retry is a new subject under an old decision, so the controls read the `decision_id` the Tool Manager stamps first and fall back to the subject (Task 5). Found while executing Task 1; without it the base policy would `BLOCK` a Run a human just approved.
11. **A synchronous human approver's yes authorizes the same call** (Task 7; bug-hunt Foco 3, C5): the Gate's in-process human answer is re-read from the log by the manager and verified through the same `allows_execution(decision, approval)`; an automatic answer still authorizes nothing. **A plain `resume` after an out-of-band answer re-enters where the Run was parked** (Task 4; bug-hunt C7): the governance route records evidence only, so `RunService.resume` derives `resume_from` from the decision the Run was parked on.

## Global Constraints

- Everything in English: code, comments, docstrings, docs, commits.
- Run tools through `uv run …` and `just …`, never the global `python`. On this Windows machine run pytest with `--basetemp=C:/Users/lucas/AppData/Local/Temp/thy-bt3` (long temp paths hit MAX_PATH and fail as `FileNotFoundError` on `.json.tmp`).
- ruff full rule set (`line-length = 100`, Google docstrings, `ANN`, `D`, `PL`); ty zero diagnostics (`.ty-baseline.json` total 0); the four import-linter contracts stay kept (`just check-imports`). A `PostToolUse` hook formats every edited Python file; do not format by hand.
- Layering: `core` → `thy | mira` → `agents` → `tools` → `policies` → `state` → `events` → `schemas`. `tools` may import `policies`, `events`, `schemas`; `agents` may import `tools`; `core` may import `thy` (it already imports `thymira.thy.models.ReworkSignal`). `thymira.agents` must not load `thymira.tools` at import time beyond what it already does.
- No new `EventType` member and no new `TaskStatus` member (both are closed contract vocabularies). New facts are payload keys: `tool_intent_sha256`, `tool`, `arguments`, `decision_id` (on `tool.started`), `end_reason`.
- "LLM proposes, code authorizes": the only path that executes a review-gated tool is `allows_execution(decision, approval)` over a decision the Gate recorded and an `Approval` reconstructed from a human's `human.approval` event. No model output, no `description`, no automatic answer ever becomes an authorization. `RunController` stays the only writer of Run transitions (`park_for_review`, `advance(RESUME)`).
- `ToolInvocation` (what a tool is handed) gains nothing; a tool never sees the log, the Gate, a ticket or a decision.
- Tests: unit tests in `tests/thymira/test_<member>.py`, no marker; the new composed end-to-end test runs in the fast lane (in-process, scripted, seconds). LLM calls through `ScriptedProvider`; files only under `tmp_path`.
- The acceptance sidecars under `tests/thymira/acceptance/` must not change (no tool schema, description or system prompt moves in this plan). If a task changes them, the task is wrong.
- Conventional Commits; each commit ends with:
  ```
  Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01BkX3MhMkFHcwQB8AGZ1h4k
  ```
- One `CHANGELOG.md` `[Unreleased]` entry for the whole pull request (Task 8 consolidates whatever earlier tasks added). Never stage `.env`, `runs/`, `.superpowers/`.
- No `CONTRACT_VERSION` bump: the shared refusal helpers added to `packages/schemas` during
  implementation (ledger ruling R12) change no persisted model or closed enum.

---

## File structure

| File | Responsibility in this plan |
|---|---|
| `runtime/policies/src/thymira/policies/gate.py` | `check_capability(..., details=)`; `_record` puts `details` on `human.approval_requested` |
| `runtime/policies/src/thymira/policies/approval.py` | `PendingApproval.tool_call`; `decision_from_events` made public |
| `runtime/policies/src/thymira/policies/__init__.py` | export `decision_from_events` |
| `runtime/tools/src/thymira/tools/models.py` | `ToolExecution.pending_approval` |
| `runtime/tools/src/thymira/tools/manager.py` | `tool_intent_sha256`, the ticket fold, the review-denial recorder, the ticket check before the Gate |
| `runtime/tools/src/thymira/tools/__init__.py` | export `tool_intent_sha256` |
| `runtime/agents/src/thymira/agents/tool_bridge.py` | `[denied]` marker; `ApprovalRequired` on a pending review |
| `runtime/agents/src/thymira/agents/runner.py` | `DeferredToolRequests` in `output_type`; `AgentEndReason`; the PENDING branch |
| `runtime/agents/src/thymira/agents/delegation.py` | a PENDING delegation's `Task` and `agent.message` text |
| `runtime/agents/src/thymira/agents/__init__.py` | export `AgentEndReason` |
| `runtime/thy/src/thymira/thy/models.py` | `ThyProgress`; `ThyState.awaiting_approval()`; `ThyOutput.progress` |
| `runtime/thy/src/thymira/thy/nodes/plan.py` | short-circuit on a seeded plan |
| `runtime/thy/src/thymira/thy/nodes/execute.py` | carried outcomes; skip decided tasks; stop at a PENDING one |
| `runtime/thy/src/thymira/thy/graph.py` | `_route_after_execute` ends on a pending task; `run_thy` hands back `progress` |
| `runtime/thy/src/thymira/thy/__init__.py` | export `ThyProgress` |
| `runtime/core/src/thymira/core/graph/state.py` | `RuntimeState.thy_progress` |
| `runtime/core/src/thymira/core/graph/adapters.py` | `ThySubgraph.invoke` seeds from and folds back `thy_progress` |
| `runtime/core/src/thymira/core/graph/compose.py` | `route_after_thy`; the park on the tool-level decision; the edge tables |
| `runtime/core/src/thymira/core/dispatch.py` | `resume_from: "execution"` → `execution_gate`; the error keeps its cause (C2) |
| `runtime/core/src/thymira/core/runs.py` | `resolve_approval` resumes a tool-level decision on either answer |
| `runtime/mira/src/thymira/mira/checks/controls.py` | A3/A6 pair a tool event with the decision it names, then with its subject |
| `apps/api/src/thymira/api/routes/governance.py` | an anonymous approval is refused (401) |
| `adapters/cli/src/thymira/cli/client.py`, `adapters/cli/src/thymira/cli/render.py` | the pending decision shows the tool and its arguments |
| `tests/thymira/test_tools.py`, `test_policies.py`, `test_policies_approval.py`, `test_agents_tool_bridge.py`, `test_agents_runner.py`, `test_agents_delegation.py`, `test_thy_plan.py`, `test_thy_execute.py`, `test_thy_graph.py`, `test_core_state.py` (or wherever `RuntimeState` round-trips are pinned), `test_core_graph_adapters.py`, `test_core_composition.py`, `test_core_dispatch.py`, `test_core_runs.py` (or the module that pins `resolve_approval`), `test_e2e_runtime_spine.py`, `test_mira_checks.py`, `test_api_governance.py`, `test_api_contract.py`, `test_cli_approval.py`, new `test_e2e_tool_approval.py` | tests |
| `CHANGELOG.md`, `docs/adr/0013-harness-fundamentals-six-decisions.md`, `AGENTS.md` | the record |

---

### Task 1: The one-shot ticket in the Tool Manager

**Files:**
- Modify: `runtime/policies/src/thymira/policies/gate.py` (`check_capability` lines 283-296; `_record` lines 387-442)
- Modify: `runtime/policies/src/thymira/policies/approval.py` (`PendingApproval` lines 39-55; `pending_approvals` lines 58-104; `_decision_from_events` lines 241-252)
- Modify: `runtime/policies/src/thymira/policies/__init__.py` (export `decision_from_events`)
- Modify: `runtime/tools/src/thymira/tools/models.py` (`ToolExecution`, the last dataclass in the file)
- Modify: `runtime/tools/src/thymira/tools/manager.py` (imports lines 5-26; `execute` lines 127-165; new helpers)
- Modify: `runtime/tools/src/thymira/tools/__init__.py` (export `tool_intent_sha256`)
- Test: `tests/thymira/test_tools.py`, `tests/thymira/test_policies.py`, `tests/thymira/test_policies_approval.py`

**Interfaces:**
- Produces: `thymira.tools.tool_intent_sha256(tool_name: str, arguments: Mapping[str, Any]) -> str`; `ToolExecution.pending_approval: PolicyDecision | None`; `Gate.check_capability(..., details: Mapping[str, Any] | None = None)`; `thymira.policies.decision_from_events(events, decision_id) -> PolicyDecision`; `PendingApproval.tool_call: dict[str, Any] | None`.
- Payload keys: `human.approval_requested` for a tool call gains `tool`, `arguments` (validated, redacted), `tool_intent_sha256`; `tool.denied` from the Gate branch gains the same three; `tool.started` gains `tool_intent_sha256` and `decision_id`.
- Consumed by: Task 2 (the bridge reads `pending_approval`), Task 4 (Core rebuilds the pending tool decision with `decision_from_events`), Task 5 (MIRA reads `decision_id` off `tool.started`/`tool.denied`), Task 7 (the synchronous human answer through the same fold), Task 8 (the CLI reads `tool`/`arguments`).

- [ ] **Step 1: Write the failing tests** (`tests/thymira/test_tools.py`; reuse `_FakeTool`, `_EchoArguments`, `_context`; widen `_context`'s `approver` parameter to `Approver | None`)

```python
from thymira.schemas import Actor, ActorKind, Approval, EventType, ToolCallStatus, new_id
from thymira.tools import tool_intent_sha256


def _human_answer(context: ToolContext, decision_id: str, *, approved: bool) -> None:
    """Record what `Gate.resolve_pending_approval` writes when a real human answers."""
    context.event_log.append(
        EventType.HUMAN_APPROVAL,
        Actor(kind=ActorKind.HUMAN, id="reviewer", authenticated=True),
        {"decision_id": decision_id, "approved": approved, "automatic": False},
        subject_id=decision_id,
    )


def test_a_review_required_call_is_denied_pending_and_leaves_its_identity_on_the_log(
    tmp_path: Path,
) -> None:
    capability = ToolCapability(id="echo", external_effects=())
    tool = _FakeTool("echo", capability, arguments_model=_EchoArguments)
    context = _context(tmp_path, capability=capability, approver=None, risk_confidence=0.1)

    execution = ToolManager(ToolRegistry((tool,))).execute(
        context, "echo", {"value": "x", "description": "first phrasing"}
    )

    assert execution.call.status is ToolCallStatus.DENIED
    assert execution.pending_approval is not None
    assert execution.pending_approval.decision is Decision.REQUIRE_HUMAN_REVIEW
    assert execution.call.completed_at is not None
    ticket = tool_intent_sha256("echo", {"value": "x"})
    requested = next(
        e for e in context.event_log.events() if e.type is EventType.HUMAN_APPROVAL_REQUESTED
    )
    assert requested.payload["tool"] == "echo"
    assert requested.payload["arguments"] == {"value": "x", "description": "first phrasing"}
    assert requested.payload["tool_intent_sha256"] == ticket
    denied = context.event_log.events()[-1]
    assert denied.type is EventType.TOOL_DENIED
    assert denied.payload["tool_intent_sha256"] == ticket
    assert denied.payload["decision_id"] == execution.pending_approval.id
    assert tool.seen_invocation == []


def test_a_human_approval_of_the_exact_call_authorizes_it_once(tmp_path: Path) -> None:
    capability = ToolCapability(id="echo", external_effects=())
    tool = _FakeTool("echo", capability, arguments_model=_EchoArguments)
    context = _context(tmp_path, capability=capability, approver=None, risk_confidence=0.1)
    manager = ToolManager(ToolRegistry((tool,)))
    first = manager.execute(context, "echo", {"value": "x", "description": "first phrasing"})
    assert first.pending_approval is not None
    _human_answer(context, first.pending_approval.id, approved=True)

    # A different narration and a different agent: the same call.
    retry_context = replace(context, agent_id=new_id("agent"))
    second = manager.execute(
        retry_context, "echo", {"value": "x", "description": "second phrasing"}
    )
    third = manager.execute(retry_context, "echo", {"value": "x", "description": "again"})

    assert second.call.status is ToolCallStatus.COMPLETED
    assert second.call.policy_decision_id == first.pending_approval.id
    assert len(tool.seen_invocation) == 1
    started = next(e for e in context.event_log.events() if e.type is EventType.TOOL_STARTED)
    assert started.payload["tool_intent_sha256"] == tool_intent_sha256("echo", {"value": "x"})
    assert started.payload["decision_id"] == first.pending_approval.id
    # Exactly one decision for the approved execution: the ticket never re-asks the Gate.
    decisions = [e for e in context.event_log.events() if e.type is EventType.POLICY_DECISION]
    assert [d.payload["id"] for d in decisions][:1] == [first.pending_approval.id]
    # The answer is spent: the third identical call needs a new decision and a new human.
    assert third.call.status is ToolCallStatus.DENIED
    assert third.pending_approval is not None
    assert third.pending_approval.id != first.pending_approval.id
    assert len(decisions) == 2


def test_an_automatic_answer_never_authorizes_a_retry(tmp_path: Path) -> None:
    """`auto_approve` records `automatic: true`; a ticket is built from a human's answer only."""
    capability = ToolCapability(id="echo", external_effects=())
    tool = _FakeTool("echo", capability, arguments_model=_EchoArguments)
    context = _context(tmp_path, capability=capability, approver=auto_approve, risk_confidence=0.1)
    manager = ToolManager(ToolRegistry((tool,)))

    first = manager.execute(context, "echo", {"value": "x"})
    second = manager.execute(context, "echo", {"value": "x"})

    assert first.call.status is ToolCallStatus.DENIED
    assert second.call.status is ToolCallStatus.DENIED
    # The automatic answer already exists, so neither denial is waiting for anyone.
    assert first.pending_approval is None
    assert second.pending_approval is None
    assert tool.seen_invocation == []


def test_a_human_rejection_is_final_for_the_call_and_asks_no_one_again(tmp_path: Path) -> None:
    capability = ToolCapability(id="echo", external_effects=())
    tool = _FakeTool("echo", capability, arguments_model=_EchoArguments)
    context = _context(tmp_path, capability=capability, approver=None, risk_confidence=0.1)
    manager = ToolManager(ToolRegistry((tool,)))
    first = manager.execute(context, "echo", {"value": "x"})
    assert first.pending_approval is not None
    _human_answer(context, first.pending_approval.id, approved=False)
    decisions_before = sum(
        1 for e in context.event_log.events() if e.type is EventType.POLICY_DECISION
    )

    second = manager.execute(context, "echo", {"value": "x"})

    assert second.call.status is ToolCallStatus.DENIED
    assert second.pending_approval is None
    assert second.result.error == "tool call rejected by a human"
    assert second.call.policy_decision_id == first.pending_approval.id
    assert (
        sum(1 for e in context.event_log.events() if e.type is EventType.POLICY_DECISION)
        == decisions_before
    )
    assert context.event_log.events()[-1].type is EventType.TOOL_DENIED
    assert tool.seen_invocation == []


def test_an_approval_under_another_policy_is_not_a_ticket(tmp_path: Path) -> None:
    capability = ToolCapability(id="echo", external_effects=())
    tool = _FakeTool("echo", capability, arguments_model=_EchoArguments)
    context = _context(tmp_path, capability=capability, approver=None, risk_confidence=0.1)
    manager = ToolManager(ToolRegistry((tool,)))
    first = manager.execute(context, "echo", {"value": "x"})
    assert first.pending_approval is not None
    _human_answer(context, first.pending_approval.id, approved=True)
    other_policy = load_policy_stack("credit_risk")
    other_gate = Gate(PolicyEngine(other_policy), context.event_log)

    second = manager.execute(replace(context, gate=other_gate), "echo", {"value": "x"})

    assert second.call.status is ToolCallStatus.DENIED
    assert second.call.policy_decision_id != first.pending_approval.id
    assert tool.seen_invocation == []


@pytest.mark.parametrize(
    ("left", "right", "same"),
    [
        ({"value": "x", "description": "a"}, {"value": "x", "description": "b"}, True),
        ({"value": "x"}, {"value": "y"}, False),
    ],
)
def test_the_intent_digest_ignores_the_description_and_nothing_else(
    left: dict[str, str], right: dict[str, str], same: bool
) -> None:
    assert (tool_intent_sha256("echo", left) == tool_intent_sha256("echo", right)) is same
    assert tool_intent_sha256("echo", left) != tool_intent_sha256("other", left)


def test_an_escalated_call_counts_twice_against_the_tool_call_limit(tmp_path: Path) -> None:
    """The denied attempt and the approved execution are both attempts (decision 6)."""
    capability = ToolCapability(id="echo", external_effects=())
    tool = _FakeTool("echo", capability, arguments_model=_EchoArguments)
    base = _context(tmp_path, capability=capability, approver=None, risk_confidence=0.1)
    constraints = ExecutionConstraints(max_tool_calls=2)
    decision = base.gate.authorize_execution(base.risk_profile, (capability,))
    context = replace(
        base,
        execution_constraints=constraints,
        execution_decision=decision.model_copy(update={"execution_constraints": constraints}),
    )
    manager = ToolManager(ToolRegistry((tool,)))
    first = manager.execute(context, "echo", {"value": "x"})
    assert first.pending_approval is not None
    _human_answer(context, first.pending_approval.id, approved=True)

    second = manager.execute(context, "echo", {"value": "x"})
    third = manager.execute(context, "echo", {"value": "y"})

    assert second.call.status is ToolCallStatus.COMPLETED
    assert third.call.status is ToolCallStatus.DENIED
    assert "maximum number" in (third.result.error or "")
```

If `authorize_execution` under the default stack with a low-confidence risk does not yield a decision whose constraints can be copied this way, build the `execution_decision` the way `test_manager_enforces_the_authorised_tool_call_limit` (`test_tools.py:349`) already does — copy that test's setup verbatim.

`tests/thymira/test_policies.py` — one test:

```python
def test_check_capability_details_land_on_the_request_and_never_on_the_decision() -> None:
    log = InMemoryEventLog(new_id("run"))
    gate = Gate(PolicyEngine(load_policy_stack()), log)
    risk = RiskProfile(risk_level="limited", activity_category="analysis", confidence=0.1)

    decision = gate.check_capability(
        subject_id="tool_1",
        capability=ToolCapability(id="echo", external_effects=()),
        risk=risk,
        summary="echo",
        details={"tool": "echo", "arguments": {"value": "x"}, "tool_intent_sha256": "a" * 64},
    )

    assert decision.decision is Decision.REQUIRE_HUMAN_REVIEW
    requested = next(e for e in log.events() if e.type is EventType.HUMAN_APPROVAL_REQUESTED)
    assert requested.payload["tool"] == "echo"
    assert requested.payload["arguments"] == {"value": "x"}
    assert requested.payload["tool_intent_sha256"] == "a" * 64
    assert requested.payload["decision_id"] == decision.id
    assert "tool" not in decision.to_json_dict()
```

`tests/thymira/test_policies_approval.py` — two tests:

```python
def test_pending_fold_carries_the_tool_call_a_human_is_asked_about() -> None:
    log = InMemoryEventLog(new_id("run"))
    gate = Gate(PolicyEngine(load_policy_stack()), log, mode=GateMode.DEFERRED)
    risk = RiskProfile(risk_level="limited", activity_category="analysis", confidence=0.1)
    gate.check_capability(
        subject_id="tool_1",
        capability=ToolCapability(id="echo", external_effects=()),
        risk=risk,
        summary="echo",
        details={"tool": "echo", "arguments": {"value": "x"}, "tool_intent_sha256": "a" * 64},
    )
    gate.check_action(subject_kind="run", subject_id="run", action_type="plan.proposed")

    tool_call, run_level = pending_approvals(log.events())

    assert tool_call.tool_call == {
        "tool": "echo",
        "arguments": {"value": "x"},
        "tool_intent_sha256": "a" * 64,
    }
    assert run_level.tool_call is None


def test_decision_from_events_rebuilds_the_recorded_decision() -> None:
    log = InMemoryEventLog(new_id("run"))
    gate = Gate(PolicyEngine(load_policy_stack()), log, mode=GateMode.DEFERRED)
    decision = gate.check_action(subject_kind="run", subject_id="run", action_type="plan.proposed")

    assert decision_from_events(log.events(), decision.id) == decision
    with pytest.raises(UnknownApprovalError):
        decision_from_events(log.events(), new_id("decision"))
```

Adjust the fixtures to whatever `test_policies_approval.py` already provides (`engine`, `human` fixtures exist; `_defer` builds a deferred Gate — reuse them rather than duplicating).

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/thymira/test_tools.py tests/thymira/test_policies.py tests/thymira/test_policies_approval.py -q --basetemp=C:/Users/lucas/AppData/Local/Temp/thy-bt3 -k "ticket or intent or pending_and_leaves or automatic_answer or rejection_is_final or another_policy or counts_twice or details_land or tool_call_a_human or decision_from_events"`
Expected: FAIL (`ImportError: tool_intent_sha256`, `TypeError: unexpected keyword 'details'`, `AttributeError: pending_approval`).

- [ ] **Step 3: The Gate carries `details`** (`gate.py`)

```python
    def check_capability(
        self,
        *,
        subject_id: str,
        capability: ToolCapability,
        risk: RiskProfile,
        summary: str = "",
        cost_so_far: dict[str, Any] | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> PolicyDecision:
        """Decide whether a tool capability may be used, record it, and handle approval.

        ``details`` are the keys the Tool Manager puts on the ``human.approval_requested``
        payload -- the tool, its validated redacted arguments and the call's
        ``tool_intent_sha256`` -- so the human answering sees the exact call and the answer is
        bound to it. They never reach the decision itself.
        """
        decision = self.engine.decide_capability(
            run_id=self.log.run_id, subject_id=subject_id, capability=capability, risk=risk
        )
        return self._record(decision, summary or capability.id, cost_so_far, details=details)
```

`_record(self, decision, summary, cost_so_far, *, details: Mapping[str, Any] | None = None)`; the `human.approval_requested` payload becomes

```python
            {
                **dict(details or {}),
                "decision_id": decision.id,
                "rule_id": decision.rule_id,
                "reason": decision.reason,
                "summary": summary,
                **({"cost_so_far": cost_so_far} if cost_so_far is not None else {}),
            },
```

(`details` first, so the Gate's own keys always win). Add to the `_record` docstring: "``details`` extend the request payload with caller facts; the Gate's own keys take precedence."

- [ ] **Step 4: The approval fold knows a tool call** (`approval.py`)

```python
_TOOL_CALL_KEYS: tuple[str, ...] = ("tool", "arguments", "tool_intent_sha256")


class PendingApproval(ThymiraModel):
    ...
    requested_at: datetime
    tool_call: dict[str, Any] | None = None
    """The tool call the human is asked to approve -- ``tool``, its redacted ``arguments`` and
    the ``tool_intent_sha256`` the answer is bound to -- or ``None`` for a run-level review."""
```

In `pending_approvals`, pass `tool_call=_tool_call(event.payload)`:

```python
def _tool_call(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    """The tool-call facts a Tool Manager request carries, or ``None`` for a run-level one."""
    if not isinstance(payload.get("tool"), str):
        return None
    return {key: payload[key] for key in _TOOL_CALL_KEYS if key in payload}
```

Rename `_decision_from_events` to `decision_from_events` (update the one caller in `LocalApprovalService.resolve`), give it a full docstring ("Rebuild the recorded :class:`PolicyDecision` for ``decision_id`` from its own ``policy.decision`` event. Raises ``UnknownApprovalError`` when no such event exists or its payload no longer validates."), add it to `__all__` and to `thymira/policies/__init__.py` (import and `__all__`, alphabetical).

- [ ] **Step 5: `ToolExecution.pending_approval`** (`tools/models.py`)

```python
@dataclass(frozen=True, slots=True)
class ToolExecution:
    """What the manager hands back: the recorded call, its result, and a review still pending."""

    call: ToolCall
    result: ToolResult
    pending_approval: PolicyDecision | None = None
    """Set only when the call was denied because a human must first approve it and no answer
    exists yet: the ``REQUIRE_HUMAN_REVIEW`` decision the Gate recorded. The agent bridge ends
    the step on it; every other denial leaves it ``None`` and is an ordinary result."""
```

(`PolicyDecision` is already imported under `TYPE_CHECKING`; `from __future__ import annotations` is at the top, so the annotation stays a string.)

- [ ] **Step 6: The ticket in the manager** (`manager.py`)

Imports: `from collections.abc import Mapping` (under `TYPE_CHECKING` if only annotations use it — it is used at run time in `tool_intent_sha256`'s body only through `.items()`, so a `TYPE_CHECKING` import is fine); `from dataclasses import dataclass, replace`; `from thymira.events import canonical_json, redact_value, sha256_text`; `from thymira.policies import allows_execution, decision_from_events`; from `thymira.schemas` add `Approval`, `PolicyDecision`, `approval_decision_id`, `approval_names_decision` (real imports: `Approval` is constructed, the two accessors are called; `PolicyDecision` is only annotated — put it under `TYPE_CHECKING`).

Module-level helpers (below `_allowlist_error`):

```python
_DESCRIPTION_KEY = "description"


def tool_intent_sha256(tool_name: str, arguments: Mapping[str, Any]) -> str:
    """Digest the effect of one tool call: the tool and its validated, redacted arguments.

    This is the identity a human's answer is bound to, and what a resumed step must reproduce
    to be handed that answer. ``description`` is left out: it is the model's narration of the
    call, not its effect, and a resumed step phrases it differently while asking for exactly
    the same call. Only redacted values go in, so the digest can be recomputed from the recorded
    evidence alone.
    """
    effect = {key: value for key, value in arguments.items() if key != _DESCRIPTION_KEY}
    return sha256_text(canonical_json({"tool": tool_name, "arguments": effect}))


@dataclass(frozen=True, slots=True)
class _Answer:
    """The latest human answer the log holds for one exact call."""

    decision: PolicyDecision
    approval: Approval | None
    """A real, unspent approval reconstructed from the answer event; ``None`` for a rejection."""
    note: str | None = None


def _human_answer(context: ToolContext, ticket: str) -> _Answer | None:
    """Fold the log for the latest human answer to this exact call.

    ``None`` means no human has answered a request for ``ticket`` yet, or the answer is spent, so
    the call takes the ordinary Gate path and mints a fresh decision. Otherwise the latest answer
    wins. A rejection is final for this Run: the call is denied without asking the Gate or a
    human again. An approval is spent by the first ``tool.started`` carrying the same ticket --
    one answer, one execution -- so a second identical call needs a second answer. An in-process
    automatic answer (``"automatic": true``, the Gate's test approvers) is evidence of nothing
    here, and an answer given under a policy other than the one now loaded is not a ticket at
    all: the decision it answered no longer describes what the engine would decide.
    """
    events = context.event_log.events()
    requested = {
        decision_id
        for event in events
        if event.type is EventType.HUMAN_APPROVAL_REQUESTED
        and event.payload.get("tool_intent_sha256") == ticket
        and (decision_id := approval_decision_id(event.payload)) is not None
    }
    answers = [
        event
        for event in events
        if event.type is EventType.HUMAN_APPROVAL
        and approval_decision_id(event.payload) in requested
        and isinstance(event.payload.get("approved"), bool)
        and event.payload.get("automatic") is not True
    ]
    if not answers:
        return None
    latest = answers[-1]
    decision_id = approval_decision_id(latest.payload)
    assert decision_id is not None  # noqa: S101  # filtered above; documents the invariant
    decision = decision_from_events(events, decision_id)
    raw_note = latest.payload.get("note", latest.payload.get("rationale"))
    note = raw_note if isinstance(raw_note, str) and raw_note else None
    if latest.payload["approved"] is False:
        return _Answer(decision=decision, approval=None, note=note)
    approved = sum(1 for event in answers if event.payload["approved"] is True)
    spent = sum(
        1
        for event in events
        if event.type is EventType.TOOL_STARTED
        and event.payload.get("tool_intent_sha256") == ticket
    )
    if spent >= approved or decision.policy_sha256 != context.gate.engine.policy_sha256:
        return None
    recorded_id = latest.payload.get("id")
    approval = Approval(
        id=recorded_id if isinstance(recorded_id, str) else new_id("approval"),
        run_id=context.run_id,
        policy_decision_id=decision.id,
        # For a tool call the authorization context *is* the intent: the exact call.
        authorization_context_sha256=ticket,
        approved=True,
        approved_by=latest.actor,
        rationale=note,
    )
    return _Answer(decision=decision, approval=approval, note=note)


def _unanswered(context: ToolContext, decision_id: str) -> bool:
    """Whether no ``human.approval`` at all -- automatic or human -- names ``decision_id`` yet."""
    return not any(
        event.type is EventType.HUMAN_APPROVAL
        and approval_names_decision(event.payload, decision_id)
        for event in context.event_log.events()
    )


def _redacted(arguments: dict[str, Any]) -> dict[str, Any]:
    """Redact a tool's arguments for the log, refusing a redaction that is no longer a dict."""
    redacted = redact_value(arguments)
    if not isinstance(redacted, dict):
        msg = "redacted tool arguments must remain a dictionary"
        raise TypeError(msg)
    return redacted
```

Use `_redacted` at the top of `execute` too (replace lines 90-93). `PolicyEngine.policy_sha256` is the engine attribute the decisions already carry (`engine.py` builds every decision with `policy_sha256=self.policy_sha256`); `Gate.engine` is public.

The new method, next to `_record_constraint_denial`:

```python
    def _record_review_denial(
        self,
        context: ToolContext,
        call: ToolCall,
        tool_name: str,
        arguments: dict[str, Any],
        ticket: str,
        error: str,
        *,
        pending_approval: PolicyDecision | None = None,
    ) -> ToolExecution:
        """Record a Gate denial: a decision that does not allow execution, or a human's rejection.

        Unlike a constraint denial, this one carries the call's identity -- the tool, its
        validated redacted ``arguments`` and ``tool_intent_sha256`` -- so a later
        ``human.approval`` or ``tool.started`` can be paired with it from the log alone.
        ``pending_approval`` is the decision a human still has to answer; with it set, the agent
        bridge ends the step instead of handing the model an error line.
        """
        result = ToolResult(success=False, error=error)
        denied = call.model_copy(
            update={"status": ToolCallStatus.DENIED, "completed_at": _now(), "error": error}
        )
        context.event_log.append(
            EventType.TOOL_DENIED,
            _tool_actor(tool_name),
            {
                "tool_call_id": denied.id,
                "decision_id": denied.policy_decision_id,
                "reason": error,
                "tool": tool_name,
                "arguments": arguments,
                "tool_intent_sha256": ticket,
            },
            subject_id=denied.id,
        )
        return ToolExecution(call=denied, result=result, pending_approval=pending_approval)
```

`execute`, from the argument validation to the budget guard, becomes:

```python
        try:
            real_arguments = _validate_arguments(tool, real_arguments)
        except (ToolExecutionError, KeyError, OSError, ValueError) as exc:
            return self._record_failed_call(context, call, recorded_name, _message(exc))
        # The identity a human answers to is the *validated* call: `{"path": "a"}` and
        # `{"path": "a", "encoding": None}` are one intent once the arguments model has spoken.
        validated_arguments = _redacted(real_arguments)
        ticket = tool_intent_sha256(tool_name, validated_arguments)
        answer = _human_answer(context, ticket)
        if answer is not None and answer.approval is None:
            rejected = call.model_copy(update={"policy_decision_id": answer.decision.id})
            reason = "tool call rejected by a human" + (f": {answer.note}" if answer.note else "")
            return self._record_review_denial(
                context, rejected, tool_name, validated_arguments, ticket, reason
            )
        approval = answer.approval if answer is not None else None
        decision = (
            answer.decision
            if answer is not None
            else context.gate.check_capability(
                subject_id=call.id,
                capability=tool.capability,
                risk=context.risk_profile,
                summary=tool_name,
                details={
                    "tool": tool_name,
                    "arguments": validated_arguments,
                    "tool_intent_sha256": ticket,
                },
            )
        )
        call = call.model_copy(update={"policy_decision_id": decision.id})
        # The one authorization check for a review-gated tool, Contract 0.3's: the decision the
        # Gate recorded and a separate human Approval that names it. A ticket is only ever that
        # Approval, reconstructed from the human's own event -- never a new lenient branch.
        if not allows_execution(decision, approval):
            pending = (
                approval is None
                and decision.requires_human_approval
                and _unanswered(context, decision.id)
            )
            return self._record_review_denial(
                context,
                call,
                tool_name,
                validated_arguments,
                ticket,
                f"tool call denied: {decision.reason}",
                pending_approval=decision if pending else None,
            )
```

and the `tool.started` payload becomes

```python
            {
                "tool_call_id": call.id,
                "tool": tool_name,
                "arguments": redacted_arguments,
                "tool_intent_sha256": ticket,
                "decision_id": call.policy_decision_id,
            },
```

`execute` already carries `# noqa: PLR0911`; if ruff now reports `PLR0912`/`PLR0915` (branches/statements), extend the same noqa comment with the new codes and the reason `ordered fail-closed lifecycle branches stay explicit` — do not split `execute` in this task. Export `tool_intent_sha256` from `thymira/tools/__init__.py` (import, `__all__`, and one clause in the module docstring: "`tool_intent_sha256` names a call for a human's one-shot approval").

- [ ] **Step 7: Run the tests**

Run: `uv run pytest tests/thymira/test_tools.py tests/thymira/test_tools_manager.py tests/thymira/test_policies.py tests/thymira/test_policies_approval.py tests/thymira/test_agents_tool_bridge.py -q --basetemp=C:/Users/lucas/AppData/Local/Temp/thy-bt3`
Expected: PASS. `test_a_synchronous_approval_still_never_runs_a_review_gated_tool` and `test_a_review_the_approver_declined_denies_the_call_and_never_runs_the_tool` must pass unchanged (their `auto_*` answers are automatic, so nothing is authorized).

- [ ] **Step 8: Gates and commit**

Run: `just lint && just typecheck && just check-imports`, then

```bash
git add runtime/policies runtime/tools tests/thymira/test_tools.py tests/thymira/test_policies.py tests/thymira/test_policies_approval.py
git commit -m "feat(tools,policies): a human's one-shot ticket for the exact tool call, matched by intent and re-verified through allows_execution"
```

---

### Task 2: The step ends cleanly

**Files:**
- Modify: `runtime/agents/src/thymira/agents/tool_bridge.py` (imports; `render_tool_output` lines 75-101; `_build_one_tool` lines 104-123)
- Modify: `runtime/agents/src/thymira/agents/runner.py` (imports lines 50-52; the `PydanticAgent` construction lines 254-260; the `run_sync` block lines 275-319)
- Modify: `runtime/agents/src/thymira/agents/delegation.py` (`_render_text` lines 61-67; `delegate` lines 126-153)
- Modify: `runtime/agents/src/thymira/agents/__init__.py` (export `AgentEndReason`)
- Test: `tests/thymira/test_agents_tool_bridge.py`, `tests/thymira/test_agents_runner.py`, `tests/thymira/test_agents_delegation.py`

**Interfaces:**
- Consumes: `ToolExecution.pending_approval` (Task 1).
- Produces: `thymira.agents.AgentEndReason` (`StrEnum`: `COMPLETED = "completed"`, `MAX_TURNS = "max_turns"`, `AWAITING_APPROVAL = "awaiting_approval"`); `agent.completed` payload key `end_reason` on every step, plus `decision_id` on an awaiting one; `AgentResult.task_status is TaskStatus.PENDING` for a deferred step; `Delegator.delegate` returns a `Task` with `status=PENDING`, `error=None`, `completed_at=None`, and appends an `agent.message` whose text is `f"{agent} is waiting for a human to approve a tool call: {reason}"`; `tool_bridge.DENIED_MARKER = "[denied]"`.
- Consumed by: Task 3 (Execute reads `outcome.status is TaskStatus.PENDING`).

- [ ] **Step 1: Write the failing tests**

`tests/thymira/test_agents_tool_bridge.py` (reuse `_FakeTool`, `_EchoArguments`, `_spec`, `_tool_context`; add a `_review_context` helper):

```python
from thymira.agents import AgentEndReason
from thymira.agents.tool_bridge import DENIED_MARKER
from thymira.mira.checks import AuditContext, ControlStatus, audit_run
from thymira.policies import GateMode


def _review_context(tmp_path: Path, *, run_id: str, approver: Approver | None) -> ToolContext:
    """A context whose Gate escalates every local tool to a human (low-confidence risk)."""
    log = InMemoryEventLog(run_id)
    mode = GateMode.DEFERRED if approver is None else GateMode.SYNCHRONOUS
    return ToolContext(
        run_id=run_id,
        agent_id=new_id("agent"),
        workspace=tmp_path / "workspace",
        event_log=log,
        gate=Gate(PolicyEngine(load_policy_stack()), log, approver=approver, mode=mode),
        artifact_store=LocalArtifactStore(tmp_path / "artifacts", run_id),
        risk_profile=RiskProfile(risk_level="limited", activity_category="analysis", confidence=0.1),
    )


def _coding_catalog(spec: AgentSpec) -> AgentCatalog:
    return AgentCatalog(
        (spec,),
        system_prompts={spec.name: "You are a coding agent."},
        output_schemas={spec.name: DataProfileOutput},
    )


def test_a_review_required_denial_ends_the_step_pending_with_a_paired_lifecycle(
    tmp_path: Path,
) -> None:
    run_id = new_id("run")
    task = Task(id=new_id("task"), run_id=run_id, agent_id=new_id("agent"), objective="Profile.")
    echo = _FakeTool("echo", ToolCapability(id="echo", external_effects=()), arguments_model=_EchoArguments)
    tool_context = _review_context(tmp_path, run_id=run_id, approver=None)
    spec = AgentSpec(
        name="coding", role=Role.AGENT, task_kinds=("code",), tool_allowlist=("echo",),
        max_turns=3, max_depth=1, system_prompt_ref="prompts/coding.md",
        output_schema_ref="tests.thymira.fixtures_agent_output:DataProfileOutput",
    )
    # A second scripted item that must never be consumed: the step ends on the first call.
    provider = ScriptedProvider(
        [
            LLMToolCall(id="call-1", name="echo", arguments={"value": "x"}),
            LLMToolCall(id="call-2", name="final_result", arguments={"row_count": 1, "columns": ["value"]}),
        ]
    )

    result = AgentRunner().run(
        spec, task,
        AgentContext(catalog=_coding_catalog(spec), event_log=tool_context.event_log,
                     provider=provider, tool_registry=ToolRegistry((echo,)), tool_context=tool_context),
    )

    assert result.task_status is TaskStatus.PENDING
    assert result.output is None
    assert echo.seen_invocation == []
    assert len(provider.calls) == 1
    events = tool_context.event_log.events()
    completed = next(e for e in events if e.type == EventType.AGENT_COMPLETED)
    decision = next(e for e in events if e.type == EventType.POLICY_DECISION)
    assert completed.payload["status"] == TaskStatus.PENDING.value
    assert completed.payload["end_reason"] == AgentEndReason.AWAITING_APPROVAL.value
    assert completed.payload["decision_id"] == decision.payload["id"]
    assert [e.type for e in events if e.type in (EventType.TOOL_DENIED, EventType.TOOL_STARTED)] == [EventType.TOOL_DENIED]
    # MIRA's A9 pairs started/completed by the agent's own id: a deferred step is a closed step.
    report = audit_run(AuditContext(run_id, tuple(events)))
    a9 = next(control for control in report.controls if control.control_id == "A9")
    assert a9.status is ControlStatus.PASS


def test_an_automatically_answered_review_is_an_ordinary_denial_the_model_reads(
    tmp_path: Path,
) -> None:
    run_id = new_id("run")
    echo = _FakeTool("echo", ToolCapability(id="echo", external_effects=()))
    context = _review_context(tmp_path, run_id=run_id, approver=auto_approve)
    tools = build_agent_tools(_spec("echo"), ToolRegistry((echo,)), context)

    output = _invoke_via_model(tools, "echo")

    assert output.startswith("Error: tool call denied")
    assert output.splitlines()[-1] == DENIED_MARKER
    assert echo.seen_invocation == []


def test_render_marks_a_denied_call_last() -> None:
    call = ToolCall(id=new_id("tool"), run_id=new_id("run"), agent_id=new_id("agent"),
                    tool_name="echo", status=ToolCallStatus.DENIED)
    execution = ToolExecution(call=call, result=ToolResult(success=False, error="tool call denied: x"))

    assert render_tool_output(execution) == "Error: tool call denied: x\n[denied]"
```

Check what `AuditContext` requires (`tests/thymira/test_agents_runner.py::test_mira_detects_the_agent_that_started_and_never_completed` builds one) and copy that construction. If A9's id or the `ControlStatus` member is named differently there, use the names that test uses.

`tests/thymira/test_agents_runner.py` — extend the two existing lifecycle tests rather than adding new ones:

```python
    # in test_a_valid_structured_response_appends_started_then_completed_and_is_returned:
    assert completed.payload["end_reason"] == AgentEndReason.COMPLETED.value
    # in test_a_run_exceeding_max_turns_ends_with_task_failed_never_a_silent_drop:
    assert completed_payload["end_reason"] == AgentEndReason.MAX_TURNS.value
```

`tests/thymira/test_agents_delegation.py` — one test, modelled on `test_a_failed_delegation_diagnoses_from_the_last_failed_tool_call` (`:171`), using its `_catalog`/`_tool_context` helpers but a Gate that escalates (`confidence=0.1`, `mode=GateMode.DEFERRED`, no approver) and a scripted tool call:

```python
def test_a_pending_delegation_says_it_waits_for_a_human(tmp_path: Path) -> None:
    ...  # same setup shape as the failed-delegation test, with the review-escalating context
    task = Delegator(ctx).delegate("thy", spec, "profile it", depth=0)

    assert task.status is TaskStatus.PENDING
    assert task.error is None
    assert task.completed_at is None
    message = next(e for e in log.events() if e.type is EventType.AGENT_MESSAGE)
    assert message.payload["status"] == TaskStatus.PENDING.value
    assert message.payload["text"].startswith(
        "data is waiting for a human to approve a tool call: tool call denied:"
    )
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/thymira/test_agents_tool_bridge.py tests/thymira/test_agents_runner.py tests/thymira/test_agents_delegation.py -q --basetemp=C:/Users/lucas/AppData/Local/Temp/thy-bt3`
Expected: FAIL (`ImportError: AgentEndReason` / `DENIED_MARKER`; the deferred step returns FAILED or raises `UserError`).

- [ ] **Step 3: The bridge** (`tool_bridge.py`)

```python
from pydantic_ai import ApprovalRequired, Tool

from thymira.observability import tool as tool_observation
from thymira.schemas import ToolCallStatus
from thymira.tools import ToolExecution, ToolManager, input_schema

DENIED_MARKER = "[denied]"
"""The last line of every denied call's rendering: the fact the model anchors on to tell "not
allowed" from "failed". A denied call is not retried unchanged; the agent adapts or reports."""
```

In `render_tool_output`, after the exit-code marker:

```python
    if execution.call.status is ToolCallStatus.DENIED:
        lines.append(DENIED_MARKER)
    return "\n".join(lines)
```

and extend its docstring: "a denied call ends with ``[denied]`` after everything else". In `_call`:

```python
    def _call(**arguments: Any) -> str:
        with tool_observation(name=tool_name, arguments=arguments) as observation:
            execution = manager.execute(context, tool_name, arguments)
            if usage is not None:
                usage.charge_tool()
            output = render_tool_output(execution)
            observation.update(output=_bounded(output))
            if execution.pending_approval is not None:
                # The step ends here, cleanly: PydanticAI hands back `DeferredToolRequests`
                # instead of looping, the runner records the step as PENDING, and the runtime --
                # never the model -- decides when this exact call runs (`ToolManager`'s ticket).
                raise ApprovalRequired(
                    metadata={
                        "decision_id": execution.pending_approval.id,
                        "tool_call_id": execution.call.id,
                    }
                )
            return output
```

Update the module docstring's second sentence: a denied capability comes back as a bounded error the model reads *unless a human still has to answer it, in which case the step ends with `ApprovalRequired`*. Add `DENIED_MARKER` to `__all__`. Verify `tool_observation` tolerates an exception leaving its block (it is the isolated observability seam; if it re-raises anything other than the `ApprovalRequired`, fix it there — it must never fail a Run).

- [ ] **Step 4: The runner** (`runner.py`)

```python
from enum import StrEnum

from pydantic_ai import Agent as PydanticAgent
from pydantic_ai import DeferredToolRequests


class AgentEndReason(StrEnum):
    """Why a step ended, recorded as ``end_reason`` on every ``agent.completed``.

    ``COMPLETED``: a validated output. ``MAX_TURNS``: PydanticAI exhausted the step's retries or
    request limit. ``AWAITING_APPROVAL``: a tool call needs a human's answer; the step is closed
    (paired ``agent.started``/``agent.completed``) and the task is PENDING until the runtime
    re-runs it.
    """

    COMPLETED = "completed"
    MAX_TURNS = "max_turns"
    AWAITING_APPROVAL = "awaiting_approval"
```

The agent:

```python
        agent: PydanticAgent[None, BaseModel | DeferredToolRequests] = PydanticAgent(
            model=model,
            # `DeferredToolRequests` is not an output tool: it lets a tool end the step with
            # `ApprovalRequired` (the bridge, on a review the Gate left pending) instead of
            # PydanticAI raising `UserError`. The output tool keeps its `final_result` name.
            output_type=[output_schema, DeferredToolRequests],
            instructions=assembled.system,
            retries=max(spec.max_turns - 1, 0),
            tools=tools,
        )
```

The failure branch's payload gains `"end_reason": AgentEndReason.MAX_TURNS.value` (keep `"max_turns": spec.max_turns`). After the `try/except`, before the success append:

```python
            if isinstance(result.output, DeferredToolRequests):
                pending = next(iter(result.output.metadata.values()), {})
                ctx.event_log.append(
                    EventType.AGENT_COMPLETED,
                    ctx.actor,
                    {
                        "agent": spec.name,
                        "task_id": task.id,
                        "status": TaskStatus.PENDING.value,
                        "end_reason": AgentEndReason.AWAITING_APPROVAL.value,
                        "decision_id": pending.get("decision_id"),
                        "step_key": step_key,
                        **_usage_payload(result.usage, ctx.usage, cost_before),
                    },
                    subject_id=task.agent_id,
                )
                observation.update(output={"status": TaskStatus.PENDING.value})
                return AgentResult(output=None, usage=result.usage, task_status=TaskStatus.PENDING)
```

The success payload gains `"end_reason": AgentEndReason.COMPLETED.value`. The success `AgentResult` needs `result.output` narrowed to `BaseModel` for ty (`isinstance` above narrows it; if ty still reports, assign `output = result.output` after the `isinstance` branch and annotate). Update the module docstring (one paragraph: the deferred step, `end_reason`). Export `AgentEndReason` from `thymira/agents/__init__.py`.

- [ ] **Step 5: The delegator** (`delegation.py`)

```python
def _render_text(
    agent_name: str, status: TaskStatus, summary: str | None, error: str | None
) -> str:
    """A one-line rendering of the outcome, for a later step's history (`PromptBuilder`, THY-05)."""
    if status is TaskStatus.COMPLETED:
        return f"{agent_name} completed: {summary}"
    if status is TaskStatus.PENDING:
        return f"{agent_name} is waiting for a human to approve a tool call: {error}"
    return f"{agent_name} failed: {error}"
```

In `delegate`: diagnose for PENDING too (the last `tool.denied` reason names the call), but keep the `Task` honest:

```python
        summary = result.output.model_dump_json() if result.output is not None else None
        diagnosis = None
        if result.task_status in (TaskStatus.FAILED, TaskStatus.PENDING):
            diagnosis = _diagnose_failure(self._ctx.event_log.events()[events_before:])
        pending = result.task_status is TaskStatus.PENDING
        ...  # agent.message text uses `diagnosis`
        return task.model_copy(
            update={
                "status": result.task_status,
                "summary": summary,
                # A pending task has not failed: its reason lives on the message, not the record.
                "error": None if pending else diagnosis,
                "completed_at": None if pending else utc_now(),
            }
        )
```

Add one paragraph to the module docstring (a PENDING result: the step closed on a review the Gate left to a human; the text says so, the `Task` stays open).

- [ ] **Step 6: Run the tests**

Run: `uv run pytest tests/thymira/test_agents_tool_bridge.py tests/thymira/test_agents_runner.py tests/thymira/test_agents_delegation.py tests/thymira/test_agents_llm.py tests/thymira/test_thy_execute.py tests/thymira/test_thy_e2e.py -q --basetemp=C:/Users/lucas/AppData/Local/Temp/thy-bt3`
Expected: PASS. Every existing scripted test keeps working: with `DeferredToolRequests` in the list the output tool is still named `final_result` (verified against PydanticAI 2.33.0 before this plan was written).

- [ ] **Step 7: Gates and commit**

Run: `just lint && just typecheck && just check-imports`, then

```bash
git add runtime/agents tests/thymira/test_agents_tool_bridge.py tests/thymira/test_agents_runner.py tests/thymira/test_agents_delegation.py
git commit -m "feat(agents): a review the Gate left to a human ends the step cleanly (PENDING, end_reason), and a denial renders as a fact"
```

---

### Task 3: THY carries its progress across a park

**Files:**
- Modify: `runtime/thy/src/thymira/thy/models.py` (after `RegisteredDataset`; `ThyState`; `ThyOutput`)
- Modify: `runtime/thy/src/thymira/thy/nodes/plan.py` (`_plan`, lines 74-81)
- Modify: `runtime/thy/src/thymira/thy/nodes/execute.py` (`_run_sequential` lines 149-205; new `_carried_outcomes`)
- Modify: `runtime/thy/src/thymira/thy/graph.py` (`_route_after_execute` lines 97-108; `run_thy` lines 299-313; module docstring)
- Modify: `runtime/thy/src/thymira/thy/__init__.py` (export `ThyProgress`)
- Test: `tests/thymira/test_thy_plan.py`, `tests/thymira/test_thy_execute.py`, `tests/thymira/test_thy_graph.py`

**Interfaces:**
- Consumes: `TaskStatus.PENDING` outcomes from `Delegator` (Task 2).
- Produces: `thymira.thy.ThyProgress(plan, completed, agent_messages, experiments, artifacts)`; `ThyState.awaiting_approval() -> bool`; `ThyOutput.progress: ThyProgress | None`; `plan_node` short-circuits when `state.plan` is non-empty; `_route_after_execute` returns `END` when a task is pending; Execute skips every task whose outcome is carried and re-runs only the PENDING one.
- Consumed by: Task 4 (`ThySubgraph` seeds `ThyState` from `ThyProgress` and reads `ThyOutput.progress`).

- [ ] **Step 1: Write the failing tests**

`tests/thymira/test_thy_plan.py`:

```python
def test_a_seeded_plan_skips_the_model_and_the_gate(tmp_path: Path) -> None:
    """A Core resume seeds the gated plan: re-planning could plan the waiting call away."""
    run = _run()
    log = InMemoryEventLog(run.id)
    provider = ScriptedProvider([])
    node = plan_node(_gate(log, approver=auto_reject), log, provider=provider)
    seeded = ThyState(run=run, phase=ThyPhase.PLAN, plan=_PLAN_OUTPUT.tasks)

    result = ThyState.model_validate(node(seeded))

    assert result.phase is ThyPhase.EXECUTE
    assert result.plan == _PLAN_OUTPUT.tasks
    assert result.error is None
    assert provider.calls == []
    assert not any(e.type is EventType.POLICY_DECISION for e in log.events())
```

`tests/thymira/test_thy_execute.py` — extend `_data_agent_catalog` with `*, tool_allowlist: tuple[str, ...] = ()` (write `tool_allowlist: [echo]` into the YAML when given and pass `known_capabilities=frozenset(tool_allowlist)`), add a 12-line `_FakeTool` + `_EchoArguments` (copy from `test_agents_tool_bridge.py`), and a `_review_context(tmp_path, run, log)` helper building a `ToolContext` with `Gate(PolicyEngine(load_policy_stack()), log, mode=GateMode.DEFERRED)` and `RiskProfile(risk_level="limited", activity_category="analysis", confidence=0.1)`:

```python
def test_a_review_gated_tool_call_parks_execute_before_the_next_task(tmp_path: Path) -> None:
    run = _run()
    log = InMemoryEventLog(run.id)
    catalog = _data_agent_catalog(tmp_path, tool_allowlist=("echo",))
    echo = _FakeTool("echo", ToolCapability(id="echo", external_effects=()), arguments_model=_EchoArguments)
    provider = ScriptedProvider(
        [
            LLMToolCall(id="c1", name="echo", arguments={"value": "x"}),
            DataProfileOutput(row_count=2, columns=("b",)),  # t2's answer: must never be consumed
        ]
    )
    plan = (_task("t1"), _task("t2"))
    graph = build_thy_graph(
        catalog, log, provider=provider,
        tool_registry=ToolRegistry((echo,)), tool_context=_review_context(tmp_path, run, log),
    )

    final_state = ThyState.model_validate(graph.invoke(ThyState(run=run, plan=plan)))

    assert final_state.awaiting_approval()
    assert [m.status for m in final_state.agent_messages] == [TaskStatus.PENDING]
    assert final_state.completed == (plan[0],)
    assert final_state.summary is None  # Summarize never ran
    assert final_state.error is None
    assert len(provider.calls) == 1


def test_a_seeded_pass_keeps_decided_outcomes_and_re_runs_only_the_pending_task(
    tmp_path: Path,
) -> None:
    run = _run()
    log = InMemoryEventLog(run.id)
    catalog = _data_agent_catalog(tmp_path)
    plan = (_task("t1"), _task("t2"), _task("t3"))
    decided = Task(id=new_id("task"), run_id=run.id, agent_id=new_id("agent"), objective="do t1",
                   status=TaskStatus.COMPLETED, summary='{"row_count": 1, "columns": ["a"]}')
    pending = Task(id=new_id("task"), run_id=run.id, agent_id=new_id("agent"), objective="do t2",
                   status=TaskStatus.PENDING)
    provider = ScriptedProvider(
        [DataProfileOutput(row_count=2, columns=("b",)), DataProfileOutput(row_count=3, columns=("c",))]
    )
    graph = build_thy_graph(catalog, log, provider=provider)
    # Exactly the seed a Core resume builds: phase at its INSPECT default, the plan and the
    # outcomes carried. Inspect and Plan fall through (a seeded plan calls no model).
    seeded = ThyState(run=run, plan=plan,
                      completed=(plan[0], plan[1]), agent_messages=(decided, pending))

    final_state = ThyState.model_validate(graph.invoke(seeded))

    assert not final_state.awaiting_approval()
    assert final_state.completed == plan
    assert [m.status for m in final_state.agent_messages] == [TaskStatus.COMPLETED] * 3
    assert final_state.agent_messages[0] == decided  # carried as it was, never retried
    assert len(provider.calls) == 2
```

LangGraph always starts at `inspect` regardless of `ThyState.phase`, and both fallbacks call `advance()`, which raises from EXECUTE — so a seeded state is always built at the INSPECT default, exactly as the Core resume builds it.

`tests/thymira/test_thy_graph.py` — one test on `run_thy` (build the same review-escalating context as above):

```python
def test_run_thy_hands_back_progress_only_when_a_task_awaits_a_human(tmp_path: Path) -> None:
    ...  # parked run: run_thy(...) with the echo tool and the review context
    assert output.error is None
    assert output.progress is not None
    assert output.progress.plan == plan
    assert [m.status for m in output.progress.agent_messages] == [TaskStatus.PENDING]
    ...  # a plain run (no tools): output.progress is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/thymira/test_thy_plan.py tests/thymira/test_thy_execute.py tests/thymira/test_thy_graph.py -q --basetemp=C:/Users/lucas/AppData/Local/Temp/thy-bt3`
Expected: FAIL (`AttributeError: awaiting_approval`, the seeded plan calls the provider, the parked pass runs t2).

- [ ] **Step 3: Models** (`models.py`)

```python
from thymira.schemas import (..., TaskStatus, ...)


class ThyProgress(ThymiraModel):
    """What a parked ThyGraph pass hands the Core so the next pass resumes instead of replanning.

    Carried verbatim: the gated plan, every outcome so far (`completed[i]` paired with
    `agent_messages[i]`, the PENDING one included), and the experiments and artifacts folded so
    far. The Core seeds it back into `ThyState` on the resumed pass, where Plan short-circuits
    on the seeded plan and Execute keeps every decided outcome and re-runs only the task that
    asked -- so a human's answer is re-offered to exactly the call that asked for it.
    """

    plan: tuple[AgentTask, ...] = Field(min_length=1)
    completed: tuple[AgentTask, ...] = ()
    agent_messages: tuple[Task, ...] = ()
    experiments: tuple[Experiment, ...] = ()
    artifacts: tuple[Artifact, ...] = ()
```

On `ThyState`:

```python
    def awaiting_approval(self) -> bool:
        """Whether a delegated task ended PENDING: a tool call of its is waiting for a human."""
        return any(message.status is TaskStatus.PENDING for message in self.agent_messages)
```

On `ThyOutput`: `progress: ThyProgress | None = None` with the docstring "Set when the pass stopped on a task awaiting a human's answer (`ThyState.awaiting_approval`); the Core carries it into the next pass. `None` for a finished pass." Export `ThyProgress` from `thymira/thy/__init__.py`.

- [ ] **Step 4: Plan short-circuits** (`plan.py`, first lines of `_plan`)

```python
    def _plan(state: ThyState) -> dict[str, Any]:
        if state.plan:
            # A seeded plan (a Core resume after a human answered a tool-call review) was gated
            # when it was proposed. Asking the model again could plan the waiting call away,
            # and asking the Gate again would mint a second `plan.proposed` decision for it.
            return state.advance().model_dump()
```

Add one sentence to the module docstring and to `plan_node`'s docstring.

- [ ] **Step 5: Execute keeps decided outcomes and stops at a pending one** (`execute.py`)

Replace the start of `_run_sequential`'s bookkeeping and its loop:

```python
    delegator = Delegator(ctx)
    carried = _carried_outcomes(state)
    completed = [agent_task for agent_task, _ in carried.values()]
    agent_messages = [outcome for _, outcome in carried.values()]
    artifacts = list(state.artifacts)
    experiments = list(state.experiments)
    status_by_id: dict[str, TaskStatus] = {
        task_id: outcome.status for task_id, (_, outcome) in carried.items()
    }

    for agent_task in state.plan:
        if agent_task.id in carried:
            continue
        before_ids = _artifact_ids(store)
        if _blocked(agent_task, status_by_id):
            outcome = _skipped_task(state, agent_task)
        else:
            spec = catalog.get(agent_task.agent.value)
            outcome = delegator.delegate("thy", spec, agent_task.instruction, depth=0)
        completed.append(agent_task)
        agent_messages.append(outcome)
        status_by_id[agent_task.id] = outcome.status

        produced = _new_artifacts(store, before_ids)
        artifacts.extend(produced)
        experiment = _experiment_or_none(state, agent_task, outcome, produced)
        if experiment is not None:
            experiments.append(experiment)
        if outcome.status is TaskStatus.PENDING:
            # The Run parks here: a human must answer before this task can finish, and nothing
            # after it is attempted until then (`_route_after_execute` ends the graph).
            break
```

and add:

```python
def _carried_outcomes(state: ThyState) -> dict[str, tuple[AgentTask, Task]]:
    """The outcomes an earlier pass already decided, keyed by plan-local task id.

    A seeded state (`ThyProgress`, a Core resume) pairs `completed[i]` with `agent_messages[i]`.
    Every decided outcome -- COMPLETED, FAILED or SKIPPED -- is kept as it was and never
    retried: a resume re-offers a human's answer to the one call that asked, it is not a second
    attempt at the plan. The PENDING outcome is that task, so it is the one left out here and
    re-run.
    """
    return {
        agent_task.id: (agent_task, outcome)
        for agent_task, outcome in zip(state.completed, state.agent_messages, strict=True)
        if outcome.status is not TaskStatus.PENDING
    }
```

`_run_parallel` is unchanged: it runs only without a `tool_context`, where no denial can occur. Add a paragraph to the module docstring (a PENDING outcome, the seeded pass).

- [ ] **Step 6: The graph** (`graph.py`)

```python
def _route_after_execute(state: ThyState) -> str:
    """Route after Execute: a pending task ends the graph, a rework reopens, otherwise Summarize.

    A task failure never halts here (bug-hunt C1) ... [keep the existing paragraph] ... A task
    left PENDING -- a tool call waiting for a human -- ends the graph before Summarize instead:
    the pass is knowingly incomplete, and the Core carries `ThyOutput.progress` into the pass
    that finishes it.
    """
    if state.awaiting_approval():
        return END
    if state.rework is not None:
        return "rework"
    return "summarize"
```

In `run_thy`, before the return:

```python
    progress = (
        ThyProgress(
            plan=final_state.plan,
            completed=final_state.completed,
            agent_messages=final_state.agent_messages,
            experiments=final_state.experiments,
            artifacts=final_state.artifacts,
        )
        if final_state.awaiting_approval()
        else None
    )
```

and `progress=progress` on the `ThyOutput`. Import `ThyProgress`. Update `run_thy`'s docstring (`initial_state` is also how the Core seeds a resumed pass) and the module docstring's routing paragraph. `_CONDITIONAL_EDGES` already lists `END` for `execute`; `graph_definition_hash()` therefore does not change — assert that in `test_thy_graph.py` only if a hash literal is pinned somewhere (there is none today; do not add one).

- [ ] **Step 7: Run the tests**

Run: `uv run pytest tests/thymira/test_thy_plan.py tests/thymira/test_thy_execute.py tests/thymira/test_thy_graph.py tests/thymira/test_thy_e2e.py tests/thymira/test_thy_rework.py tests/thymira/test_thy_inspect.py -q --basetemp=C:/Users/lucas/AppData/Local/Temp/thy-bt3`
Expected: PASS.

- [ ] **Step 8: Gates and commit**

Run: `just lint && just typecheck && just check-imports`, then

```bash
git add runtime/thy tests/thymira/test_thy_plan.py tests/thymira/test_thy_execute.py tests/thymira/test_thy_graph.py
git commit -m "feat(thy): a pending tool-call review parks ThyGraph before Summarize and hands the Core its progress; a seeded pass replans nothing and re-runs only the task that asked"
```

---

### Task 4: The Core parks, resumes and seeds

**Files:**
- Modify: `runtime/core/src/thymira/core/graph/state.py` (imports line 23; `RuntimeState` lines 40-62)
- Modify: `runtime/core/src/thymira/core/graph/adapters.py` (`ThySubgraph` docstring lines 282-309; `invoke` lines 353-394)
- Modify: `runtime/core/src/thymira/core/graph/compose.py` (edge tables lines 46-59; `build_runtime_graph` line 109-114; `run_thy` lines 147-158; new `route_after_thy`, `_park_for_tool_review`)
- Modify: `runtime/core/src/thymira/core/dispatch.py` (`submit` line 74; `resume` line 100; `_resume_node` lines 102-112)
- Modify: `runtime/core/src/thymira/core/runs.py` (`resolve_approval` lines 237-276)
- Test: `tests/thymira/test_core_composition.py`, `tests/thymira/test_core_dispatch.py`, `tests/thymira/test_core_graph_adapters.py`, `tests/thymira/test_e2e_runtime_spine.py` (line 266), the `RuntimeState` round-trip test module (find it with `grep -rn "deserialize_runtime_state" tests/`)

**Interfaces:**
- Consumes: `ThyProgress`, `ThyOutput.progress` (Task 3); `decision_from_events`, `pending_approvals` (Task 1).
- Produces: `RuntimeState.thy_progress: ThyProgress | None`; the composed graph's conditional edge `("thy", ("mira", END))`; `run.transitioned` resume payload `{"resume_from": "execution", "approved": bool, "approved_by" | "rejected_by": actor}`; `InlineDispatcher._resume_node` → `"execution_gate"` for it; `ExecutionDispatchError` messages `run {id}: inline execution failed: {cause}` / `inline resume failed: {cause}`; `RunService.resume` of a Run parked on a tool-call review whose approval was answered out of band (the governance `ApprovalService`, which records evidence and never transitions) writes `resume_from: "execution"` too, derived from the decision the Run was parked on (`run.transitioned` with `command == "wait_for_approval"`, payload `policy_decision.subject_kind`).
- Consumed by: Task 6 (the composed proof), Task 8 (the record).

- [ ] **Step 1: Write the failing tests**

`tests/thymira/test_core_composition.py` — a THY double that parks on its first pass and finishes on its second, then the park/resume assertions:

```python
from thymira.policies import RiskProfile, ToolCapability
from thymira.thy.models import AgentTask, ThyAgentKind, ThyPhase, ThyProgress


class _ParkingThy(_ThySubgraph):
    """THY that stops on a review-gated tool call once, then finishes when seeded."""

    def __init__(self) -> None:
        self.calls: list[bool] = []  # one entry per invoke: True when seeded

    def invoke(self, state: RuntimeState, *, deps: SubgraphDeps) -> RuntimeState:
        self.calls.append(state.thy_progress is not None)
        if state.thy_progress is not None:
            return super().invoke(state.model_copy(update={"thy_progress": None}), deps=deps)
        deps.gate.check_capability(
            subject_id="tool_1",
            capability=ToolCapability(id="echo", external_effects=()),
            risk=RiskProfile(risk_level="limited", activity_category="analysis", confidence=0.1),
            summary="echo",
            details={"tool": "echo", "arguments": {"value": "x"}, "tool_intent_sha256": "a" * 64},
        )
        task = AgentTask(id="t1", agent=ThyAgentKind.DATA, phase=ThyPhase.EXECUTE, instruction="x")
        return state.model_copy(update={"thy_progress": ThyProgress(plan=(task,))})


@pytest.mark.parametrize("approved", [True, False])
def test_a_tool_call_review_parks_the_run_before_mira_and_either_answer_resumes_into_thy(
    tmp_path: Path, approved: bool
) -> None:
    run, store, deps, controller = _runtime(tmp_path)
    thy = _ParkingThy()
    mira_calls: list[str] = []

    class _CountingMira(_MiraSubgraph):
        def invoke(self, state: RuntimeState, *, deps: SubgraphDeps) -> RuntimeState:
            mira_calls.append(state.run_id)
            return super().invoke(state, deps=deps)

    def graph_factory(_run: Run):
        return build_runtime_graph(
            thy, _CountingMira(),
            checkpointer=StateCheckpointer(LocalCheckpointRepository(tmp_path / "checkpoints")),
            deps=deps, controller=controller,
        )

    graph = graph_factory(run)
    graph.invoke(initial_runtime_state(run), {"configurable": {"thread_id": run.id}})
    parked = RunController(store).current_state(run.id).state
    assert parked.condition is RunCondition.WAITING
    assert parked.wait_reason is WaitReason.APPROVAL
    assert mira_calls == []
    assert thy.calls == [False]

    service = RunService(store, SessionService(LocalSessionRepository(tmp_path / "sessions")),
                         dispatcher=InlineDispatcher(store, graph_factory),
                         policy_sha256="0" * 64, graph_definition_hash="1" * 64)
    reviewer = Actor(kind="human", id="reviewer", authenticated=True)
    answered = service.resolve_approval(
        run.id,
        gate=Gate(PolicyEngine(load_default_policy()), RunEventLog(store, run.id),
                  approver=lambda _request: approved, human=reviewer),
        actor=reviewer,
    )

    assert answered.status.value == "COMPLETED"
    assert thy.calls == [False, True]
    assert mira_calls == [run.id]
    resumed = next(e for e in store.events(run.id)
                   if e.type is EventType.RUN_TRANSITIONED and e.payload.get("command") == "resume")
    assert resumed.payload["resume_from"] == "execution"
    assert resumed.payload["approved"] is approved
    assert verify_events(store.events(run.id)).valid
```

A second composition test — the out-of-band answer (bug-hunt Foco 3, "C7": the governance route records evidence only, so a plain `resume` must know where the Run was parked; without this it would resume at `gate`, find no decision and fail the Run):

```python
def test_a_tool_call_review_answered_out_of_band_resumes_into_thy_on_a_plain_resume(
    tmp_path: Path,
) -> None:
    run, store, deps, controller = _runtime(tmp_path)
    thy = _ParkingThy()

    def graph_factory(_run: Run):
        return build_runtime_graph(
            thy, _MiraSubgraph(),
            checkpointer=StateCheckpointer(LocalCheckpointRepository(tmp_path / "checkpoints")),
            deps=deps, controller=controller,
        )

    graph_factory(run).invoke(initial_runtime_state(run), {"configurable": {"thread_id": run.id}})
    assert RunController(store).current_state(run.id).state.wait_reason is WaitReason.APPROVAL
    reviewer = Actor(kind="human", id="reviewer", authenticated=True)
    log = RunEventLog(store, run.id)
    decision_id = pending_approvals(log.events())[0].decision_id
    LocalApprovalService(log).resolve(run.id, decision_id, approved=True, by=reviewer)
    service = RunService(store, SessionService(LocalSessionRepository(tmp_path / "sessions")),
                         dispatcher=InlineDispatcher(store, graph_factory),
                         policy_sha256="0" * 64, graph_definition_hash="1" * 64)

    resumed = service.resume(run.id, actor=reviewer)

    assert resumed.status.value == "COMPLETED"
    assert thy.calls == [False, True]
    transition = next(e for e in store.events(run.id)
                      if e.type is EventType.RUN_TRANSITIONED and e.payload.get("command") == "resume")
    assert transition.payload["resume_from"] == "execution"
    assert verify_events(store.events(run.id)).valid
```

(`LocalApprovalService`, `pending_approvals` from `thymira.policies`.)

`tests/thymira/test_core_dispatch.py` — the resume node, through the public `resume`:

```python
class _RecordingGraph:
    """A compiled-graph double that records where the dispatcher resumes it."""

    def __init__(self) -> None:
        self.resumed_as: list[str] = []

    def get_state(self, config: object) -> SimpleNamespace:
        del config
        return SimpleNamespace(values={"run_id": "x"}, next=())

    def update_state(self, config: object, values: object, as_node: str) -> None:
        del config, values
        self.resumed_as.append(as_node)

    def invoke(self, state: object, config: object, durability: str) -> dict[str, object]:
        del state, config, durability
        return {}


@pytest.mark.parametrize(
    ("resume_from", "node"),
    [("information", "interview"), ("approval", "gate"), ("execution", "execution_gate"), (None, "gate")],
)
def test_inline_dispatcher_resumes_at_the_node_the_transition_names(
    tmp_path: Path, resume_from: str | None, node: str
) -> None:
    service, sessions, store = _service(tmp_path, dispatcher=_RecordingDispatcher())
    session = sessions.create(project_id=new_id("project"), client="test")
    run = service.create_run(session.id, "Resume", actor=Actor.system(), workspace=tmp_path)
    controller = RunController(store)
    controller.advance(run.id, RunTransitionKind.START)
    controller.advance(run.id, RunTransitionKind.PAUSE)
    payload = {} if resume_from is None else {"resume_from": resume_from}
    controller.advance(run.id, RunTransitionKind.RESUME, payload=payload)
    graph = _RecordingGraph()

    InlineDispatcher(store, lambda _run: graph).resume(run.id)

    assert graph.resumed_as == [node]
```

(`PAUSE`/`RESUME` legality: check `run_state.py`; if `PAUSE` is not an `_ADVANCE_COMMANDS` member, use `WAIT_FOR_INFORMATION` through the controller's public API or append the resume transition the way `test_core_composition.py:306` does.) Also change `test_inline_dispatcher_resume_wraps_a_failure_like_submit` to `match="inline resume failed: graph rebuild failed"`.

`tests/thymira/test_e2e_runtime_spine.py:266`: `assert failed.payload["error"].startswith("run " + run_id + ": inline execution failed: ")`.

`tests/thymira/test_core_graph_adapters.py`:

```python
def test_thy_subgraph_seeds_a_resumed_pass_from_the_carried_progress(tmp_path: Path) -> None:
    """A carried plan means no plan call and no plan decision; a finished pass clears it."""
    run = _run()
    deps = _subgraph_deps(run, tmp_path, policy=_PLAN_PASSES)
    provider = ScriptedProvider([DataProfileOutput(row_count=1, columns=("a",))])
    thy = ThySubgraph(run, _data_agent_catalog(tmp_path / "catalog"), provider=provider)
    seeded = initial_runtime_state(run).model_copy(
        update={"thy_progress": ThyProgress(plan=_PLAN_OUTPUT.tasks)}
    )

    result = thy.invoke(seeded, deps=deps)

    assert result.thy_progress is None
    assert len(provider.calls) == 1
    assert not any(
        e.type is EventType.POLICY_DECISION and e.payload.get("subject_kind") == "run"
        for e in deps.event_log.events()
    )
```

The `RuntimeState` round-trip module: add one case serializing a state with `thy_progress=ThyProgress(plan=(task,), agent_messages=(Task(..., status=TaskStatus.PENDING),))` through `serialize_runtime_state` / `deserialize_runtime_state` and asserting equality.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/thymira/test_core_composition.py tests/thymira/test_core_dispatch.py tests/thymira/test_core_graph_adapters.py -q --basetemp=C:/Users/lucas/AppData/Local/Temp/thy-bt3 -k "parks_the_run_before_mira or resumes_at_the_node or seeds_a_resumed_pass"`
Expected: FAIL (`RuntimeState` has no `thy_progress`; the graph continues into MIRA).

- [ ] **Step 3: State** (`state.py`)

```python
from thymira.thy.models import ReworkSignal, ThyProgress
...
    rework_refusal: str | None = None
    thy_progress: ThyProgress | None = None
    """THY's own record of a pass that stopped on a tool call awaiting a human, carried under
    THY's name into the next `thy` pass and cleared when a pass finishes."""
```

- [ ] **Step 4: The adapter** (`adapters.py`, `ThySubgraph.invoke`)

```python
        initial_state = None
        if state.rework_signal is not None:
            initial_state = ThyState(... unchanged ...)
        elif state.thy_progress is not None:
            # A resume after a human answered a tool-call review: seed the pass with what the
            # parked pass decided, so Plan short-circuits and Execute re-runs only the task that
            # asked. Inspect re-registers nothing (idempotent) and Summarize runs at the end.
            initial_state = ThyState(
                run=self.run,
                plan=state.thy_progress.plan,
                completed=state.thy_progress.completed,
                agent_messages=state.thy_progress.agent_messages,
                experiments=state.thy_progress.experiments,
                artifacts=state.thy_progress.artifacts,
                execution_constraints=state.execution_constraints,
            )
        output = run_thy(...)
        _record_artifact_changes(deps, before_manifest)
        if output.error is not None:
            raise ThyExecutionError(...)
        update_current(...)
        return state.model_copy(update={"rework_signal": None, "thy_progress": output.progress})
```

Add to the class docstring, after the "no fabricated fields" paragraph: "`ThyOutput.progress` is the one THY fact the state does carry, under THY's own name and type (`RuntimeState.thy_progress`): a pass that stopped on a tool call awaiting a human hands back its plan and outcomes, and the next pass is seeded from them. It is THY's record, not a mapping onto Core vocabulary."

- [ ] **Step 5: The composition** (`compose.py`)

Edge tables:

```python
_GRAPH_EDGES: tuple[tuple[str, str], ...] = (
    (START, "start"),
    ("start", "interview"),
    ("preflight", "classify"),
    ("classify", "execution_gate"),
    ("mira", "gate"),
    ("complete", END),
)
_CONDITIONAL_EDGES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("interview", ("preflight", END)),
    ("execution_gate", ("thy", END)),
    ("thy", ("mira", END)),
    ("gate", ("thy", "complete", END)),
)
```

`build_runtime_graph`: `builder.add_conditional_edges("thy", nodes.route_after_thy)` after the `execution_gate` line. Imports: `from thymira.policies import decision_from_events, pending_approvals`.

```python
    def run_thy(self, state: RuntimeState) -> dict[str, Any]:
        """Invoke THY and expose only the shared runtime state to the next node."""
        with phase("thy"):
            updated = self.thy.invoke(state, deps=_require_deps(self.deps))
        _validate_identity(state, updated, "THY")
        if state.rework_signal is not None:
            updated = updated.model_copy(update={"rework_signal": None})
        if self.controller is not None:
            if self.controller.current_state(updated.run_id).stage is RunStage.PLANNING:
                self.controller.advance(updated.run_id, RunTransitionKind.BEGIN_EXECUTION)
            if updated.thy_progress is not None:
                self._park_for_tool_review(updated.run_id)
        return updated.model_dump(mode="python")

    def route_after_thy(self, state: RuntimeState) -> str:
        """End this graph turn while a tool call THY made waits for a human's answer.

        Read from the persisted Run state, like `route_after_interview`, never from the graph
        state: the controller is the only writer of the wait. MIRA audits the pass that
        finishes, not a knowingly incomplete one it would have to invalidate on the resume.
        """
        if self.controller is None:
            return "mira"
        current = self.controller.current_state(state.run_id).state
        if current.condition is RunCondition.WAITING and current.wait_reason is WaitReason.APPROVAL:
            return END
        return "mira"

    def _park_for_tool_review(self, run_id: str) -> None:
        """Park the Run on the tool-call review THY stopped at, through the single Run writer.

        The decision is rebuilt from its own `policy.decision` event, as
        `RunService._pending_approval` does, and `park_for_review` re-checks that record. Never
        through `MiraControlPlane.handle(REQUEST_APPROVAL)`: under the shipped policy that action
        itself requires an approval first (`GOV-006`), so the Run would never park.
        """
        assert self.controller is not None  # noqa: S101  # guarded by the caller
        current = self.controller.current_state(run_id).state
        if current.condition is RunCondition.WAITING and current.wait_reason is WaitReason.APPROVAL:
            return  # a redelivered node: the park is already persisted
        events = _require_deps(self.deps).event_log.events()
        for pending in pending_approvals(events):
            decision = decision_from_events(events, pending.decision_id)
            if decision.subject_kind == "tool_call":
                self.controller.park_for_review(run_id, decision)
                return
        raise RuntimeError(
            f"run {run_id}: THY reported a task awaiting approval, but no tool-call review "
            "is pending"
        )
```

If `park_for_review` refuses the rebuilt decision (`AuthorizationScopeError: policy decision does not have a matching Gate record`) because `redact_value(decision.to_json_dict())` is not byte-identical to the recorded payload, compare against the recorded event instead: pass the decision rebuilt with `PolicyDecision.model_validate(event.payload)` from the *exact* `policy.decision` event and, if the mismatch is a redaction artefact, report it as a finding — do not weaken `park_for_review`.

- [ ] **Step 6: The dispatcher** (`dispatch.py`)

```python
        except (OSError, RuntimeError, ValueError) as exc:
            raise ExecutionDispatchError(f"run {run_id}: inline execution failed: {exc}") from exc
```

(and `inline resume failed: {exc}`), with a comment: "The cause travels on the message: `RunService.fail` records only `str(exc)`, and a `run.failed` that says why is what the CLI, the API and MIRA read (bug-hunt C2)."

```python
    def _resume_node(self, run_id: Id) -> str:
        """Select the stopped graph node from the recorded resume cause."""
        for event in reversed(self._store.events(run_id)):
            if event.type is not EventType.RUN_TRANSITIONED:
                continue
            if event.payload.get("command") != "resume":
                continue
            resume_from = event.payload.get("resume_from")
            if resume_from == "information":
                return "interview"
            if resume_from == "execution":
                # A tool-call review parked the Run inside `thy`; re-enter through the node
                # before it, so the whole `thy` pass runs again, seeded (never `mira`/`gate`).
                return "execution_gate"
            return "gate"
        return "gate"
```

- [ ] **Step 7: The service** (`runs.py`, `resolve_approval`)

```python
        approved = gate.resolve_pending_approval(decision, note=note)
        if decision.subject_kind == "tool_call":
            # A tool-call review parks the Run mid-execution. Either answer resumes it: an
            # approval lets the Tool Manager run that exact call once (its ticket), a rejection
            # is recorded as a denial the agent adapts to. Neither ends the Run -- only the
            # findings review below can block it.
            self._controller.advance(
                run_id,
                RunTransitionKind.RESUME,
                payload={
                    ("approved_by" if approved else "rejected_by"): actor.to_json_dict(),
                    "approved": approved,
                    "resume_from": "execution",
                },
            )
            if self._dispatcher is not None:
                self._resume_or_fail(self._dispatcher, run_id)
            return self.get_run(run_id)
        if approved:
            ...  # unchanged
```

Update the method docstring.

`RunService.resume` — a Run parked on a tool-call review can be answered out of band (the governance `ApprovalService`, `POST /runs/{id}/approvals/{decision_id}/approve`, records the `human.approval` and never transitions). Today `resume` then advances `RESUME` with no `resume_from`, the dispatcher defaults to `gate`, `route` finds no decision and the Run fails. Derive the marker from the park:

```python
    def _parked_subject_kind(self, run_id: Id) -> str | None:
        """The subject kind of the decision the Run was last parked on, read off the transition."""
        for event in reversed(self._store.events(run_id)):
            if (
                event.type is EventType.RUN_TRANSITIONED
                and event.payload.get("command") == RunTransitionKind.WAIT_FOR_APPROVAL.value
            ):
                decision = event.payload.get("policy_decision")
                kind = decision.get("subject_kind") if isinstance(decision, dict) else None
                return kind if isinstance(kind, str) else None
        return None
```

and in `resume`, the waiting branch:

```python
            payload: dict[str, Any] = {"resumed_by": actor.to_json_dict()}
            if current.wait_reason.value == "approval":
                # Answered out of band: re-enter where the Run was parked. A tool-call review
                # parked it inside `thy`; a findings review parked it at `gate`.
                parked_on_tool_call = self._parked_subject_kind(run_id) == "tool_call"
                payload["resume_from"] = "execution" if parked_on_tool_call else "approval"
            self._controller.advance(run_id, RunTransitionKind.RESUME, payload=payload)
```

(`Any` may need importing under `TYPE_CHECKING` is not enough — it is used in an annotation inside a function body, so import it normally from `typing`.) The `paused` branch keeps its payload. Check `RunTransitionKind.WAIT_FOR_APPROVAL.value == "wait_for_approval"` against `run_state.py` and the transition payload written by `RunController._append_transition` (`park_for_review` passes `payload={"policy_decision": decision.to_json_dict()}`; confirm the `command` key name the transition event carries by reading `_append_transition`).

- [ ] **Step 8: Run the tests**

Run: `uv run pytest tests/thymira/test_core_composition.py tests/thymira/test_core_dispatch.py tests/thymira/test_core_graph_adapters.py tests/thymira/test_core_rework_assurance.py tests/thymira/test_core_control_plane.py tests/thymira/test_e2e_runtime_spine.py tests/thymira/test_api_runs.py -q --basetemp=C:/Users/lucas/AppData/Local/Temp/thy-bt3`
Expected: PASS. `test_composed_graph_definition_hash_matches_the_real_subgraphs` still passes (it compares two computed hashes). If a test pins the composed hash as a literal, update the literal and say so in the report.

- [ ] **Step 9: Gates and commit**

Run: `just lint && just typecheck && just check-imports`, then

```bash
git add runtime/core tests/thymira/test_core_composition.py tests/thymira/test_core_dispatch.py tests/thymira/test_core_graph_adapters.py tests/thymira/test_e2e_runtime_spine.py <round-trip test module>
git commit -m "feat(core): park a Run on a tool-call review before MIRA, resume either answer into a seeded THY pass, and keep the failure cause on run.failed"
```

---

### Task 5: MIRA pairs a tool event with the decision it names (A3, A6)

**Why this task exists (found while executing Task 1):** `a3_authorization_before_tool` pairs every `tool.started` with the latest `policy.decision` recorded for the *same subject* (`controls.py:270-300`), and `a6_denials_recorded` explains a `tool.denied` only through decisions for its subject (`:347-390`). A human-approved retry is a **new** `ToolCall` (new subject) that, by design, mints no new decision: it runs under the decision recorded for the first attempt, which the Tool Manager now stamps on `tool.started` as `decision_id`. Under today's A3 that execution is "without an allowing decision" — a CRITICAL finding, which `GOV-101` turns into a `BLOCK` of a Run a human just approved. The rejection retry is likewise a `tool.denied` A6 cannot explain. The identity the controls need is already on the events; the controls only have to read it.

**Files:**
- Modify: `runtime/mira/src/thymira/mira/checks/controls.py` (`_allowing_with_approvals` lines 224-234; `a3_authorization_before_tool` lines 270-300; `a6_denials_recorded` lines 347-390)
- Test: `tests/thymira/test_mira_checks.py` (model on `_reviewed_tool_run` at `:932` and the two A3 tests at `:961-978`; reuse `_control`)

**Interfaces:**
- Consumes: the `decision_id` payload key on `tool.started` and `tool.denied` (Task 1).
- Produces: A3 pairs a start with the decision its payload names first, then with the latest decision for its subject; A6 explains a denial through the non-allowing decision its payload names first, then through its subject's decisions. Nothing else about either control changes (severity, detail text, evidence).
- Consumed by: Task 6 (the composed proof over real MIRA).

- [ ] **Step 1: Write the failing tests** (`tests/thymira/test_mira_checks.py`, next to the two A3 tests)

```python
def _reviewed_retry_run(*, approved: bool) -> tuple[InMemoryEventLog, PolicyEngine]:
    """A review-gated call, answered by a human, then retried as a *new* call naming the decision.

    This is the shape `ToolManager` writes for a one-shot ticket: the denied first attempt and
    the retry are two `ToolCall`s (two subjects); the retry mints no decision and names the
    answered one as `decision_id` on its own event. `approved` selects whether the retry ran
    (`tool.started`) or was denied as a rejection (`tool.denied`).
    """
    log = InMemoryEventLog(new_id("run"))
    system = Actor.system()
    engine = PolicyEngine(load_policy_stack("credit_risk"))
    gate = Gate(engine, log, approver=None)
    first, retry = new_id("tool"), new_id("tool")
    log.append(EventType.RUN_STARTED, system, {"run_environment": {"python": "3.13"}})
    decision = gate.check_action(
        subject_kind="tool_call", subject_id=first, action_type="run_python"
    )
    assert decision.decision is Decision.REQUIRE_HUMAN_REVIEW
    log.append(
        EventType.TOOL_DENIED,
        system,
        {"tool_call_id": first, "decision_id": decision.id, "reason": "review"},
        subject_id=first,
    )
    log.append(
        EventType.HUMAN_APPROVAL,
        Actor(kind=ActorKind.HUMAN, id="reviewer", authenticated=True),
        {"decision_id": decision.id, "approved": approved, "automatic": False},
        subject_id=first,
    )
    if approved:
        log.append(
            EventType.TOOL_STARTED,
            system,
            {"tool": "run_python", "decision_id": decision.id},
            subject_id=retry,
        )
        log.append(EventType.TOOL_COMPLETED, system, {"exit_code": 0}, subject_id=retry)
    else:
        log.append(
            EventType.TOOL_DENIED,
            system,
            {"tool_call_id": retry, "decision_id": decision.id, "reason": "rejected"},
            subject_id=retry,
        )
    log.append(EventType.RUN_COMPLETED, system, {})
    return log, engine


def test_a3_authorises_a_retry_that_names_the_decision_a_human_approved() -> None:
    log, engine = _reviewed_retry_run(approved=True)

    report = audit_run(AuditContext(log.run_id, log.events(), None, engine.policy_sha256))

    assert _control(report, "A3").status is ControlStatus.PASSED
    assert _control(report, "A6").status is ControlStatus.PASSED


def test_a6_explains_a_retry_denied_under_the_decision_a_human_rejected() -> None:
    log, engine = _reviewed_retry_run(approved=False)

    report = audit_run(AuditContext(log.run_id, log.events(), None, engine.policy_sha256))

    a6 = _control(report, "A6")
    assert a6.status is ControlStatus.PASSED
    assert a6.detail == "2 denials explained"


def test_a3_does_not_authorise_a_start_naming_an_unknown_or_unapproved_decision() -> None:
    log, engine = _reviewed_retry_run(approved=True)
    system = Actor.system()
    stray = new_id("tool")
    log.append(
        EventType.TOOL_STARTED,
        system,
        {"tool": "run_python", "decision_id": new_id("decision")},
        subject_id=stray,
    )
    log.append(EventType.TOOL_COMPLETED, system, {"exit_code": 0}, subject_id=stray)

    report = audit_run(AuditContext(log.run_id, log.events(), None, engine.policy_sha256))

    a3 = _control(report, "A3")
    assert a3.status is ControlStatus.FAILED
    assert "without an allowing decision" in a3.detail
```

(`RUN_COMPLETED` after a later `TOOL_STARTED` is fine for these controls; if another control's ordering assertion complains, append the stray pair before `RUN_COMPLETED` by building the log inline instead of extending the helper's output.)

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/thymira/test_mira_checks.py -q --basetemp=C:/Users/lucas/AppData/Local/Temp/thy-bt3 -k "retry"`
Expected: FAIL — A3 FAILED ("without an allowing decision") on the approved retry, A6 FAILED on the rejected one.

- [ ] **Step 3: The controls** (`controls.py`)

```python
def _named_decision(event: Event, decisions_by_id: dict[str, Event]) -> Event | None:
    """The decision a tool event names in its payload, when its writer stamped one.

    `ToolManager` writes `decision_id` on `tool.started` and on a Gate-branch `tool.denied`. For
    a human-approved retry it names the decision the human answered -- recorded for the *first*
    attempt's subject -- so pairing by subject alone would call the approved execution
    unauthorised. Writers that predate the key leave the subject pairing to do the work.
    """
    decision_id = event.payload.get("decision_id")
    if isinstance(decision_id, str) and decision_id:
        return decisions_by_id.get(decision_id)
    return None
```

`a3_authorization_before_tool`: keep the structure; collect `decisions_by_id: dict[str, Event]` in place of `known_decisions` (same membership test: `decision_id in decisions_by_id`), and pair each start as

```python
        elif event.type is EventType.TOOL_STARTED:
            decision = _named_decision(event, decisions_by_id) or latest_decisions.get(
                event.subject_id
            )
            if decision is None or not _allowing_with_approvals(
                decision.payload, approved_decisions
            ):
                unauthorised.append(event.seq)
```

Docstring: "Every tool execution was preceded by an allowing policy decision -- the one its event names, or the latest one for its subject."

`a6_denials_recorded`: keep the structure; add `non_allowing_decisions: set[str]` — every non-PASS/WARNING decision id goes in when recorded, and an id is discarded when a `human.approval` with `approved is True` names it (exactly where the subject bookkeeping pops it today) — and explain a denial by any of the three:

```python
        elif event.type is EventType.TOOL_DENIED and not (
            event.subject_id in permanently_denied_subjects
            or event.subject_id in pending_subjects
            or _names_a_non_allowing_decision(event, non_allowing_decisions)
        ):
            unexplained.append(event.seq)
```

with

```python
def _names_a_non_allowing_decision(event: Event, decision_ids: set[str]) -> bool:
    """Whether a denial's own payload names a decision that does not allow execution."""
    decision_id = event.payload.get("decision_id")
    return isinstance(decision_id, str) and decision_id in decision_ids
```

Docstring: "Every denied tool call points at a non-allowing decision: the one it names, or one for its subject." A rejected review (`approved is False`) stays non-allowing, so the rejection retry's `tool.denied` naming it is explained; an approved one is discarded from the set, so a denial naming an approved decision stays unexplained, exactly as the subject bookkeeping already treats it.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/thymira/test_mira_checks.py tests/thymira/test_mira_flow.py -q --basetemp=C:/Users/lucas/AppData/Local/Temp/thy-bt3`
Expected: PASS, including `test_a3_and_a6_scan_the_event_log_once_per_control` (`:406`) — the new pairing adds no second scan — and the two existing A3 precedence tests.

- [ ] **Step 5: Gates and commit**

Run: `just lint && just typecheck && just check-imports`, then

```bash
git add runtime/mira/src/thymira/mira/checks/controls.py tests/thymira/test_mira_checks.py
git commit -m "fix(mira): A3 and A6 pair a tool event with the decision it names, so a human-approved retry is authorised evidence and a rejected one is explained"
```

---

### Task 6: The composed proof — a human answers, the exact call runs once

**Files:**
- Create: `tests/thymira/test_e2e_tool_approval.py`
- Modify (only if the test forces it): the modules of Tasks 1-5

**Interfaces:**
- Consumes: everything above, unmodified. This task adds no production code unless the test finds a defect; a defect is fixed in the module that owns it, with its own unit test, and named in the report.

- [ ] **Step 1: Write the test**

```python
"""The real gate, end to end through the Core composition (harness basics 3).

A real ThyGraph delegates to a real data agent whose one tool the Policy Engine escalates to a
human (a local, effect-free tool under the base policy, with an unclassified risk profile: the
fail-safe path). The step ends cleanly, THY parks, the Core ends the turn before MIRA; a human
answers through `RunService.resolve_approval`; the resumed pass re-plans nothing, re-runs the one
task that asked, and the Tool Manager runs that exact call once under the recorded approval --
or denies it as a rejection the agent adapts to. Everything is asserted from the hash-chained
log, never from internal state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import BaseModel, Field

from tests.thymira.fixtures_agent_output import DataProfileOutput
from thymira.agents import AgentCatalog, AgentEndReason, LLMToolCall, load_agent_specs
from thymira.agents.llm.scripted import ScriptedProvider
from thymira.core import (
    InlineDispatcher, RunController, RunService, RuntimeState, SessionService,
    StateCheckpointer, SubgraphDeps, ThySubgraph, UsageLedger, build_runtime_graph,
    initial_runtime_state,
)
from thymira.core.control_plane import RunEventLog
from thymira.events import verify_events
from thymira.policies import ActionRule, Gate, Policy, PolicyEngine, ToolCapability, load_policy_stack
from thymira.schemas import Actor, Decision, EventType, Run, RunCondition, WaitReason, new_id
from thymira.state import (
    LocalArtifactStore, LocalCheckpointRepository, LocalRecordRepository, LocalRunStore,
    LocalSessionRepository,
)
from thymira.thy.models import AgentTask, PlanOutput, ThyAgentKind, ThyPhase
from thymira.tools import Tool, ToolExecutionError, ToolInvocation, ToolManager, ToolRegistry, ToolResult

if TYPE_CHECKING:
    from pathlib import Path

    from langgraph.graph.state import CompiledStateGraph

    from thymira.schemas import Event

_PLAN_PASSES = Policy(
    name="plan-passes", version="1.0",
    action_rules=(ActionRule(id="TEST-PLAN", action_types=("plan.proposed",),
                             decision=Decision.PASS, reason="test policy: the plan needs no review"),),
)
_PLAN = PlanOutput(tasks=(AgentTask(id="t1", agent=ThyAgentKind.DATA, phase=ThyPhase.EXECUTE,
                                    instruction="profile it"),))
_ECHO = LLMToolCall(id="c1", name="echo", arguments={"value": "x", "description": "look at the value"})
_FINAL = LLMToolCall(id="c2", name="final_result", arguments={"row_count": 1, "columns": ["value"]})


class _EchoArguments(BaseModel):
    value: str = Field(min_length=1)
    description: str = Field(min_length=1)


@dataclass
class _EchoTool(Tool):
    name: str = "echo"
    capability: ToolCapability = field(default_factory=lambda: ToolCapability(id="echo", external_effects=()))
    description: str = "Echo a value."
    arguments_model: type[BaseModel] | None = _EchoArguments
    seen: list[ToolInvocation] = field(default_factory=list)

    def execute(self, invocation: ToolInvocation, arguments: dict[str, Any]) -> ToolResult:
        self.seen.append(invocation)
        return ToolResult(success=True, stdout="ok")


class _MiraDouble:
    name = "scripted-mira"

    def graph_version(self) -> str:
        return "mira-v1"

    def invoke(self, state: RuntimeState, *, deps: SubgraphDeps) -> RuntimeState:
        deps.event_log.append(EventType.AUDIT_COMPLETED, Actor.system(), {"status": "completed"},
                              subject_id=state.run_id, producer="test.mira")
        return state


def _catalog(directory: Path) -> AgentCatalog:
    directory.mkdir(parents=True)
    (directory / "data.yaml").write_text(
        "name: data\nrole: agent\ntask_kinds: [analyze]\ntool_allowlist: [echo]\nmax_turns: 2\n"
        "max_depth: 1\nsystem_prompt_ref: prompts/data.md\n"
        "output_schema_ref: tests.thymira.fixtures_agent_output:DataProfileOutput\n",
        encoding="utf-8",
    )
    (directory / "prompts").mkdir()
    (directory / "prompts" / "data.md").write_text("You are the data agent.", encoding="utf-8")
    return load_agent_specs(directory, known_capabilities=frozenset({"echo"}))


@dataclass(frozen=True, slots=True)
class _Composed:
    run: Run
    store: LocalRunStore
    service: RunService
    engine: PolicyEngine
    echo: _EchoTool
    provider: ScriptedProvider


def _compose(tmp_path: Path, items: list[Any]) -> _Composed:
    run = Run(id=new_id("run"), project_id=new_id("project"), session_id=new_id("session"), prompt="profile")
    store = LocalRunStore(tmp_path / "runs")
    store.create(run, actor=Actor.system())
    engine = PolicyEngine(load_policy_stack().merged_with(_PLAN_PASSES))
    echo = _EchoTool()
    provider = ScriptedProvider(items)
    catalog = _catalog(tmp_path / "catalog")
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    def graph_factory(_run: Run) -> CompiledStateGraph:
        log = RunEventLog(store, run.id)
        deps = SubgraphDeps(
            event_log=log, gate=Gate(engine, log),
            artifact_store=LocalArtifactStore(tmp_path / "artifacts", run.id),
            tool_manager=ToolManager(ToolRegistry((echo,))),
            record_repository=LocalRecordRepository(tmp_path / "records"),
            usage_ledger=UsageLedger(),
        )
        thy = ThySubgraph(run, catalog, provider=provider, project_dir=workspace,
                          tool_registry=ToolRegistry((echo,)))
        return build_runtime_graph(
            thy, _MiraDouble(),
            checkpointer=StateCheckpointer(LocalCheckpointRepository(tmp_path / "checkpoints")),
            deps=deps, controller=RunController(store),
        )

    service = RunService(store, SessionService(LocalSessionRepository(tmp_path / "sessions")),
                         dispatcher=InlineDispatcher(store, graph_factory),
                         policy_sha256="0" * 64, graph_definition_hash="1" * 64)
    graph_factory(run).invoke(initial_runtime_state(run), {"configurable": {"thread_id": run.id}})
    return _Composed(run, store, service, engine, echo, provider)


def _of(events: list[Event], kind: EventType) -> list[Event]:
    return [event for event in events if event.type is kind]


def _answer(composed: _Composed, *, approved: bool) -> Run:
    reviewer = Actor(kind="human", id="reviewer", authenticated=True)
    gate = Gate(composed.engine, RunEventLog(composed.store, composed.run.id),
                approver=lambda _request: approved, human=reviewer)
    return composed.service.resolve_approval(composed.run.id, gate=gate, actor=reviewer,
                                             note="reviewed")


def test_the_first_pass_parks_on_the_review_before_mira(tmp_path: Path) -> None:
    composed = _compose(tmp_path, [_PLAN, _ECHO, _ECHO, _FINAL])

    state = RunController(composed.store).current_state(composed.run.id).state
    events = composed.store.events(composed.run.id)

    assert state.condition is RunCondition.WAITING
    assert state.wait_reason is WaitReason.APPROVAL
    assert composed.echo.seen == []
    assert len(composed.provider.calls) == 2  # the plan and the one agent request
    requested = _of(events, EventType.HUMAN_APPROVAL_REQUESTED)
    assert len(requested) == 1
    assert requested[0].payload["tool"] == "echo"
    assert requested[0].payload["arguments"] == _ECHO.arguments
    assert len(requested[0].payload["tool_intent_sha256"]) == 64
    completed = _of(events, EventType.AGENT_COMPLETED)
    assert [e.payload["end_reason"] for e in completed] == [AgentEndReason.AWAITING_APPROVAL.value]
    assert _of(events, EventType.AUDIT_COMPLETED) == []
    assert verify_events(events).valid


def test_an_approval_re_runs_only_the_task_that_asked_and_the_call_runs_once(tmp_path: Path) -> None:
    composed = _compose(tmp_path, [_PLAN, _ECHO, _ECHO, _FINAL])

    final = _answer(composed, approved=True)
    events = composed.store.events(composed.run.id)

    assert final.status.value == "COMPLETED"
    assert len(composed.echo.seen) == 1
    requested = _of(events, EventType.HUMAN_APPROVAL_REQUESTED)[0]
    started = _of(events, EventType.TOOL_STARTED)
    denied = _of(events, EventType.TOOL_DENIED)
    assert len(started) == 1 and len(denied) == 1
    assert started[0].payload["tool_intent_sha256"] == requested.payload["tool_intent_sha256"]
    assert started[0].payload["decision_id"] == requested.payload["decision_id"]
    approvals = _of(events, EventType.HUMAN_APPROVAL)
    assert len(approvals) == 1 and approvals[0].payload["approved"] is True
    # The plan was proposed and gated once: the resumed pass replanned nothing.
    assert sum(1 for e in _of(events, EventType.MODEL_SELECTED) if e.payload["task"] == "plan") == 1
    assert sum(1 for e in _of(events, EventType.POLICY_DECISION)
               if e.payload["subject_kind"] == "tool_call") == 1
    assert [e.payload["end_reason"] for e in _of(events, EventType.AGENT_COMPLETED)] == [
        AgentEndReason.AWAITING_APPROVAL.value, AgentEndReason.COMPLETED.value]
    assert len(_of(events, EventType.AUDIT_COMPLETED)) == 1
    assert _of(events, EventType.RUN_FAILED) == []
    assert verify_events(events).valid


def test_a_rejection_resumes_and_the_agent_is_told_without_asking_anyone_again(tmp_path: Path) -> None:
    composed = _compose(tmp_path, [_PLAN, _ECHO, _ECHO, _FINAL])

    final = _answer(composed, approved=False)
    events = composed.store.events(composed.run.id)

    assert final.status.value == "COMPLETED"
    assert composed.echo.seen == []
    denied = _of(events, EventType.TOOL_DENIED)
    assert len(denied) == 2
    assert denied[1].payload["reason"] == "tool call rejected by a human: reviewed"
    assert denied[1].payload["decision_id"] == denied[0].payload["decision_id"]
    assert _of(events, EventType.TOOL_STARTED) == []
    assert len(_of(events, EventType.HUMAN_APPROVAL_REQUESTED)) == 1
    assert sum(1 for e in _of(events, EventType.POLICY_DECISION)
               if e.payload["subject_kind"] == "tool_call") == 1
    assert verify_events(events).valid
```

Notes for the implementer: `Tool` may be a `Protocol` rather than a base class — build `_EchoTool` the way `tests/thymira/test_tools.py::_FakeTool` does. `RunController.park_for_review` compares the rebuilt decision's redacted JSON with the recorded event; if it refuses, that is a real defect of Task 4 — fix it there (see Task 4 Step 5's note), not by loosening the assertion. `ThySubgraph` gives the tool context `risk_profile=state.risk_profile or RiskProfile()` — the default profile's `confidence` is `0.0`, so the base policy's fail-safe escalation is what makes `echo` review-gated; do not add a capability rule.

The fourth test runs the same two answers through the **production factory with real MIRA** — `build_runtime_graph_factory` exactly as `tests/thymira/test_core_graph_adapters.py::test_production_factory_drives_a_run_to_a_completed_terminal_state` (`:818`) builds it, with `specs=()` (no audit agents: the deterministic controls are the point), `tool_registry=ToolRegistry((echo,))`, `project_dir=workspace`, the same engine and provider as `_compose`, and `deps.run_service.create_run(...)` to start the Run. Answer through `deps.run_service.resolve_approval(run.id, gate=deps.gate_factory(run.id, approver=lambda _r: approved, human=reviewer), actor=reviewer, note="reviewed")` — the gate factory takes those keyword arguments (see `apps/api/src/thymira/api/routes/runs.py::_resolve_human_decision`). Assert, for `approved in (True, False)`: the Run is `COMPLETED`; exactly one `audit.completed`; in its `audit_report["controls"]` the entries with `control_id` `A3` and `A6` have `status == "PASSED"` (the report dumps `ControlStatus` values; read the shape off the event rather than guessing); no `run.failed`; `verify_events(...).valid`. If `resolve_approval` raises `RunAuditStaleError` because `AuditFreshnessService.assess` treats the parked Run's preflight as an audit snapshot, that is a real defect: report it and fix it in `runtime/core/src/thymira/core/audit_freshness.py` (a Run parked before any `audit.completed` has no snapshot to be stale) with a unit test.

- [ ] **Step 2: Run it**

Run: `uv run pytest tests/thymira/test_e2e_tool_approval.py -q --basetemp=C:/Users/lucas/AppData/Local/Temp/thy-bt3 -x`
Expected: PASS after Tasks 1-5. If it fails, apply `python-debug` (reproduce, locate the owning module, fix there with a unit test), and record what it found in the report.

- [ ] **Step 3: Whole fast lane**

Run: `uv run pytest -m "not slow" -q -x --basetemp=C:/Users/lucas/AppData/Local/Temp/thy-bt3 -p no:cacheprovider`
Expected: PASS; the count grows by the tests Tasks 1-6 added. Then `just check`.

- [ ] **Step 4: Commit**

```bash
git add tests/thymira/test_e2e_tool_approval.py <any fixed modules and their tests>
git commit -m "test(core,thy,tools): the real gate end to end — a human's answer resumes the Run and the exact call runs once"
```

---

### Task 7: A synchronous human approver's yes authorizes the same call (bug-hunt Foco 3, C5)

**Why this task exists (found while executing Task 3):** `docs/bug-hunt/2026-09-04-foco-2-3-task-failure-and-approval.md` closed Foco 2 (PR #108) and left C5 open: "a synchronous approver that says yes still never reaches `allows_execution` at the tool gate". Task 1 built the ticket for the asynchronous case; the synchronous one is the same fold, one call later. When a Gate has an in-process **human** approver (a console or a test wired with `human=Actor(kind=HUMAN, …)` and a non-automatic approver), `Gate._record` consults it inside `check_capability` and writes the `human.approval` — as evidence, never as authority. The manager only has to re-read the log after the decision comes back `REQUIRE_HUMAN_REVIEW`: a human's yes is the ticket for this very call, verified through `allows_execution(decision, approval)` exactly like a retry; a human's no is the rejection, final for this Run; an automatic answer stays what it is today, a denial (design decision 3).

**Files:**
- Modify: `runtime/tools/src/thymira/tools/manager.py` (`execute`, the block between `check_capability` and the `allows_execution` check that Task 1 wrote)
- Test: `tests/thymira/test_tools.py`

**Interfaces:**
- Consumes: `_human_answer`, `_record_review_denial`, `tool_intent_sha256` (Task 1), unchanged.
- Produces: with a synchronous human approver, a review-gated call runs in the same `execute` call under one decision and one `human.approval` (`tool.started.decision_id` is that decision); a synchronous human rejection is recorded as `tool call rejected by a human` with `pending_approval is None`; `test_a_synchronous_approval_still_never_runs_a_review_gated_tool` (an *automatic* approver) keeps passing unchanged.

- [ ] **Step 1: Write the failing tests** (`tests/thymira/test_tools.py`)

```python
def _human_approver(*, approved: bool) -> tuple[Approver, Actor]:
    """A synchronous approver standing for a real human at a console, and who they are."""
    reviewer = Actor(kind=ActorKind.HUMAN, id="reviewer", authenticated=True)
    return (lambda _request: approved), reviewer


def test_a_synchronous_human_yes_authorizes_the_same_call_once(tmp_path: Path) -> None:
    capability = ToolCapability(id="echo", external_effects=())
    tool = _FakeTool("echo", capability, arguments_model=_EchoArguments)
    approver, reviewer = _human_approver(approved=True)
    base = _context(tmp_path, capability=capability, approver=approver, risk_confidence=0.1)
    context = replace(base, gate=Gate(base.gate.engine, base.event_log, approver=approver, human=reviewer))
    manager = ToolManager(ToolRegistry((tool,)))

    execution = manager.execute(context, "echo", {"value": "x"})
    again = manager.execute(context, "echo", {"value": "x"})

    assert execution.call.status is ToolCallStatus.COMPLETED
    assert len(tool.seen_invocation) == 1
    events = context.event_log.events()
    decisions = [e for e in events if e.type is EventType.POLICY_DECISION]
    approvals = [e for e in events if e.type is EventType.HUMAN_APPROVAL]
    started = next(e for e in events if e.type is EventType.TOOL_STARTED)
    assert len(decisions) == 1 and len(approvals) == 1
    assert approvals[0].payload["automatic"] is False
    assert started.payload["decision_id"] == decisions[0].payload["id"]
    assert execution.call.policy_decision_id == decisions[0].payload["id"]
    # The second identical call is a new decision, answered yes again by the same human: it runs
    # too -- each execution has its own decision and its own answer on the log.
    assert again.call.status is ToolCallStatus.COMPLETED
    assert len([e for e in context.event_log.events() if e.type is EventType.POLICY_DECISION]) == 2


def test_a_synchronous_human_no_is_a_final_rejection(tmp_path: Path) -> None:
    capability = ToolCapability(id="echo", external_effects=())
    tool = _FakeTool("echo", capability, arguments_model=_EchoArguments)
    approver, reviewer = _human_approver(approved=False)
    base = _context(tmp_path, capability=capability, approver=approver, risk_confidence=0.1)
    context = replace(base, gate=Gate(base.gate.engine, base.event_log, approver=approver, human=reviewer))
    manager = ToolManager(ToolRegistry((tool,)))

    execution = manager.execute(context, "echo", {"value": "x"})
    again = manager.execute(context, "echo", {"value": "x"})

    assert execution.call.status is ToolCallStatus.DENIED
    assert execution.pending_approval is None
    assert execution.result.error == "tool call rejected by a human"
    assert again.call.status is ToolCallStatus.DENIED
    assert again.result.error == "tool call rejected by a human"
    assert len([e for e in context.event_log.events() if e.type is EventType.POLICY_DECISION]) == 1
    assert tool.seen_invocation == []
```

(`Approver` from `thymira.policies`; `_context` builds its Gate with `approver=` and no `human`, hence the `replace` with a Gate that names the human.)

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/thymira/test_tools.py -q --basetemp=C:/Users/lucas/AppData/Local/Temp/thy-bt3 -k "synchronous_human"`
Expected: FAIL — the first call is DENIED (the answer never reaches `allows_execution`), the rejection is a plain `tool call denied: …`.

- [ ] **Step 3: Re-read the log after the Gate answers** (`manager.py`, inside `execute`, right after the `decision = (answer.decision if answer is not None else context.gate.check_capability(...))` expression and before `call = call.model_copy(update={"policy_decision_id": decision.id})`)

```python
        if answer is None and decision.requires_human_approval:
            # A synchronous *human* approver may have answered inside `check_capability`; the
            # Gate recorded that answer as evidence, never as authority. Re-read the log: a
            # human's yes is the ticket for this very call and goes through the same
            # `allows_execution` check as a retry; a human's no is final; an automatic answer
            # is no answer (design decision 3) and the call stays denied.
            answer = _human_answer(context, ticket)
            if answer is not None and answer.approval is None:
                rejected = call.model_copy(update={"policy_decision_id": decision.id})
                reason = "tool call rejected by a human" + (
                    f": {answer.note}" if answer.note else ""
                )
                return self._record_review_denial(
                    context, rejected, tool_name, validated_arguments, ticket, reason
                )
            approval = answer.approval if answer is not None else None
```

Nothing else changes: `allows_execution(decision, approval)` below now sees the human's approval and the `tool.started` carries `decision_id` and the ticket, so the answer is spent by this execution exactly as a retry's would be. Extend `execute`'s docstring with one sentence on the synchronous human approver. If the duplicated "rejected by a human" reason string bothers ruff or the reviewer, lift it into `_rejection_reason(answer)` used by both sites.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/thymira/test_tools.py tests/thymira/test_tools_manager.py tests/thymira/test_agents_tool_bridge.py tests/thymira/test_thy_e2e.py -q --basetemp=C:/Users/lucas/AppData/Local/Temp/thy-bt3`
Expected: PASS, including `test_a_synchronous_approval_still_never_runs_a_review_gated_tool` and `test_a_review_the_approver_declined_denies_the_call_and_never_runs_the_tool` unchanged (both use automatic approvers).

- [ ] **Step 5: Gates and commit**

Run: `just lint && just typecheck && just check-imports`, then

```bash
git add runtime/tools/src/thymira/tools/manager.py tests/thymira/test_tools.py
git commit -m "feat(tools): a synchronous human approver's yes authorizes the same call, through the same ticket and the same allows_execution check (bug-hunt C5)"
```

---

### Task 8: What the human sees, the anonymous approval, and the record

**Additions found during execution** (ledger rulings R2, R6, R8-R14; the seams review of 2026-09-06):

- CHANGELOG: one consolidated `[Unreleased]` entry for the whole pull request replaces the partial
  entries Tasks 1-5 left: the headline mechanism (a review-gated tool call ends the step as
  PENDING, THY hands the Core its progress, the Run parks before MIRA, a human's answer resumes it
  and the exact call runs once under that answer), the one-shot ticket, MIRA's A3/A6 recomputing
  the ticket, the synchronous human approver, the dispatcher cause on `run.failed`, the shared
  refusal vocabulary. Known limitations: a step that defers several tool calls at once is answered
  one review per call and the earlier requests stay unanswered until re-asked (A7 reports them at
  close); a Run parked on a tool review whose answer arrives out of band while a sibling fan-out
  review is still unanswered keeps answering 409 to `resume` until that sibling is answered
  through the governance route; the composed proof's production-factory scenario audits FAILED on
  A15 (no risk interview: the unclassified profile that escalates the call is also what A15
  reports); `usage.charge_tool` charges the deferred attempt and again on the resumed pass.
- ADR-0013 Consequences: correct the claim that this pull request shipped the same-call retry
  escalation with a `justification`; what shipped is the one-shot ticket and the park/resume
  (design decision 2 of this plan). Record the checkpointer defect the proof found (a nested
  ThyGraph borrowed the composition's checkpointer) and the two follow-ups it leaves:
  `StateCheckpointer` keys on `thread_id` alone (`checkpoint_ns` ignored, latent) and the
  dispatcher boundary catching every exception (final fix wave, R14).
- CLI: `thymira approve` / `thymira reject` show the decision the Run is parked on (the
  `policy_decision` of the latest `wait_for_approval` transition), falling back to the latest
  pending request, with the tool name and arguments of a tool-call review.
- AGENTS.md repository map and member READMEs: say what the branch wired (the CLI's
  `approve`/`reject` are no longer "next" if they exist), nothing more.

**Files:**
- Modify: `apps/api/src/thymira/api/routes/governance.py` (`_decision_actor` lines 57-67)
- Modify: `adapters/cli/src/thymira/cli/client.py` (`PendingApprovalView` lines 169-176; `_parse_pending_approval` lines 874-891)
- Modify: `adapters/cli/src/thymira/cli/render.py` (`render_pending_approval` lines 39-48)
- Modify: `CHANGELOG.md` (`[Unreleased]`), `docs/adr/0013-harness-fundamentals-six-decisions.md` (Consequences), `AGENTS.md` (the `runtime/tools`, `runtime/core` and invariants sentences)
- Test: `tests/thymira/test_api_governance.py`, `tests/thymira/test_cli_approval.py`, `tests/thymira/test_api_contract.py` (if it pins the `PendingApproval` schema)

**Interfaces:**
- Consumes: the `tool`/`arguments` keys on `human.approval_requested` (Task 1).
- Produces: `PendingApprovalView.tool_call: dict[str, object] | None`; `render_pending_approval` prints `Tool` and `Arguments` lines for a tool-call review; `POST /runs/{id}/approvals/{decision_id}/approve|reject` with no principal, no header and no body actor → `401 actor_required`.

- [ ] **Step 1: Write the failing tests**

`tests/thymira/test_api_governance.py` (model on `test_governance_decision_without_credentials_records_a_declared_actor`, `:321`):

```python
def test_governance_decision_with_no_identity_at_all_is_refused(tmp_path: Path) -> None:
    """Bug-hunt residual: the run-lifecycle route refuses an anonymous approval; so must this one."""
    client = _client(tmp_path)
    run_id, decision_id = _park_for_approval(client)  # whatever the helper returns today

    response = client.post(f"/runs/{run_id}/approvals/{decision_id}/approve", json={})

    assert response.status_code == 401
    assert response.json()["type"].endswith("actor_required")
    assert not any(
        event.type is EventType.HUMAN_APPROVAL for event in _events(client, run_id)
    )
```

(Use the module's existing helpers for the park and for reading events; match the `problem()` body shape the runs route's 401 test asserts.)

`tests/thymira/test_cli_approval.py`: one test that seeds a `human.approval_requested` payload with `tool`, `arguments` and `tool_intent_sha256` through whatever transport double the module uses, runs `thymira approve <run> --actor reviewer`, and asserts the output contains `Tool` / `echo` and `Arguments` / `value: x` lines; and one `render_pending_approval` unit test with `tool_call={"tool": "echo", "arguments": {"value": "x"}}`.

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/thymira/test_api_governance.py tests/thymira/test_cli_approval.py -q --basetemp=C:/Users/lucas/AppData/Local/Temp/thy-bt3 -k "no_identity or tool_call or Tool"`
Expected: FAIL (200 instead of 401; no `Tool` line).

- [ ] **Step 3: The governance route**

```python
def _decision_actor(request: Request, body_actor: str | None) -> Actor:
    """Resolve who answered: a named identity wins over the legacy body actor.

    ... [keep the paragraph] ... A fully anonymous call is refused, exactly as the run-lifecycle
    route refuses it (bug-hunt C6): recording ``Actor.system()`` for a human's decision writes
    an unreviewable, indistinguishable-from-automated approval onto the append-only log.
    """
    if body_actor is not None and not has_trusted_actor(request):
        return _declared_human_actor(body_actor)
    if not has_trusted_actor(request):
        raise problem(
            401,
            "actor_required",
            "A human decision must name who is acting: an authenticated principal, the "
            "X-Thymira-Actor header, or the request body's 'actor' field.",
        )
    return resolve_actor(request)
```

(`problem` is what `routes/runs.py` uses; import it the same way.)

- [ ] **Step 4: The CLI view**

`PendingApprovalView` gains `tool_call: dict[str, object] | None = None`; `_parse_pending_approval` sets it from `payload["tool"]`/`payload["arguments"]` when `tool` is a string (`{"tool": ..., "arguments": ...}` — the digest is not for humans); `render_pending_approval`:

```python
    fields: list[tuple[str, object]] = [("Summary", pending.summary or "-")]
    if pending.tool_call is not None:
        fields.append(("Tool", pending.tool_call.get("tool", "-")))
        arguments = pending.tool_call.get("arguments")
        if isinstance(arguments, dict) and arguments:
            fields.append(("Arguments", _render_mapping(arguments)))
```

- [ ] **Step 5: The record**

`CHANGELOG.md` `[Unreleased]`, under `### Added`, one entry (after the datasets one):

- **A human's answer to a tool call is a one-shot ticket for exactly that call, and the Run waits for it.** When the Policy Engine escalates a tool call to `REQUIRE_HUMAN_REVIEW`, the agent's step ends cleanly (`agent.completed` with `status: PENDING`, `end_reason: awaiting_approval`; the started/completed pair MIRA's A9 reads stays paired), THY stops before Summarize, and the Run parks before MIRA. `thymira approve` / `reject` (or the API) resumes it into THY: the seeded pass re-plans nothing and re-runs only the task that asked; the Tool Manager recognises the call by `tool_intent_sha256` — the tool plus its validated, redacted arguments minus the model's `description` — carried on `human.approval_requested`, `tool.denied` and `tool.started`, reconstructs the human's `Approval` from their own event and re-verifies it through `allows_execution`; the answer is spent by the one `tool.started` it authorizes. A rejection is final for that call in that Run and is a denial the agent adapts to; an automatic (test approver) answer authorizes nothing. `human.approval_requested` names the tool and its arguments, and `thymira approve` shows them.
- Under `### Changed`: every denied tool call renders with a `[denied]` last line; `agent.completed` carries `end_reason` (`completed` | `max_turns` | `awaiting_approval`); the composed graph's `("thy", "mira")` edge is conditional, so its definition hash changed and a Run mid-flight across this deploy will have its audit snapshot flagged stale (ADR-0013 decision 6); an escalated call counts twice against `max_tool_calls`.
- Under `### Fixed`: `run.failed` carries the cause of an inline execution or resume failure, not only "inline execution failed" (bug-hunt C2); the governance approvals route refuses an anonymous approval with 401 instead of recording it as `system` (bug-hunt residual of C6).

`docs/adr/0013-…md` Consequences: replace "the denial-as-fact result and the same-call escalation" with what shipped — "the denial-as-fact result (`[denied]`) and the human's one-shot ticket matched by intent; the same-call escalation with a `justification` was not adopted: the step ends on the review and the mandatory `description` is what the human reads (third pull request, 2026-09-05, plan `docs/superpowers/plans/2026-09-05-harness-basics-3.md`)".

`AGENTS.md`: in the repository map, `runtime/tools` gains "…the policy-gated, fail-closed `ToolManager` (with the one-shot approval ticket, `tool_intent_sha256`)"; `runtime/core` gains "`route_after_thy` parks a Run on a tool-call review before MIRA"; in *Architecture invariants*, "LLM proposes, code authorizes" gains one clause: "a human's approval of a tool call authorizes exactly that call, once, through `allows_execution` — never a retry the model rephrased".

- [ ] **Step 6: Run everything**

Run: `just check` and `uv run pytest -m "not slow" -q --basetemp=C:/Users/lucas/AppData/Local/Temp/thy-bt3 -p no:cacheprovider`, then `uv run pytest -m slow -q --basetemp=C:/Users/lucas/AppData/Local/Temp/thy-bt3`.
Expected: all green; sidecars under `tests/thymira/acceptance/` unchanged (`git status --short tests/thymira/acceptance` prints nothing).

- [ ] **Step 7: Commit**

```bash
git add apps/api adapters/cli tests/thymira/test_api_governance.py tests/thymira/test_cli_approval.py tests/thymira/test_api_contract.py CHANGELOG.md docs/adr/0013-harness-fundamentals-six-decisions.md AGENTS.md
git commit -m "feat(api,cli,docs): the human sees the tool call they answer, an anonymous governance approval is refused, and the record of harness basics 3"
```

---

## Out of scope (next pull request)

- Moving `test_acceptance_demo_run.py` onto the Core composition (decision 9): implemented in
  `2026-09-06-harness-basics-4.md`, with the actual audit findings and final BLOCK retained.
- `audit_model._drift` null handling (unaudited; found during PR 2).
- The `never` class as a shared `ExecutionConstraints` method rather than the manager's `_constraint_error` (PR 2 brief §10.5): `_constraint_error` already runs before any answerer, which is the property that matters.
- A `sandbox_permissions` request on a tool call (decision 2 of ADR-0013; a separate mechanism).
- Collapsing the six denial branches into one `_deny` (PR 2 brief §10.2); this plan adds a seventh recorder deliberately narrow.
