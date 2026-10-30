"""pytest 全局钩子：`subtests` 子用例聚合与 `setup_method` 失败后的清理兜底。

**子用例聚合**：数据驱动的用例常用「前置准备 + 循环 + 后置断言」结构。普通循环里
第一个参数断言失败就会跳出函数，红跑时既看不出是哪个参数，也看不到同函数其余参数
的结果（这正是 `unittest.subTest` 原先提供的能力）。这里给出等价物：

```python
from conftest import subtests

for command in cases:
    with subtests.test(command=command):
        assert ...
```

单个参数失败只记录并按参数标签留在报告里，循环继续；`pytest.skip`（以及
`skipTest` 语义的跳过）同样只跳过当前参数、不中断其余参数。用例结束时把已记录的
失败汇总成一次失败；只有跳过没有失败时整例仍按跳过上报。失败优先级始终高于跳过：
已经发生的参数失败不会被后面的 skip 或循环外异常吞掉（循环外异常会与已记录的失败
一并报告）。`KeyboardInterrupt`/`SystemExit` 保持原样中止。不在 pytest 用例调用
阶段使用时退化为立即失败。

**清理兜底**：pytest 的 xunit 钩子只在 `setup_method` 正常返回后才调用
`teardown_method`，而 `self.defer(...)` 的清理登记在实例上；这里在 teardown 阶段
补跑未执行的清理，避免夹具建到一半失败时把临时目录/模板仓库副本留在 /tmp。正常
情况下 `teardown_method` 已清空登记表，本钩子是空操作。
"""

from __future__ import annotations

import contextlib
import traceback
from typing import ContextManager, Iterator, List, Optional, Tuple

import pytest


def describe(label: str, exc: BaseException) -> Tuple[str, str]:
    """把一次子用例异常压成「标签 + traceback」的一条记录。"""
    return (label, "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))


class SubTests:
    """`unittest.subTest` 的等价物：单个参数失败/跳过不打断同函数其余参数。"""

    def __init__(self) -> None:
        self.failures: List[Tuple[str, str]] = []
        self.skips: List[Tuple[str, str]] = []  # (标签, skip 理由)
        self.active = False

    def reset(self) -> None:
        self.failures.clear()
        self.skips.clear()

    def test(self, **params: object) -> ContextManager[None]:
        if not self.active:
            # 不在 pytest 用例调用阶段（例如脚本直接 import 后调用）：保持立即失败。
            return contextlib.nullcontext()
        label = ", ".join("%s=%r" % (key, value) for key, value in params.items())
        return self._record(label or "<case>")

    @contextlib.contextmanager
    def _record(self, label: str) -> Iterator[None]:
        try:
            yield
        except (KeyboardInterrupt, SystemExit):
            raise
        except pytest.skip.Exception as exc:
            # 与 subTest 一致：跳过的是当前参数，不是整个函数。
            self.skips.append((label, str(exc)))
        except BaseException as exc:
            self.failures.append(describe(label, exc))

    def failure_report(self, extra: Optional[BaseException] = None) -> str:
        total = len(self.failures)
        lines = ["%d 个子用例失败（同函数其余参数已照常执行）：" % total]
        for index, (label, trace) in enumerate(self.failures, 1):
            lines.append("\n--- [%d/%d] %s ---" % (index, total, label))
            lines.append(trace.rstrip())
        if self.skips:
            lines.append("\n另有 %d 个子用例跳过：" % len(self.skips))
            for label, reason in self.skips:
                lines.append("- %s：%s" % (label, reason))
        if extra is not None and isinstance(extra, pytest.skip.Exception):
            lines.append("\n循环外跳过：%s" % extra)
        elif extra is not None:
            lines.append("\n循环外抛出的异常（首个失败优先，此前记录的子用例失败一并保留）：")
            lines.append(
                "".join(
                    traceback.format_exception(type(extra), extra, extra.__traceback__)
                ).rstrip()
            )
        return "\n".join(lines)

    def skip_reason(self) -> str:
        details = "；".join("%s：%s" % (label, reason) for label, reason in self.skips)
        return "%d 个子用例跳过：%s" % (len(self.skips), details)


subtests = SubTests()


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item):
    """用例调用前后切换子用例记账，结束时把记录到的失败/跳过汇总上报。"""
    subtests.reset()
    subtests.active = True
    try:
        result = yield
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as exc:
        # 已记录的失败不能被循环外的 skip/异常吞掉：失败优先，并把原异常一并报出。
        if subtests.failures:
            raise AssertionError(subtests.failure_report(extra=exc)) from exc
        if subtests.skips and isinstance(exc, pytest.skip.Exception):
            raise pytest.skip.Exception(subtests.skip_reason() + "；" + str(exc)) from exc
        raise
    finally:
        subtests.active = False
    if subtests.failures:
        raise AssertionError(subtests.failure_report())
    if subtests.skips:
        raise pytest.skip.Exception(subtests.skip_reason())
    return result


def pytest_runtest_teardown(item, nextitem) -> None:
    instance = getattr(item, "instance", None)
    cleanups = getattr(instance, "_cleanups", None)
    if not cleanups:
        return
    for cleanup in reversed(cleanups):
        # 失败现场已经有一次异常在传播：清理失败不得掩盖原始失败。
        with contextlib.suppress(Exception):
            cleanup()
    instance._cleanups = []
