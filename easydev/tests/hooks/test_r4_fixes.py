"""r3 施工审查 S01–S19 的修复行为（真实 core CLI 驱动）。

每个用例对应一个 S 编号；P0 回放守卫的 S07/S09 在
`tests/experiments/agent_runner/test_replay_read_guard.py` 的部署 command 用例里。
"""

from __future__ import annotations

import pytest

import fcntl
import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

from conftest import subtests
from support import HOOKS_ROOT, HookCase

if str(HOOKS_ROOT) not in sys.path:
    sys.path.insert(0, str(HOOKS_ROOT))

from hooks import transcripts  # noqa: E402


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


def valid_models() -> dict:
    role = {"model": "provider/model", "reasoning_effort": "high"}
    return {"developer": dict(role), "reviewer": dict(role), "designer": dict(role)}


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
        pytest.fail("epoch %d task not found" % epoch)


class R4Case(StateHelpers, HookCase):
    pass


# ---------------------------------------------------------------------------
# S01：旧 child 两次 start 重投不得复活到新任务
# ---------------------------------------------------------------------------
class S01OldChildDoubleRestartTests(StateHelpers, HookCase):
    host = "codex"

    def child_actor(self) -> dict:
        for item in actor_states(self.state):
            if item.get("parent_id") == "P" and item.get("role_source") in {
                "trusted_parent_dispatch",
                "retired_parent_epoch",
            }:
                return item
        pytest.fail("child actor not found")

    def restart(self, dispatch_id: str = ""):
        payload = {
            "hook_event_name": "SubagentStart",
            "session_id": "P",
            "agent_id": "agent-dev",
            "agent_type": "developer",
            "cwd": str(self.repo),
        }
        if dispatch_id:
            payload["dispatch_id"] = dispatch_id
        return self.run_core(payload)

    def test_twice_restarted_old_child_never_rebinds_to_new_epoch(self) -> None:
        self.activate("P")
        handoff = self.developer_handoff()
        self.allow(self.dispatch_payload("P", handoff, role="developer", tool_id="D1"))
        self.allow(self.child_start_payload("agent-dev", dispatch_id="D1"))
        assert self.child_actor()["dispatch_id"] == "D1"
        # 父切到 B 并分派唯一 D2。
        self.allow(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "P",
                "prompt": "使用 easydev，开始另一个任务",
                "cwd": str(self.repo),
                "turn_id": "turn-2",
            }
        )
        self.allow(self.dispatch_payload("P", handoff, role="developer", tool_id="D2"))
        before = self.task_for_epoch(2)
        # 连续两次 start 重投：第一次退役，第二次不能再被 unique fallback 绑到 D2。
        for attempt in range(2):
            _code, value, stderr = self.restart()
            assert not self.is_denied(value), value
            assert "未能匹配" in stderr, attempt
            actor = self.child_actor()
            assert actor["activation_scope"] == "role_only", attempt
            assert actor["dispatch_id"] == "D1", attempt
            assert "task_identity" not in actor, attempt
            assert actor["retired_binding"]["dispatch_id"] == "D1"
        # 退役后 input/start 交错仍不得回到 task_active。
        self.allow(self.child_input_payload_for("agent-dev", handoff))
        self.allow(self.child_input_payload_for("agent-dev", handoff))
        self.restart()
        actor = self.child_actor()
        assert actor["activation_scope"] == "role_only"
        assert actor.get("role_source") in {"retired_parent_epoch", "unlinked_child_handoff"}
        # 旧 child 的写与完成都不进入 B。
        self.allow(
            self.write_tool("P", "src/app.py", tool_id="cw", agent_id="agent-dev", is_child=True)
        )
        (self.repo / "src" / "app.py").write_text("value = 5\n", encoding="utf-8")
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
        self.allow(self.child_done_payload("P", report="**[P1] 证据**\n", agent="agent-dev"))
        after = self.task_for_epoch(2)
        assert after["write_seq"] == before["write_seq"]
        assert after["review_checkpoint"] == before["review_checkpoint"]
        assert not after["has_completed_code_review"]


# ---------------------------------------------------------------------------
# S02：状态 JSON 损坏不能丢掉已确定的危险 Git deny
# ---------------------------------------------------------------------------
class S02StateCorruptionTests(R4Case):
    host = "pi"

    def test_corrupt_root_and_task_json_keep_git_deny(self) -> None:
        self.activate("s1")
        backups = {}
        for kind in ("roots", "tasks"):
            for path in (self.state / kind).glob("*.json"):
                backups[path] = path.read_bytes()
        assert backups
        try:
            for kind in ("roots", "tasks"):
                files = sorted((self.state / kind).glob("*.json"))
                assert files, "no %s state file" % kind
                for label, payload in (
                    ("坏 JSON", b"{not-json"),
                    ("坏 UTF-8", b"\xff\xfe\x00garbage"),
                    ("schema 错误", b'{"schema_version": 99}'),
                ):
                    with subtests.test(kind=kind, case=label):
                        target = files[0]
                        target.write_bytes(payload)
                        os.chmod(target, 0o600)
                        try:
                            reason = self.deny(
                                self.shell("s1", "git reset --hard", tool_id="bad-" + kind)
                            )
                            assert "Git" in reason
                        finally:
                            target.write_bytes(backups[target])
                            os.chmod(target, 0o600)
        finally:
            for path, data in backups.items():
                path.write_bytes(data)
                os.chmod(path, 0o600)

    def corrupt(self, kind: str, payload: bytes) -> Path:
        files = sorted((self.state / kind).glob("*.json"))
        assert files, "no %s state file" % kind
        target = files[0]
        target.write_bytes(payload)
        os.chmod(target, 0o600)
        return target

    def test_inactive_actor_is_still_allowed_with_corrupt_state(self) -> None:
        self.activate("s1")
        # 先让 plain 写下自己的未激活锚点，再破坏它自己的 actor 状态：未激活会话
        # 不再做写预取，只有真正读取该状态的那一步才会告警降级。
        self.allow(self.shell("plain", "git status --short", tool_id="plain-warm"))
        target = None
        for path in sorted((self.state / "actors").glob("*.json")):
            if json.loads(path.read_text(encoding="utf-8")).get("activation_scope") == "inactive":
                target = path
        assert target is not None, "未找到 plain 的未激活锚点"
        target.write_bytes(b"{not-json")
        os.chmod(target, 0o600)
        _code, value, stderr = self.run_core(self.shell("plain", "git reset --hard", tool_id="plain"))
        assert not self.is_denied(value), value
        assert "warning" in stderr
        assert "私有状态不可用" in stderr


