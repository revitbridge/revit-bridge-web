"""Token bench: drive a running revit-bridge-web host over HTTP and measure each task.

run: every task in a new chat session, answers sent when the model asks, at most
max_turns user messages. By default nothing runs in Revit: once the model's spec is
accepted, a fixed execution result (the task's execution, else the fixture) goes back
to the host so its report step is measured too, and again when the host sends the task
back with a new card (at most 3 reports). --execute confirms and runs each accepted
spec instead (it changes the open model), once. The host holds the model configuration
(LLM_* or MODELS_FILE, read at its start: one host process per configuration); the
runner sends no model setting and no key. --mode (default auto: the host's default)
goes as the chat mode with every chat request - single, roles (planner + supervisor) or
roles_analyst; a cloud host runs its default whatever is asked. The results record the
mode asked for and the modes the host ran (chat). --confirm once (default never): when a
tool or ask task's turn stops at a proposal ready for confirmation (the host's tool
events: a ready reconcile, no propose_spec), one "yes" follows and the task is scored on
what comes next - the unattended ("automatic") mode; it runs nothing more in Revit.
Every request carries X-Run-Label <label>-<n>.

compare: task by task, before against after. Exit 1 when a task got worse (fewer
repeats correct, or as often correct and median input tokens up more than 5%), 3
when nothing got worse but something could not be compared (an unknown token
count, a task skipped after, or runs that fed execution results back, or confirmed
ready proposals, differently - a NOTE says so), 0 otherwise.

Unknown is never 0: a count the provider did not report is null, and so is every
sum, difference, median, minimum and maximum that contains it.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import re
import sys
import tempfile
from pathlib import Path

import httpx
import yaml

from .metrics import TOKEN_FIELDS, compare_exit_code, compare_runs
from .runner import CHAT_MODES, CONFIRM_POLICIES, FEEDBACK_ONCE, run_bench, run_labels

LABEL = re.compile(r"[A-Za-z0-9_.-]{1,64}")
REQUIRE = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
# Names Windows reserves for devices, with or without an extension: a label is a file name.
DEVICE_NAMES = frozenset({"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)),
                          *(f"lpt{i}" for i in range(1, 10))})
DEFAULT_BASE_URL = "http://127.0.0.1:7860"
DEFAULT_OUT = Path("bench-results")
# The read timeout outlasts the host's longest model call: 600 s, plus up to 600 more for a
# timeout_s of 900 (backend.llm.call_deadline) - 1200 s. Connecting gets CONNECT_TIMEOUT.
DEFAULT_TIMEOUT = 1260.0
CONNECT_TIMEOUT = 15.0
EXECUTE_WARNING = (
    "WARNING: --execute confirms every accepted spec and RUNS it in the Revit model the host is\n"
    "connected to. Elements are created, changed or deleted, once per task and repeat.\n"
    "Use a throwaway copy of a test model. Without --execute nothing runs in Revit."
)
EPILOG = """\
examples:
  python -m bench run --base-url http://127.0.0.1:7860 --label baseline --repeat 3
  python -m bench run --label nightly --repeat 3 --confirm once      (unattended: the automatic mode)
  python -m bench run --label single-1 --mode single --confirm once
  python -m bench compare bench-results/baseline.json bench-results/roles.json
  python -m bench run --label failures --tasks bench/tasks-supervisor.yml

notes:
  Unattended runs use --confirm once: a model that stops at a ready proposal and asks for a
  "yes" gets one, so the task is measured to propose_spec. Nothing runs in Revit without
  --execute either way.
  --mode is sent with every chat request; a local host runs it, a cloud host its default.
  The host's CHAT_RATE_LIMIT (default 30 per minute per address) counts the bench's
  requests too; raise it on the host for fast models or long task sets.
  Labels must be new on the host (GET /api/usage/runs): each label is one run.
  A task that picks model items by name (mentions: [{kind, pick: {name}}]) looks them up
  before its first message; when the open model has none, the task is skipped
  ("pick not found"), not scored.
