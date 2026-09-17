"""Queue-backed execution dispatch for durable lifecycle work notifications.

``QueueDispatcher`` is the FINAL-tier drop-in for the ``ExecutionDispatcher`` protocol
(``RA-CORE-10``): where :class:`~thymira.core.dispatch.InlineDispatcher` compiles and runs a Run's
composition graph in the calling process, ``QueueDispatcher`` publishes a strict
:class:`~thymira.schemas.WorkNotification` projection of durable outbox work and returns
immediately. Because the repository writes the WorkItem and outbox before this seam is called, the
broker carries only identifiers and can never become the source of lifecycle truth.

This module carries no broker dependency: the transport is injected through the
:class:`TaskPublisher` protocol. The concrete RabbitMQ transport lives in
:mod:`thymira.core.rabbitmq`, which ``thymira.core`` does not import eagerly, so the queue seam
stays usable (and testable with an in-memory publisher) without a broker installed.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from thymira.core.dispatch import ExecutionDispatchError
from thymira.events import canonical_json
from thymira.schemas import OutboxNotification, WorkNotification

if TYPE_CHECKING:
    from collections.abc import Callable

    from thymira.schemas import Id
    from thymira.state import LifecycleRepository


class WorkNotificationDecodeError(ValueError):
    """Raised when a broker body is not the exact current work notification record."""


def notification_to_bytes(notification: WorkNotification) -> bytes:
    """Serialize the strict two-field work notification body."""
    return canonical_json(notification.to_json_dict()).encode("utf-8")


def notification_from_bytes(body: bytes) -> WorkNotification:
    """Decode one broker body through the strict shared schema."""
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkNotificationDecodeError("work notification is not valid UTF-8 JSON") from exc
    if not isinstance(data, dict):
        raise WorkNotificationDecodeError("work notification must be a JSON object")
    try:
        return WorkNotification.model_validate(data)
    except ValueError as exc:
        raise WorkNotificationDecodeError("work notification has an invalid closed shape") from exc


class QueueTransportError(RuntimeError):
    """Raised when a queue transport cannot publish or consume.

    A ``RuntimeError`` subclass so :class:`QueueDispatcher` can convert a publish failure into an
    :class:`~thymira.core.dispatch.ExecutionDispatchError`, letting ``RunService.create_run`` fail
    the Run through the same path it already uses for an inline execution failure.
    """


@runtime_checkable
class TaskPublisher(Protocol):
    """Publish one strict :class:`WorkNotification` to the durable work queue."""

    def publish(self, notification: WorkNotification) -> None:
        """Enqueue ``notification``; raise :class:`QueueTransportError` on failure."""
        ...


@runtime_checkable
class TaskConsumer(Protocol):
    """Deliver queued :class:`WorkNotification` messages with acknowledgement semantics.

    ``consume`` calls ``handler`` once per delivered task. A handler that returns normally
    acknowledges the message (it is done); a handler that raises dead-letters it (a poison message
    an operator must inspect). A worker process that dies without acknowledging leaves the message
    unacknowledged, and an at-least-once transport redelivers it -- the durable-resume path.
    """

    def consume(self, handler: Callable[[WorkNotification], None]) -> None:
        """Block, delivering tasks to ``handler`` until stopped or the process ends."""
        ...


class QueueDispatcher:
    """Publish pending durable outbox notifications, implementing ``ExecutionDispatcher``.

    A drop-in for :class:`~thymira.core.dispatch.InlineDispatcher` at the ``RunService`` call site:
    ``submit`` and ``resume`` publish the repository's pending durable work and return immediately,
    so execution moves off the request thread to a run worker without putting state in the broker.
    """

    def __init__(self, publisher: TaskPublisher, repository: LifecycleRepository) -> None:
        self._publisher = publisher
        self._repository = repository

    def submit(self, run_id: Id) -> None:
        """Publish pending durable work for a newly published Run."""
        self._publish_for_run(run_id)

    def resume(self, run_id: Id) -> None:
        """Publish pending durable work after an accepted lifecycle control."""
        self._publish_for_run(run_id)

    def _publish_for_run(self, run_id: Id) -> None:
        """Publish every unannounced typed outbox work item for ``run_id``."""
        candidates: list[OutboxNotification] = []
        for raw in self._repository.list_outbox():
            try:
                notification = OutboxNotification.model_validate(raw)
            except ValueError as exc:
                raise ExecutionDispatchError("lifecycle outbox contains an invalid record") from exc
            if notification.run_id == run_id and not notification.published:
                candidates.append(notification)
        if not candidates:
            raise ExecutionDispatchError(f"run {run_id}: no pending lifecycle work notification")
        for outbox in candidates:
            notification = outbox.work_notification()
            try:
                self._publisher.publish(notification)
            except QueueTransportError as exc:
                raise ExecutionDispatchError(
                    f"run {run_id}: could not publish lifecycle work notification"
                ) from exc
            self._repository.mark_outbox_published(outbox.notification_id)


__all__ = [
    "QueueDispatcher",
    "QueueTransportError",
    "TaskConsumer",
    "TaskPublisher",
    "WorkNotificationDecodeError",
    "notification_from_bytes",
    "notification_to_bytes",
]
