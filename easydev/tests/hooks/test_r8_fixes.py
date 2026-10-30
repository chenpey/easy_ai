"""r7 施工审查 W01/W02 的修复行为（真实 core CLI 驱动）。

W01：反斜杠 + 真实换行在单引号外是行续接，必须在**运算符组合判定之前**整体
消除。`printf x >\\<换行>>1.txt` 在 Bash 里是 `>>1.txt`；若只在 word 校验时
消除续接，运算符识别仍看原始相邻字符，会把第二个 `>` 当成文件名目标，真实
`1.txt` 的写入绕过基线检查。同一词法结果必须贯通 argv、重定向与 cwd。
W02：成功读取 `SKILL.md` 的调用证据按 actor 作用域保留；停用后重复投递停用
前已消费的同一 PostToolUse（同 actor、同 tool ID、同参数、同成功结果）不是新
激活证据，只有新调用 ID 的真实成功读取才恢复 epoch2。
"""

from __future__ import annotations

import pytest

import subprocess
import sys

from support import HOOKS_ROOT, HookCase
from test_r5_fixes import StateHelpers, machine_block, root_states, task_states

if str(HOOKS_ROOT) not in sys.path:
    sys.path.insert(0, str(HOOKS_ROOT))

from hooks import shell  # noqa: E402


# ---------------------------------------------------------------------------
# W01：运算符之间的行续接
# ---------------------------------------------------------------------------
class W01OperatorContinuationTests(HookCase):
    host = "pi"

    SENTINELS = ("1", "1.txt")

    # 第一行末尾是反斜杠，后面是真实换行；Bash 先消除续接再识别运算符。
    DENY_CASES = (
        "printf x >\\\n>1.txt",  # >>1.txt
        "printf x >\\\n&1.txt",  # >&1.txt（写文件）
        "printf x >\\\n|1.txt",  # >|1.txt
    )

    # fd 复制/关闭没有文件目标；纯数字目标是普通文件。两组都必须保持原语义。
    FD_CASES = ("printf x >&1", "printf x 2>&1", "printf x >&-")
    NUMERIC_CASES = ("printf x > 1", "printf x >> 1", "printf x 2> 1")
    GIT_CASES = ("git reset --ha\\\nrd", "g\\\nit reset --hard")

    def setup_method(self) -> None:
        super().setup_method()
        for name in self.SENTINELS:
            (self.repo / name).write_text("USER-SENTINEL\n", encoding="utf-8")

    def assert_sentinels_intact(self) -> None:
        for name in self.SENTINELS:
            assert (self.repo / name).read_text(encoding="utf-8") == "USER-SENTINEL\n", name

    def dispatch_command(self, session: str, command: str, tool_id: str) -> dict:
        """经 CLI 判定；只有 allow 才在临时 Bash 里执行原命令（负例绝不执行）。"""
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

    def test_developer_denies_targets_across_operator_continuation(self) -> None:
        # 拒绝必须点名真实词法目标并指向缺手工基线：显式打开手工核对路径。
        self.use_manual_baseline()
        self.activate("dev")
        for index, command in enumerate(self.DENY_CASES):
            value = self.dispatch_command("dev", command, "w01-dev-%d" % index)
            # 拒绝必须点名真实词法目标，而不是别的原因凑巧拒绝。
            assert self.is_denied(value), value
            assert "baseline/1.txt" in self.reason(value)
        self.assert_sentinels_intact()

    def test_readonly_reviewer_denies_targets_across_operator_continuation(self) -> None:
        self.activate("rev", self.reviewer_handoff())
        for index, command in enumerate(self.DENY_CASES):
            value = self.dispatch_command("rev", command, "w01-rev-%d" % index)
            assert self.is_denied(value), value
            assert "只读" in self.reason(value)
            # 只读拒绝的文案不含文件名：用同一词法结果核对真实目标。
            violation = shell.reviewer_violation(command)
            assert violation.get("targets") == ["1.txt"], violation
        self.assert_sentinels_intact()

    def test_fd_numeric_and_git_controls_unchanged(self) -> None:
        self.use_manual_baseline()
        self.activate("rev", self.reviewer_handoff())
        for index, command in enumerate(self.FD_CASES):
            value = self.dispatch_command("rev", command, "w01-fd-%d" % index)
            assert not self.is_denied(value), value
        self.activate("dev")
        for index, command in enumerate(self.NUMERIC_CASES):
            value = self.dispatch_command("dev", command, "w01-num-%d" % index)
            assert self.is_denied(value), value
            assert "baseline/1" in self.reason(value)
        for index, command in enumerate(self.GIT_CASES):
            reason = self.deny(self.shell("dev", command, tool_id="w01-git-%d" % index))
            assert "Git" in reason
        self.assert_sentinels_intact()

    def test_continuation_is_joined_before_operator_combination(self) -> None:
        for command in self.DENY_CASES:
            analysis = shell.analyze(command)
            assert not analysis.opaque, analysis.as_dict()
            assert [item.get("target") for item in analysis.writes] == ["1.txt"]
            assert all(item.get("complete") for item in analysis.writes)
            # 第二个运算符不再是 argv 或假目标。
            assert analysis.commands == [["printf", "x"]]
        # fd 前缀也在同一词法结果里与运算符组合。
        for command in ("printf x 2\\\n>1.txt", "printf x &\\\n>1.txt"):
            analysis = shell.analyze(command)
            assert [item.get("target") for item in analysis.writes] == ["1.txt"]
        # 单引号内保持字面：`\`+换行是数据，不是续接。
        quoted = shell.analyze("printf x >&'1\\\n.txt'")
        assert [item.get("target") for item in quoted.writes] == ["1\\\n.txt"]
        # 同一份词法结果贯通 cwd：`cd` 之后的相对目标落在子目录。
        analysis = shell.analyze("cd su\\\nb && printf x >\\\n>1.txt", cwd="/tmp/base")
        assert analysis.writes[-1]["cwd"] == "/tmp/base/sub"
        assert analysis.writes[-1]["target"] == "1.txt"


