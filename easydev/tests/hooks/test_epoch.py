"""F12：父子绑定只按可信分派证据固定 epoch，旧 epoch 不得污染新任务。"""

from __future__ import annotations

import json
import unittest

from support import HookCase


def machine_block(active: bool) -> str:
    return '```easydev-hook\n{"version": 1, "active": %s}\n```\n' % ("true" if active else "false")


class EpochBindingMixin(object):
    def task_states(self) -> list:
        states = []
        directory = self.state / "tasks"
        if not directory.is_dir():
            return states
        for path in directory.glob("*.json"):
            states.append(json.loads(path.read_text(encoding="utf-8")))
        return states

    def actor_states(self) -> dict:
        states = {}
        directory = self.state / "actors"
        if not directory.is_dir():
            return states
        for path in directory.glob("*.json"):
            states[path.name] = json.loads(path.read_text(encoding="utf-8"))
        return states

    def total_write_seq(self) -> int:
        return sum(int(value.get("write_seq") or 0) for value in self.task_states())

    def any_completed_review(self) -> bool:
        return any(bool(value.get("has_completed_code_review")) for value in self.task_states())


class PiEpochBindingTests(EpochBindingMixin, HookCase):
    host = "pi"

    def new_task_input(self, prompt: str, input_id: str, session: str = "P"):
        return self.input_payload(session, prompt, inputId=input_id)

    def test_stale_child_bound_in_old_epoch_cannot_pollute_new_epoch(self) -> None:
        self.activate("P")
        handoff = self.developer_handoff()
        self.allow(self.dispatch_payload("P", handoff, role="developer", tool_id="D1"))
        # 旧 child 在 epoch 1 用可信分派摘要正常绑定。
        self.allow(self.child_input_payload("C", handoff, parent="P"))
        child = [state for state in self.actor_states().values() if state.get("parent_id") == "P"]
        self.assertTrue(child)
        self.assertEqual(child[0]["activation_scope"], "task_active")
        # 父切到新任务 B：新 epoch 开始，旧 child 绑定必须退役。
        self.allow(self.new_task_input("使用 easydev，开始另一个任务", "turn-2"))
        self.write_path("C", "cw", content="value = 5\n")
        self.assertEqual(self.total_write_seq(), 0)
        self.allow(self.child_done_payload("P", "D1"))
        self.assertFalse(self.any_completed_review())
        stale = [
            state for state in self.actor_states().values() if state.get("parent_id") == "P"
        ][0]
        self.assertEqual(stale["activation_scope"], "role_only")

    def test_child_initialized_after_epoch_switch_is_not_bound(self) -> None:
        self.activate("P")
        handoff = self.developer_handoff()
        self.allow(self.dispatch_payload("P", handoff, role="developer", tool_id="D1"))
        self.allow(self.new_task_input("使用 easydev，开始另一个任务", "turn-2"))
        # 旧分派的 epoch 已退役：初始化时只保留局部约束，并披露缺口。
        _code, value, stderr = self.run_core(self.child_input_payload("C", handoff, parent="P"))
        self.assertFalse(self.is_denied(value), value)
        self.assertIn("warning", stderr)
        self.assertEqual(
            [state for state in self.actor_states().values() if state.get("parent_id") == "P"][0][
                "activation_scope"
            ],
            "role_only",
        )

    def test_deactivated_root_retires_child_bindings(self) -> None:
        self.activate("P")
        handoff = self.developer_handoff()
        self.allow(self.dispatch_payload("P", handoff, role="developer", tool_id="D1"))
        # 真实 user 输入的 active=false 使当前 root epoch 的子绑定退役。
        self.allow(self.new_task_input("使用 easydev\n" + machine_block(False), "turn-off"))
        _code, value, _stderr = self.run_core(self.child_input_payload("C", handoff, parent="P"))
        self.assertFalse(self.is_denied(value), value)
        self.write_path("C", "cw", content="value = 6\n")
        self.assertEqual(self.total_write_seq(), 0)
        self.allow(self.child_done_payload("P", "D1"))
        self.assertFalse(self.any_completed_review())

    def test_same_actor_id_in_different_sessions_is_isolated(self) -> None:
        self.allow(self.input_payload("s1", "使用 easydev，改这个文件", actorId="shared"))
        self.write_shared("s1", "w1", "src/app.py", "value = 2\n")
        self.allow(self.input_payload("s2", "使用 easydev，改那个文件", actorId="shared"))
        identities = {value.get("task_identity") for value in self.task_states()}
        self.assertEqual(len(identities), 2)
        self.assertEqual(self.total_write_seq(), 1)
        # s2 自己的写只进它自己的任务。
        self.write_shared("s2", "w2", "src/other.py", "value = 3\n")
        self.assertEqual(self.total_write_seq(), 2)
        # 同 actor ID 的两个 session 各自推进自己的写序列，不共享基线。
        self.assertEqual(sorted(int(v.get("write_seq") or 0) for v in self.task_states()), [1, 1])

    def write_shared(self, session: str, tool_id: str, relative: str, content: str) -> None:
        self.allow(self.write_tool(session, relative, tool_id=tool_id, actorId="shared"))
        target = self.repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        self.allow(
            self.write_tool(
                session,
                relative,
                tool_id=tool_id,
                event="post_tool",
                outcome="success",
                actorId="shared",
            )
        )

    def test_standard_handoff_binds_matched_dispatch(self) -> None:
        self.activate("P")
        handoff = self.reviewer_handoff()
        self.allow(self.dispatch_payload("P", handoff, tool_id="D1"))
        _code, value, _stderr = self.run_core(self.child_input_payload("C", handoff, parent="P"))
        self.assertFalse(self.is_denied(value), value)
        # 已关联 reviewer：写工具被拒绝，且绑定固定在父 epoch 上。
        self.deny(self.write_tool("C", "src/app.py"))
        child = [
            state for state in self.actor_states().values() if state.get("parent_id") == "P"
        ][0]
        self.assertEqual(child["activation_scope"], "task_active")
        self.assertEqual(child["role"], "reviewer")
        self.assertEqual(child["dispatch_id"], "D1")


class CodexEpochBindingTests(EpochBindingMixin, HookCase):
    host = "codex"

    def test_child_start_binding_then_epoch_switch_is_retired(self) -> None:
        self.activate("P")
        handoff = self.developer_handoff()
        self.allow(self.dispatch_payload("P", handoff, role="developer", tool_id="R1"))
        self.allow(self.child_start_payload("agent-1", dispatch_id="R1"))
        # 父开始新任务：旧 child 的 epoch 退役。
        self.allow(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "P",
                "prompt": "使用 easydev，开始另一个任务",
                "cwd": str(self.repo),
                "turn_id": "turn-2",
            }
        )
        self.allow(
            self.write_tool(
                "P", "src/app.py", tool_id="cw", agent_id="agent-1", is_child=True
            )
        )
        (self.repo / "src" / "app.py").write_text("value = 7\n", encoding="utf-8")
        self.allow(
            self.write_tool(
                "P",
                "src/app.py",
                tool_id="cw",
                event="post_tool",
                outcome="success",
                agent_id="agent-1",
                is_child=True,
            )
        )
        self.assertEqual(self.total_write_seq(), 0)
        self.allow(self.child_done_payload("P", report="**[P1] 证据**\n", agent="agent-1"))
        self.assertFalse(self.any_completed_review())


if __name__ == "__main__":
    unittest.main()
