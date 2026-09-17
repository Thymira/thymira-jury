"""Reusable terminal presentation for the Thymira CLI."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.console import Console
from rich.table import Table
from rich.tree import Tree

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from thymira.cli.client import (
        AuditView,
        DurablePlanView,
        EnqueueReceiptView,
        EventView,
        ExperimentView,
        MlflowRunView,
        PendingApprovalView,
        RiskInterviewView,
        RunView,
    )
    from thymira.cli.thy_view import AgentNode, ThyActivityView


def render_plan(plan: DurablePlanView) -> str:
    """Render the durable goal/plan board returned after the request settles."""
    lines = [
        f"Plan for {plan.run_id}",
        "",
        f"  Plan ID: {plan.plan_id}",
        f"  Revision: {plan.revision}",
        f"  Mode: {plan.mode}",
        f"  Goal status: {plan.goal_status}",
    ]
    if plan.goal:
        lines.append(f"  Goal: {plan.goal}")
    if plan.items:
        lines.extend(("", "  #   State       Item", "  " + "-" * 50))
        for index, item in enumerate(plan.items, start=1):
            text = item.get("text", item.get("objective", ""))
            state = item.get("state", "todo")
            lines.append(f"  {index:<3} {state!s:<11} {text}")
    else:
        lines.extend(("", "  No todo items were proposed."))
    if plan.artifact is not None:
        lines.extend(
            (
                "",
                f"  Artifact: {plan.artifact.artifact_id}",
                f"  Artifact digest: {plan.artifact.sha256}",
                f"  Artifact content: {plan.artifact.content}",
            )
        )
    if plan.review_feedback:
        lines.append("  Review feedback: " + " | ".join(plan.review_feedback))
    if plan.blocked_cause:
        lines.append(f"  Blocked cause: {plan.blocked_cause}")
    return "\n".join(lines)


def render_pending_approval(pending: PendingApprovalView) -> str:
    """Render the pending human-approval decision the CLI is about to resolve."""
    fields: list[tuple[str, object]] = [("Summary", pending.summary or "-")]
    if pending.reason:
        fields.append(("Reason", pending.reason))
    if pending.rule_id:
        fields.append(("Rule", pending.rule_id))
    if pending.cost_so_far:
        fields.append(("Cost so far", _render_mapping(pending.cost_so_far)))
    if pending.tool_call is not None:
        fields.append(("Tool", pending.tool_call.get("tool", "-")))
        arguments = pending.tool_call.get("arguments")
        if isinstance(arguments, dict) and arguments:
            fields.append(("Arguments", _render_mapping(arguments)))
    return "\n".join(("Pending decision", "", render_fields(fields)))


def render_human_review(run: RunView, pending: PendingApprovalView) -> str:
    """Render the information a human needs before approving or rejecting a Run."""
    lines = [
        "Human review required",
        "",
        f"  Run          {run.id}",
        f"  Status       {run.status}",
        "",
        "What this run is doing",
        f"  {run.prompt}",
        "",
        "What needs your decision",
        f"  {pending.summary or 'The runtime asks to continue this run.'}",
    ]
    if pending.reason:
        lines.extend(("", "Why approval is needed", f"  {pending.reason}"))
    if pending.rule_id:
        lines.extend(("", "Technical policy reference", f"  {pending.rule_id}"))
    if pending.cost_so_far:
        lines.extend(("", "Current usage", f"  {_render_mapping(pending.cost_so_far)}"))
    if pending.tool_call is not None:
        tool = pending.tool_call.get("tool", "-")
        arguments = pending.tool_call.get("arguments")
        lines.extend(("", "Action requested", f"  Run the tool: {tool}"))
        if isinstance(arguments, dict) and arguments:
            lines.append(f"  With: {_render_mapping(arguments)}")
    else:
        lines.extend(("", "Action requested", "  Continue the run-level activity described above."))
    lines.extend(
        (
            "",
            "Choose one action",
            f"  Approve: thymira approve {run.id}",
            f'  Reject:  thymira reject {run.id} --reason "..."',
        )
    )
    return "\n".join(lines)


def render_review_hint(run_id: str) -> str:
    """Render the short pointer shown by status while a Run waits for a decision."""
    return "\n".join(
        (
            "Human review required",
            "",
            "  This run is paused until you decide.",
            f"  Read the details: thymira review {run_id}",
        )
    )


def render_follow_up_review(run_id: str, pending: PendingApprovalView) -> str:
    """Render the next safe action when an approval leaves a Run waiting."""
    reason = pending.reason or pending.summary or "The runtime needs another human decision."
    return "\n".join(
        (
            "Human review required to continue",
            "",
            f"  Why          {reason}",
            f"  Review       thymira review {run_id}",
            "",
            "After reviewing, choose one action",
            f"  Approve      thymira approve {run_id}",
            f'  Reject       thymira reject {run_id} --reason "..."',
        )
    )


def render_no_pending_approval(run_id: str) -> str:
    """Render the state where a Run has no pending human-approval decision."""
    return f"Run '{run_id}' has no pending human-approval decision."


BANNER = r"""
 _______  _     _  __     __ __  __  _____  _____          _
