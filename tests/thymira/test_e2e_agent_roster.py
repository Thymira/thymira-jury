"""Production regression: Plan repairs an unresolvable agent kind before dispatch (THY-32).

The test drives the exact shape of the mismatch that killed
``run_6f0f0976e13e47faa17266f721ecb255`` through the real production composition
(``build_default_deps`` + ``build_runtime_graph_factory``, the same composition root
``thymira.api.server`` builds), on a deliberately narrow catalog that does not carry
``data-quality-agent``. The catalog-scoped output schema rejects that first response, gives the
planner one bounded correction, and only the corrected plan reaches the Gate and Execute.

The independent oracle is MIRA recomputing over a persisted ``events.jsonl`` a freshly constructed
reader opens for itself -- never the return value of ``create_run`` or ``RunService.get_run`` --
exactly the shape ``test_e2e_tool_approval.py``'s F5.1 proof
(``test_cancelling_a_parked_run_closes_the_review_and_the_call_never_runs``) already uses.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.thymira.api_support import TEST_CREDENTIAL
from thymira.agents import LLMToolCall, ScriptedProvider
from thymira.api import build_default_deps
from thymira.core import build_runtime_graph_factory
from thymira.events import JsonlEventLog, verify_log
from thymira.mira.checks import AuditContext, audit_run
from thymira.schemas import Actor, EventType, ModelRoutePolicy, RunStatus
from thymira.thy.agents import data_agent_catalog
from thymira.thy.models import AgentTask, PlanOutput, ThyAgentKind, ThyPhase
from thymira.tools import ToolManager
from thymira.tools.builtins import builtins_registry

if TYPE_CHECKING:
    from pathlib import Path

    from langgraph.graph.state import CompiledStateGraph

    from thymira.agents import AgentCatalog
    from thymira.api import RuntimeDeps
    from thymira.schemas import Run

pytestmark = pytest.mark.integration

TEST_ROUTE_POLICY = ModelRoutePolicy.from_routes(("test-model",), authority="code-owned-tests")


@pytest.fixture(autouse=True)
def _configured_test_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bind the direct provider seams to an explicit code-owned test route."""
    for variable in ("THYMIRA_MODEL_FAST", "THYMIRA_MODEL_STANDARD", "THYMIRA_MODEL_FRONTIER"):
        monkeypatch.setenv(variable, "test-model")


_PLAN_REASON = "test policy: THY's plan needs no review"
# The same rule as a project overlay (not an in-process Policy object), so the production
# factory's own Gate, the Run's recorded policy hash and MIRA's snapshot are all one policy --
# exactly `test_e2e_tool_approval.py`'s `_PROJECT_POLICY_OVERLAY` trick.
_PROJECT_POLICY_OVERLAY = f"""name: plan-passes
version: "1.0"
action_rules:
  - id: TEST-PLAN
    description: THY's plan proposal may proceed in this proof.
    action_types: [plan.proposed]
    decision: PASS
    reason: "{_PLAN_REASON}"
"""
_PROJECT_CONFIG = "project:\n  name: roster-proof\n  domain: credit_risk\n"

_REVIEWER = Actor(kind="human", id="reviewer", authenticated=True)

_DATA_INSTRUCTION = "profile it"
_DATA_QUALITY_INSTRUCTION = "flag quality risks before this dataset trains a model"

_DATA_FINAL = LLMToolCall(
    id="data-final",
    name="final_result",
    arguments={
        "columns": ["value"],
        "dtypes": {"value": "int64"},
        "missing": {"value": 0},
        "target_candidates": [],
    },
)
# The narrow catalog holds only "data" -- the first answer still names the FINAL specialist the
# live failure died on, while the bounded correction uses only the available agent.
_NODE_A_PLAN = PlanOutput(
    tasks=(
        AgentTask(
            id="t1", agent=ThyAgentKind.DATA, phase=ThyPhase.EXECUTE, instruction=_DATA_INSTRUCTION
        ),
        AgentTask(
            id="t2",
            agent=ThyAgentKind.DATA_QUALITY,
            phase=ThyPhase.EXECUTE,
            instruction=_DATA_QUALITY_INSTRUCTION,
        ),
    )
)
_NODE_A_CORRECTED_PLAN = PlanOutput(tasks=(_NODE_A_PLAN.tasks[0],))


