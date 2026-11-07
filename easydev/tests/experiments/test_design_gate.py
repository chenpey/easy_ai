"""流程模型公开 API 回归：规则输入、事件顺序与可观察的终态，不模拟内部实现。"""

import importlib.util
from pathlib import Path
import shutil
import subprocess

import pytest


MODEL_PATH = Path(__file__).resolve().parents[1] / "cases/design-gate/gate_model.py"
SPEC = importlib.util.spec_from_file_location("design_gate_model", MODEL_PATH)
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)


@pytest.mark.parametrize("designer", ["inherit", "host/designer"])
@pytest.mark.parametrize(
    "developer_non_inherit,in_upgrade_stage,expected",
    [(False, False, False), (True, False, True), (False, True, True), (True, True, True)],
)
def test_designer_does_not_force_developer(designer, developer_non_inherit, in_upgrade_stage, expected):
    result = gate.resolve_designer(
        model=designer,
        developer_non_inherit=developer_non_inherit,
        in_upgrade_stage=in_upgrade_stage,
    )
    assert result["must_delegate_developer"] is expected
    assert result["gate"] == "closed"


@pytest.mark.parametrize(
    "stages,expected",
    [(1, {"enabled": False, "hits": []}), (2, {"enabled": True, "hits": ["多阶段施工"]})],
)
def test_gate_depends_on_acceptance_stages(stages, expected):
    assert gate.decide_gate({"multi_impl_instances": True, "independent_acceptance_stages": stages}) == expected


@pytest.mark.parametrize("design_enabled", [False, True])
@pytest.mark.parametrize("incremental", [False, True])
def test_incremental_or_design_review_cannot_replace_full_review(design_enabled, incremental):
    budget = gate.Budget(design_enabled=design_enabled)
    if design_enabled:
        budget.design_review()
        budget.open_gate()
    if incremental:
        budget.incremental_review()
    with pytest.raises(gate.RuleError, match="完整"):
        budget.finish()
    budget.full_review()
    assert budget.finish() == "闭环完成"


def test_rework_closes_gate_and_invalidates_previous_full_review():
    budget = gate.Budget()
    budget.design_review()
    budget.open_gate()
    budget.full_review()
    budget.rework()
    assert budget.gate_open is False
    assert (budget.d, budget.r) == (1, 1)
    with pytest.raises(gate.RuleError, match="施工闸"):
        budget.full_review()
    budget.design_review()
    budget.open_gate()
    with pytest.raises(gate.RuleError, match="完整"):
        budget.finish()
    budget.full_review()
    assert budget.finish() == "闭环完成"
    assert (budget.d, budget.r) == (2, 2)


def test_repair_needs_rereview_after_findings_resolved():
    budget = gate.Budget(design_enabled=False)
    budget.full_review(findings=1)
    budget.resolve_findings()
    with pytest.raises(gate.RuleError, match="审查"):
        budget.finish()
    budget.rereview()
    assert budget.finish() == "闭环完成"


def test_dependency_change_invalidates_review_without_refunding_budget():
    budget = gate.Budget(design_enabled=False)
    budget.full_review()
    budget.record_change()
    with pytest.raises(gate.RuleError, match="审查"):
        budget.finish()
    assert budget.r == 1
    budget.rereview()
    assert budget.finish() == "闭环完成"
    assert budget.r == 2


def test_rereview_cannot_replace_initial_full_review():
    budget = gate.Budget(design_enabled=False)
    with pytest.raises(gate.RuleError, match="完整"):
        budget.rereview()
    assert budget.r == 0


@pytest.mark.parametrize("prior_reviews,attempts", [(0, 3), (4, 0)])
def test_normal_does_not_repair_without_remaining_budget(prior_reviews, attempts):
    flow = gate.NormalReviewFlow(author_instance_id="author", prior_full_reviews=prior_reviews, repair_attempts=attempts)
    flow.record_review("reviewer", "局部 P2", findings=[{"severity": "P2", "in_scope": True, "is_local": True}])
    with pytest.raises(gate.Stop, match="预算|轮次|尝试"):
        flow.record_repair()
    assert flow.repair_attempts == attempts
    assert flow.review_count == 1
    assert flow.finish() == "normal 未完成"


