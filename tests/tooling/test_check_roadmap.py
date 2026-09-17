from __future__ import annotations

from typing import TYPE_CHECKING

from tests.tooling.conftest import load_script

if TYPE_CHECKING:
    from pathlib import Path

FINAL = """# Roadmap — the final product

**2 tasks — 1 MVP, 1 FINAL.**
**Status: 1 done, 0 partial, 1 todo.**

## Summary

| Task | Title | Tier | Owner | Size | Status | Depends on |
|---|---|---|---|---|---|---|
| `A-01` | First task | **MVP** | P1 | M | done | — |
| `B-01` | Second task | **FINAL** | P2 | S | todo | `A-01` |

## Tasks by domain

### Demo domain

*2 tasks — 1 MVP, 1 FINAL.*

#### `A-01` — First task

- **Tier** MVP · **Owner** P1 (Runtime) · **Size** M · **Member path** `runtime/core`
- **Depends on** none
- **Status** done
- **Done when** it is done.

#### `B-01` — Second task

- **Tier** FINAL · **Owner** P2 (THY) · **Size** S · **Member path** `runtime/thy`
- **Depends on** `A-01`
- **Status** todo
- **Done when** it is done.
"""

MVP = """# Roadmap — the minimum MVP

**1 of 2 tasks · 2 points** (S=1, M=2, L=4).
**Status: 1 done, 0 partial, 0 todo.**

## Load per member

| Member | Role | MVP tasks | Points | vs mean |
|---|---|---|---|---|
| **P1** | Runtime | 1 | 2 | +0% |

## The four acceptance gates

| Gate | Owner | Proves |
|---|---|---|
| `A-01` | P1 | That non-task tables are not read as tasks |

## MVP tasks by member

### P1 — Runtime

*1 tasks, 2 points.*

| Task | Title | Tier | Owner | Size | Status | Depends on |
|---|---|---|---|---|---|---|
| `A-01` | First task | **MVP** | P1 | M | done | — |
"""


def write_pair(root: Path, final: str = FINAL, mvp: str = MVP) -> tuple[Path, Path]:
    final_doc = root / "product-final.md"
    mvp_doc = root / "mvp-minimum.md"
    final_doc.write_text(final, encoding="utf-8", newline="\n")
    mvp_doc.write_text(mvp, encoding="utf-8", newline="\n")
    return final_doc, mvp_doc


def check(root: Path, final: str = FINAL, mvp: str = MVP) -> list[str]:
    module = load_script("scripts/check_roadmap.py")
    return module.check(*write_pair(root, final, mvp))


def test_consistent_documents_have_no_problems(tmp_path: Path) -> None:
    assert check(tmp_path) == []


def test_the_real_roadmap_documents_agree() -> None:
    """The shipped documents must pass; this is the check `just check-roadmap` runs."""
    module = load_script("scripts/check_roadmap.py")
    assert module.check(module.FINAL_DOC, module.MVP_DOC) == []


def test_a_summary_row_that_contradicts_its_detail_section_is_caught(tmp_path: Path) -> None:
    problems = check(
        tmp_path,
        final=FINAL.replace(
            "| `A-01` | First task | **MVP** | P1 | M", "| `A-01` | First task | **MVP** | P4 | M"
        ),
    )
    assert any("owner" in problem and "A-01" in problem for problem in problems)


def test_a_stale_status_in_one_view_is_caught(tmp_path: Path) -> None:
    """The whole point: a task marked done in one document and todo in another."""
    problems = check(tmp_path, mvp=MVP.replace("| M | done |", "| M | todo |"))
    assert any("status" in problem and "A-01" in problem for problem in problems)


def test_an_mvp_task_missing_from_the_member_tables_is_caught(tmp_path: Path) -> None:
    stripped = MVP.replace("| `A-01` | First task | **MVP** | P1 | M | done | — |\n", "")
    stripped = stripped.replace("*1 tasks, 2 points.*", "*0 tasks, 0 points.*")
    stripped = stripped.replace("**1 of 2 tasks · 2 points**", "**0 of 2 tasks · 0 points**")
    problems = check(tmp_path, mvp=stripped)
    assert any("A-01" in problem and "member table" in problem for problem in problems)


def test_declared_counts_that_do_not_match_reality_are_caught(tmp_path: Path) -> None:
    problems = check(
        tmp_path,
        final=FINAL.replace("**2 tasks — 1 MVP, 1 FINAL.**", "**9 tasks — 5 MVP, 4 FINAL.**"),
    )
    assert any("header declares" in problem for problem in problems)


def test_a_member_point_total_that_does_not_match_is_caught(tmp_path: Path) -> None:
    problems = check(tmp_path, mvp=MVP.replace("*1 tasks, 2 points.*", "*1 tasks, 7 points.*"))
    assert any("points" in problem and "P1" in problem for problem in problems)


def test_a_dependency_on_a_task_that_does_not_exist_is_caught(tmp_path: Path) -> None:
    problems = check(
        tmp_path, final=FINAL.replace("- **Depends on** `A-01`", "- **Depends on** `Z-99`")
    )
    assert any("Z-99" in problem for problem in problems)


def test_an_mvp_task_depending_on_final_work_is_caught(tmp_path: Path) -> None:
    """An MVP task blocked by a FINAL task cannot be built during the MVP."""
    swapped = FINAL.replace("- **Depends on** none", "- **Depends on** `B-01`").replace(
        "| `A-01` | First task | **MVP** | P1 | M | done | — |",
        "| `A-01` | First task | **MVP** | P1 | M | done | `B-01` |",
    )
    problems = check(tmp_path, final=swapped)
    assert any("is MVP but depends on" in problem for problem in problems)