# ---------------------------------------------------------------------------
# S03：没有命令名的重定向仍产生真实写副作用
# ---------------------------------------------------------------------------
class S03BareRedirectTests(HookCase):
    host = "pi"

    def setup_method(self) -> None:
        super().setup_method()
        self.touch_dirty("src/app.py", "USER CONTENT\n")
        self.sub = self.repo / "src"
        (self.repo / "notes.txt").write_text("USER NOTES\n", encoding="utf-8")

    def test_bare_write_redirects_require_baseline(self) -> None:
        self.use_manual_baseline()
        self.activate("s1")
        for index, command in enumerate(("> src/app.py", ">> src/app.py", "<> src/app.py")):
            with subtests.test(command=command):
                reason = self.deny(self.shell("s1", command, tool_id="red-%d" % index))
                assert "baseline" in reason
        assert (self.repo / "src" / "app.py").read_text(encoding="utf-8") == "USER CONTENT\n"
        # 未跟踪文件不因“没有命令名”被跳过。
        self.deny(self.shell("s1", "> notes.txt", tool_id="red-untracked"))
        assert (self.repo / "notes.txt").read_text(encoding="utf-8") == "USER NOTES\n"

    def test_redirect_after_cd_uses_that_cwd(self) -> None:
        self.use_manual_baseline()
        self.activate("s1")
        reason = self.deny(self.shell("s1", "cd src; > app.py", tool_id="red-cd"))
        assert "baseline" in reason
        assert (self.repo / "src" / "app.py").read_text(encoding="utf-8") == "USER CONTENT\n"

    def test_redirect_to_dev_null_and_clean_target_are_allowed(self) -> None:
        self.activate("s1")
        self.allow(self.shell("s1", "> /dev/null", tool_id="red-null"))
        (self.repo / "notes.txt").write_text("t\n", encoding="utf-8")
        self.git("add", "notes.txt")
        self.git("commit", "-qm", "notes")
        self.allow(self.shell("s1", "> notes.txt", tool_id="red-clean"))

    def test_reviewer_bare_redirect_is_denied(self) -> None:
        self.activate("rev", self.reviewer_handoff())
        reason = self.deny(self.shell("rev", "> src/app.py", tool_id="red-rev"))
        assert "只读" in reason
        assert (self.repo / "src" / "app.py").read_text(encoding="utf-8") == "USER CONTENT\n"


