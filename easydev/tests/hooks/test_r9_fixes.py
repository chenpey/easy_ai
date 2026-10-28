"""r8 施工审查 X01–X03 的修复行为（真实 core CLI 驱动）。

X01：行续接预处理与注释、引号、词边界、运算符共用一份词法状态。`#` 只在词首
（行首/空白/运算符后）开始注释，注释里的引号没有语法作用；注释到物理行尾结束
（`\\`+换行在注释里不是续接，下一行仍是真实命令）。否则 `# don't overwrite` 里的
单引号会一直张开，后续真实命令的续接不被消除，`printf x >\\<换行>&1.txt` 变成假
目标换行、`git reset --ha\\<换行>rd` 被放行。
X02：读取消费的幂等键是 `(actor, tool ID)`。同一 ID 的成功读取无论参数摘要是否
变化都不是新的激活证据；摘要变化只让该调用观测失效。
X03：`consumed_read_ids` 跨 epoch 保存停用前见过的调用 ID 且不做容量淘汰，所以
近期摘要索引裁剪后被淘汰的旧 ID 也不能重新激活。
"""

from __future__ import annotations

import subprocess
import sys
import unittest

from support import HOOKS_ROOT, HookCase
from test_r5_fixes import StateHelpers, machine_block, root_states, task_states

if str(HOOKS_ROOT) not in sys.path:
    sys.path.insert(0, str(HOOKS_ROOT))

from hooks import shell  # noqa: E402


