#!/usr/bin/env python3
"""独立设计环节的确定性复验：门控判定、两本账记账、施工闸与停止条件。

覆盖 `references/design.md` 中可判定的部分，以及 `references/workflow.md` 的 normal 局部 P2 修复与双审查上限；
并在最后枚举多方案任务的可达路径，回答「候选 2 轮 + 设计审查 1 轮是否会让多方案任务几乎必然停在施工闸前」。

不覆盖：模型是否遵守规则、真实宿主的分派与实例隔离、受阻后用户显式豁免。
那些只能靠行为回放，见 tests/behavior.md 的 B60。
"""

from __future__ import annotations

import itertools
import re
import sys
from pathlib import Path

from gate_model import (
    D_CAP,
    NORMAL_REVIEW_CAP,
    NORMAL_REVIEW_MAX_CAP,
    R_CAP,
    R_RESERVE,
    DESIGNER_DELEGATED,
    DESIGNER_FALLBACK,
    DESIGNER_MAIN_AGENT,
    DESIGNER_PAUSED,
    Budget,
    NormalReviewFlow,
    RuleError,
    Stop,
    decide_gate,
    decide_review_intensity,
    decide_route,
    resolve_blocked,
    resolve_designer,
)

EASYDEV = Path(__file__).resolve().parents[3]

failures = 0
checks = 0


def ok(label: str) -> None:
    global checks
    checks += 1
    print(f"  OK: {label}")


def bad(label: str, detail: str) -> None:
    global failures, checks
    failures += 1
    checks += 1
    print(f"  FAIL: {label} -> {detail}", file=sys.stderr)


# --- 门控判定 -------------------------------------------------------------

GATE_CASES = [
    (
        "基准局部任务：同包同层，只委派独立审查者",
        {"packages": 1, "layers_crossed": 0, "multi_impl_instances": False},
        (False, []),
    ),
    (
        "同包同层改三个文件：文件数不参与判定",
        {"packages": 1, "files_in_package": 3},
        (False, []),
    ),
    (
        "单包改持久化格式",
        {"packages": 1, "persistent_data": True},
        (True, ["公共契约或持久数据"]),
    ),
    (
        "名义三个方案但项目惯例已排除两个",
        {"candidate_options": 3, "candidates_both_conditions": False},
        (False, []),
    ),
    (
        "用户显式要求候选对比（产品选择已定）",
        {"user_requests_comparison": True},
        (True, ["多方案"]),
    ),
    ("跨一个额外包", {"packages": 2}, (True, ["跨边界"])),
    ("越过接口层边界", {"packages": 1, "layers_crossed": 1}, (True, ["跨边界"])),
    (
        "仅两个实施实例接力，不增加独立验收阶段",
        {"multi_impl_instances": True},
        (False, []),
    ),
    (
        "两个独立验收阶段",
        {"independent_acceptance_stages": 2},
        (True, ["多阶段施工"]),
    ),
    (
        "局部单行鉴权改动（高风险边界）",
        {"packages": 1, "high_risk": True},
        (True, ["高风险边界"]),
    ),
    (
        "把材料交给独立审查者不构成多阶段",
        {"packages": 1, "multi_impl_instances": False, "candidate_options": 1},
        (False, []),
    ),
    (
        "跨包且改公共契约：两项同时命中",
        {"packages": 2, "public_contract": True},
        (True, ["跨边界", "公共契约或持久数据"]),
    ),
    (
        "单包高风险边界仅改注释或日志措辞：其他四项未命中，不启用",
        {"packages": 1, "high_risk": True, "changes_high_risk_behavior": False},
        (False, []),
    ),
    (
        "跨两个具名包仅改高风险边界文案：第 5 项豁免不抵销第 1 项",
        {"packages": 2, "high_risk": True, "changes_high_risk_behavior": False},
        (True, ["跨边界"]),
    ),
    (
        "高风险三态 confirmed 与布尔替身一致",
        {"packages": 1, "high_risk": "confirmed"},
        (True, ["高风险边界"]),
    ),
    (
        "高风险三态 excluded 不被当成真值",
        {"packages": 1, "high_risk": "excluded"},
        (False, []),
    ),
    (
        "高风险纯展示文案：presentation 豁免与布尔口径一致",
        {"packages": 1, "high_risk": "confirmed", "presentation_or_comment_only": True},
        (False, []),
    ),
]


def run_gate_cases() -> None:
    print("=== 门控判定（design.md「启用门控」） ===")
    for label, task, expected in GATE_CASES:
        got = decide_gate(task)
        if (got["enabled"], got["hits"]) != expected:
            bad(label, f"得到 {got}，预期 enabled={expected[0]} hits={expected[1]}")
        else:
            ok(label)


# --- 分流判定与高风险降档确认 ----------------------------------------------
ROUTE_CASES = [
    # (标签, 任务, 预期路径, 预期动作)
    ("纯展示文案且无检查：不命中信号 1，quick",
     {"presentation_or_comment_only": True}, "quick", []),
    ("改既有行为且无改前检查：命中信号 1，normal",
     {}, "normal", []),
    ("改前已有检查能拦回归：无信号，quick",
     {"prior_check_catches_regression": True}, "quick", []),
    ("作者本次新增的测试不算现成检查：仍 normal",
     {"prior_check_catches_regression": False, "author_added_tests": True}, "normal", []),
    ("纯新增行为不因信号 1 命中：quick",
     {"changes_existing_behavior": False}, "quick", []),
    ("两个独立消费方：normal",
     {"prior_check_catches_regression": True, "independent_consumers": 2}, "normal", []),
    ("共享契约且有未改使用方：normal",
     {"prior_check_catches_regression": True, "shared_contract": True, "unchanged_users": True}, "normal", []),
    ("共享契约但使用方全部随改：quick",
     {"prior_check_catches_regression": True, "shared_contract": True, "unchanged_users": False}, "quick", []),
    ("用户要求独立审查：normal",
     {"prior_check_catches_regression": True, "user_requests_independent_review": True}, "normal", []),
    ("高风险已确认涉及：full",
     {"high_risk": "confirmed"}, "full", []),
    ("高风险尚未核实：不当作已排除，full",
     {"high_risk": "unverified", "prior_check_catches_regression": True}, "full", []),
    ("高风险已排除：按信号定档",
     {"high_risk": "excluded", "prior_check_catches_regression": True}, "quick", []),
    ("高风险布尔真值同样进 full",
     {"high_risk": True}, "full", []),
    ("高风险纯展示文案不因边界字段进 full",
     {"high_risk": "confirmed", "presentation_or_comment_only": True,
      "prior_check_catches_regression": True}, "quick", []),
    ("高风险指定 quick，披露后用户明确拒绝放宽：按 full 报告偏离",
     {"high_risk": "confirmed", "user_tier": "quick", "interactive": True, "relax_confirmed": False},
     "full", ["报告偏离"]),
    ("重大外部契约或持久数据影响：full",
     {"major_contract_or_data": True}, "full", []),
    ("用户要求完整闭环：full",
     {"user_requests_full": True, "prior_check_catches_regression": True}, "full", []),
    ("设计/审查等只读交付不自动扩大为实现",
     {"read_only_deliverable": True, "high_risk": "confirmed"}, "只读交付", []),
    ("用户指定高于所需：按用户指定",
     {"user_tier": "full", "prior_check_catches_regression": True}, "full", []),
    ("非高风险下用户指定低档位：按用户指定，无需放宽确认",
     {"user_tier": "quick"}, "quick", []),
    ("高风险下指定 quick（交互、未确认）：暂停披露，不静默降档",
     {"high_risk": "confirmed", "user_tier": "quick", "interactive": True}, "暂停披露并等待放宽确认",
     ["披露高风险边界与缺失独立审查风险"]),
    ("高风险下指定 normal 且已放宽确认：按指定并断言未覆盖风险",
     {"high_risk": "confirmed", "user_tier": "normal", "interactive": True, "relax_confirmed": True},
     "normal", ["断言未覆盖风险"]),
    ("高风险下指定 quick（非交互）：按 full 执行并报告偏离",
     {"high_risk": "confirmed", "user_tier": "quick", "interactive": False}, "full", ["报告偏离"]),
    ("非交互时即使带放宽确认标记也按 full",
     {"high_risk": "unverified", "user_tier": "normal", "interactive": False, "relax_confirmed": True},
     "full", ["报告偏离"]),
    ("设计门控命中不把 quick/normal 反向升级",
     {"prior_check_catches_regression": True, "public_contract": True, "packages": 3}, "quick", []),
]

INTENSITY_CASES = [
    ("高风险未指定强度：深", {"high_risk": "confirmed"}, "深"),
    ("高风险指定轻（交互、未确认）：仍按深", {"high_risk": "confirmed", "requested_intensity": "轻", "interactive": True}, "深"),
    ("高风险指定轻且已放宽确认：按指定", {"high_risk": "confirmed", "requested_intensity": "轻", "interactive": True, "relax_confirmed": True}, "轻"),
    ("高风险指定轻（非交互）：仍按深", {"high_risk": "confirmed", "requested_intensity": "轻", "interactive": False, "relax_confirmed": True}, "深"),
    ("非高风险指定轻：按指定", {"requested_intensity": "轻"}, "轻"),
    ("高风险纯展示文案不升级强度",
     {"high_risk": "confirmed", "presentation_or_comment_only": True, "requested_intensity": "轻"}, "轻"),
]