# ---------------------------------------------------------------------------
# S04：wrapper 内相对 env -C 以进入 wrapper 前的有效 cwd 解析
# ---------------------------------------------------------------------------
class S04RelativeEnvChdirTests(HookCase):
    host = "pi"

    def setup_method(self) -> None:
        super().setup_method()
        sub = self.repo / "sub"
        sub.mkdir()
        (sub / "app.py").write_text("sub = 1\n", encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-qm", "sub-app")
        (sub / "app.py").write_text("SUB USER CONTENT\n", encoding="utf-8")

    def test_relative_env_c_checks_tool_cwd_relative_target(self) -> None:
        self.use_manual_baseline()
        self.activate("s1")
        command = "env -C sub sh -c 'printf x >> app.py'"
        reason = self.deny(self.shell("s1", command, tool_id="env-rel"))
        assert "baseline" in reason
        assert (self.repo / "sub" / "app.py").read_text(encoding="utf-8") == "SUB USER CONTENT\n"
        # 仓库根的 app.py 干净：同一命令形式指向它时放行，证明检查的是真实目标。
        self.allow(self.shell("s1", "env -C . sh -c 'printf x >> app.py'", tool_id="env-dot"))

    def test_absolute_env_c_and_baseline_satisfied(self) -> None:
        self.use_manual_baseline()
        self.activate("s1")
        absolute = "env -C %s sh -c 'printf x >> app.py'" % (self.repo / "sub")
        assert "baseline" in self.deny(self.shell("s1", absolute, tool_id="env-abs"))
        task_root = self.make_task_root()
        self.save_baseline(task_root, "sub/app.py", "SUB USER CONTENT\n")
        self.write_context(task_root=str(task_root))
        self.activate("s1")
        self.allow(self.shell("s1", "env -C sub sh -c 'printf x >> app.py'", tool_id="env-ok"))

    def test_multilayer_wrapper_with_relative_chdir(self) -> None:
        self.use_manual_baseline()
        self.activate("s1")
        command = "env -C sub bash -lc \"env -C . sh -c 'printf x >> app.py'\""
        reason = self.deny(self.shell("s1", command, tool_id="env-multi"))
        assert "baseline" in reason


# ---------------------------------------------------------------------------
# S05：cp/install 到已存在目录时要检查真正被覆盖的文件
# ---------------------------------------------------------------------------
class S05CopyDirectoryTargetTests(HookCase):
    host = "pi"

    def setup_method(self) -> None:
        super().setup_method()
        sub = self.repo / "sub"
        sub.mkdir()
        (sub / "app.py").write_text("sub = 1\n", encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-qm", "sub-app")
        (sub / "app.py").write_text("SUB USER CONTENT\n", encoding="utf-8")

    def test_cp_into_directory_checks_overwritten_file(self) -> None:
        self.use_manual_baseline()
        self.activate("s1")
        reason = self.deny(self.shell("s1", "cp src/app.py sub", tool_id="cp-dir"))
        assert "baseline" in reason
        assert (self.repo / "sub" / "app.py").read_text(encoding="utf-8") == "SUB USER CONTENT\n"
        reason = self.deny(self.shell("s1", "cp -t sub src/app.py", tool_id="cp-t"))
        assert "baseline" in reason
        reason = self.deny(self.shell("s1", "install -m 644 src/app.py sub", tool_id="inst-dir"))
        assert "baseline" in reason

    def test_cp_directory_target_after_baseline_is_allowed(self) -> None:
        self.activate("s1")
        task_root = self.make_task_root()
        self.save_baseline(task_root, "sub/app.py", "SUB USER CONTENT\n")
        self.write_context(task_root=str(task_root))
        self.activate("s1")
        self.allow(self.shell("s1", "cp src/app.py sub", tool_id="cp-ok"))

    def test_cp_plain_file_target_and_multi_source_are_handled(self) -> None:
        self.activate("s1")
        # 目的不是目录：目标就是该文件本身，新文件无需基线。
        self.allow(self.shell("s1", "cp src/app.py copied.py", tool_id="cp-file"))
        # 多源 + 非目录目的无法给出完整目标：不得声称完整，必须降级披露。
        _code, value, stderr = self.run_core(self.shell("s1", "cp a.py b.py missing", tool_id="cp-multi"))
        assert not self.is_denied(value), value
        assert "写入目标不完整" in stderr


# ---------------------------------------------------------------------------
# S06：业务目录软链指向另一个 Git 仓库
# ---------------------------------------------------------------------------
class S06SymlinkToOtherRepoTests(HookCase):
    host = "pi"

    def setup_method(self) -> None:
        super().setup_method()
        self.other = self.root / "outside-repo"
        self.other.mkdir()
        subprocess.run(
            ["git", "-c", "commit.gpgsign=false", "init", "-q", "."],
            cwd=str(self.other),
            check=True,
            capture_output=True,
        )
        (self.other / "app.py").write_text("outside = 1\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=str(self.other), check=True, capture_output=True)
        subprocess.run(
            ["git", "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", "x"],
            cwd=str(self.other),
            check=True,
            capture_output=True,
        )
        (self.other / "app.py").write_text("OUTSIDE USER CONTENT\n", encoding="utf-8")
        (self.repo / "link").symlink_to(self.other)

    def test_link_into_other_git_repo_is_denied(self) -> None:
        self.activate("s1")
        reason = self.deny(self.write_tool("s1", "link/app.py", tool_id="link-w"))
        assert "符号链接" in reason
        assert (self.other / "app.py").read_text(encoding="utf-8") == "OUTSIDE USER CONTENT\n"

    def test_link_to_nested_git_repo_inside_invoking_repo_is_denied(self) -> None:
        nested = self.repo / "nested-repo"
        nested.mkdir()
        subprocess.run(
            ["git", "-c", "commit.gpgsign=false", "init", "-q", "."],
            cwd=str(nested),
            check=True,
            capture_output=True,
        )
        (nested / "app.py").write_text("nested = 1\n", encoding="utf-8")
        (self.repo / "nested-link").symlink_to(nested)
        self.activate("s1")
        reason = self.deny(self.write_tool("s1", "nested-link/app.py", tool_id="nested-w"))
        assert "符号链接" in reason
        assert (nested / "app.py").read_text(encoding="utf-8") == "nested = 1\n"

    def test_explicit_other_repo_path_still_follows_baseline_rules(self) -> None:
        self.use_manual_baseline()
        self.activate("s1")
        target = str(self.other / "app.py")
        reason = self.deny(self.write_tool("s1", target, tool_id="other-w"))
        assert "baseline" in reason
        task_root = self.make_task_root()
        self.save_baseline(task_root, "app.py", "OUTSIDE USER CONTENT\n")
        self.write_context(task_root=str(task_root))
        self.activate("s1")
        self.allow(self.write_tool("s1", target, tool_id="other-ok"))


# ---------------------------------------------------------------------------
# S08：长 transcript 的最新用户记录、普通跟进去重与成功指纹隔离
# ---------------------------------------------------------------------------
class S08LongTranscriptTests(StateHelpers, HookCase):
    host = "antigravity"

    def write_transcript(self, entries) -> str:
        path = self.root / "long-transcript.jsonl"
        with path.open("w", encoding="utf-8") as handle:
            for entry in entries:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return str(path)

    def append(self, path: str, entry) -> None:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def invocation(self, path: str, execution: int) -> dict:
        return {
            "hookEventName": "PreInvocation",
            "conversationId": "P",
            "transcriptPath": path,
            "workspacePath": str(self.repo),
            "executionNum": execution,
        }

    def filler(self) -> list:
        return [
            {"type": "assistant", "role": "assistant", "content": "步骤 %d" % index}
            for index in range(200)
        ]

    def test_new_task_beyond_200_lines_opens_new_epoch(self) -> None:
        entries = [{"type": "user", "role": "user", "message": "使用 easydev，改这个文件"}]
        entries.extend(self.filler())
        assert len(entries) == 201
        path = self.write_transcript(entries)
        self.run_core(self.invocation(path, 1))
        # 第 202 行才追加真正的当前任务：必须在 200 行之后仍被取到并开新 epoch。
        self.append(path, {"type": "user", "role": "user", "message": "使用 easydev，新任务 B"})
        self.write_path("P", "w1", content="value = 2\n")
        assert self.task_for_epoch(1)["write_seq"] == 1
        self.touch_dirty("src/app.py", "USER B CONTENT\n")
        # 新 epoch 的首写仍走手工基线核对：脏目标必须缺基线拒绝。
        self.use_manual_baseline()
        self.run_core(self.invocation(path, 2))
        epochs = {int(item.get("task_epoch") or 0) for item in task_states(self.state)}
        assert 2 in epochs
        reason = self.deny(self.write_tool("P", "src/app.py", tool_id="b-first"))
        assert "baseline" in reason
        assert (self.repo / "src" / "app.py").read_text(encoding="utf-8") == "USER B CONTENT\n"

    def test_plain_follow_up_is_deduped_across_repeated_invocations(self) -> None:
        path = self.write_transcript(
            [{"type": "user", "role": "user", "message": "使用 easydev，改这个文件"}]
        )
        self.run_core(self.invocation(path, 1))
        self.write_path("P", "w1", content="value = 2\n")
        baseline = int(self.task_for_epoch(1)["user_generation"])
        self.append(path, {"type": "user", "role": "user", "message": "继续修复"})
        self.run_core(self.invocation(path, 2))
        once = int(self.task_for_epoch(1)["user_generation"])
        assert once == (baseline + 1)
        # 同一 user 记录重复投递三次不得继续放大 generation。
        for execution in (3, 4, 5):
            self.run_core(self.invocation(path, execution))
        assert int(self.task_for_epoch(1)["user_generation"]) == once
        # 真正的新跟进仍然推进一次。
        self.append(path, {"type": "user", "role": "user", "message": "继续实现"})
        self.run_core(self.invocation(path, 6))
        assert int(self.task_for_epoch(1)["user_generation"]) == (once + 1)

    def test_latest_record_is_found_past_the_two_megabyte_boundary(self) -> None:
        filler = "x" * 900
        with open(self.root / "big-transcript.jsonl", "w", encoding="utf-8") as handle:
            for index in range(2600):
                handle.write(
                    json.dumps(
                        {"type": "assistant", "role": "assistant", "content": "%d %s" % (index, filler)}
                    )
                    + "\n"
                )
            handle.write(
                json.dumps({"type": "user", "role": "user", "message": "使用 easydev，大文件新任务"})
                + "\n"
            )
        path = str(self.root / "big-transcript.jsonl")
        assert os.path.getsize(path) > (2 * 1024 * 1024)
        _code, value, stderr = self.run_core(self.invocation(path, 1))
        assert not self.is_denied(value), value
        assert "无法从 transcript 取得最新用户记录" not in stderr
        self.deny(self.shell("P", "git reset --hard", tool_id="big-git"))

    def test_unreadable_latest_record_is_disclosed_instead_of_reusing_old(self) -> None:
        path = self.write_transcript(
            [{"type": "user", "role": "user", "message": "使用 easydev，改这个文件"}]
        )
        # 末尾追加一个超过读取上限的单行记录：最新记录不可得，必须降级披露。
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"type": "assistant", "content": "x" * (3 * 1024 * 1024)}) + "\n")
        _code, value, stderr = self.run_core(self.invocation(path, 7))
        assert not self.is_denied(value), value
        assert "无法从 transcript 取得最新用户记录" in stderr
        self.allow(self.shell("P", "git reset --hard", tool_id="unread-latest"))


