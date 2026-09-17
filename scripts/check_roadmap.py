"""Check that the two roadmap documents cannot disagree.

Every task is defined once in `docs/roadmap/product-final.md`, but its row is repeated in
three places: the summary table, the per-task detail section and — for MVP tasks — the
per-member table in `docs/roadmap/mvp-minimum.md`. Five engineers point their own AI
assistants at these documents, and an assistant does not notice a stale row: it implements
it. This script makes a disagreement a CI failure instead of a hope.

Checked: every task appears exactly once per view; title, tier, owner, size, dependencies
and status agree across all views; declared counts, the aggregate status line and the
load-per-member table match reality; every dependency exists;
no MVP task depends on a FINAL one; no dependency cycles. Exit code 1 on any mismatch.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import NamedTuple

REPO_ROOT = Path(__file__).resolve().parents[1]
FINAL_DOC = REPO_ROOT / "docs" / "roadmap" / "product-final.md"
MVP_DOC = REPO_ROOT / "docs" / "roadmap" / "mvp-minimum.md"

POINTS = {"S": 1, "M": 2, "L": 4}
TASK_HEADER = ["Task", "Title", "Tier", "Owner", "Size", "Status", "Depends on"]
STATUSES = ("done", "partial", "todo")
NO_DEPS = {"—", "-", "none", ""}

ROW_RE = re.compile(r"^\|\s*`(?P<id>[A-Z0-9-]+)`\s*\|(?P<rest>.*)\|\s*$")
DETAIL_HEAD_RE = re.compile(r"^#### `(?P<id>[A-Z0-9-]+)` — (?P<title>.+?)\s*$")
DETAIL_META_RE = re.compile(
    r"^- \*\*Tier\*\* (?P<tier>MVP|FINAL) · \*\*Owner\*\* (?P<owner>P\d)\b.*"
    r"· \*\*Size\*\* (?P<size>[SML]) ·"
)
DETAIL_FIELD_RE = re.compile(r"^- \*\*(?P<key>[A-Za-z ]+?)(?: \(.*?\))?\*\* (?P<value>.*)$")
DOMAIN_COUNT_RE = re.compile(
    r"^\*(?P<total>\d+) tasks — (?P<mvp>\d+) MVP, (?P<final>\d+) FINAL\.\*$"
)
HEADER_COUNT_RE = re.compile(
    r"^\*\*(?P<total>\d+) tasks — (?P<mvp>\d+) MVP, (?P<final>\d+) FINAL\.\*\*$", re.MULTILINE
)
MEMBER_COUNT_RE = re.compile(r"^\*(?P<tasks>\d+) tasks, (?P<points>\d+) points\.\*$")
MVP_HEADER_RE = re.compile(
    r"^\*\*(?P<mvp>\d+) of (?P<total>\d+) tasks · (?P<points>\d+) points\*\*"
)
MEMBER_HEAD_RE = re.compile(r"^### (?P<owner>P\d) — ")
LOAD_ROW_RE = re.compile(
    r"^\| \*\*(?P<owner>P\d)\*\* \| [^|]+ \| (?P<tasks>\d+) \| (?P<points>\d+) \|"
    r" (?P<pct>[+-]?\d+)% \|$",
    re.MULTILINE,
)
STATUS_LINE_RE = re.compile(
    r"^\*\*Status: (?P<done>\d+) done, (?P<partial>\d+) partial, (?P<todo>\d+) todo\.\*\*$",
    re.MULTILINE,
)
ID_RE = re.compile(r"`([A-Z0-9-]+)`")


class Task(NamedTuple):
    """One task row as it appears in a single view of the roadmap."""

    id: str
    title: str
    tier: str
    owner: str
    size: str
    depends: tuple[str, ...]
    status: str
    where: str


def _split_row(rest: str) -> list[str]:
    """Return the trimmed cells that follow the id cell of a table row."""
    return [cell.strip() for cell in rest.split("|")]


def _is_task_header(line: str) -> bool:
    """Return whether `line` is the header row of a task table."""
    return [cell.strip() for cell in line.strip().strip("|").split("|")] == TASK_HEADER


def _is_separator(line: str) -> bool:
    """Return whether `line` is a markdown table separator row."""
    return set(line.strip()) <= {"|", "-", ":", " "}


def _parse_deps(cell: str) -> tuple[str, ...]:
    """Return the dependency ids in a summary cell or a detail field."""
    if cell.strip().lower() in NO_DEPS:
        return ()
    return tuple(ID_RE.findall(cell))


def _parse_status(cell: str) -> str:
    """Return the normalized status recorded in a cell or field."""
    value = cell.strip().strip("*`").lower()
    return value if value in STATUSES else f"?{cell.strip()}"


def _parse_tier(cell: str) -> str:
    """Return the tier recorded in a summary cell (`**MVP**` or `**FINAL**`)."""
    return cell.strip().strip("*")


def parse_summary_rows(lines: list[str], *, where: str) -> list[Task]:
    """Parse every task row of every task table in `lines`.

    A task table is the one introduced by :data:`TASK_HEADER`; other tables in these documents
    (the acceptance gates, the load-per-member summary) also start their rows with an id and
    must not be read as tasks. Every task row has the shape
    ``| `ID` | Title | **Tier** | Owner | Size | Status | Depends on |``.
    """
    tasks: list[Task] = []
    in_task_table = False
    for number, line in enumerate(lines, start=1):
        if line.startswith("|"):
            if _is_task_header(line):
                in_task_table = True
                continue
            if line.startswith("| ") and not ROW_RE.match(line) and not _is_separator(line):
                in_task_table = False
        elif not line.strip():
            pass
        match = ROW_RE.match(line)
        if match is None or not in_task_table:
            continue
        cells = _split_row(match.group("rest"))
        if len(cells) < 6:
            msg = (
                f"{where}:{number}: task row `{match.group('id')}` has {len(cells)} cells, "
                f"expected 6 (title, tier, owner, size, status, depends on)"
            )
            raise ValueError(msg)
        tasks.append(
            Task(
                id=match.group("id"),
                title=cells[0],
                tier=_parse_tier(cells[1]),
                owner=cells[2],
                size=cells[3],
                depends=_parse_deps(cells[5]),
                status=_parse_status(cells[4]),
                where=f"{where}:{number}",
            )
        )
    return tasks


def parse_details(lines: list[str], *, where: str) -> list[Task]:
    """Parse every task detail section (a level-four heading) and its metadata lines."""
    tasks: list[Task] = []
    pending: dict[str, str] | None = None
    for number, line in enumerate(lines, start=1):
        head = DETAIL_HEAD_RE.match(line)
        if head is not None:
            if pending is not None:
                tasks.append(_finish_detail(pending))
            pending = {
                "id": head.group("id"),
                "title": head.group("title"),
                "where": f"{where}:{number}",
            }
            continue
        if pending is None:
            continue
        meta = DETAIL_META_RE.match(line)
        if meta is not None:
            pending |= {
                "tier": meta.group("tier"),
                "owner": meta.group("owner"),
                "size": meta.group("size"),
            }
            continue
        field = DETAIL_FIELD_RE.match(line)
        if field is not None:
            key = field.group("key").strip().lower()
            if key in {"depends on", "status"}:
                pending[key] = field.group("value")
    if pending is not None:
        tasks.append(_finish_detail(pending))
    return tasks


def _finish_detail(pending: dict[str, str]) -> Task:
    """Build a Task from a collected detail section, defaulting missing fields visibly."""
    return Task(
        id=pending["id"],
        title=pending["title"],
        tier=pending.get("tier", "?missing"),
        owner=pending.get("owner", "?missing"),
        size=pending.get("size", "?missing"),
        depends=_parse_deps(pending.get("depends on", "none")),
        status=_parse_status(pending.get("status", "?missing")),
        where=pending["where"],
    )


def _duplicates(tasks: list[Task]) -> list[str]:
    """Return ids appearing more than once in one view."""
    seen: dict[str, int] = {}
    for task in tasks:
        seen[task.id] = seen.get(task.id, 0) + 1
    return sorted(task_id for task_id, count in seen.items() if count > 1)


def _compare(left: Task, right: Task, problems: list[str]) -> None:
    """Record a problem for every field on which two views of one task disagree."""
    for field in ("title", "tier", "owner", "size", "status"):
        a, b = getattr(left, field), getattr(right, field)
        if a != b:
            problems.append(
                f"{left.id}: {field} is {a!r} at {left.where} but {b!r} at {right.where}"
            )
    if set(left.depends) != set(right.depends):
        problems.append(
            f"{left.id}: dependencies {sorted(left.depends)} at {left.where} "
            f"but {sorted(right.depends)} at {right.where}"
        )


def _check_counts(text: str, tasks: list[Task], problems: list[str]) -> None:
    """Check the declared totals in `product-final.md` against the parsed tasks."""
    mvp = [t for t in tasks if t.tier == "MVP"]
    header = HEADER_COUNT_RE.search(text)
    if header is None:
        problems.append("product-final.md: the '**N tasks — N MVP, N FINAL.**' header is missing")
    else:
        declared = (int(header["total"]), int(header["mvp"]), int(header["final"]))
        actual = (len(tasks), len(mvp), len(tasks) - len(mvp))
        if declared != actual:
            problems.append(
                f"product-final.md header declares {declared} but the catalogue has {actual}"
            )


def _check_domain_counts(lines: list[str], details: list[Task], problems: list[str]) -> None:
    r"""Check each `### Domain` heading's declared task counts against its detail sections.

    Detail sections are matched to their parsed task by id, never by position. The old positional
    pairing (`details[index]`) assumed the i-th detail heading under a domain was the i-th task in
    :func:`parse_details`, which scans the whole document: a `#### \`ID\`` heading appearing outside
    `## Tasks by domain` (or before the first `### Domain`) shifted every later pairing by one and
    fabricated mismatches for domains nobody had touched. Now each in-domain heading is looked up
    by its own id; a heading whose id has no parsed task, and a parsed task no domain claimed, are
    reported for themselves instead of corrupting the alignment.

    Only the headings under `## Tasks by domain` are domains. Prose elsewhere in the document is
    free to use `###` for its own subsections, and treating one of those as an empty domain would
    fail the gate over a heading that has nothing to do with the task catalogue.
    """
    by_id = {task.id: task for task in details}
    domain: str | None = None
    declared: dict[str, tuple[int, int, int]] = {}
    actual: dict[str, list[Task]] = {}
    assigned: set[str] = set()
    in_catalogue = False
    for line in lines:
        if line.startswith("## "):
            in_catalogue = line[3:].strip().casefold() == "tasks by domain"
            domain = None
            continue
        if not in_catalogue:
            continue
        if line.startswith("### "):
            domain = line[4:].strip()
            actual.setdefault(domain, [])
            continue
        count = DOMAIN_COUNT_RE.match(line)
        if count is not None and domain is not None:
            declared[domain] = (int(count["total"]), int(count["mvp"]), int(count["final"]))
            continue
        head = DETAIL_HEAD_RE.match(line)
        if head is not None and domain is not None:
            task = by_id.get(head["id"])
            if task is None:
                problems.append(
                    f"product-final.md: domain {domain!r} lists `{head['id']}`, "
                    "which has no parsed detail section"
                )
                continue
            actual[domain].append(task)
            assigned.add(task.id)
    # A detail section that no domain claimed used to shift the positional pairing silently; now it
    # is reported for itself. This is the failure mode a gate must not hide: a heading before the
    # first `### Domain`, or one outside `## Tasks by domain`, that the count check never sees.
    problems.extend(
        f"product-final.md: detail section `{task.id}` at {task.where} is under no domain in "
        "`## Tasks by domain`"
        for task in details
        if task.id not in assigned
    )
    # Iterate the domains that exist, not the declarations that parsed: a count line the regex
    # does not recognise (a missing period, a hyphen for the em dash) would otherwise drop its
    # whole domain out of this check silently, which is the one failure mode a gate must not have.
    for domain, found in actual.items():
        numbers = declared.get(domain)
        mvp = sum(1 for t in found if t.tier == "MVP")
        if numbers is None:
            problems.append(
                f"product-final.md: domain {domain!r} has no readable "
                f"`*N tasks — N MVP, N FINAL.*` line, so its {len(found)} task(s) go unchecked"
            )
        elif numbers != (len(found), mvp, len(found) - mvp):
            problems.append(
                f"product-final.md: domain {domain!r} declares {numbers} "
                f"but contains {(len(found), mvp, len(found) - mvp)}"
            )


def _check_member_counts(lines: list[str], problems: list[str]) -> None:
    """Check each `### PN — role` heading's declared task and point totals."""
    owner: str | None = None
    declared: dict[str, tuple[int, int]] = {}
    actual: dict[str, list[Task]] = {}
    in_task_table = False
    for number, line in enumerate(lines, start=1):
        head = MEMBER_HEAD_RE.match(line)
        if head is not None:
            owner = head.group("owner")
            actual.setdefault(owner, [])
            continue
        count = MEMBER_COUNT_RE.match(line)
        if count is not None and owner is not None:
            declared[owner] = (int(count["tasks"]), int(count["points"]))
            continue
        if _is_task_header(line):
            in_task_table = True
            continue
        if line.startswith("| ") and not ROW_RE.match(line) and not _is_separator(line):
            in_task_table = False
        row = ROW_RE.match(line)
        if row is not None and owner is not None and in_task_table:
            cells = _split_row(row.group("rest"))
            actual[owner].append(
                Task(
                    row.group("id"),
                    cells[0],
                    _parse_tier(cells[1]),
                    cells[2],
                    cells[3],
                    _parse_deps(cells[5]),
                    _parse_status(cells[4]),
                    f"mvp-minimum.md:{number}",
                )
            )
    for owner, (tasks_declared, points_declared) in declared.items():
        found = actual.get(owner, [])
        points = sum(POINTS.get(t.size, 0) for t in found)
        if (tasks_declared, points_declared) != (len(found), points):
            problems.append(
                f"mvp-minimum.md: {owner} declares {tasks_declared} tasks / "
                f"{points_declared} points but its table has {len(found)} / {points}"
            )
        if any(t.owner != owner for t in found):
            wrong = sorted(t.id for t in found if t.owner != owner)
            problems.append(
                f"mvp-minimum.md: tasks under {owner} are owned by someone else: {wrong}"
            )


