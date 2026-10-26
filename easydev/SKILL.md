---
name: easydev
description: >-
  编码、修复、重构、设计、计划、排障与审查；按风险选择 quick / normal / full。适用于 coding 或显式调用；纯解释及非编码请求不启动开发流程。
metadata:
  version: "1.1.0"
---

# easydev

子代理只执行交接职责，读 [开发角色](references/developer-role.md) 或 [审查角色](references/reviewer-role.md)；不重新分流、分派、重置预算或宣布任务完成。

## 入口与任务分流

默认 auto，先确认交付类型。仅要 design / plan / debug 定位 / review / audit / spike 时只交付对应产物；实现才按实际影响选档：

| 路径 | 条件与动作 |
| --- | --- |
| quick | 局部、明确、低风险：实施、验证、自查 |
| normal | 影响有界、需第二视角且无 full 条件：一次独立定向审查，无 full 账本 |
| full | 高风险、重大契约/数据影响、具体关键影响调查后仍不明，或用户要求闭环：设计按门控，独立审查与有限修复 |

修改鉴权、支付、敏感数据、并发、迁移、安全随机数、供应链或部署权限时核对风险。风险疑点、normal/full 或路径变化读 [分流规则](references/routing.md)；文件数和关键词不单独决定档位。

## 开始工作

读项目约定，核对目标、授权、Git 状态及基线；修改前读目标、直接调用方和相关测试。一句话说明路径与依据。保护用户改动，按已有授权推进；高代价歧义先澄清。

## quick 执行核心

普通 quick 只需本页。

1. 复用现成模式，做最小完整改动，同步受影响调用方、契约和说明。
2. 先核对已有覆盖；只为具体缺口补行为测试。Bug 先复现再修复；重构验证行为保持，不造假红测。期望值来自需求或独立契约，只 mock 外部边界。
3. 用项目脚本验证；独立无冲突检查批量并行，依赖项串行，收齐每项退出状态。阻塞命令有硬超时；只在新改动、失败或未决疑点使证据失效时重跑。
4. 同一问题整个任务最多三次有新证据的修复尝试；失败先取证，达到上限停止，切路径或换实例不重置。环境故障不计修复次数。
5. 交付前核对调用方、验证证据与风险边界；出现新影响按分流规则重判。报告改动、验证、未做独立审查及实质限制，不把未执行写成通过。

## 按需加载

只读当前职责所需模块，专项模块按其触发条件使用。

| 场景 | 模块 |
| --- | --- |
| normal、风险判定、升降级 | [routing](references/routing.md) |
| full 编排 | [orchestration](references/orchestration.md)；[独立设计环节门控](references/design.md#启用门控) |
| 复杂验证、性能效果声明 | [execution](references/execution.md) |
| 设计、计划、探针；实质多方案 | [planning](references/planning.md)；[candidates](references/candidates.md) |
| 排障；非机械重构 | [debugging](references/debugging.md)；[refactoring](references/refactoring.md) |
| 现有检查不足，需实际操作产品 | [verification](references/verification.md) |
| 脏工作区、敏感文件、恢复；工作树 | [baseline-safety](references/baseline-safety.md)；[worktree](references/worktree.md) |
| review、audit、full 审查 | [review](references/review.md) |
| 实际分派或用户要求配置；能力受阻 | [models](references/models.md)；[setup](references/setup.md) |

## 全程约束

授权跨阶段持续有效；提交、推送、发布、部署、对外消息须有用户授权。仅主代理控制流程，其他技能提供专项方法，不另开闭环。区分自查、独立审查与能力受阻；无子代理按 setup 交付自查及审查包，不虚称 full 完成。发现、预算与停止条件不得靠改名绕过。
