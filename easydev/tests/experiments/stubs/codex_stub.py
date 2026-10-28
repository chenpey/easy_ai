#!/usr/bin/env python3
"""Stand-in for ``codex exec ...`` used by the offline CLI tests.

The runner exports ``EASYDEV_STUB_PLAN`` pointing at a JSON file.  Supported
keys (all optional):

``events``
    Minimum number of valid JSONL events to emit.  Agent messages, reviewer
    events and the ``turn.completed`` event count toward it; neutral
    ``item.completed`` events pad the rest.
``agent_messages``
    Ordered ``item.completed`` / ``agent_message`` texts.  The analyzer treats
    every message except the last as the initial-route sample.
``reviewer``
    Emit one ``spawn_agent`` call plus a completed ``wait`` state whose report
    is ``reviewer_report``.
``reviewers``
    List of ``{"status": ..., "message": ..., "tool": ..., "prompt": ...}``;
    emits one ``spawn_agent``/``wait`` pair per entry so re-review rounds can be
    simulated.  ``prompt`` overrides the default reviewer handoff text, which
    must still read as an independent review for the analyzer to count it.
    Takes precedence over ``reviewer``.
``reviewer_report``
    Text of the completed reviewer report.
``commands``
    Shell command strings emitted as completed ``command_execution`` events, so
    attribution can count content reads.
``usage``
    Token usage object attached to ``turn.completed``.
``mutations``
    ``[{"path": ..., "content": ...}]`` written relative to ``-C``.
``append_files``
    ``[{"path": ..., "content": ...}]`` appended to existing files.
``post_stage_mutations``
    Mutations applied after ``stage`` (for a staged change reverted in the
    worktree).
``git_commands``
    ``[["git", "update-index", ...], ...]`` executed in the worktree.
``marker_file``
    Append one line per stub invocation, for idempotency checks.
``stage``
    Run ``git add -A`` after the mutations.
``runtime_skill_edit``
    Append a marker to ``<workdir>/.agents/skills/easydev/SKILL.md``.
``invalid_events``
    ``true``/``"malformed"`` appends one malformed line;
    ``"missing_turn_completed"`` omits the ``turn.completed`` event.
``malformed_lines``
    Number of malformed lines to append (default 1 when enabled).
``exit_code``
    Process exit status (default 0).
``sleep_seconds``
    Sleep after the pid/mode probes and before writing events.
``spawn_child`` / ``child_pid_file``
    Start a grandchild process and write its pid to an absolute path.
``pid_file``
    Write this stub's pid to an absolute path.
``mode_probes``
    ``[{"path": ..., "output": ...}]`` records the POSIX mode of ``path``.
``file_probes``
    ``[{"path": ..., "output": ...}]`` copies a file's text to ``output``.
``chmod_files``
    ``[{"path": ..., "mode": 0o755}]`` applies an octal mode after mutations.
``delete_files``
    ``["path", ...]`` deletes files after mutations (used to simulate tampering).
``skip_hooks``
    When true the stub never invokes the generated PreToolUse hook.
``hook_log``
    Append one JSON line per hook invocation (``{"tool","decision","reason"}``).
``final_message``
    Content written to the ``-o`` path (defaults to the last agent message).

The stub never imports the modules under test.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple


FLAGS_WITH_VALUE = {"-C", "-o", "-c", "-s", "-m", "--sandbox"}
SKIPPED_FLAGS = {"exec", "--json", "--ephemeral", "--skip-git-repo-check"}


def parse_args(argv: List[str]) -> Tuple[Path, Optional[Path], str]:
    workdir = Path(".")
    last_message: Optional[Path] = None
    prompt: List[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token in FLAGS_WITH_VALUE:
            value = argv[index + 1] if index + 1 < len(argv) else ""
            if token == "-C":
                workdir = Path(value)
            elif token == "-o":
                last_message = Path(value)
            index += 2
            continue
        if token in SKIPPED_FLAGS:
            index += 1
            continue
        prompt.append(token)
        index += 1
    return workdir, last_message, " ".join(prompt)


def load_plan() -> Dict[str, object]:
    plan_path = os.environ.get("EASYDEV_STUB_PLAN")
    if not plan_path:
        return {}
    return json.loads(Path(plan_path).read_text(encoding="utf-8"))


def valid_events(plan: Dict[str, object]) -> List[Dict[str, object]]:
    events: List[Dict[str, object]] = []
    for index, text in enumerate(plan.get("agent_messages") or []):
        events.append(
            {
                "type": "item.completed",
                "item": {
                    "id": f"msg-{index}",
                    "type": "agent_message",
                    "text": text,
                },
            }
        )
    reviewers = plan.get("reviewers")
    if isinstance(reviewers, list):
        for index, entry in enumerate(reviewers):
            entry = entry if isinstance(entry, dict) else {}
            thread_id = f"reviewer-{index + 1}"
            events.append(
                {
                    "type": "item.completed",
                    "item": {
                        "id": f"spawn-{index + 1}",
                        "type": "collab_tool_call",
                        "tool": "spawn_agent",
                        "receiver_thread_ids": [thread_id],
                        "prompt": entry.get("prompt")
                        or "独立只读审查 independent read-only reviewer",
                    },
                }
            )
            events.append(
                {
                    "type": "item.completed",
                    "item": {
                        "id": f"wait-{index + 1}",
                        "type": "collab_tool_call",
                        "tool": entry.get("tool", "wait"),
                        "agents_states": {
                            thread_id: {
                                "status": entry.get("status", "completed"),
                                "message": entry.get("message", ""),
                            }
                        },
                    },
                }
            )

    reviewer = plan.get("reviewer")
    if reviewer and not isinstance(reviewers, list):
        if isinstance(reviewer, dict):
            reviewer_status = reviewer.get("status", "completed")
            reviewer_message = reviewer.get("message", "")
            reviewer_tool = reviewer.get("tool", "wait")
        else:
            reviewer_status = "completed"
            reviewer_message = plan.get("reviewer_report", "")
            reviewer_tool = "wait"
        events.append(
            {
                "type": "item.completed",
                "item": {
                    "id": "spawn-1",
                    "type": "collab_tool_call",
                    "tool": "spawn_agent",
                    "receiver_thread_ids": ["reviewer-1"],
                    "prompt": "独立只读审查 independent read-only reviewer",
                },
            }
        )
        events.append(
            {
                "type": "item.completed",
                "item": {
                    "id": "wait-1",
                    "type": "collab_tool_call",
                    "tool": reviewer_tool,
                    "agents_states": {
                        "reviewer-1": {
                            "status": reviewer_status,
                            "message": reviewer_message,
                        }
                    },
                },
            }
        )
    for index, command in enumerate(plan.get("commands") or []):
        events.append(
            {
                "type": "item.completed",
                "item": {
                    "id": f"cmd-{index}",
                    "type": "command_execution",
                    "command": command,
                    "exit_code": 0,
                },
            }
        )
    target = int(plan.get("events") or 0)
    padding = max(0, target - len(events) - 1)
    for index in range(padding):
        events.append(
            {
                "type": "item.completed",
                "item": {"id": f"pad-{index}", "type": "reasoning"},
            }
        )
    if plan.get("invalid_events") != "missing_turn_completed":
        events.append(
            {
                "type": "turn.completed",
                "usage": plan.get("usage") or {},
            }
        )
    return events


def write_pid(path_value: object) -> None:
    if not path_value:
        return
    path = Path(str(path_value))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{os.getpid()}\n", encoding="utf-8")


def observe_modes(probes: object) -> None:
    for probe in probes or []:
        source = Path(str(probe["path"]))
        output = Path(str(probe["output"]))
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(f"{source.stat().st_mode & 0o777:o}\n", encoding="utf-8")


def load_hook_commands(workdir: Path) -> List[str]:
    """读取宿主配置里的 PreToolUse 命令，模拟宿主真实调用 Hook。"""
    path = workdir / ".codex" / "hooks.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    commands: List[str] = []
    buckets = value.get("hooks") if isinstance(value, dict) else None
    if isinstance(buckets, dict):
        for entries in buckets.values():
            if not isinstance(entries, list):
                continue
            for entry in entries:
                hooks = entry.get("hooks") if isinstance(entry, dict) else None
                for hook in hooks or []:
                    command = hook.get("command") if isinstance(hook, dict) else None
                    if isinstance(command, str) and command:
                        commands.append(command)
    return commands


def call_hooks(
    commands: List[str],
    workdir: Path,
    tool: str,
    tool_input: Dict[str, object],
    log_path: object,
) -> Tuple[str, str]:
    """逐个执行 Hook 命令；返回 (decision, reason)。"""
    payload = json.dumps(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": tool,
            "tool_input": tool_input,
            "cwd": str(workdir),
        }
    )
    decision = "allow"
    reason = ""
    for command in commands:
        completed = subprocess.run(
            command,
            shell=True,
            input=payload,
            text=True,
            capture_output=True,
            timeout=120,
        )
        try:
            value = json.loads(completed.stdout or "{}")
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict):
            continue
        specific = value.get("hookSpecificOutput")
        specific = specific if isinstance(specific, dict) else {}
        if specific.get("permissionDecision") == "deny":
            decision = "deny"
            reason = str(specific.get("permissionDecisionReason") or "")
            break
    if log_path:
        log = Path(str(log_path))
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {"tool": tool, "decision": decision, "reason": reason},
                    ensure_ascii=False,
                )
                + "\n"
            )
    return decision, reason


def apply_mutations(
    workdir: Path,
    mutations: object,
    commands: Optional[List[str]] = None,
    log_path: object = None,
) -> None:
    for mutation in mutations or []:
        target = Path(str(mutation["path"]))
        if not target.is_absolute():
            target = workdir / target
        if commands:
            decision, _reason = call_hooks(
                commands, workdir, "Write", {"file_path": str(target)}, log_path
            )
            if decision == "deny":
                continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(mutation["content"]), encoding="utf-8")


def append_files(
    workdir: Path,
    entries: object,
    commands: Optional[List[str]] = None,
    log_path: object = None,
) -> None:
    for entry in entries or []:
        target = Path(str(entry["path"]))
        if not target.is_absolute():
            target = workdir / target
        if commands:
            decision, _reason = call_hooks(
                commands, workdir, "Write", {"file_path": str(target)}, log_path
            )
            if decision == "deny":
                continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(str(entry["content"]))


def chmod_files(workdir: Path, entries: object) -> None:
    for entry in entries or []:
        target = Path(str(entry["path"]))
        if not target.is_absolute():
            target = workdir / target
        target.chmod(int(entry["mode"]))


def probe_files(workdir: Path, entries: object) -> None:
    for entry in entries or []:
        source = Path(str(entry["path"]))
        if not source.is_absolute():
            source = workdir / source
        output = Path(str(entry["output"]))
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")


def main(argv: List[str]) -> int:
    workdir, last_message, _prompt = parse_args(argv)
    plan = load_plan()
    if plan.get("marker_file"):
        marker = Path(str(plan["marker_file"]))
        marker.parent.mkdir(parents=True, exist_ok=True)
        with marker.open("a", encoding="utf-8") as handle:
            handle.write(f"run {os.getpid()}\n")
    write_pid(plan.get("pid_file"))
    if plan.get("spawn_child"):
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(120)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if plan.get("child_pid_file"):
            child_pid_file = Path(str(plan["child_pid_file"]))
            child_pid_file.parent.mkdir(parents=True, exist_ok=True)
            child_pid_file.write_text(f"{child.pid}\n", encoding="utf-8")
    observe_modes(plan.get("mode_probes"))
    probe_files(workdir, plan.get("file_probes"))
    sleep_seconds = float(plan.get("sleep_seconds") or 0)
    if sleep_seconds:
        time.sleep(sleep_seconds)

    hook_commands = [] if plan.get("skip_hooks") else load_hook_commands(workdir)
    hook_log = plan.get("hook_log")
    # 真实宿主会在命令执行前调用 PreToolUse；替身同样先走生成配置里的命令。
    for command in plan.get("commands") or []:
        if hook_commands and isinstance(command, str):
            call_hooks(hook_commands, workdir, "Bash", {"command": command}, hook_log)

    for event in valid_events(plan):
        print(json.dumps(event, ensure_ascii=False), flush=True)
    invalid_events = plan.get("invalid_events")
    if invalid_events in {True, "malformed"}:
        for _ in range(int(plan.get("malformed_lines") or 1)):
            print("{not-json", flush=True)

    apply_mutations(workdir, plan.get("mutations"), hook_commands, hook_log)
    append_files(workdir, plan.get("append_files"), hook_commands, hook_log)
    if plan.get("stage"):
        subprocess.run(
            ["git", "add", "-A"],
            cwd=str(workdir),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=60,
        )
    apply_mutations(workdir, plan.get("post_stage_mutations"), hook_commands, hook_log)
    chmod_files(workdir, plan.get("chmod_files"))
    for entry in plan.get("delete_files") or []:
        target = Path(str(entry))
        if not target.is_absolute():
            target = workdir / target
        try:
            target.unlink()
        except OSError:
            pass
    for command in plan.get("git_commands") or []:
        if hook_commands:
            decision, _reason = call_hooks(
                hook_commands,
                workdir,
                "Bash",
                {"command": " ".join(str(part) for part in command)},
                hook_log,
            )
            if decision == "deny":
                continue
        subprocess.run(
            [str(part) for part in command],
            cwd=str(workdir),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=60,
        )
    if plan.get("runtime_skill_edit"):
        skill = workdir / ".agents" / "skills" / "easydev" / "SKILL.md"
        skill.write_text(
            skill.read_text(encoding="utf-8") + "\n# stub runtime edit\n",
            encoding="utf-8",
        )
    if last_message is not None:
        messages = plan.get("agent_messages") or []
        content = plan.get("final_message")
        if content is None:
            content = messages[-1] if messages else ""
        last_message.parent.mkdir(parents=True, exist_ok=True)
        last_message.write_text(str(content), encoding="utf-8")
    return int(plan.get("exit_code") or 0)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
