# 路由复放协议

本协议用于评估 EasyDev 路径分流规则（第二视角信号判定与 normal 独立审查价值）。仅定义口径与评估方法，不含结果，不进 CI。

## 任务选取

选取真实历史改动或 issue，每批 3 个任务，分别覆盖：
1. **quick 预期**：局部私有逻辑改动，改动前具备拦截回归的自动化测试。
2. **normal 预期**：有界兼容改动，存在 ≥2 个独立内部调用方或未修改的共享契约使用方（命中信号②或③）。
3. **full 预期**：改变重大外部公共契约、持久化数据语义或高风险鉴权逻辑（命中设计门控）。

每个任务须配备**执行者不可见的独立验收**（口径同 [四臂消融](../review-ablation/README.md#任务与预算)）。

## 复放编排

每个任务分别以 `quick`、`normal`、`full` 各跑一次（单批 9 次），保持基线提交与模型配置一致，串行从干净工作区启动：

```bash
# 准备环境
python3 tests/experiments/swebench/run.py setup \
  --dataset … --templates … --venvs … --experiment /tmp/rr

# 运行复放（逐臂串行保证隔离）
python3 tests/experiments/swebench/run.py run \
  --experiment /tmp/rr \
  --tasks <t1> <t2> <t3> \
  --arms easydev-quick easydev-normal \
  --repeats 1 --concurrency 1

# 汇总结果
python3 tests/experiments/swebench/run.py summarize --experiment /tmp/rr
```

角色 references 读取与 token 消耗通过 `role_metric.py`（pi 宿主）或 `analyze_events.py`（Codex 宿主）统计。

## 核心记录项

| 字段 | 来源与要求 |
| --- | --- |
| 模块读取 | 工具调用事件，无事件时标注为自述 |
| 总 Token 与耗时 | 宿主 usage 真实数据，缺失标注未知 |
| 审查次数与预算 | 审查报告与设计账本记录 |
| 本档确定缺陷 | 各档审查报告报出的 P0–P3 缺陷 |
| 漏检缺陷 | 交付后独立验收暴露的问题 |
| quick 无法拦截的缺陷 | 核心收益指标：通过比对独立验收与各档报告确认 |

## 判读规则

- **信号②收紧线索**：若多个命中信号②的任务在 normal 独立审查中均为零发现，且在 quick 下也未引入回归，作为该信号可能过宽的评估线索。
- **信号①放宽否证**：若 quick 任务漏掉后续暴露的 P1/P2 缺陷，即构成不可放宽 quick 准入门槛的直接证据。
- **数据留存**：实验记录按日期存档于 `tests/experiments/routing-replay/<date>/`。
