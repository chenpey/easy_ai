"""流程状态机中可判定的部分：normal 转 full、设计门控与两本账记账。

编码 `references/routing.md` 的 normal 一次独立定向审查与升级继承，
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
NORMAL_REVIEW_CAP = 1  # normal 只允许一次独立定向审查


class RuleError(Exception):
    """请求违反了 design.md 的某条前置条件。"""


# --- 宿主能力解析（替身输入，不模拟模型） ---------------------------------
#
# 下面几个函数把「宿主环境」当作外部边界接受替身输入：有没有委派能力、
# designer 模型可不可用、上下文能否隔离、审查是否受阻。被测对象是给定这些
# 输入时规则要求做什么，不是模型会不会照做。

DESIGNER_PAUSED = "paused"  # 模型不可用 → 停在施工闸前，不回落
DESIGNER_FALLBACK = "fallback"  # 无能力/禁止委派 → 主代理兜底，不记 d/r
DESIGNER_MAIN_AGENT = "main_agent"  # inherit → 主代理设计，施工必须委派开发者
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
    # 2) 无委派能力 / 用户禁止委派：主代理兜底。
    if user_forbids_delegation:
        return {
            "mode": DESIGNER_FALLBACK,
            "counts_dr": False,
            "gate": "open_self_checked",
            "reason": "用户明确禁止委派（setup.md 硬约束优先），按无能力兜底",
        }
    if not delegation_available:
        return {
            "mode": DESIGNER_FALLBACK,
            "counts_dr": False,
            "gate": "open_self_checked",
            "reason": "宿主不具备子代理委派能力，按无能力兜底",
        }
    # 3) 有委派能力。
    if model == "inherit":
        # 主代理亲自设计 → 施工必须委派开发者（不论 developer 配置）。
        return {
            "mode": DESIGNER_MAIN_AGENT,
            "counts_dr": True,
            "gate": "closed",
            "must_delegate_developer": True,
            "context_isolation": "isolated" if context_isolated else "limited",
            "reason": "designer=inherit，主代理撰写 design.md，施工须委派独立开发者",
        }
    # 独立设计实例：只解除「主代理兼任设计者」这一项，配置与升级仍须委派开发者。
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
    """normal 的一次独立审查及其转 full 时的预算继承。

    本类不判断用户最初为何选择 normal，只约束选择之后可观察的状态转换：
    恰好一个不同实例回传非空报告；必关发现让流程转 full，并把这次审查记为
    `r=1/5`，已有修复尝试不重置。
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
        self.review_recorded = False
        self.review_count = 0
        self.reviewer_instance_id = None
        self.report = ""
        self.must_close_findings = []

    @staticmethod
    def _is_must_close(finding: dict) -> bool:
        severity = str(finding.get("severity", "")).upper()
        return bool(
            severity in {"P0", "P1"}
            or (severity == "P2" and finding.get("in_scope", True))
            or finding.get("acceptance_gap")
            or finding.get("potential_high_risk")
        )

    def record_review(
        self,
        instance_id: str,
        report: str,
        *,
        findings: list[dict] | None = None,
    ) -> None:
        """记录 normal 唯一一次已完成的定向审查。"""
        if self.review_recorded:
            raise RuleError("normal 只允许一次独立定向审查，不得形成修复循环")
        if not instance_id.strip():
            raise RuleError("没有审查实例，不能算独立定向审查")
        if instance_id == self.author_instance_id:
            raise RuleError("作者自查不是独立定向审查")
        if not report.strip():
            raise RuleError("审查实例必须回传非空完成报告")

        must_close_findings = [
            finding for finding in (findings or []) if self._is_must_close(finding)
        ]
        self.review_recorded = True
        self.review_count = 1
        self.reviewer_instance_id = instance_id
        self.report = report
        self.must_close_findings = must_close_findings
        if self.must_close_findings:
            self.path = "full"
            # normal 的这一次定向审查就是进入 full 后的首次施工审查。
            self.r = self.prior_full_reviews + 1

    def finish(self) -> str:
        """返回当前流程的可交付状态，不代替后续 full 闭环。"""
        if self.path == "full":
            return "full 未闭环"
        if (
            self.review_count == NORMAL_REVIEW_CAP
            and self.reviewer_instance_id != self.author_instance_id
            and bool(self.report.strip())
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
    if task.get("multi_impl_instances") or task.get("independent_acceptance_stages", 0) >= 2:
        hits.append("多阶段施工")
    if (task.get("candidate_options", 0) >= 2 and task.get("candidates_both_conditions")) or task.get(
        "user_requests_comparison"
    ):
        hits.append("多方案")
    if task.get("high_risk"):
        hits.append("高风险边界")
    return {"enabled": bool(hits), "hits": hits}


class Budget:
    """两本账状态机。事件名对应 design.md 里会消耗轮次或改变闸状态的动作。"""

    def __init__(self, *, candidate_second_opt_in: bool = False, design_enabled: bool = True):
        self.d = 0
        self.r = 0
        self.design_enabled = design_enabled
        self.candidate_rounds = 0
        self.design_reviewed = False
        self.design_frozen = False
        self.blocked = False
        self.rework_count = 0  # 升版次数；版本号 = 1 + rework_count
        self.open_findings = 0  # 未关闭的确认缺陷（含范围内 P2、验收缺口）
        # 未启用本环节时没有施工闸，直接按 full 其余流程施工。
        self.gate_open = not design_enabled
        self.scope_expanded = False
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
        if not self.design_enabled:
            raise RuleError(f"未启用独立设计环节，不得{why}")

    # --- 设计阶段 ---------------------------------------------------------
    def candidate(self) -> None:
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
        if blocked:
            # 受阻既不是通过也不是缺陷：不得据此认为「确认缺陷全部关闭」
            self.blocked = True

    def rework(self) -> None:
        """升版（v(N+1)）：实现者发现契约不可行，或施工审核发现根因在设计。

        重写本身不消耗 `d`；计入 `d` 的是重写之后的那一次设计审查。否则一次
        升版会吃掉两轮，与「设计审查 3 轮」的口径不符。
        """
        if self.gate_open and self._r_left() < R_RESERVE:
            raise Stop(
                "未完成",
                f"剩余 r={self._r_left()} 不足两轮，不得单开设计重审——设计契约缺陷导致未完成",
            )
        self.design_reviewed = False
        self.design_frozen = False
        self.rework_count += 1
        self.log.append(f"design v{self.rework_count + 1} (rework, no round spent)")

    def resolve_findings(self, count: int | None = None) -> None:
        self.open_findings = 0 if count is None else max(0, self.open_findings - count)

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

    # 兼容：早期写法直接设 self.gate_open 时仍走同一断言。
    def pass_gate(self) -> None:
        self.open_gate()

    # --- 施工阶段 ---------------------------------------------------------
    def expand_scope(self) -> None:
        """施工中范围扩大后命中门控：停写，按升级进入重走设计闸。"""
        if not self.gate_open:
            raise RuleError("只有施工阶段的扩范围才走升级进入")
        self.scope_expanded = True
        self.gate_open = False
        self.design_frozen = False

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
        self.open_findings += findings
        if findings and self._r_left() < 1:
            raise Stop("未完成", "完整初审报出缺陷但已无复审轮次")

    def rereview(self) -> None:
        self._spend_r("复审")
        self.resolve_findings()

    def finish(self) -> str:
        if not self.gate_open:
            raise RuleError("未过施工闸不得宣称完成")
        if self.open_findings:
            raise Stop("未完成", f"仍有 {self.open_findings} 项未关闭")
        return "闭环完成"
