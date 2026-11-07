# EasyDev

当前版本：`2.4.5`

EasyDev 按实际风险组织编码、设计、排障与审查。入口轻量（≤5000 字节），普通 quick 不加载完整编排，详细规则按需读取；主代理默认单写，full 依据配置与模型阶段委派。

## 使用

```text
使用 easydev，为保存按钮修正文案。
使用 easydev normal，为已有 API 增加兼容的筛选参数并检查调用方。
使用 easydev full，修复订单读取的对象所有权校验。
使用 easydev debug，排查重复提交。
使用 easydev review，审查当前改动，先不要修改代码。
```

默认 auto 并在调查后分流。Codex 支持 `$easydev`；各路径为提示词约定，非运行时命令行参数。

| 路径 | 用途 |
| --- | --- |
| quick | 局部明确、低风险：实现、验证、自查 |
| normal | 影响有界且具第二视角信号：实现、验证、独立定向审查（允许至多一次局部 P2 修复与复审），无 full 账本 |
| full | 高风险、重大契约/数据影响或关键影响未明：门控设计、独立审查、有限修复与复审 |
| design / plan | 设计权衡或实施计划，按请求范围交付 |
| debug | 先定位根因；授权修复后按实际风险选档 |
| review / audit | 单次只读审查或大仓分片审计 |
| spike | 最小可行性实验，产物标记 throwaway |

normal 审查任务 diff、直接调用方与相关测试；首轮仅局部 P2 时允许一次修复与定向复审，其余必关项转 full 并继承预算。排除风险后允许降档，但不可自动降级用户显式指定的档位。详见 [流程规则](references/workflow.md#分流判定)。

normal 是实现路径而非聊天长短档：输出长度遵用户要求，深度由 `reasoning_effort` 控制。

## 安装

将以下提示词交给 Agent；更新时同样适用：

```text
请安装或更新 EasyDev：
https://github.com/chenpey/easy_ai/tree/main/easydev

识别当前操作系统和 Agent，将 main 分支的 SKILL.md、references/ 和可选展示元数据 agents/ 安装到用户主目录下的 ~/.agents/skills/easydev，保持相对位置。不要安装测试或其他技能，不改动用户模型配置。

若宿主不识别该目录，在其实际用户级技能目录中创建链接；不支持链接时采用受支持的目录联接或复制。更新前检查已有文件，保留本地修改及自建文件，只替换未被本地修改的旧版文件；处理新版已移除文件时也遵守此限制，冲突先说明。

检查技能和引用文件完整性、链接有效性，报告路径、版本和生效步骤；未验证宿主实际加载时明确说明。优先使用已有工具，避免安装不必要的依赖。
```

模型、沙箱与子代理能力由宿主提供。适配要求与降级见 [环境与能力适配](references/environment.md#宿主适配与能力降级)。

## 模型配置

默认继承宿主（`inherit`，`upgrades` 为空）。主代理在阶段 0 且 developer 为 `inherit` 时可自行写入；显式指定独立模型/深度或进入升级阶段时须委派；宿主无委派能力时暂停并报受阻，经用户明确授权后方可自查。升级阶段暂停并行并串行交接。

自行配置须使用宿主支持的完整模型 ID 与供应商前缀；未识别的前缀会在首次分派时暂停报受阻，见 [模型配置](references/environment.md#模型配置)。跨供应商升级配置示例如下：

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

### 选型参考

- **开发**：示例采用 `deepseek-v4.1-flash`，长任务与常规实现吞吐稳定、高性价比。
- **审查**：示例采用 `gpt-6.1-sol`（`high` 档），代码缺陷分析与漏洞捕获能力强。
- **升级阶段开发**：切换至 `gemini-4-argon`，补足修复攻坚能力。
- **升级阶段审查**：提升至 `max` 推理档，在重试阶段使用更严复审。
- **推理深度**：只填写当前宿主支持的档位（如 Antigravity 支持 low/medium/high，Codex 支持到 max）。

macOS/Linux 查看本地配置（若存在）：

```bash
cat ~/.config/easydev/models.json
```

Windows 路径、字段及生效核对见 [environment.md](references/environment.md#模型配置)。

## 验证与交付

- **测试与验证**：优先复用既有检查，只针对覆盖缺口补写端到端测试；按项目脚本运行，独立项可并行，详见 [工程实践](references/engineering.md)。
- **交付闭环**：quick 交付自查报告；normal 交付独立定向审查报告；full 依门控流程确认闭环，见 [流程编排](references/workflow.md#full-编排)。修复次数受限于 [尝试预算](references/workflow.md#尝试预算)。
- **能力受阻**：缺失独立委派能力时先暂停报错；经授权自查须附交接审查包并标明缺口，不得宣称独立审查通过，详见 [能力降级与审查包](references/environment.md#能力降级与审查包)。
- **分片审计**：audit 按信任边界分片，正文最多 20 条，完整清单落盘任务目录 `findings.md`，规范见 [审查规程](references/review.md#6-报告规则)。
- **安全与权限**：代码执行与凭证隔离遵 [审查规程](references/review.md#不可信代码执行)；保留用户改动，外部动作须明确授权。
- **并行调度**：自判 1–5 片（用户指定优先）；normal 保持单写，full 满足隔离门槛可并写，分片不替代全量检查，见 [并行执行](references/workflow.md#并行执行)。

## 详细规则与维护

规则以 [SKILL.md](SKILL.md) 及 references 为准：

- [角色定义](references/roles.md)：开发与审查角色交接入口
- [流程编排](references/workflow.md)：分流、normal 与 full 编排、两本账与尝试预算
- [工程实践](references/engineering.md)：实现规范、代码质量判据、测试、排障与验证
- [设计与计划](references/design.md)：设计门控、施工闸、产物规范与多方案候选
- [审查规程](references/review.md)：审查原则、强度分档与全仓审计规程
- [环境与安全](references/environment.md)：模型配置、宿主能力降级、基线安全与工作区隔离
- [维护检查](tests/README.md)、[行为规约](tests/behavior.md)、[实验](tests/experiments/README.md)、[折叠评测](tests/experiments/fold-eval.md)（角色调整门槛）、[并行收益观测（可选）](tests/experiments/fold-eval.md#并行收益观测可选)、[版本历史](CHANGELOG.md)

`tests/` 不随运行时安装。修改规则须同步更新引用、行为规约与自动化检查。
