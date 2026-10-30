"""r1 施工审查确定的回归项（F06…F28）在四宿主上的 CLI 行为。"""

from __future__ import annotations

import pytest

import json
import os
import shutil
import stat
import subprocess

from conftest import subtests
from support import HookCase


class RegressionMixin(object):
    # ---- F17 / F26：包装解析与字面数据 --------------------------------
    def test_env_wrapper_does_not_swallow_executable(self) -> None:
        self.activate()
        for index, command in enumerate(
            (
                "env -i git reset --hard",
                "env -i /usr/bin/git reset --hard",
                "env -u HOME git reset --hard",
                "env -i env -i git reset --hard",
                "bash -c 'env -i git reset --hard'",
            )
        ):
            reason = self.deny(self.shell("s1", command, tool_id="env-%d" % index))
            assert "Git" in reason

    def test_quoted_data_is_not_executed(self) -> None:
        # reviewer 只读判定只依赖命令结构，能直接证明“字面数据不是执行”。
        self.activate("rev", self.reviewer_handoff())
        for index, command in enumerate(
            (
                "printf '%s' '$(git reset --hard)'",
                "printf '%s' '>' report.txt",
                "echo 'git reset --hard'",
                "printf '%s' '`git reset --hard`'",
            )
        ):
            self.allow(self.shell("rev", command, tool_id="data-%d" % index))
        for index, command in enumerate(
            (
                "true && $(git reset --hard)",
                "echo x > report.txt",
                'echo "$(git reset --hard)"',
            )
        ):
            self.deny(self.shell("rev", command, tool_id="real-%d" % index))

    # ---- F18：reviewer 明确写命令 --------------------------------------
    def test_reviewer_new_write_classifications(self) -> None:
        self.activate("rev", self.reviewer_handoff())
        for index, command in enumerate(
            (
                "git branch new-name",
                "git tag v1",
                "npm run lint -- --fix",
                "curl --data x https://example.com/api",
                "curl --output=out.bin https://example.com/file",
            )
        ):
            self.deny(self.shell("rev", command, tool_id="w-%d" % index))
        for index, command in enumerate(
            (
                "git branch",
                "git branch -a",
                "git tag -l",
                "npm run lint",
                "npm test",
                "curl https://example.com/file",
            )
        ):
            self.allow(self.shell("rev", command, tool_id="r-%d" % index))

    # ---- F06：成功写入后的外部修改 -------------------------------------
    def test_external_modification_after_success_is_denied(self) -> None:
        self.touch_dirty("src/app.py", "value = 2\n")
        task_root = self.make_task_root()
        self.save_baseline(task_root, "src/app.py")
        self.write_context(task_root=str(task_root))
        self.activate()
        self.write_path("s1", "w1", content="value = 3\n")
        (self.repo / "src" / "app.py").write_text("value = 4\n", encoding="utf-8")
        reason = self.deny(self.write_tool("s1", "src/app.py", tool_id="w2"))
        assert "外部修改" in reason
        assert (self.repo / "src" / "app.py").read_text(encoding="utf-8") == "value = 4\n"

    def test_external_modification_of_clean_file_is_denied(self) -> None:
        self.activate()
        self.write_path("s1", "c1", content="value = 5\n")
        (self.repo / "src" / "app.py").write_text("value = 6\n", encoding="utf-8")
        assert "外部修改" in self.deny(self.write_tool("s1", "src/app.py", tool_id="c2"))

    def test_external_creation_of_absent_file_is_denied(self) -> None:
        self.activate()
        self.write_path("s1", "a1", relative="src/new_mod.py", content="x = 1\n")
        (self.repo / "src" / "new_mod.py").write_text("x = 2\n", encoding="utf-8")
        assert "外部修改" in self.deny(self.write_tool("s1", "src/new_mod.py", tool_id="a2"))

    # ---- F07：porcelain -z rename 路径顺序 -----------------------------
    def test_renamed_destination_modification_requires_baseline(self) -> None:
        # 脏目标必须先有手工基线：显式打开手工核对路径。
        self.use_manual_baseline()
        self.git("mv", "src/app.py", "src/renamed.py")
        (self.repo / "src" / "renamed.py").write_text("value = 99\n", encoding="utf-8")
        self.activate()
        reason = self.deny(self.write_tool("s1", "src/renamed.py", tool_id="rn1"))
        assert "baseline" in reason
        task_root = self.make_task_root()
        self.save_baseline(task_root, "src/renamed.py", "value = 99\n")
        self.write_context(task_root=str(task_root))
        self.activate()
        self.allow(self.write_tool("s1", "src/renamed.py", tool_id="rn2"))

    def test_rename_with_space_and_copy_paths(self) -> None:
        target = "docs/with space.md"
        (self.repo / target).write_text("spaced\n", encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-qm", "space")
        self.git("mv", target, "docs/renamed space.md")
        (self.repo / "docs" / "renamed space.md").write_text("changed\n", encoding="utf-8")
        shutil.copyfile(self.repo / "docs" / "guide.md", self.repo / "docs" / "copied.md")
        self.git("add", "docs/copied.md")
        (self.repo / "docs" / "copied.md").write_text("copied-changed\n", encoding="utf-8")
        self.use_manual_baseline()
        self.activate()
        self.deny(self.write_tool("s1", "docs/renamed space.md", tool_id="sp1"))
        self.deny(self.write_tool("s1", "docs/copied.md", tool_id="cp1"))

    # ---- F09：跨 repo 同相对路径不能共用基线 ---------------------------
    def test_two_repos_same_relative_path_do_not_share_baseline(self) -> None:
        task_root = self.make_task_root()
        self.save_baseline(task_root, "src/app.py", "value = 1\n")
        self.write_context(task_root=str(task_root))
        self.touch_dirty("src/app.py", "value = 2\n")
        self.activate()
        self.allow(self.write_tool("s1", "src/app.py", tool_id="ra1"))

        other = self.root / "repo-b"
        other.mkdir()
        subprocess.run(["git", "init", "-q", "."], cwd=str(other), check=True)
        subprocess.run(["git", "config", "user.email", "b@example.com"], cwd=str(other), check=True)
        subprocess.run(["git", "config", "user.name", "b"], cwd=str(other), check=True)
        (other / "src").mkdir()
        (other / "src" / "app.py").write_text("b-value = 1\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=str(other), check=True)
        subprocess.run(["git", "commit", "-qm", "b"], cwd=str(other), check=True)
        (other / "src" / "app.py").write_text("b-value = 2\n", encoding="utf-8")
        payload = self.write_tool("s1", "src/app.py", tool_id="rb1")
        payload["cwd"] = str(other)
        reason = self.deny(payload)
        assert "baseline_shared_path" in reason.replace("占用", "baseline_shared_path")

    # ---- F10：工具级 cwd 参与目标解析 ---------------------------------
    def test_tool_cwd_is_used_for_baseline_targets(self) -> None:
        sub = self.repo / "sub"
        (sub / "src").mkdir(parents=True)
        (sub / "src" / "app.py").write_text("sub-value = 1\n", encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-qm", "sub")
        (sub / "src" / "app.py").write_text("sub-value = 2\n", encoding="utf-8")
        self.use_manual_baseline()
        self.activate()
        payload = self.tool_payload(
            "s1",
            "Bash" if self.host in {"codex", "dsh"} else "bash",
            {"command": "printf x >> src/app.py", "Cwd": str(sub)},
            tool_id="cwd-1",
        )
        if self.host == "antigravity":
            payload = self.tool_payload(
                "s1",
                "run_command",
                {"CommandLine": "printf x >> src/app.py", "Cwd": str(sub)},
                tool_id="cwd-1",
            )
        self.deny(payload)
        clean = self.tool_payload(
            "s1",
            "Bash" if self.host in {"codex", "dsh"} else "bash",
            {"command": "printf x >> src/app.py", "Cwd": str(self.repo)},
            tool_id="cwd-2",
        )
        if self.host == "antigravity":
            clean = self.tool_payload(
                "s1",
                "run_command",
                {"CommandLine": "printf x >> src/app.py", "Cwd": str(self.repo)},
                tool_id="cwd-2",
            )
        # 仓库根的同一相对路径是干净的：不能被用来替代实际脏目标。
        self.allow(clean)

    # ---- F11：machine context 跨进程持久化 ----------------------------
    def test_machine_models_file_persists_across_processes(self) -> None:
        bad = self.root / "models-bad.json"
        bad.write_text("{not-json", encoding="utf-8")
        block = '```easydev-hook\n{"version": 1, "active": true, "role": "main", "models_file": "%s"}\n```' % bad
        self.activate("s1", block)
        # 下一次分派是独立进程：必须仍用上一次 input 指定的来源。
        reason = self.deny(self.dispatch_payload("s1", self.developer_handoff(), tool_id="d1"))
        assert "模型" in reason

    def test_machine_task_root_persists_across_processes(self) -> None:
        # 该用例验证机器上下文里的 task_root 跨进程生效：显式清空安装上下文来源。
        self.write_context(task_root="")
        task_root = self.make_task_root()
        self.save_baseline(task_root, "src/app.py")
        self.touch_dirty("src/app.py", "value = 2\n")
        block = '```easydev-hook\n{"version": 1, "active": true, "role": "main", "task_root": "%s"}\n```' % task_root
        self.activate("s1", block)
        self.allow(self.write_tool("s1", "src/app.py", tool_id="m1"))

    def test_new_task_does_not_inherit_machine_override(self) -> None:
        bad = self.root / "models-bad.json"
        bad.write_text("{not-json", encoding="utf-8")
        block = '```easydev-hook\n{"version": 1, "active": true, "role": "main", "models_file": "%s"}\n```' % bad
        self.activate("s1", block)
        self.deny(self.dispatch_payload("s1", self.developer_handoff(), tool_id="d1"))
        follow_up = self.input_payload("s1", "使用 easydev，换一个新任务")
        if self.host == "antigravity":
            follow_up["messageId"] = "msg-new-task"
        elif self.host in {"codex", "dsh"}:
            follow_up["turn_id"] = "turn-new-task"
        else:
            follow_up["inputId"] = "input-new-task"
        self.run_core(follow_up)
        self.allow(self.dispatch_payload("s1", self.developer_handoff(), tool_id="d2"))

    # ---- F15：main 的只读 review/audit 请求 ----------------------------
    def test_readonly_review_request_denies_writes(self) -> None:
        self.activate("s1", "使用 easydev，只读 review 这个改动，不要修改代码")
        reason = self.deny(self.write_tool("s1", "src/app.py", tool_id="ro1"))
        assert "只读" in reason
        self.deny(self.shell("s1", "sed -i '' s/1/2/ src/app.py", tool_id="ro2"))
        # 普通开发请求放行，任务切换后不继承只读。
        self.activate("s1", "使用 easydev，实现这个功能改动")
        self.allow(self.write_tool("s1", "src/app.py", tool_id="rw1"))

    def test_plain_review_word_is_not_readonly(self) -> None:
        self.activate("s1", "使用 easydev，实现改动，之后我再 review")
        self.allow(self.write_tool("s1", "src/app.py", tool_id="rw2"))

    def test_readonly_discussion_does_not_freeze_main(self) -> None:
        """讨论“只读”本身不是只读指令：不得把主代理锁成只读。"""
        for index, prompt in enumerate(
            (
                "使用 easydev，reviewer 只读是否合理，能不能用 shell",
                "使用 easydev，审查 reviewer 角色的只读约束是否合理",
                "使用 easydev，reviewer 不修改被审代码，这合理吗",
                # 限定到具体路径的禁令不是全局只读：否则正常修复请求整段被拒。
                "使用 easydev，修这两个问题，但不要改动 SKILL.md 的规则正文",
                "使用 easydev，修这个 bug，不要修改 hook 的现有行为",
            )
        ):
            with subtests.test(prompt=prompt):
                self.activate("s1", prompt)
                self.allow(self.write_tool("s1", "src/app.py", tool_id="disc-%d" % index))

    def test_role_talk_does_not_freeze_main_readonly(self) -> None:
        """根角色只按完整标准交接派生：顺带提到角色/限定语不得把主代理锁成只读。

        1.5.0 修过 READONLY_DIRECTIVE_RE 一侧的同类 P2；角色派生侧同样要求：
        只有“声明 + 角色路径 + 机器模板”三件套齐全才改根角色，否则一条
        `角色：reviewer；不要分派子代理` 就会把“帮我修 hook”整段拒掉。
        """
        for index, prompt in enumerate(
            (
                "使用 easydev 帮我改这个 bug。\n角色：reviewer；不要分派子代理，你自己改。",
                "使用 easydev 修 hook。\n角色：独立 reviewer 这条一直被拒，为什么？帮我修。",
                "使用 easydev，你是独立 reviewer 时该怎么报告？说说就好",
                "使用 easydev，你是什么角色？reviewer 还是 main？",
                "使用 easydev，你是 main 还是 reviewer？",
            )
        ):
            with subtests.test(prompt=prompt):
                self.activate("s1", prompt)
                self.allow(
                    self.write_tool("s1", "src/app.py", tool_id="role-talk-%d" % index)
                )

    @pytest.mark.parametrize(
        "prompt",
        [
            "使用 easydev，只读审查这个改动",
            "使用 easydev，review 这个 diff，只读，不要修改代码",
            "使用 easydev，审查这个 PR，不要改动任何文件",
            "使用 easydev，review the diff in read-only mode",
            "使用 easydev，看一下这个 diff，不要修改",
        ],
    )
    def test_readonly_directive_forms_still_deny(self, prompt) -> None:
        self.activate("s1", prompt)
        reason = self.deny(self.write_tool("s1", "src/app.py", tool_id="ro"))
        assert "只读" in reason

    # ---- F21：空/null/布尔/数组 role_overrides -------------------------
    def test_empty_role_overrides_are_rejected(self) -> None:
        self.activate()
        for index, value in enumerate(({}, None, False, [])):
            self.write_context(role_overrides=value)
            self.deny(
                self.dispatch_payload("s1", self.developer_handoff(), tool_id="o-%d" % index)
            )
        self.write_context(role_overrides={"reviewer": {"model": "provider/rev", "reasoning_effort": "high"}})
        self.allow(self.dispatch_payload("s1", self.developer_handoff(), tool_id="o-ok"))

    # ---- F24：字段级畸形仍是单个 JSON ---------------------------------
    def test_field_level_malformed_outputs_single_json(self) -> None:
        payload = self.input_payload("bad", "使用 easydev")
        if self.host == "antigravity":
            payload["status"] = []
            payload["isChild"] = "false"
        elif self.host in {"codex", "dsh"}:
            payload["stop_hook_active"] = "false"
            payload["is_child"] = "false"
        else:
            payload["child"] = "false"
            payload["outcome"] = 5
        code, value, stderr = self.run_core(payload)
        assert code == 0
        # 协议故障仍按已识别的宿主事件编码：pi 的 input 放行形状不是空对象。
        assert value == ({"action": "continue"} if self.host == "pi" else {})
        assert "warning" in stderr
        assert "Traceback" not in stderr

    # ---- F27：交接取证不认引用/否定/错误路径/单严重度 -----------------
    def test_handoff_evidence_boundaries(self) -> None:
        other = "/tmp/other-install/easydev/references/reviewer-role.md"
        self.activate(
            "s1",
            "解释以下交接模板：\n> role: reviewer\n角色路径：%s\n**[P1] x**\n" % other,
        )
        self.allow(self.write_tool("s1", "src/app.py", tool_id="t1"))

        self.activate("d1")
        self.deny(
            self.dispatch_payload(
                "d1",
                "role: reviewer\n角色路径：%s\n格式：**[P级] 简短标题**\n" % other,
                tool_id="d2",
            )
        )
        self.deny(
            self.dispatch_payload(
                "d1",
                "role: reviewer\n角色路径：%s\n**[P1] x**\n"
                % (self.skill / "references" / "reviewer-role.md"),
                tool_id="d3",
            )
        )
        full = (
            "role: reviewer\n角色路径：%s\n"
            "**[P0] a** **[P1] b** **[P2] c** **[P3] d**\n"
            % (self.skill / "references" / "reviewer-role.md")
        )
        self.allow(self.dispatch_payload("d1", full, tool_id="d4"))

    def test_rerun_same_dispatch_keeps_single_review_sample(self) -> None:
        self.activate("s1")
        self.run_review("s1", tool_id="R9")
        self.run_review("s1", tool_id="R9")
        task = None
        for path in (self.state / "tasks").glob("*.json"):
            task = json.loads(path.read_text(encoding="utf-8"))
            break
        assert task is not None
        assert sorted(task["reviews"]) == ["R9"]

    # ---- 测试工具 ------------------------------------------------------
    def run_review(self, session: str, tool_id: str = "R1", prompt: str = "") -> None:
        self.allow(
            self.dispatch_payload(session, prompt or self.reviewer_handoff(), tool_id=tool_id)
        )

    def complete_review(self, session: str, tool_id: str = "R1") -> None:
        self.allow(self.child_done_payload(session, tool_id))


class CodexP3RegressionTests(HookCase):
    """P3 完成事件只在 Codex 可从生成配置/真实事件序列进入。"""

    host = "codex"

    def run_review(self, tool_id: str = "R1", agent: str = "agent-1") -> None:
        self.allow(self.dispatch_payload("P", self.reviewer_handoff(), tool_id=tool_id))
        self.allow(self.child_start_payload(agent, dispatch_id=tool_id))

    def test_redispatch_does_not_resample_checkpoint(self) -> None:
        self.activate("P")
        self.run_review(tool_id="R1", agent="agent-1")
        self.write_path("P", "w1", content="value = 2\n")
        # 同一分派 ID 重投不得把 checkpoint 抬到新写入之后。
        self.run_review(tool_id="R1", agent="agent-1")
        self.allow(self.child_done_payload("P", report="**[P1] 证据**\n", agent="agent-1"))
        _code, value, _stderr = self.run_core(self.settle_payload("P"))
        assert self.is_continued(value), value

    def test_batch_items_keep_independent_identity(self) -> None:
        self.activate("P")
        handoff = self.reviewer_handoff()
        args = {
            "agent_type": "reviewer",
            "prompt": "",
            "tasks": [
                {"agent_type": "reviewer", "prompt": handoff},
                {"agent_type": "reviewer", "prompt": handoff},
            ],
        }
        self.allow(self.tool_payload("P", "spawn_agent", args, tool_id="B"))
        self.allow(self.child_start_payload("agent-b0", dispatch_id="B#0"))
        self.allow(self.child_done_payload("P", report="**[P1] 证据**\n", agent="agent-b0"))
        task_file = None
        for path in (self.state / "tasks").glob("*.json"):
            value = json.loads(path.read_text(encoding="utf-8"))
            if value.get("reviews"):
                task_file = value
                break
        assert task_file is not None, "task state not found"
        reviews = task_file["reviews"]
        assert "B#0" in reviews
        assert "B#1" in reviews
        assert reviews["B#0"]["status"] == "completed"
        assert reviews["B#1"]["status"] == "started"
        assert task_file["has_completed_code_review"]


class PiRegressionTests(RegressionMixin, HookCase):
    host = "pi"

    def test_child_input_does_not_rearm_root_reminder(self) -> None:
        self.activate("P")
        self.run_review("P")
        self.write_path("P", "w1", content="value = 2\n")
        # pi 无法可靠归因完成事件：不续跑也不虚报，但仍披露能力缺口。
        _code, value, stderr = self.run_core(self.settle_payload("P"))
        assert not self.is_continued(value), value
        assert "P3" in stderr
        self.run_core(self.child_input_payload("C", "继续实现", parent="P"))
        self.run_core(self.child_input_payload("C", "继续跟进", parent="P"))
        _code, value, _stderr = self.run_core(self.settle_payload("P"))
        assert not self.is_continued(value), value


class AntigravityRegressionTests(RegressionMixin, HookCase):
    host = "antigravity"

    def test_child_input_does_not_rearm_root_reminder(self) -> None:
        self.activate("P")
        self.run_review("P")
        self.write_path("P", "w1", content="value = 2\n")
        _code, value, stderr = self.run_core(self.settle_payload("P"))
        assert not self.is_continued(value), value
        assert "P3" in stderr
        self.run_core(self.child_input_payload("C", "继续实现", parent="P"))
        _code, value, _stderr = self.run_core(self.settle_payload("P"))
        assert not self.is_continued(value), value


class CodexRegressionTests(RegressionMixin, HookCase):
    host = "codex"

    def test_child_input_does_not_rearm_root_reminder(self) -> None:
        self.activate("P")
        self.allow(self.dispatch_payload("P", self.reviewer_handoff(), tool_id="R1"))
        self.allow(self.child_start_payload("agent-1", dispatch_id="R1"))
        self.allow(self.child_done_payload("P", report="**[P1] 证据**\n", agent="agent-1"))
        self.write_path("P", "w1", content="value = 2\n")
        _code, value, _stderr = self.run_core(self.settle_payload("P"))
        assert self.is_continued(value), value
        # 子代理的跟进 input 不是根的新用户回合：不重新开提醒预算。
        self.run_core(self.child_input_payload("C", "继续实现", parent="P"))
        _code, value, _stderr = self.run_core(self.settle_payload("P"))
        assert not self.is_continued(value), value
        # 根真实新用户输入才重新开预算。
        self.run_core(
            {"hook_event_name": "UserPromptSubmit", "session_id": "P", "prompt": "继续",
             "cwd": str(self.repo), "turn_id": "turn-9"}
        )
        self.write_path("P", "w2", content="value = 3\n")
        _code, value, _stderr = self.run_core(self.settle_payload("P"))
        assert self.is_continued(value), value


class DshRegressionTests(RegressionMixin, HookCase):
    host = "dsh"

    def test_child_input_does_not_rearm_root_reminder(self) -> None:
        self.activate("P")
        self.run_review("P")
        self.write_path("P", "w1", content="value = 2\n")
        _code, value, stderr = self.run_core(self.settle_payload("P"))
        assert not self.is_continued(value), value
        assert "P3" in stderr
        self.run_core(self.child_input_payload("C", "继续实现", parent="P"))
        _code, value, _stderr = self.run_core(self.settle_payload("P"))
        assert not self.is_continued(value), value


class DocExclusionTests(HookCase):
    host = "pi"

    def test_doc_paths_do_not_hide_rules_tests_or_artifacts(self) -> None:
        (self.repo / "tests").mkdir(exist_ok=True)
        (self.repo / "tests" / "spec.md").write_text("spec\n", encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-qm", "spec")
        self.write_context(doc_paths=["SKILL.md", "docs/guide.md", "tests/spec.md"])
        self.activate("P")
        # 明确文档路径：不计入代码写入（共享写序列不推进）。
        self.write_path("P", "docs", "docs/guide.md", "guide2\n")
        assert self.code_write_seq() == 0
        # Markdown 测试夹具即使被列进 doc_paths 也必须计入。
        self.write_path("P", "spec", "tests/spec.md", "spec2\n")
        assert self.code_write_seq() == 1
        # 运行规则（SKILL.md）即使被误列进 doc_paths 也计入。
        self.write_path("P", "skill", "SKILL.md", "rules\n")
        assert self.code_write_seq() == 2

    def code_write_seq(self) -> int:
        for path in (self.state / "tasks").glob("*.json"):
            value = json.loads(path.read_text(encoding="utf-8"))
            if value.get("task_identity"):
                return int(value.get("last_code_write_seq") or 0)
        pytest.fail("task state not found")

    def run_review(self, session: str, tool_id: str = "R1") -> None:
        self.allow(self.dispatch_payload(session, self.reviewer_handoff(), tool_id=tool_id))

    def complete_review(self, session: str, tool_id: str = "R1") -> None:
        self.allow(self.child_done_payload(session, tool_id))


class PostPairingTests(HookCase):
    """F19/R14：只观察共享写序列，不用“禁用完成事件”的宿主冒充配对证据。"""

    host = "pi"

    def setup_method(self) -> None:
        super().setup_method()
        self.activate("P")

    def write_seq(self) -> int:
        return int(self.task_state().get("write_seq") or 0)

    def test_post_without_pre_does_not_count(self) -> None:
        (self.repo / "src" / "app.py").write_text("value = 2\n", encoding="utf-8")
        self.allow(
            self.write_tool("P", "src/app.py", tool_id="n1", event="post_tool", outcome="success")
        )
        assert self.write_seq() == 0

    def test_post_without_outcome_does_not_count(self) -> None:
        self.allow(self.write_tool("P", "src/app.py", tool_id="n2"))
        (self.repo / "src" / "app.py").write_text("value = 2\n", encoding="utf-8")
        self.allow(self.write_tool("P", "src/app.py", tool_id="n2", event="post_tool"))
        assert self.write_seq() == 0

    def test_failed_post_does_not_count(self) -> None:
        self.allow(self.write_tool("P", "src/app.py", tool_id="n3"))
        (self.repo / "src" / "app.py").write_text("value = 2\n", encoding="utf-8")
        self.allow(
            self.write_tool("P", "src/app.py", tool_id="n3", event="post_tool", outcome="error")
        )
        assert self.write_seq() == 0

    def test_noop_write_does_not_advance_sequence(self) -> None:
        self.allow(self.write_tool("P", "src/app.py", tool_id="n4"))
        # 文件内容未变：no-op 不推进写序列。
        self.allow(
            self.write_tool("P", "src/app.py", tool_id="n4", event="post_tool", outcome="success")
        )
        assert self.write_seq() == 0

    def test_duplicate_post_does_not_advance_twice(self) -> None:
        self.allow(self.write_tool("P", "src/app.py", tool_id="n5"))
        (self.repo / "src" / "app.py").write_text("value = 2\n", encoding="utf-8")
        self.allow(
            self.write_tool("P", "src/app.py", tool_id="n5", event="post_tool", outcome="success")
        )
        (self.repo / "src" / "app.py").write_text("value = 3\n", encoding="utf-8")
        self.allow(
            self.write_tool("P", "src/app.py", tool_id="n5", event="post_tool", outcome="success")
        )
        assert self.task_state()["write_seq"] == 1

    def task_state(self) -> dict:
        for path in (self.state / "tasks").glob("*.json"):
            return json.loads(path.read_text(encoding="utf-8"))
        pytest.fail("task state not found")
