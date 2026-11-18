#!/usr/bin/env python3
"""Pre-execution command guardrail shared across AI coding harnesses.

Enforces non-interactive command execution:
1. Rejects commands without explicit stdin closure or input source (e.g. `</dev/null`).
2. Rejects interactive pagers/editors/commands (e.g. `less`, `script`, `vim`).
"""

from __future__ import annotations

import argparse
import json
import re
import sys

# Strip quoted literals before scanning for command names so commit messages or
# search patterns (e.g. `git commit -m "vi mode"`) do not trigger false positives.
QUOTED_STR_RE = re.compile(r"'(?:\\.|[^'])*'|\"(?:\\.|[^\"])*\"")

# Interactive tools that block waiting for TTY input.
# Matches command position after start/separator and optional env assignments or `sudo`/`timeout`.
INTERACTIVE_CMDS_RE = re.compile(
    r"(?:^|[;&|]\s*|(?:&&|\|\|)\s*)"
    r"(?:(?:[A-Za-z_][A-Za-z0-9_]*=\S*|sudo|env|timeout\s+\S+)\s+)*"
    r"(less|more|vim|vi|nano|emacs|htop|script)(?=\s|[;&|]|$)"
)

# `top` is interactive unless run in batch/logging mode (`-l` on macOS, `-b` on Linux).
INTERACTIVE_TOP_RE = re.compile(
    r"(?:^|[;&|]\s*|(?:&&|\|\|)\s*)"
    r"(?:(?:[A-Za-z_][A-Za-z0-9_]*=\S*|sudo|env|timeout\s+\S+)\s+)*"
    r"top(?=\s|[;&|]|$)(?![^;&|]*(?:\s-[lb]|-[A-Za-z]*[lb]))"
)

# Explicit stdin closure, file redirection (< file, </dev/null, <&-, 0<...), heredoc (<<), or here-string (<<<).
STDIN_REDIRECTION_RE = re.compile(
    r"(?:^|\s)0?<(?:&-|\s*[^\s<(&>])|<<<?"
)

# Producers at the start of a pipeline that never read stdin themselves.
SAFE_PIPE_HEAD_RE = re.compile(
    r"^\s*(?:echo|printf|yes|seq|true|false|pwd|date|uname|whoami|id|printenv)\b[^|]*\|(?!\|)"
)


def extract_command(payload: dict) -> str:
    """Extract shell command string from Antigravity, Codex, DSH, or generic payloads."""
    tool_call = payload.get("toolCall")
    if isinstance(tool_call, dict):
        args = tool_call.get("args")
        if isinstance(args, dict) and isinstance(args.get("CommandLine"), str):
            return args["CommandLine"]

    tool_input = payload.get("tool_input")
    if isinstance(tool_input, dict) and isinstance(tool_input.get("command"), str):
        return tool_input["command"]

    inp = payload.get("input")
    if isinstance(inp, dict) and isinstance(inp.get("command"), str):
        return inp["command"]

    if isinstance(payload.get("command"), str):
        return payload["command"]

    return ""


def check_command(cmd: str) -> str | None:
    """Return a denial reason if `cmd` violates non-interactive rules, else None."""
    stripped = cmd.strip()
    if not stripped:
        return None

    unquoted = QUOTED_STR_RE.sub(" ", stripped)

    m = INTERACTIVE_CMDS_RE.search(unquoted) or INTERACTIVE_TOP_RE.search(unquoted)
    if m:
        name = m.group(1) if m.re is INTERACTIVE_CMDS_RE else "top"
        return (
            f"禁止执行可能阻塞的交互式命令 `{name}`。"
            "请改用非交互替代命令（如 cat/head/tail/sed）或由用户自行在终端运行。"
        )

    has_stdin = bool(STDIN_REDIRECTION_RE.search(stripped) or SAFE_PIPE_HEAD_RE.match(stripped))
    if not has_stdin:
        return (
            "无前置数据输入的命令必须显式封闭 stdin（例如在命令末尾加 `</dev/null`），"
            "严禁悬空 stdin 等待输入。请重写命令后重试。"
        )

    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Shared non-interactive command check")
    parser.add_argument(
        "--mode",
        choices=("antigravity", "exit2"),
        default="antigravity",
        help="Output contract: antigravity JSON or exit-code-2 + stderr.",
    )
    args = parser.parse_args()

    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        payload = {"command": raw}

    cmd = extract_command(payload)
    reason = check_command(cmd)

    if args.mode == "antigravity":
        # Emit nothing on pass so Antigravity falls back to the user's normal approval policy
        # instead of auto-allowing commands with `{"decision": "allow"}`.
        if reason:
            print(json.dumps({"decision": "deny", "reason": reason}, ensure_ascii=False))
        return 0

    if reason:
        print(reason, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
