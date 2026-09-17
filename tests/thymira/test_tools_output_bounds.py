"""Bounded rendering primitives shared by read_file, glob and grep (F3.1)."""

from __future__ import annotations

from thymira.tools.builtins.output_bounds import (
    MAX_LINE_CHARS,
    MAX_OUTPUT_BYTES,
    bound_line,
    bound_lines,
)


def test_bound_line_returns_a_line_at_exactly_the_bound_whole():
    text = "x" * MAX_LINE_CHARS

    rendered, truncated = bound_line(text)

    assert rendered == text
    assert truncated is False


def test_bound_line_cuts_one_over_the_bound_and_names_the_real_character_count():
    text = "x" * (MAX_LINE_CHARS + 1)

    rendered, truncated = bound_line(text)

    assert truncated is True
    assert rendered.startswith("x" * MAX_LINE_CHARS)
    assert str(MAX_LINE_CHARS + 1) in rendered
    assert len(rendered) > MAX_LINE_CHARS


def test_bound_line_never_splits_a_multi_byte_character():
    # U+1F600 GRINNING FACE is a 4-byte UTF-8 sequence. Well over the bound, so the cut lands
    # mid-string; str slicing still lands on a code-point boundary, never inside the sequence.
    text = "\U0001f600" * (MAX_LINE_CHARS + 500)

    rendered, truncated = bound_line(text)

    assert truncated is True
    encoded = rendered.encode("utf-8")
    assert encoded.decode("utf-8") == rendered
    assert "�" not in rendered


def test_a_byte_budget_landing_inside_a_multi_byte_line_drops_the_whole_line():
    # Fill the budget with character-capped ASCII lines up to a 2-byte remainder -- too small to
    # fit even one 4-byte character -- then offer a multi-byte line. It must be dropped whole
    # rather than sliced; the byte budget never splits a line.
    target = MAX_OUTPUT_BYTES - 2
    filler_lines: list[str] = []
    used = 0
    while used < target:
        remaining = target - used
        sep_cost = 1 if filler_lines else 0
        chunk_len = min(MAX_LINE_CHARS, remaining - sep_cost)
        if chunk_len <= 0:
            break
        filler_lines.append("a" * chunk_len)
        used += chunk_len + sep_cost
    assert used == target
    emoji_line = "\U0001f600" * 5  # 20 bytes; cannot fit in the 2-byte remainder

    rendered = bound_lines([*filler_lines, emoji_line], max_bytes=MAX_OUTPUT_BYTES)

    assert rendered.rendered_count == len(filler_lines)
    assert rendered.byte_bounded is True
    assert rendered.text == "\n".join(filler_lines)
    # No partial emoji bytes anywhere in the rendered text.
    rendered.text.encode("utf-8").decode("utf-8")


def test_bound_lines_reports_total_and_rendered_count_when_nothing_is_cut():
    rendered = bound_lines(["a", "b", "c"], max_bytes=MAX_OUTPUT_BYTES)

    assert rendered.text == "a\nb\nc"
    assert rendered.rendered_count == 3
    assert rendered.total == 3
    assert rendered.lines_truncated == 0
    assert rendered.byte_bounded is False


def test_bound_lines_counts_only_rendered_lines_as_truncated():
    long_line = "x" * (MAX_LINE_CHARS + 10)

    rendered = bound_lines([long_line, "short"], max_bytes=MAX_OUTPUT_BYTES)

    assert rendered.lines_truncated == 1
    assert rendered.rendered_count == 2


def test_the_worst_case_single_line_always_fits_the_output_budget():
    """Structural invariant: bound_lines never returns an empty rendering for non-empty input.

    The worst single bounded line is MAX_LINE_CHARS four-byte characters (the largest a
    character-capped line can be in bytes) plus the ASCII marker bound_line appends when it
    cuts -- and that whole line must still fit inside MAX_OUTPUT_BYTES, or a single oversized
    line could make bound_lines return nothing for a non-empty input.
    """
    worst_case_input = "\U0001f600" * (MAX_LINE_CHARS + 1)
    rendered_line, truncated = bound_line(worst_case_input)
    assert truncated is True
    worst_case_bytes = len(rendered_line.encode("utf-8"))

    assert worst_case_bytes <= MAX_OUTPUT_BYTES

    # And the property it exists to guarantee: a single worst-case line is never dropped.
    result = bound_lines([worst_case_input], max_bytes=MAX_OUTPUT_BYTES)
    assert result.rendered_count == 1
    assert result.text != ""


def test_bound_lines_returns_empty_render_for_empty_input():
    rendered = bound_lines([], max_bytes=MAX_OUTPUT_BYTES)

    assert rendered.text == ""
    assert rendered.rendered_count == 0
    assert rendered.total == 0
    assert rendered.byte_bounded is False