def _workspace(directory: Path) -> Path:
    """Create the project directory THY inspects, with the plan-passes policy overlay."""
    thymira = directory / ".thymira"
    thymira.mkdir(parents=True)
    (thymira / "config.yaml").write_text(_PROJECT_CONFIG, encoding="utf-8", newline="\n")
    (thymira / "policies.yaml").write_text(_PROJECT_POLICY_OVERLAY, encoding="utf-8", newline="\n")
    return directory


def _production_deps(
    tmp_path: Path, workspace: Path, provider: ScriptedProvider, catalog: AgentCatalog
) -> RuntimeDeps:
    """Assemble the API composition root over a graph the production factory builds.

    Mirrors `test_e2e_tool_approval.py`'s `_production_deps`: the real `ToolManager` over the
    real built-in tool registry (every dispatchable specialist's `tool_allowlist` resolves
    against it -- `test_thy_agent_roster.py`'s honesty check proves this once, for every kind),
    `specs=()` disables MIRA's audit-agent fan-out (the deterministic controls this proof reads
    still run), and `root = tmp_path / "runtime"` is the directory this test itself controls and
    later re-derives `events.jsonl`'s path from -- never an assumption about `LocalRunStore`'s
    private layout.
    """
    registry = builtins_registry()
    assembled: list[RuntimeDeps] = []

    def graph_factory(run: Run) -> CompiledStateGraph:
        deps = assembled[0]
        assert deps.project_resolution is not None
        return build_runtime_graph_factory(
            deps.run_store,
            gate_factory=deps.gate_factory,
            artifact_store_factory=deps.artifact_store_factory,
            tool_manager=ToolManager(registry),
            tool_registry=registry,
            record_repository=deps.record_repository,
            checkpoint_repository=deps.checkpoint_repository,
            provider=provider,
            project_dir=workspace,
            project_config=deps.project_resolution.config,
            catalog=catalog,
            specs=(),
            plan_repository=deps.board_repository,
            dag_repository=deps.dag_repository,
            route_policy=TEST_ROUTE_POLICY,
        )(run)

    assembled.append(
        build_default_deps(
            tmp_path / "runtime",
            workspace=workspace,
            graph_factory=graph_factory,
            principal_resolver=TEST_CREDENTIAL,
        )
    )
    return assembled[0]


def _events_path(root: Path, run_id: str) -> Path:
    """The persisted `events.jsonl` path under `root`, a directory this test itself controls.

    `build_default_deps` (`apps/api/src/thymira/api/deps.py`) builds
    `LocalRunStore(root / "runs")`, and `LocalRunStore` persists each Run under
    `<its own root>/<run_id>/events.jsonl` (`runtime/state/.../local_run_store.py`) -- read from
    both modules, not assumed. `root` is the same directory `_production_deps` above passed to
    `build_default_deps`, so this is the test deriving a path from its own known composition, the
    same way `test_e2e_tool_approval.py`'s `_compose` (a different root layout) does for its own
    `LocalRunStore(tmp_path / "runs")`.
    """
    return root / "runs" / run_id / "events.jsonl"


def _drain_reviews(deps: RuntimeDeps, run_id: str) -> None:
    """Auto-approve any review the Run unexpectedly parks on, so a test never hangs on one.

    The scripted plan calls no tool a policy escalates (the data specialist answers directly with
    `final_result`), so this proof does not expect to park -- but if the environment's default
    policy ever changes underneath it, resolving generically here keeps the test's own point (the
    roster fix) from being obscured by an unrelated review.
    """
    for _ in range(5):
        if deps.run_service.get_run(run_id).status is not RunStatus.WAITING_FOR_APPROVAL:
            return
        deps.run_service.resolve_approval(
            run_id,
            gate=deps.gate_factory(run_id, approver=lambda _request: True, human=_REVIEWER),
            actor=_REVIEWER,
            note="reviewed",
        )