def run_route_cases() -> None:
    print("=== 分流判定与降档确认（workflow.md「分流判定」、review.md「强度分档」） ===")
    for label, task, path, actions in ROUTE_CASES:
        got = decide_route(task)
        if (got["path"], got["actions"]) != (path, actions):
            bad(label, f"得到 {got['path']}/{got['actions']}，预期 {path}/{actions}")
        else:
            ok(label)
    for label, task, expected in INTENSITY_CASES:
        got = decide_review_intensity(task)
        if got != expected:
            bad(label, f"得到 {got}，预期 {expected}")
        else:
            ok(label)


# --- 记账与停止 -----------------------------------------------------------


def run_budget_cases() -> None:
    print("=== 两本账与施工闸（design.md「预算」「收敛与施工闸」） ===")

    # 不启用门控的局部 full：没有施工闸，也不记 design账。
    b = Budget(design_enabled=False)
    b.full_review()
    b.finish()
    if (b.d, b.r) == (0, 1):
        ok("不启用门控：d 不计，单次完整审查 r=1 完成")
    else:
        bad("不启用门控", f"d={b.d} r={b.r}")

    # 单方案 + 设计审查一次通过。
    b = Budget()
    b.design_review()
    b.freeze()
    b.open_gate()
    b.full_review()
    b.finish()
    if (b.d, b.r) == (1, 1):
        ok("单方案设计审查通过：d=1 r=1")
    else:
        bad("单方案路径", f"d={b.d} r={b.r}")

    # 候选首轮 + 设计审查一次通过。
    b = Budget()
    b.candidate()
    b.design_review()
    b.freeze()
    b.open_gate()
    b.full_review()
    b.finish()
    if (b.d, b.r) == (2, 1):
        ok("候选首轮 + 设计审查通过：d=2 r=1")
    else:
        bad("候选首轮路径", f"d={b.d} r={b.r}")

    # 候选第二轮未获用户明确要求：应被拒绝。
    b = Budget()
    b.candidate()
    try:
        b.candidate()
    except RuleError as exc:
        ok(f"候选第二轮未获用户 opt-in 被拒绝（{exc}）")
    else:
        bad("候选第二轮未获 opt-in", "未被拒绝")

    # 候选两轮 + 设计审查报出缺陷：d 已满，重写后的审查无轮可用。
    b = Budget(candidate_second_opt_in=True)
    b.candidate()
    b.candidate()
    b.design_review(findings=1)
    try:
        b.rework()
        b.design_review()
    except Stop as stop:
        ok(f"候选两轮后设计审查报缺陷，重审无轮可用而停止（{stop.state}）")
    else:
        bad("候选两轮后报缺陷", "未停止")

    # 默认分配（候选首轮 + 设计审查报缺陷 + 升版重审）恰好用完 d/3。
    b = Budget()
    b.candidate()
    b.design_review(findings=1)
    b.rework()
    b.design_review()
    b.freeze()
    b.open_gate()
    b.full_review()
    b.finish()
    if (b.d, b.r) == (3, 1):
        ok("默认分配：候选 1 轮 + 设计审查 1 轮 + 升版重审 1 轮 = d/3，r=1 完成")
    else:
        bad("默认分配", f"d={b.d} r={b.r}")

    # 复审必须有新的修改证据，不得在同一已审版本上直接清零缺陷。
    b = Budget(design_enabled=False)
    b.full_review(findings=1)
    try:
        b.rereview()
    except RuleError as exc:
        ok(f"无修复证据的复审被拒绝（{exc}）")
    else:
        bad("无修复证据的复审", "未被拒绝")
    b.resolve_findings()
    b.rereview()
    if b.open_findings == 0 and b.finish() == "闭环完成":
        ok("记录修复后复审可关闭缺陷并完成")
    else:
        bad("修复后复审", f"open={b.open_findings} review_current={b.review_current}")

    # 设计缺陷不得在施工闸前用 resolve_findings 核销，必须先升版重审。
    b = Budget()
    b.design_review(findings=1)
    try:
        b.resolve_findings()
    except RuleError as exc:
        ok(f"闸前核销设计缺陷被拒绝（{exc}）")
    else:
        bad("闸前核销设计缺陷", "未被拒绝")
    b.rework()
    b.design_review()
    b.freeze()
    b.open_gate()
    b.full_review()
    if b.finish() == "闭环完成":
        ok("升版重审报 0 项后可直接冻结并闭环")
    else:
        bad("升版重审闭环", b.finish())

    # 受阻轮允许原版本重派，不必为重派虚增版本。
    b = Budget()
    b.design_review(blocked=True)
    b.design_review()
    b.freeze()
    b.open_gate()
    b.full_review()
    if (b.rework_count, b.d, b.finish()) == (0, 2, "闭环完成"):
        ok("受阻轮可在原版本重派，重派成功即开闸")
    else:
        bad("受阻轮重派", f"rework={b.rework_count} d={b.d}")

    # 纯 design / plan 路径：无 d/r 账，支持至多 2 轮候选评估无需 opt-in，第 3 轮被拒绝。
    b = Budget(pure_design=True)
    b.candidate()
    b.candidate()
    if (b.d, b.r) == (0, 0) and b.candidate_rounds == 2:
        ok("纯 design 候选两轮无须 opt-in 且不占 d/r 账（d=0 r=0）")
    else:
        bad("纯 design 候选两轮", f"d={b.d} r={b.r} rounds={b.candidate_rounds}")
    try:
        b.candidate()
    except RuleError as exc:
        ok(f"纯 design 候选超过 2 轮被拒绝（{exc}）")
    else:
        bad("纯 design 候选超过 2 轮", "未被拒绝")
    try:
        b.design_review()
    except RuleError as exc:
        ok(f"纯 design 路径禁止发起施工设计审查（{exc}）")
    else:
        bad("纯 design 路径发起设计审查", "未被拒绝")
    try:
        b.rereview()
    except RuleError as exc:
        ok(f"纯 design 路径禁止发起施工复审（{exc}）")
    else:
        bad("纯 design 路径发起施工复审", "未被拒绝")
    try:
        b.full_review()
    except RuleError as exc:
        ok(f"纯 design 路径禁止发起完整初审（{exc}）")
    else:
        bad("纯 design 路径发起完整初审", "未被拒绝")
    try:
        b.incremental_review()
    except RuleError as exc:
        ok(f"纯 design 路径禁止发起阶段增量审查（{exc}）")
    else:
        bad("纯 design 路径发起阶段增量审查", "未被拒绝")

    # 未过施工闸：不得发起复审。
    b_premature = Budget()
    try:
        b_premature.rereview()
    except RuleError as exc:
        ok(f"未过施工闸禁止发起复审（{exc}）")
    else:
        bad("未过施工闸发起复审", "未被拒绝")

    # 设计审查受阻：不得据此进入施工。
    b = Budget()
    b.design_review(blocked=True)
    try:
        b.freeze()
    except Stop as stop:
        ok(f"受阻轮不算缺陷已关闭（{stop.state}）")
    else:
        bad("受阻轮", "被当成已关闭")

    # 施工中实现者发现契约不可行，剩余 r 恰好两轮：允许升版重审。
    b = Budget()
    b.design_review()
    b.freeze()
    b.open_gate()
    b.incremental_review()
    b.incremental_review()
    b.incremental_review()  # r=3，剩余 2
    b.rework()
    b.design_review()
    b.freeze()
    b.open_gate()
    b.full_review(findings=1)
    b.resolve_findings()
    b.rereview()
    b.finish()
    if b.r == R_CAP:
        ok("剩余 r=2 时允许升版重审，重审后完整初审 + 修复复审刚好用完 r/5")
    else:
        bad("剩余 r=2 时升版重审", f"r={b.r}")

    # 施工审核发现设计根因，剩余 r 不足两轮：必须停止。
    b = Budget()
    b.design_review()
    b.freeze()
    b.open_gate()
    for _ in range(4):
        b._spend_r("既有施工审核")
    try:
        b.rework()
    except Stop as stop:
        if b.rework_count == 0 and b.design_reviewed and b.gate_open:
            ok(f"剩余 r=1 时在改状态前禁止升版重审（{stop.reason[:24]}…）")
        else:
            bad(
                "剩余 r=1 时升版重审",
                f"停止前已改状态：rework_count={b.rework_count} "
                f"design_reviewed={b.design_reviewed} gate_open={b.gate_open}",
            )
    else:
        bad("剩余 r=1 时升版重审", "未停止")

    # 阶段增量审查合批上限：第 4 次必须被拒。
    b = Budget()
    b.design_review()
    b.freeze()
    b.open_gate()
    accepted = 0
    for _ in range(5):
        try:
            b.incremental_review()
            accepted += 1
        except Stop:
            break
    if accepted == 3 and b._r_left() == R_RESERVE:
        ok("阶段增量审查最多 3 轮，之后仍余 2 轮给完整审查与复审")
    else:
        bad("阶段增量审查合批", f"accepted={accepted} left={b._r_left()}")

    # 完整初审报缺陷但已无复审轮次。
    b = Budget(design_enabled=False)
    for _ in range(R_CAP - 1):
        b._spend_r("占位")
    try:
        b.full_review(findings=1)
    except Stop as stop:
        ok(f"完整初审报缺陷且无复审轮次时停止（{stop.reason[:20]}…）")
    else:
        bad("无复审轮次", "未停止")

    # 施工期 d 用尽又需重审：停止并保留现场。
    b = Budget(candidate_second_opt_in=True)
    b.candidate()
    b.candidate()
    b.design_review()
    b.freeze()
    b.open_gate()
    try:
        b.rework()
    except Stop as stop:
        if b.rework_count == 0 and b.design_reviewed:
            ok(f"施工期 d 用尽时在改状态前停止（{stop.reason[:20]}…）")
        else:
            bad("施工期 d 用尽", f"停止前已改状态：rework_count={b.rework_count} design_reviewed={b.design_reviewed}")
    else:
        bad("施工期 d 用尽", "未停止")

    # 升版重写本身不计轮：一次升版只消耗 d 的一轮（重写后的那次审查）。
    b = Budget()
    b.design_review(findings=1)
    b.rework()
    if b.d == 1:
        ok("升版重写不消耗 d（只有重写后的设计审查计轮）")
    else:
        bad("升版重写计轮", f"d={b.d}")


