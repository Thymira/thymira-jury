"""The policy-gated Tool Manager and the built-in tool surface.

Every tool call goes through `ToolManager`, the fail-closed authorization and lifecycle
boundary: it validates the arguments, asks the Policy Engine (`thymira.policies`) whether the
call is allowed, executes only what is authorized, spills oversized output to artifacts
(`spill_result`), and records a paired `tool.started`/`tool.completed` lifecycle. An unknown or
denied tool becomes a recorded failed `ToolCall`, never an exception. `ToolRegistry` holds the
registered tools; the `models` types (`Tool`, `ToolContext`, `ToolInvocation`, `ToolResult`,
`ToolExecution`, `ToolEvent`, `ToolExecutionError`, `BudgetRefusal`, `input_schema`) are the call
contract;
`Sandbox`/`LocalSubprocessSandbox` confine subprocess execution; and the dataset helpers
(`DatasetSchema`, `register_dataset`, `load_dataset`, `schema_artifact_name`,
`MAX_DATASET_BYTES`) register and load content-addressed datasets. The concrete built-in
tools -- `run_python`, file, git and git-worktree, MLflow and dataset tools -- live in
`thymira.tools.builtins`, and `thymira.tools.mcp` provides the MCP surface, the exposure
standard through which clients reach the manager. `tool_intent_sha256` names a call for a
human's one-shot approval; `unanswered` reports whether a decision still has no recorded
`human.approval` at all. `tool_guidance`
(`thymira.tools.guidance`) renders the per-tool "when to use" paragraphs a system prompt closes
with.

Owner (MVP roadmap): P3 (Tools / Execution / MLflow).
"""

from thymira.tools.approval_ticket import (
    TicketDisposition,
    TicketOutcome,
    ticket_disposition,
    tool_intent_sha256,
    unanswered,
)
from thymira.tools.artifact_media import infer_artifact_media_type
from thymira.tools.artifact_validation import (
    artifact_read_limit,
    resolve_artifact_media_type,
    validate_artifact_bytes,
)
from thymira.tools.datasets import (
    MAX_DATASET_BYTES,
    DatasetSchema,
    load_dataset,
    register_dataset,
    schema_artifact_name,
)
from thymira.tools.guidance import tool_guidance
from thymira.tools.manager import ToolManager
from thymira.tools.models import (
    BudgetRefusal,
    Tool,
    ToolContext,
    ToolEvent,
    ToolExecution,
    ToolExecutionError,
    ToolInvocation,
    ToolResult,
    ToolResultCode,
    input_schema,
)
from thymira.tools.refusals import (
    AGENT_ALLOWLIST_REFUSAL,
    FS_AMBIGUOUS_MATCH,
    FS_NO_MATCH,
    FS_READ_REQUIRED,
    FS_STALE_VERSION,
    HUMAN_REVIEW_REFUSAL,
    MISSING_EVIDENCE_REFUSAL,
    TOOL_CALL_LIMIT_REFUSAL,
    agent_allowlist_refusal,
    ambiguous_match_refusal,
    constraint_refusals,
    no_match_refusal,
    read_required_refusal,
    stale_version_refusal,
)
from thymira.tools.registry import ToolRegistry
from thymira.tools.results import (
    ComparisonValue,
    DatasetAnalysisValue,
    DiscoveryValue,
    ExperimentValue,
    FileEditValue,
    FileListingValue,
    FileReadValue,
    FileWriteValue,
    LegacyToolValue,
    MlflowMutationValue,
    ModelAuditValue,
    ModelInspectionValue,
    ProcessToolValue,
    ProfileValue,
    QueryRowsValue,
    RegulationSearchMatch,
    RegulationSearchValue,
    StatisticsValue,
    ToolFailureValue,
    ToolResultValidationError,
    ToolValue,
    TrackerRunsValue,
    TrackerRunValue,
    canonical_result,
    canonical_value,
    reconstruct_value,
    result_schema,
    value_text,
)
from thymira.tools.sandbox import LocalSubprocessSandbox, Sandbox, SandboxRun
from thymira.tools.spill import spill_result

__all__ = [
    "AGENT_ALLOWLIST_REFUSAL",
    "FS_AMBIGUOUS_MATCH",
    "FS_NO_MATCH",
    "FS_READ_REQUIRED",
    "FS_STALE_VERSION",
    "HUMAN_REVIEW_REFUSAL",
    "MAX_DATASET_BYTES",
    "MISSING_EVIDENCE_REFUSAL",
    "TOOL_CALL_LIMIT_REFUSAL",
    "BudgetRefusal",
    "ComparisonValue",
    "DatasetAnalysisValue",
    "DatasetSchema",
    "DiscoveryValue",
    "ExperimentValue",
    "FileEditValue",
    "FileListingValue",
    "FileReadValue",
    "FileWriteValue",
    "LegacyToolValue",
    "LocalSubprocessSandbox",
    "MlflowMutationValue",
    "ModelAuditValue",
    "ModelInspectionValue",
    "ProcessToolValue",
    "ProfileValue",
    "QueryRowsValue",
    "RegulationSearchMatch",
    "RegulationSearchValue",
    "Sandbox",
    "SandboxRun",
    "StatisticsValue",
    "TicketDisposition",
    "TicketOutcome",
    "Tool",
    "ToolContext",
    "ToolEvent",
    "ToolExecution",
    "ToolExecutionError",
    "ToolFailureValue",
    "ToolInvocation",
    "ToolManager",
    "ToolRegistry",
    "ToolResult",
    "ToolResultCode",
    "ToolResultValidationError",
    "ToolValue",
    "TrackerRunValue",
    "TrackerRunsValue",
    "agent_allowlist_refusal",
    "ambiguous_match_refusal",
    "artifact_read_limit",
    "canonical_result",
    "canonical_value",
    "constraint_refusals",
    "infer_artifact_media_type",
    "input_schema",
    "load_dataset",
    "no_match_refusal",
    "read_required_refusal",
    "reconstruct_value",
    "register_dataset",
    "resolve_artifact_media_type",
    "result_schema",
    "schema_artifact_name",
    "spill_result",
    "stale_version_refusal",
    "ticket_disposition",
    "tool_guidance",
    "tool_intent_sha256",
    "unanswered",
    "validate_artifact_bytes",
    "value_text",
]
