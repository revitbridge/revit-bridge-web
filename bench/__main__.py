"""Run with python -m bench. Outputs go to the system temp directory by default."""
from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
from pathlib import Path

import httpx
import yaml

from .metrics import TOKEN_FIELDS, compare_runs
from .runner import run_tasks


def load_tasks(path: Path) -> list[dict]:
    tasks = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("Tasks must be a nonempty YAML list")
    ids = set()
    for task in tasks:
        if not isinstance(task, dict):
            raise ValueError("Each task must be an object")
        task_id = task.get("id")
        if not isinstance(task_id, str) or not task_id or task_id in ids:
            raise ValueError("Task IDs must be nonempty unique strings")
        ids.add(task_id)
        if task.get("kind") not in ("chat", "query", "tool", "none", "ask"):
            raise ValueError(f"{task_id}: invalid kind")
        if not isinstance(task.get("message"), str) or not task["message"].strip():
            raise ValueError(f"{task_id}: message must be nonempty text")
        answers = task.get("answers", [])
        if not isinstance(answers, list) or any(not isinstance(answer, str) or not answer.strip() for answer in answers):
            raise ValueError(f"{task_id}: answers must be a list of nonempty strings")
        mentions = task.get("mentions", [])
        if not isinstance(mentions, list) or any(
            not isinstance(item, dict) or item.get("kind") not in ("level", "type", "link")
            or type(item.get("id")) is not int for item in mentions
        ):
            raise ValueError(f"{task_id}: invalid mentions")
        expect = task.get("expect", {})
        if not isinstance(expect, dict) or not isinstance(expect.get("params", {}), dict):
            raise ValueError(f"{task_id}: expect and params must be objects")
        maximum = expect.get("max_turns", 3)
        if type(maximum) is not int or maximum < 1:
            raise ValueError(f"{task_id}: max_turns must be a positive integer")
        if task["kind"] == "tool" or "tool" in expect:
            if not isinstance(expect.get("tool"), str) or not expect["tool"]:
                raise ValueError(f"{task_id}: expect.tool must name a tool")
    return tasks


def display(value: object) -> str:
    return "unknown" if value is None else str(value)


def print_results(result: dict) -> None:
    print("Task | Correct | Turns+feedback | Models | Tools | Input | Output | Cache read | Cache write | ms")
    for task in result["tasks"]:
        print(" | ".join([task["id"], str(task["correct"]), f"{task['turns']}+{task['feedback_turns']}",
                          display(task["model_calls"]), str(task["tool_calls"]),
                          *[display(task["usage"][field]) for field in TOKEN_FIELDS], str(task["elapsed_ms"])]))
        if task["error"]:
            print("  Error: " + task["error"])


def print_comparison(rows: list[dict]) -> None:
    print("Task | Correct before -> after | Input | Output | Cache read | Cache write | Regression")
    for row in rows:
        token_cells = [f"{display(row['tokens'][field]['before'])} -> {display(row['tokens'][field]['after'])}"
                       for field in TOKEN_FIELDS]
        print(" | ".join([row["id"], f"{display(row['before'])} -> {display(row['after'])}",
                          *token_cells, "; ".join(row["reasons"]) or "none"]))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="Measure chat with synthetic execution feedback; never execute a model change")
    run.add_argument("--base", required=True)
    run.add_argument("--label", required=True, help="1-64 ASCII letters, digits, dash, underscore or dot")
    run.add_argument("--tasks", type=Path, default=Path(__file__).with_name("tasks.yml"))
    run.add_argument("--execution-fixture", type=Path)
    run.add_argument("--out", type=Path, default=Path(tempfile.gettempdir()) / "revit-bridge-bench")
    run.add_argument("--timeout", type=float, default=120.0, help="HTTP read timeout in seconds (default: 120)")
    compare = sub.add_parser("compare", help="Exit 1 for a correctness regression or >5%% input-token increase")
    compare.add_argument("before", type=Path)
    compare.add_argument("after", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "compare":
            rows = compare_runs(json.loads(args.before.read_text(encoding="utf-8")),
                                json.loads(args.after.read_text(encoding="utf-8")))
            print_comparison(rows)
            return int(any(row["regression"] for row in rows))
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", args.label) or args.label in (".", ".."):
            raise ValueError("Invalid run label")
        url = httpx.URL(args.base)
        if url.scheme not in ("http", "https") or not url.host or url.query or url.fragment or url.username or url.password:
            raise ValueError("Base must be an HTTP(S) URL without credentials, query or fragment")
        if not 0 < args.timeout < float("inf"):
            raise ValueError("Timeout must be positive and finite")
        tasks = load_tasks(args.tasks)
        fixture = json.loads(args.execution_fixture.read_text(encoding="utf-8")) if args.execution_fixture else None
        if fixture is not None and not isinstance(fixture, dict):
            raise ValueError("Execution fixture must be a JSON object")
        with httpx.Client(timeout=args.timeout, follow_redirects=False) as client:
            result = run_tasks(client, args.base, args.label, tasks, fixture)
        args.out.mkdir(parents=True, exist_ok=True)
        destination = args.out / (args.label + ".json")
        destination.write_text(json.dumps(result, indent=2, ensure_ascii=True, allow_nan=False) + "\n", encoding="utf-8")
        print_results(result)
        print("Saved: " + str(destination))
        return int(any(not task["correct"] for task in result["tasks"]))
    except (OSError, ValueError, TypeError, KeyError, yaml.YAMLError) as exc:
        print(f"bench: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