# --- normal 一次定向审查与转 full ----------------------------------------


def run_normal_cases() -> None:
    print("=== normal：一次独立定向审查与转 full ===")

    flow = NormalReviewFlow(author_instance_id="developer-1")
    flow.record_review("reviewer-1", "未发现确定缺陷。")
    if flow.review_count == 1 and flow.finish() == "normal 完成":
        ok("一个不同实例回传非空报告：normal 可交付")
    else:
        bad("normal 正常路径", f"count={flow.review_count} finish={flow.finish()}")

    no_review = NormalReviewFlow(author_instance_id="developer-1")
    if no_review.finish() == "normal 未完成":
        ok("没有审查实例：不得称 normal 完成")
    else:
        bad("normal 无审查", no_review.finish())

    self_review = NormalReviewFlow(author_instance_id="developer-1")
    try:
        self_review.record_review("developer-1", "自查通过。")
    except RuleError:
        self_review.record_review("reviewer-1", "未发现确定缺陷。")
        if self_review.finish() == "normal 完成":
            ok("作者自查无效且不消耗名额，随后有效独立审查可完成")
        else:
            bad("normal 作者自查", self_review.finish())
    else:
        bad("normal 作者自查", "未被拒绝")

    for report_label, empty_report_text in (
        ("空字符串", ""),
        ("纯空白", " \n "),
    ):
        empty_report = NormalReviewFlow(author_instance_id="developer-1")
        try:
            empty_report.record_review("reviewer-1", empty_report_text)
        except RuleError:
            empty_report.record_review("reviewer-1", "未发现确定缺陷。")
            if empty_report.finish() == "normal 完成":
                ok(f"{report_label}完成报告无效且不消耗名额，随后有效报告可完成")
            else:
                bad(f"normal {report_label}报告", empty_report.finish())
        else:
            bad(f"normal {report_label}报告", "未被拒绝")

    two_reviews = NormalReviewFlow(author_instance_id="developer-1")
    two_reviews.record_review("reviewer-1", "未发现确定缺陷。")
    try:
        two_reviews.record_review("reviewer-2", "补充报告。")
    except RuleError as exc:
        if str(exc) == "无缺陷或未做局部修复不得进行复审" and two_reviews.review_count == NORMAL_REVIEW_CAP:
            ok("首轮无缺陷时不得发起复审：无缺陷或未做局部修复不得进行复审")
        else:
            bad("normal 复审前置", f"异常={exc} count={two_reviews.review_count}")
    else:
        bad("normal 复审前置", "未做局部修复仍被允许复审")

    local_p2 = {"severity": "P2", "in_scope": True, "is_local": True}

    repaired = NormalReviewFlow(author_instance_id="developer-1")
    repaired.record_review("reviewer-1", "首轮仅报局部 P2。", findings=[dict(local_p2)])
    repaired.record_repair()
    repaired.record_review("reviewer-1", "局部修复已关闭原缺陷。")
    if (
        repaired.review_count == NORMAL_REVIEW_MAX_CAP
        and repaired.path == "normal"
        and repaired.finish() == "normal 完成"
    ):
        ok("首轮局部 P2 修复后定向复审通过：审查 2 次、normal 完成、不转 full")
    else:
        bad(
            "normal 局部修复闭环",
            f"count={repaired.review_count} path={repaired.path} finish={repaired.finish()}",
        )

    still_open = NormalReviewFlow(author_instance_id="developer-1", repair_attempts=1)
    still_open.record_review("reviewer-1", "首轮仅报局部 P2。", findings=[dict(local_p2)])
    still_open.record_repair()
    still_open.record_review("reviewer-1", "原缺陷未关闭。", findings=[dict(local_p2)])
    if (
        still_open.path == "full"
        and still_open.r == 2
        and still_open.finish() == "full 未闭环"
        and still_open.repair_attempts == 2
    ):
        ok("局部 P2 复审仍未关闭：转 full 记 r=2/5，已用修复尝试不重置")
    else:
        bad(
            "normal 复审后转 full",
            f"path={still_open.path} r={still_open.r} attempts={still_open.repair_attempts} "
            f"finish={still_open.finish()}",
        )

    capped = NormalReviewFlow(author_instance_id="developer-1")
    capped.record_review("reviewer-1", "首轮仅报局部 P2。", findings=[dict(local_p2)])
    capped.record_repair()
    capped.record_review("reviewer-1", "局部修复已关闭原缺陷。")
    try:
        capped.record_review("reviewer-2", "第三次审查。")
    except RuleError as exc:
        if str(exc) == "normal 独立审查已达上限（至多 2 次），不得形成修复循环":
            ok("第三次审查被拒绝：normal 独立审查至多 2 次")
        else:
            bad("normal 审查硬上限", f"异常={exc}")
    else:
        bad("normal 审查硬上限", "第三次审查未被拒绝")

    for label, finding in (
        ("P2 触及公共契约", {"severity": "P2", "in_scope": False}),
        ("P2 未确证属局部", {"severity": "P2", "in_scope": True}),
    ):
        not_local = NormalReviewFlow(author_instance_id="developer-1")
        not_local.record_review("reviewer-1", "有一个未确证局部的 P2。", findings=[finding])
        if not_local.path == "full" and not_local.r == 1 and not_local.finish() == "full 未闭环":
            ok(f"{label}：不得当局部 P2 放行，转 full 记 r=1/5")
        else:
            bad(label, f"path={not_local.path} r={not_local.r} finish={not_local.finish()}")

    upgrade = NormalReviewFlow(
        author_instance_id="developer-1",
        repair_attempts=1,
    )
    upgrade.record_review(
        "reviewer-1",
        "有必关项。",
        findings=[{"severity": "P2", "in_scope": True}],
    )
    if (
        upgrade.path == "full"
        and upgrade.r == 1
        and R_CAP == 5
        and upgrade.repair_attempts == 1
        and upgrade.finish() == "full 未闭环"
    ):
        ok("范围内 P2 转 full：首次审查继承 r=1/5，修复尝试与审查不重置")
    else:
        bad(
            "normal 转 full",
            f"path={upgrade.path} r={upgrade.r} attempts={upgrade.repair_attempts} "
            f"finish={upgrade.finish()}",
        )

    # normal 候选评审第 2 轮须用户明确 opt-in，且转 full 后与 r 一并继承进 Budget。
    cand_no_opt = NormalReviewFlow(author_instance_id="developer-1")
    cand_no_opt.record_candidate()
    try:
        cand_no_opt.record_candidate()
    except RuleError as exc:
        ok(f"normal 候选第 2 轮未获 opt-in 被拒绝（{exc}）")
    else:
        bad("normal 候选第 2 轮未获 opt-in", "未被拒绝")

    # normal 候选 1 轮 + 初审转 full（d=1, r=1）+ 补设计门控报缺陷 + 升版重审闭环（d=3, r=3）。
    cand_one_up = NormalReviewFlow(author_instance_id="developer-1")
    cand_one_up.record_candidate()
    cand_one_up.record_review("reviewer-1", "非局部 P2。", findings=[{"severity": "P2", "in_scope": False}])
    b_from_normal = cand_one_up.to_full_budget(design_enabled=True)
    b_from_normal.design_review(findings=1)
    b_from_normal.rework()
    b_from_normal.design_review()
    b_from_normal.open_gate()
    b_from_normal.full_review(findings=1)
    b_from_normal.resolve_findings()
    b_from_normal.rereview()
    if (b_from_normal.d, b_from_normal.r, b_from_normal.finish()) == (3, 3, "闭环完成"):
        ok("normal 候选 1 轮转 full 补设计门控：报缺陷后升版重审仍可闭环（d=3/3 r=3/5）")
    else:
        bad(
            "normal 候选 1 轮转 full 联合路径",
            f"d={b_from_normal.d} r={b_from_normal.r}",
        )

    # normal 候选 2 轮（opt-in）+ 复审未关闭转 full（d=2, r=2）：补设计门控报缺陷时 d=3/3 停止，不留脏现场。
    cand_two_up = NormalReviewFlow(author_instance_id="developer-1", candidate_second_opt_in=True)
    cand_two_up.record_candidate()
    cand_two_up.record_candidate()
    cand_two_up.record_review("reviewer-1", "局部 P2。", findings=[dict(local_p2)])
    cand_two_up.record_repair()
    cand_two_up.record_review("reviewer-1", "仍未关闭。", findings=[dict(local_p2)])
    b_two = cand_two_up.to_full_budget(design_enabled=True)
    b_two.design_review(findings=1)
    try:
        b_two.rework()
    except Stop as stop:
        if (b_two.d, b_two.r, b_two.rework_count, b_two.gate_open) == (3, 2, 0, False):
            ok(f"normal 候选 2 轮转 full（d=2 r=2）后设计审查报缺陷：d=3/3 在改状态前停止（{stop.state}）")
        else:
            bad("normal 候选 2 轮转 full 停止状态", f"d={b_two.d} r={b_two.r} rework={b_two.rework_count}")
    else:
        bad("normal 候选 2 轮转 full 报缺陷", "未按 d=3/3 停止")

    # full（已耗 d=1 设计审查、r=1）降级 normal（无新增候选），初审非局部 P2 重入 full：
    # 必须保留历史 d=1（总 d=1、候选轮数=0、r=2），重入后经过 1 次报缺陷设计审查 + 1 次升版重审即达 d=3/3 上限。
    reenter = NormalReviewFlow(
        author_instance_id="developer-1",
        prior_design_reviews=1,
        prior_full_reviews=1,
    )
    reenter.record_review("reviewer-1", "非局部 P2。", findings=[{"severity": "P2", "in_scope": False}])
    b_reenter = reenter.to_full_budget(design_enabled=True)
    if (b_reenter.d, b_reenter.r, b_reenter.candidate_rounds) == (1, 2, 0):
        ok("full(d=1,r=1)→normal→full：保留历史设计总账 d=1、r=2，且不把历史设计审查误记为候选轮次")
    else:
        bad(
            "full→normal→full 继承账本",
            f"d={b_reenter.d} r={b_reenter.r} candidate_rounds={b_reenter.candidate_rounds}",
        )
    b_reenter.design_review(findings=1)
    b_reenter.rework()
    b_reenter.design_review(findings=1)
    try:
        b_reenter.rework()
    except Stop as stop:
        if (b_reenter.d, b_reenter.r, b_reenter.rework_count) == (3, 2, 1):
            ok(f"full→normal→full 继承 d=1 后再经两轮设计审查即触发 d=3/3 停止（{stop.state}）")
        else:
            bad("full→normal→full 额度耗尽", f"d={b_reenter.d} r={b_reenter.r} rework={b_reenter.rework_count}")
    else:
        bad("full→normal→full 额度耗尽", "丢失历史 d=1 导致多获得一轮设计重审")