# ---------------------------------------------------------------------------
# S10：pre/post 目标键必须含完整仓库身份
# ---------------------------------------------------------------------------
class S10CrossRepoPairingTests(HookCase):
    host = "pi"

    def setup_method(self) -> None:
        super().setup_method()
        # 两个用例都围绕“脏目标先要手工基线”展开：显式打开手工核对路径。
        self.use_manual_baseline()
        self.other = self.root / "repo2"
        (self.other / "src").mkdir(parents=True)
        subprocess.run(
            ["git", "-c", "commit.gpgsign=false", "init", "-q", "."],
            cwd=str(self.other),
            check=True,
            capture_output=True,
        )
        (self.other / "src" / "app.py").write_text("other = 1\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=str(self.other), check=True, capture_output=True)
        subprocess.run(
            ["git", "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", "x"],
            cwd=str(self.other),
            check=True,
            capture_output=True,
        )
        (self.other / "src" / "app.py").write_text("OTHER USER CONTENT\n", encoding="utf-8")

    def test_successful_pairs_in_two_repos_stay_independent(self) -> None:
        (self.other / "lib").mkdir()
        (self.other / "lib" / "util.py").write_text("util = 1\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=str(self.other), check=True, capture_output=True)
        subprocess.run(
            ["git", "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", "util"],
            cwd=str(self.other),
            check=True,
            capture_output=True,
        )
        (self.other / "lib" / "util.py").write_text("OTHER UTIL CONTENT\n", encoding="utf-8")
        self.activate("P")
        # A 仓库：干净文件 pre → 真实写 → 成功 post。
        self.allow(self.write_tool("P", "src/app.py", tool_id="ok-a"))
        (self.repo / "src" / "app.py").write_text("value = 2\n", encoding="utf-8")
        self.allow(
            self.write_tool("P", "src/app.py", tool_id="ok-a", event="post_tool", outcome="success")
        )
        # B 仓库：脏文件先要基线，之后同一套配对照常生效。
        other_target = str(self.other / "lib" / "util.py")
        assert "baseline" in self.deny(self.write_tool("P", other_target, tool_id="ok-b"))
        task_root = self.make_task_root()
        self.save_baseline(task_root, "lib/util.py", "OTHER UTIL CONTENT\n")
        self.write_context(task_root=str(task_root))
        self.activate("P")
        self.allow(self.write_tool("P", other_target, tool_id="ok-b"))
        (self.other / "lib" / "util.py").write_text("UTIL v2\n", encoding="utf-8")
        self.allow(
            self.write_tool(
                "P", other_target, tool_id="ok-b", event="post_tool", outcome="success"
            )
        )
        # 两个仓库各自保留成功指纹（键含仓库身份），互不覆盖。
        successes = []
        for task in task_states(self.state):
            successes.extend((task.get("last_success") or {}).keys())
        assert any(key.endswith("::src/app.py") for key in successes), successes
        assert any(key.endswith("::lib/util.py") for key in successes), successes
        # 同一仓库再次写入未变更的内容不再被误判为外部修改。
        self.allow(self.write_tool("P", other_target, tool_id="ok-b2"))

    def test_post_for_other_repo_with_same_relative_path_is_rejected(self) -> None:
        self.activate("P")
        self.allow(self.write_tool("P", "src/app.py", tool_id="pair-1"))
        (self.repo / "src" / "app.py").write_text("value = 2\n", encoding="utf-8")
        # 同 actor/epoch/tool ID，但 post 报的是另一个仓库的同相对路径。
        _code, value, stderr = self.run_core(
            self.write_tool(
                "P",
                str(self.other / "src" / "app.py"),
                tool_id="pair-1",
                event="post_tool",
                outcome="success",
            )
        )
        assert not self.is_denied(value), value
        assert "不一致" in stderr
        # B 仓库仍缺基线：错误 post 不能建立成功豁免。
        reason = self.deny(self.write_tool("P", str(self.other / "src" / "app.py"), tool_id="pair-2"))
        assert "baseline" in reason
        for task in task_states(self.state):
            for key in (task.get("last_success") or {}):
                assert "src/app.py" not in key.split("::")[-1], "other repo 不应有成功指纹"


# ---------------------------------------------------------------------------
# S11：同一分派 ID 改内容必须整段失效
# ---------------------------------------------------------------------------
class S11DispatchConflictTests(HookCase):
    host = "codex"

    def reviews(self) -> dict:
        for item in task_states(self.state):
            if item.get("reviews"):
                return item["reviews"]
        pytest.fail("reviews not found")

    def task(self) -> dict:
        for item in task_states(self.state):
            if item.get("reviews"):
                return item
        pytest.fail("task not found")

    def test_changed_prompt_invalidates_segment_and_completion(self) -> None:
        self.activate("P")
        self.allow(self.dispatch_payload("P", self.reviewer_handoff(), tool_id="R1"))
        self.allow(self.child_start_payload("agent-1", dispatch_id="R1"))
        self.write_path("P", "w1", content="value = 2\n")
        # 同一 ID 改内容：整段失效，不重新取样到新的 write_seq。
        self.allow(
            self.dispatch_payload(
                "P", self.reviewer_handoff("补充要求：检查兼容性\n"), tool_id="R1"
            )
        )
        entry = self.reviews()["R1"]
        assert entry["status"] == "invalid"
        assert int(entry["start_write_seq"]) == 0
        # 旧 child 完成不得抬高 checkpoint。
        self.allow(self.child_done_payload("P", report="**[P1] 证据**\n", agent="agent-1"))
        task = self.task()
        assert not task["has_completed_code_review"]
        assert int(task["review_checkpoint"]) == 0
        _code, value, _stderr = self.run_core(self.settle_payload("P"))
        assert not self.is_continued(value), value

    def test_role_change_with_same_id_invalidates_segment(self) -> None:
        self.activate("P")
        self.allow(self.dispatch_payload("P", self.reviewer_handoff(), tool_id="R9"))
        assert self.reviews()["R9"]["status"] == "started"
        # 同一 ID 改成 developer：审查段必须失效，不能被旧观察继续引用。
        self.allow(
            self.dispatch_payload("P", self.developer_handoff(), role="developer", tool_id="R9")
        )
        assert self.reviews()["R9"]["status"] == "invalid"
        assert not self.reviews()["R9"]["code_review"]

    def test_a_b_a_conflict_stays_invalid_and_same_content_is_idempotent(self) -> None:
        self.activate("P")
        first = self.reviewer_handoff()
        second = self.reviewer_handoff("补充要求：性能\n")
        self.allow(self.dispatch_payload("P", first, tool_id="R2"))
        self.allow(self.dispatch_payload("P", second, tool_id="R2"))
        assert self.reviews()["R2"]["status"] == "invalid"
        self.allow(self.dispatch_payload("P", first, tool_id="R2"))
        assert self.reviews()["R2"]["status"] == "invalid"
        # 合法新分派用新 ID：独立取样并保持 started。
        self.allow(self.dispatch_payload("P", first, tool_id="R3"))
        assert self.reviews()["R3"]["status"] == "started"
        self.allow(self.dispatch_payload("P", first, tool_id="R3"))
        assert self.reviews()["R3"]["status"] == "started"


# ---------------------------------------------------------------------------
# S12：exec 非零退出码/status 冲突不能确认成功
# ---------------------------------------------------------------------------
class S12ExecOutcomeTests(HookCase):
    host = "codex"

    def set_models(self):
        path = self.root / "models.json"
        path.write_text(json.dumps(valid_models()), encoding="utf-8")
        return path

    @pytest.mark.parametrize("exit_code, confirmed", [(2, False), (1, False), (0, True)])
    def test_codex_command_execution_exit_code_wins(self, exit_code, confirmed) -> None:
        path = self.root / ("exec-%s.jsonl" % exit_code)
        entries = [
            {
                "type": "item.completed",
                "item": {
                    "type": "command_execution",
                    "id": "c1",
                    "command": "cat /w/a.py",
                    "exit_code": exit_code,
                    "status": "completed",
                },
            }
        ]
        path.write_text(
            "\n".join(json.dumps(entry) for entry in entries) + "\n", encoding="utf-8"
        )
        records, _warnings = transcripts.codex_records(str(path))
        assert len(records) == 1
        assert bool(records[0]["confirmed"]) == confirmed, exit_code

    def test_codex_command_execution_without_success_flag_is_not_confirmed(self) -> None:
        path = self.root / "exec-nofield.jsonl"
        path.write_text(
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "command_execution", "id": "c1", "command": "cat /w/a.py"},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        records, _warnings = transcripts.codex_records(str(path))
        assert len(records) == 1
        assert not records[0]["confirmed"]

    def test_antigravity_success_status_cannot_override_is_error(self) -> None:
        path = self.root / "ag-conflict.jsonl"
        entries = [
            {
                "PLANNER_RESPONSE": {
                    "tool_calls": [{"name": "read_file", "args": {"Path": "/w/a.py"}, "id": "c1"}]
                }
            },
            {
                "type": "TOOL_RESULT",
                "tool_results": [{"id": "c1", "status": "success", "is_error": True}],
            },
        ]
        path.write_text(
            "\n".join(json.dumps(entry) for entry in entries) + "\n", encoding="utf-8"
        )
        records, _warnings = transcripts.antigravity_records(str(path))
        assert len(records) == 1
        assert not records[0]["confirmed"]

    def test_failed_exec_does_not_activate_via_skill_read(self) -> None:
        transcript = self.root / "exec-skill.jsonl"
        skill = str(self.skill / "SKILL.md")
        entries = [
            {"type": "session_meta", "payload": {"id": "t1", "cwd": str(self.repo)}},
            {
                "type": "item.completed",
                "item": {
                    "type": "command_execution",
                    "id": "c1",
                    "command": "cat %s" % skill,
                    "exit_code": 2,
                    "status": "completed",
                },
            },
        ]
        transcript.write_text(
            "\n".join(json.dumps(entry) for entry in entries) + "\n", encoding="utf-8"
        )
        payload = {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "t1",
            "prompt": "",
            "cwd": str(self.repo),
            "transcript_path": str(transcript),
        }
        self.run_core(payload)
        self.allow(self.shell("t1", "git reset --hard", tool_id="exec-fail"))


# ---------------------------------------------------------------------------
# S13：transcript 原生 session_meta.id 必须核对
# ---------------------------------------------------------------------------
class S13TranscriptSessionIdentityTests(HookCase):
    host = "codex"

    def write_transcript(self, entries, name: str = "identity.jsonl") -> tuple:
        path = self.root / name
        path.write_text(
            "\n".join(json.dumps(entry) for entry in entries) + "\n", encoding="utf-8"
        )
        return str(path)

    def read_entries(self, session_id=None, agent_id=None, skill=None) -> list:
        entries = []
        payload = {}
        if session_id:
            payload["id"] = session_id
        if agent_id:
            payload["agent_id"] = agent_id
        payload["cwd"] = str(self.repo)
        if payload:
            entries.append({"type": "session_meta", "payload": payload})
        entries.extend(
            [
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "name": "read_file",
                        "call_id": "c1",
                        "arguments": json.dumps({"path": skill or str(self.skill / "SKILL.md")}),
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
        )
        return entries

    def input_payload_with(self, session: str, path: str) -> dict:
        return {
            "hook_event_name": "UserPromptSubmit",
            "session_id": session,
            "prompt": "",
            "cwd": str(self.repo),
            "transcript_path": path,
        }

    def test_same_session_activates_and_other_session_does_not(self) -> None:
        same = self.write_transcript(self.read_entries(session_id="t1"), "same.jsonl")
        self.run_core(self.input_payload_with("t1", same))
        self.deny(self.shell("t1", "git reset --hard", tool_id="same-git"))

        other = self.write_transcript(self.read_entries(session_id="UNRELATED"), "other.jsonl")
        self.run_core(self.input_payload_with("t2", other))
        self.allow(self.shell("t2", "git reset --hard", tool_id="other-git"))

    def test_child_actor_identity_does_not_activate_root(self) -> None:
        path = self.write_transcript(self.read_entries(session_id="t3", agent_id="child-1"), "child.jsonl")
        self.run_core(self.input_payload_with("t3", path))
        self.allow(self.shell("t3", "git reset --hard", tool_id="child-git"))

    def test_missing_identity_is_disclosed(self) -> None:
        path = self.write_transcript(self.read_entries(), "anon.jsonl")
        _code, value, stderr = self.run_core(self.input_payload_with("t4", path))
        assert not self.is_denied(value), value
        assert "缺原生会话身份" in stderr
        self.deny(self.shell("t4", "git reset --hard", tool_id="anon-git"))

    def test_wrong_install_tree_does_not_activate(self) -> None:
        other_skill = self.root / "other-install" / "SKILL.md"
        other_skill.parent.mkdir()
        other_skill.write_text("other\n", encoding="utf-8")
        path = self.write_transcript(
            self.read_entries(session_id="t5", skill=str(other_skill)), "other-skill.jsonl"
        )
        self.run_core(self.input_payload_with("t5", path))
        self.allow(self.shell("t5", "git reset --hard", tool_id="tree-git"))


# ---------------------------------------------------------------------------
# S14：transcript 每条读取使用自己的有效 cwd
# ---------------------------------------------------------------------------
class S14TranscriptCwdTests(HookCase):
    host = "codex"

    def write_transcript(self, entries) -> str:
        path = self.root / "cwd.jsonl"
        path.write_text(
            "\n".join(json.dumps(entry) for entry in entries) + "\n", encoding="utf-8"
        )
        return str(path)

    def codex_cd_transcript(self, session_id: str = "cwd-session") -> str:
        return self.write_transcript(
            [
                {"type": "session_meta", "payload": {"id": session_id, "cwd": "/w"}},
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "name": "Bash",
                        "call_id": "c1",
                        "arguments": json.dumps({"command": "cd /w/sub; cat a.py"}),
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call_output",
                        "call_id": "c1",
                        "output": "x",
                        "is_error": False,
                    },
                },
            ]
        )

    def test_codex_shell_read_uses_parser_cwd(self) -> None:
        path = self.codex_cd_transcript()
        records, _warnings = transcripts.codex_records(path)
        assert len(records) == 1
        assert records[0]["paths"] == ["/w/sub/a.py"]
        assert records[0]["confirmed"]

    def test_antigravity_shell_read_uses_parser_cwd(self) -> None:
        path = self.write_transcript(
            [
                {
                    "PLANNER_RESPONSE": {
                        "cwd": "/w",
                        "tool_calls": [
                            {
                                "name": "run_command",
                                "args": {"CommandLine": "cd sub; cat a.py", "Cwd": "/w"},
                                "id": "c1",
                            }
                        ],
                    }
                },
                {"type": "TOOL_RESULT", "tool_results": [{"id": "c1", "status": "success"}]},
            ]
        )
        records, _warnings = transcripts.antigravity_records(path)
        assert len(records) == 1
        assert records[0]["paths"] == ["/w/sub/a.py"]

    def test_wrapper_chdir_is_resolved_for_each_read(self) -> None:
        path = self.write_transcript(
            [
                {"type": "session_meta", "payload": {"id": "wrap-session", "cwd": "/w"}},
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "name": "Bash",
                        "call_id": "c1",
                        "arguments": json.dumps({"command": "env -C sub cat a.py"}),
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call_output",
                        "call_id": "c1",
                        "output": "x",
                        "is_error": False,
                    },
                },
            ]
        )
        records, _warnings = transcripts.codex_records(path)
        assert len(records) == 1
        assert records[0]["paths"] == ["/w/sub/a.py"]

    def test_antigravity_uses_each_calls_own_cwd(self) -> None:
        path = self.write_transcript(
            [
                {
                    "PLANNER_RESPONSE": {
                        "cwd": "/w",
                        "tool_calls": [
                            {
                                "name": "run_command",
                                "args": {"CommandLine": "cat a.py", "Cwd": "/w/other"},
                                "id": "c1",
                            }
                        ],
                    }
                },
                {
                    "PLANNER_RESPONSE": {
                        "cwd": "/w",
                        "tool_calls": [
                            {
                                "name": "run_command",
                                "args": {"CommandLine": "cat a.py"},
                                "id": "c2",
                            }
                        ],
                    }
                },
                {
                    "type": "TOOL_RESULT",
                    "tool_results": [
                        {"id": "c1", "status": "success"},
                        {"id": "c2", "status": "success"},
                    ],
                },
            ]
        )
        records, _warnings = transcripts.antigravity_records(path)
        assert len(records) == 2
        assert [record["paths"] for record in records] == [["/w/other/a.py"], ["/w/a.py"]]

    def test_role_metric_respects_transcript_cwd_unless_explicit(self) -> None:
        experiments = HOOKS_ROOT / "tests" / "experiments"
        if str(experiments) not in sys.path:
            sys.path.insert(0, str(experiments))
        import role_metric  # noqa: E402

        path = self.codex_cd_transcript("cwd-metric")
        rows, _warnings = role_metric.collect_transcripts(
            "codex", Path(path), [], [], {}, None
        )
        paths = [item["path"] for item in rows[0]["file_access"]]
        assert paths == ["/w/sub/a.py"]
        # 显式 --cwd 只作为相对路径基准，不能覆盖 parser 给出的绝对目标。
        rows, _warnings = role_metric.collect_transcripts(
            "codex", Path(path), [], [], {}, Path("/elsewhere")
        )
        assert [item["path"] for item in rows[0]["file_access"]] == ["/w/sub/a.py"]


