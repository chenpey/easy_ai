# EasyThinking

版本：`0.2.7`

帮助你理清问题、检查证据、比较选择，并找到可验证的下一步。简单问题直接回答，复杂问题按需分析，不套固定模板，也不替你做决定。

## 安装

### 方式一：交给 Agent 安装（快捷）

将下面的提示词复制给你使用的 Agent，让它完成安装或更新：

```text
请安装或更新 EasyThinking：
https://github.com/chenpey/easy_ai/tree/main/easythinking

识别当前操作系统和 Agent，将仓库 main 分支的最新运行时技能文件（SKILL.md、references/ 及可选的 agents/，排除 tests/、CHANGELOG.md、README.md）保存到用户主目录下的 ~/.agents/skills/easythinking。
保留 SKILL.md 及其引用文件的相对位置。

如果当前 Agent 支持该通用目录，直接使用；否则在其实际配置的用户级技能目录中创建指向上述目录的软链接，让所有项目都能使用。确认 Agent 能读取该链接；若不支持软链接，可使用受支持的目录联接或复制，并说明采用的方式。

优先使用已有工具，避免安装不必要的依赖。更新前检查已有文件和链接，保留本地修改及自建文件；仅替换确认未被本地修改的旧版技能文件，并处理新版已移除的文件。遇到冲突先说明，不直接覆盖或删除。

完成后检查技能及引用文件是否完整、链接是否有效，告知安装路径、版本和生效步骤。未实际验证 Agent 加载时，请明确说明。
```

### 方式二：手动安装（确定性对照）

如果需要稳定、可复现的安装，可在终端执行以下命令（仅安装运行时所需的 `SKILL.md`、`references/` 与 `agents/`，先在同目录暂存校验成功后再备份替换，避免克隆或复制失败时破坏已安装版本）。各 Agent 的技能目录约定可能随版本变化，请以各自官方文档为准。

#### 1. 通用目录（Codex、Cursor、OpenCode、Pi、DeepSeek Harness 等常见约定）

- **macOS / Linux（bash / zsh）**：
  ```bash
  (
    set -euo pipefail
    tmp="$(mktemp -d)"
    stage="$HOME/.agents/skills/.easythinking.stage.$$"
    trap 'rm -rf "$tmp" "$stage"' EXIT
    git clone --depth=1 https://github.com/chenpey/easy_ai.git "$tmp/repo"
    mkdir -p "$stage"
    cp -R "$tmp/repo/easythinking/SKILL.md" "$tmp/repo/easythinking/references" "$tmp/repo/easythinking/agents" "$stage/"
    dest="$HOME/.agents/skills/easythinking"
    if [ -e "$dest" ]; then
      bak="$HOME/.agents/skills/easythinking.bak.$(date +%s)"
      mv "$dest" "$bak"
      echo "已备份旧版至: $bak"
    fi
    mv "$stage" "$dest"
    echo "已安装至: $dest ($(grep -m1 'version:' "$dest/SKILL.md" | awk '{print $2}'))"
  )
  ```

- **Windows（PowerShell）**：
  ```powershell
  $ErrorActionPreference = "Stop"
  $tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("easy_ai_" + [guid]::NewGuid())
  $skillsRoot = Join-Path $HOME ".agents\skills"
  $stage = Join-Path $skillsRoot (".easythinking.stage." + $PID)
  $dest = Join-Path $skillsRoot "easythinking"
  try {
    git clone --depth=1 https://github.com/chenpey/easy_ai.git "$tmp\repo"
    New-Item -ItemType Directory -Force -Path $stage | Out-Null
    Copy-Item "$tmp\repo\easythinking\SKILL.md" $stage
    Copy-Item -Recurse "$tmp\repo\easythinking\references", "$tmp\repo\easythinking\agents" $stage
    if (Test-Path $dest) {
      $bak = "$dest.bak.$([DateTimeOffset]::UtcNow.ToUnixTimeSeconds())"
      Move-Item $dest $bak
      Write-Host "已备份旧版至: $bak"
    }
    Move-Item $stage $dest
    Write-Host "已安装至: $dest"
  } finally {
    if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp }
    if (Test-Path $stage) { Remove-Item -Recurse -Force $stage }
  }
  ```

#### 2. 专用目录（macOS / Linux 示例）

先完成上方通用目录安装，再按需同步到专用目录：

- **Claude Code**（`~/.claude/skills/`）：
  ```bash
  (
    set -euo pipefail
    src="$HOME/.agents/skills/easythinking"
    dest="$HOME/.claude/skills/easythinking"
    stage="$HOME/.claude/skills/.easythinking.stage.$$"
    trap 'rm -rf "$stage"' EXIT
    test -f "$src/SKILL.md"
    mkdir -p "$stage"
    cp -R "$src/." "$stage/"
    if [ -e "$dest" ]; then
      bak="$HOME/.claude/skills/easythinking.bak.$(date +%s)"
      mv "$dest" "$bak"
      echo "已备份旧版至: $bak"
    fi
    mv "$stage" "$dest"
    echo "已同步至: $dest"
  )
  ```

- **Google Antigravity**（App 与 CLI 用户级目录）：
  ```bash
  (
    set -euo pipefail
    src="$HOME/.agents/skills/easythinking"
    test -f "$src/SKILL.md"
    for parent in "$HOME/.gemini/config/skills" "$HOME/.gemini/antigravity-cli/skills"; do
      dest="$parent/easythinking"
      stage="$parent/.easythinking.stage.$$"
      mkdir -p "$stage"
      cp -R "$src/." "$stage/"
      if [ -e "$dest" ]; then
        bak="$parent/easythinking.bak.$(date +%s)"
        mv "$dest" "$bak"
        echo "已备份旧版至: $bak"
      fi
      mv "$stage" "$dest"
      echo "已同步至: $dest"
    done
  )
  ```

## 使用

直接告诉 agent：

> 用 easythinking 帮我理清这个问题：……

例如：“这个判断靠谱吗？”“这两个方案怎么选？”“我是不是漏了什么关键证据？”“同事总针对我，我是不是想多了？”

也可用快捷指令：Codex 输入 `$easythinking`，Pi 输入 `/skill:easythinking`。

编码、医学、法律、投资等专业问题，可用它辅助梳理思路，但仍需专业验证。涉及重大不可逆决策或现实人身风险时，严格遵守安全优先与风险提示规则。

## 思考方法

![EasyThinking 思考脑图：明确问题、梳理信息、概念与尺度、证据与推理、判断与取舍、行动与更新](assets/easythinking-mindmap.svg)

各分支按需使用，无需每次走完。更多内容见[方法说明](references/methods.md)、[应用示例](references/examples.md)、[行为用例](tests/behavior.md)和[更新日志](CHANGELOG.md)。

## 方法来源与致谢

EasyThinking 的起点来自知乎作者“啥都不明白”的[回答](https://www.zhihu.com/question/9115866072/answer/2096187142615590644)，提炼其关于事实梳理、概念定义和程度澄清的思路。在此基础上，本技能增补了论证结构分解、证据分级、感受与价值保留、安全优先、不可逆高风险表态、多候选解释、风险偏好与承受力区分、检索默认核查及行动反馈闭环。
