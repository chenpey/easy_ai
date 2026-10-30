"""有限复合结构、单趟替换配对与 cwd 已知性传播的解析器级回归。

这些用例只驱动 `hooks/shell.py` 的分析器：绝不执行被测命令，也不访问真实用户
目录。期望值全部独立写死（真实路径、目标集合、complete 标志），不由被测函数
自己算出。
"""

from __future__ import annotations

import pytest

import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from support import CaseBase, HOOKS_ROOT

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


class CompoundShellTests:
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
            assert git_actions(analysis) == [("reset", ["--hard"])], command
            assert not analysis.compound_unresolved, command
            assert not analysis.opaque, command
        # 循环/for/case 的体与臂照常分类写目标，不因头部数据被丢弃。
        for command, expected in (
            ("while true; do touch out; done", [("out", True)]),
            ("until false; do touch out; done", [("out", True)]),
            ("for x in a b; do touch out; done", [("out", True)]),
            ('case "$1" in a) touch out ;; *) true ;; esac', [("out", True)]),
        ):
            analysis = shell.analyze(command, cwd="/tmp/base")
            assert write_targets(analysis) == expected, command
            assert not analysis.compound_unresolved, command
        # 引号里的分组符号/关键字是数据，不能升级成语法或执行证据。
        for command in (
            "echo '(git reset --hard)'",
            "printf '%s' '{ rm -rf src; }'",
            "echo then",
            "test -f x",
            "[ -f x ]",
        ):
            analysis = shell.analyze(command, cwd="/tmp/base")
            assert analysis.git == [], command
            assert analysis.writes == [], command
            assert not analysis.compound_unresolved, command
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
            assert analysis.compound_unresolved, command
            assert analysis.opaque, command

    def test_compound_cwd_scopes_and_unknown_merges(self) -> None:
        base = "/tmp/base"
        # 子 shell 的 cd 不外泄；花括号的 cd 传播到组后。
        subshell = shell.analyze("(cd sub; cat app.py); cat src/app.py", cwd=base)
        assert entry_cwds(subshell.reads) == [base + "/sub", base]
        assert not subshell.read_unknown, subshell.as_dict()
        brace = shell.analyze("{ cd sub; cat app.py; }; cat app.py", cwd=base)
        assert entry_cwds(brace.reads) == [base + "/sub", base + "/sub"]
        assert not brace.read_unknown, brace.as_dict()
        # 组后重定向按组入口 cwd 打开，组内 cd 不改变该目标位置。
        group = shell.analyze("( cd sub ) > out.txt", cwd=base)
        assert write_targets(group) == [("out.txt", True)]
        assert entry_cwds(group.writes) == [base]
        # 分支里的 cd 无法证明相同：出口合流为未知，后续相对写不冒充已知目录。
        branch = shell.analyze("if true; then cd a; else cd b; fi; printf x > app.py", cwd=base)
        assert branch.write_unknown, branch.as_dict()
        assert write_targets(branch) == [("app.py", False)]
        assert entry_cwds(branch.writes) == [None]

    def test_or_cd_exits_merge_before_following_relative_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("a", "b"):
                (Path(tmp) / name).mkdir()
                (Path(tmp) / name / "app.py").write_text("USER\n", encoding="utf-8")
            analysis = shell.analyze("cd a || cd b; printf x > app.py", cwd=tmp)
            assert analysis.write_unknown, analysis.as_dict()
            assert write_targets(analysis) == [("app.py", False)]
            assert entry_cwds(analysis.writes) == [None]
            # 真实目标由 shell 语义独立写死：可能是 a/app.py 或 b/app.py。
            for name in ("a", "b"):
                assert (Path(tmp) / name / "app.py").read_text(encoding="utf-8") == "USER\n"

    def test_and_cd_rhs_keeps_success_cwd(self) -> None:
        analysis = shell.analyze("cd sub && printf x > app.py", cwd="/tmp/base")
        assert write_targets(analysis) == [("app.py", True)]
        assert entry_cwds(analysis.writes) == ["/tmp/base/sub"]
        assert not analysis.write_unknown, analysis.as_dict()

    def test_until_condition_cd_zero_body_marks_following_write_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "sub").mkdir()
            (Path(tmp) / "sub" / "app.py").write_text("USER-SUB\n", encoding="utf-8")
            assert not (Path(tmp) / "app.py").exists()
            analysis = shell.analyze(
                "until cd sub; do true; done; printf x > app.py", cwd=tmp
            )
            assert not analysis.compound_unresolved, analysis.as_dict()
            assert analysis.write_unknown, analysis.as_dict()
            assert write_targets(analysis) == [("app.py", False)]
            assert entry_cwds(analysis.writes) == [None]
            # 真实目标独立写死：sub/app.py 原字节不变（这里从不执行命令）。
            assert (Path(tmp) / "sub" / "app.py").read_text(encoding="utf-8") == "USER-SUB\n"

    def test_while_and_if_elif_condition_cd_propagates_unknown(self) -> None:
        base = "/tmp/base"
        for command in (
            "while cd sub; do true; done; printf x > app.py",
            "if cd sub; then true; fi; printf x > app.py",
            "if true; then true; elif cd sub; then true; fi; printf x > app.py",
        ):
            analysis = shell.analyze(command, cwd=base)
            assert analysis.write_unknown, (command, analysis.as_dict())
            assert write_targets(analysis) == [("app.py", False)], command
        # 条件/体里的危险 Git 不会因目录未知而丢失。
        analysis = shell.analyze("while cd sub; do git reset --hard; done", cwd=base)
        assert git_actions(analysis) == [("reset", ["--hard"])]
        assert write_targets(analysis) == [(None, False)]

    def test_case_selector_substitution_keeps_cwd_local_and_records_writes(self) -> None:
        base = "/tmp/base"
        command = (
            'case "$(cd sub; printf x > app.py; printf token)" in *) true ;; esac;'
            " printf x > app.py"
        )
        analysis = shell.analyze(command, cwd=base)
        # 替换内的写落在 sub（子上下文），最后一段写落在父 cwd。
        assert write_targets(analysis) == [("app.py", True), ("app.py", True)]
        assert entry_cwds(analysis.writes) == [base + "/sub", base]
        assert not analysis.write_unknown, analysis.as_dict()
        assert not analysis.compound_unresolved, analysis.as_dict()


class SubstitutionSurfaceTests:
    def test_unclosed_substitutions_return_within_budget(self) -> None:
        # 未闭合替换在隔离进程里按受控时限返回：旧实现为平方级扫描会超时。
        unclosed = '"$(" ' * 10000
        elapsed = self.measure(unclosed, timeout=5.0)
        assert elapsed < 0.5, elapsed
        # 近似入口上限的输入同样设 watchdog，并覆盖“深层全部闭合”的等量输入。
        deep_closed = "echo " + "$(echo " * 5000 + "true" + ")" * 5000
        assert self.measure(deep_closed, timeout=8.0) < 2.0
        near_limit = '"$(" ' * 26000
        assert self.measure(near_limit[: shell.MAX_COMMAND_BYTES - 1], timeout=8.0) < 2.0

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
            pytest.fail("analyze 未在 %.1fs 内返回（存在平方级扫描）" % timeout)
        assert completed.returncode == 0, completed.stderr
        return float(completed.stdout.strip())

    def test_unclosed_prefix_does_not_hide_later_complete_substitution(self) -> None:
        command = "printf '%s' \"\\$(\" \"$(git reset --hard)\""
        assert "git reset --hard" in shell._substitution_spans(command)[0]
        assert git_actions(shell.analyze(command)) == [("reset", ["--hard"])]
        # 未闭合外层只丢自己，完整内层照旧提升为结果。
        assert shell._substitution_spans("$( $(git reset --hard)")[0] == ["git reset --hard"]
        assert shell._substitution_spans("$( $(git reset --hard)")[1]
        # 相邻 span 互不重叠且按源码顺序切片。
        assert shell._substitution_spans("$(a) $(b) `c`")[0] == ["a", "b", "c"]

    def test_substitution_escape_and_token_boundaries_are_preserved(self) -> None:
        command = "printf '%s' \"\\$(\" \"$(git reset --hard)\""
        assert git_actions(shell.analyze(command)) == [("reset", ["--hard"])]
        # 纯字面量、转义与相邻 token 不跨词配对成替换。
        for literal in (
            "printf '%s' '$(git reset --hard)'",
            "printf '%s' \"\\$(git reset --hard)\"",
            "printf '%s' '$' '(git reset --hard)'",
        ):
            assert shell.analyze(literal).git == [], literal
        # 反引号、嵌套、包装与 shell -c 的新脚本里的真实替换仍要暴露。
        for command in (
            "echo \"`git reset --hard`\"",
            'echo "$(echo $(git reset --hard))"',
            'env X="$(git reset --hard)" true',
            "sh -c 'printf x >$(git reset --hard)'",
        ):
            assert git_actions(shell.analyze(command)) == [("reset", ["--hard"])], command
        # 引号保护的脚本文本在外层不是执行证据（只有真的新脚本阶段才是）。
        assert shell.analyze("printf '%s' 'sh -c \"git reset --hard\"'").git == []

    def test_nested_substitution_lexical_context_preserves_comments_and_single_quotes(
        self,
    ) -> None:
        # A：内层注释末尾的反斜杠不续行，注释之后的物理行仍是代码。
        stripped_comment = shell._strip_line_continuations(COMMENT_SUBSTITUTION)
        assert "# note \\\n" in stripped_comment
        assert git_actions(shell.analyze(COMMENT_SUBSTITUTION)) == [("reset", ["--hard"])]
        # B：内层单引号里的反斜杠 + 真实换行与 `$()` 保持字节，不产生 Git。
        stripped_single = shell._strip_line_continuations(SINGLE_QUOTED_SUBSTITUTION)
        assert "'literal\\\n$(git reset --hard)'" in stripped_single
        assert shell.analyze(SINGLE_QUOTED_SUBSTITUTION).git == []
        # C：内层正常位置的续行仍合并命令名和参数。
        stripped_continuation = shell._strip_line_continuations(CONTINUATION_SUBSTITUTION)
        assert "git reset --hard" in stripped_continuation
        assert "\\\n" not in stripped_continuation
        assert git_actions(shell.analyze(CONTINUATION_SUBSTITUTION)) == [("reset", ["--hard"])]


class EvaluatedWordSubstitutionTests:
    def test_redirect_and_assignment_substitutions_survive_filtering(self) -> None:
        redirect = "printf x >$(printf out; git reset --hard; printf out)"
        analysis = shell.analyze(redirect, cwd="/tmp/base")
        assert git_actions(analysis) == [("reset", ["--hard"])]
        # 动态目标不得登记成源码字面路径；写入不完整并披露未知。
        redirects = [
            item for item in analysis.writes if item.get("kind") == "redirect"
        ]
        assert [(item.get("target"), item.get("complete")) for item in redirects] == [(None, False)]
        assert not any("$(" in str(item.get("target")) for item in analysis.writes)
        assert analysis.write_unknown, analysis.as_dict()
        assignment = "OUT=$(printf out; git reset --hard; printf out) true"
        assigned = shell.analyze(assignment, cwd="/tmp/base")
        assert git_actions(assigned) == [("reset", ["--hard"])]
        # 纯赋值的执行证据保留，且不被当作新的命令名。
        assert write_targets(assigned) == [(None, False)]
        assert assigned.commands[-1] == ["true"]

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
            actions = git_actions(shell.analyze(command, cwd="/tmp/base"))
            # 命令自身的子命令动作（如 git status）也合法，只要真的识别出 reset。
            assert ("reset", ["--hard"]) in actions, command

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
            assert shell.analyze(command, cwd="/tmp/base").git == [], command
        # 单引号目标仍是静态字面路径：有写目标，但没有执行证据。
        literal = shell.analyze("printf x >'$(git reset --hard)'", cwd="/tmp/base")
        assert write_targets(literal) == [("$(git reset --hard)", True)]
        assert entry_cwds(literal.writes) == ["/tmp/base"]


