"""C：未激活会话在入口 O(1) 短路（不调 Git、不预取写入、不建锁、不写状态）。"""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from support import HookCase

from hooks import state


class InactiveShortCircuitMixin(object):

    def warm_inactive(self, session: str = "inactive") -> None:
        """第一次事件付一次完整判定并写下该分区的未激活锚点，之后才是常数开销。"""
        self.allow(self.write_tool(session, "src/app.py", tool_id="warm"))

    def test_steady_state_write_short_circuits_without_git_or_state(self) -> None:
        calls, env = self.write_git_wrapper()
        self.warm_inactive()
        calls.write_text("", encoding="utf-8")
        before = sorted(path.name for path in (self.state / "locks").glob("*.lock"))
        samples = []
        for index in range(5):
            started = time.monotonic()
            value = self.allow(
                self.write_tool("inactive", "src/app.py", tool_id="fast-%d" % index),
                env=env,
            )
            samples.append((time.monotonic() - started) * 1000.0)
            assert value.get("reason", "") == ""
        average = sum(samples) / len(samples)
        # 无 Git 子进程、无写入预取、无新锁文件。
        assert calls.read_text(encoding="utf-8") == ""
        after = sorted(path.name for path in (self.state / "locks").glob("*.lock"))
        assert after == before
        assert average < 5.0, samples

    def test_post_tool_is_short_circuited_without_warnings(self) -> None:
        self.warm_inactive()
        _code, value, stderr = self.run_core(
            self.write_tool("inactive", "src/app.py", tool_id="post-1", event="post_tool")
        )
        assert not self.is_denied(value), value
        # 未激活 post 不再刷“缺少可信成功结果/未找到配对 pre”告警。
        assert stderr == ""

    def test_settle_and_child_done_are_allowed(self) -> None:
        self.warm_inactive()
        _code, value, _stderr = self.run_core(self.settle_payload("inactive"))
        assert not self.is_continued(value), value

    def test_short_circuit_allow_shape_is_host_specific(self) -> None:
        self.warm_inactive()
        _code, value, _stderr = self.run_core(
            self.write_tool("inactive", "src/app.py", tool_id="shape-1")
        )
        if self.host == "antigravity":
            assert value == {"decision": "allow"}
        elif self.host == "dsh":
            assert value == {"permissionDecision": "allow"}
        else:
            assert value == {}

    def test_explicit_invocation_still_activates_after_short_circuit(self) -> None:
        self.warm_inactive()
        self.activate("inactive", "使用 easydev，继续这个任务")
        self.deny(self.shell("inactive", "git reset --hard", tool_id="after-activate"))

    def test_skill_read_post_still_activates(self) -> None:
        self.warm_inactive("reader")
        path = str(self.skill / "SKILL.md")
        if self.host == "antigravity":
            post = self.tool_payload("reader", "read_file", {"Path": path}, event="post_tool", outcome="success")
        else:
            post = self.tool_payload("reader", "read", {"path": path}, event="post_tool", outcome="success")
        self.allow(post)
        self.deny(self.shell("reader", "git reset --hard", tool_id="after-skill"))

    def test_deactivation_stops_enforcement_again(self) -> None:
        self.activate("toggle")
        self.deny(self.shell("toggle", "git reset --hard", tool_id="on"))
        block = '```easydev-hook\n{"version": 1, "active": false}\n```'
        self.activate("toggle", block)
        self.allow(self.shell("toggle", "git reset --hard", tool_id="off"))
        # 停用后的状态已写回该分区：后续写事件同样短路放行。
        calls, env = self.write_git_wrapper()
        self.allow(self.write_tool("toggle", "src/app.py", tool_id="off-write"), env=env)
        assert calls.read_text(encoding="utf-8") == ""


class PiInactiveShortCircuitTests(InactiveShortCircuitMixin, HookCase):
    host = "pi"


class AntigravityInactiveShortCircuitTests(InactiveShortCircuitMixin, HookCase):
    host = "antigravity"

    def test_cli_event_short_circuit_allow(self) -> None:
        payload = {
            "conversationId": "cli-inactive",
            "toolCall": {"name": "write_file", "args": {"Path": str(self.repo / "src" / "app.py")}},
            "workspacePaths": [str(self.repo)],
        }
        self.allow(self.tool_payload("cli-inactive", "write_file", {"Path": str(self.repo / "src" / "app.py")}, tool_id="w"), event="PreToolUse")
        _code, value, _stderr = self.run_core(payload, event="PreToolUse")
        assert value == {"decision": "allow"}


class CodexInactiveShortCircuitTests(InactiveShortCircuitMixin, HookCase):
    host = "codex"


class DshInactiveShortCircuitTests(InactiveShortCircuitMixin, HookCase):
    host = "dsh"