|__   __|| |   | | \ \   / /|  \/  ||_   _||  __ \       / \
   | |   | |___| |  \ \_/ / | \  / |  | |  | |__) |     / _ \
   | |   |  ___  |   \   /  | |\/| |  | |  |  _  /     / /_\ \
   | |   | |   | |    | |   | |  | | _| |_ | | \ \    / _____ \
   |_|   |_|   |_|    |_|   |_|  |_||_____||_|  \_\  /_/     \_""".lstrip()

DESCRIPTION = "Agentic Data Science Runtime"
_FIELD_INDENT = "  "
_FIELD_WIDTH = 12


def render_welcome(help_text: str) -> str:
    """Render the branded welcome screen and the generated command help."""
    return "\n".join(
        (
            BANNER.rstrip(),
            DESCRIPTION,
            "",
            "Get started:",
            '  thymira run "Describe the Data Science work to execute."',
            "",
            help_text.rstrip(),
        )
    )


def render_fields(fields: Sequence[tuple[str, object]]) -> str:
    """Render aligned label/value rows for a terminal summary."""
    return "\n".join(f"{_FIELD_INDENT}{label:<{_FIELD_WIDTH}}{value}" for label, value in fields)


def render_run_created(run: RunView) -> str:
    """Render the result of creating a run and its next suggested command."""
    fields: list[tuple[str, object]] = [
        ("ID", run.id),
        ("Status", run.status),
        ("Prompt", run.prompt),
    ]
    if run.publication_receipt is not None:
        fields.append(("Publication", run.publication_receipt.publication_id))
    return "\n".join(
        ("Run created", "", render_fields(fields), "", f"Next: thymira status {run.id}")
    )


def render_enqueue_receipt(receipt: EnqueueReceiptView) -> str:
    """Render durable acceptance progress for a queued lifecycle mutation."""
    fields = (
        ("Input", receipt.input_id),
        ("Run", receipt.run_id),
        ("Accepted", receipt.accepted),
        ("Duplicate", receipt.duplicate),
    )
    return "\n".join(("Input enqueued", "", render_fields(fields)))


def render_run_status(run: RunView) -> str:
    """Render the fields available in a run status response."""
    fields: list[tuple[str, object]] = [
        ("ID", run.id),
        ("Status", run.status),
        ("Prompt", run.prompt),
    ]
    if run.created_at is not None:
        fields.append(("Created", run.created_at))
    if run.started_at is not None:
        fields.append(("Started", run.started_at))
    fields.extend(
        (
            ("Agents", run.agent_count),
            ("Tools", run.tool_count),
            ("Experiments", run.experiment_count),
            ("Artifacts", run.artifact_count),
        )
    )
    if run.phase is not None:
        fields.append(("Phase", run.phase))
    if run.final_decision is not None:
        fields.append(("Decision", run.final_decision))
    return "\n".join(("Run status", "", render_fields(fields)))


def render_risk_interview(interview: RiskInterviewView) -> str:
    """Render the one bounded intake action still available for a Run."""
    pending = interview.pending_question
    if pending is None:
        if interview.requires_human_review:
            return "Risk interview\n\n  The question limit was reached; human review is required."
        return "Risk interview\n\n  No information is pending."
    fields = (
        ("Question", pending.question_number),
        ("Field", pending.field),
        ("Prompt", pending.question),
    )
    return "\n".join(
        (
            "Risk information required",
            "",
            render_fields(fields),
            "",
            f'Next: thymira answer {interview.run.id} "<your answer>"',
        )
    )


def render_human_decision(run: RunView, *, approved: bool, tool_call: bool = False) -> str:
    """Render a compact receipt for a recorded approval decision."""
    subject = "Tool call" if tool_call else "Run"
    title = f"{subject} {'approved' if approved else 'rejected'}"
    fields: list[tuple[str, object]] = [("Status", run.status)]
    if run.phase is not None:
        fields.append(("Phase", run.phase))
    if run.final_decision is not None:
        fields.append(("Decision", run.final_decision))
    return "\n".join((title, "", render_fields(fields)))


def render_run_update(run: RunView, *, action: str) -> str:
    """Render a compact result after a non-approval lifecycle action."""
    fields: list[tuple[str, object]] = [("Status", run.status)]
    if run.phase is not None:
        fields.append(("Phase", run.phase))
    if run.final_decision is not None:
        fields.append(("Decision", run.final_decision))
    return "\n".join((f"Run {action}", "", render_fields(fields)))


def render_runs(runs: Sequence[RunView]) -> str:
    """Render a concise table of runs returned by the API."""
    if not runs:
        return "Runs\n\n  No runs found."

    header = "  ID                                   Status      Created                   Prompt"
    separator = "  " + "-" * (len(header) - 2)
    rows = [
        (
            f"  {run.id:<36}  {run.status:<10}  "
            f"{(run.created_at or '-'): <24}  {_truncate(run.prompt, 60)}"
        )
        for run in runs
    ]
    return "\n".join(("Runs", "", header, separator, *rows))


def render_events_header(run_id: str) -> str:
    """Render the fixed heading for a Run event history."""
    return "\n".join(
        (
            f"Events for {run_id}",
            "",
            "  Type                      Seq   Actor",
            "  -----------------------------------------",
        )
    )


def render_event(event: EventView) -> str:
    """Render one concise event line without exposing its payload."""
    return f"  {event.type:<25} {event.seq:<5} {event.actor}"


def render_no_events() -> str:
    """Render the empty event-history state."""
    return "  No events found."


def _truncate(value: str, width: int) -> str:
    """Keep a table cell within a predictable terminal width."""
    if len(value) <= width:
        return value
    return value[: width - 3] + "..."


def render_audit(audit: AuditView) -> str:
    """Render an audit report's controls, findings and the final Decision."""
    lines = [f"Audit for {audit.run_id}", "", f"  Status  {audit.status}", ""]
    lines.append("  Control ID   Status              Severity  Detail")
    lines.append("  " + "-" * 70)
    lines.extend(
        f"  {c.control_id:<12} {c.status:<19} {c.severity:<9} {c.detail or '-'}"
        for c in audit.controls
    )
    if audit.findings:
        lines.append("")
        lines.append("  Findings")
        lines.append("  " + "-" * 70)
        lines.extend(f"  {f.control_id:<12} {f.severity:<9} {f.title}" for f in audit.findings)
    lines.append("")
    if audit.decision is None:
        lines.append("Decision: none recorded")
        return "\n".join(lines)

    lines.append(f"Decision: {audit.decision}")
    if audit.decision_reason:
        lines.append(f"  Reason: {audit.decision_reason}")
    if audit.decision == "REQUIRE_HUMAN_REVIEW":
        lines.append(f"Next: thymira approve {audit.run_id} (or thymira reject {audit.run_id})")
    return "\n".join(lines)