# ---------------------------------------------------------------------------
# S15：probe 模式锁忙时只观察，不拦截
# ---------------------------------------------------------------------------
class S15ProbeLockBusyTests(R4Case):
    host = "pi"

    def hold_locks(self):
        held = []
        for path in sorted((self.state / "locks").glob("*.lock")):
            handle = open(path, "r+b")
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            held.append(handle)
        return held

    def test_probe_allows_while_enforce_denies_for_both_input_classes(self) -> None:
        self.activate("s1")
        self.activate("rev", self.reviewer_handoff())
        git_command = self.shell("s1", "git reset --hard", tool_id="probe-git")
        readonly_command = self.shell("rev", "> src/app.py", tool_id="probe-ro")
        held = self.hold_locks()
        try:
            for label, payload in (("git", git_command), ("readonly", readonly_command)):
                _code, value, stderr = self.run_core(payload, mode="probe")
                assert not self.is_denied(value), (label, value)
                assert "warning" in stderr, label
        finally:
            for handle in held:
                handle.close()
        assert "Git" in self.deny(git_command)
        assert "只读" in self.deny(readonly_command)


    def test_probe_allows_when_state_is_unavailable(self) -> None:
        self.activate("s1")
        for path in (self.state / "actors").glob("*.json"):
            path.write_bytes(b"{not-json")
            os.chmod(path, 0o600)
        _code, value, stderr = self.run_core(
            self.shell("s1", "git reset --hard", tool_id="probe-state"), mode="probe"
        )
        assert not self.is_denied(value), value
        assert "warning" in stderr