class CrossRepoPartitionMixin(object):
    """默认配置（上下文无显式 `state_root`）：短路必须按 tool cwd 的状态分区判定。

    权威 actor 状态按仓库分目录落盘；一次指向另一仓库的普通调用既不能把当前仓库
    的激活状态覆盖成 inactive，也不能留下可由下次事件复用的陈旧全局索引。
    """

    def setup_method(self) -> None:
        super().setup_method()
        self.other = self.root / "other-repo"
        shutil.copytree(self._template_repo(), str(self.other), symlinks=True)
        self.state_home = self.root / "tmpdir"
        self.state_home.mkdir()
        # 默认配置：上下文不写 state_root，状态根按仓库分区落在系统临时根下。
        self.write_context(state_root="")

    def partition(self, repo: Optional[str] = None) -> Path:
        base = Path(os.path.realpath(str(self.state_home)))
        return base / ("easydev-hooks-%d" % os.getuid()) / "v1" / state.repo_id_of(str(repo or self.repo))

    def cli_env(self, extra: Optional[dict] = None) -> dict:
        env = {"TMPDIR": str(self.state_home)}
        env.update(extra or {})
        return env

    def cli(self, payload, **kwargs):
        return self.run_core_cli(payload, env=self.cli_env(kwargs.pop("env", None)), **kwargs)

    def cli_deny(self, payload, **kwargs) -> str:
        _code, value, stderr = self.cli(payload, **kwargs)
        assert self.is_denied(value), (value, stderr)
        return self.reason(value)

    def cli_allow(self, payload, **kwargs) -> str:
        _code, value, stderr = self.cli(payload, **kwargs)
        assert not self.is_denied(value), (value, stderr)
        return stderr

    def other_shell(self, session: str, command: str, tool_id: str = "other-1") -> Dict[str, Any]:
        """一次 cwd 指向另一 Git 仓库的普通工具调用（会话 cwd 仍在 A）。"""
        if self.host == "antigravity":
            return self.tool_payload(
                session, "run_command", {"CommandLine": command, "Cwd": str(self.other)}, tool_id=tool_id
            )
        if self.host in {"codex", "dsh"}:
            return self.tool_payload(
                session, "Bash", {"command": command, "cwd": str(self.other)}, tool_id=tool_id
            )
        return self.tool_payload(
            session, "bash", {"command": command, "cwd": str(self.other)}, tool_id=tool_id
        )

    def strip_cwd(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """去掉该宿主 envelope 里全部 cwd 来源，模拟无可信 cwd 的事件。"""
        for key in ("cwd", "Cwd", "workspacePath", "workspacePaths", "working_directory"):
            payload.pop(key, None)
        tool_call = payload.get("toolCall")
        if isinstance(tool_call, dict) and isinstance(tool_call.get("args"), dict):
            for key in ("Cwd", "cwd", "WorkingDirectory"):
                tool_call["args"].pop(key, None)
        for key in ("toolInput", "tool_input"):
            if isinstance(payload.get(key), dict):
                for name in ("Cwd", "cwd", "WorkingDirectory"):
                    payload[key].pop(name, None)
        return payload

    def test_other_repo_call_does_not_disable_active_enforcement(self) -> None:
        self.cli(self.input_payload("dev", "使用 easydev，完成这个改动"))
        self.cli_deny(self.shell("dev", "git reset --hard", tool_id="a1"))
        # A 中已激活；同一会话在 B 的普通调用在 B 分区内只是一次未激活判定。
        self.cli_allow(self.other_shell("dev", "git status --short", tool_id="b1"))
        self.cli_deny(self.shell("dev", "git reset --hard", tool_id="a2"))

    def test_other_repo_call_keeps_reviewer_readonly(self) -> None:
        self.cli(self.input_payload("rev", self.reviewer_handoff()))
        self.cli_deny(self.write_tool("rev", "src/app.py", tool_id="r1"))
        self.cli_allow(self.other_shell("rev", "git status --short", tool_id="b1"))
        self.cli_deny(self.write_tool("rev", "src/app.py", tool_id="r2"))

    def project_extra(self) -> Dict[str, Any]:
        """Antigravity 的 tool 事件没有工具级 cwd，工作区在 envelope 的 `workspacePath`。"""
        return {"workspacePath": str(self.repo)} if self.host == "antigravity" else {}

    def subdir_write(self, session: str, tool_id: str = "sub-1") -> Dict[str, Any]:
        """tool cwd 在仓库子目录里的一次写入事件。"""
        subdir = self.repo / "src"
        target = str(subdir / "app.py")
        if self.host == "antigravity":
            payload = self.tool_payload(session, "write_file", {"Path": target}, tool_id=tool_id)
            payload["workspacePath"] = str(subdir)
        elif self.host in {"codex", "dsh"}:
            payload = self.tool_payload(session, "Write", {"file_path": target}, tool_id=tool_id)
            payload["cwd"] = str(subdir)
        else:
            payload = self.tool_payload(session, "write", {"path": target, "content": "x"}, tool_id=tool_id)
            payload["cwd"] = str(subdir)
        return payload

    def test_deactivated_session_is_zero_git_in_default_config(self) -> None:
        self.cli(self.input_payload("toggle", "使用 easydev，完成这个改动"))
        self.cli_deny(self.shell("toggle", "git reset --hard", tool_id="on"))
        block = '```easydev-hook\n{"version": 1, "active": false}\n```'
        self.cli(self.input_payload("toggle", block))
        calls, env = self.write_git_wrapper()
        before = self.state_files()
        for index in range(3):
            stderr = self.cli_allow(
                self.write_tool("toggle", "src/app.py", tool_id="off-%d" % index, **self.project_extra()),
                env=env,
            )
            assert stderr == ""
        assert calls.read_text(encoding="utf-8") == ""
        # 稳态短路也不写状态、不建新锁。
        assert self.state_files() == before

    def state_files(self) -> list:
        return sorted(str(path) for path in Path(os.path.realpath(str(self.state_home))).rglob("*"))

    def test_no_global_session_index_is_written(self) -> None:
        self.cli(self.input_payload("dev", "使用 easydev，完成这个改动"))
        self.cli_allow(self.other_shell("dev", "git status --short", tool_id="b1"))
        self.cli_allow(self.write_tool("plain", "src/app.py", tool_id="w1"))
        base = Path(os.path.realpath(str(self.state_home)))
        assert [str(path) for path in base.rglob("sessions")] == []

    def test_subdirectory_cwd_resolves_the_same_partition(self) -> None:
        self.cli(self.input_payload("dev", "使用 easydev，完成这个改动"))
        block = '```easydev-hook\n{"version": 1, "active": false}\n```'
        self.cli(self.input_payload("dev", block))
        calls, env = self.write_git_wrapper()
        before = self.state_files()
        stderr = self.cli_allow(self.subdir_write("dev", tool_id="sub-1"), env=env)
        assert stderr == ""
        # 从子目录向上找到的仓库根与 Git 结论一致：仍然命中同一分区，不调 Git。
        assert calls.read_text(encoding="utf-8") == ""
        assert self.state_files() == before

    def test_missing_cwd_and_context_root_skips_write_prefetch(self) -> None:
        """无 cwd 且无上下文仓库根：未激活会话不得进入写预取（不计 Git、不告警）。"""
        self.write_context(state_root="", repo_root="")
        self.cli_allow(self.write_tool("plain", "src/app.py", tool_id="warm-unbound"))
        calls, env = self.write_git_wrapper()
        stderr = self.cli_allow(
            self.strip_cwd(self.write_tool("plain", "src/app.py", tool_id="w1")), env=env
        )
        assert stderr == ""
        assert calls.read_text(encoding="utf-8") == ""

    def test_missing_cwd_inactive_events_do_not_warn(self) -> None:
        """无可信 cwd 时退回完整判定，但未激活会话仍不做写预取、不刷 post 告警。"""
        self.cli_allow(self.write_tool("plain", "src/app.py", tool_id="warm-nocwd"))
        stderr = self.cli_allow(
            self.strip_cwd(self.write_tool("plain", "src/app.py", tool_id="w1"))
        )
        assert stderr == ""
        post = self.strip_cwd(
            self.write_tool("plain", "src/app.py", tool_id="w1", event="post_tool", outcome="success")
        )
        assert self.cli_allow(post) == ""

    def test_missing_cwd_is_never_short_circuited(self) -> None:
        self.cli(self.input_payload("dev", "使用 easydev，完成这个改动"))
        payload = self.strip_cwd(self.write_tool("fresh", "src/app.py", tool_id="w1"))
        before = set(self.state_files())
        self.cli_allow(payload)
        # 无法定位分区时不短路：完整判定给这个新会话写下未激活锚点（入口短路不写状态）。
        added = [path for path in self.state_files() if path not in before]
        anchors = [path for path in added if os.sep + "actors" + os.sep in path]
        assert len(anchors) == 1, added

    def test_missing_cwd_still_honours_context_repo_partition(self) -> None:
        self.cli(self.input_payload("dev", "使用 easydev，完成这个改动"))
        payload = self.strip_cwd(self.shell("dev", "git reset --hard", tool_id="a1"))
        self.cli_deny(payload)

    def test_unreadable_partition_state_fails_open_with_warning(self) -> None:
        self.cli_allow(self.write_tool("plain", "src/app.py", tool_id="warm"))
        actors = self.partition() / "actors"
        assert actors.is_dir(), actors
        for path in actors.glob("*.json"):
            target = path.with_suffix(".bak")
            path.rename(target)
            path.symlink_to(target.name)
        stderr = self.cli_allow(self.write_tool("plain", "src/app.py", tool_id="w2"))
        # 状态不可读：按既有 fail-open 告警放行，不得当成“已验证未激活”静默短路。
        assert "私有状态不可用" in stderr


class PiCrossRepoPartitionTests(CrossRepoPartitionMixin, HookCase):
    host = "pi"


class AntigravityCrossRepoPartitionTests(CrossRepoPartitionMixin, HookCase):
    host = "antigravity"


class CodexCrossRepoPartitionTests(CrossRepoPartitionMixin, HookCase):
    host = "codex"


class DshCrossRepoPartitionTests(CrossRepoPartitionMixin, HookCase):
    host = "dsh"


# ---------------------------------------------------------------------------
# 带 transcript 的真实未激活会话：锚点必须落盘，后续事件才是常数开销
# ---------------------------------------------------------------------------
class TranscriptInactiveAnchorMixin(object):
    """首个用户回合带 transcript：未激活也必须写下 `activation_scope=inactive`。

    真实宿主在首个用户回合事件（Codex `UserPromptSubmit`、Pi `input`）都会传
    transcript 路径；锚点没落盘时，后续每次工具调用都会重付一次 Git 与写预取的
    代价，并刷出 post 告警。
    """

    def actor_state(self, session: str) -> dict:
        key = state.identity_key("actor", self.host, session, session)
        path = self.state / "actors" / (key + ".json")
        assert path.exists(), path
        return json.loads(path.read_text(encoding="utf-8"))

    def meta_entries(self, session: str) -> list:
        if self.host in {"codex", "dsh"}:
            return [{"type": "session_meta", "payload": {"id": session, "cwd": str(self.repo)}}]
        return []

    def user_entries(self, text: str) -> list:
        if self.host in {"codex", "dsh"}:
            return [
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": text}],
                    },
                }
            ]
        return [{"role": "user", "message": text}]

    def write_transcript(self, name: str, session: str, *texts: str) -> str:
        entries = self.meta_entries(session)
        for text in texts:
            entries.extend(self.user_entries(text))
        path = self.root / name
        path.write_text(
            "\n".join(json.dumps(entry, ensure_ascii=False) for entry in entries) + "\n",
            encoding="utf-8",
        )
        return str(path)

    def input_with_transcript(self, session: str, path: str) -> Dict[str, Any]:
        payload = self.input_payload(session, "")
        if self.host in {"codex", "dsh"}:
            payload["transcript_path"] = path
        else:
            payload["transcriptPath"] = path
        return payload

    def test_unactivated_turn_with_transcript_writes_inactive_anchor(self) -> None:
        path = self.write_transcript("plain.jsonl", "t1", "把文档里的错别字改一下")
        self.allow(self.input_with_transcript("t1", path))
        assert self.actor_state("t1").get("activation_scope") == "inactive"

    def test_following_tool_events_need_no_git_lock_or_warning(self) -> None:
        path = self.write_transcript("plain2.jsonl", "t2", "把文档里的错别字改一下")
        self.allow(self.input_with_transcript("t2", path))
        calls, env = self.write_git_wrapper()
        locks = self.state / "locks"
        before = sorted(item.name for item in locks.glob("*.lock"))
        _code, value, stderr = self.run_core(
            self.write_tool("t2", "src/app.py", tool_id="w1"), env=env
        )
        assert not self.is_denied(value), value
        assert stderr == ""
        _code, _value, stderr = self.run_core(
            self.write_tool("t2", "src/app.py", tool_id="w1", event="post_tool", outcome="success"),
            env=env,
        )
        assert stderr == ""
        # 未激活的 pre/post 都不启动 Git、不建锁、不写状态。
        assert calls.read_text(encoding="utf-8") == ""
        assert sorted(item.name for item in locks.glob("*.lock")) == before

    def test_deactivated_transcript_turn_stays_inactive(self) -> None:
        block = '```easydev-hook\n{"version": 1, "active": false}\n```'
        on = self.write_transcript("on.jsonl", "t3", "使用 easydev，实现这个改动")
        self.allow(self.input_with_transcript("t3", on))
        self.deny(self.shell("t3", "git reset --hard", tool_id="on"))
        off = self.write_transcript("off.jsonl", "t3", block)
        self.allow(self.input_with_transcript("t3", off))
        assert self.actor_state("t3").get("activation_scope") == "inactive"
        self.allow(self.shell("t3", "git reset --hard", tool_id="off"))


