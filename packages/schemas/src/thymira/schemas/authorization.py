"""Contract 0.3 proposals, authorization contexts, and separate approvals."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal

from pydantic import Field

from thymira.schemas.actor import Actor
from thymira.schemas.audit import Evidence
from thymira.schemas.base import ThymiraModel, utc_now
from thymira.schemas.enums import AuthorizationDecision
from thymira.schemas.ids import Id

if TYPE_CHECKING:
    from collections.abc import Mapping


class ActionKind(StrEnum):
    """Control-plane action requested by an intent, before authorization."""

    EXECUTE_TOOL = "execute_tool"
    TRANSITION_RUN = "transition_run"
    REQUEST_INFORMATION = "request_information"
    REQUEST_APPROVAL = "request_approval"
    PAUSE_RUN = "pause_run"
    RESUME_RUN = "resume_run"
    REOPEN_WORK = "reopen_work"
    REVIEW_FINDINGS = "review_findings"


class ActionIntent(ThymiraModel):
    """A bounded requested effect that has no authority to execute itself."""

    id: Id
    run_id: Id
    requester: Actor
    action_kind: ActionKind
    subject_kind: Literal["run", "task", "tool_call", "findings"]
    subject_id: str = Field(min_length=1)
    purpose: str = Field(min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)
    evidence: tuple[Evidence, ...] = ()
    idempotency_key: str = Field(min_length=1)
    created_at: datetime = Field(default_factory=utc_now)


class AuthorizationContext(ThymiraModel):
    """Immutable policy evidence that scopes a single potential effect.

    Its canonical hash is calculated by :func:`thymira.events.hash_authorization_context`.
    The model does not import the events package, preserving the contract-layer boundary.
    """

    id: Id
    run_id: Id
    intent_id: Id
    policy_decision_id: Id
    policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision: AuthorizationDecision
    subject_kind: Literal["run", "task", "tool_call", "findings"]
    subject_id: str = Field(min_length=1)
    action_kind: ActionKind
    constraints: dict[str, Any] = Field(default_factory=dict)
    requires_approval: bool = False
    expires_at: datetime | None = None

    def semantic_dict(self) -> dict[str, Any]:
        """Return only the stable, authorization-semantic fields used for hashing."""
        return self.model_dump(exclude={"id"}, mode="json")


class Approval(ThymiraModel):
    """A separate, append-only human response to one authorization context."""

    id: Id
    run_id: Id
    policy_decision_id: Id
    authorization_context_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    approved: bool
    approved_by: Actor
    rationale: str | None = None
    approved_at: datetime = Field(default_factory=utc_now)


DECISION_ID_KEYS: tuple[str, ...] = ("policy_decision_id", "decision_id")
"""The names an approval payload may use for its policy decision, in order of precedence."""


def approval_decision_id(payload: Mapping[str, Any]) -> str | None:
    """Return the policy decision an approval payload names, under either recorded field name.

    One field, two names. :class:`Approval` -- appended verbatim by the control-plane approval
    path -- calls it ``policy_decision_id``, its frozen Contract 0.3 field; the hand-built payloads
    written elsewhere (the Gate's synchronous answer, the local approval service) call it
    ``decision_id``. Both shapes are already on append-only chains, so no writer change can
    reconcile them: only a reader repairs evidence that is already written. This accessor is that
    reader, and it lives beside :class:`Approval` because the divergence is a property of that
    record's serialisation rather than of any one component -- ``policies``, ``core`` and ``mira``
    each read the same pair, and every one of them sits above ``schemas``.

    Precedence: ``policy_decision_id`` wins. It is the pydantic-validated field of the frozen
    record, written next to the ``authorization_context_sha256`` that binds the answer to a
    context naming the same decision; ``decision_id`` is a plain dict key nothing validates. A
    payload carrying both is one where something added a key beside a serialised ``Approval``, so
    believing the record's own field is the fail-closed reading -- the reverse order would let an
    added ``decision_id`` redirect a genuine approval onto a decision it never approved.

    ``None`` is never an identity: a key contributes only a non-empty string, so a payload naming
    no decision (or naming one under a value that is not a string) yields ``None`` and answers
    nothing. Compare through :func:`approval_names_decision`, so a missing id on one side can
    never match a missing id on the other.

    Args:
        payload: A ``human.approval`` or ``human.approval_requested`` event payload.

    Returns:
        The policy-decision id the payload names, or ``None`` when it names none.
    """
    for key in DECISION_ID_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def approval_names_decision(payload: Mapping[str, Any], decision_id: object) -> bool:
    """Whether an approval payload names exactly ``decision_id``.

    The comparison every reader of the pair needs: ``False`` whenever either side names no
    decision, so a payload with no id can never pair with a request (or a review) that has none
    either -- the ``None == None`` match that would silently mark an unanswerable request
    answered. Tolerating two *names* never means tolerating two *values*: the ids still match
    exactly.

    Args:
        payload: A ``human.approval`` or ``human.approval_requested`` event payload.
        decision_id: The policy-decision id to match, as read from another payload or record.

    Returns:
        ``True`` only when both sides name the same decision.
    """
    named = approval_decision_id(payload)
    return named is not None and isinstance(decision_id, str) and named == decision_id
