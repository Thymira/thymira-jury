"""Fold a Run's event history into a THY activity view (THY-30).

The CLI owns no state: it never imports a runtime member. It reads the Run's events over HTTP
(``GET /runs/{id}/events``) and folds them here into what a reader wants to see — the workflow
phases, the agent delegation tree, each agent's settled tokens and cost, and all recorded provider
response tokens. Every input is an
:class:`~thymira.cli.client.EventView` (``seq``/``type``/``actor``/``payload``); the fold reads
only the redacted, hash-chained payload fields the runtime already records, so more agents and
phases appear here automatically as the runtime emits them, with no coupling to THY internals.

Sources (all already on the log):

- **phases** — a walk over the events in ``seq`` order. The span before THY's own plan call is
  ``inspect``; THY's ``model.selected`` (``role == "thy"``) names ``plan`` (task ``plan``) or
  ``summarize`` (task ``synthesize``/``decide``); the first ``agent.started`` opens ``execute``.
- **the delegation tree** — ``agent.message`` edges (``parent_agent`` -> ``agent``), rooted at the
  delegator (``thy``); any agent seen only in ``agent.started``/``agent.parked``/``agent.completed``
  is attached at the root so nothing that ran or awaits review is dropped.
- **settled per-agent tokens/cost** — ``agent.completed`` carries this step's ``input_tokens``/
  ``output_tokens``/``cost_usd`` (recorded by the runner, THY-30); grouped by agent, they give the
  per-agent line and agent subtotal. THY's own plan/summarize and MIRA preflight calls are not
  routed through an agent runner, so their cost is not present in this settlement view.
- **all provider response tokens** — every ``model.response_chunk`` carries provider-reported
  input/output usage. Their sum covers orchestration and preflight as well as delegated agents;
  cached input and reasoning output remain explicit subsets instead of being added twice.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from thymira.cli.client import EventView

_MODEL_SELECTED = "model.selected"
_MODEL_RESPONSE_CHUNK = "model.response_chunk"
_AGENT_STARTED = "agent.started"
_AGENT_PARKED = "agent.parked"
_AGENT_COMPLETED = "agent.completed"
_AGENT_MESSAGE = "agent.message"

_THY_ROLE = "thy"
_ROOT_AGENT = "thy"
_PHASE_INSPECT = "inspect"
_PHASE_PLAN = "plan"
_PHASE_EXECUTE = "execute"
_PHASE_SUMMARIZE = "summarize"
_PLAN_TASKS = frozenset({"plan"})
_SUMMARIZE_TASKS = frozenset({"synthesize", "decide"})


@dataclass(frozen=True, slots=True)
class AgentUsage:
    """One agent's summed tokens and cost across every step it ran."""

    name: str
    input_tokens: int = 0
    output_tokens: int = 0
    #: ``None`` means *unmeasured*, never *free*. The runner records a null ``cost_usd`` when the
    #: gateway priced no step of a call, and folding that into a dollar figure would put a
    #: confident number in front of a human -- the one place the distinction the ledger, the
    #: runner and the Gate all keep would be quietly thrown away.
    cost_usd: float | None = 0.0

    @property
    def total_tokens(self) -> int:
        """Input plus output tokens attributed to this agent."""
        return self.input_tokens + self.output_tokens

    def plus(self, other: AgentUsage) -> AgentUsage:
        """This usage combined with another's tokens and cost (name kept)."""
        return AgentUsage(
            name=self.name,
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cost_usd=_add_cost(self.cost_usd, other.cost_usd),
        )


@dataclass(frozen=True, slots=True)
class ProviderUsage:
    """Provider-reported usage across every recorded model response in the Run."""

    request_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    reasoning_output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        """Input plus output tokens without double-counting their cached/reasoning subsets."""
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True, slots=True)
class AgentNode:
    """One node of the delegation tree: an agent and the agents it delegated to."""

    name: str
    usage: AgentUsage
    children: tuple[AgentNode, ...] = ()


@dataclass(frozen=True, slots=True)
class PhaseView:
    """One workflow phase and how much activity fell under it."""

    name: str
    event_count: int = 0
    model_calls: int = 0