# --- 宿主能力替身：设计分派形态与受阻处置 --------------------------------

HOST_CASES = [
    (
        "inherit + 有委派：主代理设计，模型级 0 可自行施工",
        dict(model="inherit"),
        dict(mode=DESIGNER_MAIN_AGENT, counts_dr=True, must_delegate_developer=False),
    ),
    (
        "inherit + 无委派：暂停报受阻，不记 d/r",
        dict(model="inherit", delegation_available=False),
        dict(mode=DESIGNER_PAUSED, counts_dr=False, gate="closed"),
    ),
    (
        "具体模型可用 + developer 为 inherit：主代理可自行实施",
        dict(model="gpt-6.1-sol"),
        dict(mode=DESIGNER_DELEGATED, must_delegate_developer=False),
    ),
    (
        "具体模型可用 + developer 非 inherit：仍须委派（不得绕过配置）",
        dict(model="gpt-6.1-sol", developer_non_inherit=True),
        dict(mode=DESIGNER_DELEGATED, must_delegate_developer=True),
    ),
    (
        "具体模型可用 + 升级级：仍须委派",
        dict(model="gpt-6.1-sol", in_upgrade_stage=True),
        dict(mode=DESIGNER_DELEGATED, must_delegate_developer=True),
    ),
    (
        "具体模型不可用：暂停且不回落主代理",
        dict(model="gpt-6.1-sol", model_available=False),
        dict(mode=DESIGNER_PAUSED, gate="closed", counts_dr=False),
    ),
    (
        "用户明确禁止委派：即使宿主能委派也走兜底",
        dict(model="gpt-6.1-sol", user_forbids_delegation=True),
        dict(mode=DESIGNER_FALLBACK, counts_dr=False),
    ),
    (
        "用户明确禁止委派 + developer 非 inherit：主代理接管施工而不强制委派",
        dict(model="gpt-6.1-sol", user_forbids_delegation=True, developer_non_inherit=True),
        dict(mode=DESIGNER_FALLBACK, counts_dr=False, must_delegate_developer=False),
    ),
    (
        "具体模型 + 宿主无委派能力：暂停报受阻，不静默回落",
        dict(model="gpt-6.1-sol", delegation_available=False),
        dict(mode=DESIGNER_PAUSED, counts_dr=False, gate="closed"),
    ),
    (
        "用户明确禁止委派 + 宿主无委派能力：用户授权优先，主代理兜底",
        dict(model="gpt-6.1-sol", user_forbids_delegation=True, delegation_available=False, developer_non_inherit=True),
        dict(mode=DESIGNER_FALLBACK, counts_dr=False, must_delegate_developer=False),
    ),
    (
        "宿主继承上下文：实例分离仍可审查，披露隔离限制",
        dict(context_isolated=False),
        dict(mode=DESIGNER_MAIN_AGENT, counts_dr=True, must_delegate_developer=False,
             context_isolation="limited"),
    ),
    (
        "独立设计实例继承上下文：保留实例分工和隔离限制",
        dict(model="host-designer", context_isolated=False),
        dict(mode=DESIGNER_DELEGATED, counts_dr=True, must_delegate_developer=False,
             context_isolation="limited"),
    ),
]


def run_host_cases() -> None:
    print("=== 宿主能力替身：设计分派形态（design.md「设计者」「实例隔离」） ===")
    for label, kwargs, expected in HOST_CASES:
        got = resolve_designer(**kwargs)
        mismatch = {k: (got.get(k), v) for k, v in expected.items() if got.get(k) != v}
        if mismatch:
            bad(label, f"不符为 {mismatch}")
        else:
            ok(label)

    print("=== 宿主能力替身：受阻轮处置（design.md「收敛与施工闸」） ===")
    blocked = resolve_blocked()
    if blocked["gate"] == "blocked" and blocked["may_claim_closed_loop"] is False:
        ok("受阻默认停闸，且不得声称闭环完成")
    else:
        bad("受阻默认", str(blocked))

    waived = resolve_blocked(user_accepts_no_independent_review=True)
    if (
        waived["gate"] == "open_with_waiver"
        and "未独立审查" in waived["must_flag"]
        and waived["may_claim_closed_loop"] is False
    ):
        ok("用户显式豁免可放行，但须标「未独立审查」且仍不得声称闭环")
    else:
        bad("受阻豁免", str(waived))


# --- 文档契约：把审计里靠人眼找的字段缺失变成回归 --------------------------

