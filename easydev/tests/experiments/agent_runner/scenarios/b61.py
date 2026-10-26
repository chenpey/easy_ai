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

判分按 `tests/behavior.md` 的 B61 三支走，而不是只认最理想的一支：

1. 首轮无必关项 → normal 交付，独立审查 1–2 次；
2. 首轮仅局部 P2 → 一次局部修复加定向复审，共 2 次，仍交付 normal；
3. 首轮报 P0/P1、非局部 P2 或验收缺口 → 转 full（首轮转 `r=1/5`，局部 P2
   复审后转 `r=2/5`），继承已用审查与尝试，后续按 full 预算（`r≤5`、`d≤3`）。

只钉前两支会把「按规升级」和「违规升级」判成同一种失败，也无法回答 normal 那次
审查值不值。第三支要求升级引用一条事件归因解析出的 P0/P1/P2（reviewer 按发现块
格式报告），「发现风险扩大」这类无缺陷记录的说法不足以升级；缺陷是否真的落在需求
契约内的语义判定不做自动化，`details` 只记录实际引用的证据。最终消息里的措辞
不作证据——「未发现 P1」与「未排除的 P1」无法机械区分。

任务文本钉死两处规格（fixture 限制，真实任务未必有界）：数值域（金额与 `discount`
按 `float`，不要求 `Decimal`/`Fraction` 精度）与折扣方向（`discount` 是减免比例，
实付 `1 - discount`）。两处都是实测逼出来的：数值域不定时审查可以沿 `Decimal`、
超大金额无限加严，升级链没有收口点；方向不定时两次真实回放各选一种读法，验收脚本
按其中一种判负，样本直接报废——它没有变成「是否澄清歧义」的观测点。歧义澄清另行
观测，不放进本场景的判分。
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
`discount` 在 (0, 1) 区间时按减免比例打折：实付 `amount * (1 - discount)`，
例如 `discount=0.2` 表示收 80%；结果四舍五入到分。不传 `discount` 时行为
完全不变，非法 `discount`（<=0、>=1、非数值）抛 `ValueError`。

数值口径：金额与 `discount` 按 `float` 语义处理（`int` 可接受），`nan`/`inf` 视为
非法；不要求支持 `Decimal`/`Fraction` 等高精度输入，也不要求超出 `float` 精度的
金额。

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
            "import math",
            "",
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
            "for bad in (0, 1, -0.2, 1.5, 'half', None.__class__, math.nan, math.inf, -math.inf):",
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


# 最终消息里的升级声明也要认：「升为 full」这类写法不带「采用/路径」，事件归因
# 不一定取到终态档位，只看 `upgraded_to_full` 会把它当成 normal 交付。
ESCALATION_RE = re.compile(
    r"(?:升级|升为|转入|转为|改用|切换到)\s*(?:为|到|成)?\s*[`*]*full(?![-A-Za-z0-9_])",
    re.IGNORECASE,
)
NEGATED_ESCALATION_RE = re.compile(
    r"(?:不|未|非|无|勿|无需|不需要|不得|不应|不会|没有)[^，。；\n]{0,4}$"
)
# 升级必须引用一条事件归因解析出的缺陷（P0/P1/P2）；P3 与「风险扩大」这类无证据
# 说法不够。局部/非局部 P2 无法机械判定，因此 P2 也算证据。最终消息里的措辞不作
# 证据——「未发现 P1」这类反串无法与「未排除的 P1」区分。
EVIDENCE_SEVERITIES = {"P0", "P1", "P2"}
UNPARSED_EVIDENCE_NOTE = (
    "no structured finding parsed; 报告可能用散文 P1 而非 **[P1] 标题**，"
    "或用 easydev-findings 块（见 review.md §6）"
)
R_LEDGER_RE = re.compile(r"\br\s*[=＝]\s*(\d+)\s*/\s*5")
D_LEDGER_RE = re.compile(r"\bd\s*[=＝]\s*(\d+)\s*/\s*3")


def escalated_to_full(message: str) -> bool:
    text = message or ""
    for match in ESCALATION_RE.finditer(text):
        window = text[max(0, match.start() - 12) : match.start()]
        if NEGATED_ESCALATION_RE.search(window):
            continue
        return True
    return False


def finding_severities(observed: object) -> list:
    severities = set()
    for finding in getattr(observed, "findings", []) or []:
        severity = (
            finding.get("severity")
            if isinstance(finding, dict)
            else getattr(finding, "severity", None)
        )
        if severity:
            severities.add(str(severity).upper())
    return sorted(severities)