class PiTranscriptInactiveAnchorTests(TranscriptInactiveAnchorMixin, HookCase):
    host = "pi"


class CodexTranscriptInactiveAnchorTests(TranscriptInactiveAnchorMixin, HookCase):
    host = "codex"


class ChildSkillReadHostMixin(object):
    """child 读取用例的宿主夹具：按各宿主契约表达 child 身份与工具载荷。"""

    def child_env(self) -> Dict[str, str]:
        """Pi 只用宿主环境变量证明 child，不在 payload 里伪造 child 字段。"""
        if self.host == "pi":
            return {"PI_SUBAGENT_CHILD": "1", "PI_SUBAGENT_PARENT_SESSION": "P"}
        return {}

    def child_fields(self) -> Dict[str, Any]:
        if self.host == "antigravity":
            return {"isChild": True, "parentConversationId": "P"}
        if self.host == "codex":
            # Codex 子 Hook 的 session_id 是父 session，agent_id 才是 child。
            return {"agent_id": "child-a"}
        return {}

    def child_session(self) -> str:
        return "P" if self.host == "codex" else "child-a"

    def child_actor_state(self) -> dict:
        key = state.identity_key("actor", self.host, self.child_session(), "child-a")
        path = self.state / "actors" / (key + ".json")
        assert path.exists(), path
        return json.loads(path.read_text(encoding="utf-8"))

    def run_child(
        self, payload: Dict[str, Any], **kwargs: Any
    ) -> Tuple[int, Optional[Dict[str, Any]], str]:
        merged = dict(payload)
        merged.update(self.child_fields())
        env = dict(kwargs.pop("env", None) or {})
        env.update(self.child_env())
        return self.run_core(merged, env=env, **kwargs)

    def allow_child(self, payload: Dict[str, Any], **kwargs: Any) -> Tuple[Dict[str, Any], str]:
        _code, value, stderr = self.run_child(payload, **kwargs)
        assert not self.is_denied(value), value
        return value, stderr

    def deny_child(self, payload: Dict[str, Any], **kwargs: Any) -> Tuple[Dict[str, Any], str]:
        _code, value, stderr = self.run_child(payload, **kwargs)
        assert self.is_denied(value), value
        return value, stderr

    def child_read(
        self,
        path: str,
        *,
        tool_id: str = "read-1",
        event: str = "pre_tool",
        outcome: Optional[str] = None,
    ) -> Dict[str, Any]:
        if self.host == "antigravity":
            args = {"Path": path}
            return self.tool_payload(
                self.child_session(), "read_file", args, tool_id=tool_id, event=event, outcome=outcome
            )
        return self.tool_payload(
            self.child_session(), "read", {"path": path}, tool_id=tool_id, event=event, outcome=outcome
        )

    def child_dispatch(
        self, prompt: str = "", *, tool_id: str = "nested-1", role: str = "developer"
    ) -> Dict[str, Any]:
        return self.dispatch_payload(
            self.child_session(), prompt, role=role, tool_id=tool_id
        )

    def child_status(self, tool_id: str = "status-1") -> Dict[str, Any]:
        """查询/等待子任务：不是新分派，激活后也必须放行。"""
        if self.host == "antigravity":
            return self.tool_payload(self.child_session(), "invoke_subagent", {"action": "status"}, tool_id=tool_id)
        if self.host == "codex":
            return self.tool_payload(self.child_session(), "spawn_agent", {"action": "status"}, tool_id=tool_id)
        return self.tool_payload(self.child_session(), "subagent", {"action": "status"}, tool_id=tool_id)

    def warm_child(self) -> None:
        """第一次事件付一次完整判定并写下 inactive 锚点，与真实会话一致。"""
        _value, stderr = self.allow_child(self.child_read(str(self.repo / "docs" / "guide.md"), tool_id="warm"))
        assert stderr == ""

