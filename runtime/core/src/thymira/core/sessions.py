"""Session lifecycle rules for the Thymira runtime."""

from __future__ import annotations

from typing import TYPE_CHECKING

from thymira.schemas import ModelRoutePolicy, Session, new_id

if TYPE_CHECKING:
    from thymira.schemas import Id
    from thymira.state import Page, SessionRepository


class SessionNotFoundError(LookupError):
    """Raised when a requested session does not exist in the repository."""


class SessionProjectMismatchError(ValueError):
    """Raised when a session belongs to a different project than the requested one."""


class SessionService:
    """Create, retrieve and resolve sessions through an injected repository.

    The service deliberately does not choose a default project. The API or its runtime
    configuration resolves that project before calling this boundary. When a caller omits a
    session id, a new session is created; explicit ids are always validated against the project.
    """

    def __init__(
        self,
        repository: SessionRepository,
        *,
        model_route_policy: ModelRoutePolicy | None = None,
    ) -> None:
        self._repository = repository
        self._model_route_policy = model_route_policy or ModelRoutePolicy.unavailable()

    @property
    def model_route_policy(self) -> ModelRoutePolicy:
        """Return the operator snapshot used for newly-created sessions."""
        return self._model_route_policy

    def _caller_policy(self, candidate: ModelRoutePolicy | None) -> ModelRoutePolicy:
        """Accept the configured snapshot or a cryptographically matching explicit narrowing."""
        if candidate is None or candidate == self._model_route_policy:
            return self._model_route_policy
        try:
            narrowed = self._model_route_policy.narrowed_to(
                candidate.allowed_routes,
                authority=candidate.authority,
            )
        except ValueError as exc:
            raise ValueError(
                "session model route policy must be the configured snapshot or its explicit subset"
            ) from exc
        if narrowed != candidate:
            raise ValueError(
                "session model route policy must be the configured snapshot or its explicit subset"
            )
        return candidate

    def create_session(
        self,
        *,
        project_id: Id,
        client: str,
        model_route_policy: ModelRoutePolicy | None = None,
    ) -> Session:
        """Create and persist a new session for ``project_id`` and ``client``."""
        session = Session(
            id=new_id("session"),
            project_id=project_id,
            client=client,
            model_route_policy=self._caller_policy(model_route_policy),
        )
        return self._repository.save(session)

    def get_session(self, session_id: Id) -> Session:
        """Return a session by id.

        Raises:
            SessionNotFoundError: If ``session_id`` is not present in the repository.
        """
        session = self._repository.get(session_id)
        if session is None:
            msg = f"session {session_id!r} was not found"
            raise SessionNotFoundError(msg)
        try:
            bound = self._caller_policy(session.model_route_policy)
        except ValueError as exc:
            raise ValueError(
                f"session {session.id!r} carries a route policy outside this composition authority"
            ) from exc
        if bound != session.model_route_policy:
            raise ValueError(
                f"session {session.id!r} carries a route policy outside this composition authority"
            )
        return session

    def create(
        self,
        *,
        project_id: Id,
        client: str,
        model_route_policy: ModelRoutePolicy | None = None,
    ) -> Session:
        """Compatibility alias for :meth:`create_session`."""
        return self.create_session(
            project_id=project_id,
            client=client,
            model_route_policy=model_route_policy,
        )

    def get(self, session_id: Id) -> Session:
        """Compatibility alias for :meth:`get_session`."""
        return self.get_session(session_id)

    def list_sessions(
        self,
        *,
        project_id: Id | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> Page[Session]:
        """List sessions through the repository pagination contract."""
        return self._repository.list(project_id=project_id, limit=limit, cursor=cursor)

    def resolve(
        self,
        *,
        project_id: Id,
        client: str,
        session_id: Id | None = None,
        model_route_policy: ModelRoutePolicy | None = None,
    ) -> Session:
        """Resolve an explicit session or create a new one when no id is provided.

        ``client`` is recorded when a new session is created. It is not used as an access
        restriction when an existing session is explicitly selected: a Run may be continued by
        another client, as allowed by the shared contract.

        Raises:
            SessionNotFoundError: If an explicit ``session_id`` is unknown.
            SessionProjectMismatchError: If the explicit session belongs to another project.
        """
        if session_id is None:
            return self.create_session(
                project_id=project_id,
                client=client,
                model_route_policy=self._caller_policy(model_route_policy),
            )

        session = self.get_session(session_id)
        if session.project_id != project_id:
            msg = (
                f"session {session.id!r} belongs to project {session.project_id!r}, "
                f"not {project_id!r}"
            )
            raise SessionProjectMismatchError(msg)
        return session

    def attach_run(self, session_id: Id, run_id: Id) -> Session:
        """Associate ``run_id`` with a session and persist the updated session.

        Re-attaching an existing run is idempotent. The immutable Session contract is copied
        rather than mutated in place, so callers never observe a partially changed record.

        Raises:
            SessionNotFoundError: If ``session_id`` is not present in the repository.
        """
        session = self.get_session(session_id)
        if run_id in session.run_ids:
            return session
        updated = session.model_copy(update={"run_ids": (*session.run_ids, run_id)})
        return self._repository.save(updated)

    def with_run(self, session: Session, run_id: Id) -> Session:
        """Return a session copy associated with ``run_id`` without persisting it."""
        if run_id in session.run_ids:
            return session
        return session.model_copy(update={"run_ids": (*session.run_ids, run_id)})


__all__ = ["SessionNotFoundError", "SessionProjectMismatchError", "SessionService"]