DOC_CONTRACT = [
    (
        "主代理设计不额外强制委派施工",
        "easydev/references/design.md",
        "## 实例隔离",
        ["主代理可继续施工", "full 编排"],
        {"forbidden": ["施工必须委派", "施工须委派独立开发者"]},
    ),
    (
        "多阶段按验收而非实施实例或模型升级判定",
        "easydev/references/design.md",
        "## 启用门控",
        ["独立验收", "实施实例接力", "不单独触发", "模型升级"],
        {"forbidden": ["≥2 个实施实例接力"]},
    ),
    (
        "normal 单写并受历史预算约束",
        "easydev/references/workflow.md",
        "## normal 流程",
        ["实施保持单写入者", "只读并行", "历史预算", "无复审机会的修复"],
    ),
    (
        "full 委派条件区分模型级与交付阶段",
        "easydev/references/workflow.md",
        "## full 编排",
        ["模型级 0", "模型与深度均为 `inherit`", "自上而下取第一条", "自查（非独立）"],
    ),
    (
        "并行判断门槛、验证依赖与集成预算",
        "easydev/references/workflow.md",
        "## 并行执行",
        ["并行写入仅限 full", "独立性与隔离条件全部成立才可并行，否则串行",
         "每片唯一写入者", "写集互不相交", "不依赖其他片尚未完成的结果",
         "共享契约已冻结，各片可独立验证", "单片工作量须大于调度开销",
         "任一写集会改变其他片验证前提时串行", "锁文件、生成物、迁移序号、公共配置",
         "判不准、无并行能力或被限流时串行", "需运行构建或测试的并行写入须用独立工作树",
         "端口、数据库、测试数据与输出目录须隔离", "各批验证及最终整仓验证期间所有相关写入者停写",
         "收齐停写确认后验证", "验证失败或未验证片不得作为完成片集成",
         "分片验证不替代最终完整检查", "主代理指定单一集成者", "逐片相对交接基线核对 diff",
         "验证依赖变化使相关证据失效", "停止可选流水线", "不得施工其下游",
         "所有相关写入者停写", "初次集成失败先取证", "同根因跨片共用问题预算",
         "模型升级先暂停本任务全部并行写入", "记录实际并发证据", "须证明执行重叠",
         "串行包装不得称并行"],
        {"forbidden": ["并行写入**（normal 与 full）", "默认在同一工作区写互不相交的路径",
                       "未放行默认串行", "评测通过并在账本记录放行依据后启用",
                       "未经评测通过，全量常规施工保持串行", "默认宽度不超过 3",
                       "无条件并行，不检查独立性与隔离条件"], "scope": "section"},
    ),
    (
        "并行片数由内容判断且用户指定优先",
        "easydev/references/workflow.md",
        "## 并行执行",
        ["内容自判 1–6 片，用户指定片数优先", "1 片即串行，默认不追求用满",
         "同批活跃代理片（含提前审查）合计最多 6 片，不嵌套倍增",
         "用户指定须为整数 1–6", "指定 0、7 等越界或非整数时不启动该批分派",
         "说明范围并等待改值", "不能满足时报告冲突并采用可行的较少片数或串行"],
        {"forbidden": ["默认宽度不超过 3", "指定 7 片也可执行"], "scope": "section"},
    ),
    (
        "高风险并行例外须明确要求并可隔离恢复",
        "easydev/references/workflow.md",
        "## 并行执行",
        ["高风险边界默认串行", "鉴权、支付、敏感数据、迁移、发布权限",
         "仅用户明确要求该高风险工作并行", "每片可独立验证和恢复时例外",
         "指定片数不等于高风险并行授权", "高风险并行理由、用户授权来源、风险、隔离与恢复方式",
         "不放宽 full、设计门控、独立审查或操作授权"],
        {"forbidden": ["高风险边界默认并行"], "scope": "section"},
    ),
    (
        "并行账本记录判断风险与实际证据",
        "easydev/references/workflow.md",
        "## 账本与交接",
        ["并行判断（自判或用户指定片数/实际片数/判断依据 + 风险与隔离方式/宿主额度/授权来源/实际并发证据路径）"],
        {"forbidden": ["并行放行（评测批次"], "scope": "section"},
    ),
    (
        "入口并行分片按需加载 workflow",
        "easydev/SKILL.md",
        "## 按需加载",
        ["预算、并行分片", "references/workflow.md"],
        {"forbidden": [], "scope": "section"},
    ),
    (
        "入口并行边界保留 normal 单写",
        "easydev/SKILL.md",
        "## 全程约束",
        ["并行分片先读 workflow", "内容自判 1–6 片，用户指定优先", "normal 单写",
         "full 并写须满足独立性与隔离门槛", "串行包装不得称并行"],
        {"forbidden": ["normal 允许并写"], "scope": "section"},
    ),
    (
        "B58 系列覆盖片数隔离与真实声明",
        "easydev/tests/behavior.md",
        "| B58 子代理独立分派并行",
        ["本例可取 4 片", "B58n 指定片数与内容自判", "指定 0、7 或非整数不启动该批分派",
         "B58w 并行写入与验证隔离", "full 满足门槛可并写，不要求先评测；normal 单写",
         "高风险默认串行", "失败或未验证片不得当完成片集成", "B58p 流水线失效与集成预算",
         "全部并行写入", "B59 调度开销与真实声明", "串行包装不得称并行"],
        {"forbidden": ["默认宽度不超过 3", "已获隔离评测授权", "无正式放行记录"], "scope": "section"},
    ),
    (
        "并行收益观测可选且不作为准入门槛",
        "easydev/tests/experiments/fold-eval.md",
        "## 并行收益观测（可选）",
        ["不作为启用并行写入的前置条件", "未做观测不得宣称并行收益",
         "也不得因未做观测而禁止按", "结果写入 CHANGELOG",
         "当前协议尚未实跑，不能宣称性能收益或通用并行能力已验证"],
        {"forbidden": ["未有通过记录时正式任务默认串行", "放行记录必须绑定版本",
                       "并行臂须真有收益才放行"], "scope": "section"},
    ),
    (
        "分片回报不替代任务完整检查",
        "easydev/references/roles.md",
        "## 开发角色",
        ["分片回报", "完整检查由主代理统一协调停写后执行", "不操作共享 Git 索引"],
    ),
    (
        "工作树保留任务基线且不擅自提交",
        "easydev/references/environment.md",
        "## 工作区隔离与分支收尾",
        ["分片任务基线", "未提交改动（含未跟踪）", "不为传递补丁擅自提交", "共享缓存、端口与数据"],
    ),
    (
        "设计重审关闸且最终版本仍须完整施工审查",
        "easydev/references/design.md",
        "## 收敛与施工闸",
        ["关闭施工闸", "原施工审查不能证明新版本", "最终版本仍须完整施工审查"],
    ),
    (
        "B60 对照四遵守当前委派条件",
        "easydev/tests/behavior.md",
        "| B60 独立设计环节",
        ["对照四", "模型级 0", "模型与深度均为 `inherit`", "主代理可继续施工"],
        {"forbidden": ["对照四施工委派独立开发者实例"]},
    ),
    (
        "账本模板含门控判定、冻结时间、设计者与两本账",
        "easydev/references/workflow.md",
        "目标/验收/范围/授权基线",
        ["门控", "冻结时间", "设计者", "d/3", "r/5"],
    ),
    (
        "设计环节报告段含门控、冻结时间、建议通道与施工闸",
        "easydev/references/design.md",
        "补设计环节状态：",
        ["门控", "冻结时间", "建议", "施工闸"],
    ),
    (
        "设计产物段明确重写不计轮",
        "easydev/references/design.md",
        "设计账内分配",
        ["重写", "不计轮"],
    ),
    (
        "预算段明确候选第 2 轮需 opt-in",
        "easydev/references/design.md",
        "设计账内分配",
        ["最多用 1 轮", "第 2 轮留给设计审查"],
    ),
    (
        "纯 design 路径明确至多 2 轮候选评审且不使用 dr 账",
        "easydev/references/design.md",
        "纯 design / plan 路径",
        ["无 `d`/`r` 账", "至多 2 轮独立循环"],
    ),
    (
        "quick 与 normal 候选第 2 轮须明确要求并知悉转 full 余量代价",
        "easydev/references/design.md",
        "quick / normal 实现路径",
        ["默认最多用 1 轮", "第 2 轮修订复核须用户明确要求并接受转 full 后无升版重审余量", "记入 `d/3`"],
    ),
    (
        "按需加载表链接到门控正文",
        "easydev/SKILL.md",
        "独立设计环节门控",
        ["references/design.md#启用门控"],
    ),
    (
        "B60 含升级进入与受阻两个对照",
        "easydev/tests/behavior.md",
        "| B60 独立设计环节",
        ["对照五", "对照六"],
    ),
    (
        "SKILL.md 的 quick 核心内联尝试预算且不外链整份 workflow",
        "easydev/SKILL.md",
        "## quick 执行核心",
        ["三次仍失败即停", "换实例、模型级或编号不重置"],
        {"forbidden": ["references/workflow.md#尝试预算"]},
    ),
    (
        "quick 专项验证有强制加载入口且不自动升级",
        "easydev/SKILL.md",
        "## quick 执行核心",
        [
            "例外必读",
            "性能比较或收益声明",
            "测试依赖未知外部能力",
            "references/engineering.md#测试硬约束与选择",
            "references/engineering.md#验证调度与证据",
            "均不因此升级 full",
        ],
        {"forbidden": ["普通 quick 只需本页（含尝试预算，其余按需）"]},
    ),
    (
        "workflow.md 的判定依据引入可观察的第二视角信号",
        "easydev/references/workflow.md",
        "## 分流判定",
        ["第二视角信号"],
    ),
    (
        "workflow.md 的局部 P2 判定给出可观察条件",
        "easydev/references/workflow.md",
        "局部 P2 判定",
        ["局部 P2", "公共契约", "验收缺口", "转 full"],
    ),
    (
        "roles.md 的定向复审覆盖修复所在位置的直接调用方",
        "easydev/references/roles.md",
        "normal 的定向审查",
        ["直接调用方", "不扩查"],
    ),
    (
        "workflow.md 的「现成检查」限定为改动前已存在",
        "easydev/references/workflow.md",
        "第二视角信号",
        ["改动前已存在", "新增的测试"],
    ),
    (
        "workflow normal 流程要求实现改动先读代码质量判据",
        "easydev/references/workflow.md",
        "## normal 流程",
        ["代码质量判据"],
    ),
    (
        "SKILL.md 的 quick 核心把计量降为按需",
        "easydev/SKILL.md",
        "## quick 执行核心",
        ["quick 计量仅在用户要求时提供"],
        {"forbidden": ["单行计量（见 routing 的计量节）"]},
    ),
    (
        "workflow.md 的高风险边界按实际影响判定而非关键词",
        "easydev/references/workflow.md",
        "上表按实际影响判定",
        ["不因此进入 full", "已在 full"],
    ),
    (
        "engineering 声明为唯一细则源并覆盖关键判据",
        "easydev/references/engineering.md",
        "## 代码质量判据",
        ["唯一细则源", "克制复杂度", "边界校验"],
        {"scope": "file"},
    ),
    (
        "engineering 覆盖注释与生成文件判据",
        "easydev/references/engineering.md",
        "### 注释与生成文件",
        ["关键安全条件", "不手改"],
        {"scope": "file"},
    ),
    (
        "engineering 的最小充分实现链接代码质量判据并保留外部参考",
        "easydev/references/engineering.md",
        "## 最小充分实现",
        ["代码质量判据", "外部参考"],
    ),
    (
        "P3 判据以 roles.md 为正源，review 不重复",
        "easydev/references/review.md",
        "过滤噪音与归因",
        ["P3 判据与「位置 + 代价」要求见", "roles.md#审查角色"],
        {"forbidden": ["明显违反 [代码质量判据](engineering.md#代码质量判据)且"]},
    ),
    (
        "roles.md 同步代码质量 P3 口径与位置代价",
        "easydev/references/roles.md",
        "2. 只报有位置",
        ["代码质量判据", "P3", "位置与代价"],
    ),
    (
        "roles.md 链接工程实践与代码质量判据",
        "easydev/references/roles.md",
        "做最小完整改动",
        ["工程实践"],
    ),
    (
        "environment 的来源优先级含文件不合并与不回退",
        "easydev/references/environment.md",
        "### 来源优先级",
        ["对话字段覆盖", "文件不合并", "不静默回退"],
    ),
    (
        "environment 的能力降级给出三档与审查包",
        "easydev/references/environment.md",
        "### 能力降级与审查包",
        ["不同实例且上下文独立", "不同实例但继承上下文", "自查（非独立）", "审查包"],
    ),
    (
        "environment 的敏感原内容保护给出权限与软链判定",
        "easydev/references/environment.md",
        "**敏感原内容保护**",
        ["0700", "0600", "符号链接"],
    ),
    (
        "environment 的工作区收尾须用户授权且不强推",
        "easydev/references/environment.md",
        "**收尾与集成**",
        ["用户明确授权", "不代为决定", "git worktree remove"],
    ),
    (
        "施工闸按必关项放行，P3 不阻断",
        "easydev/references/design.md",
        "## 收敛与施工闸",
        ["必关项", "P3 不构成停止条件"],
        {"forbidden": ["范围内确认缺陷全部关闭"]},
    ),
    (
        "重审前置只由必关项或契约变更触发",
        "easydev/references/design.md",
        "## 收敛与施工闸",
        ["确认必关项（P0/P1、范围内 P2、验收缺口）", "仅保留不影响验收的 P3 不触发重审"],
        {"forbidden": ["设计审查确认缺陷、设计契约不可行"]},
    ),
    (
        "入口分流含用户要求独立审查信号",
        "easydev/SKILL.md",
        "## 入口与任务分流",
        ["用户是否要求独立审查", "无第二视角信号"],
    ),
    (
        "入口把只读路径排除在代码改动与全量检查外",
        "easydev/SKILL.md",
        "## 入口与任务分流",
        ["只交产物", "只读路径不改代码、不跑全量测试、构建与全仓检查"],
    ),
    (
        "审查者只跑安全只读命令并排除全仓检查",
        "easydev/references/review.md",
        "### 不可信代码执行",
        ["只运行安全只读命令", "不跑全量测试、构建与全仓检查", "不实施修复"],
    ),
    (
        "effort 档位与宿主 thinking 集合对齐",
        "easydev/references/environment.md",
        "### 字段规范",
        ["off / minimal", "xhigh / max", "非推理模型只有"],
        {"forbidden": ["ultra"]},
    ),
    (
        "入口把开工核实角色配置变成硬门",
        "easydev/SKILL.md",
        "## 全程约束",
        ["除 quick 与单次只读审查外，开工先按 environment 核实角色配置", "未核实不得假定 inherit"],
        {"forbidden": ["需要独立设计、独立审查或指定模型委派"]},
    ),
    (
        "模型配置在开工前必读",
        "easydev/references/environment.md",
        "## 模型配置",
        ["开工前必读", "分派前按角色取模型"],
        {"forbidden": ["仅在真正分派或用户要求查看/调整配置时读取"]},
    ),
    (
        "生效模型写入交付报告",
        "easydev/references/environment.md",
        "### 分派与生效核对",
        ["交付报告列出各角色生效模型"],
    ),
    (
        "工作区外配置不可因权限被拒当作不存在",
        "easydev/references/environment.md",
        "### 来源优先级",
        ["工作区外配置优先用宿主读取工具", "不得当不存在"],
    ),
    (
        "项目配置不授予新供应商外传权限",
        "easydev/references/environment.md",
        "### 来源优先级",
        ["外发安全门槛", "项目配置不授予新供应商外传权限",
         "分派外发代码前须暂停并披露供应商与模型", "取得授权后方可分派",
         "不得静默外发", "仅读取配置或未触发的 upgrade 级不阻断"],
        {"forbidden": ["项目配置授予新供应商外传权限"]},
    ),
    (
        "normal 开工先核实角色模型",
        "easydev/references/workflow.md",
        "## normal 流程",
        ["开工先按 [模型配置](environment.md#模型配置) 核实角色模型"],
        {"forbidden": ["分派前按 [模型配置](environment.md#模型配置) 选模型"]},
    ),
    (
        "full 开工先核实角色模型与深度",
        "easydev/references/workflow.md",
        "## full 编排",
        ["开工先按 [模型配置](environment.md#模型配置) 核实角色模型与深度"],
        {"forbidden": ["分派前按 [模型配置](environment.md#模型配置) 取角色模型"]},
    ),
    (
        "normal 转 full 一并继承候选评审轮次",
        "easydev/references/workflow.md",
        "## normal 流程",
        ["继承发现、尝试、已用审查与候选评审（转 full 后记入 `d/3`）"],
        {"forbidden": ["继承发现、尝试与已用审查："]},
    ),
    (
        "降级不退还账本也不补发已用审查",
        "easydev/references/workflow.md",
        "## 路径升降级",
        ["重入 full 不退还 `d/3`、`r/5`", "降级回 normal 也不重新获得已用的 normal 审查"],
    ),
    (
        "既有入口只收集 TestCase 时不静默丢弃新测试",
        "easydev/references/engineering.md",
        "## 测试硬约束与选择",
        ["既有入口只收集 `TestCase` 时，新测试须仍能被它执行，否则暂停请示迁移入口"],
    ),
    (
        "疑似凭据停止打印与落盘先告知",
        "easydev/references/engineering.md",
        "## 验证调度与证据",
        ["疑似密钥或凭据立即停止打印与落盘，先告知用户"],
    ),
    (
        "quick 核心覆盖生成产物与逃生舱写法",
        "easydev/SKILL.md",
        "## quick 执行核心",
        ["不手改生成产物，改源头后重生", "不用 `any`、空 catch 逃生舱写法"],
    ),
    (
        "自查降级审查包有固定产物名且不算 throwaway",
        "easydev/references/environment.md",
        "## 任务目录与恢复",
        ["自查降级审查包放 `review_pack.md`；它们与 `plan.md` 均不是 throwaway", "未归档前保留"],
    ),
    (
        "review 强度分档的降档确认只引用 workflow 分流判定",
        "easydev/references/review.md",
        "高风险边界任务中用户指定低于「深」时",
        ["workflow.md#分流判定", "放宽确认"],
        {"forbidden": ["交互环境先暂停披露未覆盖风险"]},
    ),
    (
        "review 严重程度定义以 roles.md 审查角色为正源",
        "easydev/references/review.md",
        "- **严重程度**：",
        ["roles.md#审查角色", "范围内必关项"],
        {"forbidden": ["灾难性阻断问题（明确证据的数据破坏"]},
    ),
    (
        "workflow full 角色用判定表给出写入者",
        "easydev/references/workflow.md",
        "| 条件 | 写入者与标注 |",
        ["模型级 0", "实现=主代理", "自查（非独立）", "委派开发者"],
    ),
    (
        "SKILL.md 高风险简写含并发与生产日志并链接边界表",
        "easydev/SKILL.md",
        "高风险边界（",
        ["并发", "生产日志", "references/workflow.md#高风险边界"],
    ),
    (
        "终审仅在修复后触发并替代该轮复审",
        "easydev/references/workflow.md",
        "## full 编排",
        ["无修复时首审即终审", "与此前所有审查实例不同", "占该轮复审的轮次、不再另起复审",
         "首轮盲审、第二阶段补发清单对照仍计同一轮", "review.md#终审两阶段"],
        {"forbidden": ["每个 full 任务都须终审", "首轮同时提供历史问题清单", "终审与复审各占一轮"]},
    ),
    (
        "终审预留轮次在预算节界定且不循环引用",
        "easydev/references/workflow.md",
        "## 审查预算",
        ["完整初审与收口轮合计预留两轮", "阶段增量审查轮数 = max(0, 3 - 已用轮数)",
         "终审第二阶段补发清单对照不另占轮次"],
        {"forbidden": ["终审不占轮次", "终审前须为最终完整审查与复审预留两轮"]},
    ),
    (
        "终审计入施工审核账且在预算表列出",
        "easydev/references/workflow.md",
        "## 审查预算",
        ["复审、终审（有修复时以终审替代该轮复审）"],
        {"forbidden": ["阶段增量审查、完整初审、复审 |"]},
    ),
    (
        "待验证风险最小确认覆盖 P0",
        "easydev/references/workflow.md",
        "## 审查与修复",
        ["潜在影响为 P0–P2", "写入者做一次最小确认"],
    ),
    (
        "终审两阶段协议写在第 2 节且不挂在强度维度下",
        "easydev/references/review.md",
        "## 2. 审查类型与全仓审计",
        ["### 终审两阶段", "替代该轮复审、合计一轮",
         "不给历史问题清单、修复 diff 与作者解释",
         "逐条给「已关闭 / 未关闭」结论与证据", "撤销首轮发现须附证据", "仍计同一轮"],
        {"forbidden": ["## 3. 审查强度与检查深度\n\n### 终审两阶段"]},
    ),
    (
        "中间复审受剩余轮次约束且终审不重建覆盖账",
        "easydev/references/workflow.md",
        "## 审查与修复",
        ["剩余轮次充足时", "覆盖账已在首轮建立", "增补而不重建", "轮次不足则直接由终审收口"],
    ),
    (
        "审查角色给出终审实例职责入口",
        "easydev/references/roles.md",
        "## 审查角色",
        ["被指定为终审实例时按", "review.md#终审两阶段", "两阶段合计仍算同一轮"],
    ),
    (
        "报告规则给出覆盖账格式",
        "easydev/references/review.md",
        "## 6. 报告规则",
        ["覆盖账格式", "本次未改使用方", "零发现同样给出"],
    ),
    (
        "审查覆盖账含未改使用方与发现方式",
        "easydev/references/review.md",
        "## 1. 确定范围与上下文",
        ["覆盖账", "本次未改使用方", "注明发现方式", "写“无”须给搜索证据",
         "审查者自列，写入者不预生成", "未覆盖项与原因"],
        {"forbidden": ["由写入者提供覆盖账", "覆盖账不作要求"]},
    ),
    (
        "normal 首审未改使用方与复审双问",
        "easydev/references/roles.md",
        "## 审查角色",
        ["未改使用方：…（或无）", "不建覆盖账、不加实例",
         "同时回答修复改变了哪些前提", "重新确证仍属局部"],
        {"forbidden": ["normal 建覆盖账"]},
    ),
    (
        "文档校准进入完成边界",
        "easydev/references/engineering.md",
        "## 完成边界",
        ["文档校准", "单列校准状态", "未校准即未完成"],
    ),
    (
        "纯文档与注释变更免跑代码检查",
        "easydev/references/engineering.md",
        "## 验证调度与证据",
        ["纯文档与注释变更免跑代码检查", "也不为确认基线预跑", "无对应工具直接交付"],
        {"forbidden": ["纯文档变更也要跑全量测试"]},
    ),
]


