"""Define the Thymira Operations dashboard in Langfuse from code, idempotently.

The dashboard answers what an operator and a thesis evaluation both ask — what a Run costs, how
often the Policy Engine blocks or asks for a human, which MIRA controls fail — from data the
runtime already emits (ADR-0012). Keeping the definition here rather than clicking it means it
survives a project being recreated, and a reviewer can see in a diff when a chart changed.

**The dashboard API is unstable.** Langfuse says so itself: the endpoints live under
`/api/public/unstable` and "may change while the dashboard and widget contract is being finalized".
This script is therefore deliberately outside the runtime — nothing imports it, no workspace member
depends on it, and a Langfuse upgrade that breaks the contract breaks a `just` recipe, never a Run.

Every field name here was verified against the live Metrics API rather than taken from the docs,
which point at an external reference for the field list: `providedModelName`, `name`, `totalCost`
and `latency` on the observations view, `name` and `stringValue` on the categorical score view, and
`value` on the numeric one.

Run it with `just langfuse-dashboards`. With no `LANGFUSE_*` keys it does nothing and says so, the
same way tracing itself is off unless configured.
"""

from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING, Any, NamedTuple

if TYPE_CHECKING:
    from collections.abc import Sequence

DASHBOARD_NAME = "Thymira Operations"
DASHBOARD_DESCRIPTION = (
    "Cost, latency, authorization outcomes and MIRA control health for every Run."
)

PUBLIC_KEY_ENV_VAR = "LANGFUSE_PUBLIC_KEY"
SECRET_KEY_ENV_VAR = "LANGFUSE_SECRET_KEY"  # noqa: S105  # the variable name, not a secret

_TILE_WIDTH = 6
_TILE_HEIGHT = 6
_COLUMNS = 12


class Widget(NamedTuple):
    """One chart, in the shape `dashboard_widgets.create` takes."""

    name: str
    description: str
    view: str
    dimensions: tuple[str, ...]
    metrics: tuple[tuple[str, str], ...]
    filters: tuple[dict[str, Any], ...]
    chart_type: str


def _score_named(name: str) -> dict[str, Any]:
    """Restrict a score view to one score name, the way the UI's filter state expresses it."""
    return {"column": "name", "operator": "=", "type": "string", "value": name}


def _score_prefixed(prefix: str) -> dict[str, Any]:
    """Restrict a score view to a family of score names."""
    return {"column": "name", "operator": "starts with", "type": "string", "value": prefix}


WIDGETS: tuple[Widget, ...] = (
    Widget(
        name="Thymira - cost by model",
        description="What each routed model costs across all Runs (ADR-0004 routes by task kind).",
        view="observations",
        dimensions=("providedModelName",),
        metrics=(("totalCost", "sum"),),
        filters=(),
        chart_type="VERTICAL_BAR",
    ),
    Widget(
        name="Thymira - cost over time",
        description="Spend per day, so a routing or prompt change is visible in the bill.",
        view="observations",
        dimensions=(),
        metrics=(("totalCost", "sum"),),
        filters=(),
        chart_type="LINE_TIME_SERIES",
    ),
    Widget(
        name="Thymira - phase latency (p95)",
        description="How long each seam takes: the Run, its two orchestrators, each sub-agent.",
        view="observations",
        dimensions=("name",),
        metrics=(("latency", "p95"),),
        filters=(),
        chart_type="HORIZONTAL_BAR",
    ),
    Widget(
        name="Thymira - policy decisions",
        description="PASS / WARNING / REQUIRE_HUMAN_REVIEW / BLOCK, as the Gate recorded them.",
        view="scores-categorical",
        dimensions=("stringValue",),
        metrics=(("count", "count"),),
        filters=(_score_named("policy.decision"),),
        chart_type="PIE",
    ),
    Widget(
        name="Thymira - policy decisions over time",
        description="Whether the BLOCK and human-review rates are moving, and in which direction.",
        view="scores-categorical",
        dimensions=("stringValue",),
        metrics=(("count", "count"),),
        filters=(_score_named("policy.decision"),),
        chart_type="LINE_TIME_SERIES",
    ),
    Widget(
        name="Thymira - MIRA control outcomes",
        description="Each deterministic control by verdict; NOT_APPLICABLE is not a failure.",
        view="scores-categorical",
        dimensions=("name", "stringValue"),
        metrics=(("count", "count"),),
        filters=(_score_prefixed("mira.control."),),
        chart_type="PIVOT_TABLE",
    ),
    Widget(
        name="Thymira - failed controls per Run",
        description="The trend line that says whether audit health is drifting.",
        view="scores-numeric",
        dimensions=(),
        metrics=(("value", "avg"),),
        filters=(_score_named("mira.controls.failed"),),
        chart_type="LINE_TIME_SERIES",
    ),
    Widget(
        name="Thymira - audit status",
        description="passed / passed_with_warnings / failed across Runs.",
        view="scores-categorical",
        dimensions=("stringValue",),
        metrics=(("count", "count"),),
        filters=(_score_named("mira.audit.status"),),
        chart_type="PIE",
    ),
)