def _check_dependencies(tasks: dict[str, Task], problems: list[str]) -> None:
    """Check dependencies exist, no MVP task waits on FINAL work, and there are no cycles."""
    for task in tasks.values():
        for dependency in task.depends:
            other = tasks.get(dependency)
            if other is None:
                problems.append(f"{task.id}: depends on `{dependency}`, which is not a task")
            elif task.tier == "MVP" and other.tier == "FINAL":
                problems.append(
                    f"{task.id} is MVP but depends on `{dependency}`, which is FINAL — "
                    f"an MVP task cannot wait on work the MVP does not build"
                )
    colour: dict[str, int] = {}

    def visit(node: str, trail: list[str]) -> None:
        colour[node] = 1
        for dependency in tasks.get(node, Task(node, "", "", "", "", (), "", "")).depends:
            if dependency not in tasks:
                continue
            if colour.get(dependency) == 1:
                cycle = " -> ".join([*trail, node, dependency])
                problems.append(f"dependency cycle: {cycle}")
            elif colour.get(dependency, 0) == 0:
                visit(dependency, [*trail, node])
        colour[node] = 2

    for task_id in tasks:
        if colour.get(task_id, 0) == 0:
            visit(task_id, [])


def _check_status_line(text: str, tasks: list[Task], where: str, problems: list[str]) -> None:
    """Check a declared `**Status: N done, N partial, N todo.**` line against the tasks."""
    header = STATUS_LINE_RE.search(text)
    if header is None:
        problems.append(f"{where}: the '**Status: N done, N partial, N todo.**' line is missing")
        return
    counted = {status: sum(1 for task in tasks if task.status == status) for status in STATUSES}
    declared = (int(header["done"]), int(header["partial"]), int(header["todo"]))
    actual = (counted["done"], counted["partial"], counted["todo"])
    if declared != actual:
        problems.append(
            f"{where}: the status line declares {declared} (done, partial, todo) "
            f"but the tasks are {actual}"
        )