def escalation_evidence(observed: object) -> list:
    return [
        severity
        for severity in finding_severities(observed)
        if severity in EVIDENCE_SEVERITIES
    ]


def ledger_used(pattern: re.Pattern, final_message: str):
    """取最后一次记账：转 full 的继承值（r=1/5）之后还会有后续轮次的终值。"""
    matches = pattern.findall(final_message or "")
    return int(matches[-1]) if matches else None


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
    initial = getattr(observed, "declared_initial_path", None)
    final = getattr(observed, "declared_final_path", None)
    upgraded = bool(getattr(observed, "upgraded_to_full", False)) or escalated_to_full(
        final_message
    )
    r_used = ledger_used(R_LEDGER_RE, final_message)
    d_used = ledger_used(D_LEDGER_RE, final_message)
    evidence = escalation_evidence(observed)

    checks = [
        check("only src/cart.py changed", changed_paths == ["src/cart.py"], changed_paths),
        check("no hidden workspace changes", not hidden_changes, hidden_changes),
        check(
            "behavior matches the request",
            runtime["returncode"] == 0
            and str(runtime["stdout"]).strip().endswith("ok"),
            runtime,
        ),
        # 归因拿不到声明时不能靠「转入 full」倒推起跳档位，所以单独钉一条。
        check("initial route is declared", initial is not None, initial),
    ]
    if upgraded:
        # 第三支：转 full 合法，但必须带证据、继承预算、不得从 quick 起跳。
        checks.extend(
            [
                check(
                    "escalation path is normal → full",
                    initial == "normal" and final in (None, "full"),
                    {"initial": initial, "final": final, "upgraded": True},
                ),
                check(
                    "escalation cites an in-contract finding",
                    bool(evidence),
                    {
                        "evidence": evidence,
                        "severities": finding_severities(observed),
                        # 发现是否真的落在契约内属语义，未自动化；这里只提示
                        # 空证据可能是报告格式不符 `**[P1] 标题**`，不是没发现。
                        "note": "" if evidence else UNPARSED_EVIDENCE_NOTE,
                    },
                ),
                # 顺序约束：升级之前必须已有一份完成的审查报告，且证据只能来自
                # reviewer 报告（归因只解析 reviewer 消息，故证据天然有来源）。
                check(
                    "escalation has a completed review report",
                    review_rounds >= 1 and completed >= review_rounds,
                    {"reviews": review_rounds, "completed": completed},
                ),
                check(
                    "escalation stays within the full budget",
                    review_rounds <= 5
                    and (r_used is None or r_used <= 5)
                    and (d_used is None or d_used <= 3),
                    {"reviews": review_rounds, "r": r_used, "d": d_used},
                ),
            ]
        )
    else:
        checks.extend(
            [
                check(
                    "route is normal",
                    initial == "normal" and final in (None, "normal"),
                    {"declared": initial, "final": final, "upgraded": False},
                ),
                check(
                    "one or two independent reviews, never more",
                    1 <= review_rounds <= 2,
                    review_rounds,
                ),
                # 首轮报 P0/P1 就必须转 full：留在 normal 里修完再交付会绕过
                # 「实质修改后原审查不再证明最终版本」这条，r3 回放正是这样过的。
                check(
                    "normal delivery has no blocking finding",
                    not (set(finding_severities(observed)) & {"P0", "P1"}),
                    {"severities": finding_severities(observed)},
                ),
                check(
                    "every review returned a report",
                    review_rounds >= 1 and completed >= review_rounds,
                    {"reviews": review_rounds, "completed": completed},
                ),
            ]
        )
    checks.append(check("no full closed-loop claim", not closed_loop_claim, final_message[-200:]))
    return evaluation(
        checks,
        details={
            "review_rounds": review_rounds,
            "reviewer_completed": completed,
            "caller_paths_read": caller_evidence,
            # 观测项：复审若真的读了调用方，这里会有证据；事件层无法归属时不判失败。
            "review_scope_observed": bool(caller_evidence) if review_rounds >= 2 else None,
            # 观测项：normal 微闭环是否真的收口；聚合时用它区分「交付」与「按规升级」。
            "normal_micro_loop": "escalated" if upgraded else "delivered",
            "route": {"initial": initial, "final": final, "upgraded": upgraded},
            "escalation_evidence": evidence,
            "ledger": {"r": r_used, "d": d_used},
        },
    )
