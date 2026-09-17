from __future__ import annotations

import zipfile
from pathlib import Path

from tests.tooling.conftest import load_script


def _make_wheel(dist: Path, name: str, version: str, resources: tuple[str, ...] = ()) -> Path:
    """Write a minimal but valid wheel zip named `{name}-{version}-py3-none-any.whl`."""
    path = dist / f"{name}-{version}-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as archive:
        for resource in resources:
            archive.writestr(resource, "")
    return path


def test_missing_resources_reports_only_absent_entries():
    check = load_script("scripts/check_wheel.py")
    for required in check.REQUIRED.values():
        assert check.missing_resources(list(required), required) == []
        assert check.missing_resources([], required) == list(required)


def test_check_dist_reports_missing_wheels(tmp_path):
    check = load_script("scripts/check_wheel.py")
    problems = check.check_dist(tmp_path)
    assert len(problems) == len(check.EXPECTED_WHEELS)
    assert "no wheel built for thymira_policies" in problems


def test_wheel_version_reads_the_version_field():
    check = load_script("scripts/check_wheel.py")
    assert check.wheel_version(Path("thymira_core-1.2.3-py3-none-any.whl")) == "1.2.3"
    assert check.wheel_version(Path("thymira_core-0.1.0-42-py3-none-any.whl")) == "0.1.0"
    assert check.wheel_version(Path("noseparators.whl")) is None


def test_check_dist_reports_a_member_wheel_whose_version_does_not_match_the_tag(tmp_path):
    """The reported defect: the release gated the non-publishing root, not the member wheels.

    Every member wheel that is actually released must carry the tag's version; one that does not
    must be named, and the wheels that do match must not be reported.
    """
    check = load_script("scripts/check_wheel.py")
    for name in check.EXPECTED_WHEELS:
        version = "9.9.9" if name == "thymira_schemas" else "0.1.0"
        _make_wheel(tmp_path, name, version, check.REQUIRED.get(name, ()))
    problems = check.check_dist(tmp_path, expect_version="0.1.0")
    assert any("thymira_schemas" in problem and "9.9.9" in problem for problem in problems)
    assert not any("thymira_events" in problem for problem in problems)


def test_check_dist_passes_when_every_member_wheel_matches_the_tag(tmp_path):
    """The version gate must not over-correct: a clean set at the tagged version passes."""
    check = load_script("scripts/check_wheel.py")
    for name in check.EXPECTED_WHEELS:
        _make_wheel(tmp_path, name, "0.1.0", check.REQUIRED.get(name, ()))
    assert check.check_dist(tmp_path, expect_version="0.1.0") == []
