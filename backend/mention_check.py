"""The designer's picks in a spec: the host's check of ``mention:`` evidence (plan 2.4).

The page lets the designer pick model items while writing (``@level``, ``@type``,
``@link``, ``@element``); the chat checks each pick against the open model with the package's
``resolve_mentions`` and the session keeps the items it verified, per device scope
(``Session.picks_for``, keyed by the evidence string). A spec binds a pick by its
verified value with evidence ``mention:<kind>#<id>`` - the package's rule, in its
instructions. The package's ``validate_spec`` cannot know which picks this
session's designer made, so before a spec is accepted (the model's
``propose_spec``), reconciled (the model's ``reconcile``, ``/spec/reconcile``) or
confirmed (``/spec/confirm``) the host checks every binding whose evidence looks
like a pick (it starts with ``mention``, whatever the case or the spaces around):

- the evidence is exactly ``mention:<level|type|link|element>#<positive integer>`` (lower
  case, no spaces), and that of an item verified in this session for this device;
- the pick's kind fits the parameter, as far as the pack's own declaration says: a
  parameter whose value comes from ``levels`` takes a level, one from
  ``floor_types`` or ``family_types[:OST_*]`` a type (of that category when both
  name one), and a link only a parameter that is neither; a declaration that says
  nothing about it refuses nothing;
- the value is the pick's name as the package's selections block shows it (a
  link: its label, else its name), or the decimal ID string for an element;
- the source is ``tool`` for a pack parameter whose value comes from the model
  (``choices_from`` or a ``tool:*`` source) and ``designer`` for any other one:
  the package's rule, of which ``validate_spec`` enforces only part (it takes
  ``answer`` for the first and ``answer`` or ``preference`` for a designer
  parameter, and ``default`` for a parameter that declares one).

Anything else is ``unknown_mention``, naming the parameter; a spec with no such
evidence is not looked at. No value is looked up here: the items are the
package's, as ``resolve_mentions`` returned them. ``changed_picks`` compares a
spec's picks with a fresh reading of them (``/spec/confirm`` asks the model again).

The source and value rules repeat what the package's instructions state; they belong
in the package (a public ``mention_errors(spec, pack, picks)``) in a later release.
"""
from __future__ import annotations

import re
from collections.abc import Mapping

from revit_bridge.spec.models import ParamBinding, Source, TaskSpec
from revit_bridge.spec.rules import SpecError

MENTION_PREFIX = "mention:"
UNKNOWN_MENTION = "unknown_mention"
# The one form a pick's evidence takes (what mention_evidence writes for a verified item).
CANONICAL = re.compile(r"mention:(level|type|link|element)#[1-9][0-9]*")


def mention_evidence(kind: object, item_id: object) -> str:
    """The evidence a spec gives for a pick: ``mention:<kind>#<id>``."""
    return f"{MENTION_PREFIX}{kind}#{item_id}"


def looks_like_a_pick(evidence: str | None) -> bool:
    """Evidence the check applies to: it starts with ``mention`` in any case, after any spaces."""
    return (evidence or "").strip().casefold().startswith("mention")


def picked_value(item: dict) -> str:
    """The package's binding value: decimal element ID, link label or name, or level/type name."""
    if item.get("kind") == "element":
        return str(item["id"])
    value = (item.get("label") or item.get("name")) if item.get("kind") == "link" else item.get("name")
    return str(value or "")


def _same_value(bound: object, item: dict) -> bool:
    return isinstance(bound, str) and bound == picked_value(item)


def _model_sourced(pdef: dict | None) -> bool:
    """The pack parameter takes its value from the model (the package's rule for a pick's source)."""
    return pdef is not None and (bool(pdef.get("choices_from")) or str(pdef.get("source", "")).startswith("tool:"))