class ChildSkillReadActivationMixin(ChildSkillReadHostMixin):
    """未激活 child 成功读取精确 `SKILL.md` 后必须启用本 actor 的禁嵌套约束。

    子任务没有标准角色交接时也会主动读 Skill：成功读取是公开的激活条件。这里
    只要求本 actor 的约束（禁嵌套等）生效，不得因此建立父任务绑定或新 epoch。
    """

    def test_successful_child_skill_read_denies_nested_dispatch(self) -> None:
        skill_md = str(self.skill / "SKILL.md")
        self.warm_child()
        assert self.child_actor_state().get("activation_scope") == "inactive"
        self.allow_child(self.child_read(skill_md, tool_id="read-pre"))
        _value, stderr = self.allow_child(
            self.child_read(skill_md, tool_id="read-pre", event="post_tool", outcome="success")
        )
        assert stderr == ""
        assert self.child_actor_state().get("activation_scope") == "task_active"
        value, _stderr = self.deny_child(self.child_dispatch(self.developer_handoff()))
        assert "不能再派生" in self.reason(value)
        # 查询状态/等待结果不是新分派：激活后仍必须放行。
        self.allow_child(self.child_status())

    def test_activation_creates_no_parent_task_or_epoch(self) -> None:
        skill_md = str(self.skill / "SKILL.md")
        self.warm_child()
        self.allow_child(self.child_read(skill_md, tool_id="read-pre"))
        self.allow_child(
            self.child_read(skill_md, tool_id="read-pre", event="post_tool", outcome="success")
        )
        actor = self.child_actor_state()
        # 局部激活不凭空建立父任务绑定，也不开新 epoch。
        assert "dispatch_id" not in actor
        assert "task_identity" not in actor
        assert actor.get("role_source") == "skill_read"
        assert actor.get("root_actor_id") == "child-a"
        # 父 root 状态（host + 父 session + 父 actor）没有被这次局部激活创建。
        parent_root = self.state / "roots" / (
            state.identity_key("root", self.host, "P", "P") + ".json"
        )
        assert not parent_root.exists(), parent_root

    def test_failed_child_skill_read_stays_inactive(self) -> None:
        skill_md = str(self.skill / "SKILL.md")
        self.warm_child()
        self.allow_child(self.child_read(skill_md, tool_id="read-fail"))
        _value, stderr = self.allow_child(
            self.child_read(skill_md, tool_id="read-fail", event="post_tool", outcome="error")
        )
        assert stderr == ""
        assert self.child_actor_state().get("activation_scope") == "inactive"
        _value, stderr = self.allow_child(self.child_dispatch(self.developer_handoff()))
        assert stderr == ""

    def test_other_skill_path_does_not_activate(self) -> None:
        other = self.root / "other-skill" / "SKILL.md"
        other.parent.mkdir(parents=True)
        other.write_text("---\nname: other\n---\n", encoding="utf-8")
        self.warm_child()
        self.allow_child(self.child_read(str(other), tool_id="read-other"))
        _value, stderr = self.allow_child(
            self.child_read(str(other), tool_id="read-other", event="post_tool", outcome="success")
        )
        assert stderr == ""
        assert self.child_actor_state().get("activation_scope") == "inactive"
        self.allow_child(self.child_dispatch(self.developer_handoff()))

    def test_ordinary_child_post_is_still_short_circuited(self) -> None:
        self.warm_child()
        _value, stderr = self.allow_child(
            self.write_tool(
                self.child_session(), "src/app.py", tool_id="post-1", event="post_tool", outcome="success"
            )
        )
        assert stderr == ""
        assert self.child_actor_state().get("activation_scope") == "inactive"

    def test_root_inactive_session_keeps_zero_git_short_circuit(self) -> None:
        """同一状态分区里 child 已激活，普通根会话仍走零 Git 稳态短路。"""
        skill_md = str(self.skill / "SKILL.md")
        self.warm_child()
        self.allow_child(self.child_read(skill_md, tool_id="read-pre"))
        self.allow_child(
            self.child_read(skill_md, tool_id="read-pre", event="post_tool", outcome="success")
        )
        self.allow(self.write_tool("root-plain", "src/app.py", tool_id="warm"))
        calls, env = self.write_git_wrapper()
        locks = self.state / "locks"
        before = sorted(item.name for item in locks.glob("*.lock"))
        _code, value, stderr = self.run_core(
            self.write_tool("root-plain", "src/app.py", tool_id="root-w"), env=env
        )
        assert not self.is_denied(value), value
        assert stderr == ""
        assert calls.read_text(encoding="utf-8") == ""
        assert sorted(item.name for item in locks.glob("*.lock")) == before


