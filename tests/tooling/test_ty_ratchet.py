from __future__ import annotations

import json

from tests.tooling.conftest import load_script

SAMPLE = """\
runtime/core/src/thymira/core/a.py:12:5: error[invalid-return-type] Return type does not match
runtime/core/src/thymira/core/a.py:40:9: warning[possibly-unresolved-reference] Name `x` unbound
runtime/core/src/thymira/core/b.py:3:1: error[invalid-return-type] Return type does not match
Found 3 diagnostics
"""

# ty could not run at all: a crash, a missing dependency, a usage error. It exits non-zero and
# prints text that parses into no diagnostics and no summary line.
CRASH = "error: unrecognized subcommand 'check'\nno diagnostics were produced\n"


def test_parse_concise_counts_per_rule():
    ratchet = load_script("scripts/ty_ratchet.py")
    counts = ratchet.parse_concise(SAMPLE)
    assert counts == {"invalid-return-type": 2, "possibly-unresolved-reference": 1}


def test_parse_concise_falls_back_to_summary_line():
    ratchet = load_script("scripts/ty_ratchet.py")
    assert ratchet.parse_concise("Found 7 diagnostics\n") == {"<unparsed>": 7}


def test_compare_fails_on_increase_and_passes_on_decrease():
    ratchet = load_script("scripts/ty_ratchet.py")
    baseline = {"total": 2, "by_rule": {"invalid-return-type": 2}}
    ok, _ = ratchet.compare({"invalid-return-type": 3}, baseline, strict=False)
    assert ok is False
    ok, _ = ratchet.compare({"invalid-return-type": 1}, baseline, strict=False)
    assert ok is True


def test_compare_strict_fails_when_baseline_is_stale():
    ratchet = load_script("scripts/ty_ratchet.py")
    baseline = {"total": 2, "by_rule": {"invalid-return-type": 2}}
    ok, message = ratchet.compare({"invalid-return-type": 1}, baseline, strict=True)
    assert ok is False
    assert "--update-baseline" in message


def test_main_update_baseline_writes_file(tmp_path, monkeypatch):
    ratchet = load_script("scripts/ty_ratchet.py")
    # ty exits non-zero when it finds diagnostics; the run still parsed, so it is a real result.
    monkeypatch.setattr(ratchet, "run_ty", lambda: (1, SAMPLE))
    baseline = tmp_path / "baseline.json"
    assert ratchet.main(["--baseline", str(baseline), "--update-baseline"]) == 0
    data = json.loads(baseline.read_text())
    assert data["total"] == 3
    assert data["by_rule"]["invalid-return-type"] == 2


def test_main_fails_when_ty_could_not_run(tmp_path, monkeypatch):
    """The reported defect: a non-zero exit with nothing parsed used to pass as 0 == baseline 0.

    A ratchet that reports green when ty never checked the code is worse than one that is loud, so
    an unparseable non-zero run must fail — including under a zero baseline where the miscount is
    invisible.
    """
    ratchet = load_script("scripts/ty_ratchet.py")
    monkeypatch.setattr(ratchet, "run_ty", lambda: (2, CRASH))
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps({"total": 0, "by_rule": {}}), encoding="utf-8")
    assert ratchet.main(["--baseline", str(baseline)]) == 1


def test_main_update_baseline_refuses_to_write_when_ty_could_not_run(tmp_path, monkeypatch):
    """A crashed run must not be frozen into a baseline of zero, silencing the next real run."""
    ratchet = load_script("scripts/ty_ratchet.py")
    monkeypatch.setattr(ratchet, "run_ty", lambda: (2, CRASH))
    baseline = tmp_path / "baseline.json"
    assert ratchet.main(["--baseline", str(baseline), "--update-baseline"]) == 1
    assert not baseline.exists()


def test_main_passes_when_ty_ran_clean(tmp_path, monkeypatch):
    """The guard must not over-correct: a clean run exits zero and stays green at baseline zero."""
    ratchet = load_script("scripts/ty_ratchet.py")
    monkeypatch.setattr(ratchet, "run_ty", lambda: (0, "All checks passed!\n"))
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps({"total": 0, "by_rule": {}}), encoding="utf-8")
    assert ratchet.main(["--baseline", str(baseline)]) == 0


def test_main_still_counts_diagnostics_when_ty_exits_nonzero_with_findings(tmp_path, monkeypatch):
    """Ty exits non-zero when it *finds* problems; those must still be counted and compared."""
    ratchet = load_script("scripts/ty_ratchet.py")
    monkeypatch.setattr(ratchet, "run_ty", lambda: (1, SAMPLE))
    matching = tmp_path / "match.json"
    matching.write_text(
        json.dumps({"total": 3, "by_rule": {"invalid-return-type": 2}}), encoding="utf-8"
    )
    assert ratchet.main(["--baseline", str(matching)]) == 0
    stricter = tmp_path / "stricter.json"
    stricter.write_text(json.dumps({"total": 2, "by_rule": {}}), encoding="utf-8")
    assert ratchet.main(["--baseline", str(stricter)]) == 1
