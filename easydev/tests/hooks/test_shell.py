"""有限复合结构、单趟替换配对与 cwd 已知性传播的解析器级回归。

这些用例只驱动 `hooks/shell.py` 的分析器：绝不执行被测命令，也不访问真实用户
目录。期望值全部独立写死（真实路径、目标集合、complete 标志），不由被测函数
自己算出。
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from support import HOOKS_ROOT

if str(HOOKS_ROOT) not in sys.path:
    sys.path.insert(0, str(HOOKS_ROOT))

from hooks import shell  # noqa: E402


# 冻结的嵌套替换词法输入：`\` 与真实换行是这里真实存在的两个字节。
COMMENT_SUBSTITUTION = (
    "printf '%s' \"$(printf pre\n"
    "# note \\\n"
    "git reset --hard\n"
    "printf post)\""
)
SINGLE_QUOTED_SUBSTITUTION = "printf '%s' \"$(printf '%s' 'literal\\\n$(git reset --hard)')\""
CONTINUATION_SUBSTITUTION = "printf '%s' \"$(g\\\nit reset --ha\\\nrd)\""


def git_actions(analysis: shell.Analysis) -> List[Tuple[object, List[str]]]:
    return [
        (action.get("sub"), [str(token) for token in action.get("args") or []])
        for action in analysis.git
    ]


def write_targets(analysis: shell.Analysis) -> List[Tuple[object, bool]]:
    return [(item.get("target"), bool(item.get("complete"))) for item in analysis.writes]


def read_paths(analysis: shell.Analysis) -> List[object]:
    return [item.get("path") for item in analysis.reads]


def entry_cwds(entries: List[Dict[str, object]]) -> List[Optional[str]]:
    return [entry.get("cwd") for entry in entries]  # type: ignore[return-value]


class CompoundShellTests(unittest.TestCase):
    """`( list )`、`{ list; }`、if/while/for/case、`!` 与字面量数据的边界。"""

    def test_group_control_flow_and_quoted_data(self) -> None:
        # 子 shell/分组/控制流里的危险 Git 必须被识别，不因分组丢失执行证据。
        for command in (
            "(git reset --hard)",
            "{ git reset --hard; }",
            "if true; then git reset --hard; fi",
            "if false; then true; else git reset --hard; fi",
            "! git reset --hard",
            "(if true; then (git reset --hard); fi)",
        ):
            analysis = shell.analyze(command)
            self.assertEqual(git_actions(analysis), [("reset", ["--hard"])], command)
            self.assertFalse(analysis.compound_unresolved, command)
            self.assertFalse(analysis.opaque, command)
        # 循环/for/case 的体与臂照常分类写目标，不因头部数据被丢弃。
        for command, expected in (
            ("while true; do touch out; done", [("out", True)]),
            ("until false; do touch out; done", [("out", True)]),
            ("for x in a b; do touch out; done", [("out", True)]),
            ('case "$1" in a) touch out ;; *) true ;; esac', [("out", True)]),
        ):
            analysis = shell.analyze(command, cwd="/tmp/base")
            self.assertEqual(write_targets(analysis), expected, command)
            self.assertFalse(analysis.compound_unresolved, command)
        # 引号里的分组符号/关键字是数据，不能升级成语法或执行证据。
        for command in (
            "echo '(git reset --hard)'",
            "printf '%s' '{ rm -rf src; }'",
            "echo then",
            "test -f x",
            "[ -f x ]",
        ):
            analysis = shell.analyze(command, cwd="/tmp/base")
            self.assertEqual(analysis.git, [], command)
            self.assertEqual(analysis.writes, [], command)
            self.assertFalse(analysis.compound_unresolved, command)
        # 无法安全配对/归属的结构必须显式置 compound_unresolved，而不是静默放行。
        for command in (
            "(",
            ")",
            "{ touch out",
            "if true; then touch out",
            "then touch out",
            "case x in a) touch out",
        ):
            analysis = shell.analyze(command, cwd="/tmp/base")
            self.assertTrue(analysis.compound_unresolved, command)
            self.assertTrue(analysis.opaque, command)

    def test_compound_cwd_scopes_and_unknown_merges(self) -> None:
        base = "/tmp/base"
        # 子 shell 的 cd 不外泄；花括号的 cd 传播到组后。
        subshell = shell.analyze("(cd sub; cat app.py); cat src/app.py", cwd=base)
        self.assertEqual(entry_cwds(subshell.reads), [base + "/sub", base])
        self.assertFalse(subshell.read_unknown, subshell.as_dict())
        brace = shell.analyze("{ cd sub; cat app.py; }; cat app.py", cwd=base)
        self.assertEqual(entry_cwds(brace.reads), [base + "/sub", base + "/sub"])
        self.assertFalse(brace.read_unknown, brace.as_dict())
        # 组后重定向按组入口 cwd 打开，组内 cd 不改变该目标位置。
        group = shell.analyze("( cd sub ) > out.txt", cwd=base)
        self.assertEqual(write_targets(group), [("out.txt", True)])
        self.assertEqual(entry_cwds(group.writes), [base])
        # 分支里的 cd 无法证明相同：出口合流为未知，后续相对写不冒充已知目录。
        branch = shell.analyze("if true; then cd a; else cd b; fi; printf x > app.py", cwd=base)
        self.assertTrue(branch.write_unknown, branch.as_dict())
        self.assertEqual(write_targets(branch), [("app.py", False)])
        self.assertEqual(entry_cwds(branch.writes), [None])

    def test_or_cd_exits_merge_before_following_relative_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("a", "b"):
                (Path(tmp) / name).mkdir()
                (Path(tmp) / name / "app.py").write_text("USER\n", encoding="utf-8")
            analysis = shell.analyze("cd a || cd b; printf x > app.py", cwd=tmp)
            self.assertTrue(analysis.write_unknown, analysis.as_dict())
            self.assertEqual(write_targets(analysis), [("app.py", False)])
            self.assertEqual(entry_cwds(analysis.writes), [None])
            # 真实目标由 shell 语义独立写死：可能是 a/app.py 或 b/app.py。
            for name in ("a", "b"):
                self.assertEqual(
                    (Path(tmp) / name / "app.py").read_text(encoding="utf-8"), "USER\n"
                )

    def test_and_cd_rhs_keeps_success_cwd(self) -> None:
        analysis = shell.analyze("cd sub && printf x > app.py", cwd="/tmp/base")
        self.assertEqual(write_targets(analysis), [("app.py", True)])
        self.assertEqual(entry_cwds(analysis.writes), ["/tmp/base/sub"])
        self.assertFalse(analysis.write_unknown, analysis.as_dict())

    def test_until_condition_cd_zero_body_marks_following_write_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "sub").mkdir()
            (Path(tmp) / "sub" / "app.py").write_text("USER-SUB\n", encoding="utf-8")
            self.assertFalse((Path(tmp) / "app.py").exists())
            analysis = shell.analyze(
                "until cd sub; do true; done; printf x > app.py", cwd=tmp
            )
            self.assertFalse(analysis.compound_unresolved, analysis.as_dict())
            self.assertTrue(analysis.write_unknown, analysis.as_dict())
            self.assertEqual(write_targets(analysis), [("app.py", False)])
            self.assertEqual(entry_cwds(analysis.writes), [None])
            # 真实目标独立写死：sub/app.py 原字节不变（这里从不执行命令）。
            self.assertEqual(
                (Path(tmp) / "sub" / "app.py").read_text(encoding="utf-8"), "USER-SUB\n"
            )

    def test_while_and_if_elif_condition_cd_propagates_unknown(self) -> None:
        base = "/tmp/base"
        for command in (
            "while cd sub; do true; done; printf x > app.py",
            "if cd sub; then true; fi; printf x > app.py",
            "if true; then true; elif cd sub; then true; fi; printf x > app.py",
        ):
            analysis = shell.analyze(command, cwd=base)
            self.assertTrue(analysis.write_unknown, (command, analysis.as_dict()))
            self.assertEqual(write_targets(analysis), [("app.py", False)], command)
        # 条件/体里的危险 Git 不会因目录未知而丢失。
        analysis = shell.analyze("while cd sub; do git reset --hard; done", cwd=base)
        self.assertEqual(git_actions(analysis), [("reset", ["--hard"])])
        self.assertEqual(write_targets(analysis), [(None, False)])

    def test_case_selector_substitution_keeps_cwd_local_and_records_writes(self) -> None:
        base = "/tmp/base"
        command = (
            'case "$(cd sub; printf x > app.py; printf token)" in *) true ;; esac;'
            " printf x > app.py"
        )
        analysis = shell.analyze(command, cwd=base)
        # 替换内的写落在 sub（子上下文），最后一段写落在父 cwd。
        self.assertEqual(write_targets(analysis), [("app.py", True), ("app.py", True)])
        self.assertEqual(entry_cwds(analysis.writes), [base + "/sub", base])
        self.assertFalse(analysis.write_unknown, analysis.as_dict())
        self.assertFalse(analysis.compound_unresolved, analysis.as_dict())


class SubstitutionSurfaceTests(unittest.TestCase):
    def test_unclosed_substitutions_return_within_budget(self) -> None:
        # 未闭合替换在隔离进程里按受控时限返回：旧实现为平方级扫描会超时。
        unclosed = '"$(" ' * 10000
        elapsed = self.measure(unclosed, timeout=5.0)
        self.assertLess(elapsed, 0.5, elapsed)
        # 近似入口上限的输入同样设 watchdog，并覆盖“深层全部闭合”的等量输入。
        deep_closed = "echo " + "$(echo " * 5000 + "true" + ")" * 5000
        self.assertLess(self.measure(deep_closed, timeout=8.0), 2.0)
        near_limit = '"$(" ' * 26000
        self.assertLess(self.measure(near_limit[: shell.MAX_COMMAND_BYTES - 1], timeout=8.0), 2.0)

    def measure(self, command: str, timeout: float) -> float:
        script = (
            "import sys, time\n"
            "sys.path.insert(0, %r)\n"
            "from hooks import shell\n"
            "command = %r\n"
            "started = time.monotonic()\n"
            "analysis = shell.analyze(command, cwd='/tmp/base')\n"
            "assert analysis is not None\n"
            "print(time.monotonic() - started)\n"
        ) % (str(HOOKS_ROOT), command)
        try:
            completed = subprocess.run(
                [sys.executable, "-c", script],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            self.fail("analyze 未在 %.1fs 内返回（存在平方级扫描）" % timeout)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return float(completed.stdout.strip())

    def test_unclosed_prefix_does_not_hide_later_complete_substitution(self) -> None:
        command = "printf '%s' \"\\$(\" \"$(git reset --hard)\""
        self.assertIn("git reset --hard", shell._substitution_spans(command)[0])
        self.assertEqual(git_actions(shell.analyze(command)), [("reset", ["--hard"])])
        # 未闭合外层只丢自己，完整内层照旧提升为结果。
        self.assertEqual(
            shell._substitution_spans("$( $(git reset --hard)")[0], ["git reset --hard"]
        )
        self.assertTrue(shell._substitution_spans("$( $(git reset --hard)")[1])
        # 相邻 span 互不重叠且按源码顺序切片。
        self.assertEqual(shell._substitution_spans("$(a) $(b) `c`")[0], ["a", "b", "c"])

    def test_substitution_escape_and_token_boundaries_are_preserved(self) -> None:
        command = "printf '%s' \"\\$(\" \"$(git reset --hard)\""
        self.assertEqual(git_actions(shell.analyze(command)), [("reset", ["--hard"])])
        # 纯字面量、转义与相邻 token 不跨词配对成替换。
        for literal in (
            "printf '%s' '$(git reset --hard)'",
            "printf '%s' \"\\$(git reset --hard)\"",
            "printf '%s' '$' '(git reset --hard)'",
        ):
            self.assertEqual(shell.analyze(literal).git, [], literal)
        # 反引号、嵌套、包装与 shell -c 的新脚本里的真实替换仍要暴露。
        for command in (
            "echo \"`git reset --hard`\"",
            'echo "$(echo $(git reset --hard))"',
            'env X="$(git reset --hard)" true',
            "sh -c 'printf x >$(git reset --hard)'",
        ):
            self.assertEqual(git_actions(shell.analyze(command)), [("reset", ["--hard"])], command)
        # 引号保护的脚本文本在外层不是执行证据（只有真的新脚本阶段才是）。
        self.assertEqual(shell.analyze("printf '%s' 'sh -c \"git reset --hard\"'").git, [])

    def test_nested_substitution_lexical_context_preserves_comments_and_single_quotes(
        self,
    ) -> None:
        # A：内层注释末尾的反斜杠不续行，注释之后的物理行仍是代码。
        stripped_comment = shell._strip_line_continuations(COMMENT_SUBSTITUTION)
        self.assertIn("# note \\\n", stripped_comment)
        self.assertEqual(
            git_actions(shell.analyze(COMMENT_SUBSTITUTION)), [("reset", ["--hard"])]
        )
        # B：内层单引号里的反斜杠 + 真实换行与 `$()` 保持字节，不产生 Git。
        stripped_single = shell._strip_line_continuations(SINGLE_QUOTED_SUBSTITUTION)
        self.assertIn("'literal\\\n$(git reset --hard)'", stripped_single)
        self.assertEqual(shell.analyze(SINGLE_QUOTED_SUBSTITUTION).git, [])
        # C：内层正常位置的续行仍合并命令名和参数。
        stripped_continuation = shell._strip_line_continuations(CONTINUATION_SUBSTITUTION)
        self.assertIn("git reset --hard", stripped_continuation)
        self.assertNotIn("\\\n", stripped_continuation)
        self.assertEqual(
            git_actions(shell.analyze(CONTINUATION_SUBSTITUTION)), [("reset", ["--hard"])]
        )


class EvaluatedWordSubstitutionTests(unittest.TestCase):
    def test_redirect_and_assignment_substitutions_survive_filtering(self) -> None:
        redirect = "printf x >$(printf out; git reset --hard; printf out)"
        analysis = shell.analyze(redirect, cwd="/tmp/base")
        self.assertEqual(git_actions(analysis), [("reset", ["--hard"])])
        # 动态目标不得登记成源码字面路径；写入不完整并披露未知。
        redirects = [
            item for item in analysis.writes if item.get("kind") == "redirect"
        ]
        self.assertEqual([(item.get("target"), item.get("complete")) for item in redirects], [(None, False)])
        self.assertFalse(any("$(" in str(item.get("target")) for item in analysis.writes))
        self.assertTrue(analysis.write_unknown, analysis.as_dict())
        assignment = "OUT=$(printf out; git reset --hard; printf out) true"
        assigned = shell.analyze(assignment, cwd="/tmp/base")
        self.assertEqual(git_actions(assigned), [("reset", ["--hard"])])
        # 纯赋值的执行证据保留，且不被当作新的命令名。
        self.assertEqual(write_targets(assigned), [(None, False)])
        self.assertEqual(assigned.commands[-1], ["true"])

    def test_all_consumed_word_roles_are_scanned(self) -> None:
        matrix = (
            ">$(git reset --hard)",
            "cat <$(git reset --hard)",
            "printf x >&$(git reset --hard)",
            "cat <<<$(git reset --hard)",
            "OUT=$(git reset --hard)",
            "OUT=$(git reset --hard) OTHER=x >out true",
            "env OUT=$(git reset --hard) true",
            'env -C "$(git reset --hard)" true',
            'sudo -u "$(git reset --hard)" true',
            'cd "$(git reset --hard)"',
            "command env OUT=$(git reset --hard) true",
            "sh -c 'OUT=$(git reset --hard) true'",
            "eval 'printf x >$(git reset --hard)'",
            'find . -name "$(git reset --hard)"',
            'git -C "$(git reset --hard)" status',
            'for x in "$(git reset --hard)"; do true; done',
            'case "$(git reset --hard)" in *) true;; esac',
        )
        for command in matrix:
            with self.subTest(command=command):
                actions = git_actions(shell.analyze(command, cwd="/tmp/base"))
                # 命令自身的子命令动作（如 git status）也合法，只要真的识别出 reset。
                self.assertIn(("reset", ["--hard"]), actions, command)

    def test_literal_filtered_words_are_not_execution(self) -> None:
        for command in (
            "printf x >'$(git reset --hard)'",
            "OUT='$(git reset --hard)' true",
            "env X='$(git reset --hard)' true",
            "printf '%s' \"\\$(git reset --hard)\"",
            "printf x >&1",
            "printf x 2>&1",
            "printf x <<'EOF'",
        ):
            self.assertEqual(shell.analyze(command, cwd="/tmp/base").git, [], command)
        # 单引号目标仍是静态字面路径：有写目标，但没有执行证据。
        literal = shell.analyze("printf x >'$(git reset --hard)'", cwd="/tmp/base")
        self.assertEqual(write_targets(literal), [("$(git reset --hard)", True)])
        self.assertEqual(entry_cwds(literal.writes), ["/tmp/base"])


class CwdKnownPropagationTests(unittest.TestCase):
    def test_unknown_cwd_survives_substitution_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "sub").mkdir()
            (Path(tmp) / "sub" / "app.py").write_text("USER-SUB\n", encoding="utf-8")
            self.assertFalse((Path(tmp) / "app.py").exists())
            command = "until cd sub; do true; done; printf '%s' \"$(printf x > app.py)\""
            analysis = shell.analyze(command, cwd=tmp)
            self.assertTrue(analysis.write_unknown, analysis.as_dict())
            self.assertEqual(write_targets(analysis), [("app.py", False)])
            self.assertEqual(entry_cwds(analysis.writes), [None])
            # 真实物理目标独立写死为 sub/app.py，原字节不变。
            self.assertEqual(
                (Path(tmp) / "sub" / "app.py").read_text(encoding="utf-8"), "USER-SUB\n"
            )

    def test_known_cwd_substitution_targets_keep_location(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sub = Path(tmp) / "sub"
            sub.mkdir()
            (sub / "app.py").write_text("SUB\n", encoding="utf-8")
            command = "printf '%s' \"$(cat app.py; printf x > app.py)\""
            analysis = shell.analyze(command, cwd=str(sub))
            self.assertEqual(read_paths(analysis), ["app.py"])
            self.assertEqual(entry_cwds(analysis.reads), [str(sub)])
            self.assertEqual(write_targets(analysis), [("app.py", True)])
            self.assertEqual(entry_cwds(analysis.writes), [str(sub)])
            self.assertFalse(analysis.read_unknown, analysis.as_dict())
            self.assertFalse(analysis.write_unknown, analysis.as_dict())

    def test_default_substitution_read_keeps_request_cwd_fallback(self) -> None:
        # 默认无 cwd 的入口仍是“最外层延后定位”：读取目标不带 cwd 也不标未知，
        # 由调用方按请求 cwd 定位（回放守卫的既有兼容语义）。
        command = "printf '%s' \"$(cat secret.txt)\""
        analysis = shell.analyze(command)
        self.assertEqual(read_paths(analysis), ["secret.txt"])
        self.assertEqual(entry_cwds(analysis.reads), [None])
        self.assertFalse(analysis.read_unknown, analysis.as_dict())
        self.assertFalse(analysis.opaque, analysis.as_dict())
        reads, unresolved = shell.read_command_paths(command)
        self.assertEqual([item.get("path") for item in reads], ["secret.txt"])
        self.assertFalse(unresolved)

    def test_absolute_cd_restores_known_cwd_inside_substitution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            absolute = str(Path(tmp))
            command = "printf '%%s' \"$(cd %s && cat x)\"" % absolute
            analysis = shell.analyze(command, cwd_known=False)
            self.assertEqual(read_paths(analysis), ["x"])
            self.assertEqual(entry_cwds(analysis.reads), [absolute])
            self.assertFalse(analysis.read_unknown, analysis.as_dict())
            write_command = "printf '%%s' \"$(cd %s && printf x > app.py)\"" % absolute
            written = shell.analyze(write_command, cwd_known=False)
            self.assertEqual(write_targets(written), [("app.py", True)])
            self.assertEqual(entry_cwds(written.writes), [absolute])
            self.assertFalse(written.write_unknown, written.as_dict())

    def test_unknown_cwd_absolute_targets_stay_resolved(self) -> None:
        analysis = shell.analyze("cat /abs/x; printf x > /abs/app.py", cwd_known=False)
        self.assertEqual(read_paths(analysis), ["/abs/x"])
        self.assertEqual(entry_cwds(analysis.reads), [None])
        self.assertFalse(analysis.read_unknown, analysis.as_dict())
        self.assertEqual(write_targets(analysis), [("/abs/app.py", True)])
        self.assertFalse(analysis.write_unknown, analysis.as_dict())

    def test_prior_unknown_effect_is_not_cleared_by_absolute_cd(self) -> None:
        analysis = shell.analyze("cat relative; cd /abs && cat x", cwd_known=False)
        self.assertTrue(analysis.read_unknown, analysis.as_dict())
        self.assertEqual(read_paths(analysis), ["relative", "x"])
        self.assertEqual(entry_cwds(analysis.reads), [None, "/abs"])

    def test_wrapper_reparse_preserves_cwd_and_single_merge(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "a").write_text("A\n", encoding="utf-8")
            (Path(tmp) / "b").write_text("B\n", encoding="utf-8")
            command = "env X=\"$(cat a)\" sh -c 'cat b'"
            known = shell.analyze(command, cwd=tmp)
            self.assertEqual(read_paths(known), ["a", "b"])
            self.assertEqual(entry_cwds(known.reads), [tmp, tmp])
            self.assertFalse(known.read_unknown, known.as_dict())
            unknown = shell.analyze(command, cwd_known=False)
            self.assertEqual(read_paths(unknown), ["a", "b"])
            self.assertEqual(entry_cwds(unknown.reads), [None, None])
            self.assertTrue(unknown.read_unknown, unknown.as_dict())
            # 绝对 env -C 恢复定位：不保留外层未知状态。
            restored = shell.analyze("env -C %s sh -c 'cat b'" % tmp, cwd_known=False)
            self.assertEqual(read_paths(restored), ["b"])
            self.assertEqual(entry_cwds(restored.reads), [tmp])
            self.assertFalse(restored.read_unknown, restored.as_dict())
            # read_command_paths 原样透传两种状态，不回落到请求 cwd。
            _reads, resolved = shell.read_command_paths(command, cwd=tmp)
            self.assertFalse(resolved)
            _reads, unresolved = shell.read_command_paths(command, cwd_known=False)
            self.assertTrue(unresolved)


class GitPathTests(unittest.TestCase):
    def test_missing_cwd_does_not_use_process_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "only.txt"
            target.write_text("x\n", encoding="utf-8")
            # 相对 operand 在缺少可信 cwd 时不可判定：不能用进程 cwd 猜目标。
            for known in (True, False):
                action = {
                    "sub": "checkout",
                    "args": ["only.txt"],
                    "cwd": None,
                    "cwd_known": known,
                }
                self.assertIsNone(shell.git_violation(action), action)
            # 进程 cwd 里真实存在的相对名同样不算路径（不读 os.getcwd）。
            marker = "SKILL.md"
            if os.path.lexists(os.path.join(os.getcwd(), marker)):
                action = {
                    "sub": "checkout",
                    "args": [marker],
                    "cwd": None,
                    "cwd_known": True,
                }
                self.assertIsNone(shell.git_violation(action), action)
            # 存在的绝对路径独立可判定。
            absolute = {
                "sub": "checkout",
                "args": [str(target)],
                "cwd": None,
                "cwd_known": False,
            }
            self.assertIn(str(target), shell.git_violation(absolute) or "")
            # `.`/`..` 无 cwd 也直接是路径还原。
            for operand in (".", ".."):
                action = {
                    "sub": "checkout",
                    "args": [operand],
                    "cwd": None,
                    "cwd_known": False,
                }
                self.assertIn(operand, shell.git_violation(action) or "")

    def test_git_actions_follow_consecutive_dash_c(self) -> None:
        actions = shell._git_actions(
            ["git", "-C", "/a", "-C", "b", "reset", "--hard"], cwd="/base"
        )
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["cwd"], "/a/b")
        self.assertTrue(actions[0]["cwd_known"])
        detached = shell._git_actions(["git", "-C", "a", "-C", "b", "reset", "--hard"])
        self.assertIsNone(detached[0]["cwd"])
        self.assertFalse(detached[0]["cwd_known"])
        analysis = shell.analyze("git -C /abs reset --hard", cwd_known=False)
        self.assertEqual(analysis.git[0]["cwd"], "/abs")
        self.assertTrue(analysis.git[0]["cwd_known"])

    def test_condition_cd_does_not_claim_checkout_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            analysis = shell.analyze(
                "until cd sub; do true; done; git checkout app.py", cwd=tmp
            )
            action = analysis.git[0]
            self.assertIsNone(action.get("cwd"))
            self.assertFalse(action.get("cwd_known"))


class FindAndSortTests(unittest.TestCase):
    """find 的写动作与 sort 的显式输出：读写目标必须分开。"""

    def test_find_predicate_values_are_not_actions(self) -> None:
        # `-name -delete` 的模式值不是动作：只读模式查询，既无写也无未知位。
        for command, roots in (
            ("find . -name -delete", ["."]),
            ("find .", ["."]),
            ("find src -name '*.pyc'", ["src"]),
        ):
            analysis = shell.analyze(command)
            self.assertEqual(read_paths(analysis), roots, command)
            self.assertEqual(write_targets(analysis), [], command)
            self.assertFalse(analysis.read_unknown, analysis.as_dict())
            self.assertFalse(analysis.write_unknown, analysis.as_dict())
            self.assertIsNone(shell.reviewer_violation(command), command)

    def test_find_exec_arguments_are_not_read_roots(self) -> None:
        analysis = shell.analyze("find . -exec cat x ;")
        # 被执行程序的参数不是搜索根。
        self.assertEqual(read_paths(analysis), ["."])
        self.assertEqual(write_targets(analysis), [(None, False)])
        self.assertTrue(analysis.read_unknown, analysis.as_dict())
        self.assertTrue(analysis.write_unknown, analysis.as_dict())
        # 终止符缺失：动作参数边界不可证明，写目标仍不完整。
        missing = shell.analyze("find . -exec cat x")
        self.assertEqual(read_paths(missing), ["."])
        self.assertEqual(write_targets(missing), [(None, False)])
        self.assertTrue(missing.read_unknown, missing.as_dict())

    def test_find_exec_actions_mark_both_read_and_write_unknown(self) -> None:
        for command in (
            "find . -exec rm {} +",
            "find . -execdir rm {} \\;",
            "find . -ok rm {} \\;",
        ):
            analysis = shell.analyze(command)
            self.assertEqual(read_paths(analysis), ["."], command)
            self.assertEqual(write_targets(analysis), [(None, False)], command)
            self.assertTrue(analysis.read_unknown, command)
            self.assertTrue(analysis.write_unknown, command)
        # 单独 -delete 只影响写：读取边界仍可证明。
        deleted = shell.analyze("find src -name '*.tmp' -delete")
        self.assertEqual(read_paths(deleted), ["src"])
        self.assertEqual(write_targets(deleted), [(None, False)])
        self.assertFalse(deleted.read_unknown, deleted.as_dict())
        self.assertTrue(deleted.write_unknown, deleted.as_dict())
        # `-name -exec` 里的 `-exec` 是模式值，不设执行标志。
        pattern = shell.analyze("find . -name -exec")
        self.assertEqual(read_paths(pattern), ["."])
        self.assertEqual(write_targets(pattern), [])
        self.assertFalse(pattern.read_unknown, pattern.as_dict())

    def test_sort_separates_output_and_input_operands(self) -> None:
        expected = (
            ("sort -o out.txt in.txt", ["in.txt"], [("out.txt", True)]),
            ("sort -oout.txt in.txt", ["in.txt"], [("out.txt", True)]),
            ("sort --output out.txt in.txt", ["in.txt"], [("out.txt", True)]),
            ("sort --output=out.txt in.txt", ["in.txt"], [("out.txt", True)]),
            # `-t` 吃掉 `-o` 作为分隔符：它不是输出开关。
            ("sort -t -o in", ["in"], []),
            # `--` 之后的 `-o` 是文件名。
            ("sort -- -o", ["-o"], []),
            ("sort -k2,2 -n a.txt b.txt", ["a.txt", "b.txt"], []),
        )
        for command, reads, writes in expected:
            analysis = shell.analyze(command)
            self.assertEqual(read_paths(analysis), reads, command)
            self.assertEqual(write_targets(analysis), writes, command)
            self.assertFalse(analysis.write_unknown, command)
        # 输出值只进写集合；同一名字是真实 operand 时才同时是读取目标。
        analysis = shell.analyze("sort -o out.txt out.txt")
        self.assertEqual(read_paths(analysis), ["out.txt"])
        self.assertEqual(write_targets(analysis), [("out.txt", True)])

    def test_sort_plain_read_and_malformed_output(self) -> None:
        for command in ("sort in.txt", "sort -n in.txt", "sort"):
            self.assertEqual(write_targets(shell.analyze(command)), [], command)
        self.assertEqual(read_paths(shell.analyze("sort in.txt")), ["in.txt"])
        # 无文件或 `-` 是 stdin：不凭空读当前目录。
        self.assertEqual(read_paths(shell.analyze("sort")), [])
        self.assertEqual(read_paths(shell.analyze("sort -")), [])
        # 缺值的 `-o` 不是只读：写目标不完整且位置不明。
        missing = shell.analyze("sort -o")
        self.assertEqual(write_targets(missing), [(None, False)])
        self.assertTrue(missing.write_unknown, missing.as_dict())
        self.assertIsNotNone(shell.reviewer_violation("sort -o"))


class ListSeparatorNewlineTests(unittest.TestCase):
    """`&&`/`||`/`|` 后的换行是续接：后续段必须照常分类，不能变成空命令。"""

    def test_newline_after_list_operator_keeps_following_segment(self) -> None:
        # bash 在 `&&`/`||`/`|` 后继续等待右侧命令；把 `\n` 当空命令会让整条
        # 命令变成“无法归属的复合结构”，危险 Git 也就从 git 证据里消失。
        for command in (
            "true &&\ngit reset --hard",
            "true ||\ngit reset --hard",
            "true |\ngit reset --hard",
            "true &&\n\ngit reset --hard",
            "true &&\n  \tgit reset --hard",
            "true &&\ntrue ||\ngit reset --hard",
        ):
            analysis = shell.analyze(command)
            self.assertEqual(git_actions(analysis), [("reset", ["--hard"])], repr(command))
            self.assertFalse(analysis.compound_unresolved, repr(command))
            self.assertFalse(analysis.opaque, repr(command))

    def test_newline_continuation_keeps_writes_cwd_and_reviewer_targets(self) -> None:
        analysis = shell.analyze("true &&\nprintf x > out.txt", cwd="/tmp/base")
        self.assertEqual(write_targets(analysis), [("out.txt", True)])
        self.assertFalse(analysis.compound_unresolved)
        # 续接段里的 `cd` 仍然作用到同一段的相对写目标。
        analysis = shell.analyze("true &&\ncd sub && printf x > app.py", cwd="/tmp/base")
        self.assertEqual(
            [(item.get("target"), item.get("cwd")) for item in analysis.writes],
            [("app.py", "/tmp/base/sub")],
        )
        self.assertFalse(analysis.compound_unresolved)

    def test_dangling_list_operator_stays_incomplete(self) -> None:
        # 右侧确实为空：`true &&` 是不完整命令，必须继续显式披露。
        for command in ("true &&", "true ||", "true |", "true &&\n", "true &&\n\n  "):
            analysis = shell.analyze(command)
            self.assertTrue(analysis.compound_unresolved, repr(command))
            self.assertTrue(analysis.opaque, repr(command))

    def test_semicolon_newline_is_not_treated_as_continuation(self) -> None:
        # `;` 后换行是空命令：保持既有空段容忍，不引入新的“续接”语义。
        analysis = shell.analyze("true;\n; true")
        self.assertFalse(analysis.compound_unresolved)
        self.assertFalse(analysis.opaque)
        self.assertEqual(analysis.commands, [["true"], ["true"]])


class QuotedKeywordTests(unittest.TestCase):
    """引号/转义里的保留字是普通词：只有裸写才是结束符或结构关键字。"""

    def test_quoted_esac_is_a_case_pattern(self) -> None:
        for command in (
            "case $x in 'esac') echo hi ;; esac",
            'case $x in "esac") echo hi ;; esac',
            "case $x in \\esac) echo hi ;; esac",
            "case $x in 'esac') echo hi ;; 'esac') echo bye ;; esac",
        ):
            analysis = shell.analyze(command)
            self.assertFalse(analysis.compound_unresolved, command)
            self.assertFalse(analysis.opaque, command)
            self.assertEqual(git_actions(analysis), [], command)

    def test_quoted_esac_pattern_body_still_reports_writes_and_git(self) -> None:
        analysis = shell.analyze("case $x in 'esac') git reset --hard ;; esac", cwd="/tmp/base")
        self.assertEqual(git_actions(analysis), [("reset", ["--hard"])])
        self.assertFalse(analysis.compound_unresolved)
        analysis = shell.analyze("case $x in 'esac') touch out ;; esac", cwd="/tmp/base")
        self.assertEqual(write_targets(analysis), [("out", True)])

    def test_quoted_or_escaped_keywords_are_ordinary_words(self) -> None:
        # 命令位置上的引号/转义保留字是普通命令词，不是结构结束符。
        for command in (
            "if true; then 'fi'; fi",
            "if true; then \\fi; fi",
            "for x in a; do 'done'; done",
            "for x in a; do \\done; done",
            "while true; do 'do'; done",
        ):
            analysis = shell.analyze(command, cwd="/tmp/base")
            self.assertFalse(analysis.compound_unresolved, command)
            self.assertFalse(analysis.opaque, command)

    def test_bare_keywords_still_close_their_blocks(self) -> None:
        for command in (
            "if true; then git reset --hard; fi",
            "for x in a b; do git reset --hard; done",
            "case $x in a) git reset --hard ;; esac",
        ):
            analysis = shell.analyze(command, cwd="/tmp/base")
            self.assertEqual(git_actions(analysis), [("reset", ["--hard"])], command)
            self.assertFalse(analysis.compound_unresolved, command)


class ExitStateAndAsyncTests(unittest.TestCase):
    """`!` 取反、管道优先级与 `&` 异步列表的目录状态传播。"""

    def test_negation_swaps_exit_edges(self) -> None:
        # `! cd /outside || cat secret` 的 RHS 从**失败**边进入：`! cd` 成功才失败。
        analysis = shell.analyze("! cd /outside || cat secret.txt", cwd="/tmp/base")
        self.assertEqual(
            [(item.get("path"), item.get("cwd")) for item in analysis.reads],
            [("secret.txt", "/outside")],
        )
        self.assertFalse(analysis.compound_unresolved)
        # `! cd /outside && cat`：RHS 在 `cd` 失败时才运行，仍用入口 cwd。
        analysis = shell.analyze("! cd /outside && cat secret.txt", cwd="/tmp/base")
        self.assertEqual(
            [(item.get("path"), item.get("cwd")) for item in analysis.reads],
            [("secret.txt", "/tmp/base")],
        )

    def test_negation_keeps_write_target_and_cwd(self) -> None:
        # `! cd sub && …`：RHS 在 `cd` **失败**时才运行，入口 cwd 不变。
        analysis = shell.analyze("! cd sub && printf x > app.py", cwd="/tmp/base")
        self.assertEqual(
            [(item.get("target"), item.get("cwd"), bool(item.get("complete"))) for item in analysis.writes],
            [("app.py", "/tmp/base", True)],
        )
        # `! cd sub || …`：RHS 在 `cd` **成功**后运行，落在 /tmp/base/sub。
        analysis = shell.analyze("! cd sub || printf x > app.py", cwd="/tmp/base")
        self.assertEqual(
            [(item.get("target"), item.get("cwd"), bool(item.get("complete"))) for item in analysis.writes],
            [("app.py", "/tmp/base/sub", True)],
        )

    def test_pipeline_binds_tighter_than_andor(self) -> None:
        # `cd /outside && printf x | cat secret` 是 `cd /outside && (printf x | cat secret)`。
        analysis = shell.analyze("cd /outside && printf x | cat secret.txt", cwd="/tmp/base")
        self.assertEqual(
            [(item.get("path"), item.get("cwd")) for item in analysis.reads],
            [("secret.txt", "/outside")],
        )
        self.assertFalse(analysis.compound_unresolved)
        self.assertFalse(analysis.opaque)

    def test_pipeline_rhs_keeps_entry_cwd_when_and_cd_precedes(self) -> None:
        analysis = shell.analyze("cd sub || printf x | cat secret.txt", cwd="/tmp/base")
        # `||` 的 RHS 从 `cd sub` 的失败边进入：入口 cwd。
        self.assertEqual(
            [(item.get("path"), item.get("cwd")) for item in analysis.reads],
            [("secret.txt", "/tmp/base")],
        )

    def test_background_group_does_not_pollute_parent_cwd(self) -> None:
        analysis = shell.analyze(
            "cd /outside; { cd /allowed; } & cat secret.txt", cwd="/tmp/base"
        )
        self.assertEqual(
            [(item.get("path"), item.get("cwd")) for item in analysis.reads],
            [("secret.txt", "/outside")],
        )
        self.assertFalse(analysis.compound_unresolved)
        # 前台花括号分组仍在当前 shell 执行，其 cd 照旧传播（对照组，合法列表分隔符）。
        analysis = shell.analyze(
            "cd /outside; { cd /allowed; }; cat secret.txt", cwd="/tmp/base"
        )
        self.assertEqual(
            [(item.get("path"), item.get("cwd")) for item in analysis.reads],
            [("secret.txt", "/allowed")],
        )

    def test_negated_pipeline_keeps_concrete_cwd_and_reads(self) -> None:
        """`! true | cat a`：读取用入口 cwd，管道 cd 不外泄。

        注意作用域差异：真实 Bash 的 `!` 否定**整条管道**，本分析器只否定紧随的
        单条命令（`(! true) | cat a`）。本分析器不求值退出码、对所有分支都取证，
        因此该差异不隐藏 Git/读写证据，也不改变外层 cwd 的进入边；断言只锁读写与
        cwd，不把这里当成与 Bash 等价的解析。
        """
        analysis = shell.analyze("! true | cat a", cwd="/tmp/base")
        self.assertEqual(
            [(item.get("path"), item.get("cwd")) for item in analysis.reads],
            [("a", "/tmp/base")],
        )
        self.assertFalse(analysis.compound_unresolved)
        self.assertFalse(analysis.opaque)
        # `cd /outside || ! printf x | cat b`：RHS 从 cd 的失败边进入 → 入口 cwd。
        analysis = shell.analyze("cd /outside || ! printf x | cat b", cwd="/tmp/base")
        self.assertEqual(
            [(item.get("path"), item.get("cwd")) for item in analysis.reads],
            [("b", "/tmp/base")],
        )
        self.assertFalse(analysis.compound_unresolved)
        # 管道里带 cd 也不外泄：`cat a | cd /outside | cat b` 之后的读取仍用入口 cwd。
        analysis = shell.analyze("cat a | cd /outside | cat b", cwd="/tmp/base")
        self.assertEqual(
            [(item.get("path"), item.get("cwd")) for item in analysis.reads],
            [("a", "/tmp/base"), ("b", "/tmp/base")],
        )
        self.assertEqual([item.get("path") for item in analysis.reads], ["a", "b"])

    def test_background_group_keeps_write_side_effects(self) -> None:
        analysis = shell.analyze("cd /outside; cd /allowed & printf x > out.txt", cwd="/tmp/base")
        # `&` 之前的 `cd /allowed` 不外泄：其后的写仍以 /outside 为入口。
        self.assertEqual(
            [(item.get("target"), item.get("cwd")) for item in analysis.writes],
            [("out.txt", "/outside")],
        )


class DeepChainTests(unittest.TestCase):
    """极长的 `&&`/`||` 链不得把结构分析变成异常（会 fail-open 放行）。"""

    DANGEROUS = "git " + "reset --hard"

    def test_long_and_chain_keeps_git_evidence(self) -> None:
        for count in (2000, 4000):
            command = ("true && " * count) + self.DANGEROUS
            analysis = shell.analyze(command)
            self.assertEqual(git_actions(analysis), [("reset", ["--hard"])], count)
            self.assertFalse(analysis.compound_unresolved, count)
            self.assertFalse(analysis.opaque, count)

    def test_long_or_and_pipe_chains_do_not_raise(self) -> None:
        analysis = shell.analyze(("true || " * 1500) + self.DANGEROUS)
        self.assertEqual(git_actions(analysis), [("reset", ["--hard"])])
        analysis = shell.analyze(("true | " * 1500) + "cat x")
        self.assertFalse(analysis.compound_unresolved)
        self.assertFalse(analysis.opaque)

    def test_long_chain_inside_loop_body_does_not_raise(self) -> None:
        analysis = shell.analyze("for x in a; do " + ("true && " * 1200) + self.DANGEROUS + "; done")
        self.assertEqual(git_actions(analysis), [("reset", ["--hard"])])
        self.assertFalse(analysis.compound_unresolved)


class ContinuationAtLineStartTests(unittest.TestCase):
    """词首续行不得让下一物理行的 `#` 变成词内字符（注释识别回归）。"""

    def test_continuation_before_comment_keeps_later_command_visible(self) -> None:
        command = "\\\n# note \\\n" + ("git " + "reset --hard")
        prepared = shell._strip_line_continuations(command)
        # 预处理结果固定字节：两处续行只删掉第一处（注释末尾的反斜杠不续行）。
        self.assertEqual(prepared, "# note \\\n" + "git reset --hard")
        analysis = shell.analyze(command)
        self.assertEqual(git_actions(analysis), [("reset", ["--hard"])])
        self.assertFalse(analysis.compound_unresolved)
        self.assertFalse(analysis.opaque)

    def test_plain_continuation_still_joins_words(self) -> None:
        # 对照：普通续行仍按 bash 语义拼接，且不制造注释。
        self.assertEqual(shell._strip_line_continuations("g\\\nit reset --hard"), "git reset --hard")
        analysis = shell.analyze("g\\\nit reset --hard")
        self.assertEqual(git_actions(analysis), [("reset", ["--hard"])])


class BareBraceArgumentTests(unittest.TestCase):
    """花括号只在命令位置是分组；argv 里的裸 `{`/`}` 是普通词。"""

    def test_brace_arguments_are_ordinary_words(self) -> None:
        for command in ("printf '%s' {", "echo }", "echo {", "printf '%s' } { a"):
            analysis = shell.analyze(command, cwd="/tmp/base")
            self.assertFalse(analysis.compound_unresolved, command)
            self.assertFalse(analysis.opaque, command)
            self.assertEqual(analysis.writes, [], command)

    def test_brace_group_at_command_position_still_parses(self) -> None:
        analysis = shell.analyze("{ git " + "reset --hard; }", cwd="/tmp/base")
        self.assertEqual(git_actions(analysis), [("reset", ["--hard"])])
        self.assertFalse(analysis.compound_unresolved)
        analysis = shell.analyze("{ printf x > out.txt; }", cwd="/tmp/base")
        self.assertEqual([item.get("target") for item in analysis.writes], ["out.txt"])

    def test_brace_word_data_forms_are_unaffected(self) -> None:
        for command in ("foo{a,b}", "find . -name '*.py' -exec rm {} ;", "echo {a}"):
            analysis = shell.analyze(command, cwd="/tmp/base")
            self.assertFalse(analysis.compound_unresolved, command)


class SortShortOptionTests(unittest.TestCase):
    """`sort` 短选项集群：`-o` 取值是写目标，其余标准开关不得变成“读取未知”。"""

    def test_clustered_output_is_a_write_target(self) -> None:
        for command in ("sort -roout.txt in.txt", "sort -noout.txt in.txt", "sort -uoout.txt in.txt"):
            analysis = shell.analyze(command, cwd="/tmp/base")
            self.assertEqual([item.get("path") for item in analysis.reads], ["in.txt"], command)
            self.assertEqual(
                [(item.get("target"), bool(item.get("complete"))) for item in analysis.writes],
                [("out.txt", True)],
                command,
            )

    def test_standard_flags_are_not_unknown(self) -> None:
        for flag in ("-n", "-r", "-u", "-g", "-h", "-i", "-s", "-z", "-V", "-c", "-C", "-b", "-d", "-f", "-M", "-m"):
            analysis = shell.analyze("sort %s a.txt b.txt" % flag, cwd="/tmp/base")
            self.assertEqual(
                [item.get("path") for item in analysis.reads], ["a.txt", "b.txt"], flag
            )
            self.assertEqual(analysis.writes, [], flag)
            # 标准开关必须被识别：不得落进“读取未知”的保守分支。
            self.assertFalse(analysis.read_unknown, flag)
            self.assertFalse(analysis.opaque, flag)

    def test_key_and_numeric_keep_both_operands(self) -> None:
        analysis = shell.analyze("sort -k2,2 -n a.txt b.txt", cwd="/tmp/base")
        self.assertEqual([item.get("path") for item in analysis.reads], ["a.txt", "b.txt"])
        self.assertEqual(analysis.writes, [])


class PipelineOrderTests(unittest.TestCase):
    """管道元素按**源码顺序**记录：`a | b | c` 的多段一律 a,b,c。"""

    def test_three_segment_pipeline_records_source_order(self) -> None:
        analysis = shell.analyze("cat a | cat b | cat c", cwd="/tmp/base")
        self.assertEqual([item.get("path") for item in analysis.reads], ["a", "b", "c"])
        self.assertEqual(
            shell.argv_of("cat a | cat b | cat c"),
            [["cat", "a"], ["cat", "b"], ["cat", "c"]],
        )

    def test_tee_outputs_record_source_order(self) -> None:
        analysis = shell.analyze("tee b1 | tee b2 | tee b3", cwd="/tmp/base")
        self.assertEqual([item.get("target") for item in analysis.writes], ["b1", "b2", "b3"])

    def test_pipeline_order_survives_mixed_connectors(self) -> None:
        analysis = shell.analyze(
            "cat a | cat b | cat c && cat d | cat e", cwd="/tmp/base"
        )
        self.assertEqual(
            [item.get("path") for item in analysis.reads], ["a", "b", "c", "d", "e"]
        )
        self.assertEqual(
            shell.argv_of("cat a | cat b | cat c && cat d | cat e"),
            [
                ["cat", "a"],
                ["cat", "b"],
                ["cat", "c"],
                ["cat", "d"],
                ["cat", "e"],
            ],
        )

    def test_four_segment_pipeline_keeps_order_and_long_chains_do_not_raise(self) -> None:
        analysis = shell.analyze("cat 1 | cat 2 | cat 3 | cat 4", cwd="/tmp/base")
        self.assertEqual([item.get("path") for item in analysis.reads], ["1", "2", "3", "4"])
        long_pipeline = ("true | " * 1500) + "cat tail"
        analysis = shell.analyze(long_pipeline)
        self.assertEqual([item.get("path") for item in analysis.reads], ["tail"])
        self.assertFalse(analysis.compound_unresolved)
        self.assertFalse(analysis.opaque)

    def test_long_and_chain_still_keeps_git_evidence(self) -> None:
        dangerous = "git " + "reset --hard"
        for count in (2000, 4000):
            analysis = shell.analyze(("true && " * count) + dangerous)
            self.assertEqual(git_actions(analysis), [("reset", ["--hard"])], count)
            self.assertFalse(analysis.compound_unresolved, count)


class CompoundFlagTests(unittest.TestCase):
    def test_compound_unresolved_is_disclosed_and_merged(self) -> None:
        single = shell.analyze("(")
        self.assertTrue(single.compound_unresolved)
        self.assertTrue(single.opaque)
        self.assertTrue(single.as_dict()["compound_unresolved"])
        merged = shell.Analysis()
        merged.merge(single)
        self.assertTrue(merged.compound_unresolved)
        self.assertFalse(shell.analyze("(true)").compound_unresolved)

    def test_token_raw_source_preserves_quotes_and_escapes(self) -> None:
        tokens, closed = shell.tokenize('echo "a b" c\\ d')
        self.assertTrue(closed)
        self.assertEqual([token.text for token in tokens], ["echo", "a b", "c d"])
        self.assertEqual([token.raw_source for token in tokens], ["echo", '"a b"', "c\\ d"])
        manual = shell.Token("x", False, False)
        self.assertIsNone(manual.raw_source)


if __name__ == "__main__":
    unittest.main()