class CwdKnownPropagationTests:
    def test_unknown_cwd_survives_substitution_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "sub").mkdir()
            (Path(tmp) / "sub" / "app.py").write_text("USER-SUB\n", encoding="utf-8")
            assert not (Path(tmp) / "app.py").exists()
            command = "until cd sub; do true; done; printf '%s' \"$(printf x > app.py)\""
            analysis = shell.analyze(command, cwd=tmp)
            assert analysis.write_unknown, analysis.as_dict()
            assert write_targets(analysis) == [("app.py", False)]
            assert entry_cwds(analysis.writes) == [None]
            # 真实物理目标独立写死为 sub/app.py，原字节不变。
            assert (Path(tmp) / "sub" / "app.py").read_text(encoding="utf-8") == "USER-SUB\n"

    def test_known_cwd_substitution_targets_keep_location(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sub = Path(tmp) / "sub"
            sub.mkdir()
            (sub / "app.py").write_text("SUB\n", encoding="utf-8")
            command = "printf '%s' \"$(cat app.py; printf x > app.py)\""
            analysis = shell.analyze(command, cwd=str(sub))
            assert read_paths(analysis) == ["app.py"]
            assert entry_cwds(analysis.reads) == [str(sub)]
            assert write_targets(analysis) == [("app.py", True)]
            assert entry_cwds(analysis.writes) == [str(sub)]
            assert not analysis.read_unknown, analysis.as_dict()
            assert not analysis.write_unknown, analysis.as_dict()

    def test_default_substitution_read_keeps_request_cwd_fallback(self) -> None:
        # 默认无 cwd 的入口仍是“最外层延后定位”：读取目标不带 cwd 也不标未知，
        # 由调用方按请求 cwd 定位（回放守卫的既有兼容语义）。
        command = "printf '%s' \"$(cat secret.txt)\""
        analysis = shell.analyze(command)
        assert read_paths(analysis) == ["secret.txt"]
        assert entry_cwds(analysis.reads) == [None]
        assert not analysis.read_unknown, analysis.as_dict()
        assert not analysis.opaque, analysis.as_dict()
        reads, unresolved = shell.read_command_paths(command)
        assert [item.get("path") for item in reads] == ["secret.txt"]
        assert not unresolved

    def test_absolute_cd_restores_known_cwd_inside_substitution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            absolute = str(Path(tmp))
            command = "printf '%%s' \"$(cd %s && cat x)\"" % absolute
            analysis = shell.analyze(command, cwd_known=False)
            assert read_paths(analysis) == ["x"]
            assert entry_cwds(analysis.reads) == [absolute]
            assert not analysis.read_unknown, analysis.as_dict()
            write_command = "printf '%%s' \"$(cd %s && printf x > app.py)\"" % absolute
            written = shell.analyze(write_command, cwd_known=False)
            assert write_targets(written) == [("app.py", True)]
            assert entry_cwds(written.writes) == [absolute]
            assert not written.write_unknown, written.as_dict()

    def test_unknown_cwd_absolute_targets_stay_resolved(self) -> None:
        analysis = shell.analyze("cat /abs/x; printf x > /abs/app.py", cwd_known=False)
        assert read_paths(analysis) == ["/abs/x"]
        assert entry_cwds(analysis.reads) == [None]
        assert not analysis.read_unknown, analysis.as_dict()
        assert write_targets(analysis) == [("/abs/app.py", True)]
        assert not analysis.write_unknown, analysis.as_dict()

    def test_prior_unknown_effect_is_not_cleared_by_absolute_cd(self) -> None:
        analysis = shell.analyze("cat relative; cd /abs && cat x", cwd_known=False)
        assert analysis.read_unknown, analysis.as_dict()
        assert read_paths(analysis) == ["relative", "x"]
        assert entry_cwds(analysis.reads) == [None, "/abs"]

    def test_wrapper_reparse_preserves_cwd_and_single_merge(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "a").write_text("A\n", encoding="utf-8")
            (Path(tmp) / "b").write_text("B\n", encoding="utf-8")
            command = "env X=\"$(cat a)\" sh -c 'cat b'"
            known = shell.analyze(command, cwd=tmp)
            assert read_paths(known) == ["a", "b"]
            assert entry_cwds(known.reads) == [tmp, tmp]
            assert not known.read_unknown, known.as_dict()
            unknown = shell.analyze(command, cwd_known=False)
            assert read_paths(unknown) == ["a", "b"]
            assert entry_cwds(unknown.reads) == [None, None]
            assert unknown.read_unknown, unknown.as_dict()
            # 绝对 env -C 恢复定位：不保留外层未知状态。
            restored = shell.analyze("env -C %s sh -c 'cat b'" % tmp, cwd_known=False)
            assert read_paths(restored) == ["b"]
            assert entry_cwds(restored.reads) == [tmp]
            assert not restored.read_unknown, restored.as_dict()
            # read_command_paths 原样透传两种状态，不回落到请求 cwd。
            _reads, resolved = shell.read_command_paths(command, cwd=tmp)
            assert not resolved
            _reads, unresolved = shell.read_command_paths(command, cwd_known=False)
            assert unresolved

    def test_tilde_cd_marks_relative_reads_and_writes_unknown(self) -> None:
        # 去引用后以 `~` 开头即不可静态定位：不展开 HOME/passwd，也不拼 current/~/x。
        base = "/tmp/base"
        for prefix in (
            "cd ~;",
            "cd ~/x;",
            "cd ~user/x;",
            "cd ~root/x;",
            "cd '~/x';",
            'cd "~/x";',
            "cd ~+;",
        ):
            command = prefix + " cat in; printf y > a"
            analysis = shell.analyze(command, cwd=base)
            assert read_paths(analysis) == ["in"], command
            assert entry_cwds(analysis.reads) == [None], command
            assert analysis.read_unknown, command
            assert write_targets(analysis) == [("a", False)], command
            assert entry_cwds(analysis.writes) == [None], command
            assert analysis.write_unknown, command

    def test_tilde_env_chdir_marks_nested_targets_unknown(self) -> None:
        # 第二个接缝：包装器 cwd 不能把 `~` 当相对目录拼到外层 cwd 上。
        base = "/tmp/base"
        for command in (
            "env -C ~ sh -c 'cat in; printf y > a'",
            "env -C '~/x' sh -c 'cat in; printf y > a'",
            "env -C~/x sh -c 'cat in; printf y > a'",
            "env --chdir ~root/x sh -c 'cat in; printf y > a'",
            "env --chdir=~/x sh -c 'cat in; printf y > a'",
        ):
            analysis = shell.analyze(command, cwd=base)
            assert read_paths(analysis) == ["in"], command
            assert entry_cwds(analysis.reads) == [None], command
            assert analysis.read_unknown, command
            assert write_targets(analysis) == [("a", False)], command
            assert entry_cwds(analysis.writes) == [None], command
            assert analysis.write_unknown, command
            # 内层未知不污染包装命令之后的外层 cwd。
            outer = shell.analyze(command + "; cat outer", cwd=base)
            assert read_paths(outer) == ["in", "outer"], command
            assert entry_cwds(outer.reads) == [None, base], command

    def test_tilde_cd_inside_shell_wrappers_stays_local(self) -> None:
        for command in (
            "sh -c 'cd ~/x; cat in; printf y > a'; cat outer",
            "env -C sub sh -c 'cd ~root/x; cat in; printf y > a'; cat outer",
        ):
            analysis = shell.analyze(command, cwd="/tmp/base")
            assert read_paths(analysis) == ["in", "outer"], command
            assert entry_cwds(analysis.reads) == [None, "/tmp/base"], command
            assert analysis.read_unknown, command
            assert write_targets(analysis) == [("a", False)], command
            assert entry_cwds(analysis.writes) == [None], command
            assert analysis.write_unknown, command

    def test_static_cd_and_existing_unknown_targets_keep_behavior(self) -> None:
        base = "/tmp/base"
        for prefix, expected in (
            ("cd .;", base),
            ("cd sub;", base + "/sub"),
            ("cd ..;", "/tmp"),
            ("cd /abs;", "/abs"),
        ):
            command = prefix + " cat in; printf y > a"
            analysis = shell.analyze(command, cwd=base)
            assert entry_cwds(analysis.reads) == [expected], command
            assert write_targets(analysis) == [("a", True)], command
            assert entry_cwds(analysis.writes) == [expected], command
            assert not analysis.read_unknown, command
            assert not analysis.write_unknown, command
        # 既有保守目标保持未知：`-`、`$HOME/x`、转义后文本仍是 `$HOME/x`。
        for prefix in ("cd -;", "cd $HOME/x;", "cd \\$HOME/x;"):
            command = prefix + " cat in; printf y > a"
            analysis = shell.analyze(command, cwd=base)
            assert entry_cwds(analysis.reads) == [None], command
            assert write_targets(analysis) == [("a", False)], command
            assert analysis.write_unknown, command
        # `cd ./~/x` 没有词首 tilde：显式字面相对目录仍可静态定位。
        literal = shell.analyze("cd ./~/x; cat in; printf y > a", cwd=base)
        assert entry_cwds(literal.reads) == [base + "/~/x"]
        assert write_targets(literal) == [("a", True)]
        assert not literal.write_unknown, literal.as_dict()

    def test_tilde_cwd_can_recover_without_clearing_prior_unknowns(self) -> None:
        base = "/tmp/base"
        recovered = shell.analyze("cd ~/x; cd /abs; cat in; printf y > a", cwd=base)
        assert entry_cwds(recovered.reads) == ["/abs"]
        assert write_targets(recovered) == [("a", True)]
        assert entry_cwds(recovered.writes) == ["/abs"]
        assert not recovered.read_unknown, recovered.as_dict()
        assert not recovered.write_unknown, recovered.as_dict()
        # 先前的未知不因后续绝对 cd 被抹掉。
        mixed = shell.analyze("cd ~/x; cat first; cd /abs; cat second", cwd=base)
        assert read_paths(mixed) == ["first", "second"]
        assert entry_cwds(mixed.reads) == [None, "/abs"]
        assert mixed.read_unknown, mixed.as_dict()
        # 绝对读写目标不因前序 tilde cd 变未知或变不完整。
        absolute = shell.analyze("cd ~/x; cat /abs/in; printf y > /abs/a", cwd=base)
        assert read_paths(absolute) == ["/abs/in"]
        assert not absolute.read_unknown, absolute.as_dict()
        assert write_targets(absolute) == [("/abs/a", True)]
        assert not absolute.write_unknown, absolute.as_dict()
        # cd 失败边仍可使用入口 cwd，不因成功边未知而一起抹掉。
        failure = shell.analyze("cd ~/x || cat in", cwd=base)
        assert entry_cwds(failure.reads) == [base]
        assert not failure.read_unknown, failure.as_dict()

    def test_static_env_chdir_keeps_behavior(self) -> None:
        base = "/tmp/base"
        for value, expected in (
            (".", base),
            ("sub", base + "/sub"),
            ("..", "/tmp"),
            ("/abs", "/abs"),
        ):
            command = "env -C %s sh -c 'cat in; printf y > a'" % value
            analysis = shell.analyze(command, cwd=base)
            assert entry_cwds(analysis.reads) == [expected], command
            assert write_targets(analysis) == [("a", True)], command
            assert entry_cwds(analysis.writes) == [expected], command
            assert not analysis.read_unknown, command
            assert not analysis.write_unknown, command
        restored = shell.analyze("env -C /abs sh -c 'cat in'", cwd_known=False)
        assert entry_cwds(restored.reads) == ["/abs"]
        assert not restored.read_unknown, restored.as_dict()


class GitPathTests:
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
                assert shell.git_violation(action) is None, action
            # 进程 cwd 里真实存在的相对名同样不算路径（不读 os.getcwd）。
            marker = "SKILL.md"
            if os.path.lexists(os.path.join(os.getcwd(), marker)):
                action = {
                    "sub": "checkout",
                    "args": [marker],
                    "cwd": None,
                    "cwd_known": True,
                }
                assert shell.git_violation(action) is None, action
            # 存在的绝对路径独立可判定。
            absolute = {
                "sub": "checkout",
                "args": [str(target)],
                "cwd": None,
                "cwd_known": False,
            }
            assert str(target) in (shell.git_violation(absolute) or "")
            # `.`/`..` 无 cwd 也直接是路径还原。
            for operand in (".", ".."):
                action = {
                    "sub": "checkout",
                    "args": [operand],
                    "cwd": None,
                    "cwd_known": False,
                }
                assert operand in (shell.git_violation(action) or "")

    def test_git_actions_follow_consecutive_dash_c(self) -> None:
        actions = shell._git_actions(
            ["git", "-C", "/a", "-C", "b", "reset", "--hard"], cwd="/base"
        )
        assert len(actions) == 1
        assert actions[0]["cwd"] == "/a/b"
        assert actions[0]["cwd_known"]
        detached = shell._git_actions(["git", "-C", "a", "-C", "b", "reset", "--hard"])
        assert detached[0]["cwd"] is None
        assert not detached[0]["cwd_known"]
        analysis = shell.analyze("git -C /abs reset --hard", cwd_known=False)
        assert analysis.git[0]["cwd"] == "/abs"
        assert analysis.git[0]["cwd_known"]

    def test_condition_cd_does_not_claim_checkout_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            analysis = shell.analyze(
                "until cd sub; do true; done; git checkout app.py", cwd=tmp
            )
            action = analysis.git[0]
            assert action.get("cwd") is None
            assert not action.get("cwd_known")


class FindAndSortTests:
    """find 的写动作与 sort 的显式输出：读写目标必须分开。"""

    def test_find_predicate_values_are_not_actions(self) -> None:
        # `-name -delete` 的模式值不是动作：只读模式查询，既无写也无未知位。
        for command, roots in (
            ("find . -name -delete", ["."]),
            ("find .", ["."]),
            ("find src -name '*.pyc'", ["src"]),
        ):
            analysis = shell.analyze(command)
            assert read_paths(analysis) == roots, command
            assert write_targets(analysis) == [], command
            assert not analysis.read_unknown, analysis.as_dict()
            assert not analysis.write_unknown, analysis.as_dict()
            assert shell.reviewer_violation(command) is None, command

    def test_find_exec_arguments_are_not_read_roots(self) -> None:
        analysis = shell.analyze("find . -exec cat x ;")
        # 被执行程序的参数不是搜索根。
        assert read_paths(analysis) == ["."]
        assert write_targets(analysis) == [(None, False)]
        assert analysis.read_unknown, analysis.as_dict()
        assert analysis.write_unknown, analysis.as_dict()
        # 终止符缺失：动作参数边界不可证明，写目标仍不完整。
        missing = shell.analyze("find . -exec cat x")
        assert read_paths(missing) == ["."]
        assert write_targets(missing) == [(None, False)]
        assert missing.read_unknown, missing.as_dict()

    def test_find_exec_actions_mark_both_read_and_write_unknown(self) -> None:
        for command in (
            "find . -exec rm {} +",
            "find . -execdir rm {} \\;",
            "find . -ok rm {} \\;",
        ):
            analysis = shell.analyze(command)
            assert read_paths(analysis) == ["."], command
            assert write_targets(analysis) == [(None, False)], command
            assert analysis.read_unknown, command
            assert analysis.write_unknown, command
        # 单独 -delete 只影响写：读取边界仍可证明。
        deleted = shell.analyze("find src -name '*.tmp' -delete")
        assert read_paths(deleted) == ["src"]
        assert write_targets(deleted) == [(None, False)]
        assert not deleted.read_unknown, deleted.as_dict()
        assert deleted.write_unknown, deleted.as_dict()
        # `-name -exec` 里的 `-exec` 是模式值，不设执行标志。
        pattern = shell.analyze("find . -name -exec")
        assert read_paths(pattern) == ["."]
        assert write_targets(pattern) == []
        assert not pattern.read_unknown, pattern.as_dict()

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
            assert read_paths(analysis) == reads, command
            assert write_targets(analysis) == writes, command
            assert not analysis.write_unknown, command
        # 输出值只进写集合；同一名字是真实 operand 时才同时是读取目标。
        analysis = shell.analyze("sort -o out.txt out.txt")
        assert read_paths(analysis) == ["out.txt"]
        assert write_targets(analysis) == [("out.txt", True)]

    def test_sort_plain_read_and_malformed_output(self) -> None:
        for command in ("sort in.txt", "sort -n in.txt", "sort"):
            assert write_targets(shell.analyze(command)) == [], command
        assert read_paths(shell.analyze("sort in.txt")) == ["in.txt"]
        # 无文件或 `-` 是 stdin：不凭空读当前目录。
        assert read_paths(shell.analyze("sort")) == []
        assert read_paths(shell.analyze("sort -")) == []
        # 缺值的 `-o` 不是只读：写目标不完整且位置不明。
        missing = shell.analyze("sort -o")
        assert write_targets(missing) == [(None, False)]
        assert missing.write_unknown, missing.as_dict()
        assert shell.reviewer_violation("sort -o") is not None


class ListSeparatorNewlineTests:
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
            assert git_actions(analysis) == [("reset", ["--hard"])], repr(command)
            assert not analysis.compound_unresolved, repr(command)
            assert not analysis.opaque, repr(command)

    def test_newline_continuation_keeps_writes_cwd_and_reviewer_targets(self) -> None:
        analysis = shell.analyze("true &&\nprintf x > out.txt", cwd="/tmp/base")
        assert write_targets(analysis) == [("out.txt", True)]
        assert not analysis.compound_unresolved
        # 续接段里的 `cd` 仍然作用到同一段的相对写目标。
        analysis = shell.analyze("true &&\ncd sub && printf x > app.py", cwd="/tmp/base")
        assert [(item.get("target"), item.get("cwd")) for item in analysis.writes] == [("app.py", "/tmp/base/sub")]
        assert not analysis.compound_unresolved

    def test_dangling_list_operator_stays_incomplete(self) -> None:
        # 右侧确实为空：`true &&` 是不完整命令，必须继续显式披露。
        for command in ("true &&", "true ||", "true |", "true &&\n", "true &&\n\n  "):
            analysis = shell.analyze(command)
            assert analysis.compound_unresolved, repr(command)
            assert analysis.opaque, repr(command)

    def test_semicolon_newline_is_not_treated_as_continuation(self) -> None:
        # `;` 后换行是空命令：保持既有空段容忍，不引入新的“续接”语义。
        analysis = shell.analyze("true;\n; true")
        assert not analysis.compound_unresolved
        assert not analysis.opaque
        assert analysis.commands == [["true"], ["true"]]


class QuotedKeywordTests:
    """引号/转义里的保留字是普通词：只有裸写才是结束符或结构关键字。"""

    def test_quoted_esac_is_a_case_pattern(self) -> None:
        for command in (
            "case $x in 'esac') echo hi ;; esac",
            'case $x in "esac") echo hi ;; esac',
            "case $x in \\esac) echo hi ;; esac",
            "case $x in 'esac') echo hi ;; 'esac') echo bye ;; esac",
        ):
            analysis = shell.analyze(command)
            assert not analysis.compound_unresolved, command
            assert not analysis.opaque, command
            assert git_actions(analysis) == [], command

    def test_quoted_esac_pattern_body_still_reports_writes_and_git(self) -> None:
        analysis = shell.analyze("case $x in 'esac') git reset --hard ;; esac", cwd="/tmp/base")
        assert git_actions(analysis) == [("reset", ["--hard"])]
        assert not analysis.compound_unresolved
        analysis = shell.analyze("case $x in 'esac') touch out ;; esac", cwd="/tmp/base")
        assert write_targets(analysis) == [("out", True)]

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
            assert not analysis.compound_unresolved, command
            assert not analysis.opaque, command

    def test_bare_keywords_still_close_their_blocks(self) -> None:
        for command in (
            "if true; then git reset --hard; fi",
            "for x in a b; do git reset --hard; done",
            "case $x in a) git reset --hard ;; esac",
        ):
            analysis = shell.analyze(command, cwd="/tmp/base")
            assert git_actions(analysis) == [("reset", ["--hard"])], command
            assert not analysis.compound_unresolved, command


class ExitStateAndAsyncTests:
    """`!` 取反、管道优先级与 `&` 异步列表的目录状态传播。"""

    def test_negation_swaps_exit_edges(self) -> None:
        # `! cd /outside || cat secret` 的 RHS 从**失败**边进入：`! cd` 成功才失败。
        analysis = shell.analyze("! cd /outside || cat secret.txt", cwd="/tmp/base")
        assert [(item.get("path"), item.get("cwd")) for item in analysis.reads] == [("secret.txt", "/outside")]
        assert not analysis.compound_unresolved
        # `! cd /outside && cat`：RHS 在 `cd` 失败时才运行，仍用入口 cwd。
        analysis = shell.analyze("! cd /outside && cat secret.txt", cwd="/tmp/base")
        assert [(item.get("path"), item.get("cwd")) for item in analysis.reads] == [("secret.txt", "/tmp/base")]

    def test_negation_keeps_write_target_and_cwd(self) -> None:
        # `! cd sub && …`：RHS 在 `cd` **失败**时才运行，入口 cwd 不变。
        analysis = shell.analyze("! cd sub && printf x > app.py", cwd="/tmp/base")
        assert [(item.get("target"), item.get("cwd"), bool(item.get("complete"))) for item in analysis.writes] == [("app.py", "/tmp/base", True)]
        # `! cd sub || …`：RHS 在 `cd` **成功**后运行，落在 /tmp/base/sub。
        analysis = shell.analyze("! cd sub || printf x > app.py", cwd="/tmp/base")
        assert [(item.get("target"), item.get("cwd"), bool(item.get("complete"))) for item in analysis.writes] == [("app.py", "/tmp/base/sub", True)]

    def test_pipeline_binds_tighter_than_andor(self) -> None:
        # `cd /outside && printf x | cat secret` 是 `cd /outside && (printf x | cat secret)`。
        analysis = shell.analyze("cd /outside && printf x | cat secret.txt", cwd="/tmp/base")
        assert [(item.get("path"), item.get("cwd")) for item in analysis.reads] == [("secret.txt", "/outside")]
        assert not analysis.compound_unresolved
        assert not analysis.opaque

    def test_pipeline_rhs_keeps_entry_cwd_when_and_cd_precedes(self) -> None:
        analysis = shell.analyze("cd sub || printf x | cat secret.txt", cwd="/tmp/base")
        # `||` 的 RHS 从 `cd sub` 的失败边进入：入口 cwd。
        assert [(item.get("path"), item.get("cwd")) for item in analysis.reads] == [("secret.txt", "/tmp/base")]

    def test_background_group_does_not_pollute_parent_cwd(self) -> None:
        analysis = shell.analyze(
            "cd /outside; { cd /allowed; } & cat secret.txt", cwd="/tmp/base"
        )
        assert [(item.get("path"), item.get("cwd")) for item in analysis.reads] == [("secret.txt", "/outside")]
        assert not analysis.compound_unresolved
        # 前台花括号分组仍在当前 shell 执行，其 cd 照旧传播（对照组，合法列表分隔符）。
        analysis = shell.analyze(
            "cd /outside; { cd /allowed; }; cat secret.txt", cwd="/tmp/base"
        )
        assert [(item.get("path"), item.get("cwd")) for item in analysis.reads] == [("secret.txt", "/allowed")]

    def test_negated_pipeline_keeps_concrete_cwd_and_reads(self) -> None:
        """`! true | cat a`：读取用入口 cwd，管道 cd 不外泄。

        注意作用域差异：真实 Bash 的 `!` 否定**整条管道**，本分析器只否定紧随的
        单条命令（`(! true) | cat a`）。本分析器不求值退出码、对所有分支都取证，
        因此该差异不隐藏 Git/读写证据，也不改变外层 cwd 的进入边；断言只锁读写与
        cwd，不把这里当成与 Bash 等价的解析。
        """
        analysis = shell.analyze("! true | cat a", cwd="/tmp/base")
        assert [(item.get("path"), item.get("cwd")) for item in analysis.reads] == [("a", "/tmp/base")]
        assert not analysis.compound_unresolved
        assert not analysis.opaque
        # `cd /outside || ! printf x | cat b`：RHS 从 cd 的失败边进入 → 入口 cwd。
        analysis = shell.analyze("cd /outside || ! printf x | cat b", cwd="/tmp/base")
        assert [(item.get("path"), item.get("cwd")) for item in analysis.reads] == [("b", "/tmp/base")]
        assert not analysis.compound_unresolved
        # 管道里带 cd 也不外泄：`cat a | cd /outside | cat b` 之后的读取仍用入口 cwd。
        analysis = shell.analyze("cat a | cd /outside | cat b", cwd="/tmp/base")
        assert [(item.get("path"), item.get("cwd")) for item in analysis.reads] == [("a", "/tmp/base"), ("b", "/tmp/base")]
        assert [item.get("path") for item in analysis.reads] == ["a", "b"]

    def test_background_group_keeps_write_side_effects(self) -> None:
        analysis = shell.analyze("cd /outside; cd /allowed & printf x > out.txt", cwd="/tmp/base")
        # `&` 之前的 `cd /allowed` 不外泄：其后的写仍以 /outside 为入口。
        assert [(item.get("target"), item.get("cwd")) for item in analysis.writes] == [("out.txt", "/outside")]


class DeepChainTests:
    """极长的 `&&`/`||` 链不得把结构分析变成异常（会 fail-open 放行）。"""

    DANGEROUS = "git " + "reset --hard"

    def test_long_and_chain_keeps_git_evidence(self) -> None:
        for count in (2000, 4000):
            command = ("true && " * count) + self.DANGEROUS
            analysis = shell.analyze(command)
            assert git_actions(analysis) == [("reset", ["--hard"])], count
            assert not analysis.compound_unresolved, count
            assert not analysis.opaque, count

    def test_long_or_and_pipe_chains_do_not_raise(self) -> None:
        analysis = shell.analyze(("true || " * 1500) + self.DANGEROUS)
        assert git_actions(analysis) == [("reset", ["--hard"])]
        analysis = shell.analyze(("true | " * 1500) + "cat x")
        assert not analysis.compound_unresolved
        assert not analysis.opaque

    def test_long_chain_inside_loop_body_does_not_raise(self) -> None:
        analysis = shell.analyze("for x in a; do " + ("true && " * 1200) + self.DANGEROUS + "; done")
        assert git_actions(analysis) == [("reset", ["--hard"])]
        assert not analysis.compound_unresolved


class ContinuationAtLineStartTests:
    """词首续行不得让下一物理行的 `#` 变成词内字符（注释识别回归）。"""

    def test_continuation_before_comment_keeps_later_command_visible(self) -> None:
        command = "\\\n# note \\\n" + ("git " + "reset --hard")
        prepared = shell._strip_line_continuations(command)
        # 预处理结果固定字节：两处续行只删掉第一处（注释末尾的反斜杠不续行）。
        assert prepared == ("# note \\\n" + "git reset --hard")
        analysis = shell.analyze(command)
        assert git_actions(analysis) == [("reset", ["--hard"])]
        assert not analysis.compound_unresolved
        assert not analysis.opaque

    def test_plain_continuation_still_joins_words(self) -> None:
        # 对照：普通续行仍按 bash 语义拼接，且不制造注释。
        assert shell._strip_line_continuations("g\\\nit reset --hard") == "git reset --hard"
        analysis = shell.analyze("g\\\nit reset --hard")
        assert git_actions(analysis) == [("reset", ["--hard"])]


class BareBraceArgumentTests:
    """花括号只在命令位置是分组；argv 里的裸 `{`/`}` 是普通词。"""

    def test_brace_arguments_are_ordinary_words(self) -> None:
        for command in ("printf '%s' {", "echo }", "echo {", "printf '%s' } { a"):
            analysis = shell.analyze(command, cwd="/tmp/base")
            assert not analysis.compound_unresolved, command
            assert not analysis.opaque, command
            assert analysis.writes == [], command

    def test_brace_group_at_command_position_still_parses(self) -> None:
        analysis = shell.analyze("{ git " + "reset --hard; }", cwd="/tmp/base")
        assert git_actions(analysis) == [("reset", ["--hard"])]
        assert not analysis.compound_unresolved
        analysis = shell.analyze("{ printf x > out.txt; }", cwd="/tmp/base")
        assert [item.get("target") for item in analysis.writes] == ["out.txt"]

    def test_brace_word_data_forms_are_unaffected(self) -> None:
        for command in ("foo{a,b}", "find . -name '*.py' -exec rm {} ;", "echo {a}"):
            analysis = shell.analyze(command, cwd="/tmp/base")
            assert not analysis.compound_unresolved, command


class SortShortOptionTests:
    """`sort` 短选项集群：`-o` 取值是写目标，其余标准开关不得变成“读取未知”。"""

    def test_clustered_output_is_a_write_target(self) -> None:
        for command in ("sort -roout.txt in.txt", "sort -noout.txt in.txt", "sort -uoout.txt in.txt"):
            analysis = shell.analyze(command, cwd="/tmp/base")
            assert [item.get("path") for item in analysis.reads] == ["in.txt"], command
            assert [(item.get("target"), bool(item.get("complete"))) for item in analysis.writes] == [("out.txt", True)], command

    def test_standard_flags_are_not_unknown(self) -> None:
        for flag in ("-n", "-r", "-u", "-g", "-h", "-i", "-s", "-z", "-V", "-c", "-C", "-b", "-d", "-f", "-M", "-m"):
            analysis = shell.analyze("sort %s a.txt b.txt" % flag, cwd="/tmp/base")
            assert [item.get("path") for item in analysis.reads] == ["a.txt", "b.txt"], flag
            assert analysis.writes == [], flag
            # 标准开关必须被识别：不得落进“读取未知”的保守分支。
            assert not analysis.read_unknown, flag
            assert not analysis.opaque, flag

    def test_key_and_numeric_keep_both_operands(self) -> None:
        analysis = shell.analyze("sort -k2,2 -n a.txt b.txt", cwd="/tmp/base")
        assert [item.get("path") for item in analysis.reads] == ["a.txt", "b.txt"]
        assert analysis.writes == []


class PipelineOrderTests:
    """管道元素按**源码顺序**记录：`a | b | c` 的多段一律 a,b,c。"""

    def test_three_segment_pipeline_records_source_order(self) -> None:
        analysis = shell.analyze("cat a | cat b | cat c", cwd="/tmp/base")
        assert [item.get("path") for item in analysis.reads] == ["a", "b", "c"]
        assert shell.argv_of("cat a | cat b | cat c") == [["cat", "a"], ["cat", "b"], ["cat", "c"]]

    def test_tee_outputs_record_source_order(self) -> None:
        analysis = shell.analyze("tee b1 | tee b2 | tee b3", cwd="/tmp/base")
        assert [item.get("target") for item in analysis.writes] == ["b1", "b2", "b3"]

    def test_pipeline_order_survives_mixed_connectors(self) -> None:
        analysis = shell.analyze(
            "cat a | cat b | cat c && cat d | cat e", cwd="/tmp/base"
        )
        assert [item.get("path") for item in analysis.reads] == ["a", "b", "c", "d", "e"]
        assert shell.argv_of("cat a | cat b | cat c && cat d | cat e") == [
                ["cat", "a"],
                ["cat", "b"],
                ["cat", "c"],
                ["cat", "d"],
                ["cat", "e"],
            ]

    def test_four_segment_pipeline_keeps_order_and_long_chains_do_not_raise(self) -> None:
        analysis = shell.analyze("cat 1 | cat 2 | cat 3 | cat 4", cwd="/tmp/base")
        assert [item.get("path") for item in analysis.reads] == ["1", "2", "3", "4"]
        long_pipeline = ("true | " * 1500) + "cat tail"
        analysis = shell.analyze(long_pipeline)
        assert [item.get("path") for item in analysis.reads] == ["tail"]
        assert not analysis.compound_unresolved
        assert not analysis.opaque

    def test_long_and_chain_still_keeps_git_evidence(self) -> None:
        dangerous = "git " + "reset --hard"
        for count in (2000, 4000):
            analysis = shell.analyze(("true && " * count) + dangerous)
            assert git_actions(analysis) == [("reset", ["--hard"])], count
            assert not analysis.compound_unresolved, count


class CompoundFlagTests:
    def test_compound_unresolved_is_disclosed_and_merged(self) -> None:
        single = shell.analyze("(")
        assert single.compound_unresolved
        assert single.opaque
        assert single.as_dict()["compound_unresolved"]
        merged = shell.Analysis()
        merged.merge(single)
        assert merged.compound_unresolved
        assert not shell.analyze("(true)").compound_unresolved

    def test_token_raw_source_preserves_quotes_and_escapes(self) -> None:
        tokens, closed = shell.tokenize('echo "a b" c\\ d')
        assert closed
        assert [token.text for token in tokens] == ["echo", "a b", "c d"]
        assert [token.raw_source for token in tokens] == ["echo", '"a b"', "c\\ d"]
        manual = shell.Token("x", False, False)
        assert manual.raw_source is None


class RsyncLayoutTests(CaseBase):
    """rsync 独立布局：选项值不是源，目的/源按 9 行决策表分派。

    rsync 不是 cp：`-t` 是无值的 `--times`、`-T` 吃临时目录，且没有
    `--target-directory`。这些用例只投递字符串驱动分析器，绝不执行 rsync。
    """

    def setup_method(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(dir="/tmp")
        self.defer(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cwd = str(self.root)
        (self.root / "src").mkdir()
        (self.root / "dest").mkdir()
        (self.root / "src" / "f").write_text("src\n", encoding="utf-8")
        (self.root / "f").write_text("file\n", encoding="utf-8")
        (self.root / "a").write_text("a\n", encoding="utf-8")
        (self.root / "b").write_text("b\n", encoding="utf-8")
        (self.root / "existingfile").write_text("old\n", encoding="utf-8")
        (self.root / "dest" / "app.py").write_text("old\n", encoding="utf-8")

    def analyze(self, command: str) -> shell.Analysis:
        return shell.analyze(command, cwd=self.cwd)

    def test_exclude_value_and_directory_source_are_not_writes(self) -> None:
        # 原安装场景：`--exclude` 的值与目录源都曾被记成写目标 → 命中真实目录即误拒。
        analysis = self.analyze("rsync -a --exclude 'install-context.json' src/ dest/")
        assert write_targets(analysis) == [("dest/", False)]
        assert read_paths(analysis) == ["src/"]
        assert [item["kind"] for item in analysis.writes] == ["rsync"]
        assert not analysis.write_unknown
        assert not analysis.read_unknown

    def test_value_options_are_consumed(self) -> None:
        for option in (
            "--exclude",
            "--include",
            "--chmod",
            "--timeout",
            "--bwlimit",
            "--block-size",
            "--log-format",
        ):
            separated = self.analyze("rsync %s VALUE f out" % option)
            assert write_targets(separated) == [("out", True)], option
            assert read_paths(separated) == ["f"], option
            attached = self.analyze("rsync %s=VALUE f out" % option)
            assert write_targets(attached) == [("out", True)], option
            assert read_paths(attached) == ["f"], option
        for option in ("--exclude-from", "--include-from", "--password-file"):
            analysis = self.analyze("rsync %s=patterns f out" % option)
            assert write_targets(analysis) == [("out", True)], option
            assert read_paths(analysis) == ["f", "patterns"], option
        # `--filter` 的 merge 规则可能间接读文件：值不当路径，但仍记读取未知。
        filtered = self.analyze("rsync --filter='- *.tmp' f out")
        assert write_targets(filtered) == [("out", True)]
        assert read_paths(filtered) == ["f"]
        assert filtered.read_unknown
        # 已知带值选项无条件消费下一个词，即使它以 `-` 起头。
        assert write_targets(self.analyze("rsync --exclude --relative f out")) == [("out", True)]
        # 缺值：布局不完整，不把其他 token 退回源。
        missing = self.analyze("rsync --exclude")
        assert write_targets(missing) == [(None, False)]
        assert missing.write_unknown
        # 动态值不是路径候选。
        dynamic = self.analyze("rsync --exclude=$(echo pat) f out")
        assert write_targets(dynamic) == [("out", False)]
        assert read_paths(dynamic) == ["f"]
        # 选项写在位置参数之后，目的仍是最后一个位置参数。
        assert write_targets(self.analyze("rsync f out --exclude=pattern")) == [("out", True)]
        # 参照目录是额外递归读取；只有 --link-dest 影响主目的完整性。
        for option in ("--compare-dest", "--copy-dest"):
            analysis = self.analyze("rsync %s=ref f out" % option)
            assert "ref" in read_paths(analysis), option
            assert "rsync-reference" in [item["kind"] for item in analysis.reads], option
            assert write_targets(analysis) == [("out", True)], option
        link_dest = self.analyze("rsync --link-dest=ref f out")
        assert "ref" in read_paths(link_dest)
        assert write_targets(link_dest) == [("out", False)]

    def test_short_option_sets_and_independent_routing(self) -> None:
        no_value = {
            "0",
            "4",
            "6",
            "8",
            "a",
            "b",
            "c",
            "d",
            "g",
            "h",
            "i",
            "k",
            "l",
            "m",
            "n",
            "o",
            "p",
            "q",
            "r",
            "t",
            "u",
            "v",
            "x",
            "y",
            "z",
            "C",
            "D",
            "E",
            "F",
            "H",
            "I",
            "L",
            "O",
            "P",
            "R",
            "S",
            "V",
            "W",
        }
        assert set(shell._RSYNC_NO_VALUE_LETTERS) == no_value
        assert set(shell._RSYNC_VALUE_LETTERS) == {"e", "f", "B", "T"}
        assert not (shell._RSYNC_NO_VALUE_LETTERS & shell._RSYNC_VALUE_LETTERS)
        # 逐字母分类：默认只是开关，不改变落点与读取；例外独立写死。
        effects = {
            "R": {"complete": False},
            "H": {"complete": False},
            "b": {"complete": False},
            "F": {"read_unknown": True},
            "L": {"follow": True},
            "k": {"follow": True},
        }
        for letter in sorted(no_value):
            expected = effects.get(letter, {})
            analysis = self.analyze("rsync -%s f out" % letter)
            assert write_targets(analysis) == [("out", expected.get("complete", True))], letter
            sources = [item for item in analysis.reads if item["kind"] == "rsync-source"]
            assert len(sources) == 1, letter
            assert sources[0]["follow"] == expected.get("follow", False), letter
            assert analysis.read_unknown == expected.get("read_unknown", False), letter
            assert not analysis.write_unknown, letter
        # 带值字母各自映射到独立长选项，分离与附值两种写法都吃值、不吃源。
        mappings = (
            ("e", "--rsh", "ssh -p 2222"),
            ("f", "--filter", "- *.tmp"),
            ("B", "--block-size", "4096"),
            ("T", "--temp-dir", "scratch"),
        )
        for letter, option, value in mappings:
            separated = shell._rsync_layout(["-" + letter, value, "f", "out"])
            assert separated["values"] == {option: [value]}, letter
            assert separated["sources"] == ["f"], letter
            assert separated["destination"] == "out", letter
            attached = shell._rsync_layout(["-" + letter + value, "f", "out"])
            assert attached["values"] == {option: [value]}, letter
            assert attached["sources"] == ["f"], letter
            assert attached["destination"] == "out", letter
        # `-avze` 集群：后面的词是 --rsh 的值，源与目的不被吃掉。
        cluster = self.analyze("rsync -avze 'ssh -p 2222' f out")
        assert write_targets(cluster) == [("out", True)]
        assert read_paths(cluster) == ["f"]
        assert shell._rsync_layout(["-avze", "ssh -p 2222", "f", "out"])["values"] == {"--rsh": ["ssh -p 2222"]}
        # 白名单之外的字母没有已核实语义：一律未知布局，不猜是否带值。
        for letter in ("Q", "j", "M"):
            unknown = self.analyze("rsync -%s f out" % letter)
            assert write_targets(unknown) == [(None, False)], letter
            assert unknown.write_unknown, letter
            assert unknown.read_unknown, letter

    def test_times_is_flag_and_target_directory_is_unknown(self) -> None:
        # `-t` 是 --times：不吃值，源与目的照常分离（目录源目的不完整）。
        times = self.analyze("rsync -t src dest")
        assert write_targets(times) == [("dest", False)]
        assert read_paths(times) == ["src"]
        # rsync 没有 --target-directory：任何形式都不能套用 cp 的规则。
        for command in (
            "rsync --target-directory dest src",
            "rsync --target-directory=dest -t src",
            "rsync -t --target-directory dest src",
        ):
            analysis = self.analyze(command)
            assert write_targets(analysis) == [(None, False)], command
            assert analysis.write_unknown, command
            assert analysis.read_unknown, command
            violation = shell.reviewer_violation(command, cwd=self.cwd)
            assert violation["kind"] == "write", command

    def test_multi_source_writes_only_destination(self) -> None:
        analysis = self.analyze("rsync a b dest/")
        assert write_targets(analysis) == [("dest/", False)]
        assert read_paths(analysis) == ["a", "b"]
        # 已知目的但文件集不完整，与“目标不可证明”区别开。
        assert not analysis.write_unknown

    def test_files_from_keeps_single_positional_destination(self) -> None:
        single = self.analyze("rsync --files-from=list dest/")
        assert write_targets(single) == [("dest/", False)]
        assert read_paths(single) == ["list"]
        assert single.read_unknown
        assert single.write_unknown
        both = self.analyze("rsync --files-from=list src/ dest/")
        assert write_targets(both) == [("dest/", False)]
        assert read_paths(both) == ["src/", "list"]
        assert both.read_unknown
        # 没有目的时仍是未知写，不退成只读。
        missing = self.analyze("rsync --files-from=list")
        assert write_targets(missing) == [(None, False)]
        assert missing.write_unknown
        # `--files-from=-` 读 stdin：不记名为 `-` 的文件，但仍记读取未知。
        from_stdin = self.analyze("rsync --files-from=- dest/")
        assert read_paths(from_stdin) == []
        assert from_stdin.read_unknown

    def test_read_batch_ignores_explicit_sources_and_keeps_destination(self) -> None:
        (self.root / "batch.bin").write_bytes(b"")
        for command in (
            "rsync --read-batch=batch.bin f dest/",
            "rsync --read-batch=batch.bin dest/",
            "rsync --read-batch batch.bin f dest/",
        ):
            analysis = self.analyze(command)
            assert write_targets(analysis) == [("dest/", False)], command
            assert read_paths(analysis) == ["batch.bin"], command
            assert analysis.read_unknown, command
            assert analysis.write_unknown, command
            # 显式源被 rsync 忽略：既不当读取，也不产生完整的伪落点 dest/f。
            assert "f" not in read_paths(analysis), command
            assert ("dest/f", True) not in write_targets(analysis), command
            violation = shell.reviewer_violation(command, cwd=self.cwd)
            assert violation["kind"] == "write", command
            assert not violation["unknown"], command
        # 被忽略的源也不能当 --remove-source-files 的已知删除源。
        batch_remove = self.analyze("rsync --read-batch=batch.bin --remove-source-files f dest/")
        assert write_targets(batch_remove) == [("dest/", False)]
        # 没有目的才是未知写。
        no_destination = self.analyze("rsync --read-batch=batch.bin")
        assert write_targets(no_destination) == [(None, False)]
        assert no_destination.write_unknown

    def test_single_file_destination_forms(self) -> None:
        # 表 3/4：不存在且无尾斜杠、已存在普通文件都是完整文件目的。
        assert write_targets(self.analyze("rsync f newfile")) == [("newfile", True)]
        assert write_targets(self.analyze("rsync f existingfile")) == [("existingfile", True)]
        # 表 5：已存在目录只展开这一种 basename。
        assert write_targets(self.analyze("rsync f dest/")) == [("dest/f", True)]
        # 表 6：不存在但带尾斜杠 = 目录意图，不伪造 basename。
        assert write_targets(self.analyze("rsync f notexist/")) == [("notexist/", False)]
        # 目的实际是 FIFO 时仍记完整文件目标：交给原指纹检查拒绝，不改成未知绕过。
        os.mkfifo(self.root / "pipe")
        assert write_targets(self.analyze("rsync f pipe")) == [("pipe", True)]

    def test_relative_and_unknown_layouts_do_not_expand_basename(self) -> None:
        relative = self.analyze("rsync -R src/f dest/")
        assert write_targets(relative) == [("dest/", False)]
        assert read_paths(relative) == ["src/f"]
        assert write_targets(self.analyze("rsync --relative src/f dest/"))[0] == ("dest/", False)
        unknown_short = self.analyze("rsync -Q src/f dest/")
        assert write_targets(unknown_short) == [(None, False)]
        unknown_long = self.analyze("rsync --exclude-bin src/f dest/")
        assert write_targets(unknown_long) == [(None, False)]
        assert unknown_long.write_unknown
        remote = self.analyze("rsync src/f host:dest")
        assert write_targets(remote) == [(None, False)]
        assert remote.write_unknown
        # 本地带冒号文件不是远程：必须是显式 ./ 前缀。
        assert read_paths(self.analyze("rsync ./a:b dest/")) == ["./a:b"]
        # `--` 之后全是位置参数：`-Q` 是文件名，不是未知选项。
        assert read_paths(self.analyze("rsync -- -Q out")) == ["-Q"]

    def test_remove_sources_and_auxiliary_writes(self) -> None:
        assert write_targets(self.analyze("rsync --remove-source-files f dest/")) == [("dest/f", True), ("f", True)]
        assert write_targets(self.analyze("rsync --remove-sent-files f dest/")) == [("dest/f", True), ("f", True)]
        # 多源：目的不完整，已知源仍作为删除候选（整段不完整 → 见 safety 边界用例）。
        assert write_targets(self.analyze("rsync --remove-source-files a b dest/")) == [("dest/", False), ("a", True), ("b", True)]
        assert write_targets(self.analyze("rsync --log-file=log f out")) == [("out", True), ("log", True)]
        for option, value in (
            ("--backup-dir", "backup"),
            ("--temp-dir", "scratch"),
            ("--partial-dir", "partial"),
        ):
            analysis = self.analyze("rsync %s=%s f out" % (option, value))
            assert (value, False) in write_targets(analysis), option
            assert analysis.write_unknown, option
        # `-T` 是 --temp-dir 的短别名，值不冒充源或目的。
        short_temp = self.analyze("rsync -T scratch f out")
        assert write_targets(short_temp) == [("out", True), ("scratch", False)]
        assert short_temp.write_unknown

    def test_cwd_and_dynamic_targets(self) -> None:
        (self.root / "sub").mkdir()
        (self.root / "sub" / "f").write_text("sub\n", encoding="utf-8")
        (self.root / "sub" / "out").write_text("old\n", encoding="utf-8")
        tracked = self.analyze("rsync f out")
        assert [item["cwd"] for item in tracked.writes] == [self.cwd]
        inside = self.analyze("cd sub; rsync f out")
        assert write_targets(inside) == [("out", True)]
        assert [item["cwd"] for item in inside.writes] == [str(self.root / "sub")]
        unknown = self.analyze("until cd sub; do true; done; rsync f out")
        assert write_targets(unknown) == [("out", False)]
        assert unknown.write_unknown
        # 动态目的不记表达式源码；静态目的保留为不完整。
        dynamic = self.analyze('rsync f "$DEST"')
        assert write_targets(dynamic) == [(None, False)]
        assert "$DEST" not in [item["target"] for item in dynamic.writes]
        assert write_targets(self.analyze("rsync f '$DEST'")) == [("$DEST", True)]
        assert write_targets(self.analyze("rsync f \\$DEST")) == [("$DEST", True)]
        assert write_targets(self.analyze('rsync f "target"')) == [("target", True)]
        assert write_targets(self.analyze("rsync f dest*")) == [(None, False)]
        absolute = self.analyze("rsync %s %s" % (self.root / "f", self.root / "newfile"))
        assert write_targets(absolute) == [(str(self.root / "newfile"), True)]
        # 替换内的危险 Git 证据不能因 rsync 布局未知而丢失。
        danger = self.analyze("rsync $(git reset --hard) dest/")
        assert git_actions(danger) == [("reset", ["--hard"])]

    def test_batch_write_marks_main_and_batch_targets_incomplete(self) -> None:
        # 批次写的主目的与 FILE/FILE.sh 都不能是完整候选：不含“主项完整”的混合假象。
        for option in ("--write-batch", "--only-write-batch"):
            for command in (
                "rsync %s=batch.bin f existingfile" % option,
                "rsync %s batch.bin f existingfile" % option,
            ):
                analysis = self.analyze(command)
                assert write_targets(analysis) == [("existingfile", False), ("batch.bin", False), ("batch.bin.sh", False)], command
                assert analysis.write_unknown, command
                assert read_paths(analysis) == ["f"], command
            new_file = self.analyze("rsync %s=batch.bin f newfile" % option)
            assert write_targets(new_file) == [("newfile", False), ("batch.bin", False), ("batch.bin.sh", False)], option
            # 已存在的目录目的不展开成看似精确且完整的 dest/f。
            directory = self.analyze("rsync %s=batch.bin f dest/" % option)
            assert write_targets(directory) == [("dest/", False), ("batch.bin", False), ("batch.bin.sh", False)], option
            assert ("dest/f", True) not in write_targets(directory), option

    def test_batch_write_missing_empty_and_dynamic_values_are_unknown(self) -> None:
        for option in ("--write-batch", "--only-write-batch"):
            bare = self.analyze("rsync " + option)
            assert write_targets(bare) == [(None, False)], option
            assert bare.write_unknown, option
            # 选项在位置参数之后且缺值：目的仍不完整，写未知不得漏成 False。
            missing = self.analyze("rsync f out " + option)
            assert write_targets(missing) == [("out", False)], option
            assert missing.write_unknown, option
            # 空值不生成空路径或 `.sh` 文件名，批次项保留 None/False 占位。
            empty = self.analyze("rsync %s= f out" % option)
            assert write_targets(empty) == [("out", False), (None, False)], option
            assert empty.write_unknown, option
        # 动态值不记表达式源码，也不造 `$BATCH.sh`。
        dynamic = self.analyze('rsync --write-batch="$BATCH" f existingfile')
        assert write_targets(dynamic) == [("existingfile", False), (None, False)]
        assert "$BATCH" not in [str(item.get("target")) for item in dynamic.writes]
        assert dynamic.write_unknown
        # 选项作为其他选项的值被消费时不是 batch-write：仍是普通完整布局。
        consumed = self.analyze("rsync --exclude --write-batch f out")
        assert write_targets(consumed) == [("out", True)]
        assert not consumed.write_unknown

    def test_batch_write_does_not_override_read_batch_layout(self) -> None:
        for command in (
            "rsync --write-batch=batch.bin --read-batch=restore.bin f dest/",
            "rsync --read-batch=restore.bin --write-batch=batch.bin f dest/",
            "rsync --only-write-batch=batch.bin --read-batch=restore.bin "
            "--remove-source-files f dest/",
            "rsync --read-batch=restore.bin --only-write-batch=batch.bin "
            "--remove-source-files f dest/",
        ):
            analysis = self.analyze(command)
            assert read_paths(analysis) == ["restore.bin"], command
            assert write_targets(analysis) == [("dest/", False), ("batch.bin", False), ("batch.bin.sh", False)], command
            assert analysis.read_unknown, command
            assert analysis.write_unknown, command
            # read-batch 的显式源既不当读取，也不当已知删除源。
            assert "f" not in read_paths(analysis), command
            assert ("f", True) not in write_targets(analysis), command

    def test_batch_write_preserves_other_auxiliary_candidates(self) -> None:
        analysis = self.analyze(
            "rsync --write-batch=batch.bin --remove-source-files --log-file=log f existingfile"
        )
        # 源删除与日志项是自身可证明的候选；batch-write 只使主目的与批次项不完整。
        assert write_targets(analysis) == [
                ("existingfile", False),
                ("f", True),
                ("log", True),
                ("batch.bin", False),
                ("batch.bin.sh", False),
            ]
        assert analysis.write_unknown

    def test_reviewer_and_read_only_classification(self) -> None:
        for command in (
            "rsync f out",
            "rsync -a src/ dest/",
            "rsync --files-from=list dest/",
            "rsync -Q src/ dest/",
            "rsync -n src/ dest/",
            "rsync --write-batch=batch.bin f dest/",
            "rsync --only-write-batch=batch.bin f dest/",
        ):
            violation = shell.reviewer_violation(command, cwd=self.cwd)
            assert violation is not None, command
            assert violation["kind"] == "write", command
            assert not violation["unknown"], command
        assert shell.reviewer_violation("cat f", cwd=self.cwd) is None
        # is_read_only_command 不接受 cwd：完整文件用例用绝对路径写死期望。
        complete = shell.is_read_only_command(
            "rsync %s %s" % (self.root / "f", self.root / "newfile")
        )
        assert complete == (False, False)
        incomplete = shell.is_read_only_command(
            "rsync %s/ %s/" % (self.root / "src", self.root / "dest")
        )
        assert incomplete == (False, True)
        # batch-write 也披露未知写：不因主候选不完整而被当成部分可判定。
        batch_write = shell.is_read_only_command(
            "rsync --write-batch=%s %s %s"
            % (self.root / "batch.bin", self.root / "f", self.root / "newfile")
        )
        assert batch_write == (False, True)
        plain = shell.is_read_only_command(
            "rsync %s %s" % (self.root / "f", self.root / "newfile")
        )
        assert plain == (False, False)


HEREDOC_DATA_COMMANDS = (
    "cat",
    "tee",
    "wc",
    "sort",
    "uniq",
    "tr",
    "cut",
    "head",
    "tail",
    "nl",
    "od",
    "hexdump",
    "base64",
    "md5sum",
    "sha1sum",
    "sha256sum",
)

# 引用 delimiter 下必须完全当数据的正文：含危险 Git、真实替换、注释、不配对
# 引号与伪重定向，全部只应是字面字节。
QUOTED_HEREDOC_BODY = (
    "git reset --hard\n"
    "$(git reset --hard)\n"
    "`git reset --hard`\n"
    "# $(git reset --hard)\n"
    "it's \"unbalanced\n"
    "> fake\n"
)


class HeredocShellTests:
    """heredoc 正文边界、消费者判定与整次回退的证据保持。

    期望值按 Bash/POSIX 语义与冻结的旧解析结果人工写死，不由被测 tokenizer 反算；
    所有用例只驱动分析器，绝不执行被测命令。
    """

    def assert_legacy_facts(
        self,
        command: str,
        git: List[Tuple[object, List[str]]],
        writes: List[Tuple[object, object, bool]],
        compound: bool = False,
    ) -> None:
        """整次回退时必须与改动前的旧结果一致（opaque、Git、写、窄拒绝标志）。"""
        analysis = shell.analyze(command, cwd="/tmp/base")
        assert analysis.opaque, command
        assert not analysis.too_deep, command
        assert not analysis.stdin_program, command
        assert analysis.compound_unresolved == compound, command
        assert git_actions(analysis) == git, command
        assert [
                (item.get("kind"), item.get("target"), bool(item.get("complete")))
                for item in analysis.writes
            ] == writes, command

    def test_quoted_heredoc_is_data(self) -> None:
        for opener in ("<<'EOF'", '<<"EOF"', "<<\\EOF", '<<E"O"F', "<<E''OF"):
            command = "cat > out.txt %s\n%sEOF" % (opener, QUOTED_HEREDOC_BODY)
            analysis = shell.analyze(command, cwd="/tmp/base")
            assert not analysis.opaque, analysis.as_dict()
            assert not analysis.compound_unresolved
            assert not analysis.stdin_program
            # 正文与结束行都不进 commands，只留外层命令。
            assert analysis.commands == [["cat"]]
            assert analysis.git == []
            assert analysis.reads == []
            # 外层静态重定向照旧记录，cwd 沿用工具目录。
            assert write_targets(analysis) == [("out.txt", True)]
            assert entry_cwds(analysis.writes) == ["/tmp/base"]
        # 空引用 delimiter：定界词存在（不是缺 word），以空行结束。
        empty = shell.analyze("cat <<''\ngit reset --hard\n\n", cwd="/tmp/base")
        assert not empty.opaque, empty.as_dict()
        assert empty.git == []
        assert empty.commands == [["cat"]]
        assert not empty.stdin_program

    def test_unquoted_heredoc_evaluates_substitutions(self) -> None:
        for body in (
            "$(git reset --hard)",
            "`git reset --hard`",
            "'$(git reset --hard)'",
            '"$(git reset --hard)"',
            "# $(git reset --hard)",
        ):
            command = "cat <<EOF\n%s\nEOF" % body
            analysis = shell.analyze(command, cwd="/tmp/base")
            # 正文里的单引号/双引号/`#` 都没有 shell 引号或注释语义。
            assert git_actions(analysis) == [("reset", ["--hard"])]
            assert not analysis.opaque, analysis.as_dict()
            assert not analysis.stdin_program
        # 替换内部的读/写仍按既有语义合并，正文自身不生成命令。
        nested = shell.analyze("cat <<EOF\n$(cat secret; touch child)\nEOF", cwd="/tmp/base")
        assert nested.git == []
        assert read_paths(nested) == ["secret"]
        assert write_targets(nested) == [("child", True)]
        assert ["secret"] not in nested.commands
        # 同一行多个替换与多行替换按源码顺序，各自只合并一次。
        multi = shell.analyze(
            "cat <<EOF\n$(echo one) $(echo two)\n$(echo three)\nEOF", cwd="/tmp/base"
        )
        assert [command for command in multi.commands if command != ["cat"]] == [["echo", "one"], ["echo", "two"], ["echo", "three"]]
        assert multi.commands.count(["cat"]) == 1
        assert multi.git == []

    def test_heredoc_escape_rules(self) -> None:
        # 反斜杠只对 `$`/反引号/反斜杠成对生效：偶数个后是真实替换。
        for count, expected in (
            (1, []),
            (2, [("reset", ["--hard"])]),
            (3, []),
            (4, [("reset", ["--hard"])]),
        ):
            command = "cat <<EOF\n" + ("\\" * count) + "$(git reset --hard)\nEOF"
            assert git_actions(shell.analyze(command, cwd="/tmp/base")) == expected
        # 反斜杠后为非 `$`/反引号/反斜杠时只前移一个字符，后续替换照常执行。
        assert git_actions(shell.analyze("cat <<EOF\n\\a$(git reset --hard)\nEOF", cwd="/tmp/base")) == [("reset", ["--hard"])]
        assert git_actions(shell.analyze("cat <<EOF\n\\`git reset --hard\\`\nEOF", cwd="/tmp/base")) == []
        # 引用正文的同形输入一律没有执行。
        for body in ("\\$(git reset --hard)", "$(git reset --hard)", "\\\\$(git reset --hard)"):
            command = "cat <<'EOF'\n%s\nEOF" % body
            analysis = shell.analyze(command, cwd="/tmp/base")
            assert analysis.git == []
            assert not analysis.opaque, analysis.as_dict()

    def test_heredoc_multiple_bodies_follow_source_order(self) -> None:
        # `cat <<A <<'B'`：A 的替换执行，B 的字面数据不执行。
        analysis = shell.analyze(
            "cat <<A <<'B'\n$(echo one)\nA\ngit reset --hard\nB", cwd="/tmp/base"
        )
        assert not analysis.opaque, analysis.as_dict()
        assert analysis.git == []
        assert [command for command in analysis.commands if command != ["cat"]] == [["echo", "one"]]
        assert analysis.commands.count(["cat"]) == 1
        assert not analysis.stdin_program
        # 同一物理行上的两个简单命令按源码顺序消费各自正文。
        analysis = shell.analyze(
            "cat <<A; cat <<B\n$(echo one)\nA\n$(echo two)\nB", cwd="/tmp/base"
        )
        assert not analysis.opaque, analysis.as_dict()
        assert [command for command in analysis.commands if command != ["cat"]] == [["echo", "one"], ["echo", "two"]]
        assert analysis.commands.count(["cat"]) == 2
        # 同名 delimiter 仍按出现顺序配对。
        analysis = shell.analyze(
            "cat <<EOF; cat <<EOF\n$(echo one)\nEOF\n$(echo two)\nEOF", cwd="/tmp/base"
        )
        assert not analysis.opaque, analysis.as_dict()
        assert [command for command in analysis.commands if command != ["cat"]] == [["echo", "one"], ["echo", "two"]]
        # 混合 `<<-` 与非 `<<-`。
        analysis = shell.analyze(
            "cat <<A; cat <<-B\n$(echo one)\nA\n$(echo two)\n\tB", cwd="/tmp/base"
        )
        assert not analysis.opaque, analysis.as_dict()
        assert [command for command in analysis.commands if command != ["cat"]] == [["echo", "one"], ["echo", "two"]]
        # 第二份正文缺失：整次回退，不保留已消费的第一份。
        self.assert_legacy_facts(
            "cat <<A <<B\n$(echo one)\nA\n",
            [],
            [],
        )
        partial = shell.analyze("cat <<A <<B\n$(echo one)\nA\n", cwd="/tmp/base")
        assert ["echo", "one"] in partial.commands

    def test_tab_stripped_heredoc_keeps_following_command(self) -> None:
        analysis = shell.analyze("cat <<-EOF\n\thello\n\tEOF\ngit reset --hard", cwd="/tmp/base")
        assert not analysis.opaque, analysis.as_dict()
        assert git_actions(analysis) == [("reset", ["--hard"])]
        assert analysis.commands == [["cat"], ["git", "reset", "--hard"]]
        # fd 前缀与 `<<-` 是一个运算符。
        tokens, closed = shell.tokenize("cat 3<<-EOF\n\thello\n\tEOF")
        assert closed
        assert "3<<-" in [token.text for token in tokens if token.operator]
        # 引用 delimiter 与 tab 缩进同时成立时正文仍是数据，后续真实 Git 保留。
        quoted = shell.analyze(
            "cat <<-'EOF'\n\tgit reset --hard\n\tEOF\ngit reset --hard", cwd="/tmp/base"
        )
        assert not quoted.opaque, quoted.as_dict()
        assert git_actions(quoted) == [("reset", ["--hard"])]
        assert quoted.commands == [["cat"], ["git", "reset", "--hard"]]
        # `<< -EOF` 是普通 `<<` 加含 `-` 的 delimiter。
        spaced = shell.analyze("cat << -EOF\nhello\n-EOF\ngit reset --hard", cwd="/tmp/base")
        assert not spaced.opaque, spaced.as_dict()
        assert spaced.commands == [["cat"], ["git", "reset", "--hard"]]
        assert git_actions(spaced) == [("reset", ["--hard"])]
        # `<<-` 只去前导 tab：空格缩进的结束行不匹配，整次回退保留旧证据。
        self.assert_legacy_facts(
            "cat <<-EOF\n\thello\n  EOF\ngit reset --hard",
            [("reset", ["--hard"])],
            [("git-reset", None, False)],
        )

    def test_heredoc_delimiter_is_exact(self) -> None:
        # 尾随空格/制表/前缀空格/CR 都不是结束行：后续精确 EOF 之前的行属于正文。
        body = "hello\nEOF \nEOF\t\n EOF\nEOF\r\ngit reset --hard"
        analysis = shell.analyze("cat <<'EOF'\n" + body + "\nEOF", cwd="/tmp/base")
        assert not analysis.opaque, analysis.as_dict()
        assert analysis.git == []
        assert analysis.commands == [["cat"]]
        # 没有精确结束行：整次回退，旧 Git 证据不减少。
        for body in ("hello\nEOF ", "hello\nEOF\t", "hello\n EOF", "hello\nEOF\r"):
            command = "cat <<'EOF'\n" + body + "\ngit reset --hard"
            self.assert_legacy_facts(
                command,
                [("reset", ["--hard"])],
                [("git-reset", None, False)],
            )
        # Unicode 分隔符按字节精确匹配。
        unicode = shell.analyze("cat <<'ＥＯＦ'\nhello\nＥＯＦ", cwd="/tmp/base")
        assert not unicode.opaque, unicode.as_dict()
        assert unicode.commands == [["cat"]]
        self.assert_legacy_facts("cat <<'EOF'\nhello\nＥＯＦ", [], [])

    def test_heredoc_fallback_preserves_legacy_evidence(self) -> None:
        reset_git = [("reset", ["--hard"])]
        reset_writes = [("git-reset", None, False)]
        # 矩阵失败形态后接独立 reset/touch：回退必须保留旧证据，不能只置 opaque。
        cases = (
            ("cat <<$X\nhello\nEOF\ngit reset --hard", reset_git, reset_writes, False),
            ("cat <<$(printf E)\nhello\nEOF\ngit reset --hard", reset_git, reset_writes, False),
            ('cat <<"$X"\nhello\nEOF\ngit reset --hard', reset_git, reset_writes, False),
            ("cat <<EOF", [], [], False),
            ("cat <<\nEOF\ngit reset --hard", reset_git, reset_writes, False),
            ("cat <<; git reset --hard", reset_git, reset_writes, False),
            ("cat <<\nhello\n\ngit reset --hard", reset_git, reset_writes, False),
            ("cat <<'EOF'\nhello\ngit reset --hard", reset_git, reset_writes, False),
            ("cat <<A <<B\nhello\nA", [], [], False),
            ("cat <<EOF\n${NAME}\nEOF\ngit reset --hard", reset_git, reset_writes, False),
            ("cat <<EOF\n$((1+2))\nEOF\ngit reset --hard", reset_git, reset_writes, False),
            (
                "cat <<EOF\n`echo 'x'`\nEOF\ngit reset --hard",
                reset_git,
                reset_writes,
                False,
            ),
            (
                "cat <<EOF\nfoo \\\nbar\nEOF\ngit reset --hard",
                reset_git,
                reset_writes,
                False,
            ),
            (
                "cat <<EOF\n$(cat <<INNER\nx\nINNER)\nEOF\ngit reset --hard",
                reset_git,
                reset_writes,
                False,
            ),
            ("cat <<EOF\n$(git reset --hard\nEOF\ngit reset --hard", [], [], False),
            ("if true; then cat <<'EOF'\nhello\nEOF", [], [], True),
        )
        for command, git, writes, compound in cases:
            self.assert_legacy_facts(command, git, writes, compound)
        # 同一次输入里更早的 quoted 数据正文不能只回退最后一个 heredoc。
        sentinel = shell.analyze(
            "cat <<'A'\ngit reset --hard\nA\ncat <<$X\nhello\nEOF\ngit reset --hard",
            cwd="/tmp/base",
        )
        assert sentinel.opaque
        assert git_actions(sentinel) == [("reset", ["--hard"]), ("reset", ["--hard"])]

    def test_heredoc_parameter_candidate_falls_back_before_close(self) -> None:
        # `${x:-)}` 里的 `)` 不能被当成命令替换结束：预检先于配对，整次回退。
        self.assert_legacy_facts(
            "cat <<EOF\n$(echo ${x:-)}; git reset --hard)\nEOF",
            [("reset", ["--hard"])],
            [("git-reset", None, False)],
            True,
        )
        self.assert_legacy_facts(
            "cat <<EOF\n$(echo ${x:-)}; touch protected)\nEOF",
            [],
            [("touch", "protected", True)],
            True,
        )
        # 简单 `${NAME}` 与算术展开同样整次回退，不因位于内层引用而放宽。
        self.assert_legacy_facts(
            "cat <<EOF\n$(echo ${NAME}; git reset --hard)\nEOF",
            [("reset", ["--hard"])],
            [("git-reset", None, False)],
        )

    def test_substitution_failure_falls_back_to_legacy(self) -> None:
        reset_git = [("reset", ["--hard"])]
        reset_writes = [("git-reset", None, False)]
        # 候选事务里更早的 quoted 数据正文已被删除，随后普通命令替换递归超预算：
        # 子分析只有 opaque，不能 merge 后继续提交，必须整次回退恢复旧证据。
        over_budget = ("env " * 9) + "git reset --hard"
        substitution = "printf '%s' " + '"$(' + over_budget + ')"'
        self.assert_legacy_facts(
            "cat <<'EOF'\ngit reset --hard\ntouch protected\nEOF\n" + substitution,
            reset_git,
            reset_writes + [("touch", "protected", True)],
        )
        # 没有 heredoc 候选事务的普通命令维持旧结果（保守边界，不引入新放行）。
        plain = shell.analyze(substitution, cwd="/tmp/base")
        assert plain.opaque
        assert not plain.too_deep
        assert plain.git == []
        assert plain.writes == []
        assert not plain.stdin_program

    def test_inner_substitution_nodes_are_proven(self) -> None:
        # design §4.4：替换证明要覆盖内层节点。`$(echo `printf 'x'`)` 的内层复杂
        # 反引号不属于支持的简单形态，整次回退，不能只证明最外层后删掉旧正文证据。
        self.assert_legacy_facts(
            "cat <<'A'\ngit reset --hard\ntouch protected\nA\n"
            "cat <<EOF\n$(echo `printf 'x'`)\nEOF",
            [("reset", ["--hard"])],
            [("git-reset", None, False), ("touch", "protected", True)],
        )
        # 对照：替换内部的简单 `$()` 与无反引号形态仍可证明。
        simple = shell.analyze("cat <<EOF\n$(echo $(echo x))\nEOF", cwd="/tmp/base")
        assert not simple.opaque, simple.as_dict()
        assert sorted(command for command in simple.commands if command != ["cat"]) == [["echo", "$(echo x)"], ["echo", "x"]]

    def test_herestring_program_stdin_is_marked(self) -> None:
        # `<<<` 的执行/opaque/重定向语义不变，但供给解释器/壳的 herestring 是
        # stdin 程序，必须带标记，否则读取守卫会按“没有读取目标”放行。
        reading = 'python3 - <<< \'print(open("/etc/passwd").read())\''
        analysis = shell.analyze(reading, cwd="/tmp/base")
        assert analysis.stdin_program, analysis.as_dict()
        assert analysis.git == []
        assert analysis.writes == []
        assert shell.is_read_only_command(reading) == (False, True)
        reads, unknown = shell.read_command_paths(reading)
        assert reads == []
        assert unknown
        for command in ("sh <<< 'git reset --hard'", "env bash <<< 'git reset --hard'"):
            wrapped = shell.analyze(command, cwd="/tmp/base")
            # 正文不是多行 heredoc：不新增脚本分析，Git/写证据与今天一致。
            assert wrapped.stdin_program, wrapped.as_dict()
            assert wrapped.git == []
            assert wrapped.writes == []
            assert shell.is_read_only_command(command) == (False, True)
            _reads, unknown = shell.read_command_paths(command)
            assert unknown
        # 数据消费者与普通命令的 herestring 不标记。
        assert not shell.analyze("cat <<< hello", cwd="/tmp/base").stdin_program
        assert not shell.analyze("printf '%s' <<< x", cwd="/tmp/base").stdin_program

    def test_eval_herestring_program_stdin_is_marked(self) -> None:
        # `eval`/壳 `-c` 的脚本文本继承外层 stdin：`eval 'python3 -' <<< …` 的
        # 正文是程序，不能按有效命令名（eval）判成数据输入。
        for command in (
            "eval 'python3 -' <<< 'print(1)'",
            "env eval 'python3 -' <<< 'print(1)'",
        ):
            analysis = shell.analyze(command, cwd="/tmp/base")
            assert analysis.stdin_program, analysis.as_dict()
            assert not analysis.opaque, analysis.as_dict()
            assert analysis.git == []
            assert analysis.writes == []
            assert shell.is_read_only_command(command) == (False, True)
            _reads, unknown = shell.read_command_paths(command)
            assert unknown
        # 脚本阶段的任一命令名都保守标记：`eval cat` 同样可能把正文喂给程序。
        assert shell.analyze("eval 'cat' <<< 'hello'", cwd="/tmp/base").stdin_program
        assert shell.is_read_only_command("eval 'cat' <<< 'hello'") == (False, True)
        _reads, unknown = shell.read_command_paths("eval 'cat' <<< 'hello'")
        assert unknown
        # 非脚本阶段的普通命令/数据消费者仍不标记。
        for command in ("cat <<< 'x'", "printf '%s' <<< 'x'"):
            assert not shell.analyze(command, cwd="/tmp/base").stdin_program

    def test_heredoc_cwd_and_wrapper_evaluation(self) -> None:
        # 外层重定向按段入口 cwd 解析。
        chdir = shell.analyze("cd sub; cat > out <<EOF\nhello\nEOF", cwd="/tmp/base")
        assert write_targets(chdir) == [("out", True)]
        assert entry_cwds(chdir.writes) == ["/tmp/base/sub"]
        # 替换内的 cd 不污染父 shell 的后续读取。
        substitution = shell.analyze(
            "cat <<EOF\n$(cd sub; cat app.py)\nEOF\ncat other", cwd="/tmp/base"
        )
        assert read_paths(substitution) == ["app.py", "other"]
        assert entry_cwds(substitution.reads) == ["/tmp/base/sub", "/tmp/base"]
        # `env -C` 的有效 cwd 累积到正文脚本的读/写。
        wrapped = shell.analyze("env -C sub sh <<'EOF'\ntouch out\nEOF", cwd="/tmp/base")
        assert write_targets(wrapped) == [("out", True)]
        assert entry_cwds(wrapped.writes) == ["/tmp/base/sub"]
        assert wrapped.stdin_program
        # 组尾 synthetic redirect 段使用组入口 cwd，且没有命令名也要处理正文。
        group = shell.analyze("( cd sub ) <<'EOF'\ntouch out\nEOF", cwd="/tmp/base")
        assert not group.opaque, group.as_dict()
        assert write_targets(group) == [("out", True)]
        assert entry_cwds(group.writes) == ["/tmp/base"]
        assert group.stdin_program
        # 子 shell 内、仍在顶层的 heredoc 正常消费，后续真实 Git 保留。
        subshell = shell.analyze(
            "(cat <<'EOF'\nhello\nEOF\n)\ngit reset --hard", cwd="/tmp/base"
        )
        assert not subshell.opaque, subshell.as_dict()
        assert subshell.commands == [["cat"], ["git", "reset", "--hard"]]
        # argv 包装不重复消费同一阶段的正文替换。
        wrapper = shell.analyze("sudo cat <<EOF\n$(echo once)\nEOF", cwd="/tmp/base")
        assert [command for command in wrapper.commands if command == ["echo", "once"]] == [["echo", "once"]]
        assert not wrapper.stdin_program

    def test_shell_heredoc_body_is_a_script(self) -> None:
        for name in ("sh", "bash", "dash", "zsh", "ksh"):
            command = "%s <<'EOF'\ngit reset --hard\nEOF" % name
            analysis = shell.analyze(command, cwd="/tmp/base")
            # 引用 delimiter 只抑制外层展开，不能阻止接收正文的壳执行脚本。
            assert git_actions(analysis) == [("reset", ["--hard"])]
            assert analysis.stdin_program
            assert not analysis.opaque, analysis.as_dict()
        unquoted = shell.analyze("bash <<EOF\ntouch protected\nEOF", cwd="/tmp/base")
        assert write_targets(unquoted) == [("protected", True)]
        assert unquoted.stdin_program
        for command in (
            "sh -s <<'EOF'\ngit reset --hard\nEOF",
            "sudo sh <<'EOF'\ngit reset --hard\nEOF",
            "env bash <<'EOF'\ngit reset --hard\nEOF",
        ):
            analysis = shell.analyze(command, cwd="/tmp/base")
            assert git_actions(analysis) == [("reset", ["--hard"])]
            assert analysis.stdin_program
        # `bash -c` 的脚本文本是新的求值阶段；外层 stdin 仍保守按壳正文处理。
        script = shell.analyze("bash -c 'sh' <<'EOF'\ntouch protected\nEOF", cwd="/tmp/base")
        assert write_targets(script) == [("protected", True)]
        assert script.stdin_program
        # 正文的 cd 与读写都在独立子上下文里，不外泄到父 shell。
        body_cd = shell.analyze("sh <<'EOF'\ncd sub\ntouch out\nEOF\ncat after", cwd="/tmp/base")
        assert write_targets(body_cd) == [("out", True)]
        assert entry_cwds(body_cd.writes) == ["/tmp/base/sub"]
        assert read_paths(body_cd) == ["after"]
        assert entry_cwds(body_cd.reads) == ["/tmp/base"]
        # 正文的读/写/Git 证据全部合并。
        evidence = shell.analyze(
            "sh <<'EOF'\ncat secret\ntouch out\ngit reset --hard\nEOF", cwd="/tmp/base"
        )
        assert read_paths(evidence) == ["secret"]
        assert write_targets(evidence) == [("out", True), (None, False)]
        assert git_actions(evidence) == [("reset", ["--hard"])]
        # 单引号与 `#` 中的真实外层替换不因正文脚本的引号/注释规则丢失。
        outer = shell.analyze(
            "sh <<EOF\n'$(git reset --hard)'\n# $(touch protected)\nEOF", cwd="/tmp/base"
        )
        assert git_actions(outer) == [("reset", ["--hard"])]
        assert ("protected", True) in write_targets(outer)
        # 解释器对照：Python 正文不按 shell 执行，不产生假 Git 拒绝。
        python = shell.analyze("python3 - <<'PY'\ngit reset --hard\nPY", cwd="/tmp/base")
        assert python.git == []

    def test_heredoc_data_consumer_whitelist(self) -> None:
        for name in HEREDOC_DATA_COMMANDS:
            command = "%s <<'EOF'\ngit reset --hard\nEOF" % name
            analysis = shell.analyze(command, cwd="/tmp/base")
            assert not analysis.opaque, analysis.as_dict()
            assert not analysis.stdin_program
            assert analysis.git == []
            assert analysis.reads == []
            # 正文不产生假写；tee 自身的 argv 写规则不受名单改变。
            assert [item for item in analysis.writes if item.get("kind") != "tee"] == []
        # 绝对路径与 argv 包装解包后仍是纯数据消费者。
        for command in (
            "/usr/bin/cat <<'EOF'\ngit reset --hard\nEOF",
            "env cat <<'EOF'\ngit reset --hard\nEOF",
            "command cat <<'EOF'\ngit reset --hard\nEOF",
            "sudo cat <<'EOF'\ngit reset --hard\nEOF",
        ):
            analysis = shell.analyze(command, cwd="/tmp/base")
            assert analysis.git == []
            assert not analysis.stdin_program
        # 外层重定向写照旧完整，正文 Git 字面不构成执行。
        redirect = shell.analyze("cat > f <<'EOF'\ngit reset --hard\nEOF", cwd="/tmp/base")
        assert redirect.git == []
        assert write_targets(redirect) == [("f", True)]
        assert not redirect.stdin_program
        count = shell.analyze("wc -l <<'EOF'\ngit reset --hard\nEOF", cwd="/tmp/base")
        assert count.git == []
        assert count.writes == []
        # 动态有效命令名/argv 命令替换不是静态消费者：保守按脚本并标真。
        for command in (
            "$(echo cat) <<'EOF'\ngit reset --hard\nEOF",
            "cat $(echo file) <<'EOF'\ngit reset --hard\nEOF",
        ):
            analysis = shell.analyze(command, cwd="/tmp/base")
            assert git_actions(analysis) == [("reset", ["--hard"])]
            assert analysis.stdin_program

    def test_heredoc_executing_consumers_keep_reset(self) -> None:
        for command in (
            "xargs -I{} sh -c '{}' <<'EOF'\ngit reset --hard\nEOF",
            "sed e <<'EOF'\ngit reset --hard\nEOF",
            "awk '{print}' <<'EOF'\ngit reset --hard\nEOF",
            "gawk '{print}' <<'EOF'\ngit reset --hard\nEOF",
            "mawk '{print}' <<'EOF'\ngit reset --hard\nEOF",
            "nawk '{print}' <<'EOF'\ngit reset --hard\nEOF",
            "find . -maxdepth 0 <<'EOF'\ngit reset --hard\nEOF",
            "frobnicate <<'EOF'\ngit reset --hard\nEOF",
        ):
            analysis = shell.analyze(command, cwd="/tmp/base")
            # 非白名单消费者一律按脚本，正文 reset 证据必须保留。
            assert git_actions(analysis) == [("reset", ["--hard"])]
            assert analysis.stdin_program
            assert not analysis.opaque, analysis.as_dict()
        # env 无法解包时保守按脚本，且不丢正文证据。
        opaque_env = shell.analyze(
            "env --unknown-option cat <<'EOF'\ngit reset --hard\nEOF", cwd="/tmp/base"
        )
        assert git_actions(opaque_env) == [("reset", ["--hard"])]
        assert opaque_env.stdin_program

    def test_heredoc_interpreter_data_branch(self) -> None:
        for name in ("python", "python3", "node", "perl", "ruby", "php", "python3.12"):
            command = "%s - <<'PY'\ngit reset --hard\nPY" % name
            analysis = shell.analyze(command, cwd="/tmp/base")
            # 解释器正文按数据处理，但仍是不可证明的数据消费者输入。
            assert analysis.git == []
            assert analysis.stdin_program
            assert not analysis.opaque, analysis.as_dict()
            assert shell.is_read_only_command(command) == (False, True)
        wrapped = shell.analyze("env python3 - <<'PY'\ngit reset --hard\nPY", cwd="/tmp/base")
        assert wrapped.git == []
        assert wrapped.stdin_program
        # awk 系列不属于解释器数据分支：仍保守按脚本并保留 reset。
        for name in ("awk", "gawk", "mawk", "nawk"):
            command = "%s '{print}' <<'EOF'\ngit reset --hard\nEOF" % name
            analysis = shell.analyze(command, cwd="/tmp/base")
            assert git_actions(analysis) == [("reset", ["--hard"])]
            assert analysis.stdin_program

    def test_heredoc_consumer_binding_is_conservative(self) -> None:
        reading = "print(open('/etc/passwd').read())"
        for command in (
            "(python3 -) <<'PY'\n%s\nPY" % reading,
            "python3 - 3<<'PY' 0<&3\n%s\nPY" % reading,
            "cat <<'PY' | python3 -\n%s\nPY" % reading,
        ):
            analysis = shell.analyze(command, cwd="/tmp/base")
            assert analysis.stdin_program, analysis.as_dict()
            assert not analysis.opaque, analysis.as_dict()
        # 管道左段名为 cat 也不能抢先归纯数据：正文按脚本，reset 证据保留。
        pipeline = shell.analyze(
            "cat <<'EOF' | cat\ngit reset --hard\nEOF", cwd="/tmp/base"
        )
        assert pipeline.stdin_program
        assert git_actions(pipeline) == [("reset", ["--hard"])]

    def test_stdin_program_helper_contracts(self) -> None:
        direct = "python3 - <<'PY'\nopen('protected','w').write('x')\nPY"
        assert shell.analyze(direct).stdin_program
        assert shell.is_read_only_command(direct) == (False, True)
        # 壳正文：已知读取保留，未知位为真（transcript 只取已知读取的旧行为不变）。
        reads, unknown = shell.read_command_paths("sh <<'EOF'\ncat secret\nEOF")
        assert [item.get("path") for item in reads] == ["secret"]
        assert unknown
        # 普通数据 heredoc 不因该标记变未知。
        data_reads, data_unknown = shell.read_command_paths("cat <<EOF\n$(cat secret)\nEOF")
        assert [item.get("path") for item in data_reads] == ["secret"]
        assert not data_unknown
        # cat 外层写仍非只读，但标记为假。
        write_form = "cat > f <<'EOF'\nhello\nEOF"
        assert not shell.analyze(write_form).stdin_program
        assert shell.is_read_only_command(write_form) == (False, False)
        # merge 做 OR，as_dict 暴露同名布尔键。
        merged = shell.Analysis()
        merged.merge(shell.analyze(direct))
        assert merged.stdin_program
        assert shell.analyze(direct).as_dict()["stdin_program"] is True
        assert shell.Analysis().as_dict()["stdin_program"] is False

    def test_wrapped_data_heredoc_has_no_implicit_read(self) -> None:
        # argv 包装重分析必须继承“已证明的数据 stdin”事实，否则内层 cat 会再按
        # 无 stdin 的旧规则记一条隐式 `.` 读取，使读取守卫遍历整个 cwd。
        for command in (
            "env cat <<'EOF'\nhello\nEOF",
            "sudo cat <<'EOF'\nhello\nEOF",
            "command wc -l <<'EOF'\nhello\nEOF",
        ):
            analysis = shell.analyze(command, cwd="/tmp/base")
            assert read_paths(analysis) == [], analysis.as_dict()
            assert not analysis.stdin_program
        # 显式文件参数仍是读取目标。
        explicit = shell.analyze("env cat file.txt <<'EOF'\nhello\nEOF", cwd="/tmp/base")
        assert read_paths(explicit) == ["file.txt"]
        # 解释器包装仍按解释器分支标真。
        assert shell.analyze("env python3 - <<'PY'\nprint(1)\nPY", cwd="/tmp/base").stdin_program

    def test_cat_data_heredoc_has_no_implicit_path(self) -> None:
        write_form = shell.analyze("cat > f <<'EOF'\nhello\nEOF", cwd="/tmp/base")
        # 已明确提供纯数据 stdin 且无文件参数：不再产生隐式 `.` 读取候选。
        assert write_form.reads == []
        assert not write_form.stdin_program
        assert write_targets(write_form) == [("f", True)]
        plain = shell.analyze("cat <<'EOF'\nhello\nEOF", cwd="/tmp/base")
        assert plain.reads == []
        assert not plain.stdin_program
        # 显式文件参数与其他旧语义不变。
        explicit = shell.analyze("cat app.py <<'EOF'\nhello\nEOF", cwd="/tmp/base")
        assert read_paths(explicit) == ["app.py"]
        assert read_paths(shell.analyze("cat", cwd="/tmp/base")) == ["."]
        # 回退输入保持旧的隐式读取候选。
        assert read_paths(shell.analyze("cat <<$X\nhello\nEOF", cwd="/tmp/base")) == ["."]

    def test_herestring_and_existing_lexing_unchanged(self) -> None:
        herestring = shell.analyze("cat <<<$(git reset --hard)", cwd="/tmp/base")
        assert git_actions(herestring) == [("reset", ["--hard"])]
        # herestring 不排队消费后续行。
        tokens, closed = shell.tokenize("cat <<<'EOF'\nnext line")
        assert closed
        assert [token.text for token in tokens if token.operator] == ["<<<", "\n"]
        # fd 复制没有文件目标，也不把数据 heredoc 变成未知消费者。
        assert [item.get("kind") for item in shell.analyze("printf x 2>&1", cwd="/tmp/base").writes] == []
        assert not shell.analyze("cat 2>&1 <<'EOF'\nhello\nEOF", cwd="/tmp/base").stdin_program
        assert write_targets(shell.analyze("printf x > 1", cwd="/tmp/base")) == [("1", True)]
        # r7/r8/r9 的续行/注释/引号回归。
        assert shell.argv_of("g\\\nit reset --hard") == [["git", "reset", "--hard"]]
        comment = shell.analyze("\\\n# note \\\ngit reset --hard")
        assert git_actions(comment) == [("reset", ["--hard"])]
        assert not comment.opaque
        assert write_targets(shell.analyze("printf x >&'1\\\n.txt'", cwd="/tmp/base")) == [("1\\\n.txt", True)]
        continuation = shell.analyze("cd su\\\nb && printf x >\\\n>1.txt", cwd="/tmp/base")
        assert write_targets(continuation) == [("1.txt", True)]
        assert entry_cwds(continuation.writes) == ["/tmp/base/sub"]
        # 引号里的 `<<` 是数据，不触发整次回退。
        for command in ("echo 'a << b'", 'echo "a << b"'):
            analysis = shell.analyze(command, cwd="/tmp/base")
            assert not analysis.opaque, analysis.as_dict()

    def test_heredoc_body_depth_limit_preserves_legacy_evidence(self) -> None:
        command = "sh <<'EOF'\ngit reset --hard\ntouch protected\nEOF"
        # d=7：正文第 8 层完整完成，reset 与完整 touch 写仍在。
        complete = shell.analyze(command, depth=7)
        assert not complete.opaque, complete.as_dict()
        assert not complete.too_deep
        assert complete.stdin_program
        assert git_actions(complete) == [("reset", ["--hard"])]
        assert write_targets(complete) == [(None, False), ("protected", True)]
        # d=8：正文第 9 层超限 → 整次回退，旧证据保持。
        fallback = shell.analyze(command, depth=8)
        assert fallback.opaque
        assert not fallback.too_deep
        assert git_actions(fallback) == [("reset", ["--hard"])]
        assert write_targets(fallback) == [(None, False), ("protected", True)]
        # d=9：入口本身超限，维持旧预算边界，不补扫正文。
        limited = shell.analyze(command, depth=9)
        assert limited.opaque
        assert limited.too_deep
        assert limited.git == []
        assert limited.writes == []

    def test_heredoc_script_and_wrapper_limit_preserves_legacy_evidence(self) -> None:
        base = "sh <<'EOF'\ngit reset --hard\ntouch protected\nEOF"
        chain = {}
        nested = base
        for level in range(1, 10):
            nested = "sh -c " + shlex.quote(nested)
            chain[level] = nested
        # n=9 的旧预算边界：第 9 层脚本无法完成，旧结果就是空证据。
        for level, opaque, git, writes in (
            (7, False, [("reset", ["--hard"])], [(None, False), ("protected", True)]),
            (8, True, [("reset", ["--hard"])], [(None, False), ("protected", True)]),
            (9, True, [], []),
        ):
            analysis = shell.analyze(chain[level])
            assert analysis.opaque == opaque, analysis.as_dict()
            assert not analysis.too_deep
            assert git_actions(analysis) == git
            assert write_targets(analysis) == writes
        for level, opaque in ((7, False), (8, False), (9, True)):
            analysis = shell.analyze(("env " * level) + base)
            assert analysis.opaque == opaque, analysis.as_dict()
            assert not analysis.too_deep
            assert git_actions(analysis) == [("reset", ["--hard"])]
            assert write_targets(analysis) == [(None, False), ("protected", True)]

    def test_heredoc_scan_is_bounded(self) -> None:
        # 近上限正文：一次线性消费，不逐行 analyze。
        large = "cat <<'EOF'\n" + ("x" * 100000) + "\nEOF"
        analysis = shell.analyze(large, cwd="/tmp/base")
        assert not analysis.opaque, analysis.as_dict()
        assert analysis.commands == [["cat"]]
        # 大量小 heredoc 仍按源码顺序消费且不丢后续命令。
        many = "".join("cat <<'E'\nv\nE\n" for _ in range(100)) + "git reset --hard"
        many_analysis = shell.analyze(many, cwd="/tmp/base")
        assert not many_analysis.opaque, many_analysis.as_dict()
        assert many_analysis.commands.count(["cat"]) == 100
        assert git_actions(many_analysis) == [("reset", ["--hard"])]
        # 未闭合/深层替换与超长输入都及时返回，异常不外泄。
        unclosed = "cat <<EOF\n" + ("$(" * 2000) + "\nEOF"
        assert shell.analyze(unclosed, cwd="/tmp/base").opaque
        deep = "cat <<EOF\n" + ("$(echo " * 40) + "x" + (")" * 40) + "\nEOF"
        assert shell.analyze(deep, cwd="/tmp/base").opaque
        over = "cat <<'EOF'\n" + ("y" * (shell.MAX_COMMAND_BYTES + 1)) + "\nEOF"
        limited = shell.analyze(over, cwd="/tmp/base")
        assert limited.opaque
        assert limited.too_deep
