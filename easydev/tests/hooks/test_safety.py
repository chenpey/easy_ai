"""P1：Git 危险动作、基线只读核对与 reviewer 只读。"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess

from support import HookCase
from test_r5_fixes import task_states


GIT_ALLOWED = (
    "git status",
    "git diff --stat",
    "git log --oneline -3",
    "git restore --staged src/app.py",
    "git stash list",
    "git clean -nd",
    "git push --dry-run origin main",
    "git reset --soft HEAD",
    "echo 'git reset --hard'",
    "printf '%s' 'git stash'",
)

# 冻结的嵌套替换词法输入（`\` 与真实换行是真实两个字节），只作为字符串投递。
COMMENT_SUBSTITUTION = (
    "printf '%s' \"$(printf pre\n"
    "# note \\\n"
    "git reset --hard\n"
    "printf post)\""
)
SINGLE_QUOTED_SUBSTITUTION = "printf '%s' \"$(printf '%s' 'literal\\\n$(git reset --hard)')\""
CONTINUATION_SUBSTITUTION = "printf '%s' \"$(g\\\nit reset --ha\\\nrd)\""

GIT_DENIED = (
    "git reset --hard",
    "git reset --hard HEAD~1",
    "git checkout -- src/app.py",
    "git checkout -f main",
    "git restore src/app.py",
    "git restore --worktree --staged src/app.py",
    "git restore -W src/app.py",
    "git clean -f",
    "git clean -fdx",
    "git clean --force",
    "git stash",
    "git stash push",
    "git stash drop",
    "git push --force origin main",
    "git push -f origin main",
    "git push --force-with-lease origin main",
    "git push origin +main:main",
    "true && git reset --hard",
    "bash -lc 'git -C repo reset --hard'",
    "env X=1 sudo -u alice bash -lc 'git -C /repo reset --hard'",
    "git -C /missing reset --hard",
)


class SafetyMixin(object):
    def test_git_allowed_commands(self) -> None:
        self.activate()
        for index, command in enumerate(GIT_ALLOWED):
            self.allow(self.shell("s1", command, tool_id="allow-%d" % index), mode="enforce")

    def test_git_denied_commands(self) -> None:
        self.activate()
        for index, command in enumerate(GIT_DENIED):
            reason = self.deny(self.shell("s1", command, tool_id="deny-%d" % index))
            assert "Git" in reason

    def test_documented_alias_boundary_is_not_claimed(self) -> None:
        self.activate()
        self.allow(self.shell("s1", "git -c alias.wipe='reset --hard' wipe"))

    def test_dirty_write_requires_baseline(self) -> None:
        self.use_manual_baseline()
        self.touch_dirty("src/app.py")
        self.activate()
        reason = self.deny(self.write_tool("s1", "src/app.py"))
        assert "baseline" in reason
        task_root = self.make_task_root()
        self.save_baseline(task_root, "src/app.py")
        self.write_context(task_root=str(task_root))
        self.allow(self.write_tool("s1", "src/app.py", tool_id="write-2"))

    def test_untracked_write_requires_baseline(self) -> None:
        # 手工任务根下未跟踪目标同样必须要求基线（自动快照路径另见 test_baseline_auto）。
        self.use_manual_baseline()
        (self.repo / "notes.txt").write_text("user notes\n", encoding="utf-8")
        self.activate()
        self.deny(self.write_tool("s1", "notes.txt"))
        task_root = self.make_task_root()
        self.save_baseline(task_root, "notes.txt")
        self.write_context(task_root=str(task_root))
        self.allow(self.write_tool("s1", "notes.txt", tool_id="write-2"))

    def test_clean_and_absent_writes_do_not_need_task_root(self) -> None:
        self.activate()
        self.allow(self.write_tool("s1", "src/app.py", tool_id="clean"))
        self.allow(self.write_tool("s1", "src/new_file.py", tool_id="absent"))

    def test_baseline_permissions_and_links_are_verified_read_only(self) -> None:
        self.touch_dirty("src/app.py")
        self.activate()
        task_root = self.make_task_root()
        backup = self.save_baseline(task_root, "src/app.py", file_mode=0o644)
        self.write_context(task_root=str(task_root))
        self.deny(self.write_tool("s1", "src/app.py"))
        # Hook 不修改备份内容或权限。
        assert stat.S_IMODE(os.stat(backup).st_mode) == 0o644
        assert backup.read_text(encoding="utf-8") == "value = 2\n"
        os.chmod(backup, 0o600)
        backup.unlink()
        backup.symlink_to(self.repo / "src" / "app.py")
        self.deny(self.write_tool("s1", "src/app.py"))
        assert backup.is_symlink()

    def test_reused_old_task_root_is_denied_for_content_baseline(self) -> None:
        self.touch_dirty("src/app.py", "value = 2\n")
        task_a = self.make_task_root("task-a")
        backup_a = self.save_baseline(task_a, "src/app.py")
        self.write_context(task_root=str(task_a))
        self.activate("s1")
        self.allow(self.write_tool("s1", "src/app.py", tool_id="w-a"))
        digest = hashlib.sha256(backup_a.read_bytes()).hexdigest()
        mode = stat.S_IMODE(os.stat(backup_a).st_mode)

        task_b = self.make_task_root("task-b")
        self.save_baseline(task_b, "src/app.py")
        self.write_context(task_root=str(task_b))
        self.activate("s1")  # 新 epoch
        self.allow(self.write_tool("s1", "src/app.py", tool_id="w-b"))

        self.write_context(task_root=str(task_a))
        self.activate("s1")  # A -> B -> A，旧备份仍在
        reason = self.deny(self.write_tool("s1", "src/app.py", tool_id="w-a2"))
        assert "旧任务" in reason
        assert hashlib.sha256(backup_a.read_bytes()).hexdigest() == digest
        assert stat.S_IMODE(os.stat(backup_a).st_mode) == mode

        task_c = self.make_task_root("task-c")
        self.save_baseline(task_c, "src/app.py")
        self.write_context(task_root=str(task_c))
        self.activate("s1")
        self.allow(self.write_tool("s1", "src/app.py", tool_id="w-c"))

    def test_clean_or_absent_reuse_of_old_root_is_allowed(self) -> None:
        self.activate("s1")
        task_a = self.make_task_root("task-a")
        self.write_context(task_root=str(task_a))
        self.activate("s1")
        self.allow(self.write_tool("s1", "src/app.py", tool_id="clean-a"))
        self.write_context(task_root=str(task_a))
        self.activate("s1")
        self.allow(self.write_tool("s1", "src/app.py", tool_id="clean-b"))

    def test_first_manual_baseline_has_no_degradation_warning(self) -> None:
        """正常的首次手工任务根不报“状态丢失”：那会把真正的降级信号淹没。"""
        self.touch_dirty("src/app.py", "value = 2\n")
        self.use_manual_baseline()
        self.save_baseline(self.manual_task_root(), "src/app.py")
        self.activate("s1")
        _code, value, stderr = self.run_core(self.write_tool("s1", "src/app.py", tool_id="first"))
        assert not self.is_denied(value), value
        assert "首次运行" not in stderr
        assert "归属索引" not in stderr

    def test_existing_root_missing_baseline_index_warns(self) -> None:
        """读到非空 root 却缺归属索引：旧版本写入或结构丢失，必须披露。"""
        self.activate("s1")
        roots = sorted((self.state / "roots").glob("*.json"))
        assert len(roots) == 1, roots
        payload = json.loads(roots[0].read_text(encoding="utf-8"))
        assert "baseline_roots_seen" in payload
        payload.pop("baseline_roots_seen")
        roots[0].write_text(json.dumps(payload), encoding="utf-8")
        _code, value, stderr = self.run_core(self.shell("s1", "git status", tool_id="index"))
        assert not self.is_denied(value), value
        assert "归属索引" in stderr
        assert "Traceback" not in stderr

    def test_shell_write_target_is_checked(self) -> None:
        self.use_manual_baseline()
        self.touch_dirty("src/app.py")
        self.activate()
        self.deny(self.shell("s1", "printf x >> src/app.py"))
        self.deny(self.shell("s1", "sed -i '' s/1/2/ src/app.py"))

    def test_post_success_then_external_modification_is_denied(self) -> None:
        self.touch_dirty("src/app.py", "value = 2\n")
        task_root = self.make_task_root()
        self.save_baseline(task_root, "src/app.py")
        self.write_context(task_root=str(task_root))
        self.activate()
        self.write_path("s1", "w1", content="value = 3\n")
        self.allow(self.write_tool("s1", "src/app.py", tool_id="w2"))
        # 成功写入之后的外部修改必须拒绝，文件保持外部修改后的内容。
        (self.repo / "src" / "app.py").write_text("value = 4\n", encoding="utf-8")
        reason = self.deny(self.write_tool("s1", "src/app.py", tool_id="w3"))
        assert "外部修改" in reason
        assert (self.repo / "src" / "app.py").read_text(encoding="utf-8") == "value = 4\n"

    def test_reviewer_write_tools_are_denied(self) -> None:
        self.activate("rev", self.reviewer_handoff())
        self.deny(self.write_tool("rev", "src/app.py"))
        self.deny(self.shell("rev", "sed -i '' s/1/2/ src/app.py"))
        self.deny(self.shell("rev", "git add -A"))
        self.deny(self.shell("rev", "pytest -q > results.txt"))

    def test_reviewer_reads_tests_builds_are_allowed(self) -> None:
        self.activate("rev", self.reviewer_handoff())
        for index, command in enumerate(
            (
                "git diff",
                "git status --short",
                "rg TODO src/",
                "pytest -q",
                "python3 -m unittest discover -s tests",
                "npm test",
                "npm run build",
                "echo 'x > y'",
            )
        ):
            self.allow(self.shell("rev", command, tool_id="r-%d" % index))

    def test_reviewer_external_write_and_unknown_tool(self) -> None:
        self.activate("rev", self.reviewer_handoff())
        payload = self.tool_payload("rev", "send_message", {"to": "user"}, tool_id="msg")
        self.deny(payload)
        custom = self.tool_payload("rev", "some_custom_tool", {"x": 1}, tool_id="custom")
        _code, value, stderr = self.run_core(custom)
        assert not self.is_denied(value)
        assert "warning" in stderr

    def test_compound_git_and_reviewer_guards(self) -> None:
        self.activate("s1")
        for index, command in enumerate(
            ("(git reset --hard)", "if true; then git reset --hard; fi")
        ):
            reason = self.deny(self.shell("s1", command, tool_id="compound-%d" % index))
            assert "Git" in reason
        self.activate("rev", self.reviewer_handoff())
        for index, command in enumerate(("(rm -rf src)", "{ rm -rf src; }")):
            reason = self.deny(self.shell("rev", command, tool_id="compound-rev-%d" % index))
            assert "只读" in reason
        # 只读的复合结构不是拒绝面：分组本身不构成写副作用。
        self.allow(self.shell("rev", "(rg -n foo src)", tool_id="compound-readonly"))

    def test_unresolved_compound_has_narrow_reviewer_deny(self) -> None:
        self.activate("s1")
        _code, value, stderr = self.run_core(self.shell("s1", "(", tool_id="unresolved-main"))
        # 普通活动会话只告警放行：窄类别只收紧 reviewer。
        assert not self.is_denied(value), value
        assert "warning" in stderr
        _code, value, _stderr = self.run_core(
            self.shell("s1", "(", tool_id="unresolved-probe"), mode="probe"
        )
        assert not self.is_denied(value), value
        self.deny(self.shell("s1", "git reset --hard", tool_id="unresolved-direct"))
        self.allow(self.shell("s1", "rg foo", tool_id="unresolved-plain"))
        self.activate("rev", self.reviewer_handoff())
        reason = self.deny(self.shell("rev", "(", tool_id="unresolved-rev"))
        assert "复合结构" in reason
        # 对照：普通简单命令与原有只读命令不被这个窄类别牵连。
        for index, command in enumerate(("test -f x", "[ -f x ]", "rg foo")):
            self.allow(self.shell("rev", command, tool_id="unresolved-allow-%d" % index))

    def test_git_denied_after_line_start_continuation(self) -> None:
        """词首续行不得让下一物理行的 `#` 变成词内字符：注释尾部反斜杠不续行，
        第三行的危险命令必须照常进入 Git 判定。"""
        self.activate()
        command = "\\\n# note \\\n" + ("git " + "reset --hard")
        reason = self.deny(self.shell("s1", command, tool_id="cont-git"))
        assert "Git" in reason

    def test_git_denied_in_deep_async_and_pipeline_forms(self) -> None:
        """长 `&&` 链、`&` 异步组、管道与 `!` 组合都不得让 Git 证据消失或抛异常。"""
        self.activate()
        dangerous = "git " + "reset --hard"
        for index, command in enumerate(
            (
                ("true && " * 2000) + dangerous,
                "cd /tmp; { cd /var; } & " + dangerous,
                "cd /tmp && printf x | " + dangerous,
                "! true || " + dangerous,
            )
        ):
            reason = self.deny(self.shell("s1", command, tool_id="deep-git-%d" % index))
            assert "Git" in reason

    def test_git_denied_across_list_operator_newline_and_quoted_esac(self) -> None:
        """`&&`/`||`/`|` 后的换行是续接、引号里的 `esac` 是 pattern：两者都不得
        让危险 Git 从证据里消失。"""
        self.activate()
        for index, command in enumerate(
            (
                "true &&\ngit reset --hard",
                "true ||\ngit reset --hard",
                "true |\ngit reset --hard",
                "case x in 'esac') git reset --hard ;; esac",
            )
        ):
            reason = self.deny(self.shell("s1", command, tool_id="nl-git-%d" % index))
            assert "Git" in reason
        # reviewer 侧：这两种形状都不属于“无法拆解的复合结构”，普通只读命令照常放行。
        self.activate("rev", self.reviewer_handoff())
        for index, command in enumerate(
            ("case $x in 'esac') echo hi ;; esac", "true &&\nrg foo")
        ):
            self.allow(self.shell("rev", command, tool_id="nl-rev-%d" % index))

    def test_force_refspec_without_colon_is_denied(self) -> None:
        self.activate()
        for index, command in enumerate(
            (
                "git push origin +main",
                "git push origin +main:main",
                "git push +feature",
            )
        ):
            reason = self.deny(self.shell("s1", command, tool_id="force-%d" % index))
            assert "强制" in reason
        # dry-run 仍先豁免：只观察不推送。
        for index, command in enumerate(
            ("git push --dry-run origin +main", "git push -n origin +main")
        ):
            self.allow(self.shell("s1", command, tool_id="dry-%d" % index))

    def test_checkout_existing_paths_and_branch_switches(self) -> None:
        self.activate()
        absolute = str(self.repo / "src" / "app.py")
        for index, command in enumerate(
            (
                "git checkout .",
                "git checkout ..",
                "git checkout src/app.py",
                "git checkout docs",
                "git checkout %s" % absolute,
            )
        ):
            reason = self.deny(self.shell("s1", command, tool_id="checkout-path-%d" % index))
            assert "工作区" in reason
        # 没有同名路径的普通分支切换/-b 创建分支/提交表达式保持 allow。
        for index, command in enumerate(
            (
                "git checkout main",
                "git checkout HEAD~1",
                "git checkout -b new main",
                "git checkout -B new main",
                "git checkout --orphan new",
            )
        ):
            self.allow(self.shell("s1", command, tool_id="checkout-branch-%d" % index))

    def test_checkout_effective_cwd_is_preserved(self) -> None:
        (self.repo / "sub").mkdir()
        (self.repo / "sub" / "only_here.py").write_text("x = 1\n", encoding="utf-8")
        self.activate()
        for index, command in enumerate(
            (
                "git -C sub checkout only_here.py",
                "cd sub && git checkout only_here.py",
                "env -C sub git checkout only_here.py",
            )
        ):
            reason = self.deny(self.shell("s1", command, tool_id="checkout-cwd-%d" % index))
            assert "only_here.py" in reason
        # 反例：只在别的目录存在的同名路径不算路径（docs 下没有 src/app.py）。
        self.allow(self.shell("s1", "git -C docs checkout src/app.py", tool_id="checkout-cwd-other"))
        self.deny(self.shell("s1", "git -C src checkout app.py", tool_id="checkout-cwd-own"))

    def test_find_actions_and_sort_output_are_reviewer_writes(self) -> None:
        self.activate("rev", self.reviewer_handoff())
        for index, command in enumerate(
            (
                "find . -name '*.pyc' -delete",
                "find . -exec cat x ;",
                "find src -execdir rm {} \\;",
                "sort -o out.txt in.txt",
                "sort --output=out.txt in.txt",
            )
        ):
            reason = self.deny(self.shell("rev", command, tool_id="find-sort-w-%d" % index))
            assert "只读" in reason
        # 只读模式查询与普通读取保持 allow。
        for index, command in enumerate(
            ("find .", "find . -name '*.py'", "sort in.txt", "rg -n TODO src/")
        ):
            self.allow(self.shell("rev", command, tool_id="find-sort-r-%d" % index))

    def test_literal_dollar_open_does_not_hide_real_git_substitution(self) -> None:
        self.activate("s1")
        command = "printf '%s' \"\\$(\" \"$(git reset --hard)\""
        reason = self.deny(self.shell("s1", command, tool_id="dollar-open"))
        assert "Git" in reason
        for index, literal in enumerate(
            (
                "printf '%s' '$(git reset --hard)'",
                "printf '%s' \"\\$(git reset --hard)\"",
            )
        ):
            self.allow(self.shell("s1", literal, tool_id="dollar-literal-%d" % index))

    def test_filtered_word_substitutions_deny_destructive_git(self) -> None:
        self.activate("s1")
        for index, command in enumerate(
            (
                "printf x >$(printf out; git reset --hard; printf out)",
                "OUT=$(printf out; git reset --hard; printf out) true",
                "env OUT=$(git reset --hard) true",
            )
        ):
            reason = self.deny(self.shell("s1", command, tool_id="filtered-%d" % index))
            assert "Git" in reason
        # 字面量对照：单引号保护的目标不是执行证据，也不是 Git 拒绝。
        self.allow(self.shell("s1", "printf x >'$(git reset --hard)'", tool_id="filtered-literal"))

    def test_nested_substitution_comment_and_newline_preserve_git_guard(self) -> None:
        self.activate("s1")
        for index, command in enumerate(
            (COMMENT_SUBSTITUTION, CONTINUATION_SUBSTITUTION)
        ):
            reason = self.deny(self.shell("s1", command, tool_id="nested-git-%d" % index))
            assert "Git" in reason
        # 内层单引号里的替换是数据：不产生 Git 拒绝，也不作为执行证据。
        self.allow(self.shell("s1", SINGLE_QUOTED_SUBSTITUTION, tool_id="nested-literal"))

    def test_unknown_find_write_warns_and_allows(self) -> None:
        self.activate()
        _code, value, stderr = self.run_core(self.shell("s1", "find . -exec cat x ;", tool_id="find-unknown"))
        assert not self.is_denied(value), value
        assert "warning" in stderr

    def test_unactivated_write_is_allowed(self) -> None:
        self.allow(self.write_tool("plain", "src/app.py"))
        self.allow(self.shell("plain", "git reset --hard"))

    # ---- rsync 的基线口径与已披露能力边界 --------------------------
    def snapshots(self, relative: str) -> list:
        """自动快照落点：`<state_root>/baselines/<task>/<repo>/<相对路径>`。"""
        return sorted((self.state / "baselines").glob("*/*/" + relative))

    def registered_targets(self) -> list:
        """任务状态里登记过的 origin/成功/自动快照/pending 目标键。"""
        keys = []
        for task in task_states(self.state):
            keys.extend((task.get("origins") or {}).keys())
            keys.extend((task.get("last_success") or {}).keys())
            keys.extend((task.get("auto_baseline") or {}).keys())
            for entry in (task.get("pending_writes") or {}).values():
                keys.extend(entry.get("targets") or [])
        return keys

    def test_rsync_directory_sync_warns_without_source_snapshot(self) -> None:
        self.touch_dirty("src/app.py")
        self.activate()
        command = "rsync -a --exclude 'install-context.json' src/ dist/"
        _code, value, stderr = self.run_core(self.shell("s1", command, tool_id="rsync-dir"))
        # 目录目的内容不可枚举：披露后放行（与 `cp -R` 一致），不用目录级快照冒充保护。
        assert not self.is_denied(value), value
        assert "warning" in stderr
        assert not self.snapshots("src/app.py")
        keys = self.registered_targets()
        assert not [key for key in keys if key.endswith("::src/app.py")], keys
        assert not [key for key in keys if key.endswith("::install-context.json")], keys

    def test_rsync_single_file_snapshots_only_destination(self) -> None:
        self.touch_dirty("src/app.py", "value = 2\n")
        (self.repo / "docs" / "guide.md").write_text("user guide\n", encoding="utf-8")
        self.activate()
        self.allow(self.shell("s1", "rsync src/app.py docs/guide.md", tool_id="rsync-file"))
        saved = self.snapshots("docs/guide.md")
        assert len(saved) == 1, saved
        assert saved[0].read_text(encoding="utf-8") == "user guide\n"
        assert stat.S_IMODE(saved[0].stat().st_mode) == 0o600
        # 源只是读取：不为它建快照。
        assert not self.snapshots("src/app.py")
        # 手工任务根：目标缺合规基线必须拒绝，补齐后才放行。
        task_root = self.use_manual_baseline()
        reason = self.deny(self.shell("s1", "rsync src/app.py docs/guide.md", tool_id="rsync-manual"))
        assert "缺少合规首次基线" in reason
        self.save_baseline(task_root, "docs/guide.md", "user guide\n")
        self.allow(
            self.shell("s1", "rsync src/app.py docs/guide.md", tool_id="rsync-manual-2")
        )

    def test_rsync_remove_single_file_snapshots_source_and_destination(self) -> None:
        self.touch_dirty("src/app.py", "value = 2\n")
        (self.repo / "dest").mkdir()
        (self.repo / "dest" / "app.py").write_text("old dest\n", encoding="utf-8")
        self.activate()
        for index, flag in enumerate(("--remove-source-files", "--remove-sent-files")):
            command = "rsync %s src/app.py dest/" % flag
            tool_id = "rsync-remove-%d" % index
            self.allow(self.shell("s1", command, tool_id=tool_id))
            self.allow(
                self.shell("s1", command, tool_id=tool_id, event="post_tool", outcome="success")
            )
        source = self.snapshots("src/app.py")
        assert len(source) == 1, source
        assert source[0].read_text(encoding="utf-8") == "value = 2\n"
        assert stat.S_IMODE(source[0].stat().st_mode) == 0o600
        destination = self.snapshots("dest/app.py")
        assert len(destination) == 1, destination
        assert destination[0].read_text(encoding="utf-8") == "old dest\n"
        # reviewer 对同一条删除源命令明确拒绝。
        self.activate("rev", self.reviewer_handoff())
        self.deny(self.shell("rev", "rsync --remove-source-files src/app.py dest/"))

    def test_rsync_multi_source_remove_discloses_snapshot_gap(self) -> None:
        self.touch_dirty("src/app.py", "value = 2\n")
        (self.repo / "docs" / "guide.md").write_text("user guide\n", encoding="utf-8")
        (self.repo / "dest").mkdir()
        self.activate()
        _code, value, stderr = self.run_core(
            self.shell(
                "s1",
                "rsync --remove-source-files src/app.py docs/guide.md dest/",
                tool_id="rsync-multi-remove",
            )
        )
        # 整段不完整 → 连已知的源删除候选也不快照：当前能力边界，不称已保护。
        assert not self.is_denied(value), value
        assert "warning" in stderr
        assert not self.snapshots("src/app.py")
        assert not self.snapshots("docs/guide.md")

    def test_rsync_fifo_target_is_denied_without_blocking(self) -> None:
        os.mkfifo(self.repo / "pipe.out")
        self.activate()
        reason = self.deny(self.shell("s1", "rsync src/app.py pipe.out", tool_id="rsync-fifo"))
        assert "不是普通文件" in reason
        assert not self.snapshots("pipe.out")

    def test_reviewer_rsync_is_denied(self) -> None:
        self.activate("rev", self.reviewer_handoff())
        for index, command in enumerate(
            (
                "rsync -a src/ dest/",
                "rsync src/app.py newfile",
                "rsync -Q src/ dest/",
                "rsync --files-from=list dest/",
                "rsync --read-batch=batch.bin dest/",
                "rsync -n src/ dest/",
                "rsync --remove-source-files src/app.py dest/",
            )
        ):
            reason = self.deny(self.shell("rev", command, tool_id="rsync-rev-%d" % index))
            assert "只读" in reason
        # 纯读对照：rsync 之外的读取命令仍放行。
        self.allow(self.shell("rev", "cat src/app.py", tool_id="rsync-rev-read"))

    def test_rsync_read_batch_discloses_unenumerated_writes(self) -> None:
        self.touch_dirty("src/app.py", "value = 2\n")
        (self.repo / "dest").mkdir()
        (self.repo / "dest" / "app.py").write_text("dest value\n", encoding="utf-8")
        (self.repo / "batch.bin").write_bytes(b"")
        self.activate()
        for index, command in enumerate(
            (
                "rsync --read-batch=batch.bin src/app.py dest/",
                "rsync --read-batch=batch.bin dest/",
            )
        ):
            _code, value, stderr = self.run_core(
                self.shell("s1", command, tool_id="rsync-batch-%d" % index)
            )
            assert not self.is_denied(value), value
            assert "warning" in stderr
        # 不为伪落点 dest/app.py 宣称完整保护，也不动源快照。
        assert not self.snapshots("dest/app.py")
        assert not self.snapshots("src/app.py")
        keys = self.registered_targets()
        assert not [key for key in keys if key.endswith("::dest/app.py")], keys
        self.activate("rev", self.reviewer_handoff())
        self.deny(self.shell("rev", "rsync --read-batch=batch.bin src/app.py dest/"))

    def test_rsync_batch_write_keeps_snapshot_gap(self) -> None:
        self.touch_dirty("src/app.py", "value = 2\n")
        (self.repo / "dest").mkdir()
        (self.repo / "dest" / "app.py").write_text("dest value\n", encoding="utf-8")
        (self.repo / "batch.bin").write_bytes(b"")
        self.activate()
        for index, command in enumerate(
            (
                "rsync --write-batch=batch.bin src/app.py dest/",
                "rsync --only-write-batch=batch.bin src/app.py dest/",
            )
        ):
            tool_id = "rsync-batch-write-%d" % index
            _code, value, stderr = self.run_core(self.shell("s1", command, tool_id=tool_id))
            # 整段不完整：披露后放行，不为目的/批次/源伪称文件级快照。
            assert not self.is_denied(value), value
            assert "warning" in stderr
            for relative in ("dest/app.py", "src/app.py", "batch.bin", "batch.bin.sh"):
                assert not self.snapshots(relative), relative
            keys = self.registered_targets()
            assert not [key for key in keys if "::dest/app.py" in key], keys
            # 成功 post 仍披露不完整写缺口（同一任务/同一原因只提醒一次），
            # 不推进写序列。
            _code, value, stderr = self.run_core(
                self.shell("s1", command, tool_id=tool_id, event="post_tool", outcome="success")
            )
            assert not self.is_denied(value), value
            assert ("写序列记覆盖缺口" in stderr) == (index == 0), (index, stderr)
            assert not self.snapshots("dest/app.py")


class PiSafetyTests(SafetyMixin, HookCase):
    host = "pi"

    def test_apply_patch_multi_file_checks_every_target(self) -> None:
        self.touch_dirty("src/app.py")
        patch = (
            "*** Begin Patch\n"
            "*** Update File: docs/guide.md\n"
            "@@\n-guide\n+guide2\n"
            "*** Update File: src/app.py\n"
            "@@\n-value = 2\n+value = 3\n"
            "*** End Patch"
        )
        self.activate()
        task_root = self.make_task_root()
        self.save_baseline(task_root, "docs/guide.md")
        self.write_context(task_root=str(task_root))
        reason = self.deny(
            self.tool_payload("s1", "apply_patch", {"command": patch}, tool_id="p1")
        )
        assert "baseline" in reason

    def test_multi_repo_writes_use_each_repo_status(self) -> None:
        other = self.root / "repo2"
        other.mkdir()
        subprocess.run(["git", "-c", "commit.gpgsign=false", "init", "-q", "."], cwd=str(other), check=True, capture_output=True)
        (other / "app.py").write_text("v = 1\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=str(other), check=True, capture_output=True)
        subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", "x"], cwd=str(other), check=True, capture_output=True)
        (other / "app.py").write_text("v = 2\n", encoding="utf-8")
        self.activate()
        task_root = self.make_task_root()
        self.save_baseline(task_root, "src/app.py")
        self.write_context(task_root=str(task_root))
        reason = self.deny(self.write_tool("s1", str(other / "app.py")))
        assert "baseline" in reason
        self.save_baseline(task_root, "app.py", "v = 2\n")
        self.allow(self.write_tool("s1", str(other / "app.py"), tool_id="w2"))

    def test_unknown_shell_structure_warns_and_allows(self) -> None:
        self.activate()
        _code, value, stderr = self.run_core(self.shell("s1", "echo $(git reset --hard)"))
        # $(...) 的字面命令会被递归分类，因此这里是明确 deny，而不是静默放行。
        assert self.is_denied(value), stderr


class AntigravitySafetyTests(SafetyMixin, HookCase):
    host = "antigravity"


class CodexSafetyTests(SafetyMixin, HookCase):
    host = "codex"


class DshSafetyTests(SafetyMixin, HookCase):
    host = "dsh"

    def test_lost_write_args_warn_and_allow(self) -> None:
        self.touch_dirty("src/app.py")
        self.activate()
        payload = self.tool_payload("s1", "write", {"command": ""}, tool_id="lost")
        _code, value, stderr = self.run_core(payload)
        assert not self.is_denied(value)
        assert "warning" in stderr