@dataclass(frozen=True, slots=True)
class ThyActivityView:
    """The folded THY activity, agent settlements and provider tokens for one Run."""

    phases: tuple[PhaseView, ...] = ()
    tree: tuple[AgentNode, ...] = ()
    agent_usage: tuple[AgentUsage, ...] = ()
    total: AgentUsage = field(default_factory=lambda: AgentUsage(name="total"))
    provider_usage: ProviderUsage | None = None

    @property
    def has_activity(self) -> bool:
        """Whether THY actually did something worth a view.

        A lone ``inspect`` phase (a run that has only just started) is not activity; a plan,
        execute or summarize phase, a delegated agent, or any recorded usage is.
        """
        return (
            bool(self.tree)
            or bool(self.agent_usage)
            or self.provider_usage is not None
            or any(phase.name != _PHASE_INSPECT for phase in self.phases)
        )


def _payload_str(payload: Mapping[str, object], key: str) -> str | None:
    """Read one string payload field, or ``None`` when absent or not a string."""
    value = payload.get(key)
    return value if isinstance(value, str) and value else None


def _payload_int(payload: Mapping[str, object], key: str) -> int:
    """Read one non-negative integer payload field, defaulting to ``0``."""
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def _payload_float(payload: Mapping[str, object], key: str) -> float:
    """Read one numeric payload field as a float, defaulting to ``0.0``."""
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0.0
    return float(value)


def _payload_cost(payload: Mapping[str, object], key: str) -> float | None:
    """Read a cost field, keeping *unmeasured* apart from *zero*.

    An explicit ``null`` is the runner saying it could not price the step, and it stays ``None``
    all the way to the rendered table. A key that is simply absent contributes nothing, which is
    ``0.0``: an event that never carried the field is not a measurement that failed.
    """
    if key in payload and payload[key] is None:
        return None
    return _payload_float(payload, key)


def _add_cost(left: float | None, right: float | None) -> float | None:
    """Sum two costs, where one unmeasured part makes the whole sum unmeasured."""
    if left is None or right is None:
        return None
    return left + right


def _phase_transition(event: EventView, current: str) -> str:
    """The phase an event moves the walk into, or the unchanged ``current`` phase."""
    if event.type in {_AGENT_STARTED, _AGENT_PARKED}:
        return _PHASE_EXECUTE
    if event.type == _MODEL_SELECTED and _payload_str(event.payload, "role") == _THY_ROLE:
        task = _payload_str(event.payload, "task")
        if task in _PLAN_TASKS:
            return _PHASE_PLAN
        if task in _SUMMARIZE_TASKS:
            return _PHASE_SUMMARIZE
    return current


def _fold_phases(events: Sequence[EventView]) -> tuple[PhaseView, ...]:
    """Walk events in order, assigning each to a phase and counting its activity."""
    counts: dict[str, list[int]] = {}
    order: list[str] = []
    current = _PHASE_INSPECT
    for event in events:
        current = _phase_transition(event, current)
        if current not in counts:
            counts[current] = [0, 0]
            order.append(current)
        counts[current][0] += 1
        if event.type == _MODEL_SELECTED:
            counts[current][1] += 1
    return tuple(
        PhaseView(name=name, event_count=counts[name][0], model_calls=counts[name][1])
        for name in order
    )


def _fold_agent_usage(events: Sequence[EventView]) -> tuple[AgentUsage, ...]:
    """Sum ``agent.completed`` tokens and cost per agent, in first-seen order."""
    totals: dict[str, AgentUsage] = {}
    order: list[str] = []
    for event in events:
        if event.type != _AGENT_COMPLETED:
            continue
        name = _payload_str(event.payload, "agent")
        if name is None:
            continue
        step = AgentUsage(
            name=name,
            input_tokens=_payload_int(event.payload, "input_tokens"),
            output_tokens=_payload_int(event.payload, "output_tokens"),
            cost_usd=_payload_cost(event.payload, "cost_usd"),
        )
        if name not in totals:
            totals[name] = AgentUsage(name=name)
            order.append(name)
        totals[name] = totals[name].plus(step)
    return tuple(totals[name] for name in order)