@pytest.mark.parametrize("findings,expected", [([], "normal 完成"), ([{"severity": "P1"}], "full 未闭环")])
def test_normal_last_available_review_can_complete_or_return_full(findings, expected):
    flow = gate.NormalReviewFlow(author_instance_id="author", prior_full_reviews=4)
    flow.record_review("reviewer", "最终审查报告", findings=findings)
    assert flow.finish() == expected
    assert flow.r == 5


def test_normal_can_use_exactly_remaining_repair_and_review_budget():
    flow = gate.NormalReviewFlow(author_instance_id="author", prior_full_reviews=3, repair_attempts=2)
    flow.record_review("reviewer", "局部 P2", findings=[{"severity": "P2", "in_scope": True, "is_local": True}])
    flow.record_repair()
    flow.record_review("reviewer", "缺陷已关闭")
    assert flow.finish() == "normal 完成"
    assert (flow.repair_attempts, flow.r) == (3, 5)


def test_design_review_can_recover_from_blocked_round():
    budget = gate.Budget()
    budget.design_review(blocked=True)
    with pytest.raises(gate.Stop, match="受阻"):
        budget.open_gate()
    budget.rework()
    budget.design_review()
    budget.open_gate()
    budget.full_review()
    assert budget.finish() == "闭环完成"
    assert (budget.d, budget.r) == (2, 1)


def test_normal_repair_cannot_continue_after_full_transition():
    flow = gate.NormalReviewFlow(author_instance_id="author")
    flow.record_review("reviewer", "P1 必须转 full", findings=[{"severity": "P1"}])
    with pytest.raises(gate.RuleError, match="full"):
        flow.record_repair()
    assert flow.repair_attempts == 0


def test_incremental_budget_preserves_final_reviews():
    budget = gate.Budget(design_enabled=False)
    for _ in range(3):
        budget.incremental_review()
    with pytest.raises(gate.Stop, match="预留两轮"):
        budget.incremental_review()
    assert budget.r == 3
    budget.full_review(findings=1)
    budget.resolve_findings()
    budget.rereview()
    assert budget.finish() == "闭环完成"
    assert budget.r == 5


def test_normal_upgrade_inherits_candidate_and_review_budgets_into_full():
    flow = gate.NormalReviewFlow(author_instance_id="author")
    flow.record_candidate()
    with pytest.raises(gate.RuleError, match="第 2 轮"):
        flow.record_candidate()
    local_p2 = {"severity": "P2", "in_scope": True, "is_local": True}
    flow.record_review("reviewer", "首轮局部 P2", findings=[dict(local_p2)])
    flow.record_repair()
    flow.record_review("reviewer", "复审仍未关闭", findings=[dict(local_p2)])
    assert (flow.path, flow.candidate_rounds, flow.r) == ("full", 1, 2)

    budget = flow.to_full_budget(design_enabled=True)
    assert (budget.d, budget.r, budget.gate_open) == (1, 2, False)
    budget.design_review(findings=1)
    budget.rework()
    budget.design_review()
    budget.resolve_findings()
    budget.open_gate()
    # 已继承 r=2，阶段增量审查最多只能跑 3 - 2 = 1 轮。
    budget.incremental_review()
    with pytest.raises(gate.Stop, match="预留两轮"):
        budget.incremental_review()
    budget.full_review(findings=1)
    budget.resolve_findings()
    budget.rereview()
    assert budget.finish() == "闭环完成"
    assert (budget.d, budget.r) == (3, 5)


