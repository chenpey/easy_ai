#!/usr/bin/env python3
"""独立设计环节的确定性复验：门控判定、两本账记账、施工闸与停止条件。

覆盖 `references/design.md` 中可判定的部分，以及 `references/routing.md` 的 normal 局部 P2 修复与双审查上限；
并在最后枚举多方案任务的可达路径，回答「候选 2 轮 + 设计审查 1 轮是否会让多方案任务几乎必然停在施工闸前」。

不覆盖：模型是否遵守规则、真实宿主的分派与实例隔离、受阻后用户显式豁免。
那些只能靠行为回放，见 tests/behavior.md 的 B60。
"""

from __future__ import annotations

import itertools
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
        "两个实施实例接力",
        {"multi_impl_instances": True},
        (True, ["多阶段施工"]),
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
        "高风险边界仅改注释或日志措辞：门控第 5 项不启用",
        {"packages": 1, "high_risk": True, "changes_high_risk_behavior": False},
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


# --- 记账与停止 -----------------------------------------------------------


def run_budget_cases() -> None:
    print("=== 两本账与施工闸（design.md「预算」「收敛与施工闸」） ===")

    # 不启用门控的局部 full：没有施工闸，也不记 design账。
    b = Budget(design_enabled=False)
    b.full_review()
    b.rereview()
    b.finish()
    if (b.d, b.r) == (0, 2):
        ok("不启用门控：d 不计，r=2 完成")
    else:
        bad("不启用门控", f"d={b.d} r={b.r}")

    # 单方案 + 设计审查一次通过。
    b = Budget()
    b.design_review()
    b.freeze()
    b.open_gate()
    b.full_review()
    b.rereview()
    b.finish()
    if (b.d, b.r) == (1, 2):
        ok("单方案设计审查通过：d=1 r=2")
    else:
        bad("单方案路径", f"d={b.d} r={b.r}")

    # 候选首轮 + 设计审查一次通过。
    b = Budget()
    b.candidate()
    b.design_review()
    b.freeze()
    b.open_gate()
    b.full_review()
    b.rereview()
    b.finish()
    if (b.d, b.r) == (2, 2):
        ok("候选首轮 + 设计审查通过：d=2 r=2")
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
    b.resolve_findings()
    b.freeze()
    b.open_gate()
    b.full_review()
    b.rereview()
    b.finish()
    if (b.d, b.r) == (3, 2):
        ok("默认分配：候选 1 轮 + 设计审查 1 轮 + 升版重审 1 轮 = d/3，r=2 完成")
    else:
        bad("默认分配", f"d={b.d} r={b.r}")

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
    b.resolve_findings()
    b.freeze()
    b.open_gate()
    b.full_review()
    b.rereview()
    b.finish()
    if b.r == R_CAP:
        ok("剩余 r=2 时允许升版重审，重审后完整初审 + 复审刚好用完 r/5")
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
        ok(f"剩余 r=1 时禁止升版重审（{stop.reason[:24]}…）")
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
        b.design_review()
    except Stop as stop:
        ok(f"施工期 d 用尽时停止（{stop.reason[:20]}…）")
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


# --- 宿主能力替身：设计分派形态与受阻处置 --------------------------------

