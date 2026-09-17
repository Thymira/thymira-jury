"""The dashboard definition is checked against the installed SDK, not against the docs.

Langfuse says the dashboard endpoints are unstable and "may change while the dashboard and widget
contract is being finalized". These tests are what turns that warning into something actionable: a
Langfuse upgrade that renames a view, a chart type or an aggregation fails here, in a fast test,
rather than at the moment someone runs the recipe against a real project.
"""

from __future__ import annotations

import os

from langfuse.api.unstable.dashboard_widgets.types import (
    DashboardWidgetChartType,
    DashboardWidgetMetricAggregation,
    DashboardWidgetView,
)

from tests.tooling.conftest import load_script

_SCRIPT = "scripts/langfuse_dashboards.py"

# Verified against the live Metrics API rather than taken from the docs, which point at an
# external reference for the field list. Anything the script asks for must be in here.
_FIELDS = {
    "observations": {"providedModelName", "name", "environment"},
    "scores-categorical": {"name", "stringValue"},
    "scores-numeric": {"name"},
}
_MEASURES = {
    "observations": {"totalCost", "latency", "count"},
    "scores-categorical": {"count"},
    "scores-numeric": {"value", "count"},
}


def test_every_widget_uses_a_view_the_sdk_still_has():
    script = load_script(_SCRIPT)
    supported = {view.value for view in DashboardWidgetView}

    assert {widget.view for widget in script.WIDGETS} <= supported


def test_every_chart_type_and_aggregation_still_exists():
    script = load_script(_SCRIPT)
    charts = {chart.value for chart in DashboardWidgetChartType}
    aggregations = {agg.value for agg in DashboardWidgetMetricAggregation}

    assert {widget.chart_type for widget in script.WIDGETS} <= charts
    for widget in script.WIDGETS:
        assert {agg for _, agg in widget.metrics} <= aggregations


def test_every_dimension_and_measure_belongs_to_its_view():
    """A field valid on one view is rejected on another; the API answers 400, not a wrong chart."""
    script = load_script(_SCRIPT)

    for widget in script.WIDGETS:
        assert set(widget.dimensions) <= _FIELDS[widget.view], widget.name
        assert {measure for measure, _ in widget.metrics} <= _MEASURES[widget.view], widget.name


def test_every_widget_asks_for_at_least_one_metric():
    script = load_script(_SCRIPT)

    assert all(widget.metrics for widget in script.WIDGETS)


def test_widget_names_are_unique_because_the_name_is_the_idempotency_key():
    """A rerun updates the widget carrying the name; two widgets sharing one would fight."""
    script = load_script(_SCRIPT)

    names = [widget.name for widget in script.WIDGETS]
    assert len(names) == len(set(names))


def test_the_layout_fills_the_grid_without_overlapping():
    script = load_script(_SCRIPT)

    tiles = script.placements([f"w{index}" for index in range(len(script.WIDGETS))])

    cells = [
        (tile["x"] + column, tile["y"] + row)
        for tile in tiles
        for column in range(tile["width"])
        for row in range(tile["height"])
    ]
    assert len(cells) == len(set(cells))
    assert all(tile["x"] + tile["width"] <= 12 for tile in tiles)


def test_a_widget_request_carries_the_keywords_the_sdk_expects():
    script = load_script(_SCRIPT)

    request = script.widget_request(script.WIDGETS[0])

    assert set(request) == {
        "name",
        "description",
        "view",
        "dimensions",
        "metrics",
        "filters",
        "chart_type",
    }
    assert request["metrics"] == [{"measure": "totalCost", "agg": "sum"}]


def test_without_both_keys_the_script_does_nothing(monkeypatch):
    """The same gate tracing uses, so a developer with no Langfuse never sees a failing recipe."""
    script = load_script(_SCRIPT)
    monkeypatch.delenv(script.PUBLIC_KEY_ENV_VAR, raising=False)
    monkeypatch.setenv(script.SECRET_KEY_ENV_VAR, "sk-lf-x")

    assert script.keys_present() is False


def test_both_keys_present_is_what_enables_it(monkeypatch):
    script = load_script(_SCRIPT)
    monkeypatch.setenv(script.PUBLIC_KEY_ENV_VAR, "pk-lf-x")
    monkeypatch.setenv(script.SECRET_KEY_ENV_VAR, "sk-lf-x")

    assert script.keys_present() is True
    assert os.environ[script.PUBLIC_KEY_ENV_VAR] == "pk-lf-x"
