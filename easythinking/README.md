# EasyThinking

版本：`0.2.2`

EasyThinking 是一个通用思考辅助 Skill，帮助用户澄清问题、区分观察与推断、检查论证与关键证据、比较方案，并提出可验证的下一步。根据陪伴、澄清、检验、归因、决策或冲突等诉求选择方法；简单问题直接回答，复杂问题按需分析，不强制套用固定模板。

它尊重用户的感受、价值排序和风险偏好，不把推断写成事实，也不替用户做决定。可以核实问题中的关键主张与必要前提，不扩展为无关调查或未请求的对外操作。多轮对话只更新受新信息、目标变化或错误纠正影响的部分；不因用户施压而改变事实判断。

编码、医学、法律、投资等专业任务仍使用相应的专业流程，本技能可辅助梳理问题与取舍，但不替代专业验证。出现现实的暴力、胁迫、自伤或紧急医疗风险时，先关注当下安全。

## 思考脑图

各分支按需使用，编号只帮助阅读，不要求每次完整走一遍。

![EasyThinking 思考脑图：明确问题、梳理信息、概念与尺度、证据与推理、判断与取舍、行动与更新](assets/easythinking-mindmap.svg)

[查看可放大的矢量图](assets/easythinking-mindmap.svg)。图的可编辑源文件也是此 SVG，修改文字或布局后即可更新 README 中的展示。

优化重点：梳理信息时保留感受与价值，事实仍须核验；只澄清影响判断的概念，主观等级不默认等距；结论与证据强度相称，并随新证据、目标或约束变化复查。需要细节时，见[方法说明](references/methods.md)和[应用示例](references/examples.md)。

## 支持的 Harness

EasyThinking 是纯文件 Skill，运行时只依赖 `SKILL.md` 与 `references/`，不要求子代理、模型配置、网络或特定工具。能否使用取决于目标 harness 是否能发现并加载 `SKILL.md`。

下面按各宿主官方读取的用户级目录分为两类：通用目录与专用目录。

### 通用目录

这些 harness 官方读取 `~/.agents/skills/`，共用同一份安装。

| Harness | Skills CLI ID | 用户级 | 显式调用 |
| --- | --- | --- | --- |
| Codex | `codex` | `~/.agents/skills/easythinking/` | `$easythinking` |
| OpenCode | `opencode` | `~/.agents/skills/easythinking/` | 以宿主语法为准 |
| Pi | `pi` | `~/.agents/skills/easythinking/` | `/skill:easythinking` |
| Cursor | `cursor` | `~/.agents/skills/easythinking/` | 以宿主语法为准 |
| DeepSeek Harness | 无（不在 Skills CLI 内置列表中） | `~/.agents/skills/easythinking/` | `/easythinking` |

DeepSeek Harness 没有单独安装命令，直接扫描上面这个目录（见下方说明）。

### 专用目录

这些 harness 不读取通用目录，需要安装到各自的官方用户级目录。

| Harness | Skills CLI ID | 用户级 | 显式调用 |
| --- | --- | --- | --- |
| Claude Code | `claude-code` | `~/.claude/skills/easythinking/` | 以宿主语法为准 |
| Google Antigravity App | `antigravity` | `~/.gemini/config/skills/easythinking/`（需按下方说明复制） | `/easythinking` |
| Google Antigravity CLI | `antigravity-cli` | `~/.gemini/antigravity-cli/skills/easythinking/`（需按下方说明复制） | 以宿主语法为准 |

说明：

- **Skills CLI ID** 指 `npx skills add --agent` 可用的标识，已对照当前 Skills CLI 内置定义核对；未列出的 CLI ID 以 CLI 当前版本为准。
- **Claude Code 路径**：官方只读取 `.claude/skills/`，不读取 `.agents/skills/`，所以安装时使用 `--copy` 生成真实副本，避免依赖共享目录或符号链接。
- **Antigravity 路径**：官方 App 文档列出用户级 `~/.gemini/config/skills/<skill-folder>/`；官方 Antigravity CLI 迁移文档列出全局共享 `~/.gemini/antigravity-cli/skills/`。Skills CLI 的 `--global` 会把两者写入 `~/.agents/skills/`，不是官方路径，因此用户级改用复制，见下方“Antigravity 用户级复制”。官方 Antigravity IDE 页面未取到对应路径，IDE 以宿主实际发现的位置为准。
- **DeepSeek Harness 路径**：依据其官方源码 `packages/skill/skill-filesystem/src/index.ts`，用户级扫描根目录为 `$DSH_HOME/skills/`（默认 `~/.dsh/skills/`）和 `$DSH_AGENTS_HOME/skills/`（默认 `~/.agents/skills/`）。因此用户级通用目录中的安装对 DSH 可见。这是对源码的核对，尚未在 DSH 中实测加载。
- **Codex 元数据**：`agents/openai.yaml` 只提供 Codex 的显示名称、简介和默认提示词，是可选文件；其他 harness 可忽略它。
- **检索能力**：需要核实事实时，`SKILL.md` 要求按宿主的检索要求使用可靠来源。宿主没有检索工具时，应说明无法核实，而不是凭记忆下结论。