HEADING_RE = re.compile(r"^(#{1,6})\s")


def _scope_body(text: str, anchor: str, scope: str) -> str | None:
    """断言作用域：file=整份文件；section=锚点起截到同级或更高级标题（围栏内伪标题不计）。

    标题锚点按自身层级截断，所以 `### 来源优先级` 的断言不会越到 `### 字段规范`；
    非标题锚点沿用「截到下一个二级或更高级标题」。
    """
    idx = text.find(anchor)
    if idx < 0:
        return None
    if scope == "file":
        # 文件作用域断言的是“整份文件里存在”，锚点只用于确认文件结构未走样。
        return text
    anchor_heading = HEADING_RE.match(anchor)
    level = len(anchor_heading.group(1)) if anchor_heading else 2
    # 锚点可能落在围栏内（账本模板就是围栏里的多行块），必须先用前缀推出进入时的围栏状态，
    # 否则会把围栏的闭合行当成开启，导致作用域越出小节。
    fence: str | None = None
    for line in text[:idx].splitlines(keepends=True):
        stripped = line.lstrip()
        if fence is None:
            if stripped.startswith(("```", "~~~")):
                fence = stripped[:3]
        elif stripped.startswith(fence):
            fence = None
    body: list[str] = []
    for line in text[idx + len(anchor) :].splitlines(keepends=True):
        stripped = line.lstrip()
        if fence is None:
            if stripped.startswith(("```", "~~~")):
                fence = stripped[:3]
            elif HEADING_RE.match(line) and len(HEADING_RE.match(line).group(1)) <= level:
                break
            body.append(line)
        else:
            body.append(line)
            if stripped.startswith(fence):
                fence = None
    return "".join(body)


