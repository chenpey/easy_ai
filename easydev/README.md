# EasyDev

当前版本：`1.4.4`

EasyDev 按实际风险组织编码、设计、排障和审查。入口不超过 4 KiB，普通 quick 不加载完整编排；详细规则按需读取。

## 使用

```text
使用 easydev，为保存按钮修正文案。
使用 easydev normal，为已有 API 增加兼容的筛选参数并检查调用方。
使用 easydev full，修复订单读取的对象所有权校验。
使用 easydev debug，排查重复提交。
使用 easydev review，审查当前改动，先不要修改代码。
```

默认 auto，调查后选择流程。Codex 可用 `$easydev`；这些路径是提示词约定，不是安装的运行时开关。

| 路径 | 用途 |
| --- | --- |
| quick | 局部明确、低风险：实现、验证、自查 |
| normal | 影响有界且具第二视角信号：实现、验证、独立定向审查（允许至多一次局部 P2 修复与复审），无 full 账本 |
| full | 高风险、重大契约/数据影响或具体关键影响仍不明，或用户要求闭环：按门控设计，独立审查、有限修复与复审 |
| design / plan | 设计权衡或实施计划，按请求范围交付 |
| debug | 先定位；授权修复后按实际风险选档 |
| review / audit | 单次只读审查或分片审计 |
| spike | 最小可行性实验，产物标 throwaway |

normal 审完整任务 diff、直接调用方和相关测试；首轮仅局部 P2 时允许一次局部修复与定向复审，其余必关发现转 full，继承已用审查和修复预算。风险被证据排除时允许降低流程，用户指定档位及未关闭必关项不能被自动降级。完整判断、边界示例及状态转换以 [分流规则](references/routing.md) 为准。

1.1.0 保留 quick/full 名称，新增 normal；原聊天表达 normal/lite/ultra 已移除，normal 现在表示实现路径。聊天长短遵循用户要求，推理深度仍由 reasoning_effort 指定。

## 安装

将以下提示词交给 Agent；更新时同样适用：

```text
请安装或更新 EasyDev：
https://github.com/chenpey/easy_ai/tree/main/easydev

识别当前操作系统和 Agent，将 main 分支的 SKILL.md、references/ 和可选展示元数据 agents/ 安装到用户主目录下的 ~/.agents/skills/easydev，保持相对位置。不要安装测试或其他技能，不改动用户模型配置。

若宿主不识别该目录，在其实际用户级技能目录中创建链接；不支持链接时采用受支持的目录联接或复制。更新前检查已有文件，保留本地修改及自建文件，只替换未被本地修改的旧版文件；处理新版已移除文件时也遵守此限制，冲突先说明。

检查技能和引用文件完整性、链接有效性，报告路径、版本和生效步骤；未验证宿主实际加载时明确说明。优先使用已有工具，避免安装不必要的依赖。
```

模型、密钥、网络、沙箱与子代理能力由宿主提供。安装成功不代表宿主已经加载；能力要求与故障处理见 [环境适配](references/setup.md)。

可选 Hook Plugin 独立于上述安装集，用于把可观测的 Git、脏目标首次快照、reviewer 只读、模型配置与交接、审后写提醒下沉为确定性检查；未激活会话在入口 O(1) 放行，未安装时 Skill 行为完全照旧。安装、能力差异与未验证项见 [Hooks Plugin](hooks/README.md)。

## 模型配置

默认所有角色继承宿主，无须配置（等价于三者都写 `inherit`、`upgrades` 为空）。开发角色重视项目实现和工具执行，审查角色重视独立发现边界与失败路径，设计角色重视契约及权衡；按宿主实际支持的能力与 ID 选取，规则本身不固化供应商版本。

