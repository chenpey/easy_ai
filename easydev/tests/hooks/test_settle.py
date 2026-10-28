"""P3：审后写提醒、checkpoint 单调与完成事件能力边界。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

from support import HOOKS_ROOT, HookCase, clean_env

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
        self.assertEqual(completed.returncode, 0, completed.stderr)
        config = json.loads((output / "codex-hooks.json").read_text(encoding="utf-8"))
        self.assertIn("SubagentStop", config["hooks"])
        self.assertIn("SubagentStart", config["hooks"])

    def test_review_then_write_reminds_once(self) -> None:
        self.activate("P")
        self.run_review()
        self.write_code("w1", "value = 2\n")
        self.complete_review()
        self.assertTrue(self.settle())
        # 第二次 Stop 无条件不再提醒。
        self.assertFalse(self.settle())

    def test_write_before_completed_review_is_allowed(self) -> None:
        self.activate("P")
        self.write_code("w1", "value = 2\n")
        self.run_review()
        self.assertFalse(self.settle())

    def test_no_write_after_review_is_allowed(self) -> None:
        self.activate("P")
        self.run_review()
        self.complete_review()
        self.assertFalse(self.settle())

    def test_completion_without_report_is_not_counted(self) -> None:
        self.activate("P")
        self.run_review()
        self.write_code("w1", "value = 2\n")
        payload = self.child_done_payload("P", agent="agent-1")
        payload["last_assistant_message"] = ""
        _code, value, _stderr = self.run_core(payload)
        self.assertFalse(self.is_denied(value))
        # 没有结构化报告 → 不算完成 → 不续跑。
        self.assertFalse(self.settle())

    def test_child_start_binds_dispatch_and_keeps_child_writes_shared(self) -> None:
        self.activate("P")
        developer = self.developer_handoff()
        self.allow(self.dispatch_payload("P", developer, role="developer", tool_id="D1"))
        _code, value, _stderr = self.run_core(
            self.child_input_payload_for("agent-dev", developer),
        )
        self.assertFalse(self.is_denied(value), value)
        self.run_review()
        self.complete_review()
        # 已关联 developer 子代理的写进入共享写序列。
        self.child_write("agent-dev", "cw", "value = 4\n")
        self.assertTrue(self.settle())

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
        self.assertFalse(self.is_continued(value))
        self.assertTrue(self.settle())

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
        self.assertFalse(self.settle())
        self.write_code("w5", "value = 5\n")
        self.assertTrue(self.settle())
        self.assertFalse(self.settle())

    def test_stop_hook_active_is_allowed(self) -> None:
        self.activate("P")
        self.run_review()
        self.write_code("w1", "value = 2\n")
        self.complete_review()
        _code, value, _stderr = self.run_core(self.settle_payload("P", stop_hook_active=True))
        self.assertFalse(self.is_continued(value))

    def test_unrelated_session_write_does_not_pollute_root(self) -> None:
        self.activate("P")
        self.run_review()
        self.complete_review()
        self.activate("U")
        self.write_path("U", "u1", "src/app.py", "value = 9\n")
        self.assertFalse(self.settle())

    def test_corrupt_state_allows_settle(self) -> None:
        self.activate("P")
        self.run_review()
        self.write_code("w1", "value = 2\n")
        self.complete_review()
        for path in (self.state / "actors").glob("*.json"):
            os.chmod(path, 0o600)
            path.write_text("{not-json", encoding="utf-8")
        _code, value, stderr = self.run_core(self.settle_payload("P"))
        self.assertFalse(self.is_continued(value))
        self.assertIn("warning", stderr)

    def test_markdown_docs_exclusion_is_explicit_only(self) -> None:
        self.write_context(doc_paths=["docs/guide.md"])
        self.activate("P")
        self.run_review()
        self.complete_review()
        self.write_path("P", "docs", "docs/guide.md", "guide2\n")
        self.assertFalse(self.settle())
        self.write_path("P", "skill", "SKILL.md", "rules\n")
        self.assertTrue(self.settle())

    def test_follow_up_turn_rearms_reminder_without_resetting_baseline(self) -> None:
        self.activate("P")
        self.run_review()
        self.complete_review()
        self.write_code("w1", "value = 2\n")
        self.assertTrue(self.settle())
        _code, value, _stderr = self.run_core(
            {"hook_event_name": "UserPromptSubmit", "session_id": "P", "prompt": "继续",
             "cwd": str(self.repo), "turn_id": "turn-2"}
        )
        self.write_code("w2", "value = 3\n")
        self.assertTrue(self.settle())


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
        self.assertFalse(self.is_continued(value), value)
        self.assertIn("P3", stderr)

    def test_child_input_does_not_rearm_root_reminder(self) -> None:
        self.activate("P")
        self.run_review()
        self.write_path("P", "w1", content="value = 2\n")
        _code, value, stderr = self.run_core(self.settle_payload("P"))
        self.assertFalse(self.is_continued(value), value)
        self.assertIn("P3", stderr)
        self.run_core(self.child_input_payload("C", "继续实现", parent="P"))
        _code, value, _stderr = self.run_core(self.settle_payload("P"))
        self.assertFalse(self.is_continued(value), value)


class PiSettleTests(IncapableCompletionMixin, HookCase):
    host = "pi"


class AntigravitySettleTests(IncapableCompletionMixin, HookCase):
    host = "antigravity"

    def test_non_normal_termination_is_allowed(self) -> None:
        self.activate("P")
        _code, value, _stderr = self.run_core(
            self.settle_payload("P", terminationReason="CANCELLED", executionNum=3)
        )
        self.assertFalse(self.is_continued(value))
        _code, value, _stderr = self.run_core(
            self.settle_payload("P", terminationReason="NO_TOOL_CALL", fullyIdle=True, executionNum=4)
        )
        self.assertFalse(self.is_continued(value))


class DshSettleTests(IncapableCompletionMixin, HookCase):
    host = "dsh"


if __name__ == "__main__":
    unittest.main()