def collect_doc_contract(root: Path) -> list[tuple[str, str | None]]:
    """真仓库与机制自检共用的契约求值；返回 [(label, 问题或 None)]。"""
    results: list[tuple[str, str | None]] = []
    for entry in DOC_CONTRACT:
        label, rel, anchor, needles = entry[:4]
        options = entry[4] if len(entry) > 4 else {}
        scope = options.get("scope", "section")
        forbidden = options.get("forbidden", [])
        try:
            text = (root / rel).read_text(encoding="utf-8")
        except OSError as exc:
            results.append((label, f"无法读取 {rel}: {exc}"))
            continue
        body = _scope_body(text, anchor, scope)
        if body is None:
            results.append((label, f"{rel} 找不到锚点「{anchor}」"))
            continue
        missing = [needle for needle in needles if needle not in body]
        if missing:
            results.append((label, f"{rel} 的「{anchor}」附近缺 {missing}"))
            continue
        present = [word for word in forbidden if word in body]
        if present:
            results.append((label, f"{rel} 的「{anchor}」附近仍含已废弃措辞 {present}"))
            continue
        results.append((label, None))

    try:
        gate_text = (root / "easydev/references/design.md").read_text(encoding="utf-8")
    except OSError as exc:
        results.append(("启用门控项数", f"无法读取 design.md: {exc}"))
        return results
    section = gate_text.split("## 启用门控", 1)[-1].split("## 设计者", 1)[0]
    bullets = [line for line in section.splitlines() if line.startswith("- **")]
    results.append(
        ("启用门控仍为五项", None if len(bullets) == 5 else f"得到 {len(bullets)} 项")
    )
    return results


def run_doc_contract() -> None:
    print("=== 文档契约：规则与载体的字段/口径一致 ===")
    for label, problem in collect_doc_contract(EASYDEV.parent):
        if problem is None:
            ok(label)
        else:
            bad(label, problem)


def _copy_contract_tree(destination: Path) -> None:
    import shutil

    (destination / "easydev").mkdir(parents=True)
    shutil.copy2(EASYDEV / "SKILL.md", destination / "easydev/SKILL.md")
    shutil.copytree(EASYDEV / "references", destination / "easydev/references")
    # 契约也覆盖 tests/ 下的行为规约与折叠评测；机制自检须能在同一根目录内变异它们。
    (destination / "easydev/tests/experiments").mkdir(parents=True)
    shutil.copy2(EASYDEV / "tests/behavior.md", destination / "easydev/tests/behavior.md")
    shutil.copy2(
        EASYDEV / "tests/experiments/fold-eval.md",
        destination / "easydev/tests/experiments/fold-eval.md",
    )


def _apply_mutation(root: Path, rel: str, transform) -> None:
    path = root / rel
    text = path.read_text(encoding="utf-8")
    updated = transform(text)
    if updated == text:
        raise RuleError(f"变异没有改变 {rel}")
    path.write_text(updated, encoding="utf-8")


