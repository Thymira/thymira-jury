"""Read-only SQL over registered datasets."""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, field
from io import StringIO
from typing import Any

import duckdb
from pydantic import BaseModel, Field

from thymira.policies import ToolCapability
from thymira.schemas import ArtifactKind
from thymira.tools.builtins.argument_contracts import DESCRIPTION_FIELD, Description
from thymira.tools.builtins.data_analysis import _frame
from thymira.tools.builtins.sql_scope import validate_query_relations
from thymira.tools.models import ToolExecutionError, ToolInvocation, ToolResult
from thymira.tools.results import QueryRowsValue


class QuerySqlArguments(BaseModel):
    """Arguments for a read-only dataset query."""

    dataset: str = Field(min_length=1)
    query: str = Field(min_length=1)
    description: Description = DESCRIPTION_FIELD
    max_rows: int = Field(default=10_000, gt=0, le=100_000)


@dataclass(frozen=True, slots=True)
class QuerySql:
    """Execute only SELECT/WITH SQL against one registered dataset."""

    name: str = "query_sql"
    description: str = (
        "Run a bounded read-only SQL query over a registered dataset. The dataset argument only "
        "selects which registered dataset is loaded -- inside the SQL itself, the table is always "
        "named literally `dataset` (never the dataset's own name), so query it as "
        "`SELECT ... FROM dataset ...`."
    )
    arguments_model: type[BaseModel] = QuerySqlArguments
    result_model: type[BaseModel] = QueryRowsValue
    capability: ToolCapability = field(
        default_factory=lambda: ToolCapability(
            id="query_sql",
            data_access=("dataset",),
            # Large responses are persisted as a bounded report artifact.
            side_effects=("artifact_write",),
            external_effects=(),
        )
    )

    def execute(self, invocation: ToolInvocation, arguments: dict[str, Any]) -> ToolResult:
        """Execute SQL and return bounded JSON rows."""
        query = _read_only_query(arguments["query"])
        frame = _frame(invocation, {"dataset": arguments["dataset"], "max_rows": 100_000})
        # The dataset arrives through polars and is inserted below, so the engine never needs the
        # filesystem. Disabling external access closes the reach that no verb blocklist can cover:
        # table functions (read_csv, read_parquet, read_text, glob) and the direct-filename
        # replacement scan all live inside an ordinary SELECT.
        connection = duckdb.connect(":memory:", config={"enable_external_access": False})
        try:
            definitions = []
            for name, dtype in zip(frame.columns, frame.dtypes, strict=True):
                sql_type = "DOUBLE" if dtype.is_numeric() else "VARCHAR"
                definitions.append(f'"{name.replace(chr(34), chr(34) * 2)}" {sql_type}')
            connection.execute(f"CREATE TABLE dataset ({', '.join(definitions)})")
            placeholders = ", ".join("?" for _ in frame.columns)
            connection.executemany(
                f"INSERT INTO dataset VALUES ({placeholders})",  # noqa: S608  # placeholders are generated from the bounded frame
                frame.rows(),
            )
            # Everything above is our own SQL, generated from the frame; a failure there is a
            # programming error and stays uncaught so it surfaces. The statement below is the
            # model's, and disabling external access is what makes a handler necessary here: a
            # `read_csv` over an outside path used to return rows and now raises
            # `PermissionException`. Without this catch the change would trade a data leak for a
            # crashed run, because `ToolManager.execute` catches only `ToolExecutionError` and an
            # escaping error leaves `tool.started` without its `tool.completed`, which is the
            # pairing MIRA's A9 control checks.
            # The catch is `duckdb.Error` and nothing wider on purpose. Other exception types do
            # escape -- `SELECT INTERVAL 2000000000 DAY` executes and then overflows into
            # `timedelta` inside `fetchmany` as a plain `OverflowError` -- but that hole is
            # identical in every other tool, because it is `ToolManager.execute` that catches too
            # little. Widening it here would patch one instance of finding #40-1 and leave the
            # invariant broken everywhere else; it is fixed in the manager, which is the single
            # place that can guarantee the pairing for every tool at once (wave 4).
            try:
                result = connection.execute(query).fetchmany(arguments["max_rows"])
                columns = [item[0] for item in connection.description or ()]
            except duckdb.Error as exc:
                # DuckDB's own text is kept deliberately. `Binder Error: Referenced column
                # "vlaue" not found ... Candidate bindings: "value"` is what lets an agent repair
                # its query without another round trip, and the tool bridge hands this string to
                # the model verbatim. Reducing it to the exception name buys nothing either: an
                # attacker-chosen path is already on the append-only chain, because
                # `ToolManager.execute` appended `tool.started` with the redacted `query`
                # argument before the engine ever saw the statement.
                message = f"query_sql could not run the query: {exc}"
                raise ToolExecutionError(message) from exc
        finally:
            connection.close()
        typed_rows = tuple(tuple(row) for row in result)
        rows = [dict(zip(columns, row, strict=True)) for row in typed_rows]
        payload = {"columns": columns, "rows": rows, "row_count": len(rows)}
        encoded = json.dumps(payload, default=str, sort_keys=True)
        canonical_rows = json.loads(encoded)["rows"]
        typed_rows = tuple(tuple(row) for row in canonical_rows)
        if len(encoded.encode("utf-8")) <= 64 * 1024:
            return ToolResult(
                success=True,
                stdout=encoded,
                value=QueryRowsValue(
                    text=encoded,
                    columns=tuple(columns),
                    rows=typed_rows,
                    row_count=len(typed_rows),
                ),
            )
        csv_buffer = StringIO()
        writer = csv.DictWriter(csv_buffer, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        artifact = invocation.artifact_store.save_text(
            f"query/{arguments['dataset']}.csv",
            csv_buffer.getvalue(),
            produced_by=invocation.agent_id,
            kind=ArtifactKind.REPORT,
            media_type="text/csv",
        )
        text = f"[query spilled to artifact {artifact.id}]"
        return ToolResult(
            success=True,
            stdout=text,
            value=QueryRowsValue(
                text=text,
                columns=tuple(columns),
                rows=typed_rows,
                row_count=len(typed_rows),
                artifact_id=artifact.id,
            ),
            artifact_ids=(artifact.id,),
        )


_FORBIDDEN_SQL = re.compile(
    r"\b(?:insert|update|delete|drop|alter|create|copy|export|import|attach|install|load|"
    r"pragma|set|call|truncate|merge|vacuum|begin|commit|rollback)\b",
    re.IGNORECASE,
)


def _read_only_query(value: str) -> str:
    """Validate one conservative read-only statement before giving it to DuckDB.

    DuckDB is embedded here and the dataset table is the only intended input. Rejecting
    forbidden verbs anywhere in the statement also closes write statements hidden in a CTE.
    The validation is deliberately fail-closed; a false positive is safer than a write-capable
    query reaching the engine.
    """
    query = value.strip()
    if not query or ";" in query.rstrip(";"):
        raise ToolExecutionError("query_sql accepts exactly one SELECT query")
    if not re.match(r"^select\b|^with\b", query, re.IGNORECASE):
        raise ToolExecutionError("query_sql only accepts SELECT or WITH queries")
    if _FORBIDDEN_SQL.search(query):
        raise ToolExecutionError("query_sql rejects write or session-changing statements")
    normalized = query.rstrip(";").rstrip()
    validate_query_relations(normalized)
    return normalized


__all__ = ["QuerySql", "QuerySqlArguments"]
