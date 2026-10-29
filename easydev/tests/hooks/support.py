"""共享夹具：在进程内调用 core.run() 驱动四宿主 envelope。

真正的 CLI 入口（stdin/stdout、解释器启动、安装与守卫脚本）仍用子进程测；规则与
协议用例直接调用 `core.run(argv, stream, env)`，避免每个用例付一次解释器启动。
"""

from __future__ import annotations

import atexit
import contextlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

HOOKS_ROOT = Path(__file__).resolve().parents[2]
EASYDEV_ROOT = HOOKS_ROOT.parent
CORE = HOOKS_ROOT / "hooks" / "core.py"

if str(HOOKS_ROOT) not in sys.path:
    sys.path.insert(0, str(HOOKS_ROOT))

from hooks import core as core_module  # noqa: E402

HOSTS = ("pi", "antigravity", "codex", "dsh")

HANDOFF_TEMPLATE = (
    "role: reviewer\n"
    "只读审查以下 diff，不改代码。\n"
    "角色路径：{role_path}\n"
    "每条发现以 **发现** 形式起头，格式：**[P级] 简短标题**\n"
)

DEVELOPER_HANDOFF_TEMPLATE = (
    "role: developer\n"
    "实现以下改动，完成后回报。\n"
    "角色路径：{role_path}\n"
)


GIT_RECORDING_WRAPPER = """#!/bin/sh
echo "$@" >> %s
exec %s "$@"
"""


def clean_env(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("PI_SUBAGENT_") and key not in {"PYTHONPATH"}
    }
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.update(extra or {})
    return env


@contextlib.contextmanager
def _temporary_environ(environment: Dict[str, str]):
    """进程内调用期间临时换上隔离环境（单线程测试夹具，用完恢复）。"""
    saved = dict(os.environ)
    os.environ.clear()
    os.environ.update(environment)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


