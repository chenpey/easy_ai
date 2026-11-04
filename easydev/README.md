# EasyDev

当前版本：`2.0.0`

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

normal 审完整任务 diff、直接调用方和相关测试；首轮仅局部 P2 时允许一次局部修复与定向复审，其余必关发现转 full，继承已用审查和修复预算。风险被证据排除时允许降低流程，用户指定档位及未关闭必关项不能被自动降级。完整判断、边界示例及状态转换以 [流程规则](references/workflow.md#分流判定) 为准。

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

模型、密钥、网络、沙箱与子代理能力由宿主提供。安装成功不代表宿主已经加载；能力要求与故障处理见 [环境与能力适配](references/environment.md#宿主适配与能力降级)。

## 模型配置

默认所有角色继承宿主，无须额外配置（等价于各角色均设为 `inherit`、`upgrades` 为空）。

如果自行配置，**需要使用当前 Harness（宿主/运行环境，如 Antigravity、Codex 等）能识别的完整模型 ID 与供应商前缀**；原样照抄到没有对应 Provider 的宿主上，会在第一次分派时按 [模型配置](references/environment.md#模型配置) 暂停报受阻，不会静默回退到 `inherit`。规则本身不固化特定供应商版本。

下面的内联示例演示了一组典型的跨供应商升级分配方案（开发与审查分属不同供应商，`upgrades` 在升级阶段切换更强模型与更高推理档位）：

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

### 选型考虑与说明示例

各角色的模型选取考虑（数字基于实测与公开基准，仅供选型参考）：

- **开发角色**：示例采用 `deepseek-v4.1-flash`。在日常实现与长任务中单价低、缓存读取成本低、执行稳定。日常路径占用量最大，核心诉求是高性价比与长任务成功率。
- **审查角色**：示例采用 `gpt-6.1-sol`（配置 `high` 档）。缺陷发现与边界分析能力强，是独立审查中能稳定捕捉漏洞的关键选择；审查占子代理核心支出，避免因省成本导致漏审。
- **升级阶段开发（`upgrades`）**：示例切换为 `antigravity/gemini-4-argon`。在基准与代码任务中具备更高的解决上限，用于在初次修复失败后补足实现能力。
- **升级阶段审查（`upgrades`）**：示例将 `gpt-6.1-sol` 提升至 `max` 推理档位。用更高的推理代价换取更严苛的复审深度，且仅在修复失败重试时触发。
- **其他未采用/特殊模型考量**：
  - `grok-4.7`：代码与审查基准评分偏低，缓存重读与调用单价偏高，不适合作为常规主力或升级位。
  - `gpt-6-astra` 与其他 `zed/*`：虽有一定发现能力，但额度消耗或受限率高，波动较大。
  - `antigravity/claude-opus-5-5`：能力顶尖但配额极为珍贵，建议作为对话中针对特定关键问题的单次显式指定覆盖，不放入自动升级链占用常规额度。
- **推理深度**：只填写当前 Harness 实际支持的档位（例如 Antigravity 支持 low/medium/high，Codex 支持到 max；超出支持档位会映射或提示受阻）。

例如在 macOS/Linux 查看个人配置（存在时）：

```bash
cat ~/.config/easydev/models.json
```

Windows 路径、字段、模型阶段和实际生效核对统一见 [environment.md](references/environment.md#模型配置)。示例不会自动加载，安装更新不修改个人配置。

## 验证与交付

- 复用充分检查，只为明确覆盖缺口新增行为测试；按项目脚本运行，独立无冲突项并行，依赖项顺序执行。详细规则见 [工程实践](references/engineering.md)。
- quick 报自查；normal 报独立定向审查（含必要的一次局部 P2 复审）；full 按 [流程编排](references/workflow.md#full-编排) 判定闭环。达到预算上限不等于完成，修复尝试以 [尝试预算](references/workflow.md#尝试预算) 为准。
- 缺独立能力时完成授权范围内自查，给可交接审查包并明确缺口；不能称独立审查通过或 full 闭环完成。不同实例继承上下文时披露隔离限制，见 [能力降级与审查包](references/environment.md#能力降级与审查包)。
- audit 按模块或信任边界分片，记录覆盖与跨片接口；正文最多 20 条，全部发现保留附录/审查产物，关键缺陷不会因截断丢失。
- 待审脚本执行与凭证隔离遵守 [审查规程](references/review.md#不可信代码执行)。保留用户改动；提交、推送、发布和对外动作遵循明确授权。

## 详细规则与维护

运行规则以 [SKILL.md](SKILL.md) 和按需引用模块为准，README 不维护第二份细则。

- [角色定义](references/roles.md)：开发与审查角色交接入口
- [流程编排](references/workflow.md)：分流、normal 与 full 编排、两本账与尝试预算
- [工程实践](references/engineering.md)：实现、代码质量判据、测试、排障、重构与验证
- [设计与计划](references/design.md)：设计门控、施工闸、产物规范与多方案候选评审
- [审查规程](references/review.md)：审查原则、强度分档与全仓审计规程
- [环境与安全](references/environment.md)：模型配置、宿主能力降级、基线安全与工作区隔离
- [维护检查](tests/README.md)、[行为规约](tests/behavior.md)、[实验](tests/experiments/README.md)、[版本历史](CHANGELOG.md)

`tests/` 不随运行时安装。离线替身只验证验收机制，不证明真实模型遵守规则；入口字节减少也不等于已证明 token、费用或耗时收益。修改规则时同步相关引用、行为规约及受影响的可执行检查，发布保持版本一致。
