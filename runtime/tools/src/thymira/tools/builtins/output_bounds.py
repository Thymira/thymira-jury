"""Shared line/character/byte bounds for the workspace file tools (F3.1).

Three numbers, enforced at the producing tool rather than left to the transport:

* ``MAX_LINE_CHARS`` -- the most characters one rendered line may carry before it is cut.
* ``MAX_OUTPUT_BYTES`` -- the most UTF-8 bytes a bounded rendering (glob/grep/list_files) may
  carry. Deliberately half of :func:`thymira.tools.spill.spill_result`'s
  ``max_inline_bytes=64 * 1024``, so a bounded discovery rendering plus its footer never reaches
  the manager's spill path.
* ``MAX_READ_BYTES`` -- the most UTF-8 bytes a ``read_file`` window may carry.

Both helpers below cut by *character*, never by byte: slicing a ``str`` always lands on a
Unicode code-point boundary, so a line built of 4-byte characters (e.g. an emoji, U+1F600) can
never be cut mid-sequence -- the whole argument for capping by character instead of by raw bytes.
Every cut is a recorded fact (:class:`BoundedRender`), never a silent truncation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

MAX_LINE_CHARS = 2000
MAX_OUTPUT_BYTES = 32 * 1024
MAX_READ_BYTES = 256 * 1024
# Reserve space for the fact-complete footer each file tool appends after its bounded content.
# The footer is deliberately excluded from the rendered digest because it may contain an
# artifact id that is only known after the discovery artifact is persisted.
MAX_FOOTER_BYTES = 2048

# Structural invariant this module guarantees: the worst single bounded line -- MAX_LINE_CHARS
# four-byte characters, plus the ASCII marker appended when it is cut -- always fits inside
# MAX_OUTPUT_BYTES. That is what lets `bound_lines` promise it never returns an empty rendering
# for a non-empty input at this pairing of constants; see
# test_tools_output_bounds.py::test_the_worst_case_single_line_always_fits_the_output_budget
# for the computed proof.


def bound_line(text: str) -> tuple[str, bool]:
    """Cap one line to ``MAX_LINE_CHARS`` characters, returning whether it was cut.

    Args:
        text: The line to cap, without a trailing newline.

    Returns:
        ``(rendered, was_truncated)``. When ``text`` fits, ``rendered == text`` and
        ``was_truncated`` is ``False``. When it does not, ``rendered`` is the first
        ``MAX_LINE_CHARS`` characters plus an ASCII marker naming the real character count, and
        ``was_truncated`` is ``True``.
    """
    if len(text) <= MAX_LINE_CHARS:
        return text, False
    marker = f" [line truncated at {MAX_LINE_CHARS} characters; {len(text)} total]"
    return text[:MAX_LINE_CHARS] + marker, True


@dataclass(frozen=True, slots=True)
class BoundedRender:
    """The result of bounding an ordered list of lines by character and by byte."""

    text: str
    """The rendered lines actually kept, joined by ``"\\n"``. Never includes a trailing bracket
    footer -- callers append their own fact-complete summary on top of this text."""
    rendered_count: int
    """How many of ``lines`` made it into :attr:`text`."""
    total: int
    """How many lines were offered to :func:`bound_lines`."""
    lines_truncated: int
    """How many of the *rendered* lines were individually character-capped by :func:`bound_line`.

    Counts only lines that made it into :attr:`text`: a line dropped entirely by the byte budget
    was never shown, so it is not a "truncated line" in this sense."""
    byte_bounded: bool
    """Whether the byte budget stopped rendering before every line was included."""


def bound_lines(lines: Sequence[str], *, max_bytes: int) -> BoundedRender:
    r"""Character-cap then byte-budget an ordered list of lines.

    Each line is first passed through :func:`bound_line`. Lines are then accumulated in order
    while the running UTF-8 byte total (including the ``"\\n"`` join cost) stays within
    ``max_bytes``; the moment a line would not fit, accumulation stops -- the byte budget never
    splits a line to make it fit, it drops the whole line and every line after it, so the
    rendering stays a genuine prefix of ``lines``.
    """
    total = len(lines)
    kept: list[str] = []
    used_bytes = 0
    lines_truncated = 0
    byte_bounded = False
    for raw in lines:
        capped, was_cut = bound_line(raw)
        encoded_len = len(capped.encode("utf-8"))
        needed = encoded_len + (1 if kept else 0)  # the "\n" join separator
        if used_bytes + needed > max_bytes:
            byte_bounded = True
            break
        kept.append(capped)
        used_bytes += needed
        if was_cut:
            lines_truncated += 1
    return BoundedRender(
        text="\n".join(kept),
        rendered_count=len(kept),
        total=total,
        lines_truncated=lines_truncated,
        byte_bounded=byte_bounded,
    )


__all__ = [
    "MAX_FOOTER_BYTES",
    "MAX_LINE_CHARS",
    "MAX_OUTPUT_BYTES",
    "MAX_READ_BYTES",
    "BoundedRender",
    "bound_line",
    "bound_lines",
]
