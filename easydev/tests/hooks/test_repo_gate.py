"""R-3：§6b 的非法转义扫描必须在默认解释器与 3.12 上给出同一判定。

只从 `scripts/check.sh` §6b heredoc 里提取生产扫描的**前缀**（截断于唯一的模板
JSON 循环行），在隔离临时目录写 fixture 后用子进程执行：不复制生产过滤器，也不
靠源码里出现过哪些字符串来断言效果。临时 fixture 不在仓库内，不会被全仓编译
门禁当成正式 Python 文件。

缺少指定 3.12 解释器时，默认解释器的 fixture 仍全部执行；`test_python312_gate_branch`
以 `pytest.skip` 披露“只证明了默认解释器分支”，不把单解释器运行当成双解释器证据。
"""

from __future__ import annotations

import pytest

import contextlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

from conftest import subtests
from support import CaseBase, EASYDEV_ROOT

CHECK_SH = EASYDEV_ROOT / "scripts" / "check.sh"
PYTHON_312 = Path(os.path.expanduser("~/.local/bin/python3.12"))

SECTION_TITLE = 'echo "=== 6b. Plugin 脚本、模板与分发契约静态检查 ==="'
HEREDOC_START = "python3 - <<'PY'"
HEREDOC_END = "PY"
# 提取边界必须用完整唯一行：§6b 里 `glob.glob("easydev/hooks/templates/*.json")`
# 这一短前缀出现两处（模板 JSON、模板契约循环），按短前缀截断会取错位置。
BOUNDARY = (
    'for path in sorted(glob.glob("easydev/hooks/templates/*.json")) '
    '+ ["easydev/hooks/manifest.json"]:'
)
SHORT_PREFIX = 'for path in sorted(glob.glob("easydev/hooks/templates/*.json"))'

# fixture 文本：`"\\w"` 在测试源码里是合法的反斜杠+w，写进 fixture 后即非法转义。
INVALID_ESCAPE = 'value = "\\w"\n'


def section_heredoc(text: str) -> str:
    """§6b 节的 heredoc 正文；标题或起止标记缺失/不成对/重复都直接失败。"""
    lines = text.splitlines(keepends=True)
    titles = [index for index, line in enumerate(lines) if line.rstrip("\n") == SECTION_TITLE]
    if len(titles) != 1:
        raise AssertionError("§6b 标题行必须唯一，实际 %d 处" % len(titles))
    start = titles[0] + 1
    stop = len(lines)
    for index in range(start, len(lines)):
        if lines[index].startswith('echo "=== '):
            stop = index
            break
    body = lines[start:stop]
    starts = [index for index, line in enumerate(body) if line.rstrip("\n") == HEREDOC_START]
    if len(starts) != 1:
        raise AssertionError("§6b heredoc 起始行必须唯一，实际 %d 处" % len(starts))
    opens = starts[0] + 1
    closes = [index for index in range(opens, len(body)) if body[index].rstrip("\n") == HEREDOC_END]
    if not closes:
        raise AssertionError("§6b heredoc 未闭合")
    return "".join(body[opens : closes[0]])


def gate_prefix(text: str) -> str:
    """§6b heredoc 内生产扫描的前缀：唯一完整边界行之前的全部行。"""
    heredoc = section_heredoc(text)
    lines = heredoc.splitlines(keepends=True)
    matches = [index for index, line in enumerate(lines) if line.rstrip("\n") == BOUNDARY]
    if len(matches) != 1:
        raise AssertionError("截断边界行必须唯一，实际 %d 处" % len(matches))
    return "".join(lines[: matches[0]])


def interpreters() -> list:
    """本次实际能跑的解释器列表：默认加存在的指定 3.12。

    缺 3.12 时只返回默认解释器；“缺哪个分支”由 `test_python312_gate_branch` 的显式
    skip 报告，不由本函数静默报成双解释器已验。
    """
    found = [sys.executable]
    if PYTHON_312.is_file() and str(PYTHON_312) != sys.executable:
        found.append(str(PYTHON_312))
    return found