class ChildSkillReadUnknownOutcomeMixin(ChildSkillReadActivationMixin):
    """结果未知（requested 但未确认）的 child 读取不是激活证据。

    Antigravity 官方 `PostToolUse` 无 `status` 即成功、失败必带非空 `error`，
    没有“结果未知”形状；它的失败路径由上面的失败读取用例覆盖。
    """

    def test_unconfirmed_child_skill_read_stays_inactive(self) -> None:
        skill_md = str(self.skill / "SKILL.md")
        self.warm_child()
        self.allow_child(self.child_read(skill_md, tool_id="read-unknown"))
        _value, stderr = self.allow_child(
            self.child_read(skill_md, tool_id="read-unknown", event="post_tool")
        )
        assert stderr == ""
        assert self.child_actor_state().get("activation_scope") == "inactive"
        self.allow_child(self.child_dispatch(self.developer_handoff()))


class PiChildSkillReadActivationTests(ChildSkillReadUnknownOutcomeMixin, HookCase):
    host = "pi"


class AntigravityChildSkillReadActivationTests(ChildSkillReadActivationMixin, HookCase):
    host = "antigravity"


class CodexChildSkillReadActivationTests(ChildSkillReadUnknownOutcomeMixin, HookCase):
    host = "codex"


class ChildSkillReadDeactivationMixin(ChildSkillReadHostMixin):
    """显式停用后重投旧成功读取不得撤销停用边界。

    局部激活与根路径共享“同 (actor, 工具调用 ID) 只作为一次激活证据”：首次接受
    即写下 actor 级消费证据，因此停用后原样重投、改参数或改结果拼写都保持
    inactive；只有新的调用 ID 的成功读取才能重新启用本 actor 的禁嵌套约束，且
    始终不建父任务、不重绑 epoch。
    """

    DEACTIVATE = '```easydev-hook\n{"version": 1, "active": false}\n```'

    def child_skill_read(
        self,
        tool_id: str,
        *,
        event: str = "pre_tool",
        outcome: Optional[str] = None,
        **extra: Any,
    ) -> Dict[str, Any]:
        path = str(self.skill / "SKILL.md")
        if self.host == "antigravity":
            args: Dict[str, Any] = {"Path": path}
            name = "read_file"
        else:
            args = {"path": path}
            name = "read"
        args.update(extra)
        return self.tool_payload(
            self.child_session(), name, args, tool_id=tool_id, event=event, outcome=outcome
        )

    def activate_child_local(self, tool_id: str = "read-old") -> None:
        self.allow_child(self.child_skill_read(tool_id))
        _value, stderr = self.allow_child(
            self.child_skill_read(tool_id, event="post_tool", outcome="success")
        )
        assert stderr == ""
        assert self.child_actor_state().get("activation_scope") == "task_active"

    def deactivate_child(self) -> None:
        _value, stderr = self.allow_child(
            self.input_payload(self.child_session(), self.DEACTIVATE)
        )
        assert stderr == ""
        assert self.child_actor_state().get("activation_scope") == "inactive"

    def nested_allowed(self, tool_id: str) -> None:
        _value, stderr = self.allow_child(
            self.child_dispatch(self.developer_handoff(), tool_id=tool_id)
        )
        assert stderr == ""

    def test_activation_records_child_read_consumption(self) -> None:
        self.activate_child_local()
        actor = self.child_actor_state()
        # 消费证据只落在本 actor：不建父任务/任务缓存，也不开 epoch。
        assert actor.get("consumed_read_ids") == {"read-old": 0}
        entry = (actor.get("consumed_reads") or {}).get("read-old") or {}
        assert entry.get("epoch") == 0
        assert len(str(entry.get("digest") or "")) == 32
        assert "task_identity" not in actor
        assert actor.get("role_source") == "skill_read"
        assert not list((self.state / "tasks").glob("*.json"))

    def test_active_child_read_also_records_consumption(self) -> None:
        """已激活 child 的成功读取同样要留消费证据（停用后重投不再激活）。"""
        self.warm_child()
        self.activate_child_local()
        assert "read-again" not in (self.child_actor_state().get("consumed_read_ids") or {})
        self.allow_child(self.child_skill_read("read-again", event="post_tool", outcome="success"))
        actor = self.child_actor_state()
        assert "read-again" in (actor.get("consumed_read_ids") or {})
        assert actor.get("activation_scope") == "task_active"

    def test_replayed_consumed_read_keeps_deactivation(self) -> None:
        self.activate_child_local()
        self.deactivate_child()
        self.nested_allowed("nested-off")
        # 原样重投停用前的成功读取：同 actor/同 ID/同参数/同结果不是新证据。
        _value, stderr = self.allow_child(
            self.child_skill_read("read-old", event="post_tool", outcome="success")
        )
        assert stderr == ""
        assert self.child_actor_state().get("activation_scope") == "inactive"
        self.nested_allowed("nested-replay")

    def test_replayed_consumed_read_with_changed_payload_keeps_deactivation(self) -> None:
        self.activate_child_local()
        self.deactivate_child()
        # 改参数：同一调用的观测内容变化，仍不是新的读取证据。
        _value, stderr = self.allow_child(
            self.child_skill_read("read-old", event="post_tool", outcome="success", offset=5)
        )
        assert "该调用观测作废" in stderr
        assert self.child_actor_state().get("activation_scope") == "inactive"
        # success ↔ completed 拼写变化同样不恢复。
        self.allow_child(
            self.child_skill_read("read-old", event="post_tool", outcome="completed")
        )
        assert self.child_actor_state().get("activation_scope") == "inactive"
        self.nested_allowed("nested-changed")

    def test_new_tool_id_read_reactivates_local_scope(self) -> None:
        self.activate_child_local()
        self.deactivate_child()
        self.nested_allowed("nested-off")
        self.allow_child(self.child_skill_read("read-new"))
        _value, stderr = self.allow_child(
            self.child_skill_read("read-new", event="post_tool", outcome="success")
        )
        assert stderr == ""
        assert self.child_actor_state().get("activation_scope") == "task_active"
        value, _stderr = self.deny_child(
            self.child_dispatch(self.developer_handoff(), tool_id="nested-new")
        )
        assert "不能再派生" in self.reason(value)
        # 重新激活仍只动本 actor：不建父 root/task。
        parent_root = self.state / "roots" / (
            state.identity_key("root", self.host, "P", "P") + ".json"
        )
        assert not parent_root.exists(), parent_root

    def test_failed_and_ordinary_child_events_do_not_consume_reads(self) -> None:
        self.warm_child()
        self.allow_child(self.child_skill_read("read-fail"))
        _value, stderr = self.allow_child(
            self.child_skill_read("read-fail", event="post_tool", outcome="error")
        )
        assert stderr == ""
        _value, stderr = self.allow_child(
            self.write_tool(
                self.child_session(),
                "src/app.py",
                tool_id="post-1",
                event="post_tool",
                outcome="success",
            )
        )
        assert stderr == ""
        actor = self.child_actor_state()
        assert actor.get("activation_scope") == "inactive"
        assert not (actor.get("consumed_read_ids") or {})
        # 失败读取没有消费该 ID：同一 ID 之后的成功读取仍是激活证据。
        self.allow_child(self.child_skill_read("read-fail", event="post_tool", outcome="success"))
        assert self.child_actor_state().get("activation_scope") == "task_active"