# ---------------------------------------------------------------------------
# X01：注释与续行共用词法状态
# ---------------------------------------------------------------------------
class X01CommentLexicalStateTests(HookCase):
    host = "pi"

    SENTINELS = ("1", "1.txt", "1\\\n.txt")

    # 第一行是合法注释（引号在注释里没有语义），下一段是真实命令 + `\`+换行。
    NATURAL_COMMENT_REDIRECTS = (
        "# don't overwrite\nprintf x >\\\n&1.txt",
        "# don't overwrite\nprintf x >\\\n|1.txt",
    )
    NATURAL_COMMENT_GIT = ("# don't overwrite\ngit reset --ha\\\nrd",)
    # 最小注释即可诱发同一根因：注释里只有一个单引号。
    MINIMAL_COMMENT_GIT = ("# '\ngit reset --ha\\\nrd", "# '\ng\\\nit reset --hard")
    # 普通无引号注释是好注释：它不得让续接重新变成数据。
    PLAIN_COMMENT_REDIRECTS = ("# comment\nprintf x >\\\n>1.txt",)
    # 注释行尾的 `\` 不续行：下一行是真实命令，必须按命令判定。
    COMMENT_TRAILING_BACKSLASH = (
        "# note \\\ngit reset --hard",
        "# don't \\\nprintf x >\\\n&1.txt",
    )
    # 注释内的转义引号、`#`、其它引号都不得漏进真实命令。
    COMMENT_QUOTE_VARIANTS = (
        "# it\\'s fine\nprintf x >\\\n&1.txt",
        "# note # '\"` $user\nprintf x >\\\n&1.txt",
    )

    def setUp(self) -> None:
        super().setUp()
        for name in self.SENTINELS:
            (self.repo / name).write_text("USER-SENTINEL\n", encoding="utf-8")

    def redirect_deny_cases(self) -> tuple:
        return (
            self.NATURAL_COMMENT_REDIRECTS
            + self.PLAIN_COMMENT_REDIRECTS
            + self.COMMENT_QUOTE_VARIANTS
            + self.COMMENT_TRAILING_BACKSLASH[1:]
        )

    def assert_sentinels_intact(self) -> None:
        for name in self.SENTINELS:
            self.assertEqual(
                (self.repo / name).read_text(encoding="utf-8"), "USER-SENTINEL\n", name
            )

    def dispatch_command(self, session: str, command: str, tool_id: str) -> dict:
        """经 CLI 判定；只有 allow 才在临时 Bash 里执行原命令（负例绝不执行）。"""
        _code, value, _stderr = self.run_core(self.shell(session, command, tool_id=tool_id))
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

    def test_developer_names_real_target_behind_comment_quotes(self) -> None:
        self.activate("dev")
        for index, command in enumerate(self.redirect_deny_cases()):
            with self.subTest(command=command):
                value = self.dispatch_command("dev", command, "x01-dev-%d" % index)
                # 拒绝必须点名真实词法目标，而不是别的原因凑巧拒绝。
                self.assertTrue(self.is_denied(value), value)
                self.assertIn("baseline/1.txt", self.reason(value))
        self.assert_sentinels_intact()

    def test_comment_quotes_do_not_unhide_dangerous_git(self) -> None:
        self.activate("dev")
        git_cases = (
            self.NATURAL_COMMENT_GIT + self.MINIMAL_COMMENT_GIT + self.COMMENT_TRAILING_BACKSLASH[:1]
        )
        for index, command in enumerate(git_cases):
            with self.subTest(command=command):
                # 危险 Git 只作为字符串投递，绝不执行。
                reason = self.deny(self.shell("dev", command, tool_id="x01-git-%d" % index))
                self.assertIn("Git", reason)
        self.assert_sentinels_intact()

    def test_readonly_reviewer_denies_the_same_forms(self) -> None:
        self.activate("rev", self.reviewer_handoff())
        for index, command in enumerate(self.redirect_deny_cases()):
            with self.subTest(command=command):
                value = self.dispatch_command("rev", command, "x01-rev-%d" % index)
                self.assertTrue(self.is_denied(value), value)
                self.assertIn("只读", self.reason(value))
                # 只读拒绝的文案不含文件名：用同一词法结果核对真实目标。
                violation = shell.reviewer_violation(command)
                self.assertEqual(violation.get("targets"), ["1.txt"], violation)
        for index, command in enumerate(self.NATURAL_COMMENT_GIT + self.MINIMAL_COMMENT_GIT):
            with self.subTest(command=command):
                value = self.run_core(self.shell("rev", command, tool_id="x01-rev-git-%d" % index))[1]
                self.assertTrue(self.is_denied(value), value)
        self.assert_sentinels_intact()

    def test_lexical_state_is_shared_across_comment_quote_and_operator(self) -> None:
        for command in self.redirect_deny_cases():
            with self.subTest(command=command):
                analysis = shell.analyze(command)
                self.assertFalse(analysis.opaque, analysis.as_dict())
                self.assertEqual([item.get("target") for item in analysis.writes], ["1.txt"])
                self.assertEqual(analysis.commands, [["printf", "x"]])
        for command in self.NATURAL_COMMENT_GIT + self.MINIMAL_COMMENT_GIT:
            with self.subTest(command=command):
                self.assertEqual(shell.analyze(command).commands, [["git", "reset", "--hard"]])
        # 注释行尾的反斜杠不续行：下一物理行是真实命令。
        self.assertEqual(
            shell.analyze("# note \\\ngit reset --hard").commands, [["git", "reset", "--hard"]]
        )

    def test_single_quote_hash_and_fd_controls_unchanged(self) -> None:
        self.activate("dev")
        # 单引号内的 `\`+换行是数据，不是续接：目标是那个字面文件名。
        quoted = shell.analyze("printf x >&'1\\\n.txt'")
        self.assertEqual([item.get("target") for item in quoted.writes], ["1\\\n.txt"])
        value = self.run_core(self.shell("dev", "printf x >&'1\\\n.txt'", tool_id="x01-literal"))[1]
        self.assertTrue(self.is_denied(value), value)
        self.assertIn("baseline/1\\\n.txt", self.reason(value))
        self.assert_sentinels_intact()
        # 词中 `#` 不是注释开始；引号内的 `#` 也不是。
        self.assertEqual(shell.analyze("echo foo#bar").commands, [["echo", "foo#bar"]])
        self.assertEqual(shell.analyze("echo '#note'").commands, [["echo", "#note"]])
        # fd 复制/关闭没有文件目标，注释修复不得影响它们。
        for index, command in enumerate(("printf x >&1", "printf x 2>&1", "printf x >&-")):
            value = self.dispatch_command("dev", command, "x01-fd-%d" % index)
            self.assertFalse(self.is_denied(value), value)
        self.assert_sentinels_intact()


class X01CodexCommentLexicalStateTests(X01CommentLexicalStateTests):
    host = "codex"


class X01AntigravityCommentLexicalStateTests(X01CommentLexicalStateTests):
    host = "antigravity"


class X01DshCommentLexicalStateTests(X01CommentLexicalStateTests):
    host = "dsh"


