"""pytest 全局兜底：`setup_method` 失败时仍执行用例注册的清理。

pytest 的 xunit 钩子只在 `setup_method` 正常返回后才调用 `teardown_method`，而
`self.defer(...)` 的清理登记在实例上；这里在 teardown 阶段补跑未执行的清理，避免
夹具建到一半失败时把临时目录/模板仓库副本留在 /tmp。正常情况下
`teardown_method` 已清空登记表，本钩子是空操作。
"""

from __future__ import annotations

import contextlib


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
