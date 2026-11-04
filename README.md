# easy_ai

面向 AI Agent 的通用技能与规则配置。

## 技能 (Skills)

| 名称 | 版本 | 简单说明 |
| --- | --- | --- |
| [EasyThink](easythink/README.md) | 0.4.0 | 理清问题、检查证据、比较选择并形成可验证的下一步。 |
| [EasyDev](easydev/README.md) | 1.5.2 | 面向编码 Agent，按风险组织设计、实施、排障、验证与审查。 |

- 各技能按标准 `SKILL.md` 分发，安装与使用说明见各技能目录的 README。
- `easydev/agents/openai.yaml` 与 `easythink/agents/openai.yaml` 仅作为 OpenAI/Codex 界面的可选元数据，其他 harness 可忽略。

## 全局规则 (Agent Rules)

仓库文件为规则的唯一维护源：

- `AGENTS.md`：通用工作规则，发布到 `~/.config/agent-rules/AGENTS.md`（Pi、Codex、Antigravity standalone 入口通过软链共享）。
- `agent-rules/antigravity.md`：Antigravity 专有适配规则，发布到 `~/.gemini/config/rules/antigravity.md`。

### 规则同步

因代理沙箱权限限制，发布脚本须在个人终端中执行。

#### macOS / Linux (POSIX)

首次部署建立软链：

```bash
set -euo pipefail
target="$HOME/.config/agent-rules/AGENTS.md"
mkdir -p ~/.config/agent-rules ~/.pi/agent ~/.codex ~/.gemini/config ~/.gemini/config/rules
for entry in ~/.pi/agent/AGENTS.md ~/.codex/AGENTS.md ~/.gemini/config/AGENTS.md; do
  if [ -L "$entry" ] && [ "$(readlink "$entry")" = "$target" ]; then continue; fi
  if [ -e "$entry" ] || [ -L "$entry" ]; then
    echo "已有非预期入口，请先核对并备份: $entry" >&2
    exit 1
  fi
  ln -s "$target" "$entry"
done
```

发布与核对：

```bash
bash scripts/sync-agent-rules.sh --apply   # 发布到用户目录
bash scripts/sync-agent-rules.sh --check   # 核对内容一致性
```

#### Windows (PowerShell)

首次部署建立软链（需开发者模式或管理员权限）：

```powershell
$target = Join-Path $HOME '.config/agent-rules/AGENTS.md'
foreach ($d in '.config/agent-rules', '.pi/agent', '.codex', '.gemini/config', '.gemini/config/rules') {
    New-Item -ItemType Directory -Path (Join-Path $HOME $d) -Force | Out-Null
}
foreach ($entry in '.pi/agent/AGENTS.md', '.codex/AGENTS.md', '.gemini/config/AGENTS.md') {
    $path = Join-Path $HOME $entry
    $item = Get-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
    if ($null -ne $item) {
        $t = $item.Target
        if ($t -is [array]) { $t = $t[0] }
        if ($item.LinkType -eq 'SymbolicLink' -and ([IO.Path]::GetFullPath($t) -eq [IO.Path]::GetFullPath($target))) { continue }
        throw "已有非预期入口，请先核对并备份: $path"
    }
    New-Item -ItemType SymbolicLink -Path $path -Target $target | Out-Null
}
```

发布与核对：

```powershell
pwsh -NoProfile -File scripts/sync-agent-rules.ps1 -Apply   # 发布到用户目录
pwsh -NoProfile -File scripts/sync-agent-rules.ps1 -Check   # 核对内容一致性
```
