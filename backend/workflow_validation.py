"""Host checks for the bindings of a structurally valid package workflow."""
from revit_bridge.spec.models import ParamBinding, TaskSpec
from revit_bridge.spec.rules import validate_spec
from revit_bridge.workflows import resolve_step_params


# Representatives exercise the package's scalar type rules before any designer
# answers exist. A fractional value also catches narrowing a number to an integer.
_TYPED_VALUES = {"string": "", "bool": False, "boolean": False, "int": 0, "integer": 0,
                 "double": 0.5, "float": 0.5, "number": 0.5}


def binding_errors(definition, store):
    errors = []
    types = {i["name"]: i["type"] for i in definition["inputs"]}
    inputs = {name: _TYPED_VALUES[kind] for name, kind in types.items()}
    for step in definition["steps"]:
        tool = store.load(step["tool"])
        if tool is None:
            errors.append(f"step {step['id']}: unknown tool {step['tool']}")
            continue
        bindings = []
        for p in tool.parameters:
            key = p["name"]
            if key not in step["params"]:
                continue
            source = step["params"][key]
            result = isinstance(source, dict) and "from" in source
            # A result's value is checked when it exists. Its origin is already known.
            if not result:
                try:
                    resolve_step_params({**step, "params": {key: source}}, inputs, {}, tool)
                except ValueError as exc:
                    if isinstance(source, dict):
                        name = source["input"]
                        errors.append(f"step {step['id']}, parameter {key}: input {name} has type "
                                      f"{types[name]}, expected {p['type']}")
                    else:
                        errors.append(str(exc))
            bindings.append(ParamBinding(name=key, value=None, unit=p.get("unit"),
                                         source="tool" if result else "answer", evidence="workflow binding"))
        draft = TaskSpec(task=step["title"], action={"kind": "run_tool", "tool": tool.name}, parameters=bindings)
        errors.extend(f"step {step['id']}: {e.message}" for e in validate_spec(draft, tool))
    return errors
