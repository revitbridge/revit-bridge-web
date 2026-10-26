"""get_guidance for the chat model: the MCP tool's description, arguments and reply,
the package's lesson selection called in process on the same layers."""
from __future__ import annotations

import asyncio
import inspect
import json
import shutil

import pytest

import revit_bridge.mcp_server as mcp_server
from revit_bridge.paths import guidance_dir

from backend.api import model_tools
from backend.api.model_tools import (
    GET_GUIDANCE, GET_GUIDANCE_HOST_NOTE, MCP_GET_GUIDANCE_DOC, TOOL_DEFINITIONS, TOOL_NAMES, call_tool,
)
from backend.session import Session


def definition() -> dict:
    return next(t["function"] for t in TOOL_DEFINITIONS if t["function"]["name"] == GET_GUIDANCE)


def run(arguments: dict) -> dict:
    return asyncio.run(call_tool(GET_GUIDANCE, arguments, Session(session_id="s"))).result


def ids(reply: dict) -> list[str]:
    return [item["id"] for item in reply["items"]]


@pytest.fixture(autouse=True)
def fresh_library(env):
    """Each test loads the layers its own environment names."""
    model_tools._load_guidance.cache_clear()
    yield
    model_tools._load_guidance.cache_clear()


def test_the_description_and_arguments_are_the_mcp_tools():
    """Copied by hand; this fails when the installed package's docstring or signature moves.
    The description is the MCP docstring word for word, then this host's one note."""
    description = definition()["description"]
    assert MCP_GET_GUIDANCE_DOC == inspect.getdoc(mcp_server.get_guidance)
    assert description.startswith(inspect.getdoc(mcp_server.get_guidance))
    assert description == MCP_GET_GUIDANCE_DOC + "\n" + GET_GUIDANCE_HOST_NOTE
    assert description.endswith("\nThis host has no check_code; use stage=fix with the symbols you suspect.")
    assert description.count("check_code") == 2            # the docstring's fix stage, and the note
    signature = inspect.signature(mcp_server.get_guidance)
    properties = definition()["parameters"]["properties"]
    assert list(properties) == list(signature.parameters)
    for name, parameter in signature.parameters.items():
        annotation = str(parameter.annotation)
        expected = {"type": "array", "items": {"type": "string"}} if "list[str]" in annotation else {"type": "string"}
        assert properties[name] == expected, name
    assert "required" not in definition()["parameters"]           # every argument has a default
    assert GET_GUIDANCE in TOOL_NAMES
    # Read-only for the model: the host still has no execution tool.
    assert not TOOL_NAMES & {"confirm_spec", "run_tool", "execute_code", "check_code"}


def test_orient_returns_the_core_lessons_as_the_mcp_tool_does(monkeypatch):
    reply = run({"mode": "recommend", "stage": "orient"})
    assert reply["error"] is None and reply["schema_version"] == 1
    assert "revit-core/snippet-contract" in ids(reply)
    assert {item["category"] for item in reply["items"]} == {"revit-core"}
    assert all(item["layer"] == "framework" and item["body"] for item in reply["items"])
    # Byte for byte what the MCP server answers for the same call.
    monkeypatch.setattr(mcp_server, "_guidance_library", None)
    assert reply == json.loads(mcp_server.get_guidance(mode="recommend", stage="orient"))
    # mode defaults to recommend, as in the MCP signature.
    assert run({"stage": "orient"}) == reply


def test_the_other_modes_and_the_selection_arguments():
    fix = run({"stage": "fix", "errors": ["CS1061"], "symbols": ["Wall", "LocationCurve"], "revit_version": "2026"})
    assert "revit-geometry/wall-length-from-location" in ids(fix)
    assert run({"stage": "fix", "errors": ["CS1061"]})["items"] == []        # an error alone selects nothing
    numeric = run({"stage": "fix", "errors": ["CS1061"], "symbols": ["Wall", "LocationCurve"], "revit_version": 2026})
    assert numeric == fix                                                 # 2026 reads as "2026"

    index = run({"mode": "index"})
    assert "revit-core/snippet-contract" in ids(index) and all("body" not in item for item in index["items"])
    read = run({"mode": "read", "ids": ["revit-core/elementid-value", "nope/missing"]})
    assert ids(read) == ["revit-core/elementid-value"] and read["missing"] == ["nope/missing"]

    seen = run({"stage": "orient", "seen_hashes": [item["hash"] for item in run({"stage": "orient"})["items"]]})
    assert seen["items"] == [] and {o["reason"] for o in seen["omitted"]} == {"seen"}


def test_bad_arguments_are_results_the_model_can_read():
    assert run({"stage": "write", "symbols": "Wall"})["error"] == "invalid_args"        # not a list
    assert run({"stage": ["orient"]})["error"] == "invalid_args"
    assert run({"stage": "orient", "ids": [1, 2]})["error"] == "invalid_args"
    assert "stage" in str(run({"mode": "recommend"})["error"])                          # the package's own error
    assert "invalid_mode" in str(run({"mode": "everything"})["error"])


def test_the_company_project_and_personal_layers_come_from_the_environment(env, tmp_path):
    """Personal = <REVIT_BRIDGE_DATA_DIR>/guidance; a bad project layer is left out with a warning."""
    personal = tmp_path / "data" / "guidance" / "revit-core" / "lessons"
    personal.mkdir(parents=True)
    shutil.copyfile(guidance_dir() / "revit-core" / "lessons" / "elementid-value.md", personal / "elementid-value.md")
    project = tmp_path / "project-guidance"
    project.mkdir()
    (project / "guidance.yaml").write_text("schema_version: 1\nalways: [no-such-category]\n", encoding="utf-8")
    env.setenv("REVIT_BRIDGE_GUIDANCE_PROJECT", str(project))

    reply = run({"stage": "orient"})
    layers = {item["id"]: item["layer"] for item in reply["items"]}
    assert layers["revit-core/elementid-value"] == "personal"
    assert layers["revit-core/snippet-contract"] == "framework"
    assert any(w.startswith("layer_invalid: project") for w in reply["warnings"])


def test_an_invalid_framework_layer_is_a_result_not_a_crash(monkeypatch, tmp_path):
    monkeypatch.setattr(model_tools, "guidance_dir", lambda: tmp_path / "empty")
    (tmp_path / "empty").mkdir()
    reply = run({"stage": "orient"})
    assert reply["error"] == "guidance_invalid" and reply["problems"]
