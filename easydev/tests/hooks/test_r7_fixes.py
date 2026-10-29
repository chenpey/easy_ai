"""r6 施工审查 V01/V02 的修复行为（真实 core CLI 驱动）。

V01：反斜杠 + 真实换行是 shell 行续接，单引号外必须在词法阶段消除。把它拼进
word 时 `>&1\\<换行>.txt` 会变成另一个假目标 `1<换行>.txt`，`git reset
--ha\\<换行>rd` / `g\\<换行>it` 也不再是同一命令。
V02：停用后通过正常 read 工具对精确安装路径 `SKILL.md` 的**新成功**读取是可信
激活证据，必须与 input/transcript 恢复共用新 epoch/新任务语义，而不是只把 actor
改回 `task_active`。
"""

from __future__ import annotations

import subprocess
import sys
import unittest

from support import HOOKS_ROOT, HookCase
from test_r5_fixes import StateHelpers, machine_block, root_states

if str(HOOKS_ROOT) not in sys.path:
    sys.path.insert(0, str(HOOKS_ROOT))

from hooks import shell  # noqa: E402


# ---------------------------------------------------------------------------
# V01：反斜杠 + 真实换行的行续接
# ---------------------------------------------------------------------------
class V01LineContinuationTests(HookCase):
    host = "pi"

    SENTINELS = ("1", "1.txt")

    # 行续接写在字符串字面量里，保证这里是真实的 `\` + 换行两个字节。
    REDIRECT_CASES = (
        "printf x >&1\\\n.txt",
        'printf x >&"1\\\n.txt"',
        "printf x >& 1\\\n.txt",
        "printf x >&1.t\\\nxt",
        "printf x > 1\\\n.txt",
        "printf x >> 1\\\n.txt",
        "printf x 2> 1\\\n.txt",
    )

    # 只投递字符串，绝不执行：危险 Git 不作为被测命令运行。
    GIT_CASES = (
        "git reset --ha\\\nrd",
        "g\\\nit reset --hard",
        "git re\\\nset --hard HEAD~1",
    )

    # fd 复制/关闭没有文件目标；`>&1`、`2>&1`、`>&-` 必须继续放行。
    FD_CASES = ("printf x >&1", "printf x 2>&1", "printf x >&-")

    # 纯数字普通重定向是文件目标（T02 既有修复不得退回）。
    NUMERIC_CASES = ("printf x > 1", "printf x >> 1", "printf x 2> 1")

    def setUp(self) -> None:
        super().setUp()
        for name in self.SENTINELS:
            (self.repo / name).write_text("USER-SENTINEL\n", encoding="utf-8")

    def assert_sentinels_intact(self) -> None:
        for name in self.SENTINELS:
            self.assertEqual(
                (self.repo / name).read_text(encoding="utf-8"), "USER-SENTINEL\n", name
            )

    def dispatch_command(self, session: str, command: str, tool_id: str) -> dict:
        """经 CLI 判定；只有 allow 才在临时 Bash 里执行原命令（负例绝不执行）。

        allow 后执行只是为了证明“Hook 放行时原工具会改动业务文件”，因此不把
        `>&-` 这类合法的非零退出当成 Hook 判定失败。
        """
        _code, value, stderr = self.run_core(self.shell(session, command, tool_id=tool_id))
        if not self.is_denied(value):
            subprocess.run(
                ["/bin/bash", "-c", command],
                cwd=str(self.repo),
                env=self.environment(),
                capture_output=True,
                text=True,
                timeout=30,
            )
        return value

    def test_developer_denies_continuation_redirect_targets(self) -> None:
        # 拒绝必须点名真实词法目标并指向缺手工基线：显式打开手工核对路径。
        self.use_manual_baseline()
        self.activate("dev")
        for index, command in enumerate(self.REDIRECT_CASES):
            with self.subTest(command=command):
                value = self.dispatch_command("dev", command, "v01-dev-%d" % index)
                # 拒绝必须点名真实词法目标，而不是别的原因凑巧拒绝。
                self.assertTrue(self.is_denied(value), value)
                self.assertIn("baseline/1.txt", self.reason(value))
        self.assert_sentinels_intact()

    def test_readonly_reviewer_denies_continuation_redirects(self) -> None:
        self.activate("rev", self.reviewer_handoff())
        for index, command in enumerate(self.REDIRECT_CASES):
            with self.subTest(command=command):
                value = self.dispatch_command("rev", command, "v01-rev-%d" % index)
                self.assertTrue(self.is_denied(value), value)
                self.assertIn("只读", self.reason(value))
        self.assert_sentinels_intact()

    def test_git_continuation_variants_are_denied(self) -> None:
        self.activate("dev")
        for index, command in enumerate(self.GIT_CASES):
            with self.subTest(command=command):
                reason = self.deny(self.shell("dev", command, tool_id="v01-git-%d" % index))
                self.assertIn("Git", reason)

    def test_fd_duplication_and_numeric_targets_regress(self) -> None:
        self.use_manual_baseline()
        self.activate("rev", self.reviewer_handoff())
        for index, command in enumerate(self.FD_CASES):
            with self.subTest(command=command):
                value = self.dispatch_command("rev", command, "v01-fd-%d" % index)
                self.assertFalse(self.is_denied(value), value)
        self.activate("dev")
        for index, command in enumerate(self.NUMERIC_CASES):
            with self.subTest(command=command):
                value = self.dispatch_command("dev", command, "v01-num-%d" % index)
                self.assertTrue(self.is_denied(value), value)
                self.assertIn("baseline/1", self.reason(value))
        self.assert_sentinels_intact()

    def test_continuation_is_removed_from_redirect_words_and_git_argv(self) -> None:
        # 单引号内保持字面：`\`+换行是数据，不是续接。
        quoted = shell.analyze("printf x >&'1\\\n.txt'")
        self.assertEqual([item.get("target") for item in quoted.writes], ["1\\\n.txt"])
        # 单引号外（含双引号内）消除续接。
        for command in ("printf x >&1\\\n.txt", 'printf x >&"1\\\n.txt"'):
            with self.subTest(command=command):
                analysis = shell.analyze(command)
                self.assertEqual([item.get("target") for item in analysis.writes], ["1.txt"])
        self.assertEqual(shell.argv_of("g\\\nit reset --hard"), [["git", "reset", "--hard"]])
        self.assertEqual(shell.argv_of('echo "a\\\nb"'), [["echo", "ab"]])
        self.assertEqual(shell.argv_of("echo 'a\\\nb'"), [["echo", "a\\\nb"]])
        # 普通重定向与 cwd 分析使用同一份修复后的词法结果。
        analysis = shell.analyze("cd su\\\nb && printf x > 1\\\n.txt", cwd="/tmp/base")
        self.assertEqual(analysis.writes[-1]["cwd"], "/tmp/base/sub")
        self.assertEqual(analysis.writes[-1]["target"], "1.txt")


