"""Run the backend-agnostic repository contract against local storage."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest

from tests.thymira.repo_conformance import (
    RepositoryFactory,
    assert_event_and_checkpoint_contract,
    assert_record_contract,
    assert_run_and_session_contract,
    assert_unit_of_work_contract,
)
from thymira.state import (
    LocalCheckpointRepository,
    LocalEventStore,
    LocalRecordRepository,
    LocalRunRepository,
    LocalSessionRepository,
    LocalUnitOfWork,
)

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True, slots=True)
class LocalRepositoryFactory:
    """Build fresh local repository handles sharing one temporary root."""

    root: Path

    def run_repository(self) -> LocalRunRepository:
        """Return a local run repository."""
        return LocalRunRepository(self.root)

    def session_repository(self) -> LocalSessionRepository:
        """Return a local session repository."""
        return LocalSessionRepository(self.root)

    def record_repository(self) -> LocalRecordRepository:
        """Return a local child-record repository."""
        return LocalRecordRepository(self.root)

    def event_store(self) -> LocalEventStore:
        """Return a local event store."""
        return LocalEventStore(self.root)

    def checkpoint_repository(self) -> LocalCheckpointRepository:
        """Return a local checkpoint repository."""
        return LocalCheckpointRepository(self.root)

    def unit_of_work(self) -> LocalUnitOfWork:
        """Return a local atomic write boundary."""
        return LocalUnitOfWork(
            self.run_repository(),
            self.record_repository(),
            self.session_repository(),
        )


@pytest.fixture(params=("local",), ids=("local",))
def repository_factory(request: pytest.FixtureRequest, tmp_path: Path) -> RepositoryFactory:
    """Provide one parametrized backend factory for the conformance suite."""
    if request.param == "local":
        return LocalRepositoryFactory(tmp_path)
    raise AssertionError(f"unknown repository backend: {request.param!r}")


def test_run_and_session_repository_contract(repository_factory: RepositoryFactory) -> None:
    """The backend satisfies run and session persistence contracts."""
    assert_run_and_session_contract(repository_factory)


def test_record_repository_contract(repository_factory: RepositoryFactory) -> None:
    """The backend satisfies the child-record persistence contract."""
    assert_record_contract(repository_factory)


def test_event_and_checkpoint_repository_contract(repository_factory: RepositoryFactory) -> None:
    """The backend preserves event chains and checkpoint blobs."""
    assert_event_and_checkpoint_contract(repository_factory)


def test_unit_of_work_contract(repository_factory: RepositoryFactory) -> None:
    """The backend satisfies atomic commit and rollback semantics."""
    assert_unit_of_work_contract(repository_factory)
