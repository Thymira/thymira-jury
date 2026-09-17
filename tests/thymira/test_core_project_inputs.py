"""Unit tests for the project inputs an operator edits between Runs.

Real: ``ProjectInputs`` over a project directory under ``tmp_path``, the YAML configuration it
rewrites and the registration reader it checks uploads with. Nothing here starts a Run.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

import pytest
import yaml

from thymira.core import (
    ProjectInputConflictError,
    ProjectInputError,
    ProjectInputNotFoundError,
    ProjectInputs,
    ProjectInputTooLargeError,
)
from thymira.core import project_inputs as project_inputs_module
from thymira.schemas import ProjectConfig

if TYPE_CHECKING:
    from pathlib import Path

    from thymira.core import DatasetUploadResult

CONFIG = (
    "# Thymira project context (example).\n"
    "project:\n"
    "  name: credit-risk\n"
    "  domain: credit_risk\n"
    "\n"
    "datasets:\n"
    "  - name: german_credit\n"
    "    path: data/applications.csv\n"
    "    target: is_high_risk\n"
)
APPLICATIONS = "age,income,is_high_risk\n30,1000,0\n45,2000,1\n"
LOANS = "amount,term,defaulted\n500,12,0\n900,24,1\n700,36,0\n"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    (root / ".thymira").mkdir(parents=True)
    (root / ".thymira" / "config.yaml").write_text(CONFIG, encoding="utf-8", newline="\n")
    (root / "data").mkdir()
    (root / "data" / "applications.csv").write_text(APPLICATIONS, encoding="utf-8", newline="\n")
    return root


def _upload(
    inputs: ProjectInputs, name: str, filename: str, data: bytes, *, target: str | None = None
) -> DatasetUploadResult:
    upload = inputs.begin_upload(name, filename)
    try:
        upload.write(data)
        return upload.commit(target=target)
    finally:
        upload.abort()


def _config(project: Path) -> ProjectConfig:
    text = (project / ".thymira" / "config.yaml").read_text(encoding="utf-8")
    return ProjectConfig.model_validate(yaml.safe_load(text))


def test_read_context_reports_an_absent_document_with_the_empty_digest(project: Path) -> None:
    document = ProjectInputs(project).read_context()

    assert document.exists is False
    assert document.text == ""
    assert document.sha256 == hashlib.sha256(b"").hexdigest()


def test_write_context_replaces_the_document_when_the_digest_matches(project: Path) -> None:
    inputs = ProjectInputs(project)
    current = inputs.read_context()

    saved = inputs.write_context("Goal: score applicants.\n", expected_sha256=current.sha256)

    assert (project / ".thymira" / "context.md").read_bytes() == b"Goal: score applicants.\n"
    assert saved.sha256 == hashlib.sha256(b"Goal: score applicants.\n").hexdigest()
    assert inputs.read_context() == saved


def test_write_context_refuses_a_digest_that_is_no_longer_current(project: Path) -> None:
    inputs = ProjectInputs(project)
    stale = inputs.read_context().sha256
    (project / ".thymira" / "context.md").write_bytes(b"edited elsewhere\n")

    with pytest.raises(ProjectInputConflictError):
        inputs.write_context("my edit\n", expected_sha256=stale)

    assert (project / ".thymira" / "context.md").read_bytes() == b"edited elsewhere\n"


def test_write_context_refuses_text_carrying_an_export_redaction_mask(project: Path) -> None:
    inputs = ProjectInputs(project)

    with pytest.raises(ProjectInputError, match="redaction"):
        inputs.write_context(
            "Owner: [REDACTED:EMAIL]\n", expected_sha256=inputs.read_context().sha256
        )


def test_write_context_refuses_a_document_above_the_bound(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(project_inputs_module, "MAX_CONTEXT_BYTES", 8)
    inputs = ProjectInputs(project)

    with pytest.raises(ProjectInputTooLargeError):
        inputs.write_context(
            "longer than eight bytes", expected_sha256=inputs.read_context().sha256
        )


def test_upload_declares_a_new_dataset_under_data_and_keeps_the_config_header(
    project: Path,
) -> None:
    result = _upload(
        ProjectInputs(project), "loans", "loans export.csv", LOANS.encode(), target="defaulted"
    )

    assert (result.rows, result.columns) == (3, ("amount", "term", "defaulted"))
    assert (result.dataset.path, result.dataset.present) == ("data/loans.csv", True)
    assert (project / "data" / "loans.csv").read_bytes() == LOANS.encode()
    declared = {dataset.name: dataset for dataset in _config(project).datasets}
    assert declared["loans"].target == "defaulted"
    assert declared["german_credit"].path == "data/applications.csv"
    config_text = (project / ".thymira" / "config.yaml").read_text(encoding="utf-8")
    assert config_text.startswith("# Thymira project context (example).\n")


def test_upload_for_a_declared_dataset_replaces_its_file_in_place_and_keeps_its_target(
    project: Path,
) -> None:
    replacement = b"age,income,is_high_risk\n52,3100,1\n"

    result = _upload(ProjectInputs(project), "german_credit", "fresh.csv", replacement)

    assert (result.dataset.path, result.dataset.target) == ("data/applications.csv", "is_high_risk")
    assert (project / "data" / "applications.csv").read_bytes() == replacement
    assert [dataset.name for dataset in _config(project).datasets] == ["german_credit"]


@pytest.mark.parametrize("filename", ["notes.txt", "model.pkl", "data"])
def test_begin_upload_refuses_a_file_that_is_neither_csv_nor_parquet(
    project: Path, filename: str
) -> None:
    with pytest.raises(ProjectInputError, match="CSV or Parquet"):
        ProjectInputs(project).begin_upload("loans", filename)


@pytest.mark.parametrize("name", ["Loans", "../loans", "-loans", "loans.csv"])
def test_begin_upload_refuses_an_invalid_dataset_name(project: Path, name: str) -> None:
    with pytest.raises(ProjectInputError, match="dataset name"):
        ProjectInputs(project).begin_upload(name, "loans.csv")


def test_upload_refuses_a_ragged_csv_and_leaves_the_project_untouched(project: Path) -> None:
    before = (project / ".thymira" / "config.yaml").read_bytes()

    with pytest.raises(ProjectInputError, match="do not match"):
        _upload(ProjectInputs(project), "loans", "loans.csv", b"a,b\n1,2\n3\n")

    assert (project / ".thymira" / "config.yaml").read_bytes() == before
    assert sorted(path.name for path in (project / "data").iterdir()) == ["applications.csv"]


def test_upload_refuses_a_target_that_is_not_a_column(project: Path) -> None:
    with pytest.raises(ProjectInputError, match="not a column"):
        _upload(ProjectInputs(project), "loans", "loans.csv", LOANS.encode(), target="missing")


def test_upload_stops_once_the_size_bound_is_passed(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(project_inputs_module, "MAX_DATASET_UPLOAD_BYTES", 8)
    upload = ProjectInputs(project).begin_upload("loans", "loans.csv")
    try:
        with pytest.raises(ProjectInputTooLargeError):
            upload.write(LOANS.encode())
    finally:
        upload.abort()

    assert sorted(path.name for path in (project / "data").iterdir()) == ["applications.csv"]


def test_set_target_records_a_column_of_the_file(project: Path) -> None:
    updated = ProjectInputs(project).set_target("german_credit", "age")

    assert updated.target == "age"
    assert _config(project).datasets[0].target == "age"


def test_set_target_refuses_a_name_that_is_not_a_column(project: Path) -> None:
    with pytest.raises(ProjectInputError, match="not a column"):
        ProjectInputs(project).set_target("german_credit", "salary")


def test_set_target_to_none_removes_the_target(project: Path) -> None:
    ProjectInputs(project).set_target("german_credit", None)

    assert _config(project).datasets[0].target is None


def test_remove_dataset_undeclares_it_and_keeps_its_file(project: Path) -> None:
    remaining = ProjectInputs(project).remove_dataset("german_credit")

    assert remaining == ()
    assert _config(project).datasets == ()
    assert (project / "data" / "applications.csv").is_file()


def test_remove_dataset_refuses_a_name_the_project_does_not_declare(project: Path) -> None:
    with pytest.raises(ProjectInputNotFoundError):
        ProjectInputs(project).remove_dataset("loans")


def test_datasets_reports_a_declared_file_that_is_missing(project: Path) -> None:
    (project / "data" / "applications.csv").unlink()

    (dataset,) = ProjectInputs(project).datasets()

    assert (dataset.present, dataset.size_bytes, dataset.modified_at) == (False, None, None)
