# EasyDev

当前版本：`0.14.1`

EasyDev 是面向编码 Agent 的 Skill，按任务风险组织设计、实现、排障、验证与审查。简单任务直接完成，复杂或高风险任务进入开发—独立审查—修复闭环。

## 使用

直接说明任务，默认由 `auto` 选择流程：

```text
使用 easydev，为登录表单增加邮箱校验并验证错误提示。
使用 easydev debug，排查保存按钮重复提交的问题。
使用 easydev review，审查当前改动，先不要修改代码。
使用 easydev design，比较三个导入方案并推荐一个。
使用 easydev，重构订单解析模块，保持现有行为，合并重复业务规则并验证调用方。
```

Codex 可用 `$easydev`；其他宿主的技能入口以其实际支持为准。以下路径名是提示词约定，不是额外安装的命令。

| 路径 | 用途 |
| --- | --- |
| `auto` | 默认；调查后按实际影响选择流程 |
| `quick` | 局部低风险改动：实现、验证、自查 |
| `full` | 高风险、广泛影响、公共契约、持久数据、调查后仍有实质不确定性，或明确要求闭环：开发、独立审查、修复与复审 |
| `design` | 方案设计与权衡；多个实质方案按需交叉评估 |
| `plan` | 实施步骤、依赖与验证计划 |
| `debug` | 先定位根因；授权修复后按风险进入 `quick` 或 `full` |
| `review` | 只读审查代码、设计或 PR |
| `spike` | 最小可行性实验与结论，实验产物标为 `throwaway` |
| `audit` | 只读审计仓库或目录，按严重度列出最多 20 条发现 |

涉及安全与供应链、鉴权与授权、支付、敏感数据、并发一致性、破坏性数据操作或迁移兼容时至少进入 `full`；这些是高风险边界的下限，无法排除相关影响时不得因清单未列或改动很小而降为 `quick`。文件数量本身不是分流依据。仅要求设计、计划、定位、审查或实验时，按该范围交付，不自动转为实现。

可指定聊天表达为 `normal`、`lite` 或 `ultra`，只压缩文字，不改变推理深度、实现要求或验证标准。

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

## 模型配置

默认开发、审查角色均继承宿主模型与推理深度，无须配置文件。需要分别指定角色时，可参考 [跨模型示例](examples/models.cross-provider.json)，或按下文写法自行配置。示例不会自动加载。

例如，让 DeepSeek 负责开发、GPT 负责独立审查，可将以下内容保存到下述用户外部配置路径：

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
  "upgrades": [
    {
      "developer": {
        "model": "opencode-go/deepseek-v4-pro",
        "reasoning_effort": "high"
      },
      "reviewer": {
        "model": "codex/gpt-6.1-sol",
        "reasoning_effort": "high"
      }
    },
    {
      "developer": {
        "model": "codex/gpt-6.1-sol",
        "reasoning_effort": "high"
      },
      "reviewer": {
        "model": "opencode-go/deepseek-v4-pro",
        "reasoning_effort": "high"
      }
    }
  ]
}
```

`developer` 负责开发与修复，`reviewer` 负责独立审查与复审。此例各角色推理深度均为 `high`，配置依次为：

| 阶段 | 开发模型 | 审查模型 |
| --- | --- | --- |
| 初始 | DeepSeek V4.1 Flash | GPT 6.1 Sol |
| 第一次升级 | DeepSeek V4 Pro | GPT 6.1 Sol |
| 第二次升级 | GPT 6.1 Sol | DeepSeek V4 Pro |

配置 `upgrades` 后，同一确认问题在当前阶段至多试修一次；问题仍在或关闭证据不足时，按数组顺序升级，末级仍失败则停止自动修复。升级不增加审核轮数；模型不可用或网络故障不作为升级理由。不需要升级时设为 `"upgrades": []`。

模型 ID 沿用仓库示例，使用前替换为当前宿主实际支持的完整 ID 和推理深度；宿主还须支持按角色指定模型。

配置按以下优先级选取，不合并：

1. 用户外部配置：macOS/Linux 使用 `~/.config/easydev/models.json`；Windows 使用 `%APPDATA%/easydev/models.json`，变量未设置时回退到主目录下的 `.config/easydev/models.json`。
2. 对话指定的文件，仅在外部配置不存在时使用。
3. 内置默认值：`developer`、`reviewer` 均为 `inherit`，`upgrades` 为空。

macOS/Linux 可用以下命令查看配置：

```bash
cat ~/.config/easydev/models.json
```

若该文件不存在，表示尚未创建个人配置；使用内置默认值时无须创建，需要自定义时再将上面的 JSON 保存到此路径。

`developer`、`reviewer` 各含 `model` 和 `reasoning_effort`；模型 ID 须与宿主支持的完整写法一致，深度单独填写。`upgrades` 最多两个阶段，每阶段包含完整的两个角色配置。

存在的配置必须读取；无效或不可读时暂停相关分派，不静默回退。分派后核对实际模型与深度，不能把请求值当作生效证据。对话直接指定角色模型或深度可局部覆盖配置。完整字段与处理规则见 [模型配置指南](references/models.md)。

## 交付与限制

- 实现保持最小完整，同步受影响调用方与契约；复用已有测试，仅为明确的覆盖缺口新增行为测试。
- 报告改动、实际验证结果和未验证项，区分测试通过、构建通过与验收完成。
- `quick` 只做自查；`full` 需由不同实例独立审查，关闭 P0、P1、范围内 P2 及验收缺口。默认最多 4 轮审核，达到上限不代表完成。
- 无独立子代理能力时可以完成授权实现和自查，但不能声称 `full` 闭环完成。
- 待审仓库的测试、构建、依赖安装和项目脚本按不可信代码处理；仅在断开网络、无宿主凭证、被审目标只读挂载的操作系统沙箱或容器级隔离中执行，系统临时目录不构成安全隔离。
- 保留用户已有改动，沿用已有授权；提交、发布、部署及对外消息遵守用户授权和宿主权限。隔离工作区与清理见 [工作区指南](references/worktree.md)。

## 详细规则与维护

运行规则以 [SKILL.md](SKILL.md) 及其按需引用的 `references/` 为准；README 只提供使用指南。

- [设计与计划](references/planning.md)、[候选方案评审](references/candidates.md)
- [实现与验证](references/execution.md)、[重构方法](references/refactoring.md)、[排障](references/debugging.md)
- [项目验证方法](references/verification.md)：已有测试不足或需复用人工验收方法时，补足真实入口操作、取证与清理
- [独立审查](references/review.md)、[闭环编排与预算](references/orchestration.md)
- [基线与恢复](references/baseline-safety.md)
- [维护检查](tests/README.md)、[行为回归规约](tests/behavior.md)、[实验说明](tests/experiments/README.md)
- [版本历史](CHANGELOG.md)

`tests/` 仅供维护，不是运行依赖。离线替身测试不能证明真实模型行为；当前未接入真实 Agent 行为 CI，也没有当前版本的质量或成本收益结论。

修改运行规则时同步相关模块、行为用例与说明；发布时保持本页版本、`SKILL.md` 的 `metadata.version` 和 `CHANGELOG.md` 最新版本一致。
