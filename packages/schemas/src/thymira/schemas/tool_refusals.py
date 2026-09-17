"""Pure refusal vocabulary for execution constraints and their independent audit.

The Tool Manager decides which refusal applies at execution time. MIRA imports these pure
functions and constants from the contract layer without loading the tools runtime.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from thymira.schemas import ExecutionConstraints


HUMAN_REVIEW_REFUSAL = "execution constraints require human review before any tool call"
"""Historical blanket refusal for ``ExecutionConstraints.requires_human_review``.

Current calls use a separate Gate review and one-shot approval. MIRA retains this vocabulary
to verify the exact refusal written by earlier runtimes."""

MISSING_EVIDENCE_REFUSAL = "required execution evidence is unavailable"
"""``ExecutionConstraints.required_evidence``'s refusal, produced by the manager's runtime check
when the Run's recorded evidence does not cover what the constraints demand. Shared with MIRA so
the recomputed refusal is the exact sentence the manager wrote."""

TOOL_CALL_LIMIT_REFUSAL = "execution constraints reached the maximum number of tool calls"
"""``ExecutionConstraints.max_tool_calls``'s refusal, produced once the manager's own call count
for the Run reaches the recorded ceiling. Shared with MIRA so the recomputed refusal is the exact
sentence the manager wrote."""

AGENT_ALLOWLIST_REFUSAL = " is not allowed to call tool "
"""The middle clause of :func:`agent_allowlist_refusal`: the caller-owned ``allowed_tools``
allowlist refusing a tool before the Gate is ever asked (``ToolManager._allowlist_error``).
Shared with MIRA -- via :func:`agent_allowlist_refusal` -- so the recomputed refusal is the exact
sentence the manager wrote."""


def agent_allowlist_refusal(agent_id: str, tool: str) -> str:
    """Describe the caller allowlist refusing one tool before the Gate is asked."""
    return f"agent {agent_id!r}{AGENT_ALLOWLIST_REFUSAL}{tool!r}"


FS_READ_REQUIRED = "FS_READ_REQUIRED"
"""Code for :func:`read_required_refusal`: a write/edit target this run has never read."""

FS_STALE_VERSION = "FS_STALE_VERSION"
"""Code for :func:`stale_version_refusal`: a write/edit target changed since this run last read
it."""

FS_NO_MATCH = "FS_NO_MATCH"
"""Code for :func:`no_match_refusal`: a literal edit's ``old_string`` matched zero times."""

FS_AMBIGUOUS_MATCH = "FS_AMBIGUOUS_MATCH"
"""Code for :func:`ambiguous_match_refusal`: a literal edit's ``old_string`` matched more than
once and ``replace_all`` was not set."""


def read_required_refusal(path: str) -> str:
    """Describe the freshness policy refusing a write/edit this run has never read."""
    return (
        f"{FS_READ_REQUIRED}: {path!r} has not been read in this run — read it with read_file "
        "first, then retry"
    )


def stale_version_refusal(path: str) -> str:
    """Describe the freshness policy refusing a write/edit whose read content is stale."""
    return (
        f"{FS_STALE_VERSION}: {path!r} changed on disk since it was last read in this run — "
        "read it again with read_file, then retry"
    )


def no_match_refusal(path: str) -> str:
    """Describe a literal edit whose ``old_string`` was not found."""
    return (
        f"{FS_NO_MATCH}: old_string was not found in {path!r} — read the file, then retry "
        "with a matching old_string"
    )


def ambiguous_match_refusal(path: str, count: int) -> str:
    """Describe a literal edit whose ``old_string`` matched more than once."""
    return (
        f"{FS_AMBIGUOUS_MATCH}: old_string appears {count} times in {path!r} — provide a more "
        "specific old_string or set replace_all to true"
    )


def constraint_refusals(constraints: ExecutionConstraints, tool: str) -> frozenset[str]:
    """Return every refusal the recorded constraints can produce for this tool.

    Event evidence does not describe the tool's effects, so both local and effectful
    possibilities are recomputed. Dynamic call counts and available evidence remain runtime
    checks; a non-None ceiling includes zero.
    """
    refusals = {
        reason
        for effects in ((), ("external",))
        if (reason := constraints.denies_tool(tool, effects)) is not None
    }
    if constraints.requires_human_review:
        refusals.add(HUMAN_REVIEW_REFUSAL)
    if constraints.required_evidence:
        refusals.add(MISSING_EVIDENCE_REFUSAL)
    if constraints.max_tool_calls is not None:
        refusals.add(TOOL_CALL_LIMIT_REFUSAL)
    return frozenset(refusals)
