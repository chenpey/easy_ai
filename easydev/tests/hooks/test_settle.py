"""P3：审后写提醒、checkpoint 单调与完成事件能力边界。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from support import HOOKS_ROOT, HookCase, clean_env
from test_r5_fixes import task_states

INSTALL = HOOKS_ROOT / "hooks" / "install.py"


class CodexSettleTests(HookCase):
    """Codex 是唯一可从生成配置进入完成态的宿主。"""

    host = "codex"

    def run_review(self, tool_id: str = "R1", agent: str = "agent-1", prompt: str = "") -> None:
        self.allow(
            self.dispatch_payload(
                "P", prompt or self.reviewer_handoff(), tool_id=tool_id
            )
        )
        self.allow(self.child_start_payload(agent, dispatch_id=tool_id))

    def complete_review(self, agent: str = "agent-1", report: str = "**[P1] 发现** 证据\n") -> None:
        self.allow(self.child_done_payload("P", report=report, agent=agent))

    def write_code(self, tool_id: str, content: str, session: str = "P") -> None:
        self.write_path(session, tool_id, "src/app.py", content)

    def child_write(self, agent: str, tool_id: str, content: str) -> None:
        """已关联子代理的写：Codex 子 Hook 的 session_id 是父 session，用 agent_id 区分 actor。"""
        self.allow(self.write_tool("P", "src/app.py", tool_id=tool_id, agent_id=agent, is_child=True))
        (self.repo / "src" / "app.py").write_text(content, encoding="utf-8")
        self.allow(
            self.write_tool(
                "P",
                "src/app.py",
                tool_id=tool_id,
                event="post_tool",
                outcome="success",
                agent_id=agent,
                is_child=True,
            )
        )

    def settle(self) -> bool:
        _code, value, _stderr = self.run_core(self.settle_payload("P"))
        return self.is_continued(value)

    def task_state(self) -> dict:
        """当前只有一条任务状态时的持久化快照。"""
        states = task_states(self.state)
        assert len(states) == 1, states
        return states[0]

    def test_generated_config_registers_subagent_stop(self) -> None:
        output = self.root / "generated"
        completed = subprocess.run(
            [
                sys.executable,
                str(INSTALL),
                "generate",
                "--host",
                "codex",
                "--plugin-root",
                str(HOOKS_ROOT / "hooks"),
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
        assert completed.returncode == 0, completed.stderr
        config = json.loads((output / "codex-hooks.json").read_text(encoding="utf-8"))
        assert "SubagentStop" in config["hooks"]
        assert "SubagentStart" in config["hooks"]

    def test_review_then_write_reminds_once(self) -> None:
        self.activate("P")
        self.run_review()
        self.write_code("w1", "value = 2\n")
        self.complete_review()
        assert self.settle()
        # 第二次 Stop 无条件不再提醒。
        assert not self.settle()

    def test_write_before_completed_review_is_allowed(self) -> None:
        self.activate("P")
        self.write_code("w1", "value = 2\n")
        self.run_review()
        assert not self.settle()

    def test_incomplete_rsync_after_review_does_not_advance_write_sequence(self) -> None:
        self.activate("P")
        # 先做一次完整代码写，再让审查取样把 checkpoint 推到当前写序列。
        self.write_code("w1", "value = 2\n")
        self.run_review()
        self.complete_review()
        before = self.task_state()
        # 目录源 rsync：目的明确但文件集不可枚举 → 披露后放行，不建完整写序列。
        command = "rsync -a docs/ src/"
        _code, value, stderr = self.run_core(self.shell("P", command, tool_id="rsync-dir"))
        assert not self.is_denied(value), value
        assert "warning" in stderr
        # 夹具直接改目的下已有代码文件，模拟工具真实生效后发成功 post。
        target = self.repo / "src" / "app.py"
        target.write_text("value = 3\n", encoding="utf-8")
        _code, value, stderr = self.run_core(
            self.shell("P", command, tool_id="rsync-dir", event="post_tool", outcome="success")
        )
        assert not self.is_denied(value), value
        assert "写序列记覆盖缺口" in stderr
        after = self.task_state()
        assert target.read_text(encoding="utf-8") == "value = 3\n"
        # 代码字节真的变了，但写序列与 checkpoint 都不推进，也不建立成功豁免。
        assert after["write_seq"] == before["write_seq"]
        assert after["last_code_write_seq"] == before["last_code_write_seq"]
        assert after["review_checkpoint"] == before["review_checkpoint"]
        assert (after.get("last_success") or {}) == (before.get("last_success") or {})
        # 已完成的审后实际改代码却不提醒：目标不完整时 P3 覆盖缺口，按能力边界披露。
        assert not self.settle()

    def test_no_write_after_review_is_allowed(self) -> None:
        self.activate("P")
        self.run_review()
        self.complete_review()
        assert not self.settle()

    def test_completion_without_report_is_not_counted(self) -> None:
        self.activate("P")
        self.run_review()
        self.write_code("w1", "value = 2\n")
        payload = self.child_done_payload("P", agent="agent-1")
        payload["last_assistant_message"] = ""
        _code, value, _stderr = self.run_core(payload)
        assert not self.is_denied(value)
        # 没有结构化报告 → 不算完成 → 不续跑。
        assert not self.settle()

    def test_child_start_binds_dispatch_and_keeps_child_writes_shared(self) -> None:
        self.activate("P")
        developer = self.developer_handoff()
        self.allow(self.dispatch_payload("P", developer, role="developer", tool_id="D1"))
        _code, value, _stderr = self.run_core(
            self.child_input_payload_for("agent-dev", developer),
        )
        assert not self.is_denied(value), value
        self.run_review()
        self.complete_review()
        # 已关联 developer 子代理的写进入共享写序列。
        self.child_write("agent-dev", "cw", "value = 4\n")
        assert self.settle()

    def test_child_settle_does_not_consume_root_budget(self) -> None:
        self.activate("P")
        self.run_review()
        self.write_code("w1", "value = 2\n")
        self.complete_review()
        child_stop = {
            "hook_event_name": "Stop",
            "session_id": "P",
            "agent_id": "agent-1",
            "is_child": True,
            "stop_hook_active": False,
        }
        _code, value, _stderr = self.run_core(child_stop)
        assert not self.is_continued(value)
        assert self.settle()

    def test_two_reviews_out_of_order_keep_max_checkpoint(self) -> None:
        self.activate("P")
        self.write_code("w1", "value = 1\n")
        self.write_code("w2", "value = 2\n")
        self.write_code("w3", "value = 3\n")
        self.run_review(tool_id="R1", agent="agent-1")
        self.write_code("w4", "value = 4\n")
        self.run_review(tool_id="R2", agent="agent-2")
        self.complete_review(agent="agent-2")
        self.complete_review(agent="agent-1")
        assert not self.settle()
        self.write_code("w5", "value = 5\n")
        assert self.settle()
        assert not self.settle()

    def test_stop_hook_active_is_allowed(self) -> None:
        self.activate("P")
        self.run_review()
        self.write_code("w1", "value = 2\n")
        self.complete_review()
        _code, value, _stderr = self.run_core(self.settle_payload("P", stop_hook_active=True))
        assert not self.is_continued(value)

    def test_unrelated_session_write_does_not_pollute_root(self) -> None:
        self.activate("P")
        self.run_review()
        self.complete_review()
        self.activate("U")
        self.write_path("U", "u1", "src/app.py", "value = 9\n")
        assert not self.settle()

    def test_corrupt_state_allows_settle(self) -> None:
        self.activate("P")
        self.run_review()
        self.write_code("w1", "value = 2\n")
        self.complete_review()
        for path in (self.state / "actors").glob("*.json"):
            os.chmod(path, 0o600)
            path.write_text("{not-json", encoding="utf-8")
        _code, value, stderr = self.run_core(self.settle_payload("P"))
        assert not self.is_continued(value)
        assert "warning" in stderr

    def test_markdown_docs_exclusion_is_explicit_only(self) -> None:
        self.write_context(doc_paths=["docs/guide.md"])
        self.activate("P")
        self.run_review()
        self.complete_review()
        self.write_path("P", "docs", "docs/guide.md", "guide2\n")
        assert not self.settle()
        self.write_path("P", "skill", "SKILL.md", "rules\n")
        assert self.settle()

    def test_follow_up_turn_rearms_reminder_without_resetting_baseline(self) -> None:
        self.activate("P")
        self.run_review()
        self.complete_review()
        self.write_code("w1", "value = 2\n")
        assert self.settle()
        _code, value, _stderr = self.run_core(
            {"hook_event_name": "UserPromptSubmit", "session_id": "P", "prompt": "继续",
             "cwd": str(self.repo), "turn_id": "turn-2"}
        )
        self.write_code("w2", "value = 3\n")
        assert self.settle()


class IncapableCompletionMixin(object):
    """pi/Antigravity/DSH：可加载 Stop，但无法可靠归因完成 → P3 不启用。"""

    def run_review(self, tool_id: str = "R1") -> None:
        self.allow(self.dispatch_payload("P", self.reviewer_handoff(), tool_id=tool_id))

    def test_injected_completion_is_not_counted(self) -> None:
        self.activate("P")
        self.run_review()
        self.allow(self.write_tool("P", "src/app.py", tool_id="w1"))
        (self.repo / "src" / "app.py").write_text("value = 2\n", encoding="utf-8")
        self.allow(
            self.write_tool("P", "src/app.py", tool_id="w1", event="post_tool", outcome="success")
        )
        # 事件即使被注入也不得让 P3 续跑（不虚报已支持）。
        self.allow(self.child_done_payload("P", "R1"))
        _code, value, stderr = self.run_core(self.settle_payload("P"))
        assert not self.is_continued(value), value
        assert "P3" in stderr

    def test_child_input_does_not_rearm_root_reminder(self) -> None:
        self.activate("P")
        self.run_review()
        self.write_path("P", "w1", content="value = 2\n")
        _code, value, stderr = self.run_core(self.settle_payload("P"))
        assert not self.is_continued(value), value
        assert "P3" in stderr
        self.run_core(self.child_input_payload("C", "继续实现", parent="P"))
        _code, value, _stderr = self.run_core(self.settle_payload("P"))
        assert not self.is_continued(value), value


class PiSettleTests(IncapableCompletionMixin, HookCase):
    host = "pi"


class AntigravitySettleTests(IncapableCompletionMixin, HookCase):
    host = "antigravity"

    def test_non_normal_termination_is_allowed(self) -> None:
        self.activate("P")
        _code, value, _stderr = self.run_core(
            self.settle_payload("P", terminationReason="CANCELLED", executionNum=3)
        )
        assert not self.is_continued(value)
        _code, value, _stderr = self.run_core(
            self.settle_payload("P", terminationReason="NO_TOOL_CALL", fullyIdle=True, executionNum=4)
        )
        assert not self.is_continued(value)


class DshSettleTests(IncapableCompletionMixin, HookCase):
    host = "dsh"
