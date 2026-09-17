"""Descriptive and inferential statistics tool."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any, Literal

import polars as pl
from pydantic import BaseModel, Field
from scipy import stats

from thymira.policies import ToolCapability
from thymira.schemas import ArtifactKind
from thymira.tools.builtins.argument_contracts import DESCRIPTION_FIELD, Description
from thymira.tools.builtins.data_analysis import _frame
from thymira.tools.models import ToolInvocation, ToolResult
from thymira.tools.results import StatisticsValue

_REQUIRED_GROUPS = 2
_MIN_GROUP_VALUES = 2
_MIN_NORMALITY_VALUES = 3


class RunStatisticsArguments(BaseModel):
    """Arguments for a deterministic descriptive or inferential test."""

    dataset: str = Field(min_length=1)
    value_column: str = Field(min_length=1)
    description: Description = DESCRIPTION_FIELD
    group_column: str | None = Field(default=None, min_length=1)
    second_value_column: str | None = Field(default=None, min_length=1)
    test: Literal["welch_t", "chi_square", "anova", "correlation", "normality"] = "welch_t"
    max_rows: int = Field(default=100_000, gt=0, le=1_000_000)


@dataclass(frozen=True, slots=True)
class RunStatistics:
    """Run deterministic descriptive and inferential tests with SciPy."""

    name: str = "run_statistics"
    description: str = "Run a two-group Welch t-test and write a report artifact."
    arguments_model: type[BaseModel] = RunStatisticsArguments
    result_model: type[BaseModel] = StatisticsValue
    capability: ToolCapability = field(
        default_factory=lambda: ToolCapability(
            id="run_statistics",
            data_access=("dataset",),
            side_effects=("artifact_write",),
            external_effects=(),
        )
    )

    def execute(self, invocation: ToolInvocation, arguments: dict[str, Any]) -> ToolResult:
        """Compute the requested test and persist its report."""
        frame = _frame(
            invocation, {"dataset": arguments["dataset"], "max_rows": arguments["max_rows"]}
        )
        _require_columns(frame, arguments)
        test_name = arguments["test"]
        payload = _run_test(frame, arguments, test_name)
        payload.update({"dataset": arguments["dataset"], "test": test_name})
        artifact = invocation.artifact_store.save_json(
            f"statistics/{arguments['dataset']}.json",
            payload,
            produced_by=invocation.agent_id,
            kind=ArtifactKind.REPORT,
            media_type="application/json",
        )
        text = json.dumps(payload, sort_keys=True)
        return ToolResult(
            success=True,
            stdout=text,
            value=StatisticsValue(
                text=text,
                dataset=arguments["dataset"],
                test=test_name,
                value_column=arguments["value_column"],
                statistic=payload["statistic"],
                p_value=payload["p_value"],
                artifact_id=artifact.id,
            ),
            artifact_ids=(artifact.id,),
        )


def _require_columns(frame: pl.DataFrame, arguments: dict[str, Any]) -> None:
    """Fail as a normal tool error when a requested column is absent."""
    requested = [arguments["value_column"]]
    if arguments.get("group_column") is not None:
        requested.append(arguments["group_column"])
    if arguments.get("second_value_column") is not None:
        requested.append(arguments["second_value_column"])
    missing = sorted(set(requested) - set(frame.columns))
    if missing:
        raise ValueError(f"unknown dataset columns: {', '.join(missing)}")


def _run_test(frame: pl.DataFrame, arguments: dict[str, Any], test_name: str) -> dict[str, Any]:
    """Dispatch one validated test while keeping output fields stable.

    For ``chi_square`` a null row or column category is not a category: the whole
    observation is dropped, so the reported ``row_levels`` / ``column_levels`` and the
    contingency counts are computed over exactly the same non-null observations.
    """
    value_column = arguments["value_column"]
    value = frame.get_column(value_column)
    group_column = arguments.get("group_column")
    if test_name in {"welch_t", "anova"}:
        return _grouped_test(frame, value, value_column, group_column, test_name)
    if test_name == "chi_square":
        return _chi_square_test(frame, value, value_column, group_column)
    if test_name == "correlation":
        return _correlation_test(frame, value, value_column, arguments.get("second_value_column"))
    if test_name == "normality":
        return _normality_test(value, value_column)
    raise ValueError(f"unsupported statistical test: {test_name}")


def _grouped_test(
    frame: pl.DataFrame,
    value: pl.Series,
    value_column: str,
    group_column: str | None,
    test_name: str,
) -> dict[str, Any]:
    """Run Welch's t-test (exactly two groups) or a one-way ANOVA (at least two)."""
    if group_column is None:
        raise ValueError(f"{test_name} requires group_column")
    group = frame.get_column(group_column)
    levels = sorted(str(item) for item in group.unique().drop_nulls())
    if test_name == "welch_t" and len(levels) != _REQUIRED_GROUPS:
        raise ValueError("welch_t requires exactly two groups")
    if test_name == "anova" and len(levels) < _REQUIRED_GROUPS:
        raise ValueError("anova requires at least two groups")
    samples = [
        value.filter(group.cast(pl.String) == level).drop_nulls().to_list() for level in levels
    ]
    if any(len(sample) < _MIN_GROUP_VALUES for sample in samples):
        raise ValueError(f"{test_name} requires at least two values per group")
    if test_name == "welch_t":
        statistic, pvalue = stats.ttest_ind(samples[0], samples[1], equal_var=False)
    else:
        statistic, pvalue = stats.f_oneway(*samples)
    statistic, pvalue = _finite_result(test_name, statistic, pvalue)
    return {
        "value_column": value_column,
        "group_column": group_column,
        "groups": levels,
        "statistic": statistic,
        "p_value": pvalue,
    }