def _contract_problem(root: Path, label_part: str) -> tuple[bool, str]:
    matches = [
        problem
        for label, problem in collect_doc_contract(root)
        if label_part in label
    ]
    if len(matches) != 1:
        raise RuleError(f"契约标签「{label_part}」匹配到 {len(matches)} 条")
    return matches[0] is None, str(matches[0])


def run_doc_contract_mechanism() -> None:
    """契约机制自检：卡住“句子搬到邻节仍通过”和“旧新措辞并存仍通过”。"""
    import tempfile

    print("=== 文档契约机制自检：变异必须被抓到 ===")

    metric_sentence = "quick 计量仅在用户要求时提供（尝试次数与验证耗时）。"

    def move_metric(text: str) -> str:
        if metric_sentence not in text or "## 全程约束" not in text:
            raise RuleError("SKILL.md 的结构与预期不符")
        text = text.replace("；" + metric_sentence, "。", 1)
        return text.replace(
            "## 全程约束", "## 全程约束\n\n" + metric_sentence, 1
        )

    def legacy_metric(text: str) -> str:
        return text.replace(
            "## 按需加载",
            "旧句：单行计量（见 routing 的计量节）。\n\n## 按需加载",
            1,
        )

    def move_normal_contract(text: str) -> str:
        line = "2. 实施保持单写入者，允许只读并行；验证沿用 quick 核心；实现改动（非纯文案与注释）按 [代码质量判据](engineering.md#代码质量判据)。"
        if line not in text:
            raise RuleError("workflow.md 里没有 normal 第 2 条")
        text = text.replace(line + "\n", "", 1)
        return text.replace(
            "## 尝试预算", line + "\n\n## 尝试预算", 1
        )

    def move_ledger_needle(text: str) -> str:
        needle = "冻结时间"
        if needle not in text or "## 审查与修复" not in text:
            raise RuleError("workflow.md 的结构与预期不符")
        text = text.replace("设计环节（门控命中项或不启用理由）/design.md 版本与冻结时间/设计者：", "设计环节（门控命中项或不启用理由）/design.md 版本与快照/设计者：", 1)
        return text.replace("## 审查与修复", "## 审查与修复\n\n冻结时间：见上。", 1)

    def move_model_gate(text: str) -> str:
        clause = (
            "除 quick 与单次只读审查外，开工先按 environment 核实角色配置；"
            "未核实不得假定 inherit 自任设计或写入，任何分派都以核实值为准。"
        )
        if clause not in text or "## 按需加载" not in text:
            raise RuleError("SKILL.md 的开工核实段与预期不符")
        text = text.replace(clause, "", 1)
        return text.replace("## 按需加载", "## 按需加载\n\n" + clause, 1)

    def move_source_priority(text: str) -> str:
        moved = (
            "- **文件不合并**：选定最高优先级的基础文件后再应用对话局部覆盖。"
            "选中配置无效或不可读时暂停相关分派并报错，不静默回退。\n"
        )
        if moved not in text or "### 字段规范" not in text:
            raise RuleError("environment.md 的结构与预期不符")
        text = text.replace(moved, "", 1)
        return text.replace("### 字段规范", "### 字段规范\n" + moved, 1)

    def move_generate_section(text: str) -> str:
        marker = "### 注释与生成文件"
        if marker not in text:
            raise RuleError("engineering.md 里没有生成文件小节")
        head, tail = text.split(marker, 1)
        return marker + tail + "\n" + head

    quick_clause = (
        "quick 默认只读本页；例外必读：性能比较或收益声明读 [性能](references/engineering.md#测试硬约束与选择)，"
        "测试依赖未知外部能力读 [验证调度](references/engineering.md#验证调度与证据)；均不因此升级 full。"
    )

    def move_quick_clause(text: str) -> str:
        if quick_clause + "\n\n" not in text or "## 按需加载" not in text:
            raise RuleError("SKILL.md 的 quick 专项验证段与预期不符")
        text = text.replace(quick_clause + "\n\n", "", 1)
        return text.replace("## 按需加载", "## 按需加载\n\n" + quick_clause, 1)

    def legacy_quick_clause(text: str) -> str:
        if quick_clause not in text:
            raise RuleError("SKILL.md 的 quick 专项验证段与预期不符")
        return text.replace(
            "## 按需加载",
            "普通 quick 只需本页（含尝试预算，其余按需）。\n\n## 按需加载",
            1,
        )

    cases = (
        ("计量句移到邻节必须判负", "easydev/SKILL.md", move_metric, "计量降为按需", False),
        ("旧计量措辞与新措辞并存必须判负", "easydev/SKILL.md", legacy_metric, "计量降为按需", False),
        ("normal 契约移到邻节必须判负", "easydev/references/workflow.md", move_normal_contract, "normal 流程要求实现改动", False),
        ("围栏内锚点的断言不越出小节", "easydev/references/workflow.md", move_ledger_needle, "账本模板含门控判定", False),
        ("文件作用域允许跨节搬动", "easydev/references/engineering.md", move_generate_section, "注释与生成文件判据", True),
        ("来源规则搬到三级邻节必须判负", "easydev/references/environment.md", move_source_priority, "environment 的来源优先级", False),
        ("开工核实角色配置搬到按需加载必须判负", "easydev/SKILL.md", move_model_gate, "入口把开工核实角色配置变成硬门", False),
        ("quick 专项验证段移到邻节必须判负", "easydev/SKILL.md", move_quick_clause, "quick 专项验证", False),
        ("旧 quick 措辞与新措辞并存必须判负", "easydev/SKILL.md", legacy_quick_clause, "quick 专项验证", False),
        ("并行门槛搬到邻节必须判负", "easydev/references/workflow.md",
         lambda text: text.replace("独立性与隔离条件全部成立才可并行，否则串行。", "", 1).replace(
             "## 审查预算", "## 审查预算\n\n独立性与隔离条件全部成立才可并行，否则串行。", 1),
         "并行判断门槛、验证依赖与集成预算", False),
        ("旧评测门槛与新判断并存必须判负", "easydev/references/workflow.md",
         lambda text: text.replace("## 并行执行\n", "## 并行执行\n\n未放行默认串行。\n", 1),
         "并行判断门槛、验证依赖与集成预算", False),
    )
    for label, rel, transform, label_part, should_pass in cases:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _copy_contract_tree(root)
            try:
                _apply_mutation(root, rel, transform)
                passed, detail = _contract_problem(root, label_part)
            except RuleError as exc:
                bad(label, str(exc))
                continue
            expected_detail = should_pass or ("缺 [" in detail) or ("已废弃措辞" in detail)
            if passed == should_pass and expected_detail:
                ok(label)
            else:
                bad(label, f"预期{'通过' if should_pass else '判负'}，实际 {'通过' if passed else '判负'}（{detail}）")


# --- 可达性：回答「多方案任务是否几乎必然停闸」 --------------------------


def run_reachability() -> None:
    print("=== 可达性：多方案任务（门控第 4 项命中） ===")
    rows = []
    for cand_rounds, findings, opt_in in itertools.product((1, 2), (0, 1), (False, True)):
        if cand_rounds == 2 and not opt_in:
            continue
        b = Budget(candidate_second_opt_in=opt_in)
        outcome = "闭环完成"
        try:
            for _ in range(cand_rounds):
                b.candidate()
            b.design_review(findings=findings)
            if findings:
                b.rework()
                b.design_review()
            b.freeze()
            b.open_gate()
            b.full_review()
            b.finish()
        except Stop as stop:
            outcome = stop.state
        rows.append((cand_rounds, findings, opt_in, b.d, b.r, outcome))

    for cand, findings, opt_in, d, r, outcome in rows:
        print(f"  候选 {cand} 轮 / 设计缺陷 {findings} / opt-in {opt_in} -> d={d} r={r} {outcome}")

    default_ok = all(
        outcome == "闭环完成" for cand, findings, opt_in, d, r, outcome in rows if not opt_in
    )
    if default_ok:
        ok("默认分配下，候选首轮 + 设计审查报缺陷 + 升版重审仍可完成（d=3 恰好）")
    else:
        bad("默认分配", "存在默认路径无法完成")

    opt_hit = [row for row in rows if row[2] and row[1] and row[5] != "闭环完成"]
    if opt_hit:
        ok(f"候选两轮 + 设计审查报缺陷明确停止并交用户（{opt_hit[0][5]}），非静默失败")
    else:
        bad("候选两轮 + 缺陷", "未按预期停止")

    if all(d <= D_CAP and r <= R_CAP for _, _, _, d, r, _ in rows):
        ok(f"所有路径未超上限（d≤{D_CAP}、r≤{R_CAP}）")
    else:
        bad("上限", "存在越界路径")


def main() -> int:
    run_gate_cases()
    run_route_cases()
    run_budget_cases()
    run_normal_cases()
    run_host_cases()
    run_doc_contract()
    run_doc_contract_mechanism()
    run_reachability()
    print(f"\n=== 设计环节确定性复验：{checks - failures}/{checks} 通过 ===")
    if failures:
        print(f"FAIL: {failures} 项未通过", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