def render_experiments(run_id: str, experiments: Sequence[ExperimentView]) -> str:
    """Render a run's experiments with their parameters, metrics and tracker linkage."""
    if not experiments:
        return f"Experiments for {run_id}\n\n  No experiments found."

    header = "  ID           Name             Status      Seed  Tracker Run       Model Artifact"
    separator = "  " + "-" * (len(header) - 2)
    lines = [f"Experiments for {run_id}", "", header, separator]
    for experiment in experiments:
        lines.append(
            f"  {experiment.id:<12} {experiment.name:<16} {experiment.status:<11} "
            f"{_render_optional(experiment.seed):<5} "
            f"{_render_optional(experiment.tracker_run_id):<17} "
            f"{_render_optional(experiment.model_artifact_id)}"
        )
        if experiment.parameters:
            lines.append(f"    Parameters  {_render_mapping(experiment.parameters)}")
        if experiment.metrics:
            lines.append(f"    Metrics     {_render_mapping(experiment.metrics)}")
    return "\n".join(lines)


def render_mlflow_runs(run_id: str, runs: Sequence[MlflowRunView]) -> str:
    """Render a run's MLflow tracker runs with their parameters and metrics."""
    if not runs:
        return f"MLflow runs for {run_id}\n\n  No MLflow runs found."

    header = "  Tracker Run       Experiment            Status"
    separator = "  " + "-" * (len(header) - 2)
    lines = [f"MLflow runs for {run_id}", "", header, separator]
    for tracker_run in runs:
        lines.append(
            f"  {tracker_run.tracker_run_id:<17} {tracker_run.experiment_name:<21} "
            f"{tracker_run.status}"
        )
        if tracker_run.params:
            lines.append(f"    Parameters  {_render_mapping(tracker_run.params)}")
        if tracker_run.metrics:
            lines.append(f"    Metrics     {_render_mapping(tracker_run.metrics)}")
    return "\n".join(lines)


def _render_optional(value: object) -> str:
    """Render a possibly-missing field as a placeholder dash."""
    return "-" if value is None else str(value)


