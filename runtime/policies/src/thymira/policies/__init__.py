"""Policy Engine: rules as data deciding PASS / WARNING / REQUIRE_HUMAN_REVIEW / BLOCK.

Ported from the thesis prototype's PolicyGate and policy packs. Owner (MVP roadmap): P4.
The LLM never decides enforcement; it supplies actions, capabilities and findings.
"""

from thymira.policies.approval import (
    ApprovalService,
    LocalApprovalService,
    PendingApproval,
    UnknownApprovalError,
    decision_from_events,
    needs_human,
    pending_approvals,
)
from thymira.policies.approval_scope import (
    APPROVAL_EXPIRES_AT_KEY,
    APPROVAL_SCOPE_DIGEST_KEY,
    DEFAULT_APPROVAL_TTL,
    DELEGATION_DEPTH_KEY,
    ApprovalScope,
    RecordedApprovalScope,
    ScopeClosure,
    ScopeClosureReason,
    lifecycle_closure,
    recorded_delegation_depth,
    scope_closure,
)
from thymira.policies.engine import (
    FAIL_SAFE_ESCALATION,
    CapabilityEvaluation,
    PolicyEngine,
    allows_execution,
    evaluate_capability,
    policy_sha256,
)
from thymira.policies.gate import (
    ApprovalRequest,
    Approver,
    Gate,
    GateMode,
    auto_approve,
    auto_reject,
)
from thymira.policies.loader import (
    load_default_policy,
    load_policy,
    load_policy_stack,
    policy_from_dict,
)
from thymira.policies.models import (
    SEVERITY_RANK,
    ActionRule,
    BudgetRule,
    CapabilityRule,
    FindingRule,
    ModelRule,
    Policy,
    RiskProfile,
    ToolCapability,
)

__all__ = [
    "APPROVAL_EXPIRES_AT_KEY",
    "APPROVAL_SCOPE_DIGEST_KEY",
    "DEFAULT_APPROVAL_TTL",
    "DELEGATION_DEPTH_KEY",
    "FAIL_SAFE_ESCALATION",
    "SEVERITY_RANK",
    "ActionRule",
    "ApprovalRequest",
    "ApprovalScope",
    "ApprovalService",
    "Approver",
    "BudgetRule",
    "CapabilityEvaluation",
    "CapabilityRule",
    "FindingRule",
    "Gate",
    "GateMode",
    "LocalApprovalService",
    "ModelRule",
    "PendingApproval",
    "Policy",
    "PolicyEngine",
    "RecordedApprovalScope",
    "RiskProfile",
    "ScopeClosure",
    "ScopeClosureReason",
    "ToolCapability",
    "UnknownApprovalError",
    "allows_execution",
    "auto_approve",
    "auto_reject",
    "decision_from_events",
    "evaluate_capability",
    "lifecycle_closure",
    "load_default_policy",
    "load_policy",
    "load_policy_stack",
    "needs_human",
    "pending_approvals",
    "policy_from_dict",
    "policy_sha256",
    "recorded_delegation_depth",
    "scope_closure",
]
