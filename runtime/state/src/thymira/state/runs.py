"""In-memory Run repository retained as a fast test backend."""

from __future__ import annotations

from typing import TYPE_CHECKING

from thymira.state.repositories import Page, RunRepository, _page

if TYPE_CHECKING:
    from thymira.schemas import Id, Run, RunStatus


class InMemoryRunRepository:
    """Process-local Run repository for unit tests and local runtime composition."""

    def __init__(self) -> None:
        self._runs: dict[Id, Run] = {}

    def save(self, run: Run) -> Run:
        """Store ``run`` by id and return it."""
        self._runs[run.id] = run
        return run

    def get(self, run_id: Id) -> Run | None:
        """Return a stored Run, or ``None`` when the id is unknown."""
        return self._runs.get(run_id)

    def list(
        self,
        *,
        project_id: Id | None = None,
        status: RunStatus | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> Page[Run]:
        """Return filtered runs using the same cursor contract as local storage."""
        runs = list(self._runs.values())
        if project_id is not None:
            runs = [run for run in runs if run.project_id == project_id]
        if status is not None:
            runs = [run for run in runs if run.status is status]
        return _page(runs, limit=limit, cursor=cursor)


__all__ = ["InMemoryRunRepository", "RunRepository"]
