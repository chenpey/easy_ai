"""B67 (review half): the code-quality P3 wording, with a style-only control.

The workspace ships two code-quality samples that differ in cost, not in taste:

* ``src/report.py`` — four nested branches with the same fallback row written
  three times; the cost is real (adding a row shape means editing three places).
* ``src/export.py`` — trivially correct logic whose only quirks are two-space
  indentation, a camel-case loop variable and mixed quotes.

A reviewer that reports the first and not the second matches the wording in
``review.md``/``reviewer-role.md`` (P3 for real comprehension cost, style noise
suppressed).  Findings are read from the machine-checkable block only.
"""

from __future__ import annotations

from pathlib import Path

from .b16 import parse_findings, resolve_location
from .b67 import FIXTURE_FILES
from .common import check, evaluation, write_files


TITLE = "B67 code-quality review"
PROMPT = """使用 easydev 的 review 路径，以独立审查者身份审查当前仓库，只报确定发现。
本次只读：不要修改任何文件、索引、测试或配置。

报告结尾必须追加机器可核对的确定发现块（只在报告顶层给出一个块，从行首开始，
不要放进列表/引用/代码示例，也不要在报告里重复块格式示例）；块外写待验证风险、
限制与假设；块内每行一条，按 P0→P3 排序，最多 20 条；`位置` 写成 `文件:行号`，
行号必须落在被审查代码的真实行上：

```easydev-findings
P3 | 位置=src/x.py:12 | 触发=触发条件 | 后果=代价
```

判定口径：纯缩进、命名、引号与格式偏好不单独报；写法明显增加理解或修改成本时
按 P3 报，并在 `后果` 字段写明代价。
"""

COSTLY = "src/report.py"
STYLE_ONLY = "src/export.py"
ALLOWED_SEVERITIES = {"P2", "P3"}

REPORT = '''def build_report(orders, accounts, rates):
    rows = []
    for order in orders:
        if order.get("total", 0) > 0:
            account = accounts.get(order.get("owner"))
            if account is not None:
                if account.get("active"):
                    rate = rates.get(account.get("currency"), 1)
                    if rate:
                        rows.append(
                            (order.get("id"), account.get("currency"), order["total"] * rate)
                        )
                    else:
                        rows.append(
                            (order.get("id"), account.get("currency"), order["total"])
                        )
                else:
                    rows.append((order.get("id"), None, 0))
            else:
                rows.append((order.get("id"), None, 0))
        else:
            rows.append((order.get("id"), None, 0))
    return rows
'''

EXPORT = """def export_rows(rows):
  out = []
  for rowN in rows:
    if rowN['total'] > 0: out.append({'id': rowN['id']})
  return out
"""


def prepare(workspace: Path, case_root: Path) -> None:
    del case_root
    files = dict(FIXTURE_FILES)
    files.update({COSTLY: REPORT, STYLE_ONLY: EXPORT})
    write_files(workspace, files)


def evaluate(
    workspace: Path,
    changed_paths: list,
    final_message: str,
    observed: object,
) -> dict:
    entries, problems = parse_findings(final_message)
    severities = [entry["severity"] for entry in entries]
    costly = [
        entry for entry in entries if entry["location"].replace("\\", "/").startswith(COSTLY)
    ]
    style_only = [
        entry
        for entry in entries
        if entry["location"].replace("\\", "/").startswith(STYLE_ONLY)
    ]
    module_access = getattr(observed, "module_access", {})
    checks = [
        check("review changed no files", not changed_paths, changed_paths),
        check(
            "review left the git index unchanged",
            not getattr(observed, "index_changed", True),
            getattr(observed, "index_changed", None),
        ),
        check(
            "review left the runtime Skill unchanged",
            not getattr(observed, "runtime_skill_changes", []),
            getattr(observed, "runtime_skill_changes", None),
        ),
        check("review findings block is well formed", not problems, problems),
        check(
            "review ranks findings by severity",
            bool(entries) and severities == sorted(severities),
            severities,
        ),
        check(
            "review finding locations resolve",
            bool(entries) and all(resolve_location(entry, workspace) for entry in entries),
            [
                {"location": entry["location"], "ok": resolve_location(entry, workspace)}
                for entry in entries
            ],
        ),
        check(
            "review reports the costly nesting",
            bool(costly),
            [entry["location"] for entry in costly],
        ),
        check(
            "costly finding stays below blocking",
            bool(costly)
            and all(entry["severity"] in ALLOWED_SEVERITIES for entry in costly),
            [entry["severity"] for entry in costly],
        ),
        check(
            "review does not report pure style",
            not style_only,
            [entry["location"] for entry in style_only],
        ),
    ]
    return evaluation(
        checks,
        details={
            "findings": entries,
            "format_problems": problems,
            "code_quality_loaded": "code-quality.md" in module_access,
        },
    )