def test_an_unregistered_planner_choice_is_corrected_before_gate_and_execute(
    tmp_path: Path,
) -> None:
    """A narrow production catalog constrains Plan before the Run executes and reaches MIRA.

    `data_agent_catalog()` holds only "data" -- the same shape of narrow, caller-built catalog
    `Recipe.catalog()` is the one legitimate production path for (see the Agent Note). The first
    response names `data-quality-agent`, exactly like the live failure's plan did; the second obeys
    the catalog-scoped schema and is the only plan the Gate can authorize.
    """
    workspace = _workspace(tmp_path / "workspace")
    provider = ScriptedProvider([_NODE_A_PLAN, _NODE_A_CORRECTED_PLAN, _DATA_FINAL])
    deps = _production_deps(tmp_path, workspace, provider, data_agent_catalog())
    assert deps.project_resolution is not None
    session = deps.session_service.create(
        project_id=deps.project_resolution.project_id, client="test"
    )

    run = deps.run_service.create_run(
        session.id, "profile the dataset", actor=Actor.system(), workspace=workspace
    )
    _drain_reviews(deps, run.id)

    # From the returned run: the corrected plan completed. Everything else below comes from the
    # independent oracle, never this object.
    final_run = deps.run_service.get_run(run.id)
    assert final_run.status is RunStatus.COMPLETED

    # The independent oracle: a reader that wrote none of this opens the persisted log for
    # itself, verifies the chain, and hands the replayed stream to MIRA's own recomputation --
    # `thymira.mira` imports nothing from `thymira.thy.nodes.execute` (machine-checked by
    # `just check-imports`), so `audit_run` never sees, and cannot be fooled by, the producer.
    path = _events_path(tmp_path / "runtime", run.id)
    verification = verify_log(path)
    replayed = JsonlEventLog(path, run.id).events()
    report = audit_run(AuditContext(run.id, replayed))

    assert verification.valid
    assert verification.event_count == len(replayed)

    unresolved = [
        event
        for event in replayed
        if event.type is EventType.AGENT_MESSAGE
        and event.payload.get("agent") == "data-quality-agent"
    ]
    assert unresolved == []
    assert len(provider.calls) == 3

    plan_decisions = [
        event
        for event in replayed
        if event.type is EventType.POLICY_DECISION and event.payload.get("reason") == _PLAN_REASON
    ]
    assert len(plan_decisions) == 1

    audit_started = [event for event in replayed if event.type is EventType.AUDIT_STARTED]
    audit_completed = [event for event in replayed if event.type is EventType.AUDIT_COMPLETED]
    assert len(audit_started) == 1
    assert len(audit_completed) == 1
    assert audit_started[0].seq < audit_completed[0].seq

    findings_decisions = [
        event
        for event in replayed
        if event.type is EventType.POLICY_DECISION
        and event.payload.get("subject_kind") == "findings"
    ]
    assert len(findings_decisions) == 1

    terminal = [
        event for event in replayed if event.type in (EventType.RUN_COMPLETED, EventType.RUN_FAILED)
    ]
    assert len(terminal) == 1
    assert terminal[0].type is EventType.RUN_COMPLETED
    run_failed_events = [event for event in replayed if event.type is EventType.RUN_FAILED]
    assert run_failed_events == []
    # report.status is read as the honest cross-check that the recomputed audit produced a real
    # verdict, never asserted to be "passed" -- a real Run's findings can legitimately warrant
    # review; what this proof cares about is that recomputation happened at all.
    assert report.status in {"passed", "passed_with_warnings", "failed"}
