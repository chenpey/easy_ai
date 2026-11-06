# easy_ai

面向 AI Agent 的通用技能与规则配置。

## 技能 (Skills)

| 名称 | 版本 | 简单说明 |
| --- | --- | --- |
| [EasyThink](easythink/README.md) | 0.5.0 | 理清问题、检查证据、比较选择并形成可验证的下一步。 |
| [EasyDev](easydev/README.md) | 2.4.4 | 面向编码 Agent，按风险组织设计、实施、排障、验证与审查。 |

- 各技能按标准 `SKILL.md` 分发，安装与使用说明见各技能目录的 README。
- `easydev/agents/openai.yaml` 与 `easythink/agents/openai.yaml` 仅作为 OpenAI/Codex 界面的可选元数据，其他 harness 可忽略。

## 全局规则 (Agent Rules)

仓库文件为规则的唯一维护源：

- `AGENTS.md`：通用工作规则，发布到 `~/.config/agent-rules/AGENTS.md`（Pi、Codex、Antigravity standalone 入口通过软链共享）。
- `agent-rules/antigravity.md`：Antigravity 专有适配规则，发布到 `~/.gemini/config/rules/antigravity.md`。

### 规则同步

因代理沙箱权限限制，发布脚本须在个人终端中执行。无需 clone 代码到本地，在个人终端直接运行即可一键完成首次部署、后续更新与内容核对。

#### macOS / Linux (POSIX)

部署与更新（首次执行会自动创建目录与软链入口，后续执行自动拉取最新规则覆盖更新）：

```bash
curl -fsSL https://raw.githubusercontent.com/chenpey/easy_ai/main/scripts/sync-agent-rules.sh | bash -s -- --apply
```

核对内容一致性：

```bash
curl -fsSL https://raw.githubusercontent.com/chenpey/easy_ai/main/scripts/sync-agent-rules.sh | bash -s -- --check
```

#### Windows (PowerShell)

部署与更新（需开发者模式或管理员权限以建立符号链接）：

```powershell
& ([ScriptBlock]::Create((irm https://raw.githubusercontent.com/chenpey/easy_ai/main/scripts/sync-agent-rules.ps1))) -Apply
```

核对内容一致性：

```powershell
& ([ScriptBlock]::Create((irm https://raw.githubusercontent.com/chenpey/easy_ai/main/scripts/sync-agent-rules.ps1))) -Check
```

> **本地开发说明**：若已 clone 本地仓库，亦可在仓库根目录直接运行 `bash scripts/sync-agent-rules.sh --apply`（POSIX）或 `pwsh -NoProfile -File scripts/sync-agent-rules.ps1 -Apply`（Windows），脚本将优先使用本地工作区文件。
