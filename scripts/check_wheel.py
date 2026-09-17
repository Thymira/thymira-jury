"""Assert that the built wheels ship the non-Python resources Thymira needs at runtime.

Run after ``uv build --all-packages``: every workspace member must produce a wheel and the
members that carry data files (YAML policies, prompts, …) must include them. Given
``--expect-version`` (the release tag without its leading ``v``), it also asserts every member
wheel's version equals the tag: the root ``pyproject.toml`` publishes nothing, so gating the tag
against it left the eleven member wheels — the artefacts actually released — unchecked.
"""

from __future__ import annotations

import argparse
import re
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# A wheel file name is ``{distribution}-{version}(-{build})?-{python}-{abi}-{platform}.whl``. The
# distribution is normalised to contain no ``-`` (hyphens become ``_``), so the first two
# ``-``-separated fields are the name and the version.
WHEEL_RE = re.compile(r"^(?P<name>[^-]+)-(?P<version>[^-]+)-")

EXPECTED_WHEELS: tuple[str, ...] = (
    "thymira_schemas",
    "thymira_events",
    "thymira_core",
    "thymira_state",
    "thymira_tools",
    "thymira_policies",
    "thymira_agents",
    "thymira_thy",
    "thymira_mira",
    "thymira_api",
    "thymira_cli",
    "thymira_web",
)

REQUIRED: dict[str, tuple[str, ...]] = {
    "thymira_policies": (
        "thymira/policies/defaults/base.yaml",
        "thymira/policies/defaults/credit_risk.yaml",
    ),
    "thymira_mira": (
        "thymira/mira/preflight/defaults/methodology-base.json",
        "thymira/mira/preflight/defaults/credit-governance.json",
        "thymira/mira/agents/defaults/methodology.yaml",
        "thymira/mira/agents/defaults/risk.yaml",
        "thymira/mira/agents/defaults/euaiact.yaml",
        "thymira/mira/kb/corpus/regulation_corpus.json",
    ),
    "thymira_web": (
        "thymira/web/static/index.html",
        "thymira/web/static/console.css",
        "thymira/web/static/js/main.js",
    ),
}


def missing_resources(names: list[str], required: tuple[str, ...]) -> list[str]:
    """Return the required resources that are absent from a wheel's file list."""
    present = set(names)
    return [resource for resource in required if resource not in present]


def wheel_version(wheel: Path) -> str | None:
    """Return the version field of a wheel file name, or ``None`` if it does not parse."""
    match = WHEEL_RE.match(wheel.name)
    return match["version"] if match is not None else None


def check_dist(dist: Path, expect_version: str | None = None) -> list[str]:
    """Return every problem found in ``dist`` (empty when all wheels are complete).

    With ``expect_version`` set, every member wheel's version must equal it: this is the release
    gate that the tag actually publishes, replacing the old assertion against the non-publishing
    virtual-root package's version.
    """
    problems: list[str] = []
    wheels = {wheel.name.split("-")[0]: wheel for wheel in sorted(dist.glob("*.whl"))}
    problems.extend(f"no wheel built for {name}" for name in EXPECTED_WHEELS if name not in wheels)
    if expect_version is not None:
        for name in EXPECTED_WHEELS:
            wheel = wheels.get(name)
            if wheel is None:
                continue  # already reported as a missing wheel above
            version = wheel_version(wheel)
            if version != expect_version:
                problems.append(
                    f"{wheel.name} has version {version!r}, expected {expect_version!r} "
                    "to match the release tag"
                )
    for name, required in REQUIRED.items():
        wheel = wheels.get(name)
        if wheel is None:
            continue
        missing = missing_resources(zipfile.ZipFile(wheel).namelist(), required)
        if missing:
            problems.append(f"{wheel.name} is missing: {', '.join(missing)}")
    return problems


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: 0 when every wheel is complete, 1 otherwise, 2 without wheels."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--expect-version",
        help="assert every member wheel's version equals this (the release tag, without a v)",
    )
    args = parser.parse_args(argv)
    dist = REPO_ROOT / "dist"
    if not any(dist.glob("*.whl")):
        print("No wheel found in dist/. Run: uv build --all-packages")
        return 2
    problems = check_dist(dist, expect_version=args.expect_version)
    for problem in problems:
        print(problem)
    if problems:
        return 1
    checked = f"{len(EXPECTED_WHEELS)} members checked in {dist}"
    if args.expect_version is not None:
        checked += f" at version {args.expect_version}"
    print(f"wheels OK: {checked}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