下面的内联示例是仓库里唯一的模型配置示例（不再另存示例文件），取自维护者 2026-10-27 复评的一组分配：开发与审查分属不同供应商，`upgrades` 在升级阶段换上更强的实现模型、并把审查提到同一模型支持的更高推理档位，两级都保持实现与审查不同供应商。模型 ID 与供应商取决于宿主，换宿主须逐字替换为本机支持的完整 ID；原样照抄到没有对应 provider 的宿主上，会在第一次分派时按 [模型配置](references/models.md) 暂停报受阻，不会回落到 `inherit`。不另放全 `inherit` 的示例文件：内置默认已是三个角色 `inherit`、`upgrades` 为空，重复一份只会与默认值漂移。示例不会自动加载，复制到选定位置或在对话中明确指定：

```json
{
  "developer": {
    "model": "opencode-go/deepseek-v4.1-flash",
    "reasoning_effort": "high"
  },
  "reviewer": {
    "model": "codex/gpt-6.1-sol",
    "reasoning_effort": "high"
  },
  "designer": {
    "model": "codex/gpt-6.1-sol",
    "reasoning_effort": "high"
  },
  "upgrades": [
    {
      "developer": {
        "model": "antigravity/gemini-4-argon",
        "reasoning_effort": "high"
      },
      "reviewer": {
        "model": "codex/gpt-6.1-sol",
        "reasoning_effort": "max"
      }
    }
  ]
}
```

这组分配的判据是同一天的实测用量、公开基准与路由可用性；数字随模型版本变化，不构成仓库保证。同一档位才有可比性：本基准里同一模型 `high` 与 `max` 能差出 7–18 分，跨档位比较会把结论反过来。

- 开发用 `deepseek-v4.1-flash`：DeepSWE 77.0 在可调用模型中第二高（仅次于 Argon 的 77.9），单价 0.15/0.6、缓存读取 0.003；182 次子代理运行中 39 次开发任务 0 失败、平均 121s。日常路径占用量最大，所以这里要的是单价与长任务成功率，不是榜单第一。
- 审查用 `gpt-6.1-sol`：Bug Hunt Bench max 44.3（配置的 high 档实测 36.5）、AA Intelligence 51.8，是主循环里可稳定付费调用且缺陷发现能力最强的选项。审查占实测子代理支出的 62%，换更便宜的模型省的是钱、付出的是漏审。
- 升级阶段开发换 `gemini-4-argon`（Antigravity 订阅）：DeepSWE 77.9、SWE-bench Pro 81.5（估算）、AA Intelligence 52.6、Bug Hunt 40.7（实测于 Antigravity CLI high 档，正是本机请求的路径与档位）。它是唯一在两条实现指标上都不低于阶段 0 的候选：DeepSWE 77.9 ≥ 77.0，SWE-bench Pro 81.5 补上 62.7 的短板。
- 升级阶段审查把 `gpt-6.1-sol` 提到 `max`：同一模型 44.3 对 high 的 36.5，代价约 1.8× 成本与 2× 墙上时间，且只在一次修复失败后触发。不换成 Antigravity 的 Claude：`sonnet-5-5` 的 51.3 是 Claude Code 的 `max` 档，Antigravity 只到 high，同档实测 34.3（`opus-5-5` 为 31.7），并不高于 Sol 的 36.5。
- 未采用 `grok-4.7`：Bug Hunt high 19.3（自身最高 xhigh 29.0）是可调用模型里最低一档，DeepSWE 71.0 低于阶段 0 开发，单价 2/6 且缓存读 0.5 是 `deepseek-v4.1-flash` 的百倍量级，代理负载（约八成是缓存重读）折算约 19×；它只在 AA Agentic 58.4 上占优，不足以支撑一个升级位。
- 未采用 `gpt-6-astra` 与其他 `zed/*`：Astra 的 Bug Hunt max 45.0 与 Sol 的 44.3 之差在重跑噪声内（本基准同配置重跑的中位偏移为 1.8 分），却要 5× credits 与 5–45 条/5h 的额度；`zed/*` 实测 403（额度耗尽）。
- `antigravity/claude-opus-5-5` 不进 `upgrades`：SWE-bench Pro 89.9 实测最强，但订阅额度只够偶尔手动调用，按对话显式指定作一次性覆盖，不占用自动升级位。
- 深度只写模型实际支持的档位：Antigravity 侧 low/medium/high 有区分，`xhigh`/`max` 会映射为 high；Codex 侧可到 `max`，但请求值是否真的生效要按 [模型配置](references/models.md) 用实际返回值核对，不能凭请求记账。