class PiChildSkillReadDeactivationTests(ChildSkillReadDeactivationMixin, HookCase):
    host = "pi"


class AntigravityChildSkillReadDeactivationTests(ChildSkillReadDeactivationMixin, HookCase):
    host = "antigravity"


class CodexChildSkillReadDeactivationTests(ChildSkillReadDeactivationMixin, HookCase):
    host = "codex"


class PiChildSkillReadCliTests(HookCase):
    """真实 CLI 子进程 + 只带 `PI_SUBAGENT_CHILD=1` 的 child 入口。"""

    host = "pi"

    def env(self) -> Dict[str, str]:
        return {
            "PI_SUBAGENT_CHILD": "1",
            "PI_SUBAGENT_PARENT_SESSION": "missing",
        }

    def test_env_child_skill_read_denies_nested_dispatch(self) -> None:
        skill_md = str(self.skill / "SKILL.md")
        session = "env-child"
        env = self.env()
        # 先普通 input（未激活）再读取，确认不依赖任何原始 child 字段。
        _code, value, stderr = self.run_core_cli(
            self.input_payload(session, "把文档里的错别字改一下"), env=env
        )
        assert not self.is_denied(value), value
        _code, value, _stderr = self.run_core_cli(
            self.tool_payload(session, "read", {"path": skill_md}, tool_id="read-pre"), env=env
        )
        assert not self.is_denied(value), value
        _code, value, stderr = self.run_core_cli(
            self.tool_payload(
                session, "read", {"path": skill_md}, tool_id="read-pre", event="post_tool", outcome="success"
            ),
            env=env,
        )
        assert not self.is_denied(value), value
        assert stderr == ""
        _code, value, _stderr = self.run_core_cli(
            self.dispatch_payload(session, self.developer_handoff(), role="developer", tool_id="nested"),
            env=env,
        )
        assert self.is_denied(value), value
        assert "不能再派生" in self.reason(value)

    def test_env_child_consumed_read_replay_keeps_deactivation(self) -> None:
        skill_md = str(self.skill / "SKILL.md")
        session = "env-child-replay"
        env = self.env()
        block = '```easydev-hook\n{"version": 1, "active": false}\n```'

        def read(tool_id: str, event: str = "pre_tool", outcome: Optional[str] = None) -> Dict[str, Any]:
            return self.tool_payload(
                session, "read", {"path": skill_md}, tool_id=tool_id, event=event, outcome=outcome
            )

        def nested(tool_id: str) -> bool:
            _code, value, stderr = self.run_core_cli(
                self.dispatch_payload(
                    session, self.developer_handoff(), role="developer", tool_id=tool_id
                ),
                env=env,
            )
            assert stderr == ""
            return self.is_denied(value)

        self.run_core_cli(self.input_payload(session, "把文档里的错别字改一下"), env=env)
        self.run_core_cli(read("read-old"), env=env)
        _code, value, stderr = self.run_core_cli(
            read("read-old", "post_tool", "success"), env=env
        )
        assert not self.is_denied(value), value
        assert stderr == ""
        assert nested("nested-on")
        # 显式停用后嵌套放行。
        _code, value, _stderr = self.run_core_cli(self.input_payload(session, block), env=env)
        assert not self.is_denied(value), value
        assert not nested("nested-off")
        # 原样重投停用前的成功读取不撤销停用。
        _code, value, stderr = self.run_core_cli(
            read("read-old", "post_tool", "success"), env=env
        )
        assert not self.is_denied(value), value
        assert stderr == ""
        assert not nested("nested-replay")
        # 新的调用 ID 的成功读取仍可局部激活。
        self.run_core_cli(read("read-new"), env=env)
        _code, value, stderr = self.run_core_cli(
            read("read-new", "post_tool", "success"), env=env
        )
        assert not self.is_denied(value), value
        assert stderr == ""
        assert nested("nested-new")

    def test_env_child_failed_read_keeps_nested_allowed(self) -> None:
        skill_md = str(self.skill / "SKILL.md")
        session = "env-child-fail"
        env = self.env()
        self.run_core_cli(self.input_payload(session, "把文档里的错别字改一下"), env=env)
        self.run_core_cli(
            self.tool_payload(session, "read", {"path": skill_md}, tool_id="read-fail"), env=env
        )
        _code, value, stderr = self.run_core_cli(
            self.tool_payload(
                session, "read", {"path": skill_md}, tool_id="read-fail", event="post_tool", outcome="error"
            ),
            env=env,
        )
        assert not self.is_denied(value), value
        assert stderr == ""
        _code, value, stderr = self.run_core_cli(
            self.dispatch_payload(session, self.developer_handoff(), role="developer", tool_id="nested"),
            env=env,
        )
        assert not self.is_denied(value), value
        assert stderr == ""


