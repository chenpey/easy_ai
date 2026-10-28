"""P1：Git 危险动作、基线只读核对与 reviewer 只读。"""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
import unittest

from support import HookCase


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
            self.assertIn("Git", reason)

    def test_documented_alias_boundary_is_not_claimed(self) -> None:
        self.activate()
        self.allow(self.shell("s1", "git -c alias.wipe='reset --hard' wipe"))

    def test_dirty_write_requires_baseline(self) -> None:
        self.touch_dirty("src/app.py")
        self.activate()
        reason = self.deny(self.write_tool("s1", "src/app.py"))
        self.assertIn("baseline", reason)
        task_root = self.make_task_root()
        self.save_baseline(task_root, "src/app.py")
        self.write_context(task_root=str(task_root))
        self.allow(self.write_tool("s1", "src/app.py", tool_id="write-2"))

    def test_untracked_write_requires_baseline(self) -> None:
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
        self.assertEqual(stat.S_IMODE(os.stat(backup).st_mode), 0o644)
        self.assertEqual(backup.read_text(encoding="utf-8"), "value = 2\n")
        os.chmod(backup, 0o600)
        backup.unlink()
        backup.symlink_to(self.repo / "src" / "app.py")
        self.deny(self.write_tool("s1", "src/app.py"))
        self.assertTrue(backup.is_symlink())

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
        self.assertIn("旧任务", reason)
        self.assertEqual(hashlib.sha256(backup_a.read_bytes()).hexdigest(), digest)
        self.assertEqual(stat.S_IMODE(os.stat(backup_a).st_mode), mode)

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

    def test_shell_write_target_is_checked(self) -> None:
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
        self.assertIn("外部修改", reason)
        self.assertEqual((self.repo / "src" / "app.py").read_text(encoding="utf-8"), "value = 4\n")

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
        self.assertFalse(self.is_denied(value))
        self.assertIn("warning", stderr)

    def test_unactivated_write_is_allowed(self) -> None:
        self.allow(self.write_tool("plain", "src/app.py"))
        self.allow(self.shell("plain", "git reset --hard"))


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
        self.assertIn("baseline", reason)

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
        self.assertIn("baseline", reason)
        self.save_baseline(task_root, "app.py", "v = 2\n")
        self.allow(self.write_tool("s1", str(other / "app.py"), tool_id="w2"))

    def test_unknown_shell_structure_warns_and_allows(self) -> None:
        self.activate()
        _code, value, stderr = self.run_core(self.shell("s1", "echo $(git reset --hard)"))
        # $(...) 的字面命令会被递归分类，因此这里是明确 deny，而不是静默放行。
        self.assertTrue(self.is_denied(value), stderr)


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
        self.assertFalse(self.is_denied(value))
        self.assertIn("warning", stderr)


if __name__ == "__main__":
    unittest.main()
