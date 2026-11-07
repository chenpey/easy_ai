"""流程状态机中可判定的部分：normal 转 full、设计门控与两本账记账。

编码 `references/workflow.md` 的 normal 一次独立定向审查与升级继承，
以及 `references/design.md` 里能被判定成布尔或整数的五项门控、`d/3` 与
`r/5` 的消耗与上限、施工闸、停止条件。不模拟模型是否遵守规则，也不覆盖
任何需要真实宿主的分派、暂停或实例隔离行为——那些只能靠行为回放。

任何与对应 reference 正文不一致的地方都属本模型缺陷，不是文档缺陷：
本文件是那些规则的可执行断面。
"""

from __future__ import annotations

D_CAP = 3  # 设计账 d/3
R_CAP = 5  # 施工审核 r/5
R_RESERVE = 2  # 施工审核须为最终完整审查与必要复审至少预留的轮数
NORMAL_REVIEW_CAP = 1  # normal 无缺陷时的正常完成阈值
NORMAL_REVIEW_MAX_CAP = 2  # normal 独立审查硬上限（允许初审 + 1 次复审）


class RuleError(Exception):
    """请求违反了 design.md 的某条前置条件。"""


# --- 宿主能力解析（替身输入，不模拟模型） ---------------------------------
#
# 下面几个函数把「宿主环境」当作外部边界接受替身输入：有没有委派能力、
# designer 模型可不可用、上下文能否隔离、审查是否受阻。被测对象是给定这些
# 输入时规则要求做什么，不是模型会不会照做。

DESIGNER_PAUSED = "paused"  # 模型不可用 → 停在施工闸前，不回落
DESIGNER_FALLBACK = "fallback"  # 无能力/禁止委派 → 主代理兜底，不记 d/r
DESIGNER_MAIN_AGENT = "main_agent"  # inherit → 主代理设计，不额外强制委派施工
DESIGNER_DELEGATED = "delegated"  # 可用独立实例


def resolve_designer(
    *,
    model: str = "inherit",
    model_available: bool = True,
    delegation_available: bool = True,
    user_forbids_delegation: bool = False,
    context_isolated: bool = True,
    developer_non_inherit: bool = False,
    in_upgrade_stage: bool = False,
) -> dict:
    """按 design.md「设计者」与「实例隔离」判定本次设计分派的形态。"""
    # 1) 配置了具体模型但不可用：暂停并报受阻，与有无委派能力无关。
    if model != "inherit" and not model_available:
        return {
            "mode": DESIGNER_PAUSED,
            "counts_dr": False,
            "gate": "closed",
            "reason": "designer 配置的具体模型不可用：暂停设计分派并报受阻，不回落主代理",
        }
    # 2) 用户明确授权自查（含用户禁止委派）：主代理兜底；无授权的无委派能力则暂停。
    if user_forbids_delegation:
        return {
            "mode": DESIGNER_FALLBACK,
            "counts_dr": False,
            "gate": "open_self_checked",
            "must_delegate_developer": False,
            "reason": "用户明确授权主代理自查（或明确禁止委派），按无能力兜底",
        }
    if not delegation_available:
        return {
            "mode": DESIGNER_PAUSED,
            "counts_dr": False,
            "gate": "closed",
            "reason": "宿主不具备子代理委派能力：暂停设计分派并报受阻，由用户决定改配置、停止或授权自查",
        }
    # 3) 有委派能力。
    if model == "inherit":
        return {
            "mode": DESIGNER_MAIN_AGENT,
            "counts_dr": True,
            "gate": "closed",
            "must_delegate_developer": bool(developer_non_inherit or in_upgrade_stage),
            "context_isolation": "isolated" if context_isolated else "limited",
            "reason": "主代理设计；施工是否必须委派由 developer 配置与模型升级阶段决定",
        }
    # 设计者身份不改变施工的强制委派条件；可选委派由宿主编排决定。
    return {
        "mode": DESIGNER_DELEGATED,
        "counts_dr": True,
        "gate": "closed",
        "must_delegate_developer": bool(developer_non_inherit or in_upgrade_stage),
        "context_isolation": "isolated" if context_isolated else "limited",
        "reason": "designer 为可用独立实例，主代理未参与设计",
    }


def resolve_blocked(*, user_accepts_no_independent_review: bool = False) -> dict:
    """设计审查受阻时的施工闸处置。"""
    if user_accepts_no_independent_review:
        return {
            "gate": "open_with_waiver",
            "must_flag": "设计环节未独立审查",
            "may_claim_closed_loop": False,
        }
    return {
        "gate": "blocked",
        "must_flag": "受阻轮不算缺陷已关闭",
        "may_claim_closed_loop": False,
    }


