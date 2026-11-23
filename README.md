# easy_ai

面向 AI Agent 的通用技能与规则配置。

## 技能 (Skills)

| 名称 | 版本 | 说明 |
| --- | --- | --- |
| [EasyThink](easythink/README.md) | 0.7.1 | 理清问题、检查证据、比较选择并形成可验证的下一步。 |
| [EasyDev](easydev/README.md) | 2.14.2 | 面向编码 Agent，按风险组织设计、实施、排障、验证与审查。 |
| [EasyPi](easypi/README.md) | 0.8.0 | 由 Antigravity 设计与编排、分派 Pi 走 EasyDev `gate` 或 `full` 协同分支完成开发与独立审查的跨宿主工作流。 |

- 技能按标准 `SKILL.md` 分发，安装与使用见对应目录的 README。
- `agents/openai.yaml` 仅作为 OpenAI/Codex 界面的可选展示元数据，其他宿主可忽略。

## 全局规则 (Agent Rules)

规则以本仓库为唯一源头：

- `AGENTS.md`：通用工作规则，发布至 `~/.config/agent-rules/AGENTS.md`（Pi、Codex、Antigravity 软链共享）。
- `agent-rules/antigravity.md`：Antigravity 专有适配规则，发布至 `~/.gemini/config/rules/antigravity.md`。
- `agent-hooks/check_command.py`：跨宿主共享的命令与写入硬守卫（非交互 stdin 封闭、高危 Git/删除确认、密钥防写入与防提交、网络命令硬超时、`sleep` 禁令（仅 Antigravity）及敏感凭据目录确认），发布至 `~/.config/agent-hooks/check_command.py`。
- `agent-hooks/antigravity/hooks.json` 与 `agent-hooks/pi/enforce-noninteractive.ts`：Antigravity（`~/.gemini/config/hooks.json`，按命名键合并）与 Pi（`~/.pi/agent/extensions/enforce-noninteractive.ts`）的前置拦截适配器，随 `sync-agent-rules.sh`（macOS/Linux）与 `sync-agent-rules.ps1`（Windows）一并同步。
- `agent-hooks/pi/apply-model-windows.ts` 与 `scripts/models.py`：Pi 会话启动时按用户级 `models.json` 的北京时间 `windows` 应用 `developer` 模型与思考档位的扩展（发布至 `~/.pi/agent/extensions/apply-model-windows.ts`），以及供 `easypi/scripts/run_pi.sh` 使用的模型配置解析器（发布至 `~/.config/agent-hooks/models.py`；该扩展按同一 `windows` 语义独立实现，差异见 `easypi/README.md`），同样随上述同步脚本发布。

### 规则同步

受沙箱权限限制，同步脚本须在宿主终端执行。无需 clone 本地，直接在终端执行即可部署或校验：

#### macOS / Linux (POSIX)

部署与更新：

```bash
curl -fsSL --max-time 30 https://raw.githubusercontent.com/chenpey/easy_ai/main/scripts/sync-agent-rules.sh | bash -s -- --apply
```

核对一致性：

```bash
curl -fsSL --max-time 30 https://raw.githubusercontent.com/chenpey/easy_ai/main/scripts/sync-agent-rules.sh | bash -s -- --check
```

#### Windows (PowerShell)

部署与更新（需开发者模式或管理员权限）：

```powershell
& ([ScriptBlock]::Create((Invoke-RestMethod -TimeoutSec 30 https://raw.githubusercontent.com/chenpey/easy_ai/main/scripts/sync-agent-rules.ps1))) -Apply
```

核对一致性：

```powershell
& ([ScriptBlock]::Create((Invoke-RestMethod -TimeoutSec 30 https://raw.githubusercontent.com/chenpey/easy_ai/main/scripts/sync-agent-rules.ps1))) -Check
```

## 本地维护与校验

在已克隆的本地工作区中运行：

```bash
# 优先使用本地工作区规则进行同步或核对
bash scripts/sync-agent-rules.sh --apply
bash scripts/sync-agent-rules.sh --check

# 运行核心静态门禁（格式、版本、链接、schema、一致性与 hook 单测；CI 中的 pytest 与独立门禁矩阵见 `.github/workflows/checks.yml`）
bash scripts/check.sh

# 运行技能安装契约冒烟测试
bash scripts/smoke-install.sh
```
