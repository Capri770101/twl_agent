"""Load immutable routing hints; execution permissions remain in tool_scope."""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from agent.toolkit import get_tool_specs

DEFAULT_POLICY_PATH = Path(__file__).with_name('routing.json')
_NAME = re.compile(r'[a-z][a-z0-9_]*\Z')
_FIELDS = {'name', 'route_name', 'max_iterations', 'tools'}


@dataclass(frozen=True)
class RoutePolicy:
    name: str
    route_name: str
    max_iterations: int
    tools: frozenset[str]


class RoutingPolicyError(ValueError):
    """A routing policy document is malformed or references an unknown tool."""


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RoutingPolicyError(f'duplicate JSON field: {key}')
        result[key] = value
    return result


def _reject_nonstandard_constant(value: str) -> None:
    raise RoutingPolicyError(f'non-standard JSON constant: {value}')


def load_routing_policies(path: str | Path = DEFAULT_POLICY_PATH) -> Mapping[str, RoutePolicy]:
    """Validate a JSON array against the full registry, not session visibility.

    Each policy has a unique ``name`` and a public ``route_name``. Several
    narrowed variants can therefore retain the same IntentRoute name.
    Iteration limits are positive integers (JSON booleans are not integers).
    """
    try:
        records = json.loads(Path(path).read_text(encoding='utf-8'),
                             object_pairs_hook=_unique_object,
                             parse_constant=_reject_nonstandard_constant)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RoutingPolicyError(f'cannot read routing policies: {exc}') from exc
    if not isinstance(records, list) or not records:
        raise RoutingPolicyError('routing policies must be a non-empty JSON array')
    # get_tool_specs returns ToolSpec instances; visible_tool_specs filters by
    # feature flags and context scopes and must not define policy validity.
    registered = {spec.name for spec in get_tool_specs()}
    policies: dict[str, RoutePolicy] = {}
    for index, record in enumerate(records):
        if not isinstance(record, dict) or set(record) != _FIELDS:
            raise RoutingPolicyError(f'policy {index}: expected fields {sorted(_FIELDS)}')
        for field in ('name', 'route_name'):
            if not isinstance(record[field], str) or not _NAME.fullmatch(record[field]):
                raise RoutingPolicyError(f'policy {index}: invalid {field}')
        name = record['name']
        if name in policies:
            raise RoutingPolicyError(f'duplicate policy name: {name}')
        iterations = record['max_iterations']
        if type(iterations) is not int or iterations <= 0:
            raise RoutingPolicyError(f'policy {name}: max_iterations must be a positive integer')
        tools = record['tools']
        if not isinstance(tools, list) or not tools or any(not isinstance(tool, str) or not _NAME.fullmatch(tool) for tool in tools):
            raise RoutingPolicyError(f'policy {name}: tools must be a non-empty array of tool names')
        if len(tools) != len(set(tools)):
            raise RoutingPolicyError(f'policy {name}: duplicate tool name')
        unknown = set(tools) - registered
        if unknown:
            raise RoutingPolicyError(f'policy {name}: unknown tools {sorted(unknown)}')
        policies[name] = RoutePolicy(name, record['route_name'], iterations, frozenset(tools))
    return MappingProxyType(policies)