# ---------------------------------------------------------------------------
# X02/X03：读取消费证据的判据
# ---------------------------------------------------------------------------
class SkillReadReplayCase(object):
    """停用后重投 SKILL 读取：按 actor/tool ID 判定的共享夹具。"""

    def read_args(self, path: str, extra=None) -> dict:
        key = "Path" if self.host == "antigravity" else "path"
        args = {key: path}
        args.update(extra or {})
        return args

    def skill_read_payload(
        self,
        session: str,
        tool_id: str,
        path: str = "",
        event: str = "post_tool",
        outcome: str = "success",
        extra=None,
    ) -> dict:
        name = "read_file" if self.host == "antigravity" else "read"
        return self.tool_payload(
            session,
            name,
            self.read_args(path or str(self.skill / "SKILL.md"), extra),
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

    def first_read_activation(self, session: str = "P") -> None:
        """旧任务：直接成功读取 SKILL.md 激活 epoch1，再完成一次真实成功写入。"""
        self.allow(self.skill_read_payload(session, "old-read"))
        root = self.root_for(session)
        self.assertTrue(root.get("active"))
        self.assertEqual(int(root["epoch"]), 1)
        self.write_path(session, "w1")

    def deactivate(self, session: str = "P") -> None:
        _code, value, _stderr = self.run_core(
            self.input_payload(session, machine_block(active=False))
        )
        self.assertFalse(self.is_denied(value), value)
        self.assertEqual(self.actor_state(session)["activation_scope"], "inactive")
        root = self.root_for(session)
        self.assertFalse(root.get("active"))

    def assert_inactive(self, session: str = "P", epoch: int = 1) -> None:
        root = self.root_for(session)
        self.assertFalse(root.get("active"), root)
        self.assertEqual(int(root["epoch"]), epoch)
        actor = self.actor_state(session)
        self.assertEqual(actor["activation_scope"], "inactive")
        self.assertEqual(int(actor["epoch"]), epoch)

    def assert_restored(self, session: str = "P", epoch: int = 2) -> None:
        root = self.root_for(session)
        self.assertTrue(root.get("active"), root)
        self.assertEqual(int(root["epoch"]), epoch)
        actor = self.actor_state(session)
        self.assertEqual(actor["activation_scope"], "task_active")
        self.assertEqual(int(actor["epoch"]), epoch)


class X02ConsumedIdConflictTests(SkillReadReplayCase):
    def test_equivalent_path_with_same_id_does_not_restore(self) -> None:
        self.first_read_activation()
        self.deactivate()
        # 字符串拼接保留 `./`：两条拼写 canonical 指向同一个精确安装文件。
        equivalent = str(self.skill) + "/./SKILL.md"
        _code, _value, stderr = self.run_core(self.skill_read_payload("P", "old-read", equivalent))
        self.assert_inactive()
        self.assertIn("已消费", stderr)
        # 交替重投原摘要/新摘要：两次都不得恢复。
        for _attempt in range(3):
            self.allow(self.skill_read_payload("P", "old-read"))
            self.assert_inactive()
            self.allow(self.skill_read_payload("P", "old-read", equivalent))
            self.assert_inactive()
        # 该调用 ID 的观测已失效，不是新的成功调用。
        actor = self.actor_state("P")
        self.assertEqual(actor["consumed_read_ids"].get("old-read"), 1)
        self.assertIs(actor["consumed_reads"]["old-read"].get("invalid"), True)
        # 普通工具继续放行。
        self.allow(self.shell("P", "git status --short", tool_id="x02-plain-git"))
        self.allow(self.write_tool("P", "docs/guide.md", tool_id="x02-plain-write"))

    def test_changed_arguments_and_outcome_with_same_id_do_not_restore(self) -> None:
        self.first_read_activation()
        self.deactivate()
        # 有效参数变化（同一精确安装文件的额外参数）也是同一调用的观测变化。
        self.allow(self.skill_read_payload("P", "old-read", extra={"limit": 200}))
        self.assert_inactive()
        # 成功状态拼写变化不是新调用。
        for outcome in ("completed", "success"):
            self.allow(self.skill_read_payload("P", "old-read", outcome=outcome))
            self.assert_inactive()
        # 新独立 ID 的真实成功读取仍恢复 epoch2。
        self.allow(self.skill_read_payload("P", "new-read"))
        self.assert_restored(epoch=2)
        self.deny(self.shell("P", "git reset --hard", tool_id="x02-restored-git"))


class X02PiConsumedIdConflictTests(X02ConsumedIdConflictTests, StateHelpers, HookCase):
    host = "pi"


class X02CodexConsumedIdConflictTests(X02ConsumedIdConflictTests, StateHelpers, HookCase):
    host = "codex"


class X02AntigravityConsumedIdConflictTests(X02ConsumedIdConflictTests, StateHelpers, HookCase):
    host = "antigravity"


class X03ConsumedIdRetentionTests(SkillReadReplayCase):
    def test_consumed_id_stays_inactive_across_epochs(self) -> None:
        self.first_read_activation()
        self.deactivate()
        for attempt in range(3):
            self.allow(self.skill_read_payload("P", "old-read"))
            self.assert_inactive(epoch=1)
            self.assertTrue(int(self.task_for_epoch(1)["write_seq"]) == 1, attempt)
        # 普通工具 allow，不因停用变成增强态。
        self.allow(self.shell("P", "git status --short", tool_id="x03-plain-git"))
        self.allow(self.write_tool("P", "docs/guide.md", tool_id="x03-plain-write"))
        # 新 ID 的成功读取恢复 epoch2；epoch2 的读取记录不复活旧 ID。
        self.allow(self.skill_read_payload("P", "epoch2-read"))
        self.assert_restored(epoch=2)
        self.deactivate()
        self.assert_inactive(epoch=2)
        for _attempt in range(3):
            self.allow(self.skill_read_payload("P", "old-read"))
            self.assert_inactive(epoch=2)
            self.allow(self.skill_read_payload("P", "epoch2-read"))
            self.assert_inactive(epoch=2)
        # 又一个新 ID 才恢复 epoch3。
        self.allow(self.skill_read_payload("P", "epoch3-read"))
        self.assert_restored(epoch=3)
        self.deny(self.shell("P", "git reset --hard", tool_id="x03-restored-git"))

    def test_consumed_read_ids_are_isolated_per_actor_and_session(self) -> None:
        self.first_read_activation("A")
        self.deactivate("A")
        self.allow(self.skill_read_payload("A", "old-read"))
        self.assert_inactive("A")
        # 其他 session/actor 用同一 tool ID 的首次成功读取不受 A 的消费记录影响。
        self.allow(self.skill_read_payload("B", "old-read"))
        self.assert_restored("B", epoch=1)
        self.assert_inactive("A")
        # 去重证据保存在 actor 作用域，不进入会随 epoch 清空的 task 缓存。
        for item in task_states(self.state):
            self.assertNotIn("consumed_read_ids", item)


class X03PiConsumedIdRetentionTests(X03ConsumedIdRetentionTests, StateHelpers, HookCase):
    host = "pi"


class X03CodexConsumedIdRetentionTests(X03ConsumedIdRetentionTests, StateHelpers, HookCase):
    host = "codex"


class X03AntigravityConsumedIdRetentionTests(X03ConsumedIdRetentionTests, StateHelpers, HookCase):
    host = "antigravity"


class X03EvictedConsumedIdTests(SkillReadReplayCase):
    """近期摘要索引裁剪后的旧 ID 仍具备不可重新激活的持久证据。

    只在 pi envelope 上跑 >100 个不同调用 ID：淘汰逻辑是宿主无关的 core 状态，
    三个宿主已覆盖 envelope/重投语义，重复跑同一个 101 项循环只增加计数与耗时。
    """

    EVICTED = "old-read"
    FILLER = 101

    def test_evicted_consumed_id_stays_inactive(self) -> None:
        self.first_read_activation()
        for index in range(self.FILLER):
            self.allow(self.skill_read_payload("P", "filler-%03d" % index))
        self.assertEqual(int(self.root_for("P")["epoch"]), 1)
        actor = self.actor_state("P")
        # 前提：近期摘要索引已裁掉这个最早的 ID（判定不能只看它）。
        self.assertNotIn(self.EVICTED, actor.get("consumed_reads") or {})
        retained = (actor.get("consumed_read_ids") or {}).get(self.EVICTED)
        self.deactivate()
        # 同一旧成功 post 完整重投多次：仍保持 inactive/epoch1。
        for attempt in range(3):
            self.allow(self.skill_read_payload("P", self.EVICTED))
            self.assert_inactive(epoch=1)
            self.assertTrue(int(self.task_for_epoch(1)["write_seq"]) == 1, attempt)
        self.allow(self.shell("P", "git status --short", tool_id="x03-evicted-plain-git"))
        # 新 ID 的成功读取仍正常恢复 epoch2。
        self.allow(self.skill_read_payload("P", "epoch2-read"))
        self.assert_restored(epoch=2)
        self.deny(self.shell("P", "git reset --hard", tool_id="x03-evicted-restored-git"))
        # 持久证据是“停用前见过这个 ID”：只存 ID 与首次 epoch，不含内容。
        self.assertEqual(retained, 1)


class X03PiEvictedConsumedIdTests(X03EvictedConsumedIdTests, StateHelpers, HookCase):
    host = "pi"


if __name__ == "__main__":
    unittest.main()