def _render_mapping(mapping: Mapping[str, object]) -> str:
    """Render a parameters/metrics mapping as comma-separated key=value pairs."""
    return ", ".join(f"{key}={value}" for key, value in mapping.items())


def render_error(message: str, detail: str | None = None) -> str:
    """Render a concise error and an optional corrective detail."""
    lines = [f"Error: {message}"]
    if detail is not None:
        lines.append(detail)
    return "\n".join(lines)


_ACTIVITY_WIDTH = 100


def _money(value: float | None) -> str:
    """Format a USD cost with four decimals, or say so when there is no measurement.

    ``None`` reaches here when the gateway priced no step of an agent's work. Rendering it as
    ``$0.0000`` would be the display claiming the work was free, so it renders as ``unknown``.
    """
    if value is None:
        return "unknown"
    return f"${value:.4f}"


def _activity_phases(view: ThyActivityView) -> Table:
    """A table of the workflow phases and the activity that fell under each."""
    table = Table(title="Phases", title_justify="left", expand=False)
    table.add_column("#", justify="right")
    table.add_column("Phase")
    table.add_column("Events", justify="right")
    table.add_column("Model calls", justify="right")
    for index, phase in enumerate(view.phases, start=1):
        table.add_row(str(index), phase.name, str(phase.event_count), str(phase.model_calls))
    return table


def _activity_node_label(node: AgentNode) -> str:
    """A tree node's label: the agent, annotated with its tokens/cost when it has any."""
    usage = node.usage
    if usage.total_tokens == 0 and usage.cost_usd == 0.0:
        return node.name
    return f"{node.name}  {usage.total_tokens} tokens  {_money(usage.cost_usd)}"


def _add_tree_node(parent: Tree, node: AgentNode) -> None:
    """Attach ``node`` and its children under ``parent`` (depth-first, order preserved)."""
    branch = parent.add(_activity_node_label(node))
    for child in node.children:
        _add_tree_node(branch, child)


def _activity_tree(view: ThyActivityView) -> Tree:
    """The delegation tree rooted at a fixed label, one branch per delegation root."""
    tree = Tree("Delegation tree")
    for root in view.tree:
        _add_tree_node(tree, root)
    return tree


def _activity_usage_table(view: ThyActivityView) -> Table:
    """The settled per-agent token/cost table with an explicitly scoped subtotal."""
    table = Table(title="Settled agent usage", title_justify="left", expand=False)
    table.add_column("Agent")
    table.add_column("Input", justify="right")
    table.add_column("Output", justify="right")
    table.add_column("Tokens", justify="right")
    table.add_column("Cost", justify="right")
    for usage in view.agent_usage:
        table.add_row(
            usage.name,
            str(usage.input_tokens),
            str(usage.output_tokens),
            str(usage.total_tokens),
            _money(usage.cost_usd),
        )
    table.add_section()
    total = view.total
    table.add_row(
        "Agent subtotal",
        str(total.input_tokens),
        str(total.output_tokens),
        str(total.total_tokens),
        _money(total.cost_usd),
    )
    return table


def _provider_usage_table(view: ThyActivityView) -> Table | None:
    """All provider response tokens, including orchestration and preflight calls."""
    usage = view.provider_usage
    if usage is None:
        return None
    table = Table(title="All provider request tokens", title_justify="left", expand=False)
    table.add_column("Requests", justify="right")
    table.add_column("Input", justify="right")
    table.add_column("Output", justify="right")
    table.add_column("Tokens", justify="right")
    table.add_column("Cached input", justify="right")
    table.add_column("Reasoning output", justify="right")
    table.add_row(
        str(usage.request_count),
        str(usage.input_tokens),
        str(usage.output_tokens),
        str(usage.total_tokens),
        str(usage.cached_input_tokens),
        str(usage.reasoning_output_tokens),
    )
    return table


def render_thy_activity(view: ThyActivityView) -> str:
    """Render THY activity, scoped agent settlements, and all provider response tokens.

    Rich renderables (a phase table, the delegation `Tree`, the per-agent usage table) captured to
    a stable, colourless string so it echoes cleanly under UTF-8 and is identical across terminals.
    """
    console = Console(
        width=_ACTIVITY_WIDTH, no_color=True, highlight=False, emoji=False, force_terminal=False
    )
    with console.capture() as capture:
        console.print("THY activity")
        console.print(_activity_phases(view))
        console.print(_activity_tree(view))
        console.print(_activity_usage_table(view))
        provider_table = _provider_usage_table(view)
        if provider_table is not None:
            console.print(provider_table)
        console.print("Billing scope: agent subtotal excludes THY/MIRA/preflight calls.")
        console.print("Use provider or Langfuse billing for the whole-run price.")
    return capture.get().rstrip("\n")