def _check_load_table(text: str, member_rows: list[Task], problems: list[str]) -> None:
    """Check the load-per-member summary table against the per-member task tables.

    It is the roadmap's most derived number and nothing else recomputes it, so it is the first
    row to go stale when a task moves owner or leaves the catalogue.
    """
    rows = LOAD_ROW_RE.findall(text)
    if not rows:
        problems.append("mvp-minimum.md: the load-per-member table is missing or malformed")
        return
    total = sum(POINTS.get(task.size, 0) for task in member_rows)
    mean = total / len(rows) if rows else 0
    for owner, tasks_declared, points_declared, pct_declared in rows:
        owned = [task for task in member_rows if task.owner == owner]
        points = sum(POINTS.get(task.size, 0) for task in owned)
        if (int(tasks_declared), int(points_declared)) != (len(owned), points):
            problems.append(
                f"mvp-minimum.md: the load table gives {owner} "
                f"{tasks_declared} tasks / {points_declared} points, "
                f"but their table has {len(owned)} / {points}"
            )
            continue
        expected = round((points / mean - 1) * 100) if mean else 0
        if abs(int(pct_declared) - expected) > 1:
            problems.append(
                f"mvp-minimum.md: the load table says {owner} is {pct_declared}% vs the mean, "
                f"but {points} points against a mean of {mean:.1f} is {expected:+d}%"
            )