class Stop(Exception):
    """触发停止条件；携带用户可读的结局与原因。"""

    def __init__(self, state: str, reason: str):
        super().__init__(f"{state}: {reason}")
        self.state = state
        self.reason = reason


class NormalReviewFlow:
    """normal 的独立定向审查及其转 full 时的预算继承。

    本类不判断用户最初为何选择 normal，只约束选择之后可观察的状态转换：
    一个不同实例回传非空报告；首轮无必关项即可交付；首轮仅报局部 P2 时
    允许一次局部修复加一次定向复审；必关发现让流程转 full，并把已完成的
    审查记为 `r=1/5` 或 `r=2/5`，已有修复尝试不重置。
    """

    def __init__(
        self,
        *,
        author_instance_id: str,
        repair_attempts: int = 0,
        prior_full_reviews: int = 0,
    ):
        if not author_instance_id.strip():
            raise RuleError("normal 必须记录实现作者实例")
        if repair_attempts < 0:
            raise RuleError("修复尝试次数不能为负")
        if prior_full_reviews < 0 or prior_full_reviews >= R_CAP:
            raise RuleError(f"进入 normal 前已有完整审查须在 0..{R_CAP - 1} 轮内")
        self.author_instance_id = author_instance_id
        self.repair_attempts = repair_attempts
        self.path = "normal"
        self.r = prior_full_reviews
        self.prior_full_reviews = prior_full_reviews
        self.review_count = 0
        self.local_repair_done = False
        self.reviewer_instance_id = None
        self.report = ""
        self.must_close_findings = []

    @staticmethod
    def _is_local_p2(finding: dict) -> bool:
        """局部 P2：按 workflow.md「normal 流程」的「局部 P2 判定」，四条判据同时成立。

        fixture 字段到判据的对应：`in_scope` 承担判据①②③（不触公共契约、不改
        数据或状态语义、不引入依赖且不越出已改文件与直接调用方）；`acceptance_gap`
        与 `potential_high_risk` 是判据④的两个子项（不是验收缺口、无带线索未排除的
        潜在 P0/P1）；`is_local` 表示该缺陷确可按局部处理。判据⑤（无法确证即按非
        局部处理）落在这里的缺省值：`is_local` 必须显式为 True 才算局部。
        """
        severity = str(finding.get("severity", "")).upper()
        return bool(
            severity == "P2"
            and finding.get("in_scope", True)
            and finding.get("is_local", False) is True
            and not finding.get("acceptance_gap")
            and not finding.get("potential_high_risk")
        )

    @staticmethod
    def _is_immediate_full(finding: dict) -> bool:
        """P0/P1、验收缺口、潜在高风险，或未确证四条判据的 P2，一律立即转 full。"""
        severity = str(finding.get("severity", "")).upper()
        if severity in {"P0", "P1"} or finding.get("acceptance_gap") or finding.get("potential_high_risk"):
            return True
        # 判据①②③任一不成立（in_scope 为假）的 P2 同样不是局部 P2，不能被过滤掉。
        return severity == "P2" and not NormalReviewFlow._is_local_p2(finding)

    def record_review(
        self,
        instance_id: str,
        report: str,
        *,
        findings: list[dict] | None = None,
    ) -> None:
        """记录一次已完成的定向审查：初审或局部 P2 修复后的复审。"""
        if self.path != "normal":
            raise RuleError("已转 full，不得继续 normal 审查")
        if self.review_count >= NORMAL_REVIEW_MAX_CAP:
            raise RuleError("normal 独立审查已达上限（至多 2 次），不得形成修复循环")
        if self.prior_full_reviews + self.review_count >= R_CAP:
            raise Stop("未完成", "历史审查与 normal 审查已用尽 r/5 预算")
        if self.review_count == 1 and not self.local_repair_done:
            raise RuleError("无缺陷或未做局部修复不得进行复审")
        if not instance_id.strip():
            raise RuleError("没有审查实例，不能算独立定向审查")
        if instance_id == self.author_instance_id:
            raise RuleError("作者自查不是独立定向审查")
        if not report.strip():
            raise RuleError("审查实例必须回传非空完成报告")

        # P3 等非必关项不参与处置，以免掩盖同一报告里的局部 P2。
        must_close_findings = [
            finding
            for finding in (findings or [])
            if self._is_immediate_full(finding) or self._is_local_p2(finding)
        ]
        self.review_count += 1
        self.r = self.prior_full_reviews + self.review_count
        self.reviewer_instance_id = instance_id
        self.report = report
        self.must_close_findings = must_close_findings
        if any(self._is_immediate_full(finding) for finding in must_close_findings):
            self.path = "full"
            return
        if must_close_findings and self.review_count == NORMAL_REVIEW_CAP:
            # 首轮仅局部 P2：留在 normal，等待一次局部修复与复审。
            return
        if must_close_findings:
            # 复审仍未关闭：normal 不再发起第三次审查，转 full 并记 r=2。
            self.path = "full"

    def record_repair(self, repair_diff: str = "") -> None:
        """记录 normal 内唯一一次局部修复尝试。"""
        if self.path != "normal":
            raise RuleError("已转 full，不得继续 normal 修复")
        if self.review_count != 1 or not self.must_close_findings:
            raise RuleError("仅在首轮审查存在局部 P2 时允许记录修复尝试")
        if self.local_repair_done:
            raise RuleError("normal 内至多允许一次局部修复尝试")
        if self.repair_attempts >= 3:
            raise Stop("未完成", "同一问题三次修复尝试已用尽")
        if self.r >= R_CAP:
            raise Stop("未完成", "无剩余复审轮次，不提交候选修复")
        self.local_repair_done = True
        self.repair_attempts += 1

    def finish(self) -> str:
        """返回当前流程的可交付状态，不代替后续 full 闭环。"""
        if self.path == "full":
            return "full 未闭环"
        valid_reviewer = (
            bool(self.reviewer_instance_id)
            and self.reviewer_instance_id != self.author_instance_id
            and bool(self.report.strip())
        )
        if not valid_reviewer:
            return "normal 未完成"
        if self.review_count == NORMAL_REVIEW_CAP and not self.must_close_findings:
            return "normal 完成"
        if (
            self.review_count == NORMAL_REVIEW_MAX_CAP
            and self.local_repair_done
            and not self.must_close_findings
        ):
            return "normal 完成"
        return "normal 未完成"


