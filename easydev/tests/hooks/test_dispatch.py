"""P2：模型配置、reviewer 交接与子代理嵌套。"""

from __future__ import annotations

import json
import os

from conftest import subtests
from hooks import core as core_module
from support import HookCase


VALID = {
    "developer": {"model": "provider/dev", "reasoning_effort": "high"},
    "reviewer": {"model": "provider/rev", "reasoning_effort": "high"},
}


class DispatchMixin(object):
    def write_models(self, value) -> None:
        target = self.repo / ".easydev" / "models.json"
        target.parent.mkdir(exist_ok=True)
        target.write_text(
            value if isinstance(value, str) else json.dumps(value), encoding="utf-8"
        )

    def test_valid_models_and_complete_handoff_are_allowed(self) -> None:
        self.write_models(VALID)
        self.activate()
        self.allow(self.dispatch_payload("s1", self.reviewer_handoff()))

    def test_invalid_selected_models_deny(self) -> None:
        self.activate()
        self.write_models("{not-json")
        self.deny(self.dispatch_payload("s1", self.developer_handoff()))
        self.write_models({"developer": {"model": "provider/dev"}})
        self.deny(self.dispatch_payload("s1", self.developer_handoff()))
        self.write_models({"developer": {"model": "nodash", "reasoning_effort": "high"}, **{"reviewer": VALID["reviewer"]}})
        self.deny(self.dispatch_payload("s1", self.developer_handoff()))
        self.write_models({**VALID, "unknown": {}})
        self.deny(self.dispatch_payload("s1", self.developer_handoff()))
        self.write_models({**VALID, "developer": {"model": "provider/dev", "reasoning_effort": "extreme"}})
        self.deny(self.dispatch_payload("s1", self.developer_handoff()))

    def test_duplicate_keys_and_identical_upgrade_are_denied(self) -> None:
        self.activate()
        self.write_models('{"developer": {"model": "p/d", "reasoning_effort": "high"}, "developer": {"model": "p/d", "reasoning_effort": "high"}, "reviewer": {"model": "p/r", "reasoning_effort": "high"}}')
        self.deny(self.dispatch_payload("s1", self.developer_handoff()))
        same = {"developer": VALID["developer"], "reviewer": VALID["reviewer"]}
        self.write_models({**VALID, "upgrades": [same]})
        self.deny(self.dispatch_payload("s1", self.developer_handoff()))

    def test_designer_is_optional_and_valid(self) -> None:
        self.activate()
        self.write_models({**VALID, "designer": {"model": "inherit", "reasoning_effort": "inherit"}})
        self.allow(self.dispatch_payload("s1", self.developer_handoff()))
        self.write_models({**VALID, "designer": {"model": "", "reasoning_effort": "high"}})
        self.deny(self.dispatch_payload("s1", self.developer_handoff()))

    def test_unreadable_source_cannot_be_skipped(self) -> None:
        self.activate()
        user = self.home / ".config" / "easydev" / "models.json"
        user.write_text(json.dumps(VALID), encoding="utf-8")
        os.chmod(user, 0o000)
        self.defer(os.chmod, user, 0o600)
        self.deny(
            self.dispatch_payload("s1", self.developer_handoff()),
            env={"HOME": str(self.home)},
        )

    def test_invalid_models_file_at_dispatch_entry_never_falls_back(self) -> None:
        """经真实上下文传递（install-context `models_file`）拒绝，而非只测选择层：

        项目配置与用户配置都放合法哨兵——一旦回退就会被放行，因此 deny 同时证明
        “没有回退到项目/用户配置”。
        """
        self.write_models(VALID)
        (self.home / ".config" / "easydev" / "models.json").write_text(
            json.dumps(VALID), encoding="utf-8"
        )
        self.activate()
        for value in ("", "   ", 123, True, ["x"], {"path": "x"}, None):
            with subtests.test(value=value):
                self.write_context(models_file=value)
                reason = self.deny(self.dispatch_payload("s1", self.developer_handoff()))
                assert "对话指定" in reason
                assert "不能静默回退" in reason

    def test_explicit_missing_models_file_denies(self) -> None:
        self.activate()
        self.write_context(models_file=str(self.root / "missing.json"))
        self.deny(self.dispatch_payload("s1", self.developer_handoff()))

    def test_role_overrides_partial_fields(self) -> None:
        self.write_models(VALID)
        self.activate()
        self.write_context(role_overrides={"reviewer": {"reasoning_effort": "max"}})
        self.allow(self.dispatch_payload("s1", self.reviewer_handoff()))
        self.write_context(role_overrides={"reviewer": {"model": "provider/other"}})
        self.allow(self.dispatch_payload("s1", self.reviewer_handoff()))
        self.write_context(role_overrides={"reviewer": {}})
        self.deny(self.dispatch_payload("s1", self.reviewer_handoff()))
        self.write_context(role_overrides={"reviewer": {"reasoning_effort": "extreme"}})
        self.deny(self.dispatch_payload("s1", self.reviewer_handoff()))
        self.write_context(role_overrides={"reviewer": {"unknown": "x"}})
        self.deny(self.dispatch_payload("s1", self.reviewer_handoff()))

    def test_invalid_base_is_not_rescued_by_valid_override(self) -> None:
        self.write_models({"developer": {"model": "p/d", "reasoning_effort": "high"}})
        self.activate()
        self.write_context(role_overrides={"reviewer": {"reasoning_effort": "max"}})
        self.deny(self.dispatch_payload("s1", self.reviewer_handoff()))

    def test_reviewer_handoff_requires_path_and_format(self) -> None:
        self.activate()
        self.write_models(VALID)
        no_path = "role: reviewer\n只读审查 diff。\n**[P级] 简短标题**\n"
        self.deny(self.dispatch_payload("s1", no_path))
        no_format = "role: reviewer\n只读审查 diff。\nreferences/reviewer-role.md\n"
        self.deny(self.dispatch_payload("s1", no_format))
        self.allow(self.dispatch_payload("s1", self.reviewer_handoff()))

    def test_role_declaration_accepts_common_markdown_forms(self) -> None:
        """同一声明在 Markdown 强调/标题/编号列表/复合标签下都应被识别。

        交接模板 `角色/读写边界/…：reviewer` 是技能文档自己的写法，必须可检出，
        否则照模板写交接的模型会被稳定拒绝。
        """
        self.activate()
        self.write_models(VALID)
        role_path = str(self.skill / "references" / "reviewer-role.md")
        heads = (
            "**角色**：reviewer",
            "**Role**: reviewer",
            "## 角色：reviewer",
            "1. 角色：reviewer",
            "角色声明：reviewer",
            "你的角色是 reviewer",
            "角色/读写边界/项目与规则路径/授权基线：reviewer，只读",
        )
        for index, head in enumerate(heads):
            with subtests.test(head=head):
                prompt = "%s\n只读审查以下 diff。\n角色路径：%s\n**[P级] 简短标题**\n" % (
                    head,
                    role_path,
                )
                self.allow(
                    self.dispatch_payload("s1", prompt, tool_id="decl-%d" % index)
                )

    def test_role_declaration_accepts_qualified_reviewer_forms(self) -> None:
        """真实交接的角色值常带限定语，或在同一行接着写禁止项。

        回放被拒的 11 份真实 reviewer 交接：`角色：独立 reviewer`、
        `角色声明：你是 easydev 的独立 reviewer`、`你是独立设计审查者（read-only）`
        都被判“缺少角色声明”；禁令与声明同行时整行被丢弃，声明与格式证据一起
        消失。这里逐条锁住可接受写法。
        """
        self.activate()
        self.write_models(VALID)
        role_path = str(self.skill / "references" / "reviewer-role.md")
        heads = (
            "角色：独立 reviewer",
            "角色/读写边界：独立 reviewer，只读审查，不改被审代码。",
            "角色声明：你是 easydev 的独立 reviewer（reviewer-role）。",
            "你是独立 reviewer，只读审查。禁止修改任何文件。",
            "你是 EasyDev 的独立设计审查者（read-only，不实施、不修改仓库）。",
            "你是独立施工审查实例（只读；不得修改任何文件、不得 commit/push）。",
        )
        for index, head in enumerate(heads):
            with subtests.test(head=head):
                prompt = "%s\n角色路径：%s\n**[P级] 简短标题**\n" % (head, role_path)
                self.allow(
                    self.dispatch_payload("s1", prompt, tool_id="qual-%d" % index)
                )

    def test_evidence_before_same_line_prohibition_still_counts(self) -> None:
        """否定词只否定它后面的话：同行尾部禁令不得吞掉前面的声明与模板。"""
        self.activate()
        self.write_models(VALID)
        role_path = str(self.skill / "references" / "reviewer-role.md")
        prompt = (
            "角色：reviewer；角色路径：%s；每条发现以 **[P级] 简短标题** 起头；"
            "不要复述我的结论。\n" % role_path
        )
        self.allow(self.dispatch_payload("s1", prompt, tool_id="same-line"))

    def test_role_path_followed_by_fullwidth_punctuation_counts(self) -> None:
        """真实交接写成 `…/reviewer-role.md（先读它，再开始）。`，路径后不是空格。"""
        self.activate()
        self.write_models(VALID)
        role_path = str(self.skill / "references" / "reviewer-role.md")
        prompt = "角色：reviewer\n角色规则文件：%s（先读它，再开始）。\n**[P级] 简短标题**\n" % role_path
        self.allow(self.dispatch_payload("s1", prompt, tool_id="fw-path"))

    def test_finding_format_accepts_pipe_level_placeholder(self) -> None:
        """`**[P0|P1|P2|P3] 简短标题**` 是冻结核可用的等价占位写法。

        真实设计审查交接就是这么写模板的（两条），却被判“缺少机器格式”。
        """
        self.activate()
        self.write_models(VALID)
        role_path = str(self.skill / "references" / "reviewer-role.md")
        prompt = (
            "角色：reviewer\n角色路径：%s\n"
            "报告格式：每条以 `**[P0|P1|P2|P3] 简短标题**` 起头。\n" % role_path
        )
        self.allow(self.dispatch_payload("s1", prompt, tool_id="pipe-format"))

    def prompt_only_dispatch(self, prompt: str, tool_id: str):
        """不带 `agent`/`agent_type` 字段的分派：角色只能从 prompt 推断。"""
        if self.host == "antigravity":
            return self.tool_payload("s1", "invoke_subagent", {"prompt": prompt}, tool_id=tool_id)
        if self.host in {"codex", "dsh"}:
            return self.tool_payload("s1", "spawn_agent", {"prompt": prompt}, tool_id=tool_id)
        return self.tool_payload("s1", "subagent", {"action": "create", "task": prompt}, tool_id=tool_id)

    def test_prompt_only_dispatch_does_not_read_narration_as_reviewer(self) -> None:
        """`agent` 缺失时只能看 prompt：叙述句里的“独立审查者”不得把开发分派判成审查。

        `审查由独立审查者完成`、`完成后由独立 reviewer 复核，不要自行宣称完成`
        都是描述；后者还验证了“否定词只截断前缀”不会把叙述变成声明（行首锚定）。
        提问句同理：`你是什么角色？reviewer 还是 main？` 不是交接声明。误判的
        后果是整单 `deny`（reviewer 交接缺项）。
        """
        self.activate()
        self.write_models(VALID)
        for index, prompt in enumerate(
            (
                "实现这个函数并回报。\n开发完后审查由独立审查者完成，报告列未覆盖范围。",
                "实现这个函数，完成后由独立 reviewer 复核，不要自行宣称完成。",
                "改完交给 reviewer 子代理，不要自己宣称完成。",
                "实现这个函数并回报。\n你是什么角色？reviewer 还是 main？",
                "实现这个函数并回报。\n你是 main 还是 reviewer？",
                "实现这个函数并回报。\n你是什么角色？独立审查者还是主代理？",
                "实现这个函数并回报。\n你是 main 还是独立审查者？",
            )
        ):
            with subtests.test(prompt=prompt):
                self.allow(self.prompt_only_dispatch(prompt, "narr-%d" % index))

    def test_prompt_only_complete_reviewer_handoff_is_allowed(self) -> None:
        """prompt 兜底路径的正面契约：整句声明 + 角色路径 + 模板仍被接受。"""
        self.activate()
        self.write_models(VALID)
        role_path = str(self.skill / "references" / "reviewer-role.md")
        prompt = "你是独立 reviewer，只读审查。\n角色路径：%s\n**[P级] 简短标题**\n" % role_path
        self.allow(self.prompt_only_dispatch(prompt, "prompt-only-rev"))

    def test_role_path_evidence_survives_fullwidth_characters_in_install_root(self) -> None:
        """安装根含全角标点（`资料（备份）`）时路径仍是证据。

        把全角标点当分隔符会把这种安装根切碎、让路径证据永远不可检出（任何
        reviewer 分派都被拒且无法通过改写交接绕过），所以只能对候选“剥包裹”。
        """
        target = self.root / "资料（备份）" / "easydev" / "references" / "reviewer-role.md"
        target.parent.mkdir(parents=True)
        target.write_text("reviewer\n", encoding="utf-8")
        assert core_module.role_path_evidence("角色路径：%s" % target, "reviewer", str(target))
        assert core_module.role_path_evidence("角色路径：（%s）" % target, "reviewer", str(target))
        assert core_module.role_path_evidence(
            "角色细则见 `%s`（先读它，再开始）。" % target, "reviewer", str(target)
        )
        # 组合情形：安装根含全角括号，且路径后还跟着全角注释。
        assert core_module.role_path_evidence(
            "角色路径：%s（先读它，再开始）。" % target, "reviewer", str(target)
        )
        assert not core_module.role_path_evidence(
            "角色路径：/tmp/other/easydev/references/reviewer-role.md", "reviewer", str(target)
        )

    def test_discussed_negated_or_partial_role_wording_is_not_a_declaration(self) -> None:
        """放宽识别不等于取消契约：讨论、否定与半截写法仍不算声明。"""
        self.activate()
        self.write_models(VALID)
        role_path = str(self.skill / "references" / "reviewer-role.md")
        for index, head in enumerate(
            (
                "角色：应该是 reviewer 还是 developer？",
                "不要用 reviewer 模板，先讨论一下。",
                "reviewer 只读是否合理？",
                "你想让 reviewer 看哪部分？",
            )
        ):
            with subtests.test(head=head):
                prompt = "%s\n角色路径：%s\n**[P级] 简短标题**\n" % (head, role_path)
                reason = self.deny(
                    self.dispatch_payload("s1", prompt, tool_id="neg-%d" % index)
                )
                assert "角色声明" in reason

    def test_missing_handoff_message_names_the_literal_to_add(self) -> None:
        """只报“缺什么”模型只能猜格式；拒绝理由必须给出可照抄的字面量。"""
        self.activate()
        self.write_models(VALID)
        reason = self.deny(
            self.dispatch_payload("s1", "只读审查以下 diff。\n", tool_id="hint")
        )
        assert "角色：reviewer" in reason
        assert str(self.skill / "references" / "reviewer-role.md") in reason
        assert "**[P级] 简短标题**" in reason

    def test_handoff_role_still_must_be_declared(self) -> None:
        """放宽识别不等于取消契约：没有声明行的交接仍然拒绝。"""
        self.activate()
        self.write_models(VALID)
        prompt = (
            "只读审查以下 diff。\n角色路径：%s\n**[P级] 简短标题**\n"
            % (self.skill / "references" / "reviewer-role.md")
        )
        reason = self.deny(self.dispatch_payload("s1", prompt, tool_id="no-decl"))
        assert "角色声明" in reason

    def test_inline_template_explanation_keeps_complete_handoff(self) -> None:
        """“模板如下：”同一行里的冻结模板仍是格式证据，纯解释行仍被丢弃。"""
        self.activate()
        self.write_models(VALID)
        role_path = str(self.skill / "references" / "reviewer-role.md")
        base = "role: reviewer\n只读审查以下 diff。\n角色路径：%s\n" % role_path
        self.allow(
            self.dispatch_payload(
                "s1", base + "发现模板如下：**[P级] 简短标题**\n", tool_id="inline-generic"
            )
        )
        self.allow(
            self.dispatch_payload(
                "s1",
                base + "template: **[P0] a** **[P1] b** **[P2] c** **[P3] d**\n",
                tool_id="inline-four",
            )
        )
        for tool_id, extra in (
            ("inline-one", "模板如下：**[P1] x**\n"),
            ("inline-quoted", "> 模板如下：**[P级] 简短标题**\n"),
            ("inline-negated", "不要使用模板如下：**[P级] 简短标题**\n"),
        ):
            reason = self.deny(self.dispatch_payload("s1", base + extra, tool_id=tool_id))
            assert "机器格式" in reason

    def test_developer_task_without_reviewer_template_is_allowed(self) -> None:
        self.activate()
        self.write_models(VALID)
        self.allow(self.dispatch_payload("s1", "实现这个函数并回报。", role="developer"))
        self.allow(self.dispatch_payload("s1", "不要读 references/reviewer-role.md，直接实现。", role="developer"))

    def test_inactive_dispatch_is_allowed(self) -> None:
        self.write_models("{not-json")
        self.allow(self.dispatch_payload("other", self.reviewer_handoff()))

    def test_high_priority_valid_config_ignores_broken_low_priority(self) -> None:
        self.activate()
        self.write_models(VALID)
        (self.home / ".config" / "easydev" / "models.json").write_text("{broken", encoding="utf-8")
        self.allow(self.dispatch_payload("s1", self.developer_handoff()))

    def test_status_and_wait_are_not_nested_dispatches(self) -> None:
        self.activate()
        self.write_models(VALID)
        if self.host == "pi":
            payload = self.tool_payload(
                "s1", "subagent", {"action": "wait", "id": "x"}, tool_id="wait"
            )
        else:
            payload = self.tool_payload("s1", "wait", {"id": "x"}, tool_id="wait")
        self.allow(payload)