def test_reentering_full_preserves_prior_design_reviews_and_separates_candidate_count():
    # 先前 full 已用 d=1（设计审查，非候选）與 r=1，降级 normal 后做 1 轮候选，再因 P1 重入 full：
    # 继承后总 d=2、候选轮数=1、r=2；剩余 d 仅 1 轮（只够首次设计审查，不够升版重审）。
    flow = gate.NormalReviewFlow(
        author_instance_id="author",
        prior_design_reviews=1,
        prior_full_reviews=1,
    )
    flow.record_candidate()
    flow.record_review("reviewer", "发现 P1", findings=[{"severity": "P1"}])
    budget = flow.to_full_budget(design_enabled=True)
    assert (budget.d, budget.r, budget.candidate_rounds) == (2, 2, 1)
    budget.design_review(findings=1)
    with pytest.raises(gate.Stop, match="d/3 已用尽"):
        budget.rework()
    assert (budget.d, budget.r, budget.rework_count) == (3, 2, 0)


@pytest.mark.parametrize(
    "rel,old,new,label",
    [
        ("references/design.md", "主代理可继续施工", "施工必须委派独立开发者", "主代理设计不额外强制委派施工"),
        ("references/workflow.md", "实施保持单写入者", "实施允许多写入者", "normal 单写并受历史预算约束"),
        ("references/workflow.md", "独立性与隔离条件全部成立才可并行，否则串行", "无条件并行，不检查独立性与隔离条件", "并行判断门槛、验证依赖与集成预算"),
        ("references/workflow.md", "验证依赖变化使相关证据失效", "验证依赖变化不影响已有证据", "并行判断门槛、验证依赖与集成预算"),
        ("references/workflow.md", "内容自判 1–5 片，用户指定片数优先", "内容自判任意片数，用户指定不生效", "并行片数由内容判断且用户指定优先"),
        ("references/workflow.md", "高风险边界默认串行", "高风险边界默认并行", "高风险并行例外须明确要求并可隔离恢复"),
        ("references/workflow.md", "串行包装不得称并行", "串行包装可称并行", "并行判断门槛、验证依赖与集成预算"),
        ("references/workflow.md", "各批验证及最终整仓验证期间所有相关写入者停写", "各批验证及最终整仓验证期间允许写入者继续修改", "并行判断门槛、验证依赖与集成预算"),
        ("tests/experiments/fold-eval.md", "不作为启用并行写入的前置条件", "作为启用并行写入的前置条件", "并行收益观测可选且不作为准入门槛"),
        ("references/design.md", "默认最多用 1 轮", "按至多 2 轮独立评审", "quick 与 normal 候选第 2 轮须明确要求并知悉转 full 余量代价"),
    ],
)
def test_design_gate_cli_rejects_rule_regression(tmp_path, rel, old, new, label):
    root = MODEL_PATH.parents[3]
    copied = tmp_path / "easydev"
    paths = [
        "SKILL.md", "tests/behavior.md", "tests/experiments/fold-eval.md",
        "references/workflow.md", "references/design.md", "references/roles.md",
        "references/environment.md", "references/engineering.md", "references/review.md",
        "tests/cases/design-gate/run.sh", "tests/cases/design-gate/verify.py",
        "tests/cases/design-gate/gate_model.py",
    ]
    for path in paths:
        destination = copied / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / path, destination)
    command = ["bash", "tests/cases/design-gate/run.sh"]
    baseline = subprocess.run(command, cwd=copied, capture_output=True, text=True, timeout=30)
    assert baseline.returncode == 0, baseline.stdout + baseline.stderr
    target = copied / rel
    text = target.read_text()
    assert old in text
    target.write_text(text.replace(old, new, 1))
    result = subprocess.run(command, cwd=copied, capture_output=True, text=True, timeout=30)
    assert result.returncode == 1
    assert "文档契约" in result.stdout
    assert f"FAIL: {label} ->" in result.stderr
    assert "Traceback" not in result.stderr
