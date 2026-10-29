"""r5 施工审查 U01/U02 的修复行为（真实 core CLI 驱动）。

U01：`>&word` 只有 word 展开为纯数字或 `-` 时复制/关闭 fd；其余整段 word 是文件
目标（等价 `&>word`）。只消费数字前缀会把 `>&1.txt` 误判成 fd 复制。
U02：显式停用（machine context `active=false`）后，停用前的历史 SKILL 读取不得
在普通跟进时把会话重新激活；只有当前回合的新激活证据才恢复，且恢复时隔离旧 child。

Codex/Antigravity 的 transcript 夹具与状态读取复用 `test_r5_fixes`。
"""

from __future__ import annotations

import json
import unittest

from support import HookCase
from test_r5_fixes import (
    AntigravityTranscriptMixin,
    CodexTranscriptMixin,
    StateHelpers,
    machine_block,
    root_states,
)


# ---------------------------------------------------------------------------
# U01：`>&word` 的完整 word/引号/词边界解析
# ---------------------------------------------------------------------------
class U01FdWordRedirectTests(HookCase):
    host = "pi"

    SENTINELS = ("1", "1.txt", "2log", "-file")

    # (命令, 拒绝原因里必须点名的基线目标)：整段 word 是文件目标，不是 fd 复制。
    FILE_TARGET_CASES = (
        ("printf x >&1.txt", "baseline/1.txt"),
        ("printf x >&2log", "baseline/2log"),
        ("printf x >&-file", "baseline/-file"),
        ("printf x 2>&1.txt", "baseline/1.txt"),
        ("printf x >& 1.txt", "baseline/1.txt"),
        ("printf x >& -file", "baseline/-file"),
        ("printf x >& '1.txt'", "baseline/1.txt"),
        ('printf x >&"2log"', "baseline/2log"),
        ("bash -c 'printf x >&2log'", "baseline/2log"),
    )

    # fd 复制/关闭与 `/dev/null`：没有文件写入，普通跟进里必须继续放行。
    FD_CASES = (
        "printf x >&1",
        "printf x 2>&1",
        "printf x >&-",
        "printf x >&2",
        "printf x >& 1",
        'printf x >&"1"',
        "printf x > /dev/null",
    )

    def setUp(self) -> None:
        super().setUp()
        for name in self.SENTINELS:
            (self.repo / name).write_text("USER-SENTINEL\n", encoding="utf-8")

    def assert_sentinels_intact(self) -> None:
        for name in self.SENTINELS:
            self.assertEqual(
                (self.repo / name).read_text(encoding="utf-8"), "USER-SENTINEL\n", name
            )

    def test_developer_denies_fd_words_that_are_file_targets(self) -> None:
        # 拒绝必须来自“缺手工基线”：显式打开手工核对路径。
        self.use_manual_baseline()
        self.activate("dev")
        for index, (command, target) in enumerate(self.FILE_TARGET_CASES):
            with self.subTest(command=command):
                reason = self.deny(self.shell("dev", command, tool_id="u01-dev-%d" % index))
                # 拒绝必须点名被写的文件目标，而不是别的原因凑巧拒绝。
                self.assertIn(target, reason)
        self.assert_sentinels_intact()

    def test_readonly_reviewer_denies_fd_word_file_targets(self) -> None:
        self.activate("rev", self.reviewer_handoff())
        for index, (command, _target) in enumerate(self.FILE_TARGET_CASES):
            with self.subTest(command=command):
                reason = self.deny(self.shell("rev", command, tool_id="u01-rev-%d" % index))
                self.assertIn("只读", reason)
        self.assert_sentinels_intact()

    def test_multiple_fd_word_targets_do_not_hide_behind_fd_duplication(self) -> None:
        self.use_manual_baseline()
        self.activate("dev")
        reason = self.deny(self.shell("dev", "printf x >&1.txt 2>&2log", tool_id="u01-multi"))
        self.assertIn("baseline/", reason)
        self.assert_sentinels_intact()

    def test_fd_duplication_and_close_are_not_file_writes(self) -> None:
        self.activate("rev", self.reviewer_handoff())
        for index, command in enumerate(self.FD_CASES):
            with self.subTest(command=command):
                self.allow(self.shell("rev", command, tool_id="u01-fd-%d" % index))
        self.assert_sentinels_intact()

    def test_numeric_file_targets_without_ampersand_stay_protected(self) -> None:
        # T02 的既有修复不得因为放宽 `>&word` 而退回：`> 1`/`>> 1`/`2> 1` 仍是文件。
        self.use_manual_baseline()
        self.activate("dev")
        for index, command in enumerate(("printf x > 1", "printf x >> 1", "printf x 2> 1")):
            with self.subTest(command=command):
                reason = self.deny(self.shell("dev", command, tool_id="u01-num-%d" % index))
                self.assertIn("baseline/1", reason)
        self.assert_sentinels_intact()


