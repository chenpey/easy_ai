"""写序列记账：没有写目标不等于写目标不完整，非写工具不参与配对。

误报都来自把“没有写目标”当成“写目标不完整”：非写工具的成功 post 也去配对、
纯读 shell 建了空目标的 pending、同一文件两次写让目标集合看起来不一致。反向的
错误同样要钉住：执行型 stdin 与写 pre 之后的读/失败 post 仍必须披露缺口。
这里只钉可观察结果：stderr 文案、pending 内容、写序列是否推进。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List

from conftest import subtests
from support import HookCase
from test_heredoc_warnings import engine_warnings
from test_r5_fixes import task_states


POST_INCOMPLETE = "post 写入目标不完整，写序列记覆盖缺口"
POST_UNPAIRED = "post 未找到与该 actor/epoch/tool ID 配对的 pre 记录，未建立成功豁免"
POST_FAILED = "post 缺少可信成功结果，未计入成功写入"
POST_UNSAFE = "post 目标指纹不可信，未建立成功豁免"


class WriteAccountingCase(HookCase):
    host = "pi"

    def task_state(self) -> Dict[str, Any]:
        states = task_states(self.state)
        assert len(states) == 1, states
        return states[0]

    def read_post(self, session: str, outcome: str, tool_id: str) -> str:
        payload = self.tool_payload(
            session,
            "read",
            {"path": "src/app.py"},
            tool_id=tool_id,
            event="post_tool",
            outcome=outcome,
        )
        _code, value, stderr = self.run_core(payload)
        assert not self.is_denied(value), value
        return stderr

    def write_code_twice(self, session: str, tool_id: str) -> List[str]:
        """同一条命令两次写同一文件：pre/post 各一次，返回两次的 stderr。"""
        command = "printf a > src/app.py; printf b >> src/app.py"
        _code, value, pre_stderr = self.run_core(self.shell(session, command, tool_id=tool_id))
        assert not self.is_denied(value), value
        (self.repo / "src" / "app.py").write_text("ab\n", encoding="utf-8")
        _code, value, post_stderr = self.run_core(
            self.shell(session, command, tool_id=tool_id, event="post_tool", outcome="success")
        )
        assert not self.is_denied(value), value
        return [pre_stderr, post_stderr]


class NonWriteToolTests(WriteAccountingCase):
    """非写工具的 post 既不建 pending，也不做配对。"""

    def test_read_tool_post_is_silent(self) -> None:
        self.activate("s1")
        assert self.read_post("s1", "success", "read-ok") == ""
        assert self.read_post("s1", "error", "read-fail") == ""
        task = self.task_state()
        assert (task.get("pending_writes") or {}) == {}
        assert int(task.get("write_seq") or 0) == 0


class ReadOnlyShellTests(WriteAccountingCase):
    """纯读 shell 没有写目标可记账：pre 不建 pending，post 不配对。"""

    def test_readonly_shell_pre_post_are_silent_and_not_pending(self) -> None:
        self.activate("s1")
        _code, value, pre_stderr = self.run_core(self.shell("s1", "git status", tool_id="status-1"))
        assert not self.is_denied(value), value
        assert pre_stderr == ""
        assert (self.task_state().get("pending_writes") or {}) == {}
        _code, value, post_stderr = self.run_core(
            self.shell("s1", "git status", tool_id="status-1", event="post_tool", outcome="success")
        )
        assert not self.is_denied(value), value
        assert post_stderr == ""
        assert int(self.task_state().get("write_seq") or 0) == 0
        # 宿主只投递 post（没有 pre）时同样不配对、不告警。
        _code, value, post_only_stderr = self.run_core(
            self.shell("s1", "rg TODO src/", tool_id="status-2", event="post_tool", outcome="success")
        )
        assert not self.is_denied(value), value
        assert post_only_stderr == ""
        assert int(self.task_state().get("write_seq") or 0) == 0


class MetadataOnlyShellTests(WriteAccountingCase):
    """只改 .git/索引/远端的 git 命令没有工作区目标：静默且不建 pending。"""

    METADATA_COMMANDS = (
        "git add .",
        "git commit -m test",
        "git fetch",
        "git push",
        "git config user.name tester",
        "git branch feature-x",
    )

    def test_git_metadata_writes_are_silent_and_not_pending(self) -> None:
        self.activate("s1")
        for index, command in enumerate(self.METADATA_COMMANDS):
            with subtests.test(command=command):
                tool_id = "meta-%d" % index
                _code, value, pre_stderr = self.run_core(
                    self.shell("s1", command, tool_id=tool_id)
                )
                assert not self.is_denied(value), value
                assert pre_stderr == ""
                assert (self.task_state().get("pending_writes") or {}) == {}
                _code, value, post_stderr = self.run_core(
                    self.shell(
                        "s1", command, tool_id=tool_id, event="post_tool", outcome="success"
                    )
                )
                assert not self.is_denied(value), value
                assert post_stderr == ""
        task = self.task_state()
        assert int(task.get("write_seq") or 0) == 0
        assert (task.get("pending_writes") or {}) == {}

    def test_metadata_write_keeps_business_target_in_same_call(self) -> None:
        """同一次调用里既 git add 又改工作区文件：业务目标仍完整记账。"""
        self.activate("s1")
        command = "git add src/app.py; printf x > src/app.py"
        _code, value, pre_stderr = self.run_core(self.shell("s1", command, tool_id="meta-mixed"))
        assert not self.is_denied(value), value
        assert pre_stderr == ""
        entries = list((self.task_state().get("pending_writes") or {}).values())
        assert len(entries) == 1, entries
        assert entries[0]["complete"], entries[0]
        assert [key.rsplit("::", 1)[-1] for key in entries[0]["targets"]] == ["src/app.py"]


class DuplicateTargetTests(WriteAccountingCase):
    """同一文件多次写只算一个目标：配对一致，写序列只推进一次。"""

    def test_same_file_written_twice_advances_sequence_once(self) -> None:
        self.activate("s1")
        pre_stderr, post_stderr = self.write_code_twice("s1", "dup-1")
        assert pre_stderr == ""
        assert post_stderr == ""
        task = self.task_state()
        assert int(task.get("write_seq") or 0) == 1
        assert int(task.get("last_code_write_seq") or 0) == 1
        last_success = task.get("last_success") or {}
        assert sorted(key.rsplit("::", 1)[-1] for key in last_success) == ["src/app.py"], last_success
        assert (task.get("pending_writes") or {}) == {}


class GapDisclosureTests(WriteAccountingCase):
    """真正的目标集合缺口仍按原文披露：放行不等于静默。"""

    def test_genuine_gaps_still_disclosed(self) -> None:
        self.activate("s1")
        opaque = "cat > out.txt <<'EOF'\nhello\n"
        # (a) 宿主只投递 post：没有 pre 配对记录，仍是记账缺口。
        _code, value, stderr = self.run_core(
            self.shell("s1", opaque, tool_id="gap-post-only", event="post_tool", outcome="success")
        )
        assert not self.is_denied(value), value
        assert POST_UNPAIRED in engine_warnings(stderr)
        # (b) pre 目标完整、post 换成 opaque 参数：目标集合无法完整建立，不建立成功豁免。
        _code, value, pre_stderr = self.run_core(
            self.shell("s1", "printf x > src/app.py", tool_id="gap-swap")
        )
        assert not self.is_denied(value), value
        assert pre_stderr == ""
        _code, value, post_stderr = self.run_core(
            self.shell("s1", opaque, tool_id="gap-swap", event="post_tool", outcome="success")
        )
        assert not self.is_denied(value), value
        assert POST_INCOMPLETE in engine_warnings(post_stderr)
        task = self.task_state()
        assert int(task.get("write_seq") or 0) == 0
        assert (task.get("pending_writes") or {}) == {}


class RoleOnlyWriteSequenceTests(WriteAccountingCase):
    """无父关联的 reviewer 只有 role_only：不借用父任务的写序列，也不刷记账告警。"""

    def test_role_only_reviewer_readonly_shell_is_silent(self) -> None:
        self.activate("rev", self.reviewer_handoff())
        _code, value, pre_stderr = self.run_core(self.shell("rev", "git status", tool_id="rev-1"))
        assert not self.is_denied(value), value
        assert pre_stderr == ""
        _code, value, post_stderr = self.run_core(
            self.shell("rev", "git status", tool_id="rev-1", event="post_tool", outcome="success")
        )
        assert not self.is_denied(value), value
        assert post_stderr == ""
        # reviewer 只读的写拒绝不变，且仍是一行 deny。
        self.deny(self.write_tool("rev", "src/app.py", tool_id="rev-write"))


class SymlinkMergeTests(WriteAccountingCase):
    """同一命令两次写同一目标：已观察到的软链必须仍被拒绝。"""

    COMMAND = "printf a > src/app.py; printf b >> src/app.py"

    def link_app_to_guide(self) -> Path:
        guide = self.repo / "docs" / "guide.md"
        app = self.repo / "src" / "app.py"
        app.unlink()
        os.symlink(guide, str(app))
        return guide

    def assert_denied_and_guide_untouched(self, reason: str, guide: Path, before: str) -> None:
        assert "符号链接" in reason
        assert guide.read_text(encoding="utf-8") == before
        assert int(self.task_state().get("write_seq") or 0) == 0

    def test_symlinked_duplicate_target_is_denied(self) -> None:
        self.activate("s1")
        guide = self.link_app_to_guide()
        before = guide.read_text(encoding="utf-8")
        reason = self.deny(self.shell("s1", self.COMMAND, tool_id="dup-link"))
        self.assert_denied_and_guide_untouched(reason, guide, before)


class PostFingerprintTests(WriteAccountingCase):
    """post 阶段的问题指纹不得写成成功指纹：不推进写序列、不建立成功豁免。

    `deadline`（共享预算耗尽）与非普通文件/超限/不可读都不是可信指纹。它们既不
    能当成「写入后状态」，也不能留下一个成功豁免让下一次 pre 误报外部修改，或让
    dirty 目标的首次快照被跳过。
    """

    COMMAND = "printf a > src/app.py"

    def app(self) -> Path:
        return self.repo / "src" / "app.py"

    def test_post_nonregular_target_is_not_recorded_as_success(self) -> None:
        self.activate("s1")
        self.allow(self.shell("s1", self.COMMAND, tool_id="nr-post"))
        app = self.app()
        app.unlink()
        app.mkdir()
        _code, value, post_stderr = self.run_core(
            self.shell("s1", self.COMMAND, tool_id="nr-post", event="post_tool", outcome="success")
        )
        assert not self.is_denied(value), value
        assert POST_UNSAFE in engine_warnings(post_stderr)
        task = self.task_state()
        assert int(task.get("write_seq") or 0) == 0
        assert (task.get("last_success") or {}) == {}


class WrappedStdinTests(WriteAccountingCase):
    """包装器/管道下的执行型 stdin 正文同样未被解析：不得当纯读。"""

    def assert_gap_pre_and_post(self, command: str, tool_id: str, notice: bool = True) -> None:
        _code, value, pre_stderr = self.run_core(self.shell("s1", command, tool_id=tool_id))
        assert not self.is_denied(value), value
        assert pre_stderr == ""
        entries = list((self.task_state().get("pending_writes") or {}).values())
        assert len(entries) == 1, entries
        assert entries[0]["targets"] == []
        assert not entries[0]["complete"]
        (self.repo / "src" / "app.py").write_text("value = 2\n", encoding="utf-8")
        _code, value, post_stderr = self.run_core(
            self.shell("s1", command, tool_id=tool_id, event="post_tool", outcome="success")
        )
        assert not self.is_denied(value), value
        warnings = engine_warnings(post_stderr)
        if notice:
            assert POST_INCOMPLETE in warnings
        else:
            # 同一任务里重复的执行型 stdin 缺口只提醒一次：记账仍是不完整。
            assert POST_INCOMPLETE not in warnings
        task = self.task_state()
        assert int(task.get("write_seq") or 0) == 0
        assert (task.get("pending_writes") or {}) == {}

    def test_shell_dash_c_consumer_keeps_gap(self) -> None:
        self.activate("s1")
        self.assert_gap_pre_and_post(
            "bash -c 'python3 -' <<'PY'\nimport writer\nPY", "wrap-c"
        )

    def test_pipeline_consumer_keeps_gap(self) -> None:
        self.activate("s1")
        for index, command in enumerate(
            (
                "cat <<'EOF' | python3 -\nprint(1)\nEOF",
                "cat <<'EOF' | bash -s\nprint(1)\nEOF",
            )
        ):
            with subtests.test(command=command):
                self.assert_gap_pre_and_post(command, "wrap-pipe-%d" % index, notice=index == 0)

    def test_shell_script_file_argument_keeps_gap(self) -> None:
        """壳从脚本文件读脚本时，heredoc 正文只是子进程 stdin：写目标仍不可枚举。"""
        self.activate("s1")
        for index, command in enumerate(
            (
                "bash consumer.sh <<'PY'\nimport writer\nPY",
                "env bash consumer.sh <<'PY'\nimport writer\nPY",
                "sudo bash consumer.sh <<'PY'\nimport writer\nPY",
                # 脚本名之后的 `-s` 是脚本的 $1，不是壳选项。
                "bash consumer.sh -s <<'PY'\nimport writer\nPY",
                # 单个 `-` 是选项终止：后面的 `consumer.sh` 才是脚本。
                "bash - consumer.sh <<'PY'\nimport writer\nPY",
                # `--rcfile` 的取值以 `-` 开头，不能被当选项扫描。
                "bash --rcfile -s consumer.sh <<'PY'\nimport writer\nPY",
            )
        ):
            with subtests.test(command=command.splitlines()[0]):
                self.assert_gap_pre_and_post(command, "script-arg-%d" % index, notice=index == 0)


class InterpreterStdinTests(WriteAccountingCase):
    """执行型 stdin 没有枚举出写目标：不得当纯读静默，也不得误伤已解析的壳正文。"""

    PY_STDIN = (
        "python3 - <<'PY'\n"
        "from pathlib import Path\n"
        'Path("src/app.py").write_text("value = 2\\n")\n'
        "PY"
    )

    def pending_entries(self) -> List[Dict[str, Any]]:
        return list((self.task_state().get("pending_writes") or {}).values())

    def test_interpreter_stdin_write_keeps_gap(self) -> None:
        self.activate("s1")
        _code, value, pre_stderr = self.run_core(
            self.shell("s1", self.PY_STDIN, tool_id="py-stdin")
        )
        assert not self.is_denied(value), value
        # pre 不新增结构告警，但必须留下不完整的写意图。
        assert pre_stderr == ""
        entries = self.pending_entries()
        assert len(entries) == 1, entries
        assert entries[0]["targets"] == []
        assert not entries[0]["complete"]
        # 正文真实改了代码文件，但目标集不可证明：成功 post 也不建立豁免。
        (self.repo / "src" / "app.py").write_text("value = 2\n", encoding="utf-8")
        _code, value, post_stderr = self.run_core(
            self.shell("s1", self.PY_STDIN, tool_id="py-stdin", event="post_tool", outcome="success")
        )
        assert not self.is_denied(value), value
        assert POST_INCOMPLETE in engine_warnings(post_stderr)
        task = self.task_state()
        assert int(task.get("write_seq") or 0) == 0
        assert (task.get("pending_writes") or {}) == {}

    def test_shell_heredoc_script_body_stays_silent(self) -> None:
        self.activate("s1")
        command = "sh <<'EOF'\necho hi\nEOF"
        _code, value, pre_stderr = self.run_core(self.shell("s1", command, tool_id="sh-body"))
        assert not self.is_denied(value), value
        assert pre_stderr == ""
        assert self.pending_entries() == []
        _code, value, post_stderr = self.run_core(
            self.shell("s1", command, tool_id="sh-body", event="post_tool", outcome="success")
        )
        assert not self.is_denied(value), value
        assert post_stderr == ""
        assert int(self.task_state().get("write_seq") or 0) == 0

    def test_herestring_stdin_keeps_gap(self) -> None:
        self.activate("s1")
        command = "sh <<< 'echo hi'"
        _code, value, pre_stderr = self.run_core(self.shell("s1", command, tool_id="hs"))
        assert not self.is_denied(value), value
        assert pre_stderr == ""
        entries = self.pending_entries()
        assert len(entries) == 1, entries
        assert not entries[0]["complete"]
        _code, value, post_stderr = self.run_core(
            self.shell("s1", command, tool_id="hs", event="post_tool", outcome="success")
        )
        assert not self.is_denied(value), value
        assert POST_INCOMPLETE in engine_warnings(post_stderr)
        assert int(self.task_state().get("write_seq") or 0) == 0


class RepeatedGapNoticeTests(WriteAccountingCase):
    """写序列缺口按「任务 + 原因」去重：同一原因重复调用只提醒一次。

    未解析正文的写入仍然不建立成功豁免（记账仍是缺口）；只去掉同一任务里重复的同一句
    提醒，避免 `python3 - <<'PY'`、`cp -R` 这类常见调用每次都刷降级类告警。不同原因
    仍分别披露；换 epoch 会重新提醒一次。
    """

    def gap_post(self, command: str, tool_id: str) -> List[str]:
        self.allow(self.shell("s1", command, tool_id=tool_id))
        (self.repo / "src" / "app.py").write_text("value = 3\n", encoding="utf-8")
        _code, value, post_stderr = self.run_core(
            self.shell("s1", command, tool_id=tool_id, event="post_tool", outcome="success")
        )
        assert not self.is_denied(value), value
        return engine_warnings(post_stderr)

    def test_interpreter_stdin_gap_is_disclosed_once_per_task(self) -> None:
        self.activate("s1")
        for index in (1, 2):
            warnings = self.gap_post("python3 - <<'PY'\nprint(%d)\nPY" % index, "py-%d" % index)
            assert warnings == ([POST_INCOMPLETE] if index == 1 else []), (index, warnings)
        task = self.task_state()
        assert int(task.get("write_seq") or 0) == 0
        assert (task.get("last_success") or {}) == {}

    def test_different_gap_reasons_are_disclosed_separately(self) -> None:
        """不同缺口原因不被合并静默：stdin 未解析与目标集不完整各提醒一次。"""
        self.activate("s1")
        assert self.gap_post("python3 - <<'PY'\nprint(1)\nPY", "reason-py") == [POST_INCOMPLETE]
        assert self.gap_post("echo hi | tee", "reason-tee") == [POST_INCOMPLETE]
        # 同一原因的重复调用不再提醒。
        assert self.gap_post("echo hi | tee", "reason-tee-2") == []

    def test_incomplete_target_gap_is_disclosed_once_per_task(self) -> None:
        """目标集不完整（`tee` 无目标）同任务只提醒一次，换任务重新提醒。"""
        self.activate("s1")
        assert self.gap_post("echo hi | tee", "tee-a") == [POST_INCOMPLETE]
        assert self.gap_post("echo hi | tee", "tee-b") == []
        self.activate("s1", prompt="使用 easydev，换一个新任务")
        assert self.gap_post("echo hi | tee", "tee-c") == [POST_INCOMPLETE]

    def test_new_epoch_reminds_again(self) -> None:
        """换 epoch（新用户回合/新任务）后重新提醒一次，记账仍是不完整。"""
        self.activate("s1")
        assert self.gap_post("python3 - <<'PY'\nprint(1)\nPY", "py-a") == [POST_INCOMPLETE]
        self.activate("s1", prompt="使用 easydev，换一个新任务")
        assert self.gap_post("python3 - <<'PY'\nprint(2)\nPY", "py-b") == [POST_INCOMPLETE]
        for task in task_states(self.state):
            assert int(task.get("write_seq") or 0) == 0
            assert (task.get("last_success") or {}) == {}


class OutsideTargetTests(WriteAccountingCase):
    """仓库外写入不在基线范围内：单独出现静默，与仓库内目标混合时按原因去重。"""

    def test_outside_only_write_is_silent_and_not_pending(self) -> None:
        self.activate("s1")
        command = "printf x > %s" % (self.root / "outside.log")
        _code, value, pre_stderr = self.run_core(self.shell("s1", command, tool_id="out-1"))
        assert not self.is_denied(value), value
        assert pre_stderr == ""
        assert (self.task_state().get("pending_writes") or {}) == {}
        _code, value, post_stderr = self.run_core(
            self.shell("s1", command, tool_id="out-1", event="post_tool", outcome="success")
        )
        assert not self.is_denied(value), value
        assert post_stderr == ""
        assert int(self.task_state().get("write_seq") or 0) == 0

    def test_mixed_outside_target_is_disclosed_once_per_task(self) -> None:
        self.activate("s1")
        command = "printf x > %s; printf y > src/app.py" % (self.root / "outside.log")
        for index in (1, 2):
            with subtests.test(call=index):
                tool_id = "mix-out-%d" % index
                _code, value, pre_stderr = self.run_core(
                    self.shell("s1", command, tool_id=tool_id)
                )
                assert not self.is_denied(value), value
                assert pre_stderr == ""
                entries = list((self.task_state().get("pending_writes") or {}).values())
                assert len(entries) == 1, entries
                assert entries[0]["gap"]
                assert entries[0]["gap_reason"] == "outside"
                (self.repo / "src" / "app.py").write_text("value = 8\n", encoding="utf-8")
                _code, value, post_stderr = self.run_core(
                    self.shell(
                        "s1", command, tool_id=tool_id, event="post_tool", outcome="success"
                    )
                )
                assert not self.is_denied(value), value
                warnings = engine_warnings(post_stderr)
                assert warnings == ([POST_INCOMPLETE] if index == 1 else []), (index, warnings)
        assert int(self.task_state().get("write_seq") or 0) == 0


class CrossToolPostTests(WriteAccountingCase):
    """写 pre 之后换成读/失败的 post：必须消费 pending 并披露，不能静默。"""

    def test_write_pre_then_read_post_is_disclosed(self) -> None:
        self.activate("s1")
        self.allow(self.write_tool("s1", "src/app.py", tool_id="w-read"))
        assert len(self.task_state().get("pending_writes") or {}) == 1
        post = self.tool_payload(
            "s1",
            "read",
            {"path": "src/app.py"},
            tool_id="w-read",
            event="post_tool",
            outcome="success",
        )
        _code, value, stderr = self.run_core(post)
        assert not self.is_denied(value), value
        assert POST_INCOMPLETE in engine_warnings(stderr)
        task = self.task_state()
        assert (task.get("pending_writes") or {}) == {}
        assert int(task.get("write_seq") or 0) == 0

    def test_write_pre_then_failed_readonly_post_is_disclosed(self) -> None:
        self.activate("s1")
        self.allow(self.write_tool("s1", "src/app.py", tool_id="w-fail"))
        _code, value, stderr = self.run_core(
            self.shell("s1", "git status", tool_id="w-fail", event="post_tool", outcome="error")
        )
        assert not self.is_denied(value), value
        assert POST_FAILED in engine_warnings(stderr)
        task = self.task_state()
        assert (task.get("pending_writes") or {}) == {}
        assert int(task.get("write_seq") or 0) == 0

    def test_write_pre_then_failed_write_post_is_disclosed(self) -> None:
        self.activate("s1")
        self.allow(self.write_tool("s1", "src/app.py", tool_id="w-fail-2"))
        _code, value, stderr = self.run_core(
            self.write_tool(
                "s1", "src/app.py", tool_id="w-fail-2", event="post_tool", outcome="error"
            )
        )
        assert not self.is_denied(value), value
        assert POST_FAILED in engine_warnings(stderr)
        assert (self.task_state().get("pending_writes") or {}) == {}