def _fold_provider_usage(events: Sequence[EventView]) -> ProviderUsage | None:
    """Sum every provider response chunk, including non-agent orchestration calls."""
    response_events = [event for event in events if event.type == _MODEL_RESPONSE_CHUNK]
    if not response_events:
        return None
    return ProviderUsage(
        request_count=sum(
            1 for event in response_events if isinstance(event.payload.get("terminal"), dict)
        ),
        input_tokens=sum(_payload_int(event.payload, "input_tokens") for event in response_events),
        output_tokens=sum(
            _payload_int(event.payload, "output_tokens") for event in response_events
        ),
        cached_input_tokens=sum(
            _payload_int(event.payload, "cached_input_tokens") for event in response_events
        ),
        reasoning_output_tokens=sum(
            _payload_int(event.payload, "reasoning_output_tokens") for event in response_events
        ),
    )


def _collect_edges(
    events: Sequence[EventView],
) -> tuple[dict[str, list[str]], list[str]]:
    """Parent -> ordered children edges from ``agent.message``, plus every agent seen at all."""
    children: dict[str, list[str]] = {}
    seen: list[str] = []

    def _see(name: str) -> None:
        if name not in seen:
            seen.append(name)

    for event in events:
        if event.type in {_AGENT_STARTED, _AGENT_PARKED, _AGENT_COMPLETED}:
            name = _payload_str(event.payload, "agent")
            if name is not None:
                _see(name)
        elif event.type == _AGENT_MESSAGE:
            parent = _payload_str(event.payload, "parent_agent") or _ROOT_AGENT
            child = _payload_str(event.payload, "agent")
            if child is None:
                continue
            _see(child)
            edge = children.setdefault(parent, [])
            if child not in edge:
                edge.append(child)
    return children, seen


def _build_node(
    name: str,
    children: Mapping[str, list[str]],
    usage_by_name: Mapping[str, AgentUsage],
    ancestors: frozenset[str],
) -> AgentNode:
    """Build the subtree rooted at ``name``; ``ancestors`` guards against a cycle."""
    kids = tuple(
        _build_node(child, children, usage_by_name, ancestors | {name})
        for child in children.get(name, [])
        if child not in ancestors
    )
    return AgentNode(name=name, usage=usage_by_name.get(name, AgentUsage(name=name)), children=kids)


def _fold_tree(events: Sequence[EventView], usage: Sequence[AgentUsage]) -> tuple[AgentNode, ...]:
    """Build the delegation tree, rooted at the delegators nothing delegated to."""
    children, seen = _collect_edges(events)
    if not seen:
        return ()
    usage_by_name = {item.name: item for item in usage}
    all_children = {child for kids in children.values() for child in kids}
    roots = [parent for parent in children if parent not in all_children]
    # An agent that ran without a recorded delegation edge is attached at the top so it is shown.
    roots.extend(name for name in seen if name not in all_children and name not in roots)
    if not roots:
        roots = [_ROOT_AGENT]
    return tuple(
        _build_node(root, children, usage_by_name, frozenset()) for root in dict.fromkeys(roots)
    )


def fold_thy_activity(events: Sequence[EventView]) -> ThyActivityView:
    """Fold a Run's events into phases, delegation, settlements and provider tokens.

    Events are read in ``seq`` order (sorted defensively). The result is pure data — a caller
    renders it — so the fold stays trivially testable against a recorded log.
    """
    ordered = sorted(events, key=lambda event: event.seq)
    agent_usage = _fold_agent_usage(ordered)
    total = AgentUsage(name="total")
    for item in agent_usage:
        total = total.plus(item)
    return ThyActivityView(
        phases=_fold_phases(ordered),
        tree=_fold_tree(ordered, agent_usage),
        agent_usage=agent_usage,
        total=total,
        provider_usage=_fold_provider_usage(ordered),
    )


__all__ = [
    "AgentNode",
    "AgentUsage",
    "PhaseView",
    "ProviderUsage",
    "ThyActivityView",
    "fold_thy_activity",
]