def check(final_doc: Path, mvp_doc: Path) -> list[str]:
    """Return every disagreement found between and inside the two roadmap documents."""
    problems: list[str] = []
    final_text = final_doc.read_text(encoding="utf-8")
    final_lines = final_text.splitlines()
    mvp_lines = mvp_doc.read_text(encoding="utf-8").splitlines()

    summary = parse_summary_rows(final_lines[: _summary_end(final_lines)], where="product-final.md")
    details = parse_details(final_lines, where="product-final.md")
    member_rows = parse_summary_rows(mvp_lines, where="mvp-minimum.md")

    for view, rows in (
        ("summary table", summary),
        ("detail sections", details),
        ("mvp-minimum", member_rows),
    ):
        problems.extend(
            f"`{duplicate}` appears more than once in the {view}" for duplicate in _duplicates(rows)
        )

    by_detail = {task.id: task for task in details}
    by_summary = {task.id: task for task in summary}
    problems.extend(
        f"`{task_id}` is in the summary table but has no detail section"
        for task_id in sorted(set(by_summary) - set(by_detail))
    )
    problems.extend(
        f"`{task_id}` has a detail section but is missing from the summary table"
        for task_id in sorted(set(by_detail) - set(by_summary))
    )
    for task_id in sorted(set(by_summary) & set(by_detail)):
        _compare(by_summary[task_id], by_detail[task_id], problems)

    problems.extend(
        f"{task.id}: status {task.status[1:]!r} at {task.where} is not one of {STATUSES}"
        for task in details
        if task.status.startswith("?")
    )

    mvp_ids = {task.id for task in details if task.tier == "MVP"}
    member_ids = {task.id for task in member_rows}
    problems.extend(
        f"`{task_id}` is MVP but appears in no member table in mvp-minimum.md"
        for task_id in sorted(mvp_ids - member_ids)
    )
    problems.extend(
        f"`{task_id}` is in mvp-minimum.md but is not an MVP task in the catalogue"
        for task_id in sorted(member_ids - mvp_ids)
    )
    for task in member_rows:
        counterpart = by_detail.get(task.id)
        if counterpart is not None:
            _compare(task, counterpart, problems)

    _check_counts(final_text, details, problems)
    _check_status_line(final_text, details, "product-final.md", problems)
    mvp_text = mvp_doc.read_text(encoding="utf-8")
    _check_status_line(mvp_text, member_rows, "mvp-minimum.md", problems)
    _check_load_table(mvp_text, member_rows, problems)
    _check_domain_counts(final_lines, details, problems)
    _check_member_counts(mvp_lines, problems)
    _check_mvp_header(mvp_lines, details, member_rows, problems)
    _check_dependencies(by_detail, problems)
    return problems


