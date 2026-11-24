"""The designer's picks in a spec: the host's check of ``mention:`` evidence (plan 2.4).

The page lets the designer pick model items while writing (``@level``, ``@type``,
``@link``); the chat checks each pick against the open model with the package's
``resolve_mentions`` and the session keeps the items it verified
(``Session.mentions``, keyed by the evidence string). A spec binds a pick by its
name with evidence ``mention:<kind>#<id>`` - the package's rule, in its
instructions. The package's ``validate_spec`` cannot know which picks this
session's designer made, so before a spec is accepted (the model's
``propose_spec``) or reconciled and confirmed (``/spec/reconcile``,
``/spec/confirm``) the host checks every binding whose evidence starts with
``mention:``:

- the evidence is, string for string, that of an item verified in this session;
- the value is that item's: a level's or a type's name, a link's id (the number,
  or its digits as text) - the values the package's selections block states;
- the source is ``tool`` for a pack parameter whose value comes from the model
  (``choices_from`` or a ``tool:*`` source) and ``designer`` for any other one:
  the package's rule, of which ``validate_spec`` enforces only part (it takes
  ``answer`` for the first and ``answer`` or ``preference`` for a designer
  parameter, and ``default`` for a parameter that declares one).

Anything else is ``unknown_mention``, naming the parameter; a spec with no such
evidence is not looked at. No value is looked up here: the items are the
package's, as ``resolve_mentions`` returned them.
"""
from __future__ import annotations

from collections.abc import Mapping

from revit_bridge.spec.models import Source, TaskSpec
from revit_bridge.spec.rules import SpecError

MENTION_PREFIX = "mention:"
UNKNOWN_MENTION = "unknown_mention"


def mention_evidence(kind: object, item_id: object) -> str:
    """The evidence a spec gives for a pick: ``mention:<kind>#<id>``."""
    return f"{MENTION_PREFIX}{kind}#{item_id}"


def picked_value(item: dict) -> object:
    """The value a pick binds: a level's or a type's name, a link's id."""
    return item.get("id") if item.get("kind") == "link" else item.get("name")


def _same_value(bound: object, item: dict) -> bool:
    expected = picked_value(item)
    if item.get("kind") == "link":
        if isinstance(bound, bool):
            return False
        return bound == expected if isinstance(bound, int) else isinstance(bound, str) and bound == str(expected)
    return isinstance(bound, str) and bound == expected


def _model_sourced(pdef: dict | None) -> bool:
    """The pack parameter takes its value from the model (the package's rule for a pick's source)."""
    return pdef is not None and (bool(pdef.get("choices_from")) or str(pdef.get("source", "")).startswith("tool:"))


def mention_errors(spec: TaskSpec, pack, picks: Mapping[str, dict]) -> list[SpecError]:
    """One ``unknown_mention`` error per binding whose ``mention:`` evidence is not a pick of
    this session (``picks``: evidence -> the item ``resolve_mentions`` verified), whose value
    is not that pick's, or whose source is not the one a pick takes for that parameter."""
    params = {p.get("name"): p for p in (pack.parameters if pack is not None else []) if isinstance(p, dict)}
    errors: list[SpecError] = []
    for binding in spec.parameters:
        evidence = binding.evidence or ""
        if not evidence.startswith(MENTION_PREFIX):
            continue
        item = picks.get(evidence)
        if item is None:
            errors.append(SpecError(code=UNKNOWN_MENTION, param=binding.name, message=(
                f"{binding.name}: {evidence!r} is not a pick the designer made in this session; a value "
                f"bound by mention:<kind>#<id> must come from the Designer's selections block")))
            continue
        if not _same_value(binding.value, item):
            errors.append(SpecError(code=UNKNOWN_MENTION, param=binding.name, message=(
                f"{binding.name}: {evidence} is {item.get('kind')} {picked_value(item)!r}; the value bound "
                f"to it must be exactly that, not {binding.value!r}")))
            continue
        expected = Source.tool if _model_sourced(params.get(binding.name)) else Source.designer
        if binding.source is not expected:
            errors.append(SpecError(code=UNKNOWN_MENTION, param=binding.name, message=(
                f"{binding.name}: a pick bound to this parameter has source {expected.value}, "
                f"not {binding.source.value}")))
    return errors