class HookCase(unittest.TestCase):
    host = "pi"

    # 模板仓库每进程只建一次：每个用例 copytree 一份，不再逐例跑 5 次 git。
    _template: Optional[str] = None

    @classmethod
    def _template_repo(cls) -> str:
        if HookCase._template is None:
            base = tempfile.mkdtemp(prefix="easydev-hook-template-")
            atexit.register(shutil.rmtree, base, True)
            repo = Path(base) / "repo"
            repo.mkdir()
            env = clean_env(
                {
                    "HOME": str(Path(base) / "home"),
                    "XDG_CONFIG_HOME": str(Path(base) / "home" / ".config"),
                    "GIT_CONFIG_GLOBAL": os.devnull,
                    "GIT_CONFIG_NOSYSTEM": "1",
                }
            )
            for command in (
                ["git", "init", "-q", "."],
                ["git", "config", "user.email", "hook@example.com"],
                ["git", "config", "user.name", "hook-tests"],
            ):
                subprocess.run(command, cwd=str(repo), env=env, check=True, capture_output=True)
            (repo / "src").mkdir()
            (repo / "src" / "app.py").write_text("value = 1\n", encoding="utf-8")
            (repo / "docs").mkdir()
            (repo / "docs" / "guide.md").write_text("guide\n", encoding="utf-8")
            for command in (["git", "add", "-A"], ["git", "commit", "-qm", "base"]):
                subprocess.run(command, cwd=str(repo), env=env, check=True, capture_output=True)
            HookCase._template = str(repo)
        return HookCase._template

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        # 共享夹具默认把 HOME/XDG_CONFIG_HOME 与 Git 全局配置定向到临时目录：
        # 普通用例不得读取维护者真实用户配置，来源选择专项用例再显式切换。
        self.home = self.root / "home"
        (self.home / ".config" / "easydev").mkdir(parents=True)
        self.base_env = {
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
        }
        self.repo = self.root / "repo"
        shutil.copytree(self._template_repo(), str(self.repo), symlinks=True)
        self.skill = self.root / "skill" / "easydev"
        (self.skill / "references").mkdir(parents=True)
        (self.skill / "SKILL.md").write_text("---\nname: easydev\n---\n", encoding="utf-8")
        (self.skill / "references" / "reviewer-role.md").write_text("reviewer\n", encoding="utf-8")
        (self.skill / "references" / "developer-role.md").write_text("developer\n", encoding="utf-8")
        self.state = self.root / "state"
        self.context_path = self.root / "install-context.json"
        self.write_context()

    # ---- 基础工具 --------------------------------------------------
    def environment(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        """隔离 HOME/XDG/Git 全局配置；专项用例用 extra 显式切到指定临时源。"""
        merged = dict(self.base_env)
        merged.update(extra or {})
        return clean_env(merged)

    def git(self, *args: str) -> str:
        completed = subprocess.run(
            ["git", "-c", "commit.gpgsign=false", *args],
            cwd=str(self.repo),
            env=self.environment(),
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return completed.stdout.strip()

    def write_context(self, **overrides: Any) -> None:
        context = {
            "protocol": 1,
            "version": "1.4.3",
            "host": self.host,
            "mode": "enforce",
            "state_root": str(self.state),
            "skill_root": str(self.skill),
            "repo_root": str(self.repo),
            # 默认走显式手工任务根（旧严格核对路径），基线用例不再依赖隐式默认值；
            # 自动快照路径由专项用例显式传 task_root="" 打开。
            "task_root": str(self.manual_task_root()),
            "doc_paths": [],
        }
        context.update(overrides)
        self.context_path.write_text(json.dumps(context), encoding="utf-8")

    def manual_task_root(self) -> Path:
        """私有手工任务根（0700，不含 baseline/）：默认上下文用它，用例可自行改。"""
        root = self.root / "manual-task"
        if not root.exists():
            root.mkdir(mode=0o700)
            os.chmod(root, 0o700)
        return root

    def write_git_wrapper(self) -> Tuple[Path, Dict[str, str]]:
        """让 `git` 先记一行日志再转给真 git：用调用记录证明“没有启动 Git 子进程”。"""
        real_git = os.path.realpath("/usr/bin/git") if os.path.exists("/usr/bin/git") else "git"
        calls = self.root / "git-calls.txt"
        calls.write_text("", encoding="utf-8")
        wrapper_dir = self.root / "git-wrapper"
        wrapper_dir.mkdir()
        wrapper = wrapper_dir / "git"
        wrapper.write_text(GIT_RECORDING_WRAPPER % (calls, real_git), encoding="utf-8")
        os.chmod(wrapper, 0o755)
        env = {"PATH": str(wrapper_dir) + os.pathsep + os.environ.get("PATH", "")}
        return calls, env

    def run_core(
        self,
        payload: Any,
        *,
        host: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        mode: Optional[str] = None,
        event: Optional[str] = None,
        raw_stdin: Optional[str] = None,
    ) -> Tuple[int, Optional[Dict[str, Any]], str]:
        """在进程内跑一次 core.run()：stdin 用 BytesIO，stdout/stderr 捕获。

        `os.environ` 在本调用期间临时换成同样的隔离环境，因为 `paths` 的 Git 子进程
        与 models 来源选择都读 `os.environ`（只传参给 run() 不够）。
        """
        environment = self.environment(env)
        stdin = raw_stdin if raw_stdin is not None else json.dumps(payload, ensure_ascii=False)
        argv = ["--host", host or self.host, "--context", str(self.context_path)]
        if mode:
            argv.extend(["--mode", mode])
        if event:
            argv.extend(["--event", event])
        out = io.StringIO()
        err = io.StringIO()
        with _temporary_environ(environment):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = core_module.run(
                    argv, io.BytesIO(stdin.encode("utf-8")), dict(os.environ)
                )
        self.assertEqual(code, 0, err.getvalue())
        try:
            value = json.loads(out.getvalue())
        except json.JSONDecodeError:
            self.fail("stdout 不是单个 JSON: %r / stderr=%s" % (out.getvalue(), err.getvalue()))
        return code, value, err.getvalue()

    def run_core_cli(
        self,
        payload: Any,
        *,
        host: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        mode: Optional[str] = None,
        event: Optional[str] = None,
        raw_stdin: Optional[str] = None,
    ) -> Tuple[int, Optional[Dict[str, Any]], str]:
        """真正子进程跑 CLI：保留“独立进程 + stdin/stdout + 退出码”的入口覆盖。"""
        command = [
            sys.executable,
            str(CORE),
            "--host",
            host or self.host,
            "--context",
            str(self.context_path),
        ]
        if mode:
            command.extend(["--mode", mode])
        if event:
            command.extend(["--event", event])
        stdin = raw_stdin if raw_stdin is not None else json.dumps(payload, ensure_ascii=False)
        completed = subprocess.run(
            command,
            input=stdin,
            text=True,
            capture_output=True,
            env=self.environment(env),
            timeout=30,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        try:
            value = json.loads(completed.stdout)
        except json.JSONDecodeError:
            self.fail("stdout 不是单个 JSON: %r / stderr=%s" % (completed.stdout, completed.stderr))
        return completed.returncode, value, completed.stderr

    # ---- envelope --------------------------------------------------
    def input_payload(self, session: str, prompt: str, **extra: Any) -> Dict[str, Any]:
        if self.host == "antigravity":
            payload = {
                "hookEventName": "PreInvocation",
                "conversationId": session,
                "prompt": prompt,
                "workspacePath": str(self.repo),
            }
        elif self.host in {"codex", "dsh"}:
            payload = {
                "hook_event_name": "UserPromptSubmit",
                "session_id": session,
                "prompt": prompt,
                "cwd": str(self.repo),
                "turn_id": "turn-1",
            }
        else:
            payload = {
                "event": "input",
                "sessionId": session,
                "prompt": prompt,
                "cwd": str(self.repo),
            }
        payload.update(extra)
        return payload

    def tool_payload(
        self,
        session: str,
        name: str,
        args: Dict[str, Any],
        *,
        tool_id: str = "tool-1",
        event: str = "pre_tool",
        outcome: Optional[str] = None,
        **extra: Any,
    ) -> Dict[str, Any]:
        if self.host == "antigravity":
            payload = {
                "hookEventName": "PreToolUse" if event == "pre_tool" else "PostToolUse",
                "conversationId": session,
                "toolCall": {"name": name, "args": args, "id": tool_id},
            }
            if outcome:
                payload["status"] = outcome
        elif self.host in {"codex", "dsh"}:
            payload = {
                "hook_event_name": "PreToolUse" if event == "pre_tool" else "PostToolUse",
                "session_id": session,
                "tool_name": name,
                "tool_input": args,
                "tool_use_id": tool_id,
                "cwd": str(self.repo),
            }
            if outcome:
                payload["outcome"] = outcome
        else:
            payload = {
                "event": "tool_call" if event == "pre_tool" else "tool_result",
                "sessionId": session,
                "toolName": name,
                "toolInput": args,
                "toolCallId": tool_id,
                "cwd": str(self.repo),
                "outcome": outcome or "",
            }
        payload.update(extra)
        return payload

    def shell(self, session: str, command: str, tool_id: str = "shell-1", **extra: Any) -> Dict[str, Any]:
        if self.host == "antigravity":
            return self.tool_payload(
                session, "run_command", {"CommandLine": command, "Cwd": str(self.repo)}, tool_id=tool_id, **extra
            )
        if self.host in {"codex", "dsh"}:
            return self.tool_payload(session, "Bash", {"command": command}, tool_id=tool_id, **extra)
        return self.tool_payload(session, "bash", {"command": command}, tool_id=tool_id, **extra)

    def write_tool(
        self,
        session: str,
        path: str,
        tool_id: str = "write-1",
        event: str = "pre_tool",
        **extra: Any,
    ) -> Dict[str, Any]:
        if self.host == "antigravity":
            return self.tool_payload(
                session, "write_file", {"Path": path}, tool_id=tool_id, event=event, **extra
            )
        if self.host in {"codex", "dsh"}:
            return self.tool_payload(
                session, "Write", {"file_path": path}, tool_id=tool_id, event=event, **extra
            )
        return self.tool_payload(
            session, "write", {"path": path, "content": "x"}, tool_id=tool_id, event=event, **extra
        )

    def write_path(
        self,
        session: str,
        tool_id: str,
        relative: str = "src/app.py",
        content: Optional[str] = None,
    ) -> None:
        """完整的一次写入：pre_tool → 真实文件改动 → 成功 post_tool。"""
        self.allow(self.write_tool(session, relative, tool_id=tool_id))
        target = self.repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if content is None:
            content = "value = %s\n" % tool_id
        target.write_text(content, encoding="utf-8")
        self.allow(
            self.write_tool(
                session,
                relative,
                tool_id=tool_id,
                event="post_tool",
                outcome="success",
            )
        )

    def child_input_payload(self, session: str, prompt: str, parent: str = "P") -> Dict[str, Any]:
        """子代理初始/跟进 input：各宿主用各自的 child 字段表达。"""
        payload = self.input_payload(session, prompt)
        if self.host == "antigravity":
            payload.update({"parentConversationId": parent, "isChild": True, "agentId": session})
        elif self.host in {"codex", "dsh"}:
            payload.update({"is_child": True, "agent_id": session, "parent_session_id": parent})
        else:
            payload.update({"child": True, "parentId": parent})
        return payload

    def settle_payload(self, session: str, **extra: Any) -> Dict[str, Any]:
        if self.host == "antigravity":
            payload = {
                "hookEventName": "Stop",
                "conversationId": session,
                "terminationReason": "NO_TOOL_CALL",
                "fullyIdle": True,
            }
        elif self.host in {"codex", "dsh"}:
            payload = {
                "hook_event_name": "Stop",
                "session_id": session,
                "stop_hook_active": False,
            }
        else:
            payload = {"event": "settle", "sessionId": session}
        payload.update(extra)
        return payload

    def dispatch_payload(
        self,
        session: str,
        prompt: str,
        role: str = "reviewer",
        tool_id: str = "dispatch-1",
        **extra: Any,
    ) -> Dict[str, Any]:
        if self.host == "antigravity":
            args = {"agent_type": role, "prompt": prompt}
            return self.tool_payload(session, "invoke_subagent", args, tool_id=tool_id, **extra)
        if self.host in {"codex", "dsh"}:
            args = {"agent_type": role, "prompt": prompt}
            return self.tool_payload(session, "spawn_agent", args, tool_id=tool_id, **extra)
        args = {"action": "create", "agent": role, "task": prompt}
        return self.tool_payload(session, "subagent", args, tool_id=tool_id, **extra)

    def child_start_payload(
        self, agent: str, session: str = "P", dispatch_id: str = ""
    ) -> Dict[str, Any]:
        """SubagentStart：可信 child ID 来源；按宿主字段表达。"""
        if self.host == "antigravity":
            return {
                "hookEventName": "SubagentStart",
                "conversationId": session,
                "agentId": agent,
                "agent_type": "reviewer",
                "parentConversationId": session,
                "isChild": True,
            }
        if self.host in {"codex", "dsh"}:
            # 官方契约：子 Hook 的 session_id 就是父 session，agent_id 才是 child。
            # 夹具不提供 parent_session_id，避免用宿主不存在的字段制造“已闭环”。
            payload = {
                "hook_event_name": "SubagentStart",
                "session_id": session,
                "agent_id": agent,
                "agent_type": "reviewer",
                "cwd": str(self.repo),
            }
            if dispatch_id:
                payload["dispatch_id"] = dispatch_id
            return payload
        return {
            "event": "child_start",
            "sessionId": session,
            "actorId": agent,
            "parentId": session,
            "child": True,
            "cwd": str(self.repo),
        }

    def child_done_payload(
        self,
        session: str,
        dispatch_id: str = "",
        report: str = "**[P1] 发现** 证据\n",
        status: str = "completed",
        agent: str = "",
    ) -> Dict[str, Any]:
        """SubagentStop：显式分派 ID 或可信 child agent ID + 结构化报告。"""
        agent_id = agent or dispatch_id
        if self.host == "antigravity":
            payload = {
                "hookEventName": "SubagentStop",
                "conversationId": session,
                "status": status,
                "report": report,
            }
            if dispatch_id:
                payload["dispatchId"] = dispatch_id
            if agent:
                payload["agentId"] = agent
            return payload
        if self.host in {"codex", "dsh"}:
            payload = {
                "hook_event_name": "SubagentStop",
                "session_id": session,
                "agent_type": "reviewer",
                "status": status,
                "last_assistant_message": report,
                "cwd": str(self.repo),
            }
            if agent:
                payload["agent_id"] = agent
            if dispatch_id:
                payload["dispatch_id"] = dispatch_id
            return payload
        payload = {
            "event": "child_done",
            "sessionId": session,
            "status": status,
            "report": report,
        }
        if dispatch_id:
            payload["dispatchId"] = dispatch_id
        if agent:
            payload["actorId"] = agent
        return payload

    def child_input_payload_for(self, agent: str, prompt: str, parent: str = "P") -> Dict[str, Any]:
        """Codex 子 Hook 的 session_id 是父 session；直接产物用 agent_id 区分 actor。"""
        if self.host in {"codex", "dsh"}:
            return {
                "hook_event_name": "UserPromptSubmit",
                "session_id": parent,
                "agent_id": agent,
                "is_child": True,
                "parent_session_id": parent,
                "prompt": prompt,
                "turn_id": "turn-child",
                "cwd": str(self.repo),
            }
        payload = self.child_input_payload(parent, prompt, parent=parent)
        return payload

    def reviewer_handoff(self, extra: str = "") -> str:
        return HANDOFF_TEMPLATE.format(role_path=str(self.skill / "references" / "reviewer-role.md")) + extra

    def developer_handoff(self) -> str:
        return DEVELOPER_HANDOFF_TEMPLATE.format(
            role_path=str(self.skill / "references" / "developer-role.md")
        )

    # ---- 断言 ------------------------------------------------------
    def activate(self, session: str = "s1", prompt: str = "使用 easydev，完成这个改动", env=None):
        _code, value, stderr = self.run_core(self.input_payload(session, prompt), env=env)
        return value, stderr

    def is_denied(self, value: Dict[str, Any]) -> bool:
        if self.host == "antigravity":
            return value.get("decision") == "deny"
        if self.host == "pi":
            return value.get("block") is True
        return (
            value.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"
        )

    def is_continued(self, value: Dict[str, Any]) -> bool:
        if self.host == "antigravity":
            return value.get("decision") == "continue"
        if self.host == "pi":
            return value.get("continue") is True
        return value.get("decision") == "block"

    def reason(self, value: Dict[str, Any]) -> str:
        if self.host in {"antigravity", "pi"}:
            return str(value.get("reason") or "")
        return str(value.get("hookSpecificOutput", {}).get("permissionDecisionReason") or "")

    def deny(self, payload: Dict[str, Any], **kwargs: Any) -> str:
        _code, value, _stderr = self.run_core(payload, **kwargs)
        self.assertTrue(self.is_denied(value), value)
        return self.reason(value)

    def allow(self, payload: Dict[str, Any], **kwargs: Any) -> Dict[str, Any]:
        _code, value, _stderr = self.run_core(payload, **kwargs)
        self.assertFalse(self.is_denied(value), value)
        return value

    # ---- 基线 ------------------------------------------------------
    def make_task_root(self, name: str = "task-a", mode: int = 0o700) -> Path:
        task_root = self.root / name
        task_root.mkdir(mode=0o700)
        os.chmod(task_root, mode)
        return task_root

    def save_baseline(
        self,
        task_root: Path,
        relative: str,
        content: Optional[str] = None,
        *,
        file_mode: int = 0o600,
        dir_mode: int = 0o700,
    ) -> Path:
        baseline = task_root / "baseline"
        baseline.mkdir(mode=0o700, exist_ok=True)
        os.chmod(baseline, dir_mode)
        parent = baseline
        for part in relative.split("/")[:-1]:
            parent = parent / part
            parent.mkdir(exist_ok=True)
            os.chmod(parent, dir_mode)
        target = baseline / relative
        if content is None:
            content = (self.repo / relative).read_text(encoding="utf-8")
        target.write_text(content, encoding="utf-8")
        os.chmod(target, file_mode)
        return target

    def touch_dirty(self, relative: str = "src/app.py", content: str = "value = 2\n") -> None:
        (self.repo / relative).write_text(content, encoding="utf-8")


def apply_context(base: Dict[str, Any], **overrides: Any) -> Dict[str, Any]:
    result = dict(base)
    result.update(overrides)
    return result