安装成功或目录存在，不代表宿主已加载该技能，也不代表行为符合预期。当前仓库只验证了安装命令与文件集，尚未在各 harness 上做行为回归；升级宿主后需要重新确认加载情况。

## 安装

前提条件：

- Node.js 22.20.0 或更高版本（`npx` 随 npm 提供）。
- 能访问 GitHub。安装命令直接从仓库拉取，无需先 clone。

以下命令均安装到用户级目录，所有项目可用。

### 通用目录

```bash
npx skills add https://github.com/chenpey/easy_ai/tree/main/easythinking --skill easythinking --agent codex opencode pi cursor --copy --global
```

安装到 `~/.agents/skills/easythinking/`。DeepSeek Harness 没有单独命令，直接扫描该目录。

### 专用目录

#### Claude Code

```bash
npx skills add https://github.com/chenpey/easy_ai/tree/main/easythinking --skill easythinking --agent claude-code --copy --global
```

安装到 `~/.claude/skills/easythinking/`。Claude Code 官方只读取 `.claude/skills/`，因此使用 `--copy` 生成真实副本。

#### Antigravity 用户级复制

Antigravity App 的用户级目录是 `~/.gemini/config/skills/easythinking/`，Antigravity CLI 的是 `~/.gemini/antigravity-cli/skills/easythinking/`。Skills CLI 的 `--global` 会写入 `~/.agents/skills/`，不是这两个官方路径，因此从上面通用目录安装好的副本复制过去。先执行通用目录的用户级命令，再执行下面的复制块：

```bash
(
  set -e
  SRC="$HOME/.agents/skills/easythinking"
  test -f "$SRC/SKILL.md"
  mkdir -p ~/.gemini/config/skills/easythinking ~/.gemini/antigravity-cli/skills/easythinking
  cp -R "$SRC/." ~/.gemini/config/skills/easythinking/          # Antigravity App
  cp -R "$SRC/." ~/.gemini/antigravity-cli/skills/easythinking/  # Antigravity CLI
)
```

只需要其中一个产品时，删去另一个产品对应的目录与 `cp` 行。

更新时先重新执行通用目录的用户级命令，再执行复制块覆盖。

### 更新

更新由 Skills CLI 安装的副本：

```bash
npx skills update easythinking
```

Antigravity 副本需按上方说明重新复制。

### 常见问题

- **`EPERM` 或提示缓存目录含 root 拥有的文件**：`~/.npm` 曾被 `sudo` 写入。执行 `sudo chown -R $(id -u):$(id -g) ~/.npm` 修复；不想改权限时，为本次命令指定其他缓存目录，例如 `NPM_CONFIG_CACHE=/tmp/npm-cache npx skills add ...`。
- **Node 版本过低**：升级到 22.20.0 或更高版本后重试。

### 刷新与生效

安装或更新后，按宿主要求刷新技能，或开启新会话。Pi 可使用 `/reload`。若宿主仍未识别，先核对上方表格中的目录是否与该宿主的实际技能目录一致。

### 查看已安装版本

版本写在 `SKILL.md` 前置元数据的 `metadata.version` 中。以通用目录的用户级路径为例：

```bash
sed -n '/^---$/,/^---$/p' ~/.agents/skills/easythinking/SKILL.md | grep -m1 'version:'
```

发布新版本时，同步更新本 README 的版本号和 `SKILL.md` 前置元数据中的 `metadata.version`。

## 文件

```text
easythinking/
├── README.md
├── SKILL.md
├── agents/
│   └── openai.yaml
├── assets/
│   └── easythinking-mindmap.svg
└── references/
    ├── examples.md
    └── methods.md
```

运行时说明位于 `SKILL.md`；`references/` 提供按需读取的方法和应用示例。`agents/openai.yaml` 的跨 harness 说明见仓库根目录 [README](../README.md#harness-兼容性)。