def _chi_square_test(
    frame: pl.DataFrame,
    value: pl.Series,
    value_column: str,
    group_column: str | None,
) -> dict[str, Any]:
    """Run a chi-square test of independence, dropping observations with a null category."""
    if group_column is None:
        raise ValueError("chi_square requires group_column")
    row_values = frame.get_column(group_column).cast(pl.String).to_list()
    column_values = value.cast(pl.String).to_list()
    pairs = [
        (row, column)
        for row, column in zip(row_values, column_values, strict=True)
        if row is not None and column is not None
    ]
    if not pairs:
        raise ValueError("chi_square requires at least one non-null observation")
    row_levels = sorted({row for row, _ in pairs})
    column_levels = sorted({column for _, column in pairs})
    counts = [
        [sum(pair == (row, column) for pair in pairs) for column in column_levels]
        for row in row_levels
    ]
    statistic, pvalue, _, _ = stats.chi2_contingency(counts)
    statistic, pvalue = _finite_result("chi_square", statistic, pvalue)
    return {
        "value_column": value_column,
        "group_column": group_column,
        "row_levels": row_levels,
        "column_levels": column_levels,
        "statistic": statistic,
        "p_value": pvalue,
    }


def _correlation_test(
    frame: pl.DataFrame,
    value: pl.Series,
    value_column: str,
    second: str | None,
) -> dict[str, Any]:
    """Run a Pearson correlation over the value pairs with no null on either side."""
    if second is None:
        raise ValueError("correlation requires second_value_column")
    pairs = [
        (float(a), float(b))
        for a, b in zip(value.to_list(), frame.get_column(second).to_list(), strict=True)
        if a is not None and b is not None
    ]
    if len(pairs) < _MIN_GROUP_VALUES:
        raise ValueError("correlation requires at least two valid value pairs")
    left, right = zip(*pairs, strict=True)
    statistic, pvalue = stats.pearsonr(left, right)
    statistic, pvalue = _finite_result("correlation", statistic, pvalue)
    return {
        "value_column": value_column,
        "second_value_column": second,
        "statistic": statistic,
        "p_value": pvalue,
    }


def _normality_test(value: pl.Series, value_column: str) -> dict[str, Any]:
    """Run a Shapiro-Wilk normality test over the non-null values."""
    sample = [float(item) for item in value.drop_nulls().to_list()]
    if len(sample) < _MIN_NORMALITY_VALUES:
        raise ValueError("normality requires at least three values")
    statistic, pvalue = stats.shapiro(sample)
    statistic, pvalue = _finite_result("normality", statistic, pvalue)
    return {
        "value_column": value_column,
        "method": "shapiro",
        "statistic": statistic,
        "p_value": pvalue,
    }


def _finite_result(test_name: str, statistic: float, pvalue: float) -> tuple[float, float]:
    """Return the statistic and p-value as floats, failing on a non-finite result.

    ``data_analysis._finite`` maps a non-finite descriptive metric to ``null`` because a
    missing summary value is still a valid analysis. A test statistic has no such reading: a
    NaN or infinite value (a constant column, a degenerate table) means the test is undefined,
    and reporting it as a successful result would write ``NaN`` -- which is not valid JSON --
    into the hash-chained event and the report artifact. An undefined statistic is a failed
    test, so raise instead of returning it.
    """
    statistic_value = float(statistic)
    pvalue_value = float(pvalue)
    if not (math.isfinite(statistic_value) and math.isfinite(pvalue_value)):
        raise ValueError(f"{test_name} produced an undefined (non-finite) statistic")
    return statistic_value, pvalue_value


__all__ = ["RunStatistics", "RunStatisticsArguments"]
