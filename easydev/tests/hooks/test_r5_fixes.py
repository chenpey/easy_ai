"""r4 施工审查 T01–T11 的修复行为（真实 core CLI 驱动）。

T04/T06 的 P0 读取守卫用例在
`tests/experiments/agent_runner/test_replay_read_guard.py` 的部署 command 用例里；
本文件覆盖其余编号的 core CLI、临时 Git、临时 transcript 与临时安装夹具行为。
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

from support import HOOKS_ROOT, HookCase


def task_states(state_dir: Path):
    directory = state_dir / "tasks"
    if not directory.is_dir():
        return []
    return [json.loads(path.read_text(encoding="utf-8")) for path in sorted(directory.glob("*.json"))]


def actor_states(state_dir: Path):
    directory = state_dir / "actors"
    if not directory.is_dir():
        return []
    return [json.loads(path.read_text(encoding="utf-8")) for path in sorted(directory.glob("*.json"))]


def root_states(state_dir: Path):
    directory = state_dir / "roots"
    if not directory.is_dir():
        return []
    return [json.loads(path.read_text(encoding="utf-8")) for path in sorted(directory.glob("*.json"))]


def machine_block(**fields) -> str:
    payload = {"version": 1}
    payload.update(fields)
    return "```easydev-hook\n%s\n```" % json.dumps(payload)


class StateHelpers(object):
    def hook_state_module(self):
        modules = str(HOOKS_ROOT / "hooks")
        if modules not in sys.path:
            sys.path.insert(0, modules)
        import state as hook_state  # noqa: E402  (hooks 目录内的平铺模块)

        return hook_state

    def task_for_epoch(self, epoch: int) -> dict:
        for item in task_states(self.state):
            if int(item.get("task_epoch") or 0) == epoch:
                return item
        self.fail("epoch %d task not found" % epoch)

    def actor_state(self, session: str, agent: str = "") -> dict:
        """按宿主实际的 actor 键读取状态：子代理事件用 agent，根 actor 用 session。"""
        key = self.hook_state_module().identity_key(
            "actor", self.host, session, agent or session
        )
        path = self.state / "actors" / (key + ".json")
        self.assertTrue(path.is_file(), path)
        return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# T01：墓碑核对原 epoch/task_identity，同 ID 跨 epoch 复用不得复活旧 child
# ---------------------------------------------------------------------------
class T01TombstoneEpochTests(StateHelpers, HookCase):
    host = "codex"

    def child_actor(self, agent: str = "agent-dev") -> dict:
        return self.actor_state("P", agent)

    def restart(self, dispatch_id: str = "", agent: str = "agent-dev"):
        return self.run_core(self.child_start_payload(agent, dispatch_id=dispatch_id))

    def start_second_epoch(self) -> dict:
        # 父在同一会话显式开始另一个任务：epoch2，旧分派/旧 child 绑定自然退役。
        self.allow(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "P",
                "prompt": "使用 easydev，开始另一个任务",
                "cwd": str(self.repo),
                "turn_id": "turn-2",
            }
        )
        return self.task_for_epoch(2)

    def test_same_dispatch_id_in_new_epoch_does_not_revive_old_child(self) -> None:
        self.activate("P")
        handoff = self.developer_handoff()
        self.allow(self.dispatch_payload("P", handoff, role="developer", tool_id="D1"))
        self.allow(self.child_start_payload("agent-dev", dispatch_id="D1"))
        bound = self.child_actor()
        self.assertEqual(bound["activation_scope"], "task_active")
        origin_identity = bound["task_identity"]
        origin_epoch = int(bound["epoch"])
        before = self.start_second_epoch()
        # epoch2 用同一个分派 ID 重新分派同一交接（幂等重投，内容未变）。
        self.allow(self.dispatch_payload("P", handoff, role="developer", tool_id="D1"))
        # 显式 ID 与无 ID 的 start 交替重投三次：墓碑必须核对原归属，不得复活。
        for attempt in range(3):
            for dispatch_id in ("D1", ""):
                _code, value, stderr = self.restart(dispatch_id)
                self.assertFalse(self.is_denied(value), value)
                self.assertIn("未能匹配", stderr, (attempt, dispatch_id))
                actor = self.child_actor()
                self.assertEqual(actor["activation_scope"], "role_only", (attempt, dispatch_id))
                self.assertNotIn("task_identity", actor)
                self.assertEqual(actor["retired_binding"]["dispatch_id"], "D1")
                self.assertEqual(actor["retired_binding"]["task_identity"], origin_identity)
                self.assertEqual(int(actor["retired_binding"]["epoch"]), origin_epoch)
        # 旧 child 的 pre→真实文件改动→success post 与完成都不进入 epoch2。
        self.allow(
            self.write_tool("P", "src/app.py", tool_id="cw", agent_id="agent-dev", is_child=True)
        )
        (self.repo / "src" / "app.py").write_text("old child\n", encoding="utf-8")
        self.allow(
            self.write_tool(
                "P",
                "src/app.py",
                tool_id="cw",
                event="post_tool",
                outcome="success",
                agent_id="agent-dev",
                is_child=True,
            )
        )
        self.allow(self.child_done_payload("P", agent="agent-dev", report="**[P1] 证据**\n"))
        after = self.task_for_epoch(2)
        self.assertEqual(after["write_seq"], before["write_seq"])
        self.assertEqual(after["review_checkpoint"], before["review_checkpoint"])
        self.assertFalse(after["has_completed_code_review"])
        self.assertEqual(after.get("last_success") or {}, before.get("last_success") or {})

    def test_new_child_in_current_epoch_still_binds_same_dispatch_id(self) -> None:
        self.activate("P")
        handoff = self.developer_handoff()
        self.allow(self.dispatch_payload("P", handoff, role="developer", tool_id="D1"))
        self.allow(self.child_start_payload("agent-dev", dispatch_id="D1"))
        self.start_second_epoch()
        self.allow(self.dispatch_payload("P", handoff, role="developer", tool_id="D1"))
        # 新 child（新 actor，无墓碑）仍能绑定当前 epoch 的分派：修复不阻断正常关联。
        self.allow(self.child_start_payload("agent-new", dispatch_id="D1"))
        newcomer = self.child_actor("agent-new")
        self.assertEqual(newcomer["activation_scope"], "task_active")
        self.assertEqual(newcomer["dispatch_id"], "D1")
        self.assertEqual(int(newcomer["epoch"]), 2)


# ---------------------------------------------------------------------------
# T02：`> 1`/`>> 2`/`2> 1` 是普通文件目标，只有 `>&1`/`2>&1`/`/dev/null` 不是
# ---------------------------------------------------------------------------
class T02NumericRedirectTests(HookCase):
    host = "pi"

    def setUp(self) -> None:
        super().setUp()
        (self.repo / "1").write_text("USER-SENTINEL\n", encoding="utf-8")
        (self.repo / "2").write_text("USER-SENTINEL\n", encoding="utf-8")

    def test_numeric_filenames_are_file_targets_for_developer(self) -> None:
        # 拒绝必须来自“缺手工基线”而不是别的副作用：显式打开手工核对路径。
        self.use_manual_baseline()
        self.activate("s1")
        for index, (command, target) in enumerate(
            (("printf x > 1", "baseline/1"), ("printf x >> 2", "baseline/2"), ("printf x 2> 1", "baseline/1"))
        ):
            with self.subTest(command=command):
                reason = self.deny(self.shell("s1", command, tool_id="num-%d" % index))
                # 拒绝必须点名被写的数字文件名，而不是“其它写目标”的副作用。
                self.assertIn(target, reason)
        self.assertEqual((self.repo / "1").read_text(encoding="utf-8"), "USER-SENTINEL\n")
        self.assertEqual((self.repo / "2").read_text(encoding="utf-8"), "USER-SENTINEL\n")

    def test_numeric_filenames_are_denied_for_readonly_reviewer(self) -> None:
        self.activate("rev", self.reviewer_handoff())
        for index, command in enumerate(("printf x > 1", "printf x >> 2", "printf x 2> 1")):
            with self.subTest(command=command):
                reason = self.deny(self.shell("rev", command, tool_id="num-rev-%d" % index))
                self.assertIn("只读", reason)
        self.assertEqual((self.repo / "1").read_text(encoding="utf-8"), "USER-SENTINEL\n")
        self.assertEqual((self.repo / "2").read_text(encoding="utf-8"), "USER-SENTINEL\n")

    def test_fd_duplication_and_dev_null_are_not_file_writes(self) -> None:
        self.activate("rev", self.reviewer_handoff())
        for index, command in enumerate(
            ("printf x >&1", "printf x 2>&1", "printf x > /dev/null", "printf x >&2")
        ):
            with self.subTest(command=command):
                self.allow(self.shell("rev", command, tool_id="fd-%d" % index))
        # 已提交且干净的普通文件仍可写：数字文件名不是一律拒绝。
        self.git("add", "1")
        self.git("commit", "-qm", "numeric-name")
        self.activate("dev")
        self.allow(self.shell("dev", "printf x > 1", tool_id="num-clean"))


# ---------------------------------------------------------------------------
# T03：`cd . > file` 同段重定向按执行 cd 之前的 cwd 解析
# ---------------------------------------------------------------------------
class T03CdRedirectTests(HookCase):
    host = "pi"

    def setUp(self) -> None:
        super().setUp()
        (self.repo / "app.py").write_text("REPO-USER\n", encoding="utf-8")
        (self.repo / "src" / "app.py").write_text("SRC-USER\n", encoding="utf-8")

    def test_cd_same_segment_redirect_uses_pre_cd_cwd(self) -> None:
        self.use_manual_baseline()
        self.activate("s1")
        for index, command in enumerate(("cd . > app.py", "cd src > app.py", "cd src > ./app.py")):
            with self.subTest(command=command):
                reason = self.deny(self.shell("s1", command, tool_id="cd-red-%d" % index))
                # 目标按执行 cd 之前的 cwd 解析：是仓库根的 app.py，不是 src/app.py。
                self.assertIn("baseline/app.py", reason)
        # 重定向在 cd 之前打开：`cd src > app.py` 落在仓库根，不是 src/app.py。
        self.assertEqual((self.repo / "app.py").read_text(encoding="utf-8"), "REPO-USER\n")
        self.assertEqual((self.repo / "src" / "app.py").read_text(encoding="utf-8"), "SRC-USER\n")

    def test_cd_same_segment_input_redirect_is_not_a_write(self) -> None:
        self.activate("rev", self.reviewer_handoff())
        self.allow(self.shell("rev", "cd src < app.py", tool_id="cd-in"))
        reason = self.deny(self.shell("rev", "cd src > out.txt", tool_id="cd-rev"))
        self.assertIn("只读", reason)
        self.assertFalse((self.repo / "out.txt").exists())


# ---------------------------------------------------------------------------
# T05：mv 的每种目的语法都附加被移走的源（源删除受基线保护）
# ---------------------------------------------------------------------------
class T05MvSourceDeletionTests(HookCase):
    """macOS 的 mv 不支持 GNU `-t/--target-directory`，本用例只核对 Hook 判定与

    文件字节（不执行原命令）；GNU 平台上的真实移动对照未在本机运行。
    """

    host = "pi"

    def setUp(self) -> None:
        super().setUp()
        # 任务根由机器上下文提供：清空安装上下文来源，避免抢先命中默认值。
        self.write_context(task_root="")
        self.task_root = self.make_task_root()
        (self.repo / "dest").mkdir()
        (self.repo / "src" / "app.py").write_text("USER SOURCE\n", encoding="utf-8")

    def activate_with_task_root(self) -> None:
        self.activate("s1", machine_block(active=True, role="main", task_root=str(self.task_root)))

    def test_dirty_source_is_protected_for_every_destination_form(self) -> None:
        self.activate_with_task_root()
        commands = (
            "mv -t dest src/app.py",
            "mv --target-directory dest src/app.py",
            "mv -T src/app.py dest/app.py",
        )
        for index, command in enumerate(commands):
            with self.subTest(command=command):
                reason = self.deny(self.shell("s1", command, tool_id="mv-%d" % index))
                # 被移走的源同样是删除副作用：拒绝必须点名源路径。
                self.assertIn("baseline/src/app.py", reason)
                self.assertEqual(
                    (self.repo / "src" / "app.py").read_text(encoding="utf-8"), "USER SOURCE\n"
                )
                self.assertFalse((self.repo / "dest" / "app.py").exists())

    def test_source_baseline_allows_and_existing_destination_is_denied(self) -> None:
        self.activate_with_task_root()
        self.save_baseline(self.task_root, "src/app.py", "USER SOURCE\n")
        self.allow(self.shell("s1", "mv -t dest src/app.py", tool_id="mv-ok"))
        self.assertFalse((self.repo / "dest" / "app.py").exists())
        # 目的已存在且内容不可重建：源基线合规也仍然拒绝。
        (self.repo / "dest" / "app.py").write_text("DEST USER\n", encoding="utf-8")
        reason = self.deny(self.shell("s1", "mv --target-directory dest src/app.py", tool_id="mv-dest"))
        self.assertIn("baseline/dest/app.py", reason)
        self.assertEqual((self.repo / "dest" / "app.py").read_text(encoding="utf-8"), "DEST USER\n")


# ---------------------------------------------------------------------------
# T08：历史 SKILL 读取只保持激活，普通跟进不得重算角色/只读
# ---------------------------------------------------------------------------
class TranscriptActivationMixin(object):
    skill_reads: list = []

    def input_with_transcript(self, session: str, path: str) -> dict:
        payload = self.input_payload(session, "")
        if self.host in {"codex", "dsh"}:
            payload["transcript_path"] = path
        else:
            payload["transcriptPath"] = path
        return payload

    def write_transcript(self, session: str, texts, name: str) -> str:
        entries = self.meta_entries(session)
        entries.extend(self.skill_read_entries())
        for text in texts:
            entries.extend(self.user_entries(text))
        path = self.root / name
        path.write_text(
            "\n".join(json.dumps(entry, ensure_ascii=False) for entry in entries) + "\n",
            encoding="utf-8",
        )
        return str(path)


class CodexTranscriptMixin(TranscriptActivationMixin):
    def meta_entries(self, session: str) -> list:
        return [{"type": "session_meta", "payload": {"id": session, "cwd": str(self.repo)}}]

    def skill_read_entries(self) -> list:
        return [
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "read_file",
                    "call_id": "c1",
                    "arguments": json.dumps({"path": str(self.skill / "SKILL.md")}),
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "c1",
                    "output": "skill",
                    "is_error": False,
                },
            },
        ]

    def user_entries(self, text: str) -> list:
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


class AntigravityTranscriptMixin(TranscriptActivationMixin):
    def meta_entries(self, session: str) -> list:
        # Antigravity 没有原生 session_meta：由核心披露“缺身份”降级。
        return []

    def skill_read_entries(self) -> list:
        return [
            {
                "PLANNER_RESPONSE": {
                    "tool_calls": [
                        {
                            "name": "read_file",
                            "args": {"Path": str(self.skill / "SKILL.md")},
                            "id": "c1",
                        }
                    ]
                }
            },
            {"type": "TOOL_RESULT", "tool_results": [{"id": "c1", "status": "success"}]},
        ]

    def user_entries(self, text: str) -> list:
        return [{"role": "user", "message": text}]


class T08HistoricalSkillReadTests(object):
    READONLY = "使用 $easydev review 只读审查这个改动，不要修改代码"
    FOLLOW_UP = "继续审查这个改动"

    def test_readonly_survives_plain_follow_up_and_explicit_change_clears_it(self) -> None:
        transcript = self.write_transcript("s8", [self.READONLY], "t8.jsonl")
        self.run_core(self.input_with_transcript("s8", transcript))
        reason = self.deny(self.write_tool("s8", "src/app.py", tool_id="t8-w1"))
        self.assertIn("只读", reason)
        # 普通跟进：transcript 里另有成功 SKILL 读取，也不能把只读重算成可写 main。
        transcript = self.write_transcript("s8", [self.READONLY, self.FOLLOW_UP], "t8b.jsonl")
        _code, value, stderr = self.run_core(self.input_with_transcript("s8", transcript))
        self.assertFalse(self.is_denied(value), value)
        reason = self.deny(self.write_tool("s8", "src/app.py", tool_id="t8-w2"))
        self.assertIn("只读", reason)
        reason = self.deny(self.shell("s8", "printf x > src/app.py", tool_id="t8-sh2"))
        self.assertIn("只读", reason)
        self.assertEqual((self.repo / "src" / "app.py").read_text(encoding="utf-8"), "value = 1\n")
        # 对照：真实用户显式改角色（可写任务）后不再只读。
        transcript = self.write_transcript(
            "s8", [self.READONLY, self.FOLLOW_UP, "使用 $easydev，实现这个功能改动"], "t8c.jsonl"
        )
        self.run_core(self.input_with_transcript("s8", transcript))
        self.allow(self.write_tool("s8", "src/app.py", tool_id="t8-w3"))

    def test_repeated_prompt_turn_without_input_id_is_deduped(self) -> None:
        # 自带 prompt 的宿主：transcript 已记录同一条原始 user 文本时按记录键去重，
        # 没有 input ID 也不会把同一回合重复计入提醒 generation。
        self.activate("s1")
        follow_up = "继续审查这个改动"
        transcript = self.write_transcript("s1", [follow_up], "t8-prompt-dup.jsonl")
        payload = self.input_payload("s1", follow_up)
        payload.pop("turn_id", None)
        payload.pop("turnId", None)
        if self.host in {"codex", "dsh"}:
            payload["transcript_path"] = transcript
        else:
            payload["transcriptPath"] = transcript
        for _attempt in range(2):
            _code, value, stderr = self.run_core(payload)
            self.assertFalse(self.is_denied(value), value)
        task = self.task_for_epoch(1)
        self.assertEqual(int(task.get("user_generation") or 0), 2)

    def test_historical_skill_read_still_activates_but_keeps_role(self) -> None:
        # 只读由 transcript 里的显式调用建立；重复投递同一记录只推进 generation。
        transcript = self.write_transcript("s8", [self.READONLY], "t8-dup.jsonl")
        self.run_core(self.input_with_transcript("s8", transcript))
        actor = self.actor_state("s8")
        generation = int(actor.get("user_generation") or 0)
        self.assertTrue(actor.get("readonly"))
        _code, value, stderr = self.run_core(self.input_with_transcript("s8", transcript))
        self.assertFalse(self.is_denied(value), value)
        actor = self.actor_state("s8")
        self.assertTrue(actor.get("readonly"))
        self.assertEqual(int(actor.get("user_generation") or 0), generation)


class T08CodexTests(CodexTranscriptMixin, T08HistoricalSkillReadTests, StateHelpers, HookCase):
    host = "codex"


class T08AntigravityTests(
    AntigravityTranscriptMixin, T08HistoricalSkillReadTests, StateHelpers, HookCase
):
    host = "antigravity"


# ---------------------------------------------------------------------------
# T07：session_meta 身份核验先于“用户指令”与“SKILL 读取”两条激活路径
# ---------------------------------------------------------------------------
class T07TranscriptIdentityTests(CodexTranscriptMixin, StateHelpers, HookCase):
    host = "codex"

    def test_other_session_user_invocation_does_not_activate(self) -> None:
        transcript = self.write_transcript(
            "OTHER", ["使用 $easydev，实现这个改动"], "t7-other.jsonl"
        )
        self.run_core(self.input_with_transcript("t7", transcript))
        self.allow(self.shell("t7", "git reset --hard", tool_id="t7-other-git"))
        self.assertEqual(root_states(self.state), [])

    def test_other_session_machine_context_does_not_deactivate(self) -> None:
        self.activate("t7")
        before = self.actor_state("t7")
        transcript = self.write_transcript(
            "OTHER", [machine_block(active=False)], "t7-stop.jsonl"
        )
        _code, value, stderr = self.run_core(self.input_with_transcript("t7", transcript))
        self.assertFalse(self.is_denied(value), value)
        self.assertIn("身份与当前会话不一致", stderr)
        self.deny(self.shell("t7", "git reset --hard", tool_id="t7-still"))
        after = self.actor_state("t7")
        self.assertEqual(after["activation_scope"], "task_active")
        self.assertEqual(int(after.get("epoch") or 0), int(before.get("epoch") or 0))
        self.assertEqual(
            int(after.get("user_generation") or 0), int(before.get("user_generation") or 0)
        )

    def test_child_actor_identity_does_not_activate_root_via_user_text(self) -> None:
        transcript = self.write_transcript("t7c", ["使用 $easydev，实现改动"], "t7-child.jsonl")
        entries = [json.loads(line) for line in Path(transcript).read_text().splitlines()]
        entries[0]["payload"]["agent_id"] = "child-1"
        Path(transcript).write_text(
            "\n".join(json.dumps(entry) for entry in entries) + "\n", encoding="utf-8"
        )
        self.run_core(self.input_with_transcript("t7c", transcript))
        self.allow(self.shell("t7c", "git reset --hard", tool_id="t7-child-git"))

    def test_same_session_user_text_activates(self) -> None:
        transcript = self.write_transcript("t7d", ["使用 $easydev，实现这个改动"], "t7-same.jsonl")
        self.run_core(self.input_with_transcript("t7d", transcript))
        self.deny(self.shell("t7d", "git reset --hard", tool_id="t7-same-git"))


# ---------------------------------------------------------------------------
# T09：transcript 原始 user 里的机器上下文统一解析（含 active=false）
# ---------------------------------------------------------------------------
class T09TranscriptMachineContextTests(object):
    def test_active_false_from_transcript_deactivates_root_and_child(self) -> None:
        handoff = self.developer_handoff()
        self.activate("P")
        self.allow(self.dispatch_payload("P", handoff, role="developer", tool_id="D1"))
        self.allow(self.child_start_payload("agent-dev", dispatch_id="D1"))
        before = self.task_for_epoch(1)
        transcript = self.write_transcript("P", [machine_block(active=False)], "t9-stop.jsonl")
        _code, value, stderr = self.run_core(self.input_with_transcript("P", transcript))
        self.assertFalse(self.is_denied(value), value)
        roots = root_states(self.state)
        self.assertTrue(roots)
        self.assertFalse(roots[0].get("active"))
        actor = self.actor_state("P")
        self.assertEqual(actor["activation_scope"], "inactive")
        # 停用后普通写与危险 Git 都放行（新进程仍读到停用状态）。
        self.allow(self.write_tool("P", "src/app.py", tool_id="t9-w"))
        self.allow(self.shell("P", "git reset --hard", tool_id="t9-git"))
        # 旧 child 不被归并：写与完成都不进入父任务写序列。
        self.allow(
            self.write_tool("P", "src/app.py", tool_id="t9-cw", agent_id="agent-dev", is_child=True)
        )
        (self.repo / "src" / "app.py").write_text("child after stop\n", encoding="utf-8")
        self.allow(
            self.write_tool(
                "P",
                "src/app.py",
                tool_id="t9-cw",
                event="post_tool",
                outcome="success",
                agent_id="agent-dev",
                is_child=True,
            )
        )
        self.allow(self.child_done_payload("P", agent="agent-dev", report="**[P2] 证据**\n"))
        after = self.task_for_epoch(1)
        self.assertEqual(after["write_seq"], before["write_seq"])
        self.assertEqual(after["review_checkpoint"], before["review_checkpoint"])

    def test_active_true_task_root_and_role_overrides_persist_across_processes(self) -> None:
        task_root = self.make_task_root()
        block = machine_block(
            active=True,
            role="main",
            task_root=str(task_root),
            role_overrides={"reviewer": {"model": "not-a-full-id"}},
        )
        transcript = self.write_transcript("P", [block], "t9-active.jsonl")
        self.run_core(self.input_with_transcript("P", transcript))
        roots = root_states(self.state)
        self.assertTrue(roots)
        stored = roots[0].get("machine") or {}
        self.assertEqual(stored.get("task_root"), str(task_root))
        self.assertEqual(stored.get("role_overrides"), {"reviewer": {"model": "not-a-full-id"}})
        # 下一个进程仍用 transcript 给出的 task_root：脏目标走“缺基线”而不是“缺任务目录”。
        (self.repo / "src" / "app.py").write_text("USER DIRTY\n", encoding="utf-8")
        reason = self.deny(self.write_tool("P", "src/app.py", tool_id="t9-dirty"))
        self.assertIn("缺少合规首次基线", reason)
        # 模型覆盖同样跨进程生效：分派预检读到同一份非法覆盖。
        reason = self.deny(self.dispatch_payload("P", self.reviewer_handoff(), tool_id="t9-d1"))
        self.assertIn("模型", reason)


class T09CodexTests(
    CodexTranscriptMixin, T09TranscriptMachineContextTests, StateHelpers, HookCase
):
    host = "codex"


class T09AntigravityTests(
    AntigravityTranscriptMixin, T09TranscriptMachineContextTests, StateHelpers, HookCase
):
    host = "antigravity"


# ---------------------------------------------------------------------------
# T11：一次 input 的 transcript 取证在锁外归一，锁内不再 I/O
# ---------------------------------------------------------------------------
T11_LAUNCHER = '''#!/usr/bin/env python3
"""受控慢 I/O：包装 transcript 读取与 roots 状态读取，记录并注入固定延迟。"""

import os
import runpy
import sys
import time

sys.path.insert(0, %(core_dir)r)
import state
import transcripts

_real_turn = transcripts.latest_user_turn
_reads = %(reads)r
_delay = %(delay)r
_marker = %(marker)r
_marker_delay = %(marker_delay)r
_real_read = state.read_state
_first_roots = [True]


def _slow_turn(*args, **kwargs):
    with open(_reads, "a", encoding="utf-8") as handle:
        handle.write("read\\n")
    time.sleep(_delay)
    return _real_turn(*args, **kwargs)


def _slow_read(directory, name):
    value = _real_read(directory, name)
    # 第一次读 roots：先拿到值、告知测试“取证已开始”、再慢慢返回这份陈旧值。
    if _first_roots[0] and os.path.basename(directory) == "roots":
        _first_roots[0] = False
        with open(_marker, "w", encoding="utf-8") as handle:
            handle.write("read\\n")
        time.sleep(_marker_delay)
    return value


transcripts.latest_user_turn = _slow_turn
state.read_state = _slow_read
sys.argv = [%(core_path)r] + sys.argv[1:]
runpy.run_path(%(core_path)r, run_name="__main__")
'''


class T11InputEvidenceLockScopeTests(CodexTranscriptMixin, StateHelpers, HookCase):
    host = "codex"
    delay = 0.4
    evidence_delay = 0.3

    def setUp(self) -> None:
        super().setUp()
        # 并发写必须因“缺手工基线”被拒绝，而不是因锁忙降级放行。
        self.use_manual_baseline()
        self.activate("s1")
        self.touch_dirty("src/app.py", "USER CONTENT\n")
        self.reads = self.root / "transcript-reads"
        self.reads.write_text("", encoding="utf-8")
        self.evidence_started = self.root / "evidence-started"
        launcher = self.root / "slow_core.py"
        launcher.write_text(
            T11_LAUNCHER
            % {
                "core_dir": str(HOOKS_ROOT / "hooks"),
                "core_path": str(HOOKS_ROOT / "hooks" / "core.py"),
                "reads": str(self.reads),
                "delay": self.delay,
                "marker": str(self.evidence_started),
                "marker_delay": self.evidence_delay,
            },
            encoding="utf-8",
        )
        self.command = [
            sys.executable,
            str(launcher),
            "--host",
            "codex",
            "--context",
            str(self.context_path),
        ]
        self.task_lock = self.state / "locks" / ("task-" + self.task_lock_key() + ".lock")
        self.assertTrue(self.task_lock.is_file(), self.task_lock)

    def task_lock_key(self) -> str:
        hook_state = self.hook_state_module()
        for item in actor_states(self.state):
            identity = item.get("task_identity")
            if identity:
                return hook_state.identity_key("task", "codex", "s1", identity)
        self.fail("active actor without task identity")

    def start_input(self, transcript: str) -> subprocess.Popen:
        process = subprocess.Popen(
            self.command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=self.environment(),
        )
        process.stdin.write(json.dumps(self.input_with_transcript("s1", transcript)))
        process.stdin.close()
        return process

    def finish(self, process: subprocess.Popen):
        try:
            stdout = process.stdout.read()
            stderr = process.stderr.read()
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
        return stdout, stderr

    def longest_lock_hold(self, process: subprocess.Popen) -> float:
        longest = 0.0
        streak = 0.0
        while process.poll() is None:
            handle = open(self.task_lock, "r+b")
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError:
                acquired = False
            finally:
                handle.close()
            streak = 0.0 if acquired else streak + 0.01
            longest = max(longest, streak)
            time.sleep(0.005)
        return longest

    def test_slow_transcript_read_stays_outside_the_task_lock(self) -> None:
        transcript = self.write_transcript("s1", ["使用 $easydev 继续这个任务"], "t11.jsonl")
        started = time.monotonic()
        process = self.start_input(transcript)
        longest = self.longest_lock_hold(process)
        elapsed = time.monotonic() - started
        stdout, stderr = self.finish(process)
        self.assertEqual(process.returncode, 0, stderr)
        self.assertEqual(json.loads(stdout), {}, stderr)
        self.assertNotIn("状态锁不可用", stderr)
        # 慢读取真的发生过（耗时与读取次数都是外部可观察证据）。
        self.assertGreater(elapsed, self.delay)
        self.assertEqual(self.reads.read_text(encoding="utf-8").count("read"), 1)
        self.assertLess(longest, 0.15, "transcript 读取期间仍占用共享 task 锁")

    def test_concurrent_write_during_slow_read_is_not_lock_starved(self) -> None:
        transcript = self.write_transcript("s1", ["使用 $easydev 继续这个任务"], "t11b.jsonl")
        process = self.start_input(transcript)
        time.sleep(0.1)
        _code, concurrent, concurrent_stderr = self.run_core(
            self.write_tool("s1", "src/app.py", tool_id="t11-w")
        )
        # 同一个慢读取窗口里的 Stop 事件也不得因锁忙降级。
        _code, stop_value, stop_stderr = self.run_core(self.settle_payload("s1"))
        stdout, stderr = self.finish(process)
        self.assertEqual(process.returncode, 0, stderr)
        self.assertEqual(json.loads(stdout), {}, stderr)
        # 并发脏写仍被正常核对基线，而不是因锁忙降级放行。
        self.assertTrue(self.is_denied(concurrent), concurrent)
        self.assertNotIn("状态锁不可用", concurrent_stderr)
        self.assertNotIn("状态锁不可用", stop_stderr)
        self.assertEqual((self.repo / "src" / "app.py").read_text(encoding="utf-8"), "USER CONTENT\n")

    def wait_for_evidence(self) -> None:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.evidence_started.exists():
                return
            time.sleep(0.02)
        self.fail("慢读取进程未进入取证阶段")

    def test_changed_signature_discards_evidence_and_is_disclosed(self) -> None:
        transcript = self.write_transcript(
            "s1", ["使用 $easydev 只读 review 这个改动"], "t11c.jsonl"
        )
        process = self.start_input(transcript)
        # 慢读取已拿到陈旧 epoch 且尚未提交：此时父会话显式开始另一个任务。
        self.wait_for_evidence()
        self.allow(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "s1",
                "prompt": "使用 easydev，开始另一个任务",
                "cwd": str(self.repo),
                "turn_id": "turn-2",
            }
        )
        stdout, stderr = self.finish(process)
        self.assertEqual(process.returncode, 0, stderr)
        self.assertEqual(json.loads(stdout), {}, stderr)
        self.assertIn("输入取证的签名在锁内复核时已变化", stderr)
        self.task_for_epoch(2)
        actor = self.actor_state("s1")
        self.assertFalse(actor.get("readonly"))
        self.assertEqual(int(actor.get("epoch") or 0), 2)


# ---------------------------------------------------------------------------
# T10：安装 apply/uninstall 在备份/写入前验证目标与父链
# ---------------------------------------------------------------------------
class T10InstallBackupOrderTests(HookCase):
    host = "codex"
    INSTALL = HOOKS_ROOT / "hooks" / "install.py"

    def generate(self) -> dict:
        output = self.root / "generated"
        completed = subprocess.run(
            [
                sys.executable,
                str(self.INSTALL),
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
            env=self.environment(),
            timeout=60,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

    def run_install(self, args) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(self.INSTALL), *args],
            capture_output=True,
            text=True,
            env=self.environment(),
            timeout=60,
        )

    def test_symlinked_parent_is_rejected_without_leaving_backup(self) -> None:
        generated = self.generate()
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "hooks.json").write_text('{"hooks": {}}', encoding="utf-8")
        link = self.root / "link"
        link.symlink_to(outside)
        before = sorted(path.name for path in outside.iterdir())
        result = self.run_install(
            ["apply", "--target", str(link / "hooks.json"), "--source", generated["config"]]
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("父目录", result.stderr)
        self.assertEqual(sorted(path.name for path in outside.iterdir()), before)
        self.assertEqual((outside / "hooks.json").read_text(encoding="utf-8"), '{"hooks": {}}')

        sub = outside / "sub"
        sub.mkdir()
        (sub / "hooks.json").write_text('{"hooks": {}}', encoding="utf-8")
        ancestor = self.root / "ancestor"
        ancestor.symlink_to(sub)
        result = self.run_install(
            ["apply", "--target", str(ancestor / "hooks.json"), "--source", generated["config"]]
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(sorted(path.name for path in sub.iterdir()), ["hooks.json"])
        # uninstall 同样在任何备份/写入前拒绝链接目标。
        result = self.run_install(
            [
                "uninstall",
                "--target",
                str(ancestor / "hooks.json"),
                "--manifest",
                generated["manifest"],
            ]
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(sorted(path.name for path in sub.iterdir()), ["hooks.json"])

    def test_non_regular_and_hardlinked_targets_are_rejected(self) -> None:
        generated = self.generate()
        directory = self.root / "dir-target.json"
        directory.mkdir()
        result = self.run_install(
            ["apply", "--target", str(directory), "--source", generated["config"]]
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(list(directory.iterdir()), [])

        hard = self.root / "hard.json"
        hard.write_text('{"hooks": {}}', encoding="utf-8")
        os.link(hard, self.root / "hard-link.json")
        result = self.run_install(
            ["apply", "--target", str(hard), "--source", generated["config"]]
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("硬链", result.stderr)
        self.assertEqual(hard.read_text(encoding="utf-8"), '{"hooks": {}}')
        self.assertEqual(sorted(path.name for path in self.root.iterdir() if path.name.startswith("hard")), ["hard-link.json", "hard.json"])

    def test_existing_file_backup_keeps_original_bytes_and_private_mode(self) -> None:
        generated = self.generate()
        target = self.root / "hooks.json"
        original = json.dumps({"hooks": {"PreToolUse": [{"matcher": ".*", "hooks": [{"type": "command", "command": "other"}]}]}}, indent=3)
        target.write_text(original, encoding="utf-8")
        os.chmod(target, 0o644)
        completed = self.run_install(
            ["apply", "--target", str(target), "--source", generated["config"]]
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        backup = Path(result["backup"])
        self.assertTrue(backup.is_file())
        self.assertEqual(backup.read_text(encoding="utf-8"), original)
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
        self.assertEqual(backup.parent, target.parent)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