HOST_CASES = [
    (
        "inherit + 有委派：主代理设计，施工必须委派开发者",
        dict(model="inherit"),
        dict(mode=DESIGNER_MAIN_AGENT, counts_dr=True, must_delegate_developer=True),
    ),
    (
        "inherit + 无委派：走无能力兜底，不记 d/r",
        dict(model="inherit", delegation_available=False),
        dict(mode=DESIGNER_FALLBACK, counts_dr=False, gate="open_self_checked"),
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
        "具体模型可用 + 升级阶段：仍须委派",
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
        "宿主继承上下文：实例分离仍可审查，披露隔离限制",
        dict(context_isolated=False),
        dict(mode=DESIGNER_MAIN_AGENT, counts_dr=True, must_delegate_developer=True,
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
        "账本模板含门控判定、冻结时间、设计者与两本账",
        "easydev/references/orchestration.md",
        "独立审查条件/审查强度",
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
        "SKILL.md 的 quick 核心链接到尝试预算唯一事实源",
        "easydev/SKILL.md",
        "## quick 执行核心",
        ["references/routing.md#尝试预算"],
    ),
    (
        "routing.md 的判定依据引入可观察的第二视角信号",
        "easydev/references/routing.md",
        "## 判定依据",
        ["第二视角信号"],
    ),
    (
        "routing.md 的局部 P2 判定给出可观察条件",
        "easydev/references/routing.md",
        "局部 P2 判定",
        ["局部 P2", "公共契约", "验收缺口", "转 full"],
    ),
    (
        "reviewer-role 的定向复审覆盖修复所在位置的直接调用方",
        "easydev/references/reviewer-role.md",
        "normal 的定向审查",
        ["直接调用方", "不扩查"],
    ),
    (
        "routing.md 的「现成检查」限定为改动前已存在",
        "easydev/references/routing.md",
        "第二视角信号",
        ["改动前已存在", "新增的测试"],
    ),
    (
        "SKILL.md 的 quick 核心指向 routing 的计量节",
        "easydev/SKILL.md",
        "单行计量",
        ["routing 的计量节"],
    ),
    (
        "routing.md 的高风险边界按实际影响判定而非关键词",
        "easydev/references/routing.md",
        "上表按实际影响判定",
        ["不因此进入 full", "已在 full"],
    ),
    (
        "routing.md 的计量小节给出字段键序、缺省写法与角色分段",
        "easydev/references/routing.md",
        "## 计量",
        ["计量：模块=", "审查=<N>；d=<x>/3；r=<y>/5", "未进 full 记", "main:", "reviewer:", "读取事件"],
    ),
    (
        "code-quality 声明为唯一细则源并覆盖关键判据",
        "easydev/references/code-quality.md",
        "# 代码质量判据",
        ["唯一细则源", "克制复杂度", "边界校验"],
    ),
    (
        "code-quality 覆盖注释与生成文件判据",
        "easydev/references/code-quality.md",
        "## 注释",
        ["关键安全条件", "## 生成文件", "不手改"],
    ),
    (
        "execution 的最小充分实现链接代码质量判据并保留外部参考",
        "easydev/references/execution.md",
        "## 最小充分实现",
        ["code-quality.md", "外部参考"],
    ),
    (
        "review 给出代码质量 P3 口径",
        "easydev/references/review.md",
        "过滤噪音与归因",
        ["code-quality.md", "P3", "位置和代价"],
    ),
    (
        "reviewer-role 同步代码质量 P3 口径与位置代价",
        "easydev/references/reviewer-role.md",
        "2. 只报有位置",
        ["code-quality.md", "P3", "位置与代价"],
    ),
    (
        "developer-role 链接代码质量判据",
        "easydev/references/developer-role.md",
        "做最小完整改动",
        ["code-quality.md"],
    ),
]


def run_doc_contract() -> None:
    print("=== 文档契约：规则与载体的字段/口径一致 ===")
    for label, rel, anchor, needles in DOC_CONTRACT:
        path = EASYDEV.parent / rel
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            bad(label, f"无法读取 {rel}: {exc}")
            continue
        idx = text.find(anchor)
        if idx < 0:
            bad(label, f"{rel} 找不到锚点「{anchor}」")
            continue
        window = text[idx : idx + 1200]
        missing = [n for n in needles if n not in window]
        if missing:
            bad(label, f"{rel} 的「{anchor}」附近缺 {missing}")
        else:
            ok(label)

    gate_path = EASYDEV.parent / "easydev/references/design.md"
    text = gate_path.read_text(encoding="utf-8")
    section = text.split("## 启用门控", 1)[1].split("## 设计者", 1)[0]
    bullets = [ln for ln in section.splitlines() if ln.startswith("- **")]
    if len(bullets) == 5:
        ok("启用门控仍为五项")
    else:
        bad("启用门控项数", f"得到 {len(bullets)} 项")


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
                b.resolve_findings()
            b.freeze()
            b.open_gate()
            b.full_review()
            b.rereview()
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
    run_budget_cases()
    run_normal_cases()
    run_host_cases()
    run_doc_contract()
    run_reachability()
    print(f"\n=== 设计环节确定性复验：{checks - failures}/{checks} 通过 ===")
    if failures:
        print(f"FAIL: {failures} 项未通过", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
