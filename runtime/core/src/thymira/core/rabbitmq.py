"""RabbitMQ transport for the queue dispatcher and the run worker (RA-CORE-11, baseline s18).

This is the concrete AMQP transport behind the transport-agnostic :class:`TaskPublisher` and
:class:`TaskConsumer` protocols in :mod:`thymira.core.queue`. ``thymira.core`` does not import this
module, so ``pika`` is only pulled in when a caller wires the real broker -- the inline path and
the in-memory tests need no broker, exactly as ``thymira.state.postgres`` isolates SQLAlchemy.

Topology. One durable work queue dead-letters to a fanout exchange bound to a durable dead-letter
queue, so a message a worker rejects (a poison task) is preserved for inspection rather than lost
or endlessly redelivered. Both roles declare the same topology idempotently, so publisher and
consumer may start in any order. Messages are persistent (``delivery_mode=2``) and consumed with
manual acknowledgement and ``prefetch=1`` -- a worker holds one task at a time, acknowledges only
after it has driven the Run forward, and a worker that dies mid-task leaves the message
unacknowledged for the broker to redeliver (at-least-once; the durable-resume path).
"""

from __future__ import annotations

import contextlib
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pika
import pika.exceptions

from thymira.core.queue import (
    QueueTransportError,
    notification_from_bytes,
    notification_to_bytes,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from thymira.schemas import WorkNotification

_TRANSPORT_ERRORS = (pika.exceptions.AMQPError, OSError)


@dataclass(frozen=True, slots=True)
class RabbitMqSettings:
    """Connection and topology settings for the RabbitMQ transport, read from the environment.

    Secrets (the broker URL may embed credentials) stay in the environment, never in code.
    """

    url: str = "amqp://guest:guest@localhost:5672/"
    queue: str = "thymira.run.tasks"
    dead_letter_exchange: str = "thymira.run.dlx"
    dead_letter_queue: str = "thymira.run.dead"
    prefetch: int = 1
    heartbeat: int = 30

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> RabbitMqSettings:
        """Build settings from ``THYMIRA_RABBITMQ_*`` environment variables."""
        source = os.environ if env is None else env
        defaults = cls()
        return cls(
            url=source.get("THYMIRA_RABBITMQ_URL", defaults.url),
            queue=source.get("THYMIRA_RABBITMQ_QUEUE", defaults.queue),
            dead_letter_exchange=source.get("THYMIRA_RABBITMQ_DLX", defaults.dead_letter_exchange),
            dead_letter_queue=source.get("THYMIRA_RABBITMQ_DEAD_QUEUE", defaults.dead_letter_queue),
            prefetch=_env_int(source, "THYMIRA_RABBITMQ_PREFETCH", defaults.prefetch),
            heartbeat=_env_int(source, "THYMIRA_RABBITMQ_HEARTBEAT", defaults.heartbeat),
        )


def _env_int(env: Mapping[str, str], key: str, default: int) -> int:
    """Return an integer environment value, falling back to ``default`` when unset."""
    raw = env.get(key)
    return default if raw is None or not raw.strip() else int(raw)


def _connection_parameters(settings: RabbitMqSettings) -> Any:
    """Build blocking-connection parameters with the configured heartbeat."""
    parameters = pika.URLParameters(settings.url)
    parameters.heartbeat = settings.heartbeat
    return parameters


def _declare_topology(channel: Any, settings: RabbitMqSettings) -> None:
    """Idempotently declare the work queue, dead-letter exchange and dead-letter queue."""
    channel.exchange_declare(
        exchange=settings.dead_letter_exchange, exchange_type="fanout", durable=True
    )
    channel.queue_declare(queue=settings.dead_letter_queue, durable=True)
    channel.queue_bind(queue=settings.dead_letter_queue, exchange=settings.dead_letter_exchange)
    channel.queue_declare(
        queue=settings.queue,
        durable=True,
        arguments={"x-dead-letter-exchange": settings.dead_letter_exchange},
    )


class RabbitMqPublisher:
    """Publish strict :class:`WorkNotification` messages to the durable work queue."""

    def __init__(self, settings: RabbitMqSettings | None = None) -> None:
        self._settings = settings or RabbitMqSettings()
        self._connection: Any = None
        self._channel: Any = None

    def publish(self, notification: WorkNotification) -> None:
        """Publish one persistent work notification, reconnecting once if the channel was lost.

        Raises:
            QueueTransportError: The broker could not be reached or the publish failed.
        """
        try:
            channel = self._ensure_channel()
            channel.basic_publish(
                exchange="",
                routing_key=self._settings.queue,
                body=notification_to_bytes(notification),
                properties=pika.BasicProperties(delivery_mode=2, content_type="application/json"),
            )
        except _TRANSPORT_ERRORS as exc:
            self.close()
            raise QueueTransportError(
                f"run {notification.run_id}: could not publish work notification to RabbitMQ"
            ) from exc

    def _ensure_channel(self) -> Any:
        """Return an open channel, connecting and declaring the topology on first use."""
        if self._channel is not None and self._channel.is_open:
            return self._channel
        self._connection = pika.BlockingConnection(_connection_parameters(self._settings))
        self._channel = self._connection.channel()
        _declare_topology(self._channel, self._settings)
        return self._channel

    def close(self) -> None:
        """Close the connection, ignoring an already-closed transport."""
        connection = self._connection
        self._connection = None
        self._channel = None
        if connection is not None and connection.is_open:
            with contextlib.suppress(*_TRANSPORT_ERRORS):
                connection.close()


class RabbitMqConsumer:
    """Consume strict :class:`WorkNotification` messages with manual acknowledgement."""

    def __init__(self, settings: RabbitMqSettings | None = None) -> None:
        self._settings = settings or RabbitMqSettings()
        self._connection: Any = None
        self._channel: Any = None

    def consume(self, handler: Callable[[WorkNotification], None]) -> None:
        """Block, delivering each notification to ``handler`` until the broker closes.

        A handler that returns normally acknowledges the message. A handler that raises, or a
        message that is not a valid notification, is dead-lettered (``basic_nack`` without
        requeue), so a poison message never spins. A worker that dies inside ``handler`` never
        acknowledges, so the broker redelivers the notification for durable claim recovery.
        """
        self._connection = pika.BlockingConnection(_connection_parameters(self._settings))
        self._channel = self._connection.channel()
        _declare_topology(self._channel, self._settings)
        self._channel.basic_qos(prefetch_count=self._settings.prefetch)
        self._channel.basic_consume(
            queue=self._settings.queue,
            on_message_callback=_message_callback(handler),
            auto_ack=False,
        )
        try:
            self._channel.start_consuming()
        except _TRANSPORT_ERRORS as exc:
            raise QueueTransportError("RabbitMQ consumer connection failed") from exc

    def close(self) -> None:
        """Stop consuming and close the connection, ignoring an already-closed transport."""
        connection = self._connection
        channel = self._channel
        self._connection = None
        self._channel = None
        with contextlib.suppress(*_TRANSPORT_ERRORS):
            if channel is not None and channel.is_open:
                channel.stop_consuming()
            if connection is not None and connection.is_open:
                connection.close()


def _message_callback(handler: Callable[[WorkNotification], None]) -> Callable[..., None]:
    """Adapt a notification handler to pika's callback with ack/dead-letter semantics."""

    def on_message(channel: Any, method: Any, _properties: Any, body: bytes) -> None:
        """Decode, handle, and acknowledge one delivery; dead-letter on any handling failure."""
        try:
            notification = notification_from_bytes(body)
            handler(notification)
        except Exception:  # noqa: BLE001  # any handler/decode failure dead-letters
            channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
            return
        channel.basic_ack(delivery_tag=method.delivery_tag)

    return on_message


__all__ = [
    "RabbitMqConsumer",
    "RabbitMqPublisher",
    "RabbitMqSettings",
]