"""


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
        if not isinstance(mentions, list) or len(mentions) > 20:
            raise ValueError(f"{task_id}: mentions must be a list of at most 20")
        for item in mentions:
            problem = mention_problem(item)
            if problem:
                raise ValueError(f"{task_id}: invalid mentions: {problem}")
        requires = task.get("requires", [])
        if not isinstance(requires, list) or any(
            not isinstance(name, str) or not REQUIRE.fullmatch(name) for name in requires
        ):
            raise ValueError(f"{task_id}: requires must be a list of names (a-z, 0-9, -)")
        if type(task.get("placeholder_ids", False)) is not bool:
            raise ValueError(f"{task_id}: placeholder_ids must be true or false")
        if "execution" in task and (not isinstance(task["execution"], dict)
                                    or type(task["execution"].get("success")) is not bool):
            raise ValueError(f"{task_id}: execution must be an object with success true or false")
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


OST_CATEGORY = re.compile(r"OST_[A-Za-z0-9_]+")


def mention_problem(item: object) -> str | None:
    """What is wrong with one task mention, or None: ``{kind, id}`` (an integer id) or
    ``{kind, pick: {name | first: true, category?}}`` (category: the OST_* name, for a type
    only and needed there)."""
    if not isinstance(item, dict) or item.get("kind") not in ("level", "type", "link"):
        return "kind is level, type or link"
    if "pick" not in item:
        return None if type(item.get("id")) is int else "an id is an integer"
    pick = item["pick"]
    if set(item) != {"kind", "pick"} or not isinstance(pick, dict) or not set(pick) <= {"name", "first", "category"}:
        return "a pick is {kind, pick: {name | first: true, category?}}"
    if ("name" in pick) == ("first" in pick):
        return "a pick has a name or first: true"
    if "name" in pick and (not isinstance(pick["name"], str) or not pick["name"].strip()):
        return "a pick's name is nonempty text"
    if "first" in pick and pick["first"] is not True:
        return "first is true"
    if item["kind"] == "type":
        if not isinstance(pick.get("category"), str) or not OST_CATEGORY.fullmatch(pick["category"]):
            return "a type pick needs category, an OST_* name"
    elif "category" in pick:
        return "only a type pick takes a category"
    return None


def check_base_url(base: str, allow_remote: bool) -> str:
    url = httpx.URL(base)
    if url.scheme not in ("http", "https") or not url.host or url.query or url.fragment or url.username or url.password:
        raise ValueError("--base-url must be an http(s) URL without credentials, query or fragment")
    if not allow_remote and not is_loopback_host(url.host):
        raise ValueError(f"--base-url names {url.host!r}, not this machine (127.0.0.1, localhost, ::1); "
                         "pass --allow-remote to measure a host elsewhere")
    return base.rstrip("/")


def is_loopback_host(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def check_label(label: str, repeat: int) -> None:
    if not LABEL.fullmatch(label) or label in (".", ".."):
        raise ValueError("--label must be 1-64 characters of A-Z a-z 0-9 _ . - (not . or ..)")
    if label.split(".")[0].lower() in DEVICE_NAMES:
        raise ValueError(f"--label {label!r} is a reserved device name on Windows")
    if any(len(name) > 64 for name in run_labels(label, repeat)):
        raise ValueError(f"--label is too long for {repeat} repeats: <label>-<n> must stay within 64 characters")


# -- tables ---------------------------------------------------------------------------

def show(value: object) -> str:
    if value is None:
        return "?"
    if isinstance(value, float):
        return f"{value:.1f}" if not value.is_integer() else str(int(value))
    return str(value)


def show_stat(value: dict) -> str:
    """The median, and the range when the repeats differ."""
    if value["median"] is None:
        return "?"
    if value["min"] == value["max"]:
        return show(value["median"])
    return f"{show(value['median'])} ({show(value['min'])}-{show(value['max'])})"


def table(rows: list[list[str]]) -> str:
    widths = [max(len(row[index]) for row in rows if index < len(row)) for index in range(len(rows[0]))]
    return "\n".join("  ".join(cell.ljust(widths[index]) for index, cell in enumerate(row)).rstrip()
                     for row in rows)


METRIC_HEADERS = ["Calls", "Input", "Output", "Cache read", "Cache write", "Model ms"]
METRIC_FIELDS = ["calls", *TOKEN_FIELDS, "ms"]


MODE_NAMES = {"single": "single (one loop)", "roles": "roles (planner + supervisor)",
              "roles_analyst": "roles_analyst (analyst + planner + supervisor)"}


def print_results(result: dict) -> None:
    summary = result["summary"]
    repeat = result["repeat"]
    chat = result.get("chat") or {}
    how = MODE_NAMES.get(chat.get("host"), "unknown (not reported by the host)")
    seen = ", ".join(chat.get("roles_seen") or []) or "none"
    ran = ", ".join(chat.get("modes_seen") or []) or "not reported"
    print(f"Label {result['label']}  repeats {repeat}  mode {result['mode']}  chat mode {result.get('chat_mode', 'auto')}"
          f"  confirm {result.get('confirm_policy', 'never')}  host {result['base_url']}")
    print(f"Chat: host default {how}; ran in: {ran}; roles in the events: {seen}")
    if repeat > 1:
        print("Numbers: median (min-max) over the repeats.")
    rows = [["Task", "Kind", "Correct", "Role", *METRIC_HEADERS, "Tools"]]
    notes = []
    for task in summary["tasks"]:
        if task["skipped"]:
            rows.append([task["id"], task["kind"], "skipped", "", "", "", "", "", "", "", ""])
            notes.append(f"{task['id']}: skipped, {task['skipped']}")
            continue
        correct = f"{task['correct']}/{task['runs']}"
        rows.append([task["id"], task["kind"], correct, "all",
                     *[show_stat(task["metrics"][field]) for field in METRIC_FIELDS],
                     show_stat(task["metrics"]["tool_calls"])])
        for role, numbers in task["by_role"].items():
            rows.append(["", "", "", role, *[show_stat(numbers[field]) for field in METRIC_FIELDS],
                         show_stat(numbers["tool_calls"])])
        for error in dict.fromkeys(task["errors"]):
            requires = f" (requires {', '.join(task['requires'])})" if task["requires"] else ""
            notes.append(f"{task['id']}: failed{requires}: {error}")
        if task.get("confirmed"):
            notes.append(f"{task['id']}: a ready proposal confirmed with \"yes\" in {task['confirmed']} of "
                         f"{task['runs']} runs (--confirm once)")
    rows.append(["Total", "", show_stat(summary["correct"]) + "/" + show_stat(summary["scored"]), "all",
                 *[show_stat(summary["totals"][field]) for field in METRIC_FIELDS], ""])
    for role, numbers in summary["by_role"].items():
        rows.append(["", "", "", role, *[show_stat(numbers[field]) for field in METRIC_FIELDS], ""])
    print(table(rows))
    print("? = unknown (not reported by the provider, or the task failed); never counted as 0.")
    for run in result["runs"]:
        check = run["host_usage"]
        if not check["available"]:
            state = f"host totals unavailable ({check['reason']})"
        elif check["match"]:
            state = "matches the host's /api/usage/runs" + (" (per role too)" if check.get("host_by_role") else "")
        else:
            state = "DIFFERS from the host's /api/usage/runs in " + ", ".join(check["mismatches"])
        print(f"Run {run['label']}: {state}")
    for note in notes:
        print("  " + note)


def print_comparison(rows: list[dict]) -> None:
    def side(value: dict | None) -> str:
        if value is None:
            return "missing"
        if value["skipped"] or not value["runs"]:
            return "skipped"
        return f"{value['correct']}/{value['runs']}"

    def number(value: object, ran: bool) -> str:
        return show(value) if ran else "-"

    lines = [["Task", "Correct", "Calls", "Input", "Output", "Cache read", "Cache write", "Status"]]
    for row in rows:
        before, after = side(row["before"]), side(row["after"])
        ran = [before not in ("skipped", "missing"), after not in ("skipped", "missing")]
        cells = [f"{number(row['tokens'][field]['before'], ran[0])} -> {number(row['tokens'][field]['after'], ran[1])}"
                 for field in ("calls", *TOKEN_FIELDS)]
        status = row["status"] + (": " + "; ".join(row["reasons"]) if row["reasons"] else "")
        lines.append([row["id"], f"{before} -> {after}", *cells, status])
    print(table(lines))
    print("Medians over the repeats; ? = unknown, - = not run.")


# -- commands -------------------------------------------------------------------------

def result_path(value: str, out: Path) -> Path:
    """A results file, or a label looked up as <out>/<label>.json."""
    path = Path(value)
    if path.is_file() or value.lower().endswith(".json") or not LABEL.fullmatch(value):
        return path
    return out / (value + ".json")


def load_result(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("version") != 2 or "summary" not in data:
        raise ValueError(f"{path}: not a results file of this bench (version 2)")
    return data


def save(result: dict, destination: Path) -> Path:
    """Write the results; when that fails, into the system temp directory instead, so a
    paid run is not lost to a full or unwritable --out."""
    text = json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    try:
        destination.write_text(text, encoding="utf-8")
        return destination
    except OSError as exc:
        fallback = Path(tempfile.gettempdir()) / ("revit-bridge-bench-" + destination.name)
        print(f"bench: could not write {destination} ({exc}); writing {fallback}", file=sys.stderr)
        fallback.write_text(text, encoding="utf-8")
        return fallback


def command_run(args: argparse.Namespace) -> int:
    if args.repeat < 1 or args.repeat > 100:
        raise ValueError("--repeat must be between 1 and 100")
    check_label(args.label, args.repeat)
    base = check_base_url(args.base_url, args.allow_remote)
    if not 0 < args.timeout < float("inf"):
        raise ValueError("--timeout must be positive and finite")
    skip = []
    for value in args.skip_requires or []:
        for name in value.split(","):
            if not REQUIRE.fullmatch(name.strip()):
                raise ValueError(f"--skip-requires {name!r}: names are a-z, 0-9 and -")
            skip.append(name.strip())
    if any(part.split(".")[0].strip().lower() in DEVICE_NAMES for part in args.out.parts):
        raise ValueError(f"--out {str(args.out)!r} names a reserved device on Windows")
    if args.execute and args.execution_fixture:
        raise ValueError("--execute reports the real result; --execution-fixture is for the default mode")
    tasks = load_tasks(args.tasks)
    fixture = json.loads(args.execution_fixture.read_text(encoding="utf-8")) if args.execution_fixture else None
    if fixture is not None and not isinstance(fixture, dict):
        raise ValueError("Execution fixture must be a JSON object")
    args.out.mkdir(parents=True, exist_ok=True)
    destination = args.out / (args.label + ".json")
    if destination.exists():
        raise ValueError(f"{destination} exists; choose another --label or --out")
    if args.execute:
        print(EXECUTE_WARNING, file=sys.stderr)
    # trust_env=False: no proxy from the environment between the bench and its host. A host that
    # cannot be reached fails within CONNECT_TIMEOUT; a long wait for the model does not.
    timeout = httpx.Timeout(args.timeout, connect=CONNECT_TIMEOUT)
    with httpx.Client(timeout=timeout, follow_redirects=False, trust_env=False) as client:
        result = run_bench(client, base, args.label, tasks, fixture, repeat=args.repeat, skip_requires=skip,
                           include_placeholder_ids=args.include_placeholder_ids, execute=args.execute,
                           mode=args.mode, confirm=args.confirm)
    result["tasks_file"] = str(args.tasks)
    print_results(result)
    print("Saved: " + str(save(result, destination)))
    scored = [task for run in result["runs"] for task in run["tasks"] if not task["skipped"]]
    return int(any(not task["correct"] for task in scored))


def feedback_policy(result: dict) -> str:
    """How a results file fed execution results back; one written before the bench recorded it
    reported once per task."""
    policy = result.get("feedback_policy")
    return policy if isinstance(policy, str) and policy else FEEDBACK_ONCE


def confirm_policy(result: dict) -> str:
    """How a results file answered a ready proposal; one written before the bench recorded it never did."""
    policy = result.get("confirm_policy")
    return policy if policy in CONFIRM_POLICIES else "never"


def command_compare(args: argparse.Namespace) -> int:
    before = load_result(result_path(args.before, args.out))
    after = load_result(result_path(args.after, args.out))
    rows = compare_runs(before, after)
    print_comparison(rows)
    code = compare_exit_code(rows)
    policies = feedback_policy(before), feedback_policy(after)
    if policies[0] != policies[1]:
        # A task whose report was sent back is measured over more reports in one of the two runs.
        print(f"NOTE: the runs fed execution results back differently: before {policies[0]}, after "
              f"{policies[1]}; a task the host sent back is not comparable on its report step.")
        code = code or 3
    confirms = confirm_policy(before), confirm_policy(after)
    if confirms[0] != confirms[1]:
        # A task that stopped at a ready proposal got one more turn in one of the two runs.
        print(f"NOTE: the runs confirmed ready proposals differently: before --confirm {confirms[0]}, after "
              f"--confirm {confirms[1]}; a task that stopped at a proposal is not comparable.")
        code = code or 3
    modes = before.get("chat_mode", "auto"), after.get("chat_mode", "auto")
    if modes[0] != modes[1]:
        print(f"NOTE: the runs asked for different chat modes: before {modes[0]}, after {modes[1]}.")
    ran = modes_seen(before), modes_seen(after)
    if ran[0] != ran[1]:
        # What the host actually ran: a cloud host runs its default whatever is asked.
        print(f"NOTE: the host ran different chat modes: before {', '.join(ran[0]) or 'not reported'}, after "
              f"{', '.join(ran[1]) or 'not reported'}.")
    return code


def modes_seen(result: dict) -> list[str]:
    """The chat modes a results file's host said its turns ran in; none for a file without them."""
    chat = result.get("chat") if isinstance(result.get("chat"), dict) else {}
    seen = chat.get("modes_seen")
    return [m for m in seen if isinstance(m, str)] if isinstance(seen, list) else []


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m bench", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter, epilog=EPILOG)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="measure the task set against a running host",
                         formatter_class=argparse.RawDescriptionHelpFormatter, epilog=EPILOG)
    run.add_argument("--base-url", "--base", dest="base_url", default=DEFAULT_BASE_URL,
                     help=f"the host (default {DEFAULT_BASE_URL}); loopback only unless --allow-remote")
    run.add_argument("--label", required=True,
                     help="name of this run: 1-64 of A-Z a-z 0-9 _ . -; repeats are <label>-1..<label>-N")
    run.add_argument("--repeat", type=int, default=1, help="run the task set N times (default 1)")
    run.add_argument("--tasks", type=Path, default=Path(__file__).with_name("tasks.yml"),
                     help="task file (default: the bench's tasks.yml)")
    run.add_argument("--skip-requires", action="append", metavar="NAME",
                     help="skip tasks that require NAME (repeatable, or comma-separated)")
    run.add_argument("--include-placeholder-ids", action="store_true",
                     help="also run tasks whose mention ids are placeholders (skipped by default)")
    run.add_argument("--execution-fixture", type=Path,
                     help="JSON execution result sent back after an accepted spec (default: a built-in one)")
    run.add_argument("--execute", action="store_true",
                     help="confirm and RUN each accepted spec in Revit (changes the model; off by default)")
    run.add_argument("--mode", choices=CHAT_MODES, default="auto",
                     help="the chat mode sent with every chat request: auto (the host's default, the default "
                          "here), single, roles (planner + supervisor) or roles_analyst; a cloud host runs its "
                          "default whatever is asked")
    run.add_argument("--confirm", choices=CONFIRM_POLICIES, default="never",
                     help="once: when a tool or ask task's turn stops at a proposal ready for confirmation (a "
                          "ready reconcile and no propose_spec in the host's tool events), send one \"yes\" and "
                          "score the task on what follows - the unattended mode; never (default): as before")
    run.add_argument("--out", type=Path, default=DEFAULT_OUT,
                     help="directory of <label>.json (default: ./bench-results)")
    run.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                     help=f"HTTP read timeout in seconds (default {DEFAULT_TIMEOUT:g}: longer than the host's "
                          f"longest model call, 1200 s, so a long non-streamed call is not a failure); "
                          f"connecting to the host times out after {CONNECT_TIMEOUT:g} s")
    run.add_argument("--allow-remote", action="store_true",
                     help="allow a --base-url that is not this machine")
    compare = sub.add_parser("compare", help="compare two results files task by task")
    compare.add_argument("before", help="results file, or a label in --out")
    compare.add_argument("after", help="results file, or a label in --out")
    compare.add_argument("--out", type=Path, default=DEFAULT_OUT, help="where labels are looked up")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "compare":
            return command_compare(args)
        return command_run(args)
    except (OSError, ValueError, TypeError, KeyError, yaml.YAMLError) as exc:
        print(f"bench: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
