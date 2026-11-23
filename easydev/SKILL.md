---
name: easydev
description: >-
  编码、修复、重构、设计、计划、排障与审查；按风险选择 quick / normal / full / gate。适用于 coding 或显式调用；纯解释及非编码请求不启动开发流程。
metadata:
  version: "2.14.2"
---

# easydev

主代理默认唯一写入者，委派见 [full 编排](references/workflow.md#full-编排)；子代理读 [roles.md](references/roles.md)（设计 `#L5-L14`/开发 `#L15-L24`/审查 `#L25-L37`）。

## 入口与任务分流

默认 auto。仅 design/plan/debug/review/audit（只读）或 demo（`throwaway`）只交产物，不改业务代码、不跑全量测试、构建与全仓检查；自问：≥2 调用方？改共享契约有未改方？无改前检查拦回归？用户是否要求独立审查？任一成立读 workflow。

| 路径 | 条件与动作 |
| --- | --- |
| quick | 局部低风险、无第二视角信号：实施、验证、自查 |
| normal | 有界、有第二视角信号、非 full：独立定向审查，至多一次局部 P2 复审，无账本 |
| full | 高风险、重大外部契约/持久数据影响、不明或要闭环：门控设计、独立审查、有限修复 |
| gate | 外部设计：审设计无分歧再开发自验，跳 `r/5`；高风险或重大外部契约/持久数据影响走 full（[workflow](references/workflow.md#gate-模式)） |

高风险边界（鉴权/支付/敏感数据/并发/破坏性/密码学/CI/供应链/生产日志等）读 [workflow](references/workflow.md#高风险边界)。

## 开始工作

读约定、授权、Git 基线（提交与首次原内容，不含测试结果）、目标、调用方与测试；**改前不跑既有测试/构建摸底**（新红测与无 CI/旧记录的[纯重构](references/engineering.md#测试硬约束与选择)单次对照除外），改后分既有与回归；约定优先，不松安全/授权底线。一句说依据；护改动，高代价歧义走 [澄清](references/design.md#选项式澄清)。

## quick 执行核心

quick 默认只读本页；例外必读：性能比较或收益声明读 [性能](references/engineering.md#测试硬约束与选择)，测试依赖未知外部能力读 [验证调度](references/engineering.md#验证调度与证据)；均不因此升级 full。

1. 复用已有与平台能力，最小改同步调用方、契约与文档。单次内联，≥2 处提取；禁加未要求类/接口/配置/参数/wrapper。仅边界防错，不减安全、错误处理与清理；不手改生成产物，改源头后重生；不用 `any`、空 catch 逃生舱写法。
2. 先核覆盖，仅核心与失败缺口补端到端测试；不写单测，不用 mock 隔离内部。期望取需求/独立源，新增/Bug 记红绿，重构验不变。
3. 用项目脚本；独立并行，同区在跑或已过未失效的重叠检查不重跑（工作树分片/性能采样不限）。阻塞设硬超时（委派按 AGENTS §2 例外）；交前跑完整检查，纯文档免跑。
4. 修复：同根因跨阶段限 3 次新证据候选修复（调查/环境不计），按根因/症状识别，换实例或编号不重置；三次仍失败即停。
5. 交前简化冗余；核调用方、证据与风险，新影响重判。报改动、验证、未独立审查与限制，禁虚报；quick 计量仅在用户要求时提供（尝试次数与验证耗时）。

## 按需加载

| 场景 | 模块（优先按行号分段读） |
| --- | --- |
| normal/full/gate 与预算 | [workflow](references/workflow.md)（分流/normal `#L5-L59`；full/gate `#L60-L148`）；[独立设计环节门控](references/design.md#启用门控) |
| 并行分片与流水线 | [parallel](references/parallel.md#并行执行) |
| 实现、质量、排障、重构、验证 | [engineering](references/engineering.md)（常规 `#L5-L58`；其余 `#L59-L87`） |
| 澄清、计划、多方案；review/audit | [design](references/design.md)；[review](references/review.md) |
| 模型、降级、基线、工作树 | [environment](references/environment.md)（模型 `#L3-L25`；其余 `#L26-L64`） |

## 全程约束

- 授权跨阶段有效；提交、推送、发布、部署与外发须授权。
- 仅主代理控流程，他技能不另开闭环。
- 并行分片先读 parallel：内容自判 1–6 片，用户指定优先；normal 单写，full 并写须满足独立性与隔离门槛，串行包装不得称并行。
- 除 quick/gate/无委派只读路径外，开工先按 environment 核实角色配置；未核实不得假定 inherit 自任设计或写入，分派以核实值为准。（[模型](references/environment.md#模型配置) `#L3-L25`）
- 区分自查、独立审查与受阻；无委派暂停报受阻，授权后交审查包，不虚称 full 完成。
- 发现、预算与停止条件禁靠改名绕过。
