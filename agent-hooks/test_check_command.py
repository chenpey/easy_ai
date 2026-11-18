"""CLI end-to-end tests for agent-hooks/check_command.py."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent / "check_command.py"


def run_hook(
    payload: object,
    mode: str = "antigravity",
    host: str = "antigravity",
    raw_input: str | None = None,
) -> subprocess.CompletedProcess[str]:
    stdin_text = raw_input if raw_input is not None else json.dumps(payload)
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--mode", mode, "--host", host],
        input=stdin_text,
        text=True,
        capture_output=True,
        check=False,
    )


@pytest.mark.parametrize(
    "cmd",
    [
        "npm test </dev/null",
        "cd pkg && npm test </dev/null",
        "git log </dev/null | head -n 5",
        "script/build.sh </dev/null",
        "top -l 1 </dev/null",
        "echo hello | wc -l",
        "cat <<'EOF'\nsleep 5\ncurl https://example.com\nEOF",
        "curl -m 10 https://example.com </dev/null",
        "gtimeout 5 ssh user@host 'ls' </dev/null",
        "gtimeout 5 ssh -i ~/.ssh/id_ed25519 user@host 'ls' </dev/null",
        "ping -c 3 127.0.0.1 </dev/null",
        "python3 -m unittest discover </dev/null",
        "git clean -n </dev/null",
        "ls </dev/null 2>&1",
    ],
)
def test_allowed_commands_emit_no_decision(cmd: str) -> None:
    proc = run_hook({"toolCall": {"name": "run_command", "args": {"CommandLine": cmd}}})
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


@pytest.mark.parametrize(
    "cmd",
    [
        "npm test",
        "ls </dev/null && cat",
        "echo hi | less",
        "env python3 x.py | head",
        "timeout 5 vim x </dev/null",
        "FOO=1 less file.txt </dev/null",
        "top </dev/null",
        "sleep 5 </dev/null",
        "curl https://example.com </dev/null",
        "curl --connect-timeout 5 https://example.com </dev/null",
        "ssh user@host 'ls' </dev/null",
        "ping 127.0.0.1 </dev/null",
        "bash -c 'curl https://example.com' </dev/null",
    ],
)
def test_denied_commands_antigravity(cmd: str) -> None:
    proc = run_hook({"toolCall": {"name": "run_command", "args": {"CommandLine": cmd}}})
    assert proc.returncode == 0
    data = json.loads(proc.stdout)
    assert data["decision"] == "deny"
    assert data["reason"]


def test_sleep_allowed_on_pi_denied_on_antigravity() -> None:
    payload = {"toolName": "bash", "input": {"command": "sleep 2 </dev/null"}}
    agy = run_hook(payload, host="antigravity")
    assert json.loads(agy.stdout)["decision"] == "deny"

    pi = run_hook(payload, host="pi")
    assert pi.stdout.strip() == ""


@pytest.mark.parametrize(
    "cmd",
    [
        "git commit -m 'msg' </dev/null",
        "git commit -m 'fix; vi mode' </dev/null",
        'git commit -m "echo hi | less" </dev/null',
        "git push origin main </dev/null",
        "git reset --hard HEAD~1 </dev/null",
        "git clean -fd </dev/null",
        "git restore . </dev/null",
        "rm -rf ./build </dev/null",
        "cat ~/.ssh/id_rsa </dev/null",
        'cat "$HOME/.aws/credentials" </dev/null',
    ],
)
def test_force_ask_commands(cmd: str) -> None:
    proc = run_hook({"toolCall": {"name": "run_command", "args": {"CommandLine": cmd}}})
    assert proc.returncode == 0
    data = json.loads(proc.stdout)
    assert data["decision"] == "force_ask"
    assert data["reason"]


def test_deny_overrides_force_ask() -> None:
    # Missing </dev/null must trigger deny rather than force_ask.
    proc = run_hook({"toolCall": {"name": "run_command", "args": {"CommandLine": "git push"}}})
    assert json.loads(proc.stdout)["decision"] == "deny"


def test_secret_scan_on_write_and_masking() -> None:
    fake_ghp = "ghp_" + "A" * 36
    proc = run_hook(
        {
            "toolCall": {
                "name": "write_file",
                "args": {"Path": "config.py", "Content": f"TOKEN = '{fake_ghp}'\n"},
            }
        }
    )
    data = json.loads(proc.stdout)
    assert data["decision"] == "deny"
    assert fake_ghp not in data["reason"]

    # Example placeholder must not trigger false positive.
    placeholder = "AKIAIOSFODNN" + "7EXAMPLE"
    ok = run_hook(
        {
            "toolCall": {
                "name": "write_file",
                "args": {"Path": "README.md", "Content": f"example={placeholder}\n"},
            }
        }
    )
    assert ok.stdout.strip() == ""


def test_git_commit_scans_staged_diff(tmp_path: Path) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)

    fake_key = "AKIA" + "1234567890ABCDEF"
    secret_file = tmp_path / "secret.txt"
    secret_file.write_text(f"key={fake_key}\n", encoding="utf-8")
    subprocess.run(["git", "add", "secret.txt"], cwd=tmp_path, check=True)

    proc = run_hook(
        {
            "cwd": str(tmp_path),
            "toolCall": {"name": "run_command", "args": {"CommandLine": "git commit -m 'add' </dev/null"}},
        }
    )
    data = json.loads(proc.stdout)
    assert data["decision"] == "deny"
    assert fake_key not in data["reason"]


def test_fail_closed_on_invalid_json_and_exit2_mode() -> None:
    bad_json = run_hook(None, mode="antigravity", raw_input="{not-json")
    assert bad_json.returncode == 0
    assert json.loads(bad_json.stdout)["decision"] == "deny"

    bad_exit2 = run_hook({"command": "npm test"}, mode="exit2")
    assert bad_exit2.returncode == 2
    assert "stdin" in bad_exit2.stderr


def test_stdin_redirect_on_tail_segment_does_not_close_pipeline_head() -> None:
    # The redirect belongs to `tee`, so `npm test` still inherits the terminal stdin.
    proc = run_hook(
        {"toolCall": {"name": "run_command", "args": {"CommandLine": "npm test 2>&1 | tee out.log </dev/null"}}}
    )
    assert json.loads(proc.stdout)["decision"] == "deny"


def test_file_argument_exempts_stdin_redirect(tmp_path: Path) -> None:
    (tmp_path / "data.txt").write_text("x\n", encoding="utf-8")

    ok = run_hook(
        {"cwd": str(tmp_path), "toolCall": {"name": "run_command", "args": {"CommandLine": "wc -l data.txt"}}}
    )
    assert ok.stdout.strip() == ""

    # No file argument means the command would read the inherited stdin.
    blocked = run_hook(
        {"cwd": str(tmp_path), "toolCall": {"name": "run_command", "args": {"CommandLine": "wc -l"}}}
    )
    assert json.loads(blocked.stdout)["decision"] == "deny"


@pytest.mark.parametrize(
    "cmd, expected",
    [
        ("echo $(rm -rf /tmp/x) </dev/null", "force_ask"),
        ("echo `rm -rf /tmp/x` </dev/null", "force_ask"),
        ("xargs rm -rf </dev/null", "force_ask"),
        ("xargs -I {} rm -rf </dev/null", "force_ask"),
        ("xargs -I{} rm -rf </dev/null", "force_ask"),
        ("bash -lc 'curl https://example.com' </dev/null", "deny"),
        ("echo \"it's $(rm -rf /tmp/x)\" </dev/null", "force_ask"),
        ("echo $(echo $(rm -rf /tmp/x)) </dev/null", "force_ask"),
        ('python3 -c "print(1 < 2)"', "deny"),
    ],
)
def test_indirect_command_forms(cmd: str, expected: str) -> None:
    proc = run_hook({"toolCall": {"name": "run_command", "args": {"CommandLine": cmd}}})
    assert json.loads(proc.stdout)["decision"] == expected


def test_git_commit_message_is_not_treated_as_pathspec(tmp_path: Path) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)

    config_file = tmp_path / "cfg.txt"
    config_file.write_text("ok\n", encoding="utf-8")
    subprocess.run(["git", "add", "cfg.txt"], cwd=tmp_path, check=True)

    fake_key = "AKIA" + "1234567890ABCDEF"
    config_file.write_text(f"key={fake_key}\n", encoding="utf-8")

    for command in (
        "git commit -m 'msg' </dev/null",
        "git commit -m 'msg' < /dev/null",
        "git commit -nm 'msg' </dev/null",
    ):
        plain = run_hook(
            {"cwd": str(tmp_path), "toolCall": {"name": "run_command", "args": {"CommandLine": command}}}
        )
        assert json.loads(plain.stdout)["decision"] == "force_ask", command

    # `-a` really does commit the worktree, so it must still be scanned.
    staged = run_hook(
        {"cwd": str(tmp_path), "toolCall": {"name": "run_command", "args": {"CommandLine": "git commit -a -m 'msg' </dev/null"}}}
    )
    assert json.loads(staged.stdout)["decision"] == "deny"


def test_pattern_commands_never_count_as_file_input() -> None:
    # `.` 与同名文件存在时，grep/jq 的首个位置参数仍是模式，不是输入文件。
    for command in ("grep README.md", "jq .", "sed ."):
        proc = run_hook({"toolCall": {"name": "run_command", "args": {"CommandLine": command}}})
        assert json.loads(proc.stdout)["decision"] == "deny", command


def test_output_redirect_target_is_not_an_input_file(tmp_path: Path) -> None:
    target = tmp_path / "existing.txt"
    target.write_text("x\n", encoding="utf-8")

    # 此处的 `cat` 没有输入文件：已存在的同名路径是输出目标，仍会阻塞读 stdin。
    blocked = run_hook(
        {"cwd": str(tmp_path), "toolCall": {"name": "run_command", "args": {"CommandLine": "cat > existing.txt"}}}
    )
    assert json.loads(blocked.stdout)["decision"] == "deny"

    ok = run_hook(
        {
            "cwd": str(tmp_path),
            "toolCall": {"name": "run_command", "args": {"CommandLine": "head -3 existing.txt > existing.txt"}},
        }
    )
    assert ok.stdout.strip() == ""


def test_shell_c_inherits_outer_stdin_redirect() -> None:
    proc = run_hook(
        {"toolCall": {"name": "run_command", "args": {"CommandLine": "bash -lc 'npm test' </dev/null"}}}
    )
    assert proc.stdout.strip() == ""


def test_multi_replace_file_content_scans_replacement_chunks() -> None:
    fake_ghp = "ghp_" + "A" * 36
    proc = run_hook(
        {
            "toolCall": {
                "name": "multi_replace_file_content",
                "args": {
                    "TargetFile": "app.py",
                    "ReplacementChunks": [{"ReplacementContent": f"TOKEN = '{fake_ghp}'\n"}],
                },
            }
        }
    )
    data = json.loads(proc.stdout)
    assert data["decision"] == "deny"
    assert fake_ghp not in data["reason"]
