"""`conftest.subtests`：单个参数失败/跳过不打断同函数其余参数，失败不被吞掉。

这里在子进程里跑真实 pytest 会话（`conftest.py` 用仓库里的原件），验证的是机制
本身的可观察行为：退出码、失败/跳过的上报形态、报告里的参数标签，以及失败后参数
是否仍被执行。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from conftest import subtests
from support import CaseBase, HOOKS_ROOT

CONFTEST = HOOKS_ROOT / "conftest.py"

SAMPLE = '''\
from pathlib import Path

import pytest

from conftest import subtests

CASES = (("ok", 1), ("bad", 2), ("later", 3))
ATTEMPTED = Path(__file__).with_name("attempted.txt")


def test_one_parameter_fails_others_still_run() -> None:
    attempted = []
    for name, value in CASES:
        with subtests.test(name=name):
            attempted.append(name)
            ATTEMPTED.write_text(",".join(attempted), encoding="utf-8")
            assert value != 2, name


def test_all_parameters_pass() -> None:
    for name, value in CASES:
        with subtests.test(name=name):
            assert value > 0


def test_all_parameters_pass_again() -> None:
    for name, value in CASES:
        with subtests.test(name=name):
            assert value > 0


def test_failure_then_skip() -> None:
    with subtests.test(name="bad"):
        assert False
    pytest.skip("later unavailable")


def test_skip_inside_subtest_keeps_other_parameters() -> None:
    attempted = []
    for name in ("first", "second"):
        with subtests.test(name=name):
            attempted.append(name)
            ATTEMPTED.write_text(",".join(attempted), encoding="utf-8")
            pytest.skip("fixture missing")


def test_subtest_failure_then_outer_exception() -> None:
    with subtests.test(name="bad"):
        assert False
    assert False, "outer"


def test_plain_failure_outside_loop() -> None:
    assert False, "plain"


def test_skip_then_failure_then_later_parameter() -> None:
    attempted = []
    for name in ("first", "second", "third"):
        with subtests.test(name=name):
            attempted.append(name)
            ATTEMPTED.write_text(",".join(attempted), encoding="utf-8")
            if name == "first":
                pytest.skip("missing fixture")
            assert name != "second", name


class _Inner:
    """模拟 test_repo_gate.run_inner 那种「直接实例化内层用例」的间接调用。"""

    def test_uses_subtests(self) -> None:
        for name in ("ok", "bad"):
            with subtests.test(name=name):
                assert name != "bad", name


def test_indirect_call_does_not_hide_failure() -> None:
    # 内层失败被聚合器记录而不是抛回调用方：run_inner 那种 helper 会以为内层通过，
    # 所以这里固定「外层结束时仍必须把内层参数失败报出来」，不能假绿。
    failures = []
    try:
        _Inner().test_uses_subtests()
    except AssertionError as exc:  # 现有 run_inner 路径
        failures.append(str(exc))
    assert failures == [], failures
'''


def run_sample(root: Path, *args: str) -> subprocess.CompletedProcess:
    shutil.copy(CONFTEST, root / "conftest.py")
    (root / "test_sample.py").write_text(SAMPLE, encoding="utf-8")
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-rs", "-p", "no:cacheprovider", *args],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=120,
    )


class SubtestsAggregationTests(CaseBase):
    def setup_method(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.defer(self.tmp.cleanup)
        self.last = None

    def run_one(self, name: str) -> str:
        completed = run_sample(self.root, "test_sample.py::" + name)
        output = completed.stdout + completed.stderr
        self.last = (completed, output)
        return output

    def test_failure_is_labeled_and_remaining_parameters_run(self) -> None:
        output = self.run_one("test_one_parameter_fails_others_still_run")
        completed = self.last[0]
        # 失败仍是一次失败（不会因为被记录而静默通过）。
        assert completed.returncode == 1, output
        assert "1 failed" in output, output
        assert "子用例失败" in output, output
        assert "name='bad'" in output, output
        # 同函数其余参数照常执行（第 3 个参数没有被第一个失败吃掉）。
        assert (self.root / "attempted.txt").read_text(encoding="utf-8") == "ok,bad,later"
        # 报告不能退化成 pluggy 的旧式 hookwrapper 告警。
        assert "PluggyTeardownRaisedWarning" not in output, output

    def test_all_parameters_pass_is_green(self) -> None:
        output = self.run_one("test_all_parameters_pass")
        assert self.last[0].returncode == 0, output
        assert "1 passed" in output, output

    def test_failure_before_skip_is_not_swallowed(self) -> None:
        # 已发生的子用例失败不能被后面的 skip 变成「跳过」：那会让红跑 rc=0。
        output = self.run_one("test_failure_then_skip")
        assert self.last[0].returncode == 1, output
        assert "1 failed" in output, output
        assert "1 skipped" not in output, output
        # 两件事都要在报告里：参数失败 + 后来的 skip 理由。
        assert "name='bad'" in output, output
        assert "子用例失败" in output, output
        assert "later unavailable" in output, output

    def test_skip_inside_subtest_only_skips_that_parameter(self) -> None:
        output = self.run_one("test_skip_inside_subtest_keeps_other_parameters")
        completed = self.last[0]
        # 只有跳过、没有失败：整例按跳过上报，退出码仍为 0（与 subTest 一致）。
        assert completed.returncode == 0, output
        assert "1 skipped" in output, output
        assert "2 个子用例跳过" in output, output
        # 第二个参数没有被第一个参数的 skip 吃掉。
        assert (self.root / "attempted.txt").read_text(encoding="utf-8") == "first,second"

    def test_recorded_failures_survive_outer_exception(self) -> None:
        output = self.run_one("test_subtest_failure_then_outer_exception")
        assert self.last[0].returncode == 1, output
        assert "1 failed" in output, output
        # 循环外异常与已记录的参数失败都要保留。
        assert "name='bad'" in output, output
        assert "AssertionError: outer" in output, output
        assert "循环外抛出的异常" in output, output

    def test_plain_failure_without_subtests_is_unchanged(self) -> None:
        output = self.run_one("test_plain_failure_outside_loop")
        assert self.last[0].returncode == 1, output
        assert "AssertionError: plain" in output, output
        assert "子用例失败" not in output, output

    def test_skip_then_failure_keeps_later_parameters(self) -> None:
        output = self.run_one("test_skip_then_failure_then_later_parameter")
        assert self.last[0].returncode == 1, output
        assert "1 failed" in output, output
        # 跳过与失败同时存在时：整例是失败，两类记录都在报告里。
        assert "name='second'" in output, output
        assert "missing fixture" in output, output
        assert "1 skipped" not in output, output
        # skip 只跳第一个参数，后面的失败与第三个参数都照常发生。
        assert (self.root / "attempted.txt").read_text(encoding="utf-8") == "first,second,third"

    def test_state_is_reset_between_tests_in_one_session(self) -> None:
        # 同一会话里交替跑失败/全通过/只跳过/全通过：记录（失败与跳过）不能跨用例残留。
        completed = run_sample(
            self.root,
            "test_sample.py::test_one_parameter_fails_others_still_run",
            "test_sample.py::test_all_parameters_pass",
            "test_sample.py::test_skip_inside_subtest_keeps_other_parameters",
            "test_sample.py::test_all_parameters_pass_again",
        )
        output = completed.stdout + completed.stderr
        assert "1 failed" in output, output
        assert "1 skipped" in output, output
        assert "2 passed" in output, output

    def test_indirect_call_does_not_hide_failure(self) -> None:
        # run_inner 式间接调用：内层参数失败被记录后不抛回调用方，但外层必须失败。
        output = self.run_one("test_indirect_call_does_not_hide_failure")
        assert self.last[0].returncode == 1, output
        assert "1 failed" in output, output
        assert "name='bad'" in output, output

    def test_outside_pytest_call_phase_fails_immediately(self) -> None:
        # 不在 pytest 调用阶段（例如脚本直接 import 后调用）：不静默记账，立即失败。
        script = (
            "import sys\n"
            "from conftest import subtests\n"
            "assert not subtests.active, 'gateway must be inactive outside pytest'\n"
            "try:\n"
            "    with subtests.test(case='direct'):\n"
            "        assert False\n"
            "except AssertionError:\n"
            "    print('immediate')\n"
        )
        shutil.copy(CONFTEST, self.root / "conftest.py")
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=self.root,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert completed.returncode == 0, completed.stderr
        assert completed.stdout.strip() == "immediate", completed.stdout