class RepoPythonGateTests(CaseBase):
    def setup_method(self) -> None:
        self.script_text = CHECK_SH.read_text(encoding="utf-8")
        self.prefix = gate_prefix(self.script_text)
        self.tmp = tempfile.TemporaryDirectory()
        self.defer(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.checker = self.root / "scan.py"
        self.checker.write_text(
            self.prefix
            + "\nif errors:\n"
            + "    for error in errors:\n"
            + "        print(error)\n"
            + "    sys.exit(1)\n"
            + "print('OK')\n",
            encoding="utf-8",
        )

    # ---- 执行入口 --------------------------------------------------
    def run_gate(self, sources, interpreter=None, extra_env=None):
        """把 fixture 写进隔离目录后执行生产扫描前缀；返回 (退出码, 合并输出)。"""
        case = Path(tempfile.mkdtemp(dir=str(self.root)))
        for name, text in sources.items():
            path = case / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        environment = dict(os.environ)
        environment.pop("PYTHONWARNINGS", None)
        environment.update(extra_env or {})
        completed = subprocess.run(
            [interpreter or sys.executable, str(self.checker)],
            cwd=str(case),
            env=environment,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return completed.returncode, completed.stdout + completed.stderr

    def assert_rejected(self, sources, name: str) -> None:
        for interpreter in interpreters():
            with subtests.test(interpreter=interpreter, fixture=name):
                code, output = self.run_gate(sources, interpreter)
                assert code == 1, output
                assert name in output

    def assert_accepted(self, source: str, name: str) -> None:
        for interpreter in interpreters():
            with subtests.test(interpreter=interpreter, fixture=name):
                code, output = self.run_gate({name: source}, interpreter)
                assert code == 0, output

    # ---- 非法转义必须判负 ------------------------------------------
    def test_invalid_escape_is_rejected(self) -> None:
        # 内层用例由 RepoPythonGateReportingTests 直接调用，保持循环而不是参数化。
        for interpreter in interpreters():
            with subtests.test(interpreter=interpreter):
                code, output = self.run_gate({"bad.py": INVALID_ESCAPE}, interpreter)
                assert code == 1, output
                # 诊断点名文件与可定位行号，不是只报一句“有告警”。
                assert "bad.py" in output
                assert "line 1" in output
                assert "invalid escape sequence" in output

    def test_invalid_escapes_in_literal_forms_are_rejected(self) -> None:
        cases = {
            "plain.py": INVALID_ESCAPE,
            "docstring.py": '"""\\w"""\n',
            "bytes.py": 'data = b"\\u1234"\n',
            "fstring.py": 'text = f"\\w"\n',
        }
        for name, source in cases.items():
            self.assert_rejected({name: source}, name)

    # ---- 合法输入必须保持通过 --------------------------------------
    def test_raw_escaped_and_valid_literals_are_accepted(self) -> None:
        cases = {
            "raw_regex.py": 'pattern = r"\\w"\n',
            "raw_bytes.py": 'data = rb"\\w"\n',
            "double_backslash.py": 'plain = "\\\\w"\n',
            "escapes.py": 'text = "\\n\\x41\\101"\n',
            "unicode.py": 'text = "\\u1234\\U00012345"\n',
            "named_unicode.py": 'text = "\\N{LATIN CAPITAL LETTER A}"\n',
            "implicit_join.py": 'joined = r"\\w" "\\n"\n',
            "triple_double.py": 'text = """plain\ntext"""\n',
            "triple_single.py": "text = '''plain'''\n",
            "fstring_text.py": 'text = f"{1}"\n',
            "fstring_raw.py": 'text = rf"\\d+"\n',
            "fstring_braces.py": 'text = f"{{literal}}"\n',
            "comment.py": "# regex fragment: \\w and \\d\n",
            # 反斜杠 + 真实换行才是续行；`\n` 两字符不是。
            "continuation.py": 'text = "a\\\nb"\n',
        }
        for name, source in cases.items():
            self.assert_accepted(source, name)

    def test_python312_only_fstring_expression_syntax_is_separate(self) -> None:
        """3.12 才允许 f-string 表达式内有反斜杠；单列版本兼容，不用于证明扫描有效。"""
        if sys.version_info < (3, 12):
            pytest.skip("f-string 表达式内的反斜杠是 3.12 专有语法")
        code, output = self.run_gate(
            {"fstring_expr.py": 'text = f"{\'\\n\'.join([\'a\', \'b\'])}"\n'},
        )
        assert code == 0, output

    def test_python312_gate_branch(self) -> None:
        """指定 3.12 路径的扫描分支；缺路径时用显式 skip 披露实际证明范围。"""
        if not PYTHON_312.is_file():
            version = "%d.%d.%d" % tuple(sys.version_info[:3])
            if sys.version_info < (3, 12):
                pytest.skip(
                    "缺少指定解释器 %s；本次只证明默认解释器分支（%s，Python %s），"
                    "未验证 Python 3.12 分支。" % (PYTHON_312, sys.executable, version)
                )
            pytest.skip(
                "缺少指定解释器 %s；已覆盖默认解释器分支（%s，Python %s），"
                "未验证指定路径的解释器；不声称两个解释器均已验证。"
                % (PYTHON_312, sys.executable, version)
            )
        code, output = self.run_gate({"bad.py": INVALID_ESCAPE}, str(PYTHON_312))
        assert code == 1, output
        assert "bad.py" in output
        assert "line 1" in output
        assert "invalid escape sequence" in output
        code, output = self.run_gate({"raw_regex.py": 'pattern = r"\\w"\n'}, str(PYTHON_312))
        assert code == 0, output

    # ---- 其他编译错误不能被新过滤吞掉 ------------------------------
    def test_other_syntax_errors_and_syntaxwarnings_are_rejected(self) -> None:
        cases = {
            "broken.py": "def f(:\n",
            "return_outside.py": "return 1\n",
            "literal_is.py": "value = 1 is 1\n",
        }
        for name, source in cases.items():
            self.assert_rejected({name: source}, name)

    def test_gate_ignores_caller_warning_suppression(self) -> None:
        for interpreter in interpreters():
            with subtests.test(interpreter=interpreter):
                code, output = self.run_gate(
                    {"bad.py": INVALID_ESCAPE}, interpreter, {"PYTHONWARNINGS": "ignore"}
                )
                assert code == 1, output
                assert "bad.py" in output

    # ---- 提取机制本身 ----------------------------------------------
    def test_scan_boundary_is_full_unique_line(self) -> None:
        heredoc = section_heredoc(self.script_text)
        # 真实 §6b 里短前缀有两处：按短前缀截断会取错，完整唯一行才正确。
        assert heredoc.count(SHORT_PREFIX) == 2
        assert self.prefix
        assert BOUNDARY not in self.prefix
        assert "invalid escape sequence" in self.prefix
        assert len(self.prefix) < len(heredoc)

    def test_scan_boundary_missing_or_duplicated_fails(self) -> None:
        with pytest.raises(AssertionError):
            gate_prefix(self.script_text.replace(BOUNDARY + "\n", ""))
        with pytest.raises(AssertionError):
            gate_prefix(self.script_text.replace(BOUNDARY + "\n", BOUNDARY + "\n" + BOUNDARY + "\n"))

    def test_similar_text_in_other_section_does_not_change_prefix(self) -> None:
        anchor = 'echo "=== 3. 版本号跨文件一致性检查 ==="\n'
        assert anchor in self.script_text
        mutated = self.script_text.replace(anchor, anchor + BOUNDARY + "\n")
        assert gate_prefix(mutated) == self.prefix


class RepoPythonGateReportingTests(CaseBase):
    """缺指定解释器时的降级理由必须由 skip 语义携带，不靠源码字样断言。

    直接实例化内层用例逐个调用：pytest 里“跳过”就是 `pytest.skip` 抛出的 `Skipped`，
    所以这里断言的是“内层用例全部执行 + 唯一 skip 的理由文本”，不伪造 runner 输出。
    """

    def setup_method(self) -> None:
        self.module = sys.modules[__name__]
        self.tmp = tempfile.TemporaryDirectory()
        self.defer(self.tmp.cleanup)
        self.missing = Path(self.tmp.name) / "missing-python3.12"
        assert not self.missing.exists()

    def run_inner(self, version_info=None, only=None):
        """在 mock 掉 PYTHON_312 的条件下逐个跑内层 RepoPythonGateTests。

        返回 (已执行用例名, [(用例名, skip 理由)], [(用例名, 断言失败)])；跳过由
        `pytest.skip` 表达，按它的语义捕获 `Skipped`。
        """
        names = sorted(
            name for name in dir(RepoPythonGateTests) if name.startswith("test_")
        )
        if only is not None:
            names = [name for name in names if name == only]
        executed = []
        skipped = []
        failures = []
        with mock.patch.object(self.module, "PYTHON_312", self.missing):
            version_patch = (
                mock.patch.object(sys, "version_info", version_info)
                if version_info is not None
                else contextlib.nullcontext()
            )
            with version_patch:
                for name in names:
                    case = RepoPythonGateTests()
                    case.setup_method()
                    try:
                        getattr(case, name)()
                    except pytest.skip.Exception as exc:
                        skipped.append((name, str(exc)))
                    except AssertionError as exc:
                        failures.append((name, str(exc)))
                    finally:
                        case.teardown_method()
                    executed.append(name)
        return executed, skipped, failures

    def skip_reason(self, skipped):
        reasons = [reason for _name, reason in skipped if str(self.missing) in reason]
        assert len(reasons) == 1, skipped
        return reasons[0]

    def test_missing_python312_reports_skip_and_scope(self) -> None:
        executed, skipped, failures = self.run_inner()
        assert failures == [], failures
        reason = self.skip_reason(skipped)
        # 默认解释器的 fixture 仍全部执行，不能退成整类 skip。
        inner = sorted(
            name for name in dir(RepoPythonGateTests) if name.startswith("test_")
        )
        assert executed == inner, executed
        assert "test_invalid_escape_is_rejected" in executed
        assert "test_raw_escaped_and_valid_literals_are_accepted" in executed
        assert sys.executable in reason
        assert ("%d.%d.%d" % tuple(sys.version_info[:3])) in reason
        assert "缺少指定解释器" in reason
        if sys.version_info < (3, 12):
            assert "本次只证明默认解释器分支" in reason
            assert "未验证 Python 3.12 分支" in reason
        else:
            assert "未验证指定路径的解释器" in reason
            assert "不声称两个解释器均已验证" in reason
    def test_missing_python312_reason_matches_default_version(self) -> None:
        for version_info, expected, forbidden in (
            ((3, 9, 6), "本次只证明默认解释器分支", "不声称两个解释器均已验证"),
            ((3, 12, 4), "不声称两个解释器均已验证", "本次只证明默认解释器分支"),
        ):
            executed, skipped, failures = self.run_inner(
                version_info=version_info, only="test_python312_gate_branch"
            )
            assert executed == ["test_python312_gate_branch"], version_info
            assert failures == [], version_info
            assert len(skipped) == 1, version_info
            reason = skipped[0][1]
            assert str(self.missing) in reason, version_info
            assert expected in reason, version_info
            assert forbidden not in reason, version_info
            assert ("%d.%d.%d" % version_info) in reason, version_info
