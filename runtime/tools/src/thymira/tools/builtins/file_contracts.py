"""Machine-checkable behavioral contracts declared by the workspace file tools (F3.6).

Every field names one fact ``tests/thymira/test_tools_contracts.py`` drives the tool to
demonstrate in a real ``tmp_path`` workspace rather than trusts from a docstring: paging, total
counts, bounded output, the discovery-artifact reference, workspace mutation, the read-before-
edit requirement, line numbering, and the absence of code execution or network access (the last
two checked by walking the module's AST, not by trusting the declared field). A file tool
registered in :mod:`thymira.tools.builtins.files` or :mod:`thymira.tools.builtins.search` without
a ``contract`` attribute fails that registry test, so a future tool cannot ship a dishonest one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class FileToolContract:
    """What one file tool truthfully does."""

    line_numbering: Literal["none"]
    """Whether returned lines carry a synthetic "N\\t"-style prefix. Always ``"none"`` here: a
    prefix would leak into content an agent copies verbatim into ``write_file``, and every
    existing consumer of a file tool's output already expects unprefixed lines."""
    paging: bool
    """Whether the tool accepts ``offset``/``limit`` and returns a bounded window."""
    reports_total: bool
    """Whether the rendered result names how many lines/matches/paths exist in total."""
    bounded_output: bool
    """Whether the tool enforces the shared line/character/byte limits
    (:mod:`thymira.tools.builtins.output_bounds`) and records a truncation fact rather than
    cutting silently."""
    discovery_artifact: bool
    """Whether the tool persists its complete ordered result list through the ``ArtifactStore``
    and references it in ``ToolResult.artifact_ids``."""
    mutates_workspace: bool
    """Whether executing the tool changes files on disk."""
    requires_prior_read: bool
    """Whether the freshness policy can refuse this tool when its target was not read first."""
    executes_code: bool = False
    """Always ``False`` for the file tools: none of them spawns a subprocess or interpreter."""
    network: bool = False
    """Always ``False`` for the file tools: none of them opens a socket."""
    concurrency: Literal["unsafe"] = "unsafe"
    """No locking is taken: two concurrent writers to the same path race. Declared truthfully
    rather than proved -- see the Agent Note for the retained limitation."""


__all__ = ["FileToolContract"]
