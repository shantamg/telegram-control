#!/usr/bin/python3
"""Repeatable offline and live evaluation for the main router contract."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from durable_store import ManagedProject, StoreError, SurfaceBinding
from router_contract import (
    CONTROLLER_TOOLS,
    DISCOVERY_TOOLS,
    build_main_agent_prompt,
    parse_router_tool_call,
)


ROOT = Path(__file__).resolve().parent
DEFAULT_FIXTURES = ROOT / "tests" / "fixtures" / "router_cases.json"


@dataclass(frozen=True)
class RouterCase:
    name: str
    input_text: str
    expected_tool: str
    sample_output: str


def load_cases(path: Path) -> list[RouterCase]:
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise StoreError(f"Could not read router fixtures: {exc}") from exc
    known_tools = {
        str(item["name"])
        for item in tuple(CONTROLLER_TOOLS) + tuple(DISCOVERY_TOOLS)
    }
    if not isinstance(payload, list) or not payload:
        raise StoreError("Router fixtures must be a non-empty list.")
    cases: list[RouterCase] = []
    names: set[str] = set()
    for item in payload:
        if not isinstance(item, dict) or set(item) != {
            "name",
            "input",
            "expected_tool",
            "sample_output",
        }:
            raise StoreError("Router fixture shape is invalid.")
        name = str(item["name"])
        expected_tool = str(item["expected_tool"])
        if not name or name in names or expected_tool not in known_tools:
            raise StoreError("Router fixture name or expected tool is invalid.")
        names.add(name)
        cases.append(
            RouterCase(
                name=name,
                input_text=str(item["input"]),
                expected_tool=expected_tool,
                sample_output=json.dumps(
                    item["sample_output"],
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            )
        )
    return cases


def fixture_project() -> ManagedProject:
    return ManagedProject(
        project_id="project_fixture",
        slug="telegram-control",
        display_name="Telegram Control",
        provider="codex",
        project_path="/path/that/must/not/appear",
        state="active",
    )


def fixture_topic() -> SurfaceBinding:
    return SurfaceBinding(
        binding_id=1,
        chat_id=123,
        message_thread_id=62,
        surface_type="project",
        display_name="Stage 2 Test",
        target_type="controller",
        target_id="control",
        state="active",
    )


def router_prompt(input_text: str) -> str:
    return build_main_agent_prompt(
        input_text,
        [fixture_project()],
        [
            {
                "project_slug": "telegram-control",
                "state": "registered",
                "session": True,
            }
        ],
        topics=[fixture_topic()],
        current_surface={
            "kind": "private_forum_topic",
            "message_thread_id": 62,
            "forum_authorized": True,
            "forum_name": "Life",
            "workspace_bound": False,
            "workspace_name": None,
            "provider": None,
        },
    )


def evaluate_output(case: RouterCase, raw_output: str) -> dict[str, Any]:
    started = time.monotonic()
    error: Optional[str] = None
    actual_tool: Optional[str] = None
    try:
        call = parse_router_tool_call(
            raw_output,
            {"telegram-control"},
            allowed_topic_ids={62},
        )
        actual_tool = call.tool
    except StoreError as exc:
        error = str(exc)
    return {
        "name": case.name,
        "expected_tool": case.expected_tool,
        "actual_tool": actual_tool,
        "passed": actual_tool == case.expected_tool,
        "parse_error": error,
        "evaluation_ms": round((time.monotonic() - started) * 1000, 3),
    }


def run_live_case(
    case: RouterCase,
    binary: str,
    model: Optional[str],
) -> tuple[str, float]:
    command = [
        binary,
        "exec",
        "--ephemeral",
        "--json",
        "--sandbox",
        "read-only",
        "--cd",
        str(ROOT),
    ]
    if model:
        command.extend(["--model", model])
    command.append("-")
    started = time.monotonic()
    result = subprocess.run(
        command,
        input=router_prompt(case.input_text),
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    duration = time.monotonic() - started
    if result.returncode != 0:
        raise StoreError(
            result.stderr[-2000:]
            or f"Codex exited with status {result.returncode}."
        )
    final_text = ""
    for line in result.stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            raise StoreError("Codex emitted invalid JSONL.") from None
        item = event.get("item") if isinstance(event, dict) else None
        if (
            event.get("type") == "item.completed"
            and isinstance(item, dict)
            and item.get("type") == "agent_message"
        ):
            final_text = str(item.get("text", "")).strip()
    if not final_text:
        raise StoreError("Codex returned no final router output.")
    return final_text, duration


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURES)
    parser.add_argument(
        "--live",
        action="store_true",
        help="Run every fixture through an ephemeral Codex session.",
    )
    parser.add_argument("--model", help="Optional Codex model override.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        cases = load_cases(args.fixtures)
        results = []
        if args.live:
            binary = shutil.which("codex")
            if not binary:
                raise StoreError("Codex CLI is not installed.")
            for case in cases:
                raw_output, duration = run_live_case(case, binary, args.model)
                evaluated = evaluate_output(case, raw_output)
                evaluated["duration_seconds"] = round(duration, 3)
                results.append(evaluated)
        else:
            results = [
                evaluate_output(case, case.sample_output)
                for case in cases
            ]
        passed = sum(1 for result in results if result["passed"])
        report = {
            "mode": "live" if args.live else "offline",
            "model": args.model or ("default" if args.live else None),
            "passed": passed,
            "total": len(results),
            "pass_rate": passed / len(results),
            "results": results,
        }
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0 if passed == len(results) else 1
    except (OSError, subprocess.TimeoutExpired, StoreError) as exc:
        print(json.dumps({"error": str(exc)}, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