# ---------------------------------------------------------------------------
# S16：未激活的普通 child 不得被全局禁止嵌套
# ---------------------------------------------------------------------------
class S16InactiveChildNestedTests(HookCase):
    host = "pi"

    def test_inactive_child_dispatch_is_allowed(self) -> None:
        env = {"PI_SUBAGENT_CHILD": "1", "PI_SUBAGENT_PARENT_SESSION": "missing"}
        self.allow(self.dispatch_payload("child", self.developer_handoff(), tool_id="in-1"), env=env)
        self.allow(
            self.tool_payload("child", "subagent", {"action": "create", "agent": "developer", "task": "x"}, tool_id="in-2"),
            env=env,
        )

    def test_role_only_child_is_denied_for_nested_dispatch(self) -> None:
        self.activate("parent")
        child_env = {"PI_SUBAGENT_CHILD": "1", "PI_SUBAGENT_PARENT_SESSION": "parent"}
        self.run_core(
            {
                "event": "input",
                "sessionId": "child-ro",
                "prompt": self.reviewer_handoff(),
                "cwd": str(self.repo),
                "child": True,
            },
            env=child_env,
        )
        reason = self.deny(
            self.dispatch_payload("child-ro", self.reviewer_handoff(), tool_id="ro-nest"),
            env=child_env,
        )
        assert "子代理" in reason

    def test_task_active_child_is_denied_but_status_and_wait_are_allowed(self) -> None:
        self.activate("P")
        handoff = self.developer_handoff()
        self.allow(self.dispatch_payload("P", handoff, role="developer", tool_id="D1"))
        self.allow(self.child_input_payload("C", handoff, parent="P"))
        reason = self.deny(self.dispatch_payload("C", self.developer_handoff(), tool_id="nest-1"))
        assert "子代理" in reason
        for index, action in enumerate(("status", "wait")):
            payload = self.tool_payload("C", "subagent", {"action": action, "id": "x"}, tool_id="st-%d" % index)
            self.allow(payload)


