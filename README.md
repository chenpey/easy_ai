# easy_ai

面向 AI Agent 的通用技能与规则配置。

## 技能 (Skills)

| 名称 | 版本 | 说明 |
| --- | --- | --- |
| [EasyThink](easythink/README.md) | 0.5.0 | 理清问题、检查证据、比较选择并形成可验证的下一步。 |
| [EasyDev](easydev/README.md) | 2.4.8 | 面向编码 Agent，按风险组织设计、实施、排障、验证与审查。 |

- 技能按标准 `SKILL.md` 分发，安装与使用见对应目录的 README。
- `agents/openai.yaml` 仅作为 OpenAI/Codex 界面的可选展示元数据，其他宿主可忽略。

## 全局规则 (Agent Rules)

规则以本仓库为唯一源头：

- `AGENTS.md`：通用工作规则，发布至 `~/.config/agent-rules/AGENTS.md`（Pi、Codex、Antigravity 软链共享）。
- `agent-rules/antigravity.md`：Antigravity 专有适配规则，发布至 `~/.gemini/config/rules/antigravity.md`。

### 规则同步

受沙箱权限限制，同步脚本须在宿主终端执行。无需 clone 本地，直接在终端执行即可部署或校验：

#### macOS / Linux (POSIX)

部署与更新：

```bash
curl -fsSL https://raw.githubusercontent.com/chenpey/easy_ai/main/scripts/sync-agent-rules.sh | bash -s -- --apply
```

核对一致性：

```bash
curl -fsSL https://raw.githubusercontent.com/chenpey/easy_ai/main/scripts/sync-agent-rules.sh | bash -s -- --check
```

#### Windows (PowerShell)

部署与更新（需开发者模式或管理员权限）：

```powershell
& ([ScriptBlock]::Create((irm https://raw.githubusercontent.com/chenpey/easy_ai/main/scripts/sync-agent-rules.ps1))) -Apply
```

核对一致性：

```powershell
& ([ScriptBlock]::Create((irm https://raw.githubusercontent.com/chenpey/easy_ai/main/scripts/sync-agent-rules.ps1))) -Check
```

## 本地维护与校验

在已克隆的本地工作区中运行：

```bash
# 优先使用本地工作区规则进行同步或核对
bash scripts/sync-agent-rules.sh --apply
bash scripts/sync-agent-rules.sh --check

# 运行全套自动化门禁（格式、版本、链接、schema 与一致性）
bash scripts/check.sh

# 运行技能安装契约冒烟测试
bash scripts/smoke-install.sh
```
