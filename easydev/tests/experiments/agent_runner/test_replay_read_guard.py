"""回放越界读取守卫与安装契约（无真实宿主、无真实 HOME）。"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

AGENT_RUNNER_ROOT = Path(__file__).resolve().parent
EXPERIMENTS_ROOT = AGENT_RUNNER_ROOT.parent
EASYDEV_ROOT = AGENT_RUNNER_ROOT.parents[2]
for entry in (str(AGENT_RUNNER_ROOT), str(EXPERIMENTS_ROOT), str(EASYDEV_ROOT)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

import replay_read_guard as guard  # noqa: E402


def clean_env(extra=None):
    env = {key: value for key, value in os.environ.items() if not key.startswith("PI_SUBAGENT_")}
    env.update(extra or {})
    return env


class ReplayReadGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.home = base / "home"
        self.home.mkdir()
        (self.home / ".config" / "easydev").mkdir(parents=True)
        (self.home / ".config" / "easydev" / "models.json").write_text(
            '{"sentinel": true}\n', encoding="utf-8"
        )
        self.operator = base / "operator-config"
        self.operator.mkdir()
        (self.operator / "models.json").write_text('{"operator": true}\n', encoding="utf-8")
        self.skill = base / "skill" / "easydev"
        (self.skill / "references").mkdir(parents=True)
        (self.skill / "SKILL.md").write_text("skill\n", encoding="utf-8")
        (self.skill / "references" / "routing.md").write_text("routing\n", encoding="utf-8")
        self.workspace = base / "workspace"
        self.workspace.mkdir()
        (self.workspace / "src").mkdir()
        (self.workspace / "src" / "app.py").write_text("print(1)\n", encoding="utf-8")
        (self.workspace / "patterns.txt").write_text("print\n", encoding="utf-8")
        self.sibling = base / "workspace-sibling"
        self.sibling.mkdir()
        (self.sibling / "secret.txt").write_text("sibling\n", encoding="utf-8")
        self.outside = base / "outside"
        self.outside.mkdir()
        (self.outside / "sentinel.txt").write_text("sentinel\n", encoding="utf-8")
        (self.workspace / "link-file").symlink_to(self.outside / "sentinel.txt")
        (self.workspace / "link-dir").symlink_to(self.outside)
        self.policy_dir = base / "policy"
        self.policy_dir.mkdir()
        self.health = self.policy_dir / "health.jsonl"
        self.policy_path = self.policy_dir / "policy.json"
        self.write_policy()

    def write_policy(self, **overrides: object) -> None:
        policy = {
            "allowed_roots": [str(self.workspace.resolve()), str(self.skill.resolve())],
            "real_home": str(self.home.resolve()),
            "health_path": str(self.health),
            "max_entries": 20000,
            "deadline_ms": 2000,
        }
        policy.update(overrides)
        self.policy_path.write_text(json.dumps(policy), encoding="utf-8")

    def run_guard(self, request: object, policy: Path | None = None) -> dict:
        completed = subprocess.run(
            [sys.executable, str(AGENT_RUNNER_ROOT / "replay_read_guard.py"), "--policy", str(policy or self.policy_path)],
            input=json.dumps(request),
            text=True,
            capture_output=True,
            timeout=30,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        value = json.loads(completed.stdout)
        self.assertIsInstance(value, dict)
        return value

    def decision(self, value: dict) -> str:
        if not value:
            return "allow"
        return value.get("hookSpecificOutput", {}).get("permissionDecision", "unknown")

    def bash(self, command: str, cwd: Path | None = None) -> dict:
        return {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": command},
            "cwd": str(cwd or self.workspace),
        }

    def health_codes(self) -> list:
        if not self.health.exists():
            return []
        return [json.loads(line)["code"] for line in self.health.read_text(encoding="utf-8").splitlines() if line]

    def test_allowed_reads_inside_workspace_and_skill(self) -> None:
        self.assertEqual(self.decision(self.run_guard(self.bash("cat src/app.py"))), "allow")
        self.assertEqual(
            self.decision(
                self.run_guard(
                    {
                        "hook_event_name": "PreToolUse",
                        "tool_name": "read_file",
                        "tool_input": {"path": str(self.skill / "references" / "routing.md")},
                        "cwd": str(self.workspace),
                    }
                )
            ),
            "allow",
        )

    def test_outside_home_operator_and_sibling_are_denied(self) -> None:
        for target in (
            self.operator / "models.json",
            self.home / ".config" / "easydev" / "models.json",
            self.sibling / "secret.txt",
            self.workspace / ".." / "operator-config" / "models.json",
        ):
            self.assertEqual(self.decision(self.run_guard(self.bash("cat %s" % target))), "deny", target)
        self.assertIn("outside_allowed", self.health_codes())

    def test_symlink_and_prefix_similar_paths_are_denied(self) -> None:
        self.assertEqual(self.decision(self.run_guard(self.bash("cat link-file"))), "deny")
        self.assertEqual(
            self.decision(
                self.run_guard(
                    {
                        "hook_event_name": "PreToolUse",
                        "tool_name": "read",
                        "tool_input": {"path": str(self.workspace / "link-dir" / "sentinel.txt")},
                        "cwd": str(self.workspace),
                    }
                )
            ),
            "deny",
        )
        prefix = str(self.workspace) + "-sibling/secret.txt"
        self.assertEqual(self.decision(self.run_guard(self.bash("cat %s" % prefix))), "deny")

    def test_explicit_system_paths_are_denied_without_opening_them(self) -> None:
        self.assertEqual(self.decision(self.run_guard(self.bash("cat /etc/hosts"))), "deny")
        self.assertEqual(
            self.decision(
                self.run_guard(
                    {
                        "hook_event_name": "PreToolUse",
                        "tool_name": "read",
                        "tool_input": {"path": "/usr/lib/python3/os.py"},
                        "cwd": str(self.workspace),
                    }
                )
            ),
            "deny",
        )

    def test_recursive_commands_require_complete_link_check(self) -> None:
        clean = self.workspace / "src"
        self.assertEqual(self.decision(self.run_guard(self.bash("rg PATTERN .", clean))), "allow")
        self.assertEqual(self.decision(self.run_guard(self.bash("grep -R PATTERN .", clean))), "allow")
        self.assertEqual(self.decision(self.run_guard(self.bash("find .", clean))), "allow")
        self.assertEqual(self.decision(self.run_guard(self.bash("ls -R .", clean))), "allow")

    def test_follow_flags_and_links_in_tree_are_denied(self) -> None:
        for command in ("rg --follow PATTERN .", "find -L .", "ls -R -L ."):
            self.assertEqual(self.decision(self.run_guard(self.bash(command))), "deny", command)
        self.assertEqual(self.decision(self.run_guard(self.bash("rg PATTERN ."))), "deny")
        self.assertEqual(self.decision(self.run_guard(self.bash("grep -R PATTERN ."))), "deny")
        self.assertEqual(self.decision(self.run_guard(self.bash("find ."))), "deny")
        self.assertEqual(self.decision(self.run_guard(self.bash("ls -R ."))), "deny")

    def test_entry_budget_and_deadline_fail_closed(self) -> None:
        tree = self.workspace / "tree"
        tree.mkdir()
        for index in range(6):
            (tree / ("file-%d.txt" % index)).write_text("x\n", encoding="utf-8")
        self.write_policy(max_entries=2)
        self.assertEqual(self.decision(self.run_guard(self.bash("ls -R tree"))), "deny")
        self.assertIn("budget_exceeded", self.health_codes())

        self.write_policy(deadline_ms=0)
        self.assertEqual(self.decision(self.run_guard(self.bash("ls -R tree"))), "deny")
        self.assertIn("deadline_exceeded", self.health_codes())

    def test_malformed_input_and_missing_policy_deny(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(AGENT_RUNNER_ROOT / "replay_read_guard.py"), "--policy", str(self.policy_path)],
            input="{not-json",
            text=True,
            capture_output=True,
            timeout=30,
        )
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(self.decision(json.loads(completed.stdout)), "deny")
        self.assertIn("input_invalid", self.health_codes())

        missing = self.policy_dir / "missing.json"
        completed = subprocess.run(
            [sys.executable, str(AGENT_RUNNER_ROOT / "replay_read_guard.py"), "--policy", str(missing)],
            input="{}",
            text=True,
            capture_output=True,
            timeout=30,
        )
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(self.decision(json.loads(completed.stdout)), "deny")

    def test_structured_tool_without_paths_is_not_empty_target(self) -> None:
        value = self.run_guard(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "read_file",
                "tool_input": {},
                "cwd": str(self.workspace),
            }
        )
        self.assertEqual(self.decision(value), "deny")
        self.assertIn("target_unresolved", self.health_codes())

    def test_unknown_event_and_non_read_tool_are_allowed(self) -> None:
        self.assertEqual(
            self.decision(
                self.run_guard(
                    {
                        "hook_event_name": "PostToolUse",
                        "tool_name": "Bash",
                        "tool_input": {"command": "cat /etc/hosts"},
                        "cwd": str(self.workspace),
                    }
                )
            ),
            "allow",
        )
        self.assertEqual(
            self.decision(
                self.run_guard(
                    {
                        "hook_event_name": "PreToolUse",
                        "tool_name": "apply_patch",
                        "tool_input": {"command": "*** Begin Patch"},
                        "cwd": str(self.workspace),
                    }
                )
            ),
            "allow",
        )

    def test_unresolved_and_option_reads_are_denied(self) -> None:
        patterns = self.outside / "patterns.txt"
        patterns.write_text("sentinel\n", encoding="utf-8")
        cases = {
            "残留变量": 'p=%s/models.json; cat "$p"' % self.operator,
            "读取型选项": "grep -f %s src/app.py" % patterns,
            "cd 目录切换": "cd %s; cat models.json" % self.operator,
            "通配符": "cat %s/*.json" % self.operator,
            "未知 cd": "cd unknown-dir; cat sentinel.txt",
        }
        for label, command in cases.items():
            with self.subTest(case=label):
                value = self.run_guard(self.bash(command))
                self.assertEqual(self.decision(value), "deny", (label, value))
        # 允许根内的同样写法仍然放行。
        self.assertEqual(
            self.decision(self.run_guard(self.bash("grep -f patterns.txt src/app.py"))), "allow"
        )

    def test_named_user_tilde_is_unresolved(self) -> None:
        # 允许根下真的存在同名目录：拒绝不能靠“文件不存在”偶然成立。
        for name in ("~root", "~other"):
            decoy = self.workspace / name
            decoy.mkdir()
            (decoy / "secret").write_text("decoy\n", encoding="utf-8")
        for target in ("~root/secret", "~other/secret"):
            value = self.run_guard(self.bash("cat %s" % target))
            self.assertEqual(self.decision(value), "deny", (target, value))
            self.assertNotIn(target, json.dumps(value, ensure_ascii=False))
        self.assertIn("target_unresolved", self.health_codes())

    def test_plain_home_tilde_keeps_policy_expansion(self) -> None:
        fake_home = self.workspace / "fakehome"
        fake_home.mkdir()
        (fake_home / "x").write_text("inside\n", encoding="utf-8")
        self.write_policy(real_home=str(fake_home))
        for command in ("cat ~/x", "cat $HOME/x", "cat ${HOME}/x"):
            self.assertEqual(self.decision(self.run_guard(self.bash(command))), "allow", command)

    def test_missing_health_is_unreadable(self) -> None:
        self.assertFalse(self.health.exists())
        self.assertEqual(guard.read_health(str(self.health)), ["health_unreadable"])

    def test_install_writes_absolute_command_and_excludes_hooks_file(self) -> None:
        run_dir = Path(self.tmp.name) / "run dir'quoted"
        workspace = run_dir / "workspace"
        workspace.mkdir(parents=True)
        (workspace / ".git" / "info").mkdir(parents=True)
        info = guard.install_replay_guard(
            str(run_dir), str(workspace), str(self.skill), str(self.home)
        )
        hooks = json.loads(Path(info["hooks_path"]).read_text(encoding="utf-8"))
        command = hooks["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        self.assertIn(info["script"].replace("'", "'\"'\"'"), command)
        self.assertIn("--policy", command)
        self.assertIn("'\"'\"'", command)
        self.assertIn(".codex/hooks.json", (workspace / ".git" / "info" / "exclude").read_text())
        probe = guard.probe(info["policy_path"])
        self.assertTrue(probe["ok"], probe)
        with self.assertRaises(RuntimeError):
            guard.install_replay_guard(
                str(run_dir), str(workspace), str(self.skill), str(self.home)
            )

    def test_install_initializes_private_empty_health(self) -> None:
        run_dir = Path(self.tmp.name) / "health run"
        workspace = run_dir / "workspace"
        workspace.mkdir(parents=True)
        info = guard.install_replay_guard(
            str(run_dir), str(workspace), str(self.skill), str(self.home)
        )
        health = Path(info["health_path"])
        self.assertTrue(health.is_file())
        self.assertEqual(health.read_bytes(), b"")
        self.assertEqual(stat.S_IMODE(health.stat().st_mode), 0o600)
        # 运行期健康日志已存在时，重新安装不得覆盖或截断。
        os.remove(info["hooks_path"])
        kept = '{"hook": "replay-read-guard", "code": "kept"}\n'
        health.write_text(kept, encoding="utf-8")
        guard.install_replay_guard(
            str(run_dir), str(workspace), str(self.skill), str(self.home)
        )
        self.assertEqual(health.read_text(encoding="utf-8"), kept)
        self.assertEqual(stat.S_IMODE(health.stat().st_mode), 0o600)

    def test_probe_fails_when_health_is_missing(self) -> None:
        run_dir = Path(self.tmp.name) / "probe-missing-health"
        workspace = run_dir / "workspace"
        workspace.mkdir(parents=True)
        info = guard.install_replay_guard(
            str(run_dir), str(workspace), str(self.skill), str(self.home)
        )
        os.remove(info["health_path"])
        result = guard.probe(info["policy_path"])
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["error"], "health_unreadable")
        self.assertEqual(result["cases"], {})
        # 探针不“修复”运行期被删除的健康日志。
        self.assertFalse(Path(info["health_path"]).exists())

    def run_runner(self, label: str, plan_values: dict, scenario: str = "b01"):
        base = Path(self.tmp.name) / label
        base.mkdir(parents=True, exist_ok=True)
        auth = base / "auth.json"
        auth.write_text("{}", encoding="utf-8")
        plan = base / "plan.json"
        plan.write_text(json.dumps(plan_values), encoding="utf-8")
        out = base / "out"
        stub = base / "codex-stub"
        shutil.copyfile(EXPERIMENTS_ROOT / "stubs" / "codex_stub.py", stub)
        os.chmod(stub, 0o755)
        completed = subprocess.run(
            [
                sys.executable,
                str(AGENT_RUNNER_ROOT / "runner.py"),
                "run",
                "--scenario",
                scenario,
                "--out",
                str(out),
                "--codex-bin",
                str(stub),
                "--codex-auth",
                str(auth),
                "--agent-timeout",
                "60",
            ],
            capture_output=True,
            text=True,
            env=clean_env({"EASYDEV_STUB_PLAN": str(plan)}),
            timeout=180,
        )
        result_file = out / "runs" / scenario / "easydev" / "r1" / "result.json"
        result = json.loads(result_file.read_text(encoding="utf-8")) if result_file.exists() else {}
        return completed, result

    def test_probe_runs_deployed_command_and_blocks_broken_import(self) -> None:
        run_dir = Path(self.tmp.name) / "probe run 'quoted"
        workspace = run_dir / "workspace"
        workspace.mkdir(parents=True)
        (workspace / "src").mkdir()
        (workspace / "src" / "app.py").write_text("print(1)\n", encoding="utf-8")
        info = guard.install_replay_guard(
            str(run_dir), str(workspace), str(self.skill), str(self.home)
        )
        # 部署件必须自带依赖：从只有分发文件的目录也能启动。
        self.assertTrue(Path(info["shell"]).is_file())
        self.assertNotIn(str(EASYDEV_ROOT / "hooks"), info["command"])
        result = guard.probe(info["policy_path"], info["command"])
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["cases"]["allow"]["decision"], "allow")
        self.assertEqual(result["cases"]["deny"]["decision"], "deny")
        self.assertEqual(result["cases"]["malformed"]["decision"], "deny")
        self.assertEqual(result["cases"]["budget"]["decision"], "deny")
        self.assertEqual(guard.read_health(info["health_path"]), [])

        # 依赖被删：命令启动即拒绝，不再放行模型。
        Path(info["shell"]).write_text("raise SystemExit(0)\n", encoding="utf-8")
        broken = guard.probe(info["policy_path"], info["command"])
        self.assertFalse(broken["ok"], broken)
        self.assertEqual(broken["cases"]["allow"]["decision"], "deny")

    def test_runner_invalid_when_hook_never_invoked(self) -> None:
        completed, result = self.run_runner(
            "never-invoked",
            {
                "events": 3,
                "agent_messages": ["采用 quick，这是单文件文案修改。", "已完成按钮修改。"],
                "mutations": [{"path": "src/ui.py", "content": 'BUTTON_LABEL = "保存"\n'}],
                "skip_hooks": True,
            },
        )
        self.assertTrue(result, completed.stderr + completed.stdout)
        self.assertEqual(result["read_guard"]["invocations"], 0)
        self.assertFalse(result["valid_invocation"])
        self.assertEqual(completed.returncode, 1, completed.stdout)

    def test_runner_invalid_when_guard_health_fails(self) -> None:
        outside = Path(self.tmp.name) / "operator-secret.txt"
        outside.write_text("secret\n", encoding="utf-8")
        completed, result = self.run_runner(
            "unhealthy",
            {
                "events": 3,
                "agent_messages": ["采用 quick，这是单文件文案修改。", "已完成按钮修改。"],
                "commands": ["cat " + str(outside)],
                "mutations": [{"path": "src/ui.py", "content": 'BUTTON_LABEL = "保存"\n'}],
                "hook_log": str(Path(self.tmp.name) / "unhealthy" / "hooks.log"),
            },
        )
        self.assertTrue(result, completed.stderr + completed.stdout)
        self.assertIn("outside_allowed", result["read_guard"]["health"])
        self.assertFalse(result["valid_invocation"])
        self.assertEqual(completed.returncode, 1, completed.stdout)

    def test_runner_invalid_when_health_deleted_after_successful_invocations(self) -> None:
        """守卫被成功调用过、但健康日志事后被删：样本必须判为无效（不可当通过证据）。"""
        base = Path(self.tmp.name) / "health-gone"
        health = base / "out" / "runs" / "b01" / "easydev" / "r1" / "policy" / "health.jsonl"
        completed, result = self.run_runner(
            "health-gone",
            {
                "events": 3,
                "agent_messages": ["采用 quick，这是单文件文案修改。", "已完成按钮修改。"],
                "mutations": [{"path": "src/ui.py", "content": 'BUTTON_LABEL = "保存"\n'}],
                # 守卫已真实调用（前面 mutation 走过 PreToolUse）之后再删健康日志。
                "delete_files": [str(health)],
            },
        )
        self.assertTrue(result, completed.stderr + completed.stdout)
        self.assertGreater(result["read_guard"]["invocations"], 0, result["read_guard"])
        self.assertIn("health_unreadable", result["read_guard"]["health"])
        self.assertFalse(result["valid_invocation"])
        self.assertEqual(completed.returncode, 1, completed.stdout)

    def test_runner_survives_deleted_guard_files(self) -> None:
        completed, result = self.run_runner(
            "tampered",
            {
                "events": 3,
                "agent_messages": ["采用 quick，这是单文件文案修改。", "已完成按钮修改。"],
                "mutations": [{"path": "src/ui.py", "content": 'BUTTON_LABEL = "保存"\n'}],
                "delete_files": [
                    str(Path(self.tmp.name) / "tampered" / "out" / "runs" / "b01" / "easydev" / "r1" / "policy" / "policy.json")
                ],
            },
        )
        self.assertTrue(result, completed.stderr + completed.stdout)
        self.assertTrue(result["read_guard"]["tampered"])
        self.assertFalse(result["valid_invocation"])
        self.assertEqual(completed.returncode, 1, completed.stdout)

    def test_runner_wires_guard_and_reports_integrity(self) -> None:
        auth = Path(self.tmp.name) / "auth.json"
        auth.write_text("{}", encoding="utf-8")
        plan = Path(self.tmp.name) / "plan.json"
        plan.write_text(
            json.dumps(
                {
                    "events": 3,
                    "agent_messages": [
                        "采用 quick，这是单文件文案修改。",
                        "已完成按钮修改。\n审查状态：自查",
                    ],
                    "mutations": [{"path": "src/ui.py", "content": 'BUTTON_LABEL = "保存"\n'}],
                }
            ),
            encoding="utf-8",
        )
        out = Path(self.tmp.name) / "out"
        stub = Path(self.tmp.name) / "codex-stub"
        shutil.copyfile(EXPERIMENTS_ROOT / "stubs" / "codex_stub.py", stub)
        os.chmod(stub, 0o755)
        completed = subprocess.run(
            [
                sys.executable,
                str(AGENT_RUNNER_ROOT / "runner.py"),
                "run",
                "--scenario",
                "b01",
                "--out",
                str(out),
                "--codex-bin",
                str(stub),
                "--codex-auth",
                str(auth),
                "--agent-timeout",
                "60",
            ],
            capture_output=True,
            text=True,
            env=clean_env({"EASYDEV_STUB_PLAN": str(plan)}),
            timeout=120,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
        result = json.loads(completed.stdout)
        self.assertTrue(result["read_guard"]["probe"]["ok"])
        self.assertFalse(result["read_guard"]["tampered"])
        self.assertTrue(result["valid_invocation"])
        self.assertNotIn(".codex/hooks.json", result["changed_paths"])
        self.assertTrue(Path(result["read_guard"]["hooks"]).exists())


class DeployedGuardCommandTests(unittest.TestCase):
    """R06/R16：部署后的真实 command；deny 时原工具不执行、不读 sentinel。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.home = base / "home"
        self.home.mkdir()
        (self.home / ".config" / "easydev").mkdir(parents=True)
        (self.home / ".config" / "easydev" / "models.json").write_text(
            '{"sentinel": true}\n', encoding="utf-8"
        )
        self.operator = base / "operator-config"
        self.operator.mkdir()
        self.sentinel = self.operator / "patterns.txt"
        self.sentinel.write_text("SENTINEL-OPERATOR-SECRET\n", encoding="utf-8")
        self.skill = base / "skill" / "easydev"
        (self.skill / "references").mkdir(parents=True)
        (self.skill / "SKILL.md").write_text("skill\n", encoding="utf-8")
        self.workspace = base / "workspace"
        (self.workspace / "src").mkdir(parents=True)
        (self.workspace / "src" / "app.py").write_text("workspace-app\n", encoding="utf-8")
        (self.workspace / "patterns.txt").write_text("workspace-patterns\n", encoding="utf-8")
        run_dir = base / "run"
        run_dir.mkdir()
        self.info = guard.install_replay_guard(
            str(run_dir), str(self.workspace), str(self.skill), str(self.home)
        )
        hooks = json.loads(Path(self.info["hooks_path"]).read_text(encoding="utf-8"))
        self.command = hooks["hooks"]["PreToolUse"][0]["hooks"][0]["command"]

    def health_codes(self) -> list:
        path = self.info["health_path"]
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as handle:
            payload = handle.read()
        return [
            json.loads(line)["code"] for line in payload.splitlines() if line.strip()
        ]

    def guard_decision(self, request: dict) -> dict:
        completed = subprocess.run(
            self.command,
            shell=True,
            input=json.dumps(request),
            text=True,
            capture_output=True,
            timeout=60,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        value = json.loads(completed.stdout)
        decision = (
            "allow"
            if not value
            else value.get("hookSpecificOutput", {}).get("permissionDecision", "unknown")
        )
        return {"decision": decision, "raw": value, "health": self.health_codes()}

    def host_run_tool(self, tool: str, tool_input: dict) -> dict:
        return self.guard_decision(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": tool,
                "tool_input": tool_input,
                "cwd": str(self.workspace),
            }
        )

    def host_run(self, command: str) -> dict:
        """最小宿主替身：只运行 guard；只有 allow 才执行原工具。"""
        request = {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": command},
            "cwd": str(self.workspace),
        }
        completed = subprocess.run(
            self.command,
            shell=True,
            input=json.dumps(request),
            text=True,
            capture_output=True,
            timeout=60,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        value = json.loads(completed.stdout)
        decision = (
            "allow"
            if not value
            else value.get("hookSpecificOutput", {}).get("permissionDecision", "unknown")
        )
        executed = None
        if decision == "allow":
            executed = subprocess.run(
                command,
                shell=True,
                cwd=str(self.workspace),
                text=True,
                capture_output=True,
                timeout=60,
            )
        return {
            "decision": decision,
            "executed": executed,
            "health": self.health_codes(),
            "raw": value,
        }

    def current_health(self) -> str:
        """本次调用写下的健康码：health 文件按调用追加，只看最后一条。"""
        codes = self.health_codes()
        return codes[-1] if codes else ""

    def find_exec_command(self, action: str) -> str:
        """`find <action> cat <sentinel> \\;`：终止符必须真的到达 find。

        `host_run` 用 `shell=True`：裸 `;` 会被 shell 吃掉，find 收不到 `-exec`
        必需的终止符，用例就变成“命令本身不完整”而不是真实布局。
        """
        return "find . " + action + " cat " + str(self.sentinel) + " \\;"

    def test_find_exec_actions_cannot_smuggle_outside_reads(self) -> None:
        """DR-02：`find -exec/-execdir/-ok` 的被执行程序不可证明，部署守卫必须拒绝，
        且原命令不执行（sentinel 内容不会出现在任何输出里）。"""
        for action in ("-exec", "-execdir", "-ok"):
            command = self.find_exec_command(action)
            with self.subTest(action=action):
                # 终止符 `;` 确实在 find 的 argv 里（否则测的只是“命令不完整”）。
                analysis = guard.shell.analyze(command)
                self.assertEqual(analysis.commands[0][-1], ";", analysis.commands)
                self.assertTrue(analysis.read_unknown, analysis.as_dict())
                result = self.host_run(command)
                self.assertEqual(result["decision"], "deny", (command, result))
                self.assertIsNone(result["executed"], command)
                self.assertEqual(self.current_health(), "target_unresolved", result)
                self.assertNotIn("SENTINEL-OPERATOR-SECRET", json.dumps(result["raw"]))
        # 对照：没有 exec 动作的 find 仍放行并真的执行。
        for command, expected in (("find .", "./src/app.py"), ("find . -name '*.py'", "./src/app.py")):
            with self.subTest(command=command):
                result = self.host_run(command)
                self.assertEqual(result["decision"], "allow", (command, result))
                self.assertIsNotNone(result["executed"], command)
                self.assertIn(expected, result["executed"].stdout)

    def test_find_exec_is_denied_at_guard_entry_without_host_stand_in(self) -> None:
        """不经宿主替身：守卫自己的入口对“带真实终止符的 find -exec”同样拒绝。"""
        policy = json.loads(Path(self.info["policy_path"]).read_text(encoding="utf-8"))
        for action in ("-exec", "-execdir", "-ok"):
            command = self.find_exec_command(action)
            with self.subTest(action=action):
                with self.assertRaises(guard.GuardFailure) as caught:
                    guard.check_request(
                        {
                            "tool_name": "Bash",
                            "tool_input": {"command": command},
                            "cwd": str(self.workspace),
                        },
                        policy,
                    )
                self.assertEqual(caught.exception.code, "target_unresolved")
                self.assertIn("不可证明", caught.exception.message)
                # 对照：同样带终止符的普通 find 不触发任何拒绝。
                guard.check_request(
                    {
                        "tool_name": "Bash",
                        "tool_input": {"command": "find . -name '*.py'"},
                        "cwd": str(self.workspace),
                    },
                    policy,
                )

    def test_unknown_cwd_from_condition_cd_is_denied(self) -> None:
        """DR-05：条件 cd 让 cwd 不可证明时，替换内的相对读取必须拒绝。"""
        command = "until cd src; do true; done; printf '%s' \"$(cat app.py)\""
        result = self.host_run(command)
        self.assertEqual(result["decision"], "deny", result)
        self.assertIsNone(result["executed"])
        self.assertEqual(self.current_health(), "target_unresolved", result)

    def test_request_cwd_relative_read_is_allowed(self) -> None:
        """DR-05：默认请求 cwd 下可证明的相对读取放行并真的读到内容。"""
        result = self.host_run("cat src/app.py")
        self.assertEqual(result["decision"], "allow", result)
        self.assertIn("workspace-app", result["executed"].stdout)

    def test_substitution_cd_into_allowed_root_is_allowed(self) -> None:
        """DR-05：`$(cd <允许根内绝对目录> && cat <根内文件>)` 在允许根内成立。"""
        inside_dir = self.workspace / "src"
        result = self.host_run("printf '%s' \"$(cd " + str(inside_dir) + " && cat app.py)\"" )
        self.assertEqual(result["decision"], "allow", result)
        self.assertIn("workspace-app", result["executed"].stdout)
        # 换到根外目录：同一写法必须拒绝。
        outside = self.host_run(
            "printf '%s' \"$(cd " + str(self.operator) + " && cat patterns.txt)\""
        )
        self.assertEqual(outside["decision"], "deny", outside)
        self.assertIsNone(outside["executed"])
        self.assertEqual(self.current_health(), "outside_allowed", outside)

    def test_default_cwd_substitution_read_is_allowed(self) -> None:
        """DR-05①：默认（请求）cwd 下的命令替换读取 → allow 且真的读到内容。"""
        result = self.host_run("printf '%s' \"$(cat src/app.py)\"")
        self.assertEqual(result["decision"], "allow", result)
        self.assertIn("workspace-app", result["executed"].stdout)

    def test_absolute_cd_after_unknown_cwd_restores_read_boundary(self) -> None:
        """DR-05②：先进入未知 cwd，再用**绝对 cd** 恢复后读取：根内 allow、根外 deny。"""
        prefix = "until cd src; do true; done; printf '%s' "
        inside = self.host_run(
            prefix + "\"$(cd " + str(self.workspace) + " && cat src/app.py)\""
        )
        self.assertEqual(inside["decision"], "allow", inside)
        self.assertIn("workspace-app", inside["executed"].stdout)
        outside = self.host_run(
            prefix + "\"$(cd " + str(self.operator) + " && cat patterns.txt)\""
        )
        self.assertEqual(outside["decision"], "deny", outside)
        self.assertIsNone(outside["executed"])
        self.assertEqual(self.current_health(), "outside_allowed", outside)

    def test_substitution_absolute_outside_read_is_denied(self) -> None:
        """DR-05：替换内的绝对根外读取与根内绝对读取组成一对对照。"""
        outside = self.host_run("printf '%s' \"$(cat " + str(self.sentinel) + ")\"")
        self.assertEqual(outside["decision"], "deny", outside)
        self.assertIsNone(outside["executed"])
        self.assertEqual(self.current_health(), "outside_allowed", outside)
        inside = self.host_run(
            "printf '%s' \"$(cat " + str(self.workspace / "src" / "app.py") + ")\""
        )
        self.assertEqual(inside["decision"], "allow", inside)
        self.assertIn("workspace-app", inside["executed"].stdout)

    def test_named_user_tilde_is_denied(self) -> None:
        decoy = self.workspace / "~root"
        decoy.mkdir()
        (decoy / "secret").write_text("decoy\n", encoding="utf-8")
        result = self.host_run("cat ~root/secret")
        self.assertEqual(result["decision"], "deny", result)
        self.assertIsNone(result["executed"])
        self.assertIn("target_unresolved", result["health"])

    def test_env_chdir_read_is_denied_at_deployed_command(self) -> None:
        result = self.host_run("env -C %s cat patterns.txt" % self.operator)
        self.assertEqual(result["decision"], "deny", result)
        self.assertIsNone(result["executed"])
        # 允许根内的同一写法仍然放行并真的读到工作区文件。
        inside = self.host_run("env -C %s cat patterns.txt" % self.workspace)
        self.assertEqual(inside["decision"], "allow", inside)
        self.assertIn("workspace-patterns", inside["executed"].stdout)

    def test_short_option_attached_pattern_file_is_denied(self) -> None:
        result = self.host_run("grep -f%s src/app.py" % self.sentinel)
        self.assertEqual(result["decision"], "deny", result)
        self.assertIsNone(result["executed"])
        inside = self.host_run("grep -fpatterns.txt src/app.py")
        self.assertEqual(inside["decision"], "allow", inside)

    def test_combined_short_options_pattern_file_is_denied(self) -> None:
        for command in (
            "grep -nf%s src/app.py" % self.sentinel,
            "grep -rf%s src/app.py" % self.sentinel,
            "rg -nf%s src/app.py" % self.sentinel,
            "grep -n -f %s src/app.py" % self.sentinel,
        ):
            with self.subTest(command=command):
                result = self.host_run(command)
                self.assertEqual(result["decision"], "deny", (command, result))
                self.assertIsNone(result["executed"])
                self.assertIn("outside_allowed", result["health"])
        # 允许根内的同一组写法放行，并且真的执行了原工具。
        inside = self.host_run("grep -nfpatterns.txt src/app.py")
        self.assertEqual(inside["decision"], "allow", inside)
        self.assertIsNotNone(inside["executed"])
        self.assertEqual(inside["executed"].returncode, 1)  # 无匹配即正常执行
        spaced = self.host_run("grep -n -f patterns.txt -- src/app.py")
        self.assertEqual(spaced["decision"], "allow", spaced)

    def test_unknown_short_option_cluster_is_denied(self) -> None:
        result = self.host_run("grep -jf%s src/app.py" % self.sentinel)
        self.assertEqual(result["decision"], "deny", result)
        self.assertIsNone(result["executed"])
        self.assertIn("target_unresolved", result["health"])

    def test_direct_link_targets_are_denied(self) -> None:
        loop = self.workspace / "self-loop"
        loop.symlink_to(loop)
        dangling = self.workspace / "dangling"
        dangling.symlink_to(self.workspace / "missing-target")
        first = self.workspace / "cycle-a"
        second = self.workspace / "cycle-b"
        first.symlink_to(second)
        second.symlink_to(first)
        healthy = self.workspace / "ok.txt"
        healthy.write_text("ok\n", encoding="utf-8")
        for tool, tool_input in (
            ("read_file", {"path": str(loop)}),
            ("read_file", {"path": str(dangling)}),
            ("read", {"path": str(first)}),
            ("ls", {"path": str(dangling)}),
            ("grep", {"path": str(loop)}),
        ):
            with self.subTest(tool=tool, target=tool_input["path"]):
                result = self.host_run_tool(tool, tool_input)
                self.assertEqual(result["decision"], "deny", (tool, tool_input, result))
                self.assertIn("symlink_unresolved", result["health"])
        for command in ("cat self-loop", "cat dangling", "cat cycle-a"):
            with self.subTest(command=command):
                result = self.host_run(command)
                self.assertEqual(result["decision"], "deny", (command, result))
                self.assertIsNone(result["executed"])
        # 对照：允许根内普通文件仍放行。
        self.assertEqual(
            self.host_run_tool("read_file", {"path": str(healthy)})["decision"], "allow"
        )

    def test_static_python_literal_read_is_denied(self) -> None:
        code = 'from pathlib import Path; print(Path("%s").read_text())' % self.sentinel
        result = self.host_run("python3 -c '%s'" % code)
        self.assertEqual(result["decision"], "deny", result)
        self.assertIsNone(result["executed"])
        inside_code = 'from pathlib import Path; print(Path("patterns.txt").read_text())'
        inside = self.host_run("python3 -c '%s'" % inside_code)
        self.assertEqual(inside["decision"], "allow", inside)
        self.assertIn("workspace-patterns", inside["executed"].stdout)

    def test_symlink_loop_and_dangling_are_denied(self) -> None:
        loop = self.workspace / "loop"
        loop.symlink_to(loop)
        dangling = self.workspace / "dangling"
        dangling.symlink_to(self.workspace / "missing-target")
        first = self.workspace / "cycle-a"
        second = self.workspace / "cycle-b"
        first.symlink_to(second)
        second.symlink_to(first)
        for command in ("rg PATTERN .", "find .", "grep -R PATTERN ."):
            with self.subTest(command=command):
                result = self.host_run(command)
                self.assertEqual(result["decision"], "deny", (command, result))
                self.assertIsNone(result["executed"])
                self.assertIn("symlink_unresolved", result["health"])
        for link in (loop, dangling, first, second):
            link.unlink()
        clean = self.host_run("rg PATTERN .")
        self.assertEqual(clean["decision"], "allow", clean)

    def test_external_link_inside_allowed_directory_is_denied(self) -> None:
        tree = self.workspace / "tree"
        tree.mkdir()
        (tree / "ok.txt").write_text("ok\n", encoding="utf-8")
        (tree / "out").symlink_to(self.operator)
        result = self.host_run("ls -R tree")
        self.assertEqual(result["decision"], "deny", result)
        self.assertIsNone(result["executed"])
        self.assertIn("outside_allowed", result["health"])

    def test_input_redirect_is_checked(self) -> None:
        """T03 关联：输入重定向的目标也是可观察读取。"""
        result = self.host_run("cat < %s" % self.sentinel)
        self.assertEqual(result["decision"], "deny", result)
        self.assertIsNone(result["executed"])
        self.assertIn("outside_allowed", result["health"])
        inside = "cat < patterns.txt"
        decision = self.host_run(inside)
        self.assertEqual(decision["decision"], "allow", decision)
        self.assertIn("workspace-patterns", decision["executed"].stdout)

    def test_copy_target_directory_sources_are_read_targets(self) -> None:
        """T04：`-t/--target-directory` 下所有位置参数都是源（含多源最后一项）。"""
        source_dir = self.workspace / "src"
        inside = self.workspace / "patterns.txt"
        for command in (
            "cp -t %s %s" % (source_dir, self.sentinel),
            "cp --target-directory %s %s" % (source_dir, self.sentinel),
            "install -t %s %s" % (source_dir, self.sentinel),
            "install --target-directory %s %s" % (source_dir, self.sentinel),
            "cp -t %s %s %s" % (source_dir, inside, self.sentinel),
        ):
            with self.subTest(command=command):
                result = self.host_run(command)
                self.assertEqual(result["decision"], "deny", (command, result))
                self.assertIsNone(result["executed"])
                self.assertIn("outside_allowed", result["health"])
        # 允许根内的源放行；本机 macOS 无 GNU -t，不执行也不宣称已真实复制。
        for command in (
            "cp -t %s %s" % (source_dir, inside),
            "install -t %s %s" % (source_dir, inside),
            "cp -t %s %s %s" % (source_dir, inside, inside),
        ):
            with self.subTest(command=command):
                result = self.host_run_tool("Bash", {"command": command})
                self.assertEqual(result["decision"], "allow", (command, result))

    def test_rg_files_roots_are_all_checked(self) -> None:
        """T06：`rg --files` 的位置参数全是根，不能把第一个当成搜索 pattern。"""
        result = self.host_run("rg --files %s" % self.operator)
        self.assertEqual(result["decision"], "deny", result)
        self.assertIsNone(result["executed"])
        self.assertIn("outside_allowed", result["health"])
        # 允许根内的软链指向外部：根同样不能绕过归属核对。
        (self.workspace / "out-link").symlink_to(self.operator)
        linked = self.host_run("rg --files out-link")
        self.assertEqual(linked["decision"], "deny", linked)
        self.assertIsNone(linked["executed"])
        # 允许根内干净目录正例：真的执行 rg，只列出工作区文件。
        inside = self.host_run("rg --files src")
        self.assertEqual(inside["decision"], "allow", inside)
        if shutil.which("rg") is None:
            # ripgrep 不是 POSIX 必备工具（ubuntu-latest 默认无），缺失时只校验
            # 守卫放行判定，不宣称原工具已执行（与 `cp -t` 的 GNU 差异同一口径）；
            # CI 先安装 ripgrep，真实执行分支由 CI 覆盖。
            return
        self.assertIsNotNone(inside["executed"])
        self.assertIn("app.py", inside["executed"].stdout)
        self.assertEqual(inside["executed"].returncode, 0)

    def test_python_heredoc_stdin_program_is_denied(self) -> None:
        """G1：解释器 stdin heredoc 不是可证明的数据消费者输入。"""
        for command in (
            "python3 - <<'PY'\nprint(open(%r).read())\nPY" % str(self.sentinel),
            "(python3 -) <<'PY'\nprint(open(%r).read())\nPY" % str(self.sentinel),
        ):
            with self.subTest(command=command):
                analysis = guard.shell.analyze(command)
                self.assertTrue(analysis.stdin_program, analysis.as_dict())
                self.assertFalse(analysis.opaque, analysis.as_dict())
                result = self.host_run(command)
                self.assertEqual(result["decision"], "deny", (command, result))
                self.assertIsNone(result["executed"], command)
                self.assertEqual(self.current_health(), "target_unresolved", result)
                self.assertNotIn("SENTINEL-OPERATOR-SECRET", json.dumps(result["raw"]))

    def test_fd_forwarded_heredoc_stdin_program_is_denied(self) -> None:
        """G1：非零 fd 的正文再经 `0<&3` 转接，仍不能证明是数据输入。"""
        command = "python3 - 3<<'PY' 0<&3\nprint(open(%r).read())\nPY" % str(self.sentinel)
        analysis = guard.shell.analyze(command)
        self.assertTrue(analysis.stdin_program, analysis.as_dict())
        self.assertFalse(analysis.opaque, analysis.as_dict())
        result = self.host_run(command)
        self.assertEqual(result["decision"], "deny", result)
        self.assertIsNone(result["executed"])
        self.assertEqual(self.current_health(), "target_unresolved", result)

    def test_piped_heredoc_stdin_program_is_denied(self) -> None:
        """G1：管道左段名为 cat 也不能把正文证明成纯数据。"""
        command = "cat <<'PY' | python3 -\nprint(open(%r).read())\nPY" % str(self.sentinel)
        analysis = guard.shell.analyze(command)
        self.assertTrue(analysis.stdin_program, analysis.as_dict())
        self.assertFalse(analysis.opaque, analysis.as_dict())
        result = self.host_run(command)
        self.assertEqual(result["decision"], "deny", result)
        self.assertIsNone(result["executed"])
        self.assertEqual(self.current_health(), "target_unresolved", result)

    def test_herestring_program_stdin_is_denied(self) -> None:
        """G1：`<<<` 供给解释器/壳的 herestring 也是 stdin 程序，不能按“无读取目标”放行。"""
        reading = "print(open(%r).read())" % str(self.sentinel)
        commands = (
            'python3 - <<< "%s"' % reading,
            "sh <<< 'echo EASYDEV-HERESTRING-SENTINEL'",
        )
        for command in commands:
            with self.subTest(command=command):
                analysis = guard.shell.analyze(command)
                # herestring 的执行/opaque/读写语义不变：正文不进入脚本分析。
                self.assertTrue(analysis.stdin_program, analysis.as_dict())
                self.assertFalse(analysis.opaque, analysis.as_dict())
                self.assertEqual(analysis.git, [])
                self.assertEqual(analysis.writes, [])
                result = self.host_run(command)
                self.assertEqual(result["decision"], "deny", (command, result))
                self.assertIsNone(result["executed"], command)
                self.assertEqual(self.current_health(), "target_unresolved", result)
                self.assertNotIn("SENTINEL-OPERATOR-SECRET", json.dumps(result["raw"]))

    def test_eval_herestring_program_stdin_is_denied(self) -> None:
        """G1：`eval 'python3 -' <<< …` 的正文随脚本文本继承 stdin，同样不能放行。"""
        reading = "print(open(%r).read())" % str(self.sentinel)
        for command in (
            'eval \'python3 -\' <<< "%s"' % reading,
            'env eval \'python3 -\' <<< "%s"' % reading,
        ):
            with self.subTest(command=command):
                analysis = guard.shell.analyze(command)
                self.assertTrue(analysis.stdin_program, analysis.as_dict())
                self.assertFalse(analysis.opaque, analysis.as_dict())
                self.assertEqual(analysis.reads, [])
                result = self.host_run(command)
                self.assertEqual(result["decision"], "deny", (command, result))
                self.assertIsNone(result["executed"], command)
                self.assertEqual(self.current_health(), "target_unresolved", result)
                self.assertNotIn("SENTINEL-OPERATOR-SECRET", json.dumps(result["raw"]))

    def test_cat_heredoc_output_and_data_are_not_stdin_program(self) -> None:
        """G1：外层写形态与普通数据 heredoc 都不带该标记，守卫照常放行。"""
        write_form = "cat > f <<'EOF'\nhello\nEOF"
        analysis = guard.shell.analyze(write_form)
        self.assertFalse(analysis.stdin_program, analysis.as_dict())
        self.assertEqual(analysis.reads, [])
        self.assertEqual(
            [(item.get("kind"), item.get("target")) for item in analysis.writes],
            [("redirect", "f")],
        )
        result = self.host_run(write_form)
        self.assertEqual(result["decision"], "allow", result)
        self.assertIsNotNone(result["executed"])
        self.assertEqual((self.workspace / "f").read_text(encoding="utf-8"), "hello\n")
        # 普通数据 heredoc：正文没有读取目标/执行证据，守卫放行。
        data = "cat <<'EOF'\nordinary data\nEOF"
        data_analysis = guard.shell.analyze(data)
        self.assertFalse(data_analysis.stdin_program, data_analysis.as_dict())
        self.assertEqual(data_analysis.reads, [])
        allowed = self.host_run(data)
        self.assertEqual(allowed["decision"], "allow", allowed)
        self.assertEqual(allowed["executed"].stdout, "ordinary data\n")


if __name__ == "__main__":
    unittest.main()