# ---------------------------------------------------------------------------
# U02：显式停用后的历史 SKILL 读取不得重新激活，恢复时隔离旧 child
# ---------------------------------------------------------------------------
class U02DeactivationBoundaryTests(object):
    FOLLOW_UP = "普通问题：解释这个函数"

    def append_entries(self, path: str, entries) -> None:
        with open(path, "a", encoding="utf-8") as handle:
            for entry in entries:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def append_user(self, path: str, text: str) -> None:
        self.append_entries(path, self.user_entries(text))

    def start_child(self) -> None:
        self.activate("P")
        self.allow(
            self.dispatch_payload("P", self.developer_handoff(), role="developer", tool_id="D1")
        )
        self.allow(self.child_start_payload("agent-dev", dispatch_id="D1"))

    def deactivate(self, name: str) -> str:
        path = self.write_transcript("P", [machine_block(active=False)], name)
        _code, value, _stderr = self.run_core(self.input_with_transcript("P", path))
        self.assertFalse(self.is_denied(value), value)
        self.assertEqual(self.actor_state("P")["activation_scope"], "inactive")
        self.assertFalse(root_states(self.state)[0].get("active"))
        return path

    def child_write(self, tool_id: str) -> None:
        """旧 child 的一次完整写入：pre_tool → 真实文件修改 → 成功 post_tool。"""
        self.allow(self.child_write_tool(tool_id))
        (self.repo / "src" / "app.py").write_text("child after stop\n", encoding="utf-8")
        self.allow(self.child_write_tool(tool_id, event="post_tool", outcome="success"))

    def child_write_tool(self, tool_id: str, event: str = "pre_tool", outcome: str = "") -> dict:
        """子代理工具事件：Codex 用 agent_id/is_child，Antigravity 用 agentId/isChild。"""
        payload = self.write_tool(
            "P", "src/app.py", tool_id=tool_id, event=event, outcome=outcome or None
        )
        if self.host == "antigravity":
            payload.update({"agentId": "agent-dev", "isChild": True})
        else:
            payload.update({"agent_id": "agent-dev", "is_child": True})
        return payload

    def new_skill_read_entries(self) -> list:
        """停用后的一次新成功读取：真实 transcript 每次读取都有自己的工具 ID。"""
        id_keys = {"call_id", "id", "tool_call_id", "toolCallId"}

        def rewrite(value):
            if isinstance(value, dict):
                return {
                    key: ("c1-new" if key in id_keys and item == "c1" else rewrite(item))
                    for key, item in value.items()
                }
            if isinstance(value, list):
                return [rewrite(item) for item in value]
            return value

        return rewrite(self.skill_read_entries())

    def test_plain_follow_ups_keep_root_inactive_and_old_child_retired(self) -> None:
        self.start_child()
        before = self.task_for_epoch(1)
        path = self.deactivate("u02-follow.jsonl")
        # 停用后连续多个普通新回合：transcript 里仍有停用前的成功 SKILL 读取。
        for index in range(3):
            with self.subTest(turn=index):
                self.append_user(path, self.FOLLOW_UP)
                _code, value, _stderr = self.run_core(self.input_with_transcript("P", path))
                self.assertFalse(self.is_denied(value), value)
                self.assertEqual(self.actor_state("P")["activation_scope"], "inactive")
                self.assertFalse(root_states(self.state)[0].get("active"))
                # 普通工具与危险 Git 继续放行：停用后不由本 Plugin 管。
                self.allow(self.shell("P", "git reset --hard", tool_id="u02-git-%d" % index))
                self.allow(self.write_tool("P", "src/app.py", tool_id="u02-w-%d" % index))
        # 旧 child 的 pre/post/Stop 都不推进旧任务写序列与审查检查点。
        self.child_write("u02-child")
        self.allow(self.child_done_payload("P", agent="agent-dev", report="**[P1] 证据**\n"))
        after = self.task_for_epoch(1)
        self.assertEqual(after["write_seq"], before["write_seq"])
        self.assertEqual(after["review_checkpoint"], before["review_checkpoint"])

    def test_explicit_reinvocation_restores_in_new_epoch_and_retires_old_child(self) -> None:
        self.start_child()
        path = self.deactivate("u02-reinvoke.jsonl")
        self.append_user(path, "使用 $easydev，开始另一个任务")
        self.run_core(self.input_with_transcript("P", path))
        roots = root_states(self.state)
        self.assertTrue(roots[0].get("active"))
        self.assertEqual(int(roots[0]["epoch"]), 2)
        actor = self.actor_state("P")
        self.assertEqual(actor["activation_scope"], "task_active")
        self.assertEqual(int(actor["epoch"]), 2)
        # 恢复后旧 child 仍不得写入新任务。
        revived = self.task_for_epoch(2)
        self.child_write("u02-old-child")
        self.allow(self.child_done_payload("P", agent="agent-dev", report="**[P1] 旧任务**\n"))
        after = self.task_for_epoch(2)
        self.assertEqual(after["write_seq"], revived["write_seq"])
        self.assertEqual(after["review_checkpoint"], revived["review_checkpoint"])

    def test_machine_active_true_restores_in_new_epoch(self) -> None:
        self.start_child()
        path = self.deactivate("u02-machine.jsonl")
        self.append_user(path, machine_block(active=True, role="main"))
        self.run_core(self.input_with_transcript("P", path))
        roots = root_states(self.state)
        self.assertTrue(roots[0].get("active"))
        self.assertEqual(int(roots[0]["epoch"]), 2)
        self.assertEqual(self.actor_state("P")["activation_scope"], "task_active")

    def test_new_skill_read_after_deactivation_restores_in_new_epoch(self) -> None:
        self.start_child()
        path = self.deactivate("u02-skill.jsonl")
        # 停用之后的新成功读取（不是停用前的历史记录）是契约允许的新激活证据；
        # 真实 transcript 里读取先于下一条用户记录，因此顺序是 读取 → 新用户回合。
        self.append_entries(path, self.new_skill_read_entries())
        self.append_user(path, self.FOLLOW_UP)
        self.run_core(self.input_with_transcript("P", path))
        roots = root_states(self.state)
        self.assertTrue(roots[0].get("active"))
        self.assertEqual(int(roots[0]["epoch"]), 2)
        self.assertEqual(self.actor_state("P")["activation_scope"], "task_active")


class U02CodexTests(
    CodexTranscriptMixin, U02DeactivationBoundaryTests, StateHelpers, HookCase
):
    host = "codex"


class U02AntigravityTests(
    AntigravityTranscriptMixin, U02DeactivationBoundaryTests, StateHelpers, HookCase
):
    host = "antigravity"


if __name__ == "__main__":
    unittest.main()
