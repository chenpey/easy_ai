"""安装生成、合并、卸载、分发包启动与 pi 适配器（A0 / F04 / F05 / F23）。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

from support import HOOKS_ROOT, HookCase, clean_env

INSTALL = HOOKS_ROOT / "hooks" / "install.py"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "pi_adapter_fixture.mjs"
ANTIGRAVITY_GROUP = "easydev-hooks"


class InstallTests(HookCase):
    host = "codex"

    def generate(self, host: str = "codex", output: Path | None = None, mode: str = "probe", root: Path | None = None) -> dict:
        output = output or (self.root / "generated")
        completed = subprocess.run(
            [
                sys.executable,
                str(INSTALL),
                "generate",
                "--host",
                host,
                "--plugin-root",
                str(root or HOOKS_ROOT / "hooks"),
                "--skill-root",
                str(self.skill),
                "--output",
                str(output),
                "--mode",
                mode,
            ],
            capture_output=True,
            text=True,
            env=clean_env(),
            timeout=30,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

    def test_generate_writes_only_output_dir(self) -> None:
        before = sorted(path.name for path in self.root.iterdir())
        result = self.generate("codex", mode="enforce")
        output = Path(result["output"])
        self.assertTrue(output.is_dir())
        context = json.loads((output / "install-context.json").read_text(encoding="utf-8"))
        self.assertEqual(context["host"], "codex")
        self.assertEqual(context["mode"], "enforce")
        release = json.loads(
            (HOOKS_ROOT / "hooks" / "manifest.json").read_text(encoding="utf-8")
        )["release"]
        self.assertEqual(context["version"], release)
        self.assertEqual(context["skill_root"], str(self.skill))
        self.assertEqual(Path(context["plugin_root"]), HOOKS_ROOT / "hooks")
        manifest = json.loads((output / "install-manifest.json").read_text(encoding="utf-8"))
        self.assertFalse(manifest["install_skill"])
        self.assertFalse(manifest["install_tests"])
        files = " ".join(manifest["files"])
        self.assertNotIn("tests/", files)
        self.assertNotIn("SKILL.md", files)
        self.assertNotIn("references/", files)
        self.assertIn("hooks/core.py".replace("hooks/", ""), manifest["files"])
        # 清单条目带完整标识/摘要，apply/uninstall 不再靠路径子串。
        self.assertTrue(manifest["entries"])
        for item in manifest["entries"]:
            self.assertTrue(item["identity"])
            self.assertTrue(item["digest"])
            self.assertIn("--context", item["command"])
        # 只在生成目录内产生文件，不改宿主配置。
        for name in ("codex-hooks.json", "install-context.json", "install-manifest.json"):
            self.assertTrue((output / name).exists(), name)

    def test_command_is_absolute_and_safely_quoted(self) -> None:
        output = self.root / "out dir'with$dollar`tick"
        result = self.generate("codex", output=output)
        command = result["command"]
        self.assertTrue(os.path.isabs(command.split(" ")[0]))
        self.assertIn(str(output / "install-context.json").replace("'", "'\"'\"'"), command)
        config = json.loads((output / "codex-hooks.json").read_text(encoding="utf-8"))
        hook_command = config["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        self.assertEqual(hook_command, command)

    def test_all_hosts_generate_declared_events(self) -> None:
        for host in ("antigravity", "codex", "dsh"):
            result = self.generate(host)
            config = json.loads(Path(result["config"]).read_text(encoding="utf-8"))
            # Antigravity 用命名 Hook 组，Codex/DSH 用顶层 hooks。
            group = ANTIGRAVITY_GROUP if host == "antigravity" else "hooks"
            self.assertIn(group, config)
            buckets = config[group]
            for event in ("SessionStart", "PreToolUse", "PostToolUse", "Stop"):
                self.assertIn(event, buckets, host)
            text = Path(result["config"]).read_text(encoding="utf-8")
            self.assertNotIn("additionalContext", text)
            self.assertNotIn("overwrite", text)
        plugin = self.copied_plugin("plugin-pi")
        pi = self.generate("pi", root=plugin)
        self.assertIsNone(pi["config"])
        manifest = json.loads(Path(pi["manifest"]).read_text(encoding="utf-8"))
        self.assertTrue(manifest["extension_entry"].endswith("index.ts"))
        # pi 的 context 必须落在扩展实际读取的位置，并记录绝对解释器。
        plugin_context = Path(pi["plugin_context"])
        self.assertEqual(plugin_context, plugin / "install-context.json")
        value = json.loads(plugin_context.read_text(encoding="utf-8"))
        self.assertEqual(value["python"], os.path.realpath(sys.executable))

    def test_apply_is_idempotent_preserves_others_and_uninstall(self) -> None:
        result = self.generate("codex")
        source = result["config"]
        manifest = result["manifest"]
        target = self.root / "hooks.json"
        other = {
            "hooks": {
                "PreToolUse": [
                    {"matcher": ".*", "hooks": [{"type": "command", "command": "other-hook"}]}
                ]
            },
            "custom": {"keep": True},
        }
        target.write_text(json.dumps(other), encoding="utf-8")
        first = self.run_install(["apply", "--target", str(target), "--source", source])
        self.assertTrue(first["changed"])
        merged = json.loads(target.read_text(encoding="utf-8"))
        commands = [
            hook["command"]
            for entry in merged["hooks"]["PreToolUse"]
            for hook in entry.get("hooks", [])
        ]
        self.assertIn("other-hook", commands)
        self.assertTrue(any("core.py" in command for command in commands))
        self.assertEqual(merged["custom"], {"keep": True})
        backup = Path(first["backup"])
        self.assertTrue(backup.exists())
        self.assertEqual(backup.read_text(encoding="utf-8"), json.dumps(other))
        second = self.run_install(["apply", "--target", str(target), "--source", source])
        self.assertFalse(second["changed"])

        removed = self.run_install(["uninstall", "--target", str(target), "--manifest", manifest])
        self.assertTrue(removed["changed"])
        after = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(after["hooks"]["PreToolUse"], other["hooks"]["PreToolUse"])
        self.assertEqual(after["custom"], {"keep": True})
        again = self.run_install(["uninstall", "--target", str(target), "--manifest", manifest])
        self.assertFalse(again["changed"])

    def test_apply_conflict_and_unsafe_target(self) -> None:
        result = self.generate("codex")
        target = self.root / "hooks.json"
        conflict = {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": ".*",
                        "hooks": [
                            {
                                "type": "command",
                                "command": str(HOOKS_ROOT / "hooks" / "core.py") + " --old",
                            }
                        ],
                    }
                ]
            }
        }
        target.write_text(json.dumps(conflict), encoding="utf-8")
        failed = subprocess.run(
            [
                sys.executable,
                str(INSTALL),
                "apply",
                "--target",
                str(target),
                "--source",
                result["config"],
            ],
            capture_output=True,
            text=True,
            env=clean_env(),
            timeout=30,
        )
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("同标识", failed.stderr)

        link = self.root / "link.json"
        link.symlink_to(target)
        failed = subprocess.run(
            [sys.executable, str(INSTALL), "apply", "--target", str(link), "--source", result["config"]],
            capture_output=True,
            text=True,
            env=clean_env(),
            timeout=30,
        )
        self.assertNotEqual(failed.returncode, 0)

    # ---- 工具 ------------------------------------------------------
    def copied_plugin(self, name: str = "plugin-rand0m") -> Path:
        """按 manifest.distribution.files 构建独立 Plugin 目录（随机名）。"""
        manifest = json.loads((HOOKS_ROOT / "hooks" / "manifest.json").read_text(encoding="utf-8"))
        plugin = self.root / name
        for relative in manifest["distribution"]["files"]:
            source = HOOKS_ROOT / "hooks" / relative
            destination = plugin / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        return plugin

    def run_install(self, args: list) -> dict:
        completed = subprocess.run(
            [sys.executable, str(INSTALL)] + args,
            capture_output=True,
            text=True,
            env=clean_env(),
            timeout=30,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

    def install_failure(self, args: list) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(INSTALL)] + args,
            capture_output=True,
            text=True,
            env=clean_env(),
            timeout=30,
        )

    def commands_for(self, target: Path, event: str) -> list:
        config = json.loads(target.read_text(encoding="utf-8"))
        group = config.get(ANTIGRAVITY_GROUP) if isinstance(config.get(ANTIGRAVITY_GROUP), dict) else config.get("hooks", {})
        bucket = group.get(event) or []
        return [
            hook["command"] for entry in bucket for hook in (entry.get("hooks") or [entry])
            if isinstance(hook, dict) and isinstance(hook.get("command"), str)
        ]


class DistributionLaunchTests(HookCase):
    """F04：只用 manifest 分发集构建随机命名 Plugin，从仓库外 cwd 启动。"""

    host = "pi"

    def test_distributed_plugin_runs_core_and_installer_outside_repo(self) -> None:
        manifest = json.loads((HOOKS_ROOT / "hooks" / "manifest.json").read_text(encoding="utf-8"))
        plugin = self.root / "plugin-x9f2"
        for relative in manifest["distribution"]["files"]:
            destination = plugin / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(HOOKS_ROOT / "hooks" / relative, destination)
        self.assertTrue((plugin / "manifest.json").is_file())
        self.assertFalse((plugin / "hooks").exists())

        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        payload = {
            "event": "input",
            "sessionId": "s1",
            "prompt": "使用 easydev，完成这个改动",
            "cwd": str(self.repo),
        }
        completed = subprocess.run(
            [sys.executable, str(plugin / "core.py"), "--host", "pi", "--context", str(self.context_path)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            cwd=str(elsewhere),
            env=clean_env(),
            timeout=60,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout), {"action": "continue"})
        self.assertNotIn("No module named 'hooks'", completed.stderr)

        output = self.root / "generated"
        generated = subprocess.run(
            [
                sys.executable,
                str(plugin / "install.py"),
                "generate",
                "--host",
                "codex",
                "--plugin-root",
                str(plugin),
                "--skill-root",
                str(self.skill),
                "--output",
                str(output),
                "--mode",
                "enforce",
            ],
            capture_output=True,
            text=True,
            cwd=str(elsewhere),
            env=clean_env(),
            timeout=60,
        )
        self.assertEqual(generated.returncode, 0, generated.stderr)
        self.assertTrue((output / "codex-hooks.json").is_file())
        installed = json.loads((output / "install-manifest.json").read_text(encoding="utf-8"))
        self.assertIn("core.py", " ".join(installed["files"]))
        self.assertTrue(str(installed["plugin_root"]).startswith(str(plugin)))


class PiExtensionTests(HookCase):
    """F05：按文档复制 Plugin 树 + generate 后，pi 适配器调真实 core 并阻断。"""

    host = "pi"

    def build(self, name: str = "easydev-hooks") -> Path:
        manifest = json.loads((HOOKS_ROOT / "hooks" / "manifest.json").read_text(encoding="utf-8"))
        plugin = self.root / name
        for relative in manifest["distribution"]["files"]:
            destination = plugin / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(HOOKS_ROOT / "hooks" / relative, destination)
        output = self.root / "generated"
        completed = subprocess.run(
            [
                sys.executable,
                str(plugin / "install.py"),
                "generate",
                "--host",
                "pi",
                "--plugin-root",
                str(plugin),
                "--skill-root",
                str(self.skill),
                "--output",
                str(output),
                "--mode",
                "enforce",
            ],
            capture_output=True,
            text=True,
            env=clean_env(),
            timeout=60,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        context_path = plugin / "install-context.json"
        source = plugin / "pi" / "index.ts"
        self.assertTrue(context_path.is_file())
        self.assertNotIn("shell", source.read_text(encoding="utf-8"))
        return plugin

    def test_extension_entry_calls_real_core_and_blocks(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("node 不可用，无法运行 pi 适配夹具")
        plugin = self.build()
        context_path = plugin / "install-context.json"
        context = json.loads(context_path.read_text(encoding="utf-8"))
        # 安装上下文本就可显式选择同等私有状态根；夹具用它避免落到共享临时根。
        context.update({"state_root": str(self.state), "repo_root": str(self.repo)})
        context_path.write_text(json.dumps(context), encoding="utf-8")

        spec = {
            "entry": str(plugin / "index.ts"),
            "plugin": str(plugin),
            "repo": str(self.repo),
            "session": "pi-session",
            "activation": "使用 easydev 执行只读 review，不要修改代码",
            "calls": [
                {
                    "toolName": "write",
                    "toolCallId": "w1",
                    "input": {"path": str(self.repo / "src" / "app.py"), "content": "x"},
                },
                {"toolName": "bash", "toolCallId": "t1", "input": {"command": "git reset --hard"}},
                {"toolName": "bash", "toolCallId": "t2", "input": {"command": "git status --short"}},
            ],
        }
        completed = subprocess.run(
            [node, str(FIXTURE), json.dumps(spec)],
            capture_output=True,
            text=True,
            cwd=str(self.root),
            # 清空 PATH：适配器若回落 PATH 里的 python3 就起不来核心，从而暴露问题。
            env=clean_env({"PATH": "/nonexistent"}),
            timeout=120,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        value = json.loads(completed.stdout)
        self.assertIn("tool_call", value["handlers"])
        write_result, reset_result, status_result = value["results"]
        # 只读 review：写工具被真实核心拒绝。
        self.assertIs(write_result["value"]["block"], True)
        # 破坏性 Git：真实核心拒绝。
        self.assertIs(reset_result["value"]["block"], True)
        # 只读命令放行。
        self.assertIsNone(status_result["value"])
        # 核心确实运行过：私有状态已由本次调用写出。
        self.assertTrue(any((self.state / "actors").glob("*.json")))


class AntigravityNamedGroupTests(HookCase):
    """B：Antigravity 命名 Hook 组、逐事件 `--event`、apply/uninstall 只动本组。"""

    host = "antigravity"

    def setUp(self) -> None:
        super().setUp()
        self.target = self.root / "hooks.json"
        self.other = {
            "easydev-hooks": {
                "PreToolUse": [
                    {"matcher": "*", "hooks": [{"type": "command", "command": "other-hook"}]}
                ]
            },
            "user-hooks": {
                "Stop": [{"hooks": [{"type": "command", "command": "stop-hook"}]}]
            },
            "custom": {"keep": True},
        }
        self.target.write_text(json.dumps(self.other), encoding="utf-8")

    def generate(self, mode: str = "probe") -> dict:
        output = self.root / "generated"
        completed = subprocess.run(
            [
                sys.executable,
                str(INSTALL),
                "generate",
                "--host",
                "antigravity",
                "--plugin-root",
                str(HOOKS_ROOT / "hooks"),
                "--skill-root",
                str(self.skill),
                "--output",
                str(output),
                "--mode",
                mode,
            ],
            capture_output=True,
            text=True,
            env=clean_env(),
            timeout=60,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

    def run_install(self, args: list) -> dict:
        completed = subprocess.run(
            [sys.executable, str(INSTALL)] + args,
            capture_output=True,
            text=True,
            env=clean_env(),
            timeout=60,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

    def group(self) -> dict:
        return json.loads(self.target.read_text(encoding="utf-8"))[ANTIGRAVITY_GROUP]

    def test_generated_config_uses_named_group_and_per_event_command(self) -> None:
        result = self.generate("enforce")
        config = json.loads(Path(result["config"]).read_text(encoding="utf-8"))
        self.assertEqual(list(config), [ANTIGRAVITY_GROUP])
        buckets = config[ANTIGRAVITY_GROUP]
        self.assertEqual(
            sorted(buckets),
            ["PostToolUse", "PreInvocation", "PreToolUse", "SessionStart", "Stop"],
        )
        for event, entries in buckets.items():
            # 官方两种结构：工具事件 Grouped（matcher + hooks[]），其余 Flat。
            if event in ("PreToolUse", "PostToolUse"):
                self.assertIsInstance(entries[0].get("hooks"), list, event)
                self.assertEqual(entries[0]["matcher"], "*", event)
                hook = entries[0]["hooks"][0]
            else:
                self.assertNotIn("hooks", entries[0], event)
                hook = entries[0]
            self.assertEqual(hook["type"], "command", event)
            self.assertIn("--event %s" % event, hook["command"], event)
            self.assertIn("--mode enforce", hook["command"])
        manifest = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
        self.assertEqual({item["group"] for item in manifest["entries"]}, {ANTIGRAVITY_GROUP})

    def test_apply_and_uninstall_touch_only_own_named_group(self) -> None:
        result = self.generate()
        applied = self.run_install(["apply", "--target", str(self.target), "--source", result["config"]])
        self.assertTrue(applied["changed"])
        value = json.loads(self.target.read_text(encoding="utf-8"))
        commands = [
            hook["command"]
            for entry in value[ANTIGRAVITY_GROUP]["PreToolUse"]
            for hook in entry.get("hooks", [])
        ]
        self.assertIn("other-hook", commands)
        easydev = [command for command in commands if "core.py" in command]
        self.assertEqual(len(easydev), 1)
        self.assertIn("--event PreToolUse", easydev[0])
        self.assertEqual(value["user-hooks"], self.other["user-hooks"])
        self.assertEqual(value["custom"], {"keep": True})
        self.assertNotIn("hooks", value)

        again = self.run_install(["apply", "--target", str(self.target), "--source", result["config"]])
        self.assertFalse(again["changed"])

        removed = self.run_install(["uninstall", "--target", str(self.target), "--manifest", result["manifest"]])
        self.assertTrue(removed["changed"])
        after = json.loads(self.target.read_text(encoding="utf-8"))
        # 用户自己的 easydev-hooks 条目保留（本组不清空），其他命名组原样。
        self.assertEqual(after[ANTIGRAVITY_GROUP], self.other[ANTIGRAVITY_GROUP])
        self.assertEqual(after["user-hooks"], self.other["user-hooks"])
        self.assertEqual(after["custom"], {"keep": True})

    def test_flat_non_tool_events_apply_and_uninstall(self) -> None:
        result = self.generate("enforce")
        self.run_install(["apply", "--target", str(self.target), "--source", result["config"]])
        group = self.group()
        for event in ("SessionStart", "PreInvocation", "Stop"):
            easydev = [entry for entry in group[event] if "core.py" in entry.get("command", "")]
            self.assertEqual(len(easydev), 1, event)
            self.assertNotIn("hooks", easydev[0], event)
            self.assertIn("--event %s" % event, easydev[0]["command"], event)
        self.run_install(["uninstall", "--target", str(self.target), "--manifest", result["manifest"]])
        self.assertEqual(self.group(), self.other[ANTIGRAVITY_GROUP])

    def test_user_modified_flat_entry_is_preserved(self) -> None:
        result = self.generate()
        self.run_install(["apply", "--target", str(self.target), "--source", result["config"]])
        value = json.loads(self.target.read_text(encoding="utf-8"))
        value[ANTIGRAVITY_GROUP]["Stop"][0]["command"] += " --user-flag"
        self.target.write_text(json.dumps(value), encoding="utf-8")
        self.run_install(["uninstall", "--target", str(self.target), "--manifest", result["manifest"]])
        after = json.loads(self.target.read_text(encoding="utf-8"))
        stop = after[ANTIGRAVITY_GROUP]["Stop"]
        self.assertEqual(len(stop), 1, stop)
        self.assertTrue(stop[0]["command"].endswith("--user-flag"), stop)

    def test_empty_own_group_is_removed_on_uninstall(self) -> None:
        target = self.root / "clean-hooks.json"
        target.write_text(json.dumps({"user-hooks": self.other["user-hooks"]}), encoding="utf-8")
        result = self.generate()
        self.run_install(["apply", "--target", str(target), "--source", result["config"]])
        value = json.loads(target.read_text(encoding="utf-8"))
        self.assertIn(ANTIGRAVITY_GROUP, value)
        self.run_install(["uninstall", "--target", str(target), "--manifest", result["manifest"]])
        after = json.loads(target.read_text(encoding="utf-8"))
        self.assertNotIn(ANTIGRAVITY_GROUP, after)
        self.assertEqual(after["user-hooks"], self.other["user-hooks"])

    def test_probe_to_enforce_replaces_in_named_group(self) -> None:
        probe = self.generate("probe")
        self.run_install(["apply", "--target", str(self.target), "--source", probe["config"]])
        enforce = self.generate("enforce")
        self.run_install(["apply", "--target", str(self.target), "--source", enforce["config"]])
        commands = [
            hook["command"]
            for entry in self.group()["PreToolUse"]
            for hook in entry.get("hooks", [])
            if "core.py" in hook.get("command", "")
        ]
        self.assertEqual(len(commands), 1)
        self.assertIn("--mode enforce", commands[0])


class ApplyManifestTests(HookCase):
    """F23：安装清单驱动的 apply/uninstall。"""

    host = "codex"

    def setUp(self) -> None:
        super().setUp()
        self.target = self.root / "hooks.json"
        self.other = {
            "hooks": {
                "PreToolUse": [
                    {"matcher": ".*", "hooks": [{"type": "command", "command": "other-hook"}]}
                ]
            },
            "custom": {"keep": True},
        }
        self.target.write_text(json.dumps(self.other), encoding="utf-8")

    def generate(self, plugin_root: Path, mode: str = "probe") -> dict:
        output = self.root / "generated"
        completed = subprocess.run(
            [
                sys.executable,
                str(plugin_root / "install.py"),
                "generate",
                "--host",
                "codex",
                "--plugin-root",
                str(plugin_root),
                "--skill-root",
                str(self.skill),
                "--output",
                str(output),
                "--mode",
                mode,
            ],
            capture_output=True,
            text=True,
            env=clean_env(),
            timeout=60,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

    def copied_plugin(self, name: str = "plugin-rand0m") -> Path:
        manifest = json.loads((HOOKS_ROOT / "hooks" / "manifest.json").read_text(encoding="utf-8"))
        plugin = self.root / name
        for relative in manifest["distribution"]["files"]:
            destination = plugin / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(HOOKS_ROOT / "hooks" / relative, destination)
        return plugin

    def run_install(self, args: list) -> dict:
        completed = subprocess.run(
            [sys.executable, str(INSTALL)] + args,
            capture_output=True,
            text=True,
            env=clean_env(),
            timeout=60,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

    def commands(self, event: str = "PreToolUse") -> list:
        value = json.loads(self.target.read_text(encoding="utf-8"))
        bucket = value.get("hooks", {}).get(event) or []
        return [
            hook["command"]
            for entry in bucket
            for hook in (entry.get("hooks") or [entry])
            if isinstance(hook, dict) and isinstance(hook.get("command"), str)
        ]

    def test_default_usage_needs_no_plugin_root(self) -> None:
        plugin = self.copied_plugin()
        result = self.generate(plugin)
        applied = self.run_install(["apply", "--target", str(self.target), "--source", result["config"]])
        self.assertTrue(applied["changed"])
        commands = self.commands()
        self.assertIn("other-hook", commands)
        easydev = [command for command in commands if "core.py" in command]
        self.assertEqual(len(easydev), 1)
        self.assertIn("--mode probe", easydev[0])
        self.assertEqual(json.loads(self.target.read_text(encoding="utf-8"))["custom"], {"keep": True})

    def test_probe_to_enforce_replaces_instead_of_appending(self) -> None:
        plugin = self.copied_plugin()
        probe = self.generate(plugin, mode="probe")
        self.run_install(["apply", "--target", str(self.target), "--source", probe["config"]])
        enforce = self.generate(plugin, mode="enforce")
        self.run_install(["apply", "--target", str(self.target), "--source", enforce["config"]])
        commands = self.commands()
        self.assertEqual(len([command for command in commands if "core.py" in command]), 1)
        self.assertTrue(any("--mode enforce" in command for command in commands))
        self.assertFalse(any("--mode probe" in command for command in commands))
        self.assertIn("other-hook", commands)

    def test_user_modified_entry_is_preserved_and_reported(self) -> None:
        plugin = self.copied_plugin()
        result = self.generate(plugin)
        self.run_install(["apply", "--target", str(self.target), "--source", result["config"]])
        value = json.loads(self.target.read_text(encoding="utf-8"))
        hooks = value["hooks"]["PreToolUse"]
        target_entry = [entry for entry in hooks if "core.py" in json.dumps(entry)][0]
        target_entry["hooks"][0]["command"] = target_entry["hooks"][0]["command"] + " --user-edit"
        self.target.write_text(json.dumps(value), encoding="utf-8")
        modified = json.dumps(target_entry, sort_keys=True)

        failed = subprocess.run(
            [sys.executable, str(INSTALL), "apply", "--target", str(self.target), "--source", result["config"]],
            capture_output=True,
            text=True,
            env=clean_env(),
            timeout=60,
        )
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("同标识", failed.stderr)
        self.assertIn(modified, json.dumps(json.loads(self.target.read_text(encoding="utf-8")), sort_keys=True))

        removed = self.run_install(["uninstall", "--target", str(self.target), "--manifest", result["manifest"]])
        self.assertTrue(removed["changed"])
        # 用户修改过的条目必须保留，其余仍与清单一致的条目被移除。
        after = json.dumps(json.loads(self.target.read_text(encoding="utf-8")), sort_keys=True)
        self.assertIn(modified, after)
        self.assertEqual(len([command for command in self.commands() if "core.py" in command]), 1)
        again = self.run_install(["uninstall", "--target", str(self.target), "--manifest", result["manifest"]])
        self.assertFalse(again["changed"])

    def test_plugin_root_with_special_characters(self) -> None:
        plugin = self.copied_plugin("plug in'with$dollar`tick")
        result = self.generate(plugin)
        self.run_install(["apply", "--target", str(self.target), "--source", result["config"]])
        commands = self.commands()
        self.assertEqual(len([command for command in commands if "core.py" in command]), 1)
        removed = self.run_install(["uninstall", "--target", str(self.target), "--manifest", result["manifest"]])
        self.assertTrue(removed["changed"])
        self.assertEqual(self.commands(), ["other-hook"])

    def test_repeated_uninstall_is_idempotent(self) -> None:
        plugin = self.copied_plugin()
        result = self.generate(plugin)
        self.run_install(["apply", "--target", str(self.target), "--source", result["config"]])
        first = self.run_install(["uninstall", "--target", str(self.target), "--manifest", result["manifest"]])
        self.assertTrue(first["changed"])
        second = self.run_install(["uninstall", "--target", str(self.target), "--manifest", result["manifest"]])
        self.assertFalse(second["changed"])
        value = json.loads(self.target.read_text(encoding="utf-8"))
        self.assertEqual(value["hooks"]["PreToolUse"], self.other["hooks"]["PreToolUse"])
        self.assertEqual(value["custom"], {"keep": True})


class GenerateLayoutAndPruneTests(HookCase):
    """1.4.3：默认 enforce、`--output-root` 稳定布局、旧版本生成目录清理。"""

    host = "codex"

    def _install(self, *args: str) -> dict:
        completed = subprocess.run(
            [sys.executable, str(INSTALL), *args],
            capture_output=True,
            text=True,
            env=clean_env(),
            timeout=60,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

    def _generate(
        self,
        output: Path | None = None,
        output_root: Path | None = None,
        host: str = "codex",
        mode: str | None = None,
    ) -> dict:
        args = [
            "generate",
            "--host",
            host,
            "--plugin-root",
            str(HOOKS_ROOT / "hooks"),
            "--skill-root",
            str(self.skill),
        ]
        if output is not None:
            args += ["--output", str(output)]
        else:
            args += ["--output-root", str(output_root)]
        if mode is not None:
            args += ["--mode", mode]
        return self._install(*args)

    def _old_release(self, config_root: Path, version: str, host: str = "codex") -> Path:
        """模拟上一代 release 留下的生成目录：目录名与清单 release 一致。"""
        target = config_root / version / host
        self._generate(output=target, host=host, mode="enforce")
        manifest_path = target / "install-manifest.json"
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
        value["release"] = version
        manifest_path.write_text(json.dumps(value), encoding="utf-8")
        context_path = target / "install-context.json"
        context = json.loads(context_path.read_text(encoding="utf-8"))
        context["version"] = version
        context_path.write_text(json.dumps(context), encoding="utf-8")
        return config_root / version

    def test_generate_defaults_to_enforce_and_uses_stable_layout(self) -> None:
        config_root = self.root / "config"
        result = self._generate(output_root=config_root)
        self.assertEqual(result["mode"], "enforce")
        self.assertEqual(Path(result["output"]), config_root / "codex")
        self.assertEqual(result["stale"], [])
        context = json.loads((config_root / "codex" / "install-context.json").read_text(encoding="utf-8"))
        self.assertEqual(context["mode"], "enforce")
        # 再次生成同一稳定目录：原地覆盖，不产生版本号目录。
        self._generate(output_root=config_root, mode="probe")
        self.assertEqual(sorted(item.name for item in config_root.iterdir()), ["codex"])
        context = json.loads((config_root / "codex" / "install-context.json").read_text(encoding="utf-8"))
        self.assertEqual(context["mode"], "probe")

    def test_generate_requires_exactly_one_output_target(self) -> None:
        base = [
            sys.executable,
            str(INSTALL),
            "generate",
            "--host",
            "codex",
            "--plugin-root",
            str(HOOKS_ROOT / "hooks"),
            "--skill-root",
            str(self.skill),
        ]
        both = subprocess.run(
            base + ["--output", str(self.root / "a"), "--output-root", str(self.root / "b")],
            capture_output=True,
            text=True,
            env=clean_env(),
            timeout=60,
        )
        self.assertEqual(both.returncode, 2)
        neither = subprocess.run(base, capture_output=True, text=True, env=clean_env(), timeout=60)
        self.assertEqual(neither.returncode, 2)
        self.assertFalse((self.root / "a").exists())

    def test_generate_reports_stale_release_dirs(self) -> None:
        config_root = self.root / "config"
        old = self._old_release(config_root, "0.9.0")
        result = self._generate(output_root=config_root)
        self.assertEqual(result["stale"], [str(old)])

    def test_prune_dry_run_then_apply_keeps_current_and_keep(self) -> None:
        config_root = self.root / "config"
        old = self._old_release(config_root, "0.9.0")
        kept = self._old_release(config_root, "0.9.1")
        self._generate(output_root=config_root)

        dry = self._install("prune", "--root", str(config_root))
        self.assertTrue(dry["dry_run"])
        self.assertEqual(dry["removed"], [os.path.realpath(old), os.path.realpath(kept)])
        self.assertTrue(old.is_dir() and kept.is_dir())

        applied = self._install("prune", "--root", str(config_root), "--keep", "0.9.1", "--apply")
        self.assertFalse(applied["dry_run"])
        self.assertEqual(applied["removed"], [os.path.realpath(old)])
        self.assertFalse(old.exists())
        self.assertTrue(kept.is_dir())
        self.assertIn("note", applied)
        # 当前 release 与稳定布局的宿主目录都不能被删。
        self.assertTrue((config_root / "codex").is_dir())
        self.assertEqual(
            [item["reason"] for item in applied["skipped"]],
            ["当前或显式保留的版本"],
        )

    def test_prune_skips_user_content_symlinks_and_foreign_manifests(self) -> None:
        config_root = self.root / "config"
        notes = self._old_release(config_root, "0.8.0")
        (notes / "codex" / "NOTES.md").write_text("mine\n", encoding="utf-8")
        foreign = config_root / "0.8.1" / "codex"
        foreign.mkdir(parents=True)
        (foreign / "install-manifest.json").write_text(
            json.dumps({"name": "other-hooks", "release": "0.8.1"}), encoding="utf-8"
        )
        unexpected = config_root / "0.8.2"
        (unexpected / "notes").mkdir(parents=True)
        (unexpected / "notes" / "a.txt").write_text("x\n", encoding="utf-8")
        real = config_root / "real"
        (real / "codex").mkdir(parents=True)
        (real / "codex" / "install-manifest.json").write_text(
            json.dumps({"name": "easydev-hooks", "release": "0.8.3"}), encoding="utf-8"
        )
        (config_root / "0.8.3").symlink_to(real)

        result = self._install("prune", "--root", str(config_root), "--apply")
        self.assertEqual(result["removed"], [])
        reasons = " ".join(item["reason"] for item in result["skipped"])
        self.assertIn("未知文件", reasons)
        self.assertIn("不是本插件生成", reasons)
        self.assertIn("未知子目录", reasons)
        self.assertIn("软链", reasons)
        for path in (notes, config_root / "0.8.1", unexpected, config_root / "0.8.3"):
            self.assertTrue(os.path.lexists(path), path)

    def test_prune_rejects_symlinked_root(self) -> None:
        real_root = self.root / "real-config"
        real_root.mkdir()
        link = self.root / "link-config"
        link.symlink_to(real_root)
        completed = subprocess.run(
            [sys.executable, str(INSTALL), "prune", "--root", str(link)],
            capture_output=True,
            text=True,
            env=clean_env(),
            timeout=60,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("非软链", completed.stderr)

    def test_upgrade_never_overwrites_user_modified_entry(self) -> None:
        config_root = self.root / "config"
        target = self.root / "hooks.json"
        target.write_text(json.dumps({"hooks": {}}), encoding="utf-8")
        first = self._generate(output_root=config_root)
        self._install("apply", "--target", str(target), "--source", first["config"])
        value = json.loads(target.read_text(encoding="utf-8"))
        entry = [item for item in value["hooks"]["PreToolUse"] if "core.py" in json.dumps(item)][0]
        entry["hooks"][0]["command"] += " --user-edit"
        target.write_text(json.dumps(value), encoding="utf-8")
        edited = json.dumps(entry, sort_keys=True)

        # 升级 = 同一稳定目录重新生成 + 再次 apply。
        upgraded = self._generate(output_root=config_root, mode="enforce")
        failed = subprocess.run(
            [sys.executable, str(INSTALL), "apply", "--target", str(target), "--source", upgraded["config"]],
            capture_output=True,
            text=True,
            env=clean_env(),
            timeout=60,
        )
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("同标识", failed.stderr)
        after = json.dumps(json.loads(target.read_text(encoding="utf-8")), sort_keys=True)
        self.assertIn(edited, after)


if __name__ == "__main__":
    unittest.main()
