"""In-memory Session repository retained as a fast test backend."""

from __future__ import annotations

from typing import TYPE_CHECKING

from thymira.state.repositories import Page, SessionRepository, _page

if TYPE_CHECKING:
    from thymira.schemas import Id, Session


class InMemorySessionRepository:
    """Process-local session repository for unit tests and local runtime composition."""

    def __init__(self) -> None:
        self._sessions: dict[Id, Session] = {}

    def save(self, session: Session) -> Session:
        """Store ``session`` by id and return it."""
        self._sessions[session.id] = session
        return session

    def get(self, session_id: Id) -> Session | None:
        """Return a stored session, or ``None`` when the id is unknown."""
        return self._sessions.get(session_id)

    def list(
        self,
        *,
        project_id: Id | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> Page[Session]:
        """Return filtered sessions using the same cursor contract as local storage."""
        sessions = list(self._sessions.values())
        if project_id is not None:
            sessions = [session for session in sessions if session.project_id == project_id]
        return _page(sessions, limit=limit, cursor=cursor)


__all__ = ["InMemorySessionRepository", "SessionRepository"]
