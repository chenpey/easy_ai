# easy_ai

本仓库包含 2 个 Skill：

| 名称 | 版本 | 简单说明 |
| --- | --- | --- |
| [EasyThink](easythink/README.md) | 0.4.0 | 理清问题、检查证据、比较选择并形成可验证的下一步。 |
| [EasyDev](easydev/README.md) | 1.4.2 | 面向编码 Agent，按风险组织设计、实施、排障、验证与审查。 |

## Harness 兼容性

`easydev/agents/openai.yaml` 和 `easythink/agents/openai.yaml` 是 OpenAI/Codex 专用的可选界面元数据，用于提供技能显示名称、简介和默认提示词。它们不是 Agent Skills 的必需文件，也不负责其他 harness 的兼容性；其他 harness 可以忽略这些文件。跨 harness 的核心是各技能目录中的 `SKILL.md`（包含规范要求的 `name` 和 `description`）及其引用的随包文件。实际安装和加载能力取决于目标 harness；通过 Skills CLI 安装时，`--agent '*'` 只覆盖该 CLI 当前支持的 harness。

## 全局规则（AGENTS.md）

仓库中的 `AGENTS.md` 与 `agent-rules/antigravity.md` 是规则的唯一维护源；用户目录中的对应文件是显式发布的运行时副本，不在运行时副本上直接维护。发布前两者可以暂时不同，发布成功后应逐字节一致。

通用规则发布到 `~/.config/agent-rules/AGENTS.md`，Pi、Codex 和 Antigravity 的 standalone 入口通过软链共享该文件。Antigravity 专有规则发布到 `~/.gemini/config/rules/antigravity.md`，保留其 YAML frontmatter（`trigger: always_on`）。其他支持 AGENTS.md 的 harness 按其约定加载路径软链到同一文件；不支持 AGENTS.md 的 harness 不在本仓库覆盖范围。

当前代理沙箱不能写入 `~/.config/agent-rules/`，完整发布须由用户在自己的终端显式执行。脚本不请求提权、不绕过沙箱，也不在失败后自动换目录继续发布。内容校验成功只证明安装文件一致，不证明宿主已加载。

规则和技能均只在仓库维护，并在显式发布或更新后进入用户级运行目录：通用规则共享一个发布文件；EasyDev 保留用户手动同步的安装目录，各宿主通过技能入口发现它。两者均不直接跟随仓库工作区变化；技能更新继续遵守 EasyDev 安装说明中的本地修改保护要求，规则同步脚本不更新技能。人工同步技能前可先干跑核对差异：`rsync -rcni easydev/SKILL.md easydev/references easydev/agents ~/.agents/skills/easydev/`（不写入、不删除）。

首次在新机器上部署时，先为三个入口建软链（脚本只负责发布内容，不创建入口）；`scripts/sync-agent-rules.sh` 要求上述三个入口都已存在且指向 `~/.config/agent-rules/AGENTS.md`，入口不是预期软链时会直接非零退出：

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

首次部署按 bootstrap → `--apply` → `--check` 顺序执行：bootstrap 之后目标文件尚不存在，此前的 `--check` 会以非零退出，这是首次部署的预期结果。日常更新用 `--check` → `--apply` → `--check`，先审阅差异再发布。

```bash
cd /path/to/easy_ai
bash scripts/sync-agent-rules.sh --apply   # 首次部署直接发布；日常更新同样用它
bash scripts/sync-agent-rules.sh --check   # 发布后核对内容一致
```

### 平台

`scripts/*.sh` 面向 POSIX（macOS/Linux），发布命令是 `bash scripts/sync-agent-rules.sh --apply` 与 `bash scripts/sync-agent-rules.sh --check`；`--check` 需要 `sha256sum` 或 `shasum` 之一（脚本自动选择，两者都缺时非零退出并提示）。

Windows 使用 `scripts/sync-agent-rules.ps1`，语义相同：默认 `-Check` 只读核对，发布用 `-Apply`，两个开关互斥（同时传入按用法错误退出码 2）。首次部署同样按 bootstrap → `-Apply` → `-Check`。创建入口软链需要开发者模式或管理员权限；若某个 harness 在 Windows 使用不同加载路径，改脚本顶部那三个数组，不要另写一份。

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
pwsh -NoProfile -File scripts/sync-agent-rules.ps1 -Apply
pwsh -NoProfile -File scripts/sync-agent-rules.ps1 -Check
```

PowerShell 脚本目前只有语法门禁（本机 `check.sh` 在装有 `pwsh` 时解析、CI 的 Windows 作业固定解析），运行时行为尚未在 Windows 上实测。