class W01CodexOperatorContinuationTests(W01OperatorContinuationTests):
    host = "codex"


class W01AntigravityOperatorContinuationTests(W01OperatorContinuationTests):
    host = "antigravity"


# ---------------------------------------------------------------------------
# W02：停用后旧成功读取事件重投
# ---------------------------------------------------------------------------
class W02ReplayGuardMixin(object):
    def skill_read_payload(
        self,
        session: str,
        tool_id: str,
        event: str = "post_tool",
        outcome: str = "success",
    ) -> dict:
        target = str(self.skill / "SKILL.md")
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
        pytest.fail("root not found for %s" % session)

    def first_read_activation(self, session: str = "P") -> None:
        """旧任务：直接成功读取 SKILL.md 激活 epoch1，再完成一次真实成功写入。"""
        self.allow(self.skill_read_payload(session, "old-read"))
        root = self.root_for(session)
        assert root.get("active")
        assert int(root["epoch"]) == 1
        self.write_path(session, "w1")

    def deactivate(self, session: str = "P") -> None:
        _code, value, _stderr = self.run_core(
            self.input_payload(session, machine_block(active=False))
        )
        assert not self.is_denied(value), value
        assert self.actor_state(session)["activation_scope"] == "inactive"
        root = self.root_for(session)
        assert not root.get("active")
        assert int(root["epoch"]) == 1

    def child_fields(self, agent: str) -> dict:
        if self.host == "antigravity":
            return {"agentId": agent, "parentConversationId": "P", "isChild": True}
        if self.host in {"codex", "dsh"}:
            return {"agent_id": agent}
        return {"actorId": agent, "parentId": "P", "child": True}

    def child_write(self, tool_id: str) -> None:
        fields = self.child_fields("agent-dev")
        self.allow(self.write_tool("P", "src/child.py", tool_id=tool_id, **fields))
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

    def assert_still_inactive(self, session: str = "P") -> None:
        root = self.root_for(session)
        assert not root.get("active"), root
        assert int(root["epoch"]) == 1
        actor = self.actor_state(session)
        assert actor["activation_scope"] == "inactive"
        assert int(actor["epoch"]) == 1

    def test_replayed_success_read_after_deactivation_stays_inactive(self) -> None:
        self.first_read_activation()
        # 旧任务里还发起了 developer child 并完成一次写入。
        self.allow(
            self.dispatch_payload("P", self.developer_handoff(), role="developer", tool_id="D1")
        )
        self.allow(self.child_start_payload("agent-dev", dispatch_id="D1"))
        before = self.task_for_epoch(1)
        assert int(before["write_seq"]) == 1
        self.deactivate()
        # 不发起新读取、不执行读取工具：只重投停用前已成功交付的同一 PostToolUse。
        for attempt in range(3):
            self.allow(self.skill_read_payload("P", "old-read"))
            self.assert_still_inactive()
            assert (int(self.task_for_epoch(1)["write_seq"]) == 1), attempt
        # 失败结果重投同样不是新证据。
        self.allow(self.skill_read_payload("P", "old-read", outcome="error"))
        self.assert_still_inactive()
        # 普通工具继续放行，没有把停用会话变成增强态。
        self.allow(self.shell("P", "git status --short", tool_id="plain-git"))
        self.allow(self.write_tool("P", "docs/guide.md", tool_id="plain-write"))
        # 旧 child 的写与完成不推进旧任务记录。
        self.child_write("w-old-child")
        self.allow(self.child_done_payload("P", agent="agent-dev", report="**[P1] 旧任务**\n"))
        after = self.task_for_epoch(1)
        assert int(after["write_seq"]) == int(before["write_seq"])
        assert int(after["review_checkpoint"]) == int(before["review_checkpoint"])
        self.assert_still_inactive()

    def test_new_read_tool_id_restores_epoch_two(self) -> None:
        self.first_read_activation()
        self.deactivate()
        for _attempt in range(2):
            self.allow(self.skill_read_payload("P", "old-read"))
        self.assert_still_inactive()
        # 新调用 ID 的真实成功读取仍是可信激活证据。
        self.allow(self.skill_read_payload("P", "new-read"))
        root = self.root_for("P")
        assert root.get("active")
        assert int(root["epoch"]) == 2
        actor = self.actor_state("P")
        assert actor["activation_scope"] == "task_active"
        assert int(actor["epoch"]) == 2
        assert int(self.task_for_epoch(2)["write_seq"]) == 0
        self.deny(self.shell("P", "git reset --hard", tool_id="restored-git"))

    def test_consumed_read_ids_are_isolated_per_actor_and_session(self) -> None:
        self.first_read_activation("A")
        self.deactivate("A")
        self.allow(self.skill_read_payload("A", "old-read"))
        self.assert_still_inactive("A")
        # 其他 session/actor 用同一 tool ID 的首次成功读取不受 A 的消费记录影响。
        self.allow(self.skill_read_payload("B", "old-read"))
        root_b = self.root_for("B")
        assert root_b.get("active")
        assert int(root_b["epoch"]) == 1
        assert self.actor_state("B")["activation_scope"] == "task_active"
        self.assert_still_inactive("A")
        # 去重证据保存在 actor 作用域并跨 epoch 保留，不随新任务缓存清空。
        self.allow(self.skill_read_payload("B", "second-read"))
        assert int(self.root_for("B")["epoch"]) == 1
        consumed = self.actor_state("B").get("consumed_reads") or {}
        assert sorted(consumed) == ["old-read", "second-read"]
        for item in task_states(self.state):
            assert "consumed_reads" not in item


class W02PiReplayGuardTests(W02ReplayGuardMixin, StateHelpers, HookCase):
    host = "pi"


class W02CodexReplayGuardTests(W02ReplayGuardMixin, StateHelpers, HookCase):
    host = "codex"


class W02AntigravityReplayGuardTests(W02ReplayGuardMixin, StateHelpers, HookCase):
    host = "antigravity"