仅真正分派或用户要求配置时读取。当前对话角色字段覆盖优先；基础文件按对话指定文件、项目根 `.easydev/models.json`、用户配置、内置默认的顺序选取，文件不合并。选中配置无效或不可读时暂停相关分派，不静默回退。

例如在 macOS/Linux 查看个人配置（存在时）：

```bash
cat ~/.config/easydev/models.json
```

Windows 路径、字段、模型阶段和实际生效核对统一见 [models.md](references/models.md)。项目级覆盖及对话指定文件优先于用户配置是本版变化；未设置更高来源时，原用户配置继续有效。示例不会自动生效，安装更新不修改个人配置。

## 验证与交付

- 复用充分检查，只为明确覆盖缺口新增行为测试；按项目脚本运行，独立无冲突项并行，依赖项顺序执行。详细规则见 [execution](references/execution.md)。
- quick 报自查；normal 报独立定向审查（含必要的一次局部 P2 复审）；full 按 [编排与预算](references/orchestration.md) 判定闭环。达到预算上限不等于完成，修复尝试以 [尝试预算](references/routing.md#尝试预算) 为准。
- 缺独立能力时完成授权范围内自查，给可交接审查包并明确缺口；不能称独立审查通过或 full 闭环完成。不同实例继承上下文时披露隔离限制，见 [setup](references/setup.md#能力降级与审查包)。
- audit 按模块或信任边界分片，记录覆盖与跨片接口；正文最多 20 条，全部发现保留附录/审查产物，关键缺陷不会因截断丢失。
- 待审脚本执行与凭证隔离遵守 [审查规程](references/review.md#不可信代码执行)。保留用户改动；提交、推送、发布和对外动作遵循明确授权。

## 详细规则与维护

运行规则以 [SKILL.md](SKILL.md) 和按需引用模块为准，README 不维护第二份细则。

- [开发角色](references/developer-role.md)、[审查角色](references/reviewer-role.md)：短交接入口，不重新遍历完整技能
- [独立设计](references/design.md)、[计划](references/planning.md)、[候选评审](references/candidates.md)
- [重构](references/refactoring.md)、[排障](references/debugging.md)、[产品验证](references/verification.md)
- [代码质量判据](references/code-quality.md)：克制复杂度、边界校验、结构与抽象、控制流、类型与异常、同步与异步、依赖方向、术语、注释、生成文件（代码写法的唯一细则源）
- [基线恢复](references/baseline-safety.md)、[工作区](references/worktree.md)
- [维护检查](tests/README.md)、[行为规约](tests/behavior.md)、[实验](tests/experiments/README.md)、[版本历史](CHANGELOG.md)

`tests/` 不随运行时安装。离线替身只验证验收机制，不证明真实模型遵守规则；入口字节减少也不等于已证明 token、费用或耗时收益。修改规则时同步相关引用、行为规约及受影响的可执行检查，发布保持版本一致。

入口 4096 字节上限触顶时按固定顺序腾挪，不临时压缩措辞、不放宽上限：先合并按需加载表里相近的行，或把新模块并入现有行，再把计量与报告格式细节移进 routing，然后才考虑把入口的高风险边界枚举压成一句并指向 routing，最后才把按需加载表移进 routing（quick 需要它，因此放最后）。只改措辞、计数或错别字的补丁（不改判定条件、预算、状态机或可执行断言）最多跑一轮 normal 独立审查，不按 full 闭环反复复审；这一轮报 P0/P1、或改动实际超出措辞与计数范围时，按 [routing](references/routing.md) 的转 full 规则执行。
