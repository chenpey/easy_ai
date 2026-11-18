"""CLI end-to-end tests for agent-hooks/check_command.py."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent / "check_command.py"


def run_hook(payload: dict, mode: str = "antigravity") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--mode", mode],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
    )


@pytest.mark.parametrize(
    "cmd",
    [
        "npm test </dev/null",
        "git commit -m 'fix; vi mode' </dev/null",
        'git commit -m "echo hi | less" </dev/null',
        "script/build.sh </dev/null",
        "top -l 1 </dev/null",
        "echo hello | wc -l",
        "cat <<'EOF'\nhello\nEOF",
    ],
)
def test_allowed_commands_antigravity_emit_no_decision(cmd: str) -> None:
    proc = run_hook({"toolCall": {"args": {"CommandLine": cmd}}}, mode="antigravity")
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


@pytest.mark.parametrize(
    "cmd",
    [
        "npm test",
        "echo hi | less",
        "env python3 x.py | head",
        "timeout 5 vim x </dev/null",
        "FOO=1 less file.txt </dev/null",
        "top </dev/null",
    ],
)
def test_denied_commands_antigravity(cmd: str) -> None:
    proc = run_hook({"toolCall": {"args": {"CommandLine": cmd}}}, mode="antigravity")
    assert proc.returncode == 0
    data = json.loads(proc.stdout)
    assert data["decision"] == "deny"
    assert data["reason"]


def test_exit2_mode_contract() -> None:
    ok = run_hook({"command": "ls </dev/null"}, mode="exit2")
    assert ok.returncode == 0
    assert ok.stderr == ""

    bad = run_hook({"command": "ls"}, mode="exit2")
    assert bad.returncode == 2
    assert "stdin" in bad.stderr