def keys_present() -> bool:
    """The same gate tracing uses: configured, or nothing happens."""
    return bool(
        os.environ.get(PUBLIC_KEY_ENV_VAR, "").strip()
        and os.environ.get(SECRET_KEY_ENV_VAR, "").strip()
    )


def placements(widget_ids: Sequence[str]) -> list[dict[str, Any]]:
    """Lay the widgets out two per row on Langfuse's twelve-column grid."""
    return [
        {
            "type": "widget",
            "id": f"placement-{index}",
            "widgetId": widget_id,
            "x": (index * _TILE_WIDTH) % _COLUMNS,
            "y": (index * _TILE_WIDTH // _COLUMNS) * _TILE_HEIGHT,
            "width": _TILE_WIDTH,
            "height": _TILE_HEIGHT,
        }
        for index, widget_id in enumerate(widget_ids)
    ]


def widget_request(widget: Widget) -> dict[str, Any]:
    """The keyword arguments `dashboard_widgets.create` and `.update` take for one widget."""
    return {
        "name": widget.name,
        "description": widget.description,
        "view": widget.view,
        "dimensions": [{"field": field} for field in widget.dimensions],
        "metrics": [{"measure": measure, "agg": agg} for measure, agg in widget.metrics],
        "filters": list(widget.filters),
        "chart_type": widget.chart_type,
    }


def _apply_widgets(api: Any) -> list[str]:
    """Create each widget, or update the one already carrying its name. Returns their ids."""
    existing = {item.name: item.id for item in api.dashboard_widgets.list(limit=100).data}
    ids: list[str] = []
    for widget in WIDGETS:
        request = widget_request(widget)
        if widget.name in existing:
            api.dashboard_widgets.update(widget_id=existing[widget.name], **request)
            ids.append(existing[widget.name])
            print(f"  updated  {widget.name}")  # a CLI script reports on stdout
        else:
            ids.append(api.dashboard_widgets.create(**request).id)
            print(f"  created  {widget.name}")  # same
    return ids


def _apply_dashboard(api: Any, widget_ids: Sequence[str]) -> str:
    """Create or update the dashboard, replacing its whole layout so a rerun cannot duplicate."""
    definition = {"widgets": placements(widget_ids)}
    for item in api.dashboards.list(limit=100).data:
        if item.name == DASHBOARD_NAME:
            api.dashboards.update(
                dashboard_id=item.id,
                name=DASHBOARD_NAME,
                description=DASHBOARD_DESCRIPTION,
                definition=definition,
            )
            print(f"  updated  {DASHBOARD_NAME}")  # same
            return str(item.id)
    created = api.dashboards.create(
        name=DASHBOARD_NAME, description=DASHBOARD_DESCRIPTION, definition=definition
    )
    print(f"  created  {DASHBOARD_NAME}")  # same
    return str(created.id)


def main() -> int:
    """Apply the dashboard definition, or explain why it did nothing."""
    from dotenv import load_dotenv  # noqa: PLC0415  # a script convenience, not a runtime import

    load_dotenv(override=False)
    if not keys_present():
        print(  # a CLI script reports on stdout
            f"{PUBLIC_KEY_ENV_VAR} and {SECRET_KEY_ENV_VAR} are not both set; nothing to do."
        )
        return 0

    from langfuse import Langfuse  # noqa: PLC0415  # never imported unless configured

    api = Langfuse().api.unstable
    print(f"applying {len(WIDGETS)} widgets")  # same
    dashboard_id = _apply_dashboard(api, _apply_widgets(api))
    print(f"dashboard {dashboard_id}")  # same
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through the just recipe.
    sys.exit(main())