class AntigravitySameTurnSkillReadTests(HookCase):
    """`PreInvocation #1` → 同回合 `read_file(SKILL.md)` → `PreInvocation #2` 仍能激活。

    官方 `PreInvocation` 的 stdin 不带 `prompt`，事件只能由 `--event PreInvocation`
    指定；同一用户回合的后续调用不能因为 transcript 记录键相同就跳过 `SKILL.md`
    读取的激活分支。
    """

    host = "antigravity"

    def actor_state(self, session: str) -> dict:
        key = state.identity_key("actor", self.host, session, session)
        path = self.state / "actors" / (key + ".json")
        assert path.exists(), path
        return json.loads(path.read_text(encoding="utf-8"))

    def invocation(self, session: str, path: str, number: int) -> Dict[str, Any]:
        return {
            "conversationId": session,
            "invocationNum": number,
            "transcriptPath": path,
            "workspacePaths": [str(self.repo)],
        }

    def test_second_invocation_activates_after_same_turn_skill_read(self) -> None:
        transcript = self.root / "ag-invocation.jsonl"
        transcript.write_text(
            json.dumps({"role": "user", "message": "把文档里的错别字改一下"}, ensure_ascii=False)
            + "\n",
            encoding="utf-8",
        )
        # #1：同回合第一次模型调用，transcript 里还没有 SKILL 读取。
        self.allow(self.invocation("ag1", str(transcript), 1), event="PreInvocation")
        assert self.actor_state("ag1").get("activation_scope") == "inactive"
        # 模型在本回合读了 SKILL.md：transcript 追加 planner 行与结果行。
        with transcript.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "PLANNER_RESPONSE": {
                            "tool_calls": [
                                {"name": "read_file", "args": {"Path": str(self.skill / "SKILL.md")}}
                            ]
                        }
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            handle.write(
                json.dumps(
                    {
                        "type": "GENERIC",
                        "status": "DONE",
                        "content": "Showing lines 1-3 of 3\n---\nname: easydev",
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
        # #2：同一用户回合（transcript 记录键未变）仍必须完成激活。
        self.allow(self.invocation("ag1", str(transcript), 2), event="PreInvocation")
        assert self.actor_state("ag1").get("activation_scope") == "task_active"
        self.deny(self.shell("ag1", "git reset --hard", tool_id="after-skill"))

    def test_full_turn_post_tool_use_then_second_invocation(self) -> None:
        """完整回合：`#1` → 成功 `PostToolUse(read_file SKILL.md)` → `#2` 仍激活。

        `PostToolUse` 先直接激活；`#2` 不得因同一记录键开新 epoch，也不能把
        已激活的会话退回未激活。
        """
        transcript = self.root / "ag-full-turn.jsonl"
        transcript.write_text(
            json.dumps({"role": "user", "message": "把文档里的错别字改一下"}, ensure_ascii=False)
            + "\n",
            encoding="utf-8",
        )
        self.allow(self.invocation("ag3", str(transcript), 1), event="PreInvocation")
        assert self.actor_state("ag3").get("activation_scope") == "inactive"
        with transcript.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "PLANNER_RESPONSE": {
                            "tool_calls": [
                                {"name": "read_file", "args": {"Path": str(self.skill / "SKILL.md")}}
                            ]
                        }
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            handle.write(
                json.dumps(
                    {
                        "type": "GENERIC",
                        "status": "DONE",
                        "content": "Showing lines 1-3 of 3\n---\nname: easydev",
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
        skill_post = {
            "conversationId": "ag3",
            "toolCall": {"name": "read_file", "args": {"Path": str(self.skill / "SKILL.md")}},
            "stepIdx": 2,
            "workspacePaths": [str(self.repo)],
        }
        self.allow(skill_post, event="PostToolUse")
        first = self.actor_state("ag3")
        assert first.get("activation_scope") == "task_active"
        self.allow(self.invocation("ag3", str(transcript), 2), event="PreInvocation")
        second = self.actor_state("ag3")
        assert second.get("activation_scope") == "task_active"
        assert first.get("epoch") == second.get("epoch")
        self.deny(self.shell("ag3", "git reset --hard", tool_id="after-full-turn"))

    def test_activated_turn_keeps_the_same_record_dedupe(self) -> None:
        """已激活后同一记录键仍去重：不重开 epoch、不重开提醒预算。"""
        transcript = self.root / "ag-epoch.jsonl"
        transcript.write_text(
            json.dumps({"role": "user", "message": "使用 easydev，完成这个改动"}) + "\n",
            encoding="utf-8",
        )
        self.allow(self.invocation("ag2", str(transcript), 1), event="PreInvocation")
        first = self.actor_state("ag2")
        self.allow(self.invocation("ag2", str(transcript), 2), event="PreInvocation")
        second = self.actor_state("ag2")
        assert first.get("epoch") == second.get("epoch")
        assert first.get("user_generation") == second.get("user_generation")
