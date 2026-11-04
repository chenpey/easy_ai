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

默认所有角色继承宿主，无须额外配置。

```json
{
  "developer": {
    "model": "inherit",
    "reasoning_effort": "inherit"
  },
  "reviewer": {
    "model": "inherit",
    "reasoning_effort": "inherit"
  },
  "designer": {
    "model": "inherit",
    "reasoning_effort": "inherit"
  },
  "upgrades": []
}
```

### 效果说明

- **继承宿主**：开发、审查、设计角色均继承当前宿主默认模型与推理深度，不依赖特定供应商或外部模型 ID。
- **不自动升级**：`upgrades` 为空，修复失败重试时不自动切换更高阶模型或推理档位。
- **实例分离**：委派时仍按规则分离不同实例；若宿主无委派能力则自动触发能力降级自查。

来源优先级与详细规范见 [环境与模型配置](references/environment.md#模型配置)。

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
