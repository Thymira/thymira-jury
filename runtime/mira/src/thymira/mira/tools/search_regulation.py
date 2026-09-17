"""Read-only regulation search tool for MIRA audit agents."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field, field_validator

from thymira.events import canonical_json, sha256_text
from thymira.policies import ToolCapability
from thymira.schemas import Framework
from thymira.tools import RegulationSearchMatch, RegulationSearchValue, ToolInvocation, ToolResult

if TYPE_CHECKING:
    from thymira.mira.kb import RegulationStore

_MAX_RESULTS = 20


class SearchRegulationArguments(BaseModel):
    """Validated arguments for a bounded regulation lookup."""

    query: str = Field(min_length=1)
    framework: Framework | None = None
    k: int = Field(default=5, gt=0, le=_MAX_RESULTS)

    @field_validator("query")
    @classmethod
    def _strip_query(cls, value: str) -> str:
        """Reject whitespace-only search queries before Tool Manager authorization."""
        stripped = value.strip()
        if not stripped:
            raise ValueError("query must not be blank")
        return stripped


@dataclass(frozen=True, slots=True)
class SearchRegulation:
    """Search an injected regulation store without accessing workspace or runtime state."""

    store: RegulationStore
    name: str = "search_regulation"
    description: str = "Search local regulation chunks by deterministic keywords."
    arguments_model: type[BaseModel] = SearchRegulationArguments
    result_model: type[BaseModel] = RegulationSearchValue
    capability: ToolCapability = field(
        default_factory=lambda: ToolCapability(
            id="search_regulation",
            data_access=("regulation_store",),
            external_effects=(),
        )
    )

    def execute(self, invocation: ToolInvocation, arguments: dict[str, Any]) -> ToolResult:
        """Return stable JSON matches with citable text and backend ranking provenance."""
        del invocation
        results = self.store.search(
            arguments["query"],
            framework=arguments.get("framework"),
            k=arguments["k"],
        )
        payload = [result.model_dump(mode="json") for result in results]
        stdout = canonical_json(payload)
        value = RegulationSearchValue(
            text=stdout,
            result_count=len(results),
            matches=tuple(RegulationSearchMatch.model_validate(item) for item in payload),
        )
        return ToolResult(
            success=True,
            stdout=stdout,
            value=value,
            result_sha256=sha256_text(stdout),
        )


__all__ = ["SearchRegulation", "SearchRegulationArguments"]
