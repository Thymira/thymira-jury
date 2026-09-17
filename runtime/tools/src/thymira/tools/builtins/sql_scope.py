"""Fail-closed relation-scope validation for model-authored SQL."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from thymira.tools.models import ToolExecutionError

_TokenKind = Literal["identifier", "string", "symbol", "other"]
_Group = tuple[int, ...]

_CLAUSE_BOUNDARIES = frozenset(
    {
        "except",
        "fetch",
        "group",
        "having",
        "intersect",
        "limit",
        "offset",
        "order",
        "qualify",
        "union",
        "where",
        "window",
    }
)
_QUERY_STARTS = frozenset({"select", "with"})
_PARENTHESIZED_QUERY_STARTS = frozenset({"from", "select", "with"})
_SCOPE_ERROR = (
    "query_sql can only read the registered dataset and CTEs or subqueries derived from it"
)


@dataclass(frozen=True, slots=True)
class _Token:
    """One SQL token with its direct parenthesis scope."""

    kind: _TokenKind
    value: str
    group: _Group
    quoted: bool = False
    child_group: _Group | None = None

    @property
    def normalized(self) -> str:
        """Return the case-insensitive identifier form used by DuckDB."""
        return self.value.casefold()


def validate_query_relations(query: str) -> None:
    """Reject any SQL relation that is not ``dataset`` or an in-scope CTE.

    The tool already disables DuckDB external access. This independent, pre-execution guard
    prevents a model query from reaching table functions, replacement scans, catalog-qualified
    relations, or any other relation DuckDB may learn to resolve in a future release.
    """
    tokens = _tokenize(query)
    grouped = _group_tokens(tokens)
    ctes_by_group = _collect_ctes(grouped)

    for group, direct_tokens in grouped.items():
        if not _is_query_group(direct_tokens):
            continue
        for index, token in enumerate(direct_tokens):
            if _is_keyword(token, "from"):
                _validate_from_clause(
                    direct_tokens,
                    index + 1,
                    group=group,
                    grouped=grouped,
                    ctes_by_group=ctes_by_group,
                )


def _tokenize(query: str) -> tuple[_Token, ...]:
    """Tokenize the SQL subset needed to identify relation positions."""
    tokens: list[_Token] = []
    group: _Group = ()
    next_group_id = 0
    position = 0

    while position < len(query):
        ignored_end = _ignored_end(query, position)
        if ignored_end is not None:
            position = ignored_end
            continue
        character = query[position]
        if character == "'":
            position = _scan_quoted(query, position, quote="'")
            tokens.append(_Token("string", "", group))
            continue
        if character == '"':
            end = _scan_quoted(query, position, quote='"')
            value = query[position + 1 : end - 1].replace('""', '"')
            tokens.append(_Token("identifier", value, group, quoted=True))
            position = end
            continue
        if character.isalpha() or character == "_":
            end = position + 1
            while end < len(query) and (query[end].isalnum() or query[end] in {"_", "$"}):
                end += 1
            tokens.append(_Token("identifier", query[position:end], group))
            position = end
            continue
        if character == "(":
            next_group_id += 1
            child_group = (*group, next_group_id)
            tokens.append(_Token("symbol", character, group, child_group=child_group))
            group = child_group
            position += 1
            continue
        if character == ")":
            if not group:
                raise ToolExecutionError("query_sql contains unmatched parentheses")
            child_group = group
            group = group[:-1]
            tokens.append(_Token("symbol", character, group, child_group=child_group))
            position += 1
            continue
        kind: _TokenKind = "symbol" if character in {",", ".", ";"} else "other"
        tokens.append(_Token(kind, character, group))
        position += 1

    if group:
        raise ToolExecutionError("query_sql contains unmatched parentheses")
    return tuple(tokens)


def _ignored_end(query: str, position: int) -> int | None:
    """Return the position after whitespace or a comment, if one starts here."""
    if query[position].isspace():
        return position + 1
    if query.startswith("--", position):
        newline = query.find("\n", position + 2)
        return len(query) if newline < 0 else newline + 1
    if query.startswith("/*", position):
        return _skip_block_comment(query, position)
    return None


def _skip_block_comment(query: str, start: int) -> int:
    """Return the first position after one possibly nested block comment."""
    depth = 1
    position = start + 2
    while position < len(query) and depth:
        if query.startswith("/*", position):
            depth += 1
            position += 2
        elif query.startswith("*/", position):
            depth -= 1
            position += 2
        else:
            position += 1
    if depth:
        raise ToolExecutionError("query_sql contains an unterminated comment")
    return position


def _scan_quoted(query: str, start: int, *, quote: str) -> int:
    """Return the first position after a SQL string or quoted identifier."""
    position = start + 1
    while position < len(query):
        if query[position] != quote:
            position += 1
            continue
        if position + 1 < len(query) and query[position + 1] == quote:
            position += 2
            continue
        return position + 1
    description = "string" if quote == "'" else "identifier"
    raise ToolExecutionError(f"query_sql contains an unterminated quoted {description}")


def _group_tokens(tokens: tuple[_Token, ...]) -> dict[_Group, tuple[_Token, ...]]:
    """Group tokens by their direct parenthesis scope while preserving order."""
    mutable: dict[_Group, list[_Token]] = {}
    for token in tokens:
        mutable.setdefault(token.group, []).append(token)
    return {group: tuple(items) for group, items in mutable.items()}


def _collect_ctes(grouped: dict[_Group, tuple[_Token, ...]]) -> dict[_Group, set[str]]:
    """Collect CTE names from each query scope before relation validation."""
    result: dict[_Group, set[str]] = {}
    for group, tokens in grouped.items():
        names: set[str] = set()
        for index, token in enumerate(tokens):
            if _is_keyword(token, "with"):
                names.update(_cte_names_after_with(tokens, index + 1, grouped=grouped))
        if names:
            result[group] = names
    return result


def _cte_names_after_with(
    tokens: tuple[_Token, ...],
    start: int,
    *,
    grouped: dict[_Group, tuple[_Token, ...]],
) -> set[str]:
    """Parse the names in one non-recursive ``WITH`` prefix."""
    position = start
    if position < len(tokens) and _is_keyword(tokens[position], "recursive"):
        raise ToolExecutionError("query_sql does not allow recursive CTEs")
    names: set[str] = set()
    while position < len(tokens):
        name = tokens[position]
        if name.kind != "identifier":
            return names
        position += 1
        if position < len(tokens) and _is_open_parenthesis(tokens[position]):
            position = _after_matching_parenthesis(tokens, position)
        if position >= len(tokens) or not _is_keyword(tokens[position], "as"):
            return names
        position += 1
        if position < len(tokens) and _is_keyword(tokens[position], "not"):
            position += 1
        if position < len(tokens) and _is_keyword(tokens[position], "materialized"):
            position += 1
        if position >= len(tokens) or not _is_open_parenthesis(tokens[position]):
            return names
        child_group = tokens[position].child_group
        if child_group is None or not _starts_with_query(grouped.get(child_group, ())):
            raise ToolExecutionError(_SCOPE_ERROR)
        normalized = name.normalized
        if normalized == "dataset":
            raise ToolExecutionError("query_sql cannot shadow the registered dataset")
        names.add(normalized)
        position = _after_matching_parenthesis(tokens, position)
        if position >= len(tokens) or tokens[position].value != ",":
            return names
        position += 1
    return names


def _validate_from_clause(
    tokens: tuple[_Token, ...],
    start: int,
    *,
    group: _Group,
    grouped: dict[_Group, tuple[_Token, ...]],
    ctes_by_group: dict[_Group, set[str]],
) -> None:
    """Validate every relation in one top-level ``FROM`` clause."""
    position = _validate_relation(
        tokens,
        start,
        group=group,
        grouped=grouped,
        ctes_by_group=ctes_by_group,
    )
    while position < len(tokens):
        token = tokens[position]
        if token.kind == "identifier" and token.normalized in _CLAUSE_BOUNDARIES:
            return
        if _is_keyword(token, "join") or token.value == ",":
            position = _validate_relation(
                tokens,
                position + 1,
                group=group,
                grouped=grouped,
                ctes_by_group=ctes_by_group,
            )
            continue
        position += 1


def _validate_relation(
    tokens: tuple[_Token, ...],
    start: int,
    *,
    group: _Group,
    grouped: dict[_Group, tuple[_Token, ...]],
    ctes_by_group: dict[_Group, set[str]],
) -> int:
    """Validate one relation and return the following direct-token position."""
    position = start
    if position < len(tokens) and _is_keyword(tokens[position], "lateral"):
        position += 1
    if position >= len(tokens):
        raise ToolExecutionError(_SCOPE_ERROR)

    source = tokens[position]
    if _is_open_parenthesis(source):
        child_group = source.child_group
        if child_group is None or not _starts_with_query(grouped.get(child_group, ())):
            raise ToolExecutionError(_SCOPE_ERROR)
        return _after_matching_parenthesis(tokens, position)
    if source.kind != "identifier":
        raise ToolExecutionError(_SCOPE_ERROR)

    following = tokens[position + 1] if position + 1 < len(tokens) else None
    if following is not None and (following.value == "." or _is_open_parenthesis(following)):
        raise ToolExecutionError(_SCOPE_ERROR)
    if source.normalized not in _allowed_relations(group, ctes_by_group):
        raise ToolExecutionError(_SCOPE_ERROR)
    return position + 1


def _allowed_relations(group: _Group, ctes_by_group: dict[_Group, set[str]]) -> set[str]:
    """Return the base table and CTEs visible from a nested query group."""
    allowed = {"dataset"}
    for length in range(len(group) + 1):
        allowed.update(ctes_by_group.get(group[:length], set()))
    return allowed


def _after_matching_parenthesis(tokens: tuple[_Token, ...], start: int) -> int:
    """Return the position after the close paired with an open parenthesis."""
    child_group = tokens[start].child_group
    for position in range(start + 1, len(tokens)):
        token = tokens[position]
        if token.value == ")" and token.child_group == child_group:
            return position + 1
    raise ToolExecutionError("query_sql contains unmatched parentheses")


def _is_query_group(tokens: tuple[_Token, ...]) -> bool:
    """Return whether direct tokens contain a query expression."""
    has_select_or_with = any(
        token.kind == "identifier" and not token.quoted and token.normalized in _QUERY_STARTS
        for token in tokens
    )
    return has_select_or_with or _starts_with_keyword(tokens, "from")


def _starts_with_query(tokens: tuple[_Token, ...]) -> bool:
    """Return whether a parenthesized relation starts with a supported query form."""
    return bool(tokens) and any(
        token.kind == "identifier"
        and not token.quoted
        and token.normalized in _PARENTHESIZED_QUERY_STARTS
        for token in tokens[:1]
    )


def _starts_with_keyword(tokens: tuple[_Token, ...], value: str) -> bool:
    """Return whether a token sequence starts with one unquoted keyword."""
    return bool(tokens) and _is_keyword(tokens[0], value)


def _is_open_parenthesis(token: _Token) -> bool:
    """Return whether a token opens a child scope."""
    return token.value == "(" and token.child_group is not None


def _is_keyword(token: _Token, value: str) -> bool:
    """Match one unquoted, case-insensitive SQL keyword."""
    return token.kind == "identifier" and not token.quoted and token.normalized == value


__all__ = ["validate_query_relations"]