def test_a_dependency_cycle_is_caught(tmp_path: Path) -> None:
    cyclic = (
        FINAL.replace("- **Depends on** none", "- **Depends on** `B-01`")
        .replace(
            "| `A-01` | First task | **MVP** | P1 | M | done | — |",
            "| `A-01` | First task | **FINAL** | P1 | M | done | `B-01` |",
        )
        .replace("- **Tier** MVP · **Owner** P1", "- **Tier** FINAL · **Owner** P1")
    )
    cyclic = cyclic.replace("**2 tasks — 1 MVP, 1 FINAL.**", "**2 tasks — 0 MVP, 2 FINAL.**")
    cyclic = cyclic.replace("*2 tasks — 1 MVP, 1 FINAL.*", "*2 tasks — 0 MVP, 2 FINAL.*")
    problems = check(tmp_path, final=cyclic)
    assert any("dependency cycle" in problem for problem in problems)


def test_an_unknown_status_value_is_caught(tmp_path: Path) -> None:
    problems = check(tmp_path, final=FINAL.replace("- **Status** done", "- **Status** nearly"))
    assert any("is not one of" in problem for problem in problems)


def test_a_duplicated_task_id_is_caught(tmp_path: Path) -> None:
    doubled = FINAL.replace(
        "| `B-01` | Second task | **FINAL** | P2 | S | todo | `A-01` |",
        "| `B-01` | Second task | **FINAL** | P2 | S | todo | `A-01` |\n"
        "| `A-01` | First task | **MVP** | P1 | M | done | — |",
    )
    problems = check(tmp_path, final=doubled)
    assert any("more than once" in problem for problem in problems)


def test_a_stale_aggregate_status_line_is_caught(tmp_path: Path) -> None:
    """The counted totals are the first thing to rot; the script recounts them."""
    problems = check(
        tmp_path,
        final=FINAL.replace(
            "**Status: 1 done, 0 partial, 1 todo.**", "**Status: 2 done, 0 partial, 0 todo.**"
        ),
    )
    assert any("status line declares" in problem for problem in problems)


def test_a_stale_load_table_row_is_caught(tmp_path: Path) -> None:
    """The most derived number in the roadmap; nothing else recomputes it."""
    problems = check(
        tmp_path,
        mvp=MVP.replace("| **P1** | Runtime | 1 | 2 | +0% |", "| **P1** | Runtime | 4 | 9 | +0% |"),
    )
    assert any("load table gives P1" in problem for problem in problems)


def test_a_load_table_percentage_that_does_not_follow_is_caught(tmp_path: Path) -> None:
    problems = check(
        tmp_path,
        mvp=MVP.replace(
            "| **P1** | Runtime | 1 | 2 | +0% |", "| **P1** | Runtime | 1 | 2 | +40% |"
        ),
    )
    assert any("vs the mean" in problem for problem in problems)


def test_a_domain_whose_count_line_is_unreadable_is_reported_not_skipped(tmp_path: Path) -> None:
    """A gate's worst failure is the silent one: an unparsed line must not drop its domain."""
    problems = check(
        tmp_path,
        final=FINAL.replace("*2 tasks — 1 MVP, 1 FINAL.*", "*2 tasks - 1 MVP, 1 FINAL*"),
    )
    assert any("has no readable" in problem and "Demo domain" in problem for problem in problems)


def test_a_heading_outside_the_task_catalogue_is_not_read_as_a_domain(tmp_path: Path) -> None:
    """Prose sections may use `###` freely; only `## Tasks by domain` holds domains."""
    with_prose = FINAL.replace(
        "## Tasks by domain",
        "## Notes\n\n### Still open — someone's call\n\nProse, no tasks.\n\n## Tasks by domain",
    )
    assert check(tmp_path, final=with_prose) == []


def test_a_detail_heading_outside_the_catalogue_does_not_shift_domain_pairing() -> None:
    """The reported defect: a stray detail heading before the catalogue shifted `details[index]`.

    Positional pairing counted the wrong task for every heading after the stray one, fabricating a
    mismatch for a domain whose declared counts were in fact correct. Pairing by id must count the
    real tasks, and the stray section must be reported for itself rather than corrupting the count.
    """
    module = load_script("scripts/check_roadmap.py")
    task = module.Task
    stray = task("X-99", "Stray", "MVP", "P3", "S", (), "todo", "product-final.md:3")
    first = task("A-01", "First task", "MVP", "P1", "M", (), "done", "product-final.md:30")
    second = task("B-01", "Second task", "FINAL", "P2", "S", (), "todo", "product-final.md:35")
    details = [stray, first, second]  # document order: the stray section parses first
    lines = [
        "## Notes",
        "",
        "#### `X-99` — Stray",  # outside `## Tasks by domain`, so no domain claims it
        "",
        "## Tasks by domain",
        "",
        "### Demo domain",
        "",
        "*2 tasks — 1 MVP, 1 FINAL.*",  # the true count of A-01 (MVP) + B-01 (FINAL)
        "",
        "#### `A-01` — First task",
        "#### `B-01` — Second task",
    ]
    problems: list[str] = []
    module._check_domain_counts(lines, details, problems)
    assert not any("Demo domain" in problem and "declares" in problem for problem in problems)
    assert any("X-99" in problem for problem in problems)
