#!/usr/bin/env python3
"""EasyThink 行为规约的一致性门禁。

只校验可机械判定的部分，不评价行为质量：
1. TR/NT/BH 用例编号唯一、连续，且断言表与覆盖矩阵的编号集合相等；
2. 「全量定义」的范围在所有出现处一致；
3. RG/SV/EV 标签被引用前必须已在「标签索引」中定义；
4. README 的关键判据短语与 SKILL.md 同源（README 只是副本，规则以 SKILL.md 为准）；
5. 回归记录首行版本与 SKILL.md 的 metadata.version 一致。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from skill_version import FrontmatterError, read_version  # noqa: E402
BEHAVIOR = ROOT / "easythink/tests/behavior.md"
RECORDS = ROOT / "easythink/tests/runs/README.md"
SKILL = ROOT / "easythink/SKILL.md"
README = ROOT / "easythink/README.md"

# README 是给人看的规则副本，这些判据短语必须与 SKILL.md 逐字同源。
SHARED_PHRASES = (
    "当前自伤意图、计划/日期、手段在手、准备行为",
    "近期已实施自伤或药物过量",
    "且指向心境而非单纯事务忙碌",
    "且无任何上述风险标记",
    "绝不替用户推荐具体投资标的或杠杆倍数",
)

errors: list[str] = []


def read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        errors.append("无法读取 %s（%s）" % (path.relative_to(ROOT), exc))
        return ""


def ids(text: str, prefix: str) -> list[str]:
    return re.findall(r"^\| (%s-\d{2}) " % prefix, text, re.MULTILINE)


behavior = read(BEHAVIOR)
records = read(RECORDS)
skill = read(SKILL)
readme = read(README)

# 1. 用例编号：断言表 01..N 连续，覆盖矩阵与断言表集合相等
defined: set[str] = set()
for prefix in ("TR", "NT", "BH"):
    found = ids(behavior, prefix)
    unique = sorted(set(found))
    defined |= set(unique)
    expected = ["%s-%02d" % (prefix, index) for index in range(1, len(unique) + 1)]
    if unique != expected:
        errors.append("%s 编号不连续或有重复：%s" % (prefix, unique))
    elif len(found) != len(unique):
        errors.append("%s 在断言表里重复出现：%s" % (prefix, found))

matrix = set(re.findall(r"^\| ((?:TR|NT|BH)-\d{2}) ", records, re.MULTILINE))
if not matrix:
    errors.append("回归记录里找不到覆盖矩阵")
elif matrix != defined:
    errors.append(
        "覆盖矩阵与断言表不一致：仅矩阵有 %s，仅断言表有 %s"
        % (sorted(matrix - defined), sorted(defined - matrix))
    )

# 2. 全量定义范围一致，且等于断言表里实际存在的最大用例（不写死数字）
ranges = set(re.findall(r"BH-01～(\d{2})", behavior + records))
if len(ranges) > 1:
    errors.append("全量定义的 BH 上限不一致：%s" % sorted(ranges))
if not ranges:
    errors.append("找不到全量定义里的 BH 范围")
else:
    bh_ids = sorted({i for i in defined if i.startswith("BH-")})
    expected_top = bh_ids[-1][3:] if bh_ids else None
    for value in sorted(ranges):
        if value != expected_top:
            errors.append(
                "全量定义的 BH 上限 %s 与断言表最大用例 %s 不一致" % (value, expected_top)
            )

# 3. 标签：引用集合必须是标签索引里已定义的子集
legend = behavior.split("## 标签索引", 1)
if len(legend) != 2:
    errors.append("behavior.md 缺少「标签索引」小节")
else:
    defined_labels = set(re.findall(r"\*\*((?:RG|SV|EV)-\d{2}) ", legend[1].split("## 1.", 1)[0]))
    referenced = set(re.findall(r"(?:RG|SV|EV)-\d{2}", behavior)) | set(
        re.findall(r"(?:RG|SV|EV)-\d{2}", records)
    )
    missing = sorted(referenced - defined_labels)
    if missing:
        errors.append("标签被引用但未在标签索引定义：%s" % missing)
    if not defined_labels:
        errors.append("标签索引里没有解析到任何标签定义")

# 4. README 与 SKILL 的共享判据
for phrase in SHARED_PHRASES:
    for name, text in (("SKILL.md", skill), ("README.md", readme)):
        if phrase not in text:
            errors.append("%s 缺少共享判据短语：%s" % (name, phrase))

# 5. 回归记录首行版本 == SKILL.md 版本
rows = re.findall(r"^\|\s*\d{4}-\d{2}-\d{2}\s*\|\s*(\d+\.\d+\.\d+)\s*\|", records, re.MULTILINE)
if not rows:
    errors.append("回归记录里没有版本行")
else:
    try:
        # 与 check.sh §2/§3 共用同一解析器：单引号、双引号、无引号写法都算合法。
        skill_version = read_version(SKILL)
    except (FrontmatterError, OSError) as exc:
        errors.append("无法从 SKILL.md 提取 metadata.version（%s）" % exc)
    else:
        if rows[0] != skill_version:
            errors.append(
                "回归记录首行版本 %s 与 SKILL.md 版本 %s 不一致" % (rows[0], skill_version)
            )

if errors:
    for error in errors:
        print("Error: %s" % error, file=sys.stderr)
    sys.exit(1)
print(
    "OK: 行为规约门禁通过（%d 条用例、%d 个标签、%d 条共享判据、记录版本 %s）"
    % (len(defined), len(re.findall(r"\*\*(?:RG|SV|EV)-\d{2} ", behavior)), len(SHARED_PHRASES), rows[0])
)
