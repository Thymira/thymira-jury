"""Per-tool guidance paragraphs for the system prompt (harness basics 1).

Each paragraph tells the model when to use a tool, what its result looks like and the mistake to
avoid, in the style of a coding harness's system prompt. The registry is consulted so an unknown
name fails loudly, the same contract `build_agent_tools` enforces; a registered tool with no
paragraph simply contributes nothing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from thymira.tools.registry import ToolRegistry

GUIDANCE: dict[str, str] = {
    "read_file": (
        "Use the read_file tool — not run_python with open() — to inspect text files. Use "
        "offset and limit to continue reading a large file; very long lines and very large "
        "windows are bounded, with the cut recorded in the trailing bracket."
    ),
    "write_file": (
        "Use the write_file tool to create files or completely replace their contents. "
        "Overwriting an existing file requires reading it first in this run — an unread or "
        "stale target is refused with FS_READ_REQUIRED or FS_STALE_VERSION. For a change, read "
        "the current file first and provide its complete replacement contents."
    ),
    "edit_file": (
        "Use the edit_file tool for targeted changes to existing text files. Read the file "
        "first in this run — an unread or stale target is refused with FS_READ_REQUIRED or "
        "FS_STALE_VERSION. It replaces literal old_string with new_string; by default "
        "old_string must appear exactly once. If it appears several times, provide a more "
        "specific old_string or set replace_all to true."
    ),
    "glob": (
        "Use the glob tool — not run_python with os.walk — to discover files by path pattern. A "
        'pattern with no "/" matches basenames at any depth. Results are files only, never '
        "directories, ordered by path (never by modification time); a capped rendering still "
        "names the true total and a discovery artifact holding the complete list."
    ),
    "grep": (
        "Use the grep tool — not run_python — to search file contents, ordered by path then "
        "line number. A capped rendering still names the true total and a discovery artifact "
        "holding the complete list. Use read_file on a matched file when you need surrounding "
        "context."
    ),
    "run_python": (
        "Check the [exit code: N] marker on every run_python result; investigate a failure "
        "before moving on. Each call runs in a fresh interpreter: write files for anything the "
        "next call needs, and keep printed output short — long output is truncated."
    ),
    "profile_dataset": (
        "Use the profile_dataset tool to understand a registered dataset before modelling; do "
        "not read the raw file with read_file. Registered datasets are listed in the runtime "
        "context."
    ),
    "run_experiment": (
        "Use the run_experiment tool to train a baseline on a registered dataset; report only "
        "the metrics it returns, never a number you have not seen in its output."
    ),
    "export_pdf": (
        "Use the export_pdf tool to compile a Markdown file already in the workspace, and any "
        "image it embeds, into a PDF. Write the Markdown (and its plots) with write_file/"
        "run_python first — export_pdf reads the finished source back; it does not accept "
        'inline content. It always registers the result as a kind="report" Artifact.'
    ),
    "run_notebook": (
        "Use the run_notebook tool only when the task asks for a Jupyter notebook deliverable: "
        "write the .ipynb JSON with write_file first, then run_notebook executes it top to "
        "bottom in a fresh kernel and registers the executed copy (cell outputs inline) as a "
        'kind="code" Artifact. Its result text {"status": "executed", ...} is '
        "final: the deliverable is registered, so do not validate, rewrite or re-run the "
        "notebook afterwards. A failing cell stops the run; fix the notebook before running "
        "it again."
    ),
}


def tool_guidance(registry: ToolRegistry, names: Sequence[str]) -> tuple[str, ...]:
    """Return the guidance paragraphs for ``names``, in order, skipping tools without one.

    Args:
        registry: The tool registry, consulted so an unknown name fails loudly.
        names: The tool names to render guidance for, in order.

    Returns:
        One paragraph per named tool that has one, in the order given.

    Raises:
        KeyError: A name the registry does not hold.
    """
    paragraphs: list[str] = []
    for name in names:
        registry.get(name)
        paragraph = GUIDANCE.get(name)
        if paragraph is not None:
            paragraphs.append(paragraph)
    return tuple(paragraphs)


__all__ = ["GUIDANCE", "tool_guidance"]
