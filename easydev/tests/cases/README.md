# 测试用例目录

本目录保存可执行的自包含确定性用例。每个子目录使用描述行为的语义化 slug，包含被测初始状态、参考材料与独立验收入口。

## 约定

- **目录命名**：描述验收行为，不使用易失的数字顺序编号。
- **材料隔离**：`initial/` 为执行者可见的初始仓库；`solution.patch`、独立验收脚本等属于评测私有材料，评测时须与执行者隔离。
- **执行封装**：`verify.py` 负责确定性复验并可直接进入 CI；`run.sh` 为标准执行入口；真实模型回放由 `tests/experiments/agent_runner/` 负责。

## 用例列表

| 用例 | 验证目标 | 执行入口 |
| --- | --- | --- |
| `public-contract-callers` | 公共契约变更、对象所有权校验与调用方适配 | `bash tests/cases/public-contract-callers/run.sh` |
| `version-gate` | `SKILL.md` 版本提取合法性与非法格式防御（PyYAML 与标准库双分支） | `bash tests/cases/version-gate/run.sh` |
| `models-config` | `scripts/models.py` schema 校验与来源优先级（项目 > 用户 > 默认） | `bash tests/cases/models-config/run.sh` |
| `design-gate` | normal 审查复审、转 full 继承、启用门控、两本账与施工闸状态机 | `bash tests/cases/design-gate/run.sh` |
| `audit-overflow` | 23 项发现时正文上限 20 条、附录全量保留与溢出 P1 披露 | `bash tests/cases/audit-overflow/run.sh` |

其余行为场景由 [tests/behavior.md](../behavior.md) 定义：
- B01、B02、B16、B61（观测部分）、B67 由 `tests/experiments/agent_runner/` 驱动。
- B60 与 B61（确定性部分）由 `design-gate` 覆盖。
- B65（确定性部分）由 `audit-overflow` 覆盖。
