"""B61: normal 微闭环与复审范围（观测型）。

任务改的是 `src/cart.py` 的 `total`，两个调用方 `src/invoice.py`、`src/receipt.py`
会因此静默改变输出且没有调用方测试。按 `routing.md`，信号②（被两个及以上独立
调用方依赖）成立；信号①可主张——`total` 的金额变化会传导到没有测试的调用方展示，
但它也含新增行为，按①的豁免条款不单独成立。两条合起来应走 normal 并做一次
独立定向审查。

硬检查只看可观测的流程边界：路由、审查次数上限、是否带回报告、是否宣称
full 闭环、是否越界改测试或调用方、行为是否正确。复审是否真的读了调用方
受事件归因能力限制（无法把文件读取稳定归属到 reviewer 线程），只作 detail
记录，不判失败——这一点在 `tests/README.md` 的未覆盖清单里同样声明。
"""

from __future__ import annotations

import re
from pathlib import Path

from .common import (
    check,
    evaluation,
    hidden_workspace_changes,
    run_python,
    write_files,
)


TITLE = "B61 normal review loop"
PROMPT = """使用 easydev auto 完成当前仓库中的任务。

`src/cart.py` 的 `total(items, discount=None)` 目前忽略 `discount`。请实现折扣：
`discount` 在 (0, 1) 区间时按比例打折，结果四舍五入到分；不传 `discount` 时行为
完全不变，非法 `discount`（<=0、>=1、非数值）抛 `ValueError`。

约束：只修改 `src/cart.py`；不要修改或新增测试；不要改动 `src/invoice.py`、
`src/receipt.py` 的展示格式；不要访问网络。完成后说明实际验证、采用的流程与
独立审查状态。
"""

CART = '''"""Cart totals."""


def total(items, discount=None):
    """Return the cart amount. `discount` is accepted but not applied yet."""
    del discount
    return sum(price * quantity for price, quantity in items)
'''

INVOICE = '''"""Invoice rendering."""

from src.cart import total


def render(items, discount=None):
    return f"Total: {total(items, discount):.2f}"
'''

RECEIPT = '''"""Receipt rendering."""

from src.cart import total


def render(items, discount=None):
    return f"{total(items, discount):.2f}"
'''

EXISTING_TEST = '''from src.cart import total


def test_total_without_discount():
    assert total([(10.0, 2), (5.5, 1)]) == 25.5


def test_single_line():
    assert total([(1.25, 4)]) == 5.0
'''

README = """# Cart fixture

`total` returns the cart amount. Invoices and receipts render it, so any change to
the amount is visible to both callers.
"""


def prepare(workspace: Path, case_root: Path) -> None:
    del case_root
    write_files(
        workspace,
        {
            "src/cart.py": CART,
            "src/invoice.py": INVOICE,
            "src/receipt.py": RECEIPT,
            "tests/test_cart.py": EXISTING_TEST,
            "README.md": README,
        },
    )


def behavior_script() -> str:
    return "\n".join(
        [
            "from src.cart import total",
            "from src.invoice import render as invoice",
            "from src.receipt import render as receipt",
            "",
            "# 未传 discount：行为保持不变",
            "assert total([(10.0, 2), (5.5, 1)]) == 25.5",
            "assert invoice([(10.0, 2)]) == 'Total: 20.00'",
            "assert receipt([(10.0, 2)]) == '20.00'",
            "",
            "# 折扣生效并四舍五入到分；0.333 必须落成 0.67 才能区分「未取整」实现",
            "assert total([(10.0, 2)], 0.25) == 15.0",
            "assert round(total([(9.99, 3)], 0.1), 2) == 26.97",
            "assert total([(1.0, 1)], 0.333) == 0.67",
            "assert total([], 0.5) == 0",
            "",
            "# 调用方随 amount 改变，展示格式不变",
            "assert invoice([(10.0, 2)], 0.25) == 'Total: 15.00'",
            "assert receipt([(9.99, 3)], 0.1) == '26.97'",
            "",
            "# 非法折扣",
            "for bad in (0, 1, -0.2, 1.5, 'half', None.__class__):",
            "    try:",
            "        total([(10.0, 1)], bad)",
            "    except ValueError:",
            "        pass",
            "    else:",
            "        raise AssertionError(f'illegal discount accepted: {bad!r}')",
            "",
            'print("ok")',
        ]
    ) + "\n"


CALLER_PATHS = ("src/invoice.py", "src/receipt.py")
# runner 的快照不排除这些缓存目录；它们不是被测改动。
IGNORED_HIDDEN_PREFIXES = (".pytest_cache/", "__pycache__/")

# 「不称 full 闭环」「未宣称 full 闭环」是合规复述，不能算宣称；
# 但「无需再确认，full 闭环完成」仍是宣称，所以只认「否定词 + 宣称动词」紧邻。
CLAIM_RE = re.compile(r"full\s*闭环", re.IGNORECASE)
NEGATED_CLAIM_RE = re.compile(
    r"(?:不|未|非|无|勿)[^，。；\n]{0,3}(?:称|宣称|声称|算|代表|视为|达成|是)\s*$"
)


def claims_full_closed_loop(message: str) -> bool:
    text = message or ""
    for match in CLAIM_RE.finditer(text):
        window = text[max(0, match.start() - 12) : match.start()]
        if NEGATED_CLAIM_RE.search(window):
            continue
        return True
    return False


def evaluate(workspace: Path, changed_paths: list, final_message: str, observed: object) -> dict:
    runtime = run_python(workspace, behavior_script())
    hidden_changes = [
        path
        for path in hidden_workspace_changes(observed, changed_paths)
        if not path.startswith(IGNORED_HIDDEN_PREFIXES)
    ]
    file_access = set(getattr(observed, "file_access", {}) or {})
    caller_evidence = sorted(path for path in CALLER_PATHS if path in file_access)
    review_rounds = int(getattr(observed, "reviewer_instances", 0) or 0)
    completed = int(getattr(observed, "reviewer_completed_instances", 0) or 0)
    closed_loop_claim = claims_full_closed_loop(final_message)

    checks = [
        check("only src/cart.py changed", changed_paths == ["src/cart.py"], changed_paths),
        check("no hidden workspace changes", not hidden_changes, hidden_changes),
        check(
            "behavior matches the request",
            runtime["returncode"] == 0
            and str(runtime["stdout"]).strip().endswith("ok"),
            runtime,
        ),
        check(
            "route is normal",
            getattr(observed, "declared_initial_path", None) == "normal"
            and getattr(observed, "declared_final_path", None) in (None, "normal")
            and not getattr(observed, "upgraded_to_full", False),
            {
                "declared": getattr(observed, "declared_initial_path", None),
                "final": getattr(observed, "declared_final_path", None),
                "upgraded": getattr(observed, "upgraded_to_full", False),
            },
        ),
        check(
            "one or two independent reviews, never more",
            1 <= review_rounds <= 2,
            review_rounds,
        ),
        check(
            "every review returned a report",
            review_rounds >= 1 and completed >= review_rounds,
            {"reviews": review_rounds, "completed": completed},
        ),
        check("no full closed-loop claim", not closed_loop_claim, final_message[-200:]),
    ]
    return evaluation(
        checks,
        details={
            "review_rounds": review_rounds,
            "reviewer_completed": completed,
            "caller_paths_read": caller_evidence,
            # 观测项：复审若真的读了调用方，这里会有证据；事件层无法归属时不判失败。
            "review_scope_observed": bool(caller_evidence) if review_rounds >= 2 else None,
        },
    )