def _wanted_kind(pdef: dict | None) -> tuple[str | None, str | None]:
    """``(kind, category)`` of the picks a parameter takes, read from the pack's own words: its
    ``choices_from``, else the part of a ``tool:*`` source after ``tool:``. ``levels`` is a
    level; ``floor_types`` and ``family_types`` (``:OST_*`` names the category) are types;
    anything else says nothing (None)."""
    if pdef is None:
        return None, None
    source = str(pdef.get("choices_from") or "")
    if not source:
        declared = str(pdef.get("source", ""))
        source = declared[len("tool:"):] if declared.startswith("tool:") else ""
    if source == "elements" or source.startswith("elements:"):
        return "element", source.partition(":")[2] or None
    if source == "levels":
        return "level", None
    if source == "floor_types":
        return "type", None
    if source == "family_types" or source.startswith("family_types:"):
        return "type", source.partition(":")[2] or None
    return None, None


def _kind_problem(binding: ParamBinding, pdef: dict | None, item: dict) -> str | None:
    kind, category = _wanted_kind(pdef)
    picked = item.get("kind")
    if kind is None:
        return None
    if picked != kind:
        return f"{binding.name}: takes a {kind} (the pack's {pdef.get('choices_from') or pdef.get('source')}), not a {picked}"
    if category and (kind == "element" or item.get("category")) and item.get("category") != category:
        return f"{binding.name}: takes a {kind} of {category}, and this one is of {item.get('category') or 'unknown category'}"
    return None


def _refused(binding: ParamBinding, message: str) -> SpecError:
    return SpecError(code=UNKNOWN_MENTION, param=binding.name, message=message)


def mention_errors(spec: TaskSpec, pack, picks: Mapping[str, dict]) -> list[SpecError]:
    """One ``unknown_mention`` error per binding whose evidence looks like a pick and is not in
    the canonical form, not a pick of this session (``picks``: evidence -> the item
    ``resolve_mentions`` verified, for this device), of a kind the parameter does not take,
    not bound to the pick's name, or not of the source a pick takes for that parameter."""
    params = {p.get("name"): p for p in (pack.parameters if pack is not None else []) if isinstance(p, dict)}
    errors: list[SpecError] = []
    for binding in spec.parameters:
        evidence = binding.evidence or ""
        if not looks_like_a_pick(evidence):
            continue
        if not CANONICAL.fullmatch(evidence):
            errors.append(_refused(binding, (
                f"{binding.name}: evidence {evidence!r} is not mention:<kind>#<id> exactly (kind level, type, link "
                f"or element, a positive id, lower case, no spaces)")))
            continue
        item = picks.get(evidence)
        if item is None:
            errors.append(_refused(binding, (
                f"{binding.name}: {evidence!r} is not a pick the designer made in this session; a value "
                f"bound by mention:<kind>#<id> must come from the Designer's selections block")))
            continue
        pdef = params.get(binding.name)
        problem = _kind_problem(binding, pdef, item)
        if problem:
            errors.append(_refused(binding, problem))
            continue
        if not _same_value(binding.value, item):
            errors.append(_refused(binding, (
                f"{binding.name}: {evidence} is {item.get('kind')} {picked_value(item)!r}; the value bound "
                f"to it must be exactly that value, not {binding.value!r}")))
            continue
        expected = Source.tool if _model_sourced(pdef) else Source.designer
        if binding.source is not expected:
            errors.append(_refused(binding, (
                f"{binding.name}: a pick bound to this parameter has source {expected.value}, "
                f"not {binding.source.value}")))
    return errors


def bound_picks(spec: TaskSpec, picks: Mapping[str, dict]) -> list[tuple[ParamBinding, dict]]:
    """The spec's bindings to picks of this session, with the item each was verified as."""
    return [(b, picks[b.evidence]) for b in spec.parameters if b.evidence in picks]


def changed_picks(bound: list[tuple[ParamBinding, dict]], fresh: Mapping[str, dict]) -> list[SpecError]:
    """``unknown_mention`` for each bound pick that a fresh reading (``fresh``: evidence -> item)
    no longer has, or reads under another name than the one bound."""
    errors: list[SpecError] = []
    for binding, _ in bound:
        now = fresh.get(binding.evidence)
        if now is None:
            errors.append(_refused(binding, (
                f"{binding.name}: {binding.evidence} is no longer in the open model (or cannot be read): "
                f"pick it again")))
        elif not _same_value(binding.value, now):
            errors.append(_refused(binding, (
                f"{binding.name}: {binding.evidence} is now {picked_value(now)!r} in the open model, "
                f"not {binding.value!r}: propose the spec again")))
    return errors
