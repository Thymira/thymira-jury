"""Integration tests for API request tracing and Run graph provenance.

Real: the FastAPI application, local Run persistence and request instrumentation. Faked: the
execution dispatcher and the telemetry exporter, which is an in-memory SDK exporter.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from tests.thymira.api_support import TEST_CREDENTIAL, authenticated_client
from thymira.api import TelemetryConfigurationError, build_default_deps, create_app
from thymira.core import Subgraph, graph_definition_hash
from thymira.schemas import EventType, Id

if TYPE_CHECKING:
    from pathlib import Path

    from fastapi.testclient import TestClient

    from thymira.api.deps import RuntimeDeps

pytestmark = pytest.mark.integration


class _RecordingDispatcher:
    """Record submitted Runs without invoking a model or graph."""

    def submit(self, run_id: Id) -> None:
        """Record one dispatch request."""
        del run_id

    def resume(self, run_id: Id) -> None:
        """Record one resume request."""
        del run_id


class _ThySubgraph:
    """Minimal THY definition used to derive a deterministic composition hash."""

    name = "thy"

    def graph_version(self) -> str:
        """Return the version included in the composed graph definition."""
        return "thy-v1"


class _MiraSubgraph:
    """Minimal MIRA definition used to derive a deterministic composition hash."""

    name = "mira"

    def graph_version(self) -> str:
        """Return the version included in the composed graph definition."""
        return "mira-v1"


def _workspace(tmp_path: Path) -> Path:
    """Create a minimal configured project for the API composition root."""
    workspace = tmp_path / "workspace"
    config_dir = workspace / ".thymira"
    config_dir.mkdir(parents=True)
    (config_dir / "config.yaml").write_text(
        "project:\n  name: telemetry-test\n  domain: credit_risk\n",
        encoding="utf-8",
        newline="\n",
    )
    return workspace


def _app_with_exporter(tmp_path: Path) -> tuple[TestClient, InMemorySpanExporter, RuntimeDeps]:
    """Build the real API with a test-only in-memory span exporter."""
    expected_hash = graph_definition_hash(
        cast("Subgraph", _ThySubgraph()),
        cast("Subgraph", _MiraSubgraph()),
    )
    deps = build_default_deps(
        tmp_path / "runtime",
        workspace=_workspace(tmp_path),
        dispatcher=_RecordingDispatcher(),
        graph_definition_hash=expected_hash,
        principal_resolver=TEST_CREDENTIAL,
    )
    exporter = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource.create({"service.name": "telemetry-test"}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return authenticated_client(create_app(deps, tracer_provider=provider)), exporter, deps


def test_post_runs_creates_route_span_and_preserves_graph_hash(tmp_path: Path) -> None:
    """A Run creation produces a route span and records the composition hash in run.started."""
    client, exporter, deps = _app_with_exporter(tmp_path)

    with client:
        response = client.post("/runs", json={"prompt": "Trace this request"})

    assert response.status_code == 201
    spans = exporter.get_finished_spans()
    route_spans = [span for span in spans if span.name == "POST /runs"]
    assert len(route_spans) == 1
    attributes = route_spans[0].attributes
    assert attributes is not None
    assert attributes["http.method"] == "POST"
    assert attributes["http.route"] == "/runs"
    assert attributes["http.status_code"] == 201
    assert "Trace this request" not in {str(value) for value in attributes.values()}

    run_id = response.json()["id"]
    started = deps.event_store.read(run_id)[0]
    assert started.type is EventType.RUN_STARTED
    graph_hash = started.payload["graph_definition_hash"]
    assert graph_hash == graph_definition_hash(
        cast("Subgraph", _ThySubgraph()),
        cast("Subgraph", _MiraSubgraph()),
    )
    assert isinstance(graph_hash, str)
    assert len(graph_hash) == 64
    assert all(character in "0123456789abcdef" for character in graph_hash)


def test_tracing_is_noop_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The API remains usable without configuring a telemetry exporter."""
    monkeypatch.delenv("OTEL_TRACES_EXPORTER", raising=False)
    deps = build_default_deps(
        tmp_path / "runtime", workspace=_workspace(tmp_path), principal_resolver=TEST_CREDENTIAL
    )

    app = create_app(deps)

    with authenticated_client(app) as client:
        response = client.get("/healthz")

    assert response.status_code == 200


def test_invalid_exporter_configuration_fails_at_app_startup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unsupported exporter names fail clearly instead of silently misconfiguring tracing."""
    monkeypatch.setenv("OTEL_TRACES_EXPORTER", "unsupported")
    deps = build_default_deps(
        tmp_path / "runtime", workspace=_workspace(tmp_path), principal_resolver=TEST_CREDENTIAL
    )

    with pytest.raises(TelemetryConfigurationError, match="OTEL_TRACES_EXPORTER"):
        create_app(deps)