class PiDispatchTests(DispatchMixin, HookCase):
    host = "pi"

    def test_child_cannot_dispatch_nested(self) -> None:
        self.activate("parent")
        self.write_models(VALID)
        child_env = {"PI_SUBAGENT_CHILD": "1", "PI_SUBAGENT_PARENT_SESSION": "parent"}
        self.run_core(
            {
                "event": "input",
                "sessionId": "child",
                "prompt": self.reviewer_handoff(),
                "cwd": str(self.repo),
                "child": True,
            },
            env=child_env,
        )
        reason = self.deny(
            self.dispatch_payload("child", self.reviewer_handoff(), tool_id="d2"),
            env=child_env,
        )
        assert "子代理" in reason

    def test_unactivated_child_dispatch_is_allowed(self) -> None:
        # 冻结契约：未激活一律放行；只带 PI_SUBAGENT_CHILD 的普通子任务不属于
        # 本 Plugin 的分派语义，不能因为它派生子代理就被拦截。
        env = {"PI_SUBAGENT_CHILD": "1", "PI_SUBAGENT_PARENT_SESSION": "missing"}
        self.allow(
            self.dispatch_payload("child", self.developer_handoff(), tool_id="nc"),
            env=env,
        )
        self.allow(
            self.dispatch_payload("child", self.reviewer_handoff(), tool_id="nc2"),
            env=env,
        )

    def test_batch_item_with_bad_handoff_denies_whole_call(self) -> None:
        self.activate()
        self.write_models(VALID)
        payload = self.tool_payload(
            "s1",
            "subagent",
            {
                "action": "create",
                "tasks": [
                    {"agent": "developer", "task": "实现"},
                    {"agent": "reviewer", "task": "只读审查，没有格式"},
                ],
            },
            tool_id="batch",
        )
        self.deny(payload)


class AntigravityDispatchTests(DispatchMixin, HookCase):
    host = "antigravity"


class CodexDispatchTests(DispatchMixin, HookCase):
    host = "codex"


class DshDispatchTests(DispatchMixin, HookCase):
    host = "dsh"

    def test_lost_reviewer_prompt_warns_and_allows(self) -> None:
        self.activate()
        self.write_models(VALID)
        payload = self.tool_payload(
            "s1", "subagent", {"agent_type": "reviewer", "command": ""}, tool_id="lost"
        )
        _code, value, stderr = self.run_core(payload)
        assert not self.is_denied(value)
        assert "warning" in stderr