def _summary_end(lines: list[str]) -> int:
    """Return the index at which the summary table section ends."""
    for index, line in enumerate(lines):
        if line.startswith("## Tasks by domain"):
            return index
    return len(lines)


def _check_mvp_header(
    lines: list[str], details: list[Task], member_rows: list[Task], problems: list[str]
) -> None:
    """Check the `**N of N tasks · N points**` header in mvp-minimum.md."""
    for line in lines:
        header = MVP_HEADER_RE.match(line)
        if header is None:
            continue
        points = sum(POINTS.get(task.size, 0) for task in member_rows)
        declared = (int(header["mvp"]), int(header["total"]), int(header["points"]))
        actual = (len(member_rows), len(details), points)
        if declared != actual:
            problems.append(
                f"mvp-minimum.md header declares {declared} but the tables give {actual}"
            )
        return
    problems.append("mvp-minimum.md: the '**N of N tasks · N points**' header is missing")


def main(argv: list[str] | None = None) -> int:
    """Report every roadmap disagreement and return a process exit code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--final", type=Path, default=FINAL_DOC)
    parser.add_argument("--mvp", type=Path, default=MVP_DOC)
    args = parser.parse_args(argv)

    problems = check(args.final, args.mvp)
    if problems:
        print(f"roadmap: {len(problems)} disagreement(s)")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("roadmap: the two documents agree")
    return 0


if __name__ == "__main__":
    sys.exit(main())