# ---------------------------------------------------------------------------
# S17：被误列 doc_paths 的运行配置仍计入代码写入
# ---------------------------------------------------------------------------
class S17MislabeledConfigTests(HookCase):
    host = "pi"

    def setup_method(self) -> None:
        super().setup_method()
        (self.repo / "deploy.conf").write_text("mode = 1\n", encoding="utf-8")
        (self.repo / "reports").mkdir()
        (self.repo / "reports" / "other.conf").write_text("k = 1\n", encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-qm", "configs")

    def code_write_seq(self) -> int:
        for item in task_states(self.state):
            if item.get("task_identity"):
                return int(item.get("last_code_write_seq") or 0)
        pytest.fail("task state not found")

    def test_mislabeled_configs_still_count_as_code_writes(self) -> None:
        self.write_context(
            task_root=str(self.repo),
            doc_paths=["deploy.conf", "reports/other.conf", "docs/guide.md"],
        )
        self.activate("P")
        self.write_path("P", "conf", "deploy.conf", "mode = 2\n")
        assert self.code_write_seq() == 1
        self.write_path("P", "rconf", "reports/other.conf", "k = 2\n")
        assert self.code_write_seq() == 2
        # 纯文档路径仍按文档排除。
        self.write_path("P", "doc", "docs/guide.md", "guide v2\n")
        assert self.code_write_seq() == 2


# ---------------------------------------------------------------------------
# S18：分派前的 Git/models 读取不在 task 锁内
# ---------------------------------------------------------------------------
class S18DispatchLockScopeTests(StateHelpers, HookCase):
    host = "pi"

    def slow_git_env(self) -> tuple:
        real_git = subprocess.run(
            ["which", "git"], capture_output=True, text=True
        ).stdout.strip()
        wrapper_dir = self.root / "slow-git"
        wrapper_dir.mkdir()
        counter = self.root / "git-calls"
        counter.write_text("", encoding="utf-8")
        wrapper = wrapper_dir / "git"
        wrapper.write_text(
            "#!/bin/sh\nprintf 'x' >> %s\nsleep 0.2\nexec %s \"$@\"\n" % (counter, real_git),
            encoding="utf-8",
        )
        os.chmod(wrapper, 0o755)
        # git 子进程有 300ms 单次上限：先预热一次，再把计数器归零测真实调用次数。
        subprocess.run([str(wrapper), "--version"], capture_output=True, timeout=30)
        counter.write_text("", encoding="utf-8")
        env = {"PATH": str(wrapper_dir) + os.pathsep + os.environ.get("PATH", "")}
        return env, counter

    def prepare(self) -> tuple:
        self.activate("s1")
        models = self.repo / ".easydev" / "models.json"
        models.parent.mkdir()
        models.write_text(json.dumps(valid_models()), encoding="utf-8")
        task_lock = self.state / "locks" / ("task-" + self.task_lock_key() + ".lock")
        assert task_lock.is_file(), task_lock
        env, counter = self.slow_git_env()
        return env, counter, task_lock

    def task_lock_key(self) -> str:
        hook_state = self.hook_state_module()
        for item in actor_states(self.state):
            identity = item.get("task_identity")
            if identity:
                return hook_state.identity_key("task", "pi", "s1", identity)
        return ""

    def core_command(self) -> list:
        return [
            sys.executable,
            str(HOOKS_ROOT / "hooks" / "core.py"),
            "--host",
            "pi",
            "--context",
            str(self.context_path),
        ]

    def start_dispatch(self, env) -> subprocess.Popen:
        process = subprocess.Popen(
            self.core_command(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=self.environment(env),
        )
        process.stdin.write(
            json.dumps(self.dispatch_payload("s1", self.developer_handoff(), tool_id="slow"))
        )
        process.stdin.close()
        return process

    def finish(self, process: subprocess.Popen) -> tuple:
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

    def test_git_query_stays_outside_shared_task_lock(self) -> None:
        env, counter, task_lock = self.prepare()
        started = time.monotonic()
        process = self.start_dispatch(env)
        longest = 0.0
        streak = 0.0
        while process.poll() is None:
            handle = open(task_lock, "r+b")
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
        elapsed = time.monotonic() - started
        stdout, stderr = self.finish(process)
        assert process.returncode == 0, stderr
        assert json.loads(stdout) == {}, stderr
        assert "状态锁不可用" not in stderr
        # 慢 Git 确实执行过（说明窗口存在），但只应在锁外查一次。
        assert counter.read_text(encoding="utf-8") == "x", stderr
        assert elapsed > 0.15
        assert longest < 0.15, "共享 task 锁在慢 Git 期间被长期占用"

    def test_concurrent_write_during_slow_git_is_not_lock_starved(self) -> None:
        env, _counter, _task_lock = self.prepare()
        process = self.start_dispatch(env)
        time.sleep(0.08)
        # 并发的第二次状态写（child/写事件同路径）：慢 Git 期间不得因锁忙降级。
        _code, concurrent, concurrent_stderr = self.run_core(
            self.write_tool("s1", "src/app.py", tool_id="concurrent"), env=env
        )
        stdout, stderr = self.finish(process)
        assert process.returncode == 0, stderr
        assert json.loads(stdout) == {}, stderr
        assert not self.is_denied(concurrent), concurrent
        assert "状态锁不可用" not in concurrent_stderr
        assert "锁" not in concurrent_stderr


# ---------------------------------------------------------------------------
# S19：共享夹具默认隔离 HOME/XDG/Git 全局配置
# ---------------------------------------------------------------------------
class S19HomeIsolationTests(HookCase):
    host = "pi"

    def setup_method(self) -> None:
        super().setup_method()
        # 模拟“调用者 HOME”：含无效用户配置的 sentinel 目录。
        self.caller_home = self.root / "caller-home"
        (self.caller_home / ".config" / "easydev").mkdir(parents=True)
        (self.caller_home / ".config" / "easydev" / "models.json").write_text(
            '{"broken": true}\n', encoding="utf-8"
        )

    def test_default_environment_points_at_fixture_home(self) -> None:
        env = self.environment()
        assert env["HOME"] == str(self.home)
        assert env["XDG_CONFIG_HOME"] == str(self.home / ".config")
        assert env["GIT_CONFIG_GLOBAL"] == os.devnull
        completed = subprocess.run(
            ["git", "config", "--global", "--list"],
            env=self.environment(),
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert completed.returncode == 0, completed.stderr
        assert completed.stdout.strip() == ""

    def test_normal_case_ignores_caller_home(self) -> None:
        models = self.repo / ".easydev" / "models.json"
        models.parent.mkdir()
        models.write_text(json.dumps(valid_models()), encoding="utf-8")
        self.activate("s1")
        previous = os.environ.get("HOME")
        os.environ["HOME"] = str(self.caller_home)
        try:
            self.allow(
                self.dispatch_payload("s1", self.developer_handoff(), tool_id="iso-1")
            )
        finally:
            if previous is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = previous

    def test_source_selection_case_still_reads_the_explicit_temp_source(self) -> None:
        self.activate("s1")
        # 专项用例显式切到指定的临时用户配置源：坏配置必须被真的读到并拒绝。
        self.deny(
            self.dispatch_payload("s1", self.developer_handoff(), tool_id="iso-2"),
            env={"HOME": str(self.caller_home)},
        )
        (self.caller_home / ".config" / "easydev" / "models.json").write_text(
            json.dumps(valid_models()), encoding="utf-8"
        )
        self.allow(
            self.dispatch_payload("s1", self.developer_handoff(), tool_id="iso-3"),
            env={"HOME": str(self.caller_home)},
        )