def decide_gate(task: dict) -> dict:
    """按 design.md「启用门控」的五项判定是否启用本环节。"""
    hits = []
    if task.get("packages", 1) >= 2 or task.get("layers_crossed", 0) >= 1:
        hits.append("跨边界")
    if task.get("public_contract") or task.get("persistent_data"):
        hits.append("公共契约或持久数据")
    if task.get("independent_acceptance_stages", 0) >= 2:
        hits.append("多阶段施工")
    if (task.get("candidate_options", 0) >= 2 and task.get("candidates_both_conditions")) or task.get(
        "user_requests_comparison"
    ):
        hits.append("多方案")
    if task.get("high_risk") and task.get("changes_high_risk_behavior", True):
        hits.append("高风险边界")
    return {"enabled": bool(hits), "hits": hits}


class Budget:
    """两本账状态机。事件名对应 design.md 里会消耗轮次或改变闸状态的动作。"""

    def __init__(
        self,
        *,
        candidate_second_opt_in: bool = False,
        design_enabled: bool = True,
        pure_design: bool = False,
    ):
        self.d = 0
        self.r = 0
        self.pure_design = pure_design
        self.design_enabled = design_enabled and not pure_design
        self.candidate_rounds = 0
        self.design_reviewed = False
        self.design_frozen = False
        self.blocked = False
        self.rework_count = 0  # 升版次数；版本号 = 1 + rework_count
        self.open_findings = 0  # 未关闭的确认缺陷（含范围内 P2、验收缺口）
        self.full_reviewed = False
        self.review_current = False
        # 未启用本环节时没有施工闸，直接按 full 其余流程施工。
        self.gate_open = not self.design_enabled and not pure_design
        # 用户是否明确要求候选第二轮并接受「设计审查将无升版重审余量」
        self.candidate_second_opt_in = candidate_second_opt_in
        self.log = []

    # --- 记账原语 ---------------------------------------------------------
    def _spend_d(self, why: str) -> None:
        if self.d >= D_CAP:
            raise Stop("未完成", f"d/3 已用尽，无法再发起{why}")
        self.d += 1
        self.log.append(f"d={self.d} {why}")

    def _spend_r(self, why: str) -> None:
        if self.r >= R_CAP:
            raise Stop("未完成", f"r/5 已用尽，无法再发起{why}")
        self.r += 1
        self.log.append(f"r={self.r} {why}")

    def _r_left(self) -> int:
        return R_CAP - self.r

    def _require_design_phase(self, why: str) -> None:
        if self.pure_design:
            raise RuleError(f"纯 design 路径无独立设计施工审查环节，不得{why}")
        if not self.design_enabled:
            raise RuleError(f"未启用独立设计环节，不得{why}")

    # --- 设计阶段 ---------------------------------------------------------
    def candidate(self) -> None:
        if self.pure_design:
            if self.candidate_rounds >= 2:
                raise RuleError("纯 design 候选评审合计至多 2 轮")
            self.candidate_rounds += 1
            self.log.append(f"pure_design candidate round {self.candidate_rounds}")
            return
        self._require_design_phase("组织候选评审")
        if self.design_reviewed:
            raise RuleError("候选评审应在设计审查之前")
        if self.candidate_rounds >= 2:
            raise RuleError("候选评审合计至多 2 轮")
        if self.candidate_rounds == 1:
            if not self.candidate_second_opt_in:
                raise RuleError(
                    "设计审查尚未进行时候选最多用 1 轮；第 2 轮须用户明确要求并接受无升版重审余量"
                )
            if self.d + 1 > D_CAP:
                raise Stop("未完成", "d/3 已用尽，无法进行候选第二轮复核")
        self._spend_d("候选评审" if self.candidate_rounds == 0 else "候选第二轮复核")
        self.candidate_rounds += 1

    def design_review(self, findings: int = 0, blocked: bool = False) -> None:
        self._require_design_phase("发起设计审查")
        if self.design_reviewed:
            raise RuleError("同一版本的设计审查只能发起一次；升版后走 rework")
        self._spend_d("设计审查")
        self.design_reviewed = True
        self.open_findings += findings
        # 新一轮结果替代旧的受阻状态，但不清除未关闭发现。
        self.blocked = blocked

    def rework(self) -> None:
        """升版（v(N+1)）：实现者发现契约不可行，或施工审核发现根因在设计。

        重写本身不消耗 `d`；计入 `d` 的是重写之后的那一次设计审查。否则一次
        升版会吃掉两轮，与「设计审查 3 轮」的口径不符。
        """
        self._require_design_phase("升版重审")
        # 停止条件先于状态改写：否则会留下升版脏现场，直到下一次设计审查才报错。
        if self.d >= D_CAP:
            raise Stop("未完成", "d/3 已用尽，无法升版重审")
        if self._r_left() < R_RESERVE:
            raise Stop(
                "未完成",
                f"剩余 r={self._r_left()} 不足两轮，不得单开设计重审——设计契约缺陷导致未完成",
            )
        self.gate_open = False
        self.full_reviewed = False
        self.design_reviewed = False
        self.design_frozen = False
        self.record_change()
        self.rework_count += 1
        self.log.append(f"design v{self.rework_count + 1} (rework, no round spent)")

    def resolve_findings(self, count: int | None = None) -> None:
        self.record_change()
        self.open_findings = 0 if count is None else max(0, self.open_findings - count)

    def record_change(self) -> None:
        """实现、调用方或验证依赖变化：旧审查不再证明当前版本。"""
        self.review_current = False

    def freeze(self) -> None:
        self._require_design_phase("冻结设计版本")
        if self.blocked:
            raise Stop("受阻", "设计审查受阻轮不被视为缺陷已关闭，停在施工闸前")
        if self.open_findings:
            raise Stop("未完成", f"仍有 {self.open_findings} 项未关闭，不进入施工")
        if not self.design_reviewed:
            raise RuleError("未经设计审查不得冻结送审版本")
        self.design_frozen = True
        self.log.append("design frozen")

    def open_gate(self) -> None:
        self.freeze()
        self.gate_open = True
        self.log.append("gate open")

    # --- 施工阶段 ---------------------------------------------------------
    def incremental_review(self) -> None:
        if not self.gate_open:
            raise RuleError("阶段增量审查须在施工闸之后")
        if self._r_left() - 1 < R_RESERVE:
            raise Stop(
                "未完成",
                f"阶段增量审查后仅余 {self._r_left() - 1} 轮，无法为最终完整审查与必要复审预留两轮；应按依赖合批或报未完成",
            )
        self._spend_r("阶段增量审查")

    def full_review(self, findings: int = 0) -> None:
        if not self.gate_open:
            raise RuleError("完整初审须在施工闸之后")
        self._spend_r("完整初审")
        self.full_reviewed = True
        self.open_findings += findings
        self.review_current = self.open_findings == 0
        if findings and self._r_left() < 1:
            raise Stop("未完成", "完整初审报出缺陷但已无复审轮次")

    def rereview(self) -> None:
        if self.pure_design:
            raise RuleError("纯 design 路径无独立设计施工审查环节，不得发起施工复审")
        if not self.gate_open:
            raise RuleError("复审须在施工闸之后")
        if not self.full_reviewed:
            raise RuleError("未经完整初审不得以复审替代")
        self._spend_r("复审")
        self.resolve_findings()
        self.review_current = True

    def finish(self) -> str:
        if not self.gate_open:
            raise RuleError("未过施工闸不得宣称完成")
        if self.open_findings:
            raise Stop("未完成", f"仍有 {self.open_findings} 项未关闭")
        if not self.full_reviewed:
            raise RuleError("最终版本尚无完整施工审查")
        if not self.review_current:
            raise RuleError("最终修改尚未经审查")
        return "闭环完成"