class V01CodexLineContinuationTests(V01LineContinuationTests):
    host = "codex"
    REDIRECT_CASES = ("printf x >&1\\\n.txt", "printf x > 1\\\n.txt")
    GIT_CASES = ("g\\\nit reset --hard",)


# ---------------------------------------------------------------------------
# V02：停用后直接成功读取 SKILL.md 的恢复
# ---------------------------------------------------------------------------
class V02DirectSkillReadRestoreTests(object):
    FOLLOW_UP = "普通问题：解释这个函数"

    def skill_read_payload(
        self,
        session: str,
        tool_id: str,
        event: str = "post_tool",
        outcome: str = "success",
        path: str = "",
    ) -> dict:
        target = path or str(self.skill / "SKILL.md")
        if self.host == "antigravity":
            return self.tool_payload(
                session,
                "read_file",
                {"Path": target},
                tool_id=tool_id,
                event=event,
                outcome=outcome or None,
            )
        return self.tool_payload(
            session,
            "read",
            {"path": target},
            tool_id=tool_id,
            event=event,
            outcome=outcome or None,
        )

    def root_for(self, session: str) -> dict:
        """按 actor 的 root 绑定取该会话的 root 状态（不用列表下标臆测顺序）。"""
        root_actor = self.actor_state(session).get("root_actor_id") or ""
        for item in root_states(self.state):
            if item.get("root_actor_id") == root_actor:
                return item
        self.fail("root not found for %s" % session)

    def start_child_and_write(self) -> None:
        """旧任务：激活 → 分派 developer child → 本任务一次成功写入 src/app.py。"""
        self.activate("P")
        self.allow(
            self.dispatch_payload("P", self.developer_handoff(), role="developer", tool_id="D1")
        )
        self.allow(self.child_start_payload("agent-dev", dispatch_id="D1"))
        self.write_path("P", "w1")
        task = self.task_for_epoch(1)
        self.assertTrue(task.get("origins"))
        self.assertTrue(task.get("last_success"))
        self.assertEqual(
            (self.repo / "src" / "app.py").read_text(encoding="utf-8"), "value = w1\n"
        )

    def deactivate(self, session: str = "P") -> None:
        _code, value, _stderr = self.run_core(
            self.input_payload(session, machine_block(active=False))
        )
        self.assertFalse(self.is_denied(value), value)
        self.assertEqual(self.actor_state(session)["activation_scope"], "inactive")

    def child_fields(self, agent: str) -> dict:
        """子代理工具事件的宿主字段：各宿主用各自的 child 身份字段。"""
        if self.host == "antigravity":
            return {"agentId": agent, "parentConversationId": "P", "isChild": True}
        if self.host in {"codex", "dsh"}:
            return {"agent_id": agent}
        return {"actorId": agent, "parentId": "P", "child": True}

    def child_write(self, tool_id: str) -> None:
        """旧 child 的一次完整写入：pre_tool → 真实文件修改 → 成功 post_tool。"""
        fields = self.child_fields("agent-dev")
        self.allow(
            self.write_tool("P", "src/child.py", tool_id=tool_id, **fields)
        )
        (self.repo / "src" / "child.py").write_text("child after stop\n", encoding="utf-8")
        self.allow(
            self.write_tool(
                "P",
                "src/child.py",
                tool_id=tool_id,
                event="post_tool",
                outcome="success",
                **fields,
            )
        )

    def test_direct_success_read_restores_new_epoch_and_old_caches_are_gone(self) -> None:
        # 新 epoch 里旧任务留下的脏目标必须重新按手工基线核对，不能复用旧指纹。
        self.use_manual_baseline()
        self.start_child_and_write()
        before = self.task_for_epoch(1)
        self.deactivate()
        self.assertFalse(self.root_for("P").get("active"))
        self.allow(self.skill_read_payload("P", "new-read-1"))
        root = self.root_for("P")
        self.assertTrue(root.get("active"))
        self.assertEqual(int(root["epoch"]), 2)
        actor = self.actor_state("P")
        self.assertEqual(actor["activation_scope"], "task_active")
        self.assertEqual(int(actor["epoch"]), 2)
        self.assertNotIn("deactivated", actor)
        restored = self.task_for_epoch(2)
        self.assertEqual(int(restored["write_seq"]), 0)
        self.assertEqual(restored.get("last_success"), {})
        self.assertEqual(restored.get("reviews"), {})
        self.assertEqual(int(restored["review_checkpoint"]), 0)
        # 旧任务缓存留在旧 epoch，不被新任务复用。
        self.assertEqual(int(before["write_seq"]), 1)
        # 旧任务结果（现在是 dirty）在新任务里必须重新按当前基线核对，不能复用旧指纹。
        reason = self.deny(self.write_tool("P", "src/app.py", tool_id="w2"))
        self.assertIn("baseline", reason)
        self.assertEqual(
            (self.repo / "src" / "app.py").read_text(encoding="utf-8"), "value = w1\n"
        )
        # 旧 child 的写与完成不推进新 epoch。
        self.child_write("w-old-child")
        self.allow(self.child_done_payload("P", agent="agent-dev", report="**[P1] 旧任务**\n"))
        after = self.task_for_epoch(2)
        self.assertEqual(int(after["write_seq"]), int(restored["write_seq"]))
        self.assertEqual(
            int(after["review_checkpoint"]), int(restored["review_checkpoint"])
        )

    def test_new_standard_handoff_child_enters_restored_epoch(self) -> None:
        self.start_child_and_write()
        self.deactivate()
        self.allow(self.skill_read_payload("P", "new-read-2"))
        self.allow(
            self.dispatch_payload("P", self.reviewer_handoff(), role="reviewer", tool_id="D2")
        )
        self.allow(self.child_start_payload("agent-rev", dispatch_id="D2"))
        child = self.actor_state("P", "agent-rev")
        self.assertEqual(child["activation_scope"], "task_active")
        self.assertEqual(int(child["epoch"]), 2)
        self.assertEqual(child.get("role"), "reviewer")
        # 新任务的标准 reviewer 在 epoch2 正常生效：只读拒绝。
        reason = self.deny(
            self.write_tool("P", "src/app.py", tool_id="rev-w", **self.child_fields("agent-rev"))
        )
        self.assertIn("只读", reason)

    def test_failed_requested_and_other_path_reads_do_not_restore(self) -> None:
        other = self.root / "other-install" / "SKILL.md"
        other.parent.mkdir(exist_ok=True)
        other.write_text("---\nname: easydev\n---\n", encoding="utf-8")
        cases = (
            ("requested", dict(event="pre_tool", outcome="")),
            ("failed", dict(outcome="error")),
            ("other-path", dict(path=str(other))),
        )
        for index, (name, options) in enumerate(cases):
            with self.subTest(case=name):
                session = "n%d" % index
                self.allow(self.input_payload(session, "使用 easydev，完成这个改动"))
                self.deactivate(session)
                self.allow(self.skill_read_payload(session, "neg-%d" % index, **options))
                self.assertEqual(self.actor_state(session)["activation_scope"], "inactive")
                root = self.root_for(session)
                self.assertFalse(root.get("active"))
                self.assertEqual(int(root["epoch"]), 1)

    def test_first_skill_read_still_activates_in_epoch_one(self) -> None:
        # 对照：从未激活的会话，首次成功读取 SKILL.md 仍是契约允许的激活入口。
        self.allow(self.skill_read_payload("fresh", "first-read"))
        root = self.root_for("fresh")
        self.assertTrue(root.get("active"))
        self.assertEqual(int(root["epoch"]), 1)
        actor = self.actor_state("fresh")
        self.assertEqual(actor["activation_scope"], "task_active")
        self.deny(self.shell("fresh", "git reset --hard"))


class V02PiTests(V02DirectSkillReadRestoreTests, StateHelpers, HookCase):
    host = "pi"


class V02CodexTests(V02DirectSkillReadRestoreTests, StateHelpers, HookCase):
    host = "codex"


class V02AntigravityTests(V02DirectSkillReadRestoreTests, StateHelpers, HookCase):
    host = "antigravity"


if __name__ == "__main__":
    unittest.main()
